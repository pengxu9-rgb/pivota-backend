"""scripts/repair_same_title_listings.py host aliases: one listing, two spellings of its host, two live offers.

offer_id hashes the URL as spelled, so a store crawled as `www.<host>` and as `<host>` kept two live offers of the
same page on one shelf (prod 2026-10-13: one brand store, every row it re-crawled). The row's own spelling is kept.
"""
import argparse
from unittest.mock import AsyncMock

import pytest

import scripts.repair_same_title_listings as tool
from scripts.repair_same_title_listings import (ALIAS_REASON, apply_refusal, plan_alias, plan_digest, plan_row,
                                                summary)
from services.retailer_ingest.shopify_markets import sibling_offer_id

KEY = "ext:store-brand-base-duo::88888888"
APEX, WWW = "https://store.example/products/base-duo", "https://www.store.example/products/base-duo"


def row(url=APEX):
    return {"product_key": KEY, "content_key": "ck_88888888", "canonical_url": url, "image_url": url + ".jpg"}


def offer(offer_id, url, *, sku="::canonical", market="US", suppressed=False):
    return {"offer_id": offer_id, "product_key": KEY, "sku_key": KEY + sku, "channel": "default", "market": market,
            "source_ref": url, "destination_url": url, "canonical_url": url, "availability": "in_stock",
            "suppressed_at": "2026-10-01" if suppressed else None, "suppression_reason": "x" if suppressed else None,
            "suppression_metadata": None, "updated_at": None, "price_checked_at": None, "source_variant_id": None,
            "image_url": url + ".jpg"}


def sibling(base, market):
    """A Shopify Markets sibling as shopify_markets writes it: same row and SKU, the base's source_ref."""
    return {**base, "offer_id": sibling_offer_id(base["offer_id"], market), "market": market}


OFFERS = [offer("o_apex", APEX), offer("o_www", WWW),
          offer("o_v_apex", APEX, sku="::v:1"), offer("o_v_www", WWW, sku="::v:1"),
          offer("o_v2_www", WWW, sku="::v:2"),                # alone on its shelf: never the row's last offer there
          offer("o_sg_apex", APEX, market="SG"),              # another market's, at the row's spelling
          offer("o_old_www", WWW, sku="::v:3", suppressed=True)]


def _plan(offers, market):
    groups = plan_row(row(), offers, [], market=market)
    return {"market": market, "hosts": ["store.example"], "rows_read": 1, "groups": groups}


def _apply(offers, groups):
    """What an apply leaves: the planned offers suppressed."""
    gone = {i for g in groups if not g["skipped"] for i in g["suppress"]}
    return [{**o, "suppressed_at": "now"} if o["offer_id"] in gone else o for o in offers]


def test_the_row_s_spelling_is_kept_and_its_twins_at_the_other_spelling_are_suppressed():
    [g] = plan_row(row(), OFFERS, [], market="US")
    assert (g["kind"], g["reason"], g["keeper"], g["left_out"]) == (
        "host_alias", ALIAS_REASON, "store.example", ["www.store.example"])
    assert g["suppress"] == ["o_www", "o_v_www"] and g["alias_kept_alone"] == ["o_v2_www"]
    assert g["restore"] == [] and g["deactivate"] == [] and g["repoint"] is None and g["skipped"] is None
    s = summary({"market": "US", "hosts": [], "rows_read": 1, "groups": [g]})
    assert (s["host_alias_groups"], s["host_alias_offers_to_suppress"], s["host_alias_kept_alone"]) == (1, 2, 1)


def test_a_www_row_keeps_its_www_offers():
    [g] = plan_row(row(WWW), OFFERS, [], market="US")
    assert g["keeper"] == "www.store.example" and g["suppress"] == ["o_apex", "o_v_apex"]
    assert g["alias_kept_alone"] == []


def test_nothing_to_do_without_a_second_spelling_or_a_row_on_this_listing():
    assert plan_row(row(), [offer("o_apex", APEX)], [], market="US") == []
    assert plan_alias(row("https://store.example/products/other"), "store.example", "base-duo", OFFERS,
                      market="US") is None
    assert plan_alias(row("https://us.store.example/products/base-duo"), "store.example", "base-duo", OFFERS,
                      market="US") is None
    [g] = plan_row(row(), [offer("o_apex", APEX), offer("o_v2_www", WWW, sku="::v:2")], [], market="US")
    assert g["skipped"] == "alias_alone_on_its_shelf" and g["suppress"] == []


def test_a_base_whose_sibling_is_live_is_held_and_a_sibling_never_is():
    """Controller review of #2596: a Shopify Markets sibling copies its base's source_ref and is refreshed only while
    the base is live, so suppressing a base strands its live siblings -- suppressing a sibling strands nothing. Only
    the first holds a row; an unrelated other-market offer at the alias spelling never does."""
    sg_www, sg_apex = sibling(OFFERS[1], "SG"), sibling(OFFERS[0], "SG")
    [us] = plan_row(row(), OFFERS + [sg_www, sg_apex], [], market="US")
    assert us["skipped"] == "sibling_market_alias_live" and us["stranded_siblings"] == [sg_www["offer_id"]]
    assert us["other_market_left"] == 1 and us["other_market_alias_live"] == [sg_www["offer_id"]]
    [sg] = plan_row(row(), OFFERS + [sg_www, sg_apex], [], market="SG")
    assert sg["skipped"] is None and sg["suppress"] == [sg_www["offer_id"]] and sg["stranded_siblings"] == []
    stray = offer("o_jp_www", WWW, market="JP")  # not a sibling of anything this plan suppresses
    [ok] = plan_row(row(), OFFERS + [stray], [], market="US")
    assert ok["skipped"] is None and ok["other_market_left"] == 1 and ok["stranded_siblings"] == []


@pytest.mark.parametrize("home, siblings", [("US", ("SG", "AU")), ("AU", ("US", "SG"))])
def test_the_markets_run_in_sequence_and_end_with_every_alias_suppressed(home, siblings):
    """Sibling markets first, the base market last (the order the docstring prescribes): every alias of every market
    ends suppressed and every kept offer stays live. The review's probe -- an AU-home store with US and SG siblings --
    held every market while the skip counted any other-market alias."""
    bases = [offer("b_apex", APEX, market=home), offer("b_www", WWW, market=home),
             offer("b_v_apex", APEX, sku="::v:1", market=home), offer("b_v_www", WWW, sku="::v:1", market=home)]
    offers = bases + [sibling(b, m) for b in bases for m in siblings]
    assert _plan(offers, home)["groups"][0]["skipped"] == "sibling_market_alias_live"  # base first: held
    for market in (*siblings, home):
        p = _plan(offers, market)
        assert [g["skipped"] for g in p["groups"]] == [None], market
        offers = _apply(offers, p["groups"])
    live = [o["source_ref"] for o in offers if o["suppressed_at"] is None]
    assert set(live) == {APEX} and len(live) == 2 * (1 + len(siblings))


def test_a_folded_shade_s_own_listing_is_not_a_left_out_listing():
    """The extended fold points a variant's offer at the listing it is sold on: that page carries no `::canonical`
    offer and no seed of the row, so it is not a second listing to suppress (controller review of #2593)."""
    child = "https://store.example/products/base-duo-light"
    offers = [offer("o_apex", APEX), offer("o_v_apex", APEX, sku="::v:1"), offer("o_v_child", child, sku="::v:2")]
    seeds = [{"id": "s_apex", "canonical_url": APEX, "destination_url": APEX, "status": "active",
              "availability": "in_stock", "destination_verdict": "live", "market": "US", "updated_at": None,
              "image_url": APEX + ".jpg"}]
    assert plan_row(row(), offers, seeds, market="US") == []
    # a real second listing (its own `::canonical` offer) is still one
    [g] = plan_row(row(), offers + [offer("o_child", child)], seeds, market="US")
    assert g["left_out"] == ["base-duo-light"] and sorted(g["suppress"]) == ["o_child", "o_v_child"]


REVIEWED = _plan(OFFERS, "US")
DIGEST = plan_digest(REVIEWED)


def _args(**kw):
    return argparse.Namespace(**{"host": ["store.example"], "max_groups": 1, "expect_digest": DIGEST, **kw})


def test_an_apply_is_bound_to_the_reviewed_dry_run():
    s = summary(REVIEWED)
    assert s["plan_digest"] == DIGEST and apply_refusal(_args(), s) is None
    assert "exactly one --host" in apply_refusal(_args(host=[]), s)
    assert "exactly one --host" in apply_refusal(_args(host=["store.example", "other.example"]), s)
    assert "--max-groups" in apply_refusal(_args(max_groups=None), s)
    assert "--expect-digest" in apply_refusal(_args(expect_digest=None), s)
    assert "exceeds --max-groups 0" in apply_refusal(_args(max_groups=0), s)


def test_any_change_since_the_dry_run_changes_the_digest():
    """A plan that shrank, grew, or holds other offers or an extra restore at the same count is refused."""
    v3 = [offer("o_v3_apex", APEX, sku="::v:3"), offer("o_v3_www", WWW, sku="::v:3")]
    shrunk = _plan([o for o in OFFERS if o["offer_id"] != "o_v_www"], "US")
    grown = _plan(OFFERS + v3, "US")
    same_count = _plan([o for o in OFFERS if o["offer_id"] != "o_v_www"] + v3, "US")
    assert summary(same_count)["offers_to_suppress"] == summary(REVIEWED)["offers_to_suppress"]
    restored = {**REVIEWED, "groups": [{**REVIEWED["groups"][0], "restore": [{"offer_id": "o_extra", "price_checked_at": None}]}]}
    for changed in (shrunk, grown, same_count, restored):
        s = summary(changed)
        assert s["plan_digest"] != DIGEST
        assert "is not the reviewed dry run's" in apply_refusal(_args(), s)


def test_the_cli_refuses_before_writing(monkeypatch):
    """run() wiring: a refused --apply raises before apply() is reached; a matching one applies."""
    class _Db:
        connect = AsyncMock()
        disconnect = AsyncMock()

    applied = AsyncMock(return_value={"run_id": "samelisting_x"})
    monkeypatch.setattr(tool, "database", _Db())
    monkeypatch.setattr(tool, "plan", AsyncMock(return_value=REVIEWED))
    monkeypatch.setattr(tool, "apply", applied)
    with pytest.raises(SystemExit, match="refused: plan_digest .* nothing written"):
        tool.main(["--host", "store.example", "--max-groups", "1", "--expect-digest", "0" * 16, "--apply"])
    with pytest.raises(SystemExit, match="refused: --apply needs exactly one --host"):
        tool.main(["--max-groups", "1", "--expect-digest", DIGEST, "--apply"])
    assert applied.await_count == 0
    assert tool.main(["--host", "store.example", "--max-groups", "1", "--expect-digest", DIGEST, "--apply"]) == 0
    assert applied.await_count == 1
