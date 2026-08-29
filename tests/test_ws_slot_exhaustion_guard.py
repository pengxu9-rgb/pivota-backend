"""An unauthenticated WebSocket must not be able to hold the instance hostage.

Every handler here occupies a Cloud Run concurrency slot for the LIFE of the
socket, not the length of a request, and the service runs with
`--concurrency 80 --timeout 300` (infra/gcp/deploy_backend.sh). Before
`realtime/ws_guard`, an anonymous caller could hold `concurrency` slots for five
minutes each by opening sockets and saying nothing.

These tests are written to fail against the pre-fix handlers:

* remove the ceiling  -> the "third socket is refused" tests admit it
* make the ceiling per-route -> the cross-route test admits it
* drop the idle deadline -> the idle test blocks until its own timeout
* move `release()` back into `except Exception` -> the cancellation test leaks
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from realtime import ws_guard  # noqa: E402
from realtime.ws_guard import (  # noqa: E402
    WS_CLOSE_IDLE_TIMEOUT,
    WS_CLOSE_TRY_AGAIN_LATER,
    WebSocketAdmission,
    WebSocketIdleTimeout,
    idle_receive_text,
    ws_admission,
)
from routes import dashboard_routes, simple_ws_routes  # noqa: E402
from utils.auth import create_jwt_token  # noqa: E402


def admin_url(path: str) -> str:
    """Every socket here needs a real credential now.

    Minted with the SAME issuer the app verifies against (utils.auth), not a
    hand-rolled token or a monkeypatched gate — a fixture the gate would reject,
    or a gate stubbed out to accept it, would make every assertion below vacuous
    while still going green.
    """
    return f"{path}?token={create_jwt_token('ws-tests', 'admin')}"


def wait_for_slots(expected: int, timeout: float = 2.0) -> None:
    """Wait for the server side to reach `expected` held slots, then assert.

    TestClient closes the client end of a socket and returns immediately, while
    the handler's `finally` runs on the portal thread, so reading the counter
    the instant the `with` block exits races that teardown. Polling removes the
    race WITHOUT weakening the check: a handler that never releases still never
    reaches `expected`, so the "release() moved out of finally" mutant still
    fails here — it just fails after the deadline instead of instantly.
    """
    deadline = time.monotonic() + timeout
    while ws_admission.active != expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ws_admission.active == expected


@pytest.fixture(autouse=True)
def _clean_admission_state():
    """The ceiling is process-wide by design, so it is also test-global state.

    Reset on setup, asserted on TEARDOWN — those do different jobs and an
    earlier version of this docstring conflated them. The reset stops a
    predecessor's leak from being measured as this test's premise; the teardown
    assert catches a leak this test caused itself, which is what makes the
    "release() moved out of finally" mutant fail here.
    """
    ws_admission._active = 0
    # The denial log is throttled to one line per 10s, and that throttle is
    # process state on a process-wide object — without this reset, a test
    # asserting on the log passes or fails depending on whether an EARLIER test
    # happened to trip a denial in the same wall-clock window.
    ws_admission._last_denial_log = 0.0
    ws_admission._denied_since_log = 0
    ws_admission._saturated_since = None
    simple_ws_routes.simple_manager.active_connections.clear()
    yield
    wait_for_slots(0)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(simple_ws_routes.router)
    app.include_router(dashboard_routes.router)
    with TestClient(app) as test_client:
        yield test_client


# --- the ceiling: the control that does not depend on any identity ----------


def test_the_socket_that_would_exhaust_the_instance_is_refused(client, monkeypatch):
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "2")

    with client.websocket_connect(admin_url("/api/ws/simple")) as first:
        first.receive_json()
        with client.websocket_connect(admin_url("/api/ws/simple")) as second:
            second.receive_json()

            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect(admin_url("/api/ws/simple")):
                    pass

    assert refused.value.code == WS_CLOSE_TRY_AGAIN_LATER


def test_the_ceiling_is_shared_across_every_websocket_route(client, monkeypatch):
    """Capping one route while its twin stays open would only move the attack.

    Both routes now require a credential, and authentication runs before
    `reserve()`, so the flood this ceiling was written against cannot reach it
    at all. The ceiling still has to be SHARED: a per-route budget would let one
    credential holder take 2N slots instead of N, and this is the test that says
    so.
    """
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "2")

    with client.websocket_connect(admin_url("/api/ws/simple")) as first:
        first.receive_json()
        with client.websocket_connect(admin_url("/api/ws/simple")) as second:
            second.receive_json()

            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect(admin_url("/api/ws/metrics")):
                    pass

    assert refused.value.code == WS_CLOSE_TRY_AGAIN_LATER


def test_the_metrics_route_returns_its_slot_too(client):
    """The other route's `finally` was covered by nothing at all.

    Every other test that touches /api/ws/metrics expects it to be REFUSED, and
    a refusal returns before the try/finally exists — so deleting either
    `ws_admission.release()` or `manager.disconnect()` from that handler left
    the suite green. That is the worst place in this change to have a hole:
    /api/ws/metrics has no idle deadline by design, so a leaked slot there is
    never reclaimed by anything, and eight of them refuse every WebSocket on
    BOTH routes for the life of the process — the "ceiling that ratchets shut"
    this design is supposed to rule out.
    """
    manager = dashboard_routes.get_connection_manager()

    for _ in range(3):
        with client.websocket_connect(admin_url("/api/ws/metrics")) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        wait_for_slots(0)

    assert manager.get_connection_count() == 0


def test_a_closed_socket_returns_its_slot(client, monkeypatch):
    """A ceiling that never gives slots back is an outage on a timer."""
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "1")

    for _ in range(3):
        with client.websocket_connect(admin_url("/api/ws/simple")) as ws:
            ws.receive_json()
        wait_for_slots(0)

    with client.websocket_connect(admin_url("/api/ws/simple")) as ws:
        ws.receive_json()


def test_the_refusal_is_distinguishable_in_the_log(client, monkeypatch, caplog):
    """uvicorn answers a pre-accept close with a flat HTTP 403 and DISCARDS the
    close code, so the server log is the only place capacity is separable from
    authorization. The 1013 the TestClient sees below is a TestClient artifact;
    this is the production signal.
    """
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "1")

    with caplog.at_level("WARNING", logger="ws_guard"):
        with client.websocket_connect(admin_url("/api/ws/simple")) as ws:
            ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(admin_url("/api/ws/simple")):
                    pass

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert str(WS_CLOSE_TRY_AGAIN_LATER) in line
    assert "saturated for" in line
    assert "ceiling 1" in line


def test_an_oversized_ceiling_is_treated_as_a_typo(monkeypatch):
    """One stray zero must not silently disable the whole guarantee."""
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "80000")
    assert WebSocketAdmission(default_max_connections=8).max_connections == 8


def test_the_ceiling_is_not_published_to_callers(client):
    """Telling an anonymous caller the ceiling tells it how many to open.

    middleware/rate_limiter withholds its thresholds for the same reason.
    """
    body = client.get(
        "/api/ws/status",
        headers={"Authorization": f"Bearer {create_jwt_token('ws-tests', 'admin')}"},
    ).json()
    assert set(body) == {"active_connections", "timestamp"}, (
        "a new key here is how the ceiling leaks; add it deliberately or not at all"
    )


# --- the idle deadline: bounds a silent holder, and only that ----------------


class _FakeWebSocket:
    """Only the WebSocket methods the handler actually calls.

    No invented attributes: accept/send_text/receive_text/close all exist on
    starlette.websockets.WebSocket with these signatures.
    """

    def __init__(self, on_receive, token=None):
        self._on_receive = on_receive
        self.sent: list = []
        self.closed_with = None
        # Real starlette WebSockets expose .headers; authenticate_websocket
        # reads Authorization and X-ADMIN-KEY off it. Nothing invented.
        self.headers = {
            "authorization": f"Bearer {token or create_jwt_token('ws-tests', 'admin')}"
        }

    async def accept(self) -> None:
        return None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        return await self._on_receive()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code


async def test_a_silent_socket_is_reclaimed_before_the_platform_timeout(monkeypatch):
    """The whole exposure in one test: a socket that says nothing gets closed.

    Driven through `simple_websocket` directly rather than TestClient, and the
    reason is the pre-fix behaviour itself. Without the deadline this handler
    does not fail — it BLOCKS, until Cloud Run gives up at --timeout 300 — so a
    TestClient version of this test hangs a CI job instead of failing it. The
    `wait_for` here converts that same blocking into a fast, legible failure.
    `simple_websocket` is the delivering coroutine the route decorator
    registers, so nothing about the path under test is simulated.
    """
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "1")

    async def _never_speaks() -> str:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    websocket = _FakeWebSocket(_never_speaks)
    await asyncio.wait_for(simple_ws_routes.simple_websocket(websocket), timeout=15)

    assert websocket.closed_with == WS_CLOSE_IDLE_TIMEOUT
    reclaimed = json.loads(websocket.sent[-1])
    assert reclaimed["type"] == "error"
    assert reclaimed["message"] == "Idle timeout"
    assert ws_admission.active == 0


def test_ping_gets_a_pong(client, monkeypatch):
    """Round-trip only — retitled to what it actually measures.

    This was called "a talking client is not reclaimed", which it could not
    show: TestClient queues the ping into the ASGI receive queue before the
    handler awaits, so `receive_text()` is already satisfiable and the deadline
    never runs down. Verified — it still passes with the deadline mutated to
    0.1ms. The claim it used to make is now carried by
    test_a_client_that_obeys_the_published_interval_survives, which sleeps in
    real time across the deadline twice.
    """
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "5")

    with client.websocket_connect(admin_url("/api/ws/simple")) as ws:
        ws.receive_json()
        for _ in range(3):
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_a_client_that_obeys_the_published_interval_survives(client, monkeypatch):
    """The contract, not the field.

    The first version of this test asserted only that keepalive_seconds was
    echoed back, and the value echoed was the DEADLINE — so a client doing
    exactly what it was told pinged at deadline + epsilon and was dropped every
    time, and the test called that correct. Asserting the echo is not asserting
    the contract; this survives past the deadline or it fails.
    """
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "2")

    with client.websocket_connect(admin_url("/api/ws/simple")) as ws:
        hello = ws.receive_json()
        interval = hello["keepalive_seconds"]
        assert interval < hello["idle_timeout_seconds"], (
            "an interval >= the deadline drops every compliant client"
        )

        deadline = time.monotonic() + 2 * hello["idle_timeout_seconds"]
        while time.monotonic() < deadline:
            time.sleep(interval)
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_the_published_interval_leaves_room_for_one_dropped_ping(monkeypatch):
    """Half, not "a bit under": one lost ping must not cost the connection."""
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "90")
    assert ws_guard.keepalive_seconds() == 45
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "1")
    assert ws_guard.keepalive_seconds() == 1  # never 0, which would busy-loop


async def test_idle_receive_text_returns_the_message_when_one_arrives():
    class _Speaking:
        async def receive_text(self) -> str:
            return "hello"

    assert await idle_receive_text(_Speaking(), timeout=5) == "hello"


async def test_idle_receive_text_raises_on_silence():
    class _Silent:
        async def receive_text(self) -> str:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    with pytest.raises(WebSocketIdleTimeout):
        await idle_receive_text(_Silent(), timeout=0.05)


# --- slot accounting under teardown -----------------------------------------


async def test_a_cancelled_handler_returns_its_slot():
    """`except Exception` does NOT catch asyncio.CancelledError.

    That is exactly how Cloud Run ends a socket at --timeout 300 and how a
    shutdown drain ends one, so cleanup living in the exception handlers leaked
    a slot on every ordinary teardown — a ceiling that ratchets shut. This fails
    if `release()` moves out of the `finally`.
    """

    async def _cancelled():
        raise asyncio.CancelledError()

    websocket = _FakeWebSocket(_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await simple_ws_routes.simple_websocket(websocket)

    assert ws_admission.active == 0
    assert websocket not in simple_ws_routes.simple_manager.active_connections


async def test_an_erroring_handler_returns_its_slot():
    async def _explode():
        raise RuntimeError("transport died")

    websocket = _FakeWebSocket(_explode)
    await simple_ws_routes.simple_websocket(websocket)

    assert ws_admission.active == 0
    assert websocket not in simple_ws_routes.simple_manager.active_connections


# --- configuration cannot accidentally disable the ceiling -------------------


@pytest.mark.parametrize("raw", ["0", "-1", "", "  ", "eight", None])
def test_an_unusable_ceiling_falls_back_instead_of_disabling_itself(raw, monkeypatch):
    """None of these spellings can have meant "hold unlimited sockets"."""
    if raw is None:
        monkeypatch.delenv("WS_MAX_CONNECTIONS", raising=False)
    else:
        monkeypatch.setenv("WS_MAX_CONNECTIONS", raw)

    admission = WebSocketAdmission(default_max_connections=7)
    assert admission.max_connections == 7


def test_the_shipped_defaults_are_the_ones_reasoned_about(monkeypatch):
    """These two literals ARE the production configuration.

    Neither WS_MAX_CONNECTIONS nor WS_IDLE_TIMEOUT_SECONDS is set in any .sh,
    .yml, .env, Dockerfile or .tf in this repo — grep the tree. So every
    quantitative claim in this change ("8 against --concurrency 80", "90s
    against --timeout 300") rests on defaults that every other test in this file
    overrides via monkeypatch. Without this, changing 8 to 800 makes the fix a
    no-op in prod with a green build.
    """
    monkeypatch.delenv("WS_MAX_CONNECTIONS", raising=False)
    monkeypatch.delenv("WS_IDLE_TIMEOUT_SECONDS", raising=False)

    assert WebSocketAdmission().max_connections == 8
    assert ws_guard.idle_timeout_seconds() == 90
    assert ws_guard.keepalive_seconds() == 45


def test_the_close_codes_are_the_ones_on_the_wire():
    """Pinned numerically: the tests below compare output against these same
    constants, so without this the values float free and mean nothing.
    """
    assert WS_CLOSE_TRY_AGAIN_LATER == 1013  # RFC 6455 Try Again Later
    assert WS_CLOSE_IDLE_TIMEOUT == 4408  # private-use range, NOT 1001


async def test_a_close_that_fails_still_returns_the_slot(monkeypatch):
    """A socket is often silent BECAUSE the peer is already gone, so the close
    on the reclaim path is the one most likely to raise. The fake used
    elsewhere never raises, leaving that guard unexercised.
    """
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "1")

    async def _never_speaks() -> str:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    websocket = _FakeWebSocket(_never_speaks)

    async def _explode(code: int = 1000, reason: str | None = None) -> None:
        raise RuntimeError("peer already gone")

    websocket.close = _explode

    await asyncio.wait_for(simple_ws_routes.simple_websocket(websocket), timeout=15)
    assert ws_admission.active == 0


def test_the_ceiling_is_read_per_use_so_it_is_tunable_without_a_deploy(monkeypatch):
    admission = WebSocketAdmission(default_max_connections=7)
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "3")
    assert admission.max_connections == 3
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "11")
    assert admission.max_connections == 11


def test_idle_timeout_falls_back_on_junk(monkeypatch):
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "0")
    assert ws_guard.idle_timeout_seconds(default=90) == 90
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "30")
    assert ws_guard.idle_timeout_seconds(default=90) == 30


def test_release_cannot_drive_the_counter_negative():
    """Otherwise a double release would silently mint extra slots."""
    admission = WebSocketAdmission(default_max_connections=1)
    admission.release()
    admission.release()
    assert admission.active == 0
