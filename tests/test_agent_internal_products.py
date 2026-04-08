from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.agent_internal_products as module


def _build_client(context):
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_agent_context] = lambda: context
    return TestClient(app), app


def test_internal_products_search_rejects_orchestration_fields() -> None:
    context = SimpleNamespace(
        can_access_merchant=lambda _merchant_id: True,
        allowed_merchants=None,
    )
    client, app = _build_client(context)
    try:
        response = client.post(
            "/agent/internal/products/search",
            json={
                "query": "oil control treatment",
                "semantic_contract": {"owner": "beauty"},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "INVALID_INTERNAL_PRODUCTS_SEARCH_REQUEST"
    assert "semantic_contract" in body["forbidden_fields"]


def test_internal_products_search_uses_fast_cache_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def _fake_fast_mode_search(**kwargs):
        captured.update(kwargs)
        return {
            "products": [
                {
                    "id": "prod_1",
                    "product_id": "prod_1",
                    "merchant_id": "merch_1",
                    "title": "Oil Control Serum",
                    "in_stock": True,
                }
            ],
            "total": 1,
            "source_breakdown": {
                "internal_count": 1,
                "external_seed_count": 0,
                "stale_cache_used": False,
                "strategy_applied": "legacy",
            },
        }

    monkeypatch.setattr(module.agent_api, "_search_products_fast_mode", _fake_fast_mode_search)

    context = SimpleNamespace(
        can_access_merchant=lambda _merchant_id: True,
        allowed_merchants=None,
    )
    client, app = _build_client(context)
    try:
        response = client.post(
            "/agent/internal/products/search",
            headers={
                "X-Internal-Search-Timeout-Ms": "4800",
                "X-Trace-ID": "trace_123",
                "X-Internal-Caller-Lane": "beauty_discovery_mainline",
            },
            json={
                "query": "oil control treatment",
                "limit": 6,
                "offset": 0,
                "search_all_merchants": True,
                "catalog_surface": "beauty",
                "in_stock_only": True,
                "allow_external_seed": False,
                "external_seed_strategy": "legacy",
                "semantic_family": "framework_generic",
                "target_step_family": "treatment",
                "query_step_strength": "primary",
                "product_only": True,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["products"]) == 1
    assert body["metadata"]["endpoint_kind"] == "internal_primitive"
    assert body["metadata"]["transport_owner"] == "internal_products_search_primitive"
    assert body["metadata"]["trace_id"] == "trace_123"
    assert body["metadata"]["caller_lane"] == "beauty_discovery_mainline"
    assert body["metadata"]["query_target_step_family"] == "treatment"
    assert body["metadata"]["semantic_family"] == "framework_generic"
    assert captured["catalog_surface"] == "beauty"
    assert captured["query"] == "oil control treatment"
    assert captured["allow_stale_cache"] is False
    assert captured["allow_external_seed"] is False
    assert captured["query_semantic_class"] == "beauty"


def test_internal_products_search_checks_merchant_access() -> None:
    context = SimpleNamespace(
        can_access_merchant=lambda merchant_id: merchant_id == "merch_allowed",
        allowed_merchants=["merch_allowed"],
    )
    client, app = _build_client(context)
    try:
        response = client.post(
            "/agent/internal/products/search",
            json={
                "query": "oil control treatment",
                "merchant_id": "merch_blocked",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
