from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from models.order import CreateOrderRequest
from services.quote_service import QuoteError


@pytest.mark.asyncio
async def test_quote_first_create_order_persists_authoritative_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
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
        captured["order_data"] = order_data
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

    req = CreateOrderRequest(
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
        metadata={},
    )

    resp = await module.create_new_order(req, BackgroundTasks(), current_user={"user_id": "test"})

    order_data = captured["order_data"]
    assert order_data["subtotal"] == pytest.approx(1.69)
    assert order_data["discount_total"] == pytest.approx(0.60)
    assert order_data["shipping_fee"] == pytest.approx(8.00)
    assert order_data["total"] == pytest.approx(9.09)
    assert order_data["metadata"]["amounts_source"] == "quote_snapshot"
    assert order_data["metadata"]["pricing_quote"]["quote_id"] == "q_test"
    assert order_data["metadata"]["pricing_quote"]["live_validation"]["status"] == "validated"
    assert order_data["items"][0]["product_title"] == "Winona Soothing Repair Serum"
    assert Decimal(order_data["items"][0]["unit_price"]) == Decimal("1.09")
    assert Decimal(order_data["items"][0]["subtotal"]) == Decimal("1.09")
    assert resp.discount_total == Decimal("0.60")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quote_error_code",
    ["QUOTE_STALE_REPRICE_REQUIRED", "INSUFFICIENT_INVENTORY"],
)
async def test_live_revalidation_failure_blocks_order_and_psp(
    monkeypatch: pytest.MonkeyPatch,
    quote_error_code: str,
) -> None:
    import routes.order_routes as module

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
                "items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
                "discount_codes": [],
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
                    "subtotal": "10.00",
                    "discount_total": "0.00",
                    "shipping_fee": "0.00",
                    "tax": "0.00",
                    "total": "10.00",
                },
                "line_items": [
                    {
                        "product_id": "p_1",
                        "variant_id": "v_1",
                        "quantity": 1,
                        "unit_price_original": "10.00",
                        "unit_price_effective": "10.00",
                        "line_discount_total": "0.00",
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
        assert create_replacement_quote_on_mismatch is True
        details = {"quote_id": quote.quote_id, "mismatches": [{"field": "pricing.total"}]}
        if quote_error_code == "QUOTE_STALE_REPRICE_REQUIRED":
            details["replacement_quote"] = {
                "quote_id": "q_repriced",
                "expires_at": "2026-04-30T06:00:00+00:00",
                "currency": "USD",
                "availability_status": "available_confirmed",
                "is_final": True,
                "pricing": {
                    "subtotal": "11.00",
                    "discount_total": "0.00",
                    "shipping_fee": "0.00",
                    "tax": "0.00",
                    "total": "11.00",
                },
                "requires_user_confirmation": True,
            }
        raise QuoteError(
            quote_error_code,
            "Quote no longer matches live store pricing or availability. Refresh the quote before checkout.",
            details=details,
        )

    async def fail_create_order(*args, **kwargs):
        raise AssertionError("stale quote must not create a Pivota order")

    async def fail_create_payment_with_failover(*args, **kwargs):
        raise AssertionError("stale quote must not call PSP")

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "check_inventory_availability", fake_check_inventory)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", fake_load_active_quote_or_raise)
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", fake_validate_quote_snapshot_live)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_: "fp_1")
    monkeypatch.setattr(module, "create_order", fail_create_order)
    monkeypatch.setattr(module, "create_payment_with_failover", fail_create_payment_with_failover)

    req = CreateOrderRequest(
        merchant_id="merch_1",
        customer_email="buyer@example.com",
        quote_id="q_stale",
        items=[{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
        shipping_address={
            "name": "Buyer",
            "address_line1": "1 Market St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
        },
        currency="USD",
        metadata={},
    )

    with pytest.raises(HTTPException) as exc:
        await module.create_new_order(req, BackgroundTasks(), current_user={"user_id": "test"})

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == quote_error_code
    if quote_error_code == "QUOTE_STALE_REPRICE_REQUIRED":
        assert exc.value.detail["status"] == "reprice_required"
        assert exc.value.detail["action"] == "review_repriced_quote"
        assert exc.value.detail["requires_user_confirmation"] is True
        assert exc.value.detail["payment_created"] is False
        assert exc.value.detail["order_created"] is False
        assert exc.value.detail["replacement_quote_id"] == "q_repriced"
        assert exc.value.detail["details"]["replacement_quote"]["pricing"]["total"] == "11.00"
