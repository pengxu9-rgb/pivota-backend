from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, String, Table
from sqlalchemy.sql import func

from db.database import IS_POSTGRES, database, metadata


logger = logging.getLogger(__name__)


product_quality_backfill_jobs = Table(
    "product_quality_backfill_jobs",
    metadata,
    Column("job_id", String(64), primary_key=True),
    Column("merchant_id", String(100), nullable=False, index=True),
    Column("platform", String(50), nullable=True),
    Column("status", String(32), nullable=False, default="queued"),
    Column("requested_by", String(255), nullable=True),
    Column("requested_at", DateTime, server_default=func.now(), nullable=False),
    Column("started_at", DateTime, nullable=True),
    Column("finished_at", DateTime, nullable=True),
    Column("total_candidates", Integer, nullable=False, default=0),
    Column("processed", Integer, nullable=False, default=0),
    Column("skipped", Integer, nullable=False, default=0),
    Column("failed", Integer, nullable=False, default=0),
    Column("force_refresh", Boolean, nullable=False, default=False),
    Column("missing_only", Boolean, nullable=False, default=True),
    Column("errors_sample", JSON, nullable=True),
    extend_existing=True,
)

Index(
    "idx_quality_backfill_jobs_merchant_status_requested",
    product_quality_backfill_jobs.c.merchant_id,
    product_quality_backfill_jobs.c.status,
    product_quality_backfill_jobs.c.requested_at,
)

_DDL_READY = False
_DDL_LOCK: Optional[asyncio.Lock] = None


def _ddl_lock() -> asyncio.Lock:
    global _DDL_LOCK
    if _DDL_LOCK is None:
        _DDL_LOCK = asyncio.Lock()
    return _DDL_LOCK


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _json_type_sql() -> str:
    return "JSONB" if IS_POSTGRES else "JSON"


def _json_param(value: Any) -> str:
    return json.dumps(value if value is not None else [])


def _normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    payload = dict(row)
    for key in ("requested_at", "started_at", "finished_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            payload[key] = value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return payload


async def ensure_product_quality_backfill_jobs_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _ddl_lock():
        if _DDL_READY:
            return
        try:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS product_quality_backfill_jobs (
                  job_id VARCHAR(64) PRIMARY KEY,
                  merchant_id VARCHAR(100) NOT NULL,
                  platform VARCHAR(50),
                  status VARCHAR(32) NOT NULL DEFAULT 'queued',
                  requested_by VARCHAR(255),
                  requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at TIMESTAMPTZ,
                  finished_at TIMESTAMPTZ,
                  total_candidates INTEGER NOT NULL DEFAULT 0,
                  processed INTEGER NOT NULL DEFAULT 0,
                  skipped INTEGER NOT NULL DEFAULT 0,
                  failed INTEGER NOT NULL DEFAULT 0,
                  force_refresh BOOLEAN NOT NULL DEFAULT FALSE,
                  missing_only BOOLEAN NOT NULL DEFAULT TRUE,
                  errors_sample """ + _json_type_sql() + """
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_quality_backfill_jobs_merchant_status_requested ON product_quality_backfill_jobs(merchant_id, status, requested_at);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS platform VARCHAR(50);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS requested_by VARCHAR(255);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS total_candidates INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS processed INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS skipped INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS failed INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS force_refresh BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS missing_only BOOLEAN NOT NULL DEFAULT TRUE;",
                f"ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS errors_sample {_json_type_sql()};",
            ]
            for statement in statements:
                await database.execute(statement)
        except Exception as exc:
            logger.warning("ensure_product_quality_backfill_jobs_table failed: %s", str(exc)[:200])
            return
        _DDL_READY = True


async def create_quality_backfill_job(
    *,
    merchant_id: str,
    platform: Optional[str],
    requested_by: Optional[str],
    force_refresh: bool = False,
    missing_only: bool = True,
) -> Dict[str, Any]:
    await ensure_product_quality_backfill_jobs_table()
    job_id = f"qbf_{uuid.uuid4().hex[:20]}"
    requested_at = _utcnow()

    await database.execute(
        product_quality_backfill_jobs.insert().values(
            job_id=job_id,
            merchant_id=merchant_id,
            platform=platform,
            status="queued",
            requested_by=requested_by,
            requested_at=requested_at,
            force_refresh=force_refresh,
            missing_only=missing_only,
            total_candidates=0,
            processed=0,
            skipped=0,
            failed=0,
            errors_sample=[],
        )
    )
    row = await database.fetch_one(
        product_quality_backfill_jobs.select().where(product_quality_backfill_jobs.c.job_id == job_id)
    )
    return _normalize_row(row) or {}


# ---------------------------------------------------------------------------
# Unattended enqueue dedupe.
#
# Every sync enqueues a backfill (the readiness hooks in
# services/catalog_sync_service and routes/universal_product_sync), and the
# gateway's catalog auto-sync reaches the second hook through
# routes/platform_products_sync_api with force_refresh=True — deliberately, so a
# product that newly gained a field gets rescored. The cost, measured on prod
# 2026-09-02: one 20-product Wix store synced up to 16 times a day and wrote 20
# fresh product_quality_snapshot rows per sync (20 to 320 a day) for products
# that had not changed, and those rows became the whole recent window of the
# dead_quality_component invariant.
#
# "Skip when the sync changed nothing" is not available at the hook: the sync
# stats count rows touched, not rows changed. So this is a per-(merchant,
# platform) cooldown for UNATTENDED requests only. A merchant pressing Sync is
# never deduped — that request is the delivery path for their own edits. A
# request stronger than the job it would collide with (force_refresh after a
# missing-only job) is never deduped either, for the same reason the universal
# hook honours force_refresh at all.
# ---------------------------------------------------------------------------
QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV = "QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS"
DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS = 6 * 3600

# Both twins are module-level constants so tests/test_repo_sql_prepare_postgres.py
# plans the Postgres one. `CAST(:platform AS TEXT)` on both sides: asyncpg
# cannot type a bare parameter that only appears in `IS NULL`.
#
# THE TIME COMPARISON IS IN EPOCH SECONDS, NOT `> CURRENT_TIMESTAMP - interval`.
# `requested_at` is written CLIENT-side as `_utcnow()` into a column that is a
# naive `timestamp` in prod (create_all builds it before migration 054 — see
# tests/test_quality_backfill_partial_update.py). Comparing a naive column to
# `CURRENT_TIMESTAMP - interval` (timestamptz) makes Postgres reinterpret the
# stored UTC wall clock in the SESSION time zone, which nothing in this app
# pins: under America/Los_Angeles the 6h cooldown silently became ~13h, under
# Asia/Shanghai a brand-new job already failed the test and the dedupe was a
# no-op (reviewed and measured on PR #2012). `EXTRACT(EPOCH FROM ...)` reads a
# naive timestamp as UTC and a timestamptz as itself, so this is correct under
# BOTH column declarations and any session zone. _REQUEUE_STALE_SQL does not
# need this: it compares `started_at`, which claim_quality_backfill_job writes
# with CURRENT_TIMESTAMP, so that column round-trips in the session zone.
#
# Strength is in the WHERE: only a job at least as strong as the request
# qualifies (a force_refresh job satisfies any request; a missing-only job
# satisfies only a missing-only request), so an intervening weaker attended job
# cannot hide a stronger one, and a force_refresh request is never folded into
# a missing-only job. `_utcnow()` truncates to whole seconds, so same-second
# ties are common; `job_id DESC` is a determinism tiebreak only — any row that
# qualifies is a correct fold target, the reported id may be a same-second
# sibling.
_RECENT_JOB_SQL = """
    SELECT job_id
    FROM product_quality_backfill_jobs
    WHERE merchant_id = :merchant_id
      AND (CAST(:platform AS TEXT) IS NULL OR platform = CAST(:platform AS TEXT))
      AND status IN ('queued', 'running', 'completed')
      AND (force_refresh OR NOT CAST(:force_refresh AS BOOLEAN))
      AND EXTRACT(EPOCH FROM requested_at)
          > EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) - CAST(:cooldown_seconds AS DOUBLE PRECISION)
    ORDER BY requested_at DESC, job_id DESC
    LIMIT 1
    """

# SQLite stores `_utcnow()` as 'YYYY-MM-DD HH:MM:SS[.ffffff]' and datetime('now')
# is UTC in the same format, so the string comparison is exact. force_refresh
# is an INTEGER 0/1 there; `NOT :force_refresh` with a bound bool is fine.
_RECENT_JOB_SQL_SQLITE = """
    SELECT job_id
    FROM product_quality_backfill_jobs
    WHERE merchant_id = :merchant_id
      AND (CAST(:platform AS TEXT) IS NULL OR platform = CAST(:platform AS TEXT))
      AND status IN ('queued', 'running', 'completed')
      AND (force_refresh OR NOT :force_refresh)
      AND requested_at > datetime('now', :cooldown_window)
    ORDER BY requested_at DESC, job_id DESC
    LIMIT 1
    """


def quality_backfill_enqueue_cooldown_seconds() -> int:
    """0 disables the dedupe. An unparseable value falls back to the default
    rather than to 0 — a typo must not silently turn the churn back on."""
    raw = os.getenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS


async def _recent_quality_backfill_job_id(
    merchant_id: str, platform: Optional[str], force_refresh: bool, cooldown_seconds: int
) -> Optional[str]:
    values = {
        "merchant_id": merchant_id,
        "platform": platform or None,
        "force_refresh": bool(force_refresh),
    }
    if IS_POSTGRES:
        row = await database.fetch_one(
            _RECENT_JOB_SQL, {**values, "cooldown_seconds": int(cooldown_seconds)}
        )
    else:
        row = await database.fetch_one(
            _RECENT_JOB_SQL_SQLITE,
            {**values, "cooldown_window": f"-{int(cooldown_seconds)} seconds"},
        )
    return str(row["job_id"]) if row is not None else None


async def enqueue_quality_backfill_if_needed(
    *,
    merchant_id: str,
    platform: Optional[str],
    requested_by: Optional[str],
    force_refresh: bool = False,
    missing_only: bool = True,
    unattended: bool = False,
    cooldown_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Enqueue a backfill unless an unattended request would only repeat one.

    Returns {"job", "enqueued", "reason"}. `job` is the full job row on BOTH
    paths: the created job, or the recent job the request was folded into.
    Attended requests (unattended=False) always enqueue, exactly as
    create_quality_backfill_job did.
    """
    await ensure_product_quality_backfill_jobs_table()
    seconds = (
        quality_backfill_enqueue_cooldown_seconds()
        if cooldown_seconds is None
        else max(0, int(cooldown_seconds))
    )
    if unattended and seconds > 0:
        recent_id = await _recent_quality_backfill_job_id(
            merchant_id, platform, bool(force_refresh), seconds
        )
        if recent_id is not None:
            recent = await get_quality_backfill_job(recent_id)
            if recent is not None:
                # Not silent: an operator who curls the internal sync API to
                # force a rescore needs to be able to read that it was folded.
                logger.info(
                    "quality backfill enqueue folded into recent job %s "
                    "(merchant=%s platform=%s requested_by=%s cooldown=%ss)",
                    recent_id, merchant_id, platform, requested_by, seconds,
                )
                return {
                    "job": recent,
                    "enqueued": False,
                    "reason": "recent_job_within_cooldown",
                }
    job = await create_quality_backfill_job(
        merchant_id=merchant_id,
        platform=platform,
        requested_by=requested_by,
        force_refresh=force_refresh,
        missing_only=missing_only,
    )
    return {"job": job, "enqueued": True, "reason": "created"}


async def get_quality_backfill_job(job_id: str) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    row = await database.fetch_one(
        product_quality_backfill_jobs.select().where(product_quality_backfill_jobs.c.job_id == job_id)
    )
    return _normalize_row(row)


async def get_active_quality_backfill_job(
    merchant_id: str,
    *,
    platform: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    query = product_quality_backfill_jobs.select().where(
        (product_quality_backfill_jobs.c.merchant_id == merchant_id)
        & (product_quality_backfill_jobs.c.status.in_(["queued", "running"]))
    )
    if platform:
        query = query.where(product_quality_backfill_jobs.c.platform == platform)
    query = query.order_by(product_quality_backfill_jobs.c.requested_at.desc()).limit(1)
    row = await database.fetch_one(query)
    return _normalize_row(row)


async def claim_quality_backfill_job(job_id: str) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    row = await database.fetch_one(
        """
        UPDATE product_quality_backfill_jobs
        SET status = 'running',
            started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
        WHERE job_id = :job_id
          AND status = 'queued'
        RETURNING *
        """,
        {"job_id": job_id},
    )
    return _normalize_row(row)


async def claim_next_quality_backfill_job() -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    queued = await database.fetch_one(
        """
        SELECT job_id
        FROM product_quality_backfill_jobs
        WHERE status = 'queued'
        ORDER BY requested_at ASC
        LIMIT 1
        """
    )
    if not queued:
        return None
    queued_payload = dict(queued)
    return await claim_quality_backfill_job(str(queued_payload.get("job_id") or ""))


# ---------------------------------------------------------------------------
# The three UPDATE ... RETURNING statements below, spelled out per dialect.
#
# WHY NOT ONE f-STRING. These used to be built with an f-string over a
# `_json_assignment_sql()` helper (deleted with this change, it had no other
# caller) — which reads fine and is invisible to
# tests/test_repo_sql_prepare_postgres.py:
# that sweep resolves a `database.*` first argument only when it is an
# `ast.Constant` or a MODULE-level name, and an f-string is neither. Three
# statements on the live backfill path were therefore never PREPAREd against the
# real schema. Written out as module-level constants they are collected, and the
# `if IS_POSTGRES:` split at each call site is a shape `_non_postgres_nodes()`
# already understands, so the SQLite twin is skipped rather than planned.
#
# WHY THE DIALECTS CANNOT BE UNIFIED. The only difference is the errors_sample
# assignment: Postgres needs `CAST(:errors_sample AS JSONB)`, SQLite must not
# have it. SQLite's CAST resolves the unknown type name JSONB to NUMERIC
# affinity, so `CAST('[{"error": ...}]' AS JSONB)` silently stores 0 — the JSON
# payload is destroyed, without an error. The split is load-bearing.
#
# WHY errors_sample IS CAST ON BOTH SIDES OF ITS COALESCE. This column is
# declared THREE times and they do not agree: migration 054 and this module's own
# runtime DDL say JSONB, the SQLAlchemy Column above says JSON. Postgres has no
# implicit json<->jsonb coercion, so `COALESCE(CAST(:x AS JSONB), errors_sample)`
# is unplannable wherever the column landed as json — which is any database built
# model-first, including the dialect gate's own fixture (create_all runs before
# the migrations, whose CREATE TABLE IF NOT EXISTS is then a no-op). Casting both
# arms makes the statement correct under either declaration. A plain assignment
# does not need this and does not have it: `errors_sample = CAST(:x AS JSONB)`
# reaches the column through an assignment cast, which json accepts.
#
# The drift itself is NOT fixed here — changing the model's column type rebuilds
# every create_all-backed test database in the repo, which is its own change.
#
# WHY COALESCE. `update_*` and `complete_*` used to join a list of `assignments`
# built from whichever keyword arguments were not None, which is what made their
# SQL dynamic. `COALESCE(:param, column)` expresses the same rule — NULL means
# leave this column alone — in one static statement. Callers still pass None for
# "do not touch" (routes/merchant_products.py relies on it, forwarding
# `active_job.get(...)` straight through), so the contract is unchanged for every
# caller. One nuance on errors_sample: where COALESCE keeps the existing value it
# now REWRITES it with itself, which is byte-identical on a jsonb column but not
# on a json one (jsonb normalises key order, drops duplicate keys, and rejects a
# stored \u0000). Production and the runtime DDL are jsonb, so this is inert
# there; it is only reachable on a model-first database, and it is another reason
# to close the drift above rather than live with it.

# `RETURNING job_id` on both requeue variants is NOT decoration: it is what lets
# the helper below count the rows it moved, uniformly on both engines. Dropping
# it makes the helper return 0 on Postgres while still doing the work — the
# asyncpg backend's `execute` is `fetchval`, which yields None for an UPDATE
# with nothing to return. (The SQLite backend answers correctly either way: it
# reads `cursor.lastrowid`, which is 0 on the fresh connection `databases`
# acquires per statement, and falls back to `rowcount`. Verified, because a
# review claimed the opposite.)
_REQUEUE_STALE_SQL = """
    UPDATE product_quality_backfill_jobs
    SET status = 'queued',
        started_at = NULL,
        finished_at = NULL,
        total_candidates = 0,
        processed = 0,
        skipped = 0,
        failed = 0,
        errors_sample = CAST(:errors_sample AS JSONB)
    WHERE job_id IN (
        SELECT job_id
        FROM product_quality_backfill_jobs
        WHERE status = 'running'
          AND started_at IS NOT NULL
          AND started_at < CURRENT_TIMESTAMP - (:stale_seconds * INTERVAL '1 second')
        ORDER BY started_at ASC
        LIMIT :limit
    )
    RETURNING job_id
    """

_REQUEUE_STALE_SQL_SQLITE = """
    UPDATE product_quality_backfill_jobs
    SET status = 'queued',
        started_at = NULL,
        finished_at = NULL,
        total_candidates = 0,
        processed = 0,
        skipped = 0,
        failed = 0,
        errors_sample = :errors_sample
    WHERE job_id IN (
        SELECT job_id
        FROM product_quality_backfill_jobs
        WHERE status = 'running'
          AND started_at IS NOT NULL
          AND started_at < datetime('now', :stale_window)
        ORDER BY started_at ASC
        LIMIT :limit
    )
    RETURNING job_id
    """

_UPDATE_PROGRESS_SQL = """
    UPDATE product_quality_backfill_jobs
    SET total_candidates = COALESCE(:total_candidates, total_candidates),
        processed = COALESCE(:processed, processed),
        skipped = COALESCE(:skipped, skipped),
        failed = COALESCE(:failed, failed),
        errors_sample = COALESCE(CAST(:errors_sample AS JSONB), CAST(errors_sample AS JSONB))
    WHERE job_id = :job_id
    RETURNING *
    """

_UPDATE_PROGRESS_SQL_SQLITE = """
    UPDATE product_quality_backfill_jobs
    SET total_candidates = COALESCE(:total_candidates, total_candidates),
        processed = COALESCE(:processed, processed),
        skipped = COALESCE(:skipped, skipped),
        failed = COALESCE(:failed, failed),
        errors_sample = COALESCE(:errors_sample, errors_sample)
    WHERE job_id = :job_id
    RETURNING *
    """

# errors_sample is NOT COALESCEd here: completing a job always rewrites it, and
# `_json_param(None)` deliberately yields `[]` so a clean run clears the sample.
_COMPLETE_SQL = """
    UPDATE product_quality_backfill_jobs
    SET status = :status,
        finished_at = :finished_at,
        errors_sample = CAST(:errors_sample AS JSONB),
        total_candidates = COALESCE(:total_candidates, total_candidates),
        processed = COALESCE(:processed, processed),
        skipped = COALESCE(:skipped, skipped),
        failed = COALESCE(:failed, failed)
    WHERE job_id = :job_id
    RETURNING *
    """

_COMPLETE_SQL_SQLITE = """
    UPDATE product_quality_backfill_jobs
    SET status = :status,
        finished_at = :finished_at,
        errors_sample = :errors_sample,
        total_candidates = COALESCE(:total_candidates, total_candidates),
        processed = COALESCE(:processed, processed),
        skipped = COALESCE(:skipped, skipped),
        failed = COALESCE(:failed, failed)
    WHERE job_id = :job_id
    RETURNING *
    """


async def requeue_stale_quality_backfill_jobs(
    *,
    stale_after_seconds: int = 300,
    limit: int = 5,
) -> int:
    """Put timed-out `running` jobs back to `queued`; return how many moved.

    THE COUNT COMES FROM `RETURNING job_id`, NOT FROM THE DRIVER. This used to
    be `int(await database.execute(...) or 0)`, which reports the truth on
    SQLite and 0 on Postgres however many rows it moved: the asyncpg backend
    yields no rowcount for an UPDATE without a RETURNING clause. The requeue
    itself was always correct — only the number was wrong, and the number is
    the only thing anyone reads: `jobs/product_quality_backfill_worker.py` gates
    its "Requeued %s stale quality backfill job(s)" warning on it, so a falsy
    count does not misreport the requeue, it deletes the log line. That warning
    does not fire in production today for a simpler reason —
    `start_product_quality_backfill_loop` has no callers anywhere — so the wrong
    count is what would greet whoever revives that loop.

    THE CUTOFF IS COMPUTED BY THE SERVER, NOT BOUND FROM PYTHON. It used to be
    `_utcnow() - timedelta(...)`, a NAIVE datetime, and asyncpg encodes a naive
    datetime for a `timestamptz` parameter as local wall time — local to THIS
    PROCESS, from its `TZ`, not to the database session. Measured on PG 15 with
    the database and session both UTC, from a process on Pacific time, a
    `utcnow()` bound as `$1::timestamptz` lands SEVEN HOURS in the future. (The
    same probe before and after `SET TIME ZONE` on the connection returns the
    same value, which is how the session was ruled out as the cause.)

    THAT CONVERSION ONLY HAPPENS FOR A `timestamptz` PARAMETER, and `started_at`
    is NOT one in the deployed database, so the old form was not wrong for THAT
    reason. main.py runs its final `metadata.create_all` before
    `run_sql_migrations` and this module is in metadata by then, so the table is
    built from the bare `DateTime` Column as naive `timestamp` and migration
    054's `CREATE TABLE IF NOT EXISTS` — which does say TIMESTAMPTZ — never
    runs. Verified by building a database in that order.

    IT WAS STILL WRONG, FOR A DIFFERENT REASON, AND STILL IS ON ANY NON-UTC
    SESSION. Against the naive column production has, `started_at` is written by
    `CURRENT_TIMESTAMP` (see `claim_quality_backfill_job`) in SESSION-local time,
    while the old `:cutoff` was a client-UTC value — two clocks, compared. The
    server-side form promotes `started_at` with the same session timezone that
    wrote it, so the comparison is self-consistent whatever that timezone is.
    Binding a tz-AWARE datetime would fix the arithmetic only after the
    model/migration drift is resolved: asyncpg REFUSES an aware datetime against
    a plain `timestamp` column (`DataError`), which is what the column is today.
    `CURRENT_TIMESTAMP - interval` binds no datetime at all, so it is correct
    under either declaration and under any session timezone.
    """
    await ensure_product_quality_backfill_jobs_table()
    seconds = max(30, stale_after_seconds)
    values: Dict[str, Any] = {
        "limit": max(1, limit),
        "errors_sample": _json_param(
            [
                {
                    "error": "stale_backfill_job_requeued",
                    "message": "Quality backfill worker requeued a stale running job.",
                }
            ]
        ),
    }
    if IS_POSTGRES:
        rows = await database.fetch_all(
            _REQUEUE_STALE_SQL, {**values, "stale_seconds": seconds}
        )
    else:
        rows = await database.fetch_all(
            _REQUEUE_STALE_SQL_SQLITE, {**values, "stale_window": f"-{seconds} seconds"}
        )
    return len(rows)


async def requeue_quality_backfill_job(job_id: str) -> bool:
    """Put ONE running job back to `queued` (issue #1754 follow-up).

    The scheduler now bounds every run by a deadline and cancels it on
    expiry; a CancelledError bypasses the tick's `except Exception ->
    status='failed'` path, and nothing in production requeues a `running`
    row (`requeue_stale_quality_backfill_jobs` is only called by the dormant
    jobs/product_quality_backfill_worker loop). Without this the merchant's
    job would sit `running` forever and never become audit-ready. Products
    that already have scores are skipped on the re-run (`missing_only`), so a
    requeued job resumes cheaply rather than starting over.
    """
    await ensure_product_quality_backfill_jobs_table()
    row = await database.fetch_one(
        """
        UPDATE product_quality_backfill_jobs
        SET status = 'queued', started_at = NULL, finished_at = NULL
        WHERE job_id = :job_id AND status = 'running'
        RETURNING job_id
        """,
        {"job_id": job_id},
    )
    return row is not None


async def update_quality_backfill_job_progress(
    job_id: str,
    *,
    total_candidates: Optional[int] = None,
    processed: Optional[int] = None,
    skipped: Optional[int] = None,
    failed: Optional[int] = None,
    errors_sample: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()

    if (
        total_candidates is None
        and processed is None
        and skipped is None
        and failed is None
        and errors_sample is None
    ):
        return await get_quality_backfill_job(job_id)

    # NULL means "leave this column alone" — see the COALESCE note above. Note
    # `_json_param` is only applied when a sample was actually passed: it maps
    # None to `[]`, which would clear the stored sample instead of keeping it.
    values: Dict[str, Any] = {
        "job_id": job_id,
        "total_candidates": total_candidates,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "errors_sample": _json_param(errors_sample) if errors_sample is not None else None,
    }

    if IS_POSTGRES:
        row = await database.fetch_one(_UPDATE_PROGRESS_SQL, values)
    else:
        row = await database.fetch_one(_UPDATE_PROGRESS_SQL_SQLITE, values)
    return _normalize_row(row)


async def complete_quality_backfill_job(
    job_id: str,
    *,
    status: str = "completed",
    total_candidates: Optional[int] = None,
    processed: Optional[int] = None,
    skipped: Optional[int] = None,
    failed: Optional[int] = None,
    errors_sample: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    values: Dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "finished_at": _utcnow(),
        "errors_sample": _json_param(errors_sample),
        "total_candidates": total_candidates,
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
    }

    if IS_POSTGRES:
        row = await database.fetch_one(_COMPLETE_SQL, values)
    else:
        row = await database.fetch_one(_COMPLETE_SQL_SQLITE, values)
    return _normalize_row(row)
