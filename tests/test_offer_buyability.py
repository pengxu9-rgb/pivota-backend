"""Market-aware buyability: a cross-border brand-direct offer isn't a same-market
buy; the buy pick is the cheapest in-market offer. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.offer_buyability import (  # noqa: E402
    annotate_offer_buyability,
    is_buyable_in,
)


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
