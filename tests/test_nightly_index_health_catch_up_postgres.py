"""Postgres gate: nightly_index_health's advisory lock is held for the WHOLE run, a killed run
lets go of it, and catch-up re-runs the slot under it.

WHY THIS NEEDS POSTGRES. The lock is `pg_try_advisory_lock`, session-scoped, and SQLite has
none. Before this change it was taken with a bare `database.fetch_one`: databases 0.7.0 hands
that raw connection straight back to the asyncpg pool, and asyncpg's reset on release runs
`pg_advisory_unlock_all()`. So the lock was released the instant it was granted — measured
locally, 0 advisory locks held right after a successful acquire — and "the advisory lock
prevents double-runs" was never true. Catch-up adds a second way to start the job (a tick
every 10 minutes, on whichever instance is up), so the lock now has to be real.

The redeploy is the real one: a real AsyncIOScheduler fires the job through the real
`wrap_job`, and the real `audit_scheduler.stop_scheduler` cancels it while it is parked
mid-batch. The cancelled run unwinds through its own `finally` (unlock, then the pool's reset
on release), which is what frees the lock. `run_isolated` also tries to terminate a zombie's
connection, but on py3.11 that lookup misses (the task runs in a COPY of the context it
inspects), so nothing here relies on it.

Every test runs in its own `nihcu_<hex>` schema (the slot ledger is created there), so
nothing here can collide with the tables other gate files build in the shared database.

    DATABASE_URL=postgresql://localhost/pivota_<you>_dialect_check \\
        .venv/bin/python -m pytest tests/test_nightly_index_health_catch_up_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — advisory locks exist only on the production dialect",
)

#: Same markers as the sibling gate files, so CI's pivota_dialect_check RUNS these.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

CRON_FIRES_AT = datetime(2026, 9, 27, 4, 0, 1, tzinfo=timezone.utc)
KEYS = ["ck_a", "ck_b", "ck_c", "ck_d", "ck_e"]


def _asyncpg_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class _HybridDb:
    """The job's own catalog SQL is scripted (it needs the whole catalog schema); the lock,
    the slot ledger, its DDL and `connection()` go to the REAL Postgres `Database`, as do
    `url` and `_connection_context`."""

    def __init__(self, real, *, park_at_cursor=None):
        import jobs.nightly_index_health_job as job

        self._job = job
        self.real = real
        self.park_at_cursor = park_at_cursor
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.upserted: list = []
        self.executed: list = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def connection(self):
        return self.real.connection()

    def _job_sql(self, query: str) -> bool:
        j = self._job
        return query in {
            j._BATCH_QUERY, j._ORPHAN_COUNT_QUERY, j._IPS_TOTAL_COUNT_QUERY, j._ORPHAN_DELETE,
            j._STALE_INVALIDATION_UPDATE, j._REGRESSION_BLOCKER_UPDATE, j._UPSERT_QUERY,
        } or "domain_extractor_baselines" in query

    async def fetch_all(self, query, values=None):
        if query == self._job._BATCH_QUERY:
            from test_nightly_index_health_job import _full_row

            cursor = (values or {}).get("cursor")
            if cursor == self.park_at_cursor and not self.release.is_set():
                self.parked.set()
                await self.release.wait()
            return [_full_row(content_key=k) for k in [k for k in KEYS if k > cursor][:2]]
        if self._job_sql(query):
            return []
        return await self.real.fetch_all(query, values)

    async def fetch_one(self, query, values=None):
        if self._job_sql(query):
            return {"n": 0}
        return await self.real.fetch_one(query, values)

    async def execute(self, query, values=None):
        if self._job_sql(query):
            self.executed.append(query)
            return None
        return await self.real.execute(query, values)

    async def execute_many(self, query, values):
        self.upserted.extend(v["content_key"] for v in values)


@pytest.fixture
async def pg(monkeypatch):
    """A Database whose connections resolve unqualified names in a fresh schema."""
    import asyncpg
    from databases import Database

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in {dbname!r} — throwaway only")

    schema = f"nihcu_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_asyncpg_dsn())
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
    finally:
        await admin.close()

    db = Database(_asyncpg_dsn(), min_size=1, max_size=4, server_settings={"search_path": schema})
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()
        admin = await asyncpg.connect(_asyncpg_dsn())
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await admin.close()


@pytest.fixture
def runner():
    import services.scheduler_job_runner as mod

    mod._reset_for_tests()
    yield mod
    mod._reset_for_tests()


@pytest.fixture
def job(monkeypatch):
    import jobs.nightly_index_health_job as job

    now = {"t": CRON_FIRES_AT}
    monkeypatch.setattr(job, "_utcnow", lambda: now["t"])
    job._test_clock = now  # type: ignore[attr-defined]

    async def _no_scorecards():
        return {"domains_scored": 0, "domains_regressed": 0, "domains_recovered": 0, "errors": []}

    async def _no_snapshot():
        return {}

    async def _no_indexnow(before, summary):
        return None

    monkeypatch.setattr(job, "BATCH_SIZE", 2)
    monkeypatch.setattr(job, "_compute_domain_extractor_scorecards", _no_scorecards)
    monkeypatch.setattr(job, "_snapshot_serving_eligibility", _no_snapshot)
    monkeypatch.setattr(job, "_submit_indexnow_transitions", _no_indexnow)
    return job


def _install(monkeypatch, hybrid):
    import db._ddl_guard as ddl_guard
    import db.database
    from db import scheduler_job_slots

    monkeypatch.setattr(db.database, "database", hybrid)
    monkeypatch.setattr(ddl_guard, "_state", {})
    monkeypatch.setattr(scheduler_job_slots, "_DDL_READY", False)


async def _another_instance_can_take_the_lock(job) -> bool:
    """What a second worker would see: a separate session trying the job's lock."""
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", job._JOB_LOCK_ID)
        if got:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", job._JOB_LOCK_ID)
        return bool(got)
    finally:
        await conn.close()


async def _run_now(runner, job_id):
    """run_job_now, bounded: a regression that lets the run park forever must FAIL here,
    not hang the suite."""
    return await asyncio.wait_for(runner.run_job_now(job_id), timeout=15)


async def _ledger(pg, slot="2026-09-27"):
    row = await pg.fetch_one(
        "SELECT attempts, completed_at FROM scheduler_job_slots "
        "WHERE job_id = 'nightly_index_health' AND slot_date = :s", {"s": slot},
    )
    return dict(row) if row is not None else None


async def _start_the_04_00_run(runner, job, hybrid):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="UTC")
    wrapped = runner.wrap_job("nightly_index_health", job.run_nightly_index_health, deadline_seconds=7200)
    sched.add_job(wrapped, "date", run_date=datetime.now(timezone.utc), id="nightly_index_health")
    sched.start()
    await asyncio.wait_for(hybrid.parked.wait(), timeout=10)
    return sched


async def _redeploy(monkeypatch, runner, sched):
    import services.audit_scheduler as audit_scheduler

    monkeypatch.setattr(audit_scheduler, "_SCHEDULER", sched)
    monkeypatch.setattr(audit_scheduler, "_DRAIN_SECONDS", 0.2)
    await audit_scheduler.stop_scheduler()
    st = runner._REGISTRY["nightly_index_health"]
    for _ in range(250):
        if st.runs_cancelled and all(t.done() for t in st.zombies):
            break
        await asyncio.sleep(0.02)
    assert st.runs_cancelled == 1, "the redeploy never cancelled the run"


async def test_the_lock_is_held_for_the_whole_run_not_just_the_acquire(monkeypatch, pg, runner, job):
    """THE BUG THIS PR FOUND: unpinned, a second instance could take the lock while the first
    run was mid-batch."""
    hybrid = _HybridDb(pg, park_at_cursor="ck_b")
    _install(monkeypatch, hybrid)
    assert await _another_instance_can_take_the_lock(job)  # free before the run

    sched = await _start_the_04_00_run(runner, job, hybrid)
    try:
        assert not await _another_instance_can_take_the_lock(job), (
            "the job's advisory lock is not held while the run is in flight"
        )
    finally:
        hybrid.release.set()
        for _ in range(250):
            if runner._REGISTRY["nightly_index_health"].runs_ok:
                break
            await asyncio.sleep(0.02)
        sched.shutdown(wait=False)
    assert runner._REGISTRY["nightly_index_health"].runs_ok == 1
    assert await _another_instance_can_take_the_lock(job)  # released at the end
    ledger = await _ledger(pg)
    assert ledger["attempts"] == 1 and ledger["completed_at"] is not None


async def test_a_catch_up_tick_during_an_in_flight_run_skips_without_counting(monkeypatch, pg, runner, job):
    hybrid = _HybridDb(pg, park_at_cursor="ck_b")
    _install(monkeypatch, hybrid)
    sched = await _start_the_04_00_run(runner, job, hybrid)
    try:
        runner.wrap_job("nightly_index_health_catch_up", job.run_nightly_index_health_catch_up,
                        deadline_seconds=7200)
        job._test_clock["t"] = CRON_FIRES_AT + timedelta(minutes=2)
        out = await _run_now(runner, "nightly_index_health_catch_up")
        assert out["result"]["skip_reason"] == "lock_held", out
        assert (await _ledger(pg))["attempts"] == 1
    finally:
        hybrid.release.set()
        await asyncio.sleep(0.2)
        sched.shutdown(wait=False)


async def test_a_run_killed_by_a_redeploy_frees_the_lock_and_catch_up_completes_the_slot(
    monkeypatch, pg, runner, job,
):
    hybrid = _HybridDb(pg, park_at_cursor="ck_b")
    _install(monkeypatch, hybrid)
    sched = await _start_the_04_00_run(runner, job, hybrid)

    await _redeploy(monkeypatch, runner, sched)

    assert hybrid.upserted == ["ck_a", "ck_b"]
    assert job._STALE_INVALIDATION_UPDATE not in hybrid.executed
    assert await _another_instance_can_take_the_lock(job), (
        "the killed run still holds the lock, so no instance could catch it up"
    )
    assert (await _ledger(pg)) == {"attempts": 1, "completed_at": None}

    hybrid.release.set()
    job._test_clock["t"] = CRON_FIRES_AT + timedelta(minutes=5)
    runner.wrap_job("nightly_index_health_catch_up", job.run_nightly_index_health_catch_up,
                    deadline_seconds=7200)
    out = await _run_now(runner, "nightly_index_health_catch_up")
    assert out["outcome"] == "ok" and out["result"]["slot_completed"] is True, out
    assert hybrid.upserted[2:] == KEYS
    assert job._STALE_INVALIDATION_UPDATE in hybrid.executed
    ledger = await _ledger(pg)
    assert ledger["attempts"] == 2 and ledger["completed_at"] is not None
    assert await _another_instance_can_take_the_lock(job)

    job._test_clock["t"] = CRON_FIRES_AT + timedelta(minutes=15)
    again = await _run_now(runner, "nightly_index_health_catch_up")
    assert again["result"]["skip_reason"] == "already_completed"
