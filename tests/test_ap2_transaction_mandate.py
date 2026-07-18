"""
End-to-end AP2 mandate authority (ADR-012 slice 3): POST /ap2/transaction/initiate
with an optional user-signed Intent→Cart→Payment mandate chain. Exercises the
full stack — agent signs the request (verify_ap2_signature), the issuer-signed
chain is verified against the agent's trusted issuers (registry), and the chain
is bound to the concrete transaction. Deny-by-default; absent a chain, behavior
is unchanged.
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
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

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CONSENT_TOKEN = "consent_txn_mandate"
NONCE = "txn-mandate-1"
# Realistic identity shape: agent_id is the INTERNAL id (VARCHAR-50), the DID
# lives in agents.public_key. A did:key (~57 chars) cannot BE the agent_id.
AGENT_ID = "agent_internal_txn"


def _b58(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode("ascii")


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return priv, "did:key:z" + _b58(b"\x80\x24" + comp)


def _pem_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pem


def _es256(priv, message: bytes) -> str:
    return base64.b64encode(priv.sign(message, ec.ECDSA(hashes.SHA256()))).decode()


def _sign_obj(priv, obj) -> str:
    return _es256(priv, crypto_service.canonicalize_json(obj).encode())


class FakeDB:
    def __init__(self, agent_did, trusted_issuer_dids, *, agent_id=AGENT_ID,
                 public_key=None):
        self.agent_id = agent_id                       # internal id (in consent)
        self.agent_did = agent_did                     # subject the mandates bind to
        # agents.public_key holds the DID for a DID agent (default), but a
        # raw-PEM pilot agent has a non-DID public_key.
        self.public_key = public_key if public_key is not None else agent_did
        self.trusted = set(trusted_issuer_dids)
        self.used_nonces = set()
        self.tx = []

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            return {
                "consent_id": CONSENT_TOKEN, "agent_id": self.agent_id,
                "scope": json.dumps({"actions": ["read", "create_payment"]}),
                "status": "active",
                "spending_limit": None,
                "spent_amount": 0,
                "expires_at": datetime.utcnow() + timedelta(hours=1),
            }
        if "FROM agents" in query:
            return {"public_key": self.public_key} if values["agent_id"] == self.agent_id else None
        if "FROM nonce_tracker" in query:
            return {"present": 1} if values["nonce"] in self.used_nonces else None
        return None

    async def fetch_all(self, query, values=None):
        if "FROM ap2_trusted_issuers" in query:
            return [{"issuer_did": d} for d in self.trusted]
        return []

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO x402_transactions" in query:
            self.tx.append(values)


def _chain(issuer_priv, issuer_did, agent_did, *, total="50.00", currency="USD",
           merchant="m_1"):
    # Real-time-relative: the route verifies the chain against the real clock
    # (it injects no `now`), so the validity window must bracket the actual now.
    now = datetime.now(timezone.utc)
    env = lambda: {
        "issuer": issuer_did, "subject": agent_did,
        "issued_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    intent = {**env(), "type": "IntentMandate", "id": "intent-1", "constraints": {
        "actions": ["create_payment"], "max_amount": "100.00",
        "currency": "USD", "merchants": ["m_1"]}}
    cart = {**env(), "type": "CartMandate", "id": "cart-1", "intent_ref": "intent-1",
            "total": total, "currency": currency, "merchant_id": merchant}
    payment = {**env(), "type": "PaymentMandate", "cart_ref": "cart-1",
               "amount": total, "currency": currency}
    return {
        "intent": intent, "intent_signature": _sign_obj(issuer_priv, intent),
        "cart": cart, "cart_signature": _sign_obj(issuer_priv, cart),
        "payment": payment, "payment_signature": _sign_obj(issuer_priv, payment),
    }


def _client(fake, monkeypatch):
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(database, "execute", fake.execute)
    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def _post(client, agent_priv, body):
    # The agent signs the exact canonical payload verify_ap2_signature checks.
    signed = build_ap2_signed_payload(body, NONCE)
    agent_sig = _es256(agent_priv, crypto_service.canonicalize_json(signed).encode())
    return client.post(
        "/ap2/transaction/initiate", json=body,
        headers={"X-Agent-Consent": CONSENT_TOKEN,
                 "X-AP2-Signature": agent_sig, "X-AP2-Nonce": NONCE},
    )


def test_initiate_with_valid_mandate_chain_succeeds(monkeypatch):
    agent_priv, agent_did = _keypair()
    issuer_priv, issuer_did = _keypair()
    fake = FakeDB(agent_did, {issuer_did})
    client = _client(fake, monkeypatch)

    body = {"merchant_id": "m_1", "amount": 50.0, "currency": "USD",
            "mandate_chain": _chain(issuer_priv, issuer_did, agent_did)}
    res = _post(client, agent_priv, body)
    assert res.status_code == 200, res.text
    assert len(fake.tx) == 1


def test_initiate_untrusted_issuer_rejected_403(monkeypatch):
    agent_priv, agent_did = _keypair()
    issuer_priv, issuer_did = _keypair()
    fake = FakeDB(agent_did, set())  # NO trusted issuers registered → deny
    client = _client(fake, monkeypatch)

    body = {"merchant_id": "m_1", "amount": 50.0, "currency": "USD",
            "mandate_chain": _chain(issuer_priv, issuer_did, agent_did)}
    res = _post(client, agent_priv, body)
    assert res.status_code == 403, res.text
    assert fake.tx == []


def test_initiate_chain_not_bound_to_transaction_rejected_403(monkeypatch):
    # Chain authorizes 40.00 but the transaction requests 50.00 → binding fails.
    agent_priv, agent_did = _keypair()
    issuer_priv, issuer_did = _keypair()
    fake = FakeDB(agent_did, {issuer_did})
    client = _client(fake, monkeypatch)

    body = {"merchant_id": "m_1", "amount": 50.0, "currency": "USD",
            "mandate_chain": _chain(issuer_priv, issuer_did, agent_did, total="40.00")}
    res = _post(client, agent_priv, body)
    assert res.status_code == 403, res.text
    assert fake.tx == []


def test_initiate_without_mandate_chain_unchanged(monkeypatch):
    # Backward compatibility: no mandate chain → consent-scope path, still works.
    agent_priv, agent_did = _keypair()
    fake = FakeDB(agent_did, set())
    client = _client(fake, monkeypatch)

    body = {"merchant_id": "m_1", "amount": 50.0, "currency": "USD"}
    res = _post(client, agent_priv, body)
    assert res.status_code == 200, res.text
    assert len(fake.tx) == 1


def test_initiate_non_did_agent_with_chain_rejected_403(monkeypatch):
    # A raw-PEM (non-DID) agent cannot use mandate authority — a mandate's subject
    # is a DID, so agent_did must resolve to one. Reaches the mandate block (the
    # request signature verifies against the PEM), then 403s on the DID guard.
    agent_priv, agent_pem = _pem_keypair()
    issuer_priv, issuer_did = _keypair()
    fake = FakeDB("did:key:zUnusedSubject", {issuer_did}, public_key=agent_pem)
    client = _client(fake, monkeypatch)

    body = {"merchant_id": "m_1", "amount": 50.0, "currency": "USD",
            "mandate_chain": _chain(issuer_priv, issuer_did, "did:key:zUnusedSubject")}
    res = _post(client, agent_priv, body)
    assert res.status_code == 403, res.text
    assert "DID-identity agent" in res.json()["detail"]
    assert fake.tx == []
