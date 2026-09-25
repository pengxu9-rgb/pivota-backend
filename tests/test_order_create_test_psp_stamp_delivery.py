"""The order-create handler must actually CALL the server-side test-PSP stamp.

Every other test in tests/test_server_granted_test_psp_stamp.py exercises the helper directly.
An adversarial review deleted the call site from routes/order_routes.py and all 13,050 tests in
the repo stayed byte-identically green — the feature could ship completely inert, with the buyer's
payment still dying "All PSPs blocked", and nothing would notice. This test is the one that fails
when the delivering line is gone.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from models.order import CreateOrderRequest


def _psp_rows(monkeypatch, rows, counter=None):
    async def fake(*, merchant_id, provider=None, database_override=None):
        if counter is not None:
            counter["calls"] += 1
        return rows

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", fake)


def _order_request(metadata=None) -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id="merch_1",
        customer_email="peng@chydan.com",
        quote_id="q_test",
        discount_codes=["PIVOTA_AUDIT_20260421C_FIXPROD60"],
        items=[{"product_id": "10064558129449", "variant_id": "53012602618153", "quantity": 1}],
        shipping_address={
            "name": "Peng Chydan",
            "address_line1": "1 Market St",
            "address_line2": "",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": "",
        },
        currency="USD",
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_order_create_stamps_an_allowlisted_test_mode_merchant(monkeypatch: pytest.MonkeyPatch) -> None:
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
                "discount_codes": ["PIVOTA_AUDIT_20260421C_FIXPROD60"],
                "shipping_address": {
                    "country": "US",
                    "postal_code": "94105",
                    "city": "San Francisco",
                    "state": "CA",
                },
            },
            snapshot_json={
                "currency": "USD",
                "pricing": {
                    "subtotal": "1.69",
                    "discount_total": "0.60",
                    "shipping_fee": "8.00",
                    "tax": "0.00",
                    "total": "9.09",
                },
                "promotion_lines": [
                    {
                        "code": "PIVOTA_AUDIT_20260421C_FIXPROD60",
                        "amount": "-0.60",
                    }
                ],
                "line_items": [
                    {
                        "product_id": "10064558129449",
                        "variant_id": "53012602618153",
                        "quantity": 1,
                        "title": "Winona Soothing Repair Serum",
                        "unit_price_original": "1.69",
                        "unit_price_effective": "1.09",
                        "line_discount_total": "0.60",
                    }
                ],
            },
        )

    async def fake_validate_quote_snapshot_live(
        self,
        quote,
        *,
        customer_email: str | None = None,
        create_replacement_quote_on_mismatch: bool = False,
    ):
        assert quote.quote_id == "q_test"
        assert customer_email == "peng@chydan.com"
        assert create_replacement_quote_on_mismatch is True
        return {"status": "validated", "engine": "shopify_storefront_cart", "engine_ref": "cart_ref_live"}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
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

    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_1")
    counter = {"calls": 0}
    _psp_rows(
        monkeypatch,
        [{"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_test_abc"}],
        counter,
    )

    await module.create_new_order(_order_request(), BackgroundTasks(), current_user={"user_id": "test"})

    metadata = captured["order_data"]["metadata"]
    # The delivering assertions: the PERSISTED order carries the stamp, so the gate permits a test
    # processor for a buyer who supplied no URL parameter at all.
    assert metadata.get("allow_test_psp_surfaces") is True
    assert metadata.get("test_psp_surfaces_granted_by") == "server_allowlist"
    assert module._resolve_order_live_readiness_requirement(metadata, "merch_1") is False
    # And prove the PSP double was really consulted, so the assertions above cannot be satisfied by
    # the ambient fail-closed path.
    assert counter["calls"] >= 1


@pytest.mark.asyncio
async def test_order_create_does_not_stamp_a_merchant_with_a_live_processor(monkeypatch: pytest.MonkeyPatch) -> None:
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
                "discount_codes": ["PIVOTA_AUDIT_20260421C_FIXPROD60"],
                "shipping_address": {
                    "country": "US",
                    "postal_code": "94105",
                    "city": "San Francisco",
                    "state": "CA",
                },
            },
            snapshot_json={
                "currency": "USD",
                "pricing": {
                    "subtotal": "1.69",
                    "discount_total": "0.60",
                    "shipping_fee": "8.00",
                    "tax": "0.00",
                    "total": "9.09",
                },
                "promotion_lines": [
                    {
                        "code": "PIVOTA_AUDIT_20260421C_FIXPROD60",
                        "amount": "-0.60",
                    }
                ],
                "line_items": [
                    {
                        "product_id": "10064558129449",
                        "variant_id": "53012602618153",
                        "quantity": 1,
                        "title": "Winona Soothing Repair Serum",
                        "unit_price_original": "1.69",
                        "unit_price_effective": "1.09",
                        "line_discount_total": "0.60",
                    }
                ],
            },
        )

    async def fake_validate_quote_snapshot_live(
        self,
        quote,
        *,
        customer_email: str | None = None,
        create_replacement_quote_on_mismatch: bool = False,
    ):
        assert quote.quote_id == "q_test"
        assert customer_email == "peng@chydan.com"
        assert create_replacement_quote_on_mismatch is True
        return {"status": "validated", "engine": "shopify_storefront_cart", "engine_ref": "cart_ref_live"}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
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

    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_1")
    counter = {"calls": 0}
    _psp_rows(
        monkeypatch,
        [{"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_live_REAL"}],
        counter,
    )

    await module.create_new_order(_order_request(), BackgroundTasks(), current_user={"user_id": "test"})

    metadata = captured["order_data"]["metadata"]
    assert counter["calls"] >= 1  # the guard really ran and really refused
    assert "allow_test_psp_surfaces" not in metadata
    # Positive counterpart: the order was really built, so the absence above means something.
    assert metadata["amounts_source"] == "quote_snapshot"
    assert module._resolve_order_live_readiness_requirement(metadata, "merch_1") is True
