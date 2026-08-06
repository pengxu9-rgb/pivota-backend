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
async def test_supervisor_repairs_a_connected_but_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-17 R17-1. `databases` leaves `is_connected` True when the pool is
    dead (the shape `_force_mark_database_disconnected` exists for: every
    query raises `InterfaceError: pool is closed`). Gating the supervisor on
    that flag skipped this shape entirely — nothing repaired it, while the
    OLD self-healing /health did. Liveness must be decided by a query."""
    calls = {"n": 0}

    class BrokenPoolDatabase(SpyDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            raise RuntimeError("pool is closed")

    spy = BrokenPoolDatabase(connected=True)  # flag says healthy, pool is not

    async def repair() -> None:
        calls["n"] += 1
        spy.execute_raises = None

    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", repair)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=1
    )

    assert calls["n"] == 1, (
        "the supervisor skipped a connected-but-broken pool — it trusted the "
        "is_connected flag instead of probing")


@pytest.mark.asyncio
async def test_supervisor_keeps_trying_after_a_failed_repair_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-17 R17-2, the nastier half. `ensure_database_ready` sets
    `is_connected=True` as a side effect of its connect attempt, even when the
    probe then fails. Gating on the flag therefore wedged the supervisor after
    ONE failed cycle — permanently, for the life of the process, including
    for 'the database system is starting up', the exact restart case this
    supervisor exists to ride out."""
    attempts = {"n": 0}

    class FlagSettingDatabase(SpyDatabase):
        async def execute(self, _query):
            self.execute_calls += 1
            raise RuntimeError("the database system is starting up")

    spy = FlagSettingDatabase(connected=False)

    async def failing_repair() -> None:
        attempts["n"] += 1
        spy.is_connected = True  # the side effect that caused the wedge
        raise DatabaseUnavailableError(
            phase="probe", error_type="RuntimeError",
            message="the database system is starting up")

    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", failing_repair)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=4
    )

    assert attempts["n"] == 4, (
        f"supervision stopped after {attempts['n']} attempt(s) — a failed "
        "repair left is_connected True and wedged the loop forever")


@pytest.mark.asyncio
async def test_supervisor_keeps_supervising_after_the_backend_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-17 R17-5 (surviving mutation `continue` -> `break`): a healthy
    cycle must CONTINUE the loop, not end it. Otherwise the first healthy
    moment retires the supervisor and the next failure is unattended."""
    spy = SpyDatabase(connected=True)
    monkeypatch.setattr(readiness, "database", spy)
    repairs = {"n": 0}

    async def repair() -> None:
        repairs["n"] += 1

    monkeypatch.setattr(readiness, "ensure_database_ready", repair)

    # Healthy for two cycles, then broken on the third.
    async def execute(_query):
        spy.execute_calls += 1
        if spy.execute_calls >= 3:
            raise RuntimeError("pool is closed")
        return 1

    monkeypatch.setattr(spy, "execute", execute)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert repairs["n"] == 1, (
        "the supervisor stopped watching after healthy cycles — a later "
        "failure would go unrepaired")


@pytest.mark.asyncio
async def test_supervisor_leaves_a_healthy_backend_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must not churn a working pool. It DOES probe every cycle — that is
    the round-17 fix (liveness decided by a query, never by the flag the
    repair path sets itself) — but a healthy probe must lead to no repair:
    no connect, no disconnect, no pool reset."""
    spy = SpyDatabase(connected=True)
    monkeypatch.setattr(readiness, "database", spy)
    repairs = {"n": 0}

    async def repair() -> None:
        repairs["n"] += 1

    monkeypatch.setattr(readiness, "ensure_database_ready", repair)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert repairs["n"] == 0, "supervisor repaired a healthy backend"
    assert spy.connect_calls == 0 and spy.disconnect_calls == 0
    assert spy.execute_calls == 3, (
        "the supervisor must PROBE each cycle — trusting is_connected is "
        "what let a dead pool go unrepaired (round-17 R17-1)")


@pytest.mark.asyncio
async def test_supervisor_survives_a_failing_reconnect_and_keeps_trying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor that dies on an error stops supervising — silently. It
    must outlive both a clean DatabaseUnavailableError and an unexpected
    exception, and still be trying on the next cycle."""

    class ExplodingDatabase(SpyDatabase):
        async def connect(self):
            self.connect_calls += 1
            raise RuntimeError("connection refused")

    spy = ExplodingDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert spy.connect_calls == 3, (
        "the supervisor stopped after a failure — it must keep trying")


@pytest.mark.asyncio
async def test_supervisor_survives_an_UNEXPECTED_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test above only reaches the DatabaseUnavailableError branch —
    `ensure_database_ready` wraps connect failures into that type, so a
    mutation deleting the catch-all `except Exception` SURVIVED the suite
    (found by mutation M4 on this commit). The catch-all is the one that
    matters: it is what stops an unforeseen error from silently ending
    supervision for the life of the process."""
    calls = {"n": 0}

    async def exploding_ensure() -> None:
        calls["n"] += 1
        raise ValueError("something nobody predicted")

    spy = SpyDatabase(connected=False)
    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", exploding_ensure)

    await readiness.run_database_reconnect_supervisor(
        interval_seconds=0.01, max_cycles=3
    )

    assert calls["n"] == 3, (
        "an unexpected exception ended supervision — the process would stay "
        "degraded forever with nothing left to repair it")


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


def test_force_disconnect_terminates_the_pool_instead_of_leaking_it():
    """ROUND-17 R17-3. Nulling `_pool` abandons the asyncpg pool with its
    server connections still open. Measured on real Postgres: one leak of
    DB_POOL_MIN_SIZE connections per call. Harmless-ish on the request path;
    on the supervisor's 30s timer it burns ~600 connections an hour and
    exhausts max_connections — killing the database for every client during
    the outage this code exists to recover from. The pool must be terminated
    before the reference is dropped."""
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

    assert terminated["n"] == 1, (
        "the pool was dropped without terminate() — its server connections "
        "leak until the backend times them out")
    assert fake_db._backend._pool is None
    assert fake_db.is_connected is False


def test_the_lifespan_supervisor_actually_repairs_with_zero_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUND-17 R17-4 (surviving mutation: lifespan started a supervisor that
    did nothing). The wiring test below is a source check; this drives the
    REAL app lifespan and proves the task it starts genuinely repairs a
    degraded backend WITHOUT any request touching a repair path."""
    # The supervisor resolves its interval at CALL time, so patching the
    # module constant reaches the task the lifespan starts — no reload, no
    # touching main's source.
    monkeypatch.setattr(
        readiness, "DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS", 0.05
    )

    import main as main_module

    repaired = {"n": 0}
    spy = SpyDatabase(connected=False)

    async def repair() -> None:
        repaired["n"] += 1
        spy.is_connected = True

    monkeypatch.setattr(readiness, "database", spy)
    monkeypatch.setattr(readiness, "ensure_database_ready", repair)

    with TestClient(main_module.app):
        # No requests at all — only the lifespan's background task runs.
        deadline = time.monotonic() + 5.0
        while repaired["n"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert repaired["n"] >= 1, (
        "the lifespan-started supervisor never repaired anything — a "
        "read-only process would stay degraded forever")


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
