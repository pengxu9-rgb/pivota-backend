from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

import pytest


def _bb_lab_product() -> Dict[str, Any]:
    return {
        "title": "BB Lab Good Night Collagen (Halal), 2g x 30 sticks",
        "raw_title": "BB LAB Good Night Collagen (Halal), 2g x 30 sticks",
        "vendor": "BB Lab",
        "product_type": "collagen supplement",
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
                {
                    "title": "2g x 30 sticks",
                    "price": "25.99",
                    "option1": "30 sticks",
                    "available": True,
                }
            ],
            "options": [{"name": "Size", "values": ["30 sticks"]}],
        },
    }


def _sku_ctx(*, attributes: bool = True) -> Dict[str, Any]:
    product = {
        "product_key": "prod-1",
        "merchant_id": "m-1",
        "title": "BB Lab Good Night Collagen",
        "brand": "BB Lab",
        "vendor": "BB Lab",
        "product_type": "collagen supplement",
        "category": "supplements",
        "canonical_url": "https://bblab.shop/products/good-night-collagen",
    }
    if attributes:
        product["attributes_raw"] = _bb_lab_product()["attributes_raw"]
    return {
        "sku_key": "sku-1",
        "merchant_id": "m-1",
        "product_key": "prod-1",
        "product": product,
        "sku": {"sku_key": "sku-1", "product_key": "prod-1", "title": "2g x 30 sticks"},
        "product_enrichment": {
            "topic_tags": ["skin routine"],
            "bullet_points": ["mixed berry"],
        },
    }


def test_sku_attribute_graph():
    from services.sku_sidewalk import build_sku_attribute_graph

    graph = build_sku_attribute_graph(_bb_lab_product())
    classes = graph["classes"]
    evidence = graph["evidence"]

    assert "collagen" in classes["category"]
    assert "stick" in classes["format"]
    assert "halal" in classes["certification_constraint"]
    assert "fish collagen" in classes["ingredient"]
    assert "vitamin c" in classes["ingredient"]
    assert "no water" in classes["exclusion"]
    assert evidence["halal"] == "tag"
    assert evidence["fish collagen"] == "body"
    assert evidence["stick"] in {"title", "body", "variant"}


def test_sku_attribute_graph_uses_merchant_type_and_tags_for_multivitamin():
    from services.sku_sidewalk import build_sku_attribute_graph

    product = {
        "title": "Ritual Essential for Women 18+ Multivitamin",
        "vendor": "Ritual",
        "product_type": "Multivitamin",
        "attributes_raw": {
            "tags": ["Vegan", "Iron-Free", "Omega-3 DHA", "Traceable"],
            "description": (
                "Vegan capsules with omega-3 DHA, methylated folate, "
                "and traceable nutrients."
            ),
        },
    }

    graph = build_sku_attribute_graph(product)
    classes = graph["classes"]

    assert "multivitamin" in classes["category"]
    assert "iron-free" in classes["exclusion"]
    assert "omega-3 dha" in classes["ingredient"]
    assert "vegan" in classes["certification_constraint"]
    assert "traceable" in classes["proof"]


def test_sku_attribute_graph_filters_flavor_noise_from_direct_attrs():
    from services.sku_sidewalk import build_sku_attribute_graph

    product = {
        "title": "Triple Shine Grape",
        "vendor": "Ownist",
        "product_type": "Belight grape jelly",
        "attributes_raw": {
            "tags": [
                "collagen",
                "belight collagen",
                "vitamin c",
                "grape",
                "k-beauty",
                "skin radiance",
            ],
            "description": (
                "Ownist Triple Shine Grape is a K-beauty supplement with "
                "Belight collagen and vitamin C."
            ),
        },
    }

    graph = build_sku_attribute_graph(product)
    classes = graph["classes"]

    assert "belight grape jelly" not in classes["category"]
    assert "grape" not in classes["use_case"]
    assert "collagen" in classes["category"]
    assert "vitamin c" in classes["ingredient"]


def test_sidewalk_generation_bb_lab():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = _bb_lab_product()
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph,
        title=product["title"],
        product_type=product["product_type"],
        n=8,
    )
    queries = [spec["query"] for spec in specs]

    assert "halal collagen sticks" in queries
    assert "collagen stick no water travel" in queries
    assert all(spec["attribute_basis"] for spec in specs)
    assert all(spec["evidence"] for spec in specs)
    assert not any("sleep aid" in query or "helps you sleep" in query for query in queries)
    assert not any("sleep" in query for query in queries)


def test_sidewalk_no_category_noun_repeat_electronics():
    """When the product-type noun leaks into the use_case class (audio: the
    category IS 'bone conduction headphones' and 'bone conduction headphones' also
    lands in use_case), the generator must not emit shapes that repeat the head
    noun ('...open-ear structure bone conduction headphones'). _destutter only
    collapses ADJACENT repeats; the category-head-noun count guard catches the
    non-adjacent one."""
    import re
    from services.sku_sidewalk import generate_sidewalk_query_specs

    graph = {
        "classes": {
            "category": ["bone conduction headphones"],
            "format": ["open-ear structure"],
            "certification_constraint": ["ip68 waterproof certified"],
            "use_case": ["bone conduction headphones", "daily sports"],
        }
    }
    specs = generate_sidewalk_query_specs(
        graph,
        title="Purra Swim",
        product_type="bone conduction headphones",
        n=12,
    )
    queries = [s["query"] for s in specs]
    assert queries, "expected some clean sidewalk queries"
    for q in queries:
        assert re.findall(r"[a-z0-9]+", q.lower()).count("headphones") <= 1, (
            f"category noun repeated in sidewalk query: {q!r}"
        )
    # the legitimate, non-repeating use_case still produces a spec query.
    assert any("daily sports" in q for q in queries)


def test_sidewalk_guardrail_health():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = {
        "title": "Sunny Kids Mineral Sunscreen Stick SPF 50",
        "product_type": "kids sunscreen",
        "attributes_raw": {
            "tags": ["kids", "mineral", "sensitive skin"],
            "description": (
                "Mineral sunscreen stick with zinc oxide for kids, summer camp, "
                "sensitive skin and no white cast. Eczema-prone shoppers often "
                "ask about it, but the PDP makes no medical claim."
            ),
        },
    }
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph,
        title=product["title"],
        product_type=product["product_type"],
        n=10,
    )
    queries = [spec["query"] for spec in specs]

    assert queries
    assert not any("safe for kids" in query for query in queries)
    assert not any("eczema" in query for query in queries)
    assert not any("treat" in query or "medical" in query for query in queries)


def test_sidewalk_no_stutter_when_attr_spans_classes():
    # "sensitive skin" is both an audience and a use_case; lanes must not stutter
    # ("deodorant for sensitive skin sensitive skin").
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = {
        "title": "FreshNest Deodorant Refill Pods",
        "product_type": "deodorant",
        "attributes_raw": {
            "tags": ["baking-soda-free", "refill pod"],
            "description": (
                "Aluminum free deodorant refill pods, baking soda free, for "
                "sensitive skin."
            ),
        },
    }
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph, title=product["title"], product_type=product["product_type"], n=10,
    )
    queries = [spec["query"] for spec in specs]

    assert queries  # the deodorant vertical still produces lanes
    for query in queries:
        words = query.split()
        # no immediately-repeated phrase of length 1 or 2
        assert not any(words[i] == words[i + 1] for i in range(len(words) - 1)), query
        assert not any(
            words[i:i + 2] == words[i + 2:i + 4]
            for i in range(len(words) - 3)
        ), query


def test_sidewalk_negated_halal_does_not_generate_certified_lane():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = {
        "title": "PureGlow Collagen Capsules",
        "product_type": "collagen supplement",
        "attributes_raw": {
            "description": "Fish collagen capsules. This product is not halal certified.",
        },
    }
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph, title=product["title"], product_type=product["product_type"], n=8,
    )

    assert "halal" not in graph["classes"]["certification_constraint"]
    assert not any("halal" in spec["query"] for spec in specs)


def test_sidewalk_negated_pregnancy_audience_is_suppressed():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = {
        "title": "PureGlow Collagen Capsules",
        "product_type": "collagen supplement",
        "attributes_raw": {
            "description": (
                "Collagen capsules. No melatonin. Not intended for pregnant people."
            ),
        },
    }
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph, title=product["title"], product_type=product["product_type"], n=8,
    )

    assert "pregnancy" not in graph["classes"]["audience"]
    assert not any("pregnancy" in spec["query"] or "pregnant" in spec["query"] for spec in specs)


def test_sidewalk_mineral_oil_free_deodorant_does_not_get_mineral_lane():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = {
        "title": "FreshNest Mineral Oil Free Deodorant Refill Pods",
        "product_type": "deodorant",
        "attributes_raw": {
            "description": "Refill pods for sensitive skin. Mineral oil free and baking soda free.",
        },
    }
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph, title=product["title"], product_type=product["product_type"], n=8,
    )

    assert "mineral" not in graph["classes"]["certification_constraint"]
    assert not any(spec["query"].startswith("mineral deodorant") for spec in specs)


def test_sidewalk_positive_high_care_and_certification_lanes_survive():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    halal_product = {
        "title": "PureGlow Halal Collagen Capsules",
        "product_type": "collagen supplement",
        "attributes_raw": {
            "description": "Halal certified fish collagen capsules.",
        },
    }
    halal_graph = build_sku_attribute_graph(halal_product)
    halal_specs = generate_sidewalk_query_specs(
        halal_graph,
        title=halal_product["title"],
        product_type=halal_product["product_type"],
        n=8,
    )
    assert "halal" in halal_graph["classes"]["certification_constraint"]
    assert any("halal collagen capsules" == spec["query"] for spec in halal_specs)

    sunscreen_product = {
        "title": "SunnyPocket Kids Mineral Sunscreen Stick",
        "product_type": "kids sunscreen",
        "attributes_raw": {
            "tags": ["kids", "mineral"],
            "description": "Mineral sunscreen stick with zinc oxide for kids and summer camp.",
        },
    }
    sunscreen_graph = build_sku_attribute_graph(sunscreen_product)
    sunscreen_specs = generate_sidewalk_query_specs(
        sunscreen_graph,
        title=sunscreen_product["title"],
        product_type=sunscreen_product["product_type"],
        n=10,
    )
    sunscreen_queries = {spec["query"] for spec in sunscreen_specs}
    assert "kids" in sunscreen_graph["classes"]["audience"]
    assert "mineral" in sunscreen_graph["classes"]["certification_constraint"]
    assert "mineral sunscreen sticks" in sunscreen_queries
    assert any("for kids" in query for query in sunscreen_queries)


def test_sidewalk_category_format_stutter_and_generic_supplement_fallback():
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    balm_product = {
        "title": "Leaf Root Vegan Lip Balm",
        "product_type": "balm",
        "attributes_raw": {
            "tags": ["vegan"],
            "description": "A vegan balm for sensitive skin.",
        },
    }
    balm_graph = build_sku_attribute_graph(balm_product)
    balm_specs = generate_sidewalk_query_specs(
        balm_graph, title=balm_product["title"], product_type=balm_product["product_type"], n=8,
    )
    balm_queries = [spec["query"] for spec in balm_specs]
    assert "vegan balm" in balm_queries
    assert not any("balm balm" in query for query in balm_queries)

    vitamin_product = {
        "title": "Maternova Postpartum Hair Vitamins Gummies",
        "product_type": "supplement",
        "attributes_raw": {
            "tags": ["vegan", "gummies", "postpartum", "hair vitamins"],
            "description": "Vegan gummies for postpartum hair vitamins routines. No melatonin.",
        },
    }
    vitamin_graph = build_sku_attribute_graph(vitamin_product)
    vitamin_specs = generate_sidewalk_query_specs(
        vitamin_graph,
        title=vitamin_product["title"],
        product_type=vitamin_product["product_type"],
        n=8,
    )
    vitamin_queries = [spec["query"] for spec in vitamin_specs]
    assert "hair vitamin" in vitamin_graph["classes"]["category"]
    assert any("hair vitamin" in query for query in vitamin_queries)
    assert not any("supplement" in query for query in vitamin_queries)


def test_per_sku_budget_mix():
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_metadata,
        _build_per_sku_audit_query_specs,
    )

    # No-attributes SKU: diverse intent axes (head / problem_jtbd / trust / nav), the
    # demoted-superlative forms gone as PRIMARY specs (only the demoted head pair +
    # filler-pool variants remain). Budget-inversion + Step-1 reframe.
    no_attrs = _build_per_sku_audit_query_specs(_sku_ctx(attributes=False), 14)
    no_attrs_q = [q for q, _ in no_attrs]
    assert len(no_attrs) == 14
    assert "where can I buy BB Lab Good Night Collagen" in no_attrs_q   # navigational
    assert "best collagen supplement" in no_attrs_q                     # head (kept)
    assert "what collagen supplement should I buy" in no_attrs_q        # head (kept)
    assert "what helps with before bed" in no_attrs_q                   # problem_jtbd
    assert "is BB Lab legit" in no_attrs_q                              # trust
    # removed-by-Step-1 superlative forms are no longer generated:
    assert "what is the best collagen supplement" not in no_attrs_q
    assert "collagen supplement buying guide" not in no_attrs_q

    # Attributed SKU: the SPECIFIC stacked sidewalk long-tail is present + substantial.
    wedge = _build_per_sku_audit_query_specs(_sku_ctx(attributes=True), 14)
    assert len(wedge) == 14
    assert sum(1 for _query, axis in wedge if axis == "sidewalk") >= 4
    assert any(query == "collagen stick no water travel" for query, _axis in wedge)
    assert not any("shoppers considering" in query for query, _axis in wedge)

    metadata = _build_per_sku_audit_query_metadata(_sku_ctx(attributes=True), 14)
    assert metadata["collagen stick no water travel"]["attribute_basis"]
    assert metadata["collagen stick no water travel"]["evidence"]

    # Large budget: specific stacked is the SINGLE LARGEST axis (the inversion) —
    # capped only by how many stacks the SKU's attribute graph can produce.
    large = _build_per_sku_audit_query_specs(_sku_ctx(attributes=True), 40)
    large_axes = Counter(axis for _q, axis in large)
    assert len(large) == 40
    assert large_axes["sidewalk"] >= 10
    assert large_axes["sidewalk"] == max(large_axes.values())
    assert any(q == "collagen stick no water travel" for q, _ in large)


def test_per_sku_prompts_include_unbranded_multivitamin_discovery():
    from services.agent_center_bd_report_service import _build_per_sku_audit_query_specs

    product = {
        "title": "Ritual Essential for Women 18+ Multivitamin",
        "brand": "Ritual",
        "vendor": "Ritual",
        "product_type": "Multivitamin",
        "canonical_url": "https://ritual.com/products/essential-for-women-multivitamin-18",
        "attributes_raw": {
            "tags": ["Vegan", "Sugar-Free", "Iron-Free", "Omega-3 DHA", "Traceable"],
        },
    }
    ctx = {
        "sku_key": "ritual-women",
        "product": product,
        "sku": {"title": "60 capsules"},
    }

    specs = _build_per_sku_audit_query_specs(ctx, 14)
    queries = [query for query, _axis in specs]

    assert "best multivitamin" in queries
    # Step 1 reframed the audience discovery query "best X for {aud}" → "{cat} for {aud}".
    assert "multivitamin for women" in queries
    assert "iron-free multivitamin" in queries
    assert "vegan multivitamin" in queries
    # Brand-naming (branded) prompts are now CAPPED at a minority share of the
    # 14-prompt budget (#1521: default 30% cap = 4, floor 2). Was 5 (2 nav + 3
    # trust) before the rebalance; the surplus trust prompt is dropped in favour
    # of unbranded discovery. This is the deliberate mix change.
    assert sum("Ritual" in query for query in queries) == 4


@pytest.mark.asyncio
async def test_fetch_curated_preserves_attributes(monkeypatch):
    from services import bd_cold_start_service as bdcs

    async def fake_native(url):
        return {
            "title": "[Bundle] Good Night Collagen (Halal), 30 sticks",
            "vendor": "BB Lab",
            "product_type": "collagen supplement",
            "tags": "halal, collagen, k-beauty",
            "body_html": "<p>No water needed. Fish collagen with vitamin C.</p>",
            "variants": [
                {
                    "title": "30 sticks",
                    "price": "25.99",
                    "option1": "30 sticks",
                    "available": True,
                    "inventory_quantity": 100,
                }
            ],
            "options": [{"name": "Size", "values": ["30 sticks"]}],
            "handle": "good-night-collagen",
            "images": [{"src": "https://cdn.test/good-night.jpg"}],
        }

    async def fail_pdp(url):
        raise AssertionError("native fetch should win")

    monkeypatch.setattr(bdcs, "_fetch_shopify_native", fake_native)
    monkeypatch.setattr(bdcs, "_fetch_pdp_metadata", fail_pdp)

    product, reason = await bdcs.fetch_curated_audit_product(
        "https://bblab.shop/products/good-night-collagen"
    )

    assert reason is None
    assert product["title"] == "Good Night Collagen"
    assert product["raw_title"] == "[Bundle] Good Night Collagen (Halal), 30 sticks"
    assert product["pdp_url"] == "https://bblab.shop/products/good-night-collagen"
    assert product["vendor"] == "BB Lab"
    assert product["product_type"] == "collagen supplement"
    attrs = product["attributes_raw"]
    assert attrs["tags"] == ["halal", "collagen", "k-beauty"]
    assert attrs["description"] == "No water needed. Fish collagen with vitamin C."
    assert attrs["variants"] == [
        {
            "title": "30 sticks",
            "price": "25.99",
            "option1": "30 sticks",
            "available": True,
        }
    ]
    assert attrs["options"] == [{"name": "Size", "values": ["30 sticks"]}]
    assert attrs["handle"] == "good-night-collagen"
    assert attrs["images"] == {
        "count": 1,
        "first_url": "https://cdn.test/good-night.jpg",
    }


# --- tablet-SKU regressions (BB Lab Slimming / White Up Plus live audit) ----
def _bb_lab_slimming_tablet() -> Dict[str, Any]:
    """Mirrors the live Slimming Collagen SKU: an 84-tablet product the merchant
    also tags loosely with 'powder', whose ingredient copy lists titanium
    dioxide (a tablet-coating colorant)."""
    return {
        "title": "Slimming Collagen",
        "raw_title": "[Bundle] Slimming Collagen 84 tablets x 3box",
        "product_type": "Debloat",
        "attributes_raw": {
            "tags": ["beauty", "collagen", "diet", "powder", "slimming",
                     "tablets", "weight loss"],
            "body_html": (
                "<p>Fish collagen weight loss tablets. Other ingredients: "
                "titanium dioxide, microcrystalline cellulose, magnesium "
                "stearate. Low molecular collagen.</p>"
            ),
            "variants": [{"title": "84 tablets x 3box"}],
        },
    }


def _bb_lab_white_up_tablet() -> Dict[str, Any]:
    return {
        "title": "White Up Plus",
        "raw_title": "[Bundle] White Up Plus 30 tablets x 3box",
        "product_type": "Brightening",
        "attributes_raw": {
            "tags": ["Brightening", "Health", "Skin Care", "Tablets", "white"],
            "body_html": "<p>Vitamin C brightening tablets with glycine and fish collagen.</p>",
            "variants": [{"title": "30 tablets x 3box"}],
        },
    }


def _lanes(product):
    from services.sku_sidewalk import build_sku_attribute_graph, generate_sidewalk_query_specs
    graph = build_sku_attribute_graph(product)
    specs = generate_sidewalk_query_specs(
        graph, title=product["title"],
        product_type=product.get("product_type") or "", n=10,
    )
    return graph, [s["query"] for s in specs]


def test_tablet_sku_resolves_to_tablet_form_not_powder():
    """A tablet SKU loosely tagged 'powder' must read as tablets — the title
    form wins over the loose tag — so lanes never recommend 'collagen powder'
    the tablet can't substantiate."""
    graph, lanes = _lanes(_bb_lab_slimming_tablet())
    assert graph["classes"]["format"] == ["tablet"]
    assert not any("powder" in q for q in lanes)
    assert any("tablets" in q for q in lanes)


def test_ingestible_excipient_titanium_dioxide_never_a_lane():
    """Titanium dioxide is a tablet-coating colorant in an ingestible — it must
    not be mined as an ingredient or surface as a lane (the live 'titanium
    dioxide collagen powder' money-shot bug)."""
    graph, lanes = _lanes(_bb_lab_slimming_tablet())
    assert "titanium dioxide" not in (graph["classes"]["ingredient"] or [])
    assert not any("titanium" in q for q in lanes)


def test_pure_filler_excipients_never_a_lane():
    graph, lanes = _lanes(_bb_lab_slimming_tablet())
    assert not any("magnesium stearate" in q or "microcrystalline" in q
                   or "stearate" in q for q in lanes)


def test_white_up_tablet_lanes_are_clean_and_substantiated():
    graph, lanes = _lanes(_bb_lab_white_up_tablet())
    assert graph["classes"]["format"] == ["tablet"]
    assert not any("powder" in q or "titanium" in q for q in lanes)
    assert any("vitamin c collagen" in q for q in lanes)
    assert all(spec for spec in lanes)


def test_mineral_sunscreen_keeps_titanium_dioxide():
    """Context guard: titanium dioxide IS a real mineral-sunscreen active, so it
    is kept for a sunscreen even though it is stripped for ingestibles."""
    from services.sku_sidewalk import build_sku_attribute_graph
    sun = {
        "title": "Mineral Sunscreen Stick SPF 50",
        "product_type": "sunscreen",
        "attributes_raw": {
            "tags": ["mineral", "reef-safe"],
            "body_html": "<p>Mineral sunscreen with zinc oxide and titanium dioxide.</p>",
        },
    }
    graph = build_sku_attribute_graph(sun)
    assert "titanium dioxide" in (graph["classes"]["ingredient"] or [])


def test_stick_sku_unchanged_regression():
    """The original stick SKU must keep producing clean stick lanes."""
    graph, lanes = _lanes(_bb_lab_product())
    assert graph["classes"]["format"] == ["stick"]
    assert any("collagen sticks" in q for q in lanes)
    assert not any("powder" in q or "tablet" in q for q in lanes)


def test_attribute_graph_drops_promo_terms_from_all_classes():
    """Regression: a promotional merchant tag ("skincare discount") leaked into
    ``attribute_graph.use_case`` on the live DAMDAM audit and surfaced verbatim in
    the merchant-facing brief ("...specific — sensitive skin, skincare discount,
    ..."), which a DTC founder flagged as an outright bug. The same promo gate that
    query generation uses must apply when the attribute graph is built, so promo
    noise never becomes a product attribute in any class."""
    from services.sku_sidewalk import build_sku_attribute_graph
    from services.promo_terms import is_promo_term

    product = {
        "title": "Snow Mushroom Salt Cleanser",
        "product_type": "cleanser",
        "attributes_raw": {
            # promo debris interleaved with real attributes and a merchandising label
            "tags": [
                "skincare discount",
                "on sale",
                "bestseller",
                "sensitive skin",
                "snow fungus",
            ],
        },
    }
    graph = build_sku_attribute_graph(product)

    # The exact leak from the DAMDAM report is gone...
    assert "skincare discount" not in (graph["classes"]["use_case"] or [])
    # ...and no promo term survives in ANY class (single-chokepoint guarantee).
    for class_name, attrs in graph["classes"].items():
        for attr in attrs:
            assert not is_promo_term(attr), (class_name, attr)

    # Real, non-promo attributes that shared the tag list are untouched.
    assert "sensitive skin" in (graph["classes"]["use_case"] or [])


def test_attribute_graph_drops_operational_app_tags():
    """Regression: DAMDAM's PORE CARE RITUAL carried the Shopify tag
    ``exclude_rebuy`` (a Rebuy-upsell-app directive). Live, it became the query
    "best set for rebuy exclude" AND cascaded into a fabricated competitor
    ("AI names Rebuy, not your product", citing rebuyengine.com). Storefront-app
    / catalog-state tags must never become attributes — filtered at the same
    single chokepoint as promo noise."""
    from services.sku_sidewalk import build_sku_attribute_graph
    from services.promo_terms import is_promo_term

    product = {
        "title": "Pore Care Ritual",
        "product_type": "set",
        "attributes_raw": {
            "tags": [
                "exclude_rebuy",  # the exact live leak
                "rebuy exclude",
                "yotpo",
                "preorder",
                "pore care",  # real attribute sharing the tag list
            ],
        },
    }
    graph = build_sku_attribute_graph(product)

    for class_name, attrs in graph["classes"].items():
        for attr in attrs:
            assert not is_promo_term(attr), (class_name, attr)
            assert "rebuy" not in attr and "yotpo" not in attr, (class_name, attr)

    # A real attribute that rode in the same tag list survives.
    assert any("pore care" in v for vs in graph["classes"].values() for v in vs)


def _anuko_ko_product() -> Dict[str, Any]:
    """The live ANUKO SKU as the Korean crawl actually stores it (all-Hangul
    title / product_type / copy) — anukoofficial.com is an all-Korean
    storefront. Audit run bfabfe9c (deployed e61581f5) came out generic because
    the ASCII-only tokenizer stripped every Hangul character, so the graph
    resolved zero attributes and no category."""
    return {
        "title": "아누코 루트 액티베이팅 탈모 볼륨 샴푸",
        "product_type": "샴푸",
        "attributes_raw": {
            "tags": ["비건", "두피"],
            "description": "탈모 케어 나이아신아마이드 두피 샴푸. 비건, 무향.",
        },
    }


def test_korean_catalog_resolves_attributes_and_category():
    """Regression for the i18n gap: an all-Korean SKU must resolve its category
    and evidenced attributes (K-beauty head lexicon) instead of tokenizing to
    nothing. Without this the strategic brief falls back to the literal
    'the evidenced product attributes' and drops to a trust lane."""
    from services.sku_sidewalk import (
        build_sku_attribute_graph,
        generate_sidewalk_query_specs,
    )

    product = _anuko_ko_product()
    graph = build_sku_attribute_graph(product)
    classes = graph["classes"]

    assert classes["category"] == ["shampoo"]
    assert "niacinamide" in classes["ingredient"]
    assert "vegan" in classes["certification_constraint"]
    assert "fragrance-free" in classes["certification_constraint"]

    specs = generate_sidewalk_query_specs(
        graph, title=product["title"], product_type=product["product_type"], n=6
    )
    queries = [spec["query"] for spec in specs]
    assert queries, "Korean SKU should now yield category-lane sidewalk queries"
    assert all("shampoo" in q for q in queries)
    assert any("niacinamide" in q for q in queries)
    # Every emitted query is still ASCII/English — Korean is normalized to the
    # canonical English attribute, so downstream probing/rendering is unchanged.
    assert all(q.isascii() for q in queries)


def test_korean_and_english_serum_reach_parity():
    """A Korean serum should resolve the same category/ingredient/cert lanes as
    the English equivalent (serum was already in the lexicon, so the only thing
    that differed was language)."""
    from services.sku_sidewalk import build_sku_attribute_graph

    ko = build_sku_attribute_graph(
        {
            "title": "글로우 비타민C 세럼",
            "product_type": "세럼",
            "attributes_raw": {
                "tags": ["비건"],
                "description": "나이아신아마이드 세럼. 비건, 무향.",
            },
        }
    )["classes"]

    assert ko["category"] == ["serum"]
    assert "vitamin c" in ko["ingredient"]
    assert "niacinamide" in ko["ingredient"]
    assert "vegan" in ko["certification_constraint"]
    assert "fragrance-free" in ko["certification_constraint"]


def test_ascii_only_catalog_is_unaffected_by_cjk_tokenizer():
    """Widening the tokenizer to preserve CJK must not change extraction for the
    existing English catalog — guards the BB Lab collagen fixture end to end."""
    from services.sku_sidewalk import build_sku_attribute_graph

    classes = build_sku_attribute_graph(_bb_lab_product())["classes"]
    assert "collagen" in classes["category"]
    assert "stick" in classes["format"]
    assert "fish collagen" in classes["ingredient"]
    assert "halal" in classes["certification_constraint"]
    assert "no water" in classes["exclusion"]
