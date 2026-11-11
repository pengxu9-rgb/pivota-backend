"""GET /agent/v1/products/{merchant}/{SKU-*} must not fabricate a product (FIX_PLAN D-10).

The route used to answer ANY `SKU-` id with a made-up "Platform Order Product" at 10.00 USD with stock
999, before looking anything up. An unknown SKU id now gets the same 404 as any other unknown id, and a
SKU id the catalog does hold is served from the catalog like any other.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_api as agent_api_module

    calls = {"cache": [], "upstream": []}
    found = {}

    async def fake_cache(*, merchant_id, product_ref, include_expired=False, stale_cutoff=None):
        calls["cache"].append((merchant_id, product_ref, include_expired))
        return None if include_expired else found.get(product_ref)

    async def fake_upstream(*, merchant_id, product_ref, context):
        calls["upstream"].append((merchant_id, product_ref))
        return {"product": None, "query_source": None}

    monkeypatch.setattr(agent_api_module, "_load_cached_product_for_agent_detail", fake_cache)
    monkeypatch.setattr(agent_api_module, "_load_upstream_product_for_agent_detail", fake_upstream)
    monkeypatch.setattr(agent_api_module, "_context_can_access_merchant", lambda _ctx, _mid: True)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_FALLBACK_ENABLED", True)
    monkeypatch.setattr(agent_api_module, "AGENT_PRODUCT_DETAIL_STALE_MAX_AGE_HOURS", 336)
    return calls, found


@pytest.mark.parametrize("product_id", ["SKU-12345", "SKU-ABC-1", "SKU-"])
def test_an_unknown_sku_id_is_the_normal_not_found(client, lookups, product_id):
    calls, _ = lookups
    response = client.get(f"/agent/v1/products/merch_test/{product_id}", headers={"X-API-Key": "test-api-key"})
    assert response.status_code == 404
    assert response.json().get("detail") == "Product not found"
    body = response.text
    assert "Platform Order Product" not in body and "999" not in body
    # It took the ordinary lookup: fresh cache, then upstream, then the bounded stale cache.
    assert calls["cache"] == [("merch_test", product_id, False), ("merch_test", product_id, True)]
    assert calls["upstream"] == [("merch_test", product_id)]


def test_a_sku_id_the_catalog_holds_is_served_from_the_catalog(client, lookups):
    _, found = lookups
    found["SKU-777"] = {"id": "SKU-777", "product_id": "SKU-777", "title": "Real Serum", "price": 24.0, "currency": "EUR"}
    response = client.get("/agent/v1/products/merch_test/SKU-777", headers={"X-API-Key": "test-api-key"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["detail_source"] == "fresh_cache"
    assert payload["product"]["title"] == "Real Serum"
    assert payload["product"]["price"] == 24.0
    assert payload["product"].get("stock") != 999
