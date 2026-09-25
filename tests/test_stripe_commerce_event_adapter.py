from datetime import datetime, timezone

import pytest


def _order(**overrides):
    value = {
        "order_id": "ORD_123",
        "merchant_id": "merchant-1",
        "store_id": "store-1",
        "buyer_id": "buyer-1",
        "agent_session_id": "session-1",
        "customer_email": "private@example.com",
        "shipping_address": {"address1": "private street"},
        "metadata": {
            "pivota_click_id": "clk_abcdefgh",
            "checkout_id": "checkout-1",
            "brief_id": "brief-1",
            "private_note": "do not persist",
        },
    }
    value.update(overrides)
    return value


def test_stripe_payment_success_maps_verified_minor_units_and_order_links():
    from services.stripe_commerce_event_adapter import map_stripe_webhook_event

    event = map_stripe_webhook_event(
        {
            "id": "pi_123",
            "status": "succeeded",
            "amount": 2600,
            "amount_received": 2550,
            "currency": "usd",
            "created": 1788084000,
            "receipt_email": "private@example.com",
        },
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_1",
        event_created=1788083999,
        order=_order(),
        store_id="store-1",
        platform="shopify",
    ).events[0]

    assert event.event_type == "payment.succeeded"
    assert event.payment_id == "pi_123"
    assert event.order_id == "ORD_123"
    assert event.store_id == "store-1"
    assert event.platform == "shopify"
    assert event.session_id == "session-1"
    assert event.buyer_id == "buyer-1"
    assert event.click_id == "clk_abcdefgh"
    assert event.checkout_id == "checkout-1"
    assert event.amount_cents == 2550
    assert event.currency == "USD"
    assert event.metadata["native_amount_semantics"] == "psp_minor_units"
    serialized = event.model_dump_json()
    assert "private@example.com" not in serialized
    assert "private street" not in serialized
    assert "do not persist" not in serialized


@pytest.mark.parametrize(
    ("native_type", "canonical_type", "amount_key"),
    [
        ("payment_intent.amount_capturable_updated", "payment.authorized", "amount_capturable"),
        ("payment_intent.payment_failed", "payment.failed", "amount"),
    ],
)
def test_stripe_payment_lifecycle_mapping(native_type, canonical_type, amount_key):
    from services.stripe_commerce_event_adapter import map_stripe_webhook_event

    data = {"id": "pi_123", "status": "requires_payment_method", "currency": "usd"}
    data[amount_key] = 1200
    event = map_stripe_webhook_event(
        data,
        event_type=native_type,
        stripe_event_id="evt_1",
        event_created="2026-08-30T10:00:00Z",
        order=_order(),
        store_id="store-1",
        platform="shopify",
    ).events[0]
    assert event.event_type == canonical_type
    assert event.amount_cents == 1200


def test_stripe_terminal_event_ids_are_entity_stable_across_delivery_ids():
    from services.stripe_commerce_event_adapter import map_stripe_webhook_event

    kwargs = {
        "data": {"id": "pi_123", "amount_received": 2550, "currency": "usd"},
        "event_type": "payment_intent.succeeded",
        "event_created": 1788084000,
        "order": _order(),
        "store_id": "store-1",
        "platform": "shopify",
    }
    first = map_stripe_webhook_event(stripe_event_id="evt_1", **kwargs).events[0]
    replay = map_stripe_webhook_event(stripe_event_id="evt_2", **kwargs).events[0]
    assert first.event_id == replay.event_id
    assert first.trace_id != replay.trace_id


def test_stripe_refund_created_and_succeeded_are_distinct_facts():
    from services.stripe_commerce_event_adapter import map_stripe_webhook_event

    data = {
        "id": "re_123",
        "payment_intent": "pi_123",
        "amount": 500,
        "currency": "usd",
        "status": "pending",
    }
    created = map_stripe_webhook_event(
        data,
        event_type="refund.created",
        stripe_event_id="evt_created",
        event_created=1788084000,
        order=_order(),
        store_id="store-1",
        platform="shopify",
    ).events[0]
    assert created.event_type == "refund.created"
    assert created.refund_id == "re_123"
    assert created.payment_id == "pi_123"
    assert created.amount_cents is None

    data["status"] = "succeeded"
    succeeded = map_stripe_webhook_event(
        data,
        event_type="refund.updated",
        stripe_event_id="evt_updated",
        event_created=1788084010,
        order=_order(),
        store_id="store-1",
        platform="shopify",
    ).events[0]
    assert succeeded.event_type == "refund.succeeded"
    assert succeeded.amount_cents == 500


def test_stripe_charge_refund_uses_embedded_refund_ids_and_dedupes_with_update():
    from services.stripe_commerce_event_adapter import map_stripe_webhook_event

    refund = {
        "id": "re_123",
        "payment_intent": "pi_123",
        "amount": 500,
        "currency": "usd",
        "status": "succeeded",
        "created": 1788084000,
    }
    from_charge = map_stripe_webhook_event(
        {
            "id": "ch_123",
            "payment_intent": "pi_123",
            "amount_refunded": 500,
            "currency": "usd",
            "refunds": {"data": [refund, refund]},
        },
        event_type="charge.refunded",
        stripe_event_id="evt_charge",
        event_created=1788084010,
        order=_order(),
        store_id="store-1",
        platform="shopify",
    )
    from_update = map_stripe_webhook_event(
        refund,
        event_type="refund.updated",
        stripe_event_id="evt_refund",
        event_created=1788084010,
        order=_order(),
        store_id="store-1",
        platform="shopify",
    ).events[0]

    assert len(from_charge.events) == 1
    assert from_charge.events[0].refund_id == "re_123"
    assert from_charge.events[0].event_id == from_update.event_id


def test_stripe_nonterminal_refund_update_and_cumulative_only_charge_are_ignored():
    from services.stripe_commerce_event_adapter import (
        UnsupportedStripeCommerceEvent,
        map_stripe_webhook_event,
    )

    with pytest.raises(UnsupportedStripeCommerceEvent):
        map_stripe_webhook_event(
            {"id": "re_1", "status": "pending"},
            event_type="refund.updated",
            stripe_event_id="evt_1",
            event_created=None,
            order=_order(),
            store_id="store-1",
            platform="shopify",
        )
    with pytest.raises(UnsupportedStripeCommerceEvent):
        map_stripe_webhook_event(
            {"id": "ch_1", "amount_refunded": 500, "refunds": {"data": []}},
            event_type="charge.refunded",
            stripe_event_id="evt_2",
            event_created=None,
            order=_order(),
            store_id="store-1",
            platform="shopify",
        )


@pytest.mark.asyncio
async def test_stripe_ingest_uses_order_store_scope_and_writes_canonical_batch(monkeypatch):
    from services import psp_commerce_event_ingest as service

    captured = []

    async def stores(_merchant_id):
        return [
            {"store_id": "store-other", "platform": "woocommerce", "is_primary": True},
            {"store_id": "store-1", "platform": "shopify", "is_primary": False},
        ]

    async def ingest(**kwargs):
        captured.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0}

    monkeypatch.setattr(service, "get_merchant_active_stores", stores)
    monkeypatch.setattr(service, "ingest_merchant_event_batch", ingest)
    result = await service.ingest_stripe_commerce_event_best_effort(
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_1",
        event_created=datetime(2026, 8, 30, tzinfo=timezone.utc),
        data={"id": "pi_123", "amount_received": 2550, "currency": "usd"},
        order=_order(),
        signature_verified=True,
    )

    assert result["status"] == "accepted"
    assert result["store_id"] == "store-1"
    assert captured[0]["agent_identity_confidence"] == "platform_asserted"
    assert captured[0]["write_path"] == "stripe_webhook"
    assert captured[0]["batch"].events[0].platform == "shopify"


@pytest.mark.asyncio
async def test_stripe_ingest_requires_signature_and_is_best_effort(monkeypatch):
    from services import psp_commerce_event_ingest as service

    async def broken_stores(_merchant_id):
        raise RuntimeError("store database unavailable")

    monkeypatch.setattr(service, "get_merchant_active_stores", broken_stores)
    unverified = await service.ingest_stripe_commerce_event_best_effort(
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_1",
        event_created=None,
        data={},
        order=_order(),
        signature_verified=False,
    )
    assert unverified == {"status": "skipped", "reason": "signature_not_verified"}

    degraded = await service.ingest_stripe_commerce_event_best_effort(
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_1",
        event_created=None,
        data={"id": "pi_123", "amount_received": 2550, "currency": "usd"},
        order=_order(),
        signature_verified=True,
    )
    assert degraded == {"status": "degraded", "reason": "canonical_ingest_failed"}


@pytest.mark.asyncio
async def test_route_safety_boundary_swallows_unexpected_bridge_failure(monkeypatch):
    from routes import webhook_routes as route

    async def broken_bridge(**kwargs):
        raise RuntimeError("optional canonical bridge unavailable")

    monkeypatch.setattr(route, "ingest_stripe_commerce_event_best_effort", broken_bridge)
    await route._record_stripe_canonical_event_best_effort(
        event_type="payment_intent.succeeded",
        stripe_event_id="evt_1",
        event_created=None,
        data={"id": "pi_123"},
        order=_order(),
    )
