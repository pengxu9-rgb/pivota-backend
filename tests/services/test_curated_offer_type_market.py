"""Curated offers must be written SERVABLE: seller type, market and transact mode.

The gateway's catalog_offers arm (routes/agent_shop_gateway.py, `_handle_offers_resolve`)
selects `offer_type = 'retailer' AND offer_mode = 'redirect'`. This lane used to write
offer_type NULL (the column was not even in the INSERT) and offer_mode='external_referral',
so every curated retailer offer landed in the table and was then invisible to the door that
serves offers — measured 2026-09-16 on prod: an ext:-keyed product with 83 catalog_offers
rows returned offer_count 0.
"""
import re

import pytest

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion
from services.catalog_enrichment_agent.apply import _OFFER_UPSERT_SQL, _with_offer_write_defaults
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl


def shopify_product(number=1, *, variants=None):
    return {
        "id": 9000000 + number, "vendor": "A'PIEU", "title": "A'PIEU Honey & Milk Lip Oil (5g)",
        "handle": f"lip-oil-{number}", "product_type": "Lip Oil",
        "body_html": "<p>Ingredients: Water, Glycerin</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": variants or [
            {"id": 45000000000001, "price": "19.00", "available": True, "option1": "5g",
             "sku": "LIP1", "barcode": "8809530070499"},
        ],
    }


def plan_for(*, host, role, brand="A'PIEU", product=None, native_variants=False, **record_kwargs):
    record = feed.shopify_product_to_record(
        product or shopify_product(), domain=host, category_path="beauty/makeup",
        brand_override=brand, currency="USD", source_role=role, retailer_name=host,
        # The canary runs --emit-real-variants, which is what creates the SECOND offer
        # call site this module has to keep consistent.
        emit_native_variants=native_variants,
    )
    record["pdp"].update(record_kwargs)
    return ingest_validated_jsonl([record])


def test_retailer_offers_are_written_with_the_shape_the_serving_arm_selects():
    plan = plan_for(host="eyurs.com", role="retailer")
    assert plan["offers"], "a retailer listing must plan at least one offer"
    for offer in plan["offers"]:
        assert offer["offer_type"] == "retailer"
        assert offer["offer_mode"] == "redirect"
        assert offer["market"] == "US"
        # catalog_track is a different axis and must NOT be collapsed into the mode.
        assert offer["catalog_track"] == "external_referral"


def test_every_offer_of_one_listing_agrees_about_the_seller():
    """Canonical and per-variant offers are built at two separate call sites; a second
    resolution at the variant site is exactly where the seller identity could drift."""
    two_variants = [
        {"id": 45000000000001, "price": "19.00", "available": True, "option1": "5g",
         "sku": "LIP1", "barcode": "8809530070499"},
        {"id": 45000000000002, "price": "21.00", "available": True, "option1": "10g",
         "sku": "LIP2", "barcode": "8809530070505"},
    ]
    plan = plan_for(host="eyurs.com", role="retailer", native_variants=True,
                    product=shopify_product(variants=two_variants))
    assert len(plan["offers"]) >= 2, "per-variant offers should exist alongside the canonical one"
    assert {o["offer_type"] for o in plan["offers"]} == {"retailer"}
    assert {o["offer_mode"] for o in plan["offers"]} == {"redirect"}
    assert {o["market"] for o in plan["offers"]} == {"US"}


def test_a_brand_storefront_is_not_relabelled_a_retailer():
    """Widening must not mint seller claims: only the retailer cohort changes shape."""
    plan = plan_for(host="kosas.com", role="brand_official", brand="Kosas")
    for offer in plan["offers"]:
        assert offer["offer_type"] != "retailer"
        assert offer["offer_mode"] == "external_referral"


@pytest.mark.parametrize("role,domain,brand,expected", [
    ("retailer", "eyurs.com", "A'PIEU", "retailer"),
    ("brand_official", "kosas.com", "Kosas", "brand_direct"),
    # No positive evidence either way: the helper refuses to guess, and so must we —
    # a fabricated 'brand_direct' here would label a third party's listing as the brand's.
    ("brand_official", "unrelated-shop.example", "A'PIEU", None),
])
def test_offer_type_follows_the_evidence(role, domain, brand, expected):
    assert ingestion.resolve_offer_type({
        "source_role": role, "source_domain": domain, "brand": brand,
        "canonical_url": f"https://{domain}/products/x",
    }) == expected


def test_an_explicit_caller_value_wins():
    assert ingestion.resolve_offer_type({
        "offer_type": "marketplace", "source_role": "retailer", "source_domain": "eyurs.com",
    }) == "marketplace"


def test_the_writer_binds_every_column_it_names():
    """A column added to the INSERT without the row carrying it fails the whole batch at
    execute time — and the plan is built far from the SQL, so nothing else couples them."""
    binds = set(re.findall(r":([a-z_]+)", _OFFER_UPSERT_SQL))
    offer = plan_for(host="eyurs.com", role="retailer")["offers"][0]
    missing = binds - set(offer)
    assert not missing, f"_OFFER_UPSERT_SQL binds names no planned offer row carries: {missing}"
    assert {"offer_type", "market"} <= binds, "the servable-shape columns must be written"


def test_rows_from_other_producers_still_satisfy_the_new_binds():
    """Defaults are NULL, not a guess: an unknown seller type stays unknown."""
    legacy = [{"offer_id": "o1", "sku_key": "s1", "product_key": "p1"}]
    filled = _with_offer_write_defaults(legacy)
    assert filled[0]["offer_type"] is None and filled[0]["market"] is None
    # An already-decided value is never overwritten by the default.
    typed = _with_offer_write_defaults([{"offer_type": "retailer", "market": "KR"}])[0]
    assert typed["offer_type"] == "retailer" and typed["market"] == "KR"


def test_reingest_backfills_a_null_but_never_clobbers_a_decided_value():
    """ON CONFLICT must not let this plan's view overwrite a type another lane decided."""
    update_clause = _OFFER_UPSERT_SQL.split("DO UPDATE SET", 1)[1]
    assert "offer_type = COALESCE(catalog_offers.offer_type, EXCLUDED.offer_type)" in update_clause
    assert "market = COALESCE(catalog_offers.market, EXCLUDED.market)" in update_clause
