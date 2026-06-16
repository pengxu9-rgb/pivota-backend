"""Step 3: per_sku runs must persist non-NULL run-level scores (visibility =
mean per-SKU overall/weakest-dimension; attribution = mean citation score) so the
run-over-run trend isn't permanently empty."""

from __future__ import annotations

from services.agent_center_bd_report_service import _per_sku_run_aggregate


def _sku(identity, content, routability, citation):
    return {
        "scores": {
            "identity": {"score": identity},
            "content_richness": {"score": content},
            "routability": {"score": routability},
            "citation": {"score": citation},
        }
    }


def test_overall_is_min_dimension_and_citation_is_clean():
    # _overall_score is the SKU's weakest dimension → 40 and 20.
    reports = [_sku(80, 60, 70, 40), _sku(50, 50, 50, 20)]
    agg = _per_sku_run_aggregate(reports)
    assert agg["avg_visibility"] == 30.0  # mean(40, 20)
    assert agg["avg_attribution"] == 30.0  # mean citation(40, 20)
    assert agg["avg_category_visibility"] is None
    assert agg["products_succeeded"] == 2
    assert agg["products_failed"] == 0


def test_empty_reports_yield_null_scores():
    agg = _per_sku_run_aggregate([])
    assert agg["avg_visibility"] is None
    assert agg["avg_attribution"] is None
    assert agg["avg_category_visibility"] is None
    assert agg["products_succeeded"] == 0


def test_robust_to_missing_scores_and_non_dicts():
    # Only the two dicts count; a SKU with no scores → overall 0; no citation → None avg.
    reports = [{"scores": {}}, {"foo": "bar"}, "not-a-dict", None]
    agg = _per_sku_run_aggregate(reports)  # must not raise
    assert agg["products_succeeded"] == 2
    assert agg["avg_visibility"] == 0.0  # mean(0, 0)
    assert agg["avg_attribution"] is None  # no citation scores present


def test_citation_averaged_only_over_present_values():
    reports = [_sku(90, 90, 90, 60), {"scores": {"identity": {"score": 30}}}]
    agg = _per_sku_run_aggregate(reports)
    # overalls: min(90,90,90,60)=60, and min(30)=30 → mean 45
    assert agg["avg_visibility"] == 45.0
    # only the first SKU has a citation score → 60
    assert agg["avg_attribution"] == 60.0
