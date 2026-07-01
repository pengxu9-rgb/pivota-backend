"""Market-aware buyability: each offer is domestic or cross_border against the
serving market (never a hard "unavailable" — we lack ships_to data and market is
~100% a US default, so a market-equality gate would erase the catalog for foreign
buyers). The buy pick prefers domestic, falls back to a flagged cross_border.
Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.catalog import OfferNode, PivotPricing  # noqa: E402
from services.offer_buyability import (  # noqa: E402
    MARKET_CROSS_BORDER,
    MARKET_DOMESTIC,
    annotate_offer_buyability,
    annotate_offer_nodes,
    offer_market_availability,
)


# --- core rule -------------------------------------------------------------

def test_same_market_is_domestic():
    assert offer_market_availability("US", "US") == MARKET_DOMESTIC


def test_foreign_market_is_cross_border():
    assert offer_market_availability("KR", "US") == MARKET_CROSS_BORDER


def test_us_offer_to_foreign_buyer_is_cross_border_not_erased():
    # the Korean-buyer case: a US-listed product to a KR buyer is cross-border
    # (possibly shippable) — NOT unavailable.
    assert offer_market_availability("US", "KR") == MARKET_CROSS_BORDER


def test_blank_market_assumes_default_market():
    assert offer_market_availability(None, "US") == MARKET_DOMESTIC   # US-default catalog, US buyer
    assert offer_market_availability("", "KR") == MARKET_CROSS_BORDER  # probably a US listing, KR buyer


def test_case_insensitive():
    assert offer_market_availability("us", "US") == MARKET_DOMESTIC


# --- dict path (agent_pdp_view.offers) -------------------------------------

def _offer(market=None, price=None, availability="in_stock", merchant_id="m"):
    return {"merchant_id": merchant_id, "market": market, "price": price, "availability": availability}


def test_cross_border_only_still_has_a_buy_pick():
    # ANUKO today: only a KRW/KR offer served to US -> cross_border, but it IS the
    # buy pick (flagged) rather than "nothing to buy".
    out = annotate_offer_buyability([_offer(market="KR", price=26900.0)], "US")
    assert out[0]["market_availability"] == MARKET_CROSS_BORDER
    assert out[0]["is_buy_pick"] is True


def test_domestic_offer_wins_the_buy_pick_over_cross_border():
    out = annotate_offer_buyability(
        [_offer(market="KR", price=25.0, merchant_id="brand_kr"),
         _offer(market="US", price=30.0, merchant_id="retailer_us")],
        "US",
    )
    kr = next(o for o in out if o["merchant_id"] == "brand_kr")
    us = next(o for o in out if o["merchant_id"] == "retailer_us")
    # domestic US is the pick even though the KR offer is cheaper
    assert us["market_availability"] == MARKET_DOMESTIC and us["is_buy_pick"] is True
    assert kr["market_availability"] == MARKET_CROSS_BORDER and kr["is_buy_pick"] is False


def test_us_default_catalog_not_erased_for_foreign_buyer():
    out = annotate_offer_buyability([_offer(market="US", price=20.0)], "KR")
    assert out[0]["market_availability"] == MARKET_CROSS_BORDER
    assert out[0]["is_buy_pick"] is True  # still buyable (cross-border), not erased


def test_cheapest_in_stock_domestic_pick():
    out = annotate_offer_buyability(
        [_offer(market="US", price=20.0, availability="out_of_stock", merchant_id="cheap_oos"),
         _offer(market="US", price=25.0, merchant_id="mid_instock")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["mid_instock"]


# --- node path (OfferNode, search path) ------------------------------------

def _node(market="US", price=None, availability="in_stock", offer_id="o"):
    return OfferNode(
        offer_id=offer_id, catalog_track="external_referral", truth_tier="primary",
        readiness_tier="commerce_ready", offer_mode="redirect",
        market=market, availability=availability,
        pricing=PivotPricing(estimated_best_price=price),
    )


def test_nodes_domestic_preferred_cross_border_fallback():
    nodes = annotate_offer_nodes(
        [_node(market="KR", price=25.0, offer_id="kr"),
         _node(market="US", price=30.0, offer_id="us")],
        "US",
    )
    kr = next(n for n in nodes if n.offer_id == "kr")
    us = next(n for n in nodes if n.offer_id == "us")
    assert us.market_availability == MARKET_DOMESTIC and us.is_buy_pick is True
    assert kr.market_availability == MARKET_CROSS_BORDER and kr.is_buy_pick is False


def test_nodes_cross_border_only_is_the_pick():
    nodes = annotate_offer_nodes([_node(market="KR", price=26900.0, offer_id="kr")], "US")
    assert nodes[0].market_availability == MARKET_CROSS_BORDER
    assert nodes[0].is_buy_pick is True
