"""`stop_scheduler` must let in-flight runs land, and must never hang or raise.

WHY THIS FILE EXISTS. The function carried a comment saying it "prefers wait=True so a
re-audit in progress completes — but capped at 30s", above a call to
`_SCHEDULER.shutdown(wait=False)`. Both halves of that comment were unachievable:

  * For an AsyncIOScheduler the `wait` argument IS A NO-OP. APScheduler 3.11's
    AsyncIOExecutor.shutdown says so in its own body — "there is no way to honor wait=True
    without converting this method into a coroutine method" — and cancels every pending
    future either way. Flipping the flag would have changed nothing while making the comment
    look satisfied, which is worse than the visible mismatch.
  * `BaseScheduler.shutdown()` takes no timeout at all, so "capped at 30s" could not be
    expressed through it.

So the drain is done here, before APScheduler is told to stop. `test_the_wait_argument_is_a_
no_op_for_this_scheduler` pins the library fact the design rests on — if a future APScheduler
makes `wait` meaningful, that test fails and this whole approach should be revisited.

The cases below run REAL asyncio tasks through the REAL registry rather than asserting on
source text: the question is whether an in-flight run survives a shutdown, and only running
one can answer it.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture()
def runner():
    mod = importlib.import_module("services.scheduler_job_runner")
    mod._reset_for_tests()
    yield mod
    mod._reset_for_tests()


class _FakeScheduler:
    """Records what the shutdown path did to it, in order."""

    def __init__(self, *, pause_raises: bool = False):
        self.calls: list = []
        self._pause_raises = pause_raises

    def pause(self):
        self.calls.append("pause")
        if self._pause_raises:
            raise RuntimeError("cannot pause")

    def shutdown(self, wait=True):
        self.calls.append(f"shutdown(wait={wait})")


async def _install(sched, monkeypatch, drain: float):
    mod = importlib.import_module("services.audit_scheduler")
    monkeypatch.setattr(mod, "_SCHEDULER", sched, raising=False)
    monkeypatch.setattr(mod, "_DRAIN_SECONDS", drain, raising=False)
    return mod


# ── the library fact the design rests on ───────────────────────────────────────────────


def test_the_wait_argument_is_a_no_op_for_this_scheduler():
    """The reason the fix is a drain rather than `wait=True`. If this ever fails, APScheduler
    has changed and `stop_scheduler` should be reconsidered — not patched around."""
    import inspect
    from apscheduler.executors.asyncio import AsyncIOExecutor

    src = inspect.getsource(AsyncIOExecutor.shutdown)
    assert "no way to honor wait=True" in src and "f.cancel()" in src, (
        "AsyncIOExecutor.shutdown no longer ignores `wait` and cancels unconditionally.\n"
        "stop_scheduler drains by hand BECAUSE of that behaviour; re-derive the design.\n"
        f"{src}"
    )


# ── behaviour ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_short_run_in_flight_is_allowed_to_finish(runner, monkeypatch):
    """The whole point. Before this, a redeploy cancelled it — and with the worker now
    rolled on every push to main, that happens 15-34 times a day."""
    landed = []

    async def work():
        await asyncio.sleep(0.05)
        landed.append("done")

    task = runner.spawn_isolated(work(), name="job:short")
    st = runner._state("short", 30.0)
    st.active[task] = 0.0

    sched = _FakeScheduler()
    mod = await _install(sched, monkeypatch, drain=5.0)
    await mod.stop_scheduler()

    assert landed == ["done"], "the in-flight run was cancelled instead of being drained"
    assert task.done() and not task.cancelled()
    assert sched.calls == ["pause", "shutdown(wait=False)"], (
        f"the scheduler must be paused BEFORE draining, or the set of runs grows while we "
        f"wait on it; got {sched.calls}"
    )


@pytest.mark.asyncio
async def test_a_long_run_does_not_hold_shutdown_open(runner, monkeypatch):
    """The cap is the safety property. Cloud Run SIGKILLs the container after its grace
    period, and uvicorn has no --timeout-graceful-shutdown, so a drain that outlasts the
    grace loses `database.disconnect()` — a strictly worse trade than cancelling a job."""
    async def forever():
        await asyncio.sleep(3600)

    task = runner.spawn_isolated(forever(), name="job:long")
    st = runner._state("long", 30.0)
    st.active[task] = 0.0

    sched = _FakeScheduler()
    mod = await _install(sched, monkeypatch, drain=0.15)
    started = asyncio.get_running_loop().time()
    # wait_for, NOT a bare await plus an elapsed-time assertion. Measured while mutating:
    # dropping the drain's own `timeout=` makes stop_scheduler never return, so a bare await
    # HANGS here instead of failing — and a hang is not a test result, it is a CI job burning
    # its whole timeout with nothing to read afterwards. The cap under test is 0.15s; 5s is
    # far outside it and still finite.
    try:
        await asyncio.wait_for(mod.stop_scheduler(), timeout=5.0)
    except asyncio.TimeoutError:
        task.cancel()
        pytest.fail(
            "stop_scheduler never returned: the drain is unbounded, so a long-running job "
            "holds the lifespan open until Cloud Run SIGKILLs the container mid-shutdown "
            "and `database.disconnect()` never runs."
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.0, f"shutdown was held open for {elapsed:.1f}s by a long-running job"
    assert "shutdown(wait=False)" in sched.calls, "it never reached the actual shutdown"
    task.cancel()


@pytest.mark.asyncio
async def test_zero_disables_the_drain(runner, monkeypatch):
    """The kill switch has to actually kill it, or it is not a way back."""
    async def work():
        await asyncio.sleep(0.05)

    task = runner.spawn_isolated(work(), name="job:x")
    st = runner._state("x", 30.0)
    st.active[task] = 0.0

    sched = _FakeScheduler()
    mod = await _install(sched, monkeypatch, drain=0.0)
    await mod.stop_scheduler()
    assert not task.done(), "SCHEDULER_DRAIN_SECONDS=0 must not wait for anything"
    task.cancel()


@pytest.mark.asyncio
async def test_shutdown_still_happens_when_pause_raises(runner, monkeypatch):
    """Best-effort means best-effort. A scheduler that cannot be paused must still be shut
    down — leaving it running would keep the drainers alive past the lifespan."""
    sched = _FakeScheduler(pause_raises=True)
    mod = await _install(sched, monkeypatch, drain=0.1)
    await mod.stop_scheduler()
    assert "shutdown(wait=False)" in sched.calls, (
        f"a failing pause() suppressed the shutdown entirely: {sched.calls}"
    )


@pytest.mark.asyncio
async def test_a_broken_registry_does_not_break_shutdown(runner, monkeypatch):
    """The drain reaches into another module. If that import or call ever fails, shutdown
    must degrade to the old behaviour rather than raising out of the lifespan."""
    import services.scheduler_job_runner as sjr
    monkeypatch.setattr(sjr, "active_tasks", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    sched = _FakeScheduler()
    mod = await _install(sched, monkeypatch, drain=1.0)
    await mod.stop_scheduler()
    assert "shutdown(wait=False)" in sched.calls


@pytest.mark.asyncio
async def test_calling_it_with_no_scheduler_is_a_no_op(monkeypatch):
    mod = importlib.import_module("services.audit_scheduler")
    monkeypatch.setattr(mod, "_SCHEDULER", None, raising=False)
    await mod.stop_scheduler()  # must not raise


@pytest.mark.asyncio
async def test_zombies_are_not_waited_on(runner, monkeypatch):
    """A zombie already missed its deadline and refused to unwind, so waiting on one would
    burn the entire budget on the task least likely to finish."""
    async def forever():
        await asyncio.sleep(3600)

    z = runner.spawn_isolated(forever(), name="job:zombie")
    st = runner._state("z", 30.0)
    st.zombies.add(z)          # a zombie, NOT active
    assert runner.active_tasks() == set(), "zombies must be excluded from the drain set"

    sched = _FakeScheduler()
    mod = await _install(sched, monkeypatch, drain=5.0)
    started = asyncio.get_running_loop().time()
    await mod.stop_scheduler()
    assert asyncio.get_running_loop().time() - started < 2.0
    z.cancel()
