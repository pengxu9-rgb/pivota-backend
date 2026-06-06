from __future__ import annotations

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
    assert set(nba["cta"]) == {"label", "trust_note"}


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


def test_sku_nba_content_gap_references_content_richness_bucket():
    opportunity = _sku_base_opportunity()
    gaps = [
        {
            "dimension": "content_richness",
            "bucket": "vertical_structure",
            "points": 0,
            "max": 20,
            "gap": 20,
            "reason": "missing ingredients and usage guides",
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
    assert "vertical structure" in nba["first_move"]
    assert "vertical structure" in nba["why_this_first"]
    _assert_70_30(nba)


def test_sku_nba_source_route_repair_uses_retailer_and_publisher_roles():
    for route, ownership, expected in [
        ("retailer", "retailer-owned", "first-order offer"),
        ("publisher", "publisher-owned", "pitch the cited publisher"),
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
        blob = _nba_strings(nba)
        assert expected in nba["first_move"].lower()
        assert nba["prescription_class"] == "operational_efficiency"
        assert nba["merchant_path"]["archetype"] == "brand"
        assert "bundle" in blob
        assert "subscription" in blob or "buying reason" in blob
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
    assert nba["evidence_used"]["source_route_prompt"]["sources"][0]["host"] == "amazon.com"
    assert "%" not in copy and "$" not in copy


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
    assert "won't invent" in nba["why_this_first"]
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
        str(nba.get("pivota_path") or ""),
        *[str(a) for a in nba.get("self_serve_actions") or []],
        str((nba.get("cta") or {}).get("label") or ""),
        str((nba.get("cta") or {}).get("trust_note") or ""),
    ]
    return " ".join(parts).lower()


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
