"""`/health` must OBSERVE the database, never REPAIR it.

WHAT IS PROVEN, AND WHAT IS NOT. `/health` called `ensure_database_ready`,
which reconnects and resets pool state before answering. Driven on the
pre-fix commit: against a disconnected backend `/health` answered 200 five
times out of five (`db_ok: true`), having silently reconnected on the first
poll. An indicator that repairs what it measures cannot be trusted to reveal
an outage — and Railway's healthcheck points at `/health`.

The 12.5-hour outage of 2026-08-05/06 (`AssertionError: DatabaseBackend is
not running` on every DB-backed request, `/health` green throughout) is what
sent us looking here. Review rounds 16 and 17 could NOT reproduce that
combination from this code path — a single poll repairs the degraded-start
shape — so the mechanism behind that outage remains UNIDENTIFIED. Treat the
incident as motivation; the narrower claim above is what these tests pin.

The rule these tests enforce: the health path performs NO connect and NO
pool reset, and a disconnected backend surfaces as 503. Request-time
recovery is a different concern and stays in `ensure_database_ready`, which
the auth/quote/order paths still call — pinned below so a future "cleanup"
cannot collapse the two back together.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from utils import database_readiness as readiness
from utils.database_readiness import DatabaseUnavailableError


class SpyDatabase:
    """Counts every repair attempt. The health path must leave these at 0."""

    def __init__(self, *, connected: bool, execute_raises: Exception | None = None):
        self.is_connected = connected
        self.execute_raises = execute_raises
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.execute_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def execute(self, _query):
        self.execute_calls += 1
        if self.execute_raises is not None:
            raise self.execute_raises
        return 1


@pytest.mark.asyncio
async def test_probe_raises_on_a_disconnected_backend_without_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production state: is_connected False. The probe must REPORT
    it, not fix it — fixing it is what made the outage invisible."""
    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    with pytest.raises(DatabaseUnavailableError) as excinfo:
        await readiness.probe_database_health()

    assert excinfo.value.phase == "disconnected"
    assert excinfo.value.error_type == "DatabaseBackendNotRunning"
    assert spy.connect_calls == 0, (
        "the health probe reconnected — this is the defect: it reports the "
        "health of its own repair, so a dead backend reads as healthy")
    assert spy.disconnect_calls == 0
    assert spy.execute_calls == 0, "no query should be attempted while disconnected"


@pytest.mark.asyncio
async def test_probe_raises_on_query_failure_without_resetting_pool_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected-but-broken backend also reports, and must not force the
    pool-reset path (`_force_mark_database_disconnected`) that the recovery
    helper uses — the health check owns no state."""
    spy = SpyDatabase(connected=True, execute_raises=RuntimeError("pool is closed"))
    monkeypatch.setattr(readiness, "database", spy)

    with pytest.raises(DatabaseUnavailableError) as excinfo:
        await readiness.probe_database_health()

    assert excinfo.value.phase == "probe"
    assert excinfo.value.error_type == "RuntimeError"
    assert spy.connect_calls == 0 and spy.disconnect_calls == 0
    assert spy.is_connected is True, "the probe mutated connection state"


@pytest.mark.asyncio
async def test_probe_passes_and_stays_read_only_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = SpyDatabase(connected=True)
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.probe_database_health()

    assert spy.execute_calls == 1
    assert spy.connect_calls == 0 and spy.disconnect_calls == 0


@pytest.mark.asyncio
async def test_probe_reports_instead_of_hanging_when_the_query_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The jobs that hung for 12.5 hours did so on an unbounded await. The
    probe's timeout must convert a stalled backend into an answer."""

    class StallingDatabase(SpyDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            await asyncio.sleep(30)

    spy = StallingDatabase(connected=True)
    monkeypatch.setattr(readiness, "database", spy)

    started = time.monotonic()
    with pytest.raises(DatabaseUnavailableError) as excinfo:
        await readiness.probe_database_health(probe_timeout_seconds=0.05)
    elapsed = time.monotonic() - started

    assert excinfo.value.phase == "probe"
    # Exactly TimeoutError: `except Exception` cannot see CancelledError on
    # 3.11 (it derives from BaseException), so accepting it would be a value
    # that can never occur (round-16 finding).
    assert excinfo.value.error_type == "TimeoutError"
    # Wall-clock bound: without it, a lost timeout still "fails" but only
    # after the full 30s sleep — the suite hangs instead of reporting.
    assert elapsed < 1.0, f"probe ignored its timeout ({elapsed:.2f}s)"


def test_health_endpoint_reports_503_when_the_backend_is_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the REAL /health route. This is the assertion that would have
    turned a silent 12.5-hour outage into a failing healthcheck."""
    from main import app

    with TestClient(app) as client:
        # Baseline: a live backend must read healthy through the REAL route,
        # so the 503 below is attributable to connectivity and nothing else.
        baseline_resp = client.get("/health")
        baseline = baseline_resp.json()
        assert baseline_resp.status_code == 200, baseline
        assert baseline["db_ok"] is True, "baseline: a live DB must read as ok"

        spy = SpyDatabase(connected=False)
        monkeypatch.setattr(readiness, "database", spy)
        resp = client.get("/health")

    assert resp.status_code == 503, (
        "a disconnected backend reported healthy — Railway's healthcheck "
        "(healthcheckPath=/health) would never restart the container")

    # A global middleware wraps every non-2xx response in the standard
    # envelope {"status": "error", "error": {"code", "message", "details"}},
    # so the health payload arrives nested under error.details. Unwrap rather
    # than assert the envelope, so this test keeps pinning health semantics
    # even if the envelope changes.
    payload = resp.json()
    body = payload.get("error", {}).get("details", payload)
    assert body["status"] == "unhealthy"
    assert body["db_ok"] is False
    assert body["error"] == "DatabaseBackendNotRunning"
    assert spy.connect_calls == 0, "/health tried to repair the database"


@pytest.mark.asyncio
async def test_supervisor_reconnects_a_degraded_process_without_any_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE REGRESSION GUARD FOR ROUND 16. Making /health honest removed the
    accidental heal it performed when polled; the only remaining repair path
    is `ensure_database_ready`, reachable ONLY from POST login/quote/order.
    A process serving read-only traffic (agent product search, promotions —
    the endpoints that actually failed) would otherwise stay broken forever.
    The supervisor must restore it with NO request at all."""
    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=1
    )

    assert spy.connect_calls == 1, (
        "the supervisor did not reconnect a disconnected backend — a "
        "read-only process can never recover")
    assert spy.is_connected is True

@pytest.mark.asyncio
async def test_supervisor_can_be_switched_off_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-18 F5: if this loop ever misbehaves in production it must be
    stoppable without a redeploy."""
    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_ENABLED", "false")

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert spy.connect_calls == 0, "the kill switch did not stop the supervisor"

@pytest.mark.asyncio
async def test_supervisor_is_cancellable_for_clean_shutdown() -> None:
    """The lifespan cancels it on shutdown; it must not swallow that."""
    task = asyncio.create_task(
        readiness.run_database_reconnect_supervisor(interval_seconds=5)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_force_disconnect_never_terminates_a_possibly_live_pool():
    """ROUND-19 FATAL, inverted into a guard.

    A version of `_force_mark_database_disconnected` called `pool.terminate()`
    to stop the orphaned pool leaking connections. But this function runs from
    `ensure_database_ready`'s UNCONDITIONAL `finally`, after a probe bounded at
    3s — and a timeout does not prove death: a slow database, or a merely
    saturated pool whose probe queues behind `acquire`, times out identically.
    Driven on a real pool: healthy pools destroyed and 30 in-flight queries
    killed with `connection was closed in the middle of operation`.

    So the pool is ABANDONED, never terminated. That leaks its connections
    (round-17 R17-3) — the lesser harm, and the only one that does not take
    working requests down with it. If you want the leak closed, make the
    DECISION unambiguous upstream; do not re-add terminate() here."""
    terminated = {"n": 0}

    class FakePool:
        def terminate(self):
            terminated["n"] += 1

    class FakeBackend:
        def __init__(self):
            self._pool = FakePool()

    class FakeDb:
        is_connected = True

    fake_db = FakeDb()
    fake_db._backend = FakeBackend()

    original = readiness.database
    readiness.database = fake_db
    try:
        readiness._force_mark_database_disconnected()
    finally:
        readiness.database = original

    assert terminated["n"] == 0, (
        "the pool was TERMINATED on an ambiguous signal — this kills queries "
        "running on a possibly-healthy pool (round-19 FATAL)")
    assert fake_db._backend._pool is None, "the stale reference must be dropped"
    assert fake_db.is_connected is False


@pytest.mark.asyncio
async def test_supervisor_reconnects_directly_never_via_the_repairing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the CALL SHAPE, not just the effect. A mutation that swapped
    `database.connect()` for `ensure_database_ready()` survived the first
    battery, because that helper also ends up calling connect() — so counting
    connects cannot tell the safe path from the round-19 FATAL one. The
    difference that matters is invisible in the outcome: ensure_database_ready
    carries an unconditional `finally: _force_mark_database_disconnected()`
    on a 3s probe, which is how healthy pools got destroyed."""
    called = {"helper": 0}

    async def helper(**_kwargs) -> None:
        called["helper"] += 1

    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", helper)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=1
    )

    assert spy.connect_calls == 1, "the supervisor did not reconnect"
    assert called["helper"] == 0, (
        "the supervisor routed through ensure_database_ready — that helper "
        "resets pool state on an ambiguous timeout (round-19 FATAL)")


@pytest.mark.asyncio
async def test_supervisor_reconnect_is_timeout_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's background jobs hung forever on an unbounded
    `database.connect()`, holding their slots. The supervisor must not repeat
    that shape: a connect that never returns must be abandoned, not awaited
    for the life of the process."""

    class HangingDatabase(SpyDatabase):
        async def connect(self) -> None:
            self.connect_calls += 1
            await asyncio.sleep(30)

    spy = HangingDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    started = time.monotonic()
    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, connect_timeout_seconds=0.05, max_cycles=1
    )
    elapsed = time.monotonic() - started

    assert spy.connect_calls == 1
    assert elapsed < 1.0, (
        f"the supervisor awaited a hanging connect for {elapsed:.2f}s — an "
        "unbounded await is what wedged every scheduler job in the incident")


class _FakePool:
    def __init__(self, closed: bool = False):
        self._closed = closed


class RealisticSpyDatabase(SpyDatabase):
    """Models the one `databases.Database.connect()` behaviour that matters
    here: it RETURNS IMMEDIATELY when `is_connected` is already True.

    Without that, a fake happily "reconnects" a backend whose stale flag was
    never cleared — and a mutation deleting the flag-clearing survived the
    whole suite (round-20 battery), because the shipped code would have
    silently no-opped while the fake reported success. `real_connects` counts
    only connections that actually happened.
    """

    def __init__(self, *, connected: bool):
        super().__init__(connected=connected)
        self.real_connects = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.is_connected:
            return  # exactly what databases does — a silent no-op
        self.real_connects += 1
        self.is_connected = True


def _with_pool(spy, pool):
    """Give a spy DB the `_backend._pool` shape `_pool_is_provably_dead` reads."""

    class _Backend:
        pass

    backend = _Backend()
    backend._pool = pool
    spy._backend = backend
    return spy


@pytest.mark.asyncio
async def test_supervisor_recovers_a_dead_pool_behind_a_connected_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-20 F1. `is_connected=True` with `_pool=None` is the OTHER producer
    of the incident's own `AssertionError: DatabaseBackend is not running`, and
    an earlier version of this supervisor skipped it forever — a regression
    against the parent, which recovered it on any /health poll. It is a LOCAL
    fact (the pool object is gone), not an inference from a timed probe, so
    acting on it cannot destroy live work."""
    spy = _with_pool(RealisticSpyDatabase(connected=True), None)
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=1
    )

    assert spy.real_connects == 1, (
        "a provably dead pool behind a connected flag was not recovered — if "
        "the stale flag is not cleared first, databases' connect() returns "
        "immediately and the 'reconnect' is a silent no-op")
    assert spy.is_connected is True


@pytest.mark.asyncio
async def test_supervisor_recovers_a_closed_pool_behind_a_connected_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client-side `InterfaceError: pool is closed` shape — also a local
    fact (`pool._closed`), also recovered by the parent, also safe."""
    spy = _with_pool(RealisticSpyDatabase(connected=True), _FakePool(closed=True))
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=1
    )

    assert spy.real_connects == 1, (
        "a closed pool was not actually reconnected (stale flag no-op)")


@pytest.mark.asyncio
async def test_supervisor_ignores_a_live_pool_however_slow_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line that must never move again. An OPEN pool on a healthy-but-slow
    or saturated database is indistinguishable from a dead one without
    guessing — and guessing is what destroyed live pools in rounds 18 and 19.
    An open pool is left strictly alone."""
    spy = _with_pool(SpyDatabase(connected=True), _FakePool(closed=False))
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert spy.connect_calls == 0 and spy.disconnect_calls == 0, (
        "the supervisor acted on a LIVE pool — this is how in-flight queries "
        "got killed in rounds 18 and 19")
    assert spy.execute_calls == 0, "the supervisor probed; it must not"


@pytest.mark.asyncio
async def test_supervisor_keeps_supervising_after_a_successful_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-20 F2 (surviving mutation: `return` after the first success). A
    one-shot supervisor repairs the first degradation and never the second —
    precisely the failure class this arc keeps re-inventing."""
    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    async def flap():
        # Degrade again right after each successful reconnect.
        for _ in range(3):
            await asyncio.sleep(0.015)
            spy.is_connected = False

    flapper = asyncio.create_task(flap())
    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=4
    )
    flapper.cancel()

    assert spy.connect_calls >= 2, (
        f"the supervisor reconnected {spy.connect_calls} time(s) then stopped "
        "— it must keep supervising for the life of the process")


@pytest.mark.asyncio
async def test_a_failed_reconnect_is_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROUND-20 F2 (surviving mutation: failure swallowed inside the try, so
    the success line logged on failures too). In a change whose whole thesis
    is that an indicator must not lie, a failed reconnect must not be reported
    as a reconnection."""

    class RefusingDatabase(SpyDatabase):
        async def connect(self) -> None:
            self.connect_calls += 1
            raise RuntimeError("connection refused")

    spy = RefusingDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    with caplog.at_level("WARNING"):
        await readiness.run_database_reconnect_supervisor(
            interval_seconds=0.01, max_cycles=1
        )

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "FAILED" in messages, "a failed reconnect was not reported"
    assert "reconnected a dead backend" not in messages, (
        "a FAILED reconnect logged the SUCCESS line — the log lies")


def test_interval_is_clamped_at_both_ends(monkeypatch: pytest.MonkeyPatch):
    """ROUND-20 F2/F4 (surviving mutation: clamp removed). 0 or negative would
    turn the loop into a hot spin hammering `connect()` at a struggling
    database; a finite-but-huge typo (86400) would silently retire the
    supervisor with no log line."""
    for raw in ("0", "-5", "0.001"):
        monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS", raw)
        assert readiness._supervisor_interval() >= 1.0, (
            f"{raw!r} produced a hot-spin interval")
    for raw in ("86400", "1e9"):
        monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS", raw)
        assert (readiness._supervisor_interval()
                <= readiness.MAX_RECONNECT_SUPERVISOR_INTERVAL_SECONDS), (
            f"{raw!r} silently retired the supervisor")
    for raw in ("inf", "1e400", "nan", "abc"):
        monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS", raw)
        assert (readiness._supervisor_interval()
                == readiness.DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS)


def test_pool_death_is_decided_by_local_facts_only():
    """The classifier that replaced four rounds of probe-based inference."""
    class _B:
        pass

    class _DB:
        is_connected = True

    db = _DB()
    original = readiness.database
    readiness.database = db
    try:
        db._backend = None
        assert readiness._pool_is_provably_dead() is False, (
            "no backend must not read as death")
        b = _B(); b._pool = None; db._backend = b
        assert readiness._pool_is_provably_dead() is True
        b._pool = _FakePool(closed=True)
        assert readiness._pool_is_provably_dead() is True
        b._pool = _FakePool(closed=False)
        assert readiness._pool_is_provably_dead() is False, (
            "an OPEN pool must never read as dead — that is the inference "
            "that destroyed live pools")
    finally:
        readiness.database = original


@pytest.mark.asyncio
async def test_supervisor_never_touches_a_connected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SAFETY PROPERTY OF THE WHOLE DESIGN. The supervisor acts only when
    `is_connected` is False — i.e. when there is no pool in use to damage. It
    must never probe, never reset, and never call the repairing helper, even
    when the backend is broken behind the flag: four review rounds
    (PR #1683, 16-19) were spent failing to make a repairing supervisor safe,
    and every one of them destroyed something live."""
    repairs = {"n": 0}

    async def must_not_run(**_kwargs) -> None:
        repairs["n"] += 1

    class BrokenBehindTheFlag(SpyDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            raise RuntimeError("pool is closed")

    spy = BrokenBehindTheFlag(connected=True)
    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", must_not_run)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert repairs["n"] == 0, "the supervisor called the REPAIRING helper"
    assert spy.execute_calls == 0, (
        "the supervisor probed a connected backend — probing is how it talks "
        "itself into a destructive repair")
    assert spy.connect_calls == 0 and spy.disconnect_calls == 0


def test_the_lifespan_supervisor_actually_repairs_with_zero_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-17 R17-4 (surviving mutation: lifespan started a supervisor that
    did nothing). The wiring test below is a source check; this drives the
    REAL app lifespan and proves the task it starts genuinely repairs a
    degraded backend WITHOUT any request touching a repair path.

    NOTHING IS STUBBED BETWEEN THE SUPERVISOR AND ITS EFFECT. Round 19 found
    the previous version of this suite mocked out `ensure_database_ready` in
    every supervisor test, so the seam containing a FATAL bug was untested by
    construction. Here the supervisor's real code path runs and the assertion
    is on the database object it actually reconnects."""
    # The interval is resolved per call from the env (clamped to >= 1s), so
    # this reaches the task the lifespan starts without reloading main.
    monkeypatch.setenv("DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS", "1")

    import main as main_module

    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    with TestClient(main_module.app):
        # No requests at all — only the lifespan's background task runs.
        deadline = time.monotonic() + 8.0
        while spy.connect_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert spy.connect_calls >= 1, (
        "the lifespan-started supervisor never reconnected anything — a "
        "read-only process would stay degraded forever")
    assert spy.is_connected is True


def test_the_supervisor_is_wired_into_the_app_lifespan_not_the_scheduler():
    """It must be owned by the lifespan, NOT APScheduler: during the incident
    every scheduler job sat at 'maximum number of running instances reached',
    and a repair mechanism must not share fate with what it repairs."""
    import inspect

    import main
    import services.audit_scheduler as scheduler

    lifespan_src = inspect.getsource(main.app_lifespan)
    assert "run_database_reconnect_supervisor" in lifespan_src, (
        "the reconnect supervisor is not started by the app lifespan")
    assert "cancel()" in lifespan_src, "the supervisor is never cancelled"
    assert "await reconnect_supervisor" in lifespan_src, (
        "the cancelled task is never awaited — shutdown races it and Python "
        "reports 'Task was destroyed but it is pending' (round-17 R17-8)")
    assert "run_database_reconnect_supervisor" not in inspect.getsource(scheduler), (
        "the supervisor was moved into APScheduler — during the incident "
        "every scheduler job was wedged; it must not share that fate")


def test_request_paths_keep_their_recovery_helper():
    """Reporting the truth must not cost the app its ability to heal: the
    request-time recovery path stays where it belongs. If a refactor points
    these at the observe-only probe, live traffic loses reconnection."""
    import ast
    import inspect
    import textwrap

    import routes.auth as auth_routes
    import routes.order_routes as order_routes
    import routes.quote_routes as quote_routes

    for module in (auth_routes, quote_routes, order_routes):
        assert hasattr(module, "ensure_database_ready"), (
            f"{module.__name__} lost its request-time recovery helper")

    import main

    # Pin the CALL inside the health path — not main's import list, and not
    # any mention of the name. Two earlier versions of this assertion were
    # false positives: `not hasattr(main, "ensure_database_ready")` rejected a
    # legitimate reconnect-supervisor import (round-16 finding), and a bare
    # substring check tripped on the docstring that explains where recovery
    # lives. AST, so prose can never decide whether this passes.
    tree = ast.parse(textwrap.dedent(inspect.getsource(main.health_check)))
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # BOTH call shapes. A Name-only version was defeated in review round
        # 17 by `readiness.ensure_database_ready(...)` — a module-qualified
        # call is an ast.Attribute and was invisible to the pin.
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert "probe_database_health" in called, "/health stopped observing"
    assert "ensure_database_ready" not in called, (
        "/health calls the REPAIRING helper again — it must observe only")

    # ROUND-18 F4 (surviving mutation): the CONFIGURED timeout must reach the
    # probe. Hardcoding a large value shipped green — in a PR whose thesis is
    # that /health must answer Railway fast.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "probe_database_health"):
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            assert "probe_timeout_seconds" in kwargs, (
                "/health calls the probe without its configured timeout")
            assert isinstance(kwargs["probe_timeout_seconds"], ast.Name), (
                "/health hardcodes the probe timeout instead of using the "
                "DB_HEALTH_PROBE_TIMEOUT_SECONDS-derived value")
            break
    else:
        raise AssertionError("no probe_database_health call found in /health")
