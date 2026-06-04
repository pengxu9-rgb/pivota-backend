from __future__ import annotations

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


def test_per_sku_budget_mix():
    from services.agent_center_bd_report_service import (
        _build_per_sku_audit_query_metadata,
        _build_per_sku_audit_query_specs,
    )

    no_attrs_expected: List[tuple[str, str]] = [
        ("where can I buy BB Lab Good Night Collagen", "intent"),
        ("shop BB Lab Good Night Collagen online", "intent"),
        ("BB Lab Good Night Collagen for sale", "intent"),
        ("best price for BB Lab Good Night Collagen", "price"),
        ("BB Lab Good Night Collagen discount", "price"),
        ("BB Lab Good Night Collagen reviews", "review"),
        ("is BB Lab Good Night Collagen worth it", "review"),
        ("BB Lab Good Night Collagen alternatives", "comparison"),
        ("BB Lab Good Night Collagen vs competitors", "comparison"),
        (
            "best collagen supplement for shoppers considering "
            "BB Lab Good Night Collagen",
            "category",
        ),
        ("top collagen supplement like BB Lab Good Night Collagen", "category"),
        ("what is the best collagen supplement to buy online", "category"),
        ("BB Lab Good Night Collagen", "brand"),
        ("buy BB Lab collagen supplement online", "brand"),
    ]

    no_attrs = _build_per_sku_audit_query_specs(_sku_ctx(attributes=False), 14)
    assert no_attrs == no_attrs_expected

    wedge = _build_per_sku_audit_query_specs(_sku_ctx(attributes=True), 14)
    assert len(wedge) == 14
    assert 4 <= sum(1 for _query, axis in wedge if axis == "sidewalk") <= 6
    assert any(query == "collagen stick no water travel" for query, _axis in wedge)

    metadata = _build_per_sku_audit_query_metadata(_sku_ctx(attributes=True), 14)
    assert metadata["collagen stick no water travel"]["attribute_basis"]
    assert metadata["collagen stick no water travel"]["evidence"]

    large = _build_per_sku_audit_query_specs(_sku_ctx(attributes=True), 40)
    assert len(large) == 40
    assert sum(1 for _query, axis in large if axis == "sidewalk") > 0
    for spec in no_attrs_expected:
        assert spec in large


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
