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
    assert payload["metadata"]["route_health"]["primary_path_used"] == "cross_merchant_search_fast_mode"
    assert payload["metadata"]["source_breakdown"]["internal_count"] == 1
