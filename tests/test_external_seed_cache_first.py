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
async def test_external_seed_cache_miss_is_non_blocking_and_triggers_async_refresh(
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

    assert products == []
    assert metrics.get("executed") is False
    assert metrics.get("skip_reason") == "cache_miss_async_refresh"
    assert metrics.get("cache_hit") is False
    assert len(agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT) == 1

    await asyncio.gather(*list(agent_api_module._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.values()))

    cache_key = agent_api_module._build_external_seed_cache_key(
        query="fenty gloss",
        market="US",
        strategy="supplement_internal_first",
        surface="beauty",
        limit=20,
    )
    cached = agent_api_module._get_cached_external_seed_products(cache_key)
    assert isinstance(cached, list)
    assert len(cached) == 1
    assert cached[0]["product_id"] == "ext_refresh_1"
    assert live_loader.await_count >= 1
