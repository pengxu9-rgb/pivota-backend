from __future__ import annotations

from typing import Any, Dict, List

import pytest

import services.agent_center_bd_report_service as bd


def _hero_product(*, attributes: bool = True) -> Dict[str, Any]:
    product: Dict[str, Any] = {
        "title": "BB Lab Good Night Collagen",
        "raw_title": "BB LAB Good Night Collagen (Halal), 2g x 30 sticks",
        "pdp_url": "https://bblab.shop/products/good-night-collagen",
        "vendor": "BB Lab",
        "product_type": "collagen supplement",
    }
    if attributes:
        product["attributes_raw"] = {
            "tags": ["halal", "collagen", "k-beauty", "stick"],
            "body_html": (
                "<p>Halal low molecular fish collagen sticks with vitamin C "
                "and glycine. No water needed before bed.</p>"
            ),
            "description": (
                "Halal low molecular fish collagen sticks with vitamin C and "
                "glycine. No water needed before bed."
            ),
            "variants": [{"title": "2g x 30 sticks"}],
        }
    return product


def _four_money_shot_records(sku_ctx: Dict[str, Any], prompts_per_sku: int) -> List[Dict[str, Any]]:
    del sku_ctx, prompts_per_sku
    return [
        {"query": "where can I buy BB Lab Good Night Collagen", "axis": "intent"},
        {"query": "best collagen supplements for skin", "axis": "category"},
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "attribute_basis": ["halal", "collagen", "stick", "before bed"],
            "evidence": ["tag: halal", "body: before bed"],
            "intent_weight": 1.0,
        },
        {"query": "BB Lab collagen alternatives", "axis": "comparison"},
    ]


def _empty_state_records(sku_ctx: Dict[str, Any], prompts_per_sku: int) -> List[Dict[str, Any]]:
    del sku_ctx, prompts_per_sku
    return [
        {"query": "where can I buy BB Lab Good Night Collagen", "axis": "intent"},
        {"query": "best collagen supplements for skin", "axis": "category"},
        {"query": "BB Lab collagen alternatives", "axis": "comparison"},
    ]


def _sources(kind: str) -> List[Dict[str, str]]:
    if kind == "merchant":
        return [
            {
                "uri": "https://bblab.shop/products/good-night-collagen",
                "title": "BB Lab official PDP",
            }
        ]
    if kind == "head":
        return [
            {"uri": "https://forbes.com/best-collagen", "title": "Best collagen supplements"},
            {"uri": "https://amazon.com/vital-proteins", "title": "Vital Proteins"},
            {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
        ]
    if kind == "sidewalk":
        return [
            {"uri": "https://wellness-notes.example/collagen-before-bed", "title": "Collagen before bed"},
            {"uri": "https://halal-beauty.example/collagen-sticks", "title": "Halal collagen sticks"},
        ]
    return [
        {"uri": "https://amazon.com/vital-proteins", "title": "Vital Proteins"},
        {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
    ]


def _fake_run(query: str, provider: str) -> Dict[str, Any]:
    if "where can i buy" in query.lower():
        return {
            "query": query,
            "provider": provider,
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "grounding_sources": _sources("merchant"),
            "grounding_chunks": [src["uri"] for src in _sources("merchant")],
            "raw": "BB Lab Good Night Collagen is available from BB Lab.",
            "url_match": {
                "in_grounding": True,
                "llm_self_report": {
                    "product_visible": True,
                    "correct_sku": True,
                    "sku_mentioned": True,
                },
            },
        }
    if "alternatives" in query.lower():
        competitors = ["Vital Proteins", "Vital Beautie", "NeoCell"]
        return {
            "query": query,
            "provider": provider,
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
                "competitors_appearing": competitors,
            },
            "grounding_sources": _sources("alternatives"),
            "grounding_chunks": [src["uri"] for src in _sources("alternatives")],
            "raw": "For BB Lab alternatives, the answer recommends Vital Proteins and NeoCell.",
        }
    if "halal collagen sticks" in query.lower():
        return {
            "query": query,
            "provider": provider,
            "parsed": {"product_visible": False, "correct_sku": False},
            "grounding_sources": _sources("sidewalk"),
            "grounding_chunks": [src["uri"] for src in _sources("sidewalk")],
            "raw": "Small sources discuss halal collagen sticks before bed, with no clear owner.",
        }
    competitors = ["Vital Proteins", "Vital Beautie", "NeoCell", "Sports Research", "HUM"]
    return {
        "query": query,
        "provider": provider,
        "parsed": {
            "product_visible": False,
            "correct_sku": False,
            "competitors_listed": competitors,
            "competitors_appearing": competitors,
        },
        "grounding_sources": _sources("head"),
        "grounding_chunks": [src["uri"] for src in _sources("head")],
        "raw": "Vital Proteins, Vital Beautie, NeoCell, Sports Research, and HUM are recommended.",
    }


def _install_probe(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    async def fake_probe(**kwargs):
        calls.append(kwargs)
        queries = list((kwargs.get("context") or {}).get("queries") or [])
        return {
            "scan_mode": kwargs["scan_mode"],
            "provider": kwargs["provider"],
            "model": kwargs.get("model"),
            "model_is_override": kwargs.get("model_is_override"),
            "raw_runs": [_fake_run(query, kwargs["provider"]) for query in queries],
            "scores": {"visibility_score": 0},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(bd.llm_client, "probe", fake_probe)
    return calls


@pytest.mark.asyncio
async def test_run_wedge_hero_sku_intelligence_builds_money_shot(monkeypatch):
    monkeypatch.setattr(bd, "_build_per_sku_audit_query_records", _four_money_shot_records)
    calls = _install_probe(monkeypatch)

    out = await bd.run_wedge_hero_sku_intelligence(
        hero_product=_hero_product(),
        merchant_id="merch-A",
        run_id="run-url-1",
        coverage_profile="gemini_deepseek",
        prompts_per_sku=4,
    )

    assert len(calls) == 2
    assert {call["provider"] for call in calls} == {"gemini", "deepseek"}
    assert all(call["merchant_id"] == "merch-A" for call in calls)
    assert out["is_empty"] is False
    assert out["hero_sku"] == {
        "title": "BB Lab Good Night Collagen",
        "pdp_url": "https://bblab.shop/products/good-night-collagen",
        "vendor": "BB Lab",
    }
    assert out["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
    assert out["top_open_lanes"][0]["first_move"] == "Add a PDP section + FAQ for this lane"
    assert out["next_best_action"]["primary_gap"] == "open_lane_capture"
    assert out["next_best_action"]["first_move"] == "Add a PDP section + FAQ for this lane"
    assert len(out["next_best_action"]["self_serve_actions"]) == 2
    assert out["next_best_action"]["pivota_path"]
    assert "You lost `best collagen supplements for skin`" in out["headline"]
    assert "`halal collagen sticks before bed`" in out["headline"]
    assert out["substitution_alert"]["present"] is True
    assert out["substitution_alert"]["substituted_by"] == "Vital Proteins"
    assert len(out["prompt_matrix"]) == 4
    assert {row["query"] for row in out["prompt_matrix"]} == {
        "where can I buy BB Lab Good Night Collagen",
        "best collagen supplements for skin",
        "halal collagen sticks before bed",
        "BB Lab collagen alternatives",
    }
    sidewalk = next(
        row for row in out["prompt_matrix"]
        if row["query"] == "halal collagen sticks before bed"
    )
    assert sidewalk["intent_ladder_layer"] == "sidewalk_opportunity"
    assert sidewalk["ownership_state"] == "open-lane"
    assert out["coverage"]["prompt_count"] == 4


@pytest.mark.asyncio
async def test_run_wedge_hero_sku_intelligence_tri_prober_includes_chatgpt(monkeypatch):
    # The wedge hero SKU uses the gemini_deepseek_chatgpt profile: ChatGPT runs
    # as a co-equal grounded prober and shows up as a third matrix column.
    monkeypatch.setattr(bd, "_build_per_sku_audit_query_records", _four_money_shot_records)
    calls = _install_probe(monkeypatch)

    out = await bd.run_wedge_hero_sku_intelligence(
        hero_product=_hero_product(),
        merchant_id="merch-A",
        run_id="run-url-1",
        coverage_profile="gemini_deepseek_chatgpt",
        prompts_per_sku=4,
    )

    assert {call["provider"] for call in calls} == {"gemini", "deepseek", "chatgpt"}
    assert out["is_empty"] is False
    assert all("chatgpt" in row for row in out["prompt_matrix"])
    branded = next(
        row for row in out["prompt_matrix"]
        if "where can i buy" in row["query"].lower()
    )
    assert branded["chatgpt"] == "win"


@pytest.mark.asyncio
async def test_run_wedge_hero_sku_intelligence_mock_upstream_is_honest(monkeypatch):
    # Per-SKU honesty parity with the brand-report mock guard: if the upstream
    # returns fallback/mock data, do NOT fabricate a money-shot on synthetic
    # runs — return the honest empty-state with a clear note.
    monkeypatch.setattr(bd, "_build_per_sku_audit_query_records", _four_money_shot_records)

    async def fake_mock_probe(**kwargs):
        queries = list((kwargs.get("context") or {}).get("queries") or [])
        return {
            "scan_mode": kwargs["scan_mode"],
            "provider": "mock_fallback_no_gemini_key",
            "raw_runs": [_fake_run(query, "mock_fallback_no_gemini_key") for query in queries],
            "scores": {"visibility_score": 0},
            "usage": {},
        }

    monkeypatch.setattr(bd.llm_client, "probe", fake_mock_probe)

    out = await bd.run_wedge_hero_sku_intelligence(
        hero_product=_hero_product(),
        merchant_id="merch-A",
        run_id="run-url-1",
        coverage_profile="gemini_deepseek",
        prompts_per_sku=4,
    )

    assert out["is_empty"] is True
    assert out["top_open_lanes"] == []
    assert out["next_best_action"]["primary_gap"] == "insufficient_data"
    assert "fallback data" in out.get("note", "")
    # The synthetic runs must NOT produce a money-shot.
    assert "You lost" not in out["headline"]
    assert "nobody owns" not in out["headline"]


@pytest.mark.asyncio
async def test_run_wedge_hero_sku_intelligence_empty_state_without_attributes(monkeypatch):
    monkeypatch.setattr(bd, "_build_per_sku_audit_query_records", _empty_state_records)
    _install_probe(monkeypatch)

    out = await bd.run_wedge_hero_sku_intelligence(
        hero_product=_hero_product(attributes=False),
        merchant_id="merch-A",
        run_id="run-url-1",
        coverage_profile="gemini_deepseek",
        prompts_per_sku=3,
    )

    assert out["is_empty"] is True
    assert out["top_open_lanes"] == []
    assert "next_best_action" in out
    assert "We tested 3 buyer prompts for BB Lab Good Night Collagen" in out["headline"]
    assert "No open lane stood out this run" in out["headline"]
    assert out["intent_ladder"]
    assert len(out["prompt_matrix"]) == 3
    assert out["substitution_alert"]["present"] is True
