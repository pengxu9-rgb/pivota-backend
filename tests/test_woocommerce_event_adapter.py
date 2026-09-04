import base64
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _order_payload(status="processing"):
    return {
        "id": 44,
        "order_key": "wc_order_secretish",
        "status": status,
        "currency": "USD",
        "total": "25.50",
        "total_refunded": "25.50" if status == "refunded" else "0.00",
        "customer_id": 8,
        "transaction_id": "txn-44",
        "payment_method": "stripe",
        "date_created_gmt": "2026-08-27T10:00:00",
        "date_paid_gmt": "2026-08-27T10:01:00" if status == "processing" else None,
        "date_modified_gmt": "2026-08-27T10:02:00",
        "billing": {"email": "buyer@example.com", "phone": "555-0100"},
        "line_items": [
            {
                "id": 1,
                "name": "Private-ish product label",
                "product_id": 10,
                "variation_id": 11,
                "sku": "SKU-11",
                "quantity": 2,
                "total": "25.50",
            }
        ],
        "meta_data": [
            {"key": "_wc_order_attribution_utm_content", "value": "clk_abcdef1234"}
        ],
    }


def test_woocommerce_paid_order_maps_to_idempotent_created_and_paid_events():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    created = map_woocommerce_webhook(
        _order_payload(),
        topic="order.created",
        delivery_id="delivery-1",
        store_id="store-woo",
    )
    updated = map_woocommerce_webhook(
        _order_payload(),
        topic="order.updated",
        delivery_id="delivery-2",
        store_id="store-woo",
    )

    assert [event.event_type for event in created.events] == ["order.created", "order.paid"]
    assert [event.event_id for event in created.events] == [
        event.event_id for event in updated.events
    ]
    assert created.events[0].order_id == "44"
    assert created.events[0].payment_id == "txn-44"
    assert created.events[0].click_id == "clk_abcdef1234"
    assert created.events[0].amount_cents == 2550
    assert created.events[0].occurred_at.isoformat() == "2026-08-27T10:00:00+00:00"
    assert created.events[1].occurred_at.isoformat() == "2026-08-27T10:01:00+00:00"
    metadata = created.events[0].metadata
    assert "billing" not in metadata
    assert "email" not in json.dumps(metadata)
    assert "name" not in metadata["native_line_items"][0]


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        ("cancelled", "order.cancelled"),
        ("failed", "payment.failed"),
        ("refunded", "refund.succeeded"),
    ],
)
def test_woocommerce_order_status_maps_lifecycle(status, event_type):
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    batch = map_woocommerce_webhook(
        _order_payload(status),
        topic="order.updated",
        delivery_id=f"delivery-{status}",
        store_id="store-woo",
    )

    assert len(batch.events) == 1
    assert batch.events[0].event_type == event_type
    assert batch.events[0].order_id == "44"
    assert batch.events[0].occurred_at.isoformat() == "2026-08-27T10:02:00+00:00"


def test_woocommerce_terminal_status_wins_over_historical_paid_date():
    from services.woocommerce_event_adapter import map_woocommerce_webhook

    payload = _order_payload("refunded")
    payload["date_paid_gmt"] = "2026-08-27T10:01:00"
    batch = map_woocommerce_webhook(
        payload,
        topic="order.updated",
        delivery_id="delivery-refunded-after-paid",
        store_id="store-woo",
    )

    assert [event.event_type for event in batch.events] == ["refund.succeeded"]


def test_woocommerce_webhook_route_requires_valid_hmac_and_source(monkeypatch):
    from routes import woocommerce_webhooks as route

    ingested = []

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-woo",
                "merchant_id": "merchant-1",
                "domain": "https://shop.example",
                "api_key": json.dumps(
                    {
                        "consumer_key": "ck_test",
                        "consumer_secret": "cs_test",
                        "webhook_secret": "hook-secret",
                    }
                ),
            }

    async def fake_ingest(**kwargs):
        ingested.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", fake_ingest)
    app = FastAPI()
    app.include_router(route.router)
    client = TestClient(app)
    raw = json.dumps(_order_payload(), separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(b"hook-secret", raw, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "X-WC-Webhook-Signature": signature,
        "X-WC-Webhook-Topic": "order.updated",
        "X-WC-Webhook-Delivery-ID": "delivery-1",
        "X-WC-Webhook-Source": "https://shop.example/",
    }

    response = client.post("/webhooks/woocommerce/store-woo", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    assert ingested[0]["merchant_id"] == "merchant-1"
    assert ingested[0]["agent_identity_confidence"] == "platform_asserted"

    invalid = client.post(
        "/webhooks/woocommerce/store-woo",
        content=raw,
        headers={**headers, "X-WC-Webhook-Signature": "invalid"},
    )
    assert invalid.status_code == 401

    wrong_source = client.post(
        "/webhooks/woocommerce/store-woo",
        content=raw,
        headers={**headers, "X-WC-Webhook-Source": "https://attacker.example"},
    )
    assert wrong_source.status_code == 401


def test_woocommerce_legacy_consumer_secret_is_valid_webhook_secret_fallback():
    from routes.woocommerce_webhooks import _credentials

    assert _credentials("ck_test:cs_test") == {
        "consumer_key": "ck_test",
        "consumer_secret": "cs_test",
    }


@pytest.mark.asyncio
async def test_woocommerce_connect_persists_webhook_secret_and_returns_setup_path(monkeypatch):
    from adapters import woocommerce_adapter
    from routes import merchant_store_connections as route

    writes = []

    class FakeAdapter:
        def __init__(self, config):
            self.store_url = "https://shop.example"

        def validate_config(self):
            return True, None

        async def test_connection(self):
            return {"success": True, "store_name": "Example Woo"}

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return None

        async def execute(self, query, values):
            writes.append((query, values))

    monkeypatch.setattr(woocommerce_adapter, "WooCommerceAdapter", FakeAdapter)
    monkeypatch.setattr(route, "database", FakeDB())

    result = await route.merchant_connect_woocommerce(
        route.ConnectWooCommerceRequest(
            merchant_id="merchant-1",
            store_url="shop.example",
            consumer_key="ck_test",
            consumer_secret="cs_test",
            webhook_secret="hook-secret",
        ),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    persisted = json.loads(writes[0][1]["api_key"])
    assert persisted["webhook_secret"] == "hook-secret"
    assert result["webhook_path"].startswith("/webhooks/woocommerce/")
    assert result["webhook_subscription_path"].endswith("/webhooks/ensure")
    assert result["required_webhook_topics"] == ["order.created", "order.updated"]
    assert "hook-secret" not in json.dumps(result)
