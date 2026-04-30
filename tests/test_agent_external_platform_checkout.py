from __future__ import annotations

from typing import Any, Dict

import pytest


class _AgentContext:
    agent_id = "agent_external_checkout"

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def can_access_merchant(self, merchant_id: str) -> bool:
        return self.allowed and merchant_id == "merch_external"


def _request(platform_product_id: str = "25"):
    from routes.agent_checkout_intents import CheckoutIntentItem, CreateCheckoutIntentRequest

    return CreateCheckoutIntentRequest(
        items=[
            CheckoutIntentItem(
                merchant_id="merch_external",
                product_id=platform_product_id,
                quantity=2,
                title="External Checkout Product",
                unit_price=19.99,
                currency="USD",
            )
        ],
        source="agent_api",
    )


@pytest.mark.asyncio
async def test_external_platform_checkout_returns_woocommerce_redirect_without_order_or_psp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_checkout_intents as module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module

    captured: Dict[str, Any] = {}

    async def fake_get_primary_store(merchant_id: str):
        assert merchant_id == "merch_external"
        return {"platform": "woocommerce", "domain": "shop.example.com"}

    async def fake_platform_checkout_fallback(**kwargs: Any):
        captured.update(kwargs)
        return {
            "url": "https://shop.example.com/checkout/?add-to-cart=25&quantity=2",
            "platform": "woocommerce",
            "method": "checkout_add_to_cart",
        }

    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes_module, "_get_platform_checkout_fallback_url_best_effort", fake_platform_checkout_fallback)

    result = await module.create_external_platform_checkout(
        _request(),
        context=_AgentContext(),
    )

    assert result["status"] == "requires_external_platform_checkout"
    assert result["checkout_source"] == "external_platform_checkout"
    assert result["checkout_url"] == "https://shop.example.com/checkout/?add-to-cart=25&quantity=2"
    assert result["platform"] == "woocommerce"
    assert result["creates_pivota_order"] is False
    assert result["creates_psp_payment"] is False
    assert result["requires_platform_checkout_validation"] is True
    assert result["inventory_guarantee"] == "not_guaranteed_by_pivota"
    assert result["availability_status"] == "unknown_requires_validation"
    assert result["commerce_path"] == "external_platform_checkout"
    assert result["legacy_or_fallback"] is True
    assert result["validation_authority"] == "merchant_platform_checkout"
    assert result["execution_policy"]["allows_pivota_order"] is False
    assert result["execution_policy"]["allows_psp_creation"] is False
    assert result["execution_policy"]["allows_external_redirect"] is True
    assert result["pricing"]["is_final"] is False
    assert captured["merchant_id"] == "merch_external"
    assert captured["items"][0].product_id == "25"


@pytest.mark.asyncio
async def test_external_platform_checkout_fails_closed_for_wix_without_verified_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_checkout_intents as module
    import services.merchant_store_service as merchant_store_module

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "wix", "domain": "wix-site-id"}

    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.create_external_platform_checkout(
            _request("wix_product_1"),
            context=_AgentContext(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "EXTERNAL_PLATFORM_CHECKOUT_UNSUPPORTED"
    assert exc_info.value.detail["platform"] == "wix"
    assert exc_info.value.detail["commerce_path"] == "unsupported"
    assert exc_info.value.detail["validation_authority"] == "unsupported"


@pytest.mark.asyncio
async def test_external_platform_checkout_fails_closed_when_cart_shape_has_no_safe_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_checkout_intents as module
    import routes.order_routes as order_routes_module
    import services.merchant_store_service as merchant_store_module

    async def fake_get_primary_store(_merchant_id: str):
        return {"platform": "bigcommerce", "domain": "store.example.com"}

    async def fake_platform_checkout_fallback(**_kwargs: Any):
        return None

    monkeypatch.setattr(merchant_store_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(order_routes_module, "_get_platform_checkout_fallback_url_best_effort", fake_platform_checkout_fallback)

    with pytest.raises(module.HTTPException) as exc_info:
        await module.create_external_platform_checkout(
            _request("77"),
            context=_AgentContext(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "EXTERNAL_PLATFORM_CHECKOUT_UNAVAILABLE"
    assert exc_info.value.detail["platform"] == "bigcommerce"
    assert exc_info.value.detail["commerce_path"] == "external_platform_checkout"
