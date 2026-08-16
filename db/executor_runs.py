"""PR-4a: persistence for executor agent runs.

Each row tracks ONE invocation of an executor agent. Lifecycle:
  - record_executor_run_started(agent_name, merchant_id?, parent_audit_run_id?)
    → returns UUID run_id, inserts row with status='running'
  - record_executor_run_completed(run_id, status, evidence_jsonb?, error?)
    → updates the row at completion

Read helpers:
  - recent_runs_for_merchant(merchant_id, limit) → drives the
    "what Pivota did for you this week" panel
  - recent_runs_for_agent(agent_name, limit) → ops dashboard
  - runs_for_audit(parent_audit_run_id) → audit detail view's
    "fixes shipped after this audit" section

All best-effort: DB failures log + degrade gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db._jsonb_safe import _json_safe

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


executor_runs = Table(
    "executor_runs",
    metadata,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    Column("agent_name", Text, nullable=False),
    Column("merchant_id", Text, nullable=True),
    Column("parent_audit_run_id", UUID(as_uuid=False), nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("evidence_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    # P3.1: durable work-queue columns. See
    # db/migrations/085_executor_runs_durability.sql for column-level
    # commentary.
    Column("stage", Text, nullable=False, server_default="succeeded"),
    Column("stage_updated_at", DateTime(timezone=True), nullable=True),
    Column("claimed_by_worker", Text, nullable=True),
    Column("claimed_until", DateTime(timezone=True), nullable=True),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="3"),
    Column("error_jsonb", JSONB, nullable=True),
    Column("payload_jsonb", JSONB, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Index("idx_executor_runs_agent_recent", "agent_name", "requested_at"),
    Index("idx_executor_runs_merchant_recent", "merchant_id", "requested_at"),
    Index(
        "idx_executor_runs_parent_audit",
        "parent_audit_run_id", "requested_at",
    ),
    extend_existing=True,
)


# P3.1: state machine constants. The work queue's lifecycle:
#   queued → claimed → succeeded
#                     → failed → (re-enqueue) → claimed → ...
#                     → exhausted_retries (when retry_count == max_retries)
# W5 P7: when a merchant opts into approval mode (executor_auto_execute
# = false), the dispatcher enqueues rows in `pending_approval` instead of
# `queued`. The worker's claim query only pulls queued/claimed, so pending
# rows are inert until a merchant approves (→ queued) or declines
# (→ declined, terminal).
STAGE_QUEUED = "queued"
STAGE_CLAIMED = "claimed"
STAGE_SUCCEEDED = "succeeded"
STAGE_FAILED = "failed"
STAGE_EXHAUSTED_RETRIES = "exhausted_retries"
STAGE_PENDING_APPROVAL = "pending_approval"
STAGE_DECLINED = "declined"

ACTIVE_STAGES = frozenset({STAGE_QUEUED, STAGE_CLAIMED})
TERMINAL_STAGES = frozenset({
    STAGE_SUCCEEDED, STAGE_FAILED, STAGE_EXHAUSTED_RETRIES,
    STAGE_DECLINED,
})
# Stages that count as "already scheduled" for idempotency dedupe. A
# pending_approval row is not actively worked but must still dedupe a
# re-dispatch of the same (agent, merchant, audit) tuple.
IN_FLIGHT_STAGES = frozenset(ACTIVE_STAGES | {STAGE_PENDING_APPROVAL})

# W5 P7: pending_approval rows older than this are treated as expired —
# they can't be approved (the underlying audit is stale). Enforced at
# approve time, not by a cron (matches the lease-reclaim-inline pattern).
PENDING_APPROVAL_TTL_DAYS = 30

# Validate state transitions before any UPDATE so a worker race or
# stale read can't silently corrupt the queue.
VALID_STAGE_TRANSITIONS: dict = {
    STAGE_QUEUED: {STAGE_CLAIMED},
    STAGE_CLAIMED: {
        STAGE_SUCCEEDED, STAGE_FAILED, STAGE_QUEUED,
        STAGE_EXHAUSTED_RETRIES,
    },
    # W5 P7: approve (→ queued) or decline (→ declined, terminal).
    STAGE_PENDING_APPROVAL: {STAGE_QUEUED, STAGE_DECLINED},
    STAGE_SUCCEEDED: set(),
    STAGE_FAILED: set(),
    STAGE_EXHAUSTED_RETRIES: set(),
    STAGE_DECLINED: set(),
}


def is_valid_stage_transition(from_stage: str, to_stage: str) -> bool:
    """Return True iff the transition is in the canonical state
    machine. Worker calls this before UPDATE to catch races early."""
    return to_stage in VALID_STAGE_TRANSITIONS.get(from_stage, set())


# Default lease for a claimed executor_run. Worker can extend on
# claim if it knows the agent runs long (sitemap fetch, GSC submit).
DEFAULT_LEASE_SECONDS = 300

# Stale-lease grace period before reclaim runs.
STALE_LEASE_GRACE_SECONDS = 30


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS executor_runs (
      run_id              UUID PRIMARY KEY,
      agent_name          TEXT NOT NULL,
      merchant_id         TEXT NULL,
      parent_audit_run_id UUID NULL,
      requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at        TIMESTAMPTZ NULL,
      status              TEXT NOT NULL,
      evidence_jsonb      JSONB NULL,
      error_message       TEXT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_agent_recent "
    "ON executor_runs (agent_name, requested_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_merchant_recent "
    "ON executor_runs (merchant_id, requested_at DESC) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_parent_audit "
    "ON executor_runs (parent_audit_run_id, requested_at DESC) "
    "WHERE parent_audit_run_id IS NOT NULL;",
    # P3.1: durable work-queue columns. Schema-guard backstop for
    # environments where db/migrations/085_executor_runs_durability.sql
    # hasn't been applied yet. Each ALTER is idempotent.
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'succeeded';",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS stage_updated_at TIMESTAMPTZ;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS claimed_by_worker TEXT;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 3;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS error_jsonb JSONB;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS payload_jsonb JSONB;",
    "ALTER TABLE executor_runs "
    "ADD COLUMN IF NOT EXISTS idempotency_key TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_worker_pull "
    "ON executor_runs (stage, claimed_until, requested_at) "
    "WHERE stage IN ('queued', 'claimed');",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_idempotency "
    "ON executor_runs (idempotency_key) "
    "WHERE idempotency_key IS NOT NULL "
    "AND stage IN ('queued', 'claimed');",
]


async def ensure_executor_runs_table() -> None:
    """Per-statement-tolerant backstop. Postgres prod runs the .sql
    migrations directly; this inline DDL covers hermetic test
    environments where partial-index syntax + ADD COLUMN IF NOT
    EXISTS aren't supported (matches the P2.1 pattern)."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_executor_runs_table",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_executor_run_started(
    *,
    agent_name: str,
    merchant_id: Optional[str] = None,
    parent_audit_run_id: Optional[str] = None,
) -> Optional[str]:
    await ensure_executor_runs_table()
    run_id = str(uuid.uuid4())
    try:
        await database.execute(
            executor_runs.insert().values(
                run_id=run_id,
                agent_name=agent_name,
                merchant_id=merchant_id,
                parent_audit_run_id=parent_audit_run_id,
                requested_at=_now_utc(),
                status="running",
            )
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_executor_run_started failed for agent=%s: %s",
            agent_name, str(exc)[:200],
        )
        return None


async def record_executor_run_completed(
    *,
    run_id: Optional[str],
    status: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return
    try:
        values: Dict[str, Any] = {
            "status": status,
            "completed_at": _now_utc(),
        }
        if evidence_jsonb is not None:
            # JSONB write boundary — coerce UUID/datetime/Decimal.
            values["evidence_jsonb"] = _json_safe(evidence_jsonb)
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        await database.execute(
            executor_runs.update()
            .where(executor_runs.c.run_id == run_id)
            .values(**values)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_executor_run_completed failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    return {
        "run_id": str(d.get("run_id")) if d.get("run_id") else None,
        "agent_name": d.get("agent_name"),
        "merchant_id": d.get("merchant_id"),
        "parent_audit_run_id": (
            str(d.get("parent_audit_run_id"))
            if d.get("parent_audit_run_id") else None
        ),
        # P1.1 dual-key shim: emit both `requested_at` (legacy
        # column name) and `started_at` (frontend's expected name).
        # Same timestamp value for both during the migration window.
        # Drop `requested_at` from the API response after Phase 2.
        "requested_at": (
            d["requested_at"].isoformat()
            if isinstance(d.get("requested_at"), datetime) else None
        ),
        "started_at": (
            d["requested_at"].isoformat()
            if isinstance(d.get("requested_at"), datetime) else None
        ),
        "completed_at": (
            d["completed_at"].isoformat()
            if isinstance(d.get("completed_at"), datetime) else None
        ),
        "status": d.get("status"),
        # P1.1 dual-key shim: see merchant_tasks._row_to_dict.
        "evidence": d.get("evidence_jsonb"),
        "evidence_jsonb": d.get("evidence_jsonb"),
        "error_message": d.get("error_message"),
    }


async def recent_runs_for_merchant(
    *,
    merchant_id: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.merchant_id == merchant_id)
            .order_by(executor_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recent_runs_for_merchant (executor_runs) failed: %s",
            str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def recent_runs_for_agent(
    *,
    agent_name: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.agent_name == agent_name)
            .order_by(executor_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recent_runs_for_agent failed for %s: %s",
            agent_name, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def runs_for_audit(
    *,
    parent_audit_run_id: str,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.parent_audit_run_id == parent_audit_run_id)
            .order_by(executor_runs.c.requested_at.desc())
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runs_for_audit failed for parent=%s: %s",
            parent_audit_run_id, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


# =====================================================================
# P3.1 — durable work-queue accessors. Used by:
#   - services/executor_agents/dispatcher.py → enqueue_executor_run
#                                              + find_in_flight_by_idem
#   - services/executor_run_worker.py → claim_next +
#                                       mark_succeeded /
#                                       mark_failed_with_retry +
#                                       release_stale_leases
# =====================================================================


async def enqueue_executor_run(
    *,
    agent_name: str,
    merchant_id: Optional[str] = None,
    parent_audit_run_id: Optional[str] = None,
    payload_jsonb: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    max_retries: int = 3,
    stage: str = STAGE_QUEUED,
) -> Optional[str]:
    """Insert a row for the executor_run_worker to claim. Returns the
    run_id, or None on persistence failure.

    `stage` (W5 P7) defaults to 'queued' (auto-execute). Pass
    'pending_approval' when the merchant opted into approval mode — the
    worker's claim query ignores it until the merchant approves.
    """
    if stage not in (STAGE_QUEUED, STAGE_PENDING_APPROVAL):
        raise ValueError(f"enqueue_executor_run: invalid stage {stage!r}")
    await ensure_executor_runs_table()
    run_id = str(uuid.uuid4())
    now = _now_utc()
    # Legacy `status` mirrors `stage` during the dual-key window — old
    # readers see 'running' for queued work, 'pending_approval' for
    # awaiting-consent work.
    status = (
        "pending_approval" if stage == STAGE_PENDING_APPROVAL else "running"
    )
    try:
        # TODO(brief-future): wire execution-credit debit to executor_runs
        # writes when execution layer ships.
        await database.execute(
            executor_runs.insert().values(
                run_id=run_id,
                agent_name=agent_name,
                merchant_id=merchant_id,
                parent_audit_run_id=parent_audit_run_id,
                requested_at=now,
                status=status,
                stage=stage,
                stage_updated_at=now,
                # JSONB write boundary — coerce UUID/datetime/Decimal.
                payload_jsonb=_json_safe(payload_jsonb) if payload_jsonb else payload_jsonb,
                idempotency_key=idempotency_key,
                max_retries=int(max_retries),
            )
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "enqueue_executor_run failed for agent=%s: %s",
            agent_name, str(exc)[:200],
        )
        return None


async def find_in_flight_executor_run_by_idempotency(
    *, idempotency_key: str,
) -> Optional[str]:
    """Return the run_id of any in-flight executor run matching this
    idempotency key, or None. Dispatcher calls this before enqueueing
    to dedupe re-audits that would otherwise produce duplicate
    executor work."""
    if not idempotency_key:
        return None
    await ensure_executor_runs_table()
    try:
        from sqlalchemy.sql import select
        row = await database.fetch_one(
            select(executor_runs.c.run_id)
            .where(
                executor_runs.c.idempotency_key == idempotency_key,
                executor_runs.c.stage.in_(list(IN_FLIGHT_STAGES)),
            )
            .limit(1)
        )
        if row is None:
            return None
        return str(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "find_in_flight_executor_run_by_idempotency failed: %s",
            str(exc)[:200],
        )
        return None


async def claim_next_pending_executor_run(
    *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest queued or stale-leased run.
    Returns the claimed row dict, or None if nothing to claim.
    SKIP LOCKED makes it safe under multiple workers."""
    await ensure_executor_runs_table()
    now = _now_utc()
    new_until = datetime.fromtimestamp(
        now.timestamp() + lease_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE executor_runs
           SET claimed_by_worker = :worker_id,
               claimed_until     = :new_until,
               stage             = 'claimed',
               stage_updated_at  = :now
         WHERE run_id = (
             SELECT run_id
               FROM executor_runs
              WHERE stage IN ('queued', 'claimed')
                AND (
                    claimed_until IS NULL
                 OR claimed_until < :now
                )
              ORDER BY requested_at ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
        RETURNING run_id, agent_name, merchant_id,
                  parent_audit_run_id, payload_jsonb,
                  idempotency_key, retry_count, max_retries,
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
            "agent_name": d.get("agent_name"),
            "merchant_id": d.get("merchant_id"),
            "parent_audit_run_id": (
                str(d.get("parent_audit_run_id"))
                if d.get("parent_audit_run_id") else None
            ),
            "payload_jsonb": d.get("payload_jsonb"),
            "idempotency_key": d.get("idempotency_key"),
            "retry_count": int(d.get("retry_count") or 0),
            "max_retries": int(d.get("max_retries") or 3),
            "requested_at": (
                d["requested_at"].isoformat()
                if isinstance(d.get("requested_at"), datetime)
                else None
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "claim_next_pending_executor_run failed for worker_id=%s: %s",
            worker_id, str(exc)[:200],
        )
        return None


async def mark_executor_run_succeeded(
    *,
    run_id: str,
    worker_id: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
) -> bool:
    """Terminal succeeded transition. Guarded on worker ownership."""
    await ensure_executor_runs_table()
    now = _now_utc()
    values: Dict[str, Any] = {
        "stage": STAGE_SUCCEEDED,
        "stage_updated_at": now,
        "completed_at": now,
        # Keep legacy `status` aligned during the dual-key window.
        "status": "succeeded",
        # Q-P1-7: clear stale failure text. A run that bounced from
        # STAGE_FAILED → retry → STAGE_SUCCEEDED previously kept the
        # error_message from the failed attempt, leaving operators
        # looking at "succeeded" rows that still carry a failure
        # message. The transient failure is no longer the truth once
        # the retry succeeded.
        "error_message": None,
    }
    if evidence_jsonb is not None:
        # JSONB write boundary — coerce UUID/datetime/Decimal.
        from db._jsonb_safe import _json_safe
        values["evidence_jsonb"] = _json_safe(evidence_jsonb)
    try:
        result = await database.execute(
            executor_runs.update()
            .where(
                executor_runs.c.run_id == run_id,
                executor_runs.c.stage == STAGE_CLAIMED,
                executor_runs.c.claimed_by_worker == worker_id,
            )
            .values(**values)
        )
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_executor_run_succeeded failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return False


async def mark_executor_run_failed_with_retry(
    *,
    run_id: str,
    worker_id: str,
    error_message: str,
    error_jsonb: Optional[Dict[str, Any]] = None,
) -> str:
    """Failed-attempt handling. If retry_count + 1 < max_retries:
    increment retry_count, transition back to STAGE_QUEUED so a
    sibling worker can claim. Otherwise transition to
    STAGE_EXHAUSTED_RETRIES (terminal).

    P5.8.6: atomic — single UPDATE with CASE expression on stage +
    `retry_count + 1` increment. The original was SELECT-then-
    UPDATE which had a race: between the SELECT (line 567 of P3.1)
    and the UPDATE (line 616), the reaper could null
    claimed_by_worker. The UPDATE would then no-op silently +
    retry_count never incremented + retry budget effectively
    infinite for stuck rows.

    Returns the new stage string for caller logging.
    """
    await ensure_executor_runs_table()
    now = _now_utc()
    # Single atomic UPDATE. RETURNING gives us the new stage so we
    # know whether the row landed in queued (retry) or
    # exhausted_retries (terminal) without re-querying.
    #
    # Logic:
    #   IF retry_count + 1 < max_retries:
    #     stage = 'queued', clear lease, increment retry_count
    #   ELSE:
    #     stage = 'exhausted_retries', set completed_at, increment
    query = """
        UPDATE executor_runs
           SET retry_count = retry_count + 1,
               stage = CASE
                   WHEN retry_count + 1 < max_retries THEN 'queued'
                   ELSE 'exhausted_retries'
               END,
               stage_updated_at = :now,
               completed_at = CASE
                   WHEN retry_count + 1 < max_retries THEN completed_at
                   ELSE :now
               END,
               claimed_by_worker = CASE
                   WHEN retry_count + 1 < max_retries THEN NULL
                   ELSE claimed_by_worker
               END,
               claimed_until = CASE
                   WHEN retry_count + 1 < max_retries THEN NULL
                   ELSE claimed_until
               END,
               status = CASE
                   WHEN retry_count + 1 < max_retries THEN 'running'
                   ELSE 'failed'
               END,
               error_message = :error_message,
               error_jsonb = :error_jsonb
         WHERE run_id = :run_id
           AND stage = 'claimed'
           AND claimed_by_worker = :worker_id
         RETURNING stage
    """
    import json as _json
    try:
        row = await database.fetch_one(
            query,
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "now": now,
                "error_message": error_message[:2000],
                # JSONB column accepts dict via the databases driver,
                # but the raw-SQL path needs explicit serialization
                # because we're not going through the SQLAlchemy
                # Table.update() shape that auto-coerces.
                # _json_safe coerces UUID/datetime/Decimal first so
                # _json.dumps doesn't raise on those types.
                "error_jsonb": (
                    _json.dumps(_json_safe(error_jsonb))
                    if error_jsonb is not None else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_executor_run_failed_with_retry atomic UPDATE "
            "failed for run_id=%s: %s", run_id, str(exc)[:200],
        )
        return STAGE_FAILED
    if row is None:
        # Lost the lease before the UPDATE matched — sibling worker
        # already retook + advanced this row. No-op cleanly.
        return STAGE_FAILED
    return dict(row).get("stage") or STAGE_FAILED


async def fetch_executor_run_by_id(
    *, run_id: str,
) -> Optional[Dict[str, Any]]:
    """Read one executor_run row including the new lifecycle fields."""
    await ensure_executor_runs_table()
    try:
        row = await database.fetch_one(
            executor_runs.select().where(
                executor_runs.c.run_id == run_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fetch_executor_run_by_id failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    d = dict(row)
    base = _row_to_dict(row)
    # Add the durable-queue fields the basic _row_to_dict didn't
    # know about (it's the legacy fire-and-forget shape).
    base.update({
        "stage": d.get("stage"),
        "stage_updated_at": (
            d["stage_updated_at"].isoformat()
            if isinstance(d.get("stage_updated_at"), datetime)
            else None
        ),
        "retry_count": int(d.get("retry_count") or 0),
        "max_retries": int(d.get("max_retries") or 3),
        "error_jsonb": d.get("error_jsonb"),
        "payload_jsonb": d.get("payload_jsonb"),
        "idempotency_key": d.get("idempotency_key"),
    })
    return base


# =====================================================================
# W5 P7 — per-merchant consent: pending_approval read + approve/decline.
# Used by routes/merchant_dashboard_routes.py (merchant-authenticated).
# =====================================================================


def _pending_is_expired(stage_updated_at_iso: Optional[str]) -> bool:
    """True when a pending_approval row is past its TTL. The audit that
    produced the pending executor work is stale, so approving would run
    an agent against a report the merchant never actually saw fresh."""
    if not stage_updated_at_iso:
        return False
    try:
        dt = datetime.fromisoformat(stage_updated_at_iso)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now_utc() - dt) > timedelta(days=PENDING_APPROVAL_TTL_DAYS)


async def pending_approval_runs_for_merchant(
    *,
    merchant_id: str,
    parent_audit_run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List a merchant's executor_runs awaiting approval. Optionally
    filtered to one audit run. Each row carries an `expired` flag so the
    portal can grey out rows past the TTL (they can't be approved)."""
    await ensure_executor_runs_table()
    try:
        query = (
            executor_runs.select()
            .where(
                executor_runs.c.merchant_id == merchant_id,
                executor_runs.c.stage == STAGE_PENDING_APPROVAL,
            )
            .order_by(executor_runs.c.requested_at.desc())
        )
        if parent_audit_run_id:
            query = query.where(
                executor_runs.c.parent_audit_run_id == parent_audit_run_id
            )
        rows = await database.fetch_all(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pending_approval_runs_for_merchant failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    out: List[Dict[str, Any]] = []
    for r in (rows or []):
        d = dict(r)
        base = _row_to_dict(r)
        stage_updated_iso = (
            d["stage_updated_at"].isoformat()
            if isinstance(d.get("stage_updated_at"), datetime) else None
        )
        base.update({
            "stage": d.get("stage"),
            "stage_updated_at": stage_updated_iso,
            "expired": _pending_is_expired(stage_updated_iso),
        })
        out.append(base)
    return out


async def approve_executor_run(
    *, run_id: str, merchant_id: str,
) -> Dict[str, Any]:
    """Approve a pending_approval executor_run: transition it to
    `queued` so the worker can claim it. Merchant-ownership enforced.

    Idempotent: approving a row that's already queued/claimed/succeeded
    is a no-op success. Returns {"status": ...} where status is one of
    'success' | 'not_found' | 'forbidden' | 'expired' | 'conflict'.
    """
    row = await fetch_executor_run_by_id(run_id=run_id)
    if row is None:
        return {"status": "not_found", "run_id": run_id}
    if (row.get("merchant_id") or None) != merchant_id:
        return {"status": "forbidden", "run_id": run_id}

    stage = row.get("stage")
    # Idempotent no-op: an already-approved (or completed) run is a
    # success — approving twice must not double-enqueue.
    if stage in (STAGE_QUEUED, STAGE_CLAIMED, STAGE_SUCCEEDED):
        return {"status": "success", "run_id": run_id, "stage": stage,
                "noop": True}
    if stage != STAGE_PENDING_APPROVAL:
        # declined / failed / exhausted — not approvable.
        return {"status": "conflict", "run_id": run_id, "stage": stage}
    if _pending_is_expired(row.get("stage_updated_at")):
        return {"status": "expired", "run_id": run_id, "stage": stage}

    now = _now_utc()
    cutoff = now - timedelta(days=PENDING_APPROVAL_TTL_DAYS)
    # Atomic guarded transition — only the FIRST approve moves the row,
    # so approve-exactly-once holds under concurrent calls. The TTL is
    # re-checked in SQL so a row that expired between fetch and update
    # can't slip through.
    query = """
        UPDATE executor_runs
           SET stage            = 'queued',
               status           = 'running',
               stage_updated_at = :now
         WHERE run_id = :run_id
           AND merchant_id = :merchant_id
           AND stage = 'pending_approval'
           AND stage_updated_at >= :cutoff
         RETURNING run_id
    """
    try:
        matched = await database.fetch_one(
            query,
            {"run_id": run_id, "merchant_id": merchant_id,
             "now": now, "cutoff": cutoff},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "approve_executor_run failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return {"status": "conflict", "run_id": run_id, "stage": stage}
    if matched is not None:
        return {"status": "success", "run_id": run_id, "stage": STAGE_QUEUED}
    # UPDATE matched nothing — re-read to classify (raced approve or
    # just-expired).
    fresh = await fetch_executor_run_by_id(run_id=run_id)
    fresh_stage = fresh.get("stage") if fresh else None
    if fresh_stage in (STAGE_QUEUED, STAGE_CLAIMED, STAGE_SUCCEEDED):
        return {"status": "success", "run_id": run_id, "stage": fresh_stage,
                "noop": True}
    return {"status": "expired", "run_id": run_id, "stage": fresh_stage}


async def decline_executor_run(
    *, run_id: str, merchant_id: str,
) -> Dict[str, Any]:
    """Decline a pending_approval executor_run: transition it to the
    terminal `declined` stage so it never runs. Merchant-ownership
    enforced. Idempotent: declining an already-declined run is a no-op
    success. Returns {"status": ...} as with approve_executor_run.
    """
    row = await fetch_executor_run_by_id(run_id=run_id)
    if row is None:
        return {"status": "not_found", "run_id": run_id}
    if (row.get("merchant_id") or None) != merchant_id:
        return {"status": "forbidden", "run_id": run_id}

    stage = row.get("stage")
    if stage == STAGE_DECLINED:
        return {"status": "success", "run_id": run_id, "stage": stage,
                "noop": True}
    if stage != STAGE_PENDING_APPROVAL:
        # Already queued/claimed/succeeded/failed — can't decline.
        return {"status": "conflict", "run_id": run_id, "stage": stage}

    now = _now_utc()
    query = """
        UPDATE executor_runs
           SET stage            = 'declined',
               status           = 'declined',
               stage_updated_at = :now,
               completed_at     = :now
         WHERE run_id = :run_id
           AND merchant_id = :merchant_id
           AND stage = 'pending_approval'
         RETURNING run_id
    """
    try:
        matched = await database.fetch_one(
            query, {"run_id": run_id, "merchant_id": merchant_id, "now": now},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "decline_executor_run failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )
        return {"status": "conflict", "run_id": run_id, "stage": stage}
    if matched is not None:
        return {"status": "success", "run_id": run_id, "stage": STAGE_DECLINED}
    # Raced — re-read to report idempotently.
    fresh = await fetch_executor_run_by_id(run_id=run_id)
    fresh_stage = fresh.get("stage") if fresh else None
    if fresh_stage == STAGE_DECLINED:
        return {"status": "success", "run_id": run_id, "stage": fresh_stage,
                "noop": True}
    return {"status": "conflict", "run_id": run_id, "stage": fresh_stage}


async def release_stale_executor_leases(
    *, grace_seconds: int = STALE_LEASE_GRACE_SECONDS,
) -> int:
    """Reclaim leases that are past expiry + grace. Returns the
    count released. Background reaper backstop — claim_next already
    tolerates stale leases inline."""
    await ensure_executor_runs_table()
    cutoff = datetime.fromtimestamp(
        _now_utc().timestamp() - grace_seconds, tz=timezone.utc,
    )
    query = """
        UPDATE executor_runs
           SET stage             = 'queued',
               claimed_by_worker = NULL,
               claimed_until     = NULL
         WHERE claimed_until IS NOT NULL
           AND claimed_until < :cutoff
           AND stage = 'claimed'
    """
    try:
        result = await database.execute(query, {"cutoff": cutoff})
        if isinstance(result, int):
            return result
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_stale_executor_leases failed: %s", str(exc)[:200],
        )
        return 0


def compute_executor_idempotency_key(
    *,
    agent_name: str,
    merchant_id: Optional[str],
    parent_audit_run_id: Optional[str],
) -> str:
    """Pure helper — sha256 of the dedupe tuple. Dispatcher calls
    this before enqueue so re-running the same audit doesn't
    enqueue duplicate executor work for in-flight rows."""
    import hashlib
    components = [
        (agent_name or "").strip(),
        (merchant_id or "").strip(),
        (parent_audit_run_id or "").strip(),
    ]
    return hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()
