"""
Phase C-4 / PR-C: persisted merchant-audit run history.

Replaces the in-memory rate-limit + history dict in
`routes/merchant_audit_routes.py` (which lost state on every restart
and didn't support trend / "audit history" UX). Each row records
ONE invocation of `POST /api/merchant-center/audit/ai-commerce-readiness`.

Lifecycle:
  - `record_audit_run_started(merchant_id, product_keys)` returns a
    `run_id` UUID and inserts a row with `status='running'`.
  - `record_audit_run_completed(run_id, status, ...)` updates the row
    once the audit finishes (succeeded or failed).
  - Both helpers are best-effort: on DB error they log + return None
    rather than raising. The audit pipeline must continue to function
    even if persistence is degraded.

Read helpers:
  - `count_runs_in_window(merchant_id, window_seconds)` — replaces
    the in-memory rate-limit deque.
  - `recent_runs_for_merchant(merchant_id, limit=5)` — drives the
    `GET /api/merchant-center/audit/history` endpoint and the trend
    delta in `merchant_view.tracking`.

The Postgres-shaped DDL also lives in
`db/migrations/072_merchant_audit_runs.sql` for prod deploys; the
ensure-table helper here is the schema_guard-style backstop in case
the migration is missed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    ARRAY,
    Column,
    DateTime,
    Integer,
    Index,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.database import database, metadata

logger = logging.getLogger(__name__)


merchant_audit_runs = Table(
    "merchant_audit_runs",
    metadata,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    Column("merchant_id", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("product_keys", ARRAY(Text), nullable=False),
    Column("verdict_labels", ARRAY(Text), nullable=True),
    Column("visibility_score_avg", Integer, nullable=True),
    Column("attribution_score_avg", Integer, nullable=True),
    Column("category_visibility_score_avg", Integer, nullable=True),
    Column("audited_via_pivota_canonical", ARRAY(Text), nullable=True),
    Column("report_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    Index(
        "idx_merchant_audit_runs_merchant_window",
        "merchant_id",
        "requested_at",
    ),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()


_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_audit_runs (
      run_id                        UUID PRIMARY KEY,
      merchant_id                   TEXT NOT NULL,
      requested_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at                  TIMESTAMPTZ NULL,
      status                        TEXT NOT NULL,
      product_keys                  TEXT[] NOT NULL,
      verdict_labels                TEXT[] NULL,
      visibility_score_avg          INTEGER NULL,
      attribution_score_avg         INTEGER NULL,
      category_visibility_score_avg INTEGER NULL,
      audited_via_pivota_canonical  TEXT[] NULL,
      report_jsonb                  JSONB NULL,
      error_message                 TEXT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_merchant_window "
    "ON merchant_audit_runs (merchant_id, requested_at DESC);",
]


async def ensure_merchant_audit_runs_table() -> None:
    """Best-effort ensure-table helper. Mirrors the schema_guard
    pattern — runs once per process, swallows errors, lets the route
    continue even if DDL fails."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        try:
            for stmt in _DDL_STATEMENTS:
                await database.execute(stmt)
        except Exception as exc:
            logger.warning(
                "ensure_merchant_audit_runs_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_audit_run_started(
    *,
    merchant_id: str,
    product_keys: List[str],
) -> Optional[str]:
    """Insert a row with `status='running'`. Returns the new run_id
    (a UUID string) or None on persistence failure. Audit continues
    either way — the run_id is also used to UPDATE the row at the
    completion step, so a None return short-circuits that update too.
    """
    await ensure_merchant_audit_runs_table()
    run_id = str(uuid.uuid4())
    try:
        await database.execute(
            merchant_audit_runs.insert().values(
                run_id=run_id,
                merchant_id=merchant_id,
                requested_at=_now_utc(),
                status="running",
                product_keys=list(product_keys or []),
            )
        )
        return run_id
    except Exception as exc:
        logger.warning(
            "record_audit_run_started failed for merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return None


async def record_audit_run_completed(
    *,
    run_id: Optional[str],
    status: str,
    verdict_labels: Optional[List[str]] = None,
    visibility_score_avg: Optional[int] = None,
    attribution_score_avg: Optional[int] = None,
    category_visibility_score_avg: Optional[int] = None,
    audited_via_pivota_canonical: Optional[List[str]] = None,
    report_jsonb: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """UPDATE the row inserted at start. No-op when `run_id` is None
    (the start insert was already best-effort and may have failed).

    `report_jsonb` is the full structured `brand_report` so the
    history endpoint can render trend cards without re-running probes.
    """
    if not run_id:
        return
    try:
        values: Dict[str, Any] = {
            "status": status,
            "completed_at": _now_utc(),
        }
        if verdict_labels is not None:
            values["verdict_labels"] = verdict_labels
        if visibility_score_avg is not None:
            values["visibility_score_avg"] = int(visibility_score_avg)
        if attribution_score_avg is not None:
            values["attribution_score_avg"] = int(attribution_score_avg)
        if category_visibility_score_avg is not None:
            values["category_visibility_score_avg"] = int(category_visibility_score_avg)
        if audited_via_pivota_canonical is not None:
            values["audited_via_pivota_canonical"] = list(audited_via_pivota_canonical)
        if report_jsonb is not None:
            values["report_jsonb"] = report_jsonb
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        await database.execute(
            merchant_audit_runs.update()
            .where(merchant_audit_runs.c.run_id == run_id)
            .values(**values)
        )
    except Exception as exc:
        logger.warning(
            "record_audit_run_completed failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )


async def count_runs_in_window(
    *,
    merchant_id: str,
    window_seconds: int,
) -> int:
    """Count audit runs (any status) for this merchant in the trailing
    window. Used by the rate limiter — replaces the in-memory deque.

    Returns 0 on DB error so a degraded DB doesn't lock merchants out
    of auditing entirely. Trade-off: prefers availability over strict
    rate enforcement when persistence is broken; the alternative
    (return high count + 429 everyone) is worse UX.
    """
    await ensure_merchant_audit_runs_table()
    try:
        from sqlalchemy.sql import func, select
        cutoff = datetime.fromtimestamp(
            _now_utc().timestamp() - window_seconds, tz=timezone.utc,
        )
        row = await database.fetch_one(
            select(func.count())
            .select_from(merchant_audit_runs)
            .where(
                merchant_audit_runs.c.merchant_id == merchant_id,
                merchant_audit_runs.c.requested_at >= cutoff,
            )
        )
        if row is None:
            return 0
        # databases lib returns the COUNT in column 0
        for v in row.values() if hasattr(row, "values") else [row[0]]:
            return int(v or 0)
        return 0
    except Exception as exc:
        logger.warning(
            "count_runs_in_window failed for merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return 0


async def recent_runs_for_merchant(
    *,
    merchant_id: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Return the most recent audit runs (any status) for this
    merchant, newest first. Used by `GET /audit/history` and
    `merchant_view.tracking` trend deltas.

    Returns trend-friendly fields only (no full report_jsonb); the
    full report is fetched separately by run_id when needed.
    """
    await ensure_merchant_audit_runs_table()
    try:
        rows = await database.fetch_all(
            merchant_audit_runs.select()
            .where(merchant_audit_runs.c.merchant_id == merchant_id)
            .order_by(merchant_audit_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:
        logger.warning(
            "recent_runs_for_merchant failed for merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return []

    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "requested_at": (
                d["requested_at"].isoformat()
                if isinstance(d.get("requested_at"), datetime)
                else None
            ),
            "completed_at": (
                d["completed_at"].isoformat()
                if isinstance(d.get("completed_at"), datetime)
                else None
            ),
            "status": d.get("status"),
            "product_keys": list(d.get("product_keys") or []),
            "verdict_labels": list(d.get("verdict_labels") or []),
            "visibility_score_avg": d.get("visibility_score_avg"),
            "attribution_score_avg": d.get("attribution_score_avg"),
            "category_visibility_score_avg": d.get("category_visibility_score_avg"),
            "audited_via_pivota_canonical_count": len(
                d.get("audited_via_pivota_canonical") or []
            ),
        })
    return out
