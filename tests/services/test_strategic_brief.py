from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

import pytest

from services import strategic_brief
from services.next_best_action import attach_sku_strategic_brief


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
                    {"host": "forbes.com", "role": "publisher"},
                    {"host": "amazon.com", "role": "marketplace"},
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
                    {"host": "wellness-notes.example", "role": "publisher"},
                    {"host": "halal-beauty.example", "role": "publisher"},
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
            "Stop chasing the broad collagen category first; claim halal "
            "collagen sticks before bed because that lane fits BB Lab."
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
                "how": "Do not chase this lane first because the named winners already hold it.",
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


def test_assemble_sku_brief_evidence_is_traceable_to_audit_inputs():
    evidence = _evidence()

    assert evidence["product"]["title"] == "BB Lab Good Night Collagen"
    assert evidence["product"]["brand"] == "BB Lab"
    assert evidence["product"]["attributes"]["certification"] == ["halal"]
    assert evidence["product"]["attributes"]["format"] == ["stick"]
    assert evidence["position"] == {
        "strong_when_named": 100,
        "weak_in_category": 0,
        "branded_consideration": 0,
    }
    assert evidence["category_battle"]["prompts"] == ["best collagen supplements for skin"]
    assert evidence["category_battle"]["winners"] == [
        "Vital Proteins",
        "NeoCell",
        "Sports Research",
    ]
    assert {"host": "forbes.com", "role": "publisher"} in evidence["category_battle"]["ranked_by"]
    assert {"host": "amazon.com", "role": "marketplace"} in evidence["category_battle"]["ranked_by"]
    assert evidence["substitution"]["on_prompt"] == "bb lab collagen alternatives"
    assert evidence["substitution"]["handed_to"] == "Vital Proteins"
    assert evidence["open_lanes"][0] == {
        "query": "halal collagen sticks before bed",
        "why_fit": ["halal", "collagen", "stick", "before bed"],
        "who_controls": "none/fragmented",
        "channel_role": "open",
    }
    assert evidence["channel_map"][0]["controlled_by"] == [
        {"host": "wellness-notes.example", "role": "publisher"},
        {"host": "halal-beauty.example", "role": "publisher"},
    ]


def test_build_sku_brief_prompt_uses_exact_role_and_injects_evidence_json():
    evidence = _evidence()
    system, user = strategic_brief.build_sku_brief_prompt(evidence)

    assert system.startswith("You are a senior D2C brand & growth strategist")
    assert "ABSOLUTE GROUNDING RULES" in system
    assert "WRITE the brief as JSON with these fields" in system
    assert user.startswith("EVIDENCE:\n")
    assert json.loads(user.split("EVIDENCE:\n", 1)[1]) == evidence


def test_validate_grounding_accepts_fully_grounded_brief():
    assert strategic_brief.validate_grounding(_grounded_brief(), _evidence()) is True


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

    brief = await strategic_brief.generate_sku_strategic_brief(_evidence())

    assert brief == _grounded_brief()


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_retries_then_returns_none_for_ungrounded(monkeypatch):
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

    assert await strategic_brief.generate_sku_strategic_brief(_evidence()) is None
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_generate_sku_strategic_brief_returns_none_on_synthesis_error(monkeypatch):
    from services.llm_synthesis import LLMSynthesisError

    monkeypatch.setattr(strategic_brief.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(strategic_brief.settings, "deepseek_api_key", "sk-test")

    async def fake_synthesize(**kwargs):
        raise LLMSynthesisError("missing", provider=kwargs["provider"])

    monkeypatch.setattr(strategic_brief, "synthesize", fake_synthesize)

    assert await strategic_brief.generate_sku_strategic_brief(_evidence()) is None


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
    assert "strategic_brief" not in nba


@pytest.mark.asyncio
async def test_attach_sku_strategic_brief_leaves_nba_unchanged_on_failure(monkeypatch):
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

    assert attached == nba
    assert "strategic_brief" not in attached
