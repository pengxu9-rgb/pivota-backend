"""W3 F2: the test_psp_probe signal stamps ONLY an order the server granted the test-processor bypass.

Being on TEST_PSP_PROBE_MERCHANTS is not enough: a probe merchant whose processor is live has real
orders, and those must stay in GMV. Driven through the real create route
(routes.order_routes.create_new_order) with its I/O faked: the metadata it hands create_order and
the attribution edge is then run through the real stamp (db.orders._stamp_test_traffic) and the
real predicate (services.test_traffic).
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from models.order import CreateOrderRequest

MERCHANT = "merch_probe_shop_a"
TEST_ROW = {"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_test_fake"}
LIVE_ROW = {"provider": "stripe", "environment": "live", "runtime_secret_key": "sk_live_fake"}


def _order_request(metadata=None) -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id=MERCHANT,
        customer_email="buyer@example.com",
        quote_id="q_test",
        items=[{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
        shipping_address={
            "name": "Buyer A", "address_line1": "1 Example St", "address_line2": "",
            "city": "Example City", "state": "CA", "postal_code": "94105", "country": "US", "phone": "",
        },
        currency="USD",
        metadata=metadata or {},
    )


async def _create(monkeypatch, *, psp_row, caller_metadata=None):
    import routes.order_routes as module

    captured: dict = {}

    async def _onboarding(_m):
        return {"merchant_id": _m}

    async def _inventory(_m, _items):
        return True, {}

    async def _quote(self, *, quote_id):
        return SimpleNamespace(
            quote_id=quote_id, expires_at=None, engine="shopify_storefront_cart", engine_ref="cart_ref",
            request_fingerprint="fp_1", quote_hash_sha256="h" * 64, debug_id="debug_1", merchant_id=MERCHANT,
            request_json={"items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
                          "shipping_address": {"country": "US", "postal_code": "94105", "city": "Example City",
                                               "state": "CA"}},
            snapshot_json={"currency": "USD",
                           "pricing": {"subtotal": "10.00", "discount_total": "0.00", "shipping_fee": "0.00",
                                       "tax": "0.00", "total": "10.00"},
                           "line_items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1,
                                           "title": "Product A", "unit_price_original": "10.00",
                                           "unit_price_effective": "10.00", "line_discount_total": "0.00"}]},
        )

    async def _validate(self, quote, *, customer_email=None, create_replacement_quote_on_mismatch=False):
        return {"status": "validated", "engine": "shopify_storefront_cart", "engine_ref": "cart_ref_live"}

    async def _select(self, *, agent_id, merchant_id, amount, currency):
        return "stripe", {"route_id": "route_test"}

    async def _resolve(_m, _hint, **_k):
        return "stripe", "psp_1"

    async def _create_order(order_data):
        captured["order_data"] = copy.deepcopy(order_data)
        return "ORD_TEST"

    async def _edge(*, order_id, merchant_id, metadata):
        captured["edge_metadata"] = copy.deepcopy(metadata)

    async def _none(*a, **k):
        return None

    async def _pay(*a, **k):
        return False, None, "payment skipped", "stripe"

    async def _psps(*, merchant_id, provider=None, database_override=None):
        return [psp_row]

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", _psps)
    monkeypatch.setattr(module, "get_merchant_onboarding", _onboarding)
    monkeypatch.setattr(module, "check_inventory_availability", _inventory)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", _quote)
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", _validate)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_: "fp_1")
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", _select)
    monkeypatch.setattr(module, "_resolve_active_order_psp", _resolve)
    monkeypatch.setattr(module, "create_order", _create_order)
    monkeypatch.setattr(module, "get_primary_store", _none)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", _edge)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", _none)
    monkeypatch.setattr(module, "log_order_event", _none)
    monkeypatch.setattr(module.QuoteService, "consume_quote_best_effort", _none)
    monkeypatch.setattr(module.database, "fetch_one", _none)
    monkeypatch.setattr(module, "create_payment_with_failover", _pay)
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", MERCHANT)

    await module.create_new_order(_order_request(caller_metadata), BackgroundTasks(), current_user={"user_id": "t"})
    return captured


def _reasons(captured):
    """What the real writers would stamp: create_order's stamp, and the edge's predicate."""
    from db.orders import _stamp_test_traffic
    from services import test_traffic

    order_data = copy.deepcopy(captured["order_data"])
    _stamp_test_traffic(order_data)
    order = (order_data.get("metadata") or {}).get("test_traffic_reasons")
    edge = test_traffic.classify_test_traffic(merchant_ids=[MERCHANT], server_metadata=captured["edge_metadata"])
    return order, edge


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("TEST_TRAFFIC_STAMP_ENABLED", raising=False)
    monkeypatch.delenv("PIVOTA_TEST_MERCHANT_IDS", raising=False)


@pytest.mark.asyncio
async def test_a_granted_test_processor_order_is_stamped(monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    captured = await _create(monkeypatch, psp_row=TEST_ROW)
    assert captured["order_data"]["metadata"]["test_psp_probe_granted"] is True
    assert _reasons(captured) == (["test_psp_probe"], ["test_psp_probe"])


@pytest.mark.asyncio
async def test_a_probe_merchants_live_processor_order_stays_real(monkeypatch):
    """On the probe list, but the processor is live: the server does not grant, nothing is stamped."""
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    captured = await _create(monkeypatch, psp_row=LIVE_ROW)
    assert "test_psp_probe_granted" not in captured["order_data"]["metadata"]
    assert _reasons(captured) == (None, [])


@pytest.mark.asyncio
async def test_a_caller_cannot_forge_the_grant(monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    captured = await _create(monkeypatch, psp_row=LIVE_ROW, caller_metadata={"test_psp_probe_granted": True})
    assert "test_psp_probe_granted" not in captured["order_data"]["metadata"]
    assert _reasons(captured) == (None, [])


@pytest.mark.asyncio
async def test_flag_off_a_granted_order_metadata_is_unchanged(monkeypatch):
    captured = await _create(monkeypatch, psp_row=TEST_ROW)
    md = captured["order_data"]["metadata"]
    assert md["test_psp_surfaces_granted_by"] == "server_allowlist"  # the grant still happens
    assert "test_psp_probe_granted" not in md
    assert _reasons(captured) == (None, [])
