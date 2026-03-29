from __future__ import annotations

import pytest

from services.beauty_external_ranking import (
    BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
    build_external_seed_filter_product,
    rank_external_seed_rows,
)


def _seed_row(
    *,
    external_product_id: str,
    title: str,
    canonical_url: str,
    destination_url: str | None = None,
    category: str = "Serum",
    description: str = "",
    reviewed_ingredient_ids: list[str] | None = None,
    visible_attributes: dict | None = None,
    variants: list[dict] | None = None,
    price_amount: float = 20.0,
    price_currency: str = "USD",
    availability: str = "in_stock",
    brand_term_hit: int = 0,
    updated_at: str = "2026-03-29T00:00:00Z",
) -> dict:
    return {
        "id": f"seed::{external_product_id}",
        "external_product_id": external_product_id,
        "title": title,
        "canonical_url": canonical_url,
        "destination_url": destination_url or canonical_url,
        "domain": canonical_url.split("/")[2],
        "category": category,
        "price_amount": price_amount,
        "price_currency": price_currency,
        "availability": availability,
        "brand_term_hit": brand_term_hit,
        "updated_at": updated_at,
        "seed_data": {
            "title": title,
            "description": description,
            "category": category,
            "reviewed_ingredient_ids": reviewed_ingredient_ids or [],
            "visible_attributes": visible_attributes or {},
            "variants": variants or [],
            "brand": "Demo Brand",
        },
    }


def test_rank_external_seed_rows_prefers_acne_concern_for_active_ingredient_query() -> None:
    ranked = rank_external_seed_rows(
        [
            _seed_row(
                external_product_id="acne_mist",
                title="Body Acne Clearing Mist with 2% Salicylic Acid",
                canonical_url="https://example.com/products/body-acne-clearing-mist-salicylic-acid",
                category="Treatment",
                description="Targets acne and pores with salicylic acid.",
                reviewed_ingredient_ids=["salicylic_acid"],
                visible_attributes={"skin_concern": ["acne", "pores"]},
            ),
            _seed_row(
                external_product_id="plain_serum",
                title="Salicylic Acid Serum 2%",
                canonical_url="https://example.com/products/salicylic-acid-serum",
                description="A plain exfoliating serum.",
                reviewed_ingredient_ids=["salicylic_acid"],
            ),
        ],
        query="salicylic acid serum for acne and pores",
        limit=5,
    )

    assert ranked[0].title == "Body Acne Clearing Mist with 2% Salicylic Acid"
    assert ranked[0].ranking_score_breakdown["active_ingredient_score"] > 0
    assert ranked[0].ranking_score_breakdown["concern_score"] > 0


def test_rank_external_seed_rows_penalizes_eye_cream_and_bundle_noise() -> None:
    ranked = rank_external_seed_rows(
        [
            _seed_row(
                external_product_id="eye_cream",
                title="Barrier Repair Eye Cream",
                canonical_url="https://example.com/products/barrier-repair-eye-cream",
                category="Eye Cream",
                visible_attributes={"product_category": ["eye_cream"]},
            ),
            _seed_row(
                external_product_id="routine",
                title="Fragrance-Free Barrier Routine Set",
                canonical_url="https://example.com/products/barrier-routine-set",
                category="Set",
            ),
            _seed_row(
                external_product_id="moisturizer",
                title="Hydrating Barrier Moisturizer Fragrance Free",
                canonical_url="https://example.com/products/hydrating-barrier-moisturizer",
                category="Moisturizer",
                visible_attributes={
                    "product_category": ["moisturizer"],
                    "formula_constraint": ["fragrance_free"],
                    "skin_concern": ["hydrating"],
                },
            ),
        ],
        query="hydrating barrier moisturizer fragrance free",
        limit=5,
    )

    assert ranked[0].external_product_id == "moisturizer"
    assert ranked[-1].external_product_id in {"eye_cream", "routine"}


def test_rank_external_seed_rows_uses_source_order_only_as_tie_break() -> None:
    ranked = rank_external_seed_rows(
        [
            _seed_row(
                external_product_id="later_seed",
                title="Ultra Gentle Face Cleanser",
                canonical_url="https://example.com/products/later-gentle-cleanser",
                category="Cleanser",
                visible_attributes={"product_category": ["cleanser"]},
                updated_at="2026-03-30T00:00:00Z",
            ),
            _seed_row(
                external_product_id="earlier_seed",
                title="Ultra Gentle Face Cleanser",
                canonical_url="https://example.com/products/earlier-gentle-cleanser",
                category="Cleanser",
                visible_attributes={"product_category": ["cleanser"]},
                updated_at="2025-01-01T00:00:00Z",
            ),
        ],
        query="gentle cleanser",
        limit=5,
    )

    assert ranked[0].external_product_id == "later_seed"
    assert ranked[0].candidate_score == ranked[1].candidate_score


def test_build_external_seed_filter_product_preserves_structured_beauty_fields() -> None:
    row = _seed_row(
        external_product_id="spf_50",
        title="Mineral Sunscreen SPF 50",
        canonical_url="https://example.com/products/mineral-sunscreen-spf-50",
        category="Sunscreen",
        reviewed_ingredient_ids=["zinc_oxide"],
        visible_attributes={
            "product_category": ["sunscreen"],
            "formula_constraint": ["fragrance_free"],
        },
        variants=[
            {
                "id": "variant_1",
                "title": "SPF 50",
                "price": 24.0,
                "options": {"SPF": "50"},
            }
        ],
    )

    product = build_external_seed_filter_product(
        row=row,
        seed_data=row["seed_data"],
        external_product={
            "id": "spf_50",
            "product_id": "spf_50",
            "title": row["title"],
            "description": row["seed_data"]["description"],
            "price": row["price_amount"],
            "currency": row["price_currency"],
            "image_url": None,
            "in_stock": True,
            "external_seed_id": row["id"],
        },
    )

    assert product.visible_attributes["product_category"] == ["sunscreen"]
    assert product.visible_attributes["formula_constraint"] == ["fragrance_free"]
    assert product.ingredient_ids == ["zinc_oxide"]
    assert "spf_50" in product.variants[0].visible_option_labels


def test_ranked_feature_dump_exposes_audit_version_and_structure() -> None:
    ranked = rank_external_seed_rows(
        [
            _seed_row(
                external_product_id="cleanser",
                title="Gentle Cleanser",
                canonical_url="https://example.com/products/gentle-cleanser",
                category="Cleanser",
                visible_attributes={"product_category": ["cleanser"]},
            )
        ],
        query="gentle cleanser",
        limit=1,
    )

    dump = ranked[0].as_feature_dump()

    assert ranked[0].ranking_score_breakdown["ranking_audit_version"] == BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION
    assert dump["candidate_source"] == "external_seed"
    assert dump["normalized_visible_attributes"]["product_category"] == ["cleanser"]
