"""THE GATEWAY'S DISCOVERY-FALLBACK HOP, AS IT IS SENT, AGAINST THE REAL SDK ROUTE (2026-10-09).

PIVOTA-Agent's discovery feed rebuilds a non-US buyer's page from this door
(`fetchBuyerMarketSearchRows` -> `GET /agent/v1/products/search`). With the gateway search proxy on
(`AGENT_BEAUTY_SEARCH_VIA_GATEWAY`, on in prod) this route forwards the caller's query string back to
the gateway's REST door, and refuses `limit > 100` or an offset that is not a multiple of the limit
with HTTP 422 `gateway_pagination_unsupported` BEFORE any search runs. The first hop sent the browse
candidate limit (120) and every hop was 422, read by the feed as "0 rows" for a day. This pins the
hop's exact shape (one page, offset 0, `serving_market`) as accepted, the 120 shape as the refusal
it is, and that `serving_market` survives into the proxied query string.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    try:
        from main import app
    except Exception:  # pragma: no cover - environment-specific
        pytest.skip("main app import requires a clean repo without duplicate _tmp order_routes.py")
    return TestClient(app)


# The hop, exactly as PIVOTA-Agent src/services/discoveryFeed.js sends it (fetchDiscoveryRecallStep):
# query, serving_market, in_stock_only=false, limit (one page, PRODUCTS_SEARCH_PAGE_SIZE=60), offset 0,
# with the gateway's own credential on all three headers.
HOP_PARAMS: Dict[str, Any] = {
    "query": "beauty skincare serum",
    "serving_market": "SG",
    "in_stock_only": "false",
    "limit": 60,
    "offset": 0,
}
HOP_HEADERS = {"X-Agent-API-Key": "test-api-key", "X-API-Key": "test-api-key", "Authorization": "Bearer test-api-key"}


def _proxy_on(monkeypatch: pytest.MonkeyPatch) -> List[List[Tuple[str, str]]]:
    import routes.agent_sdk_fixed as sdk
    from services import agent_search_gateway_proxy as proxy

    monkeypatch.setenv("AGENT_BEAUTY_SEARCH_VIA_GATEWAY", "on")
    monkeypatch.delenv("AGENT_BEAUTY_SEARCH_VIA_GATEWAY_AGENT_IDS", raising=False)
    forwarded: List[List[Tuple[str, str]]] = []

    async def fake_search(*, base_url: str, query_items: List[Tuple[str, str]], headers: Any, catalog_surface: Any = None):
        forwarded.append(list(query_items))
        return ({"status": "success", "products": [], "total": 0}, "enabled", 200, None)

    monkeypatch.setattr(proxy, "search", fake_search)
    monkeypatch.setattr(sdk, "log_agent_request", AsyncMock(return_value=None), raising=False)
    return forwarded


def test_the_hop_as_sent_is_accepted_under_the_proxy_and_serving_market_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    forwarded = _proxy_on(monkeypatch)
    response = client.get("/agent/v1/products/search", params=HOP_PARAMS, headers=HOP_HEADERS)
    assert response.status_code == 200, response.text
    assert forwarded, "the proxy was not reached"
    sent = dict(forwarded[-1])
    assert sent.get("serving_market") == "SG"
    assert sent.get("query") == "beauty skincare serum"
    assert sent.get("limit") == "60" and sent.get("offset") == "0"


def test_the_first_cuts_shape_limit_120_is_refused_with_gateway_pagination_unsupported(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    forwarded = _proxy_on(monkeypatch)
    response = client.get("/agent/v1/products/search", params={**HOP_PARAMS, "limit": 120}, headers=HOP_HEADERS)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "gateway_pagination_unsupported"
    assert not forwarded, "a refused hop must never reach the gateway"
    # And an offset that is not a page multiple, the same.
    response = client.get("/agent/v1/products/search", params={**HOP_PARAMS, "limit": 60, "offset": 30}, headers=HOP_HEADERS)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "gateway_pagination_unsupported"


def test_with_the_proxy_off_the_hop_reaches_the_local_search_with_serving_market(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_api as agent_api_module

    monkeypatch.setenv("AGENT_BEAUTY_SEARCH_VIA_GATEWAY", "off")

    async def fake_fetch_all(query: Any, values: Any = None) -> List[Dict[str, Any]]:
        return []

    monkeypatch.setattr(agent_api_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api_module, "log_agent_request", AsyncMock(return_value=None))
    seen: List[Dict[str, Any]] = []

    async def fake_loader(**kwargs: Any) -> List[Dict[str, Any]]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(agent_api_module, "_load_external_seed_products_with_cache", fake_loader)
    response = client.get("/agent/v1/products/search", params=HOP_PARAMS, headers=HOP_HEADERS)
    assert response.status_code == 200, response.text
    assert seen and seen[-1]["serving_market"] == "SG" and seen[-1]["market"] is None
