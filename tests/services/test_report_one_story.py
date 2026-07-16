"""One-story coherence round (holistic review 2026-07-16).

Read end-to-end, the audit told four different stories about one measurement:
the verdict printed a dual-metric pair that was really one citation median,
the losing section led with an endorsement, the band label said "Recommended"
beside a recommended-0/8 discovery split, and copy nits ("0 days ago",
"accuracy is the watch item" on a 4/4-clean verify, quadruplicated source
labels, a one-action plan) made the report read unfinished. These pin the
coherent versions.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# --- verdict lens labeling -----------------------------------------------------

def test_per_sku_verdict_names_the_citation_metric_not_the_legacy_pair():
    from services.agent_center_bd_report_service import _per_sku_brand_verdict

    _, _, explanation = _per_sku_brand_verdict(
        48, 1, 0,
        evidence={"attribution_runs_total": 15, "merchant_cited_runs": 8},
    )
    assert "citation score 48/100" in explanation
    # The legacy dual-metric vocabulary collided with the summary score
    # block's differently-defined visibility subscore.
    assert "visibility 48/100" not in explanation
    assert "attribution 48/100" not in explanation


def test_legacy_verdict_keeps_dual_metric_wording():
    from services.agent_center_bd_report_service import verdict_for

    _, explanation = verdict_for(
        48, 30,
        evidence={"attribution_runs_total": 15, "merchant_cited_runs": 8},
    )
    assert "visibility 48/100" in explanation


# --- band label honesty ----------------------------------------------------------

def test_band_display_listing_only_says_found_not_recommended():
    from services.agent_center_bd_report_service import _band_display

    scores = {"citation": {"score": 48}}
    softened = _band_display("blocked", scores, listing_only=True)
    assert softened["label"] == "Found by AI, but not agent-ready"
    assert "recommends" not in softened["meaning"].lower().split("independent")[0]

    recommended = _band_display("blocked", scores, listing_only=False)
    assert recommended["label"] == "Recommended, but not agent-ready"


# --- losing section leads with a loss -------------------------------------------

def _endorsed_summary() -> Dict[str, Any]:
    return {
        "independently_recommended_for_category": True,
        "endorsement_hosts": ["wired.com"],
        "endorsement_category_hosts": ["wired.com"],
        "findability_hosts": ["mojawa.com"],
    }


def _win_plan_with_loss() -> Dict[str, Any]:
    return {
        "available": True,
        "sku_plans": [{
            "losing_queries": [
                {"query": "ip68 waterproof headphones for competitive swimmers",
                 "broad_head_prompt": False, "grounds_in": []},
                {"query": "best headphones", "broad_head_prompt": True,
                 "grounds_in": []},
            ],
        }],
    }


def test_losing_summary_leads_with_the_loss_when_endorsed():
    from services.merchant_narrative_builder import _where_youre_losing

    out = _where_youre_losing(
        "Mojawa",
        {"hosts": []},
        _endorsed_summary(),
        win_plan=_win_plan_with_loss(),
    )
    text = str(out.get("summary") or "")
    assert "ip68 waterproof headphones for competitive swimmers" in text
    assert "already recommends" in text
    # never leads with the endorsement-only sentence
    assert not text.startswith("Mojawa earns independent")


def test_losing_summary_honest_when_no_loss_measured():
    from services.merchant_narrative_builder import _where_youre_losing

    out = _where_youre_losing(
        "Mojawa",
        {"hosts": []},
        _endorsed_summary(),
        win_plan={"available": True, "sku_plans": []},
    )
    assert "no measured category loss this run" in str(out.get("summary") or "")


# --- finding title matches its content -------------------------------------------

def test_top_finding_title_flips_on_endorsement():
    from services.report_summary_builder import _top_findings

    endorsed = _top_findings({
        "where_youre_losing": {
            "summary": "wired.com already recommends Mojawa — open losses …",
            "independently_recommended_for_category": True,
        },
    })
    assert endorsed[0]["title"].startswith("Independent endorsement")
    assert endorsed[0]["severity"] == "info"

    losing = _top_findings({
        "where_youre_losing": {
            "summary": "AI recommends Shokz instead of Mojawa.",
            "independently_recommended_for_category": False,
        },
    })
    assert losing[0]["title"] == "Who AI recommends instead"
    assert losing[0]["severity"] == "high"


# --- copy hygiene -----------------------------------------------------------------

def test_day_phrase_same_day_reads_earlier_today():
    from services.audit_delta import _day_phrase

    assert _day_phrase(0) == " earlier today"
    assert _day_phrase(1) == " 1 day ago"
    assert _day_phrase(7) == " 7 days ago"
    assert _day_phrase(None) == ""


def test_verify_summary_clean_sample_does_not_hedge():
    from services.merchant_narrative_builder import _verify_plain

    clean = _verify_plain({
        "status": "completed", "citation_positive_candidates": 4,
        "verified": 4, "flagged": 0,
    })
    text = str(clean.get("text") or "")
    assert "accuracy checked out" in text
    assert "watch item" not in text

    flagged = _verify_plain({
        "status": "completed", "citation_positive_candidates": 4,
        "verified": 4, "flagged": 1,
    })
    assert "watch item" in str(flagged.get("text") or "")


def test_whats_working_excerpt_source_labels_deduped():
    from services.merchant_narrative_builder import _first_branded_excerpt

    reports = [{
        "sku_title": "Purra Swim",
        "verbatim_grounding_evidence": [{
            "query": "where can I buy Purra Swim",
            "evidence_excerpt": "Available directly from Mojawa's site.",
            "product_visible": True,
            "axis_metadata": {"axis": "intent"},
            "grounding_sources": [
                {"title": "mojawa.com"}, {"title": "mojawa.com"},
                {"title": "mojawa.com"}, {"title": "rtings.com"},
            ],
        }],
    }]
    out = _first_branded_excerpt(reports)
    assert out["source_labels"] == ["mojawa.com", "rtings.com"]


# --- the plan has more than one action --------------------------------------------

def test_prioritized_actions_surface_secondary_moves():
    from services.merchant_narrative_builder import _prioritized_actions

    reports: List[Dict[str, Any]] = [{
        "sku_title": "Purra Swim",
        "next_best_action": {
            "headline": "Sony owns the broad question — win your lane first.",
            "primary_gap": "citation",
            "first_move": "Get cited on wired.com.",
            "why_this_first": "…",
            "secondary_moves": [
                {"title": "Add the missing product facts",
                 "lever": "content_gap",
                 "concrete_next_step": "Add specs and proof to the PDP.",
                 "reason": "Named in the gap evidence."},
                {"title": "Re-test failed SKU prompt: ip68 headphones",
                 "lever": "sku_prompt_retest",
                 "concrete_next_step": "Revise the PDP, re-run the prompt.",
                 "reason": "Named in the failing prompt evidence."},
            ],
        },
    }]
    actions = _prioritized_actions(reports)
    assert len(actions) == 3
    # primary stays first; secondaries follow
    assert actions[0]["headline"].startswith("Sony owns")
    headlines = [a["headline"] for a in actions]
    assert "Add the missing product facts" in headlines
    assert any(h.startswith("Re-test failed SKU prompt") for h in headlines)


def test_losing_summary_head_only_losses_dont_claim_no_loss():
    """Every measured loss being a head baseline must not read as 'no measured
    category loss' — the win-plan summary on the same panel counts them."""
    from services.merchant_narrative_builder import _where_youre_losing

    head_only_plan = {
        "available": True,
        "sku_plans": [{
            "losing_queries": [
                {"query": "best headphones", "broad_head_prompt": True,
                 "grounds_in": []},
            ],
        }],
    }
    out = _where_youre_losing(
        "Mojawa", {"hosts": []}, _endorsed_summary(), win_plan=head_only_plan,
    )
    text = str(out.get("summary") or "")
    assert "broad head terms" in text
    assert "no measured category loss" not in text


def test_losing_summary_pluralizes_multiple_endorsers():
    from services.merchant_narrative_builder import _where_youre_losing

    summary = _endorsed_summary()
    summary["endorsement_category_hosts"] = ["wired.com", "rtings.com"]
    summary["endorsement_hosts"] = ["wired.com", "rtings.com"]
    out = _where_youre_losing(
        "Mojawa", {"hosts": []}, summary, win_plan=_win_plan_with_loss(),
    )
    text = str(out.get("summary") or "")
    assert "wired.com, rtings.com already recommend Mojawa" in text


def test_top_actions_secondary_rows_never_inherit_primary_evidence():
    """Review P1: the sku_title fallback join attached the PRIMARY NBA's
    evidence, tracking, and CTA to secondary-move rows — duplicated,
    wrongly-attributed evidence whose CTA performed the primary's action."""
    from services.report_summary_builder import _top_actions

    narrative = {
        "prioritized_actions": [
            {"sku_title": "Purra Swim", "primary_gap": "citation",
             "headline": "Primary headline", "first_move": "Primary move",
             "why_this_first": "…", "growth_phase": "create_and_distribute"},
            {"sku_title": "Purra Swim", "primary_gap": None,
             "headline": "Add the missing product facts",
             "first_move": "Add specs to the PDP.",
             "why_this_first": "Named in the gap evidence.",
             "growth_phase": "evidence_intake",
             "action_source": "secondary"},
        ],
    }
    per_sku = [{
        "sku_key": "sku-1",
        "sku_title": "Purra Swim",
        # SKU-level pool: _supporting_prompts unions this into EVERY action
        # from the SKU — the founder-reported "all evidences repeat".
        "failing_prompts": [
            {"query": "ip68 headphones for lap swimming",
             "provider": "gemini", "competitors_named": []},
        ],
        "next_best_action": {
            "headline": "Primary headline",
            "primary_gap": "citation",
            "evidence_summary": "PRIMARY EVIDENCE",
            "how_to_track": ["track primary"],
            "cta": {"label": "Do the primary", "target_sku_key": "sku-1"},
        },
    }]
    rows = _top_actions(narrative, per_sku)
    primary = next(r for r in rows if r["headline"] == "Primary headline")
    secondary = next(
        r for r in rows if r["headline"] == "Add the missing product facts"
    )
    assert primary["evidence_summary"] == "PRIMARY EVIDENCE"
    assert primary["cta"]
    # measured evidence renders ONCE, under the action it was diagnosed from
    assert primary["supporting_prompts"], "primary keeps its measured evidence"
    assert secondary["supporting_prompts"] == []
    assert secondary["evidence_summary"] is None
    assert secondary["cta"] is None
    assert secondary["how_to_track"] == []
    assert secondary["first_move"] == "Add specs to the PDP."


# --- attribute phrase cap ----------------------------------------------------------

def test_head_reframe_lane_phrase_skips_clause_length_attributes():
    from services.next_best_action import build_sku_next_best_action

    opportunity: Dict[str, Any] = {
        "per_prompt": [],
        "substitution_alert": {
            "present": True, "prompt": "best headphones",
            "substituted_by": "Bose", "engines": ["gemini"],
            "kind": "category", "broad_head_prompt": True,
        },
        "sideways_wedge": {
            "recommended_beachhead_lane": {
                "query": "bone conduction headphones open-ear no ear pressure",
                "ownership_state": "publisher-owned",
                "who_owns": "wired.com",
                "attribute_basis": [
                    "bone conduction headphones",
                    "open ear",
                    "no ear pressure no water trapped in ears daily sports",
                ],
            },
        },
        "intent_ladder": {}, "top_open_lanes": [],
        "demand_state_summary": "contested",
    }
    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores={"identity": {"score": 70}, "content_richness": {"score": 55},
                "routability": {"score": 60}, "citation": {"score": 50}},
        identity={"name": "Purra Swim", "confidence": "high"},
        sku_title="Purra Swim Headphones",
    )
    assert "no ear pressure no water trapped in ears" not in nba["first_move"]
    assert "the bone conduction headphones, open ear ask" in nba["first_move"]
