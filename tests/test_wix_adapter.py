from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Dict

import pytest


os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


class DummyResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _wix_order() -> Dict[str, Any]:
    return {
        "order_id": "ORD_WIX_1",
        "merchant_id": "merch_wix",
        "payment_status": "paid",
        "payment_intent_id": "pi_wix_1",
        "customer_email": "buyer@example.com",
        "customer_name": "Wix Buyer",
        "shipping_address": {
            "name": "Wix Buyer",
            "address_line1": "1 Commerce St",
            "address_line2": "Unit 2",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
            "phone": "555-0100",
        },
        "items": [
            {
                "product_id": "prod_wix_1",
                "variant_id": "var_wix_1",
                "quantity": 2,
                "unit_price": "12.50",
                "product_title": "Wix Test Product",
                "sku": "SKU-WIX-1",
                "options": {"Size": "M"},
            }
        ],
        "subtotal": "25.00",
        "shipping_fee": "4.00",
        "tax": "2.00",
        "total": "31.00",
        "currency": "USD",
        "metadata": {},
        "store": {
            "store_id": "store_wix_1",
            "platform": "wix",
            "domain": "site_123",
            "api_credentials": {
                "access_token": "token_123",
                "site_id": "site_123",
            },
        },
    }


@pytest.mark.asyncio
async def test_create_wix_order_posts_payload_and_returns_order_id(monkeypatch):
    from adapters import wix_adapter

    captured: Dict[str, Any] = {}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update({"url": url, "headers": headers, "json": json})
            return DummyResponse(201, {"id": "wix_order_123", "number": "1001"})

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result == {
        "order_id": "wix_order_123",
        "status": "created",
        "raw_response": {"id": "wix_order_123", "number": "1001"},
    }
    assert captured["url"] == wix_adapter.WIX_STORES_CREATE_ORDER_URL
    assert captured["headers"]["Authorization"] == "Bearer token_123"
    assert captured["headers"]["wix-site-id"] == "site_123"
    assert captured["json"]["lineItems"][0]["name"] == "Wix Test Product"
    assert captured["json"]["lineItems"][0]["quantity"] == 2
    assert captured["json"]["shippingInfo"]["shipmentDetails"]["address"]["city"] == "Austin"
    assert captured["json"]["billingInfo"]["paymentMethod"] == "Pivota External Payment"


@pytest.mark.asyncio
async def test_create_wix_order_auth_failure_returns_error_shape(monkeypatch):
    from adapters import wix_adapter

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return DummyResponse(401, {"message": "bad token"})

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["order_id"] is None
    assert result["status"] == "error"
    assert result["error"] == "wix_auth_failed"
    assert result["status_code"] == 401
    assert result["raw_response"] == {"message": "bad token"}


@pytest.mark.asyncio
async def test_create_wix_order_network_error_returns_error_shape(monkeypatch):
    from adapters import wix_adapter

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise wix_adapter.httpx.ConnectError("dns failure")

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["order_id"] is None
    assert result["status"] == "error"
    assert result["error"] == "wix_network_error"
    assert result["retryable"] is True


@pytest.mark.asyncio
async def test_create_wix_order_missing_credentials_returns_not_configured(monkeypatch):
    from adapters import wix_adapter

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called without credentials")

    order = _wix_order()
    order["store"] = {"store_id": "store_wix_1", "platform": "wix", "domain": "site_123"}
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["order_id"] is None
    assert result["status"] == "error"
    assert result["error"] == "wix_credentials_not_configured"
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_create_wix_order_partial_credentials_token_only_returns_not_configured(monkeypatch):
    """Per the codex code review of PR #491: a Wix call with an
    access_token but NO site_id was previously accepted and would
    fail upstream with a 4xx (wasting a paid attempt). Strict-pair
    validation rejects this BEFORE the network call."""
    from adapters import wix_adapter

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called without site_id")

    order = _wix_order()
    order["store"] = {
        "store_id": "store_wix_1",
        "platform": "wix",
        # access_token present but site_id MISSING — should be rejected.
        "api_credentials": {"access_token": "tok_partial_only"},
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_credentials_not_configured"
    assert result["retryable"] is False
    raw = result.get("raw_response") or {}
    assert "site_id" in (raw.get("missing_fields") or []), (
        "Adapter must enumerate which credential fields are missing "
        "so the operator can fix the right one — token-only must "
        "surface 'site_id' in the missing_fields list."
    )


@pytest.mark.asyncio
async def test_create_wix_order_partial_credentials_site_only_returns_not_configured(monkeypatch):
    """Mirror of the token-only case: a Wix call with site_id but
    NO access_token must also reject before network call."""
    from adapters import wix_adapter

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called without access_token")

    order = _wix_order()
    order["store"] = {
        "store_id": "store_wix_1",
        "platform": "wix",
        "api_credentials": {"site_id": "site_partial_only"},
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_credentials_not_configured"
    assert result["retryable"] is False
    raw = result.get("raw_response") or {}
    assert "access_token" in (raw.get("missing_fields") or [])


def test_build_wix_order_payload_populates_required_wix_fields_from_order():
    from adapters.wix_adapter import build_wix_order_payload

    payload = build_wix_order_payload(_wix_order())

    assert payload["lineItems"][0]["productId"] == "prod_wix_1"
    assert payload["lineItems"][0]["variantId"] == "var_wix_1"
    assert payload["lineItems"][0]["options"] == [{"option": "Size", "selection": "M"}]
    assert payload["shippingInfo"]["shipmentDetails"]["address"]["zipCode"] == "78701"
    assert payload["billingInfo"]["email"] == "buyer@example.com"
    assert payload["billingInfo"]["paymentProviderTransactionId"] == "pi_wix_1"
    assert payload["billingInfo"]["paymentMethod"] == "Pivota External Payment"
    assert payload["paymentMethod"] == "Pivota External Payment"
    assert payload["paymentStatus"] == "PAID"


@pytest.mark.asyncio
async def test_sync_order_to_connected_store_routes_to_wix_adapter(monkeypatch):
    from routes import order_routes

    adapter_calls: list[Dict[str, Any]] = []
    fulfillment_updates: list[Dict[str, Any]] = []
    order_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []
    order = _wix_order()
    order.pop("store")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_WIX_1"
        return dict(order)

    async def fake_get_primary_store(merchant_id: str):
        assert merchant_id == "merch_wix"
        return {"store_id": "store_wix_1", "platform": "wix", "domain": "site_123"}

    async def fake_get_merchant_active_stores(merchant_id: str):
        assert merchant_id == "merch_wix"
        return [
            {
                "store_id": "store_wix_1",
                "platform": "wix",
                "domain": "site_123",
                "api_credentials": {
                    "access_token": "token_123",
                    "site_id": "site_123",
                },
            }
        ]

    async def fake_create_wix_order_via_adapter(merchant_id: str, order_dict: Dict[str, Any]):
        adapter_calls.append({"merchant_id": merchant_id, "order": dict(order_dict)})
        return {"order_id": "wix_order_123", "status": "created", "raw_response": {"number": "1001"}}

    async def fake_update_fulfillment_info(*, order_id: str, fulfillment_status: str, **_kwargs):
        fulfillment_updates.append({"order_id": order_id, "fulfillment_status": fulfillment_status})
        return True

    async def fake_update_order_row(order_id: str, update_data: Dict[str, Any]):
        order_updates.append({"order_id": order_id, "update_data": dict(update_data)})
        return True

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)
        return None

    @asynccontextmanager
    async def fake_lock(*, lock_key: int):
        assert isinstance(lock_key, int)
        yield True

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "create_wix_order_via_adapter", fake_create_wix_order_via_adapter)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)

    ok = await order_routes.sync_order_to_connected_store("ORD_WIX_1")

    assert ok is True
    assert adapter_calls[0]["merchant_id"] == "merch_wix"
    assert adapter_calls[0]["order"]["store"]["store_id"] == "store_wix_1"
    assert fulfillment_updates == [
        {"order_id": "ORD_WIX_1", "fulfillment_status": "processing"}
    ]
    merchant_order = order_updates[0]["update_data"]["metadata"]["merchant_order"]
    assert merchant_order["platform"] == "wix"
    assert merchant_order["platform_order_id"] == "wix_order_123"
    assert merchant_order["platform_order_name"] == "1001"
    assert order_events[0]["event_type"] == "merchant_order_created"
    assert order_events[0]["metadata"]["platform"] == "wix"
