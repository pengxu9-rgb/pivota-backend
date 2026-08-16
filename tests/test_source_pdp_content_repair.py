from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import source_pdp_content_repair as repair  # noqa: E402


def _row(**overrides):
    base = {
        "content_key": "ck_content",
        "product_key": "prod_content",
        "blocker_code": "no_seed",
        "description_length": 0,
        "has_price": True,
        "has_image": True,
        "title": "Glam Eyeshadow Palette",
        "apv_title": "Glam Eyeshadow Palette",
        "cp_description": None,
        "apv_description": None,
        "brand": "Natasha Denona",
        "canonical_url": "https://natashadenona.com/products/glam-eyeshadow-palette",
    }
    base.update(overrides)
    return base


def test_usable_description_rejects_generic_footer_copy() -> None:
    text = "Free shipping on orders over $50. Subscribe to our newsletter and read our privacy policy."

    assert repair.usable_description(text) == ""


def test_usable_description_rejects_brand_boilerplate() -> None:
    text = (
        "Fenty Beauty by Rihanna was created with promise of inclusion for all women. "
        "With an unmatched offering of shades and colors for all skin tones, browse our foundation."
    )

    assert repair.usable_description(text) == ""


def test_evaluate_candidate_accepts_exact_title_with_clean_description() -> None:
    extracted = {
        "source": "html",
        "canonical_url": "https://natashadenona.com/products/glam-eyeshadow-palette",
        "title": "Glam Eyeshadow Palette",
        "description": (
            "A neutral eyeshadow palette with matte, metallic, and sparkling shades "
            "designed for everyday soft glam looks and deeper evening dimension."
        ),
        "evidence_provider": "jsonld",
    }

    out = repair.evaluate_candidate(_row(), extracted)

    assert out["safe_content_repair"] is True
    assert out["description_len"] >= repair.MIN_DESCRIPTION_LENGTH


def test_evaluate_candidate_requires_product_specific_description() -> None:
    extracted = {
        "source": "html",
        "canonical_url": "https://www.glossier.com/products/hair-comb",
        "title": "Hair Comb",
        "description": (
            "Shop the new beauty essentials exclusively at Glossier.com. Good routines start here. "
            "Get makeup and skincare products inspired by real people and their routines."
        ),
        "evidence_provider": "jsonld",
    }

    out = repair.evaluate_candidate(
        _row(
            title="Hair Comb",
            apv_title="Hair Comb",
            brand="Glossier",
            canonical_url="https://www.glossier.com/products/hair-comb",
        ),
        extracted,
    )

    assert out["safe_content_repair"] is False
    assert out["reject_reason"] == "missing_or_unsafe_description"


def test_evaluate_candidate_accepts_description_with_product_tokens() -> None:
    extracted = {
        "source": "shopify_product_json",
        "canonical_url": "https://www.glossier.com/products/lash-slick.js",
        "title": "Lash Slick",
        "description": (
            "A flexible mascara that lengthens lashes with a lightweight finish, "
            "giving a clean lifted look without clumps or flakes."
        ),
        "evidence_provider": "shopify_product_json",
    }

    out = repair.evaluate_candidate(
        _row(
            title="Lash Slick Mascara",
            apv_title="Lash Slick Mascara",
            brand="Glossier",
            canonical_url="https://www.glossier.com/products/lash-slick",
        ),
        extracted,
    )

    assert out["safe_content_repair"] is True
    assert out["description_mentions_title"] is True


def test_evaluate_candidate_does_not_overwrite_good_description() -> None:
    existing = (
        "Glam Eyeshadow Palette already has enough detail for the PDP and should not "
        "be replaced by public source repair."
    )
    extracted = {
        "source": "html",
        "canonical_url": "https://natashadenona.com/products/glam-eyeshadow-palette",
        "title": "Glam Eyeshadow Palette",
        "description": existing,
        "evidence_provider": "jsonld",
    }

    out = repair.evaluate_candidate(_row(apv_description=existing), extracted)

    assert out["safe_content_repair"] is False
    assert out["safe_quality_refresh"] is True
    assert out["current_description_mentions_title"] is True
    assert out["reject_reason"] is None


def test_quality_refresh_rejects_weak_source_evidence() -> None:
    existing = "Kindle Colorsoft Signature Edition has a short but product-specific description."
    extracted = {
        "source": "html",
        "canonical_url": "https://www.amazon.com/All-New-Amazon-Kindle-Colorsoft-Signature-Edition/dp/B0CN3XR57P",
        "title": "Amazon",
        "description": "",
        "evidence_provider": "og",
    }

    out = repair.evaluate_candidate(
        _row(
            title="Kindle Colorsoft Signature Edition",
            apv_title="Kindle Colorsoft Signature Edition",
            apv_description=existing,
            brand="Amazon",
            canonical_url="https://www.amazon.com/dp/B0CN3XR57P",
        ),
        extracted,
    )

    assert out["safe_content_repair"] is False
    assert out["safe_quality_refresh"] is False
    assert out["reject_reason"] == "missing_title_tokens"


def test_quality_refresh_rejects_current_description_without_product_tokens() -> None:
    existing = (
        "A complete description already exists with enough detail for the PDP, "
        "but it does not prove that it belongs to the target product."
    )
    extracted = {
        "source": "html",
        "canonical_url": "https://natashadenona.com/products/glam-eyeshadow-palette",
        "title": "Glam Eyeshadow Palette",
        "description": (
            "Glam Eyeshadow Palette includes neutral matte, metallic, and sparkling shades "
            "for soft glam eye looks and deeper evening dimension."
        ),
        "evidence_provider": "jsonld",
    }

    out = repair.evaluate_candidate(_row(apv_description=existing), extracted)

    assert out["safe_quality_refresh"] is False
    assert out["reject_reason"] == "current_description_not_product_specific"


def test_evaluate_candidate_rejects_title_mismatch() -> None:
    extracted = {
        "source": "html",
        "canonical_url": "https://kosas.com/products/wet-lip-oil-gloss",
        "title": "Wet Lip Oil Gloss - Grapesicle",
        "description": (
            "A glossy lip treatment with a comfortable cushion texture and sheer color "
            "for hydrated-looking lips throughout the day."
        ),
        "evidence_provider": "jsonld",
    }

    out = repair.evaluate_candidate(
        _row(
            title="Wet Lip Oil Plumping Treatment Gloss Wet Cherry",
            apv_title="Wet Lip Oil Plumping Treatment Gloss Wet Cherry",
            canonical_url="https://www.kosas.com/products/wet-lip-oil-gloss",
        ),
        extracted,
    )

    assert out["safe_content_repair"] is False
    assert out["reject_reason"] == "variant_token_mismatch"


def test_product_json_url_for_shopify_product_path() -> None:
    assert (
        repair.product_json_url("https://example.com/products/my-product?variant=1")
        == "https://example.com/products/my-product.js"
    )


def test_update_description_sql_only_fills_short_live_description() -> None:
    sql = repair.UPDATE_DESCRIPTION_SQL

    assert "sync_status = 'live'" in sql
    assert "length(btrim(coalesce(description, ''))) < CAST(:min_existing_description_length AS integer)" in sql
    assert "description = CAST(:description AS text)" in sql


def test_quality_model_version_fits_snapshot_column() -> None:
    assert len(repair.QUALITY_MODEL_VERSION) <= 32


def test_float_or_none_accepts_decimal_strings() -> None:
    assert repair._float_or_none("72.00") == 72.0
    assert repair._float_or_none(None) is None


def test_candidate_query_targets_content_blockers() -> None:
    sql, values = repair.build_candidate_query(limit=10)

    # 'not_scored' rides with 'low_quality': the 2026-08-15 split separated
    # never-scored from scored-below-bar, and this cohort must keep BOTH or a
    # mass unscoring (the A9-4 re-key stranded 6,424 rows) becomes invisible to
    # the very tool meant to repair it.
    assert (
        "ips.blocker_code IN ('no_seed', 'short_description', 'low_quality', 'not_scored')"
        in sql
    )
    assert "CAST(:content_key AS text) IS NULL" in sql
    assert "CAST(:content_key AS text) IS NOT NULL" in sql
    assert values["min_existing_description_length"] == repair.MIN_EXISTING_DESCRIPTION_LENGTH


def test_candidate_query_prefers_direct_source_rows_over_retailer_rows() -> None:
    sql, _ = repair.build_candidate_query(limit=10)

    assert "sephora|nordstrom|ulta|amazon" in sql
    assert "amzn" in sql
    assert "bestbuy" in sql
    assert "CASE WHEN product_key LIKE 'ext:%' THEN 0 ELSE 1 END" in sql
    assert "length(btrim(coalesce(cp_description, ''))) >= :min_existing_description_length" in sql
