"""Market-aware buyability: a cross-border brand-direct offer isn't a same-market
buy; the buy pick is the cheapest in-market offer. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.catalog import OfferNode, PivotPricing  # noqa: E402
from services.offer_buyability import (  # noqa: E402
    annotate_offer_buyability,
    annotate_offer_nodes,
    is_buyable_in,
    market_is_buyable,
)


def _node(market="US", price=None, availability="in_stock", offer_id="o"):
    return OfferNode(
        offer_id=offer_id, catalog_track="external_referral", truth_tier="primary",
        readiness_tier="commerce_ready", offer_mode="redirect",
        market=market, availability=availability,
        pricing=PivotPricing(estimated_best_price=price),
    )


def test_market_primitive():
    assert market_is_buyable("US", "US") is True
    assert market_is_buyable("KR", "US") is False
    assert market_is_buyable(None, "US") is True
    assert market_is_buyable("us", "US") is True


def test_annotate_nodes_foreign_only_no_pick():
    nodes = annotate_offer_nodes([_node(market="KR", price=26900.0)], "US")
    assert nodes[0].buyable is False
    assert nodes[0].is_buy_pick is False


def test_annotate_nodes_us_retailer_is_buy_pick():
    nodes = annotate_offer_nodes(
        [_node(market="KR", price=26900.0, offer_id="brand"),
         _node(market="US", price=25.90, offer_id="oy_us")],
        "US",
    )
    brand = next(n for n in nodes if n.offer_id == "brand")
    us = next(n for n in nodes if n.offer_id == "oy_us")
    assert brand.buyable is False and brand.is_buy_pick is False
    assert us.buyable is True and us.is_buy_pick is True


def test_annotate_nodes_cheapest_in_stock_pick():
    nodes = annotate_offer_nodes(
        [_node(market="US", price=20.0, availability="out_of_stock", offer_id="cheap_oos"),
         _node(market="US", price=25.0, offer_id="mid_instock")],
        "US",
    )
    picked = [n.offer_id for n in nodes if n.is_buy_pick]
    assert picked == ["mid_instock"]


def _offer(market=None, price=None, availability="in_stock", merchant_id="m"):
    return {"merchant_id": merchant_id, "market": market, "price": price, "availability": availability}


def test_same_market_is_buyable():
    assert is_buyable_in(_offer(market="US"), "US") is True


def test_foreign_market_is_not_buyable():
    assert is_buyable_in(_offer(market="KR"), "US") is False


def test_blank_market_defaults_buyable():
    # legacy single-market offers (market defaulted/blank) must not regress
    assert is_buyable_in(_offer(market=None), "US") is True
    assert is_buyable_in(_offer(market=""), "US") is True


def test_market_match_is_case_insensitive():
    assert is_buyable_in(_offer(market="us"), "US") is True


def test_brand_direct_only_foreign_has_no_buy_pick():
    # the ANUKO case today: only a KRW/KR offer -> nothing purchasable in US
    out = annotate_offer_buyability([_offer(market="KR", price=26900.0)], "US")
    assert out[0]["buyable"] is False
    assert out[0]["is_buy_pick"] is False


def test_us_retailer_offer_becomes_the_buy_pick():
    offers = [
        _offer(market="KR", price=26900.0, merchant_id="brand_direct"),
        _offer(market="US", price=25.90, merchant_id="oliveyoung_us"),
    ]
    out = annotate_offer_buyability(offers, "US")
    kr = next(o for o in out if o["merchant_id"] == "brand_direct")
    us = next(o for o in out if o["merchant_id"] == "oliveyoung_us")
    assert kr["buyable"] is False and kr["is_buy_pick"] is False
    assert us["buyable"] is True and us["is_buy_pick"] is True


def test_buy_pick_is_cheapest_in_stock_us_offer():
    offers = [
        _offer(market="US", price=30.0, merchant_id="a"),
        _offer(market="US", price=20.0, merchant_id="b", availability="out_of_stock"),
        _offer(market="US", price=25.0, merchant_id="c"),
    ]
    out = annotate_offer_buyability(offers, "US")
    picked = [o["merchant_id"] for o in out if o["is_buy_pick"]]
    assert picked == ["c"]  # cheapest in-stock (b is cheaper but OOS)
