import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _shopline_order():
    return {
        "id": "sl-order-1",
        "created_at": "2026-08-27T10:00:00Z",
        "processed_at": "2026-08-27T10:01:00Z",
        "cancelled_at": "2026-08-27T10:02:00Z",
        "currency": "USD",
        "current_total_price": "25.50",
        "financial_status": "paid",
        "cart_token": "cart-1",
        "checkout_id": "checkout-1",
        "landing_site": "https://demo.myshopline.com/?utm_content=clk_abcdef1234",
        "customer": {
            "id": "customer-1",
            "email": "buyer@example.com",
            "phone": "555-0100",
            "addresses": {"address1": "private"},
        },
        "billing_address": {"name": "Private Buyer", "address1": "private"},
        "line_items": [
            {
                "id": "line-1",
                "product_id": "product-1",
                "variant_id": "variant-1",
                "sku": "SKU-1",
                "quantity": 2,
                "price": "12.75",
                "title": "Private-ish title",
                "properties": [{"name": "engraving", "value": "private"}],
            }
        ],
        "transactions": [{"id": "payment-1", "kind": "sale", "status": "success"}],
    }


def _shoplazza_order():
    return {
        "id": "sz-order-1",
        "created_at": "2026-08-27T11:00:00Z",
        "placed_at": "2026-08-27T11:01:00Z",
        "updated_at": "2026-08-27T11:02:00Z",
        "currency": "USD",
        "total_price": "40.00",
        "real_total_paid": "40.00",
        "total_refund_price": "10.00",
        "financial_status": "paid",
        "landing_site": "https://demo.myshoplaza.com/?pivota_click_id=clk_12345678",
        "customer": {"id": "buyer-2", "email": "private@example.com"},
        "shipping_address": {"name": "Private", "phone": "555-0199"},
        "line_items": [
            {
                "id": "line-2",
                "product_id": "product-2",
                "variant_id": "variant-2",
                "sku": "SKU-2",
                "quantity": 1,
                "total": "40.00",
                "product_title": "Do not persist",
                "custom_properties": {"message": "private"},
            }
        ],
        "payment_line": {
            "id": "payment-line-2",
            "transaction_no": "transaction-2",
            "credit_card_number": "4242",
            "merchant_email": "merchant@example.com",
        },
    }


def test_shopline_order_topics_map_to_canonical_lifecycle_without_pii():
    from services.shopline_family_event_adapter import map_shopline_webhook

    expected = {
        "orders/create": "order.created",
        "orders/paid": "order.paid",
        "orders/cancelled": "order.cancelled",
    }
    for topic, event_type in expected.items():
        batch = map_shopline_webhook(
            _shopline_order(),
            topic=topic,
            delivery_id=f"delivery-{topic}",
            store_id="store-sl",
        )
        event = batch.events[0]
        assert event.event_type == event_type
        assert event.order_id == "sl-order-1"
        assert event.click_id == "clk_abcdef1234"
        assert event.amount_cents == 2550
        assert event.payment_id == "payment-1"
        serialized = event.model_dump_json()
        for private_value in ("buyer@example.com", "555-0100", "Private Buyer", "private"):
            assert private_value not in serialized
        assert event.metadata["native_line_items"] == [
            {
                "id": "line-1",
                "product_id": "product-1",
                "variant_id": "variant-1",
                "sku": "SKU-1",
                "quantity": 2,
                "price": "12.75",
            }
        ]


def test_shopline_refund_requires_explicit_successful_transaction():
    from services.shopline_family_event_adapter import (
        UnsupportedShoplineFamilyEvent,
        map_shopline_webhook,
    )

    pending = {
        "id": "refund-1",
        "order_id": "sl-order-1",
        "transactions": [
            {"id": "refund-txn-1", "kind": "refund", "status": "pending", "amount": "5.00"}
        ],
    }
    with pytest.raises(UnsupportedShoplineFamilyEvent):
        map_shopline_webhook(
            pending,
            topic="refunds/create",
            delivery_id="delivery-refund",
            store_id="store-sl",
        )

    pending["transactions"][0]["status"] = "success"
    pending["transactions"][0]["currency"] = "USD"
    batch = map_shopline_webhook(
        pending,
        topic="refunds/create",
        delivery_id="delivery-refund",
        store_id="store-sl",
    )
    event = batch.events[0]
    assert event.event_type == "refund.succeeded"
    assert event.refund_id == "refund-1"
    assert event.payment_id == "refund-txn-1"
    assert event.order_id == "sl-order-1"
    assert event.amount_cents == 500


def test_shopline_zero_decimal_currency_uses_minor_unit_semantics():
    from services.shopline_family_event_adapter import map_shopline_webhook

    order = _shopline_order()
    order["currency"] = "JPY"
    order["current_total_price"] = "2500"
    event = map_shopline_webhook(
        order,
        topic="orders/create",
        delivery_id="delivery-jpy",
        store_id="store-sl",
    ).events[0]
    assert event.amount_cents == 2500


def test_shoplazza_paid_and_refund_events_are_wrapper_tolerant_and_safe():
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    paid = map_shoplazza_webhook(
        {"order": _shoplazza_order()},
        topic="orders/paid",
        delivery_id="dedupe-paid",
        store_id="store-sz",
    ).events[0]
    assert paid.event_type == "order.paid"
    assert paid.order_id == "sz-order-1"
    assert paid.payment_id == "transaction-2"
    assert paid.click_id == "clk_12345678"
    assert paid.amount_cents == 4000

    refunded = map_shoplazza_webhook(
        {"order": _shoplazza_order()},
        topic="orders/partially_refunded",
        delivery_id="dedupe-refund",
        store_id="store-sz",
    ).events[0]
    assert refunded.event_type == "refund.succeeded"
    assert refunded.amount_cents is None
    assert refunded.metadata["native_cumulative_refund_total"] == "10.00"
    assert refunded.metadata["native_amount_semantics"] == "cumulative_refund_total"
    serialized = refunded.model_dump_json()
    assert "private@example.com" not in serialized
    assert "merchant@example.com" not in serialized
    assert "credit_card_number" not in serialized


@pytest.mark.parametrize(
    ("platform", "path", "signature_header", "topic_header", "delivery_header", "domain_header", "domain"),
    [
        (
            "shopline",
            "/webhooks/shopline/store-native",
            "X-Shopline-Hmac-Sha256",
            "X-Shopline-Topic",
            "X-Shopline-Webhook-Id",
            "X-Shopline-Shop-Domain",
            "demo.myshopline.com",
        ),
        (
            "shoplazza",
            "/webhooks/shoplazza/store-native",
            "X-Shoplazza-Hmac-Sha256",
            "X-Shoplazza-Topic",
            "X-Shoplazza-Deduplication-ID",
            "X-Shoplazza-Shop-Domain",
            "demo.myshoplaza.com",
        ),
    ],
)
def test_shopline_family_webhook_routes_verify_signature_and_source(
    monkeypatch,
    platform,
    path,
    signature_header,
    topic_header,
    delivery_header,
    domain_header,
    domain,
):
    from routes import shopline_family_webhooks as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-native",
                "merchant_id": "merchant-1",
                "domain": domain,
                "api_key": json.dumps({"app_secret": "app-secret"}),
            }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": 1, "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    payload = _shopline_order() if platform == "shopline" else {"order": _shoplazza_order()}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(b"app-secret", raw, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        signature_header: signature,
        topic_header: "orders/paid",
        delivery_header: "delivery-1",
        domain_header: domain,
        "Content-Type": "application/json",
    }

    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["platform"] == platform
    assert ingested[0]["merchant_id"] == "merchant-1"
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"
    assert ingested[0]["write_path"] in {"shopline_webhook", "shoplazza_webhook"}

    invalid = client.post(path, content=raw, headers={**headers, signature_header: "invalid"})
    assert invalid.status_code == 401
    wrong_source = client.post(
        path,
        content=raw,
        headers={**headers, domain_header: "attacker.example"},
    )
    assert wrong_source.status_code == 401
    missing_source_headers = dict(headers)
    del missing_source_headers[domain_header]
    missing_source = client.post(path, content=raw, headers=missing_source_headers)
    assert missing_source.status_code == 401


@pytest.mark.asyncio
async def test_shopline_reconnect_preserves_existing_app_secret(monkeypatch):
    from routes import shopline_integrations as route

    writes = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-existing",
                "api_key": json.dumps(
                    {"access_token": "old-token", "app_secret": "keep-me"}
                ),
            }

        async def execute(self, query, values):
            writes.append(values)

    monkeypatch.setattr(route, "database", FakeDB())
    store_id = await route._upsert_store(
        merchant_id="merchant-1",
        platform="shopline",
        domain="demo.myshopline.com",
        name="Demo",
        credentials={"access_token": "new-token"},
    )
    assert store_id == "store-existing"
    persisted = json.loads(writes[0]["api_key"])
    assert persisted == {"access_token": "new-token", "app_secret": "keep-me"}
