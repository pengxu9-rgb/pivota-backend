"""A zombie scheduler run's own DB connection must actually be terminated — and only a zombie's.

`run_isolated` spawns every run in a fresh `contextvars.Context` and keeps that Context so that,
when the run is abandoned, `_terminate_run_connection` can read the run's `databases` 0.7.0
Connection out of it and terminate the raw asyncpg connection. It never could: the task was
created with `ctx.run(loop.create_task, coro)`, and `Task.__init__` without `context=` runs the
coroutine in `copy_context()` of the current context — a COPY of `ctx`. Every ContextVar the run
set (the `databases` Connection included) landed in the copy, `ctx` stayed empty, the lookup hit
LookupError, and the ZOMBIE ERROR log never carried "; its DB connection was terminated". A real
zombie (a socket gone silent past its deadline) kept its pool slot and its server session — a
session-level advisory lock included — until the process exited.

The fix has a second half that the first would otherwise break: the wrapper-cancelled branch
(every redeploy with a run in flight) adopts the run as a zombie right after `task.cancel()`,
before the run has had one chance to unwind. Terminating THEN would cut the connection out from
under the run's own cancellation cleanup (the quality backfill requeues its row in `except
CancelledError`). So on that branch the terminate waits `cancel_grace_seconds`, and fires only if
the run is still alive — the same grace the deadline branch gives before it abandons a run.

POSTGRES GATE: the evidence is server-side (pg_stat_activity, pg_locks). Each test uses its own
schema, alone on the search_path, and its own advisory-lock key; the schema is dropped after.
🚨 THESE GATE FILES SHARE ONE DATABASE.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — the evidence is the server session",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
RUNNER_LOGGER = "services.scheduler_job_runner"
POOL_MAX = 3


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")


@pytest.fixture
async def env(monkeypatch):
    """(database, observer, schema, lock_key). `database` is the one the code under test uses."""
    _assert_throwaway_database()
    import db.database as dbmod  # noqa: F401 — installs the `databases` patches first
    from databases import Database

    import services.scheduler_job_runner as runner

    schema = f"pivota_test_zombie_{uuid.uuid4().hex[:12]}"
    lock_key = int(uuid.uuid4().int % (2**62))

    observer = Database(DATABASE_URL, min_size=1, max_size=2)
    await observer.connect()
    await observer.execute(f'CREATE SCHEMA "{schema}"')
    await observer.execute(f'CREATE TABLE "{schema}".cleanup (what text NOT NULL)')

    database = Database(
        DATABASE_URL, min_size=1, max_size=POOL_MAX, server_settings={"search_path": f'"{schema}"'},
    )
    await database.connect()
    monkeypatch.setattr(dbmod, "database", database)
    runner._reset_for_tests()
    try:
        yield database, observer, schema, lock_key
    finally:
        runner._reset_for_tests()
        try:
            # A zombie that was never cut holds a slot, and pool.close() would wait for it
            # forever: terminate instead, which also ends its session and advisory lock.
            if _in_use(database):
                database._backend._pool.terminate()
            await asyncio.wait_for(database.disconnect(), timeout=10)
        finally:
            await observer.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await asyncio.wait_for(observer.disconnect(), timeout=10)


def _in_use(database) -> int:
    pool = database._backend._pool
    return pool.get_size() - pool.get_idle_size()


async def _every_slot_can_be_checked_out(database) -> bool:
    """All POOL_MAX slots at once. `_in_use` cannot say this: a terminated connection counts as
    neither in use nor idle, whether or not its holder went back to the pool."""
    pool = database._backend._pool
    held = []
    try:
        for _ in range(POOL_MAX):
            held.append(await asyncio.wait_for(pool.acquire(), timeout=2))
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        for c in held:
            await pool.release(c)


async def _backend_alive(observer, pid: int) -> bool:
    return bool(await observer.fetch_val(
        "SELECT count(*) FROM pg_stat_activity WHERE pid = :p", {"p": pid}
    ))


async def _lock_is_free(observer, key: int) -> bool:
    got = await observer.fetch_val("SELECT pg_try_advisory_lock(:k)", {"k": key})
    if got:
        await observer.fetch_val("SELECT pg_advisory_unlock(:k)", {"k": key})
    return bool(got)


async def _eventually(check, *, timeout: float = 5.0) -> bool:
    """Poll `check` (sync, or returning an awaitable) until it is truthy or `timeout` passes."""

    async def once() -> bool:
        out = check()
        return bool(await out) if asyncio.iscoroutine(out) else bool(out)

    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await once():
            return True
        await asyncio.sleep(0.05)
    return await once()


def _silent_socket_run(database, lock_key: int, pid_box: asyncio.Future):
    """A run whose socket goes silent mid-statement — the #1754 zombie, not a simulation of one.

    It takes a session advisory lock (as nightly_index_health does), then stops READING its
    socket and sends a statement. The server answers and goes idle; asyncpg waits forever. On
    cancel, asyncpg's pool release waits for the server to acknowledge the cancel on that same
    silent socket — forever — so the run cannot unwind: it holds its pool slot, its server
    session and its lock until something terminates the connection.
    """

    async def run():
        async with database.connection() as conn:
            await database.fetch_val("SELECT pg_advisory_lock(:k)", {"k": lock_key})
            pid_box.set_result(await database.fetch_val("SELECT pg_backend_pid()"))
            conn.raw_connection._con._transport.pause_reading()
            await database.fetch_val("SELECT 1")
        return "finished"

    return run


# --------------------------------------------------------------------------------------------
# 0. the mechanism itself
# --------------------------------------------------------------------------------------------


async def test_the_spawn_context_is_the_one_the_run_writes_its_connection_into(env):
    """The root cause, stated directly: whatever `databases` Connection the run creates must be
    readable from the Context `_spawn_in_new_context` hands back."""
    database, _observer, _schema, _key = env
    from services.scheduler_job_runner import _spawn_in_new_context

    seen = {}

    async def run():
        conn = database.connection()
        seen["conn"] = conn
        async with conn:
            await database.fetch_val("SELECT 1")
        await asyncio.sleep(3600)

    task, ctx = _spawn_in_new_context(run(), "job:probe")
    try:
        await _eventually(lambda: "conn" in seen)
        assert ctx.get(database._connection_context) is seen["conn"], (
            "the run's Connection is not in the Context the runner kept: the task is running in "
            "a copy of it, so a zombie's connection can never be found"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------------------------
# 1. a real zombie loses its session
# --------------------------------------------------------------------------------------------


async def test_a_deadline_zombie_has_its_connection_terminated_and_releases_its_session(
    env, caplog
):
    database, observer, _schema, key = env
    from services.scheduler_job_runner import (
        JobDeadlineExceeded,
        registry_snapshot,
        run_isolated,
    )

    caplog.set_level(logging.ERROR, logger=RUNNER_LOGGER)
    pid_box: asyncio.Future = asyncio.get_running_loop().create_future()
    run = _silent_socket_run(database, key, pid_box)

    with pytest.raises(JobDeadlineExceeded) as ei:
        await run_isolated("stuck", run, deadline_seconds=1.0, cancel_grace_seconds=0.5)
    assert ei.value.zombie is True
    pid = pid_box.result()

    zombie_logs = [r.getMessage() for r in caplog.records if "ABANDONED as a zombie" in r.getMessage()]
    assert zombie_logs and all("its DB connection was terminated" in m for m in zombie_logs), (
        f"the zombie's connection was not terminated: {zombie_logs}"
    )
    assert await _eventually(lambda: _lock_is_free(observer, key)), (
        "the zombie's session still holds its advisory lock"
    )
    assert not await _backend_alive(observer, pid), "the zombie's server backend is still alive"
    assert await _every_slot_can_be_checked_out(database), "the zombie still holds its pool slot"
    # What terminate does NOT do: wake a zombie parked on the cancel acknowledgement. asyncpg
    # never resolves that waiter on connection loss (and it is not reachable from Python), so
    # the task stays parked — a leaked task, holding no slot, no session and no lock.
    assert registry_snapshot()["stuck"]["zombies_alive"] == 1


async def test_a_run_that_ignores_a_redeploy_is_terminated_after_the_grace(env, caplog):
    """The path the #2404 probe measured: a REAL AsyncIOScheduler, a wrapped job with a statement
    in flight, `stop_scheduler` cancelling it. The run will not unwind, so after the grace its
    connection is cut and its session (and advisory lock) is gone."""
    database, observer, _schema, key = env
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import services.audit_scheduler as audit_scheduler
    from services.scheduler_job_runner import registry_snapshot, wrap_job

    caplog.set_level(logging.ERROR, logger=RUNNER_LOGGER)
    grace = 0.5
    pid_box: asyncio.Future = asyncio.get_running_loop().create_future()
    job = wrap_job("stuck_on_redeploy", _silent_socket_run(database, key, pid_box),
                   deadline_seconds=60, cancel_grace_seconds=grace)
    sched = AsyncIOScheduler()
    sched.add_job(job, id="stuck_on_redeploy")  # no trigger: runs once, now
    sched.start()
    old = (audit_scheduler._SCHEDULER, audit_scheduler._DRAIN_SECONDS)
    try:
        pid = await asyncio.wait_for(pid_box, timeout=10)
        audit_scheduler._SCHEDULER, audit_scheduler._DRAIN_SECONDS = sched, 0.2
        await audit_scheduler.stop_scheduler()  # pause, drain 0.2s, shutdown -> cancels it
    finally:
        audit_scheduler._SCHEDULER, audit_scheduler._DRAIN_SECONDS = old
        if sched.running:
            sched.shutdown(wait=False)

    # The executor's cancel lands on the wrapper's next step; it then adopts the zombie.
    assert await _eventually(lambda: registry_snapshot()["stuck_on_redeploy"]["zombies_alive"] == 1)
    assert await _backend_alive(observer, pid), "terminated before the run had its grace"

    assert await _eventually(lambda: _lock_is_free(observer, key), timeout=grace + 5), (
        "the abandoned run still holds its session and advisory lock after the grace"
    )
    assert not await _backend_alive(observer, pid)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("its DB connection was terminated" in m for m in msgs), msgs
    assert await _every_slot_can_be_checked_out(database), "the abandoned run still holds its slot"


# --------------------------------------------------------------------------------------------
# 2. ... and a run that DOES unwind keeps its connection for its own cleanup
# --------------------------------------------------------------------------------------------


async def test_a_cancelled_run_finishes_its_own_db_cleanup_before_anything_is_terminated(
    env, caplog
):
    """The redeploy path must not cut a connection the run is about to use: its `except
    CancelledError` writes to the DB (the quality backfill requeues its row there) and unlocks.
    A terminate issued at wrapper-cancel time — before the run has run again — kills that."""
    database, observer, schema, key = env
    from services.scheduler_job_runner import registry_snapshot, wrap_job

    caplog.set_level(logging.ERROR, logger=RUNNER_LOGGER)
    in_flight = asyncio.Event()
    cleanup_errors: list = []

    async def polite_run():
        async with database.connection():
            await database.fetch_val("SELECT pg_advisory_lock(:k)", {"k": key})
            in_flight.set()
            try:
                await asyncio.sleep(3600)  # between statements: an HTTP call, a backoff
            except asyncio.CancelledError:
                try:
                    await database.execute("SELECT pg_sleep(0.2)")
                    await database.execute(
                        "INSERT INTO cleanup (what) VALUES ('requeued')"
                    )
                    await database.fetch_val("SELECT pg_advisory_unlock(:k)", {"k": key})
                except BaseException as exc:  # noqa: BLE001 — recorded for the assertion
                    cleanup_errors.append(exc)
                raise

    fn = wrap_job("polite", polite_run, deadline_seconds=60, cancel_grace_seconds=2.0)
    wrapper = asyncio.ensure_future(fn())
    await asyncio.wait_for(in_flight.wait(), timeout=10)
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapper

    assert await _eventually(lambda: registry_snapshot()["polite"]["zombies_alive"] == 0)
    assert cleanup_errors == [], f"the run's cancellation cleanup was cut off: {cleanup_errors!r}"
    rows = await observer.fetch_all(f'SELECT what FROM "{schema}".cleanup')
    assert [r["what"] for r in rows] == ["requeued"]
    assert await _lock_is_free(observer, key)
    await asyncio.sleep(2.5)  # past the grace: the deferred check must now be a no-op
    assert not any("DB connection was terminated" in r.getMessage() for r in caplog.records)
    assert await _every_slot_can_be_checked_out(database)
