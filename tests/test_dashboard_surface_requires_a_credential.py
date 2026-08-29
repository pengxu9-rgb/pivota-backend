"""The metrics dashboard surface must not answer an anonymous caller.

`realtime.metrics_store.snapshot()` defaulted to `role="admin"` and every caller
took that default, so four separate paths served the whole platform's per-PSP,
per-agent and per-merchant figures to anybody who asked — and one of them was a
MUTATION. Written to fail against the pre-fix tree:

* drop any dependency -> the parametrised anonymous sweep admits that path
* trust `?role=` again  -> a caller names its own authority
* restore the hardcoded ws_manager secret -> real tokens stop working and forged
  ones start
* reserve a slot before authenticating -> anonymous sockets spend the ceiling
* broadcast one unscoped snapshot -> a merchant socket receives PSP figures
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from realtime import metrics_store, ws_manager  # noqa: E402
from realtime.ws_guard import ws_admission  # noqa: E402
from routes import dashboard_routes, simple_ws_routes  # noqa: E402
from utils.auth import JWT_SECRET, create_jwt_token  # noqa: E402
from utils.dashboard_auth import (  # noqa: E402
    WS_CLOSE_POLICY_VIOLATION,
    DashboardAuthError,
    resolve_principal,
)

# The literal ConnectionManager used to verify against, while every token this
# system issues is signed with utils.auth.JWT_SECRET. Kept as a fixture value so
# the drift can be asserted against rather than described.
ABANDONED_WS_SECRET = "your-secret-key"


@pytest.fixture(autouse=True)
def _clean_state():
    ws_admission._active = 0
    simple_ws_routes.simple_manager.active_connections.clear()
    ws_manager.get_connection_manager().active_connections.clear()
    ws_manager.get_connection_manager().connection_metadata.clear()
    metrics_store.get_metrics_store().reset_metrics()
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(simple_ws_routes.router)
    app.include_router(dashboard_routes.router)
    with TestClient(app) as test_client:
        yield test_client


def bearer(role="admin", sub="tester", entity_id=None):
    return {"Authorization": f"Bearer {create_jwt_token(sub, role, entity_id)}"}


def wait_until(predicate, timeout: float = 2.0) -> bool:
    """TestClient closes the client end and returns while the handler's
    `finally` still runs on the portal thread. Polling removes that race
    without weakening the check — a handler that never cleans up never
    satisfies the predicate."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def seed_one_event():
    metrics_store.get_metrics_store().record_event(
        {
            "type": "payment",
            "status": "succeeded",
            "psp": "stripe",
            "agent": "agent_a",
            "merchant": "merchant_a",
            "latency_ms": 12,
        }
    )


# --- no path answers an anonymous caller ------------------------------------

ANONYMOUS_HTTP = [
    ("GET", "/api/snapshot"),
    ("GET", "/api/snapshot?role=admin"),  # naming your own authority
    ("GET", "/api/recent-events"),
    ("GET", "/api/connection-stats"),
    ("GET", "/api/ws/status"),
    ("POST", "/api/reset-metrics"),
]


@pytest.mark.parametrize("method,path", ANONYMOUS_HTTP)
def test_no_dashboard_path_answers_without_a_credential(client, method, path):
    """Parametrised deliberately: a new route added without a dependency is a
    line someone must add here to make green, rather than a silent gap."""
    response = client.request(method, path)
    assert response.status_code == 401, f"{method} {path} answered anonymously"


@pytest.mark.parametrize("path", ["/api/ws/simple", "/api/ws/metrics"])
def test_no_websocket_accepts_an_anonymous_socket(client, path):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(path):
            pass
    assert refused.value.code == WS_CLOSE_POLICY_VIOLATION


@pytest.mark.parametrize("path", ["/api/ws/simple", "/api/ws/metrics"])
def test_an_anonymous_socket_never_spends_the_ceiling(client, path, monkeypatch):
    """The residual this closes, stated as a test.

    While anonymous sockets could reserve, eight of them held the shared ceiling
    and refused the dashboard to everyone. Authentication runs first, so a
    refused socket costs nothing — reverse the two and this fails.
    """
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "2")

    for _ in range(5):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path):
                pass
        assert ws_admission.active == 0

    # and the budget is still fully available to a real caller
    token = create_jwt_token("tester", "admin")
    with client.websocket_connect(f"/api/ws/simple?token={token}") as first:
        first.receive_json()
        with client.websocket_connect(f"/api/ws/simple?token={token}") as second:
            second.receive_json()


# --- the credential decides the scope, not the caller ------------------------

def test_a_merchant_cannot_ask_for_admin_scope(client):
    seed_one_event()

    body = client.get(
        "/api/snapshot?role=admin&id=someone_else",
        headers=bearer(role="merchant", entity_id="merchant_a"),
    ).json()

    assert body["psp"] == {}, "a merchant was handed platform PSP figures"
    assert body["psp_usage"] == {}
    assert set(body["merchant"]) == {"merchant_a"}


def test_an_admin_may_still_narrow_the_view(client):
    """The operator use case survives — scope narrows, it does not escalate."""
    seed_one_event()

    body = client.get(
        "/api/snapshot?role=merchant&id=merchant_a", headers=bearer(role="admin")
    ).json()
    assert body["psp"] == {}
    assert set(body["merchant"]) == {"merchant_a"}

    full = client.get("/api/snapshot", headers=bearer(role="admin")).json()
    assert "stripe" in full["psp"]


def test_a_token_with_no_role_is_rejected_not_defaulted():
    """"Missing means admin" is the precise shape of the bug being fixed."""
    roleless = jwt.encode({"sub": "someone"}, JWT_SECRET, algorithm="HS256")
    with pytest.raises(DashboardAuthError):
        resolve_principal(token=roleless)


def test_reset_metrics_is_admin_only(client):
    seed_one_event()
    assert client.post("/api/reset-metrics", headers=bearer(role="merchant")).status_code == 403
    assert metrics_store.get_metrics_store().counters["total"] == 1

    assert client.post("/api/reset-metrics", headers=bearer(role="admin")).status_code == 200
    assert metrics_store.get_metrics_store().counters["total"] == 0


# --- one issuer, not two -----------------------------------------------------

def test_a_token_forged_with_the_abandoned_ws_secret_is_refused(client):
    """ConnectionManager verified against this literal, which is in the source."""
    forged = jwt.encode(
        {"sub": "attacker", "role": "admin"}, ABANDONED_WS_SECRET, algorithm="HS256"
    )
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/api/ws/metrics?token={forged}"):
            pass
    assert refused.value.code == WS_CLOSE_POLICY_VIOLATION


def test_a_genuinely_issued_token_is_accepted_on_the_socket(client):
    """The positive counterpart, and the one that shows the old check was inert.

    A token from this system's own issuer used to fail to decode against the
    hardcoded secret and be silently downgraded to an anonymous viewer — the
    token parameter had never once authenticated anybody. Refusing forgeries is
    only half the claim; this is the half that shows real callers work.
    """
    token = create_jwt_token("real-operator", "admin")
    with client.websocket_connect(f"/api/ws/metrics?token={token}") as ws:
        assert ws.receive_json()["type"] == "snapshot"

    manager = ws_manager.get_connection_manager()
    assert wait_until(lambda: manager.get_connection_count() == 0)


def test_the_socket_snapshot_is_scoped_to_the_token(client):
    seed_one_event()
    token = create_jwt_token("m", "merchant", "merchant_a")
    with client.websocket_connect(f"/api/ws/simple?token={token}") as ws:
        payload = ws.receive_json()["data"]
    assert payload["psp"] == {}
    assert set(payload["merchant"]) == {"merchant_a"}


# --- pushed data is scoped too, or the handshake check bought nothing --------

async def test_a_broadcast_is_built_per_connection():
    """Recipient filtering is not payload scoping.

    publish_event_to_ws built ONE unscoped snapshot and fanned it out, so a
    merchant-scoped socket received the whole platform's figures no matter how
    carefully its handshake was authenticated.
    """
    sent: dict = {}

    class _Recorder:
        def __init__(self, name):
            self.name = name

        async def send_text(self, data: str) -> None:
            import json

            sent[self.name] = json.loads(data)

    manager = ws_manager.get_connection_manager()
    manager.active_connections = {"a": _Recorder("admin"), "m": _Recorder("merchant")}
    manager.connection_metadata = {
        "a": {"user_info": {"user_id": "a", "role": "admin", "entity_id": None}},
        "m": {"user_info": {"user_id": "m", "role": "merchant", "entity_id": "merchant_a"}},
    }

    await ws_manager.publish_event_to_ws(
        {
            "type": "payment",
            "status": "succeeded",
            "psp": "stripe",
            "agent": "agent_a",
            "merchant": "merchant_a",
            "latency_ms": 12,
        }
    )

    assert "stripe" in sent["admin"]["snapshot"]["psp"]
    assert sent["merchant"]["snapshot"]["psp"] == {}
    assert set(sent["merchant"]["snapshot"]["merchant"]) == {"merchant_a"}


# --- the store itself refuses to guess --------------------------------------

def test_the_snapshot_has_no_default_role():
    """The single line that made all four paths leak."""
    with pytest.raises(TypeError):
        metrics_store.snapshot()


def test_an_unrecognised_role_receives_nothing():
    seed_one_event()
    body = metrics_store.snapshot(role="not-a-real-role")
    assert body["psp"] == {}
    assert body["agent"] == {}
    assert body["merchant"] == {}
    assert body["summary"] == {"total": 0, "success": 0, "fail": 0, "retries": 0}


# --- the auth must not rest on a secret anyone can read ----------------------

def test_a_forgeable_jwt_secret_disables_token_auth_on_a_real_server(monkeypatch):
    """config/production.py declares this secret required with min_length=32 —
    but ProductionSettings is never instantiated, so that guard has never run.
    utils.auth binds the settings fallback, a literal in this repo. Accepting
    tokens signed with it would make the whole fix theatre.
    """
    from config import platform
    from utils import dashboard_auth

    monkeypatch.setattr(dashboard_auth, "JWT_SECRET", "your-super-secret-key")
    monkeypatch.setenv("K_SERVICE", "pivota-backend")  # a Cloud Run marker
    platform.reset_platform_state()
    try:
        assert dashboard_auth._refuse_forgeable_tokens() is True
        with pytest.raises(DashboardAuthError):
            resolve_principal(token=create_jwt_token("someone", "admin"))

        # ...and the admin key still works, because it does not derive from the
        # JWT secret. Whoever has to go fix the secret needs a way in.
        monkeypatch.setenv("ADMIN_API_KEY", "a-real-admin-key")
        assert resolve_principal(admin_key="a-real-admin-key").role == "admin"
    finally:
        platform.reset_platform_state()


def test_a_strong_secret_on_a_real_server_is_accepted(monkeypatch):
    """The positive counterpart: the guard must gate on WEAKNESS, not on being
    deployed — otherwise it would disable the dashboard everywhere."""
    from config import platform
    from utils import dashboard_auth

    monkeypatch.setattr(dashboard_auth, "JWT_SECRET", "x" * 32)
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    platform.reset_platform_state()
    try:
        assert dashboard_auth._refuse_forgeable_tokens() is False
    finally:
        platform.reset_platform_state()


@pytest.mark.parametrize(
    "secret,trustworthy",
    [
        ("your-super-secret-key", False),  # the repo's literal
        ("", False),
        ("   ", False),
        ("x" * 31, False),  # under RFC 7518 3.2's floor for HS256
        ("x" * 32, True),
    ],
)
def test_secret_strength_rules(monkeypatch, secret, trustworthy):
    from utils import dashboard_auth

    monkeypatch.setattr(dashboard_auth, "JWT_SECRET", secret)
    assert dashboard_auth._jwt_secret_is_trustworthy() is trustworthy
