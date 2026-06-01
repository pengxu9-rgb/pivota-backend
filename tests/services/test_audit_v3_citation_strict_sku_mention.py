"""Regression test: sku_mention must not count name-echoes in negative answers.

The 5th Ownist run scored Triple Collagen Garden citation 46 on only 4/40
product-visible runs, because sku_mention_rate counted 35/40 — providers echo
the queried product name even when DENYING the product is the right/visible one
("'Garden Gift Set' does not contain 'Ownist Triple Collagen'"). The strict fix
gates the loose text-mention branch on non-negative visibility: a
`product_visible: False` name-echo must not count as a SKU mention. Structured
affirmative flags (sku_mentioned/correct_sku) and unknown-visibility mentions
still count.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.agent_center_bd_report_service as bd

TITLE = "Triple Collagen Garden edition"


def _sku_ctx() -> Dict[str, Any]:
    # No canonical_url / grounding sources -> isolates the sku_mention bucket
    # (first_party stays 0), so the test asserts purely on sku_mention counting.
    return {"product": {"title": TITLE, "brand": "Ownist"}, "sku": {"title": TITLE},
            "sku_key": "p4::v::g", "product_key": "p4"}


def _run(query: str, *, product_visible, answer: str) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"answer": answer}
    if product_visible is not None:
        parsed["product_visible"] = product_visible
    return {"query": query, "provider": "gemini", "parsed": parsed,
            "evidence_excerpt": answer}


def _sku_mention_numerator(runs: List[Dict[str, Any]]) -> int:
    _score, breakdown = bd.compute_citation_score(_sku_ctx(), runs)
    return breakdown["sku_mention_rate"]["numerator"]


def test_negative_visibility_name_echo_not_counted() -> None:
    runs = [
        # Negative verdict but the answer echoes the product name -> must NOT count.
        _run("where can I buy Ownist Triple Collagen Garden edition", product_visible=False,
             answer="'Garden Gift Set' is generic and does not contain 'Ownist Triple Collagen Garden edition'"),
        _run("shop Ownist Triple Collagen Garden edition online", product_visible=False,
             answer="The query 'Ownist Triple Collagen Garden edition' does not match a visible product"),
        _run("Ownist Triple Collagen Garden edition for sale", product_visible=False,
             answer="No listing found for Ownist Triple Collagen Garden edition"),
        # Positive verdict, name present -> counts.
        _run("Ownist Triple Collagen Garden edition", product_visible=True,
             answer="Ownist Triple Collagen Garden edition is available"),
        _run("buy Ownist Triple Collagen Garden edition", product_visible=True,
             answer="Yes — Ownist Triple Collagen Garden edition, in stock"),
    ]
    # Only the 2 product-visible runs count; the 3 negative name-echoes do not.
    assert _sku_mention_numerator(runs) == 2


def test_unknown_visibility_mention_still_counts() -> None:
    # product_visible absent (provider gave no verdict) + name present -> still
    # counts. The fix only excludes EXPLICIT negatives, not unknowns.
    runs = [_run("Ownist Triple Collagen Garden edition review", product_visible=None,
                 answer="Reviews mention Ownist Triple Collagen Garden edition favorably")]
    assert _sku_mention_numerator(runs) == 1


def test_affirmative_flag_counts_even_without_text() -> None:
    # Structured correct_sku=True is an affirmative provider signal; counts even
    # if the excerpt text doesn't repeat the title.
    run = {"query": "q", "provider": "gemini",
           "parsed": {"correct_sku": True, "product_visible": True, "answer": "yes"},
           "evidence_excerpt": "yes"}
    assert _sku_mention_numerator([run]) == 1
