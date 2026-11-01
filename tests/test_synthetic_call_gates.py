"""Synthetic calls (our own eval traffic, services/synthetic_call.py) skip every partner-facing write.

The end-to-end cases drive the REAL app (main.app) with the gateway's own signed vector
(tests/fixtures/synthetic_assertion_gateway_vector.json) and the gateway agent's key, so the middleware,
the key binding and the route all run as in production. Only DB reads and the readiness gate are faked.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import types
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from main import app
from services import synthetic_call as sc
from services.outbound_links_service import TOKEN_SYNTHETIC_KEY
from tests.test_offers_resolve import _seed_row_for_exec_spec

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "synthetic_assertion_gateway_vector.json").read_text())
GATEWAY_AGENT_ID = "agent_982b1ea2df866206"  # routes.agent_auth's default voucher: the gateway's agent
PARTNER_AGENT_ID = "agent_partner_example"
CALL = sc.SyntheticCall(caller=VECTOR["caller"], gw_request_id=VECTOR["gw_request_id"])
PROBE_PATH = "/agent/v1/products/search"  # one of the read tools' backend requests


@pytest.fixture(autouse=True)
def _assertion_env(monkeypatch):
    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, VECTOR["secret"])
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV, raising=False)
    # The vector's own clock, so the real verifier runs unpatched otherwise.
    monkeypatch.setattr(sc, "time", types.SimpleNamespace(time=lambda: VECTOR["iat"]))


@pytest.fixture
def key_owner(monkeypatch):
    """Which agent the vector's key belongs to (the real _is_service_caller reads it)."""
    import routes.agent_auth as agent_auth

    from db.database import database

    owner = {"agent_id": GATEWAY_AGENT_ID, "eval_client_is_a_partners": False}

    async def fake_get_agent_by_key(api_key, metrics_out=None):
        if api_key == VECTOR["api_key"]:
            return {"agent_id": owner["agent_id"], "agent_name": "x", "is_active": True}
        return None

    real_fetch_one = database.fetch_one

    async def fake_fetch_one(query, values=None):
        if "agent_oauth_clients" in str(query):  # the partner-client check, and nothing else
            return {"agent_id": PARTNER_AGENT_ID} if owner["eval_client_is_a_partners"] else None
        return await real_fetch_one(query, values)

    monkeypatch.setattr(agent_auth, "get_agent_by_key", fake_get_agent_by_key)
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)
    sc._partner_client_cache.clear()
    yield owner
    sc._partner_client_cache.clear()


@pytest.fixture
def synthetic():
    token = sc.set_synthetic_call(CALL)
    try:
        yield CALL
    finally:
        sc.reset_synthetic_call(token)


def _headers(*, token: Optional[str] = None, api_key: Optional[str] = None) -> Dict[str, str]:
    return {
        "X-API-Key": api_key or VECTOR["api_key"],
        sc.SYNTHETIC_ASSERTION_HEADER: token or VECTOR["token"],
    }


# ---------------------------------------------------------------------------------------------------
# The middleware: who is synthetic


def _probe_app() -> FastAPI:
    from middleware.synthetic_call import SyntheticCallMiddleware

    probe = FastAPI()

    async def _verdict():
        call = sc.current_synthetic_call()
        # A task created inside the request sees the verdict too (the ledger's create_task writers).
        in_task = await asyncio.create_task(asyncio.sleep(0, result=sc.is_synthetic_call()))
        return {"synthetic": call is not None, "rid": call.gw_request_id if call else None, "in_task": in_task}

    @probe.get(PROBE_PATH)
    async def _probe():
        return await _verdict()

    @probe.post("/agent/shop/v1/invoke")
    async def _invoke(request: Request):
        body = await request.json()  # the middleware read it first and must hand it back intact
        return {**(await _verdict()), "operation": body.get("operation")}

    @probe.post("/agent/v2/orders")
    async def _order():
        return await _verdict()

    probe.add_middleware(SyntheticCallMiddleware)
    return probe


def test_the_gateway_vector_on_the_gateway_key_is_synthetic(key_owner):
    res = TestClient(_probe_app()).get(PROBE_PATH, headers=_headers())
    assert res.json() == {"synthetic": True, "rid": VECTOR["gw_request_id"], "in_task": True}


def test_the_bearer_form_of_the_key_binds_too(key_owner):
    headers = {"Authorization": f"Bearer {VECTOR['api_key']}", sc.SYNTHETIC_ASSERTION_HEADER: VECTOR["token"]}
    assert TestClient(_probe_app()).get(PROBE_PATH, headers=headers).json()["synthetic"] is True


def test_a_partner_key_cannot_mark_its_own_traffic_synthetic(key_owner):
    key_owner["agent_id"] = PARTNER_AGENT_ID
    assert TestClient(_probe_app()).get(PROBE_PATH, headers=_headers()).json()["synthetic"] is False


@pytest.mark.parametrize(
    "case",
    ["no_header", "other_key", "no_key", "no_secret", "expired", "tampered"],
)
def test_anything_else_is_an_ordinary_call(monkeypatch, key_owner, case):
    headers = _headers()
    if case == "no_header":
        headers.pop(sc.SYNTHETIC_ASSERTION_HEADER)
    elif case == "other_key":
        headers["X-API-Key"] = "ak_live_" + "1" * 64
    elif case == "no_key":
        headers.pop("X-API-Key")
    elif case == "no_secret":
        monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV)
    elif case == "expired":
        monkeypatch.setattr(sc, "time", types.SimpleNamespace(time=lambda: VECTOR["iat"] + 61))
    elif case == "tampered":
        headers[sc.SYNTHETIC_ASSERTION_HEADER] = VECTOR["token"][:-3] + "AAA"
    assert TestClient(_probe_app()).get(PROBE_PATH, headers=headers).json() == {
        "synthetic": False,
        "rid": None,
        "in_task": False,
    }


@pytest.mark.parametrize(
    "operation, synthetic",
    [("find_products_multi", True), ("offers.resolve", True), ("create_order", False), ("submit_payment", False)],
)
def test_on_the_invoke_route_only_the_read_operations_are_synthetic(key_owner, operation, synthetic):
    res = TestClient(_probe_app()).post(
        "/agent/shop/v1/invoke", json={"operation": operation, "payload": {}}, headers=_headers()
    )
    assert res.json()["synthetic"] is synthetic
    assert res.json()["operation"] == operation, "the route still reads the body"


def test_a_money_path_with_a_valid_assertion_writes_as_a_real_call(key_owner, caplog):
    with caplog.at_level("WARNING", logger="middleware.synthetic_call"):
        assert TestClient(_probe_app()).post("/agent/v2/orders", headers=_headers()).json()["synthetic"] is False
    assert any('"reason": "op_not_allowed"' in r.getMessage() for r in caplog.records)


def test_an_eval_client_registered_to_a_partner_is_never_synthetic(key_owner):
    key_owner["eval_client_is_a_partners"] = True
    assert TestClient(_probe_app()).get(PROBE_PATH, headers=_headers()).json()["synthetic"] is False


def test_accepted_and_rejected_lines_name_the_request_never_the_header(monkeypatch, key_owner, caplog):
    from utils.logger import logger as pivota_logger

    accepted: List[str] = []
    monkeypatch.setattr(pivota_logger, "info", lambda msg, *a, **k: accepted.append(msg % a if a else msg))
    TestClient(_probe_app()).get(PROBE_PATH, headers=_headers())
    [line] = [x for x in accepted if x.startswith("synthetic_assertion_accepted ")]
    fields = json.loads(line.split(" ", 1)[1])
    assert fields == {
        "gw_request_id": VECTOR["gw_request_id"],
        "synthetic_caller": VECTOR["caller"],
        "method": "GET",
        "path": PROBE_PATH,
        "op": None,
    }

    key_owner["agent_id"] = PARTNER_AGENT_ID
    with caplog.at_level("WARNING", logger="middleware.synthetic_call"):
        TestClient(_probe_app()).get(PROBE_PATH, headers=_headers())
    [rejected] = [r.getMessage() for r in caplog.records if "synthetic_assertion_rejected" in r.getMessage()]
    assert json.loads(rejected.split(" ", 1)[1]) == {
        "reason": "not_service_caller",
        "method": "GET",
        "path": PROBE_PATH,
        "op": None,
    }
    assert VECTOR["token"] not in rejected and VECTOR["token"] not in line


def test_the_middleware_wraps_everything_that_writes_after_the_route():
    """add_middleware prepends: an earlier index in user_middleware runs OUTSIDE a later one."""
    names = [m.cls.__name__ for m in app.user_middleware]
    outer = names.index("SyntheticCallMiddleware")
    assert outer < names.index("UsageLoggerMiddleware")
    assert outer < names.index("RateLimitMiddleware")


# ---------------------------------------------------------------------------------------------------
# offers.resolve end to end: no clicks, no preflight row, synthetic /r links, same answer


def _token_payload(link: str) -> Dict[str, Any]:
    token = re.search(r"[?&]token=([^&]+)", link).group(1)
    body = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _resolve(monkeypatch, *, headers: Dict[str, str]):
    import routes.agent_shop_gateway as gateway
    from services import checkout_preflight as cp
    from services.issuing_agent_assertion import IssuingContext

    row = copy.deepcopy(_seed_row_for_exec_spec(evidence=True))
    issued: List[str] = []
    preflighted: List[Dict[str, Any]] = []

    async def fake_fetch_all(query: str, values=None):
        return [row] if "FROM external_product_seeds" in str(query) and "JOIN catalog_offers" not in str(query) else []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    async def fake_resolve_issuing(api_key, assertion=None, *, op=None):
        return IssuingContext(agent_id=None)

    async def fake_issue(clicks):
        issued.extend(c.click_id for c in clicks)
        return len(clicks)

    async def fake_preflight(offer):
        preflighted.append(offer)
        return cp.PreflightVerdict(outcome=cp.OK, reason=cp.R_OK, would_block=False)

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=["brand.com"]))
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request", fake_resolve_issuing)
    monkeypatch.setattr(gateway, "issue_clicks", fake_issue)
    monkeypatch.setattr(cp, "preflight_and_record", fake_preflight)
    payload = {"product": {"product_id": "ext_spec"}, "limit": 10, "market": "US", "tool": "*"}
    res = TestClient(app).post(
        "/agent/shop/v1/invoke",
        json={"operation": "offers.resolve", "payload": payload, "metadata": {"source": "creator-agent-ui"}},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    offers = [o for o in res.json().get("offers") or [] if o.get("purchase_route") == "affiliate_outbound"]
    assert offers, "the fixture serves an outbound offer"
    return offers, issued, preflighted


def _links(offers):
    return [o["affiliate_url"] for o in offers]


def _shape(offer: dict) -> str:
    """The offer with what differs per call anyway (signed token, click id, expiry) blanked."""
    text = json.dumps(offer, sort_keys=True)
    text = re.sub(r"token=[A-Za-z0-9_.%-]+", "token=*", text)
    text = re.sub(r"clk_[0-9a-f]+", "clk_*", text)
    return re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "*", text)


def test_a_synthetic_offers_resolve_issues_no_clicks_and_writes_no_preflight_row(monkeypatch, key_owner):
    real, real_issued, real_preflight = _resolve(monkeypatch, headers={"X-API-Key": VECTOR["api_key"]})
    assert real_issued and real_preflight, "control: the same call made as a real one does both"
    assert all(TOKEN_SYNTHETIC_KEY not in _token_payload(link) for link in _links(real))

    syn, syn_issued, syn_preflight = _resolve(monkeypatch, headers=_headers())
    assert syn_issued == [] and syn_preflight == []
    assert all(_token_payload(link)[TOKEN_SYNTHETIC_KEY] == 1 for link in _links(syn))
    assert [_shape(o) for o in syn] == [_shape(o) for o in real], "the answer is the same"


def test_a_partner_key_with_the_vector_still_issues_its_clicks(monkeypatch, key_owner):
    key_owner["agent_id"] = PARTNER_AGENT_ID
    offers, issued, _ = _resolve(monkeypatch, headers=_headers())
    assert issued
    assert all(TOKEN_SYNTHETIC_KEY not in _token_payload(link) for link in _links(offers))


# ---------------------------------------------------------------------------------------------------
# /r and /impression: a synthetic link writes nothing and builds no cart


@pytest.fixture
def click_writes(monkeypatch):
    import routes.outbound_links as outbound

    calls: List[str] = []

    async def log_click(**kw):
        calls.append("click")

    async def log_event(**kw):
        calls.append("event")

    async def warm(**kw):
        calls.append("warm")
        return None

    monkeypatch.setattr(outbound, "log_outbound_click", log_click)
    monkeypatch.setattr(outbound, "log_outbound_event", log_event)
    monkeypatch.setattr(outbound, "resolve_warm_handoff", warm)
    monkeypatch.setattr(outbound.settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(outbound, "evaluate_warm_eligibility", lambda **kw: (True, "eligible"))
    return calls


def _mint(dest: str = "https://brand.com/products/serum") -> str:
    from services.outbound_links_service import make_redirect_token

    return make_redirect_token({"market": "US", "tool": "*", "dest": dest, "ctx": {"pvt_click_id": "clk_1"}})


def test_only_a_link_minted_in_a_synthetic_call_carries_the_bit():
    from services.outbound_links_service import parse_and_verify_redirect_token, token_is_synthetic

    real = parse_and_verify_redirect_token(_mint())
    token = sc.set_synthetic_call(CALL)
    try:
        syn = parse_and_verify_redirect_token(_mint())
    finally:
        sc.reset_synthetic_call(token)
    assert TOKEN_SYNTHETIC_KEY not in real and not token_is_synthetic(real)
    assert syn[TOKEN_SYNTHETIC_KEY] == 1 and token_is_synthetic(syn)
    assert {k: v for k, v in syn.items() if k != TOKEN_SYNTHETIC_KEY}.keys() == real.keys()
    assert TOKEN_SYNTHETIC_KEY not in syn["ctx"], "ctx is copied into the click rows"


def test_a_followed_synthetic_link_redirects_with_no_writes_and_no_cart(click_writes, synthetic):
    token = _mint()
    sc.reset_synthetic_call(sc.set_synthetic_call(None))  # the follow is its own request
    with sc_unset():
        res = TestClient(app).get("/r", params={"token": token}, follow_redirects=False)
        assert res.status_code == 302 and res.headers["location"] == "https://brand.com/products/serum"
        imp = TestClient(app).post("/api/links/impression", json={"token": token})
        assert imp.status_code == 200 and imp.json() == {"ok": True}
    assert click_writes == []


def test_a_followed_real_link_still_writes(click_writes):
    token = _mint()
    TestClient(app).get("/r", params={"token": token}, follow_redirects=False)
    TestClient(app).post("/api/links/impression", json={"token": token})
    assert "click" in click_writes and "event" in click_writes and "warm" in click_writes


class sc_unset:
    """Runs a block outside any synthetic call, as a buyer's follow-up request would."""

    def __enter__(self):
        self._token = sc.set_synthetic_call(None)

    def __exit__(self, *exc):
        sc.reset_synthetic_call(self._token)


# ---------------------------------------------------------------------------------------------------
# Each writer


class _Recorder:
    def __init__(self):
        self.calls: List[str] = []
        self.is_connected = True

    async def execute(self, *a, **k):
        self.calls.append("execute")

    async def execute_many(self, *a, **k):
        self.calls.append("execute_many")


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_usage_rows_agent_stats_and_product_events_skip_only_for_synthetic(monkeypatch, is_synthetic):
    import db.agent_product_events as ape
    import db.agents as agents

    rec = _Recorder()
    monkeypatch.setattr(agents, "database", rec)
    monkeypatch.setattr(ape, "database", rec)

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            rid = await agents.log_agent_usage("agent_x", "/agent/v1/products/search", "GET", 200, 5)
            await agents.update_agent_stats("agent_x", increment_requests=1)
            await ape.log_product_events([{"event_type": "impression", "agent_id": "agent_x"}])
        finally:
            sc.reset_synthetic_call(token)
        return rid

    rid = asyncio.run(run())
    assert rid.startswith("req_"), "callers still get a request id"
    assert rec.calls == ([] if is_synthetic else ["execute", "execute", "execute_many"])


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_the_decision_ledger_queue_gets_nothing_from_a_synthetic_call(monkeypatch, is_synthetic):
    from services import agent_decision_event_store as store

    queued: List[Any] = []
    monkeypatch.setattr(store, "_ensure_worker", lambda: None)
    monkeypatch.setattr(store, "_QUEUE", types.SimpleNamespace(put_nowait=queued.append))

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            await store.record_decision_event(decision_id="dec_1", surface="search")
        finally:
            sc.reset_synthetic_call(token)

    asyncio.run(run())
    assert (queued == []) is is_synthetic


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_the_usage_middleware_writes_no_row_and_no_failure_webhook(monkeypatch, key_owner, is_synthetic):
    from fastapi.responses import JSONResponse

    import middleware.usage_logger as usage
    from middleware.synthetic_call import SyntheticCallMiddleware

    written: List[str] = []

    async def insert(payload):
        written.append("usage")

    async def webhook(payload):
        written.append("webhook")

    monkeypatch.setattr(usage, "_insert_usage_log", insert)
    monkeypatch.setattr(usage, "_emit_request_failure_event", webhook)
    probe = FastAPI()

    @probe.get(PROBE_PATH)
    async def _fail(request: Request):
        request.state.agent_id = GATEWAY_AGENT_ID  # as get_agent_context sets it
        return JSONResponse({"error": "x"}, status_code=500)

    probe.add_middleware(usage.UsageLoggerMiddleware)
    probe.add_middleware(SyntheticCallMiddleware)
    headers = _headers() if is_synthetic else {"X-API-Key": VECTOR["api_key"]}
    TestClient(probe).get(PROBE_PATH, headers=headers)
    assert written == ([] if is_synthetic else ["usage", "webhook"])


def test_a_synthetic_preflight_answers_but_records_nothing(monkeypatch, synthetic):
    from services import checkout_preflight as cp

    recorded: List[Any] = []
    verdict = cp.PreflightVerdict(outcome=cp.OK, reason=cp.R_OK, would_block=False)

    async def fake_preflight(offer):
        return verdict

    async def fake_record(*a, **k):
        recorded.append(a)

    monkeypatch.setattr(cp, "preflight", fake_preflight)
    monkeypatch.setattr(cp, "record", fake_record)
    assert asyncio.run(cp.preflight_and_record({"offer_id": "o"})) is verdict
    assert recorded == []


def test_a_synthetic_live_verification_never_touches_the_merchant(monkeypatch, synthetic):
    from services import live_offer_verification as lov

    async def miss(key):
        return None

    async def forbidden(*a, **k):
        raise AssertionError("merchant egress from a synthetic call")

    monkeypatch.setattr(lov, "_cache_get", miss)
    monkeypatch.setattr(lov.crawl_politeness, "before_request", forbidden)
    monkeypatch.setattr(lov, "_target", lambda offer: ("https://brand.com/products/serum.js", "1"))
    monkeypatch.setattr(lov, "_seed_of", lambda offer: {}, raising=False)
    verdict = asyncio.run(lov._check_one({"merchant_domain": "brand.com"}))
    assert (verdict.status, verdict.reason) == (lov.UNVERIFIED, lov.NO_CACHED_EVIDENCE)


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_a_synthetic_product_read_takes_the_cache_path(monkeypatch, is_synthetic):
    from services import product_query_service as pqs

    upstream: List[str] = []

    async def config(merchant_id):
        return pqs.RealtimeConfig(True, "https://merchant-api.example.invalid", "k", 60, "shopify")

    class Adapter:
        def __init__(self, *a, **k):
            pass

        async def query_products(self, **k):
            upstream.append("merchant_api")
            return [], None

    async def cache(*a, **k):
        return []

    monkeypatch.setattr(pqs, "get_merchant_realtime_config", config)
    monkeypatch.setattr(pqs, "MerchantAPIAdapter", Adapter)
    monkeypatch.setattr(pqs, "_get_from_cache", cache)

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            return await pqs.get_products_hybrid("merch_x", 5, "agent_x")
        finally:
            sc.reset_synthetic_call(token)

    _, source, _ = asyncio.run(run())
    assert upstream == ([] if is_synthetic else ["merchant_api"])
    assert (source == "cache") is is_synthetic


def test_a_synthetic_search_never_writes_the_shared_caches_or_circuit(monkeypatch, synthetic):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS", 60)
    page_before = dict(gateway._MULTI_SEARCH_PAGE_REQUEST_CACHE)
    upstream_before = dict(gateway._MULTI_SEARCH_UPSTREAM_CACHE)
    circuit_before = gateway._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL
    gateway._multi_page_request_cache_put("syn-key", {"products": []})
    gateway._multi_upstream_cache_put("syn-key", {"products": []}, "result")
    for _ in range(50):
        gateway._multi_upstream_record_outcome(False, timeout=True)
    assert dict(gateway._MULTI_SEARCH_PAGE_REQUEST_CACHE) == page_before
    assert dict(gateway._MULTI_SEARCH_UPSTREAM_CACHE) == upstream_before
    assert gateway._MULTI_SEARCH_UPSTREAM_CIRCUIT_OPEN_UNTIL == circuit_before


def test_a_real_search_still_writes_the_page_cache(monkeypatch):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS", 60)
    gateway._multi_page_request_cache_put("real-key", {"products": []})
    try:
        assert gateway._multi_page_request_cache_get("real-key") == {"products": []}
    finally:
        gateway._MULTI_SEARCH_PAGE_REQUEST_CACHE.pop("real-key", None)


def test_a_synthetic_search_skips_the_unauthenticated_fallback_upstream(monkeypatch, synthetic):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "MULTI_SEARCH_UPSTREAM_FALLBACK_BASE_URL", "https://fallback.example.invalid")
    assert asyncio.run(gateway._invoke_multi_upstream_fallback(None, {}, timeout_seconds=1, hop=0)) is None


def test_the_gateway_proxies_are_never_taken_by_a_synthetic_call(monkeypatch, synthetic):
    from services import agent_product_detail_gateway_proxy as detail
    from services import agent_search_gateway_proxy as search

    assert search.enabled_for("agent_x", {}, env={search.FLAG: "1"}) == (False, "synthetic_call")
    assert detail.enabled_for(detail.OBSERVED_SELLER_PREFIX + "x", {}, env={detail.FLAG: "1"}) == (
        False,
        "synthetic_call",
    )


def test_a_queued_task_runs_under_its_own_requests_verdict():
    """The task queue starts a waiting task from inside whichever task just finished."""
    from services.agent_task_manager import _bind_synthetic_verdict

    async def run():
        seen: Dict[str, Any] = {}

        async def probe(name):
            seen[name] = sc.current_synthetic_call()

        token = sc.set_synthetic_call(CALL)
        try:
            eval_task = _bind_synthetic_verdict(lambda: probe("eval"))
        finally:
            sc.reset_synthetic_call(token)
        real_task = _bind_synthetic_verdict(lambda: probe("real"))

        # Start each from the OTHER request's context.
        token = sc.set_synthetic_call(CALL)
        try:
            await asyncio.create_task(real_task())
        finally:
            sc.reset_synthetic_call(token)
        await asyncio.create_task(eval_task())
        return seen

    assert asyncio.run(run()) == {"eval": CALL, "real": None}


def test_the_assertion_header_is_redacted_from_request_logs():
    from middleware.structured_logging import REDACTED_HEADER_NAMES, redact_headers

    assert sc.SYNTHETIC_ASSERTION_HEADER.lower() in REDACTED_HEADER_NAMES
    out = redact_headers({sc.SYNTHETIC_ASSERTION_HEADER: VECTOR["token"], "X-API-Key": "k", "Accept": "a"})
    assert out == {sc.SYNTHETIC_ASSERTION_HEADER: "[REDACTED]", "X-API-Key": "[REDACTED]", "Accept": "a"}


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_a_synthetic_search_never_leads_the_page_request_join(monkeypatch, key_owner, is_synthetic):
    """A real request joining a synthetic leader would get an answer built with every write skipped."""
    import routes.agent_shop_gateway as gateway

    during: List[bool] = []

    async def fake_multi(payload, metadata, background_tasks):
        during.append(bool(gateway._MULTI_SEARCH_PAGE_REQUEST_INFLIGHT))
        return {"status": "success", "products": [], "total": 0, "metadata": {}}

    monkeypatch.setattr(gateway, "INVOKE_MULTI_BYPASS_QUEUE_SHOPPING", True)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_ENABLED", True)
    monkeypatch.setattr(gateway, "MULTI_SEARCH_PAGE_REQUEST_DEDUP_TTL_SECONDS", 60)
    monkeypatch.setattr(gateway, "_handle_find_products_multi", fake_multi)
    monkeypatch.setattr(gateway, "_MULTI_SEARCH_PAGE_REQUEST_CACHE", {})
    headers = _headers() if is_synthetic else {"X-API-Key": VECTOR["api_key"]}
    res = TestClient(app).post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "find_products_multi",
            "payload": {"search": {"query": "water bottle"}},
            "metadata": {"source": "shopping-agent", "page_request_id": "pr_1"},
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert during == [not is_synthetic], "a synthetic call runs without registering as the in-flight leader"
    assert bool(gateway._MULTI_SEARCH_PAGE_REQUEST_CACHE) is (not is_synthetic)


def test_a_queued_task_keeps_its_verdict_when_another_requests_task_starts_it():
    """max_workers=1: the eval task waits behind a real one, and is started from the real task's context."""
    from services.agent_task_manager import AgentTaskManager

    async def run():
        mgr = AgentTaskManager(max_workers=1, max_queue_size=4, task_timeout_seconds=5, max_queue_wait_seconds=5)
        release = asyncio.Event()
        seen: Dict[str, Any] = {}

        async def blocker():
            await release.wait()
            seen["real"] = sc.current_synthetic_call()

        async def probe():
            seen["eval"] = sc.current_synthetic_call()

        _, real_future = await mgr.enqueue(operation="op", session_id=None, payload_hash="a", coro_factory=blocker)
        token = sc.set_synthetic_call(CALL)
        try:
            _, eval_future = await mgr.enqueue(operation="op", session_id=None, payload_hash="b", coro_factory=probe)
        finally:
            sc.reset_synthetic_call(token)
        release.set()
        await asyncio.wait_for(asyncio.gather(real_future, eval_future), timeout=5)
        return seen

    assert asyncio.run(run()) == {"real": None, "eval": CALL}


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_a_synthetic_product_detail_never_tries_the_admin_fallback(monkeypatch, is_synthetic):
    from fastapi import BackgroundTasks, HTTPException

    import routes.agent_shop_gateway as gateway
    import services.merchant_store_service as stores

    asked: List[str] = []

    async def no_cache(*a, **k):
        return None

    async def hybrid(*a, **k):
        return [], "cache", None

    async def active_stores(merchant_id):
        asked.append(merchant_id)
        return []

    monkeypatch.setattr(gateway, "_load_product_by_id", no_cache)
    monkeypatch.setattr(gateway, "get_products_hybrid", hybrid)
    monkeypatch.setattr(stores, "get_merchant_active_stores", active_stores)

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            await gateway._handle_get_product_detail(
                gateway.ProductRef(merchant_id="merch_x", product_id="12345"), BackgroundTasks()
            )
        finally:
            sc.reset_synthetic_call(token)

    with pytest.raises(HTTPException) as err:
        asyncio.run(run())
    assert err.value.status_code == 404
    assert asked == ([] if is_synthetic else ["merch_x"])


def test_a_synthetic_search_never_fills_the_shared_seed_search_cache(monkeypatch):
    """Its cached products would carry synthetic /r links to the next real buyer, whose click /r drops."""
    import routes.agent_api as agent_api
    from services.outbound_links_service import make_redirect_token, parse_redirect_token_verified, token_is_synthetic

    monkeypatch.setattr(agent_api, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True)
    monkeypatch.setattr(agent_api, "_EXTERNAL_SEED_SEARCH_CACHE", type(agent_api._EXTERNAL_SEED_SEARCH_CACHE)())
    monkeypatch.setattr(agent_api, "_EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT", {})
    loads: List[bool] = []

    async def fake_load(**kw):
        loads.append(sc.is_synthetic_call())
        token = make_redirect_token({"market": "US", "tool": "search", "dest": "https://brand.example/p", "ctx": {}})
        kw["metrics_out"].update({"executed": True, "rows_fetched": 1, "rows_built": 1, "skip_reason": None})
        return [{"product_id": "p1", "_token": token}]

    monkeypatch.setattr(agent_api, "_load_external_seed_products_for_search", fake_load)
    req = Request({"type": "http", "method": "GET", "path": PROBE_PATH, "headers": [], "query_string": b""})
    kwargs = dict(
        query="lip tint", market="US", query_semantic_class=None, limit=10, build_budget_ms=None,
        build_concurrency=None, include_seed_data_text_match=False, normalized_seed_strategy="default",
        normalized_catalog_surface="beauty", page_offset=0,
    )

    async def run():
        token = sc.set_synthetic_call(CALL)
        try:
            await agent_api._load_external_seed_products_with_cache(req=req, **kwargs)
        finally:
            sc.reset_synthetic_call(token)
        assert not agent_api._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT, "no refresh scheduled by a synthetic call"
        real = await agent_api._load_external_seed_products_with_cache(req=req, **kwargs)
        # A synthetic call may still READ what a real one cached.
        token = sc.set_synthetic_call(CALL)
        try:
            synthetic_read = await agent_api._load_external_seed_products_with_cache(req=req, **kwargs)
        finally:
            sc.reset_synthetic_call(token)
        return real, synthetic_read

    real, synthetic_read = asyncio.run(run())
    payload, _ = parse_redirect_token_verified(real[0]["_token"])
    assert not token_is_synthetic(payload), "the real buyer gets a link of its own"
    assert loads == [True, False], "the real call loaded fresh; the synthetic one read the real call's entry"
    assert synthetic_read[0]["_token"] == real[0]["_token"]


# ---------------------------------------------------------------------------------------------------
# Review of #2686: merchant egress on the variant route, the currency read, and the middleware's edges


@pytest.fixture
def shopify_admin(monkeypatch):
    """The variant route's dependencies, faked; every Shopify Admin GET is recorded and answers 404."""
    import routes.agent_products as products

    calls: List[str] = []

    class _Resp:
        status_code = 404

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            calls.append(url)
            return _Resp()

    async def none(*a, **k):
        return None

    async def onboarding(merchant_id):
        return {"merchant_id": merchant_id}

    async def store(merchant_id):
        return {"platform": "shopify", "domain": "brand.example.invalid", "api_key": "k", "store_id": "s1"}

    async def token(**k):
        return "k", None

    monkeypatch.setattr(products, "_external_seller_detail_via_gateway", none)
    monkeypatch.setattr(products, "get_merchant_onboarding", onboarding)
    monkeypatch.setattr(products.merchant_store_service, "get_primary_store", store)
    monkeypatch.setattr(products, "resolve_shopify_admin_access_token", token)
    monkeypatch.setattr(products, "get_redis_client", lambda: None)
    monkeypatch.setattr(products.httpx, "AsyncClient", FakeClient)
    return calls


class _Ctx:
    agent_id = GATEWAY_AGENT_ID
    session_id = None

    def can_access_merchant(self, merchant_id):
        return True


@pytest.mark.parametrize("is_synthetic", [False, True])
@pytest.mark.parametrize("route", ["variant", "product"])
def test_the_variant_and_product_routes_make_no_admin_call_for_a_synthetic_call(shopify_admin, is_synthetic, route):
    from fastapi import BackgroundTasks, HTTPException

    import routes.agent_products as products

    req = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            if route == "variant":
                await products.get_product_details_by_variant(req, "merch_x", "4455", BackgroundTasks(), _Ctx())
            else:
                await products.get_product_details(
                    req=req, merchant_id="merch_x", product_id="4455", background_tasks=BackgroundTasks(), context=_Ctx()
                )
        finally:
            sc.reset_synthetic_call(token)

    with pytest.raises(HTTPException) as err:
        asyncio.run(run())
    assert err.value.status_code == 404
    if is_synthetic:
        assert shopify_admin == []
    else:
        assert shopify_admin and "/admin/api/" in shopify_admin[0]


def test_a_synthetic_call_never_writes_the_product_details_cache(monkeypatch, synthetic):
    import routes.agent_products as products

    monkeypatch.setattr(products, "get_redis_client", lambda: (_ for _ in ()).throw(AssertionError("redis written")))
    assert asyncio.run(products._redis_set_json("k", {"a": 1}, 60)) is None


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_the_shop_currency_read_is_cache_only_for_a_synthetic_call(monkeypatch, is_synthetic):
    import routes.agent_shop_gateway as gateway
    import services.merchant_store_service as stores

    asked: List[str] = []

    async def active_stores(merchant_id):
        asked.append(merchant_id)
        return []

    monkeypatch.setattr(stores, "get_merchant_active_stores", active_stores)
    monkeypatch.setattr(gateway, "_get_cached_merchant_shopify_currency", lambda merchant_id: None)

    async def run():
        token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            return await gateway._resolve_shopify_currency_for_merchant("merch_x")
        finally:
            sc.reset_synthetic_call(token)

    assert asyncio.run(run()) is None
    assert asked == ([] if is_synthetic else ["merch_x"])


def test_with_no_secret_the_header_is_not_even_read(monkeypatch, key_owner, caplog):
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV)

    async def boom(*a, **k):
        raise AssertionError("resolved")

    import middleware.synthetic_call as mw

    monkeypatch.setattr(mw, "resolve_synthetic_call", boom)
    monkeypatch.setattr(mw, "_invoke_operation", boom)
    with caplog.at_level("WARNING", logger="middleware.synthetic_call"):
        res = TestClient(_probe_app()).post(
            "/agent/shop/v1/invoke", json={"operation": "find_products_multi", "payload": {}}, headers=_headers()
        )
    assert res.json()["synthetic"] is False and res.json()["operation"] == "find_products_multi"
    assert not [r for r in caplog.records if "synthetic_assertion" in r.getMessage()]


@pytest.mark.parametrize(
    "path",
    [
        "/agent/v1/products/search/extra",
        "/x/agent/v1/products/search",
        "/agent/v1/products/searchx",
        "/agent/shop/v1/invoke/x",
        "/agent/v1/products/merchants/m/variant/v/x",
    ],
)
def test_the_scope_patterns_are_anchored_at_both_ends(path):
    assert sc.synthetic_scope_allows("GET", path) is False
    assert sc.synthetic_scope_allows("POST", path, "find_products_multi") is False


@pytest.mark.parametrize(
    "raw, expect_synthetic",
    [
        ('{"operation": "create_order", "op": "find_products_multi", "payload": {}}', False),
        ('{"op": "find_products_multi", "operation": "create_order", "payload": {}}', False),
        ('{"operation": "find_products_multi", "operation": "create_order", "payload": {}}', False),
        ('{"operation": "create_order", "operation": "find_products_multi", "payload": {}}', True),
    ],
)
def test_the_middleware_and_the_route_read_the_same_operation(key_owner, raw, expect_synthetic):
    """Whatever the body says, the op that decided synthetic is the op the route runs."""
    res = TestClient(_probe_app()).post(
        "/agent/shop/v1/invoke", content=raw, headers={**_headers(), "Content-Type": "application/json"}
    )
    body = res.json()
    assert body["synthetic"] is expect_synthetic
    assert (body["operation"] == "find_products_multi") is expect_synthetic


def test_a_query_string_cannot_mark_a_real_link_synthetic(click_writes):
    token = _mint()
    TestClient(app).get(f"/r?token={token}&syn=1", follow_redirects=False)
    assert "click" in click_writes


@pytest.mark.parametrize("caller", ["a|b|c", "|client", "issuer|", "no-separator", ""])
def test_a_caller_that_does_not_split_into_issuer_and_client_is_treated_as_a_partner(monkeypatch, caller):
    from db.database import database

    async def registry_says_not_a_partner(query, values=None):
        return None

    monkeypatch.setattr(database, "fetch_one", registry_says_not_a_partner)
    sc._partner_client_cache.clear()
    assert asyncio.run(sc._is_partner_client(caller)) is True
    assert asyncio.run(sc._is_partner_client("https://as.example.invalid|client_1")) is False
    sc._partner_client_cache.clear()


def test_sentry_events_never_carry_the_assertion():
    from config.sentry_config import filter_sensitive_data

    event = {"request": {"headers": {"X-Pivota-Synthetic-Assertion": VECTOR["token"], "X-Api-Key": "k", "Accept": "a"}}}
    out = filter_sensitive_data(event, None)
    assert out["request"]["headers"] == {
        "X-Pivota-Synthetic-Assertion": "[REDACTED]",
        "X-Api-Key": "[REDACTED]",
        "Accept": "a",
    }


# ---------------------------------------------------------------------------------------------------
# Follow-ups to #2686: the token resolver, the group shadow, and the scope patterns' edges


@pytest.mark.parametrize("is_synthetic", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_the_variant_route_never_resolves_a_token_for_a_synthetic_call(monkeypatch, shopify_admin, is_synthetic, cached):
    """The resolver may refresh the token: a client_credentials POST and a merchant_stores UPDATE."""
    from fastapi import BackgroundTasks, HTTPException

    import routes.agent_products as products

    resolved: List[dict] = []
    served: List[str] = []

    async def token(**k):
        resolved.append(k)
        return "k", None

    class Redis:
        async def get(self, key):
            return "pid_1" if cached else None

        async def set(self, *a, **k):
            return True

    async def details(*, req, merchant_id, product_id, background_tasks, context):
        served.append(product_id)
        return {"status": "success", "product": {"id": product_id}}

    monkeypatch.setattr(products, "resolve_shopify_admin_access_token", token)
    monkeypatch.setattr(products, "get_redis_client", lambda: Redis())
    monkeypatch.setattr(products, "get_product_details", details)
    req = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})

    async def run():
        ctx_token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            return await products.get_product_details_by_variant(req, "merch_x", "4455", BackgroundTasks(), _Ctx())
        finally:
            sc.reset_synthetic_call(ctx_token)

    if cached:
        assert asyncio.run(run())["selected_variant_id"] == "4455"
        assert served == ["pid_1"]
    else:
        with pytest.raises(HTTPException) as err:
            asyncio.run(run())
        assert err.value.status_code == 404
    assert len(resolved) == (0 if is_synthetic else 1)
    if is_synthetic or cached:
        assert shopify_admin == []
    else:
        assert shopify_admin and "/admin/api/" in shopify_admin[0]


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_the_token_resolver_never_refreshes_for_a_synthetic_call(monkeypatch, is_synthetic):
    import services.shopify_access_token_service as tokens

    exchanged: List[dict] = []
    written: List[Any] = []

    async def exchange(**k):
        exchanged.append(k)
        return "new_token", 3600, None

    async def execute(query, values=None):
        written.append(values)

    monkeypatch.setattr(tokens, "exchange_shopify_client_credentials_token", exchange)
    monkeypatch.setattr(tokens.database, "execute", execute)
    # Client credentials and no expiry metadata: a real call refreshes once and persists.
    creds = json.dumps({"client_id": "cid", "client_secret": "csecret", "access_token": "old_token"})

    async def run():
        ctx_token = sc.set_synthetic_call(CALL if is_synthetic else None)
        try:
            return await tokens.resolve_shopify_admin_access_token(
                shop_domain="brand.myshopify.com", api_key_raw=creds, store_id="s1"
            )
        finally:
            sc.reset_synthetic_call(ctx_token)

    token, meta = asyncio.run(run())
    if is_synthetic:
        assert (token, exchanged, written) == ("old_token", [], [])
        assert meta["refresh_error"] == "synthetic_call" and meta["refreshed"] is False
    else:
        assert token == "new_token" and len(exchanged) == 1 and len(written) == 1


@pytest.mark.parametrize("is_synthetic", [False, True])
def test_a_synthetic_call_schedules_no_group_shadow(monkeypatch, is_synthetic):
    """The shadow runs in a fresh Context, where the verdict is not visible, so the check is made here."""
    from fastapi import BackgroundTasks

    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "_group_shadow_cache_verdict", lambda keys: "run")
    tasks = BackgroundTasks()
    token = sc.set_synthetic_call(CALL if is_synthetic else None)
    try:
        scheduled = gateway._schedule_group_shadow(
            tasks, product_id="p_1", sku_id=None, market_hint="US", limit=3, aliases=[], content_keys={"ck_1"},
            served=[], internal=[], external=[], considered_seed_ids=set(),
        )
    finally:
        sc.reset_synthetic_call(token)
    assert scheduled is (not is_synthetic)
    assert len(tasks.tasks) == (0 if is_synthetic else 1)


# One request each read pattern exists for, in SYNTHETIC_READ_ROUTES order.
_ROUTE_EXAMPLES = [
    ("POST", "/agent/shop/v1/invoke", "find_products_multi"),
    ("POST", "/agent/v2/products/search", None),
    ("GET", "/agent/v1/products/search", None),
    ("POST", "/agent/internal/products/search", None),
    ("POST", "/agent/shop/v1/attribution/external-seed-links", None),
    ("GET", "/agent/v1/products/merchants/m_1/variant/v_1", None),
    ("GET", "/agent/v1/product-groups/resolve", None),
    ("GET", "/agent/v1/product-groups/resolve-by-product-id", None),
    ("GET", "/agent/v1/product-groups/pg_1", None),
]


def test_every_scope_pattern_has_an_example():
    assert len(_ROUTE_EXAMPLES) == len(sc.SYNTHETIC_READ_ROUTES)
    for (method, pattern), (ex_method, path, _op) in zip(sc.SYNTHETIC_READ_ROUTES, _ROUTE_EXAMPLES):
        assert method == ex_method and pattern.match(path), path


@pytest.mark.parametrize("index", range(len(_ROUTE_EXAMPLES)))
def test_each_scope_pattern_is_anchored_at_both_ends(index):
    method, pattern = sc.SYNTHETIC_READ_ROUTES[index]
    _m, path, op = _ROUTE_EXAMPLES[index]
    assert sc.synthetic_scope_allows(method, path, op) is True
    for bad in (f"/x{path}", f"{path}/extra", f"{path}/", f"x{path}"):
        assert not pattern.match(bad), bad
        assert sc.synthetic_scope_allows(method, bad, op) is False, bad


def test_the_middleware_scopes_on_the_routed_path_not_the_url(key_owner):
    """Under a root_path (a mount or proxy prefix) the URL path carries the prefix; the router matches on
    scope["path"], and so does the scope check."""
    res = TestClient(_probe_app(), root_path="/pfx").get(PROBE_PATH, headers=_headers())
    assert res.json()["synthetic"] is True


@pytest.mark.parametrize("cached", [False, True])
def test_a_real_call_with_no_credentials_is_still_refused_before_anything_is_served(monkeypatch, shopify_admin, cached):
    """The credentials check now runs after the variant cache read; for a real call it must still refuse
    on a hit as on a miss, before get_product_details or any Admin GET."""
    from fastapi import BackgroundTasks, HTTPException

    import routes.agent_products as products

    served: List[str] = []

    async def no_token(**k):
        return None, None

    class Redis:
        async def get(self, key):
            return "pid_1" if cached else None

        async def set(self, *a, **k):
            return True

    async def details(**k):
        served.append(k["product_id"])
        return {"status": "success", "product": {"id": k["product_id"]}}

    monkeypatch.setattr(products, "resolve_shopify_admin_access_token", no_token)
    monkeypatch.setattr(products, "get_redis_client", lambda: Redis())
    monkeypatch.setattr(products, "get_product_details", details)
    req = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})

    with pytest.raises(HTTPException) as err:
        asyncio.run(products.get_product_details_by_variant(req, "merch_x", "4455", BackgroundTasks(), _Ctx()))
    assert err.value.status_code == 400
    assert err.value.headers == {"X-Error-Code": "INVALID_STORE_CONFIG"}
    assert (served, shopify_admin) == ([], [])
