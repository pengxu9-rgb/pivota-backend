"""Issue #1754 — every APScheduler job on prod wedged, "maximum number of
running instances reached (1)" on every tick for hours, payment reconciler
included.

Root cause: on the pinned `databases==0.7.0` the `Connection` object lives in a
ContextVar that child tasks inherit, and main.startup_event() touches the DB
before start_scheduler(), so EVERY job task shared ONE Connection (one asyncpg
connection, one set of locks). One run blocked in a DB await blocked them all,
forever; `max_instances=1` then skipped every tick.

These tests pin the two halves of the fix in services/scheduler_job_runner.py:
  1. isolation — a run inherits NOTHING from the context that scheduled it, so
     it gets its own `databases` Connection;
  2. the watchdog — a run that never completes is bounded by its deadline, so
     neither its own future ticks nor any other job's are starved.

Mutation-checked against the pre-fix code on databases 0.7.0 (prod pin) AND
0.9.0 (per-task connections already, so only the ContextVar + watchdog tests
bite there) — see the PR for the table.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

import services.scheduler_job_runner as runner
from services.scheduler_job_runner import (
    JobDeadlineExceeded,
    JobRunCancelled,
    cancel_running,
    registry_snapshot,
    run_job_now,
    spawn_isolated,
    wrap_job,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    runner._reset_for_tests()
    yield
    runner._reset_for_tests()


# --------------------------------------------------------------------------
# 1. isolation
# --------------------------------------------------------------------------

_INHERITED = contextvars.ContextVar("test_inherited", default="<unset>")


async def test_wrapped_job_run_inherits_nothing_from_the_scheduling_context():
    """The scheduling context (startup, or an admin request via
    restart_scheduler) carries state in ContextVars. On databases 0.7.0 that
    state IS the shared Connection. A run must not see it."""
    _INHERITED.set("startup-connection")
    seen = []

    async def job():
        seen.append(_INHERITED.get())

    await wrap_job("iso", job, deadline_seconds=5)()
    # And the plain-task path used for startup workers:
    await spawn_isolated(job())
    assert seen == ["<unset>", "<unset>"], seen


async def test_wrapped_jobs_do_not_share_the_startup_databases_connection():
    """Drive the real `databases` object the way main.startup_event() does:
    a query in the parent context creates a Connection there; then two jobs
    run. Neither may see the parent's Connection object, and they must not
    share one with each other. On databases 0.7.0 the pre-fix code fails all
    three assertions (identical id() everywhere)."""
    from db.database import database

    if not getattr(database, "is_connected", False):
        await database.connect()
    await database.execute("SELECT 1")
    parent_conn = database.connection()

    ids = {}
    gate = asyncio.Event()

    async def job(tag):
        # Hold the connection open so both runs overlap and any sharing shows.
        async with database.connection() as conn:
            ids[tag] = id(conn)
            if len(ids) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 5)

    a = wrap_job("job_a", job, deadline_seconds=5)
    b = wrap_job("job_b", job, deadline_seconds=5)
    await asyncio.gather(a("a"), b("b"))

    assert ids["a"] != id(parent_conn), "job A inherited the startup Connection"
    assert ids["b"] != id(parent_conn), "job B inherited the startup Connection"
    assert ids["a"] != ids["b"], "jobs A and B share one Connection"


# --------------------------------------------------------------------------
# 2. watchdog: a run that never completes cannot starve future ticks
# --------------------------------------------------------------------------

async def test_a_run_that_never_completes_does_not_starve_its_own_future_ticks():
    """Real AsyncIOScheduler, max_instances=1, coalesce=True — the prod job
    config. The job blocks forever. Pre-fix: ONE run starts, every later tick
    is skipped with 'maximum number of running instances reached (1)'.
    Post-fix: the deadline cancels the wedged run and later ticks proceed."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    starts = []
    never = asyncio.Event()

    async def wedged():
        starts.append(1)
        await never.wait()

    sched = AsyncIOScheduler()
    sched.add_job(
        wrap_job("wedged", wedged, deadline_seconds=0.25, cancel_grace_seconds=0.1),
        "interval", seconds=0.2, id="wedged", max_instances=1, coalesce=True,
        misfire_grace_time=None,  # CI stalls must not turn ticks into misfires
    )
    sched.start()
    try:
        await asyncio.sleep(2.0)
        snap = registry_snapshot()["wedged"]  # before shutdown cancels the in-flight run
    finally:
        sched.shutdown(wait=False)
        never.set()
        await asyncio.sleep(0.05)

    assert len(starts) >= 3, f"only {len(starts)} run(s) started in 2s — later ticks were starved"
    assert snap["runs_deadline_exceeded"] >= 2, snap
    assert snap["last_outcome"] == "deadline_exceeded", snap


async def test_one_wedged_job_does_not_starve_other_jobs():
    """The #1754 shape end-to-end on the real `databases` object: job A blocks
    inside a query and never returns; job B just runs a query on every tick.
    Under the shared-Connection defect (databases 0.7.0, pre-fix) B blocks
    behind A's query lock on its first tick and never completes again —
    every tick of A AND B is then skipped. Post-fix, B keeps completing and A
    is bounded by its deadline."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from db.database import database

    if not getattr(database, "is_connected", False):
        await database.connect()
    await database.execute("SELECT 1")  # startup-context Connection, as in main.py

    never = asyncio.Event()
    b_done = []

    async def a_wedged():
        # Streaming rows and blocking mid-stream: `iterate` holds the
        # Connection's `_query_lock` across the yield — the same lock every
        # other query on a SHARED Connection has to take.
        async for _row in database.iterate("SELECT 1"):
            await never.wait()

    async def b_query():
        await database.fetch_one("SELECT 1")
        b_done.append(1)

    sched = AsyncIOScheduler()
    sched.add_job(
        wrap_job("a_wedged", a_wedged, deadline_seconds=0.6, cancel_grace_seconds=0.1),
        "interval", seconds=0.2, id="a_wedged", max_instances=1, coalesce=True,
        misfire_grace_time=None,
    )
    sched.add_job(
        wrap_job("b_query", b_query, deadline_seconds=1.0, cancel_grace_seconds=0.1),
        "interval", seconds=0.2, id="b_query", max_instances=1, coalesce=True,
        misfire_grace_time=None,
    )
    sched.start()
    try:
        await asyncio.sleep(2.0)
        a = registry_snapshot()["a_wedged"]
    finally:
        sched.shutdown(wait=False)
        never.set()
        await asyncio.sleep(0.05)

    assert len(b_done) >= 5, f"job B completed only {len(b_done)} time(s) in 2s — starved by A"
    assert a["runs_started"] >= 2, a
    assert a["runs_deadline_exceeded"] >= 1, a


async def test_zombie_run_is_abandoned_after_grace_and_the_slot_is_freed():
    """A run that refuses to unwind on cancel (asyncpg's cancel path can hang)
    must not hold the watchdog: after the grace it is abandoned, recorded as a
    zombie, and the next run proceeds."""
    stage2 = asyncio.Event()
    entered = []

    async def stubborn():
        entered.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await stage2.wait()  # ignores the cancel; keeps running

    fn = wrap_job("stubborn", stubborn, deadline_seconds=0.1, cancel_grace_seconds=0.1)
    # wait_for: a watchdog that never fires must fail this test, not hang it
    with pytest.raises(JobDeadlineExceeded) as ei:
        await asyncio.wait_for(fn(), 5)
    assert ei.value.zombie is True
    snap = registry_snapshot()["stubborn"]
    assert snap["zombie_count"] == 1 and snap["zombies_alive"] == 1
    assert snap["running"] == 0, "the abandoned run must not count as running"

    # slot free: a second run starts even though the zombie is still alive
    with pytest.raises(JobDeadlineExceeded):
        await asyncio.wait_for(fn(), 5)
    assert len(entered) == 2

    # operator lever reaches zombies too
    assert cancel_running("stubborn") == 2
    stage2.set()
    await asyncio.sleep(0.05)
    assert registry_snapshot()["stubborn"]["zombies_alive"] == 0


async def test_normal_and_failing_runs_are_recorded_and_errors_still_propagate():
    async def ok():
        return {"n": 1}

    async def boom():
        raise ValueError("kaboom")

    assert await wrap_job("ok", ok, deadline_seconds=1)() == {"n": 1}
    with pytest.raises(ValueError):
        await wrap_job("boom", boom, deadline_seconds=1)()
    snap = registry_snapshot(include_error_text=True)
    assert snap["ok"]["last_outcome"] == "ok" and snap["ok"]["runs_ok"] == 1
    assert snap["boom"]["last_outcome"] == "error"
    assert snap["boom"]["last_error_type"] == "ValueError"
    assert snap["boom"]["last_error"] == "kaboom"
    # public snapshot never carries error text
    assert "last_error" not in registry_snapshot()["boom"]


async def test_wrapper_keeps_the_job_function_name_for_log_lines():
    async def run_payment_reconcile_tick():
        return None

    fn = wrap_job("payment_reconcile_tick", run_payment_reconcile_tick, deadline_seconds=1)
    # The issue was diagnosed from APScheduler log lines keyed on this name.
    assert fn.__name__ == "run_payment_reconcile_tick"
    assert asyncio.iscoroutinefunction(fn)


async def test_run_job_now_forces_an_out_of_band_run_through_the_same_path():
    calls = []

    async def tick():
        calls.append(1)
        return {"scanned": 0}

    wrap_job("payment_reconcile_tick", tick, deadline_seconds=1)
    out = await run_job_now("payment_reconcile_tick")
    assert calls == [1]
    assert out["outcome"] == "ok" and out["result"] == {"scanned": 0}
    assert out["state"]["runs_ok"] == 1
    with pytest.raises(KeyError):
        await run_job_now("not_registered")


async def test_wrapper_cancelled_while_run_ignores_cancel_keeps_the_run_visible():
    """Review finding on #1756: scheduler shutdown / admin restart cancels the
    WRAPPER. If the run will not unwind, it must not become a ghost — it stays
    a zombie on the registry (visible, cancellable, exception retrieved)."""
    stage2 = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await stage2.wait()

    fn = wrap_job("ghost", stubborn, deadline_seconds=10)
    wrapper = asyncio.ensure_future(fn())
    await asyncio.sleep(0.05)
    wrapper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapper
    snap = registry_snapshot()["ghost"]
    assert snap["running"] == 0
    assert snap["zombies_alive"] == 1 and snap["zombie_count"] == 1, snap
    assert snap["last_outcome"] == "cancelled"
    assert cancel_running("ghost") == 1
    stage2.set()
    await asyncio.sleep(0.05)
    assert registry_snapshot()["ghost"]["zombies_alive"] == 0


async def test_cancel_running_on_a_live_run_reports_cancelled_not_a_stray_cancellederror():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.Event().wait()

    fn = wrap_job("payment_reconcile_tick", slow, deadline_seconds=10)
    t = asyncio.ensure_future(run_job_now("payment_reconcile_tick"))
    await started.wait()
    assert cancel_running("payment_reconcile_tick") == 1
    out = await asyncio.wait_for(t, 5)
    assert out["outcome"] == "cancelled", out
    with pytest.raises(JobRunCancelled):
        t2 = asyncio.ensure_future(fn())
        await asyncio.sleep(0.02)
        cancel_running("payment_reconcile_tick")
        await asyncio.wait_for(t2, 5)


def test_wrap_job_rejects_a_sync_function():
    def sync_job():
        return None

    with pytest.raises(TypeError):
        wrap_job("sync", sync_job, deadline_seconds=1)


async def test_quality_backfill_tick_requeues_its_row_when_cancelled_mid_run(monkeypatch):
    """A cancelled run (deadline / shutdown) must not strand the job row in
    `running` — nothing in production would ever pick it up again."""
    import services.product_quality_backfill_service as svc

    events = []

    async def fake_requeue(job_id):
        events.append(("requeue", job_id))
        return True

    async def hang(*a, **k):
        await asyncio.Event().wait()

    async def fake_complete(job_id, **kw):
        events.append(("complete", job_id, kw.get("status")))
        return None

    monkeypatch.setattr(svc, "requeue_quality_backfill_job", fake_requeue)
    monkeypatch.setattr(svc, "complete_quality_backfill_job", fake_complete)
    monkeypatch.setattr(svc, "_load_cached_rows", hang)
    task = asyncio.ensure_future(
        svc._process_claimed_quality_backfill_job({"job_id": "job-1", "merchant_id": "m-1"})
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == [("requeue", "job-1")]


# --------------------------------------------------------------------------
# 3. every registered job is wrapped and has an explicit deadline
# --------------------------------------------------------------------------

class _RecordingScheduler:
    def __init__(self, *a, **k):
        self.added = []  # (id, func)

    def add_job(self, func, *a, **k):
        self.added.append((k.get("id"), func))

    def start(self):
        pass

    def get_jobs(self):
        return []


async def test_start_scheduler_wraps_every_job_and_every_job_has_an_explicit_deadline(monkeypatch):
    import services.audit_scheduler as sched

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)

    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    ids = [jid for jid, _ in rec.added]
    assert len(ids) > 20, ids

    unwrapped = [jid for jid, fn in rec.added if getattr(fn, "__wrapped_job_id__", None) != jid]
    assert unwrapped == [], f"jobs registered WITHOUT the isolating wrapper: {unwrapped}"

    missing = sorted(set(ids) - set(sched._JOB_RUN_DEADLINES))
    assert missing == [], f"jobs without an explicit run deadline: {missing}"
    stale = sorted(set(sched._JOB_RUN_DEADLINES) - set(ids))
    assert stale == [], f"deadline table entries for jobs that are not registered: {stale}"

    # the money-path job is registered, wrapped, and forceable
    assert "payment_reconcile_tick" in runner.registered_job_ids()
    for jid, d in sched._JOB_RUN_DEADLINES.items():
        assert 0 < d <= 14400, (jid, d)


def test_scheduler_health_carries_the_run_registry(monkeypatch):
    from fastapi.testclient import TestClient
    import services.audit_scheduler as sched
    from routes.scheduler_health import router

    async def tick():
        return None

    wrap_job("payment_reconcile_tick", tick, deadline_seconds=600)
    asyncio.run(run_job_now("payment_reconcile_tick"))
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    client = TestClient(_app_with(router))
    body = client.get("/__scheduler_health").json()
    assert body["runs"]["payment_reconcile_tick"]["runs_ok"] == 1
    assert body["runs"]["payment_reconcile_tick"]["deadline_seconds"] == 600
    assert body["stalled"] == {}


def _app_with(router):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return app


def test_admin_run_now_is_allowlisted_and_runs_the_registered_job():
    from fastapi.testclient import TestClient
    import routes.admin_scheduler_jobs as module

    calls = []

    async def tick():
        calls.append(1)

    wrap_job("payment_reconcile_tick", tick, deadline_seconds=600)
    app = _app_with(module.router)
    app.dependency_overrides[module.require_admin] = lambda: {"email": "admin@example.com"}
    client = TestClient(app)

    r = client.post("/admin/scheduler/jobs/daily_audit_check/run-now")
    assert r.status_code == 403 and r.json()["error"] == "job_not_runnable"

    r = client.post("/admin/scheduler/jobs/stamp_attribution_reaper/run-now")
    assert r.status_code == 404 and r.json()["error"] == "job_not_registered"

    r = client.post("/admin/scheduler/jobs/payment_reconcile_tick/run-now")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "ok" and calls == [1]

    r = client.get("/admin/scheduler/runs")
    assert r.status_code == 200
    assert r.json()["runs"]["payment_reconcile_tick"]["runs_ok"] == 1
    assert "last_error" in r.json()["runs"]["payment_reconcile_tick"]

    r = client.post("/admin/scheduler/jobs/payment_reconcile_tick/cancel-running")
    assert r.status_code == 200 and r.json() == {"job_id": "payment_reconcile_tick", "cancelled": 0}
