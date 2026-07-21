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
            "status": "active",
            "domain": "site_123",
            "api_key": "IST.test_key",
            "order_writeback_status": "enabled",
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
            if url == wix_adapter.WIX_ECOM_CREATE_ORDER_URL:
                captured.update({"url": url, "headers": headers, "json": json})
                return DummyResponse(201, {"id": "wix_order_123"})
            captured.update({"payment_url": url, "payment_headers": headers, "payment_json": json})
            return DummyResponse(200, {"paymentsIds": ["wix_payment_123"]})

        async def get(self, url, headers=None):
            captured.update({"get_url": url, "get_headers": headers})
            return DummyResponse(
                200,
                {"order": {"id": "wix_order_123", "number": "1001", "status": "APPROVED"}},
            )

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["order_id"] == "wix_order_123"
    assert result["status"] == "created"
    assert result["raw_response"]["number"] == "1001"
    assert result["raw_response"]["add_payment_response"] == {"paymentsIds": ["wix_payment_123"]}
    assert captured["url"] == wix_adapter.WIX_ECOM_CREATE_ORDER_URL
    assert "stores/v2/orders" not in captured["url"]
    assert captured["headers"]["Authorization"] == "IST.test_key"
    assert captured["headers"]["wix-site-id"] == "site_123"
    order = captured["json"]["order"]
    assert order["lineItems"][0]["name"] == "Wix Test Product"
    assert order["lineItems"][0]["quantity"] == 2
    assert order["lineItems"][0]["taxInfo"] == {
        "taxAmount": {"amount": "0.00"},
        "taxableAmount": {"amount": "25.00"},
        "taxRate": "0",
        "taxIncludedInPrice": False,
        "taxBreakdown": [],
    }
    assert order["lineItems"][0]["catalogReference"] == {
        "appId": wix_adapter.WIX_STORES_APP_ID,
        "catalogItemId": "prod_wix_1",
        "options": {"variantId": "var_wix_1"},
    }
    assert order["lineItems"][0]["physicalProperties"] == {
        "sku": "SKU-WIX-1",
        "shippable": True,
    }
    assert order["shippingInfo"]["logistics"]["shippingDestination"]["address"]["city"] == "Austin"
    assert order["shippingInfo"]["logistics"]["shippingDestination"]["contactDetails"]["firstName"] == "Wix"
    assert order["shippingInfo"]["cost"]["price"]["amount"] == "4.00"
    assert order["recipientInfo"] == order["shippingInfo"]["logistics"]["shippingDestination"]
    assert order["billingInfo"]["contactDetails"]["lastName"] == "Buyer"
    assert order["paymentStatus"] == "NOT_PAID"
    assert "fulfillmentStatus" not in order
    assert "paymentMethod" not in order
    assert captured["json"]["settings"] == {
        "orderApprovalStrategy": "PAYMENT_RECEIVED",
        "notifications": {
            "sendNotificationToBuyer": False,
            "sendNotificationsToBusiness": False,
        },
    }
    assert captured["payment_url"] == (
        wix_adapter.WIX_ECOM_ADD_PAYMENT_URL_TEMPLATE.format(order_id="wix_order_123")
    )
    assert captured["payment_json"]["orderId"] == "wix_order_123"
    assert captured["payment_json"]["payments"][0]["amount"]["amount"] == "31.00"
    assert captured["payment_json"]["payments"][0]["regularPaymentDetails"]["offlinePayment"] is True
    assert captured["payment_json"]["payments"][0]["regularPaymentDetails"]["status"] == "APPROVED"
    assert captured["get_url"] == wix_adapter.WIX_ECOM_ORDER_URL_TEMPLATE.format(order_id="wix_order_123")


@pytest.mark.asyncio
async def test_create_wix_order_polls_until_wix_display_number_is_assigned(monkeypatch):
    from adapters import wix_adapter

    captured: Dict[str, Any] = {"get_count": 0}

    async def no_sleep(_delay):
        return None

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            if url == wix_adapter.WIX_ECOM_CREATE_ORDER_URL:
                return DummyResponse(201, {"id": "wix_order_delayed"})
            return DummyResponse(200, {"paymentsIds": ["wix_payment_delayed"]})

        async def get(self, url, headers=None):
            captured["get_count"] += 1
            if captured["get_count"] == 1:
                return DummyResponse(200, {"order": {"id": "wix_order_delayed", "number": "0"}})
            return DummyResponse(200, {"order": {"id": "wix_order_delayed", "number": "1004"}})

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)
    monkeypatch.setattr(wix_adapter.asyncio, "sleep", no_sleep)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["order_id"] == "wix_order_delayed"
    assert result["raw_response"]["number"] == "1004"
    assert captured["get_count"] == 2


@pytest.mark.asyncio
async def test_create_wix_order_fails_closed_when_wix_auto_fulfills_physical_order(monkeypatch):
    from adapters import wix_adapter

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            if url == wix_adapter.WIX_ECOM_CREATE_ORDER_URL:
                return DummyResponse(201, {"id": "wix_order_auto_fulfilled"})
            return DummyResponse(200, {"paymentsIds": ["wix_payment_auto_fulfilled"]})

        async def get(self, url, headers=None):
            return DummyResponse(
                200,
                {
                    "order": {
                        "id": "wix_order_auto_fulfilled",
                        "number": "1005",
                        "paymentStatus": "PAID",
                        "fulfillmentStatus": "FULFILLED",
                        "lineItems": [
                            {
                                "id": "line_1",
                                "itemType": {"preset": "PHYSICAL"},
                                "catalogReference": {
                                    "catalogItemId": "prod_wix_1",
                                    "options": {"variantId": "var_wix_1"},
                                },
                            }
                        ],
                    }
                },
            )

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["order_id"] is None
    assert result["status"] == "error"
    assert result["error"] == "wix_physical_order_auto_fulfilled"
    assert result["retryable"] is False
    assert result["raw_response"]["platform_order_id"] == "wix_order_auto_fulfilled"
    assert result["raw_response"]["number"] == "1005"
    assert result["raw_response"]["observed_fulfillment_status"] == "FULFILLED"
    assert result["raw_response"]["request_physical_line_items"] == 1
    assert result["raw_response"]["observed_physical_line_items"] == 1


@pytest.mark.asyncio
async def test_create_wix_order_uses_bearer_only_for_explicit_oauth(monkeypatch):
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
            if url == wix_adapter.WIX_ECOM_CREATE_ORDER_URL:
                captured.update({"url": url, "headers": headers, "json": json})
                return DummyResponse(201, {"order": {"id": "wix_order_oauth"}})
            captured.update({"payment_url": url, "payment_headers": headers, "payment_json": json})
            return DummyResponse(200, {"paymentsIds": ["pay_oauth"]})

        async def get(self, url, headers=None):
            captured.update({"get_url": url, "get_headers": headers})
            return DummyResponse(200, {"order": {"id": "wix_order_oauth", "number": "1002"}})

    order = _wix_order()
    order["store"] = {
        "store_id": "store_wix_1",
        "platform": "wix",
        "status": "active",
        "domain": "site_123",
        "order_writeback_status": "enabled",
        "api_credentials": {
            "auth_mode": "oauth",
            "access_token": "token_123",
            "site_id": "site_123",
        },
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["order_id"] == "wix_order_oauth"
    assert captured["headers"]["Authorization"] == "Bearer token_123"
    assert captured["payment_headers"]["Authorization"] == "Bearer token_123"


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
    order["store"] = {
        "store_id": "store_wix_1",
        "platform": "wix",
        "status": "active",
        "domain": "site_123",
        "order_writeback_status": "enabled",
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["order_id"] is None
    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
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
        "status": "active",
        "order_writeback_status": "enabled",
        # Explicit OAuth token present but site_id MISSING — should be rejected.
        "api_credentials": {"auth_mode": "oauth", "access_token": "tok_partial_only"},
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
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
        "status": "active",
        "order_writeback_status": "enabled",
        "api_credentials": {"site_id": "site_partial_only"},
    }
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
    assert result["retryable"] is False
    raw = result.get("raw_response") or {}
    assert "api_key" in (raw.get("missing_fields") or [])


@pytest.mark.asyncio
async def test_create_wix_order_store_disabled_fails_closed(monkeypatch):
    from adapters import wix_adapter

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called when store writeback is disabled")

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    order = _wix_order()
    order["store"]["order_writeback_status"] = "disabled"
    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
    assert result["retryable"] is False
    assert result["raw_response"]["readiness"]["blocker"] == "order_writeback_disabled"


@pytest.mark.asyncio
async def test_create_wix_order_global_kill_switch_fails_closed(monkeypatch):
    from adapters import wix_adapter

    monkeypatch.setenv("DISABLE_PLATFORM_ORDER_WRITEBACK", "true")

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called while global writeback kill switch is on")

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", _wix_order())

    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
    assert result["raw_response"]["readiness"]["blocker"] == "global_order_writeback_disabled"


@pytest.mark.asyncio
async def test_create_wix_order_respects_canary_order_scope(monkeypatch):
    from adapters import wix_adapter

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called outside canary scope")

    order = _wix_order()
    order["store"]["order_writeback_status"] = "canary"
    order["store"]["order_writeback_canary_order_id"] = "ORD_ALLOWED"
    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_order_writeback_not_ready"
    assert result["raw_response"]["readiness"]["blocker"] == "canary_order_mismatch"


@pytest.mark.asyncio
async def test_create_wix_order_allows_matching_canary_order(monkeypatch):
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
            if url == wix_adapter.WIX_ECOM_CREATE_ORDER_URL:
                captured.update({"url": url, "headers": headers, "json": json})
                return DummyResponse(201, {"id": "wix_order_123"})
            captured.update({"payment_url": url, "payment_json": json})
            return DummyResponse(200, {"paymentsIds": ["pay_canary"]})

        async def get(self, url, headers=None):
            captured.update({"get_url": url, "get_headers": headers})
            return DummyResponse(200, {"order": {"id": "wix_order_123", "number": "1003"}})

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", DummyAsyncClient)

    order = _wix_order()
    order["store"]["order_writeback_status"] = "canary"
    order["store"]["order_writeback_canary_order_id"] = "ORD_WIX_1"
    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["order_id"] == "wix_order_123"
    assert captured["url"] == wix_adapter.WIX_ECOM_CREATE_ORDER_URL
    assert captured["payment_url"] == wix_adapter.WIX_ECOM_ADD_PAYMENT_URL_TEMPLATE.format(order_id="wix_order_123")


@pytest.mark.asyncio
async def test_create_wix_order_missing_catalog_mapping_fails_before_network(monkeypatch):
    from adapters import wix_adapter

    order = _wix_order()
    order["items"][0].pop("product_id")
    order["items"][0].pop("platform_product_id", None)
    order["items"][0].pop("wix_product_id", None)

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network should not be called without a Wix catalog item id")

    monkeypatch.setattr(wix_adapter.httpx, "AsyncClient", FailingAsyncClient)

    result = await wix_adapter.create_wix_order("merch_wix", order)

    assert result["status"] == "error"
    assert result["error"] == "wix_order_payload_invalid"
    assert "lineItems[0].catalogReference.catalogItemId" in result["raw_response"]["blockers"]


def test_build_wix_order_payload_populates_required_wix_fields_from_order():
    from adapters.wix_adapter import build_wix_order_payload

    payload = build_wix_order_payload(_wix_order())

    order = payload["order"]
    assert order["lineItems"][0]["catalogReference"]["catalogItemId"] == "prod_wix_1"
    assert order["lineItems"][0]["catalogReference"]["options"]["variantId"] == "var_wix_1"
    assert order["lineItems"][0]["selectedOptions"] == [{"option": "Size", "selection": "M"}]
    assert order["lineItems"][0]["physicalProperties"]["shippable"] is True
    assert order["lineItems"][0]["physicalProperties"]["sku"] == "SKU-WIX-1"
    assert order["shippingInfo"]["logistics"]["shippingDestination"]["address"]["postalCode"] == "78701"
    assert order["shippingInfo"]["logistics"]["shippingDestination"]["contactDetails"]["phone"] == "555-0100"
    assert order["buyerInfo"]["email"] == "buyer@example.com"
    assert order["billingInfo"]["contactDetails"]["firstName"] == "Wix"
    assert order["paymentStatus"] == "NOT_PAID"
    assert "paymentMethod" not in order
    assert "fulfillmentStatus" not in order
    assert order["taxIncludedInPrices"] is False
    assert order["lineItems"][0]["taxInfo"]["taxAmount"]["amount"] == "0.00"
    assert order["lineItems"][0]["taxInfo"]["taxableAmount"]["amount"] == "25.00"
    assert order["priceSummary"]["total"]["amount"] == "31.00"
    assert payload["settings"]["orderApprovalStrategy"] == "PAYMENT_RECEIVED"


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
                "status": "active",
                "domain": "site_123",
                "api_key": "IST.test_key",
                "order_writeback_status": "enabled",
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
    assert order_events[1]["event_type"] == "wix_order_created"


@pytest.mark.asyncio
async def test_sync_order_to_connected_store_records_wix_auto_fulfilled_failure_metadata(monkeypatch):
    from routes import order_routes

    order_events: list[Dict[str, Any]] = []
    sync_failures: list[Dict[str, Any]] = []
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
                "status": "active",
                "domain": "site_123",
                "api_key": "IST.test_key",
                "order_writeback_status": "enabled",
            }
        ]

    async def fake_create_wix_order_via_adapter(merchant_id: str, order_dict: Dict[str, Any]):
        return {
            "order_id": None,
            "status": "error",
            "error": "wix_physical_order_auto_fulfilled",
            "retryable": False,
            "raw_response": {
                "platform_order_id": "wix_order_auto_fulfilled",
                "number": "1005",
                "observed_fulfillment_status": "FULFILLED",
                "request_physical_line_items": 1,
                "observed_physical_line_items": 1,
            },
        }

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)

    async def fake_mark_failed(**kwargs):
        sync_failures.append(kwargs)

    @asynccontextmanager
    async def fake_lock(*, lock_key: int):
        yield True

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "create_wix_order_via_adapter", fake_create_wix_order_via_adapter)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes, "_mark_merchant_order_sync_failed_best_effort", fake_mark_failed)

    ok = await order_routes.sync_order_to_connected_store("ORD_WIX_1")

    assert ok is False
    assert [event["event_type"] for event in order_events] == [
        "merchant_order_failed",
        "wix_order_writeback_failed",
    ]
    failed_metadata = order_events[0]["metadata"]
    assert failed_metadata["error"] == "wix_physical_order_auto_fulfilled"
    assert failed_metadata["retryable"] is False
    assert failed_metadata["platform_order_id"] == "wix_order_auto_fulfilled"
    assert failed_metadata["number"] == "1005"
    assert failed_metadata["observed_fulfillment_status"] == "FULFILLED"
    assert failed_metadata["request_physical_line_items"] == 1
    assert failed_metadata["observed_physical_line_items"] == 1
    assert sync_failures[0]["platform"] == "wix"


@pytest.mark.asyncio
async def test_sync_order_to_connected_store_wix_store_disabled_does_not_call_adapter(monkeypatch):
    from routes import order_routes

    order = _wix_order()
    order.pop("store")
    order["store_id"] = "store_wix_1"
    order_events: list[Dict[str, Any]] = []
    sync_failures: list[Dict[str, Any]] = []

    async def fake_get_order(order_id: str):
        return dict(order)

    async def fake_get_store_by_id(store_id: str, *, merchant_id: str):
        return {
            "store_id": store_id,
            "platform": "wix",
            "status": "active",
            "domain": "site_123",
            "api_key": "IST.test_key",
            "order_writeback_status": "disabled",
        }

    async def fake_get_merchant_active_stores(merchant_id: str):
        return [await fake_get_store_by_id("store_wix_1", merchant_id=merchant_id)]

    async def fail_adapter(*args, **kwargs):
        raise AssertionError("adapter should not be called while store writeback is disabled")

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)

    async def fake_mark_failed(**kwargs):
        sync_failures.append(kwargs)

    @asynccontextmanager
    async def fake_lock(*, lock_key: int):
        yield True

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_store_by_id", fake_get_store_by_id)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "create_wix_order_via_adapter", fail_adapter)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes, "_mark_merchant_order_sync_failed_best_effort", fake_mark_failed)

    ok = await order_routes.sync_order_to_connected_store("ORD_WIX_1")

    assert ok is False
    assert [event["event_type"] for event in order_events] == [
        "wix_order_writeback_skipped",
        "merchant_order_failed",
        "wix_order_writeback_not_ready",
    ]
    assert order_events[0]["metadata"]["readiness"]["blocker"] == "order_writeback_disabled"
    assert sync_failures[0]["platform"] == "wix"
    assert sync_failures[0]["error"] == "wix_order_writeback_not_ready"
    assert sync_failures[0]["retryable"] is False


@pytest.mark.asyncio
async def test_sync_order_to_connected_store_wix_canary_scope_does_not_call_adapter(monkeypatch):
    from routes import order_routes

    order = _wix_order()
    order.pop("store")
    order["store_id"] = "store_wix_1"
    order_events: list[Dict[str, Any]] = []
    sync_failures: list[Dict[str, Any]] = []

    async def fake_get_order(order_id: str):
        return dict(order)

    async def fake_get_store_by_id(store_id: str, *, merchant_id: str):
        return {
            "store_id": store_id,
            "platform": "wix",
            "status": "active",
            "domain": "site_123",
            "api_key": "IST.test_key",
            "order_writeback_status": "canary",
            "order_writeback_canary_order_id": "ORD_ALLOWED",
        }

    async def fake_get_merchant_active_stores(merchant_id: str):
        return [await fake_get_store_by_id("store_wix_1", merchant_id=merchant_id)]

    async def fail_adapter(*args, **kwargs):
        raise AssertionError("adapter should not be called outside canary scope")

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)

    async def fake_mark_failed(**kwargs):
        sync_failures.append(kwargs)

    @asynccontextmanager
    async def fake_lock(*, lock_key: int):
        yield True

    monkeypatch.setattr(order_routes, "_pg_advisory_lock_best_effort", fake_lock)
    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_store_by_id", fake_get_store_by_id)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_merchant_active_stores)
    monkeypatch.setattr(order_routes, "create_wix_order_via_adapter", fail_adapter)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes, "_mark_merchant_order_sync_failed_best_effort", fake_mark_failed)

    ok = await order_routes.sync_order_to_connected_store("ORD_WIX_1")

    assert ok is False
    assert order_events[0]["event_type"] == "wix_order_writeback_skipped"
    assert order_events[0]["metadata"]["readiness"]["blocker"] == "canary_order_mismatch"
    assert sync_failures[0]["error"] == "wix_order_writeback_not_ready"
