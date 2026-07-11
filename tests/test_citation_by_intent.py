"""Step 2 — fine intent-axis classification + per-intent citation breakdown.
Additive layer over the coarse `axis`; snapshot-only. See
PIVOTA-Agent/docs/ai_readiness_query_axes_build_plan.md.
"""

from services import agent_center_bd_report_service as m


def test_intent_axis_classification():
    f = m._intent_axis_for
    # category-tagged → head vs problem/need-framed
    assert f("best collagen", "category") == "category_head"
    assert f("what collagen should I buy", "category") == "category_head"
    assert f("top collagen", "category") == "category_head"
    assert f("best collagen for sleep", "category") == "problem_jtbd"
    assert f("what helps with sleep", "category") == "problem_jtbd"
    assert f("collagen for women over 40", "category") == "problem_jtbd"
    # other coarse axes
    assert f("vegan collagen", "attribute") == "constraint"
    assert f("halal collagen sticks", "sidewalk") == "constraint"
    assert f("is bb lab legit", "review") == "trust"
    assert f("bb lab collagen reviews", "review") == "trust"
    assert f("where can I buy bb lab collagen", "intent") == "navigational"
    assert f("bb lab collagen 2 box", "identity") == "navigational"
    assert f("collagen for picky eaters", "custom") == "custom"


def test_citation_by_intent_buckets_and_rate():
    per_prompt = [
        {"normalized_query": "best collagen", "axis": "category",
         "source_summary": {"merchant_cited_runs": 0}},
        {"normalized_query": "best collagen for sleep", "axis": "category",
         "source_summary": {"merchant_cited_runs": 2}},
        {"normalized_query": "what helps with sleep", "axis": "category",
         "source_summary": {"merchant_cited_runs": 0}},
        {"normalized_query": "is bb lab legit", "axis": "review",
         "source_summary": {"merchant_cited_runs": 1}},
        {"normalized_query": "where can I buy bb lab", "axis": "intent",
         "source_summary": {"merchant_cited_runs": 1}},
    ]
    out = m._citation_by_intent(per_prompt)
    # PR #1323 added the strict SKU split (sku_cited/sku_rate) to every bucket;
    # these rows carry no sku_cited_runs, so the strict counts are zero.
    assert out["category_head"] == {
        "cited": 0, "total": 1, "rate": 0.0, "sku_cited": 0, "sku_rate": 0.0}
    assert out["problem_jtbd"] == {
        "cited": 1, "total": 2, "rate": 0.5, "sku_cited": 0, "sku_rate": 0.0}
    assert out["trust"] == {
        "cited": 1, "total": 1, "rate": 1.0, "sku_cited": 0, "sku_rate": 0.0}
    assert out["navigational"] == {
        "cited": 1, "total": 1, "rate": 1.0, "sku_cited": 0, "sku_rate": 0.0}


def test_citation_by_intent_handles_empty_and_garbage():
    assert m._citation_by_intent(None) == {}
    assert m._citation_by_intent([]) == {}
    # rows missing source_summary count as not-cited, never crash
    out = m._citation_by_intent([{"normalized_query": "best collagen", "axis": "category"}])
    assert out["category_head"] == {
        "cited": 0, "total": 1, "rate": 0.0, "sku_cited": 0, "sku_rate": 0.0}


def test_brand_citation_by_intent_rolls_up_skus():
    reports = [
        {"citation_by_intent": {"problem_jtbd": {"cited": 1, "total": 2, "rate": 0.5},
                                 "trust": {"cited": 1, "total": 1, "rate": 1.0}}},
        {"citation_by_intent": {"problem_jtbd": {"cited": 0, "total": 3, "rate": 0.0}}},
        {"no_citation_by_intent": True},  # tolerated
    ]
    out = m._brand_citation_by_intent(reports)
    assert out["problem_jtbd"] == {"cited": 1, "total": 5, "skus": 2, "rate": 0.2}
    assert out["trust"] == {"cited": 1, "total": 1, "skus": 1, "rate": 1.0}
    assert m._brand_citation_by_intent(None) == {}
