from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from models.catalog import OfferNode, PivotPricing
from services.offer_classification import (
    OFFER_TYPE_BRAND_DIRECT,
    classify_offer_type,
    is_first_party_track,
    select_best_us_offer,
)


def test_external_referral_is_unknown_not_first_party():
    # Fix Plan C: the lane cannot name the seller (brand-D2C and marketplace
    # mirror both ingest as external_referral), so offer_type is None ("unknown")
    # here — the brand-vs-retailer verdict is domain-derived + stored elsewhere.
    # We never guess 'retailer' from the lane. Still never first-party.
    assert classify_offer_type("external_referral") is None
    assert is_first_party_track("external_referral") is False


def test_internal_merchant_is_first_party():
    assert is_first_party_track("internal_merchant") is True


def test_internal_brand_direct_requires_verified_flag():
    # Unverified internal merchant -> unknown, never assumed brand_direct.
    assert classify_offer_type("internal_merchant") is None
    assert classify_offer_type("internal_merchant", brand_relationship="") is None
    assert classify_offer_type("internal_merchant", brand_relationship="reseller") is None
    # Verified -> brand_direct (case-insensitive).
    assert (
        classify_offer_type("internal_merchant", brand_relationship="brand_direct")
        == OFFER_TYPE_BRAND_DIRECT
    )
    assert (
        classify_offer_type("internal_merchant", brand_relationship="Brand_Direct")
        == OFFER_TYPE_BRAND_DIRECT
    )


def _offer(offer_id, price, *, market="US", availability="in_stock", first_party=True):
    return OfferNode(
        offer_id=offer_id,
        catalog_track="internal_merchant",
        truth_tier="primary",
        readiness_tier="commerce_ready",
        offer_mode="merchant_checkout",
        availability=availability,
        market=market,
        is_first_party=first_party,
        pricing=PivotPricing(merchant_effective_price=Decimal(str(price))),
    )


def test_best_us_offer_picks_lowest_priced_buyable():
    offers = [
        _offer("a", 30),
        _offer("b", 20),
        _offer("c", 25),
    ]
    assert select_best_us_offer(offers).offer_id == "b"


def test_best_us_offer_excludes_non_us_and_unbuyable():
    offers = [
        _offer("eu", 10, market="EU"),
        _offer("oos", 12, availability="out_of_stock"),
        _offer("us", 22),
    ]
    assert select_best_us_offer(offers).offer_id == "us"


def test_best_us_offer_tiebreaks_to_first_party():
    third_party = _offer("retail", 20, first_party=False)
    direct = _offer("direct", 20, first_party=True)
    assert select_best_us_offer([third_party, direct]).offer_id == "direct"


def test_best_us_offer_none_when_nothing_qualifies():
    assert select_best_us_offer([]) is None
    assert select_best_us_offer([_offer("eu", 10, market="EU")]) is None
    # Priced-but-unknown availability still counts; unpriced does not.
    unpriced = OfferNode(
        offer_id="np",
        catalog_track="internal_merchant",
        truth_tier="primary",
        readiness_tier="commerce_ready",
        offer_mode="merchant_checkout",
        market="US",
        is_first_party=True,
    )
    assert select_best_us_offer([unpriced]) is None
