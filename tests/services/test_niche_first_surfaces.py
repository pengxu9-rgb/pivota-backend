"""Niche-first showcase ordering across report surfaces (sweep 2026-07-16).

Every surface that picks a probed query to showcase draws from a pool in
probe order — which deliberately front-loads the 1-2 head baseline probes —
so a plain row[0]/[:N] showcased the flagship fight ("best headphones") on
every audit. One rule everywhere: specific queries lead, head baselines
trail as honest measurements, and the LLM/merchant prompt_source exemptions
always apply. These tests pin each surface's ordering/selection; the
substitution alert + win-plan own-content gate are pinned in their own files.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# --- shared helper -----------------------------------------------------------

def test_sort_specific_first_stable_partition():
    from services.win_plan_builder import sort_specific_first

    rows = [
        {"query": "best headphones"},
        {"query": "ip68 headphones for lap swimming"},
        {"query": "top serums"},
        {"query": "vegan collagen for sensitive stomachs"},
        # exempt stamps keep head-shaped prompts in the specific bucket
        {"query": "best waterproof headphones", "prompt_source": "llm_winnable"},
        {"query": "best headphones", "prompt_source": "merchant_custom"},
    ]
    out = sort_specific_first(rows)
    assert [r["query"] for r in out] == [
        "ip68 headphones for lap swimming",
        "vegan collagen for sensitive stomachs",
        "best waterproof headphones",
        "best headphones",  # merchant_custom — exempt, keeps relative order
        "best headphones",  # bare head baseline
        "top serums",
    ]


# --- discovery.missed --------------------------------------------------------

def _disc_row(query: str, verdict: str = "loss", prompt_source: str | None = None):
    row: Dict[str, Any] = {
        "query": query,
        "normalized_query": query,
        "axis": "category",
        "provider_verdicts": {"gemini": verdict, "chatgpt": verdict},
        "competitors": [],
    }
    if prompt_source:
        row["prompt_source"] = prompt_source
    return row


def test_discovery_missed_orders_specific_first():
    from services.agent_center_bd_report_service import build_product_competitiveness

    pc = build_product_competitiveness([
        _disc_row("best headphones"),  # head baseline leads probe order
        _disc_row("ip68 waterproof headphones for competitive swimmers"),
    ])
    missed = pc["discovery"]["missed"]
    assert missed[0] == "ip68 waterproof headphones for competitive swimmers"
    assert missed[1] == "best headphones"  # kept, trailing


def test_model_divergence_orders_specific_first_and_stamps_source():
    from services.agent_center_bd_report_service import build_product_competitiveness

    pc = build_product_competitiveness([
        {**_disc_row("best headphones"),
         "provider_verdicts": {"gemini": "win", "chatgpt": "loss"}},
        {**_disc_row("bone conduction headphones for swimming without phone",
                     prompt_source="llm_winnable"),
         "provider_verdicts": {"gemini": "loss", "chatgpt": "win"}},
    ])
    divergence = pc["model_divergence"]
    assert divergence[0]["query"] == (
        "bone conduction headphones for swimming without phone"
    )
    assert divergence[0]["prompt_source"] == "llm_winnable"
    assert divergence[1]["query"] == "best headphones"


# --- top_open_lanes ----------------------------------------------------------

def test_top_open_lanes_excludes_shared_classifier_head_shapes():
    from services.sku_opportunity import _top_open_lanes

    def _lane(query: str, **kw):
        return {
            "query": query,
            "open_lane": True,
            "query_class": kw.pop("query_class", "attribute"),
            "opportunity_score": kw.pop("opportunity_score", 10.0),
            **kw,
        }

    lanes = _top_open_lanes([
        # head by the SHARED classifier but classed attribute/category by the
        # narrower query_class rule — previously lane-eligible.
        _lane("popular headphones", opportunity_score=99.0),
        _lane("what headphones should I buy", query_class="category",
              opportunity_score=98.0),
        _lane("bone conduction headphones for lap swimming"),
        # merchant-authored head shape stays eligible (deliberate test)
        _lane("best headphones", prompt_source="merchant_custom",
              opportunity_score=50.0),
    ])
    queries = [l["query"] for l in lanes]
    assert "popular headphones" not in queries
    assert "what headphones should I buy" not in queries
    assert "best headphones" in queries  # merchant_custom exempt
    assert "bone conduction headphones for lap swimming" in queries


# --- outreach losing_queries per host ----------------------------------------

def test_losing_queries_by_host_specific_leads_the_slice():
    from services.merchant_narrative_builder import _losing_queries_by_host

    win_plan = {
        "available": True,
        "sku_plans": [{
            "losing_queries": [
                {"query": "best headphones", "broad_head_prompt": True,
                 "grounds_in": [{"host": "techradar.com"}]},
                {"query": "ip68 headphones for competitive swimmers",
                 "broad_head_prompt": False,
                 "grounds_in": [{"host": "techradar.com"}]},
            ],
        }],
    }
    by_host = _losing_queries_by_host(win_plan)
    assert by_host["techradar.com"][0] == (
        "ip68 headphones for competitive swimmers"
    )
    assert by_host["techradar.com"][1] == "best headphones"


# --- brand NBA lane pick -----------------------------------------------------

def test_first_specific_query_classifies_through_the_quotes():
    """_query_examples wraps queries in literal double quotes — the classifier
    must strip them or every quoted head term classes as specific and the
    lane pick degenerates to queries[0] (review P1: silent no-op)."""
    from services.next_best_action import _first_specific_query

    assert _first_specific_query(
        ['"best headphones"', '"ip68 headphones for lap swimming"']
    ) == '"ip68 headphones for lap swimming"'
    # head-only falls back to the first example
    assert _first_specific_query(['"best headphones"']) == '"best headphones"'
    assert _first_specific_query([]) == ""


# --- failing-prompt interleave -----------------------------------------------

def test_interleave_failing_prompts_niche_first_then_provider_fair():
    from services.next_best_action import _interleave_failing_prompts

    rows = [
        {"query": "best headphones", "provider": "gemini"},
        {"query": "ip68 headphones for lap swimming", "provider": "gemini"},
        {"query": "top serums", "provider": "chatgpt"},
        {"query": "vegan collagen for sensitive stomachs", "provider": "chatgpt"},
    ]
    out = _interleave_failing_prompts(rows)
    queries = [r["query"] for r in out]
    # every row survives exactly once; specific rows lead
    assert sorted(queries) == sorted(r["query"] for r in rows)
    assert set(queries[:2]) == {
        "ip68 headphones for lap swimming",
        "vegan collagen for sensitive stomachs",
    }
    assert set(queries[2:]) == {"best headphones", "top serums"}


# --- playbook example query --------------------------------------------------

def test_playbook_example_query_prefers_specific_match():
    from services.audit_playbook_engine import _example_query_for_host

    detailed: List[Dict[str, Any]] = [
        {"query": "best headphones", "top_cited_host": "techradar.com"},
        {"query": "ip68 headphones for competitive swimmers",
         "top_cited_host": "techradar.com"},
        {"query": "vegan collagen sticks", "top_cited_host": "byrdie.com"},
    ]
    picked = _example_query_for_host("techradar.com", detailed)
    assert picked["query"] == "ip68 headphones for competitive swimmers"
    # head-only match still returns the honest head example
    head_only = _example_query_for_host(
        "techradar.com",
        [{"query": "best headphones", "top_cited_host": "techradar.com"}],
    )
    assert head_only["query"] == "best headphones"
    assert _example_query_for_host("nomatch.com", detailed) is None


# --- routed-lane copy hygiene (review P1/P2) ----------------------------------

def test_head_reframe_route_host_never_merchant_and_never_list_repr():
    from services.next_best_action import build_sku_next_best_action

    opportunity: Dict[str, Any] = {
        "per_prompt": [],
        "substitution_alert": {
            "present": True,
            "prompt": "best headphones",
            "substituted_by": "Bose",
            "engines": ["gemini"],
            "kind": "category",
            "broad_head_prompt": True,
        },
        "sideways_wedge": {
            "recommended_beachhead_lane": {
                "query": "ip68 waterproof certified bone conduction headphones",
                "ownership_state": "marketplace-owned",
                # ties persist who_owns as a LIST; first controller is the
                # merchant's own host — neither may leak into copy verbatim.
                "who_owns": ["mojawa.com", "ebay.com"],
                "controllers": ["mojawa.com", "ebay.com"],
                "attribute_basis": ["ip68 waterproof", "bone conduction"],
            },
        },
        "intent_ladder": {},
        "top_open_lanes": [],
        "demand_state_summary": "contested",
    }
    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores={
            "identity": {"score": 70},
            "content_richness": {"score": 55},
            "routability": {"score": 60},
            "citation": {"score": 50},
        },
        identity={"name": "Purra Swim", "confidence": "high"},
        sku_title="Purra Swim Headphones",
        merchant_host="mojawa.com",
    )
    text = nba["first_move"] + " " + " ".join(nba["self_serve_actions"])
    assert "[" not in text and "]" not in text  # no Python list repr
    # who_owns list resolves past the merchant's own host to the real router
    assert "routed through ebay.com" in nba["first_move"]
    assert "routed through mojawa.com" not in text


def test_head_reframe_publisher_owned_gets_pitch_not_why_buy_direct():
    from services.next_best_action import build_sku_next_best_action

    opportunity: Dict[str, Any] = {
        "per_prompt": [],
        "substitution_alert": {
            "present": True,
            "prompt": "best headphones",
            "substituted_by": "Bose",
            "engines": ["gemini"],
            "kind": "category",
            "broad_head_prompt": True,
        },
        "sideways_wedge": {
            "recommended_beachhead_lane": {
                "query": "bone conduction headphones for lap swimming",
                "ownership_state": "publisher-owned",
                "who_owns": "healthline.com",
                "attribute_basis": ["bone conduction", "lap swimming"],
            },
        },
        "intent_ladder": {},
        "top_open_lanes": [],
        "demand_state_summary": "contested",
    }
    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores={
            "identity": {"score": 70},
            "content_richness": {"score": 55},
            "routability": {"score": 60},
            "citation": {"score": 50},
        },
        identity={"name": "Purra Swim", "confidence": "high"},
        sku_title="Purra Swim Headphones",
    )
    text = nba["first_move"] + " " + " ".join(nba["self_serve_actions"])
    # publishers don't sell — never "why buy direct vs healthline.com"
    assert "why-buy-direct" not in text
    assert "get" in nba["first_move"].lower()
    assert "healthline.com" in nba["first_move"]
