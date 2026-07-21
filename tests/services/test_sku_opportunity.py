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
        # Serving-eligible: this fixture exercises the open-lane-capture path,
        # which only applies once the SKU is indexed. Without this the SKU is
        # un-indexed and (correctly) classified get_indexed instead.
        "index_pipeline_state": {
            "serving_eligible": True,
            "pipeline_stage": "public_indexed",
            "identity_resolved": True,
        },
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
    assert head["ownership_state"] in {
        "competitor-owned",
        "marketplace-owned",
        "publisher-owned",
        "retailer-owned",
    }
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
        "kind": "branded",
        # A branded/specific prompt, not a broad head term — the comparison
        # play stays the prescribed move.
        "broad_head_prompt": False,
    }

    assert opportunity["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
    assert opportunity["top_open_lanes"][0]["current_ownership"] == "open-lane"
    assert opportunity["top_open_lanes"][0]["source_route"] == "unclassified"
    assert opportunity["top_open_lanes"][0]["first_move"] == "Add a PDP section + FAQ for this lane"
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


def test_substitution_fires_on_unbranded_category_demand():
    """Organic-mode signal: a NON-branded category query (the brand's own
    category, query never names it) that comes back loss/absent with a rival
    named IS substitution — "AI recommends <competitor> instead of you for your
    category". Previously suppressed because the gate required the query to name
    the merchant; discovery probes never do."""
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
            # No brand in the query — pure category demand (axis=category).
            "query": "best natural deodorant",
            "axis": "category",
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
                "competitors_appearing": competitors,
            },
            "sources": competitor_sources,
            "raw": "Top natural deodorants include Native, Lume, and Myro.",
        },
    ])
    opportunity = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opportunity)["best natural deodorant"]
    assert row["substitution"]["present"] is True
    assert row["substitution"]["kind"] == "category"
    assert row["substitution"]["substituted_by"] in competitors
    # And it bubbles up to the SKU-level alert.
    assert opportunity["substitution_alert"]["present"] is True


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


def test_branded_prompt_with_publisher_citations_is_not_merchant_owned():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = {
        "sku_key": "sku-ritual",
        "merchant_id": "m-ritual",
        "product": {
            "title": "Ritual Essential for Women 18+ Multivitamin",
            "raw_title": "Ritual Essential for Women 18+ Multivitamin",
            "brand": "Ritual",
            "vendor": "Ritual",
            "product_type": "Multivitamin",
            "canonical_url": "https://ritual.com/products/essential-for-women-multivitamin-18",
            "attributes_raw": {"tags": ["vegan", "iron-free"]},
        },
    }
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "Ritual Essential for Women 18+ Multivitamin reviews",
            "axis": "intent",
            "parsed": {
                "product_visible": True,
                "correct_sku": True,
                "sku_mentioned": True,
            },
            "sources": [
                {"uri": "https://medicalnewstoday.com/best-multivitamins", "title": "Medical News Today"},
                {"uri": "https://healthline.com/nutrition/best-multivitamins", "title": "Healthline"},
                {"uri": "https://ulta.com/p/ritual", "title": "Ulta"},
            ],
            "raw": "Ritual is mentioned in publisher reviews and retailer listings.",
        },
    ])

    row = _by_query(build_sku_opportunity(ctx, runs, attribute_graph=graph))[
        "ritual essential for women 18+ multivitamin reviews"
    ]

    assert row["provider_verdicts"] == {"gemini": "win", "deepseek": "win"}
    assert row["ownership_state"] == "publisher-owned"
    assert row["who_owns"] == ["healthline.com", "medicalnewstoday.com"]


def test_branded_prompt_with_retailer_citations_names_retailer_owner():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = {
        "sku_key": "sku-ritual",
        "merchant_id": "m-ritual",
        "product": {
            "title": "Ritual Essential for Women 18+ Multivitamin",
            "brand": "Ritual",
            "vendor": "Ritual",
            "product_type": "Multivitamin",
            "canonical_url": "https://ritual.com/products/essential-for-women-multivitamin-18",
            "attributes_raw": {"tags": ["vegan"]},
        },
    }
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "where can I buy Ritual Essential for Women 18+ Multivitamin",
            "axis": "intent",
            "parsed": {
                "product_visible": True,
                "correct_sku": True,
                "sku_mentioned": True,
            },
            "sources": [
                {"uri": "https://ulta.com/p/ritual", "title": "Ulta"},
                {"uri": "https://walmart.com/ip/ritual", "title": "Walmart"},
            ],
            "raw": "Ritual is available at Ulta and Walmart.",
        },
    ])

    row = _by_query(build_sku_opportunity(ctx, runs, attribute_graph=graph))[
        "where can i buy ritual essential for women 18+ multivitamin"
    ]

    assert row["ownership_state"] == "retailer-owned"
    assert row["who_owns"] == ["ulta.com", "walmart.com"]
    assert row["source_summary"]["buyer_path_controllers"] == [
        {"host": "ulta.com", "role": "retailer", "times_cited": 2},
        {"host": "walmart.com", "role": "retailer", "times_cited": 2},
    ]


def test_merchant_owned_requires_dominant_first_party_citation():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = {
        "sku_key": "sku-ritual",
        "merchant_id": "m-ritual",
        "product": {
            "title": "Ritual Essential for Women 18+ Multivitamin",
            "brand": "Ritual",
            "vendor": "Ritual",
            "product_type": "Multivitamin",
            "canonical_url": "https://ritual.com/products/essential-for-women-multivitamin-18",
            "attributes_raw": {"tags": ["vegan"]},
        },
    }
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "where can I buy Ritual Essential for Women 18+ Multivitamin",
            "axis": "intent",
            "parsed": {
                "product_visible": True,
                "correct_sku": True,
                "sku_mentioned": True,
            },
            "sources": [
                {
                    "uri": "https://ritual.com/products/essential-for-women-multivitamin-18",
                    "title": "Ritual PDP",
                },
                {"uri": "https://medicalnewstoday.com/best-multivitamins", "title": "Medical News Today"},
            ],
            "raw": "Ritual's PDP and Medical News Today are cited.",
        },
    ])

    row = _by_query(build_sku_opportunity(ctx, runs, attribute_graph=graph))[
        "where can i buy ritual essential for women 18+ multivitamin"
    ]

    assert row["ownership_state"] == "merchant-owned"
    assert row["who_owns"] is None


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
            "sources": [_redirector_source("bblab.shop official PDP", "bblab-pdp")],
            "raw": "BB Lab Good Night Collagen is cited from the BB Lab official PDP.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["provider_verdicts"] == {"gemini": "win", "deepseek": "win"}
    assert row["ownership_state"] == "merchant-owned"
    assert row["open_lane"] is False


def test_cold_start_url_audit_pdp_url_only_is_merchant_owned():
    """Regression for the URL-audit P0: the cold-start product carries the
    merchant's own URL as `pdp_url` only (no canonical_url). First-party
    detection must still recognize the merchant's own host, or the merchant's
    own domain gets mislabeled third-party and merchant_owned_count is 0 even
    when AI cites the merchant's site."""
    from services.sku_opportunity import build_sku_opportunity, _merchant_host
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    # Real cold-start shape: drop canonical fields, keep only pdp_url.
    ctx["product"].pop("canonical_url", None)
    ctx["product"].pop("pivota_canonical_url", None)
    ctx["product"]["pdp_url"] = "https://bblab.shop/products/good-night-collagen"

    assert _merchant_host(ctx, ctx["product"]) == "bblab.shop"

    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([
        {
            "query": "halal collagen sticks before bed",
            "axis": "sidewalk",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": [_redirector_source("bblab.shop official PDP", "bblab-pdp")],
            "raw": "BB Lab Good Night Collagen is cited from the BB Lab official PDP.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["ownership_state"] == "merchant-owned"


def _fragmented_tail():
    """A long tail of single-citation external hosts — the realistic shape of AI
    grounding that makes `first_party >= sum(external)` unachievable."""
    return [
        {"uri": f"https://shop{i}.example/collagen", "title": f"shop{i}.example"}
        for i in range(12)
    ]


def test_branded_lane_with_merchant_cited_is_merchant_owned_amid_fragmentation():
    """Intent-aware ownership: on the merchant's OWN branded buyer-path query, if
    the merchant's site is cited (>= any single competitor) and the product wins,
    the merchant owns the lane even when AI grounds the answer in a long tail of
    external hosts. Pre-fix this returned retailer-owned because the strict gate
    required first-party to outweigh the SUM of all external sources."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    sources = [
        _redirector_source("bblab.shop", "bblab-1"),
        {"uri": "https://yesstyle.com/bblab", "title": "YesStyle - BB Lab"},
    ] + _fragmented_tail()
    runs = _runs_both_providers([
        {
            "query": "where can I buy BB Lab Good Night Collagen",
            "axis": "intent",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": sources,
            "raw": "Available on the BB Lab official site (bblab.shop) and many resellers.",
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["where can i buy bb lab good night collagen"]
    assert row["ownership_state"] == "merchant-owned"


def test_category_lane_with_merchant_cited_stays_third_party():
    """Honesty guard: the intent-aware floor must NOT extend to category/head
    lanes. A single merchant citation among a fragmented field of publishers is
    third-party-controlled, not merchant-owned (no inflation)."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    sources = [
        _redirector_source("bblab.shop", "bblab-1"),
        {"uri": "https://goodhousekeeping.com/best-collagen", "title": "Good Housekeeping"},
    ] + _fragmented_tail()
    runs = _runs_both_providers([
        {
            "query": "best collagen",
            "axis": "category",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": sources,
            "raw": "Best collagen roundup naming many brands.",
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["best collagen"]
    assert row["ownership_state"] != "merchant-owned"


def test_is_merchant_self_recognizes_own_product_not_competitor():
    """Bug (b) root: the merchant's own product line must never be mined as a
    competitor (which then surfaced as who_owns)."""
    from services.sku_opportunity import _is_merchant_self
    title = "Good Night Collagen Low-Molecular Weight Collagen"
    assert _is_merchant_self(
        "Good Night Collagen (Low-Molecular Weight Collagen) Halal 30 sticks",
        merchant_brand="BB LAB", merchant_title=title) is True
    assert _is_merchant_self("BB Lab", merchant_brand="BB LAB", merchant_title=title) is True
    # a real competitor sharing only the generic 'collagen' token is NOT self
    assert _is_merchant_self(
        "Vital Proteins Collagen Peptides", merchant_brand="BB LAB",
        merchant_title=title) is False


def test_per_lane_controllers_exclude_merchant_first_party_host():
    """Bug (a): the merchant's own host is cited in a lane's sources but must NOT
    be named as a per-lane controller (it was rendered as a 'weak citation
    trail' against the merchant)."""
    from services.buyer_path_stable_controllers import (
        stable_buyer_path_controllers_for_row,
    )
    row = {
        "who_owns": "yesstyle.com",
        "source_route": "retailer",
        "ownership_state": "retailer-owned",
        "source_roles": [
            {"host": "yesstyle.com", "role": "retailer", "times_cited": 2},
            {"host": "bblab.shop", "role": "unclassified", "times_cited": 3},
        ],
    }
    hosts = [c["host"] for c in
             stable_buyer_path_controllers_for_row(row, exclude_hosts="bblab.shop")]
    assert "bblab.shop" not in hosts
    assert "yesstyle.com" in hosts
    # subdomains of the merchant are excluded too; a look-alike is not
    row["source_roles"].append({"host": "shop.bblab.shop", "times_cited": 4})
    row["source_roles"].append({"host": "notbblab.shop", "role": "retailer", "times_cited": 2})
    hosts2 = [c["host"] for c in
              stable_buyer_path_controllers_for_row(row, exclude_hosts="bblab.shop")]
    assert "shop.bblab.shop" not in hosts2
    assert "notbblab.shop" in hosts2


def test_merchant_own_product_does_not_become_who_owns():
    """End-to-end (bug b): a lane whose only 'competitor' is the merchant's own
    product must not read competitor-owned with the merchant's product title as
    who_owns."""
    from services.sku_opportunity import build_sku_opportunity, _merchant_host
    from services.sku_sidewalk import build_sku_attribute_graph
    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([{
        "query": "low molecular collagen sticks",
        "axis": "category",
        "parsed": {
            "product_visible": True, "correct_sku": True, "sku_mentioned": True,
            "competitors_listed": ["BB Lab Good Night Collagen Low-Molecular Weight Collagen"],
        },
        "sources": [_redirector_source("bblab.shop", "bblab-pdp")],
        "raw": "BB Lab Good Night Collagen Low-Molecular Weight Collagen.",
    }])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["low molecular collagen sticks"]
    who = row.get("who_owns")
    assert who != "Good Night Collagen Low-Molecular Weight Collagen"
    # whatever who_owns ends up, it is never the merchant's own product title
    if isinstance(who, str):
        assert "good night collagen" not in who.lower()


def test_branded_lane_without_merchant_citation_not_merchant_owned():
    """Honesty guard: on a branded query where the merchant's own site is NOT
    cited, the floor must not fire (first-party present is required)."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    sources = [
        {"uri": "https://yesstyle.com/bblab", "title": "YesStyle - BB Lab"},
    ] + _fragmented_tail()
    runs = _runs_both_providers([
        {
            "query": "shop BB Lab Good Night Collagen online",
            "axis": "intent",
            "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
            "sources": sources,
            "raw": "Sold by resellers; the official store is not surfaced.",
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["shop bb lab good night collagen online"]
    assert row["ownership_state"] != "merchant-owned"


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


def test_grounded_denial_does_not_create_demand_or_open_lane():
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
                {"uri": "https://glossary-one.example/acai", "title": "Acai glossary"},
                {"uri": "https://glossary-two.example/collagen", "title": "Collagen overview"},
                {"uri": "https://nutrition-notes.example/beauty", "title": "Nutrition notes"},
            ],
            "raw": "I don't have enough information to identify a buyer recommendation.",
            "axis_metadata": _sidewalk_meta(),
        },
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["demand_signal"] == 0.0
    assert row["ownership_state"] == "no-demand"
    assert row["demand_state"] == "no-demand"
    assert row["open_lane"] is False
    assert opp["top_open_lanes"] == []


def test_weak_single_source_demand_is_not_primary_open_lane():
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = [{
        "provider": "gemini",
        "probe_run_id": "probe-gemini",
        "raw_runs": [
            _run(
                query="halal collagen sticks before bed",
                axis="sidewalk",
                provider="gemini",
                parsed={},
                sources=[
                    {"uri": "https://niche-notes.example/halal-collagen", "title": "Niche collagen notes"},
                ],
                raw="One niche source says shoppers can buy halal collagen sticks before bed.",
                axis_metadata=_sidewalk_meta(),
            )
        ],
    }]
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]

    assert row["demand_signal"] == 0.4
    assert row["confidence"] < 0.8
    assert row["open_lane"] is False
    assert opp["top_open_lanes"] == []


def test_branded_only_summary_marks_unbranded_not_measured():
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
    ])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)

    assert opp["intent_ladder"]["branded_transactional"]["score"] >= 70
    assert opp["intent_ladder"]["head_category"]["prompts"] == 0
    assert opp["intent_ladder"]["attribute_category"]["prompts"] == 0
    assert opp["demand_state_summary"] == "branded demand protected, unbranded not measured"


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

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
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
    assert report["opportunity"]["top_open_lanes"][0]["first_move"] == "Add a PDP section + FAQ for this lane"
    assert "next_best_action" in report
    assert report["next_best_action"]["primary_gap"] == "open_lane_capture"
    assert "halal collagen sticks before bed" in report["next_best_action"]["first_move"]


@pytest.mark.asyncio
async def test_build_per_sku_report_attaches_optional_strategic_brief(monkeypatch):
    from services import agent_center_bd_report_service as bd

    async def fake_load_sku_context(sku_key: str, merchant_id: str) -> Dict[str, Any]:
        ctx = _bb_lab_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_load_runs(sku_key: str, merchant_id: str, audit_run_id: str, include_internal_comparison: bool = False) -> List[Dict[str, Any]]:
        return _bb_lab_probe_runs()

    attach_calls: List[Dict[str, Any]] = []

    async def fake_attach(next_best_action: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        attach_calls.append(kwargs)
        assert next_best_action["primary_gap"] == "open_lane_capture"
        out = dict(next_best_action)
        out["strategic_brief"] = {"position": "grounded"}
        return out

    monkeypatch.setattr(bd, "load_sku_context", fake_load_sku_context)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_load_runs)
    monkeypatch.setattr(bd, "attach_sku_strategic_brief", fake_attach)

    report = await bd.build_per_sku_report("sku-bblab", "m-1", "audit-1")

    assert report["next_best_action"]["primary_gap"] == "open_lane_capture"
    assert report["next_best_action"]["strategic_brief"] == {"position": "grounded"}
    assert len(attach_calls) == 1
    assert attach_calls[0]["opportunity"]["top_open_lanes"][0]["query"] == "halal collagen sticks before bed"
    assert attach_calls[0]["attribute_graph"]["classes"]["certification_constraint"] == ["halal"]


def test_lane_captures_verbatim_cited_evidence_for_losing_lane():
    """The merchant must be able to SEE what AI actually said on a lane it loses
    — the answer excerpt + who it named — not just the competitor hostname."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph
    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    excerpt = ("For the best fish collagen tablets, Vital Proteins and NeoCell are "
               "widely recommended and available at Walmart and Ulta.")
    runs = _runs_both_providers([{
        "query": "best fish collagen tablets",
        "axis": "category",
        "parsed": {"product_visible": False, "correct_sku": False,
                   "evidence_excerpt": excerpt,
                   "competitors_listed": ["Vital Proteins", "NeoCell"]},
        "sources": [
            {"uri": "https://walmart.com/best-collagen", "title": "walmart.com"},
            {"uri": "https://ulta.com/collagen", "title": "ulta.com"},
        ],
        "raw": excerpt,
    }])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["best fish collagen tablets"]
    ce = row.get("cited_evidence")
    assert ce is not None
    assert "fish collagen tablets" in ce["excerpt"].lower()
    # cited hosts are the external controllers AI named, not the merchant
    assert "bblab.shop" not in ce["cited_hosts"]
    assert any(h in ce["cited_hosts"] for h in ("walmart.com", "ulta.com"))
    assert "Vital Proteins" in ce["competitors_named"]


def test_lane_cited_evidence_is_none_without_excerpt():
    """No answer excerpt -> cited_evidence is absent, never fabricated."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph
    ctx = _bb_lab_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    runs = _runs_both_providers([{
        "query": "halal collagen sticks before bed",
        "axis": "sidewalk",
        "parsed": {"product_visible": True, "correct_sku": True, "sku_mentioned": True},
        "sources": [_redirector_source("bblab.shop", "bblab")],
        "raw": "cited",
        "axis_metadata": _sidewalk_meta(),
    }])
    opp = build_sku_opportunity(ctx, runs, attribute_graph=graph)
    row = _by_query(opp)["halal collagen sticks before bed"]
    assert row.get("cited_evidence") is None


def test_brand_in_grounding_titles_detects_own_listing():
    """The endorsement-vs-findability discriminator: brand in a grounding
    SOURCE TITLE = the brand's own product/store listing was retrieved (weak);
    brand absent from titles = it surfaced via an independent source."""
    from services.sku_opportunity import _brand_in_grounding_titles
    own_listing = [{"grounding_sources": [
        {"uri": "https://hwahae.com/x", "title": "Anuko NOURISHING HAIR BUTTER | Hwahae"}]}]
    independent = [{"grounding_sources": [
        {"uri": "https://stylecraze.com/best", "title": "The 15 Best Hair Butters | StyleCraze"}]}]
    assert _brand_in_grounding_titles(own_listing, merchant_brand="Anuko") is True
    assert _brand_in_grounding_titles(independent, merchant_brand="Anuko") is False
    # No brand -> can't claim a listing.
    assert _brand_in_grounding_titles(own_listing, merchant_brand="") is False


def test_substitution_drops_ingredient_only_competitors():
    """#4: a category lane lost only to ingredient/material TYPES ("Shea
    Butter") is NOT a brand substitution — don't produce 'AI names Shea Butter,
    not you'. A real brand among them IS named."""
    from services.sku_opportunity import build_sku_opportunity
    from services.sku_sidewalk import build_sku_attribute_graph

    ctx = _freshnest_sku_ctx()
    graph = build_sku_attribute_graph(ctx["product"])
    sources = [{"uri": "https://example.com/x", "title": "best hair butter"}]
    runs = _runs_both_providers([
        {   # all-generic competitors -> no brand substitution
            "query": "best hair butter",
            "axis": "category",
            "parsed": {"product_visible": False, "correct_sku": False,
                       "competitors_listed": ["Shea Butter", "Castor Oil"],
                       "competitors_appearing": ["Shea Butter", "Castor Oil"]},
            "sources": sources,
            "raw": "Top options are shea butter and castor oil.",
        },
        {   # a real brand present -> it is the substitute
            "query": "top hair butter",
            "axis": "category",
            "parsed": {"product_visible": False, "correct_sku": False,
                       "competitors_listed": ["Shea Butter", "Aunt Jackie's"],
                       "competitors_appearing": ["Shea Butter", "Aunt Jackie's"]},
            "sources": sources,
            "raw": "Top picks include Aunt Jackie's.",
        },
    ])
    rows = _by_query(build_sku_opportunity(ctx, runs, attribute_graph=graph))
    assert rows["best hair butter"]["substitution"]["present"] is False
    top = rows["top hair butter"]["substitution"]
    assert top["present"] is True
    assert top["substituted_by"] == "Aunt Jackie's"


def test_substitution_alert_prefers_specific_prompt_over_head_term():
    """Niche-first showcase: the head baseline probe ("best headphones")
    carries the highest demand_signal by construction, so demand-only sorting
    showcased the flagship fight on every audit. A specific losing prompt must
    win the alert slot; the head row only surfaces when it's the sole
    evidence — and is then flagged for the copy reframe."""
    from services.sku_opportunity import _substitution_alert

    head_row = {
        "query": "best headphones",
        "demand_signal": 3.0,
        "substitution": {
            "present": True,
            "prompt": "best headphones",
            "substituted_by": "Bose",
            "engines": ["chatgpt", "gemini"],
            "kind": "category",
        },
    }
    specific_row = {
        "query": "bone conduction headphones for lap swimming",
        "demand_signal": 1.0,
        "substitution": {
            "present": True,
            "prompt": "bone conduction headphones for lap swimming",
            "substituted_by": "Shokz",
            "engines": ["gemini"],
            "kind": "category",
        },
    }

    alert = _substitution_alert([head_row, specific_row])
    assert alert["prompt"] == "bone conduction headphones for lap swimming"
    assert alert["substituted_by"] == "Shokz"
    assert alert["broad_head_prompt"] is False

    head_only = _substitution_alert([head_row])
    assert head_only["prompt"] == "best headphones"
    assert head_only["broad_head_prompt"] is True


def test_substitution_alert_merchant_custom_head_prompt_not_deprioritized():
    """A merchant-authored prompt is a deliberate test: even head-shaped, it
    keeps its demand-ranked slot (prompt_source exemption) and is never
    flagged as a broad head term."""
    from services.sku_opportunity import _substitution_alert

    merchant_head = {
        "query": "best headphones",
        "prompt_source": "merchant_custom",
        "demand_signal": 3.0,
        "substitution": {
            "present": True,
            "prompt": "best headphones",
            "substituted_by": "Bose",
            "engines": ["gemini"],
            "kind": "category",
        },
    }
    specific_row = {
        "query": "bone conduction headphones for lap swimming",
        "demand_signal": 1.0,
        "substitution": {
            "present": True,
            "prompt": "bone conduction headphones for lap swimming",
            "substituted_by": "Shokz",
            "engines": ["gemini"],
            "kind": "category",
        },
    }

    alert = _substitution_alert([merchant_head, specific_row])
    assert alert["prompt"] == "best headphones"
    assert alert["broad_head_prompt"] is False
