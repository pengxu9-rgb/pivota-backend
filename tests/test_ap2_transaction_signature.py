"""
The mutating AP2 transaction routes must enforce a per-request signature via
Depends(verify_ap2_signature) — not just a bearer consent token. A stolen/leaked
consent token alone must not be able to initiate or confirm a transaction, and a
bad signature must not consume the nonce.
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


AGENT_ID = "agent_txn_test"
CONSENT_TOKEN = "consent_txn_token"
WALLET_ADDRESS = "0xWALLET"


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pub


def _sign(priv, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return base64.b64encode(
        priv.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    ).decode("utf-8")


class FakeDB:
    def __init__(self, agents, consents, agent_wallets=None):
        self.agents = agents
        self.consents = consents
        # (agent_id, address) pairs that are registered + active for the agent.
        self.agent_wallets = set(agent_wallets or set())
        self.used_nonces = set()
        self.tx_inserts = []
        self.tx_updates = []

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            return self.consents.get(values["consent_id"])
        if "FROM agents" in query:
            aid = values["agent_id"]
            return {"public_key": self.agents[aid]} if aid in self.agents else None
        if "FROM agent_wallets" in query:
            # WalletService.verify_agent_wallet ownership + active-status check.
            key = (values.get("agent_id"), values.get("address"))
            return {"wallet_id": "w_1"} if key in self.agent_wallets else None
        if "FROM nonce_tracker" in query:
            return {"present": 1} if values["nonce"] in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO x402_transactions" in query:
            self.tx_inserts.append(values)
        elif "UPDATE x402_transactions" in query:
            self.tx_updates.append(values)


def _active_consent():
    return {
        "consent_id": CONSENT_TOKEN,
        "agent_id": AGENT_ID,
        "scope": json.dumps({"actions": ["read", "create_payment"]}),
        "status": "active",
        "expires_at": datetime.utcnow() + timedelta(hours=1),
    }


@pytest.fixture
def keypair():
    return _keypair()


@pytest.fixture
def fake_db(keypair, monkeypatch):
    _, pub = keypair
    fake = FakeDB(
        agents={AGENT_ID: pub},
        consents={CONSENT_TOKEN: _active_consent()},
        agent_wallets={(AGENT_ID, WALLET_ADDRESS)},
    )
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


def _headers(signature, nonce, wallet=None):
    h = {
        "X-Agent-Consent": CONSENT_TOKEN,
        "X-AP2-Signature": signature,
        "X-AP2-Nonce": nonce,
    }
    if wallet is not None:
        h["X-Wallet-Address"] = wallet
    return h


# ---- initiate --------------------------------------------------------------

def test_initiate_valid_signature_creates_transaction(client, fake_db, keypair):
    priv, _ = keypair
    body = {"merchant_id": "m_1", "amount": 25, "currency": "USD"}
    signature = _sign(priv, {**body, "nonce": "txn-001"})

    res = client.post("/ap2/transaction/initiate", json=body,
                      headers=_headers(signature, "txn-001"))

    assert res.status_code == 200
    assert res.json()["status"] == "pending"
    assert len(fake_db.tx_inserts) == 1
    assert fake_db.tx_inserts[0]["agent_id"] == AGENT_ID
    assert "txn-001" in fake_db.used_nonces


def test_initiate_bad_signature_rejected_and_no_side_effects(client, fake_db, keypair):
    body = {"merchant_id": "m_1", "amount": 25, "currency": "USD"}
    # A consent token alone must NOT be enough — garbage signature is rejected.
    res = client.post("/ap2/transaction/initiate", json=body,
                      headers=_headers(base64.b64encode(b"garbage").decode(), "txn-002"))

    assert res.status_code == 401
    assert fake_db.tx_inserts == []          # no transaction written
    assert "txn-002" not in fake_db.used_nonces  # bad sig did NOT burn the nonce


def test_initiate_missing_signature_rejected_401(client, fake_db):
    body = {"merchant_id": "m_1", "amount": 25, "currency": "USD"}
    res = client.post(
        "/ap2/transaction/initiate",
        json=body,
        headers={"X-Agent-Consent": CONSENT_TOKEN, "X-AP2-Nonce": "txn-003"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Missing request signature"
    assert fake_db.tx_inserts == []


def test_initiate_replayed_nonce_rejected_409(client, fake_db, keypair):
    priv, _ = keypair
    fake_db.used_nonces.add("txn-used")
    body = {"merchant_id": "m_1", "amount": 25, "currency": "USD"}
    signature = _sign(priv, {**body, "nonce": "txn-used"})

    res = client.post("/ap2/transaction/initiate", json=body,
                      headers=_headers(signature, "txn-used"))

    assert res.status_code == 409
    assert fake_db.tx_inserts == []


# ---- confirm ---------------------------------------------------------------

def test_confirm_valid_signature_passes_guard_and_completes(client, fake_db, keypair):
    priv, _ = keypair
    # A valid signature passes the Depends(verify_ap2_signature) guard, and the
    # confirming wallet (registered + active in fake_db) is authorized by the
    # real WalletService.verify_agent_wallet — so the transaction completes.
    body = {"transaction_id": "ap2_txn_x"}
    signature = _sign(priv, {**body, "nonce": "cf-001"})

    res = client.post("/ap2/transaction/confirm", json=body,
                      headers=_headers(signature, "cf-001", wallet=WALLET_ADDRESS))

    assert res.status_code == 200
    assert res.json()["status"] == "completed"
    assert len(fake_db.tx_updates) == 1
    assert "cf-001" in fake_db.used_nonces


def test_confirm_valid_signature_unregistered_wallet_rejected_403(client, fake_db, keypair):
    priv, _ = keypair
    # Signature is valid (passes the guard), but the wallet is NOT registered to
    # this agent, so the real verify_agent_wallet rejects it: 403, no update.
    body = {"transaction_id": "ap2_txn_x"}
    signature = _sign(priv, {**body, "nonce": "cf-003"})

    res = client.post("/ap2/transaction/confirm", json=body,
                      headers=_headers(signature, "cf-003", wallet="0xNOTMINE"))

    assert res.status_code == 403
    assert fake_db.tx_updates == []


def test_confirm_bad_signature_rejected_no_update(client, fake_db):
    # A bad signature is rejected by the guard BEFORE any wallet / DB work, so no
    # wallet_service stub is needed here.
    body = {"transaction_id": "ap2_txn_x"}
    res = client.post(
        "/ap2/transaction/confirm", json=body,
        headers=_headers(base64.b64encode(b"garbage").decode(), "cf-002", wallet="0xWALLET"),
    )

    assert res.status_code == 401
    assert fake_db.tx_updates == []
    assert "cf-002" not in fake_db.used_nonces
