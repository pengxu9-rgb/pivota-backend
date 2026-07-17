"""
Route-level coverage for POST /ap2/consent/grant signature verification.

The route must resolve the agent's REGISTERED public key (agents.public_key)
and verify X-AP2-Signature against it — never trust a caller-supplied key,
and never issue a consent token for an unverified signature.
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

# Allow running from repo root without manually setting PYTHONPATH.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


AGENT_ID = "agent_test_ap2"


def _generate_es256_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, public_pem


def _sign_consent_payload(private_key, agent_id, scope, duration_hours, nonce):
    """Sign the exact canonical payload ConsentService.create_consent verifies."""
    payload = {
        "agent_id": agent_id,
        "scope": scope,
        "duration_hours": duration_hours,
        "nonce": nonce,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = private_key.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode("utf-8")


class FakeDB:
    """Dispatches the queries grant_consent issues; tracks nonces and consents."""

    def __init__(self, agents=None):
        # agent_id -> public_key (None = registered agent without a key)
        self.agents = agents if agents is not None else {}
        self.used_nonces = set()
        self.consents = []

    async def fetch_one(self, query, values=None):
        if "FROM agents" in query:
            agent_id = values["agent_id"]
            if agent_id not in self.agents:
                return None
            return {"public_key": self.agents[agent_id]}
        if "FROM nonce_tracker" in query:
            nonce = values["nonce"]
            return {"nonce": nonce} if nonce in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO agent_consents" in query:
            self.consents.append(values)


@pytest.fixture
def keypair():
    return _generate_es256_keypair()


@pytest.fixture
def fake_db(keypair, monkeypatch):
    _, public_pem = keypair
    fake = FakeDB(agents={AGENT_ID: public_pem})
    # Patch methods on the shared singleton (route and service both bind it).
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)
    return fake


@pytest.fixture
def client():
    from routes.ap2_routes import router as ap2_router

    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def _grant(client, *, signature, nonce, agent_id=AGENT_ID, scope=None,
           duration_hours=24, algorithm=None):
    body = {
        "agent_id": agent_id,
        "scope": scope if scope is not None else ["read"],
        "duration_hours": duration_hours,
    }
    if algorithm is not None:
        body["algorithm"] = algorithm
    return client.post(
        "/ap2/consent/grant",
        json=body,
        headers={"X-AP2-Signature": signature, "X-AP2-Nonce": nonce},
    )


def test_valid_signature_grants_consent(client, fake_db, keypair):
    private_key, _ = keypair
    scope = ["read", "create_payment"]
    signature = _sign_consent_payload(private_key, AGENT_ID, scope, 24, "nonce-001")

    res = _grant(client, signature=signature, nonce="nonce-001", scope=scope)

    assert res.status_code == 200
    data = res.json()
    assert data["consent_token"].startswith("consent_")
    assert data["scope"] == scope
    assert len(fake_db.consents) == 1
    assert fake_db.consents[0]["agent_id"] == AGENT_ID


def test_invalid_signature_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    # Signature over a DIFFERENT payload (tampered scope) must not verify.
    signature = _sign_consent_payload(
        private_key, AGENT_ID, ["read"], 24, "nonce-002"
    )

    res = _grant(
        client, signature=signature, nonce="nonce-002",
        scope=["read", "create_payment"],  # escalated vs what was signed
    )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid signature"
    assert fake_db.consents == []


def test_garbage_signature_rejected_401(client, fake_db):
    res = _grant(
        client,
        signature=base64.b64encode(b"not-a-real-signature").decode(),
        nonce="nonce-003",
    )

    assert res.status_code == 401
    assert fake_db.consents == []


def test_signature_from_wrong_key_rejected_401(client, fake_db):
    other_key, _ = _generate_es256_keypair()
    signature = _sign_consent_payload(other_key, AGENT_ID, ["read"], 24, "nonce-004")

    res = _grant(client, signature=signature, nonce="nonce-004")

    assert res.status_code == 401
    assert fake_db.consents == []


def test_unknown_agent_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign_consent_payload(
        private_key, "agent_nobody", ["read"], 24, "nonce-005"
    )

    res = _grant(client, signature=signature, nonce="nonce-005",
                 agent_id="agent_nobody")

    assert res.status_code == 401
    assert res.json()["detail"] == "Unknown agent"


def test_agent_without_registered_key_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    fake_db.agents["agent_keyless"] = None
    signature = _sign_consent_payload(
        private_key, "agent_keyless", ["read"], 24, "nonce-006"
    )

    res = _grant(client, signature=signature, nonce="nonce-006",
                 agent_id="agent_keyless")

    assert res.status_code == 401
    assert "no registered public key" in res.json()["detail"]
    assert fake_db.consents == []


def test_nonce_replay_rejected_409(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign_consent_payload(private_key, AGENT_ID, ["read"], 24, "nonce-007")

    first = _grant(client, signature=signature, nonce="nonce-007")
    assert first.status_code == 200

    replay = _grant(client, signature=signature, nonce="nonce-007")
    assert replay.status_code == 409
    assert replay.json()["detail"] == "Nonce already used"
    assert len(fake_db.consents) == 1


def test_missing_headers_rejected_400(client, fake_db):
    res = client.post("/ap2/consent/grant", json={"agent_id": AGENT_ID})
    assert res.status_code == 400


def test_unsupported_algorithm_rejected_400(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign_consent_payload(private_key, AGENT_ID, ["read"], 24, "nonce-008")

    res = _grant(client, signature=signature, nonce="nonce-008", algorithm="none")

    assert res.status_code == 400
    assert "Unsupported algorithm" in res.json()["detail"]


def test_algorithm_selector_excluded_from_signed_payload(client, fake_db, keypair):
    """
    `algorithm` is a scheme selector, not signed content (services/ap2_signing.py).
    A signature over the 4-key payload verifies even when the body advertises
    `algorithm`, so the canonical contract stays identical to the middleware's.
    """
    private_key, _ = keypair
    scope = ["read"]
    signature = _sign_consent_payload(private_key, AGENT_ID, scope, 24, "nonce-009")

    res = _grant(client, signature=signature, nonce="nonce-009", scope=scope,
                 algorithm="ES256")

    assert res.status_code == 200
