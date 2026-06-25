"""Unit tests for build_product_competitiveness — product-first view: discovery
(non-branded, winnable) appearance + who AI recommends instead, with branded
name queries reported separately as low-value."""
from __future__ import annotations

from services.agent_center_bd_report_service import build_product_competitiveness


def _row(query, axis, merchant_cited_runs=0, competitors=None, grounded=True):
    # `grounded` = the AI returned real sources for this query. A grounded row
    # carries a citation run + cited hosts so it counts toward the denominator;
    # an ungrounded row (no sources) is inconclusive and excluded.
    ss = {"merchant_cited_runs": merchant_cited_runs}
    if grounded:
        ss["runs_with_citations"] = max(1, merchant_cited_runs)
        ss["top_cited_hosts"] = [{"host": "example.com", "times_cited": 1}]
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "source_summary": ss,
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


def test_ungrounded_discovery_excluded_from_denominator():
    # A discovery query the AI didn't ground is inconclusive — not "appeared",
    # not "missed", and not in the total. Honest rate over grounded only.
    per_prompt = [
        _row("best hair oil for damaged hair", "category", merchant_cited_runs=1),
        _row("best hair oil for split ends", "category", merchant_cited_runs=0,
             competitors=["Olaplex"]),
        _row("best hair oil for frizz", "category", grounded=False),  # ungrounded
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["discovery"]["total"] == 2          # ungrounded one excluded
    assert pc["discovery"]["appeared"] == 1
    assert pc["discovery"]["ungrounded"] == 1
    assert pc["grounding_unavailable"] is False


def test_all_discovery_ungrounded_flags_couldnt_measure():
    # Discovery queries ran but the AI grounded NONE -> couldn't measure this
    # run; must NOT report a false "appears in 0 of N".
    per_prompt = [
        _row("best hair oil for damaged hair", "category", grounded=False),
        _row("bond repair hair oil for breakage", "category", grounded=False),
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["has_discovery"] is False
    assert pc["grounding_unavailable"] is True
    assert pc["discovery"]["total"] == 0
    assert pc["discovery"]["ungrounded"] == 2


def _vrow(query, axis, verdicts):
    # row with per-model provider_verdicts (win/loss/absent) + grounding so the
    # aggregate path counts it.
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "source_summary": {"runs_with_citations": 1,
                           "top_cited_hosts": [{"host": "x.com", "times_cited": 1}]},
        "provider_verdicts": verdicts,
    }


def test_per_model_discovery_split_and_divergence():
    per_prompt = [
        _vrow("best hair oil for damaged hair", "category",
              {"gemini": "win", "chatgpt": "loss", "deepseek": "win"}),
        _vrow("bond repair hair oil for breakage", "category",
              {"gemini": "win", "chatgpt": "win"}),
        _vrow("hair oil for split ends", "category",
              {"gemini": "loss", "chatgpt": "absent"}),  # chatgpt ungrounded->skip
    ]
    pc = build_product_competitiveness(per_prompt)
    bm = pc["by_model"]
    # gemini graded all 3 (win,win,loss) -> 2/3; chatgpt graded 2 (loss,win) -> 1/2
    assert bm["gemini"] == {"appeared": 2, "total": 3, "rate": round(2 / 3, 3)}
    assert bm["chatgpt"] == {"appeared": 1, "total": 2, "rate": 0.5}
    # divergence: query 1 (gemini win, chatgpt loss) — deepseek excluded (verify)
    div_qs = [d["query"] for d in pc["model_divergence"]]
    assert "best hair oil for damaged hair" in div_qs
    d0 = next(d for d in pc["model_divergence"] if d["query"] == "best hair oil for damaged hair")
    assert d0["won"] == ["gemini"] and d0["lost"] == ["chatgpt"]
    # query 2 both win -> not divergent
    assert "bond repair hair oil for breakage" not in div_qs


def test_per_model_excludes_branded_and_deepseek():
    per_prompt = [
        _vrow("is anuko legit", "review", {"gemini": "win", "chatgpt": "win"}),  # branded
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["by_model"] == {}          # branded not in per-model discovery
    assert pc["model_divergence"] == []
