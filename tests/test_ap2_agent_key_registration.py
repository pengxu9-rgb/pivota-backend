"""
AP2 agent signing-key registration (#1442) — Blocker 1 for ENABLE_AP2_ROUTES.

`POST /agent/ap2/signing-key` persists an agent's ES256/Ed25519 signing public key
into `agents.public_key` (read by `consent_service.get_agent_identity`), so the
consent-grant path stops failing closed. These tests cover:
  - the key validator (a validated key is provably verifiable — no broken key),
  - the endpoint's SECURITY binding (the key is written for the AUTHENTICATED
    agent only; a body `agent_id` is ignored),
  - validation / auth rejection,
  - the writer's SQL (drift guard on the `agents.public_key` column).
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


def _client():
    from routes.ap2_agent_registration import router

    app = FastAPI()
    app.include_router(router)
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
    # verify path — otherwise an agent could register a key that then 401s grants.
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


# --- endpoint: auth binding + validation --------------------------------------

@pytest.fixture
def stub_writer(monkeypatch):
    """Capture set_agent_public_key calls without a DB."""
    calls = []

    async def fake_set(agent_id, public_key):
        calls.append((agent_id, public_key))

    monkeypatch.setattr("db.agents.set_agent_public_key", fake_set)
    return calls


def test_register_stores_key_for_authenticated_agent(stub_writer):
    _, pem = _es256_keypair()
    res = _client().post(
        "/agent/ap2/signing-key",
        json={"public_key": pem, "algorithm": "ES256"},
        headers={"X-API-Key": "test-agent-key"},  # test-bypass → agent_id "agent_test"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "registered"
    assert body["agent_id"] == "agent_test"
    assert stub_writer == [("agent_test", pem.strip())]  # route strips surrounding whitespace


def test_register_ignores_body_agent_id(stub_writer):
    # SECURITY: an agent_id in the body must NOT let a caller set another agent's
    # key — the write is bound to the authenticated identity only.
    _, pem = _es256_keypair()
    res = _client().post(
        "/agent/ap2/signing-key",
        json={"agent_id": "victim_agent", "public_key": pem},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert res.status_code == 200, res.text
    assert stub_writer == [("agent_test", pem.strip())]
    assert stub_writer[0][0] != "victim_agent"


def test_register_rejects_invalid_key_400(stub_writer):
    res = _client().post(
        "/agent/ap2/signing-key",
        json={"public_key": "not-a-real-pem"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert res.status_code == 400
    assert stub_writer == [], "an invalid key must never reach the writer"


def test_register_rejects_unsupported_algorithm_400(stub_writer):
    _, pem = _es256_keypair()
    res = _client().post(
        "/agent/ap2/signing-key",
        json={"public_key": pem, "algorithm": "RS256"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert res.status_code == 400
    assert stub_writer == []


def test_register_requires_auth_401(stub_writer):
    _, pem = _es256_keypair()
    res = _client().post("/agent/ap2/signing-key", json={"public_key": pem})  # no credential
    assert res.status_code == 401
    assert stub_writer == [], "an unauthenticated caller must never reach the writer"
