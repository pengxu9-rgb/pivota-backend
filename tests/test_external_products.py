import asyncio
import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _patch_agent_sdk_ranking(monkeypatch: pytest.MonkeyPatch, module) -> None:
    monkeypatch.setattr(module, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(module, "serialize_features_for_log", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "log_ranking_batch", AsyncMock(return_value=None))
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

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None):
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
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

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

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module, "is_destination_domain_allowed", lambda *_args, **_kwargs: True)

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
    assert route_health.get("primary_path_used") == "agent_sdk_fixed_external_seed"
    assert route_health.get("external_seed_executed") is True
    assert route_health.get("external_seed_query_timeout") is False
    assert "external_seed_returned_count" in metadata


def test_agent_products_search_matches_title_field_for_shopify_rows(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    _patch_agent_sdk_ranking(monkeypatch, agent_sdk_fixed_module)

    captured = {"sql": ""}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM products_cache p" in q:
            captured["sql"] = q
            assert values is not None
            assert values.get("query") == "%ipsa%"
            return [
                {
                    "id": 1,
                    "merchant_id": "merch_test",
                    "platform": "shopify",
                    "platform_product_id": "9886500127048",
                    "product_data": {
                        "title": "IPSA Time Reset Aqua",
                        "description": "Hydrating toner",
                        "price": 45.0,
                    },
                    "cached_at": datetime.now(timezone.utc),
                    "merchant_name": "Test Merchant",
                }
            ]
        if "FROM external_product_seeds" in q:
            return []
        return []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "SELECT merchant_id FROM merchant_onboarding" in q:
            return {"merchant_id": "merch_test"}
        if "SELECT COUNT(*) as total" in q:
            return {"total": 1}
        return None

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        agent_sdk_fixed_module, "_load_external_seed_products_for_search", AsyncMock(return_value=[])
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
    assert "product_data->>'title'" in captured["sql"]


def test_agent_products_search_merchant_scope_does_not_mix_external_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    _patch_agent_sdk_ranking(monkeypatch, agent_sdk_fixed_module)

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

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM products_cache p" in q:
            return [
                {
                    "id": 2,
                    "merchant_id": "merch_test",
                    "platform": "shopify",
                    "platform_product_id": "9886500127048",
                    "product_data": {
                        "title": "IPSA Time Reset Aqua",
                        "description": "Hydrating toner",
                        "price": 45.0,
                    },
                    "cached_at": datetime.now(timezone.utc),
                    "merchant_name": "Test Merchant",
                }
            ]
        return []

    async def fake_fetch_one(query: str, values=None):
        q = str(query)
        if "SELECT merchant_id FROM merchant_onboarding" in q:
            return {"merchant_id": "merch_test"}
        if "SELECT COUNT(*) as total" in q:
            return {"total": 1}
        return None

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        agent_sdk_fixed_module, "_load_external_seed_products_for_search", external_loader
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
    import db.database as db_database_module
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
        if "FROM external_product_seeds" in q:
            values = values or {}
            haystack = " ".join(
                [
                    seed_row.get("title") or "",
                    seed_row.get("domain") or "",
                    seed_row.get("canonical_url") or "",
                    seed_row.get("destination_url") or "",
                    str(seed_row.get("seed_data") or ""),
                ]
            ).lower()

            like_terms = []
            for key, val in values.items():
                if str(key).startswith("like_"):
                    term = str(val).strip("%").lower()
                    if term:
                        like_terms.append(term)
            return [seed_row] if any(t in haystack for t in like_terms) else []

        return []

    monkeypatch.setattr(db_database_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_shop_gateway_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        agent_shop_gateway_module,
        "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT",
        True,
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
    assert any(p.get("source") == "external_seed" for p in products)


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
    assert result.get("metadata", {}).get("query_source") == "agent_products_resolver_fallback_empty"
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
    assert second.get("metadata", {}).get("query_source") == "agent_products_resolver_fallback_empty"
    assert second.get("metadata", {}).get("upstream_response_cache", {}).get("hit") is True
    assert second.get("metadata", {}).get("upstream_response_cache", {}).get("kind") == "error"
    get_products_mock.assert_not_awaited()
    assert invoke_mock.await_count == 1
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
    assert meta.get("query_source") == "agent_products_resolver_fallback_empty"
    assert meta.get("upstream_circuit_open") is True
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
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

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

    res = client.get(
        "/agent/v1/products/search?query=tom%20ford&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)


def test_agent_products_search_external_seed_ignores_stopword_product(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
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

    res = client.get(
        "/agent/v1/products/search?merchant_id=external_seed&query=fenty%20beauty%20product&limit=20&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
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


def test_agent_products_search_delegate_hard_timeout_returns_504(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    async def fake_slow_agent_search_products(**_kwargs):
        await asyncio.sleep(0.3)
        return {
            "status": "success",
            "products": [],
            "pagination": {"total": 0, "limit": 20, "offset": 0, "has_more": False},
        }

    monkeypatch.setattr(agent_api_module, "agent_search_products", fake_slow_agent_search_products)
    monkeypatch.setattr(agent_sdk_fixed_module, "AGENT_SDK_FIXED_DELEGATE_TIMEOUT_SECONDS", 0.05)

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=slow&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 504
    body = res.json()
    assert "Search timeout" in str(body.get("detail"))


def test_agent_sdk_fixed_delegate_path_does_not_double_inject_external_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

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

    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=ipsa&allow_external_seed=true&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
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
    assert products[0].get("merchant_id") != "external_seed"
    assert any(p.get("merchant_id") == "external_seed" for p in products[1:])
    source_breakdown = ((payload.get("metadata") or {}).get("source_breakdown") or {})
    assert source_breakdown.get("internal_count", 0) >= 1
    assert source_breakdown.get("external_seed_count", 0) >= 1
    assert source_breakdown.get("strategy_applied") == "supplement_internal_first"


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
