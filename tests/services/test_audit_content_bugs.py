"""Two merchant-facing content bugs from the 2026-07-03 post-fix review of the
ANUKO b77a15b2 run:
  1) the 'category winner' panel showed a retailer/marketplace host (Coupang,
     Bunjang) instead of a competing brand product;
  2) the per-engine note read 'You surface on chatgpt, gemini but not chatgpt,
     gemini' when each engine surfaced the product on DIFFERENT queries.
"""
from __future__ import annotations

from services.agent_center_bd_report_service import (
    _competitor_is_brandlike,
    _durable_competitor_for_brief,
    build_engine_playbook,
)


def _disc_row(query, gemini, chatgpt):
    return {
        "axis": "category",
        "query": query,
        "normalized_query": query,
        "provider_verdicts": {"gemini": gemini, "chatgpt": chatgpt},
    }


# ---- Bug 2: per-engine divergence note -----------------------------------

def test_mixed_divergence_never_self_contradicts():
    # gemini wins A / chatgpt wins B -> aggregate won==lost=={both}. Old code
    # said "surface on chatgpt, gemini but not chatgpt, gemini".
    ep = build_engine_playbook(per_prompt=[
        _disc_row("argan oil hair oil", "win", "loss"),
        _disc_row("bonding technology hair repair oil", "loss", "win"),
    ])
    note = ep["divergence_note"]
    assert note
    assert "but not chatgpt, gemini" not in note.lower()
    assert "but not gemini, chatgpt" not in note.lower()
    assert "each surface you on different category queries" in note
    assert "argan oil hair oil" in note


def test_clean_divergence_names_direction_with_capitalized_labels():
    # gemini consistently ahead on both divergent queries.
    ep = build_engine_playbook(per_prompt=[
        _disc_row("argan oil hair oil", "win", "loss"),
        _disc_row("yuzu seed oil hair oil", "win", "loss"),
    ])
    note = ep["divergence_note"]
    assert note == (
        'You surface on Gemini but not ChatGPT for category queries like '
        '"argan oil hair oil" — closing the ChatGPT gap is the per-engine priority.'
    )
    # never the raw lowercase key
    assert "chatgpt" not in note


def test_no_divergence_leaves_note_empty():
    ep = build_engine_playbook(per_prompt=[
        _disc_row("argan oil hair oil", "win", "win"),
        _disc_row("yuzu seed oil hair oil", "loss", "loss"),
    ])
    assert ep["divergence_note"] is None


# ---- Bug 1: category-winner must be a competing brand, not a store --------

def test_brandlike_guard_rejects_stores_and_gray_market():
    assert _competitor_is_brandlike("Olaplex")
    assert _competitor_is_brandlike("Moroccanoil")
    assert not _competitor_is_brandlike("Coupang")          # retailer name token
    assert not _competitor_is_brandlike("Bunjang Global")   # gray-market token
    assert not _competitor_is_brandlike("Olive Young")      # classify_host retailer
    assert not _competitor_is_brandlike("Amazon")           # classify_host marketplace
    assert not _competitor_is_brandlike("")


def test_durable_competitor_picks_brand_over_repeated_host_owner():
    # A retailer dominates as repeated_owner across prompts, but the real
    # competing brand is Olaplex. The winner must be the brand.
    per_prompt = [
        {
            "competitors": ["Olaplex", "Moroccanoil"],
            "density": {"features": {"repeated_owner": "Coupang"}},
        },
        {
            "competitors": ["Olaplex"],
            "density": {"features": {"repeated_owner": "Coupang"}},
        },
        {
            "competitors": ["Bunjang Global"],  # a store leaked into competitors
            "density": {"features": {"repeated_owner": "Bunjang Global"}},
        },
    ]
    winner = _durable_competitor_for_brief({"per_prompt": per_prompt})
    assert winner == "Olaplex"


def test_durable_competitor_returns_none_when_only_stores():
    per_prompt = [
        {"competitors": ["Coupang"], "density": {"features": {"repeated_owner": "Coupang"}}},
        {"competitors": ["Bunjang Global"], "density": {"features": {"repeated_owner": "Bunjang Global"}}},
    ]
    assert _durable_competitor_for_brief({"per_prompt": per_prompt}) is None
