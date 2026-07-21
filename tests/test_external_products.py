import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request


from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _patch_agent_sdk_ranking(monkeypatch: pytest.MonkeyPatch, module) -> None:
    monkeypatch.setattr(module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "log_ranking_batch", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(module, "log_product_events", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_agent_api_load_external_seed_products_builds_without_allowlist_prefetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seed_rows = [
        {
            "id": "seed_1",
            "external_product_id": "ext_1",
            "destination_url": "https://example.com/p/1",
            "canonical_url": "https://example.com/p/1",
            "seed_data": {},
        },
        {
            "id": "seed_2",
            "external_product_id": "ext_2",
            "destination_url": "https://example.com/p/2",
            "canonical_url": "https://example.com/p/2",
            "seed_data": {},
        },
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return seed_rows
        return []

    build_seen_allowed = []

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None, metrics_out=None):
        build_seen_allowed.append(list(allowed_domains or []))
        return {
            "id": seed_row.get("external_product_id"),
            "product_id": seed_row.get("external_product_id"),
            "merchant_id": "external_seed",
            "source": "external_seed",
        }

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,  # req is unused by this test double.
        query="example",
        limit=10,
        build_budget_ms=1000,
    )

    assert len(products) == 2
    assert all(item == [] for item in build_seen_allowed)


@pytest.mark.asyncio
async def test_agent_api_load_external_seed_products_records_build_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seed_rows = [
        {
            "id": "seed_1",
            "external_product_id": "ext_1",
            "destination_url": "https://example.com/p/1",
            "canonical_url": "https://example.com/p/1",
            "seed_data": {},
        }
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return seed_rows
        return []

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None, metrics_out=None):
        raise ValueError("bad seed payload")

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="example",
        limit=10,
        build_budget_ms=1000,
        metrics_out=metrics,
    )

    assert products == []
    assert metrics.get("build_drop_reasons", {}) == {}
    assert metrics["build_exception_reasons"]["valueerror"] == 1
    assert metrics.get("candidate_rows") == 1
    assert metrics.get("build_tasks_started") == 1
    assert metrics.get("build_deadline_skips") == 0
    assert int(metrics.get("rows_built") or 0) == 0


@pytest.mark.asyncio
async def test_agent_api_load_external_seed_products_records_null_builder_results_without_drop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seed_rows = [
        {
            "id": "seed_1",
            "external_product_id": "ext_1",
            "destination_url": "https://example.com/p/1",
            "canonical_url": "https://example.com/p/1",
            "seed_data": {},
        }
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return seed_rows
        return []

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None, metrics_out=None):
        return None

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="sunscreen",
        limit=10,
        build_budget_ms=1000,
        metrics_out=metrics,
    )

    assert products == []
    assert metrics.get("build_drop_reasons", {}) == {}
    assert metrics.get("build_exception_reasons", {}) == {}
    assert metrics.get("build_null_reasons", {}) == {"null_without_drop_reason": 1}


@pytest.mark.asyncio
async def test_agent_api_build_external_seed_product_skips_blocked_referral_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    async def fake_gate(*args, **kwargs):
        return True, type("GateStatus", (), {"blocker_anomaly_types": ["stale_snapshot"]})()

    monkeypatch.setattr(agent_api_module, "should_block_external_referral_runtime", fake_gate)

    metrics = {}
    product = await agent_api_module._build_external_seed_product(
        req=type("Req", (), {"base_url": "https://agent.pivota.cc/"})(),
        seed_row={
            "id": "seed_1",
            "external_product_id": "ext_1",
            "market": "US",
            "tool": "*",
            "destination_url": "https://example.com/p/1",
            "canonical_url": "https://example.com/p/1",
            "seed_data": {},
        },
        allowed_domains=[],
        metrics_out=metrics,
    )

    assert product is None
    assert metrics["build_drop_reasons"]["blocked_referral_runtime"] == 1


@pytest.mark.asyncio
async def test_agent_api_build_external_seed_product_uses_canonical_url_when_destination_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    metrics = {}
    product = await agent_api_module._build_external_seed_product(
        req=type("Req", (), {"base_url": "https://agent.pivota.cc/"})(),
        seed_row={
            "id": "seed_2",
            "external_product_id": "ext_2",
            "market": "US",
            "tool": "*",
            "destination_url": "",
            "canonical_url": "https://example.com/p/2",
            "seed_data": {"title": "Fallback Canonical Product"},
        },
        allowed_domains=[],
        metrics_out=metrics,
    )

    assert product is not None
    assert product["destination_url"] == "https://example.com/p/2"
    assert product["external_url"] == "https://example.com/p/2"
    assert "/r?token=" in product["external_redirect_url"]
    assert metrics.get("build_drop_reasons", {}) == {}


@pytest.mark.asyncio
async def test_agent_api_build_external_seed_product_uses_snapshot_fields_when_top_level_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    product = await agent_api_module._build_external_seed_product(
        req=type("Req", (), {"base_url": "https://agent.pivota.cc/"})(),
        seed_row={
            "id": "seed_snapshot_1",
            "external_product_id": "",
            "market": "US",
            "tool": "*",
            "destination_url": "",
            "canonical_url": "",
            "seed_data": {
                "snapshot": {
                    "external_product_id": "ext_snapshot_1",
                    "canonical_url": "https://example.com/p/snapshot-1",
                    "title": "Snapshot Product",
                    "product_type": "sunscreen",
                    "category": "face sunscreen",
                }
            },
        },
        allowed_domains=[],
        metrics_out={},
    )

    assert product is not None
    assert product["product_id"] == "ext_snapshot_1"
    assert product["destination_url"] == "https://example.com/p/snapshot-1"
    assert product["title"] == "Snapshot Product"
    assert product["product_type"] == "sunscreen"


def test_dedupe_external_seed_rows_uses_snapshot_identity_when_top_level_missing() -> None:
    from services.external_seed_search import dedupe_external_seed_rows

    rows = [
        {
            "id": "seed_snapshot_1",
            "external_product_id": "",
            "canonical_url": "",
            "destination_url": "",
            "seed_data": {
                "snapshot": {
                    "external_product_id": "ext_snapshot_1",
                    "canonical_url": "https://example.com/p/snapshot-1",
                }
            },
        }
    ]

    deduped = dedupe_external_seed_rows(rows, limit=10)

    assert len(deduped) == 1
    assert deduped[0]["id"] == "seed_snapshot_1"


@pytest.mark.asyncio
async def test_agent_api_load_external_seed_products_records_builder_exception_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seed_rows = [
        {
            "id": "seed_1",
            "external_product_id": "ext_1",
            "destination_url": "https://example.com/p/1",
            "canonical_url": "https://example.com/p/1",
            "seed_data": {},
        }
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return seed_rows
        return []

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None, metrics_out=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="sunscreen",
        limit=10,
        build_budget_ms=1000,
        metrics_out=metrics,
    )

    assert products == []
    assert metrics.get("build_drop_reasons", {}) == {}
    assert metrics.get("build_exception_reasons", {}) == {"runtimeerror": 1}
    assert metrics.get("candidate_rows") == 1
    assert metrics.get("build_tasks_started") == 1
    assert metrics.get("build_deadline_skips") == 0


@pytest.mark.asyncio
async def test_shop_gateway_make_external_redirect_url_without_allowlist_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    redirect = await agent_shop_gateway_module._make_external_redirect_url(
        market="US",
        tool="*",
        destination_url="https://example.com/p/1",
        utm_template=None,
        ctx={"seedId": "seed_1"},
        allowed_domains=["example.com"],
    )

    assert isinstance(redirect, str)
    assert "/r?token=" in redirect


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_strict_surface_returns_only_internal_eligible_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_live_1",
                    "external_product_id": "ext_live_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": "https://example.com/product/1",
                    "domain": "example.com",
                    "title": "External Product",
                    "price_amount": 19.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {},
                    "status": "active",
                }
            ]
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_live_1",
                    "platform": "shopify",
                    "platform_product_id": "prod_live_1",
                    "product_data": {
                        "id": "prod_live_1",
                        "platform": "shopify",
                        "merchant_id": "merch_live_1",
                        "title": "Eligible Internal Product",
                        "description": "Internal item",
                        "vendor": "Pivota",
                        "product_type": "serum",
                        "tags": [],
                        "price": 29.0,
                        "currency": "USD",
                        "inventory_quantity": 8,
                        "image_url": "https://cdn.example.com/internal.jpg",
                        "status": "active",
                        "orderable": True,
                        "variants": [
                            {
                                "id": "var_live_1",
                                "sku": "sku_live_1",
                                "title": "Default",
                                "price": 29.0,
                                "inventory_quantity": 8,
                            }
                        ],
                    },
                }
            ]
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="serum",
            page=1,
            limit=10,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_live_1"
    assert products[0].get("commerce_surface") == "agent_api"
    assert products[0]["top_offer_summary"]["purchase_route"] == "internal_checkout"
    assert products[0]["exact_resolution_identifiers"]["variant_id"] == "var_live_1"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_empty_query_live_fallback_keeps_strict_serving_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        product = agent_shop_gateway_module.StandardProduct(
            id="prod_live_1",
            product_id="prod_live_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Eligible Fallback Product",
            description="Internal eligible fallback item",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            variants=[
                {
                    "id": "var_live_1",
                    "sku": "sku_live_1",
                    "title": "Default",
                    "price": 29.0,
                    "inventory_quantity": 8,
                }
            ],
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_live_1"
    assert products[0].get("commerce_surface") == "agent_api"
    assert products[0]["top_offer_summary"]["purchase_route"] == "internal_checkout"
    assert metadata.get("query_source") == "live_merchant_fallback"
    assert metadata.get("commerce_surface") == "agent_api"
    assert metadata.get("serving_mode") == "eligible_only"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_strict_surface_prefetches_full_merchant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    captured_prefetch_values = {}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [
                {"merchant_id": "merch_test_1", "business_name": "Test Merchant 1"},
                {"merchant_id": "merch_test_2", "business_name": "Test Merchant 2"},
                {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
            ]
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q:
            captured_prefetch_values["merchant_ids"] = list((values or {}).get("merchant_ids") or [])
            return [
                {
                    "merchant_id": "merch_live_1",
                    "platform": "shopify",
                    "platform_product_id": "prod_live_1",
                    "product_data": {
                        "id": "prod_live_1",
                        "platform": "shopify",
                        "merchant_id": "merch_live_1",
                        "title": "Winona Soothing Repair Serum",
                        "description": "Internal item",
                        "vendor": "Winona",
                        "product_type": "serum",
                        "tags": [],
                        "price": 29.0,
                        "currency": "USD",
                        "inventory_quantity": 8,
                        "image_url": "https://cdn.example.com/internal.jpg",
                        "status": "active",
                        "orderable": True,
                        "variants": [
                            {
                                "id": "var_live_1",
                                "sku": "WINONA-SOOTHING-REPAIR-SERUM",
                                "title": "Default",
                                "price": 29.0,
                                "inventory_quantity": 8,
                            }
                        ],
                    },
                }
            ]
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT", 1)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="winona",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert captured_prefetch_values["merchant_ids"] == [
        "merch_test_1",
        "merch_test_2",
        "merch_live_1",
    ]
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_live_1"
    assert products[0].get("commerce_surface") == "agent_api"
    assert products[0]["top_offer_summary"]["purchase_route"] == "internal_checkout"
    assert metadata.get("query_source") == "cache_multi_intent"
    assert metadata.get("commerce_surface") == "agent_api"
    assert metadata.get("serving_mode") == "eligible_only"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_strict_surface_uses_live_query_fallback_when_prefetch_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_test_1", "business_name": "Test Merchant 1"},
        {"merchant_id": "merch_test_2", "business_name": "Test Merchant 2"},
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    calls: list[dict[str, Any]] = []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        calls.append(
            {
                "merchant_id": merchant_id,
                "limit": limit,
                "agent_id": agent_id,
                "force_cache_only": force_cache_only,
            }
        )
        if merchant_id != "merch_live_1":
            return [], "cache_all_platforms", None
        product = agent_shop_gateway_module.StandardProduct(
            id="prod_live_1",
            product_id="prod_live_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            description="Internal item",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_live_1",
                    sku="WINONA-SOOTHING-REPAIR-SERUM",
                    title="Default",
                    price=29.0,
                    inventory_quantity=8,
                )
            ],
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT", 1)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="winona",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert [call["merchant_id"] for call in calls] == [
        "merch_test_1",
        "merch_test_2",
        "merch_live_1",
    ]
    assert all(call["force_cache_only"] is True for call in calls)
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_live_1"
    assert products[0]["top_offer_summary"]["purchase_route"] == "internal_checkout"
    assert metadata.get("strict_live_query_fallback_used") is True
    assert metadata.get("merchants_scanned") == 3
    assert metadata.get("commerce_surface") == "agent_api"
    assert metadata.get("serving_mode") == "eligible_only"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_pet_accessory_query_fails_closed_when_only_description_mentions_accessory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        product = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_1",
            product_id="prod_vest_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Leash-friendly detail for hassle-free walks (works with most harnesses).",
            price=24.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="cat harness",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("page_size") == 0
    assert result.get("products") == []
    assert "eligible pet accessory" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("pet_accessory_intent_query") is True
    assert metadata.get("strict_live_query_fallback_used") is True


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_pet_accessory_query_prefers_actual_accessory_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        vest = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_1",
            product_id="prod_vest_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Pet apparel only",
            price=24.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        harness = agent_shop_gateway_module.StandardProduct(
            id="prod_harness_1",
            product_id="prod_harness_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Reflective Escape-Proof Cat Harness",
            description="Secure walking harness for cats",
            price=31.0,
            currency="USD",
            inventory_quantity=6,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [vest, harness], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="cat harness",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_harness_1"
    assert "harness" in str(products[0]["title"] or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("pet_accessory_intent_query") is True


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_category_intent_prefers_serum_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        apparel = agent_shop_gateway_module.StandardProduct(
            id="prod_apparel_1",
            product_id="prod_apparel_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Soft-touch fabric for all-day comfort on sensitive skin.",
            product_type="Padded Vest",
            price=24.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Hydrating Serum for Sensitive Skin",
            description="Repair-focused serum.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [apparel, serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hydrating serum for sensitive skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_serum_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("visible_attribute_intents") == ["sensitive_skin", "hydrating"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_beauty_query_filters_pet_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        pet_vest = agent_shop_gateway_module.StandardProduct(
            id="prod_pet_vest_1",
            product_id="prod_pet_vest_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Soft-touch fabric for all-day comfort on sensitive skin.",
            product_type="Padded Winter Vest",
            price=24.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [pet_vest], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="oil free sunscreen",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "beauty"
    assert metadata.get("beauty_pet_noise_filtered_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_beauty_query_filters_sleepwear_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        sleepwear = agent_shop_gateway_module.StandardProduct(
            id="prod_sleepwear_1",
            product_id="prod_sleepwear_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Velvet Padded Deep V women's sleepwear set 6271",
            description="Romantic lounge set with robe and slip dress.",
            product_type="Women's Sleepwear Set",
            price=23.68,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [sleepwear], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="overnight mask for dry skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "beauty"
    assert metadata.get("beauty_apparel_noise_filtered_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_skin_care_category_intent_keeps_matching_moisturizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        moisturizer = agent_shop_gateway_module.StandardProduct(
            id="prod_moisturizer_1",
            product_id="prod_moisturizer_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Daily Moisturizer",
            description="Barrier moisturizer.",
            product_type="Moisturizer",
            tags=["hydrating"],
            price=31.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            description="Repair-focused serum.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [moisturizer, serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="moisturizer",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_moisturizer_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["moisturizer"]
    assert metadata.get("matched_visible_attributes") == {
        "product_category": ["moisturizer"],
    }


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_non_strict_beauty_prefetch_expands_scope_and_uses_text_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_test_1", "business_name": "Test Merchant 1"},
        {"merchant_id": "merch_test_2", "business_name": "Test Merchant 2"},
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]
    captured_prefetch_values: dict[str, Any] = {}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            captured_prefetch_values["merchant_ids"] = list((values or {}).get("merchant_ids") or [])
            return [
                {
                    "merchant_id": "merch_live_1",
                    "product_data": {
                        "id": "prod_moisturizer_live",
                        "product_id": "prod_moisturizer_live",
                        "platform": "shopify",
                        "merchant_id": "merch_live_1",
                        "title": "Barrier Repair Cream",
                        "description": "A barrier moisturizer for dry skin.",
                        "product_type": "Face Cream",
                        "price": 29.0,
                        "currency": "USD",
                        "inventory_quantity": 8,
                        "image_url": "https://cdn.example.com/moisturizer.jpg",
                        "status": "active",
                        "orderable": True,
                        "variants": [],
                    },
                }
            ]
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT", 1)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="moisturizer",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert captured_prefetch_values["merchant_ids"] == [
        "merch_test_1",
        "merch_test_2",
        "merch_live_1",
    ]
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_moisturizer_live"
    assert metadata.get("expanded_shopping_beauty_prefetch") is True
    assert metadata.get("non_strict_beauty_text_recall_enabled") is True
    assert metadata.get("non_strict_beauty_text_recall_used") is True
    assert metadata.get("visible_category_intents") == ["moisturizer"]
    assert metadata.get("matched_visible_attributes") == {}


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_non_strict_beauty_text_matches_ingredient_and_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_ha",
            product_id="prod_serum_ha",
            platform="shopify",
            merchant_id=merchant_id,
            title="Barrier Repair Serum",
            description="Hydrating hyaluronic acid serum for dry and sensitive skin.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hyaluronic acid hydrating serum",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_serum_ha"
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("visible_attribute_intents") == ["hydrating"]
    assert metadata.get("ingredient_intents") == ["hyaluronic_acid"]
    assert metadata.get("matched_ingredient_ids") == ["hyaluronic_acid"]
    assert metadata.get("matched_ingredient_labels") == ["Hyaluronic Acid"]
    assert metadata.get("non_strict_beauty_text_recall_used") is True


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_non_strict_foundation_does_not_require_explicit_shade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        foundation = agent_shop_gateway_module.StandardProduct(
            id="prod_foundation_1",
            product_id="prod_foundation_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Soft Focus Foundation",
            product_type="Foundation",
            price=39.0,
            currency="USD",
            inventory_quantity=6,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_foundation_210",
                    title="Shade 210 Neutral Beige",
                    price=39.0,
                    inventory_quantity=6,
                    options={"Shade": "210"},
                )
            ],
        )
        return [foundation], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="foundation",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_foundation_1"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_non_strict_beauty_uses_live_query_fallback_when_cache_prefetch_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_test_1", "business_name": "Test Merchant 1"},
        {"merchant_id": "merch_test_2", "business_name": "Test Merchant 2"},
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    calls: list[dict[str, Any]] = []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        calls.append(
            {
                "merchant_id": merchant_id,
                "limit": limit,
                "agent_id": agent_id,
                "force_cache_only": force_cache_only,
            }
        )
        if merchant_id != "merch_live_1":
            return [], "cache_all_platforms", None
        product = agent_shop_gateway_module.StandardProduct(
            id="prod_live_1",
            product_id="prod_live_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Barrier Repair Moisturizer",
            description="A soothing barrier moisturizer.",
            product_type="Moisturizer",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_live_1",
                    sku="BARRIER-REPAIR-MOISTURIZER",
                    title="Default",
                    price=29.0,
                    inventory_quantity=8,
                )
            ],
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SHOPPING_FAST_MERCHANT_SEED_LIMIT", 1)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="moisturizer",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert [call["merchant_id"] for call in calls] == [
        "merch_test_1",
        "merch_test_2",
        "merch_live_1",
    ]
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_live_1"
    assert metadata.get("beauty_live_query_fallback_used") is True
    assert metadata.get("expanded_shopping_beauty_prefetch") is True
    assert metadata.get("merchants_scanned") == 3


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_attribute_intent_fails_closed_without_visible_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            description="Hydrating serum for sensitive skin.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hydrating serum for sensitive skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "visible attributes" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("visible_attribute_intents") == ["sensitive_skin", "hydrating"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_skin_care_multi_constraint_requires_all_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Serum for Sensitive Skin",
            description="Repair-focused serum.",
            product_type="Serum",
            tags=["fragrance-free"],
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="fragrance free serum for sensitive skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_serum_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("visible_attribute_intents") == ["fragrance_free", "sensitive_skin"]
    assert metadata.get("matched_visible_attributes") == {
        "product_category": ["serum"],
        "formula_constraint": ["fragrance_free"],
        "skin_concern": ["sensitive_skin"],
    }


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_skin_care_multi_constraint_fails_closed_when_one_label_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Serum for Sensitive Skin",
            description="Repair-focused serum.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="fragrance free serum for sensitive skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "visible attributes" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("visible_attribute_intents") == ["fragrance_free", "sensitive_skin"]
    assert metadata.get("matched_visible_attributes") == {}


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_unsupported_cosmetics_category_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        foundation = agent_shop_gateway_module.StandardProduct(
            id="prod_foundation_1",
            product_id="prod_foundation_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Visible Foundation Product",
            description="Makeup item.",
            product_type="Foundation",
            price=35.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [foundation], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="foundation",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "eligible foundation shade match" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["foundation"]
    assert metadata.get("unsupported_beauty_category_intents") == []


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_apparel_attribute_intent_fails_closed_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        sweater = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_1",
            product_id="prod_sweater_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Striped Knitted Sweater for Dogs & Cats",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [sweater], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="wool sweater",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "visible attributes" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["sweater"]
    assert metadata.get("visible_attribute_intents") == ["wool"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_style_intent_keeps_matching_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        striped = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_striped_1",
            product_id="prod_sweater_striped_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Striped Knitted Sweater for Dogs & Cats – Striped",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        color_block = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_color_block_1",
            product_id="prod_sweater_color_block_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Sleeveless Knitted Sweater for Dogs & Cats – Color-Block",
            description="Sleeveless knit sweater.",
            product_type="Sleeveless Knit Sweater",
            price=29.12,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [striped, color_block], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="striped sweater",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_sweater_striped_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_attribute_intents") == ["striped"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_style_intent_supports_hyphenated_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        color_block = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_color_block_1",
            product_id="prod_sweater_color_block_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Sleeveless Knitted Sweater for Dogs & Cats – Color-Block",
            description="Sleeveless knit sweater.",
            product_type="Sleeveless Knit Sweater",
            price=29.12,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        plain = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_plain_1",
            product_id="prod_sweater_plain_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Striped Knitted Sweater for Dogs & Cats – Striped",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [color_block, plain], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="color block sweater",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_sweater_color_block_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_attribute_intents") == ["color_block"]
@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_color_intent_keeps_matching_variant_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        sweater = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_1",
            product_id="prod_sweater_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Knitted Sweater for Dogs & Cats",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_red_1",
                    title="Medium / Red",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Red"},
                ),
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_blue_1",
                    title="Large / Blue",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Large", "Color": "Blue"},
                ),
            ],
        )
        return [sweater], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="cat sweater red",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_sweater_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["sweater"]
    assert metadata.get("visible_attribute_intents") == []
    assert metadata.get("visible_option_intents") == ["color_red"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_color_blue_intent_keeps_matching_variant_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        sweater = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_blue_1",
            product_id="prod_sweater_blue_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Knitted Sweater for Dogs & Cats",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_blue_1",
                    title="Medium / Blue",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Blue"},
                ),
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_red_1",
                    title="Large / Red",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Large", "Color": "Red"},
                ),
            ],
        )
        return [sweater], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="blue sweater",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_sweater_blue_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_option_intents") == ["color_blue"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_size_option_intent_keeps_matching_variant_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        hoodie = agent_shop_gateway_module.StandardProduct(
            id="prod_hoodie_1",
            product_id="prod_hoodie_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Cozy Hoodie",
            description="Cozy hoodie.",
            product_type="Hoodie",
            price=39.0,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_m_1",
                    title="Medium / Black",
                    price=39.0,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Black"},
                ),
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_l_1",
                    title="Large / Black",
                    price=39.0,
                    inventory_quantity=4,
                    options={"Size": "Large", "Color": "Black"},
                ),
            ],
        )
        return [hoodie], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hoodie size m",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_hoodie_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["hoodie"]
    assert metadata.get("visible_option_intents") == ["size_m"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_combined_style_color_constraint_keeps_exact_sweater(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        striped_blue = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_blue_striped_1",
            product_id="prod_sweater_blue_striped_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Striped Knitted Sweater for Dogs & Cats – Striped",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_blue_1",
                    title="Medium / Blue",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Blue"},
                ),
            ],
        )
        striped_red = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_red_striped_1",
            product_id="prod_sweater_red_striped_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Striped Knitted Sweater for Dogs & Cats – Striped",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_red_1",
                    title="Medium / Red",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Red"},
                ),
            ],
        )
        plain_blue = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_blue_plain_1",
            product_id="prod_sweater_blue_plain_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Knitted Sweater for Dogs & Cats",
            description="Classic knit sweater.",
            product_type="Knit Sweater",
            price=27.65,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_blue_plain_1",
                    title="Medium / Blue",
                    price=27.65,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Blue"},
                ),
            ],
        )
        return [striped_red, plain_blue, striped_blue], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="blue striped sweater",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_sweater_blue_striped_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["sweater"]
    assert metadata.get("visible_attribute_intents") == ["striped"]
    assert metadata.get("visible_option_intents") == ["color_blue"]
    assert metadata.get("matched_visible_categories") == ["sweater"]
    assert metadata.get("matched_visible_attribute_labels") == ["striped"]
    assert metadata.get("matched_visible_option_labels") == ["color_blue"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_combined_style_color_size_constraint_fails_closed_without_full_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        sleeveless_pink_large = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_pink_large_1",
            product_id="prod_sweater_pink_large_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Sleeveless Knitted Sweater for Dogs & Cats – Sleeveless",
            description="Sleeveless knit sweater.",
            product_type="Sleeveless Knit Sweater",
            price=29.12,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_large_pink_1",
                    title="Large / Pink",
                    price=29.12,
                    inventory_quantity=4,
                    options={"Size": "Large", "Color": "Pink"},
                ),
            ],
        )
        sleeveless_blue_medium = agent_shop_gateway_module.StandardProduct(
            id="prod_sweater_blue_medium_1",
            product_id="prod_sweater_blue_medium_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Sleeveless Knitted Sweater for Dogs & Cats – Sleeveless",
            description="Sleeveless knit sweater.",
            product_type="Sleeveless Knit Sweater",
            price=29.12,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_medium_blue_1",
                    title="Medium / Blue",
                    price=29.12,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Blue"},
                ),
            ],
        )
        return [sleeveless_pink_large, sleeveless_blue_medium], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="pink sleeveless sweater size m",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "visible attributes and options" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["sweater"]
    assert metadata.get("visible_attribute_intents") == ["sleeveless"]
    assert metadata.get("visible_option_intents") == ["size_m", "color_pink"]
    assert metadata.get("matched_visible_categories") == []
    assert metadata.get("matched_visible_attribute_labels") == []
    assert metadata.get("matched_visible_option_labels") == []


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_combined_style_size_constraint_keeps_exact_vest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        fleece_xl = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_fleece_xl_1",
            product_id="prod_vest_fleece_xl_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Padded Winter Vest for Dogs & Cats – Color-Block, Polar Fleece",
            description="Polar fleece vest.",
            product_type="Polar Fleece Vest",
            price=25.85,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_xl_black_1",
                    title="XL / Black",
                    price=25.85,
                    inventory_quantity=4,
                    options={"Size": "XL", "Color": "Black"},
                ),
            ],
        )
        fleece_medium = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_fleece_m_1",
            product_id="prod_vest_fleece_m_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Padded Winter Vest for Dogs & Cats – Color-Block, Polar Fleece",
            description="Polar fleece vest.",
            product_type="Polar Fleece Vest",
            price=25.85,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_m_black_1",
                    title="Medium / Black",
                    price=25.85,
                    inventory_quantity=4,
                    options={"Size": "Medium", "Color": "Black"},
                ),
            ],
        )
        padded_xl = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_plain_xl_1",
            product_id="prod_vest_plain_xl_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Warm vest.",
            product_type="Padded Vest",
            price=22.93,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_xl_plain_1",
                    title="XL / Black",
                    price=22.93,
                    inventory_quantity=4,
                    options={"Size": "XL", "Color": "Black"},
                ),
            ],
        )
        return [fleece_medium, padded_xl, fleece_xl], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="polar fleece vest size xl",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_vest_fleece_xl_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["vest"]
    assert metadata.get("visible_attribute_intents") == ["fleece"]
    assert metadata.get("visible_option_intents") == ["size_xl"]
    assert metadata.get("matched_visible_categories") == ["vest"]
    assert metadata.get("matched_visible_attribute_labels") == ["fleece"]
    assert metadata.get("matched_visible_option_labels") == ["size_xl"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_numeric_size_option_intent_keeps_matching_variant_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        dress = agent_shop_gateway_module.StandardProduct(
            id="prod_dress_1",
            product_id="prod_dress_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Elegant Evening Dress",
            description="Formal dress.",
            product_type="Dress",
            price=59.0,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_40_1",
                    title="Size 40 / Blue",
                    price=59.0,
                    inventory_quantity=4,
                    options={"Size": "40", "Color": "Blue"},
                ),
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_42_1",
                    title="Size 42 / Blue",
                    price=59.0,
                    inventory_quantity=4,
                    options={"Size": "42", "Color": "Blue"},
                ),
            ],
        )
        return [dress], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="size 40 dress",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_dress_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_option_intents") == ["size_40"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_visible_category_intent_fails_closed_on_brand_only_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            description="Hydrating serum for sensitive skin.",
            product_type="Serum",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="winona hoodie",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    assert "eligible hoodie match" in str(result.get("reply") or "").lower()
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["hoodie"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_query_budget_max_keeps_matching_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Sensitive Skin Serum",
            description="Repair-focused serum.",
            product_type="Serum",
            price=29.0,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="winona serum under €30 for sensitive skin",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_serum_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("budget_price_max") == 30.0
    assert metadata.get("budget_currency") == "EUR"
    assert metadata.get("visible_attribute_intents") == ["sensitive_skin"]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_query_budget_max_fails_closed_when_no_product_meets_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_1",
            product_id="prod_serum_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            description="Hydrating serum for sensitive skin.",
            product_type="Serum",
            price=29.0,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="winona serum below 20",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("budget_price_max") == 20.0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_query_budget_filters_visible_category_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "merch_live_1", "business_name": "Live Merchant"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        vest_a = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_1",
            product_id="prod_vest_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
            description="Warm pet vest.",
            product_type="Padded Vest",
            price=22.93,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        vest_b = agent_shop_gateway_module.StandardProduct(
            id="prod_vest_2",
            product_id="prod_vest_2",
            platform="shopify",
            merchant_id=merchant_id,
            title="Warm Fall/Winter Color-Block Padded Winter Vest for Dogs & Cats",
            description="Warm pet vest.",
            product_type="Padded Vest",
            price=25.85,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [vest_a, vest_b], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="dog vest under 25",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_vest_1"
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["vest"]
    assert metadata.get("budget_price_max") == 25.0


def test_agent_cart_validate_rejects_external_seed_merchant(client: TestClient) -> None:
    res = client.post(
        "/agent/v1/cart/validate?merchant_id=external_seed&shipping_country=US",
        headers={"X-API-Key": "test-api-key"},
        json=[{"product_id": "ext_1", "quantity": 1}],
    )
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "EXTERNAL_PRODUCT_CHECKOUT_DISABLED"


def test_agent_orders_create_rejects_external_seed_merchant(client: TestClient) -> None:
    payload = {
        "merchant_id": "external_seed",
        "customer_email": "buyer@example.com",
        "items": [
            {
                "product_id": "ext_1",
                "product_title": "External",
                "variant_id": "ext_1",
                "quantity": 1,
                "unit_price": 10.0,
                "subtotal": 10.0,
            }
        ],
        "shipping_address": {
            "name": "Buyer",
            "address_line1": "1 Main St",
            "city": "SF",
            "postal_code": "94105",
            "country": "US",
        },
        "currency": "USD",
    }

    res = client.post(
        "/agent/v1/orders/create",
        headers={"X-API-Key": "test-api-key"},
        json=payload,
    )
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "EXTERNAL_PRODUCT_CHECKOUT_DISABLED"


def test_agent_products_search_surfaces_external_seeds(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_test_1",
                    "external_product_id": "ext_test_1",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Example External Product",
                        "image_url": None,
                        "price_amount": 12.34,
                        "price_currency": "USD",
                        "availability": "in_stock",
                        "seed_data": {
                            "variants": [
                                {
                                    "variant_id": "v1",
                                    "title": "50ml",
                                    "price_amount": 12.34,
                                    "price_currency": "USD",
                                    "availability": "in_stock",
                                }
                            ]
                        },
                        "status": "active",
                        "notes": None,
                        "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_sdk_fixed_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    res = client.get(
        "/agent/v1/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)
    external = next(p for p in products if p.get("merchant_id") == "external_seed")
    assert isinstance(external.get("external_redirect_url"), str)
    assert "/r?token=" in external.get("external_redirect_url")
    metadata = payload.get("metadata") or {}
    route_health = metadata.get("route_health") or {}
    assert route_health.get("primary_path_used") == "cross_merchant_browse_standard"
    assert route_health.get("external_seed_executed") is True
    assert route_health.get("external_seed_query_timeout") is False
    assert "external_seed_returned_count" in metadata


def test_agent_products_search_matches_title_field_for_shopify_rows(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    _patch_agent_sdk_ranking(monkeypatch, agent_api_module)

    class _FakeProduct:
        def model_dump(self):
            return {
                "id": "prod_ipsa_1",
                "product_id": "prod_ipsa_1",
                "title": "IPSA Time Reset Aqua",
                "description": "Hydrating toner",
                "platform": "shopify",
                "platform_product_id": "9886500127048",
                "price": 45.0,
                "in_stock": True,
            }

    monkeypatch.setattr(
        agent_api_module,
        "get_merchant_onboarding",
        AsyncMock(return_value={"merchant_id": "merch_test", "business_name": "Test Merchant", "status": "active"}),
    )
    monkeypatch.setattr(
        agent_api_module,
        "get_products_hybrid",
        AsyncMock(return_value=([_FakeProduct()], "cache", None)),
    )
    monkeypatch.setattr(
        agent_api_module, "_load_external_seed_products_for_search", AsyncMock(return_value=[])
    )

    res = client.get(
        "/agent/v1/products/search?merchant_id=merch_test&query=ipsa&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )

    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert products
    assert products[0].get("platform_product_id") == "9886500127048"
    assert products[0].get("title") == "IPSA Time Reset Aqua"


def test_agent_products_search_merchant_scope_does_not_mix_external_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    _patch_agent_sdk_ranking(monkeypatch, agent_api_module)

    external_loader = AsyncMock(
        return_value=[
            {
                "id": "ext_1",
                "product_id": "ext_1",
                "title": "External Product",
                "merchant_id": "external_seed",
                "platform": "external",
            }
        ]
    )

    class _FakeProduct:
        def model_dump(self):
            return {
                "id": "prod_ipsa_1",
                "product_id": "prod_ipsa_1",
                "title": "IPSA Time Reset Aqua",
                "description": "Hydrating toner",
                "platform": "shopify",
                "platform_product_id": "9886500127048",
                "price": 45.0,
                "in_stock": True,
            }

    monkeypatch.setattr(
        agent_api_module,
        "get_merchant_onboarding",
        AsyncMock(return_value={"merchant_id": "merch_test", "business_name": "Test Merchant", "status": "active"}),
    )
    monkeypatch.setattr(
        agent_api_module,
        "get_products_hybrid",
        AsyncMock(return_value=([_FakeProduct()], "cache", None)),
    )
    monkeypatch.setattr(
        agent_api_module, "_load_external_seed_products_for_search", external_loader
    )

    res = client.get(
        "/agent/v1/products/search?merchant_id=merch_test&query=ipsa&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )

    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert products
    assert all(p.get("merchant_id") != "external_seed" for p in products)
    external_loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_matches_external_seeds_with_stopwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_row = {
        "id": "eps_test_1",
        "external_product_id": "ext_test_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://fentybeauty.com/products/gloss-bomb",
        "canonical_url": "https://fentybeauty.com/products/gloss-bomb",
        "domain": "fentybeauty.com",
        # Title intentionally does NOT include the stopword "product".
        "title": "Gloss Bomb",
        "image_url": None,
        "price_amount": 19.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"brand": "Fenty Beauty"},
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": None,
    }

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT",
        True,
    )

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "fenty beauty product"
        prefer_terms = kwargs.get("prefer_terms") or []
        assert "fenty" in prefer_terms
        assert "beauty" in prefer_terms
        assert "product" not in prefer_terms
        return {
            "rows": [seed_row],
            "query_timeout": False,
            "query_ms": 12,
            "total_count": 1,
        }

    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )

    async def fake_redirect_url(**_kwargs):
        return "https://example.com/r?token=test"

    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_redirect_url)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="fenty beauty product",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "creator-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert any(p.get("source") == "external_seed" for p in products)
    assert metadata.get("external_seed_rows_fetched") == 1
    assert metadata.get("external_seed_query_timeout") is False


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_limits_merchant_fanout_and_uses_cache_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_count = max(agent_shop_gateway_module.MULTI_SEARCH_MERCHANT_SCAN_LIMIT + 5, 25)
    merchant_rows = [
        {"merchant_id": f"m_{idx:03d}", "business_name": f"Merchant {idx}"}
        for idx in range(merchant_count)
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING",
        False,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL",
        "",
    )

    calls = []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        calls.append(
            {
                "merchant_id": merchant_id,
                "limit": limit,
                "agent_id": agent_id,
                "force_cache_only": force_cache_only,
            }
        )
        product = agent_shop_gateway_module.StandardProduct(
            id=f"p_{merchant_id}",
            product_id=f"p_{merchant_id}",
            platform="shopify",
            merchant_id=merchant_id,
            title=f"Winona {merchant_id}",
            description="test",
            price=19.0,
            currency="USD",
            inventory_quantity=9,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    expected_scanned = min(
        merchant_count,
        agent_shop_gateway_module.MULTI_SEARCH_MERCHANT_SCAN_LIMIT,
    )
    assert len(calls) == expected_scanned
    assert all(
        c["force_cache_only"] is agent_shop_gateway_module.MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING
        for c in calls
    )

    meta = result.get("metadata") or {}
    assert meta.get("merchants_scanned") == expected_scanned
    assert meta.get("merchants_searched") == merchant_count
    assert meta.get("merchant_scan_limited") is True
    assert meta.get("force_cache_only") is False


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_shopping_disables_base_fanout_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "m_001", "business_name": "Merchant 1"},
        {"merchant_id": "m_002", "business_name": "Merchant 2"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING",
        False,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL",
        "",
    )

    calls = []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        calls.append(merchant_id)
        return [], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert calls == []
    meta = result.get("metadata") or {}
    assert meta.get("base_merchant_fanout_enabled") is False
    assert meta.get("merchants_scanned") == 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_to_upstream_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL",
        "https://resolver.example",
    )

    upstream_response = {
        "products": [{"id": "p_upstream_1", "title": "Upstream Product"}],
        "total": 1,
        "page": 1,
        "page_size": 1,
        "metadata": {"query_source": "agent_products_resolver_fallback"},
    }
    invoke_mock = AsyncMock(return_value=upstream_response)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_invoke_multi_upstream_fallback",
        invoke_mock,
    )
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 1
    assert (result.get("products") or [{}])[0].get("id") == "p_upstream_1"
    get_products_mock.assert_not_awaited()
    invoke_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_upstream_failure_returns_empty_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL",
        "https://resolver.example",
    )

    invoke_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_invoke_multi_upstream_fallback",
        invoke_mock,
    )
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="not_found_query",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("metadata", {}).get("query_source") == "cache_multi"
    get_products_mock.assert_not_awaited()
    invoke_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_upstream_failure_aurora_forces_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module
    from models.standard_product import StandardProduct

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "m_001", "business_name": "Merchant One"}]
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://resolver.example")
    monkeypatch.setattr(agent_shop_gateway_module, "CATALOG_RELIABILITY_V2_ENABLED", False)
    monkeypatch.setattr(agent_shop_gateway_module, "CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_AURORA_FORCE_LOCAL_FALLBACK_ON_DELEGATE_FAIL", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", True)

    invoke_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_shop_gateway_module, "_invoke_multi_upstream_fallback", invoke_mock)
    get_products_mock = AsyncMock(
        return_value=(
            [
                StandardProduct(
                    id="p_local_1",
                    platform="shopify",
                    merchant_id="m_001",
                    title="Copper Peptides Serum",
                    description="test",
                    price=29.0,
                    in_stock=True,
                    inventory_quantity=10,
                    orderable=True,
                )
            ],
            "cache",
            None,
        )
    )
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="copper peptides serum",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "aurora-bff"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert (result.get("metadata") or {}).get("query_source") != "agent_products_resolver_fallback_empty"
    invoke_mock.assert_awaited_once()
    get_products_mock.assert_awaited()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_upstream_success_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://resolver.example")
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_RESPONSE_CACHE_TTL_SECONDS", 120.0)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_CACHE_MAX_ENTRIES", 64)
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()

    upstream_response = {
        "products": [{"id": "p_upstream_1", "title": "Upstream Product"}],
        "total": 1,
        "page": 1,
        "page_size": 1,
        "metadata": {"query_source": "agent_products_resolver_fallback"},
    }
    invoke_mock = AsyncMock(return_value=upstream_response)
    monkeypatch.setattr(agent_shop_gateway_module, "_invoke_multi_upstream_fallback", invoke_mock)
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    first = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )
    second = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert first.get("total") == 1
    assert second.get("total") == 1
    assert second.get("metadata", {}).get("upstream_response_cache", {}).get("hit") is True
    assert second.get("metadata", {}).get("upstream_response_cache", {}).get("kind") == "result"
    get_products_mock.assert_not_awaited()
    assert invoke_mock.await_count == 1
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_upstream_error_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://resolver.example")
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_ERROR_CACHE_TTL_SECONDS", 120.0)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_CACHE_MAX_ENTRIES", 64)
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()

    invoke_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_shop_gateway_module, "_invoke_multi_upstream_fallback", invoke_mock)
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="not_found_query",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    first = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )
    second = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert first.get("total") == 0
    assert second.get("total") == 0
    assert second.get("metadata", {}).get("query_source") == "cache_multi"
    assert second.get("metadata", {}).get("upstream_response_cache", {}) == {}
    get_products_mock.assert_not_awaited()
    assert invoke_mock.await_count == 2
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_uses_shopping_timeout_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://resolver.example")
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_TIMEOUT_SECONDS", 9.0)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_SHOPPING_TIMEOUT_CAP_SECONDS", 1.1)
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0

    observed: dict[str, float] = {}

    async def fake_upstream(payload, request_metadata, *, timeout_seconds: float, hop: int):
        observed["timeout_seconds"] = timeout_seconds
        return None

    invoke_mock = AsyncMock(side_effect=fake_upstream)
    monkeypatch.setattr(agent_shop_gateway_module, "_invoke_multi_upstream_fallback", invoke_mock)
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert observed.get("timeout_seconds") == pytest.approx(1.1, rel=0, abs=1e-6)
    invoke_mock.assert_awaited_once()
    get_products_mock.assert_not_awaited()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_delegate_circuit_open_returns_empty_without_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://resolver.example")
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_CACHE_MAX_ENTRIES", 64)
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = time.monotonic() + 30.0

    invoke_mock = AsyncMock(return_value={"products": [{"id": "p_should_not_be_used"}], "total": 1})
    monkeypatch.setattr(agent_shop_gateway_module, "_invoke_multi_upstream_fallback", invoke_mock)
    get_products_mock = AsyncMock(return_value=([], "cache", None))
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", get_products_mock)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    meta = result.get("metadata") or {}
    assert meta.get("query_source") == "cache_multi"
    assert meta.get("upstream_circuit_open") is None
    invoke_mock.assert_not_awaited()
    get_products_mock.assert_not_awaited()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CACHE.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0


def test_shop_gateway_upstream_timeout_opens_circuit_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_ON_TIMEOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_SECONDS", 60.0)

    agent_shop_gateway_module._multi_upstream_record_outcome(False, timeout=True)

    assert agent_shop_gateway_module._multi_upstream_circuit_is_open() is True
    assert len(agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS) == 1

    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_FAILURE_EVENTS.clear()
    agent_shop_gateway_module._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL = 0.0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_shopping_skips_history_lookup_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    observed = {"orders_queries": 0}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            observed["orders_queries"] += 1
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert observed.get("orders_queries") == 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_shopping_caps_seed_query_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    observed = {"seed_limit": None}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            observed["seed_limit"] = (values or {}).get("limit")
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SEED_QUERY_LIMIT_SHOPPING", 7)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="lipstick",
            page=1,
            limit=50,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert observed.get("seed_limit") == 7


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_creator_surface_uses_creator_cache_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    merchant_rows = [
        {"merchant_id": "m_001", "business_name": "Merchant 1"},
        {"merchant_id": "m_002", "business_name": "Merchant 2"},
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return merchant_rows
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_FORCE_CACHE_ONLY_CREATOR",
        True,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_FORCE_CACHE_ONLY_SHOPPING",
        False,
    )

    calls = []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        calls.append(force_cache_only)
        product = agent_shop_gateway_module.StandardProduct(
            id=f"p_{merchant_id}",
            product_id=f"p_{merchant_id}",
            platform="shopify",
            merchant_id=merchant_id,
            title=f"Winona {merchant_id}",
            description="test",
            price=19.0,
            currency="USD",
            inventory_quantity=9,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="Winona",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "creator-agent-ui", "creator_id": "creator_1"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert calls
    assert all(v is True for v in calls)
    meta = result.get("metadata") or {}
    assert meta.get("force_cache_only") is True


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_recall_terms_drop_common_stopwords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "m_001", "business_name": "Merchant"}]
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q and "cache_limit" in (values or {}):
            return [
                {
                    "merchant_id": "m_001",
                    "product_data": {
                        "id": "p_the_only",
                        "product_id": "p_the_only",
                        "platform": "shopify",
                        "merchant_id": "m_001",
                        "title": "The Brush",
                        "description": "soft finish",
                        "price": 19.0,
                        "currency": "USD",
                        "inventory_quantity": 10,
                        "orderable": True,
                        "status": "active",
                    },
                },
                {
                    "merchant_id": "m_001",
                    "product_data": {
                        "id": "p_ordinary",
                        "product_id": "p_ordinary",
                        "platform": "shopify",
                        "merchant_id": "m_001",
                        "title": "Ordinary Niacinamide Serum",
                        "description": "zinc formula",
                        "price": 20.0,
                        "currency": "USD",
                        "inventory_quantity": 8,
                        "orderable": True,
                        "status": "active",
                    },
                },
            ]
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="The Ordinary Niacinamide 10% + Zinc 1%",
            page=1,
            limit=10,
            in_stock_only=False,
        )
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    ids = {p.get("id") for p in products}
    assert "p_ordinary" in ids
    assert "p_the_only" not in ids


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_ignores_stopword_only_query_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.database as db_database_module
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "m_001", "business_name": "Merchant"}]
        if "FROM external_product_seeds" in q:
            return []
        if "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        product = agent_shop_gateway_module.StandardProduct(
            id="p_001",
            product_id="p_001",
            platform="shopify",
            merchant_id=merchant_id,
            title="Round Powder Brush",
            description="Use the brush for soft focus finish",
            price=19.0,
            currency="USD",
            inventory_quantity=9,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="The Ordinary Niacinamide 10% + Zinc 1%",
            page=1,
            limit=10,
            in_stock_only=False,
        ),
        user=agent_shop_gateway_module.UserIntent(
            recent_queries=["The Ordinary Niacinamide 10% + Zinc 1%"],
        ),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 0
    assert result.get("products") == []


def test_agent_products_search_external_seed_includes_variants(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_test_1",
                    "external_product_id": "ext_test_1",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Example External Product",
                    "image_url": None,
                    "price_amount": 12.34,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "image_urls": [
                            "https://example.com/img_1.jpg",
                            "https://example.com/img_2.jpg",
                        ],
                        "variants": [
                            {
                                "variant_id": "v1",
                                "title": "50ml",
                                "price_amount": 12.34,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            },
                            {
                                "variant_id": "v2",
                                "title": "100ml",
                                "price_amount": 19.99,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            },
                        ]
                    },
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_sdk_fixed_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    res = client.get(
        "/agent/v1/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    external = next(p for p in products if p.get("merchant_id") == "external_seed")
    assert len(external.get("variants") or []) == 2
    assert len(external.get("image_urls") or []) == 2


def test_agent_product_detail_external_seed_includes_variants(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    async def fake_fetch_one(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return {
                "id": "eps_test_1",
                "external_product_id": "ext_test_1",
                "market": "US",
                "tool": "*",
                "utm_template": None,
                "partner_type": None,
                "disclosure_text": None,
                "destination_url": "https://example.com/product/1",
                "canonical_url": None,
                "domain": "example.com",
                "title": "Example External Product",
                "image_url": None,
                "price_amount": 12.34,
                "price_currency": "USD",
                "availability": "in_stock",
                "seed_data": {
                    "image_urls": [
                        "https://example.com/img_1.jpg",
                        "https://example.com/img_2.jpg",
                    ],
                    "variants": [
                        {
                            "variant_id": "v1",
                            "title": "50ml",
                            "price_amount": 12.34,
                            "price_currency": "USD",
                            "availability": "in_stock",
                        },
                        {
                            "variant_id": "v2",
                            "title": "100ml",
                            "price_amount": 19.99,
                            "price_currency": "USD",
                            "availability": "in_stock",
                        },
                    ]
                },
                "status": "active",
                "notes": None,
                "created_by_employee_id": None,
                "attached_product_key": None,
                "attached_variant_id": None,
                "created_at": None,
                "updated_at": None,
            }
        return None

    monkeypatch.setattr(agent_api_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    res = client.get(
        "/agent/v1/products/external_seed/ext_test_1",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    product = payload.get("product") or {}
    assert product.get("merchant_id") == "external_seed"
    assert len(product.get("variants") or []) == 2
    assert len(product.get("image_urls") or []) == 2


def test_agent_products_search_cross_merchant_injects_external_seeds_by_domain(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_test_1",
                    "external_product_id": "ext_test_1",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Example External Product",
                    "image_url": None,
                    "price_amount": 12.34,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {},
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    async def fake_fetch_one(query: str, values=None):
        if "COUNT" in str(query):
            return {"total": 0}
        return None

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_sdk_fixed_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    res = client.get(
        "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)


def test_agent_products_search_external_seed_compacts_spaced_query(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            if not values or values.get("q_compact_like") != "%tomford%":
                return []
            return [
                {
                    "id": "eps_test_tomford",
                    "external_product_id": "ext_test_tomford",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://www.tomfordbeauty.com/product/figue-erotique-eau-de-parfum",
                    "canonical_url": None,
                    "domain": "tomfordbeauty.com",
                    "title": "Figue Érotique Eau de Parfum",
                    "image_url": None,
                    "price_amount": 255,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {},
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    async def fake_fetch_one(query: str, values=None):
        if "COUNT" in str(query):
            return {"total": 0}
        return None

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_sdk_fixed_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_api_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_api_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )

    res = client.get(
        "/agent/v1/products/search?query=tom%20ford&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)


@pytest.mark.asyncio
async def test_agent_products_search_external_seed_ignores_stopword_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            assert values is not None
            # Ensure the query is tokenized so long NL strings still match.
            assert values.get("q_term_0") == "%fenty%"
            assert values.get("q_term_1") == "%beauty%"
            return [
                {
                    "id": "eps_test_fenty",
                    "external_product_id": "ext_test_fenty",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/fenty",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Gloss Bomb Universal Lip Luminizer",
                    "image_url": None,
                    "price_amount": 24,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {"brand": "Fenty Beauty"},
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent_sdk_fixed_module,
        "should_block_external_referral_runtime",
        AsyncMock(return_value=(False, None)),
    )
    products = await agent_sdk_fixed_module._load_external_seed_products_for_search(
        req=type("Req", (), {"base_url": "https://agent.pivota.cc/"})(),
        query="fenty beauty product",
        limit=20,
        offset=0,
    )
    assert any(p.get("merchant_id") == "external_seed" for p in products)


def test_agent_products_search_tolerates_non_numeric_price(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    class _FakeProduct:
        def dict(self):
            return {
                "id": "p_bad_price_1",
                "product_id": "p_bad_price_1",
                "title": "Winona bad-price sample",
                "description": "test",
                "platform": "shopify",
                "price": "N/A",
                "in_stock": True,
            }

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "m_001", "business_name": "Merchant One"}]
        if "FROM external_product_seeds" in q:
            return []
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", AsyncMock(return_value=([_FakeProduct()], "cache", None)))
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(agent_api_module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent_api_module, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_sdk_fixed_module, "_load_external_seed_products_for_search", AsyncMock(return_value=[]))

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=Winona&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "success"
    assert isinstance(payload.get("products"), list)


def test_agent_products_search_handles_db_rows_with_noncallable_get(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    class _FakeRecord:
        def __init__(self, data):
            self._mapping = data
            self.get = None

    class _FakeProduct:
        def dict(self):
            return {
                "id": "p_row_record_1",
                "product_id": "p_row_record_1",
                "title": "Winona row-record sample",
                "description": "test",
                "platform": "shopify",
                "price": 29.9,
                "in_stock": True,
            }

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [_FakeRecord({"merchant_id": "m_001", "business_name": "Merchant One"})]
        if "FROM external_product_seeds" in q:
            return []
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", AsyncMock(return_value=([_FakeProduct()], "cache", None)))
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(agent_api_module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent_api_module, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_sdk_fixed_module, "_load_external_seed_products_for_search", AsyncMock(return_value=[]))

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=Winona&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "success"
    assert isinstance(payload.get("products"), list)


@pytest.mark.asyncio
async def test_agent_products_search_delegate_hard_timeout_returns_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module
    from routes.agent_auth import AgentContext

    async def fake_slow_agent_search_products(**_kwargs):
        await asyncio.sleep(0.3)
        return {
            "status": "success",
            "products": [],
            "pagination": {"total": 0, "limit": 20, "offset": 0, "has_more": False},
        }

    monkeypatch.setattr(agent_api_module, "agent_search_products", fake_slow_agent_search_products)
    monkeypatch.setattr(agent_sdk_fixed_module, "AGENT_SDK_FIXED_DELEGATE_TIMEOUT_SECONDS", 0.05)

    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agent/v1/products/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 0),
            "scheme": "https",
            "server": ("agent.pivota.cc", 443),
        }
    )
    context = AgentContext(
        {"agent_id": "agent_test", "agent_name": "Test Agent", "allowed_merchants": None},
        req,
    )

    with pytest.raises(HTTPException) as exc_info:
        await agent_sdk_fixed_module.search_products(
            req=req,
            background_tasks=BackgroundTasks(),
            search_all_merchants=True,
            query="slow",
            in_stock_only=False,
            limit=10,
            offset=0,
            context=context,
        )

    assert exc_info.value.status_code == 504
    assert "Search timeout" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_agent_sdk_fixed_delegate_path_does_not_double_inject_external_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module
    from routes.agent_auth import AgentContext

    async def fake_agent_search_products(**_kwargs):
        return {
            "status": "success",
            "products": [
                {
                    "id": "prod_internal_1",
                    "product_id": "prod_internal_1",
                    "title": "Internal Product",
                    "merchant_id": "m_001",
                    "platform": "shopify",
                }
            ],
            "pagination": {"total_count": 1, "limit": 10, "offset": 0, "has_more": False},
            "metadata": {"source": "agent_search_products", "reason_code": "ok"},
        }

    external_loader = AsyncMock(
        return_value=[
            {
                "id": "ext_seed_ignored",
                "product_id": "ext_seed_ignored",
                "merchant_id": "external_seed",
                "source": "external_seed",
                "title": "Should not be injected by sdk_fixed delegate path",
            }
        ]
    )

    monkeypatch.setattr(agent_api_module, "agent_search_products", fake_agent_search_products)
    monkeypatch.setattr(agent_sdk_fixed_module, "_load_external_seed_products_for_search", external_loader)

    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agent/v1/products/search",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 0),
            "scheme": "https",
            "server": ("agent.pivota.cc", 443),
        }
    )
    context = AgentContext(
        {"agent_id": "agent_test", "agent_name": "Test Agent", "allowed_merchants": None},
        req,
    )

    payload = await agent_sdk_fixed_module.search_products(
        req=req,
        background_tasks=BackgroundTasks(),
        search_all_merchants=True,
        query="ipsa",
        allow_external_seed=True,
        in_stock_only=False,
        limit=10,
        offset=0,
        context=context,
    )

    products = payload.get("products") or []
    assert len(products) == 1
    assert all(str(p.get("merchant_id") or "") != "external_seed" for p in products)
    assert ((payload.get("metadata") or {}).get("source") or "") == "agent_sdk_fixed_delegate"
    external_loader.assert_not_awaited()


def test_agent_products_search_allow_external_seed_false_disables_external_merge(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    class _FakeProduct:
        def model_dump(self):
            return {
                "id": "prod_internal_1",
                "product_id": "prod_internal_1",
                "title": "IPSA Time Reset Aqua",
                "description": "internal cache",
                "platform": "shopify",
                "price": 39,
                "in_stock": True,
            }

    async def fake_fetch_all(query: str, values=None):
        text = str(query)
        if "FROM merchant_onboarding" in text:
            return [{"merchant_id": "m_001", "business_name": "Merchant One"}]
        return []

    external_loader = AsyncMock(
        return_value=[
            {
                "id": "ext_seed_1",
                "product_id": "ext_seed_1",
                "merchant_id": "external_seed",
                "source": "external_seed",
                "title": "IPSA External Seed",
                "price": 29.0,
                "in_stock": True,
            }
        ]
    )

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", AsyncMock(return_value=([_FakeProduct()], "cache", None)))
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", external_loader)
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(agent_api_module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent_api_module, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=ipsa&allow_external_seed=false&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert products
    assert all(p.get("merchant_id") != "external_seed" for p in products)
    source_breakdown = ((payload.get("metadata") or {}).get("source_breakdown") or {})
    assert source_breakdown.get("external_seed_count") == 0
    assert source_breakdown.get("strategy_applied") == "external_seed_disabled"
    assert external_loader.await_count == 0


def test_agent_products_search_supplement_internal_first_keeps_internal_ahead(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)

    class _FakeProduct:
        def model_dump(self):
            return {
                "id": "prod_internal_2",
                "product_id": "prod_internal_2",
                "title": "IPSA Internal Toner",
                "description": "internal candidate",
                "platform": "shopify",
                "price": 35,
                "in_stock": True,
            }

    async def fake_fetch_all(query: str, values=None):
        text = str(query)
        if "FROM merchant_onboarding" in text:
            return [{"merchant_id": "m_001", "business_name": "Merchant One"}]
        return []

    external_loader = AsyncMock(
        return_value=[
            {
                "id": "ext_seed_2",
                "product_id": "ext_seed_2",
                "merchant_id": "external_seed",
                "source": "external_seed",
                "title": "IPSA External Seed Candidate",
                "description": "external supplement",
                "price": 28.0,
                "in_stock": True,
            }
        ]
    )

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", AsyncMock(return_value=([_FakeProduct()], "cache", None)))
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", external_loader)
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 0.1)
    monkeypatch.setattr(agent_api_module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent_api_module, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=ipsa&allow_external_seed=true&external_seed_strategy=supplement_internal_first&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert len(products) >= 2
    assert any(p.get("merchant_id") != "external_seed" for p in products)
    assert any(p.get("merchant_id") == "external_seed" for p in products)
    source_breakdown = ((payload.get("metadata") or {}).get("source_breakdown") or {})
    assert source_breakdown.get("internal_count", 0) >= 1
    assert source_breakdown.get("external_seed_count", 0) >= 1
    assert source_breakdown.get("strategy_applied") == "unified_relevance"


def test_agent_products_search_includes_route_health_metadata(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    class _FakeProduct:
        def model_dump(self):
            return {
                "id": "prod_internal_3",
                "product_id": "prod_internal_3",
                "title": "IPSA Route Health Probe",
                "description": "internal candidate",
                "platform": "shopify",
                "price": 31,
                "in_stock": True,
            }

    async def fake_fetch_all(query: str, values=None):
        text = str(query)
        if "FROM merchant_onboarding" in text:
            return [{"merchant_id": "m_001", "business_name": "Merchant One"}]
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", AsyncMock(return_value=([_FakeProduct()], "cache", None)))
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(agent_api_module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(agent_api_module, "log_product_events", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=ipsa&allow_external_seed=false&allow_stale_cache=false&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    route_health = ((payload.get("metadata") or {}).get("route_health") or {})
    assert route_health.get("primary_path_used") == "cross_merchant_search_standard"
    assert isinstance(route_health.get("primary_latency_ms"), int)
    assert route_health.get("fallback_triggered") is False
    assert route_health.get("fallback_reason") is None
    for key in (
        "segment_fetch_ms",
        "segment_external_seed_ms",
        "segment_filter_ms",
        "segment_hydrate_ms",
        "segment_rank_sort_ms",
        "segment_log_ms",
        "segment_known_total_ms",
        "segment_unattributed_ms",
    ):
        assert isinstance(route_health.get(key), int)
        assert route_health.get(key) >= 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_requires_structured_ingredient_match_for_skin_care(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        niacinamide_serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_niacinamide",
            product_id="prod_serum_niacinamide",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            product_type="Serum",
            ingredient_ids=["niacinamide", "panthenol"],
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        plain_serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_plain",
            product_id="prod_serum_plain",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Plain Serum",
            product_type="Serum",
            price=31.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [plain_serum, niacinamide_serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="niacinamide serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_serum_niacinamide"
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("ingredient_intents") == ["niacinamide"]
    assert metadata.get("matched_ingredient_ids") == ["niacinamide"]
    assert metadata.get("matched_ingredient_labels") == ["Niacinamide"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "ingredient"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_text_recall_surfaces_description_only_ingredient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1659 policy change (soft / text-relevance): a beauty ingredient query is no longer a hard must-have
    # gate that fails closed when structured ingredient_ids are absent. A serum that names the ingredient in
    # its text ("Niacinamide-powered" in the description) now surfaces for "niacinamide serum" via the beauty
    # ingredient text-recall, even on the strict agent_api surface. Structured-evidence products still rank
    # higher; the strict precision gate still rejects external seeds lacking a surface anchor (covered by the
    # hyaluronic/retinol rejection tests).
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        product = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_plain",
            product_id="prod_serum_plain",
            platform="shopify",
            merchant_id=merchant_id,
            title="Winona Soothing Repair Serum",
            product_type="Serum",
            description="Niacinamide-powered care from description only.",
            price=29.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [product], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="niacinamide serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("total") == 1
    product_ids = [p.get("product_id") for p in (result.get("products") or [])]
    assert "prod_serum_plain" in product_ids
    metadata = result.get("metadata") or {}
    assert metadata.get("visible_category_intents") == ["serum"]
    assert metadata.get("ingredient_intents") == ["niacinamide"]
    # Now matched via text-recall (was [] under the old fail-closed policy).
    assert metadata.get("matched_ingredient_ids") == ["niacinamide"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "ingredient"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_allows_external_seed_strict_ingredient_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_niacinamide_1",
                    "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "canonical_url": "https://brand.example/products/the-ordinary-niacinamide",
                    "destination_url": "https://brand.example/products/the-ordinary-niacinamide",
                    "category": "Serum",
                    "price_amount": 12.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                        "description": "Reviewed serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["niacinamide", "zinc_pca"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="niacinamide serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["source"] == "external_seed"
    assert products[0]["orderable"] is False
    assert products[0]["ingredient_ids"] == ["niacinamide", "zinc_pca"]
    assert metadata.get("ingredient_intents") == ["niacinamide"]
    assert metadata.get("matched_ingredient_ids") == ["niacinamide"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "ingredient"
    assert metadata.get("ranking_audit_version") == "beauty_external_ranking_v1"
    assert source_breakdown.get("internal_count") == 0
    assert source_breakdown.get("external_seed_count") == 1
    assert (metadata.get("route_health") or {}).get("ranking_audit_version") == "beauty_external_ranking_v1"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_accepts_prefetched_external_seed_candidates_for_strict_ingredient_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="niacinamide serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {
            "source": "shopping_agent",
            "external_seed_candidates": [
                {
                    "id": "ext_prefetch_1",
                    "product_id": "ext_prefetch_1",
                    "external_seed_id": "seed_prefetch_1",
                    "title": "Watch Ya Tone Niacinamide Dark Spot Serum Refill",
                    "description": "Prefetched external reviewed serum seed.",
                    "price": 22.0,
                    "currency": "USD",
                    "product_type": "Serum",
                    "category": "Serum",
                    "source": "external_seed",
                    "market": "US",
                    "tool": "*",
                    "in_stock": True,
                    "availability": "in_stock",
                    "ingredient_ids": ["niacinamide"],
                    "canonical_url": "https://brand.example/products/watch-ya-tone-refill",
                    "destination_url": "https://brand.example/products/watch-ya-tone-refill",
                    "variants": [
                        {
                            "id": "ext_prefetch_variant_1",
                            "variant_id": "ext_prefetch_variant_1",
                            "title": "Default Title",
                            "price": 22.0,
                            "currency": "USD",
                            "in_stock": True,
                            "availability": "in_stock",
                            "options": [],
                        }
                    ],
                }
            ],
        },
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["source"] == "external_seed"
    assert products[0]["orderable"] is False
    assert products[0]["ingredient_ids"] == ["niacinamide"]
    assert metadata.get("ingredient_intents") == ["niacinamide"]
    assert metadata.get("matched_ingredient_ids") == ["niacinamide"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "ingredient"
    assert source_breakdown.get("internal_count") == 0
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_infers_prefetched_external_seed_serum_category_from_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hyaluronic serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {
            "source": "shopping_agent",
            "external_seed_candidates": [
                {
                    "id": "ext_prefetch_hyaluronic_1",
                    "product_id": "ext_prefetch_hyaluronic_1",
                    "external_seed_id": "seed_hyaluronic_1",
                    "title": "Hyaluronic Acid 2% + B5 (with Ceramides)",
                    "description": "Prefetched external reviewed hyaluronic seed.",
                    "price": 14.0,
                    "currency": "USD",
                    "product_type": "external",
                    "source": "external_seed",
                    "market": "US",
                    "tool": "*",
                    "in_stock": True,
                    "availability": "in_stock",
                    "ingredient_ids": ["hyaluronic_acid", "panthenol"],
                    "canonical_url": "https://theordinary.com/en-us/hyaluronic-acid-2-b5-serum-with-ceramides-100637.html",
                    "destination_url": "https://theordinary.com/en-us/hyaluronic-acid-2-b5-serum-with-ceramides-100637.html",
                    "variants": [
                        {
                            "id": "ext_prefetch_variant_1",
                            "variant_id": "ext_prefetch_variant_1",
                            "title": "Default Title",
                            "price": 14.0,
                            "currency": "USD",
                            "in_stock": True,
                            "availability": "in_stock",
                            "options": [],
                        }
                    ],
                }
            ],
        },
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["source"] == "external_seed"
    assert products[0]["product_type"] == "Serum"
    assert metadata.get("ingredient_intents") == ["hyaluronic_acid"]
    assert metadata.get("matched_ingredient_ids") == ["hyaluronic_acid"]
    assert metadata.get("ingredient_candidate_breakdown") == {
        "eligible_total": 1,
        "eligible_internal": 0,
        "eligible_external_seed": 1,
        "precision_passed_total": 1,
        "precision_passed_internal": 0,
        "precision_passed_external_seed": 1,
    }
    assert source_breakdown.get("internal_count") == 0
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_mixes_internal_and_external_strict_ingredient_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_niacinamide_1",
                    "title": "Niacinamide Serum",
                    "canonical_url": "https://brand.example/products/niacinamide-serum",
                    "destination_url": "https://brand.example/products/niacinamide-serum",
                    "category": "Serum",
                    "price_amount": 18.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Niacinamide Serum",
                        "description": "External reviewed serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["niacinamide"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        internal = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_internal",
            product_id="prod_serum_internal",
            platform="shopify",
            merchant_id=merchant_id,
            title="Niacinamide Serum",
            product_type="Serum",
            ingredient_ids=["niacinamide"],
            price=21.0,
            currency="USD",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [internal], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="niacinamide serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    expected_external_product_id = agent_shop_gateway_module._stable_external_product_id(
        "https://brand.example/products/niacinamide-serum"
    )
    assert result.get("total") == 2
    assert [products[0]["product_id"], products[1]["product_id"]] == [
        "prod_serum_internal",
        expected_external_product_id,
    ]
    assert products[1]["source"] == "external_seed"
    assert metadata.get("matched_ingredient_ids") == ["niacinamide"]
    assert source_breakdown.get("internal_count") == 1
    assert source_breakdown.get("external_seed_count") == 1


def test_extract_skin_care_ingredient_intents_supports_registry_parity_aliases() -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    hyaluronic = agent_shop_gateway_module._extract_skin_care_ingredient_intents(
        "hyaluronic serum",
        query_semantic_class="beauty",
    )
    peptides = agent_shop_gateway_module._extract_skin_care_ingredient_intents(
        "peptide serum",
        query_semantic_class="beauty",
    )
    salicylic = agent_shop_gateway_module._extract_skin_care_ingredient_intents(
        "salicylic serum",
        query_semantic_class="beauty",
    )

    assert hyaluronic == [
        {
            "label": "hyaluronic_acid",
            "ingredient_id": "hyaluronic_acid",
            "display_name": "Hyaluronic Acid",
            "query_terms": ["hyaluronic"],
        }
    ]
    assert peptides == [
        {
            "label": "peptides",
            "ingredient_id": "peptides",
            "display_name": "Peptides",
            "query_terms": ["peptide"],
        }
    ]
    assert salicylic == [
        {
            "label": "salicylic_acid",
            "ingredient_id": "salicylic_acid",
            "display_name": "Salicylic Acid",
            "query_terms": ["salicylic"],
        }
    ]


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_rejects_external_seed_without_target_surface_anchor_for_hyaluronic_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_hyaluronic",
                    "title": "Banana Bright 15% Vitamin C Dark Spot Serum",
                    "canonical_url": "https://ole.example/products/banana-bright-vitamin-c-serum",
                    "destination_url": "https://ole.example/products/banana-bright-vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 70.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Banana Bright 15% Vitamin C Dark Spot Serum",
                        "description": "Reviewed seed with hyaluronic acid in ingredient list.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid", "hyaluronic_acid"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(*args, **kwargs):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hyaluronic acid serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert result.get("total") == 0
    assert metadata.get("ingredient_intents") == ["hyaluronic_acid"]
    assert metadata.get("matched_ingredient_ids") == []
    assert metadata.get("ingredient_precision_mode") == "precision_first_v1"
    assert metadata.get("ingredient_candidate_breakdown") == {
        "eligible_total": 1,
        "eligible_internal": 0,
        "eligible_external_seed": 1,
        "precision_passed_total": 0,
        "precision_passed_internal": 0,
        "precision_passed_external_seed": 0,
    }
    assert metadata.get("ingredient_rejected_reason_summary") == {"competing_surface_anchor": 1}


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_rejects_external_seed_without_target_anchor_for_retinol_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_retargeted",
                    "title": "Vitamin-C Serum",
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Vitamin-C Serum",
                        "description": "Structured row that also carries retinol in reviewed ingredients.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid", "retinol"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(*args, **kwargs):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="retinol serum",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert result.get("total") == 0
    assert metadata.get("ingredient_intents") == ["retinol"]
    assert metadata.get("matched_ingredient_ids") == []
    assert metadata.get("ingredient_precision_mode") == "precision_first_v1"
    assert metadata.get("ingredient_rejected_reason_summary") == {"competing_surface_anchor": 1}


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_requires_structured_shade_match_for_cosmetics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        foundation = agent_shop_gateway_module.StandardProduct(
            id="prod_foundation_1",
            product_id="prod_foundation_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Soft Focus Foundation",
            product_type="Foundation",
            price=39.0,
            currency="USD",
            inventory_quantity=6,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_foundation_210",
                    title="Shade 210 Neutral Beige",
                    price=39.0,
                    inventory_quantity=6,
                    options={"Shade": "210"},
                )
            ],
        )
        lipstick = agent_shop_gateway_module.StandardProduct(
            id="prod_lipstick_1",
            product_id="prod_lipstick_1",
            platform="shopify",
            merchant_id=merchant_id,
            title="Velvet Lipstick",
            product_type="Lipstick",
            price=25.0,
            currency="USD",
            inventory_quantity=9,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
            variants=[
                agent_shop_gateway_module.StandardProductVariant(
                    id="var_lipstick_1",
                    title="Rose Nude",
                    price=25.0,
                    inventory_quantity=9,
                )
            ],
        )
        return [foundation, lipstick], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="foundation shade 210",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["product_id"] == "prod_foundation_1"
    assert metadata.get("visible_category_intents") == ["foundation"]
    assert metadata.get("visible_option_intents") == ["shade_210"]
    assert metadata.get("matched_visible_option_labels") == ["shade_210"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "shade"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_marks_ingredient_budget_queries_as_multi_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_live_1", "business_name": "Live Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        serum = agent_shop_gateway_module.StandardProduct(
            id="prod_serum_vitamin_c",
            product_id="prod_serum_vitamin_c",
            platform="shopify",
            merchant_id=merchant_id,
            title="Vitamin C Repair Serum",
            product_type="Serum",
            ingredient_ids=["ascorbic_acid"],
            price=29.0,
            currency="EUR",
            inventory_quantity=8,
            orderable=True,
            status=agent_shop_gateway_module.ProductStatus.ACTIVE,
        )
        return [serum], "cache_all_platforms", None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert result.get("total") == 1
    assert metadata.get("ingredient_intents") == ["ascorbic_acid"]
    assert metadata.get("matched_ingredient_ids") == ["ascorbic_acid"]
    assert metadata.get("budget_price_max") == 30.0
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "multi_constraint"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_allows_cross_currency_external_seed_budget_match_with_fx_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_1",
                    "title": "Vitamin-C Serum",
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Vitamin-C Serum",
                        "description": "Reviewed vitamin c serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "FROM x402_exchange_rates" in q and (values or {}).get("base_currency") == "USD":
            return {
                "base_currency": "USD",
                "rates": {"EUR": 0.9},
            }
        return None

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["title"] == "Vitamin-C Serum"
    assert products[0]["source"] == "external_seed"
    assert metadata.get("ingredient_intents") == ["ascorbic_acid"]
    assert metadata.get("matched_ingredient_ids") == ["ascorbic_acid"]
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "multi_constraint"
    assert metadata.get("budget_fx_applied") is True
    assert metadata.get("budget_fx_rate") == 0.9
    assert metadata.get("budget_fx_source") == "x402_snapshot_direct"
    assert metadata.get("budget_fx_candidate_currency") == "USD"
    assert metadata.get("budget_fx_unresolved") is False
    assert source_breakdown.get("internal_count") == 0
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_search_source_uses_latest_fx_fallback_for_cross_currency_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_1",
                    "title": "Vitamin-C Serum",
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Vitamin-C Serum",
                        "description": "Reviewed vitamin c serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        return None

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    async def fake_lookup_budget_fx_latest_rate(
        from_currency: str,
        to_currency: str,
    ):
        assert from_currency == "USD"
        assert to_currency == "EUR"
        return 0.9, "latest_rate_api"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_lookup_budget_fx_latest_rate",
        fake_lookup_budget_fx_latest_rate,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="search"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "search"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["title"] == "Vitamin-C Serum"
    assert products[0]["source"] == "external_seed"
    assert metadata.get("query_source") == "cache_multi_intent"
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "multi_constraint"
    assert metadata.get("force_cache_only") is True
    assert metadata.get("budget_currency") == "EUR"
    assert metadata.get("budget_fx_applied") is True
    assert metadata.get("budget_fx_rate") == 0.9
    assert metadata.get("budget_fx_source") == "latest_rate_api"
    assert metadata.get("budget_fx_candidate_currency") == "USD"
    assert metadata.get("budget_fx_unresolved") is False
    assert source_breakdown.get("internal_count") == 0
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_search_source_uses_static_fx_fallback_when_snapshot_and_latest_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_1",
                    "title": "Vitamin-C Serum",
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Vitamin-C Serum",
                        "description": "Reviewed vitamin c serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        return None

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    async def fake_lookup_budget_fx_latest_rate(
        from_currency: str,
        to_currency: str,
    ):
        assert from_currency == "USD"
        assert to_currency == "EUR"
        return None, None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_lookup_budget_fx_latest_rate",
        fake_lookup_budget_fx_latest_rate,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "_BUDGET_FX_STATIC_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_BUDGET_FX_USD_RATES",
        {"USD": 1.0, "EUR": 1.09},
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_BUDGET_FX_STATIC_SOURCE", "static_default")
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="search"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "search"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["title"] == "Vitamin-C Serum"
    assert metadata.get("query_source") == "cache_multi_intent"
    assert metadata.get("budget_currency") == "EUR"
    assert metadata.get("budget_fx_applied") is True
    assert metadata.get("budget_fx_rate") == pytest.approx(1 / 1.09, rel=1e-6)
    assert metadata.get("budget_fx_source") == "static_default"
    assert metadata.get("budget_fx_candidate_currency") == "USD"
    assert metadata.get("budget_fx_unresolved") is False
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_prefetched_external_seed_candidates_use_static_fx_fallback_for_cross_currency_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q or "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        return None

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    async def fake_lookup_budget_fx_latest_rate(
        from_currency: str,
        to_currency: str,
    ):
        assert from_currency == "USD"
        assert to_currency == "EUR"
        return None, None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_lookup_budget_fx_latest_rate",
        fake_lookup_budget_fx_latest_rate,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    monkeypatch.setattr(agent_shop_gateway_module, "_BUDGET_FX_STATIC_FALLBACK_ENABLED", True)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_BUDGET_FX_USD_RATES",
        {"USD": 1.0, "EUR": 1.09},
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_BUDGET_FX_STATIC_SOURCE", "static_default")
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {
            "source": "shopping_agent",
            "external_seed_candidates": [
                {
                    "id": "ext_prefetch_vitc_1",
                    "product_id": "ext_prefetch_vitc_1",
                    "external_seed_id": "seed_vitc_1",
                    "title": "Vitamin-C Serum",
                    "description": "Prefetched external vitamin c serum seed.",
                    "price": 24.0,
                    "currency": "USD",
                    "product_type": "Serum",
                    "category": "Serum",
                    "source": "external_seed",
                    "market": "US",
                    "tool": "*",
                    "in_stock": True,
                    "availability": "in_stock",
                    "ingredient_ids": ["ascorbic_acid"],
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "variants": [
                        {
                            "id": "ext_prefetch_variant_1",
                            "variant_id": "ext_prefetch_variant_1",
                            "title": "Default Title",
                            "price": 24.0,
                            "currency": "USD",
                            "in_stock": True,
                            "availability": "in_stock",
                            "options": [],
                        }
                    ],
                }
            ],
        },
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 1
    assert len(products) == 1
    assert products[0]["title"] == "Vitamin-C Serum"
    assert products[0]["source"] == "external_seed"
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "multi_constraint"
    assert metadata.get("budget_currency") == "EUR"
    assert metadata.get("budget_fx_applied") is True
    assert metadata.get("budget_fx_rate") == pytest.approx(1 / 1.09, rel=1e-6)
    assert metadata.get("budget_fx_source") == "static_default"
    assert metadata.get("budget_fx_candidate_currency") == "USD"
    assert metadata.get("budget_fx_unresolved") is False
    assert source_breakdown.get("external_seed_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_marks_cross_currency_budget_as_unresolved_without_fx_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "seed_vitamin_c_1",
                    "title": "Vitamin-C Serum",
                    "canonical_url": "https://brand.example/products/vitamin-c-serum",
                    "destination_url": "https://brand.example/products/vitamin-c-serum",
                    "category": "Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {
                        "title": "Vitamin-C Serum",
                        "description": "Reviewed vitamin c serum seed.",
                        "category": "Serum",
                        "reviewed_ingredient_ids": ["ascorbic_acid"],
                        "variants": [],
                    },
                }
            ]
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        return None

    async def fake_get_products_hybrid(
        merchant_id: str,
        limit: int,
        agent_id: str,
        background_tasks=None,
        force_cache_only: bool = False,
    ):
        return [], "cache_all_platforms", None

    async def fake_make_external_redirect_url(**kwargs):
        return "https://api.example/r/ext"

    async def fake_lookup_budget_fx_latest_rate(
        from_currency: str,
        to_currency: str,
    ):
        return None, None

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_shop_gateway_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_lookup_budget_fx_latest_rate",
        fake_lookup_budget_fx_latest_rate,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_BUDGET_FX_STATIC_FALLBACK_ENABLED", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    agent_shop_gateway_module._BUDGET_FX_RATE_CACHE.clear()

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vitamin c serum under €30",
            page=1,
            limit=5,
            in_stock_only=True,
            commerce_surface="agent_api",
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    source_breakdown = metadata.get("source_breakdown") or {}
    assert result.get("total") == 0
    assert metadata.get("ingredient_intents") == ["ascorbic_acid"]
    assert metadata.get("matched_ingredient_ids") == []
    assert metadata.get("strict_constraint_query") is True
    assert metadata.get("strict_constraint_reason") == "multi_constraint"
    assert metadata.get("budget_fx_applied") is False
    assert metadata.get("budget_fx_rate") is None
    assert metadata.get("budget_fx_source") is None
    assert metadata.get("budget_fx_candidate_currency") == "USD"
    assert metadata.get("budget_fx_unresolved") is True
    assert source_breakdown.get("external_seed_count") == 0


def _gateway_ranking_seed_row(
    *,
    seed_id: str,
    external_product_id: str,
    title: str,
    canonical_url: str,
    category: str,
    description: str = "",
    visible_attributes: Optional[dict[str, Any]] = None,
    reviewed_ingredient_ids: Optional[list[str]] = None,
    price_amount: float = 20.0,
    source_order: int = 0,
) -> dict[str, Any]:
    return {
        "id": seed_id,
        "external_product_id": external_product_id,
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": canonical_url,
        "canonical_url": canonical_url,
        "domain": canonical_url.split("/")[2],
        "title": title,
        "image_url": None,
        "price_amount": price_amount,
        "price_currency": "USD",
        "availability": "in_stock",
        "source_order": source_order,
        "updated_at": "2026-03-29T00:00:00Z",
        "seed_data": {
            "title": title,
            "description": description,
            "category": category,
            "visible_attributes": visible_attributes or {},
            "reviewed_ingredient_ids": reviewed_ingredient_ids or [],
            "variants": [],
            "brand": "Demo Brand",
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": "2026-03-29T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_preserves_canonical_ranking_for_acne_cleanser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_cleanser_large",
            external_product_id="ext_cleanser_large",
            title="Clarifying Cleanser Larger Size",
            canonical_url="https://example.com/products/clarifying-cleanser-larger-size",
            category="Cleanser",
            description="Clarifying cleanser for blemish-prone skin.",
            visible_attributes={"product_category": ["cleanser"]},
            price_amount=32.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_cleanser_acne",
            external_product_id="ext_cleanser_acne",
            title="Acne Control Clarifying Cleanser",
            canonical_url="https://example.com/products/acne-control-clarifying-cleanser",
            category="Cleanser",
            description="Clarifying cleanser for acne-prone skin and pores.",
            visible_attributes={
                "product_category": ["cleanser"],
                "skin_concern": ["acne", "pores"],
            },
            price_amount=28.0,
            source_order=5,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "acne cleanser"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 10,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="acne cleanser",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert [product.get("title") for product in products[:2]] == [
        "Acne Control Clarifying Cleanser",
        "Clarifying Cleanser Larger Size",
    ]
    assert products[0]["candidate_source"] == "external_seed"
    assert (
        products[0]["ranking_score_breakdown"]["candidate_score"]
        > products[1]["ranking_score_breakdown"]["candidate_score"]
    )
    assert products[0]["ranking_score_breakdown"]["concern_score"] > 0
    assert products[1]["ranking_score_breakdown"]["quality_penalties_total"] > 0
    assert metadata.get("external_seed_returned_count") == 2
    assert metadata.get("ranking_audit_version") == agent_shop_gateway_module.BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_keeps_cleanser_intent_over_treatment_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_acne_mist",
            external_product_id="ext_acne_mist",
            title="Body Acne Clearing Mist with 2% Salicylic Acid",
            canonical_url="https://example.com/products/body-acne-clearing-mist-salicylic-acid",
            category="Treatment",
            description="Targets acne and pores with salicylic acid.",
            visible_attributes={"skin_concern": ["acne", "pores"]},
            price_amount=28.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_acne_cleanser",
            external_product_id="ext_acne_cleanser",
            title="Acne Control Clarifying Cleanser",
            canonical_url="https://example.com/products/acne-control-clarifying-cleanser",
            category="Cleanser",
            description="Clarifying cleanser for acne-prone skin and pores.",
            visible_attributes={
                "product_category": ["cleanser"],
                "skin_concern": ["acne", "pores"],
            },
            price_amount=28.0,
            source_order=5,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "acne cleanser"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 10,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="acne cleanser",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Acne Control Clarifying Cleanser",
        "Body Acne Clearing Mist with 2% Salicylic Acid",
    ]
    assert products[1]["ranking_score_breakdown"]["quality_penalties"]["missing_category_anchor"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_uses_category_anchor_for_spf_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_spf_moisturizer",
            external_product_id="ext_spf_moisturizer",
            title="Superactive Moisturizer SPF 50: Brightening Travel Size",
            canonical_url="https://example.com/products/superactive-moisturizer-spf-50-brightening-travel-size",
            category="Moisturizer",
            description="Daily brightening moisturizer with SPF 50.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=20.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_spf_sunscreen",
            external_product_id="ext_spf_sunscreen",
            title="Mineral Sunscreen SPF 50",
            canonical_url="https://example.com/products/mineral-sunscreen-spf-50",
            category="Sunscreen",
            description="Broad spectrum sunscreen SPF 50.",
            visible_attributes={"product_category": ["sunscreen"]},
            price_amount=24.0,
            source_order=6,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "spf 50"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 9,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="spf 50",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Mineral Sunscreen SPF 50",
        "Superactive Moisturizer SPF 50: Brightening Travel Size",
    ]
    assert (
        products[0]["ranking_score_breakdown"]["candidate_score"]
        > products[1]["ranking_score_breakdown"]["candidate_score"]
    )
    assert products[0]["ranking_score_breakdown"]["category_anchor_score"] > 0
    assert products[1]["ranking_score_breakdown"]["quality_penalties_total"] > 0
    assert products[1]["ranking_score_breakdown"]["quality_penalties"]["missing_sunscreen_category"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_prefers_sunscreen_query_over_spf_moisturizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_spf_moisturizer",
            external_product_id="ext_spf_moisturizer",
            title="Daily Moisturizer SPF 50",
            canonical_url="https://example.com/products/daily-moisturizer-spf-50",
            category="Moisturizer",
            description="Daily moisturizer with SPF 50.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=20.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_sunscreen",
            external_product_id="ext_sunscreen",
            title="Mineral Sunscreen SPF 50",
            canonical_url="https://example.com/products/mineral-sunscreen-spf-50",
            category="Sunscreen",
            description="Broad spectrum sunscreen SPF 50.",
            visible_attributes={"product_category": ["sunscreen"]},
            price_amount=24.0,
            source_order=6,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "sunscreen"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 9,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="sunscreen",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Mineral Sunscreen SPF 50",
        "Daily Moisturizer SPF 50",
    ]
    assert products[1]["ranking_score_breakdown"]["quality_penalties"]["missing_sunscreen_category"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_prefers_full_size_spf_over_travel_without_size_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_travel_spf",
            external_product_id="ext_travel_spf",
            title="Superactive Moisturizer SPF 50: Brightening Travel Size",
            canonical_url="https://example.com/products/superactive-moisturizer-spf-50-brightening-travel-size",
            category="Moisturizer",
            description="Brightening moisturizer with SPF 50.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=20.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_jumbo_spf",
            external_product_id="ext_jumbo_spf",
            title="Dew-Glow Moisturizer SPF 50 - Jumbo",
            canonical_url="https://example.com/products/dew-glow-moisturizer-spf-50-jumbo",
            category="Moisturizer",
            description="Dewy moisturizer with SPF 50.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=40.0,
            source_order=4,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "spf 50"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="spf 50",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Dew-Glow Moisturizer SPF 50 - Jumbo",
        "Superactive Moisturizer SPF 50: Brightening Travel Size",
    ]
    assert products[1]["ranking_score_breakdown"]["quality_penalties"]["travel_size_without_intent"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_penalizes_travel_size_without_size_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_cleanser_travel",
            external_product_id="ext_cleanser_travel",
            title="Ultra Gentle Cream-to-Foam Face Cleanser Travel Size",
            canonical_url="https://example.com/products/ultra-gentle-cleanser-travel",
            category="Cleanser",
            description="Travel size ultra gentle cleanser.",
            visible_attributes={"product_category": ["cleanser"]},
            price_amount=12.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_cleanser_jumbo",
            external_product_id="ext_cleanser_jumbo",
            title="Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
            canonical_url="https://example.com/products/ultra-gentle-cleanser-jumbo",
            category="Cleanser",
            description="Gentle cleanser jumbo size.",
            visible_attributes={"product_category": ["cleanser"]},
            price_amount=24.0,
            source_order=7,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_body_wash_noise",
            external_product_id="ext_body_wash_noise",
            title="Pistachio & Dark Cherry Hand & Body Wash",
            canonical_url="https://example.com/products/pistachio-dark-cherry-hand-body-wash",
            category="Body Wash",
            description="Powered by goat milk, this gentle cleanser creates a warm, foamy lather.",
            price_amount=22.0,
            source_order=2,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "gentle cleanser"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="gentle cleanser",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:3]] == [
        "Ultra Gentle Cream-to-Foam Face Cleanser Jumbo",
        "Ultra Gentle Cream-to-Foam Face Cleanser Travel Size",
        "Pistachio & Dark Cherry Hand & Body Wash",
    ]
    assert (
        products[1]["ranking_score_breakdown"]["quality_penalties"]["travel_size_without_intent"]
        > 0
    )
    assert products[2]["ranking_score_breakdown"]["quality_penalties"]["missing_category_anchor"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_penalizes_spf_moisturizer_without_spf_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_plain_moisturizer",
            external_product_id="ext_plain_moisturizer",
            title="Multi-Peptide Moisturizer",
            canonical_url="https://example.com/products/multi-peptide-moisturizer",
            category="Moisturizer",
            description="Daily peptide moisturizer.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=24.0,
            source_order=7,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_spf_moisturizer",
            external_product_id="ext_spf_moisturizer",
            title="Dew-Glow Moisturizer SPF 50",
            canonical_url="https://example.com/products/dew-glow-moisturizer-spf-50",
            category="Moisturizer",
            description="Daily moisturizer with SPF 50.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=24.0,
            source_order=0,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "moisturizer"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="moisturizer",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Multi-Peptide Moisturizer",
        "Dew-Glow Moisturizer SPF 50",
    ]
    assert products[1]["ranking_score_breakdown"]["quality_penalties"]["sun_protection_without_intent"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_prefers_gel_moisturizer_for_acne_prone_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_gel_moisturizer",
            external_product_id="ext_gel_moisturizer",
            title="Lightweight Gel Moisturizer for Acne-Prone Skin",
            canonical_url="https://example.com/products/lightweight-gel-moisturizer-acne-prone",
            category="Moisturizer",
            description="Oil-free gel moisturizer for acne-prone skin.",
            visible_attributes={
                "product_category": ["moisturizer"],
                "skin_concern": ["acne"],
            },
            price_amount=24.0,
            source_order=5,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_cream_moisturizer",
            external_product_id="ext_cream_moisturizer",
            title="Barrier Repair Cream Moisturizer",
            canonical_url="https://example.com/products/barrier-repair-cream-moisturizer",
            category="Moisturizer",
            description="Rich daily moisturizer.",
            visible_attributes={"product_category": ["moisturizer"]},
            price_amount=26.0,
            source_order=0,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_acne_serum",
            external_product_id="ext_acne_serum",
            title="Acne Treatment Serum",
            canonical_url="https://example.com/products/acne-treatment-serum",
            category="Serum",
            description="Targeted acne treatment serum.",
            visible_attributes={"product_category": ["serum"], "skin_concern": ["acne"]},
            price_amount=21.0,
            source_order=2,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "lightweight gel moisturizer for acne-prone skin"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="lightweight gel moisturizer for acne-prone skin",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:3]] == [
        "Lightweight Gel Moisturizer for Acne-Prone Skin",
        "Barrier Repair Cream Moisturizer",
        "Acne Treatment Serum",
    ]
    assert products[2]["ranking_score_breakdown"]["quality_penalties"]["missing_category_anchor"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_prefers_barrier_moisturizer_over_spf_for_fragrance_free_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_barrier_moisturizer",
            external_product_id="ext_barrier_moisturizer",
            title="Cellular Hydration Barrier Repair Cream Moisturizer",
            canonical_url="https://example.com/products/barrier-repair-cream-moisturizer",
            category="Moisturizer",
            description="Hydrating barrier moisturizer fragrance free.",
            visible_attributes={
                "product_category": ["moisturizer"],
                "skin_concern": ["hydrating"],
                "formula_constraint": ["fragrance_free"],
            },
            price_amount=32.0,
            source_order=3,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_spf_hydrating",
            external_product_id="ext_spf_hydrating",
            title="Superactive Moisturizer SPF 50: Hydrating",
            canonical_url="https://example.com/products/superactive-moisturizer-spf-50-hydrating",
            category="Moisturizer",
            description="Hydrating moisturizer with SPF 50.",
            visible_attributes={
                "product_category": ["moisturizer", "sunscreen"],
                "skin_concern": ["hydrating"],
            },
            price_amount=28.0,
            source_order=0,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "hydrating barrier moisturizer fragrance free"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="hydrating barrier moisturizer fragrance free",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Cellular Hydration Barrier Repair Cream Moisturizer",
        "Superactive Moisturizer SPF 50: Hydrating",
    ]
    penalties = products[1]["ranking_score_breakdown"]["quality_penalties"]
    assert penalties["sun_protection_without_intent"] > 0
    assert penalties["missing_formula_constraint"] > 0


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_external_only_infers_title_ingredient_for_salicylic_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    seed_rows = [
        _gateway_ranking_seed_row(
            seed_id="seed_niacinamide_serum",
            external_product_id="ext_niacinamide_serum",
            title="Niacinamide Serum 12% Plus Zinc 2%",
            canonical_url="https://example.com/products/niacinamide-serum",
            category="",
            description="Serum for visible pores.",
            visible_attributes={},
            price_amount=31.0,
            source_order=9,
        ),
        _gateway_ranking_seed_row(
            seed_id="seed_salicylic_mist",
            external_product_id="ext_salicylic_mist",
            title="Body Acne Clearing Mist with 2% Salicylic Acid",
            canonical_url="https://example.com/products/body-acne-clearing-mist",
            category="",
            description="Targets acne and pores with salicylic acid.",
            visible_attributes={},
            price_amount=28.0,
            source_order=0,
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM orders" in q or "FROM products_cache" in q:
            return []
        return []

    async def fake_fetch_external_seed_rows(**kwargs):
        assert kwargs.get("query") == "salicylic acid serum for acne and pores"
        return {
            "rows": list(seed_rows),
            "query_timeout": False,
            "query_ms": 8,
            "total_count": len(seed_rows),
        }

    async def fake_make_external_redirect_url(**kwargs):
        return f"https://api.example/r/{kwargs['ctx'].get('seedId')}"

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fake_fetch_external_seed_rows,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "_make_external_redirect_url", fake_make_external_redirect_url)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="salicylic acid serum for acne and pores",
            page=1,
            limit=5,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping_agent"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping_agent"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    assert [product.get("title") for product in products[:2]] == [
        "Body Acne Clearing Mist with 2% Salicylic Acid",
        "Niacinamide Serum 12% Plus Zinc 2%",
    ]
    assert products[0]["ranking_score_breakdown"]["active_ingredient_score"] > 0
    assert products[0]["ingredient_ids"] == ["salicylic_acid"]


def _generic_default_cache_row(
    *,
    product_id: str,
    title: str,
    product_type: str = "",
    description: str = "",
    merchant_id: str = "merch_generic_1",
    tags: Optional[list[str]] = None,
) -> dict:
    return {
        "merchant_id": merchant_id,
        "platform": "shopify",
        "platform_product_id": product_id,
        "product_data": {
            "id": product_id,
            "product_id": product_id,
            "platform": "shopify",
            "merchant_id": merchant_id,
            "title": title,
            "description": description,
            "product_type": product_type,
            "tags": list(tags or []),
            "price": 39.0,
            "currency": "USD",
            "inventory_quantity": 12,
            "image_url": f"https://cdn.example.com/{product_id}.jpg",
            "status": "active",
            "orderable": True,
            "variants": [
                {
                    "id": f"var_{product_id}",
                    "sku": f"sku_{product_id}",
                    "title": "Default",
                    "price": 39.0,
                    "inventory_quantity": 12,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_ui_filters_weak_bag_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    cache_rows = [
        _generic_default_cache_row(
            product_id="prod_tote",
            title="Pixi 25th Anniversary Tote Bag",
            product_type="Tote Bag",
            description="Anniversary tote for everyday carry.",
        ),
        _generic_default_cache_row(
            product_id="prod_black_bag",
            title="Puffy Makeup Bag",
            product_type="Bag",
            description="Black quilted cosmetic pouch.",
        ),
        _generic_default_cache_row(
            product_id="prod_traveler",
            title="Puffy Traveler Tote – Black",
            product_type="Tote",
            description="Travel tote in black nylon.",
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_generic_1", "business_name": "Generic Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return list(cache_rows)
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="black leather crossbody bag",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "default"
    assert metadata.get("generic_default_precision_gate_enabled") is True
    assert metadata.get("generic_default_precision_filtered_count") == len(cache_rows)


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_web_filters_partial_term_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    cache_rows = [
        _generic_default_cache_row(
            product_id="prod_palette",
            title="Cool Neutrals Eyeshadow Palette",
            product_type="Palette",
            description="Cool neutral eye looks.",
        ),
        _generic_default_cache_row(
            product_id="prod_tote",
            title="Puffy Carryall Tote – Awaken Confidence",
            product_type="Tote",
            description="Carryall tote for daily essentials.",
        ),
        _generic_default_cache_row(
            product_id="prod_extrait",
            title="L'Art & La Matière VANILLE PLANIFOLIA EXTRAIT 21 – EXTRACT 50 ML / 1.69 OZ",
            product_type="Fragrance",
            description="Vanilla extrait.",
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_generic_1", "business_name": "Generic Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return list(cache_rows)
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="vintage fig accord extrait 1987",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-web"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-web"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "default"
    assert metadata.get("generic_default_precision_gate_enabled") is True
    assert metadata.get("generic_default_precision_filtered_count") == len(cache_rows)


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_agent_source_filters_off_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1659: the agent/MCP surface (search_catalog over /mcp) sends NO metadata.source, so source_normalized
    # is empty. The generic-default precision gate must still apply on that surface — otherwise the
    # OR-over-terms lexical recall leaks off-domain products: e.g. "paula choice" returning a dog harness
    # whose DESCRIPTION merely contains the common word "choice" (no "paula" anywhere on it).
    import routes.agent_shop_gateway as agent_shop_gateway_module

    cache_rows = [
        _generic_default_cache_row(
            product_id="prod_harness",
            title="Comfy Dog Harness for Small to Medium Dogs",
            product_type="Pet Supplies",
            description="The perfect choice for hassle-free daily walks.",  # 'choice' only in description
        ),
        _generic_default_cache_row(
            product_id="prod_paula",
            title="Paula's Choice Skin Perfecting 2% BHA Liquid Exfoliant",
            product_type="Exfoliant",
            description="Leave-on exfoliant.",
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_generic_1", "business_name": "Generic Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return list(cache_rows)
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="paula choice",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source=""),  # MCP/agent sends no source
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": ""},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "default"
    # The fix: the precision gate is now enabled for the empty-source agent/MCP surface.
    assert metadata.get("generic_default_precision_gate_enabled") is True
    product_ids = [p.get("product_id") for p in (result.get("products") or [])]
    # Dog harness (only 'choice' in description, no 'paula') is filtered out.
    assert "prod_harness" not in product_ids
    # Real "Paula's Choice" item (both terms in title) is kept.
    assert "prod_paula" in product_ids


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_strict_surface_filters_off_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1659 (prod repro): the agent/MCP surface routes find_products -> find_products_multi with an explicit
    # commerce_surface, so strict_serving_mode is True — which previously disabled the precision gate and let
    # the OR-over-terms recall leak off-domain products ("paula choice" -> dog harnesses). The gate must apply
    # in strict mode too.
    import routes.agent_shop_gateway as agent_shop_gateway_module

    cache_rows = [
        _generic_default_cache_row(
            product_id="prod_harness",
            title="Comfy Dog Harness for Small to Medium Dogs",
            product_type="Pet Supplies",
            description="The perfect choice for hassle-free daily walks.",
        ),
        _generic_default_cache_row(
            product_id="prod_paula",
            title="Paula's Choice Skin Perfecting 2% BHA Liquid Exfoliant",
            product_type="Exfoliant",
            description="Leave-on exfoliant.",
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_generic_1", "business_name": "Generic Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return list(cache_rows)
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="paula choice",
            page=1,
            limit=10,
            in_stock_only=True,
            commerce_surface="agent_api",  # explicit -> strict_serving_mode True
        ),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"commerce_surface": "agent_api"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "default"
    # The fix: the gate applies even when strict_serving_mode is True (explicit commerce_surface).
    assert metadata.get("generic_default_precision_gate_enabled") is True
    product_ids = [p.get("product_id") for p in (result.get("products") or [])]
    assert "prod_harness" not in product_ids
    assert "prod_paula" in product_ids


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_ui_keeps_high_coverage_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    cache_rows = [
        _generic_default_cache_row(
            product_id="prod_chair",
            title="Ergonomic Office Chair",
            product_type="Office Chair",
            description="Ergonomic chair for desk work.",
        ),
        _generic_default_cache_row(
            product_id="prod_stool",
            title="Standing Desk Stool",
            product_type="Stool",
            description="Adjustable desk stool.",
        ),
    ]

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return [{"merchant_id": "merch_generic_1", "business_name": "Generic Merchant"}]
        if "FROM external_product_seeds" in q or "FROM orders" in q:
            return []
        if "FROM products_cache" in q:
            return list(cache_rows)
        return []

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="ergonomic office chair",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    products = result.get("products") or []
    metadata = result.get("metadata") or {}
    assert [product.get("title") for product in products] == ["Ergonomic Office Chair"]
    assert metadata.get("generic_default_precision_gate_enabled") is True
    assert metadata.get("generic_default_precision_filtered_count") == 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_generic_default_ui_skips_external_seed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_shop_gateway as agent_shop_gateway_module

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM products_cache" in q or "FROM orders" in q:
            return []
        return []

    async def fail_fetch_external_seed_rows(**kwargs):
        raise AssertionError("default generic ui/web queries should not execute external seed search")

    async def fail_prefetched_external_seed_wrappers(request_metadata):
        raise AssertionError("default generic ui/web queries should not load prefetched external seed wrappers")

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        fail_fetch_external_seed_rows,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_build_prefetched_external_seed_wrappers",
        fail_prefetched_external_seed_wrappers,
    )
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="black leather crossbody bag",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    assert result.get("products") == []
    metadata = result.get("metadata") or {}
    assert metadata.get("query_semantic_class") == "default"
    assert metadata.get("external_seed_executed") is False
    assert metadata.get("external_seed_skip_reason") == "semantic_class_blocked"


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_catalog_brand_allows_external_seed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare brand-name query that names a real catalog brand (dynamic dict)
    must NOT be blocked by the default-semantic-class external-seed gate — the
    brand's own external-seed products should be allowed to surface (#1769
    parity on the Python find_products lane)."""
    import routes.agent_shop_gateway as agent_shop_gateway_module
    import routes.agent_api as agent_api_module

    async def fake_fetch_all(query: str, values=None):
        return []

    seed_fetch_calls = {"n": 0}

    async def recording_fetch_external_seed_rows(**kwargs):
        seed_fetch_calls["n"] += 1
        # Empty result keeps the downstream pipeline simple; the assertion under
        # test is that the fetch is ATTEMPTED (gate opened), not blocked.
        return {"rows": [], "query_timeout": False, "total_count": 0}

    async def fake_prefetched_external_seed_wrappers(request_metadata):
        return []

    # Force "acropass" to detect as a catalog brand regardless of the live dict.
    async def fake_ensure_brand_dictionary_loaded():
        return None

    def fake_detect_brand_query(query):
        if "acropass" in str(query or "").lower():
            return {
                "brand_like": True,
                "brand_terms": ["acropass"],
                "mode": "catalog",
                "has_category_hint": False,
                "scope": "broad",
            }
        return {"brand_like": False, "brand_terms": [], "mode": None, "has_category_hint": False, "scope": None}

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "fetch_external_seed_rows",
        recording_fetch_external_seed_rows,
    )
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_build_prefetched_external_seed_wrappers",
        fake_prefetched_external_seed_wrappers,
    )
    monkeypatch.setattr(agent_api_module, "_ensure_brand_dictionary_loaded", fake_ensure_brand_dictionary_loaded)
    monkeypatch.setattr(agent_api_module, "_detect_brand_query", fake_detect_brand_query)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="acropass",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    # Gate opened: brand detected, external-seed fetch attempted, NOT blocked.
    assert metadata.get("brand_query_detected") is True
    assert metadata.get("brand_query_terms") == ["acropass"]
    assert metadata.get("external_seed_skip_reason") != "semantic_class_blocked"
    assert seed_fetch_calls["n"] >= 1


@pytest.mark.asyncio
async def test_shop_gateway_find_products_multi_heuristic_brand_does_not_open_external_seed_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only catalog/static brand detection opens the external-seed gate. The
    looser suffix-pattern heuristic (mode='heuristic') must NOT — it is not a
    proof the brand exists in the catalog, so a default-class query that merely
    looks brand-like stays blocked (no junk widening)."""
    import routes.agent_shop_gateway as agent_shop_gateway_module
    import routes.agent_api as agent_api_module

    async def fake_fetch_all(query: str, values=None):
        return []

    async def fail_fetch_external_seed_rows(**kwargs):
        raise AssertionError("heuristic-only brand detection must not execute external seed search")

    async def fail_prefetched_external_seed_wrappers(request_metadata):
        raise AssertionError("heuristic-only brand detection must not load prefetched external seed wrappers")

    async def fake_ensure_brand_dictionary_loaded():
        return None

    def fake_detect_brand_query(query):
        # Simulate a suffix-pattern heuristic hit (looser signal, unverified).
        return {
            "brand_like": True,
            "brand_terms": ["mystery labs"],
            "mode": "heuristic",
            "has_category_hint": False,
            "scope": "broad",
        }

    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module, "fetch_external_seed_rows", fail_fetch_external_seed_rows)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "_build_prefetched_external_seed_wrappers",
        fail_prefetched_external_seed_wrappers,
    )
    monkeypatch.setattr(agent_api_module, "_ensure_brand_dictionary_loaded", fake_ensure_brand_dictionary_loaded)
    monkeypatch.setattr(agent_api_module, "_detect_brand_query", fake_detect_brand_query)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(agent_shop_gateway_module, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)

    payload = agent_shop_gateway_module.FindProductsMultiPayload(
        search=agent_shop_gateway_module.MultiSearchFilters(
            query="mystery labs",
            page=1,
            limit=10,
            in_stock_only=True,
        ),
        metadata=agent_shop_gateway_module.RequestMetadata(source="shopping-agent-ui"),
    )
    result = await agent_shop_gateway_module._handle_find_products_multi(
        payload,
        {"source": "shopping-agent-ui"},
        agent_shop_gateway_module.BackgroundTasks(),
    )

    metadata = result.get("metadata") or {}
    assert metadata.get("brand_query_detected") is False
    assert metadata.get("external_seed_executed") is False
    assert metadata.get("external_seed_skip_reason") == "semantic_class_blocked"
