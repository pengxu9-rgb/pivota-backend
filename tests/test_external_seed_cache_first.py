import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_external_seed_cache_hit_serves_first_screen_without_live_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", True)

    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE.clear()
    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()

    cache_key = agent_api_module._build_external_seed_cache_key(
        query="ipsa toner",
        market="US",
        strategy="supplement_internal_first",
        surface="beauty",
        limit=20,
        hard_rule_prune=bool(agent_api_module.SEARCH_EXTERNAL_HARD_RULE_PRUNE),
    )
    cached_item = {
        "id": "ext_cache_1",
        "product_id": "ext_cache_1",
        "merchant_id": "external_seed",
        "source": "external_seed",
        "title": "Cached external seed",
    }
    agent_api_module._put_cached_external_seed_products(cache_key, [cached_item])

    live_loader = AsyncMock(return_value=[cached_item])
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", live_loader)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_with_cache(
        req=None,
        query="ipsa toner",
        limit=20,
        build_budget_ms=400,
        build_concurrency=4,
        include_seed_data_text_match=False,
        normalized_seed_strategy="supplement_internal_first",
        normalized_catalog_surface="beauty",
        page_offset=0,
        metrics_out=metrics,
    )

    assert len(products) == 1
    assert products[0]["product_id"] == "ext_cache_1"
    assert metrics.get("executed") is False
    assert metrics.get("skip_reason") == "cache_hit"
    assert metrics.get("cache_hit") is True
    assert int(metrics.get("rows_built") or 0) == 1
    live_loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_seed_cache_miss_sync_fills_before_async_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", True)

    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE.clear()
    agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()

    refreshed_item = {
        "id": "ext_refresh_1",
        "product_id": "ext_refresh_1",
        "merchant_id": "external_seed",
        "source": "external_seed",
        "title": "Refreshed external seed",
    }
    live_loader = AsyncMock(return_value=[refreshed_item])
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", live_loader)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_with_cache(
        req=None,
        query="fenty gloss",
        limit=20,
        build_budget_ms=400,
        build_concurrency=4,
        include_seed_data_text_match=False,
        normalized_seed_strategy="supplement_internal_first",
        normalized_catalog_surface="beauty",
        page_offset=0,
        metrics_out=metrics,
    )

    assert len(products) == 1
    assert products[0]["product_id"] == "ext_refresh_1"
    assert metrics.get("executed") is True
    assert metrics.get("skip_reason") == "cache_miss_sync_filled"
    assert metrics.get("cache_hit") is False
    assert len(agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT) == 0

    cache_key = agent_api_module._build_external_seed_cache_key(
        query="fenty gloss",
        market="US",
        strategy="supplement_internal_first",
        surface="beauty",
        limit=20,
        hard_rule_prune=bool(agent_api_module.SEARCH_EXTERNAL_HARD_RULE_PRUNE),
    )
    cached = agent_api_module._get_cached_external_seed_products(cache_key)
    assert isinstance(cached, list)
    assert len(cached) == 1
    assert cached[0]["product_id"] == "ext_refresh_1"
    assert live_loader.await_count >= 1


@pytest.mark.asyncio
async def test_brand_broad_fallback_triggers_when_strict_rows_have_no_brand_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    strict_rows = [
        {
            "id": "seed_strict_1",
            "external_product_id": "seed_strict_1",
            "market": "US",
            "title": "Generic beauty serum",
            "destination_url": "https://example.com/serum",
            "seed_data": {"brand": "Generic"},
        }
    ]
    broad_rows = [
        {
            "id": "seed_broad_1",
            "external_product_id": "seed_broad_1",
            "market": "CA",
            "title": "Fenty Beauty Eau de Parfum",
            "destination_url": "https://example.com/fenty",
            "seed_data": {"brand": "Fenty Beauty"},
        }
    ]

    calls = []

    async def fake_fetch_external_seed_rows(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "rows": strict_rows,
                "total_count": len(strict_rows),
                "query_ms": 5,
                "query_timeout": False,
                "table_missing": False,
            }
        return {
            "rows": broad_rows,
            "total_count": len(broad_rows),
            "query_ms": 6,
            "query_timeout": False,
            "table_missing": False,
        }

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains, metrics_out=None):
        return {
            "id": str(seed_row.get("external_product_id")),
            "product_id": str(seed_row.get("external_product_id")),
            "merchant_id": "external_seed",
            "source": "external_seed",
            "title": str(seed_row.get("title") or ""),
            "seed_data": seed_row.get("seed_data") or {},
        }

    monkeypatch.setattr(agent_api_module, "fetch_external_seed_rows", fake_fetch_external_seed_rows)
    monkeypatch.setattr(agent_api_module, "dedupe_external_seed_rows", lambda rows, limit: list(rows)[:limit])
    monkeypatch.setattr(agent_api_module, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="fenty beauty",
        limit=20,
        page_offset=0,
        build_budget_ms=200,
        build_concurrency=2,
        include_seed_data_text_match=True,
        enable_broad_fallback=True,
        brand_terms=["fenty beauty"],
        brand_required_terms=["fenty"],
        brand_prefer_terms=["fenty beauty"],
        brand_query_detected=True,
        metrics_out=metrics,
    )

    assert len(products) == 1
    assert products[0]["product_id"] == "seed_broad_1"
    assert metrics.get("brand_strict_rows") == 1
    assert metrics.get("brand_relevant_rows") == 0
    assert metrics.get("broad_scope_fallback_used") is True
    assert metrics.get("broad_scope_rows_fetched") == 1
    assert any(call.get("market") is None for call in calls)
