"""Readiness test (Workstream B): prove the data-dependent scoring paths produce
a GOOD result when the data the audit reads is actually present.

The synthetic Ownist fixture can't exercise this live — `products_cache` is empty
(no live store) so the quality backfill never runs, leaving content_richness
stuck at 18 / band "blocked". This test feeds a fully-populated sku_ctx (the
shape a real, fully-onboarded merchant would have: product_enrichment,
product_quality_snapshot, beauty_* tables, description/image/freshness) and
asserts content_richness scores high and the band lifts off "blocked". It
de-risks the FIRST real merchant: when onboarding (Workstream A) populates these
tables, the audit scores them correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_center_bd_report_service import (
    compute_content_richness_score,
    _sku_band,
)


def _full_sku_ctx() -> Dict[str, Any]:
    long_desc = (
        "Triple Shine Grape is an Ownist ready-to-drink beauty supplement built "
        "around Belight collagen with niacin and vitamin C for skin radiance; "
        "fourteen-day routine, grape flavor, organic agave sweetened."
    )
    return {
        "sku_key": "p1::v::a", "product_key": "p1", "merchant_id": "m1", "platform": "shopify",
        "product": {
            "title": "Triple Shine Grape", "brand": "Ownist",
            "product_type": "beauty supplement", "category": "beauty",
            "canonical_url": "https://ownist.com/products/triple-shine-1-box",
            "content_key": "ck_p1", "image_url": "https://ownist.com/img/p1.jpg",
            "description": long_desc, "readiness_tier": "commerce_ready",
            "freshness_json": {"last_seen_at": "2026-05-30T00:00:00Z", "stale": False},
            # payload facts so vertical_structure also scores under the generic path
            "product_payload": {"facts": {"servings": 14}, "ingredients": ["Belight", "Niacin"]},
        },
        "sku": {"title": "14 Servings, 2-Week Routine", "barcode": "8809ABC"},
        "product_enrichment": {
            "title_override": "Ownist Triple Shine Grape",
            "summary_short": "Ready-to-drink collagen beauty supplement, grape.",
            "bullet_points": ["Belight collagen", "Niacin + Vitamin C", "14-day routine", "Grape flavor"],
            "usage_scenarios": ["Daily skin-radiance routine"],
            "audience_tags": ["skin health", "K-beauty"],
            "description_markdown": long_desc,
            "llm_safety_flags": [],
        },
        "product_quality_snapshot": {"content_quality_score": 88, "model_readiness_score": 82},
        "beauty_sku_ingredients": [{"name": "Belight"}, {"name": "Niacin"}, {"name": "Vitamin C"}],
        "beauty_usage_guides": [{"text": "One stick daily."}],
        "beauty_compatibility_rules": [{"rule": "avoid with X"}],
        "catalog_field_facts": [{"field": "ingredients", "review_state": "approved"}],
        "offers": [{"offer_id": "o1", "price": "29.00", "currency": "USD", "sku_key": "p1::v::a"}],
    }


def test_content_richness_high_when_data_present() -> None:
    score, breakdown = compute_content_richness_score(_full_sku_ctx())
    # With full data this must be high — vs the synthetic fixture's stuck 18.
    assert score >= 85, f"content_richness only {score}: {breakdown}"
    # Each major bucket actually scored (not 'data unavailable').
    for bucket in ("product_quality_score", "enrichment_coverage", "vertical_structure",
                   "model_readiness"):
        assert breakdown[bucket]["points"] > 0, f"{bucket} unscored: {breakdown[bucket]}"
    # The four enrichment_coverage elements are all present.
    assert breakdown["enrichment_coverage"]["points"] == 20


def test_band_lifts_off_blocked_with_full_data() -> None:
    content_score, _ = compute_content_richness_score(_full_sku_ctx())
    # Realistic full-data dimension scores; content_richness was the blocker (18).
    scores = {
        "identity": {"score": 80},
        "content_richness": {"score": content_score},
        "routability": {"score": 58},
        "citation": {"score": 49},
    }
    band = _sku_band(scores)
    assert band != "blocked", f"band still blocked with full data: {band} ({scores})"
    # Sanity: with content_richness=18 (the fixture floor) it WOULD be blocked.
    blocked = _sku_band({**scores, "content_richness": {"score": 18}})
    assert blocked == "blocked"
