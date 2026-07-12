from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from models.order import CreateOrderRequest
from services.quote_service import QuoteError


def _build_quote_order_request() -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id="merch_ws11",
        customer_email="buyer@example.com",
        quote_id="q_ws11",
        items=[{"product_id": "prod_1", "variant_id": "var_1", "quantity": 1}],
        shipping_address={
            "name": "Test Buyer",
            "address_line1": "1 Test St",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94107",
            "country": "US",
        },
        currency="USD",
        metadata={},
    )


def _quote_snapshot(*, quote_id: str = "q_ws11") -> SimpleNamespace:
    return SimpleNamespace(
        quote_id=quote_id,
        expires_at=None,
        engine="shopify_storefront_cart",
        engine_ref="cart_ref",
        request_fingerprint="fp_ws11",
        quote_hash_sha256="h" * 64,
        debug_id="debug_ws11",
        merchant_id="merch_ws11",
        request_json={
            "items": [{"product_id": "prod_1", "variant_id": "var_1", "quantity": 1}],
            "discount_codes": [],
            "shipping_address": {
                "country": "US",
                "postal_code": "94107",
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
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "title": "Test Product",
                    "unit_price_original": "10.00",
                    "unit_price_effective": "10.00",
                    "line_discount_total": "0.00",
                }
            ],
        },
    )


async def _noop_async(*args, **kwargs):
    return None


def _install_success_harness(monkeypatch: pytest.MonkeyPatch, module) -> dict:
    calls: dict = {"merchant_ids": [], "inventory": [], "quote_ids": []}

    async def fake_select_psp(
        self, *, agent_id: str, merchant_id: str, amount: float, currency: str
    ):
        return "stripe", {"route_id": "route_ws11"}

    async def fake_resolve_active_order_psp(_merchant_id: str, _provider_hint, **_kwargs):
        return "stripe", "psp_ws11"

    async def fake_create_order(order_data):
        calls["order_data"] = order_data
        return "ORD_WS11"

    async def fake_load_active_quote_or_raise(self, *, quote_id: str):
        calls["quote_ids"].append(quote_id)
        return _quote_snapshot(quote_id=quote_id)

    async def fake_validate_quote_snapshot_live(
        self,
        quote,
        *,
        customer_email: str | None = None,
        create_replacement_quote_on_mismatch: bool = False,
    ):
        return {"status": "validated"}

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "payment skipped", "stripe"

    monkeypatch.delenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", raising=False)
    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(
        module, "_resolve_active_order_psp", fake_resolve_active_order_psp
    )
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "get_primary_store", _noop_async)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", _noop_async)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", _noop_async)
    monkeypatch.setattr(module, "log_order_event", _noop_async)
    monkeypatch.setattr(
        module, "create_payment_with_failover", fake_create_payment_with_failover
    )
    monkeypatch.setattr(
        module.QuoteService,
        "load_active_quote_or_raise",
        fake_load_active_quote_or_raise,
    )
    monkeypatch.setattr(
        module.QuoteService,
        "validate_quote_snapshot_live",
        fake_validate_quote_snapshot_live,
    )
    monkeypatch.setattr(module.QuoteService, "consume_quote_best_effort", _noop_async)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_: "fp_ws11")

    return calls


@pytest.mark.asyncio
async def test_create_new_order_parallel_early_reads_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    calls = _install_success_harness(monkeypatch, module)
    merchant_started = asyncio.Event()
    inventory_started = asyncio.Event()

    async def fake_get_merchant_onboarding(merchant_id: str):
        calls["merchant_ids"].append(merchant_id)
        merchant_started.set()
        await asyncio.wait_for(inventory_started.wait(), timeout=1)
        return {"merchant_id": merchant_id, "psp_connected": True}

    async def fake_check_inventory_availability(merchant_id: str, items):
        calls["inventory"].append((merchant_id, items))
        inventory_started.set()
        await asyncio.wait_for(merchant_started.wait(), timeout=1)
        return True, {"items": []}

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )

    response = await module.create_new_order(
        _build_quote_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )

    assert response.order_id == "ORD_WS11"
    assert calls["merchant_ids"] == ["merch_ws11"]
    assert len(calls["inventory"]) == 1
    assert calls["inventory"][0][0] == "merch_ws11"
    assert calls["quote_ids"] == ["q_ws11"]
    assert calls["order_data"]["metadata"]["pricing_quote"]["quote_id"] == "q_ws11"


@pytest.mark.asyncio
async def test_create_new_order_parallel_early_reads_merchant_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return None

    async def fake_check_inventory_availability(_merchant_id: str, _items):
        return True, {"items": []}

    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )

    with pytest.raises(HTTPException) as exc_info:
        await module.create_new_order(
            _build_quote_order_request(),
            BackgroundTasks(),
            current_user={"user_id": "test"},
            precomputed_quote_requirement=(False, None),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Merchant not found"


@pytest.mark.asyncio
async def test_create_new_order_parallel_early_reads_inventory_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    inventory_items = [{"product": "Test Product", "requested": 2, "available": 1}]

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id, "psp_connected": True}

    async def fake_check_inventory_availability(_merchant_id: str, _items):
        return False, {"items": inventory_items}

    async def fail_quote_load(self, *, quote_id: str):
        raise AssertionError("quote load should stay behind inventory validation")

    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )
    monkeypatch.setattr(
        module.QuoteService, "load_active_quote_or_raise", fail_quote_load
    )

    with pytest.raises(HTTPException) as exc_info:
        await module.create_new_order(
            _build_quote_order_request(),
            BackgroundTasks(),
            current_user={"user_id": "test"},
            precomputed_quote_requirement=(False, None),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "message": "Insufficient inventory",
        "items": inventory_items,
    }


@pytest.mark.asyncio
async def test_create_new_order_parallel_early_reads_quote_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id, "psp_connected": True}

    async def fake_check_inventory_availability(_merchant_id: str, _items):
        return True, {"items": []}

    async def fake_load_active_quote_or_raise(self, *, quote_id: str):
        raise QuoteError("QUOTE_NOT_FOUND", "Quote not found", debug_id="debug_missing")

    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )
    monkeypatch.setattr(
        module.QuoteService,
        "load_active_quote_or_raise",
        fake_load_active_quote_or_raise,
    )

    with pytest.raises(HTTPException) as exc_info:
        await module.create_new_order(
            _build_quote_order_request(),
            BackgroundTasks(),
            current_user={"user_id": "test"},
            precomputed_quote_requirement=(False, None),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "error": "QUOTE_NOT_FOUND",
        "message": "Quote not found",
        "debug_id": "debug_missing",
    }


@pytest.mark.asyncio
async def test_create_new_order_parallel_early_reads_prioritizes_merchant_over_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return None

    async def fake_check_inventory_availability(_merchant_id: str, _items):
        return False, {
            "items": [{"product": "Test Product", "requested": 2, "available": 1}]
        }

    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )

    with pytest.raises(HTTPException) as exc_info:
        await module.create_new_order(
            _build_quote_order_request(),
            BackgroundTasks(),
            current_user={"user_id": "test"},
            precomputed_quote_requirement=(False, None),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Merchant not found"
