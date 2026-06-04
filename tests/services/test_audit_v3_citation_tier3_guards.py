from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.agent_center_bd_report_service as bd

TITLE = "Glow Serum"


def _sku_ctx() -> Dict[str, Any]:
    return {
        "product": {
            "title": TITLE,
            "brand": "GlowCo",
            "canonical_url": "https://glow.example/products/glow-serum",
            "content_key": "ck_glow",
        },
        "sku": {"title": TITLE, "sku": "GLOW-30"},
        "sku_key": "glow::v::30",
        "product_key": "glow",
    }


def _breakdown(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    _score, breakdown = bd.compute_citation_score(_sku_ctx(), runs)
    return breakdown


def test_negative_structured_sku_mentioned_does_not_earn_sku_mention() -> None:
    run = {
        "query": "where can I buy Glow Serum",
        "provider": "gemini",
        "parsed": {
            "product_visible": False,
            "correct_sku": False,
            "sku_mentioned": True,
            "answer": "Glow Serum is mentioned, but this is not the product.",
        },
        "evidence_excerpt": "Glow Serum is mentioned, but this is not the product.",
    }

    breakdown = _breakdown([run])
    assert breakdown["sku_mention_rate"]["numerator"] == 0


def test_authority_near_variant_requires_non_negative_external_source() -> None:
    runs = [
        {
            "query": "Glow Serum official listing",
            "provider": "gemini",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "authority_near_variant_found": True,
                "answer": "A related serum page exists, but Glow Serum is not the product.",
            },
            "grounding_sources": [
                {"uri": "https://sephora.com/products/related-serum", "title": "Related serum"},
            ],
            "evidence_excerpt": "A related serum page exists, but Glow Serum is not the product.",
        },
        {
            "query": "Glow Serum reviews",
            "provider": "deepseek",
            "parsed": {
                "authority_near_variant_found": True,
                "answer": "A near variant was identified without a cited external authority.",
            },
            "grounding_sources": [],
            "evidence_excerpt": "A near variant was identified without a cited external authority.",
        },
    ]

    breakdown = _breakdown(runs)
    assert breakdown["authority_near_variant_rate"]["numerator"] == 0


def test_text_only_denial_does_not_earn_sku_mention() -> None:
    run = {
        "query": "Glow Serum availability",
        "provider": "gemini",
        "parsed": {"answer": "No listing found for Glow Serum."},
        "evidence_excerpt": "No listing found for Glow Serum.",
    }

    breakdown = _breakdown([run])
    assert breakdown["sku_mention_rate"]["numerator"] == 0
