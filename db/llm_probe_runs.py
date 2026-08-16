"""P2.5 — per-LLM-probe cost + performance telemetry.

Records one row per probe invocation. Audit-level cost rollup is
computed by aggregating these rows by `audit_run_id` at audit
completion.

Recording is best-effort: if persistence fails, the probe still
returns its result. Telemetry gaps are acceptable; broken probes
are not.

Schema mirrors db/migrations/084_llm_probe_runs.sql; an inline
ensure-table backstop lets hermetic test environments populate
the table without running the migration first.

Wired by:
  - services/audit_run_worker._compute_cost_summary (P2.2 placeholder
    replaced with aggregate_cost_for_run in P2.5)
  - services/llm_providers/orchestrator.py — record_probe_run hook
    fires after each provider call (follow-up PR; touches every
    probe call site)
  - cost-cap checks at orchestrator entry (per-merchant-per-day +
    global)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column, DateTime, Index, Integer, Numeric, Table, Text,
)
from sqlalchemy.dialects.postgresql import UUID

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


# Status values
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_TIMEOUT = "timeout"
STATUS_COST_CAPPED = "cost_capped"
STATUS_MOCK_FALLBACK = "mock_fallback"

VALID_STATUSES = frozenset({
    STATUS_SUCCEEDED, STATUS_FAILED, STATUS_RATE_LIMITED,
    STATUS_TIMEOUT, STATUS_COST_CAPPED, STATUS_MOCK_FALLBACK,
})


llm_probe_runs = Table(
    "llm_probe_runs",
    metadata,
    Column("probe_run_id", UUID(as_uuid=False), primary_key=True),
    Column("provider", Text, nullable=False),
    Column("scan_mode", Text, nullable=False),
    Column("merchant_id", Text, nullable=True),
    Column("audit_run_id", UUID(as_uuid=False), nullable=True),
    Column("latency_ms", Integer, nullable=True),
    Column("input_tokens", Integer, nullable=True),
    Column("output_tokens", Integer, nullable=True),
    Column("cost_usd", Numeric(10, 6), nullable=True),
    Column("status", Text, nullable=False),
    Column("error_message", Text, nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Index(
        "idx_llm_probe_runs_audit_run", "audit_run_id",
    ),
    Index(
        "idx_llm_probe_runs_merchant_day", "merchant_id", "completed_at",
    ),
    Index(
        "idx_llm_probe_runs_completed_at", "completed_at",
    ),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()


_DDL_STATEMENTS = [
    # CREATE TABLE — Postgres-flavored types preserved; SQLite ignores
    # the column types it doesn't recognize (NUMERIC works as REAL,
    # UUID as TEXT). The ensure-table helper tolerates per-statement
    # failures, so partial failures don't poison the whole batch.
    """
    CREATE TABLE IF NOT EXISTS llm_probe_runs (
        probe_run_id        UUID PRIMARY KEY,
        provider            TEXT NOT NULL,
        scan_mode           TEXT NOT NULL,
        merchant_id         TEXT NULL,
        audit_run_id        UUID NULL,
        latency_ms          INTEGER NULL,
        input_tokens        INTEGER NULL,
        output_tokens       INTEGER NULL,
        cost_usd            NUMERIC(10, 6) NULL,
        status              TEXT NOT NULL,
        error_message       TEXT NULL,
        completed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_audit_run "
    "ON llm_probe_runs (audit_run_id) WHERE audit_run_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_merchant_day "
    "ON llm_probe_runs (merchant_id, completed_at) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_llm_probe_runs_completed_at "
    "ON llm_probe_runs (completed_at);",
]


async def ensure_llm_probe_runs_table() -> None:
    """Best-effort ensure-table backstop. Per-statement tolerant
    for the same reason db/merchant_audit_runs.py is — Postgres
    prod runs the .sql migration; the inline DDL covers hermetic
    test environments."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_llm_probe_runs_table",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def compute_cost_usd(
    *,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_per_1k_input_tokens_usd: float,
    cost_per_1k_output_tokens_usd: float,
) -> Optional[Decimal]:
    """Pure helper. Returns Decimal cost or None when token counts
    are missing (caller should record cost_usd=None in that case
    rather than guessing)."""
    if input_tokens is None and output_tokens is None:
        return None
    in_tokens = input_tokens or 0
    out_tokens = output_tokens or 0
    cost = (
        (in_tokens / 1000.0) * cost_per_1k_input_tokens_usd
        + (out_tokens / 1000.0) * cost_per_1k_output_tokens_usd
    )
    # Round to 6 decimals to match Numeric(10, 6).
    return Decimal(str(round(cost, 6)))


async def record_probe_run(
    *,
    provider: str,
    scan_mode: str,
    status: str,
    merchant_id: Optional[str] = None,
    audit_run_id: Optional[str] = None,
    latency_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cost_usd: Optional[Decimal] = None,
    error_message: Optional[str] = None,
) -> Optional[str]:
    """Insert one probe-run telemetry row. Best-effort — returns
    the new probe_run_id on success, None on persistence failure
    (the orchestrator should not fail a probe just because
    telemetry persistence is down).

    Validates `status` against VALID_STATUSES; an unknown status is
    coerced to STATUS_FAILED + logged. Better to record a row
    with a clean status than to lose the telemetry entirely.
    """
    await ensure_llm_probe_runs_table()
    if status not in VALID_STATUSES:
        logger.warning(
            "record_probe_run: unknown status %r coerced to %r",
            status, STATUS_FAILED,
        )
        status = STATUS_FAILED
    probe_run_id = str(uuid.uuid4())
    try:
        await database.execute(
            llm_probe_runs.insert().values(
                probe_run_id=probe_run_id,
                provider=provider,
                scan_mode=scan_mode,
                merchant_id=merchant_id,
                audit_run_id=audit_run_id,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                status=status,
                error_message=(
                    error_message[:1000] if error_message else None
                ),
                completed_at=_now_utc(),
            )
        )
        return probe_run_id
    except Exception as exc:
        logger.warning(
            "record_probe_run failed for provider=%s scan_mode=%s: %s",
            provider, scan_mode, str(exc)[:200],
        )
        return None


async def aggregate_cost_for_run(
    *, audit_run_id: str,
) -> Optional[Dict[str, Any]]:
    """Roll up llm_probe_runs into the cost_summary_jsonb shape.

    Returns:
      {
        "providers": [
          {"provider": "gemini", "calls": 9,
           "input_tokens": 12000, "output_tokens": 4500,
           "cost_usd": 0.045},
          ...
        ],
        "llm_calls": 27,
        "total_input_tokens": 36000,
        "total_output_tokens": 13500,
        "estimated_cost_usd": 0.135,
        "_telemetry_source": "llm_probe_runs",
      }

    Returns None on DB error or when no rows exist for this
    audit_run_id — caller treats None as "no telemetry available"
    and falls back to the placeholder shape from P2.2.
    """
    await ensure_llm_probe_runs_table()
    query = """
        SELECT provider,
               COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost_usd
          FROM llm_probe_runs
         WHERE audit_run_id = :run_id
         GROUP BY provider
         ORDER BY provider
    """
    try:
        rows = await database.fetch_all(query, {"run_id": audit_run_id})
    except Exception as exc:
        logger.warning(
            "aggregate_cost_for_run failed for audit_run_id=%s: %s",
            audit_run_id, str(exc)[:200],
        )
        return None
    if not rows:
        return None
    providers: List[Dict[str, Any]] = []
    total_calls = 0
    total_in = 0
    total_out = 0
    total_cost = Decimal("0")
    for r in rows:
        d = dict(r)
        calls = int(d.get("calls") or 0)
        in_t = int(d.get("input_tokens") or 0)
        out_t = int(d.get("output_tokens") or 0)
        cost = Decimal(str(d.get("cost_usd") or 0))
        providers.append({
            "provider": d.get("provider"),
            "calls": calls,
            "input_tokens": in_t,
            "output_tokens": out_t,
            "cost_usd": float(cost),
        })
        total_calls += calls
        total_in += in_t
        total_out += out_t
        total_cost += cost
    return {
        "providers": providers,
        "llm_calls": total_calls,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "estimated_cost_usd": float(total_cost),
        "_telemetry_source": "llm_probe_runs",
    }


async def cost_today_for_merchant(
    *, merchant_id: str,
) -> Decimal:
    """Sum of cost_usd for this merchant since UTC day boundary.
    Powers the per-merchant-per-day cost cap check the orchestrator
    runs before each probe. Returns Decimal('0') on DB error
    (fail-open — degraded telemetry shouldn't block probes).
    """
    await ensure_llm_probe_runs_table()
    query = """
        SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd
          FROM llm_probe_runs
         WHERE merchant_id = :merchant_id
           AND completed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
    """
    try:
        row = await database.fetch_one(
            query, {"merchant_id": merchant_id},
        )
    except Exception as exc:
        logger.warning(
            "cost_today_for_merchant failed for merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return Decimal("0")
    if row is None:
        return Decimal("0")
    return Decimal(str(dict(row).get("cost_usd") or 0))


async def cost_today_global() -> Decimal:
    """Sum of cost_usd across all merchants since UTC day boundary.
    Powers the global cost cap check. Returns Decimal('0') on DB
    error (same fail-open rationale as cost_today_for_merchant)."""
    await ensure_llm_probe_runs_table()
    query = """
        SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd
          FROM llm_probe_runs
         WHERE completed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
    """
    try:
        row = await database.fetch_one(query)
    except Exception as exc:
        logger.warning(
            "cost_today_global failed: %s", str(exc)[:200],
        )
        return Decimal("0")
    if row is None:
        return Decimal("0")
    return Decimal(str(dict(row).get("cost_usd") or 0))
