"""Retailers as enrichment: a brand product's MISSING INCI is filled from its linked retailer listings,
and nothing the brand has is ever replaced."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import linked_listing_inci_fill as fill_mod
from services.canonical_inci_intake import INCI_SOURCE_RESELLER, may_write

INCI = "Water, Glycerin, Niacinamide, Butylene Glycol, Panthenol"
BRAND = "prod::external_seed::external_seed::ext_naturium_niacinamide"


def _row(listing, raw=INCI, host="sokoglam.com", brand=BRAND, category="beauty/skincare/treat/serum"):
    return {"brand_product_key": brand, "content_key": "ck_1", "brand_host": "naturium.com",
            "brand_category_path": category,
            "listing_product_key": listing, "listing_host": host, "raw_inci": raw}


def test_an_agreeing_retailer_inci_fills_the_brand_product():
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a"), _row("ext:retailer:b", host="dermstore.com")])
    assert counts == {"fill": 1}
    [f] = fills
    assert f["brand_product_key"] == BRAND and f["raw_inci"] == INCI
    assert f["from_listings"] == ["dermstore.com", "sokoglam.com"] and f["ingredient_count"] == 5


def test_spelling_and_case_differences_are_the_same_list():
    other = "WATER,  glycerin, NIACINAMIDE, butylene glycol, Panthenol"
    assert fill_mod.plan_fills([_row("ext:retailer:a"), _row("ext:retailer:b", raw=other)])[1] == {"fill": 1}


def test_retailers_that_disagree_fill_nothing():
    other = "Water, Glycerin, Niacinamide, Zinc PCA, Panthenol"
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a"), _row("ext:retailer:b", raw=other)])
    assert fills == [] and counts == {"listings_disagree": 1}


def test_a_non_inci_text_is_not_a_fill():
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a", raw="See packaging")])
    assert fills == [] and counts == {"no_valid_listing_inci": 1}


def test_an_invalid_candidate_does_not_block_a_valid_agreeing_one():
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a"), _row("ext:retailer:b", raw="n/a")])
    assert counts == {"fill": 1}


def test_a_retailer_listing_is_never_the_row_filled():
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a", brand="ext:retailer:served")])
    assert fills == [] and counts == {}


def test_the_candidate_query_only_fills_an_empty_served_brand_row():
    sql = " ".join(fill_mod.CANDIDATES_SQL.split())
    assert "JOIN catalog_products w ON w.pivota_signature_id = apv.pivota_signature_id" in sql  # the served row
    assert "w.product_key NOT LIKE 'ext:retailer:%'" in sql and "r.product_key LIKE 'ext:retailer:%'" in sql
    assert "NOT EXISTS (SELECT 1 FROM beauty_sku_ingredients own WHERE own.product_key = w.product_key" in sql
    assert "w.suppressed_at IS NULL" in sql and "r.suppressed_at IS NULL" in sql
    assert "AND w.content_key = apv.content_key" in sql       # an orphan view row cannot pick the target
    assert "AND w.platform = 'external_seed'" in sql          # never a connected merchant's synced row
    own = " ".join(fill_mod.OWN_INCI_SQL.split())
    assert "WHERE product_key = :pk AND coalesce(trim(raw_inci), '') <> ''" in own


def test_the_fill_rank_can_never_replace_the_brands_own_inci_and_is_replaced_by_it():
    assert not may_write(INCI_SOURCE_RESELLER, "brand_official")
    assert not may_write(INCI_SOURCE_RESELLER, "supplier_input")
    assert may_write("pdp_crawl", INCI_SOURCE_RESELLER)       # the brand store's own crawl later wins
    assert may_write("brand_official", INCI_SOURCE_RESELLER)


class _DB:
    def __init__(self, rows, own_inci=False):
        self.rows, self.own_inci = rows, own_inci

    async def fetch_all(self, sql, values=None):
        if "WHERE product_key = :pk" in sql:  # OWN_INCI_SQL, the write-time re-check
            return [{"?column?": 1}] if self.own_inci else []
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_fill_writes_through_the_intake_at_reseller_rank_only_when_applied(monkeypatch, apply):
    calls: List[Dict[str, Any]] = []

    async def intake(pk, raw, source, *, db, dry_run):
        calls.append({"pk": pk, "source": source, "dry_run": dry_run})
        # the real intake reports the would-write skus on a dry run too (canonical_inci_intake)
        return {"status": "ok", "written_skus": ["s1"], "skipped_outranked_skus": [],
                "serving_eligible": None if dry_run else True}

    monkeypatch.setattr(fill_mod, "ingest_canonical_inci", intake)
    out = await fill_mod.fill(_DB([_row("ext:retailer:a")]), apply=apply)
    assert calls == [{"pk": BRAND, "source": INCI_SOURCE_RESELLER, "dry_run": not apply}]
    assert out["counts"] == {"fill": 1} and "raw_inci" not in out["fills"][0]
    assert out["fills"][0]["written_skus"] == 1


@pytest.mark.asyncio
async def test_inci_that_appeared_after_planning_is_never_written_over(monkeypatch):
    """The planning query's NOT EXISTS is not the only guard: the intake lets reseller rank write over any
    rank-1 source, so the brand's own INCI is re-checked at the moment of the write."""
    calls = []

    async def intake(*a, **k):
        calls.append(a)
        return {"status": "ok", "written_skus": ["s1"]}

    monkeypatch.setattr(fill_mod, "ingest_canonical_inci", intake)
    out = await fill_mod.fill(_DB([_row("ext:retailer:a")], own_inci=True), apply=True)
    assert calls == [] and out["fills"] == [] and out["counts"]["brand_has_inci_at_write"] == 1


@pytest.mark.parametrize("category", ["beauty/tools/brush", "beauty/devices/nail", "beauty/makeup/eye/false-lashes",
                                      "beauty/makeup/nails/press-on-nails", "beauty/sets/gift-set", "", None,
                                      "fashion/apparel/tops",
                                      # review of #2395: exact coarse values and prod alias spellings
                                      "beauty/tools", "beauty/devices", "beauty/sets", "beauty/tools/",
                                      "beauty/makeup/eyes/lashes", "beauty/makeup/eyes/false_lashes",
                                      "beauty/makeup/tools/sponge", "beauty/skincare/tools/gua-sha",
                                      "beauty/accessories/makeup-bag", "beauty/skincare/sets",
                                      "beauty/oral-care/teeth-whitening-devices",
                                      # alias-only: no telltale segment until folded (category_path_aliases)
                                      "beauty/skincare/bundle", "beauty/mystery-box",
                                      # re-review: words inside a segment, and a non-beauty prefix
                                      "beauty/accessory/soap_dish", "beauty/bath/accessory/soap-saver",
                                      "beauty/travel/toiletry-bag", "beauty/travel/organizer",
                                      "beauty/skincare/sunscreen/refillable-case", "beautyfoo"])
def test_a_product_without_a_formula_is_never_given_one(category):
    """Measured: shoprescuespa.com's 3-item 'INCI' for Westman Atelier's brushes is a materials list."""
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a", category=category)])
    assert fills == [] and counts == {"category_has_no_formula": 1}


@pytest.mark.parametrize("raw", [
    "Synthetic Fibers, Wood, Aluminum",                    # a brush's materials on a coarse "beauty/makeup" row
    "Key ingredients: Niacinamide, Hyaluronic Acid",       # a label, not the list
    "Made in Korea, Cruelty free, Vegan",                  # claims
    "Water, Glycerin, Niacinamide, Panthenol",             # four: under the minimum
    # long enough, but benefit copy (the crawl lane's prose filter)
    "Boosts hydration, soothes redness, brightens tone, reduces pores, helps the barrier",
    "Key Ingredients: Niacinamide, Hyaluronic Acid, Panthenol, Allantoin, Ceramide NP",
    "Vegan, Cruelty free, Paraben free, Sulfate free, Fragrance free",
])
def test_a_label_claims_or_materials_list_is_not_an_inci(raw):
    fills, counts = fill_mod.plan_fills([_row("ext:retailer:a", raw=raw, category="beauty/makeup")])
    assert fills == [] and counts == {"no_valid_listing_inci": 1}


@pytest.mark.parametrize("category", ["beauty/makeup/face/concealer", "beauty/haircare/general",
                                      "beauty/makeup/nails/nail-polish", "beauty/fragrance/perfume"])
def test_formula_categories_are_filled(category):
    assert fill_mod.plan_fills([_row("ext:retailer:a", category=category)])[1] == {"fill": 1}
