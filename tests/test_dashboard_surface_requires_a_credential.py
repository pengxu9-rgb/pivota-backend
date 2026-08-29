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
from utils import auth as _auth  # noqa: E402
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
    """Reset on setup AND restore on teardown.

    Cleaning only on setup was not enough, because a test here REPLACES the
    process-global manager's dicts with fakes rather than mutating them. The
    residue then survived until whichever test happened to run next in this
    module, and leaked across files: running the broadcast test immediately
    before the sibling file's metrics-slot test produced `assert 2 == 0` from
    two `_Recorder` objects that were never real connections. Reachable with
    -k, --lf, a --deselect, or any reordering. The sibling file's fixture
    asserts on teardown for exactly this reason; this one adopted the reset and
    not the teardown.
    """
    manager = ws_manager.get_connection_manager()
    original = (manager.active_connections, manager.connection_metadata)

    ws_admission._active = 0
    simple_ws_routes.simple_manager.active_connections.clear()
    manager.active_connections = {}
    manager.connection_metadata = {}
    metrics_store.get_metrics_store().reset_metrics()
    yield
    manager.active_connections, manager.connection_metadata = original
    manager.active_connections.clear()
    manager.connection_metadata.clear()
    simple_ws_routes.simple_manager.active_connections.clear()
    ws_admission._active = 0


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(simple_ws_routes.router)
    app.include_router(dashboard_routes.router)
    with TestClient(app) as test_client:
        yield test_client


def connect_bounded(client, path, timeout: float = 15.0):
    """Open a socket with a wall-clock bound, and re-raise whatever it raised.

    An unbounded `websocket_connect` does not FAIL when the refusal path stops
    closing the socket — it BLOCKS, so `backend-test-sweep` reports a 15-minute
    job timeout instead of a named failure. Verified: deleting the
    `await websocket.close(...)` from authenticate_websocket hung this file past
    420s. The sibling file designs around the same trap for its idle test; these
    need it too.
    """
    import threading

    box: dict = {}

    def _open():
        try:
            with client.websocket_connect(path):
                box["ok"] = "accepted"
        except BaseException as exc:  # re-raised on the calling thread below
            box["err"] = exc

    # A DAEMON thread, deliberately. The obvious ThreadPoolExecutor version does
    # not work: `result(timeout=...)` returns control, but `__exit__` calls
    # shutdown(wait=True) and joins the still-blocked worker forever, so the
    # bound silently does nothing. A non-daemon thread has the same problem at
    # interpreter exit. This one can simply be abandoned.
    worker = threading.Thread(target=_open, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(
            f"websocket_connect({path!r}) did not settle in {timeout}s — the "
            "refusal path is blocking rather than closing the socket"
        )
    if "err" in box:
        raise box["err"]
    return box["ok"]


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
        connect_bounded(client, path)
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
            connect_bounded(client, path)
        assert ws_admission.active == 0

    # and the budget is still fully available to a real caller
    token = create_jwt_token("tester", "admin")
    with client.websocket_connect(f"/api/ws/simple?token={token}") as first:
        first.receive_json()
        with client.websocket_connect(f"/api/ws/simple?token={token}") as second:
            second.receive_json()


# --- the credential decides the scope, not the caller ------------------------

def test_a_merchant_cannot_ask_for_admin_scope(client):
    seed_two_tenants()

    body = client.get(
        "/api/snapshot?role=admin&id=merchant_b",
        headers=bearer(role="merchant", entity_id="merchant_a"),
    ).json()

    assert body["psp"] == {}, "a merchant was handed platform PSP figures"
    assert body["psp_usage"] == {}
    assert set(body["merchant"]) == {"merchant_a"}, "a merchant reached another tenant"
    assert body["agent"] == {}
    assert body["summary"]["total"] == 1  # its own row only, not the platform's 2


def test_an_admin_may_still_narrow_the_view(client):
    """The operator use case survives — scope narrows, it does not escalate."""
    seed_two_tenants()

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
    # A FULLY VALID merchant — tenant claim and all — so this proves the admin
    # check rejects it, not merely that authentication did. Without the tenant
    # it 401s at the resolver and never reaches the authorization it is testing.
    assert client.post(
        "/api/reset-metrics", headers=bearer(role="merchant", entity_id="merchant_a")
    ).status_code == 403
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
        connect_bounded(client, f"/api/ws/metrics?token={forged}")
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
    seed_two_tenants()  # two, so this can tell narrowed from "all of the one"
    token = create_jwt_token("m", "merchant", "merchant_a")
    with client.websocket_connect(f"/api/ws/simple?token={token}") as ws:
        payload = ws.receive_json()["data"]
    assert payload["psp"] == {}
    assert set(payload["merchant"]) == {"merchant_a"}
    assert payload["agent"] == {}, "the other dimension was left whole"


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

    seed_two_tenants()  # so "narrowed" is distinguishable from "all of the one"

    manager = ws_manager.get_connection_manager()
    manager.active_connections = {"a": _Recorder("admin"), "m": _Recorder("merchant")}
    manager.connection_metadata = {
        "a": {"user_info": {"user_id": "a", "role": "admin", "entity_id": None}},
        "m": {"user_info": {"user_id": "m", "role": "merchant", "entity_id": "merchant_a"}},
        # No user_info at all. Reachable in production — see
        # test_a_connection_id_collision_cannot_evict_a_live_socket — and the
        # fail-closed `.get("role", "")` default that covers it was otherwise
        # never exercised, because every other case here supplies a full one.
        "orphan": {},
    }
    manager.active_connections["orphan"] = _Recorder("orphan")

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
    assert sent["merchant"]["snapshot"]["agent"] == {}, "other dimension left whole"

    orphan = sent["orphan"]["snapshot"]
    assert orphan["psp"] == orphan["agent"] == orphan["merchant"] == {}
    assert orphan["summary"]["total"] == 0


# --- the store itself refuses to guess --------------------------------------

def test_the_snapshot_has_no_default_role():
    """The single line that made all four paths leak."""
    with pytest.raises(TypeError):
        metrics_store.snapshot()


def seed_two_tenants():
    store = metrics_store.get_metrics_store()
    for suffix in ("a", "b"):
        store.record_event(
            {
                "type": "payment",
                "status": "succeeded",
                "psp": "stripe",
                "agent": f"agent_{suffix}",
                "merchant": f"merchant_{suffix}",
                "latency_ms": 5,
            }
        )


@pytest.mark.parametrize("role", ["agent", "merchant"])
def test_a_scoped_role_with_no_entity_id_sees_nothing(role):
    """Escalation by OMITTING a claim — the same shape as "no role means admin".

    The old filter read `elif role == "agent" and entity_id:`, which simply did
    not match when the token carried no entity_id. Execution fell past every
    branch with all three datasets and the platform totals intact, so a
    `merchant` token minted without an entity_id claim saw everything. Being
    unscopeable must mean nothing, not unlimited.
    """
    seed_two_tenants()
    body = metrics_store.snapshot(role=role, entity_id=None)
    assert body["agent"] == {}
    assert body["merchant"] == {}
    assert body["psp"] == {}
    assert body["summary"] == {"total": 0, "success": 0, "fail": 0, "retries": 0}


def test_a_scoped_agent_sees_no_other_tenant():
    """Each old branch narrowed only its OWN dimension and left the other whole,
    so a scoped agent received every merchant and vice versa. Cross-tenant."""
    seed_two_tenants()

    agent = metrics_store.snapshot(role="agent", entity_id="agent_a")
    assert set(agent["agent"]) == {"agent_a"}
    assert agent["merchant"] == {}, "an agent was handed every merchant"
    assert agent["summary"]["total"] == 1

    merchant = metrics_store.snapshot(role="merchant", entity_id="merchant_a")
    assert set(merchant["merchant"]) == {"merchant_a"}
    assert merchant["agent"] == {}, "a merchant was handed every agent"
    assert merchant["summary"]["total"] == 1


def test_a_scoped_role_over_http_cannot_reach_another_tenant(client):
    seed_two_tenants()
    body = client.get(
        "/api/snapshot", headers=bearer(role="merchant", entity_id="merchant_a")
    ).json()
    assert set(body["merchant"]) == {"merchant_a"}
    assert body["agent"] == {}
    assert body["psp"] == {}


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

    # Patch utils.auth — the single source both decode_token and the guard read.
    # Patching dashboard_auth's own name (which this did while it held a
    # `from ... import JWT_SECRET` copy) asserted the guard's opinion of a
    # secret the system was not actually signing or verifying with.
    monkeypatch.setattr(_auth, "JWT_SECRET", "your-super-secret-key")
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

    monkeypatch.setattr(_auth, "JWT_SECRET", "x" * 32)
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

    monkeypatch.setattr(_auth, "JWT_SECRET", secret)
    assert dashboard_auth._jwt_secret_is_trustworthy() is trustworthy


# --- the resolver must read the claims the issuers actually emit -------------

@pytest.mark.parametrize(
    "role,claim,tenant",
    [("merchant", "merchant_id", "merchant_a"), ("agent", "agent_id", "agent_a")],
)
def test_the_tenant_id_is_read_from_the_claim_the_issuer_writes(role, claim, tenant):
    """There is no `entity_id` claim anywhere in this system.

    utils.auth.create_jwt_token writes merchant_id/agent_id, and
    routes/auth_routes.py writes merchant_id at login. Reading `entity_id` — the
    name used internally — resolved EVERY scoped token to None, and under the
    store's old fall-through a caller with no tenant id saw everything. Minted
    through the real issuer here, not hand-rolled, so the claim shape under test
    is the one production emits.
    """
    principal = resolve_principal(token=create_jwt_token("u", role, tenant))
    assert principal.entity_id == tenant

    decoded = jwt.decode(create_jwt_token("u", role, tenant), JWT_SECRET, algorithms=["HS256"])
    assert claim in decoded and "entity_id" not in decoded


@pytest.mark.parametrize("role", ["merchant", "agent"])
def test_a_scoped_token_naming_no_tenant_is_refused(role):
    """Defence in depth: the store also returns nothing for this, but the two
    layers must not rely on each other to be safe."""
    naked = jwt.encode({"sub": "u", "role": role}, JWT_SECRET, algorithm="HS256")
    with pytest.raises(DashboardAuthError):
        resolve_principal(token=naked)



# --- findings from adversarial review of 9662674b/4d35f860 ------------------

@pytest.mark.parametrize("header", ["\xe9", "adm\xefn-key", "\xff" * 4])
def test_a_non_ascii_admin_key_does_not_crash_the_surface(monkeypatch, header):
    """hmac.compare_digest on `str` raises TypeError for any non-ASCII char, and
    Starlette decodes header bytes as latin-1 — so `X-ADMIN-KEY: \xc3\xa9` was an
    uncaught TypeError: a 500 on every route here from an unauthenticated
    caller, and only once ADMIN_API_KEY was configured, i.e. only in production.

    Driven through resolve_principal rather than TestClient because httpx
    refuses to SEND a non-ASCII header value (UnicodeEncodeError in
    httpx/_utils.py), so no test client can reach this line — a real client
    speaking HTTP has no such scruples. The strings below are exactly what
    Starlette hands the app after its latin-1 decode. Confirmed end-to-end
    against uvicorn over a raw socket; see the commit message.
    """
    monkeypatch.setenv("ADMIN_API_KEY", "a-real-admin-key")
    with pytest.raises(DashboardAuthError):  # refused, NOT TypeError
        resolve_principal(admin_key=header)


def test_the_admin_key_still_matches_after_the_bytes_fix(client, monkeypatch):
    """The positive counterpart — the fix must not break the credential."""
    monkeypatch.setenv("ADMIN_API_KEY", "a-real-admin-key")
    assert client.get("/api/snapshot", headers={"X-ADMIN-KEY": "a-real-admin-key"}).status_code == 200
    assert client.get("/api/snapshot", headers={"X-ADMIN-KEY": "wrong"}).status_code == 401
    assert resolve_principal(admin_key="a-real-admin-key").role == "admin"


async def test_the_pushed_event_payload_is_platform_wide_only():
    """Scoping the snapshot while emitting `event` verbatim left the real leak.

    A merchant-scoped socket received another tenant's order id, amount,
    transaction id and customer email in full. There is no scoped version of a
    raw event — which is exactly why /api/recent-events is admin-only.
    """
    import json

    sent: dict = {}

    class _Recorder:
        def __init__(self, name):
            self.name = name

        async def send_text(self, data: str) -> None:
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
            "agent": "agent_b",
            "merchant": "merchant_b",
            "latency_ms": 12,
            "customer_email": "victim@example.com",
            "transaction_id": "txn_secret_b",
        }
    )

    assert "event" in sent["admin"]
    assert "event" not in sent["merchant"], "a merchant was handed another tenant's raw event"
    blob = json.dumps(sent["merchant"])
    assert "victim@example.com" not in blob
    assert "txn_secret_b" not in blob
    assert "merchant_b" not in blob


def test_total_events_is_scoped_like_everything_else():
    """It sat outside the filter branch, so it reported platform volume to
    everyone — a merchant saw summary.total=1 beside total_events=3."""
    seed_two_tenants()
    metrics_store.get_metrics_store().record_event(
        {"type": "p", "status": "succeeded", "psp": "stripe",
         "agent": "agent_b", "merchant": "merchant_b", "latency_ms": 5}
    )

    assert metrics_store.snapshot(role="admin")["total_events"] == 3
    scoped = metrics_store.snapshot(role="merchant", entity_id="merchant_a")
    assert scoped["total_events"] == scoped["summary"]["total"] == 1
    assert metrics_store.snapshot(role="nonsense")["total_events"] == 0


def test_id_without_role_is_refused_rather_than_silently_ignored(client):
    """`?id=` alone fell back to the admin's platform-wide role, so entity_id was
    ignored and the caller got the WHOLE platform — the opposite of the
    narrowing the parameter advertises, with no error."""
    seed_two_tenants()
    r = client.get("/api/snapshot?id=merchant_a", headers=bearer(role="admin"))
    assert r.status_code == 400
    assert "role" in r.json()["detail"]

    ok = client.get("/api/snapshot?role=merchant&id=merchant_a", headers=bearer(role="admin"))
    assert set(ok.json()["merchant"]) == {"merchant_a"}


def test_a_query_string_token_is_no_longer_accepted_over_http(client):
    """A 24h bearer in a query string lands in uvicorn's access log and Cloud
    Run's httpRequest.requestUrl. An HTTP client can always send a header; only
    a browser WebSocket cannot, which is why the socket still takes one."""
    token = create_jwt_token("tester", "admin")
    assert client.get(f"/api/snapshot?token={token}").status_code == 401
    assert client.get("/api/snapshot", headers=bearer()).status_code == 200

    with client.websocket_connect(f"/api/ws/simple?token={token}") as ws:
        assert ws.receive_json()["type"] == "snapshot"



# --- findings from the coverage review of 4d35f860 --------------------------

def test_the_guard_fires_on_staging_too_not_only_production(monkeypatch):
    """The disjunct audit: neither half was proven necessary.

    Every other test here sets only K_SERVICE, and config/platform.py fails
    CLOSED to production on any Cloud Run marker — so under that one env both
    is_deployed() and is_production() are True and either disjunct alone passes
    the suite. Staging is the separating case, and the one the docstring argues
    the `or` exists for: deployed, but explicitly not production.
    """
    from config import platform
    from utils import dashboard_auth

    monkeypatch.setattr(_auth, "JWT_SECRET", "your-super-secret-key")
    monkeypatch.setenv("K_SERVICE", "pivota-backend")
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    platform.reset_platform_state()
    try:
        assert platform.is_deployed() is True
        assert platform.is_production() is False, "not the separating case"
        assert dashboard_auth._refuse_forgeable_tokens() is True
    finally:
        platform.reset_platform_state()


def test_a_local_host_with_a_weak_secret_is_left_alone(monkeypatch):
    """The other half of the disjunct audit: a laptop must NOT be refused."""
    from config import platform
    from utils import dashboard_auth

    monkeypatch.setattr(_auth, "JWT_SECRET", "your-super-secret-key")
    for marker in ("K_SERVICE", "K_REVISION", "K_CONFIGURATION", "PIVOTA_ENV"):
        monkeypatch.delenv(marker, raising=False)
    platform.reset_platform_state()
    try:
        assert platform.is_deployed() is False and platform.is_production() is False
        assert dashboard_auth._refuse_forgeable_tokens() is False
    finally:
        platform.reset_platform_state()


def test_the_method_has_no_default_role_either():
    """`snapshot()` was pinned; `MetricsStore.get_snapshot` was not — and it is
    the one routes/operations_routes.py calls, so it could silently regain
    role="admin" with the suite green."""
    with pytest.raises(TypeError):
        metrics_store.get_metrics_store().get_snapshot()


async def test_a_connection_id_collision_cannot_evict_a_live_socket():
    """Two sockets opened in the same millisecond used to collide on
    `conn_<ms>`: the second overwrote the first in active_connections, so the
    first stopped receiving broadcasts and its disconnect() became a no-op,
    leaking the entry. Mass reconnects are exactly when that happens, and
    reverting the monotonic counter passed the entire suite."""
    from utils.dashboard_auth import DashboardPrincipal

    manager = ws_manager.ConnectionManager()

    class _Socket:
        async def accept(self):
            return None

    ids = [await manager.connect(_Socket(), DashboardPrincipal("u", "admin")) for _ in range(50)]
    assert len(set(ids)) == 50, "ids collided; a live socket was evicted"
    assert manager.get_connection_count() == 50


def test_an_unset_admin_key_never_matches(monkeypatch):
    """The invariant _admin_key_matches' own docstring states — "an unset key
    must never match an unset header — otherwise the entire surface opens up" —
    was enforced only by the caller's `if admin_key` short-circuit, in a
    different function. Two layers must not depend on each other to be safe."""
    from utils.dashboard_auth import _admin_key_matches

    for name in ("ADMIN_API_KEY", "PROMOTIONS_ADMIN_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert _admin_key_matches("") is False
    assert _admin_key_matches("anything") is False

    monkeypatch.setenv("ADMIN_API_KEY", "   ")  # whitespace-only strips to empty
    assert _admin_key_matches("") is False
    assert _admin_key_matches("   ") is False


def test_the_guard_fires_on_production_without_platform_markers(monkeypatch):
    """The OTHER separating case, without which `is_deployed()` alone passes.

    config/platform.py documents these as independent, not nested:
    PIVOTA_ENV=production on an unmanaged host is is_production()=True with
    is_deployed()=False. The staging test covers the converse. Both are needed
    or the `or` is decoration.
    """
    from config import platform
    from utils import dashboard_auth

    monkeypatch.setattr(_auth, "JWT_SECRET", "your-super-secret-key")
    for marker in ("K_SERVICE", "K_REVISION", "K_CONFIGURATION", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "production")
    platform.reset_platform_state()
    try:
        assert platform.is_deployed() is False, "not the separating case"
        assert platform.is_production() is True
        assert dashboard_auth._refuse_forgeable_tokens() is True
    finally:
        platform.reset_platform_state()


def test_the_operations_dashboard_gives_a_roleless_token_nothing():
    """The fifth path to the same store, and it had no test at all.

    routes/operations_routes.py read `credentials.get("role") or "operator"`,
    and "operator" is platform-wide — so a token carrying no role claim received
    the WHOLE platform from a route this PR touched. Exactly the default this
    change exists to abolish, re-introduced by the line meant to fix it.
    """
    from fastapi import FastAPI
    from routes import operations_routes

    seed_two_tenants()
    app = FastAPI()
    app.include_router(operations_routes.router)

    with TestClient(app) as ops:
        roleless = jwt.encode({"sub": "someone"}, JWT_SECRET, algorithm="HS256")
        body = ops.get(f"/api/operations/dashboard-summary?token={roleless}").json()
        assert body["system_health"]["total_transactions"] == 0, (
            "a role-less token was handed the whole platform"
        )

        admin = ops.get(
            f"/api/operations/dashboard-summary?token={create_jwt_token('op', 'admin')}"
        ).json()
        assert admin["system_health"]["total_transactions"] == 2

        # employee is platform-wide per utils/auth.py's own permission map
        employee = ops.get(
            f"/api/operations/dashboard-summary?token={create_jwt_token('e', 'employee')}"
        ).json()
        assert employee["system_health"]["total_transactions"] == 2
