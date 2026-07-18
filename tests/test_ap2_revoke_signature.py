"""
POST /ap2/consent/revoke is signature-authenticated at the ROUTE level
(Depends(verify_ap2_signature)) — the real gate, since AP2SecurityMiddleware is
flag-gated off. A leaked consent token alone cannot revoke; the caller must prove
the consent's own agent with a fresh-nonce signature.
"""
import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.ap2_signing import build_ap2_signed_payload  # noqa: E402
from services.crypto_service import crypto_service  # noqa: E402

AGENT_ID = "agent_revoke"
CONSENT_TOKEN = "consent_to_revoke"


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pem


class FakeDB:
    def __init__(self, public_pem):
        self.public_pem = public_pem
        self.used_nonces = set()
        self.revoked = []

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            return {
                "consent_id": CONSENT_TOKEN, "agent_id": AGENT_ID,
                "scope": json.dumps({"actions": ["read"]}),
                "status": "active",
                "expires_at": datetime.utcnow() + timedelta(hours=1),
            }
        if "FROM agents" in query:
            return {"did": None, "public_key": self.public_pem} \
                if values["agent_id"] == AGENT_ID else None
        if "FROM nonce_tracker" in query:
            return {"present": 1} if values["nonce"] in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "UPDATE agent_consents" in query and "revoked" in query:
            self.revoked.append(values)


@pytest.fixture
def keypair():
    return _keypair()


@pytest.fixture
def client(keypair, monkeypatch):
    _, pem = keypair
    fake = FakeDB(pem)
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)

    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    c = TestClient(app)
    c.fake = fake
    return c


def _sig(priv, nonce, body=None):
    signed = build_ap2_signed_payload(body or {}, nonce)
    msg = crypto_service.canonicalize_json(signed).encode()
    return base64.b64encode(priv.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()


def test_revoke_with_valid_signature_succeeds(client, keypair):
    priv, _ = keypair
    res = client.post(
        "/ap2/consent/revoke", json={},
        headers={"X-Agent-Consent": CONSENT_TOKEN,
                 "X-AP2-Signature": _sig(priv, "rev-1"), "X-AP2-Nonce": "rev-1"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "revoked"
    assert len(client.fake.revoked) == 1


def test_revoke_missing_signature_rejected_401(client):
    # A bearer consent token alone must not revoke.
    res = client.post(
        "/ap2/consent/revoke", json={},
        headers={"X-Agent-Consent": CONSENT_TOKEN, "X-AP2-Nonce": "rev-2"},
    )
    assert res.status_code == 401
    assert client.fake.revoked == []


def test_revoke_wrong_key_signature_rejected_401(client):
    other, _ = _keypair()
    res = client.post(
        "/ap2/consent/revoke", json={},
        headers={"X-Agent-Consent": CONSENT_TOKEN,
                 "X-AP2-Signature": _sig(other, "rev-3"), "X-AP2-Nonce": "rev-3"},
    )
    assert res.status_code == 401
    assert client.fake.revoked == []


def test_revoke_replayed_nonce_rejected_409(client, keypair):
    priv, _ = keypair
    h = {"X-Agent-Consent": CONSENT_TOKEN,
         "X-AP2-Signature": _sig(priv, "rev-4"), "X-AP2-Nonce": "rev-4"}
    first = client.post("/ap2/consent/revoke", json={}, headers=h)
    assert first.status_code == 200
    replay = client.post("/ap2/consent/revoke", json={}, headers=h)
    assert replay.status_code == 409
