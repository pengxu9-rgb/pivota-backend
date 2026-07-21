import pytest


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

    ok = await order_routes._create_shopify_order_impl(order_id)
    assert ok is True

    assert len(calls) >= 2
    assert calls[0]["token"] == "bad_token"
    assert calls[1]["token"] == "good_token"

    payload = calls[0]["json"] or {}
    tags = ((payload.get("order") or {}).get("tags") or "")
    assert f"pivota-order-id-{order_id.replace('_', '-')}" in tags
    assert "pivota_order_id:" not in tags

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

    ok = await order_routes._create_shopify_order_impl("ORD_LOCKED")
    assert ok is True
    assert called["get_order"] == 0
    assert called["released"] == 1


@pytest.mark.asyncio
async def test_create_shopify_order_fail_closed_uses_authoritative_post_sync_payload(monkeypatch):
    import httpx

    from routes import order_routes

    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    order_id = "ORD_FAIL_CLOSED_OK"
    captured_events = []
    fulfillment_updates = []
    transaction_sync_calls = []

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
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
                "phone": None,
            },
            "items": [
                {
                    "product_id": "p_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": 1.69,
                }
            ],
            "total": 9.09,
            "currency": "USD",
            "payment_intent_id": "pi_123",
            "psp_used": "stripe",
            "metadata": {
                "pricing_quote": {
                    "quote_id": "q_1",
                    "pricing": {
                        "subtotal": "1.69",
                        "discount_total": "0.60",
                        "shipping_fee": "8.00",
                        "tax": "0.00",
                        "total": "9.09",
                    },
                    "promotion_lines": [
                        {
                            "source": "shopify",
                            "method": "code",
                            "code": "FIX60",
                            "amount": "-0.60",
                        }
                    ],
                    "discount_evidence": {
                        "pricing_confidence": "authoritative",
                        "codes": [{"code": "FIX60", "applicable": True, "source": "shopify_storefront_cart"}],
                        "applications": [
                            {
                                "source": "shopify",
                                "method": "code",
                                "discount_class": "product",
                                "code": "FIX60",
                                "amount": "-0.60",
                            }
                        ],
                    },
                }
            },
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
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            }
        ]

    async def fake_update_fulfillment_info(**kwargs):
        fulfillment_updates.append(dict(kwargs))
        return True

    async def fake_log_order_event(*_args, **kwargs):
        captured_events.append(dict(kwargs))
        return None

    async def fake_shopify_admin_graphql(**_kwargs):
        return {"orders": {"edges": []}}

    async def fake_ensure_external_payment_transaction_best_effort(**kwargs):
        transaction_sync_calls.append(dict(kwargs))
        return {"ok": True, "created": False, "transaction_id": 777}

    async def fake_fetch_shopify_order_reconciliation_payload_best_effort(**kwargs):
        assert kwargs["shopify_order_id"] == "999"
        return {
            "id": 999,
            "total_price": "9.09",
            "total_discounts": "0.60",
            "transactions": [{"kind": "sale", "status": "success", "amount": "9.09"}],
        }

    class DummyResponse:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b"{}"

        def json(self):
            return self._payload

    async def fake_post(self, url, **kwargs):
        return DummyResponse(201, payload={"order": {"id": 999}}, text="201")

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
    monkeypatch.setattr(
        order_routes,
        "_fetch_shopify_order_reconciliation_payload_best_effort",
        fake_fetch_shopify_order_reconciliation_payload_best_effort,
    )

    import services.shopify_graphql_client as gql

    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

    ok = await order_routes._create_shopify_order_impl(order_id)

    assert ok is True
    assert transaction_sync_calls
    assert fulfillment_updates
    reconciliation_event = next(e for e in captured_events if e.get("event_type") == "shopify_discount_reconciliation")
    assert reconciliation_event["metadata"]["status"] == "passed"
    assert reconciliation_event["metadata"]["passed"] is True
    assert reconciliation_event["metadata"]["observed"]["transaction_total"] == "9.09"
    assert reconciliation_event["metadata"]["transaction_sync"]["transaction_id"] == 777


@pytest.mark.asyncio
async def test_create_shopify_order_cancels_orphan_when_fail_closed_reconciliation_blocks_link(monkeypatch):
    import httpx

    from routes import order_routes

    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    order_id = "ORD_FAIL_CLOSED_ORPHAN"
    captured_events = []
    metadata_updates = []
    cancel_calls = []
    fulfillment_updates = []

    base_order = {
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
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": None,
        },
        "items": [
            {
                "product_id": "p_1",
                "variant_id": "123",
                "product_title": "Test Product",
                "quantity": 1,
                "unit_price": 1.69,
            }
        ],
        "total": 9.09,
        "currency": "USD",
        "payment_intent_id": "pi_123",
        "psp_used": "stripe",
        "metadata": {
            "pricing_quote": {
                "quote_id": "q_1",
                "pricing": {
                    "subtotal": "1.69",
                    "discount_total": "0.60",
                    "shipping_fee": "8.00",
                    "tax": "0.00",
                    "total": "9.09",
                },
                "promotion_lines": [
                    {
                        "source": "shopify",
                        "method": "code",
                        "code": "FIX60",
                        "amount": "-0.60",
                    }
                ],
                "discount_evidence": {
                    "pricing_confidence": "authoritative",
                    "codes": [{"code": "FIX60", "applicable": True, "source": "shopify_storefront_cart"}],
                    "applications": [
                        {
                            "source": "shopify",
                            "method": "code",
                            "discount_class": "product",
                            "code": "FIX60",
                            "amount": "-0.60",
                        }
                    ],
                },
            }
        },
    }

    async def fake_get_order(_order_id: str):
        assert _order_id == order_id
        return dict(base_order)

    async def fake_get_active_stores(_merchant_id: str):
        return [
            {
                "store_id": "store_1",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            }
        ]

    async def fake_update_fulfillment_info(**kwargs):
        fulfillment_updates.append(dict(kwargs))
        return True

    async def fake_log_order_event(*_args, **kwargs):
        captured_events.append(dict(kwargs))
        return None

    async def fake_update_order_row(_order_id: str, update_data):
        metadata_updates.append(dict(update_data.get("metadata") or {}))
        return True

    async def fake_shopify_admin_graphql(**_kwargs):
        return {"orders": {"edges": []}}

    async def fake_ensure_external_payment_transaction_best_effort(**_kwargs):
        return {"ok": True, "created": False, "transaction_id": 777}

    async def fake_fetch_shopify_order_reconciliation_payload_best_effort(**_kwargs):
        return {
            "id": 999,
            "total_price": "9.09",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "9.09"}],
        }

    async def fake_annotate_shopify_order_best_effort(**kwargs):
        return {
            "ok": True,
            "shopify_order_id": kwargs["shopify_order_id"],
            "tags": kwargs.get("tags") or [],
        }

    class DummyResponse:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b"{}"

        def json(self):
            return self._payload

    async def fake_post(self, url, **kwargs):
        if url.endswith("/orders.json"):
            return DummyResponse(201, payload={"order": {"id": 999}}, text="201")
        if url.endswith("/orders/999/cancel.json"):
            cancel_calls.append(dict(kwargs.get("json") or {}))
            return DummyResponse(200, payload={"order": {"id": 999}}, text="200")
        raise AssertionError(f"Unexpected Shopify POST URL: {url}")

    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_active_stores)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(order_routes, "annotate_shopify_order_best_effort", fake_annotate_shopify_order_best_effort)
    monkeypatch.setattr(
        order_routes,
        "ensure_external_payment_transaction_best_effort",
        fake_ensure_external_payment_transaction_best_effort,
    )
    monkeypatch.setattr(
        order_routes,
        "_fetch_shopify_order_reconciliation_payload_best_effort",
        fake_fetch_shopify_order_reconciliation_payload_best_effort,
    )

    import services.shopify_graphql_client as gql

    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

    ok = await order_routes._create_shopify_order_impl(order_id)

    assert ok is False
    assert fulfillment_updates == []
    assert cancel_calls == [{"reason": "other", "email": False, "refund": False, "restock": True}]
    orphan_meta = next(m["shopify_orphan_order"] for m in metadata_updates if "shopify_orphan_order" in m)
    assert orphan_meta["shopify_order_id"] == "999"
    assert orphan_meta["recovery_status"] == "cancelled_without_shopify_refund"
    assert orphan_meta["reconciliation"]["mismatches"] == ["shopify_discount_total"]
    assert any(e.get("event_type") == "shopify_orphan_order_cancelled" for e in captured_events)
    reconciliation_event = next(e for e in captured_events if e.get("event_type") == "shopify_discount_reconciliation")
    assert reconciliation_event["metadata"]["passed"] is False


@pytest.mark.asyncio
async def test_create_shopify_order_suppresses_receipt_for_unrenderable_quote_discounts(monkeypatch):
    import httpx

    from routes import order_routes

    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe")
    order_id = "ORD_SUPPRESS_RECEIPT"
    captured_payloads = []
    captured_events = []

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
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
                "phone": None,
            },
            "items": [
                {
                    "product_id": "p_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": 1.09,
                }
            ],
            "subtotal": 1.69,
            "discount_total": 8.60,
            "shipping_fee": 0.00,
            "tax": 0.00,
            "total": 1.09,
            "currency": "USD",
            "payment_intent_id": "CNV_ADYEN_OK",
            "psp_used": "adyen",
            "metadata": {
                "pricing_quote": {
                    "quote_id": "q_combo",
                    "pricing": {
                        "subtotal": "1.69",
                        "discount_total": "8.60",
                        "shipping_fee": "0.00",
                        "tax": "0.00",
                        "total": "1.09",
                    },
                    "line_items": [
                        {
                            "product_id": "p_1",
                            "variant_id": "123",
                            "quantity": 1,
                            "unit_price_original": "1.69",
                            "unit_price_effective": "1.09",
                            "line_discount_total": "0.60",
                        }
                    ],
                    "discount_evidence": {
                        "pricing_confidence": "authoritative",
                        "codes": [
                            {
                                "code": "FIX60",
                                "applicable": True,
                                "source": "shopify_storefront_cart",
                            }
                        ],
                        "applications": [
                            {
                                "source": "shopify",
                                "method": "code",
                                "discount_class": "product",
                                "code": "FIX60",
                                "amount": "-0.60",
                            },
                            {
                                "source": "shopify",
                                "method": "automatic",
                                "discount_class": "shipping",
                                "code": None,
                                "amount": "-8.00",
                            },
                        ],
                    },
                }
            },
        }

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id, "psp_type": "adyen"}

    async def fake_get_active_stores(_merchant_id: str):
        return [
            {
                "store_id": "store_1",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            }
        ]

    async def fake_update_fulfillment_info(**_kwargs):
        return True

    async def fake_log_order_event(*_args, **kwargs):
        captured_events.append(dict(kwargs))
        return None

    async def fake_shopify_admin_graphql(**_kwargs):
        return {"orders": {"edges": []}}

    async def fake_ensure_external_payment_transaction_best_effort(**_kwargs):
        return {"ok": True, "created": False, "transaction_id": 777}

    async def fake_fetch_shopify_order_reconciliation_payload_best_effort(**_kwargs):
        return {
            "id": 999,
            "total_price": "1.69",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "1.09"}],
        }

    class DummyResponse:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b"{}"

        def json(self):
            return self._payload

    async def fake_post(self, url, **kwargs):
        captured_payloads.append(dict(kwargs.get("json") or {}))
        return DummyResponse(201, payload={"order": {"id": 999}}, text="201")

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
    monkeypatch.setattr(
        order_routes,
        "_fetch_shopify_order_reconciliation_payload_best_effort",
        fake_fetch_shopify_order_reconciliation_payload_best_effort,
    )

    import services.shopify_graphql_client as gql

    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

    ok = await order_routes._create_shopify_order_impl(order_id)

    assert ok is True
    assert captured_payloads
    order_payload = captured_payloads[0]["order"]
    assert order_payload["send_receipt"] is False
    assert order_payload["send_fulfillment_receipt"] is False
    suppressed_event = next(e for e in captured_events if e.get("event_type") == "shopify_receipt_suppressed")
    assert "product_level_discount" in suppressed_event["metadata"]["blockers"]
    assert "automatic_discount" in suppressed_event["metadata"]["blockers"]


@pytest.mark.asyncio
async def test_create_shopify_order_uses_custom_line_items_and_shipping_lines_for_quote_snapshot(monkeypatch):
    import httpx

    from routes import order_routes

    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe")
    order_id = "ORD_CUSTOM_LINE_ITEMS"
    captured_payloads = []
    captured_events = []

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
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
                "phone": None,
            },
            "items": [
                {
                    "product_id": "p_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": 1.09,
                }
            ],
            "subtotal": 1.69,
            "discount_total": 0.60,
            "shipping_fee": 0.00,
            "tax": 0.00,
            "total": 1.09,
            "currency": "USD",
            "payment_intent_id": "CNV_ADYEN_OK",
            "psp_used": "adyen",
            "metadata": {
                "pricing_quote": {
                    "quote_id": "q_combo_encodable",
                    "pricing": {
                        "subtotal": "1.69",
                        "discount_total": "0.60",
                        "shipping_fee": "0.00",
                        "tax": "0.00",
                        "total": "1.09",
                    },
                    "line_items": [
                        {
                            "product_id": "p_1",
                            "variant_id": "123",
                            "quantity": 1,
                            "unit_price_original": "1.69",
                            "unit_price_effective": "1.09",
                            "line_discount_total": "0.60",
                        }
                    ],
                    "discount_evidence": {
                        "pricing_confidence": "authoritative",
                        "shipping_evidence": {
                            "status": "authoritative",
                            "selected_delivery_option_title": "Free Shipping",
                        },
                        "codes": [
                            {
                                "code": "FIX60",
                                "applicable": True,
                                "source": "shopify_storefront_cart",
                            }
                        ],
                        "applications": [
                            {
                                "source": "shopify",
                                "method": "code",
                                "discount_class": "product",
                                "code": "FIX60",
                                "amount": "-0.60",
                            },
                            {
                                "source": "shopify",
                                "method": "automatic",
                                "discount_class": "shipping",
                                "code": None,
                                "amount": "-8.00",
                            },
                        ],
                    },
                }
            },
        }

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id, "psp_type": "adyen"}

    async def fake_get_active_stores(_merchant_id: str):
        return [
            {
                "store_id": "store_1",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            }
        ]

    async def fake_update_fulfillment_info(**_kwargs):
        return True

    async def fake_log_order_event(*_args, **kwargs):
        captured_events.append(dict(kwargs))
        return None

    async def fake_shopify_admin_graphql(**_kwargs):
        return {"orders": {"edges": []}}

    async def fake_ensure_external_payment_transaction_best_effort(**_kwargs):
        return {"ok": True, "created": False, "transaction_id": 777}

    async def fake_fetch_shopify_order_reconciliation_payload_best_effort(**_kwargs):
        return {
            "id": 999,
            "total_price": "1.09",
            "total_discounts": "0.60",
            "transactions": [{"kind": "sale", "status": "success", "amount": "1.09"}],
        }

    class DummyResponse:
        def __init__(self, status_code: int, payload=None, text: str = ""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b"{}"

        def json(self):
            return self._payload

    async def fake_post(self, url, **kwargs):
        captured_payloads.append(dict(kwargs.get("json") or {}))
        return DummyResponse(201, payload={"order": {"id": 999}}, text="201")

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
    monkeypatch.setattr(
        order_routes,
        "_fetch_shopify_order_reconciliation_payload_best_effort",
        fake_fetch_shopify_order_reconciliation_payload_best_effort,
    )

    import services.shopify_graphql_client as gql

    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)

    ok = await order_routes._create_shopify_order_impl(order_id)

    assert ok is True
    assert captured_payloads
    order_payload = captured_payloads[0]["order"]
    assert order_payload["send_receipt"] is False
    assert order_payload["send_fulfillment_receipt"] is False
    assert order_payload["shipping_lines"][0]["price"] == "0.00"
    assert order_payload["shipping_lines"][0]["title"] == "Free Shipping"
    assert order_payload["line_items"][0]["variant_id"] == 123
    assert order_payload["line_items"][0]["price"] == "1.69"
    assert order_payload["line_items"][0]["total_discount"] == "0.60"
    assert "discount_codes" not in order_payload
    assert "pivota-custom-line-items" not in order_payload["tags"]
    suppressed_event = next(e for e in captured_events if e.get("event_type") == "shopify_receipt_suppressed")
    assert "product_level_discount" in (suppressed_event.get("metadata") or {}).get("blockers", [])


@pytest.mark.asyncio
async def test_create_shopify_order_uses_draft_order_path_for_complex_quote_when_enabled(monkeypatch):
    from routes import order_routes

    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe")
    monkeypatch.setenv("SHOPIFY_DRAFT_ORDER_QUOTE_SYNC_ENABLED", "1")
    order_id = "ORD_DRAFT_ORDER"
    captured_events = []
    graphql_calls = []

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
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
                "phone": None,
            },
            "items": [
                {
                    "product_id": "p_1",
                    "variant_id": "123",
                    "product_title": "Test Product",
                    "quantity": 1,
                    "unit_price": 1.09,
                }
            ],
            "subtotal": 1.69,
            "discount_total": 0.60,
            "shipping_fee": 0.00,
            "tax": 0.00,
            "total": 1.09,
            "currency": "USD",
            "payment_intent_id": "CNV_ADYEN_OK",
            "psp_used": "adyen",
            "metadata": {
                "amounts_source": "quote_snapshot",
                "pricing_quote": {
                    "quote_id": "q_combo",
                    "pricing": {
                        "subtotal": "1.69",
                        "discount_total": "0.60",
                        "shipping_fee": "0.00",
                        "tax": "0.00",
                        "total": "1.09",
                    },
                    "line_items": [
                        {
                            "product_id": "p_1",
                            "variant_id": "123",
                            "quantity": 1,
                            "unit_price_original": "1.69",
                            "unit_price_effective": "1.09",
                            "line_discount_total": "0.60",
                        }
                    ],
                    "discount_evidence": {
                        "pricing_confidence": "authoritative",
                        "shipping_evidence": {
                            "status": "authoritative",
                            "selected_delivery_option_title": "Free Shipping",
                        },
                        "codes": [{"code": "FIX60", "applicable": True, "source": "shopify_storefront_cart"}],
                        "applications": [
                            {
                                "source": "shopify",
                                "method": "code",
                                "discount_class": "product",
                                "code": "FIX60",
                                "amount": "-0.60",
                            },
                            {
                                "source": "shopify",
                                "method": "automatic",
                                "discount_class": "shipping",
                                "code": None,
                                "amount": "-8.00",
                            },
                        ],
                    },
                },
            },
        }

    async def fake_get_active_stores(_merchant_id: str):
        return [
            {
                "store_id": "store_1",
                "merchant_id": _merchant_id,
                "platform": "shopify",
                "domain": "shop.myshopify.com",
                "api_key_raw": "good_token",
                "api_key": "good_token",
                "status": "active",
                "source": "merchant_stores",
            }
        ]

    async def fake_log_order_event(*_args, **kwargs):
        captured_events.append(dict(kwargs))
        return None

    async def fake_update_fulfillment_info(**_kwargs):
        return True

    async def fake_update_order(_order_id: str, _update_data):
        return True

    async def fake_ensure_external_payment_transaction_best_effort(**_kwargs):
        return {"ok": True, "created": False, "transaction_id": 777}

    async def fake_fetch_shopify_order_reconciliation_payload_best_effort(**_kwargs):
        return {
            "id": 999,
            "total_price": "1.09",
            "total_discounts": "0.60",
            "transactions": [{"kind": "sale", "status": "success", "amount": "1.09"}],
        }

    async def fake_shopify_admin_graphql(**kwargs):
        graphql_calls.append(kwargs)
        query = kwargs.get("query") or ""
        if "orders(first: 1" in query:
            return {"orders": {"edges": []}}
        if "draftOrderCreate" in query:
            return {"draftOrderCreate": {"draftOrder": {"id": "gid://shopify/DraftOrder/111"}, "userErrors": []}}
        if "draftOrderComplete" in query:
            return {
                "draftOrderComplete": {
                    "draftOrder": {
                        "id": "gid://shopify/DraftOrder/111",
                        "order": {"id": "gid://shopify/Order/999", "legacyResourceId": "999", "name": "#1001"},
                    },
                    "userErrors": [],
                }
            }
        return {"orders": {"edges": []}}

    async def fail_post(*_args, **_kwargs):
        raise AssertionError("REST orders.json path should not run for draft-order canary")

    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "get_merchant_active_stores", fake_get_active_stores)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(order_routes, "update_fulfillment_info", fake_update_fulfillment_info)
    monkeypatch.setattr(order_routes, "ensure_external_payment_transaction_best_effort", fake_ensure_external_payment_transaction_best_effort)
    monkeypatch.setattr(order_routes, "_fetch_shopify_order_reconciliation_payload_best_effort", fake_fetch_shopify_order_reconciliation_payload_best_effort)

    import db.orders as orders_db
    monkeypatch.setattr(orders_db, "update_order", fake_update_order)

    import services.shopify_graphql_client as gql
    monkeypatch.setattr(gql, "shopify_admin_graphql", fake_shopify_admin_graphql)

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post, raising=True)

    ok = await order_routes._create_shopify_order_impl(order_id)

    assert ok is True
    assert any("draftOrderCreate" in (call.get("query") or "") for call in graphql_calls)
    assert any("draftOrderComplete" in (call.get("query") or "") for call in graphql_calls)
    reconciliation_event = next(e for e in captured_events if e.get("event_type") == "shopify_discount_reconciliation")
    assert reconciliation_event["metadata"]["shopify_write_strategy"] == order_routes.SHOPIFY_WRITE_STRATEGY_DRAFT_ORDER_QUOTE
    assert reconciliation_event["metadata"]["receipt_policy"] == order_routes.SHOPIFY_RECEIPT_POLICY_DRAFT_SUPPRESSED


@pytest.mark.asyncio
async def test_log_shopify_receipt_suppressed_once_dedupes(monkeypatch):
    from routes import order_routes

    captured = []
    seen = {"count": 0}

    class DummyDB:
        async def fetch_val(self, *_args, **_kwargs):
            seen["count"] += 1
            return 1 if seen["count"] > 1 else None

    async def fake_log_order_event(**kwargs):
        captured.append(dict(kwargs))
        return None

    monkeypatch.setattr(order_routes, "database", DummyDB())
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)

    await order_routes._log_shopify_receipt_suppressed_once(
        order_id="ORD_1",
        merchant_id="merch_1",
        total_amount=1.09,
        currency="USD",
        metadata={"reason": "test"},
    )
    await order_routes._log_shopify_receipt_suppressed_once(
        order_id="ORD_1",
        merchant_id="merch_1",
        total_amount=1.09,
        currency="USD",
        metadata={"reason": "test"},
    )

    assert len(captured) == 1
    assert captured[0]["event_type"] == "shopify_receipt_suppressed"


@pytest.mark.asyncio
async def test_reconcile_missing_shopify_dry_run_handles_row_without_get(monkeypatch):
    from routes import order_routes

    class RowWithoutGet:
        def __init__(self, order_id: str):
            self._order_id = order_id
            self.get = None

        def __getitem__(self, key):
            if key == "order_id":
                return self._order_id
            raise KeyError(key)

    class DummyDatabase:
        async def fetch_all(self, _query):
            return [RowWithoutGet("ORD_REC_1"), RowWithoutGet("ORD_REC_2")]

    monkeypatch.setattr(order_routes, "database", DummyDatabase())

    resp = await order_routes.reconcile_missing_shopify_orders(
        merchant_id="merch_1",
        limit=50,
        min_age_seconds=120,
        dry_run=True,
        current_user={"role": "admin"},
    )

    assert resp["status"] == "success"
    assert resp["dry_run"] is True
    assert resp["count"] == 2
    assert resp["candidates"] == ["ORD_REC_1", "ORD_REC_2"]
