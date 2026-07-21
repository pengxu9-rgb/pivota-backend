from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    try:
        from main import app
    except RuntimeError as exc:
        if "Repo governance violation" in str(exc):
            pytest.skip("main app import requires a clean repo without duplicate _tmp order_routes.py")
        raise
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
    for key in (
        "orchestrator_path",
        "decision_node",
        "domain_filter_dropped_external",
        "external_fill_gate_reason",
        "semantic_retry_applied",
        "semantic_retry_query",
        "semantic_retry_hits",
        "external_seed_brand_strict_rows",
        "external_seed_brand_relevant_rows",
        "external_seed_broad_fallback_used",
        "external_seed_broad_scope_rows",
    ):
        assert key in route_health
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
    assert payload["metadata"]["query_semantic_class"] == "beauty"


def test_agent_search_beauty_surface_defaults_to_unified_external_seed_strategy(
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
                "id": "prod_beauty_3",
                "product_id": "prod_beauty_3",
                "title": "Lightweight Face Sunscreen SPF 50",
                "description": "broad spectrum sunscreen for oily skin",
                "product_type": "sunscreen",
                "category": "face sunscreen",
                "price": 24,
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
            "query": "best sunscreen for oily skin",
            "catalog_surface": "beauty",
            "limit": 10,
            "offset": 0,
            "search_all_merchants": "true",
            "fast_mode": "true",
            "allow_external_seed": "true",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["metadata"]["query_semantic_class"] == "beauty"
    assert payload["metadata"]["source_breakdown"]["strategy_applied"] == "unified_relevance"


def test_agent_search_fast_mode_beauty_query_applies_cache_prefilter_terms(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    merchant_rows = [{"merchant_id": "merch_1", "business_name": "Merchant One"}]
    observed = {}
    product_rows = [
        {
            "merchant_id": "merch_1",
            "merchant_name": "Merchant One",
            "product_data": {
                "id": "prod_sunscreen_1",
                "product_id": "prod_sunscreen_1",
                "title": "Lightweight Face Sunscreen SPF 50",
                "description": "broad spectrum sunscreen for oily skin",
                "product_type": "sunscreen",
                "category": "face sunscreen",
                "price": 24,
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
            observed["query"] = text
            observed["values"] = dict(values or {})
            return product_rows
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/search",
        params={
            "query": "best sunscreen for oily skin",
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
    assert payload["products"][0]["product_id"] == "prod_sunscreen_1"
    values = observed.get("values") or {}
    like_values = {key: value for key, value in values.items() if str(key).startswith("cache_like_")}
    assert like_values
    assert "%sunscreen%" in like_values.values()
    assert "%spf%" in like_values.values()
    assert "LOWER(COALESCE(pc.product_data->>'title','')) LIKE :cache_like_0" in observed.get("query", "")


def test_agent_search_standard_beauty_prefers_sunscreen_over_serum_candidates(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    class DummyProduct:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self):
            return dict(self.payload)

    merchant_rows = [{"merchant_id": "merch_1", "business_name": "Merchant One"}]

    async def fake_fetch_all(query, values=None):
        text = str(query)
        if "SELECT merchant_id, business_name FROM merchant_onboarding" in text:
            return merchant_rows
        return []

    async def fake_get_products_hybrid(*args, **kwargs):
        return (
            [
                DummyProduct(
                    {
                        "id": "prod_serum_1",
                        "product_id": "prod_serum_1",
                        "title": "Niacinamide 10% + Zinc 1% Serum",
                        "description": "oil control serum",
                        "product_type": "serum",
                        "category": "serum",
                        "price": 12,
                        "currency": "USD",
                        "in_stock": True,
                    }
                ),
                DummyProduct(
                    {
                        "id": "prod_sunscreen_1",
                        "product_id": "prod_sunscreen_1",
                        "title": "Lightweight Face Sunscreen SPF 50",
                        "description": "broad spectrum sunscreen for oily skin",
                        "product_type": "sunscreen",
                        "category": "face sunscreen",
                        "price": 24,
                        "currency": "USD",
                        "in_stock": True,
                    }
                ),
            ],
            "cache",
            None,
        )

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))

    response = client.get(
        "/agent/v1/products/search",
        params={
            "query": "best sunscreen for oily skin",
            "catalog_surface": "beauty",
            "limit": 10,
            "offset": 0,
            "search_all_merchants": "true",
            "allow_external_seed": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["products"][0]["product_id"] == "prod_sunscreen_1"
    product_ids = [item["product_id"] for item in payload["products"]]
    assert "prod_serum_1" not in product_ids or product_ids.index("prod_sunscreen_1") < product_ids.index("prod_serum_1")
    assert payload["metadata"]["catalog_surface"] == "beauty"


def test_agent_sdk_fixed_search_route_clamps_limit_201_to_200(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    import routes.agent_api as agent_api_module

    observed = {}

    async def fake_agent_search_products(**kwargs):
        observed["limit"] = kwargs.get("limit")
        return {
            "status": "success",
            "products": [],
            "pagination": {
                "total_count": 0,
                "limit": kwargs.get("limit"),
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
            "limit": 201,
            "offset": 0,
            "search_all_merchants": "true",
            "allow_external_seed": "false",
            "allow_stale_cache": "false",
        },
        headers={"X-API-Key": "test-api-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert observed.get("limit") == 200
    assert payload["status"] == "success"
    assert payload["pagination"]["limit"] == 200
    assert payload["metadata"]["source"] == "agent_sdk_fixed_delegate"
