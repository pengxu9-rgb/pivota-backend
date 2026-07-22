from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import source_pdp_offer_image_repair as repair  # noqa: E402


def _row(**overrides):
    base = {
        "content_key": "ck_test",
        "product_key": "prod_test",
        "blocker_code": "no_price",
        "has_price": False,
        "has_image": False,
        "title": "Jurlique Dry Body Brush",
        "apv_title": "Jurlique Dry Body Brush",
        "brand": "Jurlique",
        "canonical_url": "https://www.jurlique.com/us/dry-body-brush",
        "cp_image_url": None,
        "apv_image_url": None,
    }
    base.update(overrides)
    return base


class ItemOnlyRecord:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]


def test_record_value_supports_database_record_shape() -> None:
    record = ItemOnlyRecord({"sku_key": "sku_123"})

    assert repair._record_value(record, "sku_key") == "sku_123"
    assert repair._record_value(record, "missing") is None


def test_title_gate_rejects_shade_variant_mismatch() -> None:
    gate = repair.title_gate(
        "Wet Lip Oil Plumping Treatment Gloss Wet Cherry",
        "Wet Lip Oil Gloss - Grapesicle",
    )

    assert gate["ok"] is False
    assert gate["reason"] == "variant_token_mismatch"
    assert "cherry" in gate["target_variant_tokens"]
    assert "grapesicle" in gate["source_variant_tokens"]


def test_title_gate_rejects_overly_broad_source_title() -> None:
    gate = repair.title_gate(
        "Generation G Sheer Matte Lipstick Crush",
        "Generation G",
    )

    assert gate["ok"] is False
    assert gate["reason"] == "title_mismatch"


def test_title_gate_rejects_source_title_with_extra_specific_tokens() -> None:
    gate = repair.title_gate("AirPods Max", "AirPods Max 2")

    assert gate["ok"] is False
    assert gate["reason"] == "source_title_extra_tokens"
    assert gate["source_extra_tokens"] == ["2"]


def test_evaluate_candidate_accepts_exact_price_and_image_gap() -> None:
    extracted = {
        "canonical_url": "https://www.jurlique.com/us/dry-body-brush",
        "title": "Jurlique Dry Body Brush",
        "price_amount": 25,
        "price_currency": "USD",
        "image_url": "https://www.jurlique.com/images/dry-body-brush.jpg",
        "image_urls": ["https://www.jurlique.com/images/dry-body-brush.jpg"],
        "evidence_provider": "jsonld",
        "variants": [],
    }

    out = repair.evaluate_candidate(_row(), extracted, market="US")

    assert out["safe_price_repair"] is True
    assert out["safe_image_repair"] is True
    assert out["safe_any_repair"] is True


def test_evaluate_candidate_does_not_repair_existing_good_fields() -> None:
    extracted = {
        "canonical_url": "https://www.jurlique.com/us/dry-body-brush",
        "title": "Jurlique Dry Body Brush",
        "price_amount": 25,
        "price_currency": "USD",
        "image_url": "https://www.jurlique.com/images/dry-body-brush.jpg",
        "image_urls": ["https://www.jurlique.com/images/dry-body-brush.jpg"],
        "evidence_provider": "jsonld",
        "variants": [],
    }

    out = repair.evaluate_candidate(
        _row(
            has_price=True,
            has_image=True,
            cp_image_url="https://existing.example/product.jpg",
            apv_image_url="https://existing.example/product.jpg",
        ),
        extracted,
        market="US",
    )

    assert out["safe_price_repair"] is False
    assert out["safe_image_repair"] is False
    assert out["safe_any_repair"] is False
    assert out["price_reject_reason"] == "not_needed"
    assert out["image_reject_reason"] == "not_needed"


def test_price_gate_rejects_us_currency_mismatch() -> None:
    out = repair.price_gate(
        {"price_amount": 25, "price_currency": "CAD", "variants": []},
        market="US",
        exact_title=True,
    )

    assert out["ok"] is False
    assert out["reason"] == "currency_mismatch"


def test_price_gate_rejects_ambiguous_variant_prices_without_exact_title() -> None:
    out = repair.price_gate(
        {
            "price_amount": 20,
            "price_currency": "USD",
            "variants": [{"price_amount": 20}, {"price_amount": 24}],
        },
        market="US",
        exact_title=False,
    )

    assert out["ok"] is False
    assert out["reason"] == "variant_price_ambiguous"


def test_image_gate_filters_placeholder_assets() -> None:
    assert (
        repair.safe_image_url(
            {
                "image_url": "https://example.com/assets/no-image-2048.gif",
                "image_urls": [
                    "https://example.com/assets/no-image-2048.gif",
                    "https://example.com/assets/logo.png",
                    "https://example.com/products/real-product.webp",
                ],
            }
        )
        == "https://example.com/products/real-product.webp"
    )


def test_offer_insert_sql_preserves_existing_positive_prices() -> None:
    sql = repair.INSERT_REPAIR_OFFER_SQL

    assert "WHERE NOT EXISTS" in sql
    assert "existing_offer.product_key = CAST(:product_key AS text)" in sql
    assert "coalesce(existing_offer.list_price, 0) > 0" in sql
    assert "catalog_offers.list_price IS NULL OR catalog_offers.list_price <= 0" in sql


def test_image_update_sql_only_fills_empty_live_catalog_image() -> None:
    sql = repair.UPDATE_CATALOG_IMAGE_SQL

    assert "sync_status = 'live'" in sql
    assert "(image_url IS NULL OR btrim(image_url) = '')" in sql
    assert "product_payload" in sql


def test_candidate_query_excludes_non_repairable_blockers() -> None:
    sql, _ = repair.build_candidate_query(limit=10, include_upstream_blockers=False)

    assert "ips.blocker_code NOT IN ('not_live', 'non_core_product')" in sql
    assert "ips.blocker_code IN ('no_image', 'no_price')" in sql
    assert "ips.has_price IS FALSE" in sql
    assert "ips.has_image IS FALSE" in sql


def test_candidate_query_prefers_direct_source_rows_over_retailer_rows() -> None:
    sql, _ = repair.build_candidate_query(limit=10, include_upstream_blockers=True)

    assert "sephora|nordstrom|ulta|amazon" in sql
    assert "amzn" in sql
    assert "bestbuy" in sql
    assert "CASE WHEN product_key LIKE 'ext:%' THEN 0 ELSE 1 END" in sql
    assert "length(btrim(coalesce(cp_description, ''))) >= 50" in sql


def test_candidate_query_can_explicitly_include_upstream_blockers() -> None:
    sql, _ = repair.build_candidate_query(limit=10, include_upstream_blockers=True)

    assert "ips.blocker_code NOT IN ('not_live', 'non_core_product')" in sql
    assert "ips.blocker_code IN ('no_image', 'no_price')" not in sql


# --- opt-in --allow-source-superset relaxation -------------------------------


def test_source_superset_rejected_by_default() -> None:
    """Flag off (default) keeps the strict source_title_extra_tokens veto."""
    gate = repair.title_gate(
        "Oil-Free Ultra-Moisturizing Lotion",
        "Oil-Free Ultra-Moisturizing Lotion with Birch Sap",
    )

    assert gate["ok"] is False
    assert gate["reason"] == "source_title_extra_tokens"


def test_source_superset_accepted_when_opted_in() -> None:
    """Abbreviated feed title vs the brand's fuller product name."""
    gate = repair.title_gate(
        "Oil-Free Ultra-Moisturizing Lotion",
        "Oil-Free Ultra-Moisturizing Lotion with Birch Sap",
        allow_source_superset=True,
    )

    assert gate["ok"] is True
    assert gate["title_superset_accepted"] is True


def test_source_superset_still_rejects_bundle_and_size_variants() -> None:
    """Pack/size/count words must veto even when our title is fully contained.

    These are matched on RAW tokens: "set"/"pack"/"size" are stripped by
    STOPWORDS/GENERIC_PRODUCT_TOKENS, so a filtered-set check would miss them.
    """
    target = "COSRX Advanced Snail Mucin Power Essence"
    for suffix in (
        "Twin Pack", "Double Set", "Travel Size", "Refill", "Jumbo",
        "Sample", "Multipack", "Combo Deal", "Starter Trial",
    ):
        gate = repair.title_gate(
            target, f"{target} {suffix}", allow_source_superset=True
        )
        assert gate["ok"] is False, f"{suffix!r} must not pass as a superset match"


def test_source_superset_rejects_generic_short_target() -> None:
    """A 1-2 token target is too generic to trust as a subset match."""
    gate = repair.title_gate(
        "Toner", "Beauty of Joseon Rice Water Bright Toner",
        allow_source_superset=True,
    )

    assert gate["ok"] is False


def test_source_superset_still_honours_similarity_floor() -> None:
    """The relaxation bypasses the extra-token veto, never the score floor."""
    gate = repair.title_gate(
        "Snail Mucin Essence",
        "Snail Mucin Essence Plus Full Ritual Anti Aging Brightening Night Repair Complex",
        allow_source_superset=True,
    )

    assert gate["ok"] is False
