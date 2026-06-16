"""Step 3 mode-purity: a per_sku trend must compare only against prior per_sku
runs — legacy runs write different score semantics into the same columns, so
mixing them renders a misleading delta."""

from __future__ import annotations

from services.agent_center_bd_report_service import _per_sku_prior_runs


def test_filters_to_per_sku_only():
    runs = [
        {"run_id": "a", "audit_mode": "per_sku", "visibility_score_avg": 40},
        {"run_id": "b", "audit_mode": "legacy", "visibility_score_avg": 60},
        {"run_id": "c", "audit_mode": None, "visibility_score_avg": 50},  # unknown → excluded
        {"run_id": "d"},  # no audit_mode → excluded
        "not-a-dict",  # robustness
    ]
    out = _per_sku_prior_runs(runs)
    assert [r["run_id"] for r in out] == ["a"]


def test_empty_and_none():
    assert _per_sku_prior_runs(None) == []
    assert _per_sku_prior_runs([]) == []
