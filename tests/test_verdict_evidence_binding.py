"""
Phase C-4 (PR-A): verdict_for now binds explanation text to this
audit's actual evidence — failed query counts, top retailers cited
instead of the merchant, gap percentages, peer-framing string.

These tests assert the new behaviour for each of the 5 tiers, plus
the legacy fallback path used by callers that don't pass `evidence`.
"""

from __future__ import annotations

from typing import Any, Dict

from services.agent_center_bd_report_service import (
    VERDICT_INVISIBLE,
    VERDICT_MISATTRIBUTED,
    VERDICT_PARTIAL,
    VERDICT_STRONG,
    VERDICT_VIA_RETAILERS,
    verdict_for,
)


def _evidence(**overrides) -> Dict[str, Any]:
    """Sensible default evidence dict; overrides per-test."""
    base = {
        "attribution_runs_total": 9,
        "merchant_cited_runs": 0,
        "top_retailers": ["sephora.com", "ulta.com", "vogue.com"],
        "competitive_pressure_framing": None,
        "category_score": 70,
        "gap_pct": 70,
        "failed_attribution_query_sample": [
            "best hyaluronic acid serum under $50",
            "drugstore retinol that actually works",
        ],
    }
    base.update(overrides)
    return base


# -----------------------------------------------------------------
# INVISIBLE tier — both scores below invisible_max
# -----------------------------------------------------------------


def test_invisible_explanation_quotes_zero_cited_of_total():
    """0/9 phrased as 'None of 9 cited your URL' (post-clarity rewrite
    that splits the success/loss case into two phrasings — see
    follow-up PR fixing the '1 of 6 surfaced... cited instead'
    contradiction)."""
    label, explanation = verdict_for(
        visibility_score=5,
        attribution_score=0,
        evidence=_evidence(merchant_cited_runs=0, attribution_runs_total=9),
    )
    assert label == VERDICT_INVISIBLE
    assert "None of 9" in explanation or "0 of 9" in explanation


def test_invisible_explanation_split_phrasing_when_some_succeeded():
    """User-reported case: 1 of 6 cited the merchant; 5 went to
    others. Phrasing must split the success/loss explicitly so it
    doesn't read as contradictory."""
    label, explanation = verdict_for(
        visibility_score=5,
        attribution_score=0,
        evidence=_evidence(merchant_cited_runs=1, attribution_runs_total=6),
    )
    assert label == VERDICT_INVISIBLE
    assert "1 of 6" in explanation
    assert "other 5" in explanation


def test_invisible_explanation_names_top_retailers():
    label, explanation = verdict_for(
        visibility_score=5,
        attribution_score=0,
        evidence=_evidence(top_retailers=["sephora.com", "ulta.com", "vogue.com"]),
    )
    assert label == VERDICT_INVISIBLE
    assert "sephora.com" in explanation
    assert "ulta.com" in explanation


# -----------------------------------------------------------------
# MISATTRIBUTED tier — visibility ≥ 30, attribution < 30
# -----------------------------------------------------------------


def test_misattributed_quotes_visibility_score_and_cited_ratio():
    label, explanation = verdict_for(
        visibility_score=70,
        attribution_score=20,
        evidence=_evidence(
            attribution_runs_total=10, merchant_cited_runs=2,
        ),
    )
    assert label == VERDICT_MISATTRIBUTED
    assert "70/100" in explanation        # visibility score
    assert "2 of 10" in explanation        # cited / total


def test_misattributed_includes_competitive_pressure_framing_when_present():
    cp = "**Competitive pressure: real and immediate.** Of 5 competitors..."
    _, explanation = verdict_for(
        visibility_score=70,
        attribution_score=20,
        evidence=_evidence(competitive_pressure_framing=cp),
    )
    assert "Competitive pressure: real and immediate" in explanation


# -----------------------------------------------------------------
# VIA_RETAILERS tier — category strong, attribution weak
# -----------------------------------------------------------------


def test_via_retailers_quotes_category_score_and_gap_pct():
    label, explanation = verdict_for(
        visibility_score=20,
        attribution_score=10,
        category_visibility_score=80,
        evidence=_evidence(category_score=80, gap_pct=70),
    )
    assert label == VERDICT_VIA_RETAILERS
    assert "80/100" in explanation     # category score
    assert "70-point gap" in explanation


def test_via_retailers_names_top_retailers():
    """P0-Q1 gate: retailers are named in the buyer-intent prose
    ONLY when at least one buyer-intent run cited the merchant —
    that's the only case where claiming "the other runs grounded in
    third-party sources" is supported. Test fixture has cited > 0."""
    label, explanation = verdict_for(
        visibility_score=20,
        attribution_score=10,
        category_visibility_score=80,
        evidence=_evidence(
            category_score=80, gap_pct=70,
            attribution_runs_total=9, merchant_cited_runs=2,
            top_retailers=["sephora.com", "amazon.com"],
        ),
    )
    assert label == VERDICT_VIA_RETAILERS
    assert "sephora.com" in explanation


def test_via_retailers_does_not_name_retailers_when_zero_buyer_intent_cited():
    """P0-Q1 regression guard: when cited == 0, the buyer-intent
    grounding claim ("the other N grounded in third-party sources
    including X") is unsupported — `top_retailers` is category-scope
    evidence, not attribution-scope. The Winona prod artifact (run
    932d8261) hit this exact case. Prose must say no grounded source
    was returned, NOT name the category retailers."""
    label, explanation = verdict_for(
        visibility_score=20,
        attribution_score=0,
        category_visibility_score=33,
        evidence=_evidence(
            category_score=33, gap_pct=33,
            attribution_runs_total=3, merchant_cited_runs=0,
            top_retailers=["lookhealthystore.com", "ctfassets.net"],
        ),
    )
    assert label == VERDICT_VIA_RETAILERS
    assert "lookhealthystore.com" not in explanation, (
        f"category-scope hosts must not appear in buyer-intent prose "
        f"when cited=0; got: {explanation}"
    )
    assert "ctfassets.net" not in explanation
    assert (
        "no grounded source" in explanation.lower()
        or "could attribute" in explanation.lower()
    )


# -----------------------------------------------------------------
# STRONG tier — both scores ≥ strong_min
# -----------------------------------------------------------------


def test_strong_quotes_both_scores_and_cited_ratio():
    label, explanation = verdict_for(
        visibility_score=85,
        attribution_score=70,
        evidence=_evidence(
            attribution_runs_total=10, merchant_cited_runs=7,
        ),
    )
    assert label == VERDICT_STRONG
    assert "85/100" in explanation
    assert "70/100" in explanation
    assert "7 of 10" in explanation


# -----------------------------------------------------------------
# PARTIAL tier — fallthrough
# -----------------------------------------------------------------


def test_partial_quotes_scores_and_failed_query_sample():
    label, explanation = verdict_for(
        visibility_score=50,
        attribution_score=40,
        evidence=_evidence(
            attribution_runs_total=10,
            merchant_cited_runs=4,
            failed_attribution_query_sample=[
                "best night cream for dry skin",
                "hydrating serum routine",
            ],
        ),
    )
    assert label == VERDICT_PARTIAL
    assert "50/100" in explanation
    assert "40/100" in explanation
    assert "4" in explanation and "10" in explanation
    # At least one failed query verbatim (truncated).
    assert "best night cream" in explanation


# -----------------------------------------------------------------
# Backward compatibility: evidence=None falls back to generic prose
# (calibration-prefix unit tests rely on this; merchant audit always
# passes evidence after PR-A).
# -----------------------------------------------------------------


def test_evidence_none_returns_generic_pitch_free_explanation():
    """Existing callers that don't pass evidence still get a label +
    a sensible (pitch-free) generic sentence."""
    label, explanation = verdict_for(
        visibility_score=5,
        attribution_score=0,
    )
    assert label == VERDICT_INVISIBLE
    # No data-binding because no evidence — but also no pitch macros.
    assert "Pivota" not in explanation
    assert "12%" not in explanation
    assert "agentic-commerce" not in explanation


def test_peer_calibration_prefix_still_prepended():
    """Phase 2c calibration prefix still works with the refactor."""
    _, explanation = verdict_for(
        visibility_score=15,
        attribution_score=10,
        peer_thresholds={
            "invisible_max": 25,
            "strong_min": 75,
            "misattributed_attr_max": 20,
        },
    )
    assert "Calibrated thresholds" in explanation
    assert "INVISIBLE < 25/100" in explanation
