"""Fix Plan C — table-driven tests for services.offer_seller_identity.

Lesson (ADR-009): SQL-shape tests miss NULL / 3-valued-logic. We test BEHAVIOUR:
every branch + explicit None-domain / None-brand / None-official cases + the
contaminated-official-domain guard (prod had official_domain=ulta.com on genuine
Ulta offers)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.offer_seller_identity import (  # noqa: E402
    brand_owns_domain,
    derive_offer_seller_identity,
    domain_name_label,
    domains_match,
    is_known_retailer,
    normalize_host,
    registrable_base,
)


@pytest.mark.parametrize("value,expected", [
    ("https://www.tomfordbeauty.com/products/x", "tomfordbeauty.com"),
    ("WWW.COSRX.COM", "cosrx.com"),
    ("us.nuxe.com:443", "us.nuxe.com"),
    ("wholesale.publicgoods.com", "wholesale.publicgoods.com"),
    ("ulta.com/", "ulta.com"),
    ("", None),
    (None, None),
])
def test_normalize_host(value, expected):
    assert normalize_host(value) == expected


@pytest.mark.parametrize("host,base,label", [
    ("www.tomfordbeauty.com", "tomfordbeauty.com", "tomfordbeauty"),
    ("us.nuxe.com", "nuxe.com", "nuxe"),
    ("dearbarber.co.uk", "dearbarber.co.uk", "dearbarber"),
    ("wholesale.publicgoods.com", "publicgoods.com", "publicgoods"),
    ("reddane.co.za", "reddane.co.za", "reddane"),
])
def test_registrable_base_and_label(host, base, label):
    assert registrable_base(host) == base
    assert domain_name_label(host) == label


@pytest.mark.parametrize("a,b,expected", [
    ("www.tomfordbeauty.com", "tomfordbeauty.com", True),
    ("us.nuxe.com", "nuxe.com", True),
    ("wholesale.publicgoods.com", "publicgoods.com", True),
    ("shop.example.co.uk", "www.example.co.uk", True),
    ("ulta.com", "fentybeauty.com", False),
    ("notulta.com", "ulta.com", False),
    (None, "ulta.com", False),
    ("ulta.com", None, False),
])
def test_domains_match(a, b, expected):
    assert domains_match(a, b) is expected


@pytest.mark.parametrize("host,expected", [
    ("ulta.com", True),
    ("shop.ulta.com", True),
    ("amazon.co.uk", True),
    ("amzn.to", True),
    ("global.oliveyoung.com", True),
    ("bestbuy.com", True),
    ("fentybeauty.com", False),
    ("notulta.com", False),
])
def test_is_known_retailer(host, expected):
    assert is_known_retailer(host) is expected


def test_is_known_retailer_csv_extendable():
    assert is_known_retailer("coupang.com") is False
    assert is_known_retailer("coupang.com", "coupang.com") is True


@pytest.mark.parametrize("brand,host,expected", [
    ("Tom Ford Beauty", "www.tomfordbeauty.com", True),
    ("COSRX", "cosrx.com", True),
    ("Public Goods", "wholesale.publicgoods.com", True),
    ("Fenty Beauty", "ulta.com", False),
    ("Anua", "anua.com", True),
    ("", "anything.com", False),
    (None, "anything.com", False),
    ("Naturium", None, False),
])
def test_brand_owns_domain(brand, host, expected):
    assert brand_owns_domain(brand, host) is expected


def test_official_domain_match_brand_direct():
    r = derive_offer_seller_identity(domain="fentybeauty.com", official_domain="fentybeauty.com", brand="Fenty Beauty")
    assert r["offer_type"] == "brand_direct" and r["is_first_party"] is True
    assert r["rule"] == "official_domain_match"


def test_official_domain_match_tolerates_locale_prefix():
    r = derive_offer_seller_identity(domain="us.nuxe.com", official_domain="nuxe.com", brand="Nuxe")
    assert r["offer_type"] == "brand_direct" and r["is_first_party"] is True


def test_known_retailer_when_official_is_brand_site():
    r = derive_offer_seller_identity(domain="ulta.com", official_domain="fentybeauty.com", brand="Fenty Beauty")
    assert r["offer_type"] == "retailer" and r["is_first_party"] is False
    assert r["rule"] == "known_retailer_domain"


def test_retailer_host_preempts_contaminated_official_domain():
    # prod: official_domain wrongly = ulta.com on genuine Ulta offers (The Ordinary).
    r = derive_offer_seller_identity(domain="ulta.com", official_domain="ulta.com", brand="The Ordinary")
    assert r["offer_type"] == "retailer" and r["is_first_party"] is False
    assert r["rule"] == "known_retailer_domain"


def test_brand_in_domain_without_identity():
    r = derive_offer_seller_identity(domain="cosrx.com", brand="COSRX")
    assert r["offer_type"] == "brand_direct" and r["rule"] == "brand_in_domain"


def test_brand_wholesale_subdomain_is_brand_direct_not_retailer():
    r = derive_offer_seller_identity(domain="wholesale.publicgoods.com", official_domain="publicgoods.com", brand="Public Goods")
    assert r["offer_type"] == "brand_direct" and r["is_first_party"] is True


def test_unknown_domain_no_evidence_is_none():
    r = derive_offer_seller_identity(domain="somerandommarketplace.com", brand="Acme")
    assert r["offer_type"] is None and r["rule"] == "unknown_no_evidence"


def test_none_domain_and_none_canonical_is_unknown_no_domain():
    r = derive_offer_seller_identity(domain=None, canonical_url=None, official_domain="brand.com", brand="Brand")
    assert r["offer_type"] is None and r["rule"] == "unknown_no_domain"


def test_none_domain_but_canonical_present_derives_from_host():
    r = derive_offer_seller_identity(domain=None, canonical_url="https://roundlab.com/products/toner",
                                     official_domain="roundlab.com", brand="Round Lab")
    assert r["offer_type"] == "brand_direct" and r["rule"] == "official_domain_match"


def test_none_brand_none_official_retailer_domain_is_retailer():
    r = derive_offer_seller_identity(domain="target.com", official_domain=None, brand=None)
    assert r["offer_type"] == "retailer"


def test_none_brand_none_official_unknown_domain_is_none():
    r = derive_offer_seller_identity(domain="mystery-host.io", official_domain=None, brand=None)
    assert r["offer_type"] is None and r["rule"] == "unknown_no_evidence"


def test_all_none_args_do_not_raise():
    r = derive_offer_seller_identity()
    assert r["offer_type"] is None and r["rule"] == "unknown_no_domain"
