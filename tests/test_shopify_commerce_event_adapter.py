from datetime import datetime, timezone

import pytest


def _order():
    return {
        "id": 1001,
        "created_at": "2026-08-30T10:00:00Z",
        "processed_at": "2026-08-30T10:01:00Z",
        "updated_at": "2026-08-30T10:02:00Z",
        "cancelled_at": "2026-08-30T10:03:00Z",
        "financial_status": "paid",
        "fulfillment_status": "unfulfilled",
        "currency": "USD",
        "current_total_price": "25.50",
        "current_total_discounts": "2.00",
        "current_total_tax": "1.50",
        "gateway": "shopify_payments",
        "cart_token": "cart-1",
        "checkout_id": 2001,
        "customer": {
            "id": 3001,
            "email": "private@example.com",
            "phone": "555-0100",
            "first_name": "Private",
        },
        "billing_address": {"address1": "private street"},
        "note_attributes": [{"name": "pivota_click_id", "value": "clk_abcdefgh"}],
        "line_items": [
            {
                "id": 4001,
                "product_id": 5001,
                "variant_id": 6001,
                "sku": "SKU-1",
                "quantity": 2,
                "price": "12.75",
                "title": "Do not persist",
                "properties": [{"name": "engraving", "value": "secret"}],
            }
        ],
    }


@pytest.mark.parametrize(
    ("topic", "event_type", "occurred_at"),
    [
        ("orders/create", "order.created", "2026-08-30T10:00:00+00:00"),
        ("orders/paid", "order.paid", "2026-08-30T10:01:00+00:00"),
        ("orders/cancelled", "order.cancelled", "2026-08-30T10:03:00+00:00"),
    ],
)
def test_shopify_order_lifecycle_maps_to_safe_canonical_event(topic, event_type, occurred_at):
    from services.shopify_commerce_event_adapter import map_shopify_webhook

    event = map_shopify_webhook(
        _order(),
        topic=topic,
        delivery_id="delivery-1",
        store_id="store-shopify",
    ).events[0]

    assert event.event_type == event_type
    assert event.occurred_at.isoformat() == occurred_at
    assert event.order_id == "1001"
    assert event.buyer_id == "3001"
    assert event.click_id == "clk_abcdefgh"
    assert event.cart_id == "cart-1"
    assert event.checkout_id == "2001"
    assert event.amount_cents == 2550
    assert event.currency == "USD"
    assert event.metadata["native_line_items"] == [
        {
            "id": 4001,
            "product_id": 5001,
            "variant_id": 6001,
            "sku": "SKU-1",
            "quantity": 2,
            "price": "12.75",
        }
    ]
    serialized = event.model_dump_json()
    for private in ("private@example.com", "555-0100", "Private", "private street", "secret"):
        assert private not in serialized


def test_shopify_event_id_is_entity_stable_across_webhook_deliveries():
    from services.shopify_commerce_event_adapter import map_shopify_webhook

    first = map_shopify_webhook(
        _order(), topic="orders/paid", delivery_id="delivery-1", store_id="store-1"
    ).events[0]
    replay = map_shopify_webhook(
        _order(), topic="orders/paid", delivery_id="delivery-2", store_id="store-1"
    ).events[0]

    assert first.event_id == replay.event_id
    assert first.trace_id != replay.trace_id


def test_shopify_refund_created_is_not_misreported_as_money_moved():
    from services.shopify_commerce_event_adapter import map_shopify_webhook

    refund = {
        "id": 7001,
        "order_id": 1001,
        "created_at": "2026-08-30T12:00:00Z",
        "transactions": [
            {
                "id": 8001,
                "kind": "refund",
                "status": "pending",
                "amount": "5.00",
                "currency": "USD",
            }
        ],
    }
    batch = map_shopify_webhook(
        refund,
        topic="refunds/create",
        delivery_id="refund-delivery",
        store_id="store-1",
    )

    assert [event.event_type for event in batch.events] == ["refund.created"]
    assert batch.events[0].refund_id == "7001"
    assert batch.events[0].order_id == "1001"
    assert batch.events[0].amount_cents is None


def test_shopify_successful_refund_transaction_adds_correlated_success_event():
    from services.shopify_commerce_event_adapter import map_shopify_webhook

    refund = {
        "id": 7001,
        "order_id": 1001,
        "created_at": "2026-08-30T12:00:00Z",
        "refund_line_items": [
            {
                "line_item_id": 4001,
                "quantity": 1,
                "subtotal": "5.00",
                "line_item": {
                    "product_id": 5001,
                    "variant_id": 6001,
                    "sku": "SKU-1",
                    "name": "Private label",
                },
            }
        ],
        "transactions": [
            {
                "id": 8001,
                "kind": "refund",
                "status": "success",
                "amount": "5.00",
                "currency": "USD",
                "processed_at": "2026-08-30T12:01:00Z",
                "authorization": "do-not-persist",
            }
        ],
    }
    events = map_shopify_webhook(
        refund,
        topic="refunds/create",
        delivery_id="refund-delivery",
        store_id="store-1",
    ).events

    assert [event.event_type for event in events] == ["refund.created", "refund.succeeded"]
    succeeded = events[1]
    assert succeeded.refund_id == "7001"
    assert succeeded.order_id == "1001"
    assert succeeded.payment_id == "8001"
    assert succeeded.amount_cents == 500
    assert succeeded.currency == "USD"
    serialized = succeeded.model_dump_json()
    assert "do-not-persist" not in serialized
    assert "Private label" not in serialized


def test_shopify_adapter_rejects_unknown_topics_and_missing_ids():
    from services.shopify_commerce_event_adapter import (
        UnsupportedShopifyCommerceEvent,
        map_shopify_webhook,
    )

    with pytest.raises(UnsupportedShopifyCommerceEvent):
        map_shopify_webhook({}, topic="orders/updated", delivery_id=None, store_id="store-1")
    with pytest.raises(ValueError, match="order id"):
        map_shopify_webhook({}, topic="orders/create", delivery_id=None, store_id="store-1")


@pytest.mark.asyncio
async def test_shopify_best_effort_ingest_requires_verified_signature(monkeypatch):
    from services import shopify_commerce_event_ingest as service

    class FailDB:
        async def fetch_all(self, *args, **kwargs):
            raise AssertionError("unverified payload must not reach store resolution")

    monkeypatch.setattr(service, "database", FailDB())
    result = await service.ingest_shopify_commerce_event_best_effort(
        merchant_id="merchant-1",
        shop_domain="shop.myshopify.com",
        topic="orders/create",
        payload=_order(),
        webhook_id="delivery-1",
        occurred_at=None,
        signature_verified=False,
    )
    assert result == {"status": "skipped", "reason": "signature_not_verified"}


@pytest.mark.asyncio
async def test_shopify_best_effort_ingest_resolves_store_and_writes_batch(monkeypatch):
    from services import shopify_commerce_event_ingest as service

    captured = []

    class FakeDB:
        async def fetch_all(self, *args, **kwargs):
            return [
                {
                    "store_id": "store-1",
                    "domain": "https://shop.myshopify.com/",
                }
            ]

    async def fake_ingest(**kwargs):
        captured.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr(service, "database", FakeDB())
    monkeypatch.setattr(service, "ingest_merchant_event_batch", fake_ingest)
    result = await service.ingest_shopify_commerce_event_best_effort(
        merchant_id="merchant-1",
        shop_domain="SHOP.MYSHOPIFY.COM",
        topic="orders/paid",
        payload=_order(),
        webhook_id="delivery-1",
        occurred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        signature_verified=True,
    )

    assert result["status"] == "accepted"
    assert result["store_id"] == "store-1"
    assert captured[0]["merchant_id"] == "merchant-1"
    assert captured[0]["batch"].events[0].event_type == "order.paid"


@pytest.mark.asyncio
async def test_shopify_best_effort_ingest_never_breaks_legacy_path(monkeypatch):
    from services import shopify_commerce_event_ingest as service

    class BrokenDB:
        async def fetch_all(self, *args, **kwargs):
            raise RuntimeError("canonical database unavailable")

    monkeypatch.setattr(service, "database", BrokenDB())
    result = await service.ingest_shopify_commerce_event_best_effort(
        merchant_id="merchant-1",
        shop_domain="shop.myshopify.com",
        topic="orders/create",
        payload=_order(),
        webhook_id="delivery-1",
        occurred_at=None,
        signature_verified=True,
    )

    assert result == {"status": "degraded", "reason": "canonical_ingest_failed"}


@pytest.mark.asyncio
async def test_shopify_duplicate_still_attempts_canonical_backfill(monkeypatch):
    from fastapi import BackgroundTasks
    from routes import webhook_routes as route

    canonical_calls = []

    async def duplicate_legacy(**kwargs):
        return True, {"event_id": "legacy-event"}

    async def canonical_bridge(**kwargs):
        canonical_calls.append(kwargs)
        return {"status": "accepted"}

    monkeypatch.setattr(route, "_shopify_prod_runtime", lambda: False)
    monkeypatch.setattr(route, "ingest_shopify_webhook", duplicate_legacy)
    monkeypatch.setattr(route, "ingest_shopify_commerce_event_best_effort", canonical_bridge)
    monkeypatch.setattr(route, "record_shopify_webhook", lambda **kwargs: None)

    result = await route._process_shopify_webhook_event(
        merchant_id="merchant-1",
        payload=b"{}",
        data=_order(),
        topic="orders/paid",
        shop_domain="shop.myshopify.com",
        got_canon="shop.myshopify.com",
        occurred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        x_shopify_webhook_id="delivery-1",
        background_tasks=BackgroundTasks(),
        signature_verified=True,
    )

    assert result == {"status": "success", "topic": "orders/paid", "duplicate": True}
    assert len(canonical_calls) == 1


@pytest.mark.asyncio
async def test_shopify_canonical_bridge_failure_does_not_change_duplicate_ack(monkeypatch):
    from fastapi import BackgroundTasks
    from routes import webhook_routes as route

    async def duplicate_legacy(**kwargs):
        return True, {"event_id": "legacy-event"}

    async def broken_bridge(**kwargs):
        raise RuntimeError("optional bridge unavailable")

    monkeypatch.setattr(route, "_shopify_prod_runtime", lambda: False)
    monkeypatch.setattr(route, "ingest_shopify_webhook", duplicate_legacy)
    monkeypatch.setattr(route, "ingest_shopify_commerce_event_best_effort", broken_bridge)
    monkeypatch.setattr(route, "record_shopify_webhook", lambda **kwargs: None)

    result = await route._process_shopify_webhook_event(
        merchant_id="merchant-1",
        payload=b"{}",
        data=_order(),
        topic="orders/paid",
        shop_domain="shop.myshopify.com",
        got_canon="shop.myshopify.com",
        occurred_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        x_shopify_webhook_id="delivery-1",
        background_tasks=BackgroundTasks(),
        signature_verified=True,
    )

    assert result == {"status": "success", "topic": "orders/paid", "duplicate": True}
