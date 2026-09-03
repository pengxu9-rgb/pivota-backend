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
import hashlib
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
    or_,
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
    # C4 — the verification lifecycle. "done" means the merchant says they
    # changed something; none of these mean that, and that distinction is the
    # whole point of the Prove stage:
    #   ready_for_retest  the fix is in place and awaiting a same-basis replay
    #   verifying         a replay is in flight
    #   verified          the replay showed the gap closed — the ONLY status
    #                     that means the fix worked
    #   regressed         it closed and re-opened; actionable again, so
    #                     deliberately NOT terminal
    "ready_for_retest", "verifying", "verified", "regressed",
})

# Statuses that stop a task being actionable and stamp completed_at. `verified`
# joins them because there is nothing left to do; `regressed` deliberately does
# NOT — a gap that re-opened needs working again, and marking it complete would
# hide exactly the case the Prove stage exists to surface. `ready_for_retest`
# and `verifying` are in-flight, not finished.
TERMINAL_STATUSES = frozenset({"done", "dismissed", "failed", "verified"})

VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low"})


merchant_tasks = Table(
    "merchant_tasks",
    metadata,
    Column("task_id", UUID(as_uuid=False), primary_key=True),
    Column("merchant_id", Text, nullable=False),
    Column("parent_audit_run_id", UUID(as_uuid=False), nullable=True),
    # The stable handle for this recovery action, derived by recovery_key_for
    # from the same canonical identity dedupe uses. Nullable: every task
    # written before this column existed has none, and backfilling is a
    # separate decision — an absent key means "not attributable", which is
    # honest, where a wrong key would be worse than none.
    Column("recovery_key", Text, nullable=True),
    Column("source_executor_run_id", UUID(as_uuid=False), nullable=True),
    Column("parent_task_id", UUID(as_uuid=False), nullable=True),
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
    Index("idx_merchant_tasks_parent_task", "parent_task_id"),
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
      parent_task_id           UUID NULL,
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
    # Migrations 092/093: fresh-deploy tables get the columns inline
    # above; existing-deploy tables get them via idempotent ALTERs.
    "ALTER TABLE merchant_tasks ADD COLUMN IF NOT EXISTS "
    "superseded_by_task_id UUID NULL;",
    "ALTER TABLE merchant_tasks ADD COLUMN IF NOT EXISTS "
    "parent_task_id UUID NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_open "
    "ON merchant_tasks (merchant_id, status, severity, created_at DESC) "
    "WHERE status IN ('pending', 'in_progress');",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_audit "
    "ON merchant_tasks (parent_audit_run_id) "
    "WHERE parent_audit_run_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_executor_source "
    "ON merchant_tasks (source_executor_run_id) "
    "WHERE source_executor_run_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_merchant_tasks_parent_task "
    "ON merchant_tasks (parent_task_id) "
    "WHERE parent_task_id IS NOT NULL;",
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
    parent_task_id: Optional[str] = None,
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
                parent_task_id=parent_task_id,
                lever=lever,
                severity=severity,
                title=title[:500],
                # Derived HERE rather than asked of every caller: the key must
                # come from one rule, and a caller that computed its own would
                # be free to disagree. Evidence carries the host and product,
                # which with the lever is the whole gap identity.
                recovery_key=recovery_key_for(
                    merchant_id=merchant_id,
                    lever=lever,
                    target_host=(evidence or {}).get("target_host"),
                    product_key=(evidence or {}).get("product_key"),
                ),
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
    WARN. When status is terminal (see TERMINAL_STATUSES) sets completed_at."""
    if not task_id:
        return False
    if status not in VALID_STATUSES:
        logger.warning("update_task_status: invalid status=%r", status)
        return False
    await ensure_merchant_tasks_table()
    now = _now_utc()
    values: Dict[str, Any] = {"status": status, "updated_at": now}
    if status in TERMINAL_STATUSES:
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
        "parent_task_id": (
            str(d.get("parent_task_id"))
            if d.get("parent_task_id") else None
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
    include_unscoped: bool = False,
) -> List[Dict[str, Any]]:
    """List tasks for a merchant, newest first. Default filters to
    open work (`pending` + `in_progress`); pass empty list for all
    statuses, or pass `['superseded']` to view the audit trail of
    tasks displaced by newer audit runs (Q-P0-2).

    When scoping to a run (`parent_audit_run_id`), `include_unscoped=True`
    ALSO returns standing tasks with a NULL parent_audit_run_id — the
    non-audit tasks (sku_evidence, niche_content) not tied to any run.
    Without it, `parent_audit_run_id == X` silently drops them (SQL
    `NULL = X` is never true), so a merchant's own actions would never
    appear in their Action plan."""
    await ensure_merchant_tasks_table()
    if status_filter is None:
        # Default queue: open work only. `superseded` is excluded
        # (Q-P0-2 — stale tasks from prior audits should not clutter
        # the live queue). `dismissed`, `done`, `failed` remain
        # excluded by default too.
        # The OPEN queue: everything not finished. The verification states are
        # in-flight, not done — omitting them would make a task vanish from the
        # merchant's queue the moment it became ready to prove, and `regressed`
        # is actionable again by definition. Leaving them out is how a task
        # ends up open forever with nothing showing it.
        status_filter = [
            "pending", "in_progress",
            "ready_for_retest", "verifying", "regressed",
        ]
    # Validate each entry; silently drop unknowns.
    status_filter = [s for s in status_filter if s in VALID_STATUSES]

    try:
        q = merchant_tasks.select().where(
            merchant_tasks.c.merchant_id == merchant_id
        )
        if parent_audit_run_id is not None:
            if include_unscoped:
                q = q.where(
                    or_(
                        merchant_tasks.c.parent_audit_run_id == parent_audit_run_id,
                        merchant_tasks.c.parent_audit_run_id.is_(None),
                    )
                )
            else:
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


async def find_executor_parent_task_candidates(
    *,
    merchant_id: str,
    parent_audit_run_id: str,
    lever: Optional[str],
) -> List[Dict[str, Any]]:
    """Audit-emitted task candidates that an executor task may refine.

    Executor-produced rows have source_executor_run_id set; parent
    audit action rows do not. The service layer applies the
    host/topic/title fingerprint after this scoped lookup.

    Excludes `superseded` rows: if a newer audit run replaced the
    parent action between executor dispatch and materialization
    (PR-2 supersession), linking the executor's child task to the
    stale parent would orphan it under a task the merchant no longer
    sees. Better to leave the child standalone (parent_task_id=None,
    the existing no-match back-compat path) than to link a dead
    parent. Surfaced by the post-#525 codex review (P1).
    """
    await ensure_merchant_tasks_table()
    try:
        q = merchant_tasks.select().where(
            merchant_tasks.c.merchant_id == merchant_id,
            merchant_tasks.c.parent_audit_run_id == parent_audit_run_id,
            merchant_tasks.c.source_executor_run_id.is_(None),
            merchant_tasks.c.status != "superseded",
        )
        if lever is None:
            q = q.where(merchant_tasks.c.lever.is_(None))
        else:
            q = q.where(merchant_tasks.c.lever == lever)
        rows = await database.fetch_all(
            q.order_by(merchant_tasks.c.created_at.desc())
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "find_executor_parent_task_candidates failed merchant=%s "
            "audit=%s lever=%s: %s",
            merchant_id, parent_audit_run_id, lever, str(exc)[:200],
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


async def list_pending_audit_tasks_excluding_run(
    *,
    merchant_id: str,
    exclude_audit_run_id: str,
) -> List[Dict[str, Any]]:
    """Persistent-workspace reconciliation (page-usability Step 1): the prior
    `pending` tasks that came from an audit run OTHER than the one just
    materialized. The caller decides which to close based on whether the new
    audit actually re-covered each task's product (scope-aware — an audit of
    SKU-B must NOT close SKU-A's still-valid tasks).

    Scope is deliberately narrow:
      - `pending` only — never `in_progress` (don't steal in-flight work).
      - non-NULL `parent_audit_run_id` — standing non-audit tasks
        (niche_content / outreach_pitch / sku_evidence; NULL parent) are
        EXEMPT, so the outreach re-verify loop + the merchant's own actions
        survive.
    """
    await ensure_merchant_tasks_table()
    try:
        q = merchant_tasks.select().where(
            merchant_tasks.c.merchant_id == merchant_id,
            merchant_tasks.c.status == "pending",
            merchant_tasks.c.parent_audit_run_id.isnot(None),
            merchant_tasks.c.parent_audit_run_id != exclude_audit_run_id,
        )
        rows = await database.fetch_all(q)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_pending_audit_tasks_excluding_run failed merchant=%s "
            "run=%s: %s",
            merchant_id, exclude_audit_run_id, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def mark_task_superseded(
    *,
    task_id: str,
    superseded_by_task_id: Optional[str] = None,
) -> bool:
    """Q-P0-2: flip a pending task to `status='superseded'`. When a newer task
    replaces it, `superseded_by_task_id` points at that task; for re-audit
    reconciliation (a dropped task with no direct replacement) it stays NULL.
    Idempotent; a no-op when the task is already terminal (done / dismissed /
    failed / superseded). Returns True when the UPDATE matched a row."""
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


# Levers whose action is ACCOUNT-WIDE, not per-product — submitting a sitemap /
# requesting indexing / fixing attribution / completing onboarding is one job for
# the whole store, not one per SKU. When such an action is emitted once per product
# (each carrying that product's key + a " — {product}" title suffix), it reads as N
# duplicates. Collapse them to one. Per-product levers (sku_enrichment,
# content_publishing, niche_content, editorial_outreach, …) are NOT here — they stay
# one-per-product.
_BRAND_LEVEL_LEVERS = frozenset({
    "indexing_acceleration",
    "general_recommendation",
    "pivota_integration",
})


def _dedup_base_title(title: str) -> str:
    """Strip the trailing ' — {product}' disambiguation suffix so the same
    brand-level action repeated across SKUs shares one identity. Brand-level base
    titles don't naturally contain ' — ', so only the per-product suffix is removed."""
    t = (title or "").strip()
    if " — " in t:
        t = t.rsplit(" — ", 1)[0].strip()
    return t.lower()


def canonical_action_identity(
    *,
    lever: Optional[str],
    title: Optional[str],
    target_host: Optional[str] = None,
    product_key: Optional[str] = None,
) -> tuple:
    """The identity that makes two actions THE SAME action across audit runs.

    Extracted from dedupe_pending_tasks, which has always used it to collapse
    duplicates, and now shared with the recovery key so the two cannot drift.
    That sharing is the point: a recovery key that disagreed with the dedupe
    rule would split one merchant's fix into two keys, or merge two fixes into
    one, and either way the Prove stage would attribute an outcome to the
    wrong thing.

    Brand-level levers collapse across products — one account-wide action, not
    N per-SKU copies — which is why product_key drops out and the per-product
    title suffix is stripped.
    """
    lv = (lever or "").lower()
    host = str(target_host or "").lower()
    if lv in _BRAND_LEVEL_LEVERS:
        return (lv, _dedup_base_title(title or ""), host, "")
    return (
        lv,
        (title or "").strip().lower(),
        host,
        str(product_key or "").lower(),
    )


def recovery_identity(
    *,
    lever: Optional[str],
    target_host: Optional[str] = None,
    product_key: Optional[str] = None,
) -> tuple:
    """What makes two actions THE SAME GAP across audit runs.

    DELIBERATELY EXCLUDES THE TITLE, which is what the first cut got wrong.
    Titles are rendered per run and splice in run-variable data — counts
    ("Reach out to 3 matched creators", "4 content brief(s) drafted"), a
    competitor the model named this time, a ranked host, a product label
    truncated at 48 characters. Hashing one produced a NEW key at every audit,
    so a fix shipped in week 1 and an order in week 3 could never match and the
    join silently returned nothing. task_queue_service already tolerates this
    "title drift" by skipping links rather than trusting titles.

    What is stable is the gap: which lever, on whose host, for which product.
    Two actions sharing all three ARE the same recovery to a merchant, which
    is exactly the granularity an attributed order needs.

    NOT the same function as canonical_action_identity, and not merged with
    it. That one answers "is this the same TASK ROW" for list-time dedupe and
    must keep using the title, or it would collapse genuinely distinct tasks.
    This answers "is this the same GAP". Different questions, different rules —
    pretending otherwise is what produced a key nobody could rely on.
    """
    return (
        (lever or "").strip().lower(),
        str(target_host or "").strip().lower(),
        str(product_key or "").strip().lower(),
    )


def recovery_key_for(
    *,
    merchant_id: str,
    lever: Optional[str],
    target_host: Optional[str] = None,
    product_key: Optional[str] = None,
) -> str:
    """A stable, opaque handle for ONE merchant's ONE recovery gap.

    This is the join commerce_interactions never had: that table records a
    complete agent loop — prompt, click, cart, order — with no reference to
    the finding or action that preceded it, so a conversion could be observed
    and never attributed.

    DERIVED, NOT RANDOM, so it survives a re-audit: the same gap found again
    next week yields the same key, which is what lets "this fix worked" span
    the weeks between shipping a change and an order landing.

    Merchant-scoped so two merchants with identical gaps never collide, and
    hashed because the key rides on a published link and must not carry the
    merchant id, the host, the product, or what we advised.
    """
    identity = recovery_identity(
        lever=lever, target_host=target_host, product_key=product_key,
    )
    material = "\x1f".join([str(merchant_id or "").strip().lower(), *identity])
    return "rk_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def dedupe_pending_tasks(*, merchant_id: str) -> int:
    """Lazy, idempotent backlog cleanup (page-usability Step 1): collapse
    duplicate PENDING tasks that share a canonical identity
    (lever, title, evidence.target_host, evidence.product_key) down to the
    NEWEST one, superseding the older copies onto it.

    Self-healing: run best-effort whenever the persistent action plan is listed,
    so the queue de-duplicates itself without waiting for the next audit's
    reconciliation (the old `latest_completed` scope was masking an accumulated
    pile of identical pending tasks across runs; the persistent scope surfaced
    them). A no-op once clean — no duplicate group → no writes. Only PENDING rows
    are touched (in_progress / terminal rows are never collapsed). Returns the
    number of rows superseded."""
    await ensure_merchant_tasks_table()
    try:
        rows = [
            _row_to_dict(r)
            for r in (
                await database.fetch_all(
                    merchant_tasks.select().where(
                        merchant_tasks.c.merchant_id == merchant_id,
                        merchant_tasks.c.status == "pending",
                    )
                )
                or []
            )
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dedupe_pending_tasks fetch failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return 0

    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for t in rows:
        ev = t.get("evidence") or {}
        # The SAME function recovery_key_for derives from. Two copies of this
        # rule would let a fix's key and its dedupe identity disagree.
        key = canonical_action_identity(
            lever=t.get("lever"),
            title=t.get("title"),
            target_host=ev.get("target_host"),
            product_key=ev.get("product_key"),
        )
        groups.setdefault(key, []).append(t)

    superseded = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        # Keep the newest (created_at desc — ISO strings sort lexically); collapse
        # the older identical copies onto it.
        ordered = sorted(
            group, key=lambda r: r.get("created_at") or "", reverse=True
        )
        keep = ordered[0]
        for dup in ordered[1:]:
            if await mark_task_superseded(
                task_id=dup["task_id"],
                superseded_by_task_id=keep["task_id"],
            ):
                superseded += 1
    return superseded
