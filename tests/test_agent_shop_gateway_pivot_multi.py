from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import BackgroundTasks

import routes.agent_shop_gateway as gateway
from models.catalog import (
    MerchantNode,
    OfferNode,
    PivotPricing,
    PivotQueryResponse,
    PivotResultItem,
    ProductNode,
    SkuNode,
)


def _sample_pivot_item(
    *,
    sku_key: str,
    variant_id: str,
    sku: str,
    title: str,
    product_id: str = "111",
    product_type: str = "serum",
    description: str = "A brightening serum",
    canonical_url: str = "https://merchant.example/products/111",
    visible_attributes: dict | None = None,
    visible_option_labels: list[str] | None = None,
    ingredient_ids: list[str] | None = None,
) -> PivotResultItem:
    return PivotResultItem(
        merchant=MerchantNode(
            merchant_id="merch_1",
            merchant_name="Demo Merchant",
            primary_platform="shopify",
        ),
        product=ProductNode(
            product_key=f"prod::merch_1::shopify::{product_id}",
            source_product_id=product_id,
            title=title,
            description=description,
            brand="Demo",
            product_type=product_type,
            category=product_type,
            canonical_url=canonical_url,
            image_url="https://merchant.example/image.jpg",
        ),
        sku=SkuNode(
            sku_key=sku_key,
            source_variant_id=variant_id,
            sku=sku,
            title=f"{title} {sku}",
            visible_attributes={"size": ["30ml"]} if visible_attributes is None else visible_attributes,
            visible_option_labels=visible_option_labels or [],
            ingredient_ids=["vitamin_c"] if ingredient_ids is None else ingredient_ids,
        ),
        offers=[
            OfferNode(
                offer_id=f"offer::{sku_key}",
                catalog_track="internal_merchant",
                truth_tier="primary",
                readiness_tier="knowledge_ready",
                offer_mode="merchant_checkout",
                source_system="shopify_products_sync",
                availability="in_stock",
                inventory_quantity=5,
                pricing=PivotPricing(
                    currency="USD",
                    list_price=Decimal("32.00"),
                    merchant_effective_price=Decimal("29.00"),
                    estimated_best_price=Decimal("27.55"),
                    price_confidence=Decimal("1.0"),
                ),
                incentives=[],
            )
        ],
        catalog_track="internal_merchant",
        truth_tier="primary",
        readiness_tier="knowledge_ready",
        freshness={"updated_at": "2026-03-28T00:00:00Z"},
        source_system="shopify_products_sync",
        match_explanation={"lane": "catalog_discovery", "exact_match": False},
        verticals={"beauty": {"ingredients": ["vitamin_c"]}},
    )


def test_pivot_multi_rollout_allowed_is_guarded_by_source_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST", {"shopping_agent", "aurora"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)

    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="shopping_agent",
        page=1,
        mode="shadow",
    ) is True
    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="aurora",
        page=1,
        mode="shadow",
    ) is True
    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="creator-agent-ui",
        page=1,
        mode="shadow",
    ) is False

    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="shopping_agent",
        page=1,
        mode="serve",
    ) is True
    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="shopping_agent",
        page=2,
        mode="serve",
    ) is False
    assert gateway._pivot_multi_rollout_allowed(
        source_normalized="aurora",
        page=1,
        mode="serve",
    ) is False


def test_search_price_contract_requires_a_currency_qualified_price_or_offer() -> None:
    assert gateway._has_canonical_price_or_offer(
        {"price": {"current": {"amount": 24, "currency": "USD"}}}
    ) is True
    assert gateway._has_canonical_price_or_offer(
        {
            "price": 0,
            "offers": [{"price": {"amount": 28, "currency": "EUR"}}],
        }
    ) is True
    assert gateway._has_canonical_price_or_offer(
        {"price": 28, "currency": None}
    ) is False
    assert gateway._has_canonical_price_or_offer(
        {"price": 0, "currency": "USD"}
    ) is False


def test_search_price_contract_removes_unpriced_cards_and_records_the_drop() -> None:
    result = {
        "products": [
            {"product_id": "priced", "price": 18, "currency": "USD"},
            {"product_id": "unpriced", "price": 0, "currency": "USD"},
        ],
        "total": 2,
    }

    gateway._enforce_search_price_contract(result)

    assert [product["product_id"] for product in result["products"]] == ["priced"]
    assert result["page_size"] == 1
    assert result["total"] == 1
    assert result["metadata"]["price_contract"] == {
        "canonical_price_or_offer_required": True,
        "dropped_unpriced": 1,
    }


@pytest.mark.asyncio
async def test_handle_find_products_multi_can_serve_from_pivot_semantic_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    async def fake_search(req):
        observed["include_external"] = req.include_external
        observed["include_incentives"] = req.include_incentives
        return PivotQueryResponse(
            query="vitamin c",
            total=2,
            items=[
                _sample_pivot_item(
                    sku_key="sku::1",
                    variant_id="var_1",
                    sku="SKU-1",
                    title="Vitamin C Serum",
                ),
                _sample_pivot_item(
                    sku_key="sku::2",
                    variant_id="var_2",
                    sku="SKU-2",
                    title="Vitamin C Serum",
                ),
            ],
        )

    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", False)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST", {"shopping_agent"})
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_MAX_PAGE", 1)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_INCLUDE_EXTERNAL", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SERVE_INCLUDE_INCENTIVES", False)
    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(
            query="vitamin c",
            page=1,
            limit=10,
            in_stock_only=False,
        ),
        metadata=gateway.RequestMetadata(source="shopping_agent"),
    )

    result = await gateway._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        BackgroundTasks(),
    )

    assert result["metadata"]["query_source"] == "pivot_semantic_core_multi"
    assert observed["include_external"] is True
    assert observed["include_incentives"] is False
    assert result["total"] == 1
    assert result["page_size"] == 1
    assert len(result["products"]) == 1
    product = result["products"][0]
    assert product["product_id"] == "111"
    assert product["merchant_id"] == "merch_1"
    assert product["title"] == "Vitamin C Serum"
    assert product["inventory_quantity"] == 10
    assert len(product["variants"]) == 2
    assert product["best_deal"]["estimated_best_price"] == Decimal("27.55")


def test_build_pivot_multi_shadow_diff_summary_reports_overlap() -> None:
    served_result = {
        "products": [
            {
                "merchant_id": "merch_1",
                "product_id": "111",
                "title": "Vitamin C Serum",
                "catalog_track": "internal_merchant",
            },
            {
                "merchant_id": "merch_2",
                "product_id": "222",
                "title": "Brightening Mist",
                "catalog_track": "external_referral",
            },
        ],
        "metadata": {
            "query_source": "cache_multi_intent",
        },
    }
    pivot_result = {
        "products": [
            {
                "merchant_id": "merch_1",
                "product_id": "111",
                "title": "Vitamin C Serum",
                "catalog_track": "internal_merchant",
            },
            {
                "merchant_id": "merch_3",
                "product_id": "333",
                "title": "Glow Oil",
                "catalog_track": "external_referral",
            },
        ],
        "metadata": {
            "query_source": "pivot_semantic_core_multi",
        },
    }

    summary = gateway._build_pivot_multi_shadow_diff_summary(served_result, pivot_result)

    assert summary["pivot_shadow_attempted"] is True
    assert summary["served_query_source"] == "cache_multi_intent"
    assert summary["served_returned_count"] == 2
    assert summary["served_internal_count"] == 1
    assert summary["served_external_count"] == 1
    assert summary["pivot_shadow_returned_count"] == 2
    assert summary["pivot_shadow_overlap_count"] == 1
    assert summary["pivot_shadow_overlap_ratio"] == 0.5
    assert summary["pivot_shadow_top1_same"] is True
    assert summary["pivot_shadow_internal_count"] == 1
    assert summary["pivot_shadow_external_count"] == 1
    assert summary["pivot_shadow_returned_count_delta"] == 0
    assert summary["pivot_shadow_internal_share_delta"] == 0.0
    assert summary["pivot_shadow_external_share_delta"] == 0.0
    assert summary["pivot_shadow_no_result_mismatch"] is False
    assert summary["pivot_shadow_query_source"] == "pivot_semantic_core_multi"


def test_build_pivot_multi_shadow_diff_summary_treats_same_url_as_same_product() -> None:
    served_result = {
        "products": [
            {
                "merchant_id": "merch_1",
                "product_id": "seed_111",
                "title": "Vitamin C Serum",
                "canonical_url": "https://merchant.example/products/vitamin-c-serum",
                "catalog_track": "external_referral",
            }
        ],
        "metadata": {"query_source": "cache_multi_intent"},
    }
    pivot_result = {
        "products": [
            {
                "merchant_id": "merch_1",
                "product_id": "stable_abc",
                "title": "Vitamin C Serum",
                "canonical_url": "https://merchant.example/products/vitamin-c-serum",
                "catalog_track": "external_referral",
            }
        ],
        "metadata": {"query_source": "pivot_semantic_core_multi"},
    }

    summary = gateway._build_pivot_multi_shadow_diff_summary(served_result, pivot_result)

    assert summary["pivot_shadow_overlap_count"] == 1
    assert summary["pivot_shadow_overlap_ratio"] == 1.0
    assert summary["pivot_shadow_top1_same"] is True


def test_build_pivot_multi_shadow_diff_summary_treats_both_empty_as_match() -> None:
    served_result = {
        "products": [],
        "metadata": {"query_source": "cache_multi_intent"},
    }
    pivot_result = {
        "products": [],
        "metadata": {"query_source": "pivot_semantic_core_multi"},
    }

    summary = gateway._build_pivot_multi_shadow_diff_summary(served_result, pivot_result)

    assert summary["pivot_shadow_overlap_count"] == 0
    assert summary["pivot_shadow_overlap_ratio"] == 1.0
    assert summary["pivot_shadow_top1_same"] is True
    assert summary["pivot_shadow_no_result_mismatch"] is False
    assert summary["pivot_shadow_returned_count_delta"] == 0


@pytest.mark.asyncio
async def test_handle_find_products_multi_via_pivot_applies_ingredient_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_req):
        return PivotQueryResponse(
            query="niacinamide serum",
            total=2,
            items=[
                _sample_pivot_item(
                    sku_key="sku::niacinamide",
                    variant_id="var_1",
                    sku="SKU-1",
                    title="Niacinamide Serum",
                    visible_attributes={"product_category": ["serum"]},
                    ingredient_ids=["niacinamide"],
                ),
                _sample_pivot_item(
                    sku_key="sku::retinol",
                    variant_id="var_2",
                    sku="SKU-2",
                    title="Retinol Serum",
                    product_id="222",
                    canonical_url="https://merchant.example/products/222",
                    visible_attributes={"product_category": ["serum"]},
                    ingredient_ids=["retinol"],
                ),
            ],
        )

    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    result = await gateway._handle_find_products_multi_via_pivot(
        gateway.FindProductsMultiPayload(
            search=gateway.MultiSearchFilters(query="niacinamide serum", page=1, limit=10),
            metadata=gateway.RequestMetadata(source="shopping_agent"),
        ),
        {"source": "shopping_agent"},
    )

    assert result is not None
    assert result["total"] == 1
    assert result["products"][0]["title"] == "Niacinamide Serum"
    assert result["metadata"]["query_semantic_class"] == "beauty"


@pytest.mark.asyncio
async def test_handle_find_products_multi_via_pivot_allows_text_ingredient_match_without_structured_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_req):
        return PivotQueryResponse(
            query="hyaluronic acid hydrating serum",
            total=1,
            items=[
                _sample_pivot_item(
                    sku_key="sku::ha",
                    variant_id="var_1",
                    sku="SKU-1",
                    title="Barrier Repair Serum",
                    product_type="serum",
                    description="Hydrating hyaluronic acid serum for dry skin.",
                    visible_attributes={},
                    ingredient_ids=[],
                )
            ],
        )

    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    result = await gateway._handle_find_products_multi_via_pivot(
        gateway.FindProductsMultiPayload(
            search=gateway.MultiSearchFilters(
                query="hyaluronic acid hydrating serum",
                page=1,
                limit=10,
            ),
            metadata=gateway.RequestMetadata(source="shopping_agent"),
        ),
        {"source": "shopping_agent"},
    )

    assert result is not None
    assert result["total"] == 1
    assert result["products"][0]["title"] == "Barrier Repair Serum"


@pytest.mark.asyncio
async def test_handle_find_products_multi_via_pivot_applies_shade_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_req):
        return PivotQueryResponse(
            query="foundation shade warm",
            total=2,
            items=[
                _sample_pivot_item(
                    sku_key="sku::warm",
                    variant_id="var_1",
                    sku="SKU-1",
                    title="Silk Foundation",
                    product_type="foundation",
                    visible_option_labels=["shade_warm"],
                ),
                _sample_pivot_item(
                    sku_key="sku::cool",
                    variant_id="var_2",
                    sku="SKU-2",
                    title="Silk Foundation",
                    product_id="222",
                    product_type="foundation",
                    canonical_url="https://merchant.example/products/222",
                    visible_option_labels=["shade_cool"],
                ),
            ],
        )

    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    result = await gateway._handle_find_products_multi_via_pivot(
        gateway.FindProductsMultiPayload(
            search=gateway.MultiSearchFilters(query="foundation shade warm", page=1, limit=10),
            metadata=gateway.RequestMetadata(source="shopping_agent"),
        ),
        {"source": "shopping_agent"},
    )

    assert result is not None
    assert result["total"] == 1
    assert result["products"][0]["product_type"] == "foundation"
    assert result["products"][0]["variants"][0]["options"]["visible_option_labels"] == ["shade_warm"]


@pytest.mark.asyncio
async def test_handle_find_products_multi_via_pivot_allows_foundation_without_explicit_shade_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(_req):
        return PivotQueryResponse(
            query="foundation",
            total=1,
            items=[
                _sample_pivot_item(
                    sku_key="sku::foundation",
                    variant_id="var_1",
                    sku="SKU-1",
                    title="Silk Foundation",
                    product_type="foundation",
                    visible_option_labels=[],
                )
            ],
        )

    monkeypatch.setattr(gateway, "search_pivot_catalog", fake_search)

    result = await gateway._handle_find_products_multi_via_pivot(
        gateway.FindProductsMultiPayload(
            search=gateway.MultiSearchFilters(query="foundation", page=1, limit=10),
            metadata=gateway.RequestMetadata(source="shopping_agent"),
        ),
        {"source": "shopping_agent"},
    )

    assert result is not None
    assert result["total"] == 1
    assert result["products"][0]["product_type"] == "foundation"


def test_maybe_schedule_pivot_multi_shadow_compare_is_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_ENABLED", True)
    monkeypatch.setattr(gateway, "PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST", {"shopping_agent"})

    payload = gateway.FindProductsMultiPayload(
        search=gateway.MultiSearchFilters(
            query="vitamin c",
            page=1,
            limit=10,
        ),
        metadata=gateway.RequestMetadata(source="shopping_agent"),
    )
    served_result = {
        "products": [
            {
                "merchant_id": "merch_1",
                "product_id": "111",
                "title": "Vitamin C Serum",
            }
        ],
        "metadata": {
            "query_source": "cache_multi_intent",
        },
    }

    background_tasks = BackgroundTasks()
    scheduled = gateway._maybe_schedule_pivot_multi_shadow_compare(
        payload=payload,
        request_metadata={"source": "shopping_agent"},
        background_tasks=background_tasks,
        served_result=served_result,
        source_normalized="shopping_agent",
        page=1,
    )

    assert scheduled is True
    assert len(background_tasks.tasks) == 1

    pivot_served_result = {
        "products": served_result["products"],
        "metadata": {
            "query_source": "pivot_semantic_core_multi",
        },
    }
    blocked_tasks = BackgroundTasks()
    blocked = gateway._maybe_schedule_pivot_multi_shadow_compare(
        payload=payload,
        request_metadata={"source": "shopping_agent"},
        background_tasks=blocked_tasks,
        served_result=pivot_served_result,
        source_normalized="shopping_agent",
        page=1,
    )

    assert blocked is False
    assert len(blocked_tasks.tasks) == 0


def test_normalize_gateway_route_health_carries_pivot_shadow_fields() -> None:
    metadata = gateway._normalize_gateway_route_health(
        {
            "query_source": "cache_multi_intent",
            "pivot_shadow_scheduled": True,
            "pivot_shadow_mode": "background_compare",
            "pivot_rollout_mode": "shadow",
            "pivot_rollout_guard_passed": True,
        },
        default_decision_node="cache_multi_intent",
    )

    assert metadata["pivot_shadow_scheduled"] is True
    assert metadata["pivot_shadow_mode"] == "background_compare"
    assert metadata["pivot_rollout_mode"] == "shadow"
    assert metadata["pivot_rollout_guard_passed"] is True
    assert metadata["route_health"]["pivot_shadow_scheduled"] is True
    assert metadata["route_health"]["pivot_shadow_mode"] == "background_compare"
    assert metadata["route_health"]["pivot_rollout_mode"] == "shadow"
    assert metadata["route_health"]["pivot_rollout_guard_passed"] is True


def test_apply_pivot_rollout_metadata_preserves_serve_for_pivot_primary_path() -> None:
    metadata = gateway._apply_pivot_rollout_metadata(
        {
            "query_source": "pivot_semantic_core_multi",
            "pivot_rollout_mode": "serve",
            "pivot_rollout_guard_passed": True,
        },
        pivot_shadow_scheduled=False,
    )

    assert metadata["pivot_shadow_scheduled"] is False
    assert metadata.get("pivot_shadow_mode") is None
    assert metadata["pivot_rollout_mode"] == "serve"
    assert metadata["pivot_rollout_guard_passed"] is True
