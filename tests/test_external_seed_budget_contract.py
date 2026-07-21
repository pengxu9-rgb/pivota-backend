import asyncio

import pytest
from fastapi.testclient import TestClient


def test_external_seed_query_timeout_is_visible_in_route_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module
    try:
        from main import app
    except RuntimeError as exc:
        if "Repo governance violation" in str(exc):
            pytest.skip("main app import requires a clean repo without duplicate _tmp order_routes.py")
        raise

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            return []
        if "FROM external_product_seeds" in q:
            await asyncio.sleep(0.2)
            return []
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", False)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS", 0.05)

    client = TestClient(app)
    res = client.get(
        "/agent/v1/products/search?search_all_merchants=true&query=ipsa&allow_external_seed=true&in_stock_only=false&limit=10&offset=0",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    route_health = ((payload.get("metadata") or {}).get("route_health") or {})
    assert route_health.get("external_seed_query_timeout") is True
    assert route_health.get("external_seed_executed") is True
    assert route_health.get("external_seed_skip_reason") == "query_timeout"


@pytest.mark.asyncio
async def test_external_seed_loader_marks_query_timeout_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    async def slow_fetch_all(_query: str, values=None):
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", slow_fetch_all)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS", 0.05)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="timeout probe",
        limit=20,
        build_budget_ms=400,
        build_concurrency=4,
        metrics_out=metrics,
    )

    assert products == []
    assert metrics.get("query_timeout") is True
    assert metrics.get("executed") is True
    assert metrics.get("skip_reason") == "query_timeout"
    assert int(metrics.get("rows_fetched") or 0) == 0
    assert int(metrics.get("rows_built") or 0) == 0


@pytest.mark.asyncio
async def test_external_seed_budget_contract_rows_built_never_exceed_rows_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    seed_rows = []
    for i in range(6):
        seed_rows.append(
            {
                "id": f"seed_{i}",
                "external_product_id": f"ext_{i}",
                "market": "US",
                "destination_url": f"https://example.com/p/{i}",
                "canonical_url": f"https://example.com/p/{i}",
                "domain": "example.com",
                "title": f"External Product {i}",
                "seed_data": {},
            }
        )

    async def fast_fetch_all(_query: str, values=None):
        return seed_rows

    async def slow_build_external_seed_product(*, req, seed_row, allowed_domains=None):
        await asyncio.sleep(0.06)
        return {
            "id": seed_row.get("external_product_id"),
            "product_id": seed_row.get("external_product_id"),
            "merchant_id": "external_seed",
            "source": "external_seed",
        }

    async def fake_allowlist_loader(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fast_fetch_all)
    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", slow_build_external_seed_product)
    monkeypatch.setattr(agent_api_module, "get_allowed_domains_for_market", fake_allowlist_loader)
    monkeypatch.setattr(agent_api_module, "AGENT_EXTERNAL_SEED_QUERY_TIMEOUT_SECONDS", 1.0)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="example product",
        limit=6,
        build_budget_ms=90,
        build_concurrency=1,
        metrics_out=metrics,
    )

    assert isinstance(products, list)
    assert int(metrics.get("rows_fetched") or 0) >= int(metrics.get("rows_built") or 0)
    assert int(metrics.get("rows_built") or 0) <= len(products)
    assert int(metrics.get("rows_built") or 0) <= int(metrics.get("rows_fetched") or 0)
