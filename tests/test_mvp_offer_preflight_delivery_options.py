from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mvp.offer import preflight_offer
from mvp.schemas import Geo, OfferConstraints, OfferObject, OfferPricing, Orderability, ProductRef, QuoteRef


def test_preflight_warns_when_delivery_options_missing_but_geo_present():
    now = datetime.now(timezone.utc)
    offer = OfferObject(
        offer_id="offer_test",
        merchant_id="merch_1",
        product_ref=ProductRef(platform="shopify", platform_product_id="p1", variant_id="v1"),
        geo=Geo(country="DE", postal_code="10115", city="Berlin", state="BE"),
        pricing=OfferPricing(currency="EUR", subtotal=10.0, discount_total=0.0, shipping_fee=0.0, tax=0.0, total=10.0),
        delivery_options=[],
        orderability=Orderability(status="unknown", reasons=[]),
        quote_ref=QuoteRef(
            quote_id="q_1",
            expires_at=now + timedelta(minutes=10),
            engine="shopify_storefront_cart",
            engine_ref="gid://shopify/Cart/test?key=123",
        ),
        constraints=OfferConstraints(requires_hil=False),
    )

    res = preflight_offer(offer=offer, policy_hashes_available=True, now=now)
    assert res.status == "warn"
    assert res.checks.delivery.status == "warn"
    assert "delivery_options_unavailable" in (res.checks.delivery.reason_codes or [])
    assert "delivery_options_unavailable" in (res.warnings or [])
    assert any((opt or {}).get("type") == "CHECKOUT_FOR_FINAL_SHIPPING" for opt in (res.fallback_options or []))

