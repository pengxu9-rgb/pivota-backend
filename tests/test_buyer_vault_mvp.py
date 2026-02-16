"""
MVP coverage for Pivota Unified Buyer Account (Buyer Vault/Profile).

Focus: access controls + step-up + pairwise buyer_ref + agent scoping.
"""

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


# Allow running from repo root without manually setting PYTHONPATH.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# Ensure deterministic env for endpoints that require server-side headers.
os.environ.setdefault("CHECKOUT_UI_KEY", "test-checkout-ui-key")
os.environ.setdefault("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret")


@pytest.fixture
def app():
    from routes.agent_api import router as agent_router
    from routes.agent_checkout_intents import router as checkout_router
    from routes.buyer_api import router as buyer_router

    a = FastAPI()
    a.include_router(agent_router)
    a.include_router(checkout_router)
    a.include_router(buyer_router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def test_checkout_prefill_requires_checkout_ui_key(client):
    res = client.get("/agent/v1/checkout/prefill", headers={"X-API-Key": "test-agent-key"})
    assert res.status_code == 403
    body = res.json().get("detail") or {}
    assert body.get("error") == "FORBIDDEN"


def test_checkout_prefill_requires_checkout_token_even_with_ui_key(client):
    res = client.get(
        "/agent/v1/checkout/prefill",
        headers={
            "X-API-Key": "test-agent-key",
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 401
    body = res.json().get("detail") or {}
    assert body.get("error") == "UNAUTHENTICATED"


def test_save_from_checkout_triggers_step_up_when_not_logged_in(client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes import buyer_api

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        raise HTTPException(status_code=401, detail="not logged in")

    monkeypatch.setattr(buyer_api, "get_accounts_principal", fake_get_accounts_principal)

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(buyer_api.database, "execute", fake_execute)

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    res = client.post(
        "/buyer/v1/save_from_checkout",
        json={"intent_id": intent_id, "save_email": True, "save_address": True},
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 401
    assert "pivota_save_nonce=" in (res.headers.get("set-cookie") or "")
    detail = res.json().get("detail") or {}
    assert (detail.get("error") or {}).get("code") == "STEP_UP_REQUIRED"
    assert "save_token" in detail
    assert "login_url" in detail
    login_url = detail.get("login_url") or ""
    parsed = urlparse(login_url)
    redirect_encoded = (parse_qs(parsed.query).get("redirect") or [None])[0]
    assert redirect_encoded
    redirect_url = unquote(str(redirect_encoded))
    assert f"save_token={detail.get('save_token')}" in redirect_url
    assert f"checkout_token={token}" in redirect_url
    # Ensure we don't leak intent prefill fields on step-up response.
    assert "prefill" not in detail
    assert "customer_email" not in detail
    assert "shipping_address" not in detail


def test_save_from_checkout_without_checkout_token_uses_order_id_step_up(client, monkeypatch):
    from routes import buyer_api

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        raise HTTPException(status_code=401, detail="not logged in")

    monkeypatch.setattr(buyer_api, "get_accounts_principal", fake_get_accounts_principal)

    async def fake_fetch_one(_query, _values=None):  # noqa: ANN001
        return {
            "order_id": "ORD_TEST_NO_TOKEN",
            "agent_id": "agent_test",
            "intent_id": "ci_order_intent",
            "customer_email": "buyer@example.com",
            "shipping_address": {
                "name": "Buyer",
                "address_line1": "1 Main St",
                "city": "NYC",
                "postal_code": "10001",
                "country": "US",
            },
        }

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(buyer_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_api.database, "execute", fake_execute)

    res = client.post(
        "/buyer/v1/save_from_checkout",
        json={"order_id": "ORD_TEST_NO_TOKEN", "save_email": True, "save_address": True},
        headers={"X-Checkout-UI-Key": "test-checkout-ui-key"},
    )
    assert res.status_code == 401
    detail = res.json().get("detail") or {}
    assert (detail.get("error") or {}).get("code") == "STEP_UP_REQUIRED"
    assert "save_token" in detail
    assert "login_url" in detail
    login_url = detail.get("login_url") or ""
    parsed = urlparse(login_url)
    redirect_encoded = (parse_qs(parsed.query).get("redirect") or [None])[0]
    assert redirect_encoded
    redirect_url = unquote(str(redirect_encoded))
    assert f"save_token={detail.get('save_token')}" in redirect_url
    assert "checkout_token=" not in redirect_url


def test_save_from_checkout_without_token_and_order_id_rejected(client):
    res = client.post(
        "/buyer/v1/save_from_checkout",
        json={"save_email": True, "save_address": True},
        headers={"X-Checkout-UI-Key": "test-checkout-ui-key"},
    )
    assert res.status_code == 401
    detail = res.json().get("detail") or {}
    assert (detail.get("error") or {}).get("code") == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_pairwise_buyer_ref_is_per_agent_and_stable(monkeypatch):
    from routes import buyer_api

    minted = iter(["refA", "refB", "refC"])
    monkeypatch.setattr(buyer_api, "mint_pairwise_buyer_ref", lambda: next(minted))

    class StubDB:
        def __init__(self):
            self.links = {}

        async def fetch_one(self, query, values=None):  # noqa: ANN001
            if not isinstance(query, str):
                return None
            if "insert into buyer_agent_links" not in query.lower():
                return None
            params = values or {}
            buyer_id = str(params.get("buyer_id") or "")
            agent_id = str(params.get("agent_id") or "")
            ref = str(params.get("ref") or "")
            key = (buyer_id, agent_id)
            if key in self.links:
                return {"agent_scoped_buyer_ref": self.links[key]}
            self.links[key] = ref
            return {"agent_scoped_buyer_ref": ref}

    stub_db = StubDB()
    monkeypatch.setattr(buyer_api, "database", stub_db)

    ref_a1 = await buyer_api._get_or_create_pairwise_buyer_ref(buyer_id="buyer_1", agent_id="agent_a")
    ref_a2 = await buyer_api._get_or_create_pairwise_buyer_ref(buyer_id="buyer_1", agent_id="agent_a")
    ref_b = await buyer_api._get_or_create_pairwise_buyer_ref(buyer_id="buyer_1", agent_id="agent_b")

    assert ref_a1 == "refA"
    assert ref_a2 == "refA"
    assert ref_b == "refC"
    assert ref_a1 != ref_b


def test_save_from_checkout_redeem_requires_nonce_cookie(client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes import buyer_api

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    save_token = "sv_test_token"

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        return type("P", (), {"user_id": "buyer_1", "email": "buyer@example.com", "email_normalized": "buyer@example.com"})()

    monkeypatch.setattr(buyer_api, "get_accounts_principal", fake_get_accounts_principal)

    async def fake_fetch_one(_query, _values=None):  # noqa: ANN001
        return {
            "save_token_hash": hashlib.sha256(save_token.encode("utf-8")).hexdigest(),
            "intent_id": intent_id,
            "order_id": None,
            "checkout_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "client_nonce_hash": hashlib.sha256("nonce".encode("utf-8")).hexdigest(),
            "save_email": True,
            "save_address": True,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "redeemed_at": None,
        }

    monkeypatch.setattr(buyer_api.database, "fetch_one", fake_fetch_one)

    res = client.post(
        "/buyer/v1/save_from_checkout",
        json={"save_token": save_token},
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 403
    detail = res.json().get("detail") or {}
    assert (detail.get("error") or {}).get("code") == "FORBIDDEN"


def test_checkout_prefill_expired_intent_returns_gone(app, client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes.agent_auth import get_agent_context
    from routes import agent_checkout_intents

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    class Ctx:
        agent_id = "agent_test"
        checkout_token_payload = {"intent_id": intent_id}

    async def fake_get_agent_context(request: Request):  # noqa: ANN001
        return Ctx()

    app.dependency_overrides[get_agent_context] = fake_get_agent_context

    async def fake_fetch_one(_query, _values=None):  # noqa: ANN001
        return {
            "prefill": {"customer_email": "agent@example.com"},
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "used_at": None,
            "checkout_token_hash": agent_checkout_intents._sha256_hex(token),
            "prefill_read_count": 0,
        }

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(agent_checkout_intents.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_checkout_intents.database, "execute", fake_execute)

    res = client.get(
        "/agent/v1/checkout/prefill",
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 410


def test_checkout_prefill_used_intent_returns_gone(app, client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes.agent_auth import get_agent_context
    from routes import agent_checkout_intents

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    class Ctx:
        agent_id = "agent_test"
        checkout_token_payload = {"intent_id": intent_id}

    async def fake_get_agent_context(request: Request):  # noqa: ANN001
        return Ctx()

    app.dependency_overrides[get_agent_context] = fake_get_agent_context

    async def fake_fetch_one(_query, _values=None):  # noqa: ANN001
        return {
            "prefill": {"customer_email": "agent@example.com"},
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
            "used_at": datetime.now(timezone.utc),
            "checkout_token_hash": agent_checkout_intents._sha256_hex(token),
            "prefill_read_count": 0,
        }

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(agent_checkout_intents.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_checkout_intents.database, "execute", fake_execute)

    res = client.get(
        "/agent/v1/checkout/prefill",
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 410


def test_checkout_prefill_logged_in_prefers_buyer_vault(app, client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes.agent_auth import get_agent_context
    from routes import agent_checkout_intents

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    class Ctx:
        agent_id = "agent_test"
        checkout_token_payload = {"intent_id": intent_id}

    async def fake_get_agent_context(request: Request):  # noqa: ANN001
        return Ctx()

    app.dependency_overrides[get_agent_context] = fake_get_agent_context

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        return type("P", (), {"user_id": "buyer_1", "email": "buyer@example.com"})()

    monkeypatch.setattr(agent_checkout_intents, "get_accounts_principal", fake_get_accounts_principal)

    async def fake_fetch_one(query, values=None):  # noqa: ANN001
        if isinstance(query, str) and "from checkout_intents" in query.lower():
            return {
                "prefill": {
                    "customer_email": "agent@example.com",
                    "shipping_address": {"address_line1": "1 Agent St", "city": "NYC", "postal_code": "10001", "country": "US"},
                },
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
                "used_at": None,
                "checkout_token_hash": agent_checkout_intents._sha256_hex(token),
                "prefill_read_count": 0,
            }
        # buyer_addresses default
        if "buyer_addresses" in str(query):
            return {
                "id": "addr_1",
                "buyer_id": "buyer_1",
                "recipient_name": "Buyer Name",
                "line1": "9 Buyer Rd",
                "line2": None,
                "city": "SF",
                "region": "CA",
                "postal_code": "94105",
                "country": "US",
                "phone": "1234567890",
                "is_default": True,
            }
        return None

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(agent_checkout_intents.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_checkout_intents.database, "execute", fake_execute)

    res = client.get(
        "/agent/v1/checkout/prefill",
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 200
    prefill = (res.json() or {}).get("prefill") or {}
    assert prefill.get("customer_email") == "buyer@example.com"
    assert (prefill.get("shipping_address") or {}).get("address_line1") == "9 Buyer Rd"
    # Phone is minimized by default
    assert "phone" not in (prefill.get("shipping_address") or {})


def test_checkout_prefill_anonymous_uses_intent_prefill(app, client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes.agent_auth import get_agent_context
    from routes import agent_checkout_intents

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    class Ctx:
        agent_id = "agent_test"
        checkout_token_payload = {"intent_id": intent_id}

    async def fake_get_agent_context(request: Request):  # noqa: ANN001
        return Ctx()

    app.dependency_overrides[get_agent_context] = fake_get_agent_context

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        raise HTTPException(status_code=401, detail="not logged in")

    monkeypatch.setattr(agent_checkout_intents, "get_accounts_principal", fake_get_accounts_principal)

    async def fake_fetch_one(query, values=None):  # noqa: ANN001
        if isinstance(query, str) and "from checkout_intents" in query.lower():
            return {
                "prefill": {
                    "customer_email": "agent@example.com",
                    "shipping_address": {"address_line1": "1 Agent St", "city": "NYC", "postal_code": "10001", "country": "US", "phone": "555"},
                },
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
                "used_at": None,
                "checkout_token_hash": agent_checkout_intents._sha256_hex(token),
                "prefill_read_count": 0,
            }
        return None

    async def fake_execute(_query, _values=None):  # noqa: ANN001
        return 1

    monkeypatch.setattr(agent_checkout_intents.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_checkout_intents.database, "execute", fake_execute)

    res = client.get(
        "/agent/v1/checkout/prefill",
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 200
    prefill = (res.json() or {}).get("prefill") or {}
    assert prefill.get("customer_email") == "agent@example.com"
    assert (prefill.get("shipping_address") or {}).get("address_line1") == "1 Agent St"
    assert "phone" not in (prefill.get("shipping_address") or {})


def test_save_from_checkout_redeem_saves_address_into_vault(client, monkeypatch):
    from routes.agent_checkout_intents import mint_checkout_token
    from routes import buyer_api
    import secrets as secrets_module

    intent_id = "ci_test_intent"
    token = mint_checkout_token(
        {
            "agent_id": "agent_test",
            "intent_id": intent_id,
            "buyer_ref": "guest:123",
            "merchant_ids": ["merch_test"],
            "scopes": ["checkout"],
            "items": [],
        },
        ttl_seconds=3600,
    )

    save_token = "sv_test_token"
    nonce = "nonce123"
    client.cookies.set("pivota_save_nonce", nonce)

    async def fake_get_accounts_principal(_request):  # noqa: ANN001
        return type("P", (), {"user_id": "buyer_1", "email": "buyer@example.com", "email_normalized": "buyer@example.com"})()

    async def fake_pairwise_ref(*_args, **_kwargs):  # noqa: ANN001
        return "ref_pairwise"

    monkeypatch.setattr(buyer_api, "get_accounts_principal", fake_get_accounts_principal)
    monkeypatch.setattr(buyer_api, "_get_or_create_pairwise_buyer_ref", fake_pairwise_ref)
    monkeypatch.setattr(secrets_module, "token_hex", lambda _n: "a" * 24)

    async def fake_fetch_one(query, values=None):  # noqa: ANN001
        q = str(query)
        if "buyer_save_challenges" in q:
            return {
                "save_token_hash": hashlib.sha256(save_token.encode("utf-8")).hexdigest(),
                "intent_id": intent_id,
                "order_id": None,
                "checkout_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "client_nonce_hash": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                "save_email": True,
                "save_address": True,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                "redeemed_at": None,
            }
        if isinstance(query, str) and "select prefill from checkout_intents" in query.lower():
            return {
                "prefill": {
                    "customer_email": "agent@example.com",
                    "shipping_address": {
                        "name": "Buyer Name",
                        "address_line1": "9 Buyer Rd",
                        "city": "SF",
                        "postal_code": "94105",
                        "country": "US",
                    },
                }
            }
        if isinstance(query, str) and "select id from buyer_addresses" in query.lower():
            return None
        return None

    async def fake_execute(query, values=None):  # noqa: ANN001
        if hasattr(query, "table") and getattr(query.table, "name", None) == "buyer_save_challenges":
            return 1
        return 1

    monkeypatch.setattr(buyer_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(buyer_api.database, "execute", fake_execute)

    res = client.post(
        "/buyer/v1/save_from_checkout",
        json={"save_token": save_token},
        headers={
            "X-Checkout-Token": token,
            "X-Checkout-UI-Key": "test-checkout-ui-key",
        },
    )
    assert res.status_code == 200
    payload = res.json() or {}
    assert payload.get("status") == "ok"
    assert payload.get("agent_scoped_buyer_ref") == "ref_pairwise"
    assert payload.get("saved_address_id") == "addr_" + ("a" * 24)
    assert (payload.get("saved") or {}).get("address") is True



def test_agent_list_orders_is_agent_scoped(client, monkeypatch):
    from db import database as db_database

    async def fake_fetch_all(_query, params=None):  # noqa: ANN001
        assert (params or {}).get("agent_id") == "agent_test"
        return [
            {
                "order_id": "ord_1",
                "merchant_id": "merch_1",
                "status": "created",
                "payment_status": "unpaid",
                "total": 12.34,
                "created_at": "2026-01-26T00:00:00Z",
            }
        ]

    monkeypatch.setattr(db_database.database, "fetch_all", fake_fetch_all)

    res = client.get("/agent/v1/orders", headers={"X-API-Key": "test-agent-key"})
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "success"
    assert payload.get("total") == 1
    assert payload.get("orders")[0]["order_id"] == "ord_1"
