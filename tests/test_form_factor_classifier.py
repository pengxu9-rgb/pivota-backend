"""Tests for the form factor + price band classifier (PR-7b).

Coverage:
  - Form factor classification across all canonical buckets
  - First-match-wins ordering (gummy > chewable; softgel > capsule)
  - Defensive: no input → None; ambiguous → None
  - Price band bucketing
  - Combined classify_product
  - Cohort summary: merchant uniqueness detection
  - Integration: build_structured_report surfaces classification
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# Form factor classification
# ---------------------------------------------------------------------


def test_classifies_gummy_from_title():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Greens Gummies") == "gummy"
    assert classify_form_factor(product_title="Daily Gummy Vitamins") == "gummy"


def test_classifies_powder_from_title():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="AG1 Greens Powder") == "powder"
    assert classify_form_factor(product_title="Bloom Daily Powder Drink Mix") == "powder"


def test_classifies_capsule_from_title():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Magnesium Capsules") == "capsule"
    assert classify_form_factor(product_title="Omega-3 Softgels") == "capsule"


def test_classifies_tablet():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Daily Tablets") == "tablet"


def test_classifies_liquid():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Energy Shot 2oz") == "liquid"
    assert classify_form_factor(product_title="Vitamin D Drops") == "liquid"


def test_classifies_bar():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Protein Bar") == "bar"


def test_classifies_drink():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Kombucha Beverage") == "drink"


def test_classifies_topical_skincare():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Hydrating Serum") == "topical"
    assert classify_form_factor(product_title="Moisturizing Cream") == "topical"


def test_classifies_patch():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Sleep Patch 30 ct") == "patch"


def test_falls_back_to_product_type_when_title_ambiguous():
    """Title alone doesn't reveal form factor; product_type does."""
    from services.product_form_factor_classifier import classify_form_factor
    ff = classify_form_factor(
        product_title="Mother's Day Bundle",
        product_type="daily greens gummies",
    )
    assert ff == "gummy"


def test_returns_none_when_no_keyword_matches():
    from services.product_form_factor_classifier import classify_form_factor
    assert classify_form_factor(product_title="Mystery Product XYZ") is None
    assert classify_form_factor() is None
    assert classify_form_factor(product_title="") is None


def test_first_match_wins_gummy_before_chewable():
    """When both 'gummy' and 'chewable' could match, gummy wins
    because it's more specific / precedes in pattern list."""
    from services.product_form_factor_classifier import classify_form_factor
    # "Chewable Gummies" → gummy (more specific)
    assert classify_form_factor(product_title="Chewable Gummies") == "gummy"


# ---------------------------------------------------------------------
# Price band classification
# ---------------------------------------------------------------------


def test_classifies_mass_price_band():
    from services.product_form_factor_classifier import classify_price_band
    assert classify_price_band(9.99) == "mass"
    assert classify_price_band(15.0) == "mass"


def test_classifies_mid_price_band():
    from services.product_form_factor_classifier import classify_price_band
    assert classify_price_band(15.01) == "mid"
    assert classify_price_band(29.99) == "mid"
    assert classify_price_band(30.0) == "mid"


def test_classifies_premium_price_band():
    from services.product_form_factor_classifier import classify_price_band
    assert classify_price_band(45.0) == "premium"
    assert classify_price_band(60.0) == "premium"


def test_classifies_luxury_price_band():
    from services.product_form_factor_classifier import classify_price_band
    assert classify_price_band(60.01) == "luxury"
    assert classify_price_band(150.0) == "luxury"


def test_price_band_returns_none_for_invalid_input():
    from services.product_form_factor_classifier import classify_price_band
    assert classify_price_band(None) is None
    assert classify_price_band(0) is None
    assert classify_price_band(-5) is None
    assert classify_price_band("not a number") is None


# ---------------------------------------------------------------------
# Combined classify_product
# ---------------------------------------------------------------------


def test_classify_product_returns_both_fields():
    from services.product_form_factor_classifier import classify_product
    result = classify_product(
        product_title="Greens Gummies",
        product_type="daily greens supplements",
        price_usd=42.99,
    )
    assert result == {"form_factor": "gummy", "price_band": "premium"}


def test_classify_product_handles_partial_input():
    from services.product_form_factor_classifier import classify_product
    result = classify_product(product_title="Greens Powder")
    assert result["form_factor"] == "powder"
    assert result["price_band"] is None  # no price


# ---------------------------------------------------------------------
# Cohort form factor summary
# ---------------------------------------------------------------------


def test_cohort_summary_detects_merchant_uniqueness():
    """When the merchant is the only brand in their form-factor bucket,
    merchant_owns_unique_form_factor is True (the Grüns case)."""
    from services.product_form_factor_classifier import (
        build_cohort_form_factor_summary,
    )
    result = build_cohort_form_factor_summary(
        merchant_brand="Grüns",
        merchant_form_factor="gummy",
        competitor_brands=[
            {"name": "AG1 (Athletic Greens)"},
            {"name": "Bloom Greens"},
            {"name": "Huel Daily Greens"},
        ],
        cohort_audit_runs=[
            # All 3 competitors classified as powder via brand-name fallback
            # Note: brand-name classification is low-precision; actual cohort
            # runs would carry product_title for accurate classification
        ],
    )
    assert result["merchant_form_factor"] == "gummy"
    # merchant is in gummy bucket alone (assuming none of the competitor
    # brand names hit a form-factor keyword)
    assert "Grüns" in result["form_factor_summary"].get("gummy", [])
    # competitors ended up in "unknown" bucket since brand names alone
    # don't classify (brand fallback is low-precision intentionally)
    assert result["merchant_owns_unique_form_factor"] is True


def test_cohort_summary_uses_cohort_audit_run_data_when_available():
    """When cohort audit runs carry product_title, classification is
    much more accurate than brand-name fallback."""
    from services.product_form_factor_classifier import (
        build_cohort_form_factor_summary,
    )
    result = build_cohort_form_factor_summary(
        merchant_brand="Grüns",
        merchant_form_factor="gummy",
        competitor_brands=[
            {"name": "AG1"},
            {"name": "Bloom"},
        ],
        cohort_audit_runs=[
            {
                "competitor_brand": "AG1",
                "report_jsonb": {
                    "per_product": [{
                        "product": {
                            "title": "AG1 Greens Powder",
                            "product_type": "daily greens powder",
                        },
                    }],
                },
            },
            {
                "competitor_brand": "Bloom",
                "report_jsonb": {
                    "per_product": [{
                        "product": {
                            "title": "Bloom Daily Powder Drink Mix",
                            "product_type": "daily greens powder",
                        },
                    }],
                },
            },
        ],
    )
    # Both competitors correctly classified as powder via cohort runs
    assert "AG1" in result["form_factor_summary"].get("powder", [])
    assert "Bloom" in result["form_factor_summary"].get("powder", [])
    # Grüns alone in gummy bucket → unique form factor
    assert result["merchant_owns_unique_form_factor"] is True
    assert result["competitors_in_merchant_form_factor"] == []


def test_cohort_summary_detects_when_merchant_shares_form_factor():
    """When at least one competitor shares the merchant's form factor,
    merchant_owns_unique_form_factor is False."""
    from services.product_form_factor_classifier import (
        build_cohort_form_factor_summary,
    )
    result = build_cohort_form_factor_summary(
        merchant_brand="Grüns",
        merchant_form_factor="gummy",
        competitor_brands=[{"name": "Olly"}],
        cohort_audit_runs=[
            {
                "competitor_brand": "Olly",
                "report_jsonb": {
                    "per_product": [{
                        "product": {
                            "title": "Olly Daily Energy Gummies",
                            "product_type": "wellness gummies",
                        },
                    }],
                },
            },
        ],
    )
    # Olly is also gummy → merchant doesn't own the bucket alone
    assert "Olly" in result["form_factor_summary"].get("gummy", [])
    assert result["merchant_owns_unique_form_factor"] is False
    assert result["competitors_in_merchant_form_factor"] == ["Olly"]


def test_cohort_summary_handles_unknown_merchant_form_factor():
    """When merchant form factor wasn't classifiable, uniqueness check
    returns False (don't claim moat we can't verify)."""
    from services.product_form_factor_classifier import (
        build_cohort_form_factor_summary,
    )
    result = build_cohort_form_factor_summary(
        merchant_brand="Mystery Brand",
        merchant_form_factor=None,  # unclassified
        competitor_brands=[{"name": "AG1"}],
        cohort_audit_runs=[],
    )
    assert result["merchant_owns_unique_form_factor"] is False


# ---------------------------------------------------------------------
# Integration: build_structured_report surfaces classification
# ---------------------------------------------------------------------


def test_build_structured_report_includes_form_factor_in_product_block():
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="Grüns",
        merchant_pdp_url="https://gruns.co/p",
        product_title="Greens Gummies",
        product_vendor="Grüns",
        product_type="daily greens gummies",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        provider="gemini",
    )
    product = report.get("product") or {}
    assert product.get("form_factor") == "gummy"


def test_build_structured_report_includes_cohort_form_factor():
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="Grüns",
        merchant_pdp_url="https://gruns.co/p",
        product_title="Greens Gummies",
        product_vendor="Grüns",
        product_type="daily greens gummies",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        provider="gemini",
    )
    cohort_ff = report.get("cohort_form_factor")
    assert cohort_ff is not None
    assert "form_factor_summary" in cohort_ff
    assert cohort_ff["merchant_form_factor"] == "gummy"
