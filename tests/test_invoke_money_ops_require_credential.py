"""POST /agent/shop/v1/invoke: a money operation needs a checkout token or an ACTIVE agent key.

Before this change a credential-less caller was only rate-limited, and every money operation
reached _proxy_agent_api, which sends the caller's X-Checkout-Token when there is one and
otherwise THIS service's own AGENT_API_KEY: an anonymous caller created orders and payments as
Pivota's own agent. Owner decision (2026-11-12): no anonymous money operations.

Pinned here:
  * every money operation, refused (401 CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION) for no
    credential, a malformed, unknown or inactive key: no outbound request, no database call,
    no header value echoed;
  * an active key or a checkout token: the outbound request is byte-for-byte the one the
    pre-change code path sends (the kill switch off is that path);
  * SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL=0|false|off|no restores today's behaviour;
  * non-money operations: the same answers, credential or not, switch on or off, and the key
    lookup never runs;
  * the dispatcher's operations are all classified, and only the money ones can reach an
    order/payment/refund route.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import textwrap
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.agents as agents_db
import routes.agent_auth as agent_auth
import routes.agent_shop_gateway as gw
from db.database import database as shared_database


INVOKE = "/agent/shop/v1/invoke"
ENV = "SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL"
CODE = "CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION"

# Placeholder keys: right shape, fake values.
ACTIVE_KEY = "ak_live_" + "1" * 64
INACTIVE_KEY = "ak_live_" + "2" * 64
STATUS_INACTIVE_KEY = "ak_" + "4" * 64
UNKNOWN_KEY = "ak_live_" + "3" * 64
MALFORMED_KEY = "ak_live_not-a-real-key"
INTERNAL_KEY = "internal-fixture-key"
CHECKOUT_TOKEN = "ctok_fixture_value"
SERVICE_KEY = "service-fixture-key"

AGENTS = {
    ACTIVE_KEY: {"agent_id": "agent_fixture_active", "agent_name": "Fixture Agent", "is_active": True},
    INACTIVE_KEY: {"agent_id": "agent_fixture_inactive", "agent_name": "Fixture Agent", "is_active": False},
    STATUS_INACTIVE_KEY: {
        "agent_id": "agent_fixture_suspended",
        "agent_name": "Fixture Agent",
        "is_active": None,
        "status": "suspended",
    },
}

MONEY_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "create_order": {
        "order": {
            "merchant_id": "merch_fixture",
            "customer_email": "buyer@example.com",
            "currency": "USD",
            "quote_id": "q_fixture",
            "items": [
                {
                    "merchant_id": "merch_fixture",
                    "product_id": "prod_fixture",
                    "product_title": "Fixture Product",
                    "quantity": 1,
                    "unit_price": 10.0,
                    "subtotal": 10.0,
                }
            ],
            "shipping_address": {
                "name": "Fixture Buyer",
                "address_line1": "1 Example Street",
                "city": "Example City",
                "country": "US",
                "postal_code": "00000",
            },
        }
    },
    "create_payment_link": {"order_id": "ORD_fixture", "customer_email": "buyer@example.com"},
    "submit_payment": {
        "payment": {"order_id": "ORD_fixture", "expected_amount": 10.0, "currency": "USD", "payment_method": "card"}
    },
    "record_payment_offer_evidence": {
        "order_id": "ORD_fixture",
        "payment_method_evidence": {"brand": "visa"},
    },
}
MONEY_UPSTREAM_PATHS = {
    "create_order": "/agent/v1/orders/create",
    "create_payment_link": "/agent/v2/payments/checkout-sessions",
    "submit_payment": "/agent/v1/payments",
    "record_payment_offer_evidence": "/agent/v1/orders/payment-offer-evidence",
}

REFUSED = {
    "no_credential": {},
    "empty_api_key": {"X-API-Key": "   "},
    "basic_auth": {"Authorization": "Basic dXNlcjpwYXNz"},
    "malformed_key": {"X-API-Key": MALFORMED_KEY},
    "unknown_key": {"X-API-Key": UNKNOWN_KEY},
    "unknown_bearer": {"Authorization": f"Bearer {UNKNOWN_KEY}"},
    "inactive_key": {"X-API-Key": INACTIVE_KEY},
    "inactive_bearer": {"Authorization": f"Bearer {INACTIVE_KEY}"},
    "status_inactive_key": {"X-API-Key": STATUS_INACTIVE_KEY},
}
ADMITTED = {
    "active_key": {"X-API-Key": ACTIVE_KEY},
    "active_bearer": {"Authorization": f"Bearer {ACTIVE_KEY}"},
    "active_bearer_lowercase_scheme": {"Authorization": f"bearer {ACTIVE_KEY}"},
    "internal_trusted_key": {"X-API-Key": INTERNAL_KEY},
    "checkout_token": {"X-Checkout-Token": CHECKOUT_TOKEN},
    # The token is what the proxy forwards, and the upstream validates it; a bad key beside it
    # does not change that request.
    "checkout_token_with_unknown_key": {"X-Checkout-Token": CHECKOUT_TOKEN, "X-API-Key": UNKNOWN_KEY},
}


class _FakeResponse:
    status_code = 200
    text = '{"ok": true}'

    def json(self) -> Dict[str, Any]:
        return {"ok": True, "upstream": "fixture"}


class _FakeClient:
    def __init__(self, sent: List[Dict[str, Any]]) -> None:
        self._sent = sent

    async def request(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self._sent.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        return _FakeResponse()


@pytest.fixture
def harness(monkeypatch):
    """The real route and the real _proxy_agent_api, with the network and the database spied."""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(gw, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gw, "AGENT_API_KEY", SERVICE_KEY)

    state: Dict[str, Any] = {"sent": [], "client_acquired": 0, "db_calls": [], "lookups": [], "agent_writes": []}

    async def get_client():
        state["client_acquired"] += 1
        return _FakeClient(state["sent"])

    monkeypatch.setattr(gw, "_get_shared_upstream_http_client", get_client)

    async def get_agent_by_key(api_key, metrics_out=None):
        state["lookups"].append(len(api_key))  # never the value
        return AGENTS.get(api_key)

    monkeypatch.setattr(agents_db, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (INTERNAL_KEY,))

    for name in ("execute", "execute_many", "fetch_one", "fetch_all", "fetch_val", "transaction"):
        if hasattr(shared_database, name):
            def _spy(*args, _name=name, **kwargs):
                state["db_calls"].append(_name)
                raise AssertionError(f"database.{_name} called")

            monkeypatch.setattr(shared_database, name, _spy)

    async def _agent_write(*args, **kwargs):
        state["agent_writes"].append(True)
        return (True, 0, 0)

    for name in ("update_agent_stats", "log_agent_usage", "check_rate_limit", "check_daily_quota"):
        monkeypatch.setattr(agents_db, name, _agent_write)
        monkeypatch.setattr(agent_auth, name, _agent_write)

    app = FastAPI()
    app.include_router(gw.router)
    state["client"] = TestClient(app, raise_server_exceptions=False)
    state["app"] = app
    return state


def _post(h, operation: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None):
    return h["client"].post(INVOKE, json={"operation": operation, "payload": payload}, headers=headers or {})


def _assert_refused(h, res, operation: str, headers: Dict[str, str]) -> None:
    assert res.status_code == 401, res.text
    detail = res.json()["detail"]
    assert detail["error"] == CODE
    assert detail["operation"] == operation
    assert res.headers.get("www-authenticate") == "Bearer"
    for value in headers.values():
        if value.strip():
            assert value.strip() not in res.text
            for part in value.split():
                if len(part) > 8:
                    assert part not in res.text
    assert h["client_acquired"] == 0 and h["sent"] == [], "a refused money operation went upstream"
    assert h["db_calls"] == [], "a refused money operation touched the database"
    assert h["agent_writes"] == []


# ── refusals ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_money_operation_without_a_usable_credential_is_refused_before_anything_runs(harness, operation, case):
    headers = REFUSED[case]
    res = _post(harness, operation, MONEY_PAYLOADS[operation], headers)
    _assert_refused(harness, res, operation, headers)


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_the_refusal_comes_before_the_payload_is_read(harness, operation):
    # An empty payload used to fail validation inside the handler; anonymous, it is now refused
    # first, so a caller cannot probe the money handlers without a credential.
    res = _post(harness, operation, {})
    _assert_refused(harness, res, operation, {})


def test_a_malformed_key_is_refused_without_a_lookup(harness):
    _post(harness, "create_order", MONEY_PAYLOADS["create_order"], {"X-API-Key": MALFORMED_KEY})
    assert harness["lookups"] == []


async def _lookup_raises_transient(api_key, metrics_out=None):
    raise agents_db.AgentAuthLookupTransientError("fixture")


async def _lookup_raises_other(api_key, metrics_out=None):
    raise RuntimeError("fixture")


async def _lookup_swallowed_error(api_key, metrics_out=None):
    # What get_agent_by_key does when its query fails for a non-transient reason: None, with
    # auth_source "error".
    if metrics_out is not None:
        metrics_out["auth_source"] = "error"
    return None


@pytest.mark.parametrize(
    "lookup", [_lookup_raises_transient, _lookup_raises_other, _lookup_swallowed_error],
    ids=["transient", "other_exception", "swallowed_db_error"],
)
@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_any_key_lookup_failure_fails_closed_with_503_and_no_upstream(harness, monkeypatch, operation, lookup):
    monkeypatch.setattr(agents_db, "get_agent_by_key", lookup)
    res = _post(harness, operation, MONEY_PAYLOADS[operation], {"X-API-Key": ACTIVE_KEY})
    assert res.status_code == 503, res.text
    assert res.json()["detail"]["error"] == "TEMPORARY_UNAVAILABLE"
    assert ACTIVE_KEY not in res.text
    assert harness["client_acquired"] == 0 and harness["sent"] == []
    assert harness["db_calls"] == [] and harness["agent_writes"] == []


def test_a_lookup_that_found_nothing_is_still_401_not_503(harness, monkeypatch):
    async def not_found(api_key, metrics_out=None):
        if metrics_out is not None:
            metrics_out["auth_source"] = "not_found"
        return None

    monkeypatch.setattr(agents_db, "get_agent_by_key", not_found)
    res = _post(harness, "create_order", MONEY_PAYLOADS["create_order"], {"X-API-Key": UNKNOWN_KEY})
    _assert_refused(harness, res, "create_order", {"X-API-Key": UNKNOWN_KEY})


# A checkout token that is only whitespace is no token: the dispatcher normalises it to None and
# the proxy would send this service's own key. Driven through the raw ASGI interface, because
# TestClient re-encodes a b"\xa0" header as UTF-8 (b"\xc2\xa0"), which is not what a server hands
# the app for that byte on the wire (h11 does not trim it; Starlette decodes it as latin-1).
WHITESPACE_TOKENS = [b"   ", b"\t", b"\xa0", b" \t\xa0 "]


def _raw_asgi_post(app, operation: str, payload: Dict[str, Any], raw_headers: List[tuple]):
    body = json.dumps({"operation": operation, "payload": payload}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": INVOKE,
        "raw_path": INVOKE.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *raw_headers,
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: List[Dict[str, Any]] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), content


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
@pytest.mark.parametrize("token", WHITESPACE_TOKENS, ids=["spaces", "tab", "nbsp", "mixed"])
def test_a_whitespace_only_checkout_token_is_refused(harness, operation, token):
    status, headers, content = _raw_asgi_post(
        harness["app"], operation, MONEY_PAYLOADS[operation], [(b"x-checkout-token", token)]
    )
    assert status == 401, content
    assert json.loads(content)["detail"]["error"] == CODE
    assert headers.get(b"www-authenticate") == b"Bearer"
    assert harness["client_acquired"] == 0 and harness["sent"] == [], "our own key would have gone upstream"
    assert harness["db_calls"] == [] and harness["lookups"] == []


def test_the_raw_asgi_helper_admits_a_real_token(harness):
    """Control for the test above: the same helper with a real token goes upstream with it."""
    status, _, content = _raw_asgi_post(
        harness["app"], "submit_payment", MONEY_PAYLOADS["submit_payment"],
        [(b"x-checkout-token", CHECKOUT_TOKEN.encode())],
    )
    assert status == 200, content
    assert [s["headers"].get("X-Checkout-Token") for s in harness["sent"]] == [CHECKOUT_TOKEN]


def test_the_gate_reads_the_dispatchers_checkout_token_not_the_header():
    """The gate decides on the value the handlers forward. A header the dispatcher normalised
    away must not admit the request, whatever the header holds."""

    class _Req:
        headers = {"x-checkout-token": "ctok_fixture_value"}

    assert asyncio.run(gw._money_operation_refusal_reason(_Req(), checkout_token=None)) == "no_credential"
    assert asyncio.run(gw._money_operation_refusal_reason(_Req(), checkout_token="ctok_fixture_value")) is None


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_the_anonymous_rate_limit_runs_before_the_gate(harness, monkeypatch, operation):
    """Over budget, an anonymous money call is 429 (the limiter), not 401 (the gate)."""
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "1")
    first = _post(harness, operation, MONEY_PAYLOADS[operation])
    assert first.status_code == 401, first.text
    second = _post(harness, operation, MONEY_PAYLOADS[operation])
    assert second.status_code == 429, second.text
    assert second.headers.get("retry-after") == "60"
    assert harness["sent"] == [] and harness["lookups"] == []


# ── admitted callers proceed exactly as today ─────────────────────────────────


def _today(h, monkeypatch, operation: str, headers: Dict[str, str]):
    """The pre-change code path (switch off) for the same request."""
    monkeypatch.setenv(ENV, "0")
    try:
        res = _post(h, operation, MONEY_PAYLOADS[operation], headers)
        sent = list(h["sent"])
    finally:
        monkeypatch.delenv(ENV, raising=False)
        h["sent"].clear()
    return res, sent


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
@pytest.mark.parametrize("case", sorted(ADMITTED))
def test_an_admitted_caller_sends_exactly_the_request_it_sends_today(harness, monkeypatch, operation, case):
    headers = ADMITTED[case]
    before_res, before_sent = _today(harness, monkeypatch, operation, headers)

    res = _post(harness, operation, MONEY_PAYLOADS[operation], headers)

    assert res.status_code == 200, res.text
    assert (res.status_code, res.json()) == (before_res.status_code, before_res.json())
    assert len(harness["sent"]) == 1
    assert harness["sent"] == before_sent
    sent = harness["sent"][0]
    assert sent["url"].endswith(MONEY_UPSTREAM_PATHS[operation])
    # _proxy_agent_api's own choice is unchanged by this gate (out of scope): the token when
    # there is one, otherwise the service key.
    if "X-Checkout-Token" in headers:
        assert sent["headers"].get("X-Checkout-Token") == CHECKOUT_TOKEN
        assert "X-API-Key" not in sent["headers"]
    else:
        assert sent["headers"].get("X-API-Key") == SERVICE_KEY
    assert harness["db_calls"] == [] and harness["agent_writes"] == []


def test_a_checkout_token_needs_no_key_lookup(harness):
    _post(harness, "submit_payment", MONEY_PAYLOADS["submit_payment"], {"X-Checkout-Token": CHECKOUT_TOKEN})
    assert harness["lookups"] == []


# ── kill switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("off", ["0", "false", "off", "no", "FALSE", " Off ", "NO"])
@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_the_kill_switch_off_restores_todays_behaviour(harness, monkeypatch, operation, off):
    monkeypatch.setenv(ENV, off)
    for case in sorted(REFUSED) + sorted(ADMITTED):
        harness["sent"].clear()
        res = _post(harness, operation, MONEY_PAYLOADS[operation], {**REFUSED, **ADMITTED}[case])
        assert res.status_code == 200, (case, res.text)
        assert len(harness["sent"]) == 1, case
    assert harness["lookups"] == [], "the switch off must not even look the key up"


@pytest.mark.parametrize("value", [None, "", "1", "true", "on", "yes", "anything-else"])
def test_the_gate_is_on_unless_explicitly_switched_off(harness, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    res = _post(harness, "create_order", MONEY_PAYLOADS["create_order"])
    _assert_refused(harness, res, "create_order", {})


def test_the_switch_is_read_per_call(harness, monkeypatch):
    monkeypatch.setenv(ENV, "off")
    assert _post(harness, "submit_payment", MONEY_PAYLOADS["submit_payment"]).status_code == 200
    monkeypatch.delenv(ENV)
    harness["sent"].clear()
    res = _post(harness, "submit_payment", MONEY_PAYLOADS["submit_payment"])
    assert res.status_code == 401 and harness["sent"] == []


# ── non-money operations are untouched ────────────────────────────────────────


@pytest.fixture
def non_money(harness, monkeypatch):
    seen: List[Any] = []

    async def fake_find(filters, background_tasks, *, request_market=None):
        seen.append(("find_products", filters.merchant_id, request_market))
        return {"products": [{"product_id": "prod_fixture"}], "total": 1, "page": 1, "page_size": 1}

    async def fake_detail(product, background_tasks, *, request_market=None):
        seen.append(("get_product_detail", product.product_id, request_market))
        return {"product": {"product_id": product.product_id}}

    async def fake_offers(payload, request_metadata, background_tasks, caller_api_key=None, caller_assertion=None):
        seen.append(("offers.resolve", payload.product.sku_id, caller_api_key))
        return {"offers": [], "has_key": bool(caller_api_key)}

    monkeypatch.setattr(gw, "_handle_find_products", fake_find)
    monkeypatch.setattr(gw, "_handle_get_product_detail", fake_detail)
    monkeypatch.setattr(gw, "_handle_offers_resolve", fake_offers)
    harness["seen"] = seen
    return harness


NON_MONEY_REQUESTS = [
    ("find_products", {"search": {"merchant_id": "merch_fixture", "query": "serum"}}),
    ("get_product_detail", {"product": {"merchant_id": "merch_fixture", "product_id": "prod_fixture"}}),
    ("offers.resolve", {"product": {"sku_id": "SKU_FIXTURE"}, "limit": 5}),
    ("offers_resolve", {"product": {"sku_id": "SKU_FIXTURE"}, "limit": 5}),
    ("offersResolve", {"product": {"sku_id": "SKU_FIXTURE"}, "limit": 5}),
    ("definitely_not_an_operation", {}),
    ("", {}),
]
CREDENTIAL_VARIANTS = {"none": {}, **REFUSED, **ADMITTED}


@pytest.mark.parametrize("operation,payload", NON_MONEY_REQUESTS, ids=[op or "empty" for op, _ in NON_MONEY_REQUESTS])
def test_non_money_operations_answer_the_same_with_or_without_credentials(non_money, monkeypatch, operation, payload):
    for case, headers in CREDENTIAL_VARIANTS.items():
        monkeypatch.setenv(ENV, "0")  # the pre-change code path
        non_money["seen"].clear()
        before = _post(non_money, operation, payload, headers)
        before_seen = list(non_money["seen"])

        monkeypatch.delenv(ENV, raising=False)
        non_money["seen"].clear()
        after = _post(non_money, operation, payload, headers)

        assert (after.status_code, after.content) == (before.status_code, before.content), case
        assert non_money["seen"] == before_seen, case
        assert after.status_code != 401, case
    assert non_money["lookups"] == [], "a non-money operation ran the money key lookup"
    assert non_money["sent"] == [] and non_money["db_calls"] == []


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_NON_MONEY_OPERATIONS))
def test_the_gate_never_looks_at_a_non_money_operation(harness, operation):
    class _Req:
        headers: Dict[str, str] = {}

    assert asyncio.run(gw._require_credential_for_money_operation(operation, _Req(), checkout_token=None)) is None
    assert harness["lookups"] == []


def test_operation_names_match_exactly_like_the_dispatcher():
    # The dispatcher compares the stripped name exactly, so "Create_Order" is an unsupported
    # operation (400), not an unguarded money one.
    assert "Create_Order" not in gw.INVOKE_MONEY_OPERATIONS
    assert all(op == op.strip() for op in gw.INVOKE_MONEY_OPERATIONS)


# ── classification guard ──────────────────────────────────────────────────────

_MONEY_ROUTE_RE = re.compile(
    r"/agent/v\d+/(orders|payments|refunds|after-sales|quotes|checkout-intents|checkout)\b"
)


@pytest.mark.parametrize(
    "path",
    [
        "/agent/v1/orders/create",
        "/agent/v2/payments/checkout-sessions",
        "/agent/v2/orders/ORD_1/refunds",
        "/agent/v1/after-sales/cases",
        "/agent/v1/quotes/preview",
        "/agent/v2/quotes/preview",
        "/agent/v1/checkout-intents",
        "/agent/v1/checkout/acp-session",
    ],
)
def test_the_route_scan_recognises_every_order_payment_quote_and_checkout_path(path):
    assert _MONEY_ROUTE_RE.search(path)


@pytest.mark.parametrize("path", ["/agent/v2/products/search", "/agent/v1/merchants", "/agent/shop/v1/invoke"])
def test_the_route_scan_ignores_read_paths(path):
    assert not _MONEY_ROUTE_RE.search(path)
_MONEY_HANDLERS = {
    "_handle_create_order",
    "_handle_create_payment_link",
    "_handle_submit_payment",
    "_handle_record_payment_offer_evidence",
}


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(gw))


def _is_operation_test(node: ast.AST) -> Optional[set]:
    """The operation names an `if operation == "x"` / `if operation in (...)` test names."""
    if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name) or node.left.id != "operation":
        return None
    names: set = set()
    for op, comp in zip(node.ops, node.comparators):
        if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) and isinstance(comp.value, str):
            names.add(comp.value)
        elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
            names.update(e.value for e in comp.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
        else:
            return None
    return names


def _dispatcher_blocks() -> List[tuple]:
    func = next(
        n
        for n in ast.walk(_module_tree())
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "invoke_shop_operation"
    )
    blocks = []
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            names = _is_operation_test(stmt.test)
            if names is not None:
                called = {
                    c.func.id for c in ast.walk(stmt) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                }
                blocks.append((names, called))
    return blocks


def _functions_that_reach_money_routes() -> set:
    """Module functions that call _proxy_agent_api or name an order/payment/refund route in code."""
    found = set()
    for node in _module_tree().body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name == "_proxy_agent_api":
            continue
        doc = ast.get_docstring(node, clean=False)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "_proxy_agent_api":
                found.add(node.name)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value != doc:
                if _MONEY_ROUTE_RE.search(sub.value):
                    found.add(node.name)
    return found


def test_every_dispatcher_operation_is_classified_money_or_non_money():
    dispatched = set().union(*(names for names, _ in _dispatcher_blocks()))
    assert not (gw.INVOKE_MONEY_OPERATIONS & gw.INVOKE_NON_MONEY_OPERATIONS)
    unclassified = dispatched - gw.INVOKE_MONEY_OPERATIONS - gw.INVOKE_NON_MONEY_OPERATIONS
    assert not unclassified, (
        f"/invoke dispatches {sorted(unclassified)} but they are in neither INVOKE_MONEY_OPERATIONS nor "
        "INVOKE_NON_MONEY_OPERATIONS: classify them (an operation that creates or changes an order, a "
        "payment or a refund is money)"
    )
    stale = (gw.INVOKE_MONEY_OPERATIONS | gw.INVOKE_NON_MONEY_OPERATIONS) - dispatched
    assert not stale, f"classified but no longer dispatched: {sorted(stale)}"


def test_only_the_known_money_handlers_reach_an_order_payment_or_refund_route():
    reaching = _functions_that_reach_money_routes()
    assert reaching == _MONEY_HANDLERS, (
        "a function in routes/agent_shop_gateway.py now calls _proxy_agent_api or names an "
        f"order/payment/refund route: {sorted(reaching - _MONEY_HANDLERS)}. If /invoke dispatches to it, "
        "its operation must be in INVOKE_MONEY_OPERATIONS; then add it here."
    )


def test_a_money_handler_is_only_dispatched_from_a_money_operation():
    reaching = _functions_that_reach_money_routes()
    for names, called in _dispatcher_blocks():
        if called & reaching:
            assert names <= gw.INVOKE_MONEY_OPERATIONS, (
                f"/invoke operation(s) {sorted(names)} reach {sorted(called & reaching)} but are not "
                "classified as money operations"
            )
    money_blocks = [called for names, called in _dispatcher_blocks() if names & gw.INVOKE_MONEY_OPERATIONS]
    assert money_blocks and all(called & reaching for called in money_blocks), (
        "a classified money operation no longer reaches a money handler; re-check the list"
    )
    # ...and nothing else in the module calls a money handler (a second door to the same proxy).
    tree = _module_tree()
    callers = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in reaching:
                    callers.add(node.name)
    assert callers == {"invoke_shop_operation"}, sorted(callers)


def test_the_gate_runs_before_the_first_operation_branch():
    """Source-order guard: the credential check is awaited before any `if operation ...` branch."""
    src = textwrap.dedent(inspect.getsource(gw.invoke_shop_operation))
    gate_at = src.index(
        "await _require_credential_for_money_operation(operation, http_request, checkout_token=checkout_token)"
    )
    # ...and after the dispatcher has normalised the token it is given.
    assert src.index("checkout_token = (http_request.headers.get(\"x-checkout-token\")") < gate_at
    first_branch = min(m.start() for m in re.finditer(r"\n    if operation (==|in) ", src))
    assert gate_at < first_branch
