"""#1503: suggested_prompts must fire for any product with basic content.

Pre-fix it was empty for ~90% of SKUs in every vertical because of four stacked
gates: an `attributes_raw` requirement no catalog_products row can satisfy (the
column doesn't exist), a beauty-fitted category lexicon (hand-seeded from
Anuko's own audits), every query shape demanding category + a secondary
evidenced attribute, and probed-pool subtraction. These tests pin the fix:
gate removed for suggestions (probe lane untouched), vertical-agnostic
tail-noun category fallback, chooser shapes for category-only graphs, honest
subtraction preserved, rich-graph behavior unchanged.
"""
from services.agent_center_bd_report_service import (
    _sidewalk_query_records_for_sku,
    _suggested_prompts_for_sku,
)
from services.sku_sidewalk import (
    _first_buyer_category,
    build_sku_attribute_graph,
    generate_sidewalk_query_specs,
)


def _suggest(product, opportunity=None):
    ctx = {"product": product}
    return _suggested_prompts_for_sku(
        ctx,
        opportunity=opportunity or {},
        attribute_graph=build_sku_attribute_graph(product),
    )


def test_catalog_sku_without_attributes_raw_gets_suggestions():
    # catalog_products has no attributes_raw column — the old gate emptied
    # suggestions for EVERY connected-catalog SKU unconditionally.
    out = _suggest({
        "title": "HOVERAir X1 PRO 4K Action Flying Camera Drone",
        "product_type": "Camera Drone",
        "description": "Self-flying camera drone.",
    })
    assert out, "connected-catalog SKU must yield suggestions"
    assert any("drone" in o["query"] for o in out)


def test_tail_noun_fallback_is_vertical_agnostic():
    assert _first_buyer_category({}, "Camera Drone") == "drone"
    assert _first_buyer_category({}, "Bone Conduction Headphones") == "headphones"
    # Container/department words never become categories.
    assert _first_buyer_category({}, "Beauty") is None
    assert _first_buyer_category({}, "Supplements") is None
    assert _first_buyer_category({}, "") is None


def test_tail_noun_fallback_blocks_packaging_words():
    # Packaging/collection words describe how a product is sold, not what it
    # is — they must not become pseudo-categories ("best bundle" queries).
    assert _first_buyer_category({}, "Gift Bundle") is None
    assert _first_buyer_category({}, "Value Pack") is None
    assert _first_buyer_category({}, "2 Pack") is None
    assert _first_buyer_category({}, "Collection") is None
    assert _first_buyer_category({}, "Essentials") is None
    assert _first_buyer_category({}, "Gift Set") is None
    assert _first_buyer_category({}, "Starter Kit") is None
    assert _first_buyer_category({}, "Subscription Box") is None
    assert _first_buyer_category({}, "Variety Packs") is None
    # A real tail noun after a packaging modifier still resolves.
    assert _first_buyer_category({}, "Travel Kit Toothbrush") == "toothbrush"


def test_thin_beauty_sku_gets_chooser_suggestions():
    # The Shiso case: category resolves ("shampoo") but no secondary lexicon
    # attribute — previously zero candidates.
    out = _suggest({
        "title": "CLASSIC SHAMPOO - Shiso",
        "product_type": "Shampoo",
        "description": "A gentle daily shampoo.",
    })
    assert any("shampoo" in o["query"] for o in out)


def test_probed_subtraction_still_honest():
    product = {
        "title": "CLASSIC SHAMPOO - Shiso",
        "product_type": "Shampoo",
        "description": "A gentle daily shampoo.",
    }
    first = _suggest(product)
    assert first
    probed = {"per_prompt": [{"query": o["query"]} for o in first]}
    assert _suggest(product, opportunity=probed) == []


def test_rich_graph_keeps_attribute_shapes_no_chooser():
    # Anuko-shaped SKU: attribute-stacked shapes lead; chooser never fires.
    product = {
        "title": "ANUKO Nourishing Hair Butter",
        "product_type": "Hair Butter",
        "description": "Shea butter and argan oil treatment for damaged hair.",
        "attributes_raw": {"tags": ["shea butter", "argan oil", "k-beauty"]},
    }
    specs = generate_sidewalk_query_specs(
        build_sku_attribute_graph(product),
        title=product["title"], product_type=product["product_type"], n=10,
    )
    queries = [s["query"] for s in specs]
    assert queries, "rich graph still generates"
    assert not any("buying guide" in q for q in queries)
    assert not any("for beginners" in q for q in queries)


def test_probe_lane_gate_untouched():
    # Probe-set composition is priced/pinned behavior — the attributes_raw gate
    # deliberately REMAINS on the probe lane (#1503 scope).
    ctx = {"product": {
        "title": "HOVERAir X1 PRO", "product_type": "Camera Drone",
        "description": "Self-flying camera drone.",
    }}
    assert _sidewalk_query_records_for_sku(
        ctx, title="HOVERAir X1 PRO", product_type="Camera Drone", prompts_per_sku=14,
    ) == []
