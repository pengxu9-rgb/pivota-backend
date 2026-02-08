import os

import pytest


# `db.database` requires a PostgreSQL DATABASE_URL at import time.
# Use a dummy local URL so unit tests can import modules without a real DB.
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


@pytest.mark.asyncio
async def test_require_admin_or_key_accepts_admin_key(monkeypatch):
    from utils.auth import require_admin_or_key

    monkeypatch.setenv("ADMIN_API_KEY", "test_admin_key")
    user = await require_admin_or_key(credentials=None, x_admin_key="test_admin_key")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_create_shopify_order_falls_back_to_other_store_on_401(monkeypatch):
    import httpx

    from routes import order_routes

    order_id = "ORD_TEST_1"

    async def fake_get_order(_order_id: str):
        assert _order_id == order_id
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "store_id": "store_1",
            "payment_status": "paid",
            "shopify_order_id": None,
            "customer_email": "buyer@example.com",
            "customer_name": "Buyer",
            "shipping_address": {
                "name": "Buyer",
                "address_line1": "1 Main St",
                "address_line2": "",
                "city": "New Orleans",
                "state": "LA",
                "postal_code": "70118",
                "country": "US",
                "phone": None,
            },
            "items": [
                {
                    "product_id": "p_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": 9.99,
                }
            ],
            "total": 9.99,
            "currency": "USD",
            "payment_intent_id": "pi_123",
            "psp_used": "stripe",
        }

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id, "psp_type": "stripe"}

    async def fake_get_active_stores(_merchant_id: str):
        return [
            {
                "store_id": "store_1",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "bad_token",
                "api_key": "bad_token",
                "status": "active",
                "source": "merchant_stores",
            },
            {
                "store_id": "store_2",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            },
        ]

    updated_store_id = {}

    async def fake_update_order(_order_id: str, update_data):
        updated_store_id["order_id"] = _order_id
        updated_store_id["update_data"] = dict(update_data)
        return True

    async def fake_update_fulfillment_info(**_kwargs):
        return True

    async def fake_log_order_event(*_args, **_kwargs):
        return None

    async def fake_shopify_admin_graphql(**_kwargs):
        return {"orders": {"edges": []}}

    async def fake_ensure_external_payment_transaction_best_effort(**_kwargs):
        return {"ok": True}

    calls = []

    class DummyResponse:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b"{}"

        def json(self):
            return self._payload

    async def fake_post(self, url, **kwargs):
        token = (kwargs.get("headers") or {}).get("X-Shopify-Access-Token")
        calls.append({"url": url, "token": token, "json": kwargs.get("json")})
        if token == "bad_token":
            return DummyResponse(401, payload={"errors": "invalid token"}, text="401")
        if token == "good_token":
            return DummyResponse(201, payload={"order": {"id": 999}}, text="201")
        return DummyResponse(500, payload={"errors": "unexpected"}, text="500")

    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_active_stores)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        order_routes,
        "ensure_external_payment_transaction_best_effort",
        fake_ensure_external_payment_transaction_best_effort,
    )

    import db.orders as orders_db

    monkeypatch.setattr(orders_db, "update_order", fake_update_order)

    import services.shopify_graphql_client as gql

    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

    ok = await order_routes.create_shopify_order(order_id)
    assert ok is True

    assert len(calls) >= 2
    assert calls[0]["token"] == "bad_token"
    assert calls[1]["token"] == "good_token"

    payload = calls[0]["json"] or {}
    tags = ((payload.get("order") or {}).get("tags") or "")
    assert f"pivota_order_id:{order_id}" in tags

    assert updated_store_id["order_id"] == order_id
    assert updated_store_id["update_data"]["store_id"] == "store_2"


@pytest.mark.asyncio
async def test_create_shopify_order_returns_true_when_lock_not_acquired(monkeypatch):
    from routes import order_routes

    called = {"get_order": 0, "released": 0}

    async def fake_try_acquire(_order_id: str):
        assert _order_id == "ORD_LOCKED"
        return False, 123

    async def fake_release(_lock_key, *, lock_acquired: bool):
        called["released"] += 1
        assert lock_acquired is False

    async def fake_get_order(_order_id: str):
        called["get_order"] += 1
        return None

    monkeypatch.setattr(order_routes, "_try_acquire_shopify_order_lock", fake_try_acquire)
    monkeypatch.setattr(order_routes, "_release_shopify_order_lock", fake_release)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)

    ok = await order_routes.create_shopify_order("ORD_LOCKED")
    assert ok is True
    assert called["get_order"] == 0
    assert called["released"] == 1
