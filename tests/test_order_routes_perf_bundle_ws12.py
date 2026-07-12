from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks

from models.order import CreateOrderRequest


def _request_items(*, variant_id: str = "var_ws12") -> list[dict]:
    return [{"product_id": "prod_ws12", "variant_id": variant_id, "quantity": 1}]


def _build_order_request(*, variant_id: str = "var_ws12") -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id="merch_ws12",
        customer_email="buyer@example.com",
        quote_id="q_ws12",
        items=_request_items(variant_id=variant_id),
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


def _quote_snapshot(
    *,
    created_at: datetime | None = None,
    items: list[dict] | None = None,
) -> SimpleNamespace:
    quote_items = items or _request_items()
    return SimpleNamespace(
        quote_id="q_ws12",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        created_at=created_at,
        engine="shopify_storefront_cart",
        engine_ref="cart_ws12",
        request_fingerprint="fp_ws12",
        quote_hash_sha256="h" * 64,
        debug_id="debug_ws12",
        merchant_id="merch_ws12",
        request_json={
            "items": quote_items,
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
                    "product_id": quote_items[0]["product_id"],
                    "variant_id": quote_items[0]["variant_id"],
                    "quantity": quote_items[0]["quantity"],
                    "title": "Test Product",
                    "unit_price_original": "10.00",
                    "unit_price_effective": "10.00",
                    "line_discount_total": "0.00",
                }
            ],
        },
    )


async def _noop_async(*_args, **_kwargs):
    return None


async def _drain_background_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _capture_module_warnings(monkeypatch: pytest.MonkeyPatch, module) -> list[str]:
    warnings: list[str] = []
    original_warning = module.logger.warning

    def capture_warning(message, *args, **kwargs):
        try:
            warnings.append(str(message) % args if args else str(message))
        except Exception:
            warnings.append(str(message))
        return original_warning(message, *args, **kwargs)

    monkeypatch.setattr(module.logger, "warning", capture_warning)
    return warnings


def _install_order_create_harness(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    quote: SimpleNamespace | None = None,
    payment_success: bool = False,
) -> dict:
    captured: dict = {}
    quote = quote or _quote_snapshot()

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id}

    async def fake_check_inventory(_merchant_id: str, _items):
        return True, {"items": []}

    async def fake_select_psp(
        self, *, agent_id: str, merchant_id: str, amount: float, currency: str
    ):
        return "stripe", {"route_id": "route_ws12"}

    async def fake_resolve_active_order_psp(_merchant_id: str, _provider_hint, **_kwargs):
        return "stripe", "psp_ws12"

    async def fake_ensure_explicit_preferred_psp_available(**_kwargs):
        return None

    async def fake_create_order(order_data):
        captured["order_data"] = order_data
        return "ORD_WS12"

    async def fake_load_active_quote_or_raise(self, *, quote_id: str):
        captured.setdefault("loaded_quote_ids", []).append(quote_id)
        return quote

    async def fake_validate_quote_snapshot_live(
        self,
        quote_arg,
        *,
        customer_email: str | None = None,
        create_replacement_quote_on_mismatch: bool = False,
    ):
        captured.setdefault("validated_quotes", []).append(quote_arg.quote_id)
        return {"status": "validated", "engine": "shopify_storefront_cart"}

    async def fake_create_payment_with_failover(*_args, **_kwargs):
        if payment_success:
            return True, SimpleNamespace(id="pi_ws12", client_secret="cs_ws12"), None, "stripe"
        return False, None, "payment skipped", "stripe"

    monkeypatch.setenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(module, "ensure_database_ready", _noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "check_inventory_availability", fake_check_inventory)
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(module, "_resolve_active_order_psp", fake_resolve_active_order_psp)
    monkeypatch.setattr(
        module,
        "_ensure_explicit_preferred_psp_available",
        fake_ensure_explicit_preferred_psp_available,
    )
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "get_primary_store", _noop_async)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", _noop_async)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", _noop_async)
    monkeypatch.setattr(module, "log_order_event", _noop_async)
    monkeypatch.setattr(
        module,
        "emit_payment_offer_analytics_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(module, "update_payment_info", _noop_async)
    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", fake_load_active_quote_or_raise)
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", fake_validate_quote_snapshot_live)
    monkeypatch.setattr(module.QuoteService, "consume_quote_best_effort", _noop_async)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_kwargs: "fp_ws12")
    return captured


@pytest.mark.asyncio
async def test_order_created_log_event_failure_is_backgrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    warnings = _capture_module_warnings(monkeypatch, module)
    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    _install_order_create_harness(monkeypatch, module, payment_success=True)

    async def failing_log_order_event(**_kwargs):
        raise RuntimeError("boom-log")

    monkeypatch.setattr(module, "log_order_event", failing_log_order_event)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    assert response.payment_status == "awaiting_payment"
    assert any("background log_order_event failed: boom-log" in message for message in warnings)


@pytest.mark.asyncio
async def test_merchant_webhook_failure_is_backgrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    warnings = _capture_module_warnings(monkeypatch, module)
    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    _install_order_create_harness(monkeypatch, module)

    async def failing_emit_merchant_webhook_event(*_args, **_kwargs):
        raise RuntimeError("boom-webhook")

    monkeypatch.setattr(module, "emit_merchant_webhook_event", failing_emit_merchant_webhook_event)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    assert response.metadata["pricing_quote"]["quote_id"] == "q_ws12"
    assert any(
        "background emit_merchant_webhook_event failed: boom-webhook" in message
        for message in warnings
    )


@pytest.mark.asyncio
async def test_consume_quote_best_effort_failure_is_backgrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    warnings = _capture_module_warnings(monkeypatch, module)
    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    _install_order_create_harness(monkeypatch, module)

    async def failing_consume_quote_best_effort(
        self,
        quote_id: str,
        *,
        order_id: str | None = None,
    ):
        raise RuntimeError("boom-consume")

    monkeypatch.setattr(
        module.QuoteService,
        "consume_quote_best_effort",
        failing_consume_quote_best_effort,
    )

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    assert any(
        "background consume_quote_best_effort failed: boom-consume" in message
        for message in warnings
    )


@pytest.mark.asyncio
async def test_precomputed_loaded_quote_skips_inner_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    precomputed_quote = _quote_snapshot()
    _install_order_create_harness(monkeypatch, module, quote=precomputed_quote)
    load_mock = AsyncMock(return_value=precomputed_quote)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", load_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
        precomputed_loaded_quote=precomputed_quote,
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    load_mock.assert_not_called()


@pytest.mark.asyncio
async def test_default_loaded_quote_calls_inner_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    quote = _quote_snapshot()
    _install_order_create_harness(monkeypatch, module, quote=quote)
    load_mock = AsyncMock(return_value=quote)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", load_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    assert load_mock.await_count == 1


@pytest.mark.asyncio
async def test_precomputed_store_info_skips_inner_primary_store_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    _install_order_create_harness(monkeypatch, module)
    get_primary_store_mock = AsyncMock(return_value={"platform": "shopify", "store_id": "fresh_store"})
    monkeypatch.setattr(module, "get_primary_store", get_primary_store_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
        precomputed_store_info={"platform": "shopify", "store_id": "precomputed_store"},
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    get_primary_store_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_store_info_calls_inner_primary_store_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    _install_order_create_harness(monkeypatch, module)
    get_primary_store_mock = AsyncMock(return_value={"platform": "shopify", "store_id": "fresh_store"})
    monkeypatch.setattr(module, "get_primary_store", get_primary_store_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert response.order_id == "ORD_WS12"
    get_primary_store_mock.assert_awaited_once_with("merch_ws12")


@pytest.mark.asyncio
async def test_fresh_unchanged_quote_skips_live_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "30")
    quote = _quote_snapshot(created_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    _install_order_create_harness(monkeypatch, module, quote=quote)
    validate_mock = AsyncMock(return_value={"status": "validated"})
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", validate_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    validate_mock.assert_not_called()
    live_validation = response.metadata["pricing_quote"]["live_validation"]
    assert live_validation["validated_via"] == "skip_fresh_quote"
    assert live_validation["items_unchanged"] is True


@pytest.mark.asyncio
async def test_stale_quote_runs_live_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "30")
    quote = _quote_snapshot(created_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    _install_order_create_harness(monkeypatch, module, quote=quote)
    validate_mock = AsyncMock(return_value={"status": "validated", "validated_via": "live"})
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", validate_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert validate_mock.await_count == 1
    assert response.metadata["pricing_quote"]["live_validation"]["validated_via"] == "live"


@pytest.mark.asyncio
async def test_fresh_quote_with_changed_items_runs_live_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "30")
    quote = _quote_snapshot(created_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    _install_order_create_harness(monkeypatch, module, quote=quote)
    validate_mock = AsyncMock(return_value={"status": "validated", "validated_via": "live"})
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", validate_mock)

    response = await module.create_new_order(
        _build_order_request(variant_id="var_changed"),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert validate_mock.await_count == 1
    assert response.metadata["pricing_quote"]["live_validation"]["validated_via"] == "live"


@pytest.mark.asyncio
async def test_fresh_quote_skip_disabled_by_env_runs_live_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "0")
    quote = _quote_snapshot(created_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    _install_order_create_harness(monkeypatch, module, quote=quote)
    validate_mock = AsyncMock(return_value={"status": "validated", "validated_via": "live"})
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", validate_mock)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={"user_id": "test"},
        precomputed_quote_requirement=(False, None),
    )
    await _drain_background_tasks()

    assert validate_mock.await_count == 1
    assert response.metadata["pricing_quote"]["live_validation"]["validated_via"] == "live"
