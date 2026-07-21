from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict

import pytest


class DummyResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_create_woocommerce_order_persists_platform_link(monkeypatch):
    import httpx

    from routes import order_routes

    requests: list[Dict[str, Any]] = []
    fulfillment_updates: list[Dict[str, Any]] = []
    order_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    order_row = {
        "order_id": "ORD_WOO_1",
        "merchant_id": "merch_woo",
        "store_id": "store_woo_1",
        "payment_status": "paid",
        "customer_email": "buyer@example.com",
        "customer_name": "Woo Buyer",
        "shipping_address": {
            "name": "Woo Buyer",
            "address_line1": "1 Main St",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        },
        "items": [
            {
                "product_id": "10",
                "variant_id": "101",
                "quantity": 2,
                "unit_price": "12.50",
            }
        ],
        "metadata": {},
    }

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_WOO_1"
        return dict(order_row)

    async def fake_get_merchant_active_stores(merchant_id: str):
        assert merchant_id == "merch_woo"
        return [
            {
                "store_id": "store_woo_1",
                "platform": "woocommerce",
                "domain": "shop.example.com",
                "api_credentials": {
                    "consumer_key": "ck_test",
                    "consumer_secret": "cs_test",
                },
            }
        ]

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

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, params=None, json=None, **_kwargs):
            requests.append({"url": url, "params": params, "json": json})
            return DummyResponse(201, {"id": 1234, "number": "1001"})

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    ok = await order_routes.create_woocommerce_order("ORD_WOO_1")

    assert ok is True
    assert requests[0]["url"] == "https://shop.example.com/wp-json/wc/v3/orders"
    assert requests[0]["params"] == {
        "consumer_key": "ck_test",
        "consumer_secret": "cs_test",
    }
    assert requests[0]["json"]["line_items"] == [
        {
            "product_id": 10,
            "quantity": 2,
            "variation_id": 101,
            "subtotal": "25.00",
            "total": "25.00",
        }
    ]
    assert fulfillment_updates == [
        {"order_id": "ORD_WOO_1", "fulfillment_status": "processing"}
    ]
    merchant_order = order_updates[0]["update_data"]["metadata"]["merchant_order"]
    assert merchant_order["platform"] == "woocommerce"
    assert merchant_order["platform_order_id"] == "1234"
    assert merchant_order["platform_order_url"] == "https://shop.example.com/wp-admin/post.php?post=1234&action=edit"
    assert order_events[0]["event_type"] == "merchant_order_created"
    assert order_events[0]["metadata"]["platform"] == "woocommerce"


@pytest.mark.asyncio
async def test_create_shopify_order_dispatches_to_connected_platform(monkeypatch):
    from routes import order_routes

    calls: list[str] = []

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_ROUTE_1"
        return {
            "order_id": order_id,
            "merchant_id": "merch_route",
            "payment_status": "paid",
            "metadata": {},
        }

    async def fake_get_primary_store(merchant_id: str):
        assert merchant_id == "merch_route"
        return {"platform": "woocommerce"}

    async def fake_create_woocommerce_order(order_id: str):
        calls.append(order_id)
        return True

    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes, "create_woocommerce_order", fake_create_woocommerce_order)

    ok = await order_routes.create_shopify_order("ORD_ROUTE_1")

    assert ok is True
    assert calls == ["ORD_ROUTE_1"]


@pytest.mark.asyncio
async def test_create_bigcommerce_order_persists_platform_link(monkeypatch):
    import httpx

    from routes import order_routes

    requests: list[Dict[str, Any]] = []
    fulfillment_updates: list[Dict[str, Any]] = []
    order_updates: list[Dict[str, Any]] = []
    order_events: list[Dict[str, Any]] = []

    order_row = {
        "order_id": "ORD_BIG_1",
        "merchant_id": "merch_big",
        "store_id": "store_big_1",
        "payment_status": "paid",
        "payment_intent_id": "pi_big_1",
        "customer_email": "buyer@example.com",
        "customer_name": "Big Buyer",
        "shipping_address": {
            "name": "Big Buyer",
            "address_line1": "1 Commerce St",
            "city": "Austin",
            "state": "TX",
            "postal_code": "78701",
            "country": "US",
        },
        "items": [
            {
                "product_id": "77",
                "variant_id": "701",
                "quantity": 1,
                "unit_price": "21.00",
            }
        ],
        "metadata": {},
    }

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_BIG_1"
        return dict(order_row)

    async def fake_get_merchant_active_stores(merchant_id: str):
        assert merchant_id == "merch_big"
        return [
            {
                "store_id": "store_big_1",
                "platform": "bigcommerce",
                "domain": "abc123.mybigcommerce.com",
                "api_credentials": {
                    "store_hash": "abc123",
                    "access_token": "token_1",
                    "client_id": "client_1",
                },
            }
        ]

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

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, timeout=None):
            requests.append({"method": "GET", "url": url, "headers": headers})
            if url.endswith("/v2/order_statuses"):
                return DummyResponse(200, [{"id": 11, "name": "Awaiting Fulfillment"}])
            if url.endswith("/v3/catalog/products/77/variants/701"):
                return DummyResponse(
                    200,
                    {
                        "data": {
                            "option_values": [
                                {
                                    "option_id": 5,
                                    "id": 9,
                                }
                            ]
                        }
                    },
                )
            raise AssertionError(f"Unexpected GET {url}")

        async def post(self, url, headers=None, json=None, **_kwargs):
            requests.append({"method": "POST", "url": url, "headers": headers, "json": json})
            return DummyResponse(201, {"id": 889})

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    ok = await order_routes.create_bigcommerce_order("ORD_BIG_1")

    assert ok is True
    post_request = next(item for item in requests if item["method"] == "POST")
    assert post_request["url"] == "https://api.bigcommerce.com/stores/abc123/v2/orders"
    assert post_request["headers"]["X-Auth-Token"] == "token_1"
    assert post_request["headers"]["X-Auth-Client"] == "client_1"
    assert post_request["json"]["status_id"] == 11
    assert post_request["json"]["products"] == [
        {
            "product_id": 77,
            "quantity": 1,
            "product_options": [{"id": 5, "value": 9}],
        }
    ]
    assert fulfillment_updates == [
        {"order_id": "ORD_BIG_1", "fulfillment_status": "processing"}
    ]
    merchant_order = order_updates[0]["update_data"]["metadata"]["merchant_order"]
    assert merchant_order["platform"] == "bigcommerce"
    assert merchant_order["platform_order_id"] == "889"
    assert merchant_order["domain"] == "abc123.mybigcommerce.com"
    assert order_events[0]["event_type"] == "merchant_order_created"
    assert order_events[0]["metadata"]["platform"] == "bigcommerce"
