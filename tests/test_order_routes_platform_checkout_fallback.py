from __future__ import annotations

import pytest
from fastapi import BackgroundTasks

from models.order import CreateOrderRequest, OrderItem, ShippingAddress


def test_build_woocommerce_checkout_permalink_best_effort_single_item() -> None:
    import routes.order_routes as module

    url = module._build_woocommerce_checkout_permalink_best_effort(
        store_url="shop.example.com",
        items=[OrderItem(product_id="25", quantity=2)],
    )

    assert url == "https://shop.example.com/checkout/?add-to-cart=25&quantity=2"


def test_build_bigcommerce_checkout_permalink_best_effort_single_item() -> None:
    import routes.order_routes as module

    url = module._build_bigcommerce_checkout_permalink_best_effort(
        store_domain="abc123.mybigcommerce.com",
        items=[OrderItem(product_id="77", quantity=1)],
    )

    assert url == "https://abc123.mybigcommerce.com/cart.php?action=buy&product_id=77&qty=1"


def test_build_platform_checkout_fallback_returns_none_for_unsupported_cart_shape() -> None:
    import routes.order_routes as module

    assert (
        module._build_woocommerce_checkout_permalink_best_effort(
            store_url="shop.example.com",
            items=[
                OrderItem(product_id="25", quantity=1),
                OrderItem(product_id="26", quantity=1),
            ],
        )
        is None
    )
    assert (
        module._build_bigcommerce_checkout_permalink_best_effort(
            store_domain="abc123.mybigcommerce.com",
            items=[OrderItem(product_id="77", variant_id="701", quantity=1)],
        )
        is None
    )


@pytest.mark.asyncio
async def test_get_platform_checkout_fallback_dispatches_woocommerce(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as module

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "woocommerce", "domain": "shop.example.com"}

    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)

    payload = await module._get_platform_checkout_fallback_url_best_effort(
        merchant_id="merch_1",
        items=[OrderItem(product_id="25", quantity=2)],
    )

    assert payload == {
        "url": "https://shop.example.com/checkout/?add-to-cart=25&quantity=2",
        "platform": "woocommerce",
        "method": "checkout_add_to_cart",
    }


def _build_order_request() -> CreateOrderRequest:
    return CreateOrderRequest(
        merchant_id="merch_test",
        customer_email="buyer@example.com",
        items=[
            OrderItem(
                product_id="prod_1",
                product_title="Test Product",
                variant_id="var_1",
                quantity=1,
                unit_price="10.00",
                subtotal="10.00",
            )
        ],
        shipping_address=ShippingAddress(
            name="Test Buyer",
            address_line1="1 Test St",
            city="San Francisco",
            state="CA",
            postal_code="94107",
            country="US",
        ),
        currency="USD",
        metadata={},
    )


def _install_create_new_order_harness(
    monkeypatch: pytest.MonkeyPatch,
    module,
    quote_requirement_calls: list[str] | None = None,
) -> list[tuple[str, dict]]:
    import services.quote_first_enforcement as quote_first_enforcement

    events: list[tuple[str, dict]] = []

    async def fake_should_require_quote_for_order_create(*, merchant_id: str):
        if quote_requirement_calls is not None:
            quote_requirement_calls.append(merchant_id)
        return False, {}

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id, "psp_connected": True}

    async def fake_check_inventory_availability(merchant_id: str, items):
        return True, {"items": []}

    async def fake_select_psp(self, *, agent_id: str, merchant_id: str, amount: float, currency: str):
        return "stripe", {"route_id": "route_test"}

    async def fake_resolve_active_order_psp(merchant_id: str, provider_hint: str, **kwargs):
        return "stripe", "psp_test"

    async def fake_create_order(order_data):
        return "ORD_TEST_PLATFORM_FALLBACK"

    async def noop_async(*args, **kwargs):
        return None

    async def fake_log_order_event(*, event_type: str, order_id: str, merchant_id: str, metadata=None, **kwargs):
        events.append((event_type, metadata or {}))
        return None

    monkeypatch.setattr(
        quote_first_enforcement,
        "should_require_quote_for_order_create",
        fake_should_require_quote_for_order_create,
    )
    monkeypatch.setattr(module, "ensure_database_ready", noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(module, "_resolve_active_order_psp", fake_resolve_active_order_psp)
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", noop_async)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", noop_async)
    monkeypatch.setattr(module, "get_primary_store", noop_async)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    return events


@pytest.mark.asyncio
async def test_create_new_order_skips_quote_requirement_call_when_precomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.delenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", raising=False)
    quote_requirement_calls: list[str] = []
    _install_create_new_order_harness(monkeypatch, module, quote_requirement_calls)

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "psp unavailable", "stripe"

    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={},
        precomputed_quote_requirement=(False, {"source": "agent_create_order"}),
    )

    assert response.order_id == "ORD_TEST_PLATFORM_FALLBACK"
    assert quote_requirement_calls == []


@pytest.mark.asyncio
async def test_create_new_order_checks_quote_requirement_without_precomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.delenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", raising=False)
    quote_requirement_calls: list[str] = []
    _install_create_new_order_harness(monkeypatch, module, quote_requirement_calls)

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "psp unavailable", "stripe"

    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={},
    )

    assert response.order_id == "ORD_TEST_PLATFORM_FALLBACK"
    assert quote_requirement_calls == ["merch_test"]


@pytest.mark.asyncio
async def test_create_new_order_skips_platform_checkout_fallback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.delenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", raising=False)
    events = _install_create_new_order_harness(monkeypatch, module)

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "psp unavailable", "stripe"

    async def fail_platform_checkout_fallback(**kwargs):
        raise AssertionError("platform checkout fallback should be disabled by default")

    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(
        module,
        "_get_platform_checkout_fallback_url_best_effort",
        fail_platform_checkout_fallback,
    )

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={},
    )

    assert response.order_id == "ORD_TEST_PLATFORM_FALLBACK"
    assert response.psp == "stripe"
    assert response.payment_intent_id is None
    assert response.client_secret is None
    assert response.payment_action is None
    assert ("payment_intent_failed", {"error": "psp unavailable", "psp_type": "stripe"}) in events
    assert all(event_type != "payment_fallback_platform_checkout" for event_type, _ in events)


@pytest.mark.asyncio
async def test_create_new_order_defers_psp_surface_for_agent_v2_hosted_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.delenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", raising=False)
    events = _install_create_new_order_harness(monkeypatch, module)
    req = _build_order_request()
    req.metadata = {
        "agent_v2": {
            "contract_version": "merchant-network-middleware-v1",
            "checkout_provider": "pivota_hosted_checkout",
            "hosted_checkout": True,
        },
    }

    async def fail_create_payment_with_failover(*args, **kwargs):
        raise AssertionError("hosted checkout order creation must not create a PSP payment surface")

    monkeypatch.setattr(module, "create_payment_with_failover", fail_create_payment_with_failover)

    response = await module.create_new_order(
        req,
        BackgroundTasks(),
        current_user={},
    )

    assert response.order_id == "ORD_TEST_PLATFORM_FALLBACK"
    assert response.payment_status == "pending"
    assert response.payment_intent_id is None
    assert response.client_secret is None
    assert response.payment_action is None
    assert all(event_type != "payment_intent_failed" for event_type, _ in events)
    assert all(event_type != "payment_fallback_platform_checkout" for event_type, _ in events)


@pytest.mark.asyncio
async def test_create_new_order_allows_platform_checkout_fallback_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", "1")
    events = _install_create_new_order_harness(monkeypatch, module)

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "psp unavailable", "stripe"

    async def fake_platform_checkout_fallback(**kwargs):
        return {
            "url": "https://shop.example.com/cart/1:1?discount=CODE",
            "platform": "shopify",
            "method": "cart_permalink",
        }

    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(
        module,
        "_get_platform_checkout_fallback_url_best_effort",
        fake_platform_checkout_fallback,
    )

    response = await module.create_new_order(
        _build_order_request(),
        BackgroundTasks(),
        current_user={},
    )

    assert response.psp == "checkout"
    assert response.client_secret == "https://shop.example.com/cart/1:1?discount=CODE"
    assert response.payment_action.model_dump() == {
        "type": "redirect_url",
        "url": "https://shop.example.com/cart/1:1?discount=CODE",
        "client_secret": None,
        "public_key": None,
        "raw": {
            "reason": "psp_unavailable",
            "error": "psp unavailable",
            "platform": "shopify",
            "method": "cart_permalink",
        },
    }
    assert any(event_type == "payment_fallback_platform_checkout" for event_type, _ in events)


@pytest.mark.asyncio
async def test_create_new_order_blocks_platform_checkout_fallback_for_direct_quote_first_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", "1")
    events = _install_create_new_order_harness(monkeypatch, module)
    req = _build_order_request()
    req.metadata = {
        "commerce_path": "pivota_direct_quote_first",
        "validation_authority": "pivota_live_quote",
        "execution_policy_version": "test",
    }

    async def fake_create_payment_with_failover(*args, **kwargs):
        return False, None, "psp unavailable", "stripe"

    async def fail_platform_checkout_fallback(**kwargs):
        raise AssertionError("direct quote-first order must not call platform checkout fallback")

    monkeypatch.setattr(module, "create_payment_with_failover", fake_create_payment_with_failover)
    monkeypatch.setattr(
        module,
        "_get_platform_checkout_fallback_url_best_effort",
        fail_platform_checkout_fallback,
    )

    response = await module.create_new_order(
        req,
        BackgroundTasks(),
        current_user={},
    )

    assert response.psp == "stripe"
    assert response.client_secret is None
    assert response.payment_action is None
    assert response.commerce_path == "pivota_direct_quote_first"
    assert any(event_type == "fallback_pollution_attempt" for event_type, _ in events)
    assert all(event_type != "payment_fallback_platform_checkout" for event_type, _ in events)


@pytest.mark.asyncio
async def test_create_new_order_checkout_ui_requires_quote_id_even_when_global_quote_requirement_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    req = _build_order_request()
    req.metadata = {"ui_source": "checkout_ui"}

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": _merchant_id, "psp_connected": True}

    async def fake_check_inventory_availability(_merchant_id: str, _items):
        return True, {"items": []}

    async def noop_async(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "ensure_database_ready", noop_async)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(
        module, "check_inventory_availability", fake_check_inventory_availability
    )

    with pytest.raises(module.HTTPException) as exc_info:
        await module.create_new_order(req, BackgroundTasks(), current_user={})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "CHECKOUT_QUOTE_REQUIRED"
