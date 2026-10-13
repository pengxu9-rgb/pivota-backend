"""scripts/repair_same_title_listings.py host aliases: one listing, two spellings of its host, two live offers.

offer_id hashes the URL as spelled, so a store crawled as `www.<host>` and as `<host>` kept two live offers of the
same page on one shelf (prod 2026-10-13: one brand store, every row it re-crawled). The row's own spelling is kept.
"""
import argparse

from scripts.repair_same_title_listings import ALIAS_REASON, apply_refusal, plan_alias, plan_row, summary

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


OFFERS = [offer("o_apex", APEX), offer("o_www", WWW),
          offer("o_v_apex", APEX, sku="::v:1"), offer("o_v_www", WWW, sku="::v:1"),
          offer("o_v2_www", WWW, sku="::v:2"),                # alone on its shelf: never the row's last offer there
          offer("o_sg_apex", APEX, market="SG"),              # another market's, at the row's spelling
          offer("o_old_www", WWW, sku="::v:3", suppressed=True)]


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


def test_the_other_market_is_left_to_its_own_run():
    [g] = plan_row(row(), OFFERS + [offer("o_sg_www", WWW, market="SG")], [], market="SG")
    assert g["suppress"] == ["o_sg_www"] and g["other_market_alias_live"] == ["o_www", "o_v_www", "o_v2_www"]
    assert g["skipped"] == "other_market_alias_live"  # the US aliases are still live: plan both markets together


def test_a_live_sibling_in_another_market_at_the_alias_spelling_skips_the_row():
    """A Shopify Markets sibling copies its base offer's source_ref and is refreshed only while the base is live:
    suppressing the US base would leave the SG price served and never refreshed (controller review of #2593)."""
    [g] = plan_row(row(), OFFERS + [offer("o_sg_www", WWW, market="SG")], [], market="US")
    assert g["skipped"] == "other_market_alias_live" and g["other_market_alias_live"] == ["o_sg_www"]
    assert g["other_market_left"] == 1
    s = summary({"market": "US", "hosts": [], "rows_read": 1, "groups": [g]})
    assert s["offers_to_suppress"] == 0 and s["skipped"] == {"other_market_alias_live": 1}
    [ok] = plan_row(row(), OFFERS + [offer("o_sg_www", WWW, market="SG", suppressed=True)], [], market="US")
    assert ok["skipped"] is None and ok["other_market_left"] == 0


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


def _args(**kw):
    return argparse.Namespace(**{"host": ["store.example"], "max_groups": 30, "expect_suppress": 207, **kw})


def test_an_apply_is_bound_to_the_reviewed_dry_run():
    s = {"groups": 31, "skipped": {"alias_alone_on_its_shelf": 1}, "offers_to_suppress": 207}
    assert apply_refusal(_args(), s) is None
    assert "--host" in apply_refusal(_args(host=[]), s)
    assert "--max-groups" in apply_refusal(_args(max_groups=None), s)
    assert "--expect-suppress" in apply_refusal(_args(expect_suppress=None), s)
    assert "exceeds --max-groups 29" in apply_refusal(_args(max_groups=29), s)
    assert "reviewed dry run said 200" in apply_refusal(_args(expect_suppress=200), s)
