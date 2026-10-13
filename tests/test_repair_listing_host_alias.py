"""scripts/repair_same_title_listings.py host aliases: one listing, two spellings of its host, two live offers.

offer_id hashes the URL as spelled, so a store crawled as `www.<host>` and as `<host>` kept two live offers of the
same page on one shelf (prod 2026-10-13: one brand store, every row it re-crawled). The row's own spelling is kept.
"""
from scripts.repair_same_title_listings import ALIAS_REASON, plan_alias, plan_row, summary

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
          offer("o_sg_www", WWW, market="SG"),                # another market's
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
    [g] = plan_row(row(), [offer("o_v2_www", WWW, sku="::v:2")], [], market="US")
    assert g["skipped"] == "alias_alone_on_its_shelf" and g["suppress"] == []


def test_the_other_market_is_left_to_its_own_run():
    [g] = plan_row(row(), OFFERS, [], market="SG")
    assert g["suppress"] == [] and g["alias_kept_alone"] == ["o_sg_www"]
