"""Every products list a search route returns goes through serve_search_products.

agent_search_products' three main returns already did (be#2730 / be#2740). Three more exits served their
products as read, past both the internal-state denylist and the lone-surrogate replacement (be#2740
review P3-5):
  - the cross-merchant browse fast path (products_cache rows, read directly). It is UNREACHABLE today:
    it needs allow_external_seed false, and agent_search_products forces it true ("public recall is
    source-neutral"), so no request can test it; it is wrapped for the day that changes;
  - the SDK route's merchant-scoped legacy fallback (products_cache rows, read directly);
  - the gateway proxy's envelope (the beauty route and the SDK beauty route, behind
    AGENT_BEAUTY_SEARCH_VIA_GATEWAY).
A products_cache row stored as JSON text can hold an escaped lone surrogate; parsed, it fails the
response's encode after the route returns (a 500 for the page).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.agent_search_gateway_proxy as proxy
from main import app

REPLACEMENT = "�"

_PRODUCT_DATA = {
    "id": "p1",
    "product_id": "p1",
    "title": "Night \ud83d Cream",
    "description": "Rich \udc00 cream",
    "in_stock": True,
    "price": 10,
    "currency": "USD",
    "variants": [{"id": "v1", "title": "30ml"}, {"id": "v2", "title": "50ml \ud83d"}],
    "seed_data": {"note": "kept", "price_read": {"source": "internal"}, "manual_overrides": {"title": "x"}},
}


def _cache_row() -> Dict[str, Any]:
    # products_cache.product_data read as JSON text: the escapes parse into raw lone surrogates.
    return {
        "id": 1,
        "merchant_id": "merch_test_1",
        "merchant_name": "Test Merchant",
        "platform": "shopify",
        "platform_product_id": "p1",
        "product_data": json.dumps(_PRODUCT_DATA),
        "cached_at": None,
        "expires_at": None,
    }


def _assert_served(product: Dict[str, Any]) -> None:
    assert product["title"] == f"Night {REPLACEMENT} Cream"
    assert product["description"] == f"Rich {REPLACEMENT} cream"
    assert product["variants"][1]["title"] == f"50ml {REPLACEMENT}"
    assert product["seed_data"] == {"note": "kept"}, "internal state is denied on every exit"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def _db(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api_module
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    monkeypatch.delenv(proxy.FLAG, raising=False)

    async def fake_fetch_all(query: str, values=None):
        return [_cache_row()] if "products_cache" in str(query) else []

    async def fake_fetch_one(query: str, values=None):
        if "merchant_onboarding" in str(query):
            return {"merchant_id": "merch_test_1"}
        return None

    for module in (agent_api_module, agent_sdk_fixed_module):
        monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
        monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)


def _products(res) -> List[Dict[str, Any]]:
    assert res.status_code == 200, res.text[:300]
    return res.json()["products"]


@pytest.mark.usefixtures("_db")
def test_the_sdk_legacy_fallback_serves_its_products(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as agent_api_module

    # The delegate fails, so the merchant-scoped SDK call falls back to its own products_cache read.
    monkeypatch.setattr(agent_api_module, "agent_search_products", AsyncMock(side_effect=RuntimeError("boom")))
    res = client.get(
        "/agent/v1/products/search?merchant_id=merch_test_1&query=cream&limit=5",
        headers={"X-API-Key": "test-api-key"},
    )
    products = _products(res)
    assert len(products) == 1
    _assert_served(products[0])


def test_the_gateway_proxy_envelope_serves_its_products() -> None:
    gateway_product = json.loads(json.dumps(_PRODUCT_DATA))  # what httpx's .json() yields from escapes
    body = {"products": [gateway_product], "total": 1, "metadata": {}}
    before = copy.deepcopy(body)
    env = proxy.to_backend_envelope(
        body, limit=20, offset=0, query="q", category=None, min_price=None, max_price=None,
        in_stock_only=False, merchant_id=None, merchant_ids=None,
    )
    _assert_served(env["products"][0])
    assert body == before, "the gateway body is not mutated"
    json.dumps(env, ensure_ascii=False).encode("utf-8")  # the response encodes
