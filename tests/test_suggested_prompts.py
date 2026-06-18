"""Win-the-specific-long-tail Step 2: the audit surfaces the specific, attribute-
stacked prompts the engine BUILDS from a SKU's evidenced attributes but did NOT
probe — the niches the merchant can test next — without re-listing what already ran
and without synthetic padding.

See PIVOTA-Agent/docs/ai_readiness_specific_longtail_build_plan.md.
"""

from services import agent_center_bd_report_service as m


def _collagen_sku_ctx():
    # Real product shape: the attribute graph is built by lexicon-matching product
    # TEXT (title / description / body_html / tags), not a flat attributes dict.
    product = {
        "product_key": "prod-1",
        "merchant_id": "m-1",
        "title": "BB Lab Good Night Collagen",
        "brand": "BB Lab",
        "vendor": "BB Lab",
        "product_type": "collagen supplement",
        "category": "supplements",
        "canonical_url": "https://bblab.shop/products/good-night-collagen",
        "attributes_raw": {
            "tags": ["halal", "collagen", "k-beauty"],
            "body_html": (
                "<p>Stick format. No water needed. Fish collagen with "
                "vitamin C and glycine.</p>"
            ),
            "description": (
                "Stick format. No water needed. Fish collagen with vitamin C "
                "and glycine."
            ),
            "variants": [
                {"title": "2g x 30 sticks", "price": "25.99",
                 "option1": "30 sticks", "available": True}
            ],
            "options": [{"name": "Size", "values": ["30 sticks"]}],
        },
    }
    return {
        "product": product,
        "sku": {"title": "2g x 30 sticks"},
        "sku_title": "2g x 30 sticks",
        "sku_key": "gnc-2box",
        "product_enrichment": {"topic_tags": ["skin routine"]},
    }


def _graph(ctx):
    return m.build_sku_attribute_graph(m._get_product(ctx))


def test_suggestions_are_specific_stacked_and_real():
    ctx = _collagen_sku_ctx()
    sugg = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": []}, attribute_graph=_graph(ctx)
    )
    assert sugg, "a rich SKU must yield specific suggestions"
    assert len(sugg) <= m._SUGGESTED_PROMPTS_PER_SKU
    # specific = multi-attribute stacks (the category word + >=1 evidenced attribute)
    assert all("collagen" in s["query"] for s in sugg)
    assert all(len(s["attribute_basis"]) >= 2 for s in sugg), (
        "suggestions must be attribute-stacked (the winnable specific lane)"
    )
    # ranked by specificity weight (descending)
    weights = [s["intent_weight"] for s in sugg]
    assert weights == sorted(weights, reverse=True)


def test_probed_queries_are_excluded():
    """A suggestion must never be a prompt the audit already measured."""
    ctx = _collagen_sku_ctx()
    base = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": []}, attribute_graph=_graph(ctx)
    )
    assert base
    # mark the top suggestion as already probed -> it must disappear
    probed_q = base[0]["query"]
    after = m._suggested_prompts_for_sku(
        ctx,
        opportunity={"per_prompt": [{"normalized_query": probed_q.lower()}]},
        attribute_graph=_graph(ctx),
    )
    assert all(s["query"].lower() != probed_q.lower() for s in after)


def test_all_probed_yields_empty_no_padding():
    """If every generated candidate already ran, suggestions are [] (no junk)."""
    ctx = _collagen_sku_ctx()
    g = _graph(ctx)
    # mark the helper's OWN full candidate pool as probed (a large per-SKU cap so we
    # capture the whole pool), then nothing should remain to suggest.
    pool = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": []}, attribute_graph=g,
        max_suggestions=m._SUGGESTED_PROMPT_POOL,
    )
    probed = [{"normalized_query": s["query"].lower()} for s in pool]
    sugg = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": probed}, attribute_graph=g
    )
    assert sugg == []


def test_thin_sku_no_attributes_yields_empty():
    ctx = {"product": {"title": "Mystery", "product_type": ""}, "sku_key": "x"}
    sugg = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": []}, attribute_graph=_graph(ctx)
    )
    assert sugg == []


def test_brand_rollup_dedupes_and_ranks():
    ctx = _collagen_sku_ctx()
    g = _graph(ctx)
    per_sku = m._suggested_prompts_for_sku(
        ctx, opportunity={"per_prompt": []}, attribute_graph=g
    )
    # two SKUs sharing the same candidate niches -> deduped to one entry each
    reports = [
        {"sku_key": "a", "sku_title": "A", "identity": {"name": "A"}, "suggested_prompts": per_sku},
        {"sku_key": "b", "sku_title": "B", "identity": {"name": "B"}, "suggested_prompts": per_sku},
    ]
    rolled = m.build_suggested_prompts(reports)
    assert rolled["has_prompts"]
    qs = [p["normalized_query"] for p in rolled["prompts"]]
    assert len(qs) == len(set(qs)), "brand rollup must dedupe by normalized query"
    assert len(rolled["prompts"]) <= m._SUGGESTED_PROMPTS_BRAND_MAX
    weights = [p["intent_weight"] for p in rolled["prompts"]]
    assert weights == sorted(weights, reverse=True)
    assert all(p["source"] == "sidewalk_candidate" for p in rolled["prompts"])


def test_brand_rollup_empty_when_no_suggestions():
    rolled = m.build_suggested_prompts(
        [{"sku_key": "a", "identity": {"name": "A"}, "suggested_prompts": []}]
    )
    assert rolled["has_prompts"] is False
    assert rolled["prompts"] == []
