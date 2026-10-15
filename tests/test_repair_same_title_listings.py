"""scripts/repair_same_title_listings.py same-title half and Shopify Markets siblings, as pure plan_row.

A sibling copies its base offer's source_ref and is refreshed only while that base is live
(shopify_markets.CANDIDATES_SQL): suppressing a left-out listing's base offer strands its live siblings serving a price
nobody refreshes, suppressing a sibling strands nothing. The host-alias half's rule (plan_alias), applied one way.
"""
import pytest

from scripts.repair_same_title_listings import plan_row, summary
from services.retailer_ingest.shopify_markets import sibling_offer_id

KEY = "ext:store-brand-base-duo::99999999"
KEPT, LEFT = "https://store.example/products/base-duo", "https://store.example/products/base-duo-refill"


def row():
    return {"product_key": KEY, "content_key": "ck_99999999", "canonical_url": KEPT, "image_url": KEPT + ".jpg"}


def offer(offer_id, url, *, sku="::canonical", market="US", suppressed=False):
    return {"offer_id": offer_id, "product_key": KEY, "sku_key": KEY + sku, "channel": "default", "market": market,
            "source_ref": url, "destination_url": url, "canonical_url": url, "availability": "in_stock",
            "suppressed_at": "2026-10-01" if suppressed else None, "suppression_reason": "x" if suppressed else None,
            "suppression_metadata": None, "updated_at": None, "price_checked_at": None, "source_variant_id": None,
            "image_url": url + ".jpg"}


def seed(seed_id, url, market="US"):
    return {"id": seed_id, "canonical_url": url, "destination_url": url, "image_url": url + ".jpg",
            "status": "active", "availability": "in_stock", "destination_verdict": "live", "market": market,
            "updated_at": None}


def sibling(base, market):
    """A live Shopify Markets sibling as shopify_markets writes it: same row and SKU, the base's source_ref."""
    return {**base, "offer_id": sibling_offer_id(base["offer_id"], market), "market": market,
            "suppressed_at": None, "suppression_reason": None}


BASES = [offer("o_kept", KEPT), offer("o_left", LEFT), offer("o_v_left", LEFT, sku="::v:1")]
SEEDS = [seed("s_kept", KEPT), seed("s_left", LEFT)]
OLD = offer("o_old_left", LEFT, sku="::v:2", suppressed=True)  # a left-out offer suppressed before this run


def _apply(offers, seeds, groups):
    """What an apply leaves: the planned offers suppressed, the planned seeds inactive."""
    gone = {i for g in groups if not g["skipped"] for i in g["suppress"]}
    off = {i for g in groups if not g["skipped"] for i in g["deactivate"]}
    return ([{**o, "suppressed_at": "now"} if o["offer_id"] in gone else o for o in offers],
            [{**s, "status": "inactive"} if s["id"] in off else s for s in seeds])


def test_a_left_out_base_with_a_live_sibling_holds_the_group_whole():
    sg_left = sibling(BASES[1], "SG")
    [g] = plan_row(row(), BASES + [sg_left], SEEDS, market="US")
    assert g["keeper"] == "base-duo" and g["left_out"] == ["base-duo-refill"]
    assert g["skipped"] == "sibling_market_listing_live" and g["stranded_siblings"] == [sg_left["offer_id"]]
    assert g["suppress"] == ["o_left", "o_v_left"] and g["deactivate"] == ["s_left"]  # listed, never written
    s = summary({"market": "US", "hosts": [], "rows_read": 1, "groups": [g]})
    assert s["skipped"] == {"sibling_market_listing_live": 1}
    assert (s["offers_to_suppress"], s["seeds_to_deactivate"]) == (0, 0)


def test_the_sibling_s_own_market_is_never_held_by_its_base():
    sg_left = sibling(BASES[1], "SG")
    [g] = plan_row(row(), BASES + [sibling(BASES[0], "SG"), sg_left], SEEDS, market="SG")
    assert g["skipped"] is None and g["suppress"] == [sg_left["offer_id"]] and g["stranded_siblings"] == []


@pytest.mark.parametrize("extra", [
    pytest.param(lambda: sibling(BASES[0], "SG"), id="sibling-of-the-keeper"),
    pytest.param(lambda: {**sibling(BASES[1], "SG"), "suppressed_at": "2026-10-02"}, id="suppressed-sibling"),
    pytest.param(lambda: offer("o_jp_left", LEFT, market="JP"), id="unrelated-other-market-offer"),
    pytest.param(lambda: [OLD, sibling(OLD, "SG")], id="sibling-of-a-base-suppressed-before-this-run"),
])
def test_only_a_live_sibling_of_an_offer_this_plan_suppresses_holds(extra):
    """The keeper's siblings stay refreshed; a suppressed sibling serves nothing; an other-market offer that is
    nobody's sibling is that market's own run; a base suppressed before this run is not in its suppress list."""
    more = extra()
    more = more if isinstance(more, list) else [more]
    [g] = plan_row(row(), BASES + more, SEEDS, market="US")
    assert g["skipped"] is None and g["stranded_siblings"] == [], more[-1]["offer_id"]
    assert g["suppress"] == ["o_left", "o_v_left"]


def test_another_skip_reason_keeps_its_name():
    """A group that could never act (its keeper has nothing to sell) is reported as that, not as a sibling hold."""
    offers = [offer("o_kept", KEPT, suppressed=True), BASES[1], sibling(BASES[1], "SG")]
    [g] = plan_row(row(), offers, SEEDS, market="US")
    assert g["skipped"] == "keeper_has_no_sellable_offer" and g["stranded_siblings"] == [offers[2]["offer_id"]]


@pytest.mark.parametrize("home, siblings", [("US", ("SG", "AU")), ("AU", ("US", "SG"))])
def test_the_markets_run_in_sequence_and_end_with_every_left_out_offer_suppressed(home, siblings):
    """Sibling markets first, the base market last: every left-out listing's offers, in every market, end
    suppressed, every kept-listing offer stays live, and the left-out seed goes inactive."""
    bases = [{**b, "market": home} for b in BASES]
    seeds = [{**s, "market": home} for s in SEEDS]
    offers = bases + [sibling(b, m) for b in bases for m in siblings]
    [first] = plan_row(row(), offers, seeds, market=home)
    assert first["skipped"] == "sibling_market_listing_live" and len(first["stranded_siblings"]) == 2 * len(siblings)
    for market in (*siblings, home):
        groups = plan_row(row(), offers, seeds, market=market)
        assert [g["skipped"] for g in groups] == [None], market
        offers, seeds = _apply(offers, seeds, groups)
    assert {o["source_ref"] for o in offers if o["suppressed_at"] is None} == {KEPT}
    assert len([o for o in offers if o["suppressed_at"] is None]) == 1 + len(siblings)
    assert all(o["suppressed_at"] is not None for o in offers if o["source_ref"] == LEFT)
    assert {s["id"]: s["status"] for s in seeds} == {"s_kept": "active", "s_left": "inactive"}
