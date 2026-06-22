"""Lock-down regression: a non-deliverable buyer email must NOT take down the Storefront pricing quote.

Root cause (2026-06-21): Shopify Storefront cartCreate rejects buyerIdentity.email with a reserved TLD
(e.g. ``@pivota.test``) as "Email is invalid" -> no cart -> the whole quote 503s
(SHOPIFY_PRICING_UNAVAILABLE). Email is not needed to PRICE a cart, so a bad one is dropped, not fatal.
"""
from services.shopify_storefront_pricing_service import (
    _is_deliverable_buyer_email,
    _shopify_cart_buyer_identity_input,
)


def test_reserved_and_malformed_emails_are_not_deliverable():
    for bad in [
        "x@pivota.test", "a@b.example", "u@h.invalid", "p@q.localhost", "z@w.local",
        "noatsign", "a@b", "a@@b.com", "a b@c.com", "",
    ]:
        assert _is_deliverable_buyer_email(bad) is False, bad


def test_normal_emails_are_deliverable():
    for ok in ["canary-buyer@example.com", "user@pivota.cc", "a.b+tag@gmail.com"]:
        assert _is_deliverable_buyer_email(ok) is True, ok


def test_buyer_identity_drops_invalid_email_but_keeps_delivery():
    bi = _shopify_cart_buyer_identity_input(
        customer_email="canary-buyer@pivota.test",
        country="US", postal="94102", city="SF", province="CA",
        address1="1 Test St", address2=None, use_buyer_country_for_pricing=False,
    )
    assert "email" not in bi  # dropped -> cartCreate won't fail on it
    assert "deliveryAddressPreferences" in bi  # shipping still drives pricing


def test_buyer_identity_keeps_valid_email():
    bi = _shopify_cart_buyer_identity_input(
        customer_email="canary-buyer@example.com",
        country="US", postal="94102", city="SF", province="CA",
        address1="1 Test St", address2=None, use_buyer_country_for_pricing=False,
    )
    assert bi.get("email") == "canary-buyer@example.com"
