"""services/payment_orchestration/a2a_address_requirements.py: the region and postcode a purchase needs follow its
store's platform in the buyer's country.

Pinned here: every cell of the table, as read from each platform's published rules on 2026-11-27; the
`A2A_RAIL_MERCHANT_PLATFORMS` setting (folded domains, malformed entries and unknown platforms ignored and logged);
a merchant with no platform declared requires nothing beyond the base fields (logged); and the address module's
required list, `fields_missing`, `incomplete_address` and the write follow the table.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.payment_orchestration import a2a_address_requirements as req  # noqa: E402
from services.payment_orchestration import a2a_markets as markets  # noqa: E402
from services.payment_orchestration import a2a_rail  # noqa: E402
from services.payment_orchestration import a2a_shipping_address as shipping  # noqa: E402

SHOP = "store.example"
WOO = "woo.example"
EMAIL = "pat@example.test"
US = {"firstName": "Pat", "lastName": "Example", "phone": "+15125550100", "addressLine1": "1 Example St",
      "city": "Example City", "country": "US"}


@pytest.fixture(autouse=True)
def _platforms(monkeypatch):
    monkeypatch.setattr(req, "_WARNED", set())
    monkeypatch.setenv(req.PLATFORMS_ENV, f"www.{SHOP}:Shopify, {WOO}:woocommerce")
    monkeypatch.delenv(markets.MARKETS_ENV, raising=False)


R, P = req.REGION, req.POSTCODE


@pytest.mark.parametrize("platform,country,required", [
    # Shopify/worldwide: zip_required? and province_optional? per data/regions/<CC>.yml.
    ("shopify", "US", {R, P}), ("shopify", "CA", {R, P}), ("shopify", "AU", {R, P}), ("shopify", "NZ", {P}),
    ("shopify", "SG", {P}), ("shopify", "JP", {R, P}), ("shopify", "KR", {R, P}), ("shopify", "TW", set()),
    ("shopify", "HK", {R}),
    # WooCommerce: get_default_address_fields + get_country_locale + i18n/states.php.
    ("woocommerce", "US", {R, P}), ("woocommerce", "CA", {R, P}), ("woocommerce", "AU", {R, P}),
    ("woocommerce", "NZ", {P}), ("woocommerce", "SG", {P}), ("woocommerce", "JP", {R, P}),
    ("woocommerce", "KR", {P}), ("woocommerce", "TW", {R, P}), ("woocommerce", "HK", {R}),
])
def test_every_cell_of_the_table(platform, country, required):
    assert set(req.REQUIREMENTS[platform][country]) == required


def test_the_table_covers_every_market_the_rail_can_take():
    for platform, countries in req.REQUIREMENTS.items():
        assert set(countries) == set(markets.MARKETS), platform


def test_the_setting_folds_domains_and_ignores_what_it_cannot_read(monkeypatch, caplog):
    assert req.merchant_platform(SHOP) == "shopify" and req.merchant_platform(f"www.{SHOP}") == "shopify"
    assert req.merchant_platform(WOO) == "woocommerce"
    assert req.merchant_platform("other.example") is None and req.merchant_platform(None) is None
    monkeypatch.setenv(req.PLATFORMS_ENV, f"{SHOP}:magento, nocolon, :shopify, {WOO}:woocommerce")
    with caplog.at_level(logging.WARNING, logger=req.__name__):
        assert req.merchant_platforms() == {WOO: "woocommerce"}
    ignored = [r.getMessage() for r in caplog.records if "entry ignored" in r.getMessage()]
    assert len(ignored) == 3 and any("magento" in m for m in ignored)


def test_a_merchant_with_no_platform_requires_nothing_more_and_is_logged_once(caplog):
    with caplog.at_level(logging.WARNING, logger=req.__name__):
        assert req.required_fields("other.example", "US") == frozenset()
        assert req.required_fields("other.example", "US") == frozenset()
    assert len([r for r in caplog.records if "no platform declared" in r.getMessage()]) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=req.__name__):
        assert req.required_fields(None, "US") == frozenset()  # no merchant at all: nothing, nothing logged
    assert caplog.records == []
    assert req.required_fields(SHOP, "GB") == frozenset()  # a country the table does not list


def test_missing_names_only_the_platform_fields_an_address_lacks():
    assert req.missing(SHOP, US) == {R, P}
    assert req.missing(SHOP, {**US, "region": "TX", "postalCode": "78701"}) == frozenset()
    assert req.missing(SHOP, {**US, "region": " "}) == {R, P}
    assert req.missing(WOO, {**US, "country": "KR"}) == {P}
    assert req.missing("other.example", US) == frozenset() and req.missing(SHOP, None) == frozenset()


def test_the_address_module_asks_for_what_the_platform_requires():
    assert shipping.required_address_fields("US", SHOP) == (
        "firstName", "lastName", "phone", "addressLine1", "city", "region", "postalCode", "country")
    assert shipping.required_address_fields("US") == shipping.REQUIRED_ADDRESS_FIELDS  # no merchant: the base
    assert shipping.required_address_fields("HK", SHOP)[-2:] == ("region", "country")
    assert shipping.fields_missing(EMAIL, US, SHOP) == ["region", "postalCode"]
    assert shipping.fields_missing(EMAIL, US) == [] and shipping.fields_missing(EMAIL, US, "other.example") == []
    assert shipping.incomplete_address(US, SHOP) == US and shipping.incomplete_address(US) is None
    complete = {**US, "region": "TX", "postalCode": "78701"}
    assert shipping.incomplete_address(complete, SHOP) is None and not shipping.address_missing(complete, SHOP)
    assert shipping.contact_missing({"buyer_email": EMAIL, "shipping_address": US, "merchant_domain": SHOP})
    assert not shipping.contact_missing({"buyer_email": EMAIL, "shipping_address": US, "merchant_domain": "x.example"})


def test_the_write_requires_the_platform_fields_for_the_purchases_merchant(monkeypatch):
    monkeypatch.setenv(markets.MARKETS_ENV, "US,HK,TW")
    body = {"email": EMAIL, **US}
    read = lambda b, merchant: shipping.read_submission(  # noqa: E731
        b, email_required=True, address_required=True, market=b["country"], merchant_domain=merchant)
    assert read(body, SHOP)[2] == {"region": "required", "postalCode": "required"}
    assert read(body, "other.example")[2] == {}
    assert read({**body, "region": "TX", "postalCode": "78701"}, SHOP)[2] == {}
    hk = {**body, "country": "HK", "phone": "+85221234567"}
    assert read(hk, SHOP)[2] == {"region": "required"}  # Shopify asks for Hong Kong Island / Kowloon / NT
    assert read({**hk, "region": "KL"}, SHOP)[2] == {}  # HK has no ISO subdivisions: the value is the buyer's
    tw = {**body, "country": "TW", "phone": "+886212345678"}
    assert read(tw, SHOP)[2] == {}
    assert read(tw, WOO)[2] == {"region": "required", "postalCode": "required"}


def test_the_relay_shipping_itself_stays_neutral():
    # The relay's field list is the provider's request, not a store rule: the worker checks the platform before
    # building it (a2a_rail_worker._checkout_setup), so checkout_shipping needs neither field.
    shipping_body = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100",
                     "country": "US", "address1": "1 Example St", "city": "Example City"}
    assert a2a_rail.checkout_shipping(shipping_body) == shipping_body
