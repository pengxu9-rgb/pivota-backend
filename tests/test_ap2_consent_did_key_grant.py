"""
End-to-end: POST /ap2/consent/grant with a did:key identity (issue #1442, ADR-012).

The agent's `agents.public_key` holds a `did:key:...` value (not a PEM). The route
resolves the verification key FROM the DID and verifies the signature against it —
no key upload, no platform secret. Mirrors tests/test_ap2_consent_grant_route.py
but exercises the did:key resolution path.
"""
import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

AGENT_ID = "did-agent-1"
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_MC_ED25519 = b"\xed\x01"
_MC_P256 = b"\x80\x24"


def _b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    n_leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * n_leading + out.decode("ascii")


def _did_key(multicodec: bytes, key_bytes: bytes) -> str:
    return "did:key:z" + _b58encode(multicodec + key_bytes)


def _es256_did_key():
    priv = ec.generate_private_key(ec.SECP256R1())
    compressed = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return priv, _did_key(_MC_P256, compressed)


def _ed25519_did_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, _did_key(_MC_ED25519, raw)


def _canonical(agent_id, scope, duration_hours, nonce) -> bytes:
    # Must match services/ap2_signing.build_ap2_signed_payload as invoked by
    # create_consent: {agent_id, scope, duration_hours, nonce}.
    payload = {
        "agent_id": agent_id,
        "scope": scope,
        "duration_hours": duration_hours,
        "nonce": nonce,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_es256(priv, message: bytes) -> str:
    return base64.b64encode(priv.sign(message, ec.ECDSA(hashes.SHA256()))).decode()


def _sign_ed25519(priv, message: bytes) -> str:
    return base64.b64encode(priv.sign(message)).decode()


class FakeDB:
    def __init__(self, agents):
        self.agents = agents            # agent_id -> stored public_key (did:key str)
        self.used_nonces = set()
        self.consents = []

    async def fetch_one(self, query, values=None):
        if "FROM agents" in query:
            aid = values["agent_id"]
            if aid not in self.agents:
                return None
            return {"public_key": self.agents[aid]}
        if "FROM nonce_tracker" in query:
            n = values["nonce"]
            return {"nonce": n} if n in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO agent_consents" in query:
            self.consents.append(values)


@pytest.fixture
def client():
    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def _install_db(monkeypatch, agents):
    fake = FakeDB(agents)
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)
    return fake


def _post(client, signature, nonce, scope=None, duration_hours=24):
    return client.post(
        "/ap2/consent/grant",
        json={"agent_id": AGENT_ID, "scope": scope or ["read"], "duration_hours": duration_hours},
        headers={"X-AP2-Signature": signature, "X-AP2-Nonce": nonce},
    )


def test_es256_did_key_grants_consent(client, monkeypatch):
    priv, did = _es256_did_key()
    fake = _install_db(monkeypatch, {AGENT_ID: did})
    scope = ["read", "create_payment"]
    sig = _sign_es256(priv, _canonical(AGENT_ID, scope, 24, "n-es-1"))

    res = _post(client, sig, "n-es-1", scope=scope)

    assert res.status_code == 200, res.text
    assert res.json()["consent_token"].startswith("consent_")
    assert len(fake.consents) == 1


def test_ed25519_did_key_grants_consent(client, monkeypatch):
    priv, did = _ed25519_did_key()
    fake = _install_db(monkeypatch, {AGENT_ID: did})
    sig = _sign_ed25519(priv, _canonical(AGENT_ID, ["read"], 24, "n-ed-1"))

    res = _post(client, sig, "n-ed-1")

    assert res.status_code == 200, res.text
    assert res.json()["consent_token"].startswith("consent_")
    assert len(fake.consents) == 1


def test_wrong_key_for_did_rejected_401(client, monkeypatch):
    _, did = _es256_did_key()          # stored identity
    other = ec.generate_private_key(ec.SECP256R1())  # attacker signs with another key
    fake = _install_db(monkeypatch, {AGENT_ID: did})
    sig = _sign_es256(other, _canonical(AGENT_ID, ["read"], 24, "n-es-2"))

    res = _post(client, sig, "n-es-2")

    assert res.status_code == 401
    assert fake.consents == []


def test_malformed_did_key_fails_closed_401(client, monkeypatch):
    priv, _ = _es256_did_key()
    fake = _install_db(monkeypatch, {AGENT_ID: "did:key:z0INVALID"})  # bad base58
    sig = _sign_es256(priv, _canonical(AGENT_ID, ["read"], 24, "n-es-3"))

    res = _post(client, sig, "n-es-3")

    assert res.status_code == 401
    assert fake.consents == []
