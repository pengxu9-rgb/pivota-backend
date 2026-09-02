"""
Platform Import Tasks - EPIC‑2 Skeleton

Tracks catalog import jobs for Platform merchants. This module is additive
and does not modify any existing v1 flows.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, JSON, Text
from sqlalchemy.sql import func
from db.database import metadata, database
import json
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta

platform_import_tasks = Table(
    "platform_import_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("source_type", String(50), nullable=False),  # connector | report | unknown
    Column("connector", String(100), nullable=True),  # e.g. linnworks, channeladvisor
    Column("status", String(50), nullable=False, default="pending"),
    Column("counts", JSON, nullable=True),  # {"total": int, "succeeded": int, "failed": int}
    Column("error", Text, nullable=True),
    Column("saga_id", String(100), nullable=True),
    Column("attempt", Integer, nullable=False, default=0),
    Column("next_run_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)


async def create_import_task(
    merchant_id: str,
    source_type: str,
    connector: Optional[str] = None,
    saga_id: Optional[str] = None,
) -> int:
    """Create a new ImportTask in pending state and return its ID."""
    values = {
        "merchant_id": merchant_id,
        "source_type": source_type,
        "connector": connector,
        "status": "pending",
        "attempt": 0,
        "saga_id": saga_id,
    }
    query = platform_import_tasks.insert().values(**values)
    task_id = await database.execute(query)
    return int(task_id)


async def get_import_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single ImportTask by ID."""
    query = platform_import_tasks.select().where(platform_import_tasks.c.id == task_id)
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def list_import_tasks_for_merchant(merchant_id: str) -> List[Dict[str, Any]]:
    """List ImportTasks for a merchant, newest first."""
    query = (
        platform_import_tasks.select()
        .where(platform_import_tasks.c.merchant_id == merchant_id)
        .order_by(platform_import_tasks.c.created_at.desc())
    )
    rows = await database.fetch_all(query)
    return [dict(r) for r in rows]


async def get_next_scheduled_task() -> Optional[Dict[str, Any]]:
    """
    Fetch the next ImportTask that is ready to run.

    For EPIC‑3 we consider tasks that are:
    - in `pending` state (never run), or
    - in `retry_scheduled` state whose next_run_at is due or unset.
    """
    now = datetime.utcnow()
    query = (
        platform_import_tasks.select()
        .where(
            platform_import_tasks.c.status.in_(["pending", "retry_scheduled"])
            & (
                (platform_import_tasks.c.next_run_at.is_(None))
                | (platform_import_tasks.c.next_run_at <= now)
            )
        )
        .order_by(platform_import_tasks.c.created_at.asc())
        .limit(1)
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def update_import_task_status(
    task_id: int,
    status: str,
    counts: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    next_run_at: Optional[datetime] = None,
    attempt: Optional[int] = None,
) -> bool:
    """Update ImportTask status and optional metadata."""
    values: Dict[str, Any] = {
        "status": status,
        "updated_at": datetime.now(),
    }
    if counts is not None:
        values["counts"] = counts
    if error is not None:
        values["error"] = error
    if next_run_at is not None:
        values["next_run_at"] = next_run_at
    if attempt is not None:
        values["attempt"] = attempt

    query = platform_import_tasks.update().where(platform_import_tasks.c.id == task_id).values(
        **values
    )
    await database.execute(query)
    return True


# ---------------------------------------------------------------------------
# Atomic claim + stale recovery
#
# `get_next_scheduled_task` + `update_import_task_status(status="running")` is a
# read-then-write with no guard: two runners that read the same `pending` row
# both proceed, both import the same Shopify catalog, and the second one's
# `attempt` bookkeeping overwrites the first's. That was survivable only while
# the single caller was the request-scoped BackgroundTask in
# routes/merchant_api_extensions.py; a scheduler drain tick makes the race real.
#
# These mirror db/product_quality_backfill_jobs.py's claim pair: a conditional
# UPDATE that flips `pending`/`retry_scheduled` -> `running` and RETURNs the row
# only for the caller that actually won it. Everyone else gets None.
#
# Raw SQL (not SQLAlchemy Core) because RETURNING is not compiled for the sqlite
# dialect on the pinned SQLAlchemy 1.4; sqlite itself has supported it since
# 3.35, so the raw statement runs on both backends.
#
# THE COST OF RAW SQL, and why _normalize_claimed_row exists: on databases
# 0.7.0 a raw statement is wrapped in text(), whose compiled._result_columns is
# empty, so the backend short-circuits (`if len(self._column_map) == 0`) and NO
# SQLAlchemy result processor runs. The JSON `counts` column therefore comes
# back as a STR from `RETURNING *`, where the Core select it replaced returned a
# dict — on BOTH backends (asyncpg decodes json/jsonb to str with no codec
# registered; sqlite hands back the raw TEXT).
#
# That is not cosmetic. _process_import_task_record resumes pagination from
# `counts["shopify_next_page_info"]` behind an `isinstance(..., dict)` test, so
# an unconverted str silently resets progress: a catalog larger than
# SHOPIFY_MAX_PRODUCTS_PER_RUN re-imports its first chunk forever, and because
# the queue is FIFO on created_at with max_instances=1, that one row starves
# every other merchant's import. Convert at the boundary, once.
# ---------------------------------------------------------------------------

# A `running` row older than this is presumed abandoned (Cloud Run revision swap
# or scale-down killed the process mid-import) and is put back on the queue.
#
# WHAT MAKES THIS SAFE is the Shopify import's per-page heartbeat: it calls
# update_import_task_status(status="running") after every page
# (jobs/catalog_import_worker.py), which refreshes `updated_at`. So the window is
# measured from the LAST PAGE, not from the claim, and a healthy multi-hour
# import is never mistaken for an abandoned one even though
# SHOPIFY_MAX_RUNTIME_SECONDS is much smaller than this value.
#
# That heartbeat is therefore load-bearing, not redundant bookkeeping: delete it
# and this reaper starts handing live imports to a second runner, which is the
# double-run the atomic claim exists to prevent. 900s is sized to survive one
# slow page (a 30s HTTP timeout plus retries plus up to 250 upserts), not a
# whole run.
#
# The cost is honest and accepted: a genuinely dead import takes 15 minutes to
# recover instead of 5. A shorter window is not worth it now that a drain tick
# is standing by to claim whatever gets requeued.
STALE_RUNNING_AFTER_SECONDS = 900

# The drain lane is deliberately narrower than the table. `platform_import_tasks`
# also carries `amazon_orders` / `orders_report` / `report` rows (which write
# platform_orders and have neither a runtime cap nor a heartbeat) and a catch-all
# `unknown` row written once per platform onboarding. None of those were ever
# executed by a background runner, none is covered by the tests here, and the
# non-heartbeating ones are exactly the shape this reaper could requeue mid-run.
# Ship the lane that is understood; widen it deliberately, per source_type.
DRAIN_SOURCE_TYPE = "connector"
DRAIN_CONNECTOR = "shopify"


def _drain_scope():
    """The (source_type, connector) predicate every drain-path query shares."""
    return (platform_import_tasks.c.source_type == DRAIN_SOURCE_TYPE) & (
        platform_import_tasks.c.connector == DRAIN_CONNECTOR
    )


def _normalize_claimed_row(row: Any) -> Optional[Dict[str, Any]]:
    """Give a `RETURNING *` row the same shape the Core select produced.

    Only `counts` is converted: it is the one column a caller reads back off a
    claimed row. A value that is not valid JSON (or not an object) degrades to
    `{}` — the same thing _process_import_task_record's isinstance test would
    have produced — rather than raising inside the claim.
    """
    if row is None:
        return None
    payload = dict(row)
    counts = payload.get("counts")
    if isinstance(counts, (str, bytes)):
        try:
            decoded = json.loads(counts)
        except (ValueError, TypeError):
            decoded = None
        payload["counts"] = decoded if isinstance(decoded, dict) else {}
    return payload


async def claim_import_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Atomically claim ONE ImportTask by id; None if someone else got it.

    Ignores `next_run_at`: this is the explicit-kick path (a merchant pressing
    Sync), which has always bypassed retry backoff, and the caller named a
    specific row rather than asking for whatever is due.

    The winner's row comes back with `attempt` already incremented, so callers
    must NOT increment it again.
    """
    # The SQL is passed INLINE, not via a local `query = """..."""`. The
    # Postgres dialect gate (tests/test_repo_sql_prepare_postgres.py) collects
    # string literals at `database.*` call sites and resolves a bare Name only
    # against MODULE-level constants — a function-local assignment is invisible
    # to it, so that shape ships raw SQL that nothing ever plans against a real
    # schema. Verified by running collect_statements() over this file.
    row = await database.fetch_one(
        """
        UPDATE platform_import_tasks
        SET status = 'running',
            attempt = attempt + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :task_id
          AND status IN ('pending', 'retry_scheduled')
        RETURNING *
        """,
        {"task_id": task_id},
    )
    return _normalize_claimed_row(row)


async def get_next_drainable_task() -> Optional[Dict[str, Any]]:
    """`get_next_scheduled_task`, narrowed to the lane the drain tick runs.

    Separate from get_next_scheduled_task so that function keeps its original
    whole-table meaning for any caller that wants it.
    """
    now = datetime.utcnow()
    row = await database.fetch_one(
        platform_import_tasks.select()
        .where(
            _drain_scope()
            & platform_import_tasks.c.status.in_(["pending", "retry_scheduled"])
            & (
                (platform_import_tasks.c.next_run_at.is_(None))
                | (platform_import_tasks.c.next_run_at <= now)
            )
        )
        .order_by(platform_import_tasks.c.created_at.asc())
        .limit(1)
    )
    return dict(row) if row else None


async def claim_next_import_task() -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest ready ImportTask, or None.

    Two statements, like `claim_next_quality_backfill_job`: pick a candidate
    (SQLAlchemy Core, so the `next_run_at` datetime comparison stays dialect-
    correct), then claim it conditionally. A racing runner that grabbed the same
    candidate has already moved it out of `pending`/`retry_scheduled`, so the
    guarded UPDATE matches nothing and we return None rather than double-running
    it. `next_run_at` needs no re-check in the UPDATE: the only writer that can
    push it further out also changes `status`, which the UPDATE does check.
    """
    candidate = await get_next_drainable_task()
    if not candidate:
        return None
    return await claim_import_task(int(candidate["id"]))


async def requeue_stale_import_tasks(
    *,
    stale_after_seconds: int = STALE_RUNNING_AFTER_SECONDS,
    limit: int = 5,
    max_attempt: Optional[int] = None,
) -> int:
    """Put abandoned `running` ImportTasks back on the queue; return how many.

    Without this, a revision swap mid-import strands the row in `running`
    forever: the drain query only looks at `pending`/`retry_scheduled`, and the
    de-dupe branch in routes/merchant_api_extensions.py treats `running` as
    "already in progress", so the merchant's Sync button keeps reporting a job
    that nothing is executing.

    Scoped to the drain lane: requeueing a row the drain tick will never claim
    is pure churn, and the non-Shopify branches are the ones with no heartbeat.

    `max_attempt` is a POISON-PILL BOUND, and it is not optional in practice.
    Every attempt cutoff in the worker lives inside an `except` handler, so a
    task that KILLS its process — OOM, or a revision swap, i.e. exactly what
    this reaper is for — never reaches one. Without a bound here, a row that
    reliably OOMs the instance would be requeued, claimed, and OOM again every
    five minutes forever. Rows past the bound are left `running` for an operator
    to look at, which is the same place they sat before this reaper existed.

    The per-row UPDATE re-asserts `status = 'running'`, so a task that finished
    between the SELECT and the UPDATE is left alone.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=max(60, stale_after_seconds))
    predicate = (
        _drain_scope()
        & (platform_import_tasks.c.status == "running")
        & (platform_import_tasks.c.updated_at.isnot(None))
        & (platform_import_tasks.c.updated_at < cutoff)
    )
    if max_attempt is not None:
        predicate = predicate & (platform_import_tasks.c.attempt < max_attempt)
    stale_rows = await database.fetch_all(
        platform_import_tasks.select()
        .where(predicate)
        .order_by(platform_import_tasks.c.updated_at.asc())
        .limit(max(1, limit))
    )

    requeued = 0
    for stale in stale_rows:
        row = await database.fetch_one(
            """
            UPDATE platform_import_tasks
            SET status = 'retry_scheduled',
                next_run_at = CURRENT_TIMESTAMP,
                error = 'stale_running_recovered',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :task_id
              AND status = 'running'
            RETURNING id
            """,
            {"task_id": int(dict(stale)["id"])},
        )
        if row is not None:
            requeued += 1
    return requeued
