"""The search routes that call `routes.agent_api.agent_search_products` as a plain function.

`agent_search_products` is not itself a route: GET /agent/v1/products/search (routes/agent_sdk_fixed.
search_products), GET /agent/v1/beauty/products/search (agent_search_products_beauty) and the
/products/resolve search fallback call it directly. Its parameters used to default to FastAPI
`Query(...)` objects, and a caller that left one out passed the object itself. `external_seed_only`
was left out by all three: `bool(Query(default=False))` is True, so an unscoped search was answered
from external seeds only (scope `external_seed`) and connected merchants' products_cache rows were
never read; an agent with an allowed_merchants list got the same. The payment-context parameters
(left out by the SDK route and resolve) became `str(Query(...))` text in the payment context.

The function now has real Python defaults and every caller passes every argument. Restoring the
cross-merchant scope is a behaviour change after months of external-seed-only serving, and the
products_cache arm of this search applies no test-merchant exclusion, so it is behind
AGENT_SEARCH_DIRECT_CALL_CROSS_MERCHANT (off | shadow | on, default off = today's scope).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

HEADERS = {"X-API-Key": "test-api-key"}
CONNECTED = "m_connected_a"
SEED = "external_seed"
FLAG = "AGENT_SEARCH_DIRECT_CALL_CROSS_MERCHANT"


@pytest.fixture
def client() -> TestClient:
    from main import app

    return TestClient(app)


class _Product:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload

    def model_dump(self) -> Dict[str, Any]:
        return dict(self.payload)


def _connected(pid: str, currency: str) -> _Product:
    return _Product({
        "id": pid, "product_id": pid, "title": f"Face Sunscreen SPF 50 {pid}", "description": "sunscreen",
        "product_type": "sunscreen", "category": "sunscreen", "price": 20, "currency": currency, "in_stock": True,
    })


def _seed(pid: str, currency: str) -> Dict[str, Any]:
    return {
        "id": pid, "product_id": pid, "merchant_id": SEED, "merchant_name": "Seed", "title": f"Sunscreen SPF 30 {pid}",
        "description": "sunscreen", "product_type": "sunscreen", "category": "sunscreen", "price": 10,
        "currency": currency, "in_stock": True, "platform": "external",
    }


def _install(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    import routes.agent_api as agent_api

    seen: Dict[str, List[Any]] = {"merchants": [], "seed_calls": []}

    async def fake_fetch_all(query: Any, values: Any = None) -> List[Dict[str, Any]]:
        q = str(query)
        if "SELECT merchant_id, business_name FROM merchant_onboarding" in q:
            return [{"merchant_id": CONNECTED, "business_name": "Connected A"}]
        if "FROM merchant_onboarding" in q and isinstance(values, dict) and values.get("merchant_ids"):
            # A multi-merchant scope is checked approved + connected in one read: every one named is.
            return [{"merchant_id": m, "business_name": m} for m in values["merchant_ids"]]
        return []

    async def fake_fetch_one(query: Any, values: Any = None) -> None:
        return None

    async def fake_hybrid(*args: Any, **kwargs: Any):
        seen["merchants"].append(kwargs.get("merchant_id"))
        return [_connected("pc_usd", "USD"), _connected("pc_sgd", "SGD")], "cache", None

    async def fake_seed_loader(**kwargs: Any) -> List[Dict[str, Any]]:
        # The seed read's own currency rule, as services.external_seed_search.fetch_external_seed_rows binds it:
        # the serving market's currency, else the partition's, else US; a market with none is served no seed.
        from services.external_seed_search import seed_serving_currency
        from services.region_pricing import normalize_region

        seen["seed_calls"].append(kwargs)
        serving = kwargs.get("serving_market")
        market = kwargs.get("market")
        serving_code = normalize_region(serving) if isinstance(serving, str) else ""
        partition = str(market or "US").strip().upper() if isinstance(market, str) or market is None else str(market).upper()
        currency = seed_serving_currency(serving_code or partition)
        rows = [_seed("seed_usd", "USD"), _seed("seed_sgd", "SGD"), _seed("seed_gbp", "GBP"), _seed("seed_eur", "EUR")]
        return [r for r in rows if currency and r["currency"] == currency]

    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api.database, "fetch_one", fake_fetch_one, raising=False)
    monkeypatch.setattr(agent_api, "get_products_hybrid", fake_hybrid)
    monkeypatch.setattr(agent_api, "verify_merchant_active", AsyncMock(return_value={"business_name": "Connected A"}))
    monkeypatch.setattr(agent_api, "_load_external_seed_products_with_cache", fake_seed_loader)
    monkeypatch.setattr(agent_api, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    return seen


def _sdk_search(client: TestClient, **params: Any) -> Any:
    return client.get("/agent/v1/products/search", params={"query": "sunscreen", "limit": 10, **params}, headers=HEADERS)


def _beauty_search(client: TestClient, **params: Any) -> Any:
    return client.get("/agent/v1/beauty/products/search", params={"query": "sunscreen", "limit": 10, **params}, headers=HEADERS)


def _served(response: Any) -> List[str]:
    assert response.status_code == 200, response.text
    return sorted(p.get("product_id") for p in response.json().get("products") or [])


# --- today's scope, pinned (flag off) --------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, "off", "", "bogus"])
@pytest.mark.parametrize("door", [_sdk_search, _beauty_search])
def test_flag_off_keeps_todays_external_seed_scope(monkeypatch, client, flag, door) -> None:
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    seen = _install(monkeypatch)
    served = _served(door(client))
    assert seen["merchants"] == [], "no connected merchant is read: the external-seed scope of today"
    assert seen["seed_calls"], "the seeds are read"
    assert not any(pid.startswith("pc_") for pid in served)


# --- the restored scope (flag on) --------------------------------------------------------------------------


@pytest.mark.parametrize("door", [_sdk_search, _beauty_search])
def test_flag_on_an_unscoped_search_is_cross_merchant(monkeypatch, client, door) -> None:
    monkeypatch.setenv(FLAG, "on")
    seen = _install(monkeypatch)
    served = _served(door(client))
    assert seen["merchants"] == [CONNECTED], "the connected merchant's products_cache is read"
    assert "pc_usd" in served, "and its row is served"
    assert "seed_usd" in served, "beside the seeds"


class _ScopedContext:
    agent_id = "agent_scoped"
    agent_name = "Scoped Agent"
    session_id = "session_scoped"
    request_id = None
    allowed_merchants = ["m_allowed_a", "m_allowed_b"]

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self.allowed_merchants


@pytest.mark.parametrize("flag, merchants", [("on", ["m_allowed_a", "m_allowed_b"]), ("off", [])])
def test_an_allowed_merchants_agent_gets_its_merchants_with_the_flag_on(monkeypatch, client, flag, merchants) -> None:
    from main import app
    from routes.agent_auth import get_agent_context

    monkeypatch.setenv(FLAG, flag)
    seen = _install(monkeypatch)

    async def scoped() -> _ScopedContext:
        return _ScopedContext()

    app.dependency_overrides[get_agent_context] = scoped
    try:
        _served(_sdk_search(client))
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
    assert sorted(m for m in seen["merchants"] if m) == merchants


def test_shadow_serves_todays_scope_and_logs_counts_only(monkeypatch, client) -> None:
    import routes.agent_api as agent_api

    monkeypatch.setenv(FLAG, "shadow")
    seen = _install(monkeypatch)
    monkeypatch.setattr(agent_api, "get_excluded_merchant_ids", AsyncMock(return_value={CONNECTED}))
    lines: List[str] = []
    real_info = agent_api.logger.info

    def spy_info(msg: Any, *args: Any, **kwargs: Any) -> Any:
        if "agent_search.direct_call_scope_shadow" in str(msg):
            lines.append(str(msg) % args)
        return real_info(msg, *args, **kwargs)

    monkeypatch.setattr(agent_api.logger, "info", spy_info)
    served = _served(_sdk_search(client))
    assert seen["merchants"] == [] and not any(pid.startswith("pc_") for pid in served), "today's scope is served"
    assert len(lines) == 1, lines
    assert json.loads(lines[0].split(" ", 1)[1]) == {
        "event": "agent_search.direct_call_scope_shadow", "caller": "sdk_products_search",
        "would_search_merchants": 1, "would_search_test_merchants": 1,
    }


@pytest.mark.parametrize("params", [{"search_all_merchants": "true"}, {"merchant_id": "m_x"}])
def test_shadow_logs_nothing_for_a_search_whose_scope_does_not_change(monkeypatch, client, params) -> None:
    import routes.agent_api as agent_api

    monkeypatch.setenv(FLAG, "shadow")
    _install(monkeypatch)
    lines: List[str] = []
    monkeypatch.setattr(agent_api.logger, "info", lambda msg, *a, **k: lines.append(str(msg)))
    _sdk_search(client, **params)
    assert not [line for line in lines if "direct_call_scope_shadow" in line]


# --- priced paths: the market and currency cells, before and after ----------------------------------------
# What each door serves for each serving market, off (today, pinned) and on. "pc_*" are a connected
# merchant's rows; "seed_*" external seeds. The currency guard keeps a market's currency on both.

# MEASURED, not wished: on, the connected rows come back in EVERY currency. This door holds a currency only on
# the seed read (fetch_external_seed_rows) and, for other rows, only when a price bound is set
# (_passes_explicit_commerce_filters): a US buyer is served the SGD row, a GB buyer USD and SGD rows. Off stays
# the default until the products_cache arm holds the buyer's currency too. DE is unpriced and, on this door
# (main), ignored: it reads as the partition's US.
_CELLS = {
    # serving_market: (served with the flag off == today on main, served with the flag on)
    "US": (["seed_usd"], ["pc_sgd", "pc_usd", "seed_usd"]),
    "SG": (["seed_sgd"], ["pc_sgd", "pc_usd", "seed_sgd"]),
    "GB": (["seed_gbp"], ["pc_sgd", "pc_usd", "seed_gbp"]),
    "DE": (["seed_usd"], ["pc_sgd", "pc_usd", "seed_usd"]),
    None: (["seed_usd"], ["pc_sgd", "pc_usd", "seed_usd"]),
}


@pytest.mark.parametrize("serving_market", list(_CELLS), ids=[str(k) for k in _CELLS])
@pytest.mark.parametrize("flag", ["off", "on"])
def test_the_sdk_search_market_and_currency_cells(monkeypatch, client, serving_market, flag) -> None:
    monkeypatch.setenv(FLAG, flag)
    _install(monkeypatch)
    params = {} if serving_market is None else {"serving_market": serving_market}
    served = _served(_sdk_search(client, **params))
    assert served == _CELLS[serving_market][0 if flag == "off" else 1]


# --- the other direct callers: every argument is a value, never a Query object ------------------------------


def test_the_resolve_search_fallback_passes_values_and_reads_the_us_seeds(monkeypatch, client) -> None:
    # Before: market / serving_market / psp... were left out, so the seed read bound the TEXT of a Query
    # object as its partition (no seed) and the payment context carried that text.
    import routes.agent_api as agent_api

    monkeypatch.delenv(FLAG, raising=False)
    seen = _install(monkeypatch)
    passed: List[Dict[str, Any]] = []
    real = agent_api.agent_search_products

    async def spy(**kwargs: Any) -> Any:
        passed.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(agent_api, "agent_search_products", spy)
    response = client.get("/agent/v1/products/resolve", params={"query": "sunscreen", "limit": 10}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert passed
    from fastapi import params as fastapi_params

    for kwargs in passed:
        assert not [k for k, v in kwargs.items() if isinstance(v, (fastapi_params.Param, fastapi_params.Depends))]
        assert kwargs["market"] is None and kwargs["psp"] is None and kwargs["external_seed_only"] is True
    assert seen["seed_calls"] and seen["seed_calls"][-1]["market"] is None


@pytest.mark.asyncio
async def test_the_variant_detail_route_passes_no_query_object_to_the_detail_route(monkeypatch) -> None:
    import routes.agent_products as agent_products
    from fastapi import BackgroundTasks
    from fastapi import params as fastapi_params

    passed: List[Dict[str, Any]] = []

    async def fake_detail(**kwargs: Any) -> Dict[str, Any]:
        passed.append(kwargs)
        return {"status": "success"}

    class _Ctx:
        def can_access_merchant(self, merchant_id: str) -> bool:
            return True

    monkeypatch.setattr(agent_products, "get_product_details", fake_detail)
    monkeypatch.setattr(agent_products, "_external_seller_detail_via_gateway", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_products, "get_merchant_onboarding", AsyncMock(return_value={"merchant_id": "m_x"}))
    monkeypatch.setattr(agent_products.merchant_store_service, "get_primary_store", AsyncMock(return_value={"platform": "wix"}))
    await agent_products.get_product_details_by_variant(
        req=None, merchant_id="m_x", variant_id="v_1", background_tasks=BackgroundTasks(), context=_Ctx(),
    )
    assert passed, "the detail route was reached"
    assert not [k for k, v in passed[-1].items() if isinstance(v, (fastapi_params.Param, fastapi_params.Depends))]
    assert passed[-1]["market"] is None and passed[-1]["psp"] is None


def test_a_fastapi_parameter_object_reads_as_the_plain_default() -> None:
    from fastapi import Depends, Query

    import routes.agent_api as agent_api

    assert agent_api._plain_argument(Query(default=False), False) is False
    assert agent_api._plain_argument(Query(default=None), None) is None
    assert agent_api._plain_argument(Depends(lambda: None), "d") == "d"
    for value in (True, False, 0, "", "US", None, ["m"]):
        assert agent_api._plain_argument(value, "default") == value
