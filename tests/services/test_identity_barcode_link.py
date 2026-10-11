"""The F2 backfill proposer (services/identity_barcode_link.py, scripts/propose_variant_barcode_links.py).

Rows are what BARCODE_ROWS_SQL returns for catalog rows the REAL producer wrote: plans from
curated_brand_feed.shopify_product_to_record -> ingestion.ingest_validated_jsonl (shared with the
intake matrix), stored as the gated apply stores them, then flattened once per way each product
carries a barcode (product gtin, or a SKU's raw barcode). The SQL itself runs on Postgres in
tests/test_intake_identity_barcode_lookup_postgres.py.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import identity_barcode_link as link
from services.identity_brand_link import member_key
from tests.services.test_intake_identity_variant_barcode import (
    OPI_REUSED,
    SHALIMAR,
    SHALIMAR_16,
    SHALIMAR_30,
    FakeCatalog,
    _variant,
    gtin13,
    plan_for,
)


def shalimar_family():
    return [_variant("1.0 oz EDP Spray", SHALIMAR), _variant("1.6 oz EDP Spray", SHALIMAR_16),
            _variant("3.0 oz EDP Spray", SHALIMAR_30)]


def sql_rows(cat: FakeCatalog) -> List[Dict[str, Any]]:
    """BARCODE_ROWS_SQL's projection over the fake catalog (before its shared-family filter, which
    build_proposals does not rely on)."""
    out = []
    for p in cat.products.values():
        base = {k: p.get(k) for k in ("product_key", "merchant_id", "platform", "source_product_id",
                                      "source_domain", "brand", "title", "content_key", "gtin", "created_at")}
        if p.get("gtin"):
            out.append({**base, "match_source": "product_gtin", "sku_key": None, "sku_title": None,
                        "code14": p["gtin"].zfill(14)})
        for s in cat.skus:
            if s["product_key"] == p["product_key"] and s.get("barcode"):
                out.append({**base, "match_source": "sku_barcode", "sku_key": s["sku_key"],
                            "sku_title": s["title"], "code14": "".join(s["barcode"].split()).zfill(14)})
    return out


def build(cat: FakeCatalog):
    rows = sql_rows(cat)
    groups = {member_key(p): pg for (m, pl, sp), pg in cat.groups.items()
              for p in [{"merchant_id": m, "platform": pl, "source_product_id": sp}]}
    from_family_keys: Dict[str, List[str]] = {}
    for p in cat.products.values():
        from_family_keys.setdefault(p["content_key"], []).append(p["product_key"])
    group_sizes: Dict[str, int] = {}
    for pg in cat.groups.values():
        group_sizes[pg] = group_sizes.get(pg, 0) + 1
    return link.build_proposals(rows, groups, from_family_keys, group_sizes)


def moves(proposals):
    return [(p["evidence"]["listing_product_key"], p["keeper_product_key"]) for p in proposals]


def test_the_census_pair_proposes_the_newer_listing_onto_the_older_family():
    cat = FakeCatalog()
    pf = cat.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    be = cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    proposals, counts, pairs = build(cat)
    assert [(p["a"]["product_key"], p["b"]["product_key"], p["barcode"]) for p in pairs] == [
        (pf["product_key"], be["product_key"], "03346470113541")]
    assert moves(proposals) == [(be["product_key"], pf["product_key"])]
    p = proposals[0]
    assert (p["kind"], p["strategy"], p["content_key"]) == ("attach_membership", link.STRATEGY, pf["content_key"])
    assert p["evidence"]["barcodes"] == ["03346470113541"]
    assert p["evidence"]["from_content_key"] == be["content_key"]
    assert p["evidence"]["anchored_on_brand_row"] is False
    assert p["evidence"]["same_seller_products_on_family"] == 1
    assert counts["proposed"] == 1 and counts["cross_seller_pairs"] == 1


def test_the_brands_own_row_keeps_the_identity_even_when_newer():
    cat = FakeCatalog()
    be = cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    brand = cat.add(plan_for("guerlain.com", "Shalimar Eau de Parfum", "Guerlain",
                             [_variant("50ml", SHALIMAR_16)], source_role="brand_official"))
    assert not brand["product_key"].startswith("ext:retailer:")
    proposals, _, _ = build(cat)
    assert moves(proposals) == [(be["product_key"], brand["product_key"])]
    assert proposals[0]["evidence"]["anchored_on_brand_row"] is True


def test_two_brand_rows_sharing_a_barcode_propose_nothing():
    cat = FakeCatalog()
    cat.add(plan_for("guerlain.com", "Shalimar Eau de Parfum", "Guerlain", [_variant("50ml", SHALIMAR)],
                     source_role="brand_official"))
    cat.add(plan_for("guerlain.co.uk", "Shalimar EDP", "Guerlain", [_variant("50ml", SHALIMAR)],
                     source_role="brand_official"))
    cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    proposals, counts, _ = build(cat)
    assert proposals == []
    assert counts["component_multiple_anchor_families"] == 1


def test_a_barcode_one_store_puts_on_two_shades_links_nothing():
    """The OPI hazard: universalnailsupplies' shades share 00619828139641; ulta carries it too."""
    cat = FakeCatalog()
    cat.add(plan_for("universalnailsupplies.com", "OPI GelColor - It's A Girl #H39", "OPI",
                     [_variant("Default Title", OPI_REUSED)]))
    cat.add(plan_for("universalnailsupplies.com", "OPI GelColor - Barefoot In Barcelona #E41", "OPI",
                     [_variant("Default Title", OPI_REUSED)]))
    cat.add(plan_for("ulta.com", "GelColor It's A Girl", "OPI", [_variant("Default Title", OPI_REUSED)]))
    proposals, counts, pairs = build(cat)
    assert (proposals, pairs) == ([], [])
    assert counts["barcode_untrusted:titles_differ_within_seller"] == 1


def test_a_barcode_on_two_differently_titled_variants_of_one_product_links_nothing():
    cat = FakeCatalog()
    cat.add(plan_for("universalnailsupplies.com", "Infinite Shine Lacquer", "OPI",
                     [_variant("Big Apple Red", OPI_REUSED), _variant("Malaga Wine", OPI_REUSED)]))
    cat.add(plan_for("ulta.com", "Infinite Shine - Big Apple Red", "OPI", [_variant("Default Title", OPI_REUSED)]))
    proposals, counts, _ = build(cat)
    assert proposals == []
    assert counts["barcode_untrusted:variant_titles_differ_within_product"] == 1


def test_one_seller_splitting_a_barcode_over_two_families_links_nothing():
    """Same title, two keys at one store (a past split): the barcode cannot say which is the product."""
    cat = FakeCatalog()
    cat.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    split = cat.add(plan_for("perfumania.com", "Shalimar Perfume.", "Guerlain",
                             [_variant("Default Title", SHALIMAR)]), content_key="ck_split")
    assert len(cat.products) == 2 and split["content_key"] == "ck_split"
    cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    proposals, counts, _ = build(cat)
    assert proposals == []
    assert counts["barcode_untrusted:families_differ_within_seller"] == 1


@pytest.mark.parametrize("bad", ["0", "00000000000000", "3346470113542"])
def test_an_invalid_or_all_zero_barcode_pairs_nothing(bad):
    cat = FakeCatalog()
    cat.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", bad)]))
    cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                     [_variant("1.0 oz", bad), _variant("1.6 oz", "x")]))
    proposals, counts, pairs = build(cat)
    assert (proposals, pairs) == ([], [])
    assert counts.get("row_invalid_barcode", 0) >= 1


def test_a_listing_that_would_leave_a_row_behind_is_not_proposed():
    """identity_brand_link's rule: another live row on the mover's key would pull it back on the
    next crawl, so the move is skipped."""
    cat = FakeCatalog()
    cat.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    be = cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    cat.add(plan_for("beautybrands.com", "Shalimar by Guerlain for Women", "Guerlain",
                     [_variant("Default Title", None)]), content_key=be["content_key"])
    proposals, counts, pairs = build(cat)
    assert proposals == [] and len(pairs) == 1
    assert counts["rows_left_on_old_content_key"] == 1


def test_a_shade_row_joining_a_family_is_marked_for_the_reviewer():
    """Seller A's family; seller B lists two sizes as separate products. Both B rows join A's family
    (oldest), and the evidence says B ends up with two products on one family."""
    cat = FakeCatalog()
    fam = cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    one = cat.add(plan_for("perfumania.com", "Shalimar EDP 1 oz", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    two = cat.add(plan_for("perfumania.com", "Shalimar EDP 3 oz", "Guerlain",
                           [_variant("Default Title", SHALIMAR_30)]))
    proposals, _, _ = build(cat)
    assert sorted(moves(proposals)) == sorted([(one["product_key"], fam["product_key"]),
                                               (two["product_key"], fam["product_key"])])
    assert [p["evidence"]["same_seller_products_on_family"] for p in proposals] == [2, 2]


def test_the_script_dry_run_never_writes_and_propose_only_upserts(monkeypatch, capsys):
    import asyncio
    import json

    import scripts.propose_variant_barcode_links as script

    cat = FakeCatalog()
    cat.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    cat.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", shalimar_family()))
    built = build(cat)
    written: List[Any] = []

    class Conn:
        async def close(self):
            return None

    async def connect(*a, **k):
        return Conn()

    async def load(conn):
        return built

    async def upsert(conn, proposals):
        written.append(proposals)
        return {"proposed": len(proposals), "inserted": len(proposals), "deduped": 0}

    import asyncpg

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(script, "load_and_build", load)
    monkeypatch.setattr(script, "upsert_proposals", upsert)

    assert asyncio.run(script.main([])) == 0
    out = json.loads(capsys.readouterr().out.split("RESULT ", 1)[1])
    assert written == [] and out["mode"] == "dry_run" and "written" not in out
    assert out["counts"]["proposed"] == 1 and out["counts"]["cross_seller_pairs"] == 1
    assert out["proposal_examples"][0]["listing_title"] == "Shalimar by Guerlain for Women"
    assert out["pair_examples"][0]["barcode"] == "03346470113541"

    assert asyncio.run(script.main(["--propose"])) == 0
    out = json.loads(capsys.readouterr().out.split("RESULT ", 1)[1])
    assert len(written) == 1 and out["written"]["inserted"] == 1
    with pytest.raises(SystemExit):  # there is no apply mode
        asyncio.run(script.main(["--apply"]))
