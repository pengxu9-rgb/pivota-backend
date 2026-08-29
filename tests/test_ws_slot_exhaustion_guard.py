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
    WS_CLOSE_GOING_AWAY,
    WS_CLOSE_TRY_AGAIN_LATER,
    WebSocketAdmission,
    WebSocketIdleTimeout,
    idle_receive_text,
    ws_admission,
)
from routes import dashboard_routes, simple_ws_routes  # noqa: E402


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

    Asserted rather than merely reset: a test that starts with slots already
    held would read as a passing ceiling test while actually measuring leakage
    from its predecessor.
    """
    ws_admission._active = 0
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

    with client.websocket_connect("/api/ws/simple") as first:
        first.receive_json()
        with client.websocket_connect("/api/ws/simple") as second:
            second.receive_json()

            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect("/api/ws/simple"):
                    pass

    assert refused.value.code == WS_CLOSE_TRY_AGAIN_LATER


def test_the_ceiling_is_shared_across_every_websocket_route(client, monkeypatch):
    """Capping one route while its twin stays open would only move the attack.

    /api/ws/metrics is reachable with no credential too — ConnectionManager
    downgrades a missing or undecodable token to an anonymous session — so a
    per-route budget would leave the instance exhaustible at 2N sockets.
    """
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "2")

    with client.websocket_connect("/api/ws/simple") as first:
        first.receive_json()
        with client.websocket_connect("/api/ws/simple") as second:
            second.receive_json()

            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect("/api/ws/metrics"):
                    pass

    assert refused.value.code == WS_CLOSE_TRY_AGAIN_LATER


def test_a_closed_socket_returns_its_slot(client, monkeypatch):
    """A ceiling that never gives slots back is an outage on a timer."""
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "1")

    for _ in range(3):
        with client.websocket_connect("/api/ws/simple") as ws:
            ws.receive_json()
        wait_for_slots(0)

    with client.websocket_connect("/api/ws/simple") as ws:
        ws.receive_json()


def test_the_ceiling_is_not_published_to_callers(client):
    """Telling an anonymous caller the ceiling tells it how many to open.

    middleware/rate_limiter withholds its thresholds for the same reason.
    """
    body = client.get("/api/ws/status").json()
    assert "max_connections" not in body
    assert "ws_max_connections" not in body


# --- the idle deadline: bounds a silent holder, and only that ----------------


class _FakeWebSocket:
    """Only the WebSocket methods the handler actually calls.

    No invented attributes: accept/send_text/receive_text/close all exist on
    starlette.websockets.WebSocket with these signatures.
    """

    def __init__(self, on_receive):
        self._on_receive = on_receive
        self.sent: list = []
        self.closed_with = None

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

    assert websocket.closed_with == WS_CLOSE_GOING_AWAY
    reclaimed = json.loads(websocket.sent[-1])
    assert reclaimed["type"] == "error"
    assert reclaimed["message"] == "Idle timeout"
    assert ws_admission.active == 0


def test_a_talking_client_is_not_reclaimed(client, monkeypatch):
    """The deadline must bound silence, not the connection itself."""
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "5")

    with client.websocket_connect("/api/ws/simple") as ws:
        ws.receive_json()
        for _ in range(3):
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_the_keepalive_interval_is_published(client, monkeypatch):
    """A client cannot honour a deadline it was never told about."""
    monkeypatch.setenv("WS_IDLE_TIMEOUT_SECONDS", "42")

    with client.websocket_connect("/api/ws/simple") as ws:
        assert ws.receive_json()["keepalive_seconds"] == 42


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
