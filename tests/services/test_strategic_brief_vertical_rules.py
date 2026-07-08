"""Phase 1b — vertical-aware strategic-brief rules + leakage guard.

The Phase-0 byte-golden CANNOT see brief-level leakage (the brief is LLM output).
This file is that guard: it asserts, deterministically at the prompt layer, that

  * the BEAUTY brief prompt is byte-identical to the incumbent (no regression),
  * the ELECTRONICS brief prompt swaps in specs/claims + audio publishers and
    carries NO beauty ingredient/INCI rule and NO beauty publisher names,

so a future beauty edit can't silently reintroduce cosmetics rules into the
electronics brief. A mocked-LLM snapshot ties the end-to-end generate path to the
electronics prompt.
"""
import json

import pytest

import services.strategic_brief as sb
from services.vertical_profiles import BEAUTY_PROFILE, get_profile

# Beauty ingredient/claims language that must NEVER appear in an electronics brief prompt.
_BEAUTY_LEAK_TOKENS = [
    "INCI",
    "shea butter",
    "argan oil",
    "name ingredients in plain consumer terms",
    "Camellia Sinensis",
    "Vogue",
    "Allure",
    "Cosmopolitan",
    "Good Housekeeping",
]
# Electronics rules that MUST appear.
_ELECTRONICS_TOKENS = ["SPECS", "IP68", "bone conduction", "15-hour battery"]
_ELECTRONICS_PUBLISHERS = ["Wirecutter", "Rtings", "SoundGuys", "What Hi-Fi"]
# Vertical-neutral rules that must survive in BOTH prompts.
_NEUTRAL_TOKENS = [
    "ABSOLUTE GROUNDING RULES",
    "CLAIM DISCIPLINE",
    "WRITE the brief as JSON",
    "pitch the evidenced publisher",
    "AUTHORITY HONESTY",
]


def test_beauty_brief_prompt_is_byte_identical_to_incumbent():
    # brief_rules=None -> incumbent prompt returned verbatim.
    assert sb._render_system_prompt(BEAUTY_PROFILE) == sb._STRATEGIC_BRIEF_SYSTEM_PROMPT
    # build_sku_brief_prompt: beauty and absent-vertical both keep the incumbent.
    for evidence in ({"vertical": "beauty", "product": {"title": "x"}},
                     {"product": {"title": "x"}},
                     {"vertical": "other", "product": {"title": "x"}}):
        system, _ = sb.build_sku_brief_prompt(evidence)
        assert system == sb._STRATEGIC_BRIEF_SYSTEM_PROMPT


def test_electronics_brief_prompt_swaps_rules_with_no_beauty_leakage():
    system = sb._render_system_prompt(get_profile("electronics"))
    for tok in _ELECTRONICS_TOKENS + _ELECTRONICS_PUBLISHERS:
        assert tok in system, f"electronics prompt missing {tok!r}"
    for tok in _BEAUTY_LEAK_TOKENS:
        assert tok not in system, f"electronics prompt LEAKS beauty {tok!r}"
    for tok in _NEUTRAL_TOKENS:
        assert tok in system, f"electronics prompt dropped neutral rule {tok!r}"


def test_incumbent_prompt_still_carries_beauty_rules():
    # The beauty guard direction: the incumbent must still instruct the INCI rule
    # + beauty publisher list (so beauty briefs are unchanged).
    system = sb._STRATEGIC_BRIEF_SYSTEM_PROMPT
    for tok in ["INCI", "shea butter", "Vogue", "Allure"]:
        assert tok in system


def test_build_prompt_selects_only_electronics():
    beauty_sys, _ = sb.build_sku_brief_prompt({"vertical": "beauty", "product": {"title": "x"}})
    elec_sys, _ = sb.build_sku_brief_prompt({"vertical": "electronics", "product": {"title": "x"}})
    assert beauty_sys == sb._STRATEGIC_BRIEF_SYSTEM_PROMPT
    assert "IP68" in elec_sys and "INCI" not in elec_sys


# --------------------------- assemble integration --------------------------- #

def _electronics_assemble_inputs():
    identity = {
        "name": "Mojawa Purra Swim Bone Conduction Earphones",
        "anchors": {"brand": "Mojawa", "category": "electronics/audio/earphones"},
    }
    attribute_graph = {
        "classes": {
            "category": ["bone conduction earphones"],
            "format": ["earphones"],
            "ingredient": [],
            "certification_constraint": [],
            "audience": [],
            "use_case": ["swimming"],
            "proof": [],
            "exclusion": [],
        }
    }
    return {"per_prompt": [], "top_open_lanes": []}, attribute_graph, identity


def test_assemble_resolves_electronics_vertical_and_health_sensitive_false():
    opportunity, attribute_graph, identity = _electronics_assemble_inputs()
    evidence = sb.assemble_sku_brief_evidence(
        opportunity=opportunity,
        attribute_graph=attribute_graph,
        identity=identity,
        sku_title="Mojawa Purra Swim Bone Conduction Earphones",
    )
    assert evidence["vertical"] == "electronics"
    # health_sensitive is a metadata flag; electronics profile hard-sets it False.
    assert evidence["notes"]["health_sensitive"] is False
    # And its prompt is the electronics one.
    system, _ = sb.build_sku_brief_prompt(evidence)
    assert "IP68" in system and "INCI" not in system


def _grounded_electronics_brief():
    return {
        "position": "A niche waterproof bone-conduction earphone with real swim proof.",
        "core_decision": "Own the 'earphones for swimming' answer with your IP68 proof page.",
        "why_you_lose": "The evidenced review sites rank the broad picks, not your page yet.",
        "your_angle": "Open-ear bone conduction that survives a pool swim.",
        "traffic_strategy": "Own your product page for the swimming-earphones search first.",
        "substitution_play": None,
        "first_moves": ["Put the IP68 swim proof on your product page."],
        "diy_vs_pivota": {"self_serve": ["Add spec proof"], "pivota": "cited buyable page"},
    }


@pytest.mark.asyncio
async def test_generate_electronics_brief_sends_electronics_prompt(monkeypatch):
    monkeypatch.setattr(sb.settings, "strategic_brief_enabled", True)
    monkeypatch.setattr(sb.settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(sb.settings, "strategic_brief_provider", "deepseek")
    monkeypatch.setattr(sb.settings, "strategic_brief_model", "deepseek-chat")

    captured = {}

    async def fake_synthesize(**kwargs):
        captured["system"] = kwargs.get("system")
        return {
            "text": json.dumps(_grounded_electronics_brief()),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "provider": kwargs["provider"],
            "model": kwargs["model"],
        }

    monkeypatch.setattr(sb, "synthesize", fake_synthesize)

    evidence = {"vertical": "electronics", "product": {"title": "Mojawa Purra Swim"}}
    await sb.generate_sku_strategic_brief(evidence, debug={})

    # The LLM was handed the electronics prompt — no beauty ingredient/publisher rules.
    assert captured["system"] is not None
    assert "IP68" in captured["system"] and "Wirecutter" in captured["system"]
    for tok in _BEAUTY_LEAK_TOKENS:
        assert tok not in captured["system"]
