"""Regression for #1504: a product deliberately held out of serving must not
have its audit verdict skewed by the serving_eligibility bucket.

serving_eligibility (0/30) measures Pivota's OWN serving readiness, not the
brand's organic AI visibility. For a held-out (decision-grade / BD) product it
scores 0 and — because _fixability_for weights routability at 1.0 — dominates
primary_gaps and leads the brand fix queue, conflating "Pivota hasn't served
this" with "brand isn't AI-visible".

ANNOTATE fix (score math unchanged):
  1. _primary_gaps flags the serving gap with internal_state=True and a `why`
     that says it measures Pivota serving readiness, not organic AI visibility.
  2. The brand priority_queue demotes internal-state gaps below every
     non-internal gap, so the fix narrative headlines a real visibility gap.
A serving-eligible SKU has no serving gap, so its output is byte-identical
(the golden suite is run separately).
"""

from __future__ import annotations

import pytest

from services.agent_center_bd_report_service import (
    _INTERNAL_STATE_WHY,
    _is_internal_state_gap,
    _primary_gaps,
    build_brand_rollup,
)


def _scores_with_serving_gap():
    """A held-out product: routability serving_eligibility scores 0/30, plus a
    genuine content gap that SHOULD lead the narrative."""
    return {
        "content_richness": {
            "score": 60,
            "breakdown": {
                "enrichment_coverage": {"points": 4, "max": 20, "reason": "thin"},
                "total": 4,
            },
        },
        "routability": {
            "score": 0,
            "breakdown": {
                "serving_eligibility": {"points": 0, "max": 30, "reason": "held out"},
                "total": 0,
            },
        },
    }


def _scores_serving_eligible():
    """A serving product: serving_eligibility is full, so it never surfaces as a
    gap — the internal-state annotation must not appear anywhere."""
    return {
        "content_richness": {
            "score": 60,
            "breakdown": {
                "enrichment_coverage": {"points": 4, "max": 20, "reason": "thin"},
                "total": 4,
            },
        },
        "routability": {
            "score": 30,
            "breakdown": {
                "serving_eligibility": {"points": 30, "max": 30, "reason": "live"},
                "total": 30,
            },
        },
    }


def test_serving_gap_is_annotated_internal_state():
    gaps = _primary_gaps(_scores_with_serving_gap(), cap=100)
    serving = next(g for g in gaps if g["bucket"] == "serving_eligibility")
    assert serving.get("internal_state") is True
    assert serving["why"] == _INTERNAL_STATE_WHY
    # The `why` names serving readiness, not organic visibility.
    assert "serving readiness" in serving["why"]
    # Score math is untouched.
    assert serving["points"] == 0
    assert serving["max"] == 30
    assert serving["gap"] == 30


def test_non_serving_gaps_are_not_annotated():
    gaps = _primary_gaps(_scores_with_serving_gap(), cap=100)
    content = next(g for g in gaps if g["bucket"] == "enrichment_coverage")
    assert "internal_state" not in content


def test_serving_eligible_sku_has_no_internal_annotation():
    """Byte-identical guarantee at the gap level: a serving SKU emits no
    serving_eligibility gap and no internal_state marker."""
    gaps = _primary_gaps(_scores_serving_eligible(), cap=100)
    assert all(g["bucket"] != "serving_eligibility" for g in gaps)
    assert all("internal_state" not in g for g in gaps)


def test_is_internal_state_gap_scoped_to_serving_eligibility():
    assert _is_internal_state_gap("routability", "serving_eligibility") is True
    # Other routability buckets are real, fixable gaps — not internal-state.
    assert _is_internal_state_gap("routability", "offer_orderability") is False
    assert _is_internal_state_gap("content_richness", "enrichment_coverage") is False


def test_priority_queue_never_headlines_internal_serving_gap():
    """The brand fix queue must lead with a real visibility gap, not the
    held-out serving-state item — even though the serving gap has the larger
    raw score gap (100 vs 40) and equal fixability."""
    scores = _scores_with_serving_gap()
    report = {
        "sku_key": "s1",
        "product_key": "p1",
        "band": "blocked",
        "scores": scores,
        "primary_gaps": _primary_gaps(scores, cap=100),
    }
    rollup = build_brand_rollup([report], "merch_x")
    pq = rollup["priority_queue"]
    assert pq, "expected a non-empty priority queue"
    # Serving-state gap is present (score math unchanged)...
    assert any(row["bucket"] == "serving_eligibility" for row in pq)
    # ...but it never leads.
    assert pq[0]["bucket"] != "serving_eligibility"
    assert pq[0]["bucket"] == "enrichment_coverage"


def test_priority_queue_order_unchanged_without_internal_gaps():
    """A serving cohort (no internal-state gaps) keeps the plain priority-desc
    order — the demotion is a no-op, so serving output is unaffected."""
    scores = _scores_serving_eligible()
    report = {
        "sku_key": "s1",
        "product_key": "p1",
        "band": "partial",
        "scores": scores,
        "primary_gaps": _primary_gaps(scores, cap=100),
    }
    rollup = build_brand_rollup([report], "merch_x")
    pq = rollup["priority_queue"]
    # Only the real content gap remains; nothing was demoted or annotated.
    assert all(row["bucket"] != "serving_eligibility" for row in pq)
    assert [row["priority_score"] for row in pq] == sorted(
        (row["priority_score"] for row in pq), reverse=True
    )


def test_internal_state_why_has_no_banned_jargon():
    """The override `why` is rendered verbatim to merchants, so it must clear the
    same jargon guard tests/test_audit_gap_labels.py enforces."""
    banned = ("_", "/", "score", "pipeline", "snapshot", "content_key")
    blob = _INTERNAL_STATE_WHY.lower()
    for token in banned:
        assert token not in blob, f"banned jargon {token!r} in internal-state why"
