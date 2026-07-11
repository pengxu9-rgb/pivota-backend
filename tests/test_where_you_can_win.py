"""Phase 2: "Where you can win" surfaces the existing per-SKU niche analysis.

A long-tail merchant who fights flagship-owned head terms loses. This deliverable
names the winnable niches (open lanes — demand + attribute fit + no owner) and the
head terms to abandon (controlled by a competitor/retailer/marketplace with real
demand), from the sku_opportunity per_prompt rows the audit already computes.
"""

from __future__ import annotations

from services.agent_center_bd_report_service import build_where_you_can_win


def _report(sku_name, sku_key, rows):
    return {
        "sku_key": sku_key,
        "identity": {"name": sku_name},
        "opportunity": {"per_prompt": rows},
    }


def test_open_lanes_become_ranked_targets():
    reports = [
        _report("Aruen Tofu Collagen", "sku_a", [
            {"query": "vegan collagen for sleep", "normalized_query": "vegan collagen for sleep",
             "open_lane": True, "opportunity_score": 71.0, "attribute_fit": 0.9,
             "demand_state": "open-lane", "attribute_basis": ["vegan", "collagen", "before bed"]},
            {"query": "best collagen", "normalized_query": "best collagen",
             "open_lane": False, "ownership_state": "competitor-owned", "demand_signal": 0.8,
             "who_owns": "bigbrand.com", "cited_evidence": {"competitors_named": ["BigBrand"]}},
        ]),
        _report("BB Lab", "sku_b", [
            {"query": "low molecular collagen stick", "normalized_query": "low molecular collagen stick",
             "open_lane": True, "opportunity_score": 84.0, "attribute_fit": 0.95,
             "demand_state": "open-lane", "attribute_basis": ["low molecular", "collagen", "stick"]},
        ]),
    ]
    out = build_where_you_can_win(reports)
    assert out["has_targets"] is True
    # ranked by opportunity_score desc
    assert [t["query"] for t in out["targets"]] == [
        "low molecular collagen stick", "vegan collagen for sleep",
    ]
    top = out["targets"][0]
    assert top["sku"] == "BB Lab"
    assert top["action"] == "create_answer"
    assert "low molecular" in top["why_you_fit"]


def test_head_terms_with_demand_become_skip():
    reports = [
        _report("X", "sku_x", [
            {"query": "best vitamin c serum", "normalized_query": "best vitamin c serum",
             "open_lane": False, "ownership_state": "retailer-owned", "demand_signal": 0.9,
             "who_owns": "sephora.com", "cited_evidence": {"competitors_named": ["BrandA", "BrandB"]}},
        ]),
    ]
    out = build_where_you_can_win(reports)
    assert out["has_targets"] is False
    assert len(out["skip"]) == 1
    s = out["skip"][0]
    assert s["query"] == "best vitamin c serum"
    assert s["owned_by"] == "sephora.com"
    assert s["competitors_named"] == ["BrandA", "BrandB"]


def test_verbatim_evidence_forwarded_to_target_and_skip():
    """The per_prompt cited_evidence excerpt (the verbatim AI answer = the proof) is
    forwarded onto both winnable targets and don't-fight rows, not dropped."""
    reports = [
        _report("BB Lab", "sku_b", [
            {"query": "collagen for sleep", "normalized_query": "collagen for sleep",
             "open_lane": True, "opportunity_score": 80.0, "attribute_fit": 0.9,
             "demand_state": "open-lane", "attribute_basis": ["collagen", "sleep"],
             "cited_evidence": {"provider": "gemini", "excerpt": "AI suggests magnesium and glycine for sleep.",
                                "cited_hosts": ["healthline.com"], "competitors_named": []}},
            {"query": "best collagen", "normalized_query": "best collagen",
             "open_lane": False, "ownership_state": "retailer-owned", "demand_signal": 0.9,
             "who_owns": "sephora.com",
             "cited_evidence": {"provider": "gemini", "excerpt": "Sephora recommends Vital Proteins.",
                                "cited_hosts": ["sephora.com"], "competitors_named": ["Vital Proteins"]}},
        ]),
    ]
    out = build_where_you_can_win(reports)
    target = out["targets"][0]
    assert target["evidence"]["excerpt"] == "AI suggests magnesium and glycine for sleep."
    assert target["evidence"]["cited_hosts"] == ["healthline.com"]
    skip = out["skip"][0]
    assert skip["evidence"]["excerpt"] == "Sephora recommends Vital Proteins."
    assert skip["evidence"]["cited_hosts"] == ["sephora.com"]


def test_no_demand_losing_term_is_not_skip():
    reports = [
        _report("X", "sku_x", [
            {"query": "obscure term", "normalized_query": "obscure term",
             "open_lane": False, "ownership_state": "competitor-owned", "demand_signal": 0.1,
             "who_owns": "x.com"},
        ]),
    ]
    out = build_where_you_can_win(reports)
    assert out["skip"] == []  # abandoning a no-demand term is meaningless


def test_dedupes_same_query_keeps_best():
    reports = [
        _report("A", "sku_a", [
            {"query": "niche q", "normalized_query": "niche q", "open_lane": True,
             "opportunity_score": 40.0, "attribute_fit": 0.7, "attribute_basis": ["x"]},
        ]),
        _report("B", "sku_b", [
            {"query": "niche q", "normalized_query": "niche q", "open_lane": True,
             "opportunity_score": 90.0, "attribute_fit": 0.9, "attribute_basis": ["x"]},
        ]),
    ]
    out = build_where_you_can_win(reports)
    assert len(out["targets"]) == 1
    assert out["targets"][0]["sku"] == "B"  # higher score wins


def test_caps_targets_and_skip():
    rows = [
        {"query": f"q{i}", "normalized_query": f"q{i}", "open_lane": True,
         "opportunity_score": float(i), "attribute_basis": ["x"]}
        for i in range(10)
    ]
    out = build_where_you_can_win([_report("A", "sku_a", rows)], max_targets=3)
    assert len(out["targets"]) == 3
    assert out["targets"][0]["query"] == "q9"  # highest score first


def test_opportunity_factors_curated_and_forwarded():
    """Step 4: the 'why winnable' decomposition is forwarded (curated: fit/demand/
    low_competition/intent) so the merchant sees WHY a niche scores high."""
    reports = [
        _report("BB Lab", "sku_b", [
            {"query": "collagen for sleep", "normalized_query": "collagen for sleep",
             "open_lane": True, "opportunity_score": 82.0, "attribute_fit": 0.9,
             "demand_state": "open-lane", "attribute_basis": ["collagen", "sleep"],
             "opportunity_factors": {"attribute_fit": 0.9, "demand_signal": 0.7,
                                     "density_inverse": 0.8, "intent_weight": 0.85,
                                     "volume_proxy": 0.5, "actionability": 0.9, "confidence": 0.8}},
        ]),
    ]
    t = build_where_you_can_win(reports)["targets"][0]
    assert t["opportunity_score"] == 82.0
    assert t["opportunity_factors"] == {
        "attribute_fit": 0.9, "demand": 0.7, "low_competition": 0.8, "intent": 0.85,
    }


# --- P0-2 (operator review 2026-07-10): one query, one verdict — the sideways
# wedge owns chase/skip; its lanes must never appear in skip. Live contradiction
# on both pilot runs: Mojawa d1e80bc6 recommended beachhead "ip67 waterproof
# bone conduction headphones open-ear" while the same query sat in skip
# (retailer-owned); ANUKO 549ace84 same with "green tea hair butter".

def _report_with_wedge(sku_name, sku_key, rows, wedge):
    report = _report(sku_name, sku_key, rows)
    report["next_best_action"] = {"evidence": {"sideways_wedge": wedge}}
    return report


def test_wedge_beachhead_never_lands_in_skip():
    rows = [
        # the Mojawa shape: contested sidewalk lane, retailer-owned, real demand
        {"query": "ip67 waterproof bone conduction headphones open-ear",
         "normalized_query": "ip67 waterproof bone conduction headphones open-ear",
         "open_lane": False, "ownership_state": "retailer-owned", "demand_signal": 1.0,
         "opportunity_score": 14.06, "attribute_fit": 1.0, "demand_state": "contested",
         "who_owns": "target.com", "cited_evidence": {"competitors_named": ["Shokz"]}},
        # a genuine skip: head term, publisher-owned
        {"query": "best headphones", "normalized_query": "best headphones",
         "open_lane": False, "ownership_state": "publisher-owned", "demand_signal": 1.0,
         "who_owns": "techradar.com", "cited_evidence": {"competitors_named": ["Sony"]}},
    ]
    wedge = {
        "recommended_beachhead_lane": {
            "query": "ip67 waterproof bone conduction headphones open-ear",
            "opportunity_score": 14.06, "controllers": ["target.com", "mojawa.com"],
            "selection_reason": "stronger merchant-fit evidence",
        },
        "sideways_wedge_lanes": [
            {"query": "ip67 waterproof bone conduction headphones open-ear",
             "opportunity_score": 14.06, "controllers": ["target.com", "mojawa.com"]},
        ],
        "do_not_chase_yet": [{"query": "best headphones"}],
    }
    out = build_where_you_can_win([_report_with_wedge("Purra Run", "sku_run", rows, wedge)])
    skip_queries = [s["query"] for s in out["skip"]]
    target_queries = [t["query"] for t in out["targets"]]
    # the flagship recommendation is a target, not a skip
    assert "ip67 waterproof bone conduction headphones open-ear" in target_queries
    assert "ip67 waterproof bone conduction headphones open-ear" not in skip_queries
    # do_not_chase head terms still skip (the two verdicts agree there)
    assert skip_queries == ["best headphones"]
    wedge_target = out["targets"][0]
    assert wedge_target["source"] == "sideways_wedge"
    assert wedge_target["is_beachhead"] is True
    assert wedge_target["action"] == "create_answer"
    assert wedge_target["controllers"] == ["target.com", "mojawa.com"]


def test_wedge_lane_from_sibling_sku_suppresses_cross_sku_skip():
    # SKU A's wedge chases the lane; SKU B probed the same query and lost it —
    # B's row must not resurrect the skip verdict.
    lane_q = "green tea hair butter"
    row_contested = {
        "query": lane_q, "normalized_query": lane_q, "open_lane": False,
        "ownership_state": "retailer-owned", "demand_signal": 1.0,
        "opportunity_score": 9.0, "attribute_fit": 1.0, "demand_state": "contested",
        "who_owns": "oliveyoung.com", "cited_evidence": {},
    }
    wedge = {"recommended_beachhead_lane": {"query": lane_q, "opportunity_score": 9.0},
             "sideways_wedge_lanes": [], "do_not_chase_yet": []}
    report_a = _report_with_wedge("Hair Butter", "sku_a", [row_contested], wedge)
    report_b = _report("Sibling", "sku_b", [dict(row_contested)])
    out = build_where_you_can_win([report_a, report_b])
    assert [s["query"] for s in out["skip"]] == []
    assert [t["query"] for t in out["targets"]] == [lane_q]
    assert out["targets"][0]["sku"] == "Hair Butter"


def test_open_lane_outranks_wedge_form_of_same_query():
    q = "vegan hair butter"
    wedge = {"recommended_beachhead_lane": {"query": q, "opportunity_score": 50.0},
             "sideways_wedge_lanes": [], "do_not_chase_yet": []}
    report_a = _report_with_wedge("A", "sku_a", [
        {"query": q, "normalized_query": q, "open_lane": False,
         "ownership_state": "retailer-owned", "demand_signal": 1.0,
         "opportunity_score": 50.0, "cited_evidence": {}},
    ], wedge)
    report_b = _report("B", "sku_b", [
        {"query": q, "normalized_query": q, "open_lane": True,
         "opportunity_score": 10.0, "attribute_fit": 0.9,
         "demand_state": "open-lane", "attribute_basis": ["vegan"]},
    ])
    out = build_where_you_can_win([report_a, report_b])
    assert len(out["targets"]) == 1
    t = out["targets"][0]
    # open-lane form wins even with a lower score
    assert t["sku"] == "B"
    assert "source" not in t


def test_reports_without_wedge_keep_legacy_behavior():
    reports = [
        _report("X", "sku_x", [
            {"query": "best vitamin c serum", "normalized_query": "best vitamin c serum",
             "open_lane": False, "ownership_state": "retailer-owned", "demand_signal": 0.9,
             "who_owns": "sephora.com", "cited_evidence": {"competitors_named": ["BrandA"]}},
        ]),
    ]
    out = build_where_you_can_win(reports)
    assert [s["query"] for s in out["skip"]] == ["best vitamin c serum"]
    assert out["targets"] == []
