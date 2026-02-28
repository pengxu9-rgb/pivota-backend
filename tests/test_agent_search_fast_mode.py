import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_agent_search_fast_mode_returns_route_health(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    merchant_rows = [
        {
            "merchant_id": "merch_1",
            "business_name": "Merchant One",
        }
    ]
    product_rows = [
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_ipsa_1",
                "product_id": "prod_ipsa_1",
                "title": "IPSA Time Reset Aqua",
                "description": "hydrating lotion",
                "price": 45,
                "currency": "USD",
                "in_stock": True,
            },
            "cached_at": "2026-02-20T00:00:00Z",
            "expires_at": "2026-02-28T00:00:00Z",
        }
    ]

    async def fake_fetch_all(query, values=None):
        text = str(query)
        if "SELECT merchant_id, business_name FROM merchant_onboarding" in text:
            return merchant_rows
        if "FROM products_cache pc" in text and "ORDER BY pc.cached_at" in text:
            return product_rows
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/search",
        params={
            "query": "ipsa",
            "limit": 10,
            "offset": 0,
            "search_all_merchants": "true",
            "fast_mode": "true",
            "allow_external_seed": "false",
            "allow_stale_cache": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert len(payload["products"]) == 1
    assert payload["products"][0]["title"] == "IPSA Time Reset Aqua"
    assert payload["metadata"].get("reason_code") in {"ok", "no_candidates"}
    route_health = payload["metadata"]["route_health"]
    assert route_health["primary_path_used"] == "cross_merchant_search_fast_mode"
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
    assert payload["metadata"]["source_breakdown"]["internal_count"] == 1


def test_agent_search_beauty_alias_filters_non_beauty_products(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    merchant_rows = [{"merchant_id": "merch_1", "business_name": "Merchant One"}]
    product_rows = [
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_beauty_1",
                "product_id": "prod_beauty_1",
                "title": "Beauty Product Serum",
                "description": "hydrating serum",
                "product_type": "serum",
                "price": 45,
                "currency": "USD",
                "in_stock": True,
            },
            "cached_at": "2026-02-20T00:00:00Z",
            "expires_at": "2026-02-28T00:00:00Z",
        },
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_non_beauty_1",
                "product_id": "prod_non_beauty_1",
                "title": "Gaming Laptop Product",
                "description": "high performance notebook",
                "product_type": "electronics",
                "price": 999,
                "currency": "USD",
                "in_stock": True,
            },
            "cached_at": "2026-02-20T00:00:00Z",
            "expires_at": "2026-02-28T00:00:00Z",
        },
    ]

    async def fake_fetch_all(query, values=None):
        text = str(query)
        if "SELECT merchant_id, business_name FROM merchant_onboarding" in text:
            return merchant_rows
        if "FROM products_cache pc" in text and "ORDER BY pc.cached_at" in text:
            return product_rows
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/beauty/products/search",
        params={
            "query": "product",
            "limit": 10,
            "offset": 0,
            "search_all_merchants": "true",
            "fast_mode": "true",
            "allow_external_seed": "false",
            "allow_stale_cache": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert len(payload["products"]) == 1
    assert payload["products"][0]["product_id"] == "prod_beauty_1"
    assert payload["search_context"]["catalog_surface"] == "beauty"
    assert payload["filters_applied"]["catalog_surface"] == "beauty"
    assert payload["metadata"]["catalog_surface"] == "beauty"


def test_agent_search_catalog_surface_param_filters_non_beauty_products(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    merchant_rows = [{"merchant_id": "merch_1", "business_name": "Merchant One"}]
    product_rows = [
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_beauty_2",
                "product_id": "prod_beauty_2",
                "title": "Hydrating Beauty Essence",
                "description": "moisturizing essence",
                "product_type": "essence",
                "price": 28,
                "currency": "USD",
                "in_stock": True,
            },
            "cached_at": "2026-02-20T00:00:00Z",
            "expires_at": "2026-02-28T00:00:00Z",
        },
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_non_beauty_2",
                "product_id": "prod_non_beauty_2",
                "title": "Office Chair",
                "description": "ergonomic mesh chair",
                "product_type": "furniture",
                "price": 120,
                "currency": "USD",
                "in_stock": True,
            },
            "cached_at": "2026-02-20T00:00:00Z",
            "expires_at": "2026-02-28T00:00:00Z",
        },
    ]

    async def fake_fetch_all(query, values=None):
        text = str(query)
        if "SELECT merchant_id, business_name FROM merchant_onboarding" in text:
            return merchant_rows
        if "FROM products_cache pc" in text and "ORDER BY pc.cached_at" in text:
            return product_rows
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/search",
        params={
            "query": "hydrating",
            "catalog_surface": "beauty",
            "limit": 10,
            "offset": 0,
            "search_all_merchants": "true",
            "fast_mode": "true",
            "allow_external_seed": "false",
            "allow_stale_cache": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert len(payload["products"]) == 1
    assert payload["products"][0]["product_id"] == "prod_beauty_2"
    assert payload["metadata"]["catalog_surface"] == "beauty"


def test_agent_sdk_fixed_search_route_accepts_limit_200(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    async def fake_agent_search_products(**_kwargs):
        return {
            "status": "success",
            "products": [],
            "pagination": {
                "total_count": 0,
                "limit": 200,
                "offset": 0,
                "has_more": False,
            },
            "metadata": {
                "reason_code": "ok",
                "route_health": {"primary_path_used": "test_delegate"},
            },
        }

    monkeypatch.setattr(agent_api_module, "agent_search_products", fake_agent_search_products)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/search",
        params={
            "query": "ipsa",
            "limit": 200,
            "offset": 0,
            "search_all_merchants": "true",
            "allow_external_seed": "false",
            "allow_stale_cache": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["pagination"]["limit"] == 200
    assert payload["metadata"]["source"] == "agent_sdk_fixed_delegate"
