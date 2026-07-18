"""
Integration coverage for mounting the AP2 router behind AP2SecurityMiddleware,
the way main.py wires it when ENABLE_AP2_ROUTES=true.

Focus: the middleware must NOT block POST /ap2/consent/grant for lacking an
X-Agent-Consent token (that endpoint mints one), while still gating other
non-public /ap2/* routes.
"""

import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


AGENT_ID = "agent_mount_test"


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pub


def _sign_consent(priv, agent_id, scope, duration_hours, nonce):
    payload = {
        "agent_id": agent_id,
        "scope": scope,
        "duration_hours": duration_hours,
        "nonce": nonce,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(
        priv.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    ).decode("utf-8")


class FakeDB:
    def __init__(self, agents):
        self.agents = agents
        self.used_nonces = set()
        self.consents = []

    async def fetch_one(self, query, values=None):
        if "FROM agents" in query:
            aid = values["agent_id"]
            return {"public_key": self.agents[aid]} if aid in self.agents else None
        if "FROM nonce_tracker" in query:
            return {"nonce": values["nonce"]} if values["nonce"] in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO agent_consents" in query:
            self.consents.append(values)


@pytest.fixture
def keypair():
    return _keypair()


@pytest.fixture
def fake_db(keypair, monkeypatch):
    _, pub = keypair
    fake = FakeDB(agents={AGENT_ID: pub})
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)
    return fake


def _app(enabled: bool):
    """Mirror main.py's wiring: AP2SecurityMiddleware + ap2_router."""
    from middleware.ap2_security import AP2SecurityMiddleware
    from routes.ap2_routes import router as ap2_router

    app = FastAPI()
    app.add_middleware(AP2SecurityMiddleware, enabled=enabled)
    app.include_router(ap2_router)
    return TestClient(app)


def test_public_status_open_when_enabled(fake_db):
    client = _app(enabled=True)
    res = client.get("/ap2/status")
    assert res.status_code == 200
    assert res.json()["protocol"] == "AP2"
    # Middleware stamps protocol headers on the way out.
    assert res.headers.get("X-AP2-Version") == "0.1"


def test_protocols_advertises_only_functional_endpoints(fake_db):
    """The /protocols catalog must not advertise the 501 not-implemented stubs
    (`wallet/balance`, `x402/exchange`) — we don't promise unbuilt endpoints.
    Functional routes stay advertised."""
    client = _app(enabled=True)
    res = client.get("/ap2/protocols")
    assert res.status_code == 200
    advertised = {e for p in res.json()["protocols"] for e in p["endpoints"]}
    assert "/ap2/wallet/balance" not in advertised
    assert "/ap2/x402/exchange" not in advertised
    # sanity: the functional endpoints are still advertised
    assert {"/ap2/transaction/initiate", "/ap2/x402/quote"} <= advertised


def test_grant_reachable_through_enabled_middleware(fake_db, keypair):
    """The consent-bootstrap route must pass the middleware without a token."""
    priv, _ = keypair
    client = _app(enabled=True)
    scope = ["read"]
    sig = _sign_consent(priv, AGENT_ID, scope, 24, "mount-001")

    res = client.post(
        "/ap2/consent/grant",
        json={"agent_id": AGENT_ID, "scope": scope, "duration_hours": 24},
        headers={"X-AP2-Signature": sig, "X-AP2-Nonce": "mount-001"},
    )

    assert res.status_code == 200
    assert res.json()["consent_token"].startswith("consent_")


def test_grant_missing_headers_reaches_route_not_middleware(fake_db):
    """
    With no auth headers, grant is still treated as public by the middleware, so
    the 400 comes from the ROUTE ("Missing required headers"), not the
    middleware's 401 "Missing X-Agent-Consent".
    """
    client = _app(enabled=True)
    res = client.post(
        "/ap2/consent/grant",
        json={"agent_id": AGENT_ID, "scope": ["read"], "duration_hours": 24},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "Missing required headers (X-AP2-Signature, X-AP2-Nonce)"


def test_protected_route_gated_by_middleware(fake_db):
    """A non-public /ap2/* route with no headers is blocked by the middleware."""
    client = _app(enabled=True)
    res = client.get("/ap2/wallet/balance")
    assert res.status_code == 401
    assert res.json()["error"] == "Missing X-Agent-Consent header"


def test_disabled_middleware_is_passthrough(fake_db):
    """When disabled, the middleware does not gate anything (route handles it)."""
    client = _app(enabled=False)
    res = client.get("/ap2/wallet/balance")
    # Reaches the route, which is an honest 501 not-implemented stub (no balance
    # source; see docs/AP2_ENABLEMENT.md §6) -- NOT the middleware's 401. Proves
    # the disabled middleware passed the request through to the route.
    assert res.status_code == 501
