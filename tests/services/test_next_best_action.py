from __future__ import annotations

import json
from typing import Any, Dict, List

from services.next_best_action import (
    PRIMARY_CATEGORY_DISCOVERY,
    PRIMARY_COMPETITOR_SOURCE,
    PRIMARY_FIRST_PARTY_DEFENSE,
    PRIMARY_INTEGRATION_COMPLETION,
    PRIMARY_RETAILER_ROUTE_LEAK,
    PRIMARY_RETRIEVAL_FOUNDATION,
    PRIMARY_SKU_CONTENT_REVISION_GAP,
    PRIMARY_SKU_INSUFFICIENT_DATA,
    PRIMARY_SKU_OPEN_LANE_CAPTURE,
    PRIMARY_SKU_PROTECTED_MONITORING,
    PRIMARY_SKU_SOURCE_ROUTE_REPAIR,
    PRIMARY_SKU_SUBSTITUTION_LEAK,
    build_next_best_action,
    build_sku_next_best_action,
)
from services.sku_lane_priority import build_lane_product_evidence


def _merchant_view(
    *,
    verdict: str,
    visibility: int,
    attribution: int,
    category_visibility: int | None,
    cited_hosts: List[Dict[str, Any]] | None = None,
    competitors: List[str] | None = None,
    failed_queries: List[Dict[str, Any]] | None = None,
    actions: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "headline": {
            "verdict_label": verdict,
            "scores": {
                "visibility": visibility,
                "attribution": attribution,
                "category_visibility": category_visibility,
            },
        },
        "receipts": {
            "failed_queries_detailed": list(failed_queries or []),
            "cited_hosts_detailed": list(cited_hosts or []),
            "top_competitor_brands": list(competitors or []),
            "competitive_table": [],
        },
        "diagnosis": {"primary": "diagnostic framing"},
        "actions": list(actions or []),
        "pivota_value_prop": {},
    }


def _failed_query(
    query: str,
    *,
    host: str | None = None,
    host_type: str = "editorial",
    competitors: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "query": query,
        "top_cited_host": host,
        "host_classification": {"type": host_type},
        "competitors_named": list(competitors or []),
    }


def _state(*, store: bool, psp: bool) -> Dict[str, Any]:
    missing = []
    if not store:
        missing.append("store_platform")
    if not psp:
        missing.append("psp")
    return {
        "store_platform_integrated": store,
        "psp_integrated": psp,
        "fully_integrated": store and psp,
        "missing_pieces": missing,
    }


def _assert_70_30(nba: Dict[str, Any]) -> None:
    assert len(nba["self_serve_actions"]) == 2
    assert all(isinstance(a, str) and a.strip() for a in nba["self_serve_actions"])
    assert isinstance(nba["pivota_path"], str) and nba["pivota_path"].strip()
    assert nba["self_serve"] == nba["self_serve_actions"]
    assert nba["pivota_assisted"] == [nba["pivota_path"]]
    assert nba["evidence"] == nba["evidence_used"]
    assert isinstance(nba["evidence_summary"], str) and nba["evidence_summary"].strip()
    assert isinstance(nba["evidence_chips"], list)
    assert len(nba["tracking_metrics"]) >= 2
    assert nba["how_to_track"] == nba["tracking_metrics"]
    # Per-SKU CTAs carry an executable action descriptor; brand-report CTAs do
    # not. target_sku_key is stamped only when sku_key is passed (not by these
    # fixtures). So validate the shape loosely and the action value when present.
    from services.next_best_action import SKU_CTA_ACTIONS
    assert {"label", "trust_note"} <= set(nba["cta"])
    assert set(nba["cta"]) <= {"label", "trust_note", "action", "target_sku_key"}
    if "action" in nba["cta"]:
        assert nba["cta"]["action"] in SKU_CTA_ACTIONS


def _sku_identity(unresolved: bool = False) -> Dict[str, Any]:
    return {
        "name": "BB Lab Good Night Collagen",
        "confidence": "low" if unresolved else "medium",
        "unresolved": unresolved,
    }


def _sku_scores(score: int = 90) -> Dict[str, Any]:
    return {
        "identity": {"score": score},
        "content_richness": {"score": score},
        "routability": {"score": score},
        "citation": {"score": score},
    }


def _sku_base_opportunity() -> Dict[str, Any]:
    return {
        "per_prompt": [],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": "tested",
        "intent_ladder": {},
        "confidence": {"prompt_count": 4, "prompts_with_demand": 2},
    }


def test_invisible_retrieval_prescription_is_diy_first_not_integration():
    mv = _merchant_view(
        verdict="INVISIBLE",
        visibility=0,
        attribution=0,
        category_visibility=0,
        cited_hosts=[
            {"host": "nordstrom.com", "type": "retailer", "times_cited": 2},
        ],
        failed_queries=[
            _failed_query("where can I buy TestBrand pajamas", host="nordstrom.com", host_type="retailer"),
        ],
    )

    nba = build_next_best_action(
        merchant_view=mv,
        integration_state=_state(store=False, psp=False),
        is_cold_start=True,
    )

    assert nba["primary_gap"] == PRIMARY_RETRIEVAL_FOUNDATION
    # DIY-first: the first move is the merchant's own indexing work, not "use Pivota".
    assert "google" in nba["first_move"].lower()
    assert "pivota" not in nba["first_move"].lower()
    assert "Google Search Console" in nba["self_serve_actions"][0]
    assert "re-check" in nba["pivota_path"].lower()
    _assert_70_30(nba)


def test_weak_first_party_retrieval_does_not_depend_on_invisible_verdict_label():
    mv = _merchant_view(
        verdict="PARTIAL",
        visibility=22,
        attribution=0,
        category_visibility=45,
        cited_hosts=[
            {"host": "wellness.example", "type": "editorial", "times_cited": 1},
        ],
        failed_queries=[
            _failed_query(
                "where can I buy TestBrand collagen",
                host="wellness.example",
                host_type="editorial",
            ),
        ],
    )

    nba = build_next_best_action(merchant_view=mv, is_cold_start=True)

    assert nba["primary_gap"] == PRIMARY_RETRIEVAL_FOUNDATION
    assert "Google Search Console" in nba["self_serve_actions"][0]
    assert "Pivota onboarding" not in nba["first_move"]
    _assert_70_30(nba)


def test_category_visible_via_retailers_is_route_leak_not_retrieval_foundation():
    """Broadening retrieval-foundation beyond the INVISIBLE label must not steal
    merchants who are strong in category but weak on branded queries: AI already
    finds them (category) and routes buyers to retailers, so the fix is winning
    the click back, not 'get indexed'."""
    mv = _merchant_view(
        verdict="VISIBLE VIA RETAILERS",
        visibility=20,
        attribution=10,
        category_visibility=80,
        cited_hosts=[
            {"host": "sephora.com", "type": "retailer", "times_cited": 4},
            {"host": "amazon.com", "type": "marketplace", "times_cited": 3},
        ],
        failed_queries=[
            _failed_query("best serum for dry skin", host="sephora.com", host_type="retailer"),
        ],
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_RETAILER_ROUTE_LEAK
    assert "Category visibility is 80" in nba["evidence_summary"]
    assert "70-point route gap" in nba["evidence_summary"]
    assert "Retailer routes: sephora.com and amazon.com" in nba["evidence_chips"]
    assert 'First-party citation rate on "best serum for dry skin".' in nba["tracking_metrics"][0]
    assert "sephora.com and amazon.com" in nba["tracking_metrics"][1]


def test_low_attribution_boundary_without_route_evidence_is_not_defense():
    mv = _merchant_view(
        verdict="PARTIAL",
        visibility=29,
        attribution=29,
        category_visibility=50,
        cited_hosts=[],
        failed_queries=[],
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_RETRIEVAL_FOUNDATION
    assert nba["primary_gap"] != PRIMARY_FIRST_PARTY_DEFENSE
    assert "Google Search Console" in nba["self_serve_actions"][0]
    assert "official path is not yet reliably retrievable" in nba["evidence_summary"]
    _assert_70_30(nba)


def test_retailer_route_leak_prescribes_direct_attribution_not_generic_pr():
    mv = _merchant_view(
        verdict="VISIBLE VIA RETAILERS",
        visibility=82,
        attribution=20,
        category_visibility=78,
        cited_hosts=[
            {"host": "sephora.com", "type": "retailer", "times_cited": 4},
            {"host": "amazon.com", "type": "marketplace", "times_cited": 3},
        ],
        failed_queries=[
            _failed_query("best serum for dry skin", host="sephora.com", host_type="retailer"),
        ],
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_RETAILER_ROUTE_LEAK
    assert "retailers" in nba["headline"].lower()
    assert "margin and customer data" in nba["why_this_first"]
    assert "pr problem" in nba["why_this_first"].lower()  # not generic PR
    assert "sephora.com" in nba["self_serve_actions"][1]
    assert "Visibility is 82" in nba["evidence_summary"]
    assert "62-point route gap" in nba["evidence_summary"]
    assert 'First-party citation rate on "best serum for dry skin".' in nba["tracking_metrics"][0]
    assert "sephora.com and amazon.com" in nba["tracking_metrics"][1]
    play = nba["canonical_page_play"]
    play_blob = json.dumps(play).lower()
    assert play["lane"] == "best serum for dry skin"
    assert play["controllers"] == ["sephora.com", "amazon.com"]
    assert {move["type"] for move in play["moves"]} == {
        "retail_listing_accuracy",
        "light_retrieval_schema_layer",
        "first_order_offer",
        "starter_replenishment_bundle",
        "subscription_or_why_buy_direct",
    }
    assert "product/offer schema" in play_blob
    assert "exact discount depths" in play["economics_policy"]
    assert "agent-checkout ready" in play["checkout_readiness"]
    assert "%" not in play_blob and "$" not in play_blob
    assert "proves whether direct sales rise" not in json.dumps(nba).lower()
    _assert_70_30(nba)


def test_category_discovery_gap_prescribes_content_and_publisher_inclusion():
    mv = _merchant_view(
        verdict="PARTIAL",
        visibility=88,
        attribution=70,
        category_visibility=52,
        cited_hosts=[
            {"host": "nymag.com", "type": "editorial", "times_cited": 2},
        ],
        competitors=["Beauty of Joseon", "Laneige", "Purito"],
        failed_queries=[
            _failed_query(
                "best collagen for skin elasticity",
                host="nymag.com",
                competitors=["Beauty of Joseon", "Laneige"],
            ),
        ],
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_CATEGORY_DISCOVERY
    assert "comparison content" in nba["first_move"].lower()
    assert "sources" in nba["first_move"].lower()
    assert "category questions" in nba["self_serve_actions"][0].lower()
    assert "nymag.com" in nba["self_serve_actions"][1]
    assert "Named visibility is 88 vs category visibility 52" in nba["evidence_summary"]
    assert "36-point category gap" in nba["evidence_summary"]
    assert "Competitors named: Beauty of Joseon, Laneige, and Purito" in nba["evidence_chips"]
    assert '"best collagen for skin elasticity"' in nba["tracking_metrics"][0]
    assert "Beauty of Joseon, Laneige, and Purito" in nba["tracking_metrics"][1]
    assert "nymag.com" in nba["tracking_metrics"][2]
    _assert_70_30(nba)


def test_category_discovery_gap_without_failed_queries_reads_grammatically():
    # BB Lab shape: branded-strong (vis 67 / attr 100) with a category gap
    # (category 33) and named competitors, but NO failed-query examples. The
    # why_this_first must NOT render the templating bug
    # "no failed-query examples went to <competitors>".
    mv = _merchant_view(
        verdict="STRONG",
        visibility=67,
        attribution=100,
        category_visibility=33,
        cited_hosts=[{"host": "iherb.com", "type": "retailer", "times_cited": 2}],
        competitors=["Ancient + Brave", "Dose & Co", "Vital Proteins"],
        failed_queries=[],
    )
    nba = build_next_best_action(merchant_view=mv)
    assert nba["primary_gap"] == PRIMARY_CATEGORY_DISCOVERY
    why = nba["why_this_first"]
    assert "no failed-query examples" not in why
    assert "instead of you" in why
    assert "Ancient + Brave" in why
    _assert_70_30(nba)


def test_category_discovery_gap_without_competitors_or_failed_queries_is_grammatical():
    # category_discovery_gap has no competitor requirement, so it can fire with
    # neither named competitors nor failed-query examples. Neither why_this_first
    # nor self_serve_actions may leak a fallback noun-phrase into a verb slot
    # ("...belong next to no repeated named competitors").
    mv = _merchant_view(
        verdict="STRONG",
        visibility=67,
        attribution=100,
        category_visibility=33,
        cited_hosts=[{"host": "nymag.com", "type": "editorial", "times_cited": 1}],
        competitors=[],
        failed_queries=[],
    )
    nba = build_next_best_action(merchant_view=mv)
    assert nba["primary_gap"] == PRIMARY_CATEGORY_DISCOVERY
    blob = nba["why_this_first"] + " " + " ".join(nba["self_serve_actions"])
    assert "no repeated named competitors" not in blob
    assert "no failed-query examples" not in blob
    assert "belong in the category answer" in " ".join(nba["self_serve_actions"])
    _assert_70_30(nba)


def test_competitor_source_reuses_outreach_hints_and_playbook_secondary():
    playbook_action = {
        "severity": "high",
        "title": "Pitch nymag.com for category inclusion",
        "body": "Pitch the cited source.",
        "lever": "editorial_outreach",
        "target_host": "nymag.com",
        "concrete_next_step": "Send a comparison pitch to the Strategist editor.",
        "pitch_draft": {"subject": "Comparison proof for TestBrand"},
        "evidence": {
            "host": "nymag.com",
            "times_cited": 2,
            "example_failed_query": "best collagen sticks",
            "competitors_named": ["Beauty of Joseon", "Laneige"],
        },
    }
    mv = _merchant_view(
        verdict="PARTIAL",
        visibility=70,
        attribution=62,
        category_visibility=64,
        cited_hosts=[
            {
                "host": "nymag.com",
                "type": "editorial",
                "times_cited": 2,
                "coverage_note": "Review and shopping roundups",
                "outreach_hint": "Pitch the Strategist editor with samples.",
            },
        ],
        competitors=["Beauty of Joseon", "Laneige", "Purito"],
        failed_queries=[
            _failed_query(
                "best collagen sticks",
                host="nymag.com",
                competitors=["Beauty of Joseon", "Laneige"],
            ),
            _failed_query(
                "K-beauty collagen alternatives",
                host="nymag.com",
                competitors=["Purito", "Laneige"],
            ),
        ],
        actions=[playbook_action],
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_COMPETITOR_SOURCE
    assert nba["evidence_used"]["source_hosts"][0]["outreach_hint"] == (
        "Pitch the Strategist editor with samples."
    )
    assert nba["secondary_moves"][0]["target_host"] == "nymag.com"
    assert nba["secondary_moves"][0]["concrete_next_step"] == (
        "Send a comparison pitch to the Strategist editor."
    )
    assert nba["secondary_moves"][0]["pitch_draft"] == playbook_action["pitch_draft"]
    _assert_70_30(nba)


def test_strong_verdict_defense_has_no_fake_urgency():
    mv = _merchant_view(
        verdict="STRONG",
        visibility=94,
        attribution=90,
        category_visibility=88,
    )

    nba = build_next_best_action(merchant_view=mv)

    assert nba["primary_gap"] == PRIMARY_FIRST_PARTY_DEFENSE
    assert "defend" in nba["headline"].lower()
    assert "don't manufacture" in nba["why_this_first"].lower()
    assert "critical" not in nba["headline"].lower()
    _assert_70_30(nba)


def test_cold_audit_does_not_lead_with_integration_in_merchant_view():
    from services.agent_center_bd_report_service import VERDICT_INVISIBLE, _build_merchant_view

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=0,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[{"severity": "high", "title": "Strategic action", "body": "x"}],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=_state(store=False, psp=False),
    )

    nba = mv["next_best_action"]
    assert nba["primary_gap"] == PRIMARY_RETRIEVAL_FOUNDATION
    assert "Pivota onboarding" not in nba["first_move"]
    assert "Pivota" in nba["pivota_path"]
    assert all(a.get("lever") != "pivota_integration" for a in mv["actions"])


def test_non_cold_incomplete_integration_can_lead_when_gate_is_real():
    from services.agent_center_bd_report_service import VERDICT_PARTIAL, _build_merchant_view

    mv = _build_merchant_view(
        verdict_label=VERDICT_PARTIAL,
        verdict_explanation="Diagnostic.",
        visibility_score=60,
        attribution_score=40,
        category_visibility_score=60,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[{"severity": "high", "title": "Strategic action", "body": "x"}],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=_state(store=True, psp=False),
    )

    assert mv["actions"][0]["lever"] == "pivota_integration"
    assert mv["next_best_action"]["primary_gap"] == PRIMARY_INTEGRATION_COMPLETION
    assert mv["next_best_action"]["cta"]["label"] == "Finish Pivota setup"
    _assert_70_30(mv["next_best_action"])


def test_every_gap_keeps_two_diy_actions_and_one_pivota_path():
    cases = [
        (
            PRIMARY_RETRIEVAL_FOUNDATION,
            _merchant_view(
                verdict="INVISIBLE",
                visibility=0,
                attribution=0,
                category_visibility=0,
            ),
            {},
        ),
        (
            PRIMARY_RETAILER_ROUTE_LEAK,
            _merchant_view(
                verdict="VISIBLE VIA RETAILERS",
                visibility=80,
                attribution=20,
                category_visibility=75,
                cited_hosts=[{"host": "amazon.com", "type": "marketplace", "times_cited": 3}],
            ),
            {},
        ),
        (
            PRIMARY_CATEGORY_DISCOVERY,
            _merchant_view(
                verdict="PARTIAL",
                visibility=90,
                attribution=75,
                category_visibility=55,
            ),
            {},
        ),
        (
            PRIMARY_COMPETITOR_SOURCE,
            _merchant_view(
                verdict="PARTIAL",
                visibility=70,
                attribution=60,
                category_visibility=65,
                cited_hosts=[{"host": "youtube.com", "type": "video", "times_cited": 2}],
                competitors=["A", "B", "C"],
            ),
            {},
        ),
        (
            PRIMARY_FIRST_PARTY_DEFENSE,
            _merchant_view(
                verdict="STRONG",
                visibility=90,
                attribution=88,
                category_visibility=86,
            ),
            {},
        ),
        (
            PRIMARY_INTEGRATION_COMPLETION,
            _merchant_view(
                verdict="PARTIAL",
                visibility=50,
                attribution=40,
                category_visibility=50,
            ),
            {"integration_state": _state(store=True, psp=False)},
        ),
    ]

    for expected_gap, mv, kwargs in cases:
        nba = build_next_best_action(merchant_view=mv, **kwargs)
        assert nba["primary_gap"] == expected_gap
        _assert_70_30(nba)


def test_build_merchant_view_adds_nba_without_removing_existing_blocks():
    from services.agent_center_bd_report_service import VERDICT_PARTIAL, _build_merchant_view

    action = {"severity": "high", "title": "Strategic action", "body": "x"}
    mv = _build_merchant_view(
        verdict_label=VERDICT_PARTIAL,
        verdict_explanation="Diagnostic.",
        visibility_score=55,
        attribution_score=45,
        category_visibility_score=50,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[dict(action)],
        competitive_pressure={"framing": "peer framing"},
        what_pivota_changes={"summary": "value prop"},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )

    old_keys = {"headline", "receipts", "diagnosis", "actions", "tracking", "pivota_value_prop"}
    assert old_keys.issubset(mv)
    assert "next_best_action" in mv
    assert mv["actions"][0]["title"] == action["title"]
    assert mv["receipts"]["queries_tested"] == 0
    assert mv["pivota_value_prop"] == {"summary": "value prop"}


def test_integration_completion_gap_only_for_non_cold_incomplete():
    """A non-cold, already-onboarding merchant with a missing required piece
    leads with integration completion — but a cold audit with the SAME missing
    pieces must NOT (the both-missing state is the cold-start sentinel)."""
    base = _merchant_view(
        verdict="INVISIBLE", visibility=10, attribution=5, category_visibility=8,
    )
    # Non-cold, store connected but PSP missing -> integration completion leads.
    nba = build_next_best_action(
        merchant_view=base,
        competitive_pressure={},
        integration_state={"missing_pieces": ["psp"]},
        is_cold_start=False,
    )
    assert nba["primary_gap"] == PRIMARY_INTEGRATION_COMPLETION
    assert len(nba["self_serve_actions"]) == 2 and nba["pivota_path"]

    # Fully integrated non-cold -> not an integration gap.
    full = build_next_best_action(
        merchant_view=base,
        competitive_pressure={},
        integration_state={"fully_integrated": True},
        is_cold_start=False,
    )
    assert full["primary_gap"] != PRIMARY_INTEGRATION_COMPLETION

    # Cold audit with both pieces missing must NOT lead with integration.
    cold = build_next_best_action(
        merchant_view=base,
        competitive_pressure={},
        integration_state={"missing_pieces": ["store_platform", "psp"]},
        is_cold_start=True,
    )
    assert cold["primary_gap"] != PRIMARY_INTEGRATION_COMPLETION


def test_sku_nba_open_lane_uses_top_lane_first_move():
    opportunity = _sku_base_opportunity()
    opportunity["top_open_lanes"] = [
        {
            "query": "halal collagen sticks before bed",
            "first_move": "Add a PDP section + FAQ for this lane",
            "current_ownership": "open-lane",
            "source_route": "unclassified",
            "why_fit": ["halal", "collagen", "stick"],
            "opportunity_score": 42.5,
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(),
        identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_OPEN_LANE_CAPTURE
    assert "halal collagen sticks before bed" in nba["first_move"]
    assert nba["evidence_used"]["top_open_lane"]["query"] == "halal collagen sticks before bed"
    _assert_70_30(nba)


def test_sku_nba_substitution_names_substitute_in_first_move():
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "BB Lab collagen alternatives",
        "substituted_by": "Vital Proteins",
        "engines": ["deepseek", "gemini"],
    }

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(),
        identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK
    assert "Vital Proteins" in nba["first_move"]
    assert "comparison" in nba["first_move"].lower()
    _assert_70_30(nba)


def test_sku_nba_head_only_substitution_reframes_not_vs_flagship():
    """Niche-first reframe: when the only substitution evidence is a broad
    head prompt (Bose owns "best headphones"), the action must NOT prescribe
    a vs-flagship comparison as the first move — it names the reality and
    points at the measured beachhead lane instead."""
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "best headphones",
        "substituted_by": "Bose",
        "engines": ["chatgpt", "gemini"],
        "kind": "category",
        "broad_head_prompt": True,
    }
    opportunity["sideways_wedge"] = {
        "recommended_beachhead_lane": {
            "query": "bone conduction headphones for lap swimming",
        },
    }

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(),
        identity=_sku_identity(),
        sku_title="Purra Swim Headphones",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK
    # Names the reality without selling the head fight...
    assert "Bose" in nba["headline"]
    assert "owns the broad" in nba["headline"]
    # ...and the first move is the winnable beachhead, not a comparison page.
    assert "bone conduction headphones for lap swimming" in nba["first_move"]
    assert "comparison —" not in nba["first_move"]
    assert "vs" not in nba["first_move"].lower().split()
    _assert_70_30(nba)


def test_sku_nba_head_only_substitution_without_beachhead_stays_specific():
    """No measured beachhead lane -> the reframe still avoids the vs-flagship
    prescription and points at the prompt table's specific asks."""
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "best headphones",
        "substituted_by": "Bose",
        "engines": ["gemini"],
        "kind": "category",
        "broad_head_prompt": True,
    }

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(),
        identity=_sku_identity(),
        sku_title="Purra Swim Headphones",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK
    assert "owns the broad" in nba["headline"]
    assert "specific" in nba["first_move"].lower()
    assert "comparison —" not in nba["first_move"]


def test_blocked_thin_content_leads_with_foundation_not_substitution():
    """#9 (ANUKO 2026-07-02): a blocked SKU (content 14) with a substitution
    alert must lead with the content foundation fix, not a 'vs competitor'
    comparison — AI can't win a lane it can't even read the product in."""
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "ANUKO hair oil alternatives",
        "substituted_by": "K18",
        "engines": ["gemini"],
    }
    # Content critically thin (blocked band); other dims low too.
    scores = {
        "identity": {"score": 23},
        "content_richness": {"score": 14},
        "routability": {"score": 6},
        "citation": {"score": 21},
    }

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=scores,
        identity=_sku_identity(),
        sku_title="ANUKO Bond & Repair Hair Oil",
        catalog_unavailable=True,  # URL-wedge: get_indexed gate is skipped
    )
    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    assert "comparison" not in nba["first_move"].lower()


def test_readable_content_still_leads_with_substitution():
    """The foundation-first override only fires in the blocked band: a SKU with
    readable content (>=40) and a substitution alert still leads with the
    comparison move."""
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "BB Lab collagen alternatives",
        "substituted_by": "Vital Proteins",
        "engines": ["gemini"],
    }
    scores = {
        "identity": {"score": 70},
        "content_richness": {"score": 55},  # readable — above blocked band
        "routability": {"score": 60},
        "citation": {"score": 50},
    }
    nba = build_sku_next_best_action(
        opportunity=opportunity, scores=scores, identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )
    assert nba["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK


def test_foundation_first_boundary_is_strict_less_than_40():
    """The blocked-band boundary is exclusive: content_richness EXACTLY 40 is
    readable enough → substitution still leads. And an UNMEASURED content score
    (None) must not trigger foundation-first over a real demand signal."""
    def _nba(content_score):
        opportunity = _sku_base_opportunity()
        opportunity["substitution_alert"] = {
            "present": True, "prompt": "alts", "substituted_by": "K18",
            "engines": ["gemini"],
        }
        scores = {
            "identity": {"score": 50},
            "content_richness": {"score": content_score},
            "routability": {"score": 50},
            "citation": {"score": 40},
        }
        return build_sku_next_best_action(
            opportunity=opportunity, scores=scores, identity=_sku_identity(),
            sku_title="X",
        )

    assert _nba(40)["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK   # boundary
    assert _nba(39)["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP  # below
    assert _nba(None)["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK  # unmeasured


def test_sku_secondary_moves_never_leak_internal_metric_names():
    """ANUKO 2026-07-02: a secondary move titled 'Fix citation.first_party_rate'
    exposed the internal dimension.bucket to the merchant. Secondary moves must
    use the gap's merchant-safe label/why, never the raw scoring keys or the
    '/100 missing points' internal phrasing."""
    opportunity = _sku_base_opportunity()
    opportunity["substitution_alert"] = {
        "present": True,
        "prompt": "BB Lab collagen alternatives",
        "substituted_by": "Vital Proteins",
        "engines": ["gemini"],
    }
    gaps = [{
        "dimension": "citation",
        "bucket": "first_party_rate",
        "points": 3,
        "max": 45,
        "gap": 42,
        "reason": "1/16 prompts matched",  # internal — must NOT surface
        "label": "Cited as the source",
        "why": "When AI answers shopper questions in this category, it rarely points to you as the source.",
    }]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=gaps,
        scores=_sku_scores(),
        identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    moves = nba["secondary_moves"]
    assert moves, "expected a secondary move from the citation gap"
    move = moves[0]
    assert move["title"] == "Cited as the source"
    # No internal vocabulary anywhere in the merchant-facing move — INCLUDING the
    # evidence chip, which the sanitizer does not reach.
    blob = json.dumps(move).lower()
    for leak in ("first_party_rate", "citation.", "missing points", "score gap", "bucket", "dimension"):
        assert leak not in blob, (leak, move)
    assert move["reason"].startswith("When AI answers")
    # The evidence chip carries only merchant-safe copy.
    assert set(move["evidence"]) <= {"label", "why"}


def test_sku_nba_content_gap_uses_merchant_safe_display_copy():
    """The content-revision prescription must use the gap's _GAP_DISPLAY
    label/why — never the humanized scoring bucket ('vertical structure',
    'product quality score' are internal metric names)."""
    opportunity = _sku_base_opportunity()
    gaps = [
        {
            "dimension": "content_richness",
            "bucket": "vertical_structure",
            "points": 0,
            "max": 20,
            "gap": 20,
            # Production gaps always carry the merchant-safe display copy
            # (_primary_gaps attaches it; the coverage-guard test enforces it).
            "label": "Category-specific details",
            "why": "Shoppers in this category expect specifics (ingredients, materials, or specs) that aren't fully covered yet.",
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=gaps,
        scores=_sku_scores(65),
        identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    assert "category-specific details" in nba["first_move"].lower()
    assert "Shoppers in this category expect specifics" in nba["why_this_first"]
    assert "vertical structure" not in json.dumps(nba).lower()
    _assert_70_30(nba)


def test_sku_nba_content_gap_never_says_product_quality_score():
    """ANUKO 2026-07-03 re-run: the first move read 'Add the missing product
    quality score to <product>'s page' — an internal Pivota metric presented as
    page content. The whole NBA payload must be free of the bucket phrase and
    use the display label instead."""
    opportunity = _sku_base_opportunity()
    gaps = [{
        "dimension": "content_richness",
        "bucket": "product_quality_score",
        "points": 2,
        "max": 25,
        "gap": 23,
        "label": "Richer product detail",
        "why": "The product description is thin where shoppers and AI ask the most questions.",
    }]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=gaps,
        scores={"identity": {"score": 23}, "content_richness": {"score": 14},
                "routability": {"score": 6}, "citation": {"score": 21}},
        identity=_sku_identity(),
        sku_title="Anuko Bond & Repair Hair Oil",
        catalog_unavailable=True,
    )

    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    blob = json.dumps(nba).lower()
    assert "product quality score" not in blob
    assert "product_quality_score" not in blob
    assert "richer product detail" in nba["first_move"].lower()
    assert "thin where shoppers and AI ask" in nba["why_this_first"]


def test_sku_nba_content_gap_without_display_copy_falls_back_safely():
    """Foundation-first can fire on the content SCORE with no content gap in the
    top-N list (the ANUKO SKU2 shape) — the copy must fall back to a safe
    generic phrase, never 'content richness' (the internal dimension name)."""
    opportunity = _sku_base_opportunity()

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=[],  # no content gap surfaced
        scores={"identity": {"score": 23}, "content_richness": {"score": 14},
                "routability": {"score": 6}, "citation": {"score": 21}},
        identity=_sku_identity(),
        sku_title="Anuko Hair Butter",
        catalog_unavailable=True,
    )

    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    assert "richer product detail" in nba["first_move"].lower()
    assert "content richness" not in json.dumps(nba).lower()


def test_sku_nba_source_route_repair_uses_retailer_and_publisher_roles():
    for route, ownership, expected_type in [
        ("retailer", "retailer-owned", "retailer_listing_accuracy"),
        ("publisher", "publisher-owned", "publisher_source_pitch"),
    ]:
        opportunity = _sku_base_opportunity()
        opportunity["per_prompt"] = [
            {
                "query": f"best collagen source via {route}",
                "ownership_state": ownership,
                "source_route": route,
                "opportunity_score": 31.5,
                "demand_signal": 1.0,
                "source_summary": {
                    "top_cited_hosts": [{"host": f"{route}.example", "times_cited": 2}]
                },
            }
        ]

        nba = build_sku_next_best_action(
            opportunity=opportunity,
            scores=_sku_scores(85),
            identity=_sku_identity(),
            sku_title="BB Lab Good Night Collagen",
        )

        assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
        if route == "retailer":
            assert "first-order offer" not in nba["first_move"].lower()
            assert "starter + replenishment bundle" not in nba["first_move"].lower()
        assert nba["prescription_class"] == "operational_efficiency"
        assert nba["merchant_path"]["archetype"] == "brand"
        assert "more retrievable" in nba["first_move"].lower()
        assert "product/offer/review/faq schema" in nba["first_move"].lower()
        play_blob = json.dumps(nba["canonical_page_play"]).lower()
        assert expected_type in play_blob
        if route == "retailer":
            assert "claim or fix" in play_blob
            assert "first-order offer" in play_blob
            assert "starter + replenishment bundle" in play_blob
        else:
            assert "pitch publisher.example" in play_blob
            assert "first-order offer" in play_blob
            assert "starter + replenishment bundle" in play_blob
        assert nba["evidence_used"]["source_route_prompt"]["source_route"] == route
        _assert_70_30(nba)


def test_sku_nba_bb_lab_brand_path_ties_operational_moves_to_retailer_exposure():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 4, "prompts_with_demand": 4}
    opportunity["per_prompt"] = [
        {
            "query": "best collagen sticks",
            "axis": "category",
            "query_class": "head",
            "ownership_state": "marketplace-owned",
            "who_owns": ["amazon.com"],
            "source_route": "marketplace",
            "opportunity_score": 44.0,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "amazon.com", "times_cited": 3},
                    {"host": "walmart.com", "times_cited": 2},
                ]
            },
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(82),
        identity={
            "name": "BB LAB The Collagen Low Molecular Fish Collagen Stick",
            "anchors": {"brand": "BB Lab"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="BB LAB The Collagen Low Molecular Fish Collagen Stick",
    )

    copy = _nba_strings(nba)
    assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
    assert nba["prescription_class"] == "operational_efficiency"
    assert nba["merchant_path"]["archetype"] == "brand"
    assert nba["merchant_path"]["goal"] == "drive buyers to the brand's own website"
    assert "best collagen sticks" in copy
    assert "first-order offer" in copy
    assert "starter + replenishment bundle" in copy
    assert "subscription incentive" in copy
    assert "why-buy-direct" in copy
    assert nba["operator_moves"][0]["lane"] == "best collagen sticks"
    assert "amazon.com" in nba["operator_moves"][0]["evidence"]["controllers"]
    assert 'First-party citation rate on "best collagen sticks".' in nba["tracking_metrics"][0]
    assert "amazon.com and walmart.com" in nba["tracking_metrics"][1]
    assert nba["evidence_used"]["source_route_prompt"]["sources"][0]["host"] == "amazon.com"
    play = nba["canonical_page_play"]
    play_blob = json.dumps(play).lower()
    assert play["lane"] == "best collagen sticks"
    assert play["controllers"] == ["amazon.com", "walmart.com"]
    assert play["controller_strategy"] == "leading_retailer_competition"
    assert play["page"] == "your official PDP"
    assert {move["type"] for move in play["moves"]} == {
        "retail_listing_accuracy",
        "light_retrieval_schema_layer",
        "first_order_offer",
        "starter_replenishment_bundle",
        "subscription_or_why_buy_direct",
    }
    assert "product/offer schema" in play_blob
    assert "exact discount depths" in play["economics_policy"]
    assert "agent-checkout ready" in play["checkout_readiness"]
    assert "%" not in play_blob and "$" not in play_blob
    assert "%" not in copy and "$" not in copy


def test_sku_nba_bb_lab_sideways_wedge_prefers_halal_before_bed_over_head_pressure():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 2, "prompts_with_demand": 2}
    opportunity["product_evidence"] = build_lane_product_evidence(
        product={
            "title": "BB LAB The Collagen Low Molecular Fish Collagen Stick",
            "category": "collagen supplement",
            "tags": ["halal", "collagen", "stick"],
            "description": "Halal low molecular fish collagen sticks before bed.",
        },
        attribute_graph={
            "classes": {
                "category": ["collagen supplement"],
                "format": ["stick"],
                "ingredient": ["collagen"],
                "certification_constraint": ["halal"],
                "use_case": ["before bed"],
            }
        },
    )
    opportunity["per_prompt"] = [
        {
            "query": "best collagen sticks",
            "axis": "category",
            "query_class": "head",
            "ownership_state": "marketplace-owned",
            "source_route": "marketplace",
            "opportunity_score": 44.0,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "amazon.com", "times_cited": 3},
                    {"host": "walmart.com", "times_cited": 2},
                ]
            },
        },
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "query_class": "sidewalk",
            "ownership_state": "retailer-owned",
            "source_route": "retailer",
            "opportunity_score": 18.0,
            "demand_signal": 1.0,
            "attribute_basis": ["halal", "collagen", "stick", "before bed"],
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "sayweee.com", "times_cited": 2},
                    {"host": "dubuypk.com", "times_cited": 1},
                    {"host": "koreancare.net", "times_cited": 1},
                ]
            },
        },
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(82),
        identity={
            "name": "BB LAB The Collagen Low Molecular Fish Collagen Stick",
            "anchors": {"brand": "BB Lab"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="BB LAB The Collagen Low Molecular Fish Collagen Stick",
    )

    selected = nba["evidence_used"]["source_route_prompt"]
    wedge = nba["sideways_wedge"]
    assert selected["query"] == "halal collagen sticks before bed"
    assert wedge["recommended_beachhead_lane"]["query"] == "halal collagen sticks before bed"
    assert "best collagen sticks" in {item["query"] for item in wedge["do_not_chase_yet"]}
    assert "Start with \"halal collagen sticks before bed\"" in nba["why_this_first"]
    assert nba["canonical_page_play"]["controller_strategy"] == "canonical_source_vacuum"
    play_blob = json.dumps(nba["canonical_page_play"]).lower()
    assert "rank for the exact lane halal collagen sticks before bed" in play_blob
    assert "product/offer/review/faq schema" in play_blob
    assert "re-audit halal collagen sticks before bed" in play_blob
    assert "verify whether exposure becomes more citable" in play_blob
    # (The sideways-wedge canonical play carries lane / play / operator_moves /
    # pivota_path / economics_policy / controllers / controller_strategy — it has
    # no standalone confidence field; honesty of the play is covered by the
    # controller_strategy + re-audit assertions above and _assert_no_overpromise.)
    assert "%" not in _nba_strings(nba) and "$" not in _nba_strings(nba)
    _assert_no_overpromise(nba)


def test_sku_nba_ownist_brand_path_ties_offer_bundle_to_real_exposed_lane():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 5, "prompts_with_demand": 5}
    opportunity["per_prompt"] = [
        {
            "query": "best beauty supplement for glow",
            "axis": "category",
            "query_class": "head",
            "ownership_state": "retailer-owned",
            "who_owns": ["walmart.com", "amazon.com"],
            "source_route": "retailer",
            "opportunity_score": 39.0,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "walmart.com", "times_cited": 2},
                    {"host": "amazon.com", "times_cited": 2},
                ]
            },
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(80),
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="Ownist Triple Shine Grape",
    )

    copy = _nba_strings(nba)
    assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
    assert nba["merchant_path"]["archetype"] == "brand"
    assert "brand's own website" in nba["merchant_path"]["goal"]
    assert "best beauty supplement for glow" in copy
    assert "walmart.com" in nba["operator_moves"][0]["evidence"]["controllers"]
    assert "first-order offer" in copy
    assert "bundle" in copy
    assert "subscription incentive" in copy
    assert "why-buy-direct" in copy
    assert nba["canonical_page_play"]["lane"] == "best beauty supplement for glow"
    assert "walmart.com" in nba["canonical_page_play"]["controllers"]
    assert nba["canonical_page_play"]["controller_strategy"] == "leading_retailer_competition"
    assert "exact discount depths" in nba["canonical_page_play"]["economics_policy"]


def test_sku_nba_bb_lab_forum_authority_playbook_is_ordered_and_honest():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 4, "prompts_with_demand": 4}
    opportunity["per_prompt"] = [
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "query_class": "sidewalk",
            "ownership_state": "forum-owned",
            "source_route": "forum",
            "opportunity_score": 55.0,
            "demand_signal": 1.0,
            "attribute_basis": ["halal", "collagen", "stick", "before bed"],
            "source_summary": {
                "top_cited_hosts": [{"host": "reddit.com", "times_cited": 2}]
            },
            "source_roles": [{"host": "reddit.com", "role": "forum", "times_cited": 2}],
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(84),
        identity={"name": "BB Lab Good Night Collagen", "anchors": {"brand": "BB Lab"}},
        sku_title="BB Lab Good Night Collagen",
    )

    play = nba["canonical_page_play"]
    move_types = [move["type"] for move in play["moves"]]
    play_blob = json.dumps(play).lower()

    assert play["controller_strategy"] == "source_authority_gap"
    assert move_types == [
        "retrieval_lane_rank",
        "structured_extraction_schema",
        "facts_in_page_text",
        "reviews_authority_gap",
        "community_source_participation",
        "source_fact_consistency",
        "measure_reaudit_materiality",
        "direct_buy_reason",
    ]
    assert move_types[-1] == "direct_buy_reason"
    assert "rank for the exact lane halal collagen sticks before bed" in play_blob
    assert "product/offer/review/faq schema" in play_blob
    assert "state halal, collagen, stick, and before bed in plain page text" in play_blob
    assert "participate in or seed accurate product info in the reddit.com discussion" in play_blob
    assert "re-audit halal collagen sticks before bed" in play_blob
    assert "material buyer traffic" in play_blob
    assert "first-order offer" in play["moves"][-1]["operator_action"].lower()
    _assert_no_overpromise(nba)


def test_sku_nba_source_route_excludes_merchant_host_from_controller_copy():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 4, "prompts_with_demand": 4}
    opportunity["per_prompt"] = [
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "query_class": "sidewalk",
            "ownership_state": "forum-owned",
            "source_route": "unclassified",
            "opportunity_score": 55.0,
            "demand_signal": 1.0,
            "attribute_basis": ["halal", "collagen", "stick", "before bed"],
            "source_summary": {
                "buyer_path_controllers": [
                    {"host": "reddit.com", "role": "forum", "times_cited": 1},
                    {"host": "bblab.shop", "role": "unclassified", "times_cited": 2},
                ],
                "top_cited_hosts": [
                    {"host": "reddit.com", "times_cited": 1},
                    {"host": "bblab.shop", "times_cited": 2},
                ]
            },
            "source_roles": [
                {"host": "reddit.com", "role": "forum", "times_cited": 1},
                {"host": "bblab.shop", "role": "unclassified", "times_cited": 2},
            ],
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(84),
        identity={"name": "BB Lab Good Night Collagen", "anchors": {"brand": "BB Lab"}},
        sku_title="BB Lab Good Night Collagen",
        merchant_host="https://bblab.shop/products/good-night-collagen",
    )

    rendered = json.dumps({
        "headline": nba["headline"],
        "why_this_first": nba["why_this_first"],
        "first_move": nba["first_move"],
        "self_serve_actions": nba["self_serve_actions"],
        "operator_moves": nba["operator_moves"],
        "canonical_page_play": nba["canonical_page_play"],
        "evidence_summary": nba["evidence_summary"],
        "evidence_chips": nba["evidence_chips"],
        "tracking_metrics": nba["tracking_metrics"],
    }).lower()
    route_prompt = nba["evidence_used"]["source_route_prompt"]
    follow_up = nba["self_serve_actions"][1]

    assert [row["host"] for row in route_prompt["sources"]] == ["reddit.com"]
    assert "reddit.com" in rendered
    assert "bblab.shop" not in rendered
    assert "reddit.com discussion" in rendered
    assert "reddit.com and bblab.shop discussion" not in rendered
    assert "published on your official PDP. Keep SKU name" in follow_up
    assert "PDP Keep" not in follow_up


def test_controller_source_route_action_splits_mixed_forum_and_publisher():
    """A forum + publisher controller mix must not lump publishers into 'the
    discussion'; the forum gets the discussion play and publishers get pitched."""
    from services.next_best_action import _controller_source_route_action

    profile = {
        "classified_controllers": [
            {"host": "reddit.com", "input_role": "forum", "type": "forum"},
            {"host": "goodhousekeeping.com", "type": "publisher"},
            {"host": "whowhatwear.com", "type": "editorial"},
        ]
    }
    action = _controller_source_route_action(
        profile,
        "reddit.com, goodhousekeeping.com and whowhatwear.com",
        "halal collagen sticks before bed",
        "your PDP",
    )
    assert "in the reddit.com discussion" in action
    assert "pitch goodhousekeeping.com and whowhatwear.com with exact SKU facts" in action
    # publishers are never called part of "the discussion"
    assert "goodhousekeeping.com discussion" not in action
    assert "whowhatwear.com discussion" not in action


def test_controller_source_route_action_does_not_call_unclassified_source_a_discussion():
    from services.next_best_action import _controller_source_route_action

    profile = {
        "classified_controllers": [
            {"host": "reddit.com", "input_role": "forum", "type": "forum"},
            {"host": "moodarabia.com", "input_role": "unclassified", "type": "unclassified"},
        ]
    }
    action = _controller_source_route_action(
        profile,
        "reddit.com and moodarabia.com",
        "halal collagen sticks before bed",
        "your PDP",
    )

    assert "in the reddit.com discussion" in action
    assert "work the evidenced source trail around moodarabia.com" in action
    assert "pitch reddit.com and moodarabia.com" not in action
    assert "moodarabia.com discussion" not in action
    assert "reddit.com and moodarabia.com discussion" not in action


def test_sku_nba_publisher_authority_move_pitches_the_evidenced_publisher():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 3, "prompts_with_demand": 3}
    opportunity["per_prompt"] = [
        {
            "query": "best Korean collagen sticks",
            "axis": "category",
            "query_class": "head",
            "ownership_state": "publisher-owned",
            "source_route": "publisher",
            "opportunity_score": 28.0,
            "demand_signal": 1.0,
            "attribute_basis": ["korean", "collagen", "stick"],
            "source_summary": {
                "top_cited_hosts": [{"host": "beautyeditorial.example", "times_cited": 2}]
            },
            "source_roles": [
                {"host": "beautyeditorial.example", "role": "publisher", "times_cited": 2}
            ],
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(78),
        identity={"name": "Retailer Collagen Listing", "merchant_type": "retailer"},
        sku_title="Retailer Collagen Listing",
    )

    play = nba["canonical_page_play"]
    play_blob = json.dumps(play).lower()

    assert play["controller_strategy"] == "source_authority_gap"
    assert "publisher_source_pitch" in {move["type"] for move in play["moves"]}
    assert "pitch beautyeditorial.example for best korean collagen sticks" in play_blob
    assert "product/offer/review/faq schema" in play_blob
    assert play["moves"][-1]["type"] == "direct_buy_reason"
    _assert_no_overpromise(nba)


def test_sku_nba_ownist_leading_retailer_keeps_listing_and_conversion_path():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 5, "prompts_with_demand": 5}
    opportunity["per_prompt"] = [
        {
            "query": "where can I buy Ownist Triple Shine Grape",
            "axis": "intent",
            "query_class": "transactional",
            "ownership_state": "retailer-owned",
            "source_route": "retailer",
            "opportunity_score": 39.0,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "oliveyoung.com", "times_cited": 2},
                    {"host": "iherb.com", "times_cited": 2},
                ]
            },
            "source_roles": [
                {"host": "oliveyoung.com", "role": "retailer", "times_cited": 2},
                {"host": "iherb.com", "role": "retailer", "times_cited": 2},
            ],
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(80),
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="Ownist Triple Shine Grape",
    )

    play = nba["canonical_page_play"]
    move_types = [move["type"] for move in play["moves"]]
    play_blob = json.dumps(play).lower()

    assert play["controller_strategy"] == "leading_retailer_competition"
    assert move_types == [
        "retail_listing_accuracy",
        "light_retrieval_schema_layer",
        "first_order_offer",
        "starter_replenishment_bundle",
        "subscription_or_why_buy_direct",
    ]
    assert "claim or fix iherb.com and oliveyoung.com listings" in play_blob
    assert "product/offer schema as a light authority layer" in play_blob
    assert "product/offer/review/faq schema" not in play_blob
    assert "measure_reaudit_materiality" not in play_blob
    assert "first-order offer" in play_blob
    assert "starter + replenishment bundle" in play_blob
    assert "why-buy-direct" in play_blob
    _assert_no_overpromise(nba)


def _ownist_product_evidence(*, snack_positioning: bool = False) -> Dict[str, Any]:
    product = {
        "title": "Ownist Triple Shine Grape",
        "category": "healthy snacks" if snack_positioning else "beauty supplement",
        "tags": ["healthy snacks"] if snack_positioning else [],
    }
    graph = {
        "classes": {
            "category": ["collagen jelly"],
            "format": ["jelly"],
            "ingredient": ["collagen"] if snack_positioning else ["vitamin c", "collagen"],
            "use_case": [] if snack_positioning else ["healthy skin", "anti age"],
            "geography": [] if snack_positioning else ["korean"],
        }
    }
    return build_lane_product_evidence(product=product, attribute_graph=graph)


def _ownist_lane(
    query: str,
    *,
    opportunity_score: float,
    controllers: List[str],
    attribute_basis: List[str],
) -> Dict[str, Any]:
    return {
        "query": query,
        "axis": "sidewalk",
        "query_class": "sidewalk",
        "ownership_state": "retailer-owned",
        "source_route": "retailer",
        "opportunity_score": opportunity_score,
        "demand_signal": 1.0,
        "attribute_basis": attribute_basis,
        "source_summary": {
            "top_cited_hosts": [
                {"host": host, "times_cited": 2}
                for host in controllers
            ]
        },
    }


def test_sku_nba_ownist_prioritizes_conversion_fit_over_healthy_snacks_drift():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 5, "prompts_with_demand": 5}
    opportunity["product_evidence"] = _ownist_product_evidence()
    opportunity["per_prompt"] = [
        _ownist_lane(
            "healthy snacks collagen jelly",
            opportunity_score=18.0,
            controllers=["cogentsteps.net", "medsysgroup.com", "hellokoop.com"],
            attribute_basis=["healthy snacks", "collagen", "jelly"],
        ),
        _ownist_lane(
            "vitamin c collagen jelly",
            opportunity_score=5.45,
            controllers=["cogentsteps.net", "medsysgroup.com", "hellokoop.com"],
            attribute_basis=["vitamin c", "collagen", "jelly"],
        ),
        _ownist_lane(
            "healthy skin collagen jelly",
            opportunity_score=13.63,
            controllers=["ubuy.mq", "truehuebeauty.com", "dodoskin.com"],
            attribute_basis=["healthy skin", "collagen", "jelly"],
        ),
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(80),
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="Ownist Triple Shine Grape",
    )

    selected = nba["evidence_used"]["source_route_prompt"]
    copy = _nba_strings(nba)
    assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
    assert selected["query"] == "vitamin c collagen jelly"
    assert selected["lane_priority_score"] > 0
    wedge = nba["sideways_wedge"]
    assert wedge["recommended_beachhead_lane"]["query"] == "vitamin c collagen jelly"
    assert wedge["sideways_wedge_lanes"][0]["query"] == "vitamin c collagen jelly"
    assert "healthy snacks collagen jelly" in {
        item["query"] for item in wedge["do_not_chase_yet"]
    }
    assert "Start with \"vitamin c collagen jelly\"" in (
        wedge["why_this_lane_not_the_head_prompt"]
    )
    assert "Start with \"vitamin c collagen jelly\"" in nba["why_this_first"]
    assert wedge["canonical_page_play"]["lane"] == "vitamin c collagen jelly"
    assert "vitamin c collagen jelly" in copy
    assert "healthy snacks collagen jelly" not in nba["first_move"]
    assert "more citable + buyable" in copy
    assert "agent-checkout ready" in copy
    play = nba["canonical_page_play"]
    play_blob = json.dumps(play).lower()
    assert play["lane"] == "vitamin c collagen jelly"
    assert play["controllers"] == ["cogentsteps.net", "hellokoop.com", "medsysgroup.com"]
    assert play["controller_strategy"] == "canonical_source_vacuum"
    assert [move["type"] for move in play["moves"]] == [
        "retrieval_lane_rank",
        "structured_extraction_schema",
        "facts_in_page_text",
        "reviews_authority_gap",
        "retailer_listing_accuracy",
        "source_fact_consistency",
        "measure_reaudit_materiality",
        "direct_buy_reason",
    ]
    assert "rank for the exact lane vitamin c collagen jelly" in play_blob
    assert "product/offer/review/faq schema" in play_blob
    assert "state vitamin c, collagen, and jelly in plain page text" in play_blob
    assert "claim or fix the cogentsteps.net, hellokoop.com, and medsysgroup.com listing" in play_blob
    assert "re-audit vitamin c collagen jelly" in play_blob
    assert "after the page is more retrievable, extractable, and authoritative" in play_blob
    assert "first-order offer" in play_blob
    assert "starter + replenishment bundle" in play_blob
    assert "subscription incentive" in play_blob
    assert "why-buy-direct" in play_blob
    assert "first-order offer" not in nba["first_move"].lower()
    assert "starter + replenishment bundle" not in nba["first_move"].lower()
    assert "material buyer traffic" in play_blob
    assert "beat cogentsteps" not in play_blob
    assert "exact discount depths" in play["economics_policy"]
    assert "%" not in copy and "$" not in copy
    _assert_no_overpromise(nba)


def test_sku_nba_ownist_allows_healthy_snacks_when_explicitly_supported():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 3, "prompts_with_demand": 3}
    opportunity["product_evidence"] = _ownist_product_evidence(snack_positioning=True)
    opportunity["per_prompt"] = [
        _ownist_lane(
            "healthy snacks collagen jelly",
            opportunity_score=18.0,
            controllers=["cogentsteps.net", "medsysgroup.com", "hellokoop.com"],
            attribute_basis=["healthy snacks", "collagen", "jelly"],
        ),
        _ownist_lane(
            "vitamin c collagen jelly",
            opportunity_score=5.45,
            controllers=["cogentsteps.net", "medsysgroup.com", "oliveyoung.com"],
            attribute_basis=["vitamin c", "collagen", "jelly"],
        ),
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(80),
        identity={
            "name": "Ownist Collagen Healthy Snack Jelly",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
            "unresolved": False,
        },
        sku_title="Ownist Collagen Healthy Snack Jelly",
    )

    selected = nba["evidence_used"]["source_route_prompt"]
    assert selected["query"] == "healthy snacks collagen jelly"
    assert selected["fit_penalties"] == []
    wedge = nba["sideways_wedge"]
    assert wedge["recommended_beachhead_lane"]["query"] == "healthy snacks collagen jelly"
    assert wedge["do_not_chase_yet"] == []


def test_sku_nba_channel_path_drives_to_channel_site_when_explicit():
    opportunity = _sku_base_opportunity()
    opportunity["per_prompt"] = [
        {
            "query": "best Korean collagen sticks",
            "ownership_state": "publisher-owned",
            "source_route": "publisher",
            "opportunity_score": 28.0,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [{"host": "beautyeditorial.example", "times_cited": 2}]
            },
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        scores=_sku_scores(78),
        identity={
            "name": "Retailer Collagen Listing",
            "merchant_type": "retailer",
            "unresolved": False,
        },
        sku_title="Retailer Collagen Listing",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
    assert nba["merchant_path"]["archetype"] == "channel"
    assert nba["merchant_path"]["goal"] == "drive buyers to the channel's website"
    assert "channel PDP or category page" in nba["first_move"]
    assert "brand's own website" not in _nba_strings(nba)


def test_sku_nba_resolved_coverage_with_exposure_never_returns_insufficient_data():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 14, "prompts_with_demand": 14}
    opportunity["per_prompt"] = [
        {
            "query": "best multivitamin",
            "ownership_state": "publisher-owned",
            "source_route": "publisher",
            "opportunity_score": 4.2,
            "demand_signal": 1.0,
            "source_summary": {
                "top_cited_hosts": [
                    {"host": "medicalnewstoday.com", "times_cited": 2}
                ]
            },
        }
    ]

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=[],
        scores={},
        identity=_sku_identity(unresolved=False),
        sku_title="Ritual Essential for Women 18+ Multivitamin",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_SOURCE_ROUTE_REPAIR
    assert nba["primary_gap"] != PRIMARY_SKU_INSUFFICIENT_DATA
    assert "medicalnewstoday.com" in nba["headline"]
    _assert_70_30(nba)


def test_sku_nba_protected_monitoring_has_no_fake_urgency():
    opportunity = _sku_base_opportunity()
    opportunity["per_prompt"] = [
        {
            "query": "where can I buy BB Lab Good Night Collagen",
            "ownership_state": "merchant-owned",
            "source_route": "first-party",
            "opportunity_score": 0.0,
            "demand_signal": 1.0,
        },
        {
            "query": "BB Lab Good Night Collagen review",
            "ownership_state": "merchant-mentioned",
            "source_route": "first-party",
            "opportunity_score": 0.0,
            "demand_signal": 0.7,
        },
    ]
    opportunity["confidence"] = {"prompt_count": 2, "prompts_with_demand": 2}

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=[],
        scores=_sku_scores(90),
        identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_PROTECTED_MONITORING
    blob = (nba["headline"] + " " + nba["first_move"] + " " + nba["why_this_first"]).lower()
    assert not any(p in blob for p in ("act now", "before it", "don't wait", "hurry", "limited time"))
    _assert_70_30(nba)


def test_sku_nba_thin_coverage_returns_insufficient_data():
    opportunity = _sku_base_opportunity()
    opportunity["confidence"] = {"prompt_count": 0, "prompts_with_demand": 0}

    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=[],
        scores={},
        identity=_sku_identity(unresolved=True),
        sku_title="BB Lab Good Night Collagen",
    )

    assert nba["primary_gap"] == PRIMARY_SKU_INSUFFICIENT_DATA
    copy = _nba_strings(nba)
    assert "Build the product evidence foundation" in nba["headline"]
    assert "complete canonical PDP" in nba["first_move"]
    assert "first-order offer" in copy
    assert "starter + replenishment bundle" in copy
    assert "subscription incentive" in copy
    assert "why-buy-direct" in copy
    assert not any(
        bad in copy
        for bad in (
            "fallback",
            "re-run",
            "rerun",
            "try again",
            "not enough signal",
            "couldn't",
            "won't invent",
        )
    )
    _assert_70_30(nba)


_FORBIDDEN_JARGON = (
    "/100", "source route", "opportunity score", "canonical enriched",
    "agent-resolvable", "schema-friendly", "content_richness", "grounded agent",
)


def _nba_strings(nba: Dict[str, Any]) -> str:
    parts = [
        str(nba.get("headline") or ""),
        str(nba.get("why_this_first") or ""),
        str(nba.get("first_move") or ""),
        str(nba.get("evidence_summary") or ""),
        *[str(a) for a in nba.get("evidence_chips") or []],
        str(nba.get("pivota_path") or ""),
        *[str(a) for a in nba.get("self_serve_actions") or []],
        *[str(a) for a in nba.get("tracking_metrics") or []],
        str((nba.get("cta") or {}).get("label") or ""),
        str((nba.get("cta") or {}).get("trust_note") or ""),
    ]
    return " ".join(parts).lower()


_OVERPROMISE_PATTERNS = (
    "will cite",
    "will rank",
    "guaranteed",
    "guarantee ai citation",
    "guarantee ai ranking",
    "rank #1",
)


def _assert_no_overpromise(payload: Mapping[str, Any]) -> None:
    blob = json.dumps(payload).lower()
    leaked = [pattern for pattern in _OVERPROMISE_PATTERNS if pattern in blob]
    assert not leaked, leaked


def test_no_internal_jargon_leaks_to_merchant_copy():
    """Merchant-facing prescription copy (brand AND per-SKU) must never expose
    internal scoring jargon, raw taxonomy, or fake-precision scores."""
    nbas: List[Dict[str, Any]] = []

    nbas.append(build_next_best_action(
        merchant_view=_merchant_view(
            verdict="INVISIBLE", visibility=10, attribution=5, category_visibility=8,
            failed_queries=[_failed_query("best collagen")]),
        is_cold_start=True))
    nbas.append(build_next_best_action(
        merchant_view=_merchant_view(
            verdict="VISIBLE VIA RETAILERS", visibility=70, attribution=40, category_visibility=65,
            cited_hosts=[{"host": "amazon.com", "type": "marketplace", "times_cited": 3}]),
        is_cold_start=True))
    nbas.append(build_next_best_action(
        merchant_view=_merchant_view(
            verdict="STRONG", visibility=90, attribution=88, category_visibility=85),
        is_cold_start=True))

    open_opp = _sku_base_opportunity()
    open_opp["top_open_lanes"] = [{
        "query": "halal collagen sticks before bed", "first_move": "x",
        "current_ownership": "open-lane", "source_route": "unclassified",
        "why_fit": ["halal", "collagen"], "opportunity_score": 42.5,
    }]
    nbas.append(build_sku_next_best_action(
        opportunity=open_opp, scores=_sku_scores(), identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen"))

    sub_opp = _sku_base_opportunity()
    sub_opp["substitution_alert"] = {
        "present": True, "prompt": "BB Lab collagen alternatives",
        "substituted_by": "Vital Proteins", "engines": ["gemini"],
    }
    nbas.append(build_sku_next_best_action(
        opportunity=sub_opp, scores=_sku_scores(), identity=_sku_identity(),
        sku_title="BB Lab Good Night Collagen"))

    for nba in nbas:
        blob = _nba_strings(nba)
        leaked = [j for j in _FORBIDDEN_JARGON if j in blob]
        assert not leaked, f"{nba['primary_gap']} leaked jargon {leaked}: {blob}"
