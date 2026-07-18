"""
GET /ap2/wallet/balance — the consent-only tier. Consent is the primary auth, so
a bogus/expired token must fail closed as 401 (not an uncaught 500), and the
balance is scoped to the verified agent + owned wallet.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

AGENT_ID = "agent_wallet"
CONSENT_TOKEN = "consent_wallet"
WALLET = "0xWALLET"


class FakeDB:
    def __init__(self, *, valid_consent=True, wallet=True):
        self.valid_consent = valid_consent
        self.wallet = wallet

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            if not self.valid_consent:
                return None  # verify_consent -> ValueError -> 401
            return {
                "consent_id": CONSENT_TOKEN, "agent_id": AGENT_ID,
                "scope": json.dumps({"actions": ["read"]}),
                "status": "active",
                "expires_at": datetime.utcnow() + timedelta(hours=1),
            }
        if "FROM agent_wallets" in query:
            if not self.wallet:
                return None
            return {"wallet_id": "w_1", "balance": 100, "currency": "USD",
                    "status": "active", "last_updated": None}
        return None

    async def execute(self, query, values=None):
        return None


def _client(fake, monkeypatch):
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)
    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    return TestClient(app)


def _get(client, **headers):
    return client.get("/ap2/wallet/balance", headers=headers)


def test_valid_consent_returns_balance(monkeypatch):
    client = _client(FakeDB(), monkeypatch)
    res = _get(client, **{"X-Agent-Consent": CONSENT_TOKEN, "X-Wallet-Address": WALLET})
    assert res.status_code == 200, res.text
    assert res.json()["wallet_id"] == "w_1"


def test_bogus_consent_rejected_401(monkeypatch):
    # The fix: an invalid consent fails closed as 401, not 500.
    client = _client(FakeDB(valid_consent=False), monkeypatch)
    res = _get(client, **{"X-Agent-Consent": "nope", "X-Wallet-Address": WALLET})
    assert res.status_code == 401, res.text


def test_missing_wallet_address_rejected_400(monkeypatch):
    client = _client(FakeDB(), monkeypatch)
    res = _get(client, **{"X-Agent-Consent": CONSENT_TOKEN})
    assert res.status_code == 400


def test_wallet_not_owned_404(monkeypatch):
    client = _client(FakeDB(wallet=False), monkeypatch)
    res = _get(client, **{"X-Agent-Consent": CONSENT_TOKEN, "X-Wallet-Address": WALLET})
    assert res.status_code == 404
