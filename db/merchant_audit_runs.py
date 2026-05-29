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

from db._jsonb_safe import _json_safe
from typing import Any, Dict, List, Optional, Tuple

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
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS requested_by_user_id TEXT;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS subject_type TEXT NOT NULL "
    "DEFAULT 'merchant';",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;",
    "ALTER TABLE merchant_audit_runs "
    "ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;",
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
    pattern — runs once per process, swallows errors, lets the route
    continue even if DDL fails."""
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
        for stmt in _DDL_STATEMENTS:
            try:
                await database.execute(stmt)
            except Exception as exc:
                logger.debug(
                    "ensure_merchant_audit_runs_table skip stmt "
                    "(best-effort): %s | %s",
                    str(exc)[:120], stmt[:100],
                )
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
    merchant_id: str,
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
    await ensure_merchant_audit_runs_table()
    run_id = str(uuid.uuid4())
    now = _now_utc()
    partial_result_jsonb = (
        _json_safe(request_options_jsonb)
        if isinstance(request_options_jsonb, dict) else None
    )

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
    # against the partial unique index. ON CONFLICT requires raw SQL
    # since SQLAlchemy core's `insert().on_conflict_do_nothing()` is a
    # postgres dialect call that's awkward in our databases-wrapped
    # path; raw SQL is also faster + more explicit at this hot path.
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
            ON CONFLICT ON CONSTRAINT uniq_merchant_audit_runs_active_idempotency_key
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
    except Exception as exc:
        # Fallback for environments where the unique constraint
        # hasn't been applied yet (mid-deploy window between this
        # PR and its migration). Behavior matches the old code:
        # plain insert, no DB-enforced dedupe. Logged so ops sees
        # the gap.
        logger.warning(
            "enqueue_audit_run on-conflict path raised "
            "(constraint maybe not applied yet?) — falling back to "
            "plain insert. merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
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
                    idempotency_key=idempotency_key,
                    requested_by_user_id=requested_by_user_id,
                    partial_result_jsonb=partial_result_jsonb,
                )
            )
            return run_id, False
        except Exception as inner_exc:
            logger.warning(
                "enqueue_audit_run fallback insert failed for "
                "merchant_id=%s: %s",
                merchant_id, str(inner_exc)[:200],
            )
            return None, False

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
            "partial_result_jsonb": d.get("partial_result_jsonb"),
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
        "partial_result_jsonb": d.get("partial_result_jsonb"),
        "report_jsonb": d.get("report_jsonb"),
        "cost_summary_jsonb": d.get("cost_summary_jsonb"),
        "error_jsonb": d.get("error_jsonb"),
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
