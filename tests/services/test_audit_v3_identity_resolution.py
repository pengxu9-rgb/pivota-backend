"""Tests for resolve_sku_identity — bad-name-tolerant SKU identity resolution.

Merchant catalogs frequently carry variant/format labels as the SKU title, so
identity must come from the most-curated available signal (enrichment override
> product-level title > variant title) with a confidence, plus name-independent
anchors (PDP/brand/category/GTIN). SKUs with only a variant label resolve as
`unresolved` (low confidence) so the report can flag "enrich before trusting"
instead of scoring them invisible.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_center_bd_report_service import resolve_sku_identity


def test_high_confidence_uses_enrichment_title_override() -> None:
    ctx = {
        "product": {"title": "Triple Collagen Garden edition", "brand": "Ownist",
                    "canonical_url": "https://ownist.com/products/x", "content_key": "ck1"},
        "sku": {"title": "Garden Gift Set", "barcode": "8809XYZ"},
        "product_enrichment": {"title_override": "Ownist Triple Collagen Garden Edition (30 sticks)"},
        "product_group_id": "pg1", "sku_key": "p4::v::g",
    }
    ident = resolve_sku_identity(ctx)
    assert ident["confidence"] == "high"
    assert ident["source"] == "enrichment.title_override"
    assert ident["unresolved"] is False
    assert "Garden Edition" in ident["name"] and "Gift Set" not in ident["name"]


def test_medium_confidence_uses_product_title_with_brand() -> None:
    ctx = {
        "product": {"title": "Triple Shine Grape", "brand": "Ownist",
                    "canonical_url": "https://ownist.com/products/triple-shine-1-box"},
        "sku": {"title": "14 Servings, 2-Week Routine", "barcode": "8809ABC"},
        "sku_key": "p1::v::a",
    }
    ident = resolve_sku_identity(ctx)
    assert ident["confidence"] == "medium"
    assert ident["source"] == "catalog.product_title"
    assert ident["name"] == "Ownist Triple Shine Grape"   # brand-prefixed, NOT the variant label
    assert "14 Servings" not in ident["name"]
    assert ident["unresolved"] is False


def test_low_confidence_when_only_variant_label() -> None:
    # No product-level title — only the variant/format label. Identity unreliable.
    ctx = {
        "product": {"brand": "Ownist"},
        "sku": {"title": "Garden Gift Set"},
        "sku_key": "p4::v::g",
    }
    ident = resolve_sku_identity(ctx)
    assert ident["confidence"] == "low"
    assert ident["source"] == "catalog.sku_title"
    assert ident["unresolved"] is True


def test_anchors_are_name_independent() -> None:
    ctx = {
        "product": {"title": "Triple Shine Grape", "brand": "Ownist", "category": "supplement",
                    "canonical_url": "https://ownist.com/products/triple-shine-1-box",
                    "content_key": "ck_abc"},
        "sku": {"title": "14 Servings", "barcode": "8809123"},
        "product_group_id": "pg_9", "sku_key": "p1::v::a",
    }
    a = resolve_sku_identity(ctx)["anchors"]
    assert a["domain"] == "ownist.com"
    assert a["canonical_url"].endswith("/triple-shine-1-box")
    assert a["gtin"] == "8809123"
    assert a["brand"] == "Ownist"
    assert a["category"] == "supplement"
    assert a["content_key"] == "ck_abc"
    assert a["product_group_id"] == "pg_9"


def test_brand_not_doubled_when_title_already_branded() -> None:
    ctx = {"product": {"title": "Ownist Triple Collagen Orange", "brand": "Ownist"},
           "sku": {"title": "30 sticks"}, "sku_key": "p2::v::b"}
    ident = resolve_sku_identity(ctx)
    assert ident["name"] == "Ownist Triple Collagen Orange"
    assert "Ownist Ownist" not in ident["name"]


def test_with_brand_skips_prefix_when_title_names_brand_alias():
    """Live verify run efcdaf06: brand "HoverAir (Category Demo)" + title
    "HOVERAir X1 - …" produced the double-branded identity name "HoverAir
    (Category Demo) HOVERAir X1 - …" (and every NBA headline built from it) —
    the full-string containment check can't see that a decorated brand's CORE
    already opens the title. Alias-based matching must skip the prefix."""
    ident = resolve_sku_identity({
        "product": {
            "title": "HOVERAir X1 - Foldable Entry-Level Self-Flying Camera Drone",
            "brand": "HoverAir (Category Demo)",
        },
        "sku": {},
    })
    assert ident["name"] == "HOVERAir X1 - Foldable Entry-Level Self-Flying Camera Drone"
    # a title that does NOT name the brand still gets the prefix
    ident2 = resolve_sku_identity({
        "product": {"title": "X1 Foldable Camera Drone", "brand": "HoverAir"},
        "sku": {},
    })
    assert ident2["name"] == "HoverAir X1 Foldable Camera Drone"
