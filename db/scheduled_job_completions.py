"""Durable "this cron slot's run finished" markers for in-process scheduled jobs.

WHY THIS EXISTS. APScheduler runs inside the Cloud Run `worker` service with an
in-memory job store, and the worker is rolled on every merge to main. A run that is
in flight when its revision shuts down is cancelled (services/scheduler_job_runner
logs it "ABANDONED as a zombie"), and the new revision computes the job's next fire
time from its own boot, so the slot that was cut is simply gone until tomorrow. The
run registry on /__scheduler_health is in-process too and dies with the revision.

Measured on prod (2026-09-27): `nightly_index_health` was cut this way on 09-23
(04:00:53, revision worker-00162-sxn created 04:00:11) and 09-27 (04:04:15,
worker-00248-hdx created 04:03:42), 2 of the ~30 runs in the 30-day log window. On
09-27 the batch had reached ~19k of ~23.5k content keys, and 63 rows stayed
blocked on a cleared extractor_regression until they were recomputed by hand.

One row per job: the latest cron slot (`scheduled_for`) whose run completed. A job
that must not lose a slot records here when it finishes, and a catch-up tick
compares it against the slot that should have run (jobs/nightly_index_health_job.py
`run_nightly_index_health_catchup`). The marker only ever moves forward.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Table, Text

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)


scheduled_job_completions = Table(
    "scheduled_job_completions",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column("scheduled_for", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = guarded_statements([
    """
    CREATE TABLE IF NOT EXISTS scheduled_job_completions (
        job_id         TEXT PRIMARY KEY,
        scheduled_for  TIMESTAMPTZ NOT NULL,
        completed_at   TIMESTAMPTZ NOT NULL
    );
    """,
])


async def ensure_scheduled_job_completions_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_scheduled_job_completions_table",
            logger=logger,
            execute=database.execute,
        )


# Forward-only: a late writer for an OLDER slot (a catch-up that finishes after
# the next night's scheduled run already recorded) must not move the marker back.
_RECORD_COMPLETION_SQL = """
INSERT INTO scheduled_job_completions (job_id, scheduled_for, completed_at)
VALUES (:job_id, :scheduled_for, :completed_at)
ON CONFLICT (job_id) DO UPDATE SET
    scheduled_for = EXCLUDED.scheduled_for,
    completed_at = EXCLUDED.completed_at
WHERE scheduled_job_completions.scheduled_for <= EXCLUDED.scheduled_for
"""

_READ_COMPLETION_SQL = (
    "SELECT scheduled_for FROM scheduled_job_completions WHERE job_id = :job_id"
)


def _as_utc(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        # SQLite hands a TIMESTAMPTZ back as text.
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def record_completion(
    job_id: str, scheduled_for: datetime, *, completed_at: Optional[datetime] = None,
) -> None:
    """Mark `scheduled_for`'s run of `job_id` finished. Raises on a DB error —
    callers decide whether that is fatal."""
    await ensure_scheduled_job_completions_table()
    await database.execute(
        _RECORD_COMPLETION_SQL,
        {
            "job_id": job_id,
            "scheduled_for": _as_utc(scheduled_for),
            "completed_at": _as_utc(completed_at or datetime.now(timezone.utc)),
        },
    )


async def last_completed_slot(job_id: str) -> Optional[datetime]:
    """The latest slot recorded complete for `job_id`, or None if none ever was.
    Raises on a DB error, so a caller can tell "never" from "could not read"."""
    await ensure_scheduled_job_completions_table()
    row = await database.fetch_one(_READ_COMPLETION_SQL, {"job_id": job_id})
    if not row:
        return None
    return _as_utc(dict(row)["scheduled_for"])
