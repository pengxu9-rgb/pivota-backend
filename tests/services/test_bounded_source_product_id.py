"""A brand-store product's source_product_id fits its VARCHAR(128) column, however long the title.

Measured 2026-09-26 on kissusa.com (job rij_ab4b17dc): the title below slugs to a 134-char
source_product_id; its INSERT failed on the column and the one missing product (of 362) failed the
whole store job as partial. Built here by the real producers: shopify_product_to_record, then
ingest_validated_record.
"""
from __future__ import annotations

import pytest

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import (
    SOURCE_PRODUCT_ID_MAX,
    bounded_source_product_id,
    canonical_product_name,
    derive_product_key,
    ingest_validated_record,
)

LONG = ("Kiss Professional Full Cover Press On Fake Toenails - Tippy Toes | 130 Toenails, Includes Nail Glue, "
        "Solid, White, Short, Squoval, Pedicure")


def _kiss_record(title):
    variants = [{"id": 45199259336800 + i, "title": f"Shade {i}", "price": "9.99", "available": True,
                 "sku": f"KPT{i:02d}"} for i in range(11)]
    return feed.shopify_product_to_record(
        {"id": 8123456789, "vendor": "Kiss", "title": title, "handle": "kiss-professional-full-cover-press-on-toenails-tippy-toes",
         "product_type": "Physical Products", "body_html": "<p>Salon-quality press-on toenails with glue.</p>",
         "images": [{"src": "https://www.kissusa.com/i.jpg"}], "variants": variants},
        domain="www.kissusa.com", category_path="beauty", brand_override="KISS", currency="USD",
        source_role="brand_official", emit_native_variants=True,
    )


def test_the_kiss_title_that_failed_now_fits_every_column():
    assert len(canonical_product_name("KISS", LONG)) > SOURCE_PRODUCT_ID_MAX  # the failing input, pinned
    rec = _kiss_record(LONG)
    assert rec is not None
    plan = ingest_validated_record(rec, source_jsonl="kissusa.jsonl")
    pdp_id = plan["pdp"]["source_product_id"]
    sku_ids = [plan["sku"]["source_product_id"]] + [s["source_product_id"] for s in plan["variant_skus"]]
    assert len(plan["variant_skus"]) == 11
    assert len(pdp_id) <= SOURCE_PRODUCT_ID_MAX
    assert all(len(x) <= SOURCE_PRODUCT_ID_MAX for x in sku_ids)
    assert set(sku_ids) == {pdp_id}  # the product and every SKU agree on the id
    assert all(len(v["source_variant_id"]) <= SOURCE_PRODUCT_ID_MAX for v in plan["variant_skus"])
    # review of #2391: the canonical SKU's source_variant_id restated the 148-char product_key
    assert len(plan["pdp"]["product_key"]) > SOURCE_PRODUCT_ID_MAX
    assert len(plan["sku"]["source_variant_id"]) <= SOURCE_PRODUCT_ID_MAX
    assert plan["sku"]["source_variant_id"] == pdp_id


# A product_key that fits but whose name is 115-128 chars: the canonical SKU failed with NO
# source_product_id overflow at all (e.g. live kissusa.com "Kiss Haunt Halloween ... Glow In The Dark").
MID = ("Kiss Haunt Halloween Press On Fake Glue Nails - Midnight Makeover | French Design, Black & White, "
       "Medium, Almond, Glow In The Dark")


def test_a_key_over_the_column_restates_the_source_id_and_stays_product_derived():
    from services.agent_pdp_view_assembler import aggregate_variants
    from services.outbound_links_service import extract_shopify_numeric_variant_id
    from services.variant_identity import PRODUCT_DERIVED, variant_id_provenance

    assert SOURCE_PRODUCT_ID_MAX < len(derive_product_key("KISS", MID)) and \
        len(canonical_product_name("KISS", MID)) <= SOURCE_PRODUCT_ID_MAX
    plan = ingest_validated_record(_kiss_record(MID), source_jsonl="kissusa.jsonl")
    sku = plan["sku"]
    assert len(sku["source_variant_id"]) <= SOURCE_PRODUCT_ID_MAX
    assert sku["source_variant_id"] == plan["pdp"]["source_product_id"]
    # never a real variant: product-derived, dropped as the placeholder, not a Shopify cart id
    assert variant_id_provenance(sku["source_variant_id"], product_id=sku["source_product_id"],
                                 product_key=sku["product_key"]) == PRODUCT_DERIVED
    variants, _ = aggregate_variants([sku], plan["pdp"]["source_product_id"])
    assert variants == []
    assert extract_shopify_numeric_variant_id(sku["source_variant_id"]) is None


def test_a_key_that_fits_keeps_restating_the_product_key():
    plan = ingest_validated_record(_kiss_record("Kiss Gel Fantasy Press On Glue Nails - Chase It"), source_jsonl="x")
    assert plan["sku"]["source_variant_id"] == plan["pdp"]["product_key"]


def test_short_names_are_byte_identical_so_no_stored_row_moves():
    for brand, name in [("MAC", "Ruby Woo Matte Lipstick"), ("KISS", "Kiss Gel Fantasy Press On Glue Nails - Chase It")]:
        assert bounded_source_product_id(brand, name) == canonical_product_name(brand, name)
    exact = "x" * (SOURCE_PRODUCT_ID_MAX - 2)  # canonical = "b-" + 126 x's = exactly 128
    assert bounded_source_product_id("b", exact) == canonical_product_name("b", exact)


def test_long_names_stay_deterministic_and_distinct():
    a = bounded_source_product_id("KISS", LONG)
    assert a == bounded_source_product_id("KISS", LONG) and len(a) == SOURCE_PRODUCT_ID_MAX
    assert a.startswith(canonical_product_name("KISS", LONG)[:SOURCE_PRODUCT_ID_MAX - 9])
    b = bounded_source_product_id("KISS", LONG.replace("Pedicure", "Manicure"))  # differs only past the cut
    assert a != b


@pytest.mark.parametrize("n", [SOURCE_PRODUCT_ID_MAX + 1, 300, 1000])
def test_any_length_fits(n):
    assert len(bounded_source_product_id("b", "y" * n)) == SOURCE_PRODUCT_ID_MAX
