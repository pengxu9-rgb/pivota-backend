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


# ── the ordering the drain depends on ─────────────────────────────────────────────────


def test_the_scheduler_is_stopped_before_the_database_is_disconnected():
    """A DRAIN AFTER `database.disconnect()` IS WORSE THAN NO DRAIN, which is how this
    shipped until review caught it.

    `shutdown()` is `database.disconnect()`; `shutdown_event()` stops the scheduler. With the
    disconnect first, `PostgresConnection.acquire` asserts "DatabaseBackend is not running",
    so a run that had already made its external call (settlement, refund) failed its RECORDING
    write instead of completing — and the drain handed it seconds of extra life in which to
    reach its own end and be booked `ok`. A silent half-completed run on the money path, where
    the previous behaviour was a prompt cancel.

    Asserted on the source of `app_lifespan` rather than by executing it: the lifespan boots
    the whole application, and the fact under test is a two-line ordering.
    """
    import inspect
    import main as main_mod

    src = inspect.getsource(main_mod.app_lifespan)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    ev, sd = body.find("await shutdown_event()"), body.find("await shutdown()")
    assert ev != -1 and sd != -1, f"app_lifespan no longer calls both:\n{body[-800:]}"
    assert ev < sd, (
        "app_lifespan calls `shutdown()` (database.disconnect) BEFORE `shutdown_event()` "
        "(stop_scheduler). The scheduler drain then runs against a closed pool: jobs cannot "
        "finish, they fail their recording writes, and the drain merely delays the failure."
    )


def test_the_disconnect_still_happens_if_the_drain_is_cancelled():
    """`stop_scheduler` now contains an await, so a CancelledError can land inside it. An
    `except Exception` does not catch that, so without a `finally` the disconnect would be
    skipped entirely and the pool left open."""
    import inspect
    import main as main_mod

    src = inspect.getsource(main_mod.shutdown_event)
    assert "finally:" in src and src.index("finally:") < src.index("database.disconnect()"), (
        f"shutdown_event does not guarantee the disconnect after stop_scheduler:\n{src}"
    )


# ── the configured default, which every case above monkeypatches away ─────────────────


def test_the_shipped_default_is_sane():
    """Every behavioural case sets `_DRAIN_SECONDS` explicitly, so the value the worker
    ACTUALLY runs with is exercised by none of them. Mutating the default to 0 (feature
    silently off) or 3600 (holds shutdown until the container is killed) shipped green."""
    mod = importlib.import_module("services.audit_scheduler")
    assert 0 < mod._DRAIN_SECONDS <= mod._DRAIN_MAX_SECONDS, (
        f"the shipped drain default is {mod._DRAIN_SECONDS}s, outside "
        f"(0, {mod._DRAIN_MAX_SECONDS}]. Zero disables the drain everywhere; anything past "
        "the ceiling risks the container being killed mid-shutdown."
    )


@pytest.mark.parametrize(
    "raw, expected, why",
    [
        (None, 5.0, "unset is the ordinary case"),
        ("", 5.0, "an empty value is not a request for zero"),
        ("2.5", 2.5, "a sane value is honoured"),
        ("0", 0.0, "zero is the documented opt-out"),
        ("-1", 0.0, "negative means off, not a negative timeout"),
        ("3600", 8.0, "clamped: past the ceiling loses database.disconnect()"),
        ("5s", 5.0, "A TYPO MUST NOT RAISE - see the docstring; it used to"),
        ("abc", 5.0, "nor any other non-number"),
        # NON-FINITE: `float()` accepts all three, and neither `<= 0` nor `> ceiling` can
        # reject nan (both comparisons are False), so it would reach asyncio.wait as a nan
        # timeout - which never fires. Shutdown hangs until the platform kills the container.
        ("nan", 5.0, "nan slips BOTH numeric guards and hangs asyncio.wait"),
        ("NaN", 5.0, "and float() is case-insensitive about it"),
        ("inf", 5.0, "infinite is not a timeout"),
        ("1e400", 5.0, "overflows to inf on the way in"),
    ],
)
def test_the_env_var_can_never_break_the_import(monkeypatch, raw, expected, why):
    """`float(os.getenv(...))` at import raised ValueError on a typo — and main.py imports
    this module inside `except Exception`, which turns that into "audit_scheduler boot failed
    (continuing degraded ... no worker will drain them)". A typo in a shutdown-timing knob
    would have silently disabled EVERY cron and drainer on the worker."""
    if raw is None:
        monkeypatch.delenv("SCHEDULER_DRAIN_SECONDS", raising=False)
    else:
        monkeypatch.setenv("SCHEDULER_DRAIN_SECONDS", raw)
    mod = importlib.import_module("services.audit_scheduler")
    assert mod._read_drain_seconds() == expected, why


# ── behaviour ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_short_run_in_flight_is_allowed_to_finish(runner, monkeypatch):
    """The whole point. Before this, a redeploy cancelled it — and with the worker now
    rolled on every push to main, that happens 15-34 times a day."""
    landed = []
    seen_by_the_run = []
    sched = _FakeScheduler()

    async def work():
        await asyncio.sleep(0.05)
        # WHAT THE SCHEDULER HAD ALREADY BEEN TOLD, observed from INSIDE the drain window.
        # Asserting on `sched.calls` afterwards cannot see this: the double records nothing
        # for the drain itself, so moving pause() to AFTER the drain leaves the final call
        # list byte-identical and the ordering claim unfalsifiable. Measured by a mutation
        # audit — that exact mutant survived, in the test whose message names the ordering.
        seen_by_the_run.append(list(sched.calls))
        landed.append("done")

    task = runner.spawn_isolated(work(), name="job:short")
    st = runner._state("short", 30.0)
    st.active[task] = 0.0

    mod = await _install(sched, monkeypatch, drain=5.0)
    await mod.stop_scheduler()

    assert landed == ["done"], "the in-flight run was cancelled instead of being drained"
    assert task.done() and not task.cancelled()
    assert seen_by_the_run == [["pause"]], (
        "the run did not observe a PAUSED scheduler while it was being drained (it saw "
        f"{seen_by_the_run}). Pause must come before the drain, or the timer keeps firing "
        "and the runs it spawns are not in the set being waited on — they get cancelled by "
        "shutdown() and adopted as zombies, which is the ERROR noise this change removes."
    )
    assert sched.calls == ["pause", "shutdown(wait=False)"], f"got {sched.calls}"


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
    drain = 0.2
    mod = await _install(sched, monkeypatch, drain=drain)
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

    # PROPORTIONAL TO THE CAP, not a flat 2s. With drain=0.15 and a 2.0s assertion there was
    # 13x of slack, so a cap that was wrong by 10x still passed — and 10x of the real 5s
    # default is a 50s drain, far past the platform grace this whole design is bounded by.
    # 6x absorbs a loaded CI runner without admitting an order-of-magnitude error.
    assert elapsed < drain * 6, (
        f"shutdown took {elapsed:.2f}s against a {drain}s cap - the bound is not being applied"
    )
    assert "shutdown(wait=False)" in sched.calls, "it never reached the actual shutdown"
    task.cancel()


@pytest.mark.asyncio
async def test_every_in_flight_run_is_waited_on_not_just_the_first(runner, monkeypatch):
    """`asyncio.wait` defaults to ALL_COMPLETED, and with a single in-flight task that is
    indistinguishable from FIRST_COMPLETED — adding `return_when=FIRST_COMPLETED` survived a
    mutation audit. Two runs of different lengths is the input that separates them."""
    landed = []

    async def work(n, delay):
        await asyncio.sleep(delay)
        landed.append(n)

    st = runner._state("multi", 30.0)
    for n, delay in (("fast", 0.02), ("slow", 0.12)):
        t = runner.spawn_isolated(work(n, delay), name=f"job:{n}")
        st.active[t] = 0.0

    mod = await _install(_FakeScheduler(), monkeypatch, drain=5.0)
    await mod.stop_scheduler()
    assert sorted(landed) == ["fast", "slow"], (
        f"only {landed} landed - the drain returned as soon as one run finished and "
        "cancelled the rest"
    )


@pytest.mark.asyncio
async def test_no_work_means_no_drain_and_no_noise(runner, monkeypatch, caplog):
    """`asyncio.wait(set())` raises ValueError, which the drain's own `except` would swallow
    into a "drain failed" WARNING on EVERY clean shutdown — a log line that says something
    went wrong when nothing did. Deleting the empty-set guard is invisible without this."""
    import logging as _logging
    caplog.set_level(_logging.WARNING)
    mod = await _install(_FakeScheduler(), monkeypatch, drain=5.0)
    await mod.stop_scheduler()
    noisy = [r.getMessage() for r in caplog.records
             if "drain" in r.getMessage().lower()]
    assert not noisy, f"a shutdown with nothing in flight logged: {noisy}"


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
async def test_calling_it_with_no_scheduler_is_a_no_op(runner, monkeypatch, caplog):
    """"Must not raise" alone is satisfied by almost any implementation, because the whole
    body sits inside `except Exception` — deleting the `sched is None` guard passed. Assert
    that it does NOTHING: no drain, no warning."""
    import logging as _logging
    caplog.set_level(_logging.WARNING)

    async def forever():
        await asyncio.sleep(3600)

    t = runner.spawn_isolated(forever(), name="job:none")
    runner._state("none", 30.0).active[t] = 0.0

    mod = importlib.import_module("services.audit_scheduler")
    monkeypatch.setattr(mod, "_SCHEDULER", None, raising=False)
    monkeypatch.setattr(mod, "_DRAIN_SECONDS", 5.0, raising=False)
    await mod.stop_scheduler()

    assert not t.done(), "it drained even though there was no scheduler to shut down"
    assert not [r for r in caplog.records if "drain" in r.getMessage().lower()], (
        f"it logged about draining with no scheduler: {[r.getMessage() for r in caplog.records]}"
    )
    t.cancel()


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
