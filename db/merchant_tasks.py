"""PR-6: human task queue persistence.

Schema mirror of `db/executor_runs.py` (lifecycle bookkeeping pattern
established earlier in the session). Each row is one tracked task —
either materialized from an audit's action_items or emitted by an
executor agent that needs human follow-up.

Lifecycle:
  - record_task_created(...)                 → status='pending'
  - update_task_status(task_id, ...)         → moves through pending →
    in_progress → {done, dismissed, failed}
  - dismiss_task(task_id, reason)            → terminal status with
    operator-supplied rationale (audit trail)

Read helpers live in services/task_queue_service.py.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db._jsonb_safe import _json_safe
from db.database import database, metadata

logger = logging.getLogger(__name__)


VALID_STATUSES = frozenset({
    "pending", "in_progress", "done", "dismissed", "failed",
    # Q-P0-2 / Q-P1-4: when a fresh audit run emits the same
    # canonical action identity (merchant + lever + normalized
    # title + target_host), the older pending task is moved to
    # `superseded` and `superseded_by_task_id` points at the new
    # task. Preserved in the table for audit-trail visibility;
    # default list views exclude them like 'dismissed'.
    "superseded",
})

VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


merchant_tasks = Table(
    "merchant_tasks",
    metadata,
    Column("task_id", UUID(as_uuid=False), primary_key=True),
    Column("merchant_id", Text, nullable=False),
    Column("parent_audit_run_id", UUID(as_uuid=False), nullable=True),
    Column("source_executor_run_id", UUID(as_uuid=False), nullable=True),
    Column("lever", Text, nullable=True),
    Column("severity", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("assigned_to_agent", Text, nullable=True),
    Column("assigned_to_human", Text, nullable=True),
    Column("evidence_jsonb", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("dismissed_reason", Text, nullable=True),
    # Q-P0-2 / Q-P1-4: cross-audit supersession pointer (mig 092).
    # When a fresh audit emits the same canonical action identity,
    # the older pending row's status flips to 'superseded' and this
    # column points at the new task.
    Column("superseded_by_task_id", UUID(as_uuid=False), nullable=True),
    Index("idx_merchant_tasks_open", "merchant_id", "status", "severity", "created_at"),
    Index("idx_merchant_tasks_audit", "parent_audit_run_id"),
    Index("idx_merchant_tasks_executor_source", "source_executor_run_id"),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_tasks (
      task_id                  UUID PRIMARY KEY,
      merchant_id              TEXT NOT NULL,
      parent_audit_run_id      UUID NULL,
      source_executor_run_id   UUID NULL,
      lever                    TEXT NULL,
      severity                 TEXT NOT NULL DEFAULT 'medium',
      title                    TEXT NOT NULL,
      body                     TEXT NULL,
      status                   TEXT NOT NULL DEFAULT 'pending',
      assigned_to_agent        TEXT NULL,
      assigned_to_human        TEXT NULL,
      evidence_jsonb           JSONB NULL,
      created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at             TIMESTAMPTZ NULL,
      dismissed_reason         TEXT NULL,
      superseded_by_task_id    UUID NULL
    );
    """,
    # Migration 092: fresh-deploy tables get the column inline above;
    # existing-deploy tables get it via the ALTER below (idempotent).
    "ALTER TABLE merchant_tasks ADD COLUMN IF NOT EXISTS "
    "superseded_by_task_id UUID NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_open "
    "ON merchant_tasks (merchant_id, status, severity, created_at DESC) "
    "WHERE status IN ('pending', 'in_progress');",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_audit "
    "ON merchant_tasks (parent_audit_run_id) "
    "WHERE parent_audit_run_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_executor_source "
    "ON merchant_tasks (source_executor_run_id) "
    "WHERE source_executor_run_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_identity_pending "
    "ON merchant_tasks (merchant_id, lever, title) "
    "WHERE status = 'pending';",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_superseded_by "
    "ON merchant_tasks (superseded_by_task_id) "
    "WHERE superseded_by_task_id IS NOT NULL;",
]


async def ensure_merchant_tasks_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        try:
            for stmt in _DDL_STATEMENTS:
                await database.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ensure_merchant_tasks_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_task_created(
    *,
    merchant_id: str,
    title: str,
    body: Optional[str] = None,
    severity: str = "medium",
    lever: Optional[str] = None,
    parent_audit_run_id: Optional[str] = None,
    source_executor_run_id: Optional[str] = None,
    assigned_to_agent: Optional[str] = None,
    assigned_to_human: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Insert a new task with status='pending'. Returns task_id (UUID
    string) or None on persistence failure. Best-effort — caller can
    materialize many tasks from one audit; one failure shouldn't
    abort the batch.

    Coerces invalid severity → 'medium'."""
    if not merchant_id or not title:
        return None
    if severity not in VALID_SEVERITIES:
        severity = "medium"
    await ensure_merchant_tasks_table()
    task_id = str(uuid.uuid4())
    now = _now_utc()
    try:
        await database.execute(
            merchant_tasks.insert().values(
                task_id=task_id,
                merchant_id=merchant_id,
                parent_audit_run_id=parent_audit_run_id,
                source_executor_run_id=source_executor_run_id,
                lever=lever,
                severity=severity,
                title=title[:500],
                body=body,
                status="pending",
                assigned_to_agent=assigned_to_agent,
                assigned_to_human=assigned_to_human,
                # JSONB write boundary — coerce UUID/datetime/Decimal.
                evidence_jsonb=_json_safe(evidence) if evidence else evidence,
                created_at=now,
                updated_at=now,
            )
        )
        return task_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_task_created failed for merchant=%s title=%r: %s",
            merchant_id, title[:60], str(exc)[:200],
        )
        return None


async def update_task_status(
    *,
    task_id: str,
    status: str,
    assigned_to_human: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> bool:
    """Move a task to a new status. Returns True on success.

    Validates `status` against VALID_STATUSES — invalid → no-op +
    WARN. When status is terminal ('done', 'dismissed', 'failed'),
    sets completed_at."""
    if not task_id:
        return False
    if status not in VALID_STATUSES:
        logger.warning("update_task_status: invalid status=%r", status)
        return False
    await ensure_merchant_tasks_table()
    now = _now_utc()
    values: Dict[str, Any] = {"status": status, "updated_at": now}
    if status in ("done", "dismissed", "failed"):
        values["completed_at"] = now
    if assigned_to_human is not None:
        values["assigned_to_human"] = assigned_to_human
    if evidence is not None:
        values["evidence_jsonb"] = evidence
    try:
        await database.execute(
            merchant_tasks.update()
            .where(merchant_tasks.c.task_id == task_id)
            .values(**values)
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "update_task_status failed for task_id=%s: %s",
            task_id, str(exc)[:200],
        )
        return False


async def dismiss_task(
    *,
    task_id: str,
    reason: str,
) -> bool:
    """Terminal dismiss with operator-supplied rationale. Stored in
    dismissed_reason for audit trail."""
    if not task_id or not reason:
        return False
    await ensure_merchant_tasks_table()
    now = _now_utc()
    try:
        await database.execute(
            merchant_tasks.update()
            .where(merchant_tasks.c.task_id == task_id)
            .values(
                status="dismissed",
                dismissed_reason=reason[:2000],
                updated_at=now,
                completed_at=now,
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dismiss_task failed for task_id=%s: %s",
            task_id, str(exc)[:200],
        )
        return False


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    return {
        "task_id": str(d.get("task_id")) if d.get("task_id") else None,
        "merchant_id": d.get("merchant_id"),
        "parent_audit_run_id": (
            str(d.get("parent_audit_run_id"))
            if d.get("parent_audit_run_id") else None
        ),
        "source_executor_run_id": (
            str(d.get("source_executor_run_id"))
            if d.get("source_executor_run_id") else None
        ),
        "lever": d.get("lever"),
        "severity": d.get("severity"),
        "title": d.get("title"),
        "body": d.get("body"),
        "status": d.get("status"),
        "assigned_to_agent": d.get("assigned_to_agent"),
        "assigned_to_human": d.get("assigned_to_human"),
        # P1.1 dual-key shim: emit both `evidence` (legacy) and
        # `evidence_jsonb` (matches column name + frontend type).
        # Frontend reads `evidence_jsonb ?? evidence`. Drop the
        # legacy key after the dual-key window in Phase 2.
        "evidence": d.get("evidence_jsonb"),
        "evidence_jsonb": d.get("evidence_jsonb"),
        "created_at": (
            d["created_at"].isoformat()
            if isinstance(d.get("created_at"), datetime) else None
        ),
        "updated_at": (
            d["updated_at"].isoformat()
            if isinstance(d.get("updated_at"), datetime) else None
        ),
        "completed_at": (
            d["completed_at"].isoformat()
            if isinstance(d.get("completed_at"), datetime) else None
        ),
        "dismissed_reason": d.get("dismissed_reason"),
        "superseded_by_task_id": (
            str(d.get("superseded_by_task_id"))
            if d.get("superseded_by_task_id") else None
        ),
    }


async def list_tasks_for_merchant(
    *,
    merchant_id: str,
    status_filter: Optional[List[str]] = None,
    limit: int = 50,
    parent_audit_run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List tasks for a merchant, newest first. Default filters to
    open work (`pending` + `in_progress`); pass empty list for all
    statuses, or pass `['superseded']` to view the audit trail of
    tasks displaced by newer audit runs (Q-P0-2)."""
    await ensure_merchant_tasks_table()
    if status_filter is None:
        # Default queue: open work only. `superseded` is excluded
        # (Q-P0-2 — stale tasks from prior audits should not clutter
        # the live queue). `dismissed`, `done`, `failed` remain
        # excluded by default too.
        status_filter = ["pending", "in_progress"]
    # Validate each entry; silently drop unknowns.
    status_filter = [s for s in status_filter if s in VALID_STATUSES]

    try:
        q = merchant_tasks.select().where(
            merchant_tasks.c.merchant_id == merchant_id
        )
        if parent_audit_run_id is not None:
            q = q.where(
                merchant_tasks.c.parent_audit_run_id == parent_audit_run_id
            )
        if status_filter:
            q = q.where(merchant_tasks.c.status.in_(status_filter))
        q = q.order_by(merchant_tasks.c.created_at.desc()).limit(limit)
        rows = await database.fetch_all(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_tasks_for_merchant failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def fetch_task(*, task_id: str) -> Optional[Dict[str, Any]]:
    await ensure_merchant_tasks_table()
    try:
        row = await database.fetch_one(
            merchant_tasks.select().where(merchant_tasks.c.task_id == task_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fetch_task failed for task_id=%s: %s", task_id, str(exc)[:200],
        )
        return None
    return _row_to_dict(row) if row else None


async def tasks_for_audit(
    *,
    parent_audit_run_id: str,
) -> List[Dict[str, Any]]:
    """All tasks materialized from one audit run (any status)."""
    await ensure_merchant_tasks_table()
    try:
        rows = await database.fetch_all(
            merchant_tasks.select()
            .where(
                merchant_tasks.c.parent_audit_run_id == parent_audit_run_id
            )
            .order_by(merchant_tasks.c.created_at.desc())
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tasks_for_audit failed for %s: %s",
            parent_audit_run_id, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def find_pending_supersede_candidates(
    *,
    merchant_id: str,
    lever: Optional[str],
    title: str,
    exclude_audit_run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Q-P0-2 / Q-P1-4: find prior `pending` tasks that share the
    canonical action identity with a newly-materialized task and
    should be marked `superseded`.

    Identity for this query: (merchant_id, lever, title) — the
    backing partial index `idx_merchant_tasks_identity_pending`
    covers exactly this lookup. Finer-grained identity components
    (target_host, product_key) live in `evidence_jsonb` and are
    compared by the service layer after fetching; doing that inside
    SQL would need a functional index on JSONB which is heavier and
    less portable.

    `exclude_audit_run_id` lets the caller exclude the run that
    JUST inserted this task (so a freshly-materialized task doesn't
    supersede itself when materialize_tasks_from_audit walks the
    action list).

    Returns the candidate rows (caller decides which to supersede
    based on full identity match including evidence fields)."""
    await ensure_merchant_tasks_table()
    try:
        q = merchant_tasks.select().where(
            merchant_tasks.c.merchant_id == merchant_id,
            merchant_tasks.c.status == "pending",
            merchant_tasks.c.title == title,
        )
        if lever is None:
            q = q.where(merchant_tasks.c.lever.is_(None))
        else:
            q = q.where(merchant_tasks.c.lever == lever)
        if exclude_audit_run_id is not None:
            q = q.where(
                merchant_tasks.c.parent_audit_run_id != exclude_audit_run_id
            )
        rows = await database.fetch_all(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "find_pending_supersede_candidates failed merchant=%s "
            "lever=%s title=%s: %s",
            merchant_id, lever, title[:60], str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def mark_task_superseded(
    *,
    task_id: str,
    superseded_by_task_id: str,
) -> bool:
    """Q-P0-2: flip a pending task to `status='superseded'` and
    point `superseded_by_task_id` at the newer task. Idempotent;
    a no-op when the task is already terminal (done / dismissed /
    failed / superseded). Returns True when the UPDATE matched a
    row, False otherwise."""
    await ensure_merchant_tasks_table()
    try:
        now = datetime.now(timezone.utc)
        result = await database.execute(
            merchant_tasks.update()
            .where(
                merchant_tasks.c.task_id == task_id,
                merchant_tasks.c.status == "pending",
            )
            .values(
                status="superseded",
                superseded_by_task_id=superseded_by_task_id,
                updated_at=now,
                completed_at=now,
            )
        )
        if isinstance(result, int):
            return result > 0
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mark_task_superseded failed task_id=%s by=%s: %s",
            task_id, superseded_by_task_id, str(exc)[:200],
        )
        return False
