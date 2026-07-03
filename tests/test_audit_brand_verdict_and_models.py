"""Per-SKU brand verdict honesty (P0-2) + per-model rollup (P1a)."""

from __future__ import annotations

from services.agent_center_bd_report_service import (
    BRAND_STATE_BLOCKED_PRE_INDEX,
    BRAND_STATE_INSUFFICIENT_SIGNAL,
    BRAND_STATE_SCORED,
    _brand_citation_by_provider,
    _models_cited_for_sku,
    _per_sku_brand_verdict,
    verdict_for,
)


def test_all_blocked_brand_is_pre_index_not_invisible():
    state, label, explanation = _per_sku_brand_verdict(
        median_citation=None, total_skus=3, blocked_count=3,
    )
    assert state == BRAND_STATE_BLOCKED_PRE_INDEX
    # The pre-fix bug emitted verdict_for(0,0)'s bottom-tier label. Assert we
    # are NOT emitting that, and that the copy is honest about indexing.
    false_label, _ = verdict_for(0, 0)
    assert label != false_label
    assert "index" in explanation.lower()


def test_no_signal_but_not_all_blocked_is_insufficient():
    state, label, _ = _per_sku_brand_verdict(
        median_citation=None, total_skus=3, blocked_count=1,
    )
    assert state == BRAND_STATE_INSUFFICIENT_SIGNAL


def test_real_citation_signal_uses_normal_verdict():
    state, label, _ = _per_sku_brand_verdict(
        median_citation=72, total_skus=3, blocked_count=0,
    )
    assert state == BRAND_STATE_SCORED
    assert label == verdict_for(72, 72)[0]


def test_invisible_verdict_with_citations_does_not_claim_total_absence():
    """BUG C regression (ANUKO 2026-07-02): a low-median INVISIBLE verdict must
    NOT assert 'your URL did not appear in any grounded source' when the brand
    was first-party cited on real prompts. With evidence, the explanation
    reports the honest 'cited in N of M' instead."""
    from services.agent_center_bd_report_service import VERDICT_INVISIBLE

    state, label, explanation = _per_sku_brand_verdict(
        median_citation=25,  # below invisible_max=30 → INVISIBLE label
        total_skus=3,
        blocked_count=0,
        evidence={"attribution_runs_total": 48, "merchant_cited_runs": 10},
    )
    assert state == BRAND_STATE_SCORED
    assert label == VERDICT_INVISIBLE
    assert "did not appear in any grounded source" not in explanation
    assert "cited in 10 of 48" in explanation


def test_invisible_with_zero_citations_still_reports_absence_honestly():
    """When the brand truly earned zero first-party citations, the honest
    absolute ('None of M ... cited your url') is still correct — the fix only
    removes the FALSE absolute, not the true one."""
    from services.agent_center_bd_report_service import VERDICT_INVISIBLE

    _state, label, explanation = _per_sku_brand_verdict(
        median_citation=12,
        total_skus=3,
        blocked_count=0,
        evidence={"attribution_runs_total": 45, "merchant_cited_runs": 0},
    )
    assert label == VERDICT_INVISIBLE
    assert "None of 45" in explanation


def _cited_entry(first_party_num=0, sku_mention_num=0, score=0, prompts=10):
    return {
        "score": score,
        "prompts": prompts,
        "breakdown": {
            "first_party_rate": {"numerator": first_party_num, "denominator": 10},
            "sku_mention_rate": {"numerator": sku_mention_num, "denominator": 10},
        },
    }


def test_models_cited_counts_only_real_surfacing():
    cbp = {
        "gemini": _cited_entry(first_party_num=4, score=60),   # cited
        "deepseek": _cited_entry(sku_mention_num=2, score=40),  # mentioned -> cited
        "chatgpt": _cited_entry(score=20),                      # score but no mention/cite
        "claude": {"status": "probe_failed", "error": "x"},     # excluded from `of`
    }
    out = _models_cited_for_sku(cbp)
    assert out == {"cited": 2, "of": 3}


def test_brand_citation_by_provider_rolls_up_across_skus():
    reports = [
        {"citation_by_provider": {"gemini": _cited_entry(first_party_num=3, score=80, prompts=40)}},
        {"citation_by_provider": {"gemini": _cited_entry(score=20, prompts=40)}},
        {"citation_by_provider": {"gemini": {"status": "probe_failed"}}},
    ]
    rollup = _brand_citation_by_provider(reports)
    assert set(rollup) == {"gemini"}
    g = rollup["gemini"]
    assert g["skus_scored"] == 2          # probe_failed excluded
    assert g["skus_cited"] == 1           # only the first_party_num>0 one
    assert g["prompts"] == 80
    assert g["median"] is not None
