"""Fix Plan C read-path twin #2 — the external-seed FALLBACK lane.

`_build_external_item_from_candidate` serves rows straight from
external_product_seeds (bypassing catalog_offers), so its OfferNode never
inherited a domain-derived offer_type. It used to hardcode
offer_type='retailer' / is_first_party=False for EVERY seed — mislabeling
brand-D2C seeds as third-party retailers. These tests pin the fix: the
served offer is classified by the seed's OWN domain via the shared
services/offer_seller_identity.py derivation.
"""

from __future__ import annotations

from services.beauty_external_ranking import build_ranked_external_beauty_candidate
from services.pivot_query_service import _build_external_item_from_candidate


def _seed_row(*, canonical_url: str, brand: str, title: str = "Vitamin C Serum") -> dict:
    """Minimal external_product_seeds row. `domain` is the seed's crawled host
    (what fetch_external_seed_rows selects); brand rides in seed_data."""
    return {
        "id": "seed::x1",
        "external_product_id": "x1",
        "title": title,
        "canonical_url": canonical_url,
        "destination_url": canonical_url,
        "domain": canonical_url.split("/")[2],
        "price_amount": 24.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "market": "US",
        "seed_data": {"title": title, "brand": brand, "market": "US"},
    }


def _offer_for(*, canonical_url: str, brand: str):
    candidate = build_ranked_external_beauty_candidate(
        _seed_row(canonical_url=canonical_url, brand=brand), source_order=0
    )
    item = _build_external_item_from_candidate(candidate, query="vitamin c serum")
    assert item.offers, "external item must carry exactly one fallback offer"
    return item.offers[0]


def test_known_retailer_host_seed_is_retailer():
    # Seed crawled from a marketplace -> retailer, never first-party/official.
    offer = _offer_for(canonical_url="https://www.ulta.com/p/123", brand="Some Brand")
    assert offer.offer_type == "retailer"
    assert offer.is_first_party is False
    assert offer.official_source is False


def test_brand_owned_host_seed_is_brand_direct_and_official():
    # Seed crawled from the brand's own storefront (brand token in the domain)
    # -> brand_direct, first-party, official_source (the trust signal). This is
    # the exact case the old blanket 'retailer' hardcode got wrong.
    offer = _offer_for(canonical_url="https://roundlab.com/products/toner", brand="Round Lab")
    assert offer.offer_type == "brand_direct"
    assert offer.is_first_party is True
    assert offer.official_source is True


def test_ambiguous_host_seed_is_unknown_not_guessed():
    # No retailer match, brand token absent from the host -> honest unknown.
    # We do NOT fall back to 'retailer' (nor to 'brand_direct').
    offer = _offer_for(canonical_url="https://shop-example-store.com/p/9", brand="Round Lab")
    assert offer.offer_type is None
    assert offer.is_first_party is False
    assert offer.official_source is False


def test_offer_stays_referral_track_and_redirect_mode():
    # Deriving seller identity must not disturb the fulfillment shape: an
    # external seed is still a redirect-fulfilled external_referral offer.
    offer = _offer_for(canonical_url="https://roundlab.com/products/toner", brand="Round Lab")
    assert offer.catalog_track == "external_referral"
    assert offer.offer_mode == "redirect"
    assert offer.source_system == "external_product_seeds"
