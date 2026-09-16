"""Curated offers must be written SERVABLE: the seller identity and the transact mode.

The gateway's catalog_offers arm (routes/agent_shop_gateway.py, `_handle_offers_resolve`)
selects `offer_type = 'retailer' AND offer_mode = 'redirect'`. This lane wrote offer_type
NULL (the column was not in the INSERT at all) and offer_mode='external_referral', so every
curated retailer offer landed in the table and was then invisible to the door that serves
offers — measured 2026-09-16 on prod: an ext:-keyed product with 83 catalog_offers rows
returned offer_count 0.
"""
import re

import pytest

from db.catalog import catalog_offers
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion
from services.catalog_enrichment_agent.apply import _OFFER_UPSERT_SQL, _with_offer_write_defaults
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl


def shopify_product(number=1, *, variants=None, vendor="A'PIEU"):
    return {
        "id": 9000000 + number, "vendor": vendor, "title": "A'PIEU Honey & Milk Lip Oil (5g)",
        "handle": f"lip-oil-{number}", "product_type": "Lip Oil",
        "body_html": "<p>Ingredients: Water, Glycerin</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": variants or [
            {"id": 45000000000001, "price": "19.00", "available": True, "option1": "5g",
             "sku": "LIP1", "barcode": "8809530070499"},
        ],
    }


def plan_for(*, host, role, brand="A'PIEU", product=None, native_variants=False):
    record = feed.shopify_product_to_record(
        product or shopify_product(), domain=host, category_path="beauty/makeup",
        brand_override=brand, currency="USD", source_role=role, retailer_name=host,
        # The canary runs --emit-real-variants, which creates the SECOND offer call site.
        emit_native_variants=native_variants,
    )
    return ingest_validated_jsonl([record])


def test_retailer_offers_are_written_with_the_shape_the_serving_arm_selects():
    plan = plan_for(host="eyurs.com", role="retailer")
    assert plan["offers"], "a retailer listing must plan at least one offer"
    for offer in plan["offers"]:
        assert offer["offer_type"] == "retailer"
        assert offer["offer_mode"] == "redirect"
        assert offer["is_first_party"] is False
        # catalog_track is a different axis and must NOT be collapsed into the mode.
        assert offer["catalog_track"] == "external_referral"


def test_market_is_left_to_the_column_default():
    """catalog_offers.market is NOT NULL DEFAULT 'US'. Binding it buys no data change and
    costs a way to write NULL into a NOT NULL column — which dropped rows inside their
    per-offer SAVEPOINT and failed 18 real-Postgres tests with 'offers: 0'."""
    offer = plan_for(host="eyurs.com", role="retailer")["offers"][0]
    assert "market" not in offer
    assert "market" not in set(re.findall(r":([a-z_]+)", _OFFER_UPSERT_SQL))
    market_col = catalog_offers.c.market
    assert market_col.nullable is False and market_col.server_default.arg == "US"


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
    assert {o["is_first_party"] for o in plan["offers"]} == {False}


def test_a_brand_storefront_is_not_relabelled_a_retailer():
    """Widening must not mint seller claims: only the retailer cohort changes mode.

    The product's own vendor must BE the brand here: when a storefront's vendor disagrees
    with --brand the lane keeps the product's vendor, and a row branded A'PIEU on
    kosas.com is correctly not brand_direct.
    """
    plan = plan_for(host="kosas.com", role="brand_official", brand="Kosas",
                    product=shopify_product(vendor="Kosas"))
    for offer in plan["offers"]:
        assert offer["offer_type"] == "brand_direct"
        assert offer["offer_mode"] == "external_referral"
        # pivot_query_service derives official_source from this bit; a brand_direct offer
        # written without it is stored as "not official".
        assert offer["is_first_party"] is True


@pytest.mark.parametrize("role,domain,brand,expected_type,expected_first_party", [
    ("retailer", "eyurs.com", "A'PIEU", "retailer", False),
    ("brand_official", "kosas.com", "Kosas", "brand_direct", True),
    # No positive evidence either way: the helper refuses to guess, and so must we —
    # a fabricated 'brand_direct' here would label a third party's listing as the brand's.
    ("brand_official", "unrelated-shop.example", "A'PIEU", None, False),
])
def test_offer_type_follows_the_evidence(role, domain, brand, expected_type, expected_first_party):
    assert ingestion.resolve_offer_type({
        "source_role": role, "source_domain": domain, "brand": brand,
        "canonical_url": f"https://{domain}/products/x",
    }) == {"offer_type": expected_type, "is_first_party": expected_first_party}


def test_a_payload_offer_type_is_not_honoured():
    """_build_pdp_payload is a whitelist carrying neither offer_type nor official_domain, so
    an explicit-value branch would be unreachable from this lane AND would write a value
    outside the brand_direct|retailer vocabulary migration 149 defines."""
    assert ingestion.resolve_offer_type({
        "offer_type": "marketplace", "source_role": "retailer", "source_domain": "eyurs.com",
    })["offer_type"] == "retailer"


def test_the_writer_binds_every_column_it_names():
    """A column added to the INSERT without the row carrying it fails the whole batch at
    execute time — and the plan is built far from the SQL, so nothing else couples them."""
    binds = set(re.findall(r":([a-z_]+)", _OFFER_UPSERT_SQL))
    offer = plan_for(host="eyurs.com", role="retailer")["offers"][0]
    missing = binds - set(_with_offer_write_defaults([dict(offer)])[0])
    assert not missing, f"_OFFER_UPSERT_SQL binds names no planned offer row carries: {missing}"
    assert {"offer_type", "is_first_party"} <= binds, "the servable-shape columns must be written"


def test_columns_and_values_line_up_positionally():
    """Swapping two names in the column list while VALUES keeps its order writes each value
    into the other's column — both fit their types here, so nothing would raise; the row
    would simply carry 'US' as its offer_type."""
    insert = _OFFER_UPSERT_SQL.split("ON CONFLICT", 1)[0]
    columns_src, values_src = insert.split("VALUES", 1)
    columns = re.findall(r"[a-z_]+", columns_src.split("(", 1)[1])
    binds = re.findall(r":([a-z_]+)", values_src)
    assert columns[0] == "offer_id" and len(columns) == len(binds)
    assert columns == binds, "INSERT column order does not match its VALUES binds"


def test_defaults_keep_unknowns_unknown_and_never_replace_a_decision():
    filled = _with_offer_write_defaults([{"offer_id": "o1"}])[0]
    assert filled["offer_type"] is None, "an unknown seller type must stay unknown"
    assert filled["is_first_party"] is False, "an unmade first-party claim is false"
    assert catalog_offers.c.offer_type.nullable is True

    decided = _with_offer_write_defaults([{"offer_type": "retailer", "is_first_party": True}])[0]
    assert decided["offer_type"] == "retailer" and decided["is_first_party"] is True


def test_a_present_but_none_bind_is_coerced_for_the_not_null_column():
    """Producers that mirror the bind list supply every name with value None (the Postgres
    fixtures' _full_row does exactly this), so a setdefault would leave None in a NOT NULL
    column — the row is then dropped inside its own SAVEPOINT and only logged."""
    filled = _with_offer_write_defaults([{"offer_type": None, "is_first_party": None}])[0]
    assert filled["is_first_party"] is False
    assert catalog_offers.c.is_first_party.nullable is False
    # offer_type is genuinely nullable; None must survive as "seller type unknown".
    assert filled["offer_type"] is None


def test_reingest_backfills_the_pair_and_never_clobbers_a_decided_type():
    """Backfilling offer_type alone would strand an old row as (retailer,
    external_referral) — a pair this writer never emits and the arm never selects, so the
    re-ingest would report success while the offer stayed invisible."""
    update_clause = _OFFER_UPSERT_SQL.split("DO UPDATE SET", 1)[1]
    assert "offer_type = COALESCE(catalog_offers.offer_type, EXCLUDED.offer_type)" in update_clause
    assert "offer_mode = CASE" in update_clause
    assert "THEN 'redirect'" in update_clause
    assert "ELSE catalog_offers.offer_mode" in update_clause
