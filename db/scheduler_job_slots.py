"""Durable per-day ledger for once-a-day scheduler jobs: attempted, and did it finish.

WHY THIS EXISTS (2026-09-27). APScheduler keeps its schedule in memory. When a deploy
replaces the `worker` revision while a daily cron is running, `stop_scheduler` drains for
3s, the wrapper is cancelled, and the run is abandoned half-done. The NEW instance's cron
then computes its next fire time from "now", which is past today's slot, so the job does not
run again until tomorrow. `misfire_grace_time` cannot help: it only applies to a persistent
job store, and ours is the in-memory default.

That happened to nightly_index_health on 2026-09-23 (killed 53s in) and 2026-09-27 (killed
4m15s in, with the IPS reclassify at ck_e4..., 63 rows left wrongly blocked and the orphan
delete never run). Nothing in-process can survive the process ending, so the fact "today's
run did not finish" has to live in the database, where the replacement instance can read it.

One row per (job_id, slot_date). `slot_date` is the UTC date of the scheduled slot the run
belongs to (see `slot_for`), stored as 'YYYY-MM-DD' TEXT so the same SQL runs on SQLite and
Postgres. `attempts` counts runs that got as far as holding the job's lock; `completed_at`
is set only by a run that reached its last step.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db._ddl_guard import apply_ddl_statements
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

# Mirrors db/migrations/244_scheduler_job_slots.sql. Migrations do not self-apply in prod,
# so this backstop is what creates the table there.
_DDL_STATEMENTS = guarded_statements([
    """
    CREATE TABLE IF NOT EXISTS scheduler_job_slots (
      job_id            TEXT NOT NULL,
      slot_date         TEXT NOT NULL,
      attempts          INTEGER NOT NULL DEFAULT 0,
      first_started_at  TIMESTAMPTZ,
      last_started_at   TIMESTAMPTZ,
      completed_at      TIMESTAMPTZ,
      PRIMARY KEY (job_id, slot_date)
    )
    """,
])

_READ_SLOT_SQL = """
SELECT attempts, first_started_at, last_started_at, completed_at
FROM scheduler_job_slots
WHERE job_id = :job_id AND slot_date = :slot_date
"""

_RECORD_ATTEMPT_SQL = """
INSERT INTO scheduler_job_slots (job_id, slot_date, attempts, first_started_at, last_started_at)
VALUES (:job_id, :slot_date, 1, :started_at, :started_at)
ON CONFLICT (job_id, slot_date) DO UPDATE
SET attempts = scheduler_job_slots.attempts + 1,
    last_started_at = excluded.last_started_at
RETURNING attempts
"""

# An upsert, not an UPDATE: if the attempt row could not be written, a finished run must
# still be recorded as finished, or catch-up would repeat it.
_RECORD_COMPLETION_SQL = """
INSERT INTO scheduler_job_slots (job_id, slot_date, attempts, first_started_at, last_started_at, completed_at)
VALUES (:job_id, :slot_date, 1, :completed_at, :completed_at, :completed_at)
ON CONFLICT (job_id, slot_date) DO UPDATE
SET completed_at = excluded.completed_at
"""


async def ensure_scheduler_job_slots_table() -> bool:
    """Create the ledger table if it is missing. True once it is known to exist.
    Memoizes only after the statement succeeded, so a failure retries on a later call."""
    global _DDL_READY
    if _DDL_READY:
        return True
    async with _DDL_LOCK:
        if _DDL_READY:
            return True
        from db.database import database

        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_scheduler_job_slots_table",
            logger=logger,
            execute=database.execute,
        )
    return _DDL_READY


def slot_for(now: datetime, *, hour: int, minute: int = 0) -> str:
    """The date of the most recent daily slot at hour:minute UTC that is at or before `now`.

    A 04:00 job: 2026-09-27T04:00:00Z -> '2026-09-27'; 2026-09-27T03:59:59Z -> '2026-09-26'.
    So a run at 02:00 belongs to YESTERDAY's slot, which is the slot it would be catching up.
    """
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    slot_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < slot_start:
        slot_start -= timedelta(days=1)
    return slot_start.date().isoformat()


async def read_slot(job_id: str, slot_date: str) -> Optional[Dict[str, Any]]:
    """The slot's row, or None if no run of it ever started. Raises on a DB error: the
    caller decides whether that means skip or proceed."""
    from db.database import database

    row = await database.fetch_one(_READ_SLOT_SQL, {"job_id": job_id, "slot_date": slot_date})
    return dict(row) if row is not None else None


async def record_attempt(job_id: str, slot_date: str, started_at: datetime) -> int:
    """Count one more run of this slot. Returns the attempt count including this one."""
    from db.database import database

    row = await database.fetch_one(
        _RECORD_ATTEMPT_SQL,
        {"job_id": job_id, "slot_date": slot_date, "started_at": started_at},
    )
    return int(dict(row)["attempts"]) if row is not None else 0


async def record_completion(job_id: str, slot_date: str, completed_at: datetime) -> None:
    from db.database import database

    await database.execute(
        _RECORD_COMPLETION_SQL,
        {"job_id": job_id, "slot_date": slot_date, "completed_at": completed_at},
    )


def _reset_for_tests() -> None:
    global _DDL_READY
    _DDL_READY = False
