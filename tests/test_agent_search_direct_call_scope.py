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
AGENT_SEARCH_DIRECT_CALL_CROSS_MERCHANT (off | shadow | on, default off = today's scope), with the
/products/resolve fallback's seed read. The payment context the SDK search and resolve never read is behind
its sibling AGENT_SEARCH_DIRECT_CALL_PAYMENT_CONTEXT (off = today's decisions, with no parameter text).
Off, every answer is base's, byte for byte, except that a parameter object's text is never rendered.
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
PAYMENT_FLAG = "AGENT_SEARCH_DIRECT_CALL_PAYMENT_CONTEXT"
SAMPLE = "AGENT_SEARCH_DIRECT_CALL_SHADOW_SAMPLE_RATE"


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
    monkeypatch.setenv(SAMPLE, "1")
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
    monkeypatch.setenv(SAMPLE, "1")
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
# the default until the products_cache arm holds the buyer's currency too. DE is unpriced: refused by name
# (market_not_priced, #2848) before any read, so it is served nothing either way.
_CELLS = {
    # serving_market: (served with the flag off == today on main, served with the flag on)
    "US": (["seed_usd"], ["pc_sgd", "pc_usd", "seed_usd"]),
    "SG": (["seed_sgd"], ["pc_sgd", "pc_usd", "seed_sgd"]),
    "GB": (["seed_gbp"], ["pc_sgd", "pc_usd", "seed_gbp"]),
    "DE": ([], []),
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


def test_the_resolve_search_fallback_passes_values_and_keeps_todays_answer_with_the_flags_off(monkeypatch, client) -> None:
    import routes.agent_api as agent_api

    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(PAYMENT_FLAG, raising=False)
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
        assert kwargs["external_seed_only"] is True
        assert kwargs["payment_context_unread"] is True
        assert kwargs["market"] is None and kwargs["serving_market"] is None


def _seed_sql_recorder(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """The REAL seed loader, over a database that records every external_product_seeds statement."""
    import routes.agent_api as agent_api

    seed_sql: List[Any] = []

    async def fake_fetch_all(query: Any, values: Any = None) -> List[Dict[str, Any]]:
        if "external_product_seeds" in str(query):
            seed_sql.append(dict(values or {}))
        return []

    async def fake_fetch_one(query: Any, values: Any = None) -> None:
        if "external_product_seeds" in str(query):
            seed_sql.append(dict(values or {}))
        return None

    # services.external_seed_search.fetch_external_seed_rows is handed this same database object.
    monkeypatch.setattr(agent_api.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_api.database, "fetch_one", fake_fetch_one, raising=False)
    monkeypatch.setattr(agent_api, "log_agent_request", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_api, "hydrate_quality_and_enrichment", AsyncMock(return_value=None))
    return seed_sql


# Main (#2848) passes the resolve fallback market=None, so its seed read binds the deployment partition and, by
# the seed read's own rule, that partition's currency. This merge keeps that, whatever the flags.
@pytest.mark.parametrize("flag", [None, "off", "shadow", "on"])
def test_the_resolve_fallback_reads_seeds_held_to_the_partition_currency(monkeypatch, client, flag) -> None:
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    seed_sql = _seed_sql_recorder(monkeypatch)
    response = client.get("/agent/v1/products/resolve", params={"query": "sunscreen", "limit": 10}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert seed_sql, "the seed read runs"
    assert {v.get("serving_currency") for v in seed_sql if "serving_currency" in v} == {"USD"}, "never every currency"
    body = response.json()
    for candidate in body.get("candidates") or []:
        assert not ({"price", "currency", "url", "link"} & set(candidate)), "identity only: no price, currency or link"


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


@pytest.mark.parametrize("rate", [None, "0", "", "x", "-1", "nan"])
def test_shadow_unsampled_adds_no_query(monkeypatch, client, rate) -> None:
    import routes.agent_api as agent_api

    monkeypatch.setenv(FLAG, "shadow")
    if rate is None:
        monkeypatch.delenv(SAMPLE, raising=False)
    else:
        monkeypatch.setenv(SAMPLE, rate)
    _install(monkeypatch)
    shadow = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_api, "_log_direct_call_scope_shadow", shadow)
    _served(_sdk_search(client))
    assert shadow.await_count == 0, "default 0: shadow costs no query"


# --- payment-offer evidence on the SDK search ------------------------------------------------------------
# Three offers per card: none required, a card requirement, and one configured for SG. Before, the SDK search
# handed the search Query objects for the payment context: the card offer was rejected (and the object's
# text echoed as `actual`). Off keeps that decision, with `actual` None; on reads no payment context, as the
# HTTP detail route does.

_PARAM_TEXT = ("annotation=", "required=False", "json_schema_extra", "Query(", "<fastapi", "PydanticUndefined", "FieldInfo")


def _install_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.payment_offer_evidence_service as offers

    async def fake_rows(merchant_id: str, *, targets: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for t in targets:
            base = {"status": "active", "source_product_id": t.product_id, "source_variant_id": t.variant_id,
                    "label": "Pay benefit", "benefit_kind": "amount_off", "benefit_value": "1.10", "benefit_currency": "USD"}
            rows += [
                {**base, "incentive_id": f"io_none_{t.product_id}"},
                {**base, "incentive_id": f"io_card_{t.product_id}", "payment_method_type": "card"},
                {**base, "incentive_id": f"io_sg_{t.product_id}", "market": "SG", "benefit_currency": "SGD"},
            ]
        return rows

    monkeypatch.setattr(offers, "_fetch_candidate_rows", fake_rows)
    monkeypatch.setenv("PAYMENT_OFFERS_DISPLAY_ENABLED", "1")


def _card(response: Any, pid: str) -> Dict[str, Any]:
    assert response.status_code == 200, response.text
    return next(p for p in response.json()["products"] if p.get("product_id") == pid)


@pytest.mark.parametrize("flag", [None, "off", "bogus"])
def test_payment_evidence_off_keeps_todays_decisions_with_actual_none(monkeypatch, client, flag) -> None:
    if flag is None:
        monkeypatch.delenv(PAYMENT_FLAG, raising=False)
    else:
        monkeypatch.setenv(PAYMENT_FLAG, flag)
    _install(monkeypatch)
    _install_offers(monkeypatch)
    evidence = _card(_sdk_search(client, search_all_merchants="true"), "pc_usd")["payment_offer_evidence"]
    assert sorted(o["payment_offer_id"] for o in evidence["offers"]) == ["io_none_pc_usd", "io_sg_pc_usd"]
    assert evidence["decisions"] == [{
        "type": "payment_offer_rejected", "payment_offer_id": "io_card_pc_usd", "reason": "payment_method_type_mismatch",
        "required": "card", "actual": None,
    }]


def test_payment_evidence_on_reads_no_payment_context(monkeypatch, client) -> None:
    monkeypatch.setenv(PAYMENT_FLAG, "on")
    _install(monkeypatch)
    _install_offers(monkeypatch)
    evidence = _card(_sdk_search(client, search_all_merchants="true"), "pc_usd")["payment_offer_evidence"]
    offers = {o["payment_offer_id"]: o for o in evidence["offers"]}
    assert sorted(offers) == ["io_card_pc_usd", "io_none_pc_usd", "io_sg_pc_usd"]
    assert offers["io_card_pc_usd"]["eligibility"]["status"] == "potential"
    assert offers["io_card_pc_usd"]["estimated_total_after_payment_offer"] == "18.90"
    assert evidence["decisions"] == []


@pytest.mark.parametrize("payment_flag", [None, "on"])
@pytest.mark.parametrize("scope_flag", [None, "on"])
@pytest.mark.parametrize("door", ["sdk", "sdk_all", "beauty", "resolve"])
def test_no_response_carries_a_fastapi_parameter_objects_text(monkeypatch, client, door, scope_flag, payment_flag) -> None:
    # Unflagged: whatever the flags, no field of these answers is the text of a Query / Param object.
    for name, value in ((FLAG, scope_flag), (PAYMENT_FLAG, payment_flag)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    _install(monkeypatch)
    _install_offers(monkeypatch)
    if door == "sdk":
        response = _sdk_search(client)
    elif door == "sdk_all":
        response = _sdk_search(client, search_all_merchants="true")
    elif door == "beauty":
        response = _beauty_search(client, search_all_merchants="true")
    else:
        response = client.get("/agent/v1/products/resolve", params={"query": "sunscreen", "limit": 10}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert not [t for t in _PARAM_TEXT if t in response.text], response.text[:400]


def test_an_unread_payment_context_rejects_each_requirement_as_the_old_text_did() -> None:
    import services.payment_offer_evidence_service as offers

    for key in ("psp", "payment_method_type", "card_network", "issuer_name", "wallet_type", "installment_provider"):
        decisions: List[Dict[str, Any]] = []
        row = {"incentive_id": "io_1", key: "x_required"}
        assert offers._payment_context_status(row, offers.UNREAD_PAYMENT_CONTEXT, decisions) is None
        assert decisions == [{"type": "payment_offer_rejected", "payment_offer_id": "io_1", "reason": f"{key}_mismatch",
                              "required": "x_required", "actual": None}]
    decisions = []
    assert offers._payment_context_status({"incentive_id": "io_2"}, offers.UNREAD_PAYMENT_CONTEXT, decisions) == ("potential", [])
    assert decisions == []
    assert offers.payment_context_to_dict(offers.UNREAD_PAYMENT_CONTEXT) == {}
