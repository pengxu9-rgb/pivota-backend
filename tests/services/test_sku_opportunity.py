from __future__ import annotations

from typing import Any, Dict, List

import pytest


def _bb_lab_sku_ctx() -> Dict[str, Any]:
    product = {
        "product_key": "prod-bblab",
        "merchant_id": "m-1",
        "title": "BB Lab Good Night Collagen",
        "raw_title": "BB LAB Good Night Collagen (Halal), 2g x 30 sticks",
        "brand": "BB Lab",
        "vendor": "BB Lab",
        "product_type": "collagen supplement",
        "category": "beauty",
        "description": (
            "Halal K-beauty collagen supplement in stick format. No water "
            "needed. Fish collagen with vitamin C and glycine for a bedtime "
            "beauty routine."
        ),
        "canonical_url": "https://bblab.shop/products/good-night-collagen",
        "pivota_canonical_url": "https://agent.pivota.cc/products/bblab-good-night",
        "attributes_raw": {
            "tags": ["halal", "collagen", "k-beauty", "stick"],
            "body_html": (
                "<p>Halal low molecular fish collagen sticks with vitamin C "
                "and glycine. No water needed before bed.</p>"
            ),
            "description": (
                "Halal low molecular fish collagen sticks with vitamin C and "
                "glycine. No water needed before bed."
            ),
            "variants": [
                {"title": "2g x 30 sticks", "price": "25.99", "available": True}
            ],
        },
    }
    return {
        "sku_key": "sku-bblab",
        "merchant_id": "m-1",
        "product_key": "prod-bblab",
        "content_key": "ck-bblab",
        "product": product,
        "sku": {
            "sku_key": "sku-bblab",
            "product_key": "prod-bblab",
            "title": "2g x 30 sticks",
            "sku": "BBLAB-GN-30",
            "visible_option_labels": ["30 sticks"],
            "visible_attributes": {"size": "30 sticks"},
        },
        "all_skus": [{"sku_key": "sku-bblab", "visible_option_labels": ["30 sticks"]}],
        "offers": [
            {
                "sku_key": "sku-bblab",
                "product_key": "prod-bblab",
                "merchant_id": "m-1",
                "availability": "in_stock",
                "list_price": 25.99,
                "merchant_effective_price": 25.99,
                "offer_mode": "merchant_checkout",
            }
        ],
    }


def _run(
    *,
    query: str,
    axis: str,
    provider: str,
    parsed: Dict[str, Any],
    sources: List[Dict[str, str]],
    raw: str,
    axis_metadata: Dict[str, Any] | None = None,
    in_grounding: bool = False,
) -> Dict[str, Any]:
    meta = {"axis": axis, "sku_key": "sku-bblab"}
    meta.update(axis_metadata or {})
    return {
        "query": query,
        "_provider": provider,
        "raw": raw,
        "parsed": parsed,
        "grounding_sources": sources,
        "grounding_chunks": [source["uri"] for source in sources],
        "url_match": {
            "in_grounding": in_grounding,
            "llm_self_report": {
                key: value
                for key, value in parsed.items()
                if key in {"product_visible", "correct_sku", "sku_mentioned"}
            },
        },
        "axis_metadata": meta,
    }


def _bb_lab_probe_runs() -> List[Dict[str, Any]]:
    source_sets = {
        "merchant": [{"uri": "https://bblab.shop/products/good-night-collagen", "title": "BB Lab official PDP"}],
        "head_gemini": [
            {"uri": "https://forbes.com/best-collagen", "title": "Best collagen supplements"},
            {"uri": "https://amazon.com/vital-proteins", "title": "Vital Proteins Collagen Peptides"},
            {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
        ],
        "head_deepseek": [
            {"uri": "https://byrdie.com/best-collagen", "title": "Best collagen supplements"},
            {"uri": "https://amazon.com/neocell", "title": "NeoCell collagen"},
            {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
        ],
        "sidewalk_gemini": [
            {"uri": "https://kbeauty-shop.example/halal-collagen", "title": "K-beauty collagen sticks"},
            {"uri": "https://wellness-notes.example/collagen-before-bed", "title": "Collagen before bed"},
        ],
        "sidewalk_deepseek": [
            {"uri": "https://halal-beauty.example/collagen-sticks", "title": "Halal collagen sticks"},
            {"uri": "https://innerbeauty.example/bedtime-routine", "title": "Bedtime beauty routine"},
        ],
        "alternatives": [
            {"uri": "https://amazon.com/vital-proteins", "title": "Vital Proteins"},
            {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
        ],
    }
    common_sidewalk_meta = {
        "sidewalk_attribute_basis": ["halal", "collagen", "stick"],
        "sidewalk_evidence": {
            "halal": "tag",
            "collagen": "title",
            "stick": "body",
        },
        "sidewalk_intent_weight": 1.0,
    }
    competitors = ["Vital Proteins", "Vital Beautie", "NeoCell", "Sports Research", "HUM"]
    provider_runs: Dict[str, List[Dict[str, Any]]] = {"gemini": [], "deepseek": []}
    for provider in provider_runs:
        provider_runs[provider].append(
            _run(
                query="where can I buy BB Lab Good Night Collagen",
                axis="intent",
                provider=provider,
                parsed={"product_visible": True, "correct_sku": True, "sku_mentioned": True},
                sources=source_sets["merchant"],
                raw="BB Lab Good Night Collagen is available from BB Lab.",
                in_grounding=True,
            )
        )
        provider_runs[provider].append(
            _run(
                query="best collagen supplements for skin",
                axis="category",
                provider=provider,
                parsed={
                    "product_visible": False,
                    "correct_sku": False,
                    "competitors_listed": competitors,
                    "competitors_appearing": competitors,
                },
                sources=source_sets[f"head_{provider}"],
                raw="Vital Proteins, Vital Beautie, NeoCell, Sports Research, and HUM are recommended.",
            )
        )
        provider_runs[provider].append(
            _run(
                query="halal collagen sticks before bed",
                axis="sidewalk",
                provider=provider,
                parsed={"product_visible": False, "correct_sku": False},
                sources=source_sets[f"sidewalk_{provider}"],
                raw="A few small sources discuss halal collagen sticks before bed, with no clear owner.",
                axis_metadata=common_sidewalk_meta,
            )
        )
        provider_runs[provider].append(
            _run(
                query="BB Lab collagen alternatives",
                axis="comparison",
                provider=provider,
                parsed={
                    "product_visible": False,
                    "correct_sku": False,
                    "competitors_listed": ["Vital Proteins", "Vital Beautie", "NeoCell"],
                    "competitors_appearing": ["Vital Proteins", "Vital Beautie", "NeoCell"],
                },
                sources=source_sets["alternatives"],
                raw="For BB Lab alternatives, the answer recommends Vital Proteins and NeoCell.",
            )
        )
    return [
        {"provider": provider, "probe_run_id": f"probe-{provider}", "raw_runs": runs}
        for provider, runs in provider_runs.items()
    ]


def _freshnest_sku_ctx() -> Dict[str, Any]:
    product = {
        "product_key": "prod-freshnest",
        "merchant_id": "m-freshnest",
        "title": "FreshNest Probiotic Deodorant Refill Pods",
        "brand": "FreshNest",
        "vendor": "FreshNest",
        "product_type": "deodorant",
        "category": "beauty",
        "description": "Probiotic deodorant refill pods for sensitive skin.",
        "canonical_url": "https://freshnest.example/products/probiotic-deodorant-pods",
        "attributes_raw": {
            "tags": ["deodorant", "refill pods", "sensitive skin"],
            "description": "Probiotic deodorant refill pods for sensitive skin.",
        },
    }
    return {
        "sku_key": "sku-freshnest",
        "merchant_id": "m-freshnest",
        "product_key": "prod-freshnest",
        "product": product,
        "sku": {"sku_key": "sku-freshnest", "sku": "FN-PODS"},
    }


def _by_query(opportunity: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        row["normalized_query"]: row
        for row in opportunity["per_prompt"]
    }


def test_sku_opportunity_scores_bb_lab_prompt_cases():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    opportunity = build_sku_opportunity(ctx, _bb_lab_probe_runs(), attribute_graph=graph)
    rows = _by_query(opportunity)

    branded = rows["where can i buy bb lab good night collagen"]
    assert branded["provider_verdicts"] == {"gemini": "win", "deepseek": "win"}
    assert branded["engine_agreement"] == "both"
    assert branded["ownership_state"] == "merchant-owned"
    assert branded["demand_state"] == "protected"

    head = rows["best collagen supplements for skin"]
    assert head["provider_verdicts"] == {"gemini": "loss", "deepseek": "loss"}
    assert head["ownership_state"] in {"competitor-owned", "publisher-owned", "retailer-owned"}
    assert head["density"]["band"] == "high"
    assert head["open_lane"] is False
    assert head["demand_state"] == "contested"

    sidewalk = rows["halal collagen sticks before bed"]
    assert sidewalk["ownership_state"] == "open-lane"
    assert sidewalk["density"]["band"] == "low"
    assert sidewalk["demand_state"] == "open-lane"
    assert sidewalk["attribute_basis"] == ["halal", "collagen", "stick"]
    assert sidewalk["opportunity_score"] > head["opportunity_score"]

    substitution = rows["bb lab collagen alternatives"]
    assert substitution["substitution"]["present"] is True
    assert substitution["substitution"]["substituted_by"] == "Vital Proteins"
    assert opportunity["substitution_alert"] == {
        "present": True,
        "prompt": "bb lab collagen alternatives",
        "substituted_by": "Vital Proteins",
        "engines": ["deepseek", "gemini"],
    }

    assert opportunity["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
    assert opportunity["intent_ladder"]["branded_transactional"]["score"] >= 90
    assert opportunity["intent_ladder"]["head_category"]["score"] < 50
    assert opportunity["intent_ladder"]["sidewalk_opportunity"]["score"] > 0
    assert opportunity["demand_state_summary"] == "open lane detected"


def test_substitution_requires_merchant_named_loss_but_is_axis_agnostic():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _freshnest_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    competitors = ["Native", "Lume", "Myro"]
    competitor_sources = [
        {"uri": "https://nativecos.com/deodorant", "title": "Native deodorant"},
        {"uri": "https://lumedeodorant.com/products", "title": "Lume deodorant"},
    ]
    runs = _runs_both_providers([
        {
            "query": "where can I buy FreshNest Probiotic Deodorant Refill Pods",
            "axis": "intent",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
                "competitors_appearing": competitors,
            },
            "sources": competitor_sources,
            "raw": "I could not verify FreshNest; try Native, Lume, or Myro.",
        },
        {
            "query": "refillable deodorant pods vs sticks",
            "axis": "comparison",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
                "competitors_appearing": competitors,
            },
            "sources": competitor_sources,
            "raw": "Native, Lume, and Myro are common refillable deodorant options.",
        },
        {
            "query": "alternatives to FreshNest Probiotic Deodorant Refill Pods",
            "axis": "unknown",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
                "competitors_appearing": competitors,
            },
            "sources": competitor_sources,
            "raw": "Alternatives to FreshNest include Native, Lume, and Myro.",
        },
    ])
    opportunity = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    rows = _by_query(opportunity)

    branded_buy = rows["where can i buy freshnest probiotic deodorant refill pods"]
    assert branded_buy["substitution"]["present"] is True
    assert branded_buy["substitution"]["substituted_by"] == "Native"

    unbranded_comparison = rows["refillable deodorant pods vs sticks"]
    assert unbranded_comparison["substitution"]["present"] is False

    branded_alternatives = rows["alternatives to freshnest probiotic deodorant refill pods"]
    assert branded_alternatives["substitution"]["present"] is True
    assert branded_alternatives["substitution"]["substituted_by"] == "Native"


def test_sku_opportunity_is_deterministic():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    first = build_sku_opportunity(ctx, _bb_lab_probe_runs(), attribute_graph=graph)
    second = build_sku_opportunity(ctx, _bb_lab_probe_runs(), attribute_graph=graph)

    assert first == second
    scores = [row["opportunity_score"] for row in first["per_prompt"]]
    assert scores == sorted(scores, reverse=True)


def _runs_both_providers(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """specs: list of dicts passed to _run minus `provider`; emitted for both engines."""
    provider_runs: Dict[str, List[Dict[str, Any]]] = {"gemini": [], "deepseek": []}
    for provider in provider_runs:
        for spec in specs:
            provider_runs[provider].append(_run(provider=provider, **spec))
    return [
        {"provider": provider, "probe_run_id": f"probe-{provider}", "raw_runs": runs}
        for provider, runs in provider_runs.items()
    ]


def _sidewalk_meta() -> Dict[str, Any]:
    return {
        "sidewalk_attribute_basis": ["halal", "collagen", "stick"],
        "sidewalk_evidence": {"halal": "tag", "collagen": "title", "stick": "body"},
        "sidewalk_intent_weight": 1.0,
    }


def _redirector_source(title: str, suffix: str) -> Dict[str, str]:
    return {
        "uri": f"https://vertexaisearch.cloud.google.com/grounding-api-redirect/{suffix}",
        "title": title,
    }


def test_ownership_states_by_source_role():
    # Covers the per-prompt ownership branches the BB Lab scenario does not pin:
    # retailer-/publisher-/forum-owned routing and no-demand.
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "buy collagen sticks online",
            "axis": "intent",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                {"uri": "https://walmart.com/p/collagen", "title": "Walmart"},
                {"uri": "https://sephora.com/p/collagen", "title": "Sephora"},
            ],
            "raw": "You can buy collagen sticks at Walmart and Sephora.",
        },
        {
            "query": "collagen supplement guide",
            "axis": "intent",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                {"uri": "https://byrdie.com/collagen", "title": "Byrdie"},
                {"uri": "https://forbes.com/collagen", "title": "Forbes"},
            ],
            "raw": "Editorial guides explain collagen supplements.",
        },
        {
            "query": "collagen stick opinions",
            "axis": "intent",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                {"uri": "https://reddit.com/r/skincare/abc", "title": "Reddit thread"},
            ],
            "raw": "Reddit users share collagen stick opinions.",
        },
        {
            "query": "obscure collagen trivia xyzzy",
            "axis": "intent",
            "parsed": {},
            "sources": [],
            "raw": "I don't have enough information to answer that.",
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    rows = _by_query(opp)

    assert rows["buy collagen sticks online"]["ownership_state"] == "retailer-owned"
    assert rows["collagen supplement guide"]["ownership_state"] == "publisher-owned"
    assert rows["collagen stick opinions"]["ownership_state"] == "forum-owned"
    assert rows["obscure collagen trivia xyzzy"]["ownership_state"] == "no-demand"
    assert rows["obscure collagen trivia xyzzy"]["demand_state"] == "no-demand"


def test_redirector_publisher_owned_not_open_lane():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                _redirector_source("Healthline", "healthline-a"),
                _redirector_source("Healthline collagen supplement guide", "healthline-b"),
            ],
            "raw": "Healthline discusses collagen supplements before bed.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["source_route"] == "publisher"
    assert row["ownership_state"] == "publisher-owned"
    assert row["open_lane"] is False
    assert all(lane["query"] != "halal collagen sticks before bed" for lane in opp["top_open_lanes"])


def test_redirector_retailer_owned():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                _redirector_source("Sephora", "sephora"),
                _redirector_source("Olive Young Global", "oliveyoung"),
            ],
            "raw": "Retailers like Sephora and Olive Young Global carry similar collagen sticks.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["source_route"] == "retailer"
    assert row["ownership_state"] == "retailer-owned"
    assert row["open_lane"] is False


def test_redirector_first_party():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": [_redirector_source("BB Lab official PDP", "bblab-pdp")],
            "raw": "BB Lab Good Night Collagen is cited from the BB Lab official PDP.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["provider_verdicts"] == {"gemini": "win", "deepseek": "win"}
    assert row["ownership_state"] == "merchant-owned"
    assert row["open_lane"] is False


def test_weak_merchant_mention_not_open_lane():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {},
            "sources": [
                {"uri": "https://small-shop.example/halal-collagen", "title": "Small halal collagen shop"},
                {"uri": "https://routine-notes.example/collagen-bedtime", "title": "Bedtime collagen notes"},
            ],
            "raw": "BB Lab is mentioned in a few small bedtime collagen notes, but no source owns the lane.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["source_route"] == "unclassified"
    assert row["ownership_state"] == "merchant-mentioned"
    assert row["open_lane"] is False
    assert opp["top_open_lanes"] == []


def test_real_open_lane_survives():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {"product_visible": False, "correct_sku": False},
            "sources": [
                {"uri": "https://small-shop.example/halal-collagen", "title": "Small halal collagen shop"},
                {"uri": "https://routine-notes.example/collagen-bedtime", "title": "Bedtime collagen notes"},
            ],
            "raw": "Fragmented small sources discuss halal collagen sticks before bed with no clear owner.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["source_route"] == "unclassified"
    assert row["ownership_state"] == "open-lane"
    assert row["open_lane"] is True
    assert opp["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"


def test_demand_state_summary_branded_protected_unbranded_absent():
    # branded wins + category lost to a durable competitor + NO open lanes
    # => "branded demand protected, unbranded absent" (not "open lane detected").
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "where can I buy BB Lab Good Night Collagen",
            "axis": "intent",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": [
                {"uri": "https://bblab.shop/products/good-night-collagen", "title": "BB Lab PDP"},
            ],
            "raw": "BB Lab Good Night Collagen is available from BB Lab.",
            "in_grounding": True,
        },
        {
            "query": "best collagen supplements for skin",
            "axis": "category",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": ["Vital Proteins"],
                "competitors_appearing": ["Vital Proteins"],
            },
            "sources": [
                {"uri": "https://vitalproteins.com/products/collagen-peptides", "title": "Vital Proteins"},
            ],
            "raw": "Vital Proteins is the top recommended collagen supplement.",
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)

    assert opp["top_open_lanes"] == []
    assert opp["intent_ladder"]["branded_transactional"]["score"] >= 70
    assert opp["intent_ladder"]["head_category"]["score"] < 40
    assert opp["demand_state_summary"] == "branded demand protected, unbranded absent"


@pytest.mark.asyncio
async def test_build_per_sku_report_preserves_prior_keys_and_adds_opportunity(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _bb_lab_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str) -> List[Dict[str, Any]]:
        return _bb_lab_probe_runs()

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)

    report = await bd.build_per_sku_report("sku-bblab", "m-1", "audit-1")
    prior_keys = {
        "sku_key",
        "product_key",
        "content_key",
        "sku_title",
        "identity",
        "scores",
        "citation_by_provider",
        "band",
        "primary_gaps",
        "verbatim_grounding_evidence",
        "axis_coverage",
        "failing_prompts",
        "impact_proxy",
        "provider_models",
        "model_is_override",
        "verify_summary",
        "verify_outputs",
    }
    assert prior_keys.issubset(report.keys())
    assert "opportunity" in report
    assert report["opportunity"]["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
