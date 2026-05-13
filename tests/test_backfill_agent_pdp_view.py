"""Tests for scripts/backfill_agent_pdp_view.py — Stage 3a-ii.

The backfill is the one-shot script that seeds agent_pdp_view (mig 085)
from catalog_products × catalog_skus × catalog_offers ×
product_group_members × external_product_seeds. Tests pin:

  - canonical-row pick respects the (is_primary, has_signature,
    product_key) tiebreak ladder
  - description falls back catalog_products → external seed → NULL
    (never synthesizes prose)
  - offer aggregation: price_min/max derived per dominant currency,
    primary merchant surfaces first, top-N truncates at 5
  - variant aggregation drops singleton placeholder SKUs
    (source_variant_id == source_product_id) and caps at 50
  - GTIN passes through normalize_gtin
  - upsert SQL targets all schema columns + uses ON CONFLICT (content_key)
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backfill_agent_pdp_view as backfill  # noqa: E402


# ---------------------------------------------------------------------------
# canonical row pick
# ---------------------------------------------------------------------------


def test_pick_canonical_prefers_group_primary_first() -> None:
    rows = [
        {"product_key": "pk_a", "group_is_primary": False, "pivota_signature_id": "sig_a"},
        {"product_key": "pk_b", "group_is_primary": True, "pivota_signature_id": None},
    ]
    assert backfill._pick_canonical(rows)["product_key"] == "pk_b"


def test_pick_canonical_then_pivota_signature() -> None:
    rows = [
        {"product_key": "pk_a", "group_is_primary": False, "pivota_signature_id": None},
        {"product_key": "pk_b", "group_is_primary": False, "pivota_signature_id": "sig_b"},
    ]
    assert backfill._pick_canonical(rows)["product_key"] == "pk_b"


def test_pick_canonical_falls_back_to_lowest_product_key() -> None:
    rows = [
        {"product_key": "pk_b", "group_is_primary": False, "pivota_signature_id": None},
        {"product_key": "pk_a", "group_is_primary": False, "pivota_signature_id": None},
    ]
    assert backfill._pick_canonical(rows)["product_key"] == "pk_a"


# ---------------------------------------------------------------------------
# description fallback chain
# ---------------------------------------------------------------------------


def _assemble(**overrides) -> Dict[str, Any]:
    base = {
        "content_key": "ck_test",
        "products": [
            {
                "product_key": "pk_1",
                "merchant_id": "m_primary",
                "platform": "shopify",
                "source_product_id": "sp_1",
                "title": "Hydrating Toner",
                "description": None,
                "brand": "the ordinary",
                "product_payload": {},
                "pdp_lifecycle_stage": "published",
                "pivota_signature_id": "sig_x",
                "canonical_url": "https://m.example.com/p/sp_1",
                "sync_status": "live",
                "product_group_id": "grp_1",
                "group_is_primary": True,
            },
        ],
        "skus": [],
        "offers": [],
        "external_seed": None,
    }
    base.update(overrides)
    return backfill._assemble_row(**base)


def test_description_falls_back_to_external_seed_seed_data() -> None:
    """When catalog_products has no description, pull it from the
    attached external_product_seed's seed_data. Bootstrap content is
    employee-authored — see memory project_pivota_external_seed_bootstrap.
    """
    row = _assemble(
        external_seed={
            "id": "seed_1",
            "attached_product_key": "pk_1",
            "title": None,
            "image_url": None,
            "seed_data": {"description": "Niacinamide 10% + Zinc 1%, 30ml"},
        },
    )
    assert row["description"] == "Niacinamide 10% + Zinc 1%, 30ml"


def test_description_left_null_when_no_source_has_one() -> None:
    """Never invent description prose. If neither catalog_products nor
    the external seed carries one, leave NULL — Stage 3a-iv emits the
    JSON-LD fallback (the rendered tag still gets a value via
    pivota-agent-ui#167's resolver)."""
    row = _assemble()
    assert row["description"] is None


def test_description_truncated_to_5000_chars() -> None:
    long_desc = "x" * 6000
    row = _assemble(products=[{
        "product_key": "pk_1",
        "merchant_id": "m_primary",
        "platform": "shopify",
        "source_product_id": "sp_1",
        "title": "T",
        "description": long_desc,
        "brand": "B",
        "product_payload": {},
        "pdp_lifecycle_stage": None,
        "pivota_signature_id": None,
        "canonical_url": None,
        "sync_status": "live",
        "product_group_id": None,
        "group_is_primary": False,
    }])
    assert len(row["description"]) == 5000


# ---------------------------------------------------------------------------
# offer aggregation
# ---------------------------------------------------------------------------


def test_aggregate_offers_picks_dominant_currency_and_bounds_price() -> None:
    offers = [
        {"merchant_id": "m1", "merchant_name": "M1", "availability": "in_stock",
         "currency": "USD", "list_price": Decimal("19.99"),
         "merchant_effective_price": Decimal("17.50"), "estimated_best_price": None},
        {"merchant_id": "m2", "merchant_name": "M2", "availability": "in_stock",
         "currency": "USD", "list_price": Decimal("22.00"),
         "merchant_effective_price": None, "estimated_best_price": Decimal("20.00")},
        {"merchant_id": "m3", "merchant_name": "M3", "availability": "in_stock",
         "currency": "CAD", "list_price": Decimal("28.00"),
         "merchant_effective_price": None, "estimated_best_price": None},
    ]
    currency, price_min, price_max, count, top = backfill._aggregate_offers(
        offers, primary_merchant_id="m1", merchant_url_by_id={"m1": "https://m1"},
    )
    assert currency == "USD"
    assert price_min == Decimal("17.50")
    assert price_max == Decimal("20.00")
    assert count == 3
    # Primary first, then price ASC
    assert top[0]["merchant_id"] == "m1"
    assert top[0]["is_primary"] is True
    assert top[0]["url"] == "https://m1"


def test_aggregate_offers_caps_at_top_n() -> None:
    offers = [
        {"merchant_id": f"m{i}", "merchant_name": f"M{i}", "availability": "in_stock",
         "currency": "USD", "list_price": Decimal(str(10 + i)),
         "merchant_effective_price": None, "estimated_best_price": None}
        for i in range(10)
    ]
    _, _, _, count, top = backfill._aggregate_offers(
        offers, primary_merchant_id=None, merchant_url_by_id={},
    )
    assert count == 10
    assert len(top) == backfill.OFFER_TOP_N


def test_aggregate_offers_skips_offers_with_no_price() -> None:
    offers = [
        {"merchant_id": "m1", "merchant_name": "M1", "availability": "in_stock",
         "currency": "USD", "list_price": None,
         "merchant_effective_price": None, "estimated_best_price": None},
    ]
    _, price_min, price_max, count, top = backfill._aggregate_offers(
        offers, primary_merchant_id=None, merchant_url_by_id={},
    )
    assert price_min is None and price_max is None
    assert count == 0
    assert top == []


# ---------------------------------------------------------------------------
# variant aggregation
# ---------------------------------------------------------------------------


def test_aggregate_variants_drops_singleton_placeholder_skus() -> None:
    """Path A sync inserts one catalog_skus row per product with
    source_variant_id == source_product_id so the SKU table is always
    joinable. That row is not a real variant."""
    skus = [
        {"sku": "sku-real", "source_variant_id": "v1", "source_product_id": "p1",
         "title": "Real", "image_url": None, "currency": "USD",
         "visible_option_labels": {"color": "red"}, "visible_attributes": None,
         "merchant_id": "m1"},
        {"sku": "sku-placeholder", "source_variant_id": "p1", "source_product_id": "p1",
         "title": "Placeholder", "image_url": None, "currency": "USD",
         "visible_option_labels": None, "visible_attributes": None,
         "merchant_id": "m1"},
    ]
    variants, count = backfill._aggregate_variants(skus, canonical_source_product_id="p1")
    assert count == 1
    assert variants[0]["sku"] == "sku-real"
    assert variants[0]["options"] == {"color": "red"}


def test_aggregate_variants_caps_at_variant_cap_but_count_is_unbounded() -> None:
    skus = [
        {"sku": f"sku-{i:03d}", "source_variant_id": f"v{i}", "source_product_id": "p1",
         "title": f"Variant {i}", "image_url": None, "currency": "USD",
         "visible_option_labels": None, "visible_attributes": None,
         "merchant_id": "m1"}
        for i in range(75)
    ]
    variants, count = backfill._aggregate_variants(skus, canonical_source_product_id="p1")
    assert count == 75
    assert len(variants) == backfill.VARIANT_CAP


# ---------------------------------------------------------------------------
# GTIN
# ---------------------------------------------------------------------------


def test_pick_gtin13_normalizes_through_canonical_form() -> None:
    skus = [
        {"barcode": ""},
        {"barcode": None},
        {"barcode": "773602443796"},  # UPC-A (12 digits)
    ]
    assert backfill._pick_gtin13(skus) == "00773602443796"


def test_pick_gtin13_returns_none_when_no_skus_have_barcode() -> None:
    assert backfill._pick_gtin13([{"barcode": ""}, {"barcode": None}]) is None


# ---------------------------------------------------------------------------
# upsert SQL shape
# ---------------------------------------------------------------------------


def test_upsert_sql_targets_all_schema_columns() -> None:
    """Every schema column owned by the backfill (everything except
    refreshed_by_proposal_id, which belongs to Stage 3a-iii) must be in
    the INSERT column list AND in the ON CONFLICT DO UPDATE SET clause.
    Catches drift if mig 085's schema gets extended without updating
    the backfill."""
    sql = backfill.UPSERT_SQL
    columns_owned_by_backfill = [
        "content_key", "pivota_signature_id", "product_group_id",
        "brand", "title", "description", "image_url", "image_urls",
        "currency", "price_min", "price_max", "offer_count", "offers",
        "variants", "variants_count", "gtin13",
        "category_path", "taxonomy_tags", "breadcrumb",
        "pdp_lifecycle_stage", "sync_status", "primary_merchant_id",
        "refresh_source",
    ]
    for col in columns_owned_by_backfill:
        assert col in sql, f"column {col!r} missing from UPSERT_SQL"
    assert "ON CONFLICT (content_key) DO UPDATE" in sql
    # refreshed_at is owned by the backfill (NOW()) but not via a bind
    # parameter — it's hard-coded NOW() in both branches.
    assert "refreshed_at = NOW()" in sql


# ---------------------------------------------------------------------------
# assemble row — end-to-end shape
# ---------------------------------------------------------------------------


def test_assemble_row_full_shape() -> None:
    products = [
        {
            "product_key": "pk_primary",
            "merchant_id": "m_primary",
            "platform": "shopify",
            "source_product_id": "sp_1",
            "title": "Acme Foundation",
            "description": "Buildable medium coverage.",
            "brand": "Acme",
            "product_type": "foundation",
            "category": "Makeup",
            "image_url": "https://img/canon.jpg",
            "product_payload": {"image_urls": ["https://img/canon.jpg", "https://img/2.jpg"]},
            "tags": ["bestseller"],
            "price_tier": "premium",
            "use_case_tags": ["everyday"],
            "lifestyle_tags": None,
            "demographic": "adult",
            "pdp_lifecycle_stage": "published",
            "pivota_signature_id": "sig_xyz",
            "canonical_url": "https://primary.example.com/p/1",
            "sync_status": "live",
            "product_group_id": "grp_1",
            "group_is_primary": True,
        },
        {
            "product_key": "pk_secondary",
            "merchant_id": "m_secondary",
            "platform": "wix",
            "source_product_id": "sp_2",
            "title": "Acme Foundation",
            "description": None,
            "brand": "Acme",
            "product_type": "foundation",
            "category": None,
            "image_url": None,
            "product_payload": {},
            "tags": None,
            "price_tier": None,
            "use_case_tags": None,
            "lifestyle_tags": None,
            "demographic": None,
            "pdp_lifecycle_stage": "published",
            "pivota_signature_id": None,
            "canonical_url": "https://secondary.example.com/p/2",
            "sync_status": "live",
            "product_group_id": "grp_1",
            "group_is_primary": False,
        },
    ]
    skus = [
        {"sku": "ACME-FDN-01", "source_variant_id": "v01", "source_product_id": "sp_1",
         "title": "Shade 01", "image_url": None, "currency": "USD",
         "visible_option_labels": {"shade": "01"}, "visible_attributes": None,
         "barcode": "773602443796", "merchant_id": "m_primary"},
    ]
    offers = [
        {"merchant_id": "m_primary", "merchant_name": "PrimaryShop", "availability": "in_stock",
         "currency": "USD", "list_price": Decimal("48.00"),
         "merchant_effective_price": Decimal("44.00"), "estimated_best_price": None},
        {"merchant_id": "m_secondary", "merchant_name": "SecondaryShop", "availability": "in_stock",
         "currency": "USD", "list_price": Decimal("50.00"),
         "merchant_effective_price": None, "estimated_best_price": None},
    ]

    row = backfill._assemble_row(
        content_key="ck_acme_fdn",
        products=products,
        skus=skus,
        offers=offers,
        external_seed=None,
    )

    assert row["content_key"] == "ck_acme_fdn"
    assert row["pivota_signature_id"] == "sig_xyz"  # from canonical (group_is_primary)
    assert row["product_group_id"] == "grp_1"
    assert row["primary_merchant_id"] == "m_primary"
    assert row["title"] == "Acme Foundation"
    assert row["description"] == "Buildable medium coverage."
    assert row["currency"] == "USD"
    assert row["price_min"] == Decimal("44.00")
    assert row["price_max"] == Decimal("50.00")
    assert row["offer_count"] == 2
    assert row["offers"][0]["merchant_id"] == "m_primary"
    assert row["offers"][0]["is_primary"] is True
    assert row["variants_count"] == 1
    assert row["gtin13"] == "00773602443796"
    assert row["category_path"] == "Makeup"
    assert row["taxonomy_tags"]["price_tier"] == "premium"
    assert row["taxonomy_tags"]["tags"] == ["bestseller"]
    assert row["breadcrumb"][0]["name"] == "Home"
    assert row["breadcrumb"][-1]["name"] == "Acme Foundation"
    assert row["refresh_source"] == "backfill_3a_ii"


def test_assemble_row_returns_none_when_canonical_has_no_title() -> None:
    products = [{
        "product_key": "pk_1", "merchant_id": "m", "platform": "shopify",
        "source_product_id": "sp", "title": "", "description": None, "brand": None,
        "product_payload": {}, "pdp_lifecycle_stage": None,
        "pivota_signature_id": None, "canonical_url": None, "sync_status": "live",
        "product_group_id": None, "group_is_primary": False,
    }]
    assert backfill._assemble_row(
        content_key="ck_x", products=products, skus=[], offers=[], external_seed=None,
    ) is None
