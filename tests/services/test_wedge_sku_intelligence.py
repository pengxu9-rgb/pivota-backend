from __future__ import annotations

import json

from typing import Any, Dict, List

import pytest

import services.agent_center_bd_report_service as bd
from services.sku_lane_priority import build_lane_product_evidence


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
    assert "halal collagen sticks before bed" in out["next_best_action"]["first_move"]
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
    head = next(
        row for row in out["prompt_matrix"]
        if row["query"] == "best collagen supplements for skin"
    )
    assert head["ownership_state"] != "merchant-owned"
    assert head["buyer_path_action"]["prescription_class"] == "operational_efficiency"
    assert head["buyer_path_action"]["lane"] == "best collagen supplements for skin"
    assert "direct buyer path against amazon.com" in head["buyer_path_action"]["move"]
    assert "first-order offer" in head["buyer_path_action"]["move"]
    assert "starter + replenishment bundle" in head["buyer_path_action"]["move"]
    play = head["buyer_path_action"]["canonical_page_play"]
    assert play["lane"] == "best collagen supplements for skin"
    assert play["controller_strategy"] == "leading_retailer_competition"
    assert {move["type"] for move in play["moves"]} == {
        "first_order_offer",
        "starter_replenishment_bundle",
        "subscription_or_why_buy_direct",
    }
    play_blob = json.dumps(play).lower()
    assert "after it is source-ready" in play_blob
    assert "first-order offer" in play_blob
    assert "starter + replenishment bundle" in play_blob
    assert "exposure_read" in play_blob
    assert "credible retail controllers" in play_blob
    assert "exact discount depths" in play["economics_policy"]
    assert "agent-checkout ready" in play["checkout_readiness"]
    assert out["coverage"]["prompt_count"] == 4


@pytest.mark.asyncio
async def test_run_wedge_hero_sku_intelligence_attaches_optional_strategic_brief(monkeypatch):
    monkeypatch.setattr(bd, "_build_per_sku_audit_query_records", _four_money_shot_records)
    _install_probe(monkeypatch)
    attach_calls: List[Dict[str, Any]] = []

    async def fake_attach(next_best_action: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        attach_calls.append(kwargs)
        assert next_best_action["primary_gap"] == "open_lane_capture"
        out = dict(next_best_action)
        out["strategic_brief"] = {"position": "grounded"}
        return out

    monkeypatch.setattr(bd, "attach_sku_strategic_brief", fake_attach)

    out = await bd.run_wedge_hero_sku_intelligence(
        hero_product=_hero_product(),
        merchant_id="merch-A",
        run_id="run-url-1",
        coverage_profile="gemini_deepseek",
        prompts_per_sku=4,
    )

    assert out["next_best_action"]["primary_gap"] == "open_lane_capture"
    assert out["next_best_action"]["strategic_brief"] == {"position": "grounded"}
    assert len(attach_calls) == 1
    assert attach_calls[0]["opportunity"]["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
    assert attach_calls[0]["attribute_graph"]["classes"]["certification_constraint"] == ["halal"]


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
    # returns mock data, do NOT fabricate a money-shot on synthetic runs and do
    # not expose fallback language as merchant-facing copy.
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
    assert out["quality_gate"] == {
        "shareable": False,
        "reason": "live_sku_probe_not_real",
        "merchant_copy_allowed": False,
    }
    copy = " ".join(
        [
            out.get("headline", ""),
            out.get("note", ""),
            out["next_best_action"].get("headline", ""),
            out["next_best_action"].get("why_this_first", ""),
            out["next_best_action"].get("first_move", ""),
        ]
    ).lower()
    assert "live ai evidence" in out.get("note", "").lower()
    assert not any(
        bad in copy
        for bad in (
            "fallback",
            "try again",
            "couldn't",
            "re-run",
            "rerun",
            "not enough signal",
        )
    )
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

    assert out["is_empty"] is False
    assert out["top_open_lanes"] == []
    assert "next_best_action" in out
    # The fixture's category lane is third-party (amazon.com) controlled with
    # real demand, so the headline must LEAD with the buyer-path exposure, not
    # the stale "no open lane stood out" frame.
    assert "routes buyers to" in out["headline"]
    assert "buyer path" in out["headline"].lower()
    assert "amazon.com" in out["headline"]
    assert out["intent_ladder"]
    assert len(out["prompt_matrix"]) == 3
    assert out["substitution_alert"]["present"] is True


def test_sku_intelligence_headline_uses_merchant_fit_lane_priority_for_ownist_exposure():
    product = {
        "title": "Ownist Triple Shine Grape",
        "raw_title": "Ownist Triple Shine Grape Collagen Jelly",
        "vendor": "Ownist",
        "product_type": "beauty supplement",
        "canonical_url": "https://ownist.com/products/triple-shine-grape",
    }
    product_evidence = build_lane_product_evidence(
        product=product,
        attribute_graph={
            "classes": {
                "category": ["collagen jelly"],
                "format": ["jelly"],
                "ingredient": ["vitamin c", "collagen"],
                "use_case": ["healthy skin", "anti age"],
                "geography": ["korean"],
            }
        },
        sku_title="Ownist Triple Shine Grape",
    )
    opportunity = {
        "per_prompt": [
            {
                "query": "healthy snacks collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 18.0,
                "demand_signal": 1.0,
                "attribute_basis": ["healthy snacks", "collagen", "jelly"],
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "cogentsteps.net", "times_cited": 2},
                        {"host": "medsysgroup.com", "times_cited": 1},
                    ]
                },
            },
            {
                "query": "vitamin c collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 5.45,
                "demand_signal": 1.0,
                "attribute_basis": ["vitamin c", "collagen", "jelly"],
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "cogentsteps.net", "times_cited": 2},
                        {"host": "medsysgroup.com", "times_cited": 1},
                        {"host": "hellokoop.com", "times_cited": 1},
                    ]
                },
            },
            {
                "query": "healthy skin collagen jelly",
                "axis": "sidewalk",
                "query_class": "sidewalk",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 13.63,
                "demand_signal": 1.0,
                "attribute_basis": ["healthy skin", "collagen", "jelly"],
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "ubuy.mq", "times_cited": 1},
                        {"host": "truehuebeauty.com", "times_cited": 1},
                    ]
                },
            },
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": "third-party exposure",
        "intent_ladder": {},
        "confidence": {"prompt_count": 3, "prompts_with_demand": 3},
        "product_evidence": product_evidence,
    }

    out = bd._display_sku_intelligence(
        sku_ctx={"sku_key": "ownist", "product": product},
        opportunity=opportunity,
    )

    assert out["is_empty"] is False
    assert "vitamin c collagen jelly" in out["headline"]
    assert "healthy snacks collagen jelly" not in out["headline"]
    assert out["next_best_action"]["evidence_used"]["source_route_prompt"]["query"] == (
        "vitamin c collagen jelly"
    )
    assert out["prompt_matrix"][0]["query"] == "vitamin c collagen jelly"
    action = out["prompt_matrix"][0]["buyer_path_action"]
    assert action["controller_strategy"] == "canonical_source_vacuum"
    assert "weak citation trail" in action["move"]
    assert "structured product data" in action["move"]
    assert "first-order offer" not in action["move"]
    assert "not proof that material buyer traffic" in json.dumps(action).lower()
    assert "beat cogentsteps" not in action["move"].lower()


def test_sku_intelligence_ownist_live_like_sparse_product_prefers_vitamin_c_lane():
    product = {
        "title": "Triple Shine Grape",
        "raw_title": "Triple Shine Grape",
        "vendor": "Ownist",
        "brand": "Ownist",
        "product_type": "Belight grape jelly",
        "category": "Belight grape jelly",
        "canonical_url": "https://ownist.com/products/triple-shine-1-box",
        "attributes_raw": {},
    }
    product_evidence = build_lane_product_evidence(
        product=product,
        sku_ctx={"product": product},
        attribute_graph=bd.build_sku_attribute_graph(product),
        sku_title="Triple Shine Grape",
    )
    base = {
        "axis": "sidewalk",
        "query_class": "sidewalk",
        "demand_signal": 1.0,
    }
    opportunity = {
        "per_prompt": [
            {
                **base,
                "query": "healthy snacks collagen jelly",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 13.87,
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "cogentsteps.net", "times_cited": 1},
                        {"host": "medsysgroup.com", "times_cited": 1},
                        {"host": "hellokoop.com", "times_cited": 1},
                    ]
                },
            },
            {
                **base,
                "query": "anti age collagen jelly",
                "ownership_state": "publisher-owned",
                "source_route": "publisher",
                "opportunity_score": 13.68,
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "ubuy.mq", "times_cited": 1},
                        {"host": "genomicsworkshop.isr.umich.edu", "times_cited": 1},
                        {"host": "shop.tiktok.com", "times_cited": 1},
                    ]
                },
            },
            {
                **base,
                "query": "healthy skin collagen jelly",
                "ownership_state": "publisher-owned",
                "source_route": "publisher",
                "opportunity_score": 13.63,
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "ubuy.mq", "times_cited": 1},
                        {"host": "truehuebeauty.com", "times_cited": 1},
                        {"host": "dodoskin.com", "times_cited": 1},
                    ]
                },
            },
            {
                **base,
                "query": "korean collagen jelly",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 12.74,
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "cogentsteps.net", "times_cited": 1},
                        {"host": "medsysgroup.com", "times_cited": 1},
                        {"host": "dodoskin.com", "times_cited": 1},
                    ]
                },
            },
            {
                **base,
                "query": "vitamin c collagen jelly",
                "ownership_state": "retailer-owned",
                "source_route": "retailer",
                "opportunity_score": 5.45,
                "source_summary": {
                    "top_cited_hosts": [
                        {"host": "cogentsteps.net", "times_cited": 1},
                        {"host": "medsysgroup.com", "times_cited": 1},
                        {"host": "oliveyoung.com", "times_cited": 1},
                    ]
                },
            },
        ],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": "third-party exposure",
        "intent_ladder": {},
        "confidence": {"prompt_count": 5, "prompts_with_demand": 5},
        "product_evidence": product_evidence,
    }

    out = bd._display_sku_intelligence(
        sku_ctx={"sku_key": "ownist", "product": product},
        opportunity=opportunity,
    )
    buyer_path = bd.summarize_sku_buyer_path(out)

    assert out["is_empty"] is False
    assert "vitamin c collagen jelly" in out["headline"]
    assert "healthy snacks collagen jelly" not in out["headline"]
    assert out["prompt_matrix"][0]["query"] == "vitamin c collagen jelly"
    assert out["prompt_matrix"][0]["fit_penalties"] == []
    snack = next(row for row in out["prompt_matrix"] if row["query"] == "healthy snacks collagen jelly")
    assert "lifestyle_drift:healthy snacks" in snack["fit_penalties"]
    assert out["next_best_action"]["evidence_used"]["source_route_prompt"]["query"] == (
        "vitamin c collagen jelly"
    )
    wedge = out["sideways_wedge"]
    assert wedge["recommended_beachhead_lane"]["query"] == "vitamin c collagen jelly"
    assert wedge["sideways_wedge_lanes"][0]["query"] == "vitamin c collagen jelly"
    assert "healthy snacks collagen jelly" in {
        item["query"] for item in wedge["do_not_chase_yet"]
    }
    assert "Start with \"vitamin c collagen jelly\"" in (
        wedge["why_this_lane_not_the_head_prompt"]
    )
    assert wedge["canonical_page_play"]["lane"] == "vitamin c collagen jelly"
    assert "agent-checkout readiness" in wedge["canonical_page_play"]["pivota_path"]
    nba_wedge = out["next_best_action"]["sideways_wedge"]
    assert nba_wedge["recommended_beachhead_lane"]["query"] == "vitamin c collagen jelly"
    assert "Start with \"vitamin c collagen jelly\"" in out["next_best_action"]["why_this_first"]
    assert buyer_path["primary_lane"] == "vitamin c collagen jelly"
    assert buyer_path["state"] == "fragmented_source_trail"
    assert buyer_path["top_controllers"] == []
