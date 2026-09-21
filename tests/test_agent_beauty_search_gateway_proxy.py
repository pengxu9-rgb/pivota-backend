"""GET /agent/v1/beauty/products/search served by the gateway (services/agent_search_gateway_proxy.py).

What must hold:
* flag OFF (the default): the endpoint is exactly what it was -- the gateway is never called;
* flag ON: the CALLER's own parameters and credential reach the gateway unchanged (market=SG
  included -- that is Meitu's call), and the answer comes back in THIS endpoint's contract;
* a failed forward returns a visible error without consulting another recall lane;
* a request that already went through the proxy is never forwarded again (no loops).
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest

import routes.agent_api as agent_api
from main import app
from routes.agent_auth import get_agent_context
from services import agent_search_gateway_proxy as proxy

# This endpoint's response contract, from routes/agent_api.py (agent_search_products' return).
BACKEND_TOP_LEVEL = {"status", "products", "pagination", "search_context", "filters_applied", "metadata"}
BACKEND_PAGINATION = {"total_count", "limit", "offset", "page", "total_pages", "has_more"}

MEITU_QUERY = {
    "query": "LIP-PRESSION Metal Serum Gloss", "market": "SG", "allow_external_seed": "true",
    "allow_stale_cache": "false", "in_stock_only": "false", "limit": "20",
}
GATEWAY_BODY = {
    "status": "success", "total": 2, "page": 1, "page_size": 2,
    "products": [
        {"title": "[Devil Wears Prada II x JUNGSAEMMOOL] LIP-PRESSION Metal Serum Gloss", "price": 30, "currency": "SGD"},
        {"title": "LIP-PRESSION Metal Serum Gloss", "price": 28.2, "currency": "SGD", "product_id": "p1", "merchant_id": "m1"},
    ],
    "metadata": {"query_source": "agent_products_beauty_external_seed_mainline"},
}


# --- the switch ---------------------------------------------------------------------------


def test_off_unless_explicitly_on() -> None:
    assert proxy.enabled_for("a1", {}, env={}) == (False, "flag_off")
    for value in ("off", "0", "shadow", ""):
        assert proxy.enabled_for("a1", {}, env={proxy.FLAG: value})[0] is False, value
    for value in ("on", "true", "1", "ON"):
        assert proxy.enabled_for("a1", {}, env={proxy.FLAG: value}) == (True, "enabled"), value


def test_an_allowlist_forwards_only_the_named_callers() -> None:
    env = {proxy.FLAG: "on", proxy.AGENT_IDS_FLAG: "agent_meitu, agent_runner"}
    assert proxy.enabled_for("agent_meitu", {}, env=env) == (True, "enabled")
    assert proxy.enabled_for("agent_runner", {}, env=env) == (True, "enabled")
    assert proxy.enabled_for("agent_other", {}, env=env) == (False, "caller_not_enabled")
    assert proxy.enabled_for(None, {}, env=env) == (False, "caller_not_enabled")


def test_an_already_proxied_request_is_never_forwarded_again() -> None:
    env = {proxy.FLAG: "on"}
    assert proxy.enabled_for("a1", {"x-pivota-search-proxy-hop": "1"}, env=env) == (False, "already_proxied")
    assert proxy.enabled_for("a1", {proxy.HOP_HEADER: "1"}, env=env) == (False, "already_proxied")


# --- what reaches the gateway -------------------------------------------------------------


def test_the_proxy_preserves_filters_and_owns_the_recall_source_contract() -> None:
    items = list(MEITU_QUERY.items()) + [("catalog_surface", "fashion"), ("source", "spoofed"),
                                         ("external_seed_strategy", "legacy")]
    out = proxy.gateway_params(items)
    assert out[:2] == list(MEITU_QUERY.items())[:2]
    assert ("market", "SG") in out, "market is passed through, as Meitu sent it"
    # A caller cannot move beauty search off beauty or exclude an eligible offer source.
    assert [kv for kv in out if kv[0] == "catalog_surface"] == [("catalog_surface", "beauty")]
    assert [kv for kv in out if kv[0] == "allow_external_seed"] == [("allow_external_seed", "true")]
    assert [kv for kv in out if kv[0] == "external_seed_strategy"] == [("external_seed_strategy", "unified_relevance")]
    assert [kv for kv in out if kv[0] == "source"] == [("source", proxy.PROXY_SOURCE)]


def test_general_proxy_does_not_invent_a_beauty_surface() -> None:
    out = proxy.gateway_params([("query", "camera"), ("allow_external_seed", "false")], catalog_surface=None)
    assert not any(key == "catalog_surface" for key, _ in out)
    assert dict(out)["allow_external_seed"] == "true"


def test_the_gateway_authenticates_the_same_caller_never_a_service_credential() -> None:
    out = proxy.gateway_headers({"X-API-Key": "ak_caller", "User-Agent": "meitu", "Cookie": "s=1"})
    assert out["x-agent-api-key"] == "ak_caller" and out["x-api-key"] == "ak_caller"
    assert out[proxy.HOP_HEADER] == "1"
    assert "Cookie" not in out and "cookie" not in out, "only the credential is forwarded"
    assert proxy.gateway_headers({"Authorization": "Bearer t"})["authorization"] == "Bearer t"
    no_key = proxy.gateway_headers({})
    assert "x-agent-api-key" not in no_key and "authorization" not in no_key


# --- what comes back ----------------------------------------------------------------------


def _envelope(body: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    args = dict(limit=20, offset=0, query="q", category=None, min_price=None, max_price=None,
                in_stock_only=False, merchant_id=None, merchant_ids=None)
    args.update(kw)
    return proxy.to_backend_envelope(body, **args)


def test_the_answer_keeps_this_endpoints_contract() -> None:
    env = _envelope(GATEWAY_BODY)
    assert set(env) == BACKEND_TOP_LEVEL
    assert set(env["pagination"]) == BACKEND_PAGINATION
    assert env["products"] == GATEWAY_BODY["products"], "products exactly as the gateway serves them"
    assert env["status"] == "success"
    assert env["filters_applied"]["catalog_surface"] == "beauty"
    md = env["metadata"]
    assert md["served_by"] == "gateway"
    assert md["gateway_query_source"] == "agent_products_beauty_external_seed_mainline"
    assert md["reason_code"] == "ok" and md["source"] == "agent_search_products"


@pytest.mark.parametrize("total,limit,offset,pages,more", [(2, 20, 0, 1, False), (45, 20, 0, 3, True), (45, 20, 40, 3, False)])
def test_pagination_arithmetic(total: int, limit: int, offset: int, pages: int, more: bool) -> None:
    p = _envelope({**GATEWAY_BODY, "total": total}, limit=limit, offset=offset)["pagination"]
    assert (p["total_count"], p["total_pages"], p["has_more"], p["page"]) == (total, pages, more, offset // limit + 1)


def test_a_missing_or_short_total_never_undercounts_the_products_served() -> None:
    assert _envelope({"products": [{}, {}, {}]})["pagination"]["total_count"] == 3
    assert _envelope({"products": [{}, {}, {}], "total": 1})["pagination"]["total_count"] == 3
    assert _envelope({"products": []})["metadata"]["reason_code"] == "no_candidates"


# --- the forward itself -------------------------------------------------------------------


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(proxy, "_get_client", lambda: client)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,reason,status", [
    (lambda r: httpx.Response(500, json={}), "gateway_http_500", 502),
    (lambda r: httpx.Response(429, json={}), "gateway_http_429", 429),
    (lambda r: httpx.Response(401, json={}), "gateway_http_401", 401),
    (lambda r: httpx.Response(200, text="not json"), "gateway_invalid_json", 502),
    (lambda r: httpx.Response(200, json={"no": "products"}), "gateway_unexpected_shape", 502),
    (lambda r: httpx.Response(200, json={"status": "failed", "products": []}), "gateway_failed_response", 502),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "gateway_timeout", 504),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), "gateway_unavailable", 503),
])
async def test_every_failure_is_returned_as_a_reason_never_raised(monkeypatch: pytest.MonkeyPatch, handler, reason, status) -> None:
    _mock_client(monkeypatch, handler)
    assert await proxy.search(base_url="http://gw.test", query_items=[("query", "x")], headers={}) == (None, reason, status)


# --- the endpoint, end to end ------------------------------------------------------------


class _Ctx:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.agent_name = "test"
        self.request = None
        self.allowed_merchants = None

    def can_access_merchant(self, merchant_id: str) -> bool:
        return self.allowed_merchants is None or merchant_id in self.allowed_merchants


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch):
    local_calls: List[Dict[str, Any]] = []

    async def local_search(**kwargs: Any) -> Dict[str, Any]:
        local_calls.append(kwargs)
        return {"status": "success", "products": [], "pagination": {}, "search_context": {},
                "filters_applied": {}, "metadata": {"source": "agent_search_products", "reason_code": "no_candidates"}}

    async def no_log(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent_api, "agent_search_products", local_search)
    monkeypatch.setattr(agent_api, "log_agent_request", no_log)
    monkeypatch.setattr(agent_api._app_settings, "pivota_agent_internal_url", "http://gw.test")
    state = {"agent_id": "agent_meitu", "allowed_merchants": None}
    def context_for_test():
        ctx = _Ctx(state["agent_id"])
        ctx.allowed_merchants = state["allowed_merchants"]
        return ctx
    app.dependency_overrides[get_agent_context] = context_for_test
    yield local_calls, state
    app.dependency_overrides.pop(get_agent_context, None)


async def _get(params: Dict[str, str], headers: Dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as client:
        return await client.get("/agent/v1/beauty/products/search", params=params,
                                headers={"X-API-Key": "ak_caller", **(headers or {})})


async def _get_general(params: Dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as client:
        return await client.get("/agent/v1/products/search", params=params,
                                headers={"X-API-Key": "ak_caller"})


@pytest.mark.asyncio
async def test_general_search_ignores_legacy_false_source_switch(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    import routes.agent_sdk_fixed as sdk

    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    monkeypatch.setenv(proxy.AGENT_IDS_FLAG, "agent_meitu")
    monkeypatch.setattr(sdk, "log_agent_request", AsyncMock(return_value=None))
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))

    resp = await _get_general({
        "query": "JUNG SAEM MOOL", "market": "SG", "search_all_merchants": "true",
        "allow_external_seed": "false", "limit": "20",
    })
    assert resp.status_code == 200
    assert resp.json()["metadata"]["served_by"] == "gateway"
    assert local_calls == []
    assert len(seen) == 1
    sent = dict(seen[0].url.params)
    assert sent["market"] == "SG"
    assert sent["allow_external_seed"] == "true"
    assert sent["external_seed_strategy"] == "unified_relevance"
    assert "catalog_surface" not in sent


@pytest.mark.asyncio
async def test_general_queryless_browse_is_served_by_the_gateway(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    import routes.agent_sdk_fixed as sdk

    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    monkeypatch.setenv(proxy.AGENT_IDS_FLAG, "agent_meitu")
    monkeypatch.setattr(sdk, "log_agent_request", AsyncMock(return_value=None))
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))

    resp = await _get_general({
        "market": "SG", "search_all_merchants": "true",
        "allow_external_seed": "false", "limit": "20",
    })
    assert resp.status_code == 200
    assert resp.json()["metadata"]["served_by"] == "gateway"
    assert local_calls == []
    assert len(seen) == 1
    sent = dict(seen[0].url.params)
    assert "query" not in sent
    assert sent["market"] == "SG"
    assert sent["allow_external_seed"] == "true"
    assert sent["external_seed_strategy"] == "unified_relevance"
    assert "catalog_surface" not in sent


@pytest.mark.asyncio
async def test_general_proxy_preserves_merchant_acl(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    _, state = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    monkeypatch.setenv(proxy.AGENT_IDS_FLAG, "agent_meitu")
    state["allowed_merchants"] = ["merchant_one"]
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    resp = await _get_general({"query": "serum", "search_all_merchants": "true"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "gateway_merchant_scope_unavailable"
    assert seen == []


@pytest.mark.asyncio
async def test_flag_off_the_endpoint_is_exactly_what_it_was(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, _ = endpoint
    monkeypatch.delenv(proxy.FLAG, raising=False)
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    resp = await _get(MEITU_QUERY)
    assert resp.status_code == 200
    assert seen == [], "the gateway is never called with the flag off"
    assert len(local_calls) == 1
    assert "served_by" not in resp.json()["metadata"], "and the answer carries no proxy marker"


@pytest.mark.asyncio
async def test_flag_on_meitus_exact_call_is_served_by_the_gateway(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    resp = await _get(MEITU_QUERY)

    assert resp.status_code == 200
    assert local_calls == [], "the local implementation does not run"
    assert len(seen) == 1
    sent = seen[0]
    assert str(sent.url).startswith("http://gw.test/agent/v1/products/search?")
    sent_params = dict(sent.url.params)
    for key, value in MEITU_QUERY.items():
        assert sent_params[key] == value, key
    assert (sent_params["catalog_surface"], sent_params["source"]) == ("beauty", proxy.PROXY_SOURCE)
    assert sent.headers["x-agent-api-key"] == "ak_caller"
    assert sent.headers[proxy.HOP_HEADER] == "1"

    body = resp.json()
    assert set(body) == BACKEND_TOP_LEVEL
    assert [p["title"] for p in body["products"]] == [p["title"] for p in GATEWAY_BODY["products"]]
    assert body["metadata"]["served_by"] == "gateway"


@pytest.mark.asyncio
async def test_a_caller_outside_the_allowlist_is_served_locally(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, state = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    monkeypatch.setenv(proxy.AGENT_IDS_FLAG, "agent_runner")
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    state["agent_id"] = "agent_meitu"
    await _get(MEITU_QUERY)
    assert seen == [] and len(local_calls) == 1
    state["agent_id"] = "agent_runner"
    resp = await _get(MEITU_QUERY)
    assert len(seen) == 1 and resp.json()["metadata"]["served_by"] == "gateway"


@pytest.mark.asyncio
async def test_a_failed_forward_returns_a_visible_error_without_local_recall(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    _mock_client(monkeypatch, lambda r: httpx.Response(503, json={}))
    resp = await _get(MEITU_QUERY)
    assert resp.status_code == 503
    assert local_calls == []
    assert resp.json()["error"] == {"code": "gateway_search_failed", "reason": "gateway_http_503"}


@pytest.mark.asyncio
async def test_a_valid_empty_gateway_answer_stays_empty_and_does_not_fall_through(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    _mock_client(monkeypatch, lambda r: httpx.Response(200, json={"products": [], "total": 0}))
    resp = await _get(MEITU_QUERY)
    assert resp.status_code == 200 and resp.json()["products"] == []
    assert resp.json()["metadata"]["served_by"] == "gateway"
    assert local_calls == []


@pytest.mark.asyncio
async def test_untrusted_hop_header_cannot_restore_local_recall(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    resp = await _get(MEITU_QUERY, headers={proxy.HOP_HEADER: "1"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "gateway_search_proxy_loop"
    assert seen == [] and local_calls == []


@pytest.mark.asyncio
async def test_restricted_agent_cannot_bypass_merchant_acl(monkeypatch: pytest.MonkeyPatch, endpoint) -> None:
    local_calls, state = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    state["allowed_merchants"] = ["merchant_a"]
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    denied = await _get({**MEITU_QUERY, "merchant_id": "merchant_b"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "merchant_forbidden"
    unscoped = await _get(MEITU_QUERY)
    assert unscoped.status_code == 403
    assert unscoped.json()["error"]["code"] == "gateway_merchant_scope_unavailable"
    assert seen == [] and local_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,limit", [(15, 20), (0, 101)])
async def test_proxy_rejects_pagination_the_gateway_cannot_represent(monkeypatch: pytest.MonkeyPatch, endpoint, offset: int, limit: int) -> None:
    local_calls, _ = endpoint
    monkeypatch.setenv(proxy.FLAG, "on")
    seen = _mock_client(monkeypatch, lambda r: httpx.Response(200, json=GATEWAY_BODY))
    resp = await _get({**MEITU_QUERY, "offset": str(offset), "limit": str(limit)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gateway_pagination_unsupported"
    assert seen == [] and local_calls == []
