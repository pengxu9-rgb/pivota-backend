"""`/health` must OBSERVE the database, never REPAIR it.

THE INCIDENT THIS PINS (2026-08-05/06, 12.5 hours of production):
`/health` called `ensure_database_ready`, which reconnects and resets pool
state before answering. Startup had left the app in degraded mode (main.py
deliberately keeps the service up when the initial DB connect fails), so
every DB-backed request raised `AssertionError: DatabaseBackend is not
running` and returned 500 — agent product search, promotions, everything —
while `/health` answered 200 each time, because asking it repaired the very
thing it was asked about. Railway's healthcheck points at `/health`, so the
platform never restarted the container; background jobs that call
`database.connect()` without a timeout hung holding their max_instances=1
slots. Total outage, invisible indicator.

The rule these tests enforce: the health path performs NO connect and NO
pool reset, and a disconnected backend surfaces as 503. Request-time
recovery is a different concern and stays in `ensure_database_ready`, which
the auth/quote/order paths still call — pinned below so a future "cleanup"
cannot collapse the two back together.
"""

from __future__ import annotations

import asyncio

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

    with pytest.raises(DatabaseUnavailableError) as excinfo:
        await readiness.probe_database_health(probe_timeout_seconds=0.05)

    assert excinfo.value.phase == "probe"
    assert excinfo.value.error_type in {"TimeoutError", "CancelledError"}


def test_health_endpoint_reports_503_when_the_backend_is_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the REAL /health route. This is the assertion that would have
    turned a silent 12.5-hour outage into a failing healthcheck."""
    from main import app

    with TestClient(app) as client:
        # Baseline on db_ok, not on the status code: this suite runs against
        # SQLite where the schema guard legitimately reports missing tables,
        # so /health is 503 for a reason unrelated to connectivity. db_ok is
        # the field this change governs.
        baseline = client.get("/health").json()
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


def test_request_paths_keep_their_recovery_helper():
    """Reporting the truth must not cost the app its ability to heal: the
    request-time recovery path stays where it belongs. If a refactor points
    these at the observe-only probe, live traffic loses reconnection."""
    import inspect

    import routes.auth as auth_routes
    import routes.order_routes as order_routes
    import routes.quote_routes as quote_routes

    for module in (auth_routes, quote_routes, order_routes):
        assert hasattr(module, "ensure_database_ready"), (
            f"{module.__name__} lost its request-time recovery helper")

    import main

    assert not hasattr(main, "ensure_database_ready"), (
        "main imports the REPAIRING helper again — /health must observe only")
    assert "probe_database_health" in inspect.getsource(main.health_check)
