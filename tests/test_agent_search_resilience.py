from fastapi import BackgroundTasks
import pytest
from starlette.requests import Request


class _FakeProduct:
    def __init__(self, payload):
        self._payload = payload

    def dict(self):
        return dict(self._payload)


class _FakeContext:
    agent_id = "agent_test"
    session_id = "session_test"
    allowed_merchants = ["merch_test"]

    def can_access_merchant(self, _merchant_id: str) -> bool:
        return True


async def _noop_async(*_args, **_kwargs):
    return None


def _fake_request(path: str = "/agent/v1/products/search") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_agent_search_products_skips_malformed_product_rows(monkeypatch):
    import routes.agent_api as agent_api_module

    async def fake_verify_merchant_active(_merchant_id: str):
        return {"merchant_id": "merch_test", "business_name": "Test Merchant"}

    async def fake_get_products_hybrid(**_kwargs):
        products = [
            _FakeProduct(
                {
                    "id": "bad_1",
                    "title": None,
                    "description": None,
                    "price": "N/A",
                    "tags": [None, 123],
                    "product_type": None,
                    "platform": "shopify",
                    "in_stock": True,
                }
            ),
            _FakeProduct(
                {
                    "id": "good_1",
                    "title": "The Ordinary Niacinamide 10% + Zinc 1%",
                    "description": "Balancing serum",
                    "price": 6.9,
                    "tags": ["niacinamide", "serum"],
                    "product_type": "skincare",
                    "platform": "shopify",
                    "in_stock": True,
                }
            ),
        ]
        return products, "cache", None

    monkeypatch.setattr(agent_api_module, "verify_merchant_active", fake_verify_merchant_active)
    monkeypatch.setattr(agent_api_module, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_for_search", _noop_async)
    monkeypatch.setattr(agent_api_module, "hydrate_quality_and_enrichment", _noop_async)
    monkeypatch.setattr(agent_api_module, "passes_agent_gating", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(agent_api_module, "compute_agent_ranking_score", lambda *_args, **_kwargs: 0.9)
    monkeypatch.setattr(
        agent_api_module,
        "serialize_features_for_log",
        lambda *_args, **_kwargs: {"source": "test"},
    )
    monkeypatch.setattr(agent_api_module, "log_product_events", _noop_async)
    monkeypatch.setattr(agent_api_module, "log_agent_request", _noop_async)

    payload = await agent_api_module.agent_search_products(
        req=_fake_request(),
        background_tasks=BackgroundTasks(),
        merchant_id="merch_test",
        merchant_ids=None,
        search_all_merchants=False,
        query="niacinamide",
        category=None,
        min_price=None,
        max_price=None,
        in_stock_only=False,
        limit=20,
        offset=0,
        context=_FakeContext(),
    )

    assert payload.get("status") == "success"
    products = payload.get("products") or []
    ids = {str(p.get("id") or p.get("product_id")) for p in products}
    assert "good_1" in ids
