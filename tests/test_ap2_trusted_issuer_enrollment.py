"""
AP2 trusted-issuer enrollment endpoint (#1495) — POST/GET /admin/ap2/trusted-issuers.

Admin-gated enrollment of trusted issuers with DID-resolution proof-of-control,
supporting a platform-GLOBAL tier (approve a frontier/app issuer once → covers its
whole agent fleet) and per-agent bindings. Covers auth, scope handling, the
proof-of-control gate, the agent existence check, revoke, and list.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from utils.auth import get_current_employee  # noqa: E402

_DID_WEB = "did:web:openai.com"
_DID_KEY = "did:key:z6MkpTHR8VNsBxYAAWHut2Geadd9jSwuBV8xRoAnwWsdvktH"


def _client(*, as_admin=True):
    from routes.ap2_trusted_issuers_admin import router

    app = FastAPI()
    app.include_router(router)
    if as_admin:
        app.dependency_overrides[get_current_employee] = lambda: {"id": "emp_test"}
    return TestClient(app)


@pytest.fixture
def stubs(monkeypatch):
    """Stub DID resolution (proof-of-control) + the registry writers + get_agent.
    Returns a namespace: .adds / .revokes capture calls; .resolves ok by default
    (set .resolvable=False to fail proof-of-control); .agent_exists toggles 404."""
    import routes.ap2_trusted_issuers_admin as mod

    class S:
        adds = []
        revokes = []
        resolvable = True
        agent_exists = True

    async def fake_resolve(did, **_k):
        if not S.resolvable:
            raise ValueError("did:web document not found")
        return ("-----PEM-----", "ES256")

    async def fake_add_agent(agent_id, issuer_did, metadata=None):
        S.adds.append(("agent", agent_id, issuer_did))

    async def fake_add_global(issuer_did, metadata=None):
        S.adds.append(("global", None, issuer_did))

    async def fake_revoke_agent(agent_id, issuer_did):
        S.revokes.append(("agent", agent_id, issuer_did))

    async def fake_revoke_global(issuer_did):
        S.revokes.append(("global", None, issuer_did))

    async def fake_get_agent(agent_id):
        return {"agent_id": agent_id} if S.agent_exists else None

    async def fake_list(agent_id=None):
        return [{"issuer_did": _DID_WEB, "scope": "global", "agent_id": None, "status": "active"}]

    monkeypatch.setattr(mod, "resolve_agent_identity", fake_resolve)
    monkeypatch.setattr("db.ap2_trusted_issuers.add_trusted_issuer", fake_add_agent)
    monkeypatch.setattr("db.ap2_trusted_issuers.add_global_trusted_issuer", fake_add_global)
    monkeypatch.setattr("db.ap2_trusted_issuers.revoke_trusted_issuer", fake_revoke_agent)
    monkeypatch.setattr("db.ap2_trusted_issuers.revoke_global_trusted_issuer", fake_revoke_global)
    monkeypatch.setattr("db.ap2_trusted_issuers.list_trusted_issuers", fake_list)
    monkeypatch.setattr("db.agents.get_agent", fake_get_agent)
    return S


# --- enroll -------------------------------------------------------------------

def test_enroll_global_issuer(stubs):
    res = _client().post("/admin/ap2/trusted-issuers", json={"issuer_did": _DID_WEB, "scope": "global"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"status": "trusted", "issuer_did": _DID_WEB, "scope": "global", "agent_id": None}
    assert stubs.adds == [("global", None, _DID_WEB)]


def test_enroll_defaults_to_global(stubs):
    res = _client().post("/admin/ap2/trusted-issuers", json={"issuer_did": _DID_KEY})
    assert res.status_code == 200, res.text
    assert stubs.adds == [("global", None, _DID_KEY)]


def test_enroll_agent_scoped(stubs):
    res = _client().post(
        "/admin/ap2/trusted-issuers",
        json={"issuer_did": _DID_KEY, "scope": "agent", "agent_id": "agent_1"},
    )
    assert res.status_code == 200, res.text
    assert stubs.adds == [("agent", "agent_1", _DID_KEY)]


def test_enroll_rejects_unresolvable_did_400(stubs):
    stubs.resolvable = False  # proof-of-control fails
    res = _client().post("/admin/ap2/trusted-issuers", json={"issuer_did": _DID_WEB})
    assert res.status_code == 400
    assert "proof-of-control" in res.json()["detail"]
    assert stubs.adds == [], "an unresolvable issuer must never be trusted"


def test_enroll_rejects_non_did_400(stubs):
    res = _client().post("/admin/ap2/trusted-issuers", json={"issuer_did": "not-a-did"})
    assert res.status_code == 400
    assert stubs.adds == []


def test_enroll_agent_scope_requires_agent_id_400(stubs):
    res = _client().post("/admin/ap2/trusted-issuers", json={"issuer_did": _DID_KEY, "scope": "agent"})
    assert res.status_code == 400
    assert stubs.adds == []


def test_enroll_agent_scope_unknown_agent_404(stubs):
    stubs.agent_exists = False
    res = _client().post(
        "/admin/ap2/trusted-issuers",
        json={"issuer_did": _DID_KEY, "scope": "agent", "agent_id": "ghost"},
    )
    assert res.status_code == 404
    assert stubs.adds == []


def test_enroll_requires_admin_auth(stubs):
    res = _client(as_admin=False).post("/admin/ap2/trusted-issuers", json={"issuer_did": _DID_WEB})
    assert res.status_code in (401, 403)
    assert stubs.adds == []


# --- revoke -------------------------------------------------------------------

def test_revoke_global(stubs):
    res = _client().post("/admin/ap2/trusted-issuers/revoke", json={"issuer_did": _DID_WEB, "scope": "global"})
    assert res.status_code == 200, res.text
    assert stubs.revokes == [("global", None, _DID_WEB)]


def test_revoke_agent(stubs):
    res = _client().post(
        "/admin/ap2/trusted-issuers/revoke",
        json={"issuer_did": _DID_KEY, "scope": "agent", "agent_id": "agent_1"},
    )
    assert res.status_code == 200, res.text
    assert stubs.revokes == [("agent", "agent_1", _DID_KEY)]


def test_revoke_missing_issuer_400(stubs):
    res = _client().post("/admin/ap2/trusted-issuers/revoke", json={"scope": "global"})
    assert res.status_code == 400
    assert stubs.revokes == []


# --- list ---------------------------------------------------------------------

def test_list_returns_issuers(stubs):
    res = _client().get("/admin/ap2/trusted-issuers")
    assert res.status_code == 200, res.text
    issuers = res.json()["trusted_issuers"]
    assert issuers[0]["scope"] == "global"
    assert issuers[0]["issuer_did"] == _DID_WEB
