from __future__ import annotations

import pytest

from models.order import OrderItem


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
