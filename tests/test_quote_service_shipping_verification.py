from decimal import Decimal

import pytest

from services.quote_service import QuoteError, QuoteService
from services.shopify_pricing_service import ShopifyPricingResult


@pytest.mark.asyncio
async def test_preview_quote_fails_closed_when_storefront_shipping_is_unverified(monkeypatch):
    svc = QuoteService()

    async def fake_preview_cart_quote(**_kwargs):
        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref="cart_unverified",
            currency="USD",
            pricing={
                "subtotal": Decimal("1.69"),
                "discount_total": Decimal("0.60"),
                "shipping_fee": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "total": Decimal("1.09"),
            },
            promotion_lines=[],
            line_items=[],
            delivery_options=None,
            debug={
                "debug_id": "dbg_unverified",
                "checkout_url": "https://example.com/checkout",
                "discount_evidence": {
                    "shipping_evidence": {
                        "status": "unverified",
                        "reason": "shipping_rates_unavailable_for_shippable_lines",
                        "delivery_groups_count": 0,
                        "delivery_options_count": 0,
                    }
                },
            },
            discount_evidence={
                "shipping_evidence": {
                    "status": "unverified",
                    "reason": "shipping_rates_unavailable_for_shippable_lines",
                    "delivery_groups_count": 0,
                    "delivery_options_count": 0,
                }
            },
        )

    # Admin-checkout fallback was removed (audit fix #7): Storefront is the ONLY
    # pricing engine, so there is no fallback engine to guard against here.
    monkeypatch.setattr(svc.pricing_storefront, "preview_cart_quote", fake_preview_cart_quote)

    with pytest.raises(QuoteError) as exc:
        await svc.preview_quote(
            merchant_id="m_test",
            agent_id="agent_test",
            items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
            discount_codes=["SAVE"],
            customer_email="buyer@example.com",
            shipping_address={
                "country": "CA",
                "postal_code": "M4W 1A5",
                "city": "Toronto",
                "state": "ON",
            },
            selected_delivery_option=None,
        )

    assert exc.value.code == "QUOTE_SHIPPING_UNVERIFIED"
    assert exc.value.debug_id == "dbg_unverified"
    assert exc.value.details["shipping_evidence"]["reason"] == "shipping_rates_unavailable_for_shippable_lines"
    assert exc.value.details["shipping_evidence"]["delivery_options_count"] == 0


@pytest.mark.asyncio
async def test_preview_quote_keeps_authoritative_zero_shipping_quotes(monkeypatch):
    svc = QuoteService()

    async def fake_preview_cart_quote(**_kwargs):
        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref="cart_authoritative",
            currency="USD",
            pricing={
                "subtotal": Decimal("1.69"),
                "discount_total": Decimal("0.60"),
                "shipping_fee": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "total": Decimal("1.09"),
            },
            promotion_lines=[],
            line_items=[
                {
                    "product_id": "p1",
                    "variant_id": "v1",
                    "quantity": 1,
                    "unit_price_original": Decimal("1.69"),
                    "unit_price_effective": Decimal("1.09"),
                    "line_discount_total": Decimal("0.60"),
                    "compare_at_savings": Decimal("0.00"),
                }
            ],
            delivery_options=[
                {
                    "handle": "free",
                    "title": "Free shipping",
                    "estimatedCost": {"amount": "0.00", "currencyCode": "USD"},
                    "delivery_group_id": "gid://shopify/CartDeliveryGroup/1",
                }
            ],
            debug={
                "debug_id": "dbg_authoritative",
                "checkout_url": "https://example.com/checkout",
                "selected_delivery_option": {
                    "handle": "free",
                    "estimatedCost": {"amount": "0.00", "currencyCode": "USD"},
                },
                "discount_evidence": {
                    "shipping_evidence": {
                        "status": "authoritative",
                        "reason": None,
                        "delivery_groups_count": 1,
                        "delivery_options_count": 1,
                    }
                },
            },
            discount_evidence={
                "shipping_evidence": {
                    "status": "authoritative",
                    "reason": None,
                    "delivery_groups_count": 1,
                    "delivery_options_count": 1,
                }
            },
        )

    async def noop_insert_quote(_row):
        return None

    # Admin-checkout fallback was removed (audit fix #7); Storefront is the only engine.
    monkeypatch.setattr(svc.pricing_storefront, "preview_cart_quote", fake_preview_cart_quote)
    monkeypatch.setattr("services.quote_service.insert_quote", noop_insert_quote)

    result = await svc.preview_quote(
        merchant_id="m_test",
        agent_id="agent_test",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
        discount_codes=["SAVE"],
        customer_email="buyer@example.com",
        shipping_address={
            "country": "US",
            "postal_code": "94105",
            "city": "San Francisco",
            "state": "CA",
        },
        selected_delivery_option=None,
    )

    assert result["pricing"]["shipping_fee"] == Decimal("0.00")
    assert result["pricing"]["total"] == Decimal("1.09")
    assert len(result["delivery_options"] or []) == 1
