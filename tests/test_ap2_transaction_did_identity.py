"""
The signed AP2 transaction routes must work for DID-identity agents, not only
raw-PEM ones. Before ADR-012 item 1, verify_ap2_signature passed the stored
agents.public_key straight to crypto with no DID resolution, so a did:key /
did:web agent could grant consent but every /transaction/* request 401'd.

These tests store the agent's DID in agents.public_key (as the shipped resolver
slices do) and prove a signed transaction now succeeds, while unresolvable DIDs
and bad signatures still fail closed.
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

from services import ap2_did_web  # noqa: E402

AGENT_ID = "agent_did_txn"
CONSENT_TOKEN = "consent_did_txn"
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode("ascii")


def _multibase_p256(priv) -> str:
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return "z" + _b58(b"\x80\x24" + comp)


def _did_key_p256(priv) -> str:
    return "did:key:" + _multibase_p256(priv)


def _sign(priv, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(
        priv.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    ).decode("utf-8")


class FakeDB:
    def __init__(self, agents):
        self.agents = agents  # agent_id -> stored public_key (here: a DID string)
        self.consents = {
            CONSENT_TOKEN: {
                "consent_id": CONSENT_TOKEN,
                "agent_id": AGENT_ID,
                "scope": json.dumps({"actions": ["read", "create_payment"]}),
                "status": "active",
                "expires_at": datetime.utcnow() + timedelta(hours=1),
            }
        }
        self.used_nonces = set()
        self.tx_inserts = []

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            return self.consents.get(values["consent_id"])
        if "FROM agents" in query:
            aid = values["agent_id"]
            return {"public_key": self.agents[aid]} if aid in self.agents else None
        if "FROM nonce_tracker" in query:
            return {"present": 1} if values["nonce"] in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO x402_transactions" in query:
            self.tx_inserts.append(values)


@pytest.fixture
def client():
    from routes.ap2_routes import router as ap2_router

    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def _install_db(monkeypatch, fake):
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)


def _headers(signature, nonce):
    return {
        "X-Agent-Consent": CONSENT_TOKEN,
        "X-AP2-Signature": signature,
        "X-AP2-Nonce": nonce,
    }


def _initiate(client, priv, nonce):
    body = {"merchant_id": "m_1", "amount": 10.0, "currency": "USD"}
    signature = _sign(priv, {**body, "nonce": nonce})
    return client.post("/ap2/transaction/initiate", json=body,
                       headers=_headers(signature, nonce))


def test_initiate_with_did_key_agent_succeeds(client, monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    fake = FakeDB({AGENT_ID: _did_key_p256(priv)})  # public_key column holds the DID
    _install_db(monkeypatch, fake)

    res = _initiate(client, priv, "did-txn-key-1")
    assert res.status_code == 200, res.text
    assert len(fake.tx_inserts) == 1
    assert "did-txn-key-1" in fake.used_nonces  # nonce consumed on success


def test_initiate_with_did_web_agent_succeeds(client, monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:agents.example.com:bot"
    doc = {
        "id": did,
        "verificationMethod": [
            {"id": did + "#k1", "publicKeyMultibase": _multibase_p256(priv)}
        ],
    }

    async def fake_fetch(url):
        return doc

    ap2_did_web._clear_cache()
    monkeypatch.setattr(ap2_did_web, "_default_fetch", fake_fetch)
    fake = FakeDB({AGENT_ID: did})
    _install_db(monkeypatch, fake)

    res = _initiate(client, priv, "did-txn-web-1")
    assert res.status_code == 200, res.text
    assert len(fake.tx_inserts) == 1


def test_initiate_unresolvable_did_rejected_401(client, monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    fake = FakeDB({AGENT_ID: "did:key:zNOTvalidbase58!!"})  # garbage DID stored
    _install_db(monkeypatch, fake)

    res = _initiate(client, priv, "did-txn-bad-1")
    assert res.status_code == 401
    assert fake.tx_inserts == []
    assert "did-txn-bad-1" not in fake.used_nonces  # nonce NOT burned


def test_initiate_did_agent_bad_signature_rejected_401(client, monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())  # signs with the WRONG key
    fake = FakeDB({AGENT_ID: _did_key_p256(priv)})
    _install_db(monkeypatch, fake)

    body = {"merchant_id": "m_1", "amount": 10.0, "currency": "USD"}
    signature = _sign(other, {**body, "nonce": "did-txn-wrongkey"})
    res = client.post("/ap2/transaction/initiate", json=body,
                      headers=_headers(signature, "did-txn-wrongkey"))
    assert res.status_code == 401
    assert fake.tx_inserts == []
    assert "did-txn-wrongkey" not in fake.used_nonces  # bad sig never burns nonce
