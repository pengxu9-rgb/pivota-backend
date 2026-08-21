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
    NO_CURRENCY,
    annotate_offer_buyability,
    annotate_offer_nodes,
    expected_currency_for_market,
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

def _offer(market=None, price=None, availability="in_stock", merchant_id="m", currency=None):
    return {"merchant_id": merchant_id, "market": market, "price": price,
            "availability": availability, "currency": currency}


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

def _node(market="US", price=None, availability="in_stock", offer_id="o", currency=None):
    return OfferNode(
        offer_id=offer_id, catalog_track="external_referral", truth_tier="primary",
        readiness_tier="commerce_ready", offer_mode="redirect",
        market=market, availability=availability,
        pricing=PivotPricing(estimated_best_price=price, currency=currency),
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


# --- the buy pick never compares prices across currencies (ADR-024 Phase 0) ---
#
# The defect these cover: the pick was min(pool, key=... float(price)) over a pool
# the cross-border fallback can fill with several currencies, so 4500 (JPY) lost to
# 12 (GBP) as raw floats. Fourth layer of the same cross-unit defect (ingestion,
# read, presentation, selection).

def test_expected_currency_map_has_no_default():
    assert expected_currency_for_market("US") == "USD"
    assert expected_currency_for_market("jp") == "JPY"   # normalized, not case-sensitive
    assert expected_currency_for_market("FR") == "EUR"
    # an unmapped market is an honest None — never a silent USD
    assert expected_currency_for_market("ZZ") is None
    assert expected_currency_for_market(None) is None


def test_cheap_foreign_price_does_not_beat_the_serving_currency_price():
    # both offers carry market='US' (the 433 EUR-priced market='US' rows are real
    # on prod today), so both are domestic and land in ONE pool. GBP 5 must not
    # win against USD 50 for a US buyer: 5 < 50 is not a fact about money here.
    out = annotate_offer_buyability(
        [_offer(market="US", price=50.0, currency="USD", merchant_id="usd_50"),
         _offer(market="US", price=5.0, currency="GBP", merchant_id="gbp_5")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["usd_50"]


def test_jpy_4500_is_not_beaten_by_gbp_12_in_a_cross_border_pool():
    # the exact pair named in ADR-024's Context. No USD anywhere, so the largest
    # single-currency group wins; JPY has two members, GBP one.
    out = annotate_offer_buyability(
        [_offer(market="JP", price=4500.0, currency="JPY", merchant_id="jpy_4500"),
         _offer(market="JP", price=6000.0, currency="JPY", merchant_id="jpy_6000"),
         _offer(market="GB", price=12.0, currency="GBP", merchant_id="gbp_12")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["jpy_4500"]


def test_equal_sized_currency_groups_resolve_by_input_order_not_by_price():
    # 1 JPY + 1 GBP: no group is larger, so the FIRST-SEEN group wins and the
    # numerically smaller GBP price is never consulted.
    out = annotate_offer_buyability(
        [_offer(market="JP", price=4500.0, currency="JPY", merchant_id="jpy_4500"),
         _offer(market="GB", price=12.0, currency="GBP", merchant_id="gbp_12")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["jpy_4500"]


def test_serving_currency_wins_even_when_it_is_the_smaller_group():
    # preference beats size: one USD offer against two GBP ones, US buyer.
    out = annotate_offer_buyability(
        [_offer(market="US", price=99.0, currency="GBP", merchant_id="gbp_a"),
         _offer(market="US", price=98.0, currency="GBP", merchant_id="gbp_b"),
         _offer(market="US", price=120.0, currency="USD", merchant_id="usd")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["usd"]


def test_gb_buyer_prefers_gbp_over_a_larger_usd_group():
    # the map is used, not hardcoded to USD: a GB serving market prefers GBP.
    out = annotate_offer_buyability(
        [_offer(market="GB", price=200.0, currency="USD", merchant_id="usd_a"),
         _offer(market="GB", price=210.0, currency="USD", merchant_id="usd_b"),
         _offer(market="GB", price=190.0, currency="GBP", merchant_id="gbp")],
        "GB",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["gbp"]


def test_unmapped_serving_market_does_not_fall_back_to_usd():
    # 'ZZ' has no expected currency, so the LARGEST group wins — GBP (2) over
    # USD (1). An unmapped market must not quietly become a US market.
    out = annotate_offer_buyability(
        [_offer(market="ZZ", price=200.0, currency="GBP", merchant_id="gbp_a"),
         _offer(market="ZZ", price=210.0, currency="GBP", merchant_id="gbp_b"),
         _offer(market="ZZ", price=5.0, currency="USD", merchant_id="usd_cheap")],
        "ZZ",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["gbp_a"]


def test_currencyless_offers_do_not_merge_into_usd():
    # "no currency stated" is its own partition, not evidence of dollars: the
    # cheap unlabelled offer must not win a USD buyer's pick.
    out = annotate_offer_buyability(
        [_offer(market="US", price=50.0, currency="USD", merchant_id="usd_50"),
         _offer(market="US", price=5.0, currency=None, merchant_id="unlabelled_5")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["usd_50"]


def test_currencyless_offers_group_with_each_other_only():
    # ...and they are still pickable among themselves when nothing else matches:
    # two unlabelled offers outnumber one GBP one for an unmapped market.
    out = annotate_offer_buyability(
        [_offer(market="ZZ", price=50.0, currency=None, merchant_id="none_50"),
         _offer(market="ZZ", price=40.0, currency=None, merchant_id="none_40"),
         _offer(market="ZZ", price=1.0, currency="GBP", merchant_id="gbp_1")],
        "ZZ",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["none_40"]
    assert NO_CURRENCY == "(none)"


def test_single_currency_pool_picks_exactly_what_it_picked_before():
    # BYTE-IDENTICAL guard for the overwhelmingly common case (all-USD pool, US
    # market). The expected value is computed with the OLD rule — a bare
    # min() over float(price) with in-stock first — so this fails if the new
    # partitioning perturbs a single-currency pool at all.
    offers = [
        _offer(market="US", price=19.99, currency="USD", availability="out_of_stock", merchant_id="a"),
        _offer(market="US", price=31.50, currency="USD", merchant_id="b"),
        _offer(market="US", price=24.00, currency="USD", merchant_id="c"),
        _offer(market="US", price=24.00, currency="USD", merchant_id="d"),
        _offer(market="US", price=None, currency="USD", merchant_id="e"),
    ]
    old_pool = [o for o in offers if o["price"] is not None]
    old_pick = min(
        old_pool,
        key=lambda o: (o["availability"] != "in_stock", float(o["price"])),
    )["merchant_id"]
    out = annotate_offer_buyability(offers, "US")
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == [old_pick] == ["c"]


def test_in_stock_still_beats_a_cheaper_out_of_stock_within_one_currency():
    out = annotate_offer_buyability(
        [_offer(market="US", price=20.0, currency="USD", availability="out_of_stock", merchant_id="cheap_oos"),
         _offer(market="US", price=25.0, currency="USD", merchant_id="mid_instock")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["mid_instock"]


def test_domestic_still_beats_cross_border_before_currency_is_consulted():
    # the market gate runs FIRST and is unchanged: a domestic USD offer wins over
    # a cross-border USD one even though the cross-border one is cheaper.
    out = annotate_offer_buyability(
        [_offer(market="KR", price=25.0, currency="USD", merchant_id="kr_usd"),
         _offer(market="US", price=30.0, currency="USD", merchant_id="us_usd")],
        "US",
    )
    us = next(o for o in out if o["merchant_id"] == "us_usd")
    kr = next(o for o in out if o["merchant_id"] == "kr_usd")
    assert us["market_availability"] == MARKET_DOMESTIC and us["is_buy_pick"] is True
    assert kr["market_availability"] == MARKET_CROSS_BORDER and kr["is_buy_pick"] is False


# --- node path: same rule, currency read off .pricing.currency ---------------

def test_nodes_cheap_foreign_price_does_not_beat_the_serving_currency_price():
    nodes = annotate_offer_nodes(
        [_node(market="US", price=50.0, currency="USD", offer_id="usd_50"),
         _node(market="US", price=5.0, currency="GBP", offer_id="gbp_5")],
        "US",
    )
    assert [n.offer_id for n in nodes if n.is_buy_pick] == ["usd_50"]


def test_nodes_jpy_is_not_beaten_by_gbp_in_a_cross_border_pool():
    # the live pivot/UCP lane shape: annotate_offer_nodes(item.offers, request.market)
    nodes = annotate_offer_nodes(
        [_node(market="JP", price=4500.0, currency="JPY", offer_id="jpy_4500"),
         _node(market="JP", price=6000.0, currency="JPY", offer_id="jpy_6000"),
         _node(market="GB", price=12.0, currency="GBP", offer_id="gbp_12")],
        "US",
    )
    assert [n.offer_id for n in nodes if n.is_buy_pick] == ["jpy_4500"]


def test_nodes_currencyless_pricing_does_not_merge_into_usd():
    nodes = annotate_offer_nodes(
        [_node(market="US", price=50.0, currency="USD", offer_id="usd_50"),
         _node(market="US", price=5.0, currency=None, offer_id="unlabelled_5")],
        "US",
    )
    assert [n.offer_id for n in nodes if n.is_buy_pick] == ["usd_50"]


def test_nodes_single_currency_pool_unchanged():
    nodes = annotate_offer_nodes(
        [_node(market="US", price=19.99, currency="USD", availability="out_of_stock", offer_id="a"),
         _node(market="US", price=31.50, currency="USD", offer_id="b"),
         _node(market="US", price=24.00, currency="USD", offer_id="c")],
        "US",
    )
    assert [n.offer_id for n in nodes if n.is_buy_pick] == ["c"]


def test_nodes_in_stock_still_first_within_one_currency():
    nodes = annotate_offer_nodes(
        [_node(market="US", price=20.0, currency="USD", availability="out_of_stock", offer_id="cheap_oos"),
         _node(market="US", price=25.0, currency="USD", offer_id="mid_instock")],
        "US",
    )
    assert [n.offer_id for n in nodes if n.is_buy_pick] == ["mid_instock"]


def test_currency_partitioning_is_case_and_whitespace_insensitive():
    # ' usd ' and 'Usd' are the same currency, and both are the SERVING currency —
    # deliberately spelled so that no offer carries the exact key 'USD'. Without
    # normalization they split into two one-member groups, no group matches the
    # expected currency at all, and the pick falls to the larger JPY group.
    out = annotate_offer_buyability(
        [_offer(market="US", price=90.0, currency=" usd ", merchant_id="usd_90"),
         _offer(market="US", price=80.0, currency="Usd", merchant_id="usd_80"),
         _offer(market="US", price=1.0, currency="JPY", merchant_id="jpy_1"),
         _offer(market="US", price=2.0, currency="JPY", merchant_id="jpy_2"),
         _offer(market="US", price=3.0, currency="JPY", merchant_id="jpy_3")],
        "US",
    )
    assert [o["merchant_id"] for o in out if o["is_buy_pick"]] == ["usd_80"]
