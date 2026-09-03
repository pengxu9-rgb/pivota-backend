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
import os
import json as _json
import uuid
from datetime import datetime, timezone

from db._jsonb_safe import _json_safe
from typing import Any, Dict, List, Optional, Tuple, Mapping


def _decode_jsonb_field(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a JSONB column read result into a dict.

    `databases` + asyncpg with no explicit jsonb codec returns JSONB columns
    as JSON-encoded STRINGS. Code paths downstream (worker reading
    `partial_result_jsonb.launch.audit_mode`, route renderers, etc.) assume
    a dict and silently fall back to defaults when isinstance(dict) fails —
    which silently flips the audit to legacy mode on prod even though the
    integration test (which stores partial_result_jsonb as a real dict in
    its in-memory fake) passes. Memory feedback_test_helper_masking_production_bug
    (PR #355 incident) — same pattern.

    Always returns dict or None; never a string.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _provider_scores_from_report(report_jsonb: Any) -> Optional[Dict[str, int]]:
    """Per-model citation medians (e.g. {"gemini": 18, "chatgpt": 21}) for a run's
    trend point, read from the run's report_jsonb.brand_rollup.citation_by_provider.

    Lets the history trend show per-engine lines without a new column or migration:
    the per-provider scores already ride in each run's stored report. Retroactive —
    works for every historical run. (recent_runs_for_merchant already loads
    report_jsonb; this only decodes the small per-provider slice from it.)
    """
    report = _decode_jsonb_field(report_jsonb)
    if not report:
        return None
    rollup = report.get("brand_rollup")
    cbp = (rollup or {}).get("citation_by_provider") if isinstance(rollup, dict) else None
    if not isinstance(cbp, dict):
        return None
    out: Dict[str, int] = {}
    for provider, entry in cbp.items():
        median = entry.get("median") if isinstance(entry, dict) else None
        if median is None:
            continue
        try:
            out[str(provider)] = int(median)
        except (TypeError, ValueError):
            continue
    return out or None

from sqlalchemy import (
    ARRAY,
    Column,
    select as sa_select,
    DateTime,
    Integer,
    Index,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


merchant_audit_runs = Table(
    "merchant_audit_runs",
    metadata,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    # NULL until a merchant claims the run. An anonymous run is created by
    # the public funnel before anyone has registered; conversion claims the
    # SAME row (never a copy) via claim_audit_run_for_merchant.
    #
    # Read safety: nothing grants on a NULL owner. Every ownership check
    # compares `row.get("merchant_id") != merchant_id` against an id from
    # get_current_merchant, which raises 401 on a falsy claim and so can
    # never itself be None — so None != "<real id>" rejects.
    Column("merchant_id", Text, nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("product_keys", ARRAY(Text), nullable=False),
    Column("verdict_labels", ARRAY(Text), nullable=True),
    Column("visibility_score_avg", Integer, nullable=True),
    Column("attribution_score_avg", Integer, nullable=True),
    Column("category_visibility_score_avg", Integer, nullable=True),
    Column("audited_via_pivota_canonical", ARRAY(Text), nullable=True),
    # P0.2 W3: the canonical entities this run deposited against + per
    # product_key resolution basis (migration 158).
    Column("content_keys", ARRAY(Text), nullable=True),
    Column("content_key_basis", JSONB, nullable=True),
    Column("merchant_claimed_at", DateTime(timezone=True), nullable=True),
    Column("report_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    # P2.1: async lifecycle columns (migration 083). See
    # db/migrations/083_audit_runs_async_lifecycle.sql for the
    # column-level commentary.
    Column("stage", Text, nullable=False, server_default="completed"),
    Column("stage_updated_at", DateTime(timezone=True), nullable=True),
    Column("partial_result_jsonb", JSONB, nullable=True),
    Column("error_jsonb", JSONB, nullable=True),
    Column("cost_summary_jsonb", JSONB, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    Column("requested_by_user_id", Text, nullable=True),
    Column(
        "subject_type", Text, nullable=False, server_default="merchant",
    ),
    Column("claimed_by_worker", Text, nullable=True),
    Column("claimed_until", DateTime(timezone=True), nullable=True),
    Index(
        "idx_merchant_audit_runs_merchant_window",
        "merchant_id",
        "requested_at",
    ),
    extend_existing=True,
)


# P2.1: state machine constants. The valid transition map enforces
# audit runs can't skip stages (queued → discovering, not queued →
# completed) or move backwards. Worker uses this to validate writes.
STAGE_QUEUED = "queued"
STAGE_DISCOVERING = "discovering"
STAGE_PROBING = "probing"
STAGE_SCORING = "scoring"
STAGE_MATERIALIZING = "materializing"
STAGE_VERIFYING = "verifying"
STAGE_COMPLETED = "completed"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"

# Active stages that the worker can pick up. queued is the initial
# state; the rest indicate work is mid-flight (used for stale-lease
# reclaim too).
ACTIVE_STAGES = frozenset({
    STAGE_QUEUED, STAGE_DISCOVERING, STAGE_PROBING,
    STAGE_SCORING, STAGE_MATERIALIZING, STAGE_VERIFYING,
})

TERMINAL_STAGES = frozenset({
    STAGE_COMPLETED, STAGE_FAILED, STAGE_CANCELLED,
})

# Allowed transitions. Worker MUST validate before UPDATE; an invalid
# transition is a bug (worker reading stale state, race condition,
# etc.) and should raise loudly.
VALID_STAGE_TRANSITIONS: dict = {
    STAGE_QUEUED: {STAGE_DISCOVERING, STAGE_FAILED, STAGE_CANCELLED},
    STAGE_DISCOVERING: {STAGE_PROBING, STAGE_FAILED, STAGE_CANCELLED},
    STAGE_PROBING: {STAGE_SCORING, STAGE_FAILED, STAGE_CANCELLED},
    STAGE_SCORING: {STAGE_MATERIALIZING, STAGE_FAILED, STAGE_CANCELLED},
    STAGE_MATERIALIZING: {STAGE_VERIFYING, STAGE_FAILED, STAGE_CANCELLED},
    STAGE_VERIFYING: {STAGE_COMPLETED, STAGE_FAILED, STAGE_CANCELLED},
    # Terminal stages — no transitions out
    STAGE_COMPLETED: set(),
    STAGE_FAILED: set(),
    STAGE_CANCELLED: set(),
}


def is_valid_stage_transition(from_stage: str, to_stage: str) -> bool:
    """Return True iff the transition is in the canonical state
    machine. Worker calls this before UPDATE to catch races early."""
    return to_stage in VALID_STAGE_TRANSITIONS.get(from_stage, set())


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()


_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_audit_runs (
      run_id                        UUID PRIMARY KEY,
      merchant_id                   TEXT NULL,
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
    # P2.1: async lifecycle columns. Schema-guard backstop for
    # environments where db/migrations/083_audit_runs_async_lifecycle.sql
    # hasn't been applied yet. Each ALTER is idempotent
    # (IF NOT EXISTS) so re-running is safe.
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'completed';",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS stage_updated_at TIMESTAMPTZ;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS partial_result_jsonb JSONB;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS error_jsonb JSONB;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS cost_summary_jsonb JSONB;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;",
    # P0.2 W3 (migration 158): canonical entity keys + per-product basis.
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS content_keys TEXT[];",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS content_key_basis JSONB;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS requested_by_user_id TEXT;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL "
    "DEFAULT 'merchant';",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;",
    # C3 (migration 210): anonymous runs. The CREATE TABLE above still
    # declares merchant_id NOT NULL for fresh databases built from this
    # backstop, so the constraint has to be dropped explicitly for any
    # database that already has the table. DROP NOT NULL is idempotent on
    # Postgres and fails-and-skips on SQLite, where create_all builds the
    # table from the model (already nullable) instead.
    "ALTER TABLE merchant_audit_runs "
    "ALTER COLUMN merchant_id DROP NOT NULL;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS merchant_claimed_at TIMESTAMPTZ;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_unclaimed "
    "ON merchant_audit_runs (requested_at DESC) "
    "WHERE merchant_id IS NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_worker_pull "
    "ON merchant_audit_runs (stage, claimed_until, requested_at) "
    "WHERE stage IN ('queued', 'discovering', 'probing', 'scoring', "
    "'materializing', 'verifying');",
    "CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_idempotency "
    "ON merchant_audit_runs (idempotency_key) "
    "WHERE idempotency_key IS NOT NULL "
    "AND stage IN ('queued', 'discovering', 'probing', 'scoring', "
    "'materializing', 'verifying');",
]


async def ensure_merchant_audit_runs_table() -> None:
    """Best-effort ensure-table helper. Mirrors the schema_guard
    pattern — swallows errors and lets the route continue even if DDL
    fails. Memoizes once every statement has succeeded; a pass with a
    failure retries on a later call instead, paced by wall time (see
    db/_ddl_guard.py)."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        # Per-statement tolerance. Postgres prod runs the .sql
        # migrations directly; this inline DDL is only a backstop
        # for hermetic test environments. Some statements (partial
        # indexes with WHERE ... IN, ADD COLUMN IF NOT EXISTS) are
        # Postgres-flavored and fail-and-skip is fine on SQLite.
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_merchant_audit_runs_table",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


SUBJECT_TYPE_PUBLIC_FUNNEL = "public_funnel"

# Where an anonymous funnel run records the domain it is about. NOT a column:
# merchant_audit_runs has no domain field, and adding one for a lane that may
# not survive contact with real traffic is the wrong trade. partial_result_jsonb
# already carries lane-specific payload for the URL wedge.
_FUNNEL_DOMAIN_PATH = ("funnel", "domain")


def funnel_domain_of(row: Mapping[str, Any]) -> Optional[str]:
    """The domain an anonymous funnel run is about, or None."""
    partial = row.get("partial_result_jsonb")
    if isinstance(partial, str):
        try:
            partial = _json.loads(partial)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(partial, Mapping):
        return None
    node: Any = partial
    for key in _FUNNEL_DOMAIN_PATH:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if not isinstance(node, str):
        return None
    # Re-normalize on READ. This value is echoed to anonymous callers and is
    # the authorization key the claim gate compares against, so it must not be
    # trusted just because it came out of our own column. The only writer
    # normalizes on the way in; this makes the read side independently safe.
    from routes.store_audit_public_intake import normalize_store_domain
    return normalize_store_domain(node)


async def record_anonymous_funnel_run(*, domain: str) -> Optional[str]:
    """Start an UNOWNED run for a public-funnel visitor. Returns the run_id.

    Deliberately NOT queued for the worker: `stage` keeps its 'completed'
    server default, so claim_next_pending_run never picks it up. An
    unauthenticated endpoint must not be able to spend model credits, which is
    the whole reason the public tier is deterministic.
    """
    normalized = str(domain or "").strip().lower()
    if not normalized:
        return None
    await ensure_merchant_audit_runs_table()
    run_id = str(uuid.uuid4())
    try:
        await database.execute(
            merchant_audit_runs.insert().values(
                run_id=run_id,
                merchant_id=None,
                requested_at=_now_utc(),
                status="succeeded",
                subject_type=SUBJECT_TYPE_PUBLIC_FUNNEL,
                product_keys=[],
                partial_result_jsonb=_json_safe(
                    {"funnel": {"domain": normalized}}
                ),
            )
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_anonymous_funnel_run failed for domain=%s: %s",
            normalized, str(exc)[:200],
        )
        return None


async def find_unclaimed_funnel_run_for_domain(
    *, domain: str, since: datetime, limit: int = 2000,
) -> Optional[Dict[str, Any]]:
    """The freshest unclaimed funnel run for a domain, or None.

    Matching happens in PYTHON, not SQL: the domain lives inside
    partial_result_jsonb and `->>` is Postgres-only, while this module is
    exercised on SQLite too.

    THE BOUND MUST EXCEED WHAT THE WINDOW CAN HOLD. The candidate set is
    subject_type + unclaimed + a time window, newest first. If `limit` is
    smaller than the number of unclaimed runs inside `since`, an older run
    for the domain falls off the end and the caller mints a DUPLICATE
    instead of reusing — silently, and worse the busier the funnel gets. At
    the intake route's daily cap this bound covers well over the 24h reuse
    window it is called with.
    """
    normalized = str(domain or "").strip().lower()
    if not normalized:
        return None
    await ensure_merchant_audit_runs_table()
    try:
        # Named columns, not select(): this reads a table whose other columns
        # (ARRAY, JSONB) are dialect-specific, and the lookup needs three.
        rows = await database.fetch_all(
            sa_select(
                merchant_audit_runs.c.run_id,
                merchant_audit_runs.c.merchant_id,
                merchant_audit_runs.c.partial_result_jsonb,
                merchant_audit_runs.c.requested_at,
            )
            .where(
                merchant_audit_runs.c.subject_type == SUBJECT_TYPE_PUBLIC_FUNNEL,
            )
            .where(merchant_audit_runs.c.merchant_id.is_(None))
            .where(merchant_audit_runs.c.requested_at >= since)
            .order_by(merchant_audit_runs.c.requested_at.desc())
            .limit(int(limit)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "find_unclaimed_funnel_run_for_domain failed for domain=%s: %s",
            normalized, str(exc)[:200],
        )
        return None
    for row in rows or []:
        d = dict(row)
        if funnel_domain_of(d) == normalized:
            # asyncpg hands a UUID column back as uuid.UUID; SQLite hands back
            # a str. Normalize here so callers cannot compare the two shapes
            # and silently never match on one dialect.
            if d.get("run_id") is not None:
                d["run_id"] = str(d["run_id"])
            return d
    return None


async def claim_audit_run_for_merchant(
    *,
    run_id: str,
    merchant_id: str,
) -> bool:
    """Claim ONE unclaimed run for a merchant. Returns True iff this call
    is the one that claimed it.

    Claim-by-one-UPDATE, the pattern `claim_prospects_for_merchant` already
    uses: the guard lives in the WHERE clause, so two concurrent claims can
    never both succeed and the row is never duplicated. A run that already
    has an owner is NOT re-claimable — `merchant_id IS NULL` is what makes
    this a claim rather than a takeover.
    """
    if not run_id or not merchant_id:
        return False
    owner = str(merchant_id).strip()
    if not owner:
        return False
    await ensure_merchant_audit_runs_table()
    try:
        # Plain UPDATE ... RETURNING rather than the data-modifying CTE the
        # prospect_products claim uses: the CTE form is Postgres-only, and
        # this statement is exercised on SQLite too. The guard is unchanged —
        # it lives in the WHERE clause either way.
        rows = await database.fetch_all(
            """
            UPDATE merchant_audit_runs
               SET merchant_id = :merchant_id,
                   merchant_claimed_at = :now
             WHERE run_id = :run_id
               AND merchant_id IS NULL
            RETURNING run_id
            """,
            {"run_id": str(run_id), "merchant_id": owner, "now": _now_utc()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "claim_audit_run_for_merchant failed for run_id=%s merchant=%s: %s",
            run_id, owner, str(exc)[:200],
        )
        return False
    return len(rows or []) > 0


async def record_audit_run_started(
    *,
    merchant_id: Optional[str],
    product_keys: List[str],
    subject_type: str = "merchant",
) -> Optional[str]:
    """Insert a row with `status='running'`. Returns the new run_id
    (a UUID string) or None on persistence failure. Audit continues
    either way — the run_id is also used to UPDATE the row at the
    completion step, so a None return short-circuits that update too.

    `subject_type` marks the run kind (e.g. "merchant_url" for the free
    URL-audit wedge) so callers can count a specific kind of run.

    C3: `merchant_id` may be None for a run started by the public funnel
    before anyone has registered. An empty string is normalized to None so
    a falsy caller id can never write "" and make the row look claimed to
    a `merchant_id IS NULL` guard.
    """
    await ensure_merchant_audit_runs_table()
    run_id = str(uuid.uuid4())
    owner = str(merchant_id).strip() if merchant_id is not None else ""
    try:
        await database.execute(
            merchant_audit_runs.insert().values(
                run_id=run_id,
                merchant_id=owner or None,
                requested_at=_now_utc(),
                status="running",
                subject_type=subject_type,
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
            # JSONB write boundary — coerce UUID/datetime/Decimal to
            # JSON-safe primitives. Mirrors the upsert_projection /
            # insert_evidence_item defense from PR #477 + #479.
            values["report_jsonb"] = _json_safe(report_jsonb)
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


async def record_audit_run_content_keys(
    *,
    run_id: Optional[str],
    content_keys: List[str],
    content_key_basis: Optional[Dict[str, Any]] = None,
) -> None:
    """P0.2 W3: record which canonical entities (content_keys) this run
    deposited against, plus the per-product_key resolution basis. Best-effort;
    no-op when run_id is None. Additive to the existing product_keys[] column —
    product_keys stays the storefront-shaped key, content_keys is the
    cross-merchant canonical key the index reads."""
    if not run_id:
        return
    try:
        values: Dict[str, Any] = {"content_keys": list(content_keys or [])}
        if content_key_basis is not None:
            values["content_key_basis"] = _json_safe(content_key_basis)
        await database.execute(
            merchant_audit_runs.update()
            .where(merchant_audit_runs.c.run_id == run_id)
            .values(**values)
        )
    except Exception as exc:
        logger.warning(
            "record_audit_run_content_keys failed for run_id=%s: %s",
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


async def audit_status_counts_in_window(*, window_seconds: int) -> Dict[str, int]:
    """Global audit-run status breakdown over the trailing window (ALL merchants).
    {status -> count}. Feeds the W7 audit-health tick. Best-effort — a metrics tick
    must never raise into the scheduler — so returns {} on DB error."""
    await ensure_merchant_audit_runs_table()
    try:
        cutoff = datetime.fromtimestamp(
            _now_utc().timestamp() - window_seconds, tz=timezone.utc,
        )
        rows = await database.fetch_all(
            "SELECT status, COUNT(*) AS n FROM merchant_audit_runs "
            "WHERE requested_at >= :cutoff GROUP BY status",
            {"cutoff": cutoff},
        )
        return {str(r["status"]): int(r["n"] or 0) for r in rows or []}
    except Exception as exc:
        logger.warning("audit_status_counts_in_window failed: %s", str(exc)[:200])
        return {}


# Successful runs carry legacy status='succeeded' (transition_stage keeps the
# old column aligned; nothing writes status='completed'). 'completed' stays in
# the IN-list only for any pre-stage-era rows. Filtering on `stage='completed'`
# instead would be wrong: the stage column was backfilled with DEFAULT
# 'completed' for ALL legacy rows, including failed ones.
COMPLETED_RUN_STATUSES = ("succeeded", "completed")
_COMPLETED_STATUS_SQL = "status IN ({})".format(
    ", ".join(f"'{s}'" for s in COMPLETED_RUN_STATUSES)
)


async def recent_completed_reports(*, limit: int = 100) -> List[Dict[str, Any]]:
    """The most recent COMPLETED runs' report payloads (report_jsonb only), newest
    first — for the W7 brief-outcome scan (honest-failure lives INSIDE a completed
    run, not in run status). Bounded + best-effort; [] on DB error."""
    await ensure_merchant_audit_runs_table()
    try:
        rows = await database.fetch_all(
            "SELECT run_id, report_jsonb FROM merchant_audit_runs "
            f"WHERE {_COMPLETED_STATUS_SQL} AND report_jsonb IS NOT NULL "
            "ORDER BY requested_at DESC LIMIT :lim",
            {"lim": int(limit)},
        )
    except Exception as exc:
        logger.warning("recent_completed_reports failed: %s", str(exc)[:200])
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        d = dict(r)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "report_jsonb": _decode_jsonb_field(d.get("report_jsonb")),
        })
    return out


async def score_history_for_merchant(
    *, merchant_id: str, limit: int = 50, subject_type: str = "merchant"
) -> List[Dict[str, Any]]:
    """Completed audit runs for one merchant, OLDEST-first, with the brand-level
    score columns + report_jsonb (for basis + per-provider extraction) — for the W2
    visibility-tracking series. Best-effort; [] on DB error."""
    await ensure_merchant_audit_runs_table()
    try:
        rows = await database.fetch_all(
            "SELECT run_id, requested_at, visibility_score_avg, attribution_score_avg, "
            "category_visibility_score_avg, report_jsonb "
            "FROM merchant_audit_runs "
            f"WHERE merchant_id = :mid AND subject_type = :st AND {_COMPLETED_STATUS_SQL} "
            "ORDER BY requested_at DESC LIMIT :lim",
            {"mid": merchant_id, "st": subject_type, "lim": int(limit)},
        )
    except Exception as exc:
        logger.warning(
            "score_history_for_merchant failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return []
    out: List[Dict[str, Any]] = []
    for r in reversed(rows or []):  # DESC fetch, reversed → oldest-first for charting
        d = dict(r)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "requested_at": d.get("requested_at"),
            "visibility": d.get("visibility_score_avg"),
            "attribution": d.get("attribution_score_avg"),
            "category_visibility": d.get("category_visibility_score_avg"),
            "report_jsonb": _decode_jsonb_field(d.get("report_jsonb")),
        })
    return out


async def completed_runs_in_window(*, window_seconds: int) -> List[Dict[str, Any]]:
    """Completed runs (merchant_id, run_id, requested_at, report_jsonb) in the
    trailing window, grouped by merchant + newest-first — for the W7 stability
    canary's auto-detect (find any merchant with a same-basis pair inside a tight
    time window). Naturally bounded by the window; best-effort, [] on DB error."""
    await ensure_merchant_audit_runs_table()
    try:
        cutoff = datetime.fromtimestamp(
            _now_utc().timestamp() - window_seconds, tz=timezone.utc,
        )
        rows = await database.fetch_all(
            "SELECT merchant_id, run_id, requested_at, report_jsonb "
            "FROM merchant_audit_runs "
            f"WHERE {_COMPLETED_STATUS_SQL} AND report_jsonb IS NOT NULL "
            "AND requested_at >= :cutoff "
            "ORDER BY merchant_id, requested_at DESC",
            {"cutoff": cutoff},
        )
    except Exception as exc:
        logger.warning("completed_runs_in_window failed: %s", str(exc)[:200])
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        d = dict(r)
        out.append({
            "merchant_id": str(d.get("merchant_id")) if d.get("merchant_id") else None,
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "requested_at": d.get("requested_at"),
            "report_jsonb": _decode_jsonb_field(d.get("report_jsonb")),
        })
    return out


async def recent_completed_reports_for_merchant(
    *, merchant_id: str, limit: int = 2
) -> List[Dict[str, Any]]:
    """The most recent COMPLETED runs' full reports for ONE merchant, newest first
    — for the W7 stability canary (compare the last two same-basis runs). Bounded +
    best-effort; [] on DB error."""
    await ensure_merchant_audit_runs_table()
    try:
        rows = await database.fetch_all(
            "SELECT run_id, report_jsonb FROM merchant_audit_runs "
            f"WHERE merchant_id = :mid AND {_COMPLETED_STATUS_SQL} AND report_jsonb IS NOT NULL "
            "ORDER BY requested_at DESC LIMIT :lim",
            {"mid": merchant_id, "lim": int(limit)},
        )
    except Exception as exc:
        logger.warning(
            "recent_completed_reports_for_merchant failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        d = dict(r)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "report_jsonb": _decode_jsonb_field(d.get("report_jsonb")),
        })
    return out


async def count_runs_for_merchant_by_subject(
    *,
    merchant_id: str,
    subject_type: str,
    since: Optional[datetime] = None,
) -> int:
    """Count this merchant's DELIVERED-or-in-flight audit runs of a given
    `subject_type`. Used by the free URL-audit wedge to enforce its
    per-merchant free allowance (subject_type="merchant_url").

    `since` restricts the count to runs requested at/after that instant.
    The free-allowance gate passes FREE_AUDIT_COUNT_SINCE here so that
    turning the allowance on for the first time does not retroactively
    consume it with runs from the unmetered era (founder decision D3,
    2026-07-22).

    Failed runs are excluded — the billing invariant is "charged (or a free
    credit consumed) iff a report was delivered", so a run that failed must
    not burn the merchant's free allowance. Cancelled runs also carry
    status='failed' (transition_stage maps every non-completed terminal to
    the legacy 'failed' status) so they're excluded by the same filter.
    Runs still 'running' DO count — otherwise N concurrent submissions all
    pass the cap check before any completes.

    Returns 0 on DB error — matches count_runs_in_window's
    availability-over-strictness trade-off (the cost ceiling also depends
    on these rows being persisted at all).
    """
    await ensure_merchant_audit_runs_table()
    try:
        from sqlalchemy.sql import func, select
        conditions = [
            merchant_audit_runs.c.merchant_id == merchant_id,
            merchant_audit_runs.c.subject_type == subject_type,
            merchant_audit_runs.c.status != "failed",
        ]
        if since is not None:
            conditions.append(merchant_audit_runs.c.requested_at >= since)
        row = await database.fetch_one(
            select(func.count())
            .select_from(merchant_audit_runs)
            .where(*conditions)
        )
        if row is None:
            return 0
        for v in (row.values() if hasattr(row, "values") else [row[0]]):
            return int(v or 0)
        return 0
    except Exception as exc:
        logger.warning(
            "count_runs_for_merchant_by_subject failed for merchant_id=%s "
            "subject_type=%s: %s",
            merchant_id, subject_type, str(exc)[:200],
        )
        return 0


def _run_audit_mode(partial_result_jsonb: Any) -> Optional[str]:
    """audit_mode (per_sku | legacy) from a run's launch options. MUST decode the
    JSONB column first — asyncpg returns JSONB as a JSON STRING, not a dict (see
    _decode_jsonb_field), so reading `.get("launch")` on the raw value raises on
    prod → recent_runs_for_merchant's try/except swallows it → the whole history
    collapses to empty and the per_sku trend goes blank (the PR #355 masking
    pattern). Returns None for legacy / missing / unparseable."""
    decoded = _decode_jsonb_field(partial_result_jsonb)
    launch = decoded.get("launch") if isinstance(decoded, dict) else None
    mode = launch.get("audit_mode") if isinstance(launch, dict) else None
    return mode if isinstance(mode, str) else None


async def recent_runs_for_merchant(
    *,
    merchant_id: str,
    limit: int = 5,
    subject_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the most recent audit runs (any status) for this
    merchant, newest first. Used by `GET /audit/history` and
    `merchant_view.tracking` trend deltas.

    `subject_type` (optional) scopes to one run kind — e.g. "merchant"
    (per-SKU catalog audits) or "merchant_url" (the free URL-visibility
    wedge) — so each history surface lists only the runs it can open.
    Omitted → all kinds (back-compat).

    Returns trend-friendly fields only (no full report_jsonb); the
    full report is fetched separately by run_id when needed.
    """
    await ensure_merchant_audit_runs_table()
    try:
        query = merchant_audit_runs.select().where(
            merchant_audit_runs.c.merchant_id == merchant_id
        )
        if subject_type:
            query = query.where(merchant_audit_runs.c.subject_type == subject_type)
        rows = await database.fetch_all(
            query.order_by(merchant_audit_runs.c.requested_at.desc()).limit(limit)
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
            "subject_type": d.get("subject_type"),
            # audit_mode (per_sku | legacy) lives in the launch options; surface it
            # so trend builders can keep per_sku and legacy runs from mixing (their
            # score columns have different semantics). Decodes the JSONB column
            # (asyncpg returns it as a string) — see _run_audit_mode.
            "audit_mode": _run_audit_mode(d.get("partial_result_jsonb")),
            "product_keys": list(d.get("product_keys") or []),
            "verdict_labels": list(d.get("verdict_labels") or []),
            "visibility_score_avg": d.get("visibility_score_avg"),
            "attribution_score_avg": d.get("attribution_score_avg"),
            "category_visibility_score_avg": d.get("category_visibility_score_avg"),
            # Per-engine medians for the per-model trend (Gemini vs ChatGPT over
            # time). Decoded from the run's stored report — no new column.
            "provider_scores": _provider_scores_from_report(d.get("report_jsonb")),
            "audited_via_pivota_canonical_count": len(
                d.get("audited_via_pivota_canonical") or []
            ),
        })
    return out


# =====================================================================
# P2.1 — async lifecycle accessors. Used by:
#   - POST /api/audits          → enqueue_audit_run
#                               + find_in_flight_by_idempotency_key
#   - GET  /api/audits/{id}     → fetch_audit_run_by_id
#   - POST /api/audits/{id}/cancel → cancel_audit_run
#   - audit_run_worker.py       → claim_next_pending_run +
#                                 transition_stage +
#                                 record_partial_result +
#                                 extend_lease +
#                                 release_stale_leases
# =====================================================================

# Default lease length. Worker calls extend_lease() before each long
# stage transition. 600s gives a stage room to run + a bit of buffer
# before another worker steals it as a stale lease.
DEFAULT_LEASE_SECONDS = 600

# How long a lease can sit past its expiry before reclaim runs.
# Prevents a flapping worker from stealing a lease the original holder
# is about to renew.
STALE_LEASE_GRACE_SECONDS = 30

# Absolute-age backstop: a run still status='running' past this many
# seconds, with NO worker holding a live lease, is considered abandoned
# and force-failed by fail_abandoned_runs(). This is the terminal reaper
# release_stale_leases() is NOT — it only re-queues leased rows and can't
# see claimed_until IS NULL orphans (the URL-audit wedge never leases;
# pre-lease-era runs were inserted without a claim). Sized well past the
# longest legitimate run (per-SKU lease is 600s/stage, frontend polls
# 15min max) so a live audit is never reaped.
ABANDONED_RUN_TTL_SECONDS = int(
    os.getenv("AUDIT_RUN_ABANDONED_TTL_S", "1800")
)


async def enqueue_audit_run(
    *,
    merchant_id: str,
    product_keys: List[str],
    subject_type: str = "merchant",
    idempotency_key: Optional[str] = None,
    requested_by_user_id: Optional[str] = None,
    request_options_jsonb: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Insert a row in `stage='queued'` for the worker to pick up.

    Returns the new run_id, or None on persistence failure. Callers
    that need to know whether the returned id is a fresh insert or a
    race-deduped existing row should use `enqueue_audit_run_with_replay`
    below — this wrapper preserves the historical single-return
    signature for back-compat (idempotency_key=None callers, the
    legacy `/ai-commerce-readiness` fallback, tests, etc.).
    """
    run_id, _was_existing = await enqueue_audit_run_with_replay(
        merchant_id=merchant_id,
        product_keys=product_keys,
        subject_type=subject_type,
        idempotency_key=idempotency_key,
        requested_by_user_id=requested_by_user_id,
        request_options_jsonb=request_options_jsonb,
    )
    return run_id


async def enqueue_audit_run_with_replay(
    *,
    merchant_id: Optional[str],
    product_keys: List[str],
    subject_type: str = "merchant",
    idempotency_key: Optional[str] = None,
    requested_by_user_id: Optional[str] = None,
    request_options_jsonb: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], bool]:
    """Insert + DB-enforced idempotency dedupe (P0-3).

    Returns `(run_id, was_existing)`:
      - On a fresh insert: (new_run_id, False)
      - On a unique-key conflict (concurrent POST raced past
        find_in_flight_by_idempotency_key, OR the caller didn't
        pre-check): (existing_run_id, True). Caller should treat
        was_existing=True as "this is effectively an idempotent
        replay" and surface it to the client.
      - On persistence failure: (None, False).

    Backed by `uniq_merchant_audit_runs_active_idempotency_key`
    (partial UNIQUE index on `idempotency_key` where the row is in
    an active stage). The DB-level uniqueness closes the check-then-
    insert race that the route-layer find_in_flight could not.
    """
    # C3: same "" -> NULL normalization as record_audit_run_started, so this
    # path cannot mint the other orphan class — a row that is unowned AND
    # permanently unclaimable, because `merchant_id IS NULL` never matches "".
    owner = str(merchant_id).strip() if merchant_id is not None else ""
    merchant_id = owner or None

    # An idempotency key is MEANINGLESS without an owner, and silently
    # proceeding would be worse than refusing: Postgres unique indexes are
    # NULLS DISTINCT, so uniq_merchant_audit_runs_active_idempotency_key
    # (merchant_id, idempotency_key) does not constrain NULL-owner rows at
    # all — measured on PG 15: three inserts of the same key all landed,
    # where a real owner deduped to one. ON CONFLICT DO NOTHING never fires,
    # and the docstring's "the DB-level uniqueness closes the check-then-
    # insert race" stops being true for exactly this row class.
    #
    # Unreachable today (services/idempotency.py raises on a falsy id, so no
    # such key can be derived), and this keeps it that way rather than
    # leaving an unauthenticated, cost-bearing insert path one wiring slice
    # from silently duplicating runs.
    if idempotency_key and not merchant_id:
        raise ValueError(
            "merchant_id is required to use an idempotency key: the unique "
            "index is NULLS DISTINCT and would not dedupe an unowned run"
        )

    await ensure_merchant_audit_runs_table()
    run_id = str(uuid.uuid4())
    now = _now_utc()
    # Single line so the _json_safe coercion is visible on the assignment (the
    # jsonb-write meta-invariant is a line-grep; the value flows into .values()
    # below already coerced).
    partial_result_jsonb = _json_safe(request_options_jsonb) if isinstance(request_options_jsonb, dict) else None

    # If no idempotency_key, fall through to the original behavior —
    # no dedupe possible.
    if not idempotency_key:
        try:
            await database.execute(
                merchant_audit_runs.insert().values(
                    run_id=run_id,
                    merchant_id=merchant_id,
                    requested_at=now,
                    status="running",
                    stage=STAGE_QUEUED,
                    stage_updated_at=now,
                    product_keys=list(product_keys or []),
                    subject_type=subject_type,
                    idempotency_key=None,
                    requested_by_user_id=requested_by_user_id,
                    partial_result_jsonb=partial_result_jsonb,
                )
            )
            return run_id, False
        except Exception as exc:
            logger.warning(
                "enqueue_audit_run failed for merchant_id=%s: %s",
                merchant_id, str(exc)[:200],
            )
            return None, False

    # idempotency_key present — use INSERT ... ON CONFLICT DO NOTHING
    # against the partial unique index. Partial unique indexes are not
    # named constraints in Postgres, so this must use conflict
    # inference with the same columns + predicate as migration 144.
    try:
        inserted = await database.fetch_one(
            """
            INSERT INTO merchant_audit_runs (
                run_id, merchant_id, requested_at, status,
                stage, stage_updated_at, product_keys,
                subject_type, idempotency_key, requested_by_user_id,
                partial_result_jsonb
            ) VALUES (
                :run_id, :merchant_id, :now, 'running',
                :stage_queued, :now, CAST(:product_keys AS TEXT[]),
                :subject_type, :idempotency_key, :requested_by_user_id,
                CAST(:partial_result_jsonb AS JSONB)
            )
            ON CONFLICT (merchant_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
              AND stage = ANY(ARRAY[
                'queued'::text, 'discovering'::text, 'probing'::text,
                'scoring'::text, 'materializing'::text, 'verifying'::text
              ])
            DO NOTHING
            RETURNING run_id
            """,
            {
                "run_id": run_id,
                "merchant_id": merchant_id,
                "now": now,
                "stage_queued": STAGE_QUEUED,
                "product_keys": list(product_keys or []),
                "subject_type": subject_type,
                "idempotency_key": idempotency_key,
                "requested_by_user_id": requested_by_user_id,
                "partial_result_jsonb": json.dumps(partial_result_jsonb or {}),
            },
        )
    except Exception:
        logger.exception(
            "enqueue_audit_run on-conflict path failed; refusing "
            "plain INSERT because that silently disables audit "
            "idempotency. merchant_id=%s",
            merchant_id,
        )
        raise

    if inserted is not None:
        return str(inserted["run_id"]), False

    # ON CONFLICT fired — the row with this idempotency_key already
    # exists in an active stage. Fetch its run_id and signal replay.
    existing = await find_in_flight_by_idempotency_key(
        idempotency_key=idempotency_key,
    )
    if existing:
        return existing, True

    # Pathological case: conflict fired but the existing row was
    # cancelled / completed between conflict and the find. Surface as
    # a failure so the caller can decide (retry, fail, etc.) rather
    # than silently lose the request.
    logger.warning(
        "enqueue_audit_run ON CONFLICT fired but no in-flight row "
        "found for idempotency_key — race with terminal transition. "
        "merchant_id=%s", merchant_id,
    )
    return None, False


async def find_in_flight_by_idempotency_key(
    *, idempotency_key: str,
) -> Optional[str]:
    """Return the run_id of any in-flight run matching this
    idempotency_key, or None. POST /api/audits calls this before
    enqueue_audit_run to dedupe burst submits.
    """
    if not idempotency_key:
        return None
    await ensure_merchant_audit_runs_table()
    try:
        from sqlalchemy.sql import select
        row = await database.fetch_one(
            select(merchant_audit_runs.c.run_id)
            .where(
                merchant_audit_runs.c.idempotency_key == idempotency_key,
                merchant_audit_runs.c.stage.in_(list(ACTIVE_STAGES)),
            )
            .limit(1)
        )
        if row is None:
            return None
        return str(row[0])
    except Exception as exc:
        logger.warning(
            "find_in_flight_by_idempotency_key failed: %s",
            str(exc)[:200],
        )
        return None


async def claim_next_pending_run(
    *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest queued or stale-leased run for
    this worker. Returns the claimed row dict, or None if nothing
    to claim.

    Uses a CTE-style UPDATE ... WHERE run_id = (SELECT ... FOR
    UPDATE SKIP LOCKED LIMIT 1) RETURNING. SKIP LOCKED is what
    makes this safe under multiple worker processes.
    """
    await ensure_merchant_audit_runs_table()
    now = _now_utc()
    new_until = datetime.fromtimestamp(
        now.timestamp() + lease_seconds, tz=timezone.utc,
    )
    # Raw SQL — SQLAlchemy core doesn't have a clean CTE+UPDATE+
    # RETURNING composer, and this is hot-path worker code that
    # benefits from being explicit.
    query = """
        UPDATE merchant_audit_runs
           SET claimed_by_worker = :worker_id,
               claimed_until     = :new_until,
               stage_updated_at  = :now
         WHERE run_id = (
             SELECT run_id
               FROM merchant_audit_runs
              WHERE stage IN (
                  'queued', 'discovering', 'probing', 'scoring',
                  'materializing', 'verifying'
              )
                AND (
                    claimed_until IS NULL
                 OR claimed_until < :now
                )
                AND cancelled_at IS NULL
              ORDER BY requested_at ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
        RETURNING run_id, merchant_id, stage, product_keys,
                  subject_type, idempotency_key,
                  requested_by_user_id, partial_result_jsonb,
                  requested_at
    """
    try:
        row = await database.fetch_one(
            query,
            {"worker_id": worker_id, "new_until": new_until, "now": now},
        )
        if row is None:
            return None
        d = dict(row)
        return {
            "run_id": str(d.get("run_id")),
            "merchant_id": d.get("merchant_id"),
            "stage": d.get("stage"),
            "product_keys": list(d.get("product_keys") or []),
            "subject_type": d.get("subject_type"),
            "idempotency_key": d.get("idempotency_key"),
            "requested_by_user_id": d.get("requested_by_user_id"),
            "partial_result_jsonb": _decode_jsonb_field(d.get("partial_result_jsonb")),
            "requested_at": (
                d["requested_at"].isoformat()
                if isinstance(d.get("requested_at"), datetime)
                else None
            ),
        }
    except Exception as exc:
        logger.warning(
            "claim_next_pending_run failed for worker_id=%s: %s",
            worker_id, str(exc)[:200],
        )
        return None


async def transition_stage(
    *,
    run_id: str,
    from_stage: str,
    to_stage: str,
    worker_id: str,
    error_jsonb: Optional[Dict[str, Any]] = None,
    cost_summary_jsonb: Optional[Dict[str, Any]] = None,
    completed_at_if_terminal: bool = True,
) -> bool:
    """Atomic stage transition guarded by from_stage AND
    claimed_by_worker. Returns True on success; False means another
    worker stole the lease, the row was cancelled, or the from_stage
    was wrong (caller should reload + decide).

    Validates the transition against VALID_STAGE_TRANSITIONS first.
    """
    if not is_valid_stage_transition(from_stage, to_stage):
        logger.error(
            "transition_stage rejected invalid transition "
            "%s → %s for run_id=%s",
            from_stage, to_stage, run_id,
        )
        return False
    await ensure_merchant_audit_runs_table()
    now = _now_utc()
    values: Dict[str, Any] = {
        "stage": to_stage,
        "stage_updated_at": now,
    }
    if to_stage in TERMINAL_STAGES and completed_at_if_terminal:
        values["completed_at"] = now
        # Keep legacy `status` aligned for old readers during the
        # dual-key window. Phase 4 cleanup will drop this.
        values["status"] = (
            "succeeded" if to_stage == STAGE_COMPLETED else "failed"
        )
    # JSONB write boundary — coerce UUID/datetime/Decimal here too.
    if error_jsonb is not None:
        values["error_jsonb"] = _json_safe(error_jsonb)
        if to_stage == STAGE_FAILED:
            # Mirror short message into legacy column for old UIs.
            msg = error_jsonb.get("message") if isinstance(error_jsonb, dict) else None
            if isinstance(msg, str):
                values["error_message"] = msg[:2000]
    if cost_summary_jsonb is not None:
        values["cost_summary_jsonb"] = _json_safe(cost_summary_jsonb)
    # Cancellation contract:
    #   - For non-cancellation transitions, `cancelled_at IS NULL` is
    #     enforced so a cancel that landed mid-stage blocks further
    #     forward progress.
    #   - For the finalize-to-cancelled transition, we MUST allow
    #     `cancelled_at IS NOT NULL` — otherwise the worker can never
    #     transition the run to STAGE_CANCELLED after cancel_audit_run
    #     has set the flag, and the run sits forever in its active
    #     stage. This was the deadlock that motivated the fix.
    where_clauses = [
        merchant_audit_runs.c.run_id == run_id,
        merchant_audit_runs.c.stage == from_stage,
        merchant_audit_runs.c.claimed_by_worker == worker_id,
    ]
    if to_stage != STAGE_CANCELLED:
        where_clauses.append(
            merchant_audit_runs.c.cancelled_at.is_(None),
        )
    try:
        result = await database.execute(
            merchant_audit_runs.update()
            .where(*where_clauses)
            .values(**values)
        )
        # databases lib returns rowcount as the result for UPDATE;
        # if the WHERE didn't match, the transition was rejected.
        if isinstance(result, int):
            return result > 0
        # Some backends return None — assume success and let the
        # next read catch a problem.
        return True
    except Exception as exc:
        logger.warning(
            "transition_stage failed for run_id=%s (%s→%s): %s",
            run_id, from_stage, to_stage, str(exc)[:200],
        )
        return False


async def persist_report_jsonb(
    *,
    run_id: str,
    worker_id: str,
    report_jsonb: Dict[str, Any],
) -> bool:
    """Persist ONLY the report_jsonb column mid-run, without touching status,
    stage, or the aggregate score columns (those are set later by
    record_audit_run_completed). Also bumps stage_updated_at (observability
    only — the reaper keys off claimed_until, not this field).

    Needed by the materializing stage: executor agents are enqueued there but
    the executor_run_worker re-fetches report_jsonb from this row at claim time
    (it isn't stored inline in the queue). Since report_jsonb otherwise only
    lands during the verifying stage — AFTER dispatch — the worker could claim
    an enqueued run and read a NULL report, silently skipping (e.g. the
    content-brief agent). Writing it here, before dispatch enqueues any row,
    closes that race. Guarded on worker ownership + not-cancelled. Best-effort.
    """
    await ensure_merchant_audit_runs_table()
    query = """
        UPDATE merchant_audit_runs
           SET report_jsonb     = CAST(:report AS JSONB),
               stage_updated_at = :now
         WHERE run_id = :run_id
           AND claimed_by_worker = :worker_id
           AND cancelled_at IS NULL
    """
    try:
        await database.execute(
            query,
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "report": json.dumps(_json_safe(report_jsonb or {})),
                "now": _now_utc(),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "persist_report_jsonb failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return False


async def record_partial_result(
    *,
    run_id: str,
    worker_id: str,
    partial_result_jsonb: Dict[str, Any],
) -> bool:
    """Merge a stage's partial output into partial_result_jsonb.
    Worker calls this at the END of each stage so GET /audits/{id}
    can render progressive UI. Guarded on worker ownership.

    The merge is shallow — caller is expected to namespace by stage
    (e.g., {"discovering": {...}, "probing": {...}}).
    """
    await ensure_merchant_audit_runs_table()
    # JSONB || JSONB merges shallowly in Postgres. Run as raw SQL
    # to avoid round-tripping the existing JSON to Python.
    #
    # IMPORTANT: use CAST(:patch AS JSONB), NOT :patch::jsonb.
    # SQLAlchemy's text() parameter parser treats `:` as bind-param
    # introducer, so `:patch::jsonb` is read as one parameter name
    # 'patch:jsonb', not as :patch + ::jsonb cast. The bound value
    # then mismatches and SQLAlchemy raises:
    #   This text() construct doesn't define a bound parameter
    #   named 'patch'
    # Every other JSONB call site in the codebase uses CAST(...).
    query = """
        UPDATE merchant_audit_runs
           SET partial_result_jsonb = COALESCE(partial_result_jsonb, '{}'::jsonb)
                                     || CAST(:patch AS JSONB),
               stage_updated_at     = :now
         WHERE run_id = :run_id
           AND claimed_by_worker = :worker_id
           AND cancelled_at IS NULL
    """
    try:
        await database.execute(
            query,
            {
                "run_id": run_id,
                "worker_id": worker_id,
                # _json_safe before json.dumps so UUID/datetime/Decimal
                # in the patch (e.g., canonical_evidence summary with
                # asyncpg-typed values) don't blow up serialization.
                "patch": json.dumps(_json_safe(partial_result_jsonb or {})),
                "now": _now_utc(),
            },
        )
        return True
    except Exception as exc:
        logger.warning(
            "record_partial_result failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return False


async def extend_lease(
    *, run_id: str, worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Push claimed_until forward. Worker calls this before any
    stage that may run longer than DEFAULT_LEASE_SECONDS so a
    sibling worker doesn't reclaim the lease mid-stage.

    P5.8.6: returns False when rowcount==0. The WHERE clause filters
    on `claimed_by_worker == worker_id` so if the lease has already
    been stolen (reaper released + sibling reclaimed), the UPDATE
    matches no rows. The original code returned True unconditionally,
    so the worker would keep executing thinking it owned the lease,
    then ALL subsequent guarded UPDATEs (record_partial_result,
    transition_stage) would silently no-op and the worker's progress
    would vanish. Now the worker has a signal to abort early.
    """
    await ensure_merchant_audit_runs_table()
    new_until = datetime.fromtimestamp(
        _now_utc().timestamp() + lease_seconds, tz=timezone.utc,
    )
    try:
        result = await database.execute(
            merchant_audit_runs.update()
            .where(
                merchant_audit_runs.c.run_id == run_id,
                merchant_audit_runs.c.claimed_by_worker == worker_id,
            )
            .values(claimed_until=new_until)
        )
        # `databases` returns rowcount as the result for UPDATE on
        # Postgres; some backends return None. Treat int==0 as lost-
        # lease; treat None or int>0 as held.
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:
        logger.warning(
            "extend_lease failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return False


async def cancel_audit_run(*, run_id: str) -> bool:
    """Mark a run cancelled.

    Two paths:
      1. **Queued run, no worker has done any stage work yet** —
         atomically finalize to STAGE_CANCELLED. claim_next_pending_run
         excludes cancelled rows, so otherwise a queued run that gets
         cancelled would never be claimed and would sit at stage=queued
         forever.
      2. **Active run (stage in {discovering…verifying}, worker has the
         lease)** — set `cancelled_at` so the worker sees the flag at
         its next stage boundary and calls transition_stage(...
         to_stage=STAGE_CANCELLED) to finalize. transition_stage
         specifically allows the cancelled→cancelled transition; see
         the cancellation contract above.

    Returning True does NOT guarantee the row is already at
    STAGE_CANCELLED — for path 2 it's still active until the worker
    bails. Callers should poll if they need certainty.
    """
    await ensure_merchant_audit_runs_table()
    now = _now_utc()
    try:
        # Path 1: queued runs finalize directly. Race-safe because the
        # UPDATE filters on stage=queued; a worker that just claimed
        # the row but hasn't transitioned yet will see stage=cancelled
        # on its next transition_stage call and bail.
        queued_result = await database.execute(
            merchant_audit_runs.update()
            .where(
                merchant_audit_runs.c.run_id == run_id,
                merchant_audit_runs.c.stage == STAGE_QUEUED,
                merchant_audit_runs.c.cancelled_at.is_(None),
            )
            .values(
                stage=STAGE_CANCELLED,
                stage_updated_at=now,
                cancelled_at=now,
                completed_at=now,
            )
        )
        if isinstance(queued_result, int) and queued_result > 0:
            return True

        # Path 2: active run — flag cancelled_at; worker finalizes.
        await database.execute(
            merchant_audit_runs.update()
            .where(
                merchant_audit_runs.c.run_id == run_id,
                merchant_audit_runs.c.stage.in_(list(ACTIVE_STAGES)),
                merchant_audit_runs.c.cancelled_at.is_(None),
            )
            .values(cancelled_at=now)
        )
        return True
    except Exception as exc:
        logger.warning(
            "cancel_audit_run failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return False


async def fetch_audit_run_by_id(
    *, run_id: str,
) -> Optional[Dict[str, Any]]:
    """Read a single run for GET /api/audits/{id}. Returns the new
    canonical shape: stage + partial_result + cost_summary + error
    + the report payload when the run has reached completed.
    """
    await ensure_merchant_audit_runs_table()
    try:
        row = await database.fetch_one(
            merchant_audit_runs.select().where(
                merchant_audit_runs.c.run_id == run_id,
            )
        )
    except Exception as exc:
        logger.warning(
            "fetch_audit_run_by_id failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    d = dict(row)
    return {
        "run_id": str(d.get("run_id")) if d.get("run_id") else None,
        "merchant_id": d.get("merchant_id"),
        "subject_type": d.get("subject_type"),
        "status": d.get("status"),
        "stage": d.get("stage"),
        "stage_updated_at": (
            d["stage_updated_at"].isoformat()
            if isinstance(d.get("stage_updated_at"), datetime)
            else None
        ),
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
        "cancelled_at": (
            d["cancelled_at"].isoformat()
            if isinstance(d.get("cancelled_at"), datetime)
            else None
        ),
        "product_keys": list(d.get("product_keys") or []),
        "verdict_labels": list(d.get("verdict_labels") or []),
        "visibility_score_avg": d.get("visibility_score_avg"),
        "attribution_score_avg": d.get("attribution_score_avg"),
        "category_visibility_score_avg": d.get("category_visibility_score_avg"),
        "audited_via_pivota_canonical": list(
            d.get("audited_via_pivota_canonical") or []
        ),
        "partial_result_jsonb": _decode_jsonb_field(d.get("partial_result_jsonb")),
        "report_jsonb": _decode_jsonb_field(d.get("report_jsonb")),
        "cost_summary_jsonb": _decode_jsonb_field(d.get("cost_summary_jsonb")),
        "error_jsonb": _decode_jsonb_field(d.get("error_jsonb")),
        "error_message": d.get("error_message"),
        "idempotency_key": d.get("idempotency_key"),
        "requested_by_user_id": d.get("requested_by_user_id"),
    }


async def release_stale_leases(
    *, grace_seconds: int = STALE_LEASE_GRACE_SECONDS,
) -> int:
    """Reclaim leases that are past expiry + grace. Returns the
    count released. Background reaper calls this on its own cadence;
    claim_next_pending_run also tolerates stale leases inline so
    the reaper is a backstop, not the primary path.
    """
    await ensure_merchant_audit_runs_table()
    now = _now_utc()
    cutoff = datetime.fromtimestamp(
        now.timestamp() - grace_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE merchant_audit_runs
           SET claimed_by_worker = NULL,
               claimed_until     = NULL
         WHERE claimed_until IS NOT NULL
           AND claimed_until < :cutoff
           AND stage IN (
               'queued', 'discovering', 'probing', 'scoring',
               'materializing', 'verifying'
           )
    """
    try:
        result = await database.execute(query, {"cutoff": cutoff})
        if isinstance(result, int):
            return result
        return 0
    except Exception as exc:
        logger.warning(
            "release_stale_leases failed: %s", str(exc)[:200],
        )
        return 0


async def fail_abandoned_runs(
    *, ttl_seconds: int = ABANDONED_RUN_TTL_SECONDS,
) -> List[Dict[str, Any]]:
    """Terminal backstop reaper: force-fail any run still `status='running'`
    past an absolute age when NO worker holds a live lease. Returns the reaped
    rows (run_id, merchant_id, partial_result_jsonb) so the caller can refund
    each run's launch debit — a reaped run delivered nothing, and the billing
    invariant is "charged iff delivered".

    Distinct from release_stale_leases(): that one only NULLs an expired lease
    so a sibling worker re-claims and RE-RUNS the durable per-SKU pipeline —
    and it matches only rows with `claimed_until IS NOT NULL`. Two orphan
    classes slip through it entirely and sit `running` forever:

      1. URL-audit wedge runs (subject_type='merchant_url'): launched as a
         bare asyncio task, never enqueued/leased, so `claimed_until IS NULL`.
         If the task dies (or the web dyno restarts) the row never reaches a
         terminal status — the GET endpoint's stale-check only fires if the
         merchant happens to keep polling.
      2. Pre-lease-era durable runs inserted with no claim.

    The `claimed_until IS NULL OR claimed_until < now` guard means an audit a
    worker is actively running (lease extended each stage) is never touched —
    only genuinely abandoned work is failed. We COALESCE error_message so an
    existing failure reason is preserved, and set stage='failed' too so the
    per-SKU pollers (which read `stage`) and the wedge poller (which reads
    `status`) both see a terminal state.
    """
    await ensure_merchant_audit_runs_table()
    now = _now_utc()
    cutoff = datetime.fromtimestamp(
        now.timestamp() - ttl_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE merchant_audit_runs
           SET status = 'failed',
               stage = 'failed',
               completed_at = :now,
               stage_updated_at = :now,
               error_message = COALESCE(error_message, 'audit_abandoned_reaped')
         WHERE status = 'running'
           AND requested_at < :cutoff
           AND (claimed_until IS NULL OR claimed_until < :now)
     RETURNING run_id, merchant_id, partial_result_jsonb
    """
    try:
        rows = await database.fetch_all(query, {"now": now, "cutoff": cutoff})
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            data = dict(row)
            # asyncpg returns JSONB as a JSON STRING — decode here so callers
            # get launch options as a dict (see _decode_jsonb_field docstring).
            decoded = _decode_jsonb_field(data.get("partial_result_jsonb"))
            launch = decoded.get("launch") if isinstance(decoded, dict) else None
            out.append({
                "run_id": data.get("run_id"),
                "merchant_id": data.get("merchant_id"),
                "launch_options": launch if isinstance(launch, dict) else {},
            })
        return out
    except Exception as exc:
        logger.warning(
            "fail_abandoned_runs failed: %s", str(exc)[:200],
        )
        return []
