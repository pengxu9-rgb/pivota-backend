"""Increment B: first_party and authority must not credit negative-verdict runs.

After #716 fixed sku_mention, the over-credit moved to first_party (merchant-
domain grounding counted regardless of verdict — ~19 of Collagen Garden's 28
points on a 3/40-visible SKU) and authority (its `_text_mentions_any` branch had
the same ungated flaw). The prior regression tests used disjoint inputs and never
covered the grounding-present + NEGATIVE-verdict overlap — the actual failure
mode. This test does, for BOTH buckets.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.agent_center_bd_report_service as bd

TITLE = "Triple Collagen Garden edition"
MERCHANT_URL = "https://ownist.com/products/garden"


def _sku_ctx() -> Dict[str, Any]:
    return {"product": {"title": TITLE, "brand": "Ownist", "canonical_url": MERCHANT_URL,
                        "content_key": "ck_g"},
            "sku": {"title": "Garden Gift Set"}, "sku_key": "p4::v::g", "product_key": "p4"}


def _run(query: str, *, source_domains: List[str], product_visible: Optional[bool] = None,
         correct_sku: Optional[bool] = None, answer: str = "") -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    if product_visible is not None:
        parsed["product_visible"] = product_visible
    if correct_sku is not None:
        parsed["correct_sku"] = correct_sku
    if answer:
        parsed["answer"] = answer
    # Vertex redirector URI + real domain in title (the live shape).
    sources = [{"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
                "title": d} for d in source_domains]
    return {"query": query, "provider": "gemini", "parsed": parsed,
            "evidence_excerpt": answer, "grounding_sources": sources}


def _num(runs: List[Dict[str, Any]], bucket: str) -> int:
    _score, breakdown = bd.compute_citation_score(_sku_ctx(), runs)
    return breakdown[bucket]["numerator"]


def test_first_party_excludes_merchant_grounding_in_negative_answers() -> None:
    runs = [
        # ownist.com grounded but the answer DENIES the product -> must NOT count.
        _run("q1", source_domains=["ownist.com"], product_visible=False,
             answer="'Garden Gift Set' is not the Ownist Triple Collagen Garden edition"),
        # ownist.com grounded and product affirmed -> counts.
        _run("q2", source_domains=["ownist.com"], product_visible=True,
             answer="Ownist Triple Collagen Garden edition is available"),
    ]
    assert _num(runs, "first_party_rate") == 1  # only the affirmed run


def test_authority_excludes_name_echo_in_negative_answers() -> None:
    runs = [
        # external source + title echo, but NEGATIVE verdict -> authority must NOT count.
        _run("q1", source_domains=["sephora.com"], product_visible=False,
             answer="Sephora has no Ownist Triple Collagen Garden edition listing"),
        # external source + title echo + affirmed -> counts.
        _run("q2", source_domains=["sephora.com"], product_visible=True,
             answer="Sephora carries Ownist Triple Collagen Garden edition"),
        # external source + affirmative structured flag (no text needed) -> counts.
        _run("q3", source_domains=["holiholic.com"], correct_sku=True, answer="confirmed match"),
    ]
    assert _num(runs, "authority_near_variant_rate") == 2  # q2 + q3, not q1
