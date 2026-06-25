"""Unit tests for build_product_competitiveness — product-first view: discovery
(non-branded, winnable) appearance + who AI recommends instead, with branded
name queries reported separately as low-value."""
from __future__ import annotations

from services.agent_center_bd_report_service import build_product_competitiveness


def _row(query, axis, merchant_cited_runs=0, competitors=None):
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "source_summary": {"merchant_cited_runs": merchant_cited_runs},
        "competitors": competitors or [],
    }


def test_splits_discovery_from_branded_and_counts_appearance():
    per_prompt = [
        # discovery (axis=category): "best X" -> category_head, "best X for Y" -> problem_jtbd
        _row("best hair oil", "category", merchant_cited_runs=0,
             competitors=["Cantu Shea Butter for Natural Hair", "&honey Moist Oil"]),
        _row("best hair oil for damaged hair", "category", merchant_cited_runs=1,
             competitors=["Cantu, Shea Butter, Coconut Cream"]),
        _row("hair oil for sensitive scalp", "attribute", merchant_cited_runs=0,
             competitors=["MUCOTA Adllura"]),
        # branded (axis=intent -> navigational, axis=review -> trust)
        _row("where can I buy Anuko Hair Oil", "intent", merchant_cited_runs=1),
        _row("is Anuko legit", "review", merchant_cited_runs=1),
    ]
    pc = build_product_competitiveness(per_prompt)

    assert pc["has_discovery"] is True
    # 3 discovery queries, appeared on 1 (the problem_jtbd one).
    assert pc["discovery"]["total"] == 3
    assert pc["discovery"]["appeared"] == 1
    assert pc["discovery"]["rate"] == round(1 / 3, 3)
    # branded counted separately, appeared on both.
    assert pc["branded"]["total"] == 2
    assert pc["branded"]["appeared"] == 2


def test_competitors_grouped_by_brand_on_discovery_only():
    per_prompt = [
        _row("best hair oil", "category",
             competitors=["Cantu Shea Butter for Natural Hair",
                          "Cantu, Shea Butter, Coconut Cream"]),
        _row("best hair oil for frizz", "category",
             competitors=["Cantu, Leave-In Repair", "&honey Oil"]),
        # branded query competitors must NOT pollute the discovery competitor set
        _row("Anuko Hair Oil reviews", "review",
             competitors=["SomeBrandedOnlyComp"]),
    ]
    pc = build_product_competitiveness(per_prompt)
    names = {c["name"] for c in pc["discovery"]["top_competitors"]}
    # three Cantu SKU strings collapse into one "Cantu".
    assert "Cantu" in names
    assert "&honey" in names
    assert "SomeBrandedOnlyComp" not in names
    cantu = next(c for c in pc["discovery"]["top_competitors"] if c["name"] == "Cantu")
    assert cantu["query_count"] == 2  # cited on both discovery queries


def test_no_discovery_queries_flags_has_discovery_false():
    per_prompt = [
        _row("where can I buy Anuko Hair Oil", "intent", merchant_cited_runs=1),
        _row("is Anuko legit", "review", merchant_cited_runs=1),
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["has_discovery"] is False
    assert pc["discovery"]["total"] == 0
    assert pc["branded"]["total"] == 2
