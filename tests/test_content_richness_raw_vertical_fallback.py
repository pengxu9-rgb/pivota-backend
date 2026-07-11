"""P1-3 (operator review 2026-07-10): honest content scoring on URL-wedge runs.

The vertical_structure bucket (20 pts of content_richness) reads only curated
vertical artifacts (electronics_meta.*, beauty_* tables, fashion fields) that
NO URL-wedge SKU can have — so a spec-rich real PDP scored a flat 0/20, dragged
content into the "critically thin" band (<40), and the top action told the
merchant their page was "too thin for AI to compare" (live: Mojawa d1e80bc6
content=39 on a rich page; ANUKO 549ace84 same shape). The fix mirrors the
existing product_quality_score/model_readiness raw-PDP fallback: when the
artifacts are absent, score the fetched page's own category signals, capped at
16/20 (raw signals can't prove curated vertical meta), with `missing` still
pointing at the enrichment artifacts.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.agent_center_bd_report_service import (  # noqa: E402
    build_synthetic_sku_context,
    compute_content_richness_score,
)

MID = "merch_test"


def _electronics_item(attrs):
    return {
        "sku_key": "urlwedge:elec1",
        "product_key": "urlwedge:elec1",
        "title": "Purra Run Bone Conduction Headphones",
        "vendor": "Mojawa",
        "product_type": "Headphones",
        "pdp_url": "https://mojawa.com/products/purra-run",
        "attributes_raw": attrs,
    }


RICH_ATTRS = {
    "source": "shopify_native",
    "tags": ["ip67 waterproof", "bluetooth 5.3", "15-hour battery life", "open-ear"],
    "description": (
        "Purra Run bone conduction headphones are engineered for runners and "
        "cyclists: IP67 waterproof rating survives sweat and rain, Bluetooth "
        "5.3 keeps a stable connection, and the 15-hour battery outlasts a "
        "marathon weekend. ENC AI wind-noise reduction keeps calls clear at "
        "speeds up to 30km/h, while the open-ear design leaves you aware of "
        "traffic. In the box: charging clip, spare ear hooks, and a quick-start "
        "guide. Weighs 28 grams with an adjustable titanium frame."
    ),
    "variants": [{"title": "Black"}, {"title": "Blue"}],
}


def test_rich_raw_pdp_scores_vertical_structure_without_artifacts():
    ctx = build_synthetic_sku_context(_electronics_item(RICH_ATTRS), MID)
    score, breakdown = compute_content_richness_score(ctx)
    bucket = breakdown["vertical_structure"]
    assert bucket["points"] == 12  # spec_tags + deep_description + variant_structure
    assert "raw PDP category signals" in bucket["reason"]
    # the recommendation still targets the real gap: enrichment artifacts
    assert any(m.startswith("electronics_meta.") for m in breakdown.get("missing_inputs", []))
    # the full score escapes the "critically thin" band (<40) that produced the
    # false "too thin" lead action on the Mojawa pilot
    assert score >= 40


def test_raw_fallback_is_capped_below_enriched_ceiling():
    attrs = dict(RICH_ATTRS)
    attrs["offers"] = {"price": "159.99"}  # + structured_metadata → all 4 signals
    ctx = build_synthetic_sku_context(_electronics_item(attrs), MID)
    _, breakdown = compute_content_richness_score(ctx)
    bucket = breakdown["vertical_structure"]
    assert bucket["points"] == 16
    assert bucket["max"] == 20


def test_thin_page_still_scores_zero():
    ctx = build_synthetic_sku_context(
        _electronics_item({"source": "pdp_metadata", "description": "Headphones."}),
        MID,
    )
    _, breakdown = compute_content_richness_score(ctx)
    bucket = breakdown["vertical_structure"]
    assert bucket["points"] == 0
    assert bucket["reason"] == "data unavailable"


def test_beauty_synthetic_gets_the_same_fallback():
    item = {
        "sku_key": "urlwedge:beauty1",
        "product_key": "urlwedge:beauty1",
        "title": "Anuko Nourishing Hair Butter",
        "vendor": "Anuko",
        "product_type": "Hair Butter",
        "pdp_url": "https://tryanuko.com/products/hair-butter",
        "attributes_raw": {
            "source": "shopify_native",
            "tags": ["vegan", "green tea", "shea butter"],
            "description": "x" * 450,
            "options": [{"name": "Size"}],
        },
    }
    ctx = build_synthetic_sku_context(item, MID)
    _, breakdown = compute_content_richness_score(ctx)
    bucket = breakdown["vertical_structure"]
    assert bucket["points"] == 12
    assert "raw PDP category signals" in bucket["reason"]


def test_enriched_artifacts_bypass_the_fallback():
    ctx = build_synthetic_sku_context(_electronics_item(RICH_ATTRS), MID)
    ctx["product"]["product_payload"] = {
        "electronics_meta": {
            "spec_groups": [{"name": "Audio"}],
            "in_box": ["charging clip"],
            "pro_reviews": [{"host": "rtings.com"}],
        }
    }
    _, breakdown = compute_content_richness_score(ctx)
    bucket = breakdown["vertical_structure"]
    assert bucket["reason"] == "electronics structure coverage"
    assert bucket["points"] == 14  # 6 + 4 + 4, compare/configurator absent
