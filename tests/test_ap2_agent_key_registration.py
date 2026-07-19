"""
AP2 agent signing-key ADMIN backfill (#1442) — first-party/partner pilot
provisioning (ADR-012 carve-out; NOT agent self-serve).

`POST /admin/ap2/agent-signing-key` (admin-gated) persists a named pilot agent's
ES256/Ed25519 signing PEM into `agents.public_key` (read by
`consent_service.get_agent_identity`). These tests cover the key validator (a
validated key is provably verifiable), admin-gating, the explicit-agent_id +
existence check, validation rejection, and the writer's SQL (drift guard on the
`agents.public_key` column).
"""
import base64
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.crypto_service import crypto_service  # noqa: E402
from utils.auth import get_current_employee  # noqa: E402


# --- helpers ------------------------------------------------------------------

def _es256_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return priv, pem


def _ed25519_pem():
    return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def _client(*, as_admin=True):
    """A TestClient for the router. `as_admin` overrides the admin dependency;
    when False the real gate runs (used for the unauthenticated-rejection test)."""
    from routes.ap2_agent_registration import router

    app = FastAPI()
    app.include_router(router)
    if as_admin:
        app.dependency_overrides[get_current_employee] = lambda: {"id": "emp_test"}
    return TestClient(app)


# --- key validator ------------------------------------------------------------

def test_validate_accepts_es256_pem():
    _, pem = _es256_keypair()
    crypto_service.validate_public_key(pem, "ES256")  # no raise


def test_validate_accepts_ed25519_pem():
    crypto_service.validate_public_key(_ed25519_pem(), "Ed25519")  # no raise


def test_validate_rejects_non_p256_curve():
    pem = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    with pytest.raises(ValueError, match="P-256"):
        crypto_service.validate_public_key(pem, "ES256")


def test_validate_rejects_garbage():
    with pytest.raises(ValueError):
        crypto_service.validate_public_key("not-a-key", "ES256")


def test_validate_rejects_empty():
    with pytest.raises(ValueError):
        crypto_service.validate_public_key("", "ES256")


def test_validate_rejects_unsupported_algorithm():
    _, pem = _es256_keypair()
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        crypto_service.validate_public_key(pem, "RS256")


def test_validated_es256_key_actually_verifies():
    # The whole point: a key that passes validate_public_key must be usable by the
    # verify path — otherwise provisioning could store a key that then 401s grants.
    priv, pem = _es256_keypair()
    crypto_service.validate_public_key(pem, "ES256")
    payload = {"agent_id": "agent_x", "scope": ["pay"], "duration_hours": 24, "nonce": "n1"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = base64.b64encode(priv.sign(canonical, ec.ECDSA(hashes.SHA256()))).decode()
    assert crypto_service.verify_agent_signature(pem, sig, payload, "ES256") is True


# --- writer (drift guard on agents.public_key) --------------------------------

@pytest.mark.asyncio
async def test_writer_updates_public_key_column(monkeypatch):
    from db import agents as agents_mod

    captured = {}

    async def fake_execute(query, values=None):
        captured["query"] = query
        captured["values"] = values
        return None

    monkeypatch.setattr(agents_mod.database, "execute", fake_execute)

    _, pem = _es256_keypair()
    await agents_mod.set_agent_public_key("agent_x", pem)

    assert "UPDATE agents SET public_key" in captured["query"]
    assert captured["values"] == {"public_key": pem, "agent_id": "agent_x"}


# --- endpoint: admin backfill -------------------------------------------------

class _Calls(list):
    """A list that also carries mutable `state` (a plain list can't hold attrs)."""
    state: dict


@pytest.fixture
def stub_db(monkeypatch):
    """Stub get_agent (exists) + set_agent_public_key (capture). Returns the calls
    list; set `stub_db.state["exists"]=False` to model an unknown agent."""
    calls = _Calls()
    calls.state = {"exists": True}

    async def fake_get_agent(agent_id):
        return {"agent_id": agent_id} if calls.state["exists"] else None

    async def fake_set(agent_id, public_key):
        calls.append((agent_id, public_key))

    monkeypatch.setattr("db.agents.get_agent", fake_get_agent)
    monkeypatch.setattr("db.agents.set_agent_public_key", fake_set)
    return calls


def test_admin_backfill_stores_key_for_named_agent(stub_db):
    _, pem = _es256_keypair()
    res = _client().post(
        "/admin/ap2/agent-signing-key",
        json={"agent_id": "agent_pilot_1", "public_key": pem, "algorithm": "ES256"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "registered"
    assert body["agent_id"] == "agent_pilot_1"
    assert stub_db == [("agent_pilot_1", pem.strip())]  # route strips surrounding whitespace


def test_missing_agent_id_400(stub_db):
    _, pem = _es256_keypair()
    res = _client().post("/admin/ap2/agent-signing-key", json={"public_key": pem})
    assert res.status_code == 400
    assert stub_db == []


def test_unknown_agent_404(stub_db):
    stub_db.state["exists"] = False  # type: ignore[attr-defined]
    _, pem = _es256_keypair()
    res = _client().post(
        "/admin/ap2/agent-signing-key",
        json={"agent_id": "ghost", "public_key": pem},
    )
    assert res.status_code == 404
    assert stub_db == [], "a write must never reach an agent that doesn't exist"


def test_rejects_invalid_key_400(stub_db):
    res = _client().post(
        "/admin/ap2/agent-signing-key",
        json={"agent_id": "agent_pilot_1", "public_key": "not-a-real-pem"},
    )
    assert res.status_code == 400
    assert stub_db == [], "an invalid key must never reach the writer"


def test_rejects_unsupported_algorithm_400(stub_db):
    _, pem = _es256_keypair()
    res = _client().post(
        "/admin/ap2/agent-signing-key",
        json={"agent_id": "agent_pilot_1", "public_key": pem, "algorithm": "RS256"},
    )
    assert res.status_code == 400
    assert stub_db == []


def test_requires_admin_auth(stub_db):
    # Without the admin dependency override, the real get_current_employee gate runs
    # and rejects an unauthenticated caller (never reaching the writer).
    _, pem = _es256_keypair()
    res = _client(as_admin=False).post(
        "/admin/ap2/agent-signing-key",
        json={"agent_id": "agent_pilot_1", "public_key": pem},
    )
    assert res.status_code in (401, 403)
    assert stub_db == [], "an unauthenticated caller must never reach the writer"
