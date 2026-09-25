"""The order-create endpoint drops caller-supplied `ops_canary` / `skip_platform_order_creation`.

Those two keys on ORDER metadata make the Stripe `payment_intent.succeeded` webhook (and the
payment reconcile sweep) skip creating the merchant's Shopify order. Order metadata on this
endpoint is caller-supplied — the agent gateway forwards it verbatim — so honouring a caller's
value would let any API caller get a PAID order that is never pushed to the merchant. Only the ops
canary may set them, and it writes them itself through `db.orders.create_order`.

Drives the real `create_new_order` (not the helper), so the test fails if the delivering call is
removed. Harness shape follows tests/test_order_create_test_psp_stamp_delivery.py.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from models.order import CreateOrderRequest


def _order_request(metadata) -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id="merch_1",
        customer_email="buyer@example.com",
        quote_id="q_test",
        items=[{"product_id": "10064558129449", "variant_id": "53012602618153", "quantity": 1}],
        shipping_address={
            "name": "Buyer",
            "address_line1": "1 Market St",
            "address_line2": "",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": "",
        },
        currency="USD",
        metadata=metadata,
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    import routes.order_routes as module

    captured: dict = {}

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id}

    async def fake_check_inventory(_merchant_id: str, _items):
        return True, {}

    async def fake_load_active_quote_or_raise(self, *, quote_id: str):
        return SimpleNamespace(
            quote_id=quote_id,
            expires_at=None,
            engine="shopify_storefront_cart",
            engine_ref="cart_ref",
            request_fingerprint="fp_1",
            quote_hash_sha256="h" * 64,
            debug_id="debug_1",
            merchant_id="merch_1",
            request_json={
                "items": [{"product_id": "10064558129449", "variant_id": "53012602618153", "quantity": 1}],
                "discount_codes": [],
                "shipping_address": {"country": "US", "postal_code": "94105", "city": "San Francisco", "state": "CA"},
            },
            snapshot_json={
                "currency": "USD",
                "pricing": {"subtotal": "10.00", "discount_total": "0.00", "shipping_fee": "0.00", "tax": "0.00", "total": "10.00"},
                "promotion_lines": [],
                "line_items": [
                    {
                        "product_id": "10064558129449",
                        "variant_id": "53012602618153",
                        "quantity": 1,
                        "title": "Serum",
                        "unit_price_original": "10.00",
                        "unit_price_effective": "10.00",
                        "line_discount_total": "0.00",
                    }
                ],
            },
        )

    async def fake_validate_quote_snapshot_live(self, quote, **_kwargs):
        return {"status": "validated", "engine": "shopify_storefront_cart", "engine_ref": "cart_ref_live"}

    async def fake_select_psp(self, **_kwargs):
        return "stripe", {"route_id": "route_test"}

    async def fake_resolve_active_order_psp(_merchant_id: str, _provider_hint, **_kwargs):
        return "stripe", "psp_1"

    async def fake_create_order(order_data):
        captured["order_data"] = copy.deepcopy(order_data)
        return "ORD_TEST"

    async def fake_get_primary_store(_merchant_id: str):
        return None

    async def noop_async(*args, **kwargs):
        return None

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "payment skipped", "stripe"

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "check_inventory_availability", fake_check_inventory)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", fake_load_active_quote_or_raise)
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", fake_validate_quote_snapshot_live)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_: "fp_1")
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(module, "_resolve_active_order_psp", fake_resolve_active_order_psp)
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", noop_async)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", noop_async)
    monkeypatch.setattr(module, "log_order_event", noop_async)
    monkeypatch.setattr(module.QuoteService, "consume_quote_best_effort", noop_async)
    monkeypatch.setattr(module.database, "fetch_one", noop_async)
    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spoofed",
    [
        {"ops_canary": True},
        {"skip_platform_order_creation": "true"},
        {"ops_canary": "yes", "skip_platform_order_creation": 1},
    ],
    ids=["ops_canary", "skip_platform_order_creation", "both"],
)
async def test_a_caller_cannot_set_the_skip_platform_order_switches(captured: dict, spoofed: dict) -> None:
    import routes.order_routes as module

    await module.create_new_order(
        _order_request({"agent_id": "agt_1", **spoofed}), BackgroundTasks(), current_user={"user_id": "test"}
    )

    metadata = captured["order_data"]["metadata"]
    for key in spoofed:
        assert key not in metadata, sorted(metadata)
    # Only the two switches go; the rest of the caller's metadata is kept.
    assert metadata.get("agent_id") == "agt_1"
