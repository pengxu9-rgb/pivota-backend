from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

import pytest

from services import strategic_brief
from services.agent_center_bd_report_service import _sku_intelligence_headline
from services.next_best_action import (
    attach_sku_strategic_brief,
    build_sku_next_best_action,
)

_OVERPROMISE_PATTERNS = (
    "will cite",
    "will rank",
    "guaranteed",
    "guarantee ai citation",
    "guarantee ai ranking",
    "rank #1",
)


def _assert_no_overpromise_payload(payload: Mapping[str, Any]) -> None:
    blob = json.dumps(payload).lower()
    leaked = [pattern for pattern in _OVERPROMISE_PATTERNS if pattern in blob]
    assert not leaked, leaked


def _attribute_graph() -> Dict[str, Any]:
    return {
        "classes": {
            "category": ["collagen supplement"],
            "format": ["stick"],
            "ingredient": ["collagen", "vitamin c", "glycine"],
            "certification_constraint": ["halal"],
            "audience": [],
            "use_case": ["before bed"],
            "geography": ["k-beauty"],
            "proof": [],
            "exclusion": ["no water"],
        }
    }


def _opportunity() -> Dict[str, Any]:
    return {
        "intent_ladder": {
            "branded_transactional": {"score": 100},
            "head_category": {"score": 0},
            "branded_consideration": {"score": 0},
        },
        "per_prompt": [
            {
                "query": "best collagen supplements for skin",
                "axis": "category",
                "query_class": "head",
                "provider_verdicts": {"gemini": "loss", "deepseek": "loss"},
                "ownership_state": "competitor-owned",
                "demand_signal": 1.0,
                "opportunity_score": 14.2,
                "competitors": ["Vital Proteins", "NeoCell", "Sports Research"],
                "source_roles": [
                    {"host": "forbes.com", "role": "publisher", "times_cited": 2},
                    {"host": "amazon.com", "role": "marketplace", "times_cited": 2},
                ],
            },
            {
                "query": "halal collagen sticks before bed",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "open_lane": True,
                "ownership_state": "open-lane",
                "source_route": "unclassified",
                "demand_signal": 1.0,
                "opportunity_score": 55.0,
                "attribute_basis": ["halal", "collagen", "stick", "before bed"],
                "source_roles": [
                    {"host": "wellness-notes.example", "role": "publisher", "times_cited": 2},
                    {"host": "halal-beauty.example", "role": "publisher", "times_cited": 2},
                ],
            },
            {
                "query": "BB Lab collagen alternatives",
                "axis": "comparison",
                "query_class": "branded",
                "provider_verdicts": {"gemini": "loss", "deepseek": "loss"},
                "ownership_state": "competitor-owned",
                "demand_signal": 1.0,
                "competitors": ["Vital Proteins", "NeoCell"],
                "substitution": {
                    "present": True,
                    "prompt": "bb lab collagen alternatives",
                    "substituted_by": "Vital Proteins",
                    "engines": ["deepseek", "gemini"],
                },
            },
        ],
        "top_open_lanes": [
            {
                "query": "halal collagen sticks before bed",
                "why_fit": ["halal", "collagen", "stick", "before bed"],
                "current_ownership": "open-lane",
                "source_route": "unclassified",
            }
        ],
        "substitution_alert": {
            "present": True,
            "prompt": "bb lab collagen alternatives",
            "substituted_by": "Vital Proteins",
            "engines": ["deepseek", "gemini"],
        },
        "demand_state_summary": "open lane detected",
    }


def _identity() -> Dict[str, Any]:
    return {
        "name": "BB Lab Good Night Collagen",
        "anchors": {"brand": "BB Lab", "category": "collagen supplement"},
    }


def _controller_opportunity(
    top_cited_hosts: List[Mapping[str, Any]],
    *,
    who_owns: Any = "reddit.com",
    source_route: str = "forum",
    ownership_state: str = "forum-owned",
    source_roles: List[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "query": "halal collagen sticks before bed",
        "axis": "sidewalk",
        "query_class": "sidewalk",
        "ownership_state": ownership_state,
        "source_route": source_route,
        "who_owns": who_owns,
        "demand_signal": 1.0,
        "opportunity_score": 42.0,
        "attribute_basis": ["halal", "collagen", "stick", "before bed"],
        "source_summary": {"top_cited_hosts": list(top_cited_hosts)},
    }
    if source_roles is not None:
        row["source_roles"] = source_roles
    return {
        "intent_ladder": {},
        "per_prompt": [row],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": "third-party source route detected",
    }


def _controller_surfaces(opportunity: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )
    nba = build_sku_next_best_action(
        opportunity=opportunity,
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )
    return {
        # W4: the deterministic brief was deleted; controller-surface STABILITY is
        # asserted on the live evidence/nba surfaces below (where the property
        # actually lives), not on the retired brief prose.
        "buyer_path_controllers": (
            evidence.get("buyer_path_opportunities") or [{}]
        )[0].get("controlled_by"),
        "sku_headline": _sku_intelligence_headline(
            opportunity=opportunity,
            title="BB Lab Good Night Collagen",
            product_type="collagen supplement",
        ),
        "nba_headline": nba.get("headline"),
        "canonical_page_controllers": (
            nba.get("canonical_page_play") or {}
        ).get("controllers"),
        "controller_strategy": (
            evidence.get("buyer_path_opportunities") or [{}]
        )[0].get("controller_strategy"),
    }


def _evidence() -> Dict[str, Any]:
    return strategic_brief.assemble_sku_brief_evidence(
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )


def _grounded_brief() -> Dict[str, Any]:
    return {
        "position": (
            "BB Lab Good Night Collagen is protected when named, but absent "
            "from the broad collagen category."
        ),
        "core_decision": (
            "Your halal bedtime collagen stick fits the halal collagen sticks "
            "before bed search, so claim that search first for BB Lab."
        ),
        "why_you_lose": (
            "AI's answers show Vital Proteins, NeoCell, and Sports Research "
            "winning through forbes.com and amazon.com, which suggests a "
            "structural authority and distribution moat."
        ),
        "your_angle": (
            "BB Lab should become the halal collagen stick for before bed, "
            "using halal, collagen, stick, and before bed as the wedge."
        ),
        "traffic_strategy": [
            {
                "where": "halal collagen sticks before bed",
                "who_controls": "none/fragmented",
                "how": (
                    "Own the BB Lab page for halal collagen sticks before bed "
                    "and monitor wellness-notes.example and halal-beauty.example."
                ),
            },
            {
                "where": "best collagen supplements for skin",
                "who_controls": "forbes.com and amazon.com",
                "how": "Do not chase this search first because the named winners already hold it.",
            },
        ],
        "substitution_play": (
            "Win BB Lab collagen alternatives by explaining when BB Lab is the "
            "halal bedtime option instead of Vital Proteins."
        ),
        "first_moves": [
            "Add the halal collagen sticks before bed story to the BB Lab page.",
            "Create a BB Lab collagen alternatives comparison against Vital Proteins.",
            "Track forbes.com and amazon.com before deciding whether to pursue the broad category.",
        ],
        "diy_vs_pivota": {
            "self_serve": [
                "Write the halal bedtime section.",
                "Publish the comparison page.",
            ],
            "pivota": "Pivota turns the BB Lab page into a cited and buyable canonical page.",
        },
    }


def _validation_fix_evidence() -> Dict[str, Any]:
    return {
        "product": {
            "title": "BB Lab Low Molecular Collagen",
            "brand": "BB Lab",
            "attributes": {
                "category": ["collagen supplement"],
                "format": ["stick", "powder"],
                "ingredient": ["low molecular collagen", "vitamin c"],
                "certification": ["halal"],
                "audience": ["women"],
                "use_case": ["before bed", "skin"],
            },
        },
        "position": {
            "strong_when_named": "strong",
            "weak_in_category": "weak",
            "branded_consideration": "moderate",
        },
        "category_battle": {
            "prompts": [
                "best collagen supplements for skin",
                "top collagen for women",
            ],
            "winners": ["Vital Proteins", "NeoCell", "Sports Research"],
            "ranked_by": [
                {"host": "healthline.com", "role": "publisher"},
                {"host": "amazon.com", "role": "marketplace"},
            ],
            "prompt_details": [],
        },
        "substitution": {
            "present": True,
            "on_prompt": "BB Lab collagen alternatives",
            "handed_to": "Vital Proteins",
            "engines": ["gemini"],
        },
        "open_lanes": [
            {
                "query": "halal collagen sticks before bed",
                "why_fit": ["halal", "collagen", "stick", "before bed"],
                "who_controls": "none/fragmented",
                "channel_role": "open",
            }
        ],
        "channel_map": [
            {
                "query": "halal collagen sticks before bed",
                "controlled_by": [{"host": "reddit.com", "role": "forum"}],
                "role": "open",
            }
        ],
        "demand_state": "branded protected, category lost, niche open",
        "notes": {"merchant_can_act_in_30d": True, "health_sensitive": True},
    }


def _validation_fix_evidence_with_competitor_attributes(
    attrs: List[str] | None = None,
) -> Dict[str, Any]:
    attrs = attrs or ["collagen peptides", "grass-fed collagen"]
    evidence = _validation_fix_evidence()
    evidence["grounding_notes"] = {
        "competitor_attributes": {
            "status": "assessed",
            "competitor": "Vital Proteins",
            "attributes_present": attrs,
            "evidence": [
                {
                    "attribute": attr,
                    "provider": "gemini",
                    "verbatim": f"Vital Proteins is associated with {attr}.",
                }
                for attr in attrs
            ],
            "note": "Grounded presence only - not a claim the competitor lacks anything else.",
        },
        "merchant_channels": "unknown",
        "evidenced_channels": [
            {"host": "healthline.com", "role": "publisher"},
            {"host": "amazon.com", "role": "marketplace"},
        ],
    }
    return evidence


def _validation_fix_grounded_brief() -> Dict[str, Any]:
    return {
        "position": (
            "BB Lab is a niche challenger - strong when shoppers name you, "
            "invisible in the broad collagen category."
        ),
        "core_decision": (
            "Stop fighting Vital Proteins head-on in the category. Double down "
            "on the halal bedtime-collagen lane where no one is the answer yet."
        ),
        "why_you_lose": (
            "Vital Proteins, NeoCell, and Sports Research win because Healthline "
            "ranks them and Amazon reviews back them. Reviews and publisher "
            "authority are the moat, not your page."
        ),
        "your_angle": (
            "Reframe BB Lab as the halal, low-molecular bedtime collagen stick - "
            "a category of one. Concentrate where your halal certification IS "
            "the answer."
        ),
        "traffic_strategy": [
            {
                "where": "halal collagen sticks before bed",
                "who_controls": "none/fragmented",
                "how": "Own your product pages so AI has your answer to cite.",
            },
            {
                "where": "category prompts",
                "who_controls": "Healthline",
                "how": (
                    "Skip Healthline for now - earn niche placements first."
                ),
            },
        ],
        "substitution_play": (
            "Counter the Vital Proteins hand-off with a direct comparison on "
            "your page."
        ),
        "first_moves": [
            "Add the halal and bedtime story to your page.",
            "Seed reviews on Amazon to close the authority gap.",
            "Target the open halal lane with a dedicated page.",
            "Publish a comparison versus Vital Proteins.",
        ],
        "diy_vs_pivota": {
            "self_serve": ["Update your PDP", "Collect reviews"],
            "pivota": (
                "Pivota makes your page citable and buyable for the lanes you "
                "claim."
            ),
        },
    }


def test_assemble_sku_brief_evidence_is_traceable_to_audit_inputs():
    evidence = _evidence()

    assert evidence["product"]["title"] == "BB Lab Good Night Collagen"
    assert evidence["product"]["brand"] == "BB Lab"
    assert evidence["product"]["merchant_path"] == {
        "archetype": "brand",
        "destination": "the brand's own website",
        "page_label": "the official brand PDP",
        "goal": "drive buyers to the brand's own website",
    }
    assert evidence["product"]["attributes"]["certification"] == ["halal"]
    assert evidence["product"]["attributes"]["format"] == ["stick"]
    assert evidence["position"] == {
        "strong_when_named": "strong",
        "weak_in_category": "weak",
        "branded_consideration": "weak",
    }
    assert evidence["category_battle"]["prompts"] == ["best collagen supplements for skin"]
    assert evidence["category_battle"]["winners"] == [
        "Vital Proteins",
        "NeoCell",
        "Sports Research",
    ]
    assert {"host": "forbes.com", "role": "publisher", "times_cited": 2} in evidence["category_battle"]["ranked_by"]
    assert {"host": "amazon.com", "role": "marketplace", "times_cited": 2} in evidence["category_battle"]["ranked_by"]
    assert evidence["substitution"]["on_prompt"] == "bb lab collagen alternatives"
    assert evidence["substitution"]["handed_to"] == "Vital Proteins"
    assert evidence["open_lanes"][0] == {
        "query": "halal collagen sticks before bed",
        "why_fit": ["halal", "collagen", "stick", "before bed"],
        "who_controls": "none/fragmented",
        "channel_role": "open",
    }
    assert evidence["channel_map"][0]["controlled_by"] == [
        {"host": "halal-beauty.example", "role": "publisher", "times_cited": 2},
        {"host": "wellness-notes.example", "role": "publisher", "times_cited": 2},
    ]
    assert evidence["buyer_path_opportunities"][0]["query"] == "best collagen supplements for skin"
    assert evidence["buyer_path_opportunities"][0]["merchant_archetype"] == "brand"
    assert "first-order offer" in " ".join(
        evidence["buyer_path_opportunities"][0]["recommended_moves"]
    )
    assert {"host": "forbes.com", "role": "publisher", "times_cited": 2} in (
        evidence["buyer_path_opportunities"][0]["controlled_by"]
    )
    grounding_notes = evidence["grounding_notes"]
    assert set(grounding_notes.keys()) == {
        "competitor_attributes",
        "merchant_channels",
        "evidenced_channels",
    }
    assert grounding_notes["competitor_attributes"] == "not_assessed"
    assert grounding_notes["merchant_channels"] == "unknown"
    assert {"host": "forbes.com", "role": "publisher", "times_cited": 2} in grounding_notes["evidenced_channels"]
    assert {"host": "amazon.com", "role": "marketplace", "times_cited": 2} in grounding_notes["evidenced_channels"]
    assert {"host": "wellness-notes.example", "role": "publisher", "times_cited": 2} in grounding_notes["evidenced_channels"]
    assert {"host": "halal-beauty.example", "role": "publisher", "times_cited": 2} in grounding_notes["evidenced_channels"]


def test_stable_controller_surfaces_ignore_run_to_run_one_off_citation_tails():
    run_a = _controller_opportunity(
        [
            {"host": "sayweee.com", "times_cited": 1},
            {"host": "dubuypk.com", "times_cited": 1},
            {"host": "koreancare.net", "times_cited": 1},
        ],
        who_owns="reddit.com",
        source_roles=[{"host": "reddit.com", "role": "forum", "times_cited": 2}],
    )
    run_b = _controller_opportunity(
        [
            {"host": "shop.tiktok.com", "times_cited": 1},
            {"host": "ramuskin.com", "times_cited": 1},
            {"host": "reddit.com", "times_cited": 1},
        ],
        who_owns="reddit.com",
        source_roles=[{"host": "reddit.com", "role": "forum", "times_cited": 2}],
    )

    surfaces_a = _controller_surfaces(run_a)
    surfaces_b = _controller_surfaces(run_b)
    blob = json.dumps(surfaces_a).lower()

    assert surfaces_a == surfaces_b
    assert surfaces_a["buyer_path_controllers"] == [
        {"host": "reddit.com", "role": "forum", "times_cited": 2}
    ]
    assert surfaces_a["canonical_page_controllers"] == ["reddit.com"]
    assert "reddit.com" in surfaces_a["sku_headline"]
    for one_off in (
        "sayweee.com",
        "dubuypk.com",
        "koreancare.net",
        "shop.tiktok.com",
        "ramuskin.com",
    ):
        assert one_off not in blob


def test_stable_controller_threshold_excludes_one_offs_and_allows_repeated_hosts():
    opportunity = _controller_opportunity(
        [
            {"host": "sayweee.com", "times_cited": 1},
            {"host": "healthline.com", "times_cited": 2},
        ],
        who_owns=None,
        source_route="publisher",
        ownership_state="publisher-owned",
    )

    surfaces = _controller_surfaces(opportunity)
    blob = json.dumps(surfaces).lower()

    assert surfaces["buyer_path_controllers"] == [
        {"host": "healthline.com", "role": "publisher", "times_cited": 2}
    ]
    assert "healthline.com" in blob
    assert "sayweee.com" not in blob


def test_fragmented_controller_lanes_are_framed_without_naming_one_off_hosts():
    opportunity = _controller_opportunity(
        [
            {"host": "sayweee.com", "times_cited": 1},
            {"host": "dubuypk.com", "times_cited": 1},
        ],
        who_owns=None,
        source_route="publisher",
        ownership_state="publisher-owned",
    )

    surfaces = _controller_surfaces(opportunity)
    blob = json.dumps(surfaces).lower()

    assert surfaces["buyer_path_controllers"] == []
    assert surfaces["canonical_page_controllers"] == []
    assert surfaces["controller_strategy"] == "canonical_source_vacuum"
    assert "fragmented" in surfaces["sku_headline"]
    assert "sayweee.com" not in blob
    assert "dubuypk.com" not in blob


def test_controller_quality_strategy_uses_stable_controllers_not_one_off_tail():
    retailer = _controller_opportunity(
        [{"host": "reddit.com", "times_cited": 1}],
        who_owns="oliveyoung.com",
        source_route="retailer",
        ownership_state="retailer-owned",
    )
    forum = _controller_opportunity(
        [{"host": "oliveyoung.com", "times_cited": 1}],
        who_owns="reddit.com",
        source_route="forum",
        ownership_state="forum-owned",
    )

    retailer_surfaces = _controller_surfaces(retailer)
    forum_surfaces = _controller_surfaces(forum)

    assert retailer_surfaces["buyer_path_controllers"][0]["host"] == "oliveyoung.com"
    assert retailer_surfaces["controller_strategy"] == "leading_retailer_competition"
    assert forum_surfaces["buyer_path_controllers"][0]["host"] == "reddit.com"
    assert forum_surfaces["controller_strategy"] != "leading_retailer_competition"
    assert "oliveyoung.com" not in json.dumps(forum_surfaces["buyer_path_controllers"])


def test_aggregate_controller_profile_stable_when_hero_lane_sources_flip():
    """Run-to-run probe variance flips the hero lane's cited sources
    (retailers one run, a forum the next). The SKU-level aggregate must hold the
    same merchant-facing archetype because the SKU's other lanes are retail-
    dominant either way. This is the credibility-killer the wave fixes."""
    from services.buyer_path_controller_quality import aggregate_controller_profile

    retail_dominant_tail = [
        [{"host": "walmart.com", "times_cited": 5}],
        [{"host": "walmart.com", "times_cited": 2}, {"host": "target.com", "times_cited": 2}],
        [
            {"host": "iherb.com", "times_cited": 2},
            {"host": "target.com", "times_cited": 2},
            {"host": "walmart.com", "times_cited": 2},
        ],
    ]
    run_retail_hero = [
        [{"host": "iherb.com", "times_cited": 2}, {"host": "yesstyle.com", "times_cited": 2}],
        *retail_dominant_tail,
    ]
    run_forum_hero = [
        [{"host": "reddit.com", "times_cited": 2}],
        *retail_dominant_tail,
    ]

    profile_a = aggregate_controller_profile(run_retail_hero)
    profile_b = aggregate_controller_profile(run_forum_hero)

    assert profile_a["strategy"] == "leading_retailer_competition"
    assert profile_b["strategy"] == profile_a["strategy"]
    # The stable #1 controller (cited across many lanes) leads both runs.
    assert profile_a["controllers"][0] == "walmart.com"
    assert profile_b["controllers"][0] == "walmart.com"


def test_aggregate_controller_profile_stable_authority_when_retail_volume_spikes():
    """An authority-led SKU (a forum cited every run) must stay source_authority_gap
    even when a known-retailer's citation volume spikes on one run, so the brief
    archetype does not wobble into the canonical-vacuum/retail framing."""
    from services.buyer_path_controller_quality import aggregate_controller_profile

    run_authority_heavy = [
        [{"host": "reddit.com", "times_cited": 5}],
        [{"host": "goodhousekeeping.com", "times_cited": 4}],
        [{"host": "healthline.com", "times_cited": 2}],
        [{"host": "yesstyle.com", "times_cited": 4}],
    ]
    run_retail_volume_spike = [
        [{"host": "reddit.com", "times_cited": 5}],
        [{"host": "yesstyle.com", "times_cited": 8}],
        [{"host": "yesstyle.com", "times_cited": 1}],
    ]

    profile_a = aggregate_controller_profile(run_authority_heavy)
    profile_b = aggregate_controller_profile(run_retail_volume_spike)

    assert profile_a["strategy"] == "source_authority_gap"
    assert profile_b["strategy"] == "source_authority_gap"
    assert profile_a["controllers"][0] == "reddit.com"
    assert profile_b["controllers"][0] == "reddit.com"


def test_aggregate_controller_profile_does_not_let_tiny_authority_tail_beat_retail():
    """A small repeated authority tail should not relabel a retail-dominant SKU
    as authority-led. Absolute citation count alone is not enough when the
    retail share is overwhelming."""
    from services.buyer_path_controller_quality import aggregate_controller_profile

    profile = aggregate_controller_profile([
        [{"host": "yesstyle.com", "times_cited": 38}],
        [{"host": "reddit.com", "role": "forum", "times_cited": 2}],
    ])

    assert profile["strategy"] == "canonical_source_vacuum"
    assert profile["controllers"][0] == "yesstyle.com"


def test_aggregate_controller_profile_excludes_merchant_own_host():
    """The merchant's own site, even if heavily cited across lanes, must never be
    named as a buyer-path controller."""
    from services.buyer_path_controller_quality import aggregate_controller_profile

    groups = [
        [{"host": "reddit.com", "times_cited": 5}],
        [{"host": "bblab.shop", "times_cited": 6}],
        [{"host": "yesstyle.com", "times_cited": 3}],
    ]
    profile = aggregate_controller_profile(groups, exclude_hosts=["https://bblab.shop/products/x"])
    assert "bblab.shop" not in profile["controllers"]
    assert profile["controllers"][0] == "reddit.com"


def test_aggregate_controller_profile_excludes_merchant_subdomains_only():
    """A subdomain of the merchant's own host is excluded, but a different brand
    whose name merely ends similarly is kept (no false eTLD match)."""
    from services.buyer_path_controller_quality import aggregate_controller_profile

    profile = aggregate_controller_profile(
        [
            [{"host": "shop.bblab.shop", "times_cited": 6}],
            [{"host": "notbblab.shop", "times_cited": 5}],
            [{"host": "reddit.com", "times_cited": 3}],
        ],
        exclude_hosts=["bblab.shop"],
    )
    assert "shop.bblab.shop" not in profile["controllers"]
    assert "notbblab.shop" in profile["controllers"]


def test_exposure_headline_marks_aggregate_controllers_as_sku_level_not_lane_specific():
    opportunity = {
        "top_open_lanes": [],
        "per_prompt": [
            {
                "query": "vitamin c collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "forum-owned",
                "source_route": "forum",
                "demand_signal": 1.0,
                "opportunity_score": 99.0,
                "who_owns": "reddit.com",
                "source_summary": {
                    "top_cited_hosts": [{"host": "reddit.com", "times_cited": 2}]
                },
            },
            {
                "query": "best collagen supplement",
                "axis": "category",
                "query_class": "head",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 1.0,
                "who_owns": "walmart.com",
                "source_summary": {
                    "top_cited_hosts": [{"host": "walmart.com", "times_cited": 5}]
                },
            },
        ],
    }

    headline = _sku_intelligence_headline(
        opportunity=opportunity,
        title="Ownist Triple Shine Grape",
        product_type="collagen jelly",
    )

    assert "AI shows demand for `vitamin c collagen jelly`" in headline
    assert "across tested buyer paths for this SKU" in headline
    assert "walmart.com" in headline
    assert "routes buyers to walmart.com" not in headline


def test_assemble_sku_brief_evidence_grounding_notes_use_real_evidenced_channels():
    opportunity = _opportunity()
    opportunity["per_prompt"][0]["source_roles"] = [
        {
            "host": "https://www.healthline.com/nutrition/collagen",
            "role": "publisher",
            "times_cited": 2,
        },
        {"host": "amazon.com", "role": "marketplace", "times_cited": 2},
    ]
    opportunity["per_prompt"][2]["substitution"]["source_roles"] = [
        {"host": "comparison.example", "role": "publisher", "times_cited": 2},
    ]

    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert evidence["grounding_notes"] == {
        "competitor_attributes": "not_assessed",
        "merchant_channels": "unknown",
        "evidenced_channels": [
            {"host": "amazon.com", "role": "marketplace", "times_cited": 2},
            {"host": "healthline.com", "role": "publisher", "times_cited": 2},
            {"host": "halal-beauty.example", "role": "publisher", "times_cited": 2},
            {"host": "wellness-notes.example", "role": "publisher", "times_cited": 2},
            {"host": "comparison.example", "role": "publisher", "times_cited": 2},
        ],
    }


def test_assemble_sku_brief_evidence_grounding_notes_empty_without_channels():
    opportunity = {
        "intent_ladder": {},
        "per_prompt": [
            {
                "query": "best collagen supplements for skin",
                "axis": "category",
                "query_class": "head",
                "provider_verdicts": {"gemini": "loss"},
                "ownership_state": "competitor-owned",
                "competitors": ["Vital Proteins"],
            }
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
    }

    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert evidence["grounding_notes"] == {
        "competitor_attributes": "not_assessed",
        "merchant_channels": "unknown",
        "evidenced_channels": [],
    }


def test_build_sku_brief_prompt_uses_exact_role_and_injects_evidence_json():
    evidence = _evidence()
    system, user = strategic_brief.build_sku_brief_prompt(evidence)

    assert system.startswith("You are a senior D2C brand & growth strategist")
    assert "ABSOLUTE GROUNDING RULES" in system
    assert "WRITE the brief as JSON with these fields" in system
    # W4: the closed-world entity manifest leads, then the full evidence JSON.
    assert user.startswith("LICENSED ENTITIES")
    assert "EVIDENCE:\n" in user
    assert json.loads(user.split("EVIDENCE:\n", 1)[1]) == evidence


def test_system_prompt_contains_claim_discipline_rules():
    system = strategic_brief._STRATEGIC_BRIEF_SYSTEM_PROMPT

    assert "CLAIM DISCIPLINE" in system
    assert "competitor" in system.lower()
    assert "grounding_notes.evidenced_channels" in system
    assert "MERCHANT PATH" in system
    assert "OPERATIONAL ECONOMICS" in system
    assert "AUTHORITY HONESTY" in system
    assert "product/offer/review/FAQ schema" in system
    assert "forum/community = participate in or seed accurate product info" in system
    assert "publisher/" in system and "pitch the evidenced publisher" in system
    assert "retailer/marketplace = claim or fix" in system
    assert "never promise" in system.lower()
    assert "more retrievable, extractable, citable, and authoritative" in system
    assert "EXACT wording" in system
    assert "inference" in system.lower()


def test_validate_grounding_accepts_fully_grounded_brief():
    assert strategic_brief.validate_grounding(_grounded_brief(), _evidence()) is True


def test_grounding_ignores_intraword_apostrophe_near_quoted_lane():
    """A contraction/possessive next to a grounded single-quoted lane must not
    be mistaken for a quote delimiter (which would fabricate an
    unknown-quoted-lane failure and force the deterministic fallback)."""
    evidence = _evidence()
    brief = _grounded_brief()
    brief["why_you_lose"] = (
        "The publisher's list drives the 'best collagen supplements for skin' "
        "answer, so AI doesn't surface BB Lab there yet."
    )
    failures = strategic_brief._grounding_failures(brief, evidence)
    assert not any(f.startswith("unknown-quoted-lane") for f in failures), failures
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_validate_grounding_accepts_evidenced_operational_moves():
    evidence = _evidence()
    brief = _grounded_brief()
    brief["first_moves"] = [
        (
            "For halal collagen sticks before bed, add a first-order offer "
            "and starter + replenishment bundle to the official brand PDP."
        ),
        (
            "Add a subscription incentive and why-buy-direct proof to the "
            "official brand PDP before pitching wellness-notes.example."
        ),
        "Track amazon.com and forbes.com because they are evidenced controllers.",
    ]
    brief["diy_vs_pivota"]["self_serve"] = [
        "Add the first-order offer.",
        "Publish the bundle and why-buy-direct proof.",
    ]

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_assemble_sku_brief_evidence_supports_ownist_brand_path_exposure():
    opportunity = {
        "intent_ladder": {
            "branded_transactional": {"score": 100},
            "head_category": {"score": 20},
        },
        "per_prompt": [
            {
                "query": "best beauty supplement for glow",
                "axis": "category",
                "query_class": "head",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 38.0,
                "source_roles": [
                    {"host": "walmart.com", "role": "retailer", "times_cited": 2},
                    {"host": "amazon.com", "role": "marketplace", "times_cited": 2},
                ],
            }
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
    }

    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph={
            "classes": {
                "category": ["beauty supplement"],
                "format": ["drink stick"],
                "ingredient": ["grape"],
            }
        },
        primary_gaps=[],
        scores={},
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
        },
        sku_title="Ownist Triple Shine Grape",
    )

    assert evidence["product"]["brand"] == "Ownist"
    assert evidence["product"]["merchant_path"]["goal"] == (
        "drive buyers to the brand's own website"
    )
    assert len(evidence["buyer_path_opportunities"]) == 1
    ownist_path = evidence["buyer_path_opportunities"][0]
    assert ownist_path["query"] == "best beauty supplement for glow"
    assert ownist_path["exposure"] == "retailer-owned"
    assert ownist_path["route"] == "retailer"
    assert ownist_path["controlled_by"] == [
        {"host": "amazon.com", "role": "marketplace", "times_cited": 2},
        {"host": "walmart.com", "role": "retailer", "times_cited": 2},
    ]
    assert ownist_path["destination"] == "the brand's own website"
    assert ownist_path["merchant_archetype"] == "brand"
    assert ownist_path["controller_strategy"] == "leading_retailer_competition"
    assert ownist_path["recommended_moves"] == [
        "Make the official brand PDP the more citable + buyable canonical page for this lane.",
        "Add a first-order offer without inventing a discount depth.",
        "Add a starter + replenishment bundle.",
        "Add a subscription incentive where the product supports replenishment.",
        "Add why-buy-direct proof: guarantee, samples, loyalty, returns, stock, and fresh facts.",
    ]
    assert ownist_path["lane_priority_score"] >= 0


def test_assemble_sku_brief_evidence_prioritizes_ownist_conversion_lane_over_snack_drift():
    opportunity = {
        "intent_ladder": {
            "branded_transactional": {"score": 100},
            "head_category": {"score": 20},
        },
        "per_prompt": [
            {
                "query": "healthy snacks collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 18.0,
                "attribute_basis": ["healthy snacks", "collagen", "jelly"],
                "source_roles": [
                    {"host": "cogentsteps.net", "role": "publisher", "times_cited": 2},
                    {"host": "medsysgroup.com", "role": "publisher", "times_cited": 2},
                    {"host": "hellokoop.com", "role": "retailer", "times_cited": 2},
                ],
            },
            {
                "query": "vitamin c collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 5.45,
                "attribute_basis": ["vitamin c", "collagen", "jelly"],
                "source_roles": [
                    {"host": "cogentsteps.net", "role": "publisher", "times_cited": 2},
                    {"host": "medsysgroup.com", "role": "publisher", "times_cited": 2},
                    {"host": "hellokoop.com", "role": "retailer", "times_cited": 2},
                ],
            },
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
    }

    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph={
            "classes": {
                "category": ["collagen jelly"],
                "format": ["jelly"],
                "ingredient": ["vitamin c", "collagen"],
                "use_case": ["healthy skin", "anti age"],
                "geography": ["korean"],
            }
        },
        primary_gaps=[],
        scores={},
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
        },
        sku_title="Ownist Triple Shine Grape",
    )

    opportunities = evidence["buyer_path_opportunities"]
    assert opportunities[0]["query"] == "vitamin c collagen jelly"
    assert opportunities[0]["lane_priority_score"] > opportunities[1]["lane_priority_score"]
    # The lead opportunity's controller archetype is aggregated across the SKU's
    # lanes (not anchored to one noisy hero prompt). Publishers dominate this
    # SKU's controllers, so the stable lead archetype is source_authority_gap
    # (still the authority playbook) and the named controllers lead with the
    # authority hosts.
    assert opportunities[0]["controlled_by"] == [
        {"host": "cogentsteps.net", "role": "publisher", "times_cited": 2},
        {"host": "medsysgroup.com", "role": "publisher", "times_cited": 2},
        {"host": "hellokoop.com", "role": "retailer", "times_cited": 2},
    ]
    assert opportunities[0]["controller_strategy"] == "source_authority_gap"
    assert "rank for the exact lane vitamin c collagen jelly" in opportunities[0]["recommended_moves"][0]
    assert "product/offer/review/FAQ schema" in opportunities[0]["recommended_moves"][1]
    assert "vitamin c, collagen, and jelly in plain page text" in opportunities[0]["recommended_moves"][2]
    assert "verified review and proof signals" in opportunities[0]["recommended_moves"][3]
    assert "Pitch cogentsteps.net, medsysgroup.com, and hellokoop.com" in opportunities[0]["recommended_moves"][4]
    assert "consistent across" in opportunities[0]["recommended_moves"][5]
    assert "Re-audit vitamin c collagen jelly" in opportunities[0]["recommended_moves"][6]
    assert "material buyer traffic" in opportunities[0]["recommended_moves"][6]
    assert "After the page is more retrievable" in opportunities[0]["recommended_moves"][7]
    assert len(opportunities[0]["controlled_by"]) == 3
    wedge = evidence["sideways_wedge"]
    assert wedge["recommended_beachhead_lane"]["query"] == "vitamin c collagen jelly"
    assert wedge["sideways_wedge_lanes"][0]["query"] == "vitamin c collagen jelly"
    assert "healthy snacks collagen jelly" in {
        item["query"] for item in wedge["do_not_chase_yet"]
    }
    assert "Start with \"vitamin c collagen jelly\"" in (
        wedge["why_this_lane_not_the_head_prompt"]
    )
    assert wedge["canonical_page_play"]["lane"] == "vitamin c collagen jelly"
    # W4: the deterministic-brief rendering assertions were removed with the dead
    # renderer; this test's live subject is the evidence/wedge prioritization above
    # (the conversion lane wins over the snack-drift lane).


def test_aggregate_lead_profile_recomputes_recommended_moves_with_aggregate_controllers():
    opportunity = {
        "intent_ladder": {},
        "per_prompt": [
            {
                "query": "vitamin c collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "forum-owned",
                "source_route": "forum",
                "demand_signal": 1.0,
                "opportunity_score": 99.0,
                "attribute_basis": ["vitamin c", "collagen", "jelly"],
                "who_owns": "reddit.com",
                "source_summary": {
                    "top_cited_hosts": [{"host": "reddit.com", "times_cited": 2}]
                },
            },
            {
                "query": "best collagen supplement",
                "axis": "category",
                "query_class": "head",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 1.0,
                "who_owns": "walmart.com",
                "source_summary": {
                    "top_cited_hosts": [{"host": "walmart.com", "times_cited": 5}]
                },
            },
            {
                "query": "top collagen brands",
                "axis": "category",
                "query_class": "head",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "demand_signal": 1.0,
                "opportunity_score": 1.0,
                "who_owns": "walmart.com",
                "source_summary": {
                    "top_cited_hosts": [{"host": "walmart.com", "times_cited": 5}]
                },
            },
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
    }
    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph={
            "classes": {
                "category": ["collagen jelly"],
                "ingredient": ["vitamin c", "collagen"],
                "format": ["jelly"],
            }
        },
        primary_gaps=[],
        scores={},
        identity={
            "name": "Ownist Triple Shine Grape",
            "anchors": {"brand": "Ownist"},
            "merchant_type": "brand",
        },
        sku_title="Ownist Triple Shine Grape",
    )

    lead = evidence["buyer_path_opportunities"][0]
    moves_blob = " ".join(lead["recommended_moves"]).lower()

    assert lead["query"] == "vitamin c collagen jelly"
    assert lead["controller_strategy"] == "leading_retailer_competition"
    assert lead["controlled_by"][0]["host"] == "walmart.com"
    assert "first-order offer" in moves_blob
    assert "why-buy-direct proof" in moves_blob
    assert "reddit.com discussion" not in moves_blob


def test_controller_source_route_action_splits_forum_and_unclassified_sources():
    profile = {
        "classified_controllers": [
            {"host": "reddit.com", "input_role": "forum", "type": "forum"},
            {"host": "moodarabia.com", "input_role": "unclassified", "type": "unclassified"},
        ]
    }

    action = strategic_brief._controller_source_route_action(
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


def test_validation_fix_accepts_grounded_bb_lab_brief_without_false_positives():
    brief = _validation_fix_grounded_brief()
    evidence = _validation_fix_evidence()

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


@pytest.mark.parametrize(
    "line",
    [
        "When AI (Gemini) suggests Vital Proteins as an alternative, publish a comparison.",
        "Reframe to the only halal-certified, low-molecular collagen stick designed for bedtime skin repair.",
        "Answer the question 'What is the best halal collagen stick for bedtime?' with schema.",
        "Stop competing on generic 'best collagen for skin' terms.",
        "Do NOT chase Healthline, and you cannot DIY this at scale.",
        "Publish 'BB Lab vs Vital Proteins: Why Halal Matters for Bedtime Collagen'.",
        "Publish a blog post or guide titled 'Why a Halal Bedtime Collagen Stick Works Better' and link it to the product page.",
        "Put the exact phrase in the URL, H1, and meta description.",
        "Create a page titled 'The Halal Bedtime Collagen Stick Difference'.",
        "BB Lab vs Vital Proteins: Why Halal Matters for Bedtime Collagen",
    ],
)
def test_validation_fix_accepts_prose_robust_grounded_lines(line):
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = line

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_validation_fix_still_rejects_hallucinated_entities():
    evidence = _validation_fix_evidence()
    hallucinated = {
        "position": "You are a challenger.",
        "core_decision": "x",
        "why_you_lose": (
            "AI hands buyers to Moonjuice and CollagenKing, ranked by "
            "VogueBeauty.com."
        ),
        "your_angle": "x",
        "traffic_strategy": [{"where": "x", "who_controls": "x", "how": "x"}],
        "substitution_play": None,
        "first_moves": ["x"],
        "diy_vs_pivota": {"self_serve": ["x"], "pivota": "x"},
    }

    failures = strategic_brief._grounding_failures(hallucinated, evidence)

    assert strategic_brief.validate_grounding(hallucinated, evidence) is False
    assert any("Moonjuice" in failure for failure in failures)
    assert any("CollagenKing" in failure for failure in failures)
    assert any("VogueBeauty" in failure for failure in failures)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Pitch Halal Girl Boss and Muslim Mamas.", "Halal Girl Boss"),
        ("Share it on Pinterest for visual search.", "Pinterest"),
        ("Seed r/SkincareAddiction.", "SkincareAddiction"),
        ("Seed r/HalalBeauty.", "HalalBeauty"),
    ],
)
def test_validation_fix_rejects_invented_channels_and_publications(line, expected):
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = line

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any(expected in failure for failure in failures)
    if "Halal Girl Boss" in line:
        assert any("Muslim Mamas" in failure for failure in failures)
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_fix_rejects_lane_quotes_with_ungrounded_recombinations():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = 'Own "collagen gummies for men" next.'

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any("gummies" in failure for failure in failures)
    assert strategic_brief.validate_grounding(brief, evidence) is False


@pytest.mark.parametrize(
    ("line", "invented_brand"),
    [
        (
            "You lose to Vital Proteins and Moonjuice in the category.",
            "Moonjuice",
        ),
        (
            "AI ranks Vital Proteins and NeoCell and FakeBrandX here.",
            "FakeBrandX",
        ),
        (
            "Healthline cites Vital Proteins and Glowtox over you.",
            "Glowtox",
        ),
    ],
)
def test_validation_fix_rejects_conjoined_hallucinated_entities(line, invented_brand):
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = line

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert f"unknown-entity:{invented_brand}" in failures
    assert "unknown-entity:Vital Proteins" not in failures
    assert "unknown-entity:NeoCell" not in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_fix_rejects_title_case_invented_brand_in_common_word_title():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = "Why Moonjuice Collagen Works Better"

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "unknown-entity:Moonjuice" in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_fix_allows_cited_source_names_but_rejects_unknown_sources():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = (
        "Vital Proteins and NeoCell win because Healthline ranks them, Amazon "
        "reviews support them, and Reddit controls the forum conversation."
    )

    assert strategic_brief._grounding_failures(brief, evidence) == []

    brief["why_you_lose"] = (
        "Vital Proteins win because Healthline ranks them and Forbes repeats "
        "the answer."
    )

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any("Forbes" in failure for failure in failures)


def test_competitor_attributes_default_placeholder_is_byte_identical():
    base = strategic_brief.assemble_sku_brief_evidence(
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )
    explicit = strategic_brief.assemble_sku_brief_evidence(
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
        competitor_attributes="not_assessed",
    )

    assert json.dumps(base, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert base["grounding_notes"]["competitor_attributes"] == "not_assessed"


def test_validation_allows_grounded_competitor_presence_attribute():
    evidence = _validation_fix_evidence_with_competitor_attributes()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = (
        "Vital Proteins is known for collagen peptides, while BB Lab can "
        "differentiate around the halal bedtime stick."
    )

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_validation_rejects_competitor_lack_claim_even_when_merchant_has_attribute():
    evidence = _validation_fix_evidence_with_competitor_attributes()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "Vital Proteins lacks halal collagen positioning."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "competitor-lack-claim" in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_rejects_standalone_market_exclusivity_claim():
    """"You are the only one with <attr>" is a disguised competitor-deficiency
    claim (it asserts everyone else lacks it) and must fail even when no
    competitor is named — while merchant-self "only your page offers X" passes."""
    evidence = _validation_fix_evidence_with_competitor_attributes()

    deficiency = _validation_fix_grounded_brief()
    deficiency["your_angle"] = "You are the only brand with halal collagen."
    failures = strategic_brief._grounding_failures(deficiency, evidence)
    assert "competitor-exclusive-claim" in failures
    assert strategic_brief.validate_grounding(deficiency, evidence) is False

    merchant_self = _validation_fix_grounded_brief()
    merchant_self["your_angle"] = (
        "Only your official page can offer the guarantee and fresh facts for halal."
    )
    assert strategic_brief.validate_grounding(merchant_self, evidence) is True


def test_validation_rejects_invented_competitor_attribute():
    evidence = _validation_fix_evidence_with_competitor_attributes()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "Vital Proteins is known for keto collagen."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "ungrounded-competitor-attribute:keto" in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_rejects_unassessed_competitor_attribute_claim():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "Vital Proteins is known for marine collagen."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "unassessed-competitor-attribute:marine" in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_allows_unassessed_broad_category_positioning_inference():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = (
        "Incumbents are generally positioned as broad collagen supplement; "
        "a dedicated halal bedtime stick looks like an opening worth confirming."
    )

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_competitor_attributes_do_not_loosen_safety_sensitive_terms():
    evidence = _validation_fix_evidence_with_competitor_attributes(["clinical collagen"])
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "Vital Proteins is known for clinical collagen."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "safety-sensitive:clinical" in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_safety_term_in_product_title_is_grounded():
    # A product literally named "...Treatment" (a cosmetic term) must not have
    # its whole brief rejected for echoing its own name. Regression: the anuko
    # "Damaged Hair Treatment" SKUs got brief_status "unavailable" because
    # "treatment" was in the title but not the safety allow-list.
    evidence = _validation_fix_evidence()
    evidence["product"]["title"] = "BB Lab Low Molecular Collagen Treatment"
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = "Make your own Collagen Treatment page the buyable canonical source."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert not any(f.startswith("safety-sensitive") for f in failures), failures


def test_safety_term_only_in_competitor_attrs_stays_rejected():
    # The title fix must NOT loosen safety terms that come only from a
    # competitor (a competitor being a "treatment" doesn't let the brief call
    # THIS product a treatment).
    evidence = _validation_fix_evidence_with_competitor_attributes(["collagen treatment"])
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "Vital Proteins is known for collagen treatment."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert "safety-sensitive:treatment" in failures


@pytest.mark.parametrize(
    "claim",
    [
        "Report the position as 72/100.",
        "Set the page at $29.99 to beat the substitutes.",
        "Offer 40% off to win the lane.",
        "Claim 50,000 reviews against Vital Proteins.",
    ],
)
def test_validation_fix_rejects_fabricated_numeric_claims(claim):
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = claim

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any(failure.startswith("forbidden:") for failure in failures)
    assert strategic_brief.validate_grounding(brief, evidence) is False


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("Position it as collagen for kids.", "safety-sensitive:kids"),
        ("Target collagen for diabetics.", "safety-sensitive:diabetics"),
        (
            "Market the best collagen for pregnant women.",
            "safety-sensitive:pregnant",
        ),
    ],
)
def test_validation_fix_rejects_ungrounded_health_sensitive_claims(claim, expected):
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = claim

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert expected in failures
    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validation_fix_allows_legitimate_operational_numbers():
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = (
        "Make these 3 first moves in 30 days and keep each check under "
        "60 seconds."
    )

    assert strategic_brief._grounding_failures(brief, evidence) == []
    assert strategic_brief.validate_grounding(brief, evidence) is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda brief: brief.update({"why_you_lose": "Garden of Life owns this lane."}),
        lambda brief: brief.update({"why_you_lose": "healthline.com ranks the winners."}),
        lambda brief: brief.update({"core_decision": "Use the opportunity score to choose the lane."}),
        lambda brief: brief.update({"core_decision": 'Own "vegan collagen gummies for skin" next.'}),
    ],
)
def test_validate_grounding_rejects_unknown_entities_hosts_lanes_and_jargon(mutate):
    brief = _grounded_brief()
    mutate(brief)

    assert strategic_brief.validate_grounding(brief, _evidence()) is False


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_returns_grounded_mocked_brief(monkeypatch):
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_provider", "deepseek")
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_model", "deepseek-chat")

    async def fake_synthesize(**kwargs):
        return {
            "text": json.dumps(_grounded_brief()),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert dbg["outcome"] == "llm"
    assert brief == _grounded_brief()


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_every_draft_ungrounded_fails_honestly(monkeypatch):
    # W4: when every draft fails grounding, there is NO deterministic template —
    # the brief is withheld (None) so the section is honestly absent, not a
    # generic template pretending to be bespoke analysis.
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")
    calls: List[Mapping[str, Any]] = []
    ungrounded = _grounded_brief()
    ungrounded["why_you_lose"] = "Garden of Life owns this category."

    async def fake_synthesize(**kwargs):
        calls.append(kwargs)
        return {
            "text": json.dumps(ungrounded),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert brief is None
    assert dbg["outcome"] == "unavailable_after_rejects"
    assert len(calls) == strategic_brief._STRATEGIC_BRIEF_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_synthesis_error_fails_honestly(monkeypatch):
    # W4: a provider error withholds the brief (None), no template fallback.
    from services.llm_synthesis import LLMSynthesisError

    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    async def fake_synthesize(**kwargs):
        raise LLMSynthesisError("missing", provider=kwargs["provider"])

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert brief is None
    assert dbg["outcome"] == "unavailable_llm_error"


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_retries_transient_error_then_returns_llm(monkeypatch):
    """A single transient provider blip must NOT drop the merchant to the
    deterministic brief — it retries and lands the LLM ("mainline") brief."""
    from services.llm_synthesis import LLMSynthesisHTTPError

    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(strategic_brief.asyncio, "sleep", _no_sleep)

    calls: List[Mapping[str, Any]] = []

    async def fake_synthesize(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # transport failure (no status code) -> transient/retryable
            raise LLMSynthesisHTTPError("deepseek synthesis transport failure",
                                        provider=kwargs["provider"])
        return {
            "text": json.dumps(_grounded_brief()),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert brief is not None
    assert dbg["outcome"] == "llm"
    assert len(calls) == 2  # retried past the transient failure


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_does_not_retry_fatal_http_error(monkeypatch):
    """A non-retryable provider error (e.g. HTTP 400) withholds the brief
    immediately (W4: honest absence, no template) rather than burning retries."""
    from services.llm_synthesis import LLMSynthesisHTTPError

    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    calls: List[Mapping[str, Any]] = []

    async def fake_synthesize(**kwargs):
        calls.append(kwargs)
        raise LLMSynthesisHTTPError("bad request", provider=kwargs["provider"], status_code=400)

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert brief is None
    assert dbg["outcome"] == "unavailable_llm_error"
    assert len(calls) == 1  # 400 is fatal — no retry


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_repairs_grounding_then_returns_llm(monkeypatch):
    """A grounding-rejected first draft is retried WITH a repair hint; the
    corrected draft lands the LLM brief instead of falling back."""
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    ungrounded = _grounded_brief()
    ungrounded["why_you_lose"] = "Garden of Life owns this category."  # ungrounded competitor
    calls: List[Mapping[str, Any]] = []

    async def fake_synthesize(**kwargs):
        calls.append(kwargs)
        payload = ungrounded if len(calls) == 1 else _grounded_brief()
        return {
            "text": json.dumps(payload),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert dbg["outcome"] == "llm"
    assert len(calls) == 2
    # the retry carried a repair instruction the first call did not
    assert "grounding rules" in calls[1]["user"]
    assert "grounding rules" not in calls[0]["user"]


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_repairs_formulaic_opener(monkeypatch):
    """A grounded draft that still uses the banned "Stop trying to win …" opener
    is retried with a style-repair hint — the merchant never sees the template."""
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    formulaic = _grounded_brief()
    formulaic["core_decision"] = (
        "Stop trying to win the broad collagen category; instead own the halal "
        "collagen sticks before bed lane."
    )
    assert strategic_brief._style_failures(formulaic) == [
        "style-formulaic-opener",
        "style-jargon",
    ]
    calls: List[Mapping[str, Any]] = []

    async def fake_synthesize(**kwargs):
        calls.append(kwargs)
        payload = formulaic if len(calls) == 1 else _grounded_brief()
        return {
            "text": json.dumps(payload),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    dbg: Dict[str, Any] = {}
    brief = await strategic_brief.generate_sku_strategic_brief(_evidence(), debug=dbg)

    assert dbg["outcome"] == "llm"
    assert len(calls) == 2
    assert "Stop trying to win" not in json.dumps(brief)
    # the retry was told to drop the template opener
    assert "template" in calls[1]["user"].lower()


@pytest.mark.asyncio
async def test_attach_sku_strategic_brief_preserves_deterministic_fields_on_success(monkeypatch):
    async def fake_generate(evidence, **kwargs):
        assert evidence["product"]["brand"] == "BB Lab"
        return _grounded_brief()

    monkeypatch.setattr(strategic_brief, "generate_sku_strategic_brief", fake_generate)
    nba = {
        "primary_gap": "open_lane_capture",
        "headline": "Own the answer.",
        "first_move": "Add the lane.",
    }

    attached = await attach_sku_strategic_brief(
        nba,
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert attached["primary_gap"] == nba["primary_gap"]
    assert attached["headline"] == nba["headline"]
    assert attached["first_move"] == nba["first_move"]
    assert attached["strategic_brief"] == _grounded_brief()
    assert attached["brief_status"] == "ok"
    assert "strategic_brief" not in nba
    assert "brief_status" not in nba


@pytest.mark.asyncio
async def test_attach_sku_strategic_brief_leaves_nba_unchanged_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", False)

    async def fake_generate(evidence, **kwargs):
        return None

    monkeypatch.setattr(strategic_brief, "generate_sku_strategic_brief", fake_generate)
    nba = {
        "primary_gap": "open_lane_capture",
        "headline": "Own the answer.",
        "first_move": "Add the lane.",
    }

    attached = await attach_sku_strategic_brief(
        nba,
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    # Feature off: the brief is an opt-in enrichment, so a missing brief is not
    # a diagnosable failure — leave the deterministic NBA untouched.
    assert attached == nba
    assert "strategic_brief" not in attached
    assert "brief_status" not in attached


@pytest.mark.asyncio
async def test_attach_sku_strategic_brief_marks_unavailable_when_enabled_and_no_brief(monkeypatch):
    # The silent-failure regression: feature ON, but no brief produced (e.g.
    # grounding rejected every attempt). It must surface a diagnosable status
    # instead of silently dropping to generic NBA boilerplate.
    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)

    async def fake_generate(evidence, **kwargs):
        return None

    monkeypatch.setattr(strategic_brief, "generate_sku_strategic_brief", fake_generate)
    nba = {
        "primary_gap": "open_lane_capture",
        "headline": "Own the answer.",
        "first_move": "Add the lane.",
    }

    attached = await attach_sku_strategic_brief(
        nba,
        opportunity=_opportunity(),
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )

    assert attached["primary_gap"] == nba["primary_gap"]
    assert attached["brief_status"] == "unavailable"
    assert "strategic_brief" not in attached


def _low_signal_evidence() -> Dict[str, Any]:
    """A not-yet-visible SKU: real product facts, but no probes/citations/lanes
    so buyer_path_opportunities comes out empty."""
    return strategic_brief.assemble_sku_brief_evidence(
        opportunity={
            "intent_ladder": {},
            "per_prompt": [],
            "top_open_lanes": [],
            "substitution_alert": {"present": False},
            "demand_state_summary": "no AI answer exposure detected",
        },
        attribute_graph=_attribute_graph(),
        primary_gaps=[],
        scores={},
        identity=_identity(),
        sku_title="BB Lab Good Night Collagen",
    )


def test_validate_grounding_rejects_overwide_controller_lists():
    evidence = _evidence()
    evidence["buyer_path_opportunities"] = [
        {
            "query": "halal collagen sticks",
            "controlled_by": [
                {"host": "moodarabia.com", "role": "publisher"},
                {"host": "beautyandthebrows.co", "role": "publisher"},
                {"host": "sayweee.com", "role": "retailer"},
                {"host": "krunbeauty.re", "role": "retailer"},
                {"host": "halalgoods.co", "role": "retailer"},
                {"host": "souqbeauty.ae", "role": "retailer"},
            ],
            "recommended_moves": ["Add a first-order offer."],
        }
    ]
    brief = _grounded_brief()
    # A genuine laundry-list (6 domains in one field). Naming the few top
    # controllers (<=5) is allowed; this is over the line.
    brief["traffic_strategy"] = [
        (
            "Own halal collagen sticks against moodarabia.com, "
            "beautyandthebrows.co, sayweee.com, krunbeauty.re, halalgoods.co, "
            "and souqbeauty.ae."
        )
    ]

    assert strategic_brief.validate_grounding(brief, evidence) is False


def test_validate_grounding_rejects_unsupported_multiplier_claims():
    brief = _grounded_brief()
    brief["traffic_strategy"] = [
        "Do not chase generic collagen queries because incumbents have 10x the authority."
    ]

    assert strategic_brief.validate_grounding(brief, _evidence()) is False


def test_merchant_own_host_is_groundable():
    # A useful brief must be able to name the merchant's own canonical site
    # ("make bblab.com the buyable page"); it must not be rejected as an unknown
    # domain. Regression: the brand host was missing from the allow-list, so
    # every LLM brief naming it failed and fell back to the generic template.
    evidence = _validation_fix_evidence()
    evidence["product"]["merchant_host"] = "bblab.com"
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = "Make bblab.com the buyable canonical page for the branded lane."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert not any(f.startswith("unknown-domain") for f in failures), failures


def test_unrelated_domain_still_rejected():
    # The own-host fix must not open the door to arbitrary domains.
    evidence = _validation_fix_evidence()
    evidence["product"]["merchant_host"] = "bblab.com"
    brief = _validation_fix_grounded_brief()
    brief["core_decision"] = "Sell it on randomshop123.com instead."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any(f.startswith("unknown-domain") for f in failures)


def test_cited_source_spelled_name_is_grounded():
    # The cited retailer's human name ("Amazon") must be allowed when its domain
    # (amazon.com, via category_battle.ranked_by) is cited. Regression: "Olive
    # Young" was rejected though oliveyoung.com was allowed, blocking the brief.
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "AI cites Amazon for these lanes before your own page."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert not any(f.startswith("unknown-entity:Amazon") for f in failures), failures


def test_generic_commerce_and_aeo_constructs_are_grounded():
    # The brief RECOMMENDS generic constructs (Starter Kit, Subscribe & Save,
    # About Us page, Organization schema per schema.org). These are not
    # fabricated brands and must not trip the unknown-entity/-domain guards.
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["first_moves"] = [
        "Create a Starter Kit and a Subscribe & Save offer to reward direct buyers.",
        "Add an About Us page plus Product, Review, and Organization structured data per schema.org.",
    ]

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert not any(f.startswith("unknown-entity") for f in failures), failures
    assert not any(f.startswith("unknown-domain") for f in failures), failures


def test_invented_competitor_still_rejected():
    # The vocabulary loosening must not let a fabricated brand through.
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = "AI cites NeoGlow Laboratories as the category leader."

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert any(f.startswith("unknown-entity") for f in failures), failures


def test_cited_source_reputation_and_prose_words_are_grounded():
    # Describing the cited source as "credible/established" (grounded inference
    # about why it out-cites you) and capitalizing plain prose ("Legitimacy",
    # "After") must not trip the guards. Regression: these flakily blocked the
    # LLM brief on 2 of 3 SKUs, forcing the deterministic fallback.
    evidence = _validation_fix_evidence()
    brief = _validation_fix_grounded_brief()
    brief["why_you_lose"] = (
        "AI cites Amazon because it is a credible, established retailer. "
        "Legitimacy matters here. After the click, the buyer lands there."
    )

    failures = strategic_brief._grounding_failures(brief, evidence)

    assert not any(f.startswith("unassessed-competitor-attribute") for f in failures), failures
    assert not any(f.startswith("unknown-entity") for f in failures), failures


def test_category_answers_mine_verbatim_ai_answers_and_license_entities():
    """#1: the brief evidence surfaces the AI's VERBATIM category answers — the
    winning products, the sources, the angle — and licenses those entities for
    grounding so the brief may name them (instead of generic 'build a PDP')."""
    opportunity = {
        "intent_ladder": {},
        "per_prompt": [
            {
                "query": "best hair oil",
                "axis": "category",
                "query_class": "head",
                "provider_verdicts": {"gemini": "loss", "chatgpt": "loss"},
                "ownership_state": "competitor-owned",
                "demand_signal": 1.0,
                "competitors": ["K18", "Olaplex", "Moroccanoil"],
                "cited_evidence": {
                    "provider": "chatgpt",
                    "excerpt": ("Top hair oils per Vogue and Allure feature K18 "
                                "Molecular Repair Hair Oil and Olaplex, framed "
                                "around bond repair and disulfide bonds."),
                    "cited_hosts": ["vogue.com", "allure.com"],
                    "competitors_named": ["K18 Molecular Repair Hair Oil",
                                          "Olaplex", "Moroccanoil"],
                },
            },
        ],
        "top_open_lanes": [],
    }
    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph={},
        identity={"name": "Anuko Bond & Repair Hair Oil",
                  "anchors": {"brand": "Anuko", "category": "hair oil"}},
        sku_title="Anuko Bond & Repair Hair Oil",
        merchant_host="anukoofficial.com",
    )
    answers = evidence["category_answers"]
    assert answers and answers[0]["query"] == "best hair oil"
    assert "bond repair" in answers[0]["ai_answer"].lower()
    assert "vogue.com" in answers[0]["cited_sources"]
    assert any("K18" in r for r in answers[0]["recommends"])
    # Mined source + product are licensed for grounding.
    allowed = strategic_brief._allowed_grounding(evidence)
    assert "vogue.com" in allowed["domains"]
    assert "allure.com" in allowed["domains"]
    # A brief naming the mined winners/sources is grounded; invented ones are not.
    assert strategic_brief.validate_grounding(
        {"why_you_lose": "Vogue and Allure rank K18 and Olaplex."}, evidence) is True
    assert strategic_brief.validate_grounding(
        {"why_you_lose": "Brandzilla dominates via fakeshop.com."}, evidence) is False


def test_neutralize_numeric_claims_strips_forbidden_stats():
    """#3: free-text evidence often quotes stats ("90%", "$24", "1,200 reviews")
    the brief is forbidden to repeat. Neutralise them to words so a faithful
    draft doesn't trip the validator."""
    f = strategic_brief._neutralize_numeric_claims
    assert "%" not in f("over 90% of ingredients are moisture essences")
    assert "$" not in f("priced at $24.99 per bottle")
    assert "x" not in f("3x more repair").lower().replace("several-fold", "")
    out = f("backed by 1,200 reviews")
    assert "1,200" not in out and "reviews" in out
    # Plain prose is untouched.
    assert f("known for deep hydration") == "known for deep hydration"


def test_competitor_attributes_note_strips_percentages_before_brief():
    """The competitor 'known for' verbatim (e.g. '90% moisture essences') must
    not carry a percentage into the brief evidence — that was tripping
    forbidden:% and forcing the deterministic fallback."""
    note = strategic_brief._competitor_attributes_note({
        "status": "assessed",
        "competitor": "&Honey",
        "attributes_present": ["organic"],
        "evidence": [{
            "attribute": "organic",
            "provider": "gemini",
            "verbatim": "&Honey is over 90% moisture essences, $24 a bottle.",
        }],
    })
    assert note != "not_assessed"
    blob = json.dumps(note)
    assert "%" not in blob and "$" not in blob


def test_validate_grounding_allows_up_to_five_controllers_per_field():
    """#3b: a channel-plan field that names a few (<=5) evidenced controllers is
    legitimate and must pass — only a 6+ laundry-list is rejected. This is the
    tolerance that lets a faithful LLM brief ship instead of the generic
    deterministic fallback."""
    evidence = _evidence()
    evidence["buyer_path_opportunities"] = [
        {
            "query": "halal collagen sticks",
            "controlled_by": [
                {"host": "moodarabia.com", "role": "publisher"},
                {"host": "beautyandthebrows.co", "role": "publisher"},
                {"host": "sayweee.com", "role": "retailer"},
                {"host": "krunbeauty.re", "role": "retailer"},
            ],
            "recommended_moves": ["Add a first-order offer."],
        }
    ]
    brief = _grounded_brief()
    brief["traffic_strategy"] = [
        (
            "Own halal collagen sticks against moodarabia.com, "
            "beautyandthebrows.co, sayweee.com, and krunbeauty.re."
        )
    ]
    assert strategic_brief.validate_grounding(brief, evidence) is True


def test_own_product_facts_license_brand_angle_and_ingredients():
    """#3c: the merchant's OWN product facts (ingredients/angle) are licensed so
    the brief can name them without unknown-entity / unknown-quoted-lane
    rejections on the brand's own copy."""
    opportunity = {
        "intent_ladder": {},
        "per_prompt": [],
        "top_open_lanes": [],
        "product_evidence": {
            "explicit_text_phrases": [
                "bond technology that repairs disulfide bonds",
                "shea butter and green tea for damaged hair",
            ],
            "phrases": ["clinically shown to strengthen hair"],
        },
    }
    evidence = strategic_brief.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph={},
        identity={"name": "Anuko Bond & Repair Hair Oil",
                  "anchors": {"brand": "Anuko", "category": "hair oil"}},
        sku_title="Anuko Bond & Repair Hair Oil",
        merchant_host="anukoofficial.com",
    )
    assert any("disulfide" in f for f in evidence["own_product_facts"])
    allowed = strategic_brief._allowed_grounding(evidence)
    assert "disulfide" in allowed["attribute_words"]
    # The brand naming its OWN angle is grounded (not an unknown entity/lane).
    assert strategic_brief.validate_grounding(
        {"your_angle": "Your wedge is bond technology that repairs disulfide bonds."},
        evidence,
    ) is True


# ---------------------------------------------------------------------------
# Brief-reliability fixes (2026-07-03): cited-host display names + provider chain


def _grounding_evidence():
    return {
        "product": {
            "title": "Anuko Hair Oil", "brand": "Anuko",
            "merchant_host": "anukoofficial.com",
            "attributes": {"ingredient": ["argan oil"], "category": ["hair oil"]},
        },
        "category_answers": [{
            "recommends": ["Olaplex", "Moroccanoil"],
            "cited_sources": ["whowhatwear.com", "reddit.com", "sephora.com",
                              "nbcnews.com", "hwahae.com"],
        }],
    }


def test_cited_host_display_names_do_not_trip_unknown_entity():
    """A brief that names a REAL cited source in its natural display form must
    pass — the significant-words collapse dropped common words so 'Who What
    Wear' (whowhatwear.com) / 'NBC News' (nbcnews.com) / possessive 'Reddit's'
    were wrongly rejected, forcing the deterministic fallback."""
    ev = _grounding_evidence()
    allowed = strategic_brief._allowed_grounding(ev)
    for name in ["Who What Wear", "NBC News", "Reddit's", "Olaplex", "Sephora"]:
        assert strategic_brief._entity_allowed(name, allowed), name


def test_full_prose_brief_naming_real_hosts_passes():
    ev = _grounding_evidence()
    brief = {
        "why_you_lose": "AI cites Who What Wear and NBC News, which rank Olaplex and Moroccanoil.",
        "channel_plan": "Pitch Who What Wear; seed Reddit's community with your argan oil facts.",
    }
    assert strategic_brief._grounding_failures(brief, ev) == []


def test_fabricated_entities_still_rejected():
    """The recognition fix must NOT loosen the anti-fabrication guarantee: a
    brand/host NOT in the evidence is still an unknown-entity failure."""
    ev = _grounding_evidence()
    for bad in [
        {"why_you_lose": "Karethic and Aunt Jackie's dominate this category."},
        {"the_call": "Pitch Cosmoprof for retail placement."},
        {"the_call": "Beat FakeBrand Labs by owning the argan oil lane."},
    ]:
        failures = strategic_brief._grounding_failures(bad, ev)
        assert any(f.startswith("unknown-entity") for f in failures), bad


def test_resolve_brief_provider_defaults_gemini_then_falls_back(monkeypatch):
    import services.strategic_brief as sb
    from config.settings import settings

    monkeypatch.setattr(settings, "strategic_brief_provider", "gemini", raising=False)

    # Selection now gates on provider_available() (credential-aware, incl. Vertex
    # ADC for gemini), so drive that rather than the raw configured-key layer.

    # gemini available -> gemini
    monkeypatch.setattr(sb, "provider_available", lambda p: True, raising=False)
    assert sb._resolve_brief_provider(None) == "gemini"

    # gemini unavailable -> deepseek fallback
    monkeypatch.setattr(
        sb, "provider_available",
        lambda p: p != "gemini", raising=False,
    )
    assert sb._resolve_brief_provider(None) == "deepseek"

    # nothing available -> None (deterministic fallback path)
    monkeypatch.setattr(sb, "provider_available", lambda p: False, raising=False)
    assert sb._resolve_brief_provider(None) is None

    # explicit provider override wins
    monkeypatch.setattr(sb, "provider_available", lambda p: True, raising=False)
    assert sb._resolve_brief_provider("anthropic") == "anthropic"


# ---- W4: closed-world entity manifest --------------------------------------

def test_licensed_entity_manifest_lists_evidence_entities_only():
    evidence = _evidence()
    manifest = strategic_brief._licensed_entity_manifest(evidence)
    # real competitors from the evidence are licensed
    assert "Vital Proteins" in manifest["competitors"]
    # the merchant's own brand is licensed
    assert any("BB Lab" in m for m in manifest["merchant"])
    # a brand NOT in the evidence is not licensed (fabrication guard)
    assert "Garden of Life" not in manifest["competitors"]
    # sources are hosts present in evidence
    assert all("." in s for s in manifest["sources"])


def test_rendered_manifest_leads_the_prompt_and_names_the_rules():
    _system, user = strategic_brief.build_sku_brief_prompt(_evidence())
    assert user.startswith("LICENSED ENTITIES")
    assert "you may name ONLY the proper nouns below" in user
    assert "Vital Proteins" in user.split("EVIDENCE:\n", 1)[0]  # in the manifest, not just the JSON


def test_manifest_handles_empty_evidence_without_crashing():
    manifest = strategic_brief._licensed_entity_manifest({})
    assert manifest == {"competitors": [], "sources": [], "lanes": [], "merchant": []}
    rendered = strategic_brief._render_entity_manifest(manifest)
    assert "(none in evidence)" in rendered
