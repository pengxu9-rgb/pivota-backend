"""POST /agent/shop/v1/invoke: a caller that presented its own agent key acts as ITSELF upstream.

The four money operations are proxied to this service's own /agent/v1|v2 routes by
_proxy_agent_api. Without a checkout token it sent THIS service's key, also when the caller had
presented its own: the upstream route then applied this service's agent, so the caller's merchant
allowlist, rate limit, quota, governance status and order ownership were never applied.

SHOP_INVOKE_FORWARD_CALLER_KEY (default OFF) makes the proxy send the key the caller presented.

Two harnesses:
  * `harness`: the real route and the real proxy, the upstream call recorded. Pins which
    credential goes upstream for every caller shape, flag off (the request sent before the flag
    existed) and flag on.
  * `loop`: the upstream call is delivered to the REAL /agent/v1|v2 routes in the same app, with
    the real auth dependency. Pins what the caller is NEWLY REFUSED with the flag on:
      - a merchant outside the caller's allowed_merchants: 403;
      - the caller's own rate limit and daily quota: its own 429;
      - a suspended, blocked or deactivated caller: 403;
      - an order created before the change, owned by this service's agent, used by a caller with
        its own key: the payment link is 403; payment and evidence are still served (those
        upstream routes compare the merchant, not the owner), pinned as they are.

Keyless callers are not touched here: they keep the path they had (the money gate, or this
service's key when that gate is switched off).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import textwrap
from collections import defaultdict
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.agents as agents_db
import routes.agent_api as agent_api
import routes.agent_auth as agent_auth
import routes.agent_payment_sdk as payments
import routes.agent_shop_gateway as gw
import routes.agent_v2 as agent_v2
import services.agent_governance as governance_module
import services.quote_first_enforcement as quote_first_enforcement
from db.database import database as shared_database


INVOKE = "/agent/shop/v1/invoke"
FLAG = "SHOP_INVOKE_FORWARD_CALLER_KEY"
GATE = "SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL"
LOOPBACK_BASE = "http://127.0.0.1:8080"

ON_VALUES = ["1", "true", "on", "yes", "TRUE", " On ", "YES"]
OFF_VALUES = [None, "", "0", "false", "off", "no", "2", "enabled", "anything-else"]

# Placeholder keys: right shape, made-up values.
SERVICE_KEY = "ak_live_" + "a" * 64
CALLER_KEY = "ak_live_" + "1" * 64
OTHER_CALLER_KEY = "ak_live_" + "2" * 64
NEW_SELF_SERVE_KEY = "ak_live_" + "3" * 64
LIMITED_KEY = "ak_live_" + "4" * 64
QUOTA_KEY = "ak_live_" + "5" * 64
SUSPENDED_KEY = "ak_live_" + "6" * 64
BLOCKED_KEY = "ak_live_" + "7" * 64
DEACTIVATED_KEY = "ak_live_" + "8" * 64
UNKNOWN_KEY = "ak_live_" + "9" * 64
MALFORMED_KEY = "ak_live_not-a-real-key"
INTERNAL_KEY = "internal-fixture-key"
CHECKOUT_TOKEN = "ctok_fixture_value"
ALL_KEYS = [
    SERVICE_KEY, CALLER_KEY, OTHER_CALLER_KEY, NEW_SELF_SERVE_KEY, LIMITED_KEY, QUOTA_KEY,
    SUSPENDED_KEY, BLOCKED_KEY, DEACTIVATED_KEY, UNKNOWN_KEY, MALFORMED_KEY, INTERNAL_KEY,
]

MERCHANT = "merch_fixture_allowed"
OTHER_MERCHANT = "merch_fixture_other"

SERVICE_AGENT = "agent_fixture_service"
CALLER_AGENT = "agent_fixture_caller"


def _agent(agent_id: str, **extra: Any) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "agent_name": "Fixture Agent",
        "is_active": True,
        "allowed_merchants": None,
        "rate_limit": 1000,
        "daily_quota": 100000,
        **extra,
    }


AGENTS: Dict[str, Dict[str, Any]] = {
    SERVICE_KEY: _agent(SERVICE_AGENT),
    CALLER_KEY: _agent(CALLER_AGENT, allowed_merchants=[MERCHANT]),
    OTHER_CALLER_KEY: _agent("agent_fixture_other_caller", allowed_merchants=[MERCHANT]),
    NEW_SELF_SERVE_KEY: _agent("agent_fixture_new_self_serve", allowed_merchants=[]),
    LIMITED_KEY: _agent("agent_fixture_limited", allowed_merchants=[MERCHANT], rate_limit=2),
    QUOTA_KEY: _agent("agent_fixture_quota", allowed_merchants=[MERCHANT], daily_quota=2),
    SUSPENDED_KEY: _agent("agent_fixture_suspended", allowed_merchants=[MERCHANT]),
    BLOCKED_KEY: _agent("agent_fixture_blocked", allowed_merchants=[MERCHANT]),
    DEACTIVATED_KEY: _agent("agent_fixture_deactivated", allowed_merchants=[MERCHANT], is_active=False),
}
GOVERNANCE_STATUS = {"agent_fixture_suspended": "suspended", "agent_fixture_blocked": "blocked"}

# Request market -> the currency a request for that market carries. Never a default: every
# payload below names both, and the tests check the upstream body against this table.
MARKET_CURRENCY = {"GB": "GBP", "JP": "JPY", "US": "USD"}
NON_US_MARKETS = ["GB", "JP"]

SERVICE_ORDER = "ORD_fixture_service_owned"
CALLER_ORDER = "ORD_fixture_caller_owned"
OTHER_MERCHANT_ORDER = "ORD_fixture_other_merchant"


def _order(order_id: str, *, agent_id: str, merchant_id: str = MERCHANT, market: str = "GB") -> Dict[str, Any]:
    return {
        "order_id": order_id,
        "agent_id": agent_id,
        "merchant_id": merchant_id,
        "customer_email": "buyer@example.com",
        "currency": MARKET_CURRENCY[market],
        "total": 25.0,
        "total_amount": 25.0,
        "status": "pending",
        "payment_status": "unpaid",
        "fulfillment_status": None,
        "items": [{"product_id": "prod_fixture", "product_title": "Fixture Product", "quantity": 1, "unit_price": 25.0}],
        "shipping_address": {"country": market},
        "metadata": {},
        "created_at": "2026-01-01T00:00:00",
    }


def _orders() -> Dict[str, Dict[str, Any]]:
    return {
        # Created through /invoke before the change: owned by this service's agent.
        SERVICE_ORDER: _order(SERVICE_ORDER, agent_id=SERVICE_AGENT),
        CALLER_ORDER: _order(CALLER_ORDER, agent_id=CALLER_AGENT),
        OTHER_MERCHANT_ORDER: _order(OTHER_MERCHANT_ORDER, agent_id=SERVICE_AGENT, merchant_id=OTHER_MERCHANT, market="JP"),
    }


def _create_order_payload(merchant_id: str = MERCHANT, market: str = "GB") -> Dict[str, Any]:
    return {
        "order": {
            "merchant_id": merchant_id,
            "customer_email": "buyer@example.com",
            "currency": MARKET_CURRENCY[market],
            "items": [
                {
                    "merchant_id": merchant_id,
                    "product_id": "prod_fixture",
                    "product_title": "Fixture Product",
                    "quantity": 1,
                    "unit_price": 25.0,
                    "subtotal": 25.0,
                }
            ],
            "shipping_address": {
                "name": "Fixture Buyer",
                "address_line1": "1 Example Street",
                "city": "Example City",
                "country": market,
                "postal_code": "00000",
            },
        }
    }


def _payload(operation: str, *, order_id: str = SERVICE_ORDER, merchant_id: str = MERCHANT, market: str = "GB") -> Dict[str, Any]:
    if operation == "create_order":
        return _create_order_payload(merchant_id, market)
    if operation == "create_payment_link":
        return {"order_id": order_id, "customer_email": "buyer@example.com"}
    if operation == "submit_payment":
        return {
            "payment": {
                "order_id": order_id,
                "expected_amount": 25.0,
                "currency": MARKET_CURRENCY[market],
                "payment_method": "card",
            }
        }
    if operation == "record_payment_offer_evidence":
        return {"order_id": order_id, "payment_method_evidence": {"brand": "fixturecard"}}
    raise AssertionError(operation)


MONEY_UPSTREAM_PATHS = {
    "create_order": "/agent/v1/orders/create",
    "create_payment_link": "/agent/v2/payments/checkout-sessions",
    "submit_payment": "/agent/v1/payments",
    "record_payment_offer_evidence": "/agent/v1/orders/payment-offer-evidence",
}
MONEY_OPERATIONS = sorted(gw.INVOKE_MONEY_OPERATIONS)


def test_this_file_covers_every_money_operation():
    assert set(MONEY_UPSTREAM_PATHS) == set(gw.INVOKE_MONEY_OPERATIONS)


def _set_flag(monkeypatch, value: Optional[str]) -> None:
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)


# ══ harness 1: which credential goes upstream ═════════════════════════════════


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._body = {"ok": True, "upstream": "fixture"} if body is None else body
        self.text = json.dumps(self._body)

    def json(self) -> Dict[str, Any]:
        return self._body


class _RecordingClient:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    async def request(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self._state["sent"].append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        return self._state["respond"]()


async def _get_agent_by_key(api_key, metrics_out=None):
    return AGENTS.get(api_key)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(GATE, raising=False)
    monkeypatch.setattr(gw, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gw, "AGENT_API_KEY", SERVICE_KEY)
    monkeypatch.setattr(gw, "AGENT_API_BASE", LOOPBACK_BASE)

    state: Dict[str, Any] = {"sent": [], "db_calls": [], "respond": _FakeResponse}

    async def get_client():
        return _RecordingClient(state)

    monkeypatch.setattr(gw, "_get_shared_upstream_http_client", get_client)
    monkeypatch.setattr(agents_db, "get_agent_by_key", _get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent_by_key", _get_agent_by_key)
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (INTERNAL_KEY,))

    for name in ("execute", "execute_many", "fetch_one", "fetch_all", "fetch_val", "transaction"):
        if hasattr(shared_database, name):
            def _spy(*args, _name=name, **kwargs):
                state["db_calls"].append(_name)
                raise AssertionError(f"database.{_name} called")

            monkeypatch.setattr(shared_database, name, _spy)

    app = FastAPI()
    app.include_router(gw.router)
    state["app"] = app
    state["client"] = TestClient(app, raise_server_exceptions=False)
    return state


def _post(h, operation: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None):
    return h["client"].post(INVOKE, json={"operation": operation, "payload": payload}, headers=headers or {})


def _upstream_headers(h) -> Dict[str, str]:
    assert len(h["sent"]) == 1, h["sent"]
    return h["sent"][0]["headers"]


SERVICE_KEY_HEADERS = {"Content-Type": "application/json", "X-API-Key": SERVICE_KEY}
TOKEN_HEADERS = {"Content-Type": "application/json", "X-Checkout-Token": CHECKOUT_TOKEN}

# Caller shapes the money gate admits, and the exact upstream headers before the flag existed.
ADMITTED_BEFORE = {
    "own_key": ({"X-API-Key": CALLER_KEY}, SERVICE_KEY_HEADERS),
    "own_key_bearer": ({"Authorization": f"Bearer {CALLER_KEY}"}, SERVICE_KEY_HEADERS),
    "own_key_bearer_lowercase_scheme": ({"Authorization": f"bearer {CALLER_KEY}"}, SERVICE_KEY_HEADERS),
    "own_key_in_both_headers": (
        {"X-API-Key": CALLER_KEY, "Authorization": f"Bearer {CALLER_KEY}"}, SERVICE_KEY_HEADERS,
    ),
    "internal_trusted_key": ({"X-API-Key": INTERNAL_KEY}, SERVICE_KEY_HEADERS),
    "the_service_key_itself": ({"X-API-Key": SERVICE_KEY}, SERVICE_KEY_HEADERS),
    "checkout_token": ({"X-Checkout-Token": CHECKOUT_TOKEN}, TOKEN_HEADERS),
    "checkout_token_with_own_key": ({"X-Checkout-Token": CHECKOUT_TOKEN, "X-API-Key": CALLER_KEY}, TOKEN_HEADERS),
    "checkout_token_with_unknown_key": ({"X-Checkout-Token": CHECKOUT_TOKEN, "X-API-Key": UNKNOWN_KEY}, TOKEN_HEADERS),
}
# The same shapes with the flag on.
ADMITTED_AFTER = {
    "own_key": {"Content-Type": "application/json", "X-API-Key": CALLER_KEY},
    "own_key_bearer": {"Content-Type": "application/json", "X-API-Key": CALLER_KEY},
    "own_key_bearer_lowercase_scheme": {"Content-Type": "application/json", "X-API-Key": CALLER_KEY},
    "own_key_in_both_headers": {"Content-Type": "application/json", "X-API-Key": CALLER_KEY},
    "internal_trusted_key": {"Content-Type": "application/json", "X-API-Key": INTERNAL_KEY},
    "the_service_key_itself": SERVICE_KEY_HEADERS,
    "checkout_token": TOKEN_HEADERS,
    "checkout_token_with_own_key": TOKEN_HEADERS,
    "checkout_token_with_unknown_key": TOKEN_HEADERS,
}
# The proxy's own answer, flag on, for a presented key without the money gate's key format.
BAD_FORMAT_ANSWER = {"detail": {"detail": "Invalid API Key format"}}

# Callers the money gate refuses; with that gate switched OFF they reach the proxy.
GATE_OFF_ONLY = {
    "no_credential": {},
    "whitespace_api_key": {"X-API-Key": "   "},
    "basic_auth": {"Authorization": "Basic dXNlcjpwYXNz"},
    "malformed_key": {"X-API-Key": MALFORMED_KEY},
    "unknown_key": {"X-API-Key": UNKNOWN_KEY},
    "deactivated_key": {"X-API-Key": DEACTIVATED_KEY},
}


def test_the_admitted_tables_cover_the_same_caller_shapes():
    assert set(ADMITTED_BEFORE) == set(ADMITTED_AFTER)


# ── flag off: the request sent before the flag existed ─────────────────────────


@pytest.mark.parametrize("off", OFF_VALUES, ids=[repr(v) for v in OFF_VALUES])
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_off_sends_the_credential_sent_before_the_flag_existed(harness, monkeypatch, operation, off):
    _set_flag(monkeypatch, off)
    for case, (headers, expected) in sorted(ADMITTED_BEFORE.items()):
        harness["sent"].clear()
        res = _post(harness, operation, _payload(operation), headers)
        assert res.status_code == 200, (case, res.text)
        assert res.json() == {"ok": True, "upstream": "fixture"}, case
        sent = harness["sent"]
        assert len(sent) == 1, case
        assert sent[0]["method"] == "POST" and sent[0]["url"] == LOOPBACK_BASE + MONEY_UPSTREAM_PATHS[operation], case
        assert sent[0]["headers"] == expected, case
    assert harness["db_calls"] == []


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_off_with_the_money_gate_off_lends_the_service_key_to_everyone_as_before(harness, monkeypatch, operation):
    monkeypatch.setenv(GATE, "0")
    for case, headers in sorted({**GATE_OFF_ONLY, **{k: v[0] for k, v in ADMITTED_BEFORE.items()}}.items()):
        harness["sent"].clear()
        res = _post(harness, operation, _payload(operation), headers)
        assert res.status_code == 200, (case, res.text)
        expected = TOKEN_HEADERS if "X-Checkout-Token" in headers else SERVICE_KEY_HEADERS
        assert _upstream_headers(harness) == expected, case


@pytest.mark.parametrize("market", NON_US_MARKETS)
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_the_flag_changes_the_credential_and_nothing_else_in_the_upstream_request(harness, monkeypatch, operation, market):
    """Same body, method and URL, flag off and on; only the X-API-Key value differs. The body's
    currency is the request market's, never a substituted one."""
    payload = _payload(operation, market=market)
    off = _post(harness, operation, payload, {"X-API-Key": CALLER_KEY})
    sent_off = harness["sent"].pop()
    monkeypatch.setenv(FLAG, "1")
    on = _post(harness, operation, payload, {"X-API-Key": CALLER_KEY})
    sent_on = harness["sent"].pop()

    assert (off.status_code, off.json()) == (on.status_code, on.json()) == (200, {"ok": True, "upstream": "fixture"})
    assert (sent_off["method"], sent_off["url"], sent_off["json"]) == (sent_on["method"], sent_on["url"], sent_on["json"])
    assert sent_off["headers"] == SERVICE_KEY_HEADERS
    assert sent_on["headers"] == {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}
    if operation in {"create_order", "submit_payment"}:
        assert sent_on["json"]["currency"] == MARKET_CURRENCY[market] != "USD"
    if operation == "create_order":
        assert sent_on["json"]["shipping_address"]["country"] == market


# ── flag on: the caller's own key goes upstream ────────────────────────────────


@pytest.mark.parametrize("on", ON_VALUES)
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_sends_the_callers_own_key_not_the_service_key(harness, monkeypatch, operation, on):
    monkeypatch.setenv(FLAG, on)
    for case, (headers, _before) in sorted(ADMITTED_BEFORE.items()):
        harness["sent"].clear()
        res = _post(harness, operation, _payload(operation), headers)
        assert res.status_code == 200, (case, res.text)
        sent = _upstream_headers(harness)
        assert sent == ADMITTED_AFTER[case], case
        assert "Authorization" not in sent and "authorization" not in sent, case
        if case != "the_service_key_itself":
            assert SERVICE_KEY not in sent.values(), case
    assert harness["db_calls"] == []


def test_flag_on_forwards_the_key_the_money_gate_judged_when_two_keys_are_sent(harness, monkeypatch):
    """X-API-Key wins over Authorization for the gate (request_api_key) and for the upstream
    route; the proxy forwards that same one, so a second key cannot ride on the first."""
    monkeypatch.setenv(FLAG, "1")
    res = _post(
        harness, "create_order", _payload("create_order"),
        {"X-API-Key": CALLER_KEY, "Authorization": f"Bearer {OTHER_CALLER_KEY}"},
    )
    assert res.status_code == 200, res.text
    assert _upstream_headers(harness) == {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}

    harness["sent"].clear()
    res = _post(
        harness, "create_order", _payload("create_order"),
        {"X-API-Key": UNKNOWN_KEY, "Authorization": f"Bearer {CALLER_KEY}"},
    )
    assert res.status_code == 401 and harness["sent"] == [], "the gate judged X-API-Key; nothing may go upstream"


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_checkout_token_still_wins_over_a_key(harness, monkeypatch, operation):
    monkeypatch.setenv(FLAG, "1")
    res = _post(harness, operation, _payload(operation), {"X-Checkout-Token": CHECKOUT_TOKEN, "X-API-Key": CALLER_KEY})
    assert res.status_code == 200, res.text
    assert _upstream_headers(harness) == TOKEN_HEADERS


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_keyless_caller_is_untouched(harness, monkeypatch, operation):
    """Out of scope here: keyless is refused by the money gate; with that gate switched off it
    still gets the path it had (this service's key)."""
    monkeypatch.setenv(FLAG, "1")
    for case in ("no_credential", "whitespace_api_key", "basic_auth"):
        harness["sent"].clear()
        refused = _post(harness, operation, _payload(operation), GATE_OFF_ONLY[case])
        assert refused.status_code == 401 and harness["sent"] == [], case

        monkeypatch.setenv(GATE, "0")
        served = _post(harness, operation, _payload(operation), GATE_OFF_ONLY[case])
        monkeypatch.delenv(GATE)
        assert served.status_code == 200, (case, served.text)
        assert _upstream_headers(harness) == SERVICE_KEY_HEADERS, case


@pytest.mark.parametrize("case", ["malformed_key", "unknown_key", "deactivated_key"])
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_presented_key_is_never_swapped_for_the_service_key(harness, monkeypatch, operation, case):
    """With the money gate switched off a bad key reaches the proxy. A key with the right format
    goes upstream as it is, for that route to refuse; one without it is refused by the proxy and
    nothing is sent. Either way this service's key is not lent to a caller that named a key."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(GATE, "0")
    headers = GATE_OFF_ONLY[case]
    res = _post(harness, operation, _payload(operation), headers)
    if case == "malformed_key":
        assert res.status_code == 401, res.text
        assert res.json() == BAD_FORMAT_ANSWER
        assert harness["sent"] == []
        return
    assert _upstream_headers(harness) == {"Content-Type": "application/json", "X-API-Key": headers["X-API-Key"]}


def _raw_asgi_post(
    app, operation: str, payload: Dict[str, Any], raw_headers: List[tuple], *, path: str = INVOKE,
    raw_body: Optional[Dict[str, Any]] = None,
):
    body = json.dumps({"operation": operation, "payload": payload} if raw_body is None else raw_body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
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
    return start["status"], content


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_key_that_cannot_be_a_header_is_401_not_a_crash(harness, monkeypatch, operation):
    """A non-ASCII X-API-Key (latin-1 on the wire) reaches the proxy only with the money gate
    off. It cannot be sent upstream; the answer is the upstream route's malformed-key answer, and
    this service's key is not used instead."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(GATE, "0")
    status, content = _raw_asgi_post(harness["app"], operation, _payload(operation), [(b"x-api-key", b"ak_live_\xe9\xe9")])
    assert status == 401, content
    assert json.loads(content) == {"detail": {"detail": "Invalid API Key format"}}
    assert harness["sent"] == []

    # Flag off: the same request is served with this service's key, as before.
    monkeypatch.delenv(FLAG)
    status, content = _raw_asgi_post(harness["app"], operation, _payload(operation), [(b"x-api-key", b"ak_live_\xe9\xe9")])
    assert status == 200, content
    assert _upstream_headers(harness) == SERVICE_KEY_HEADERS


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_needs_no_service_key_for_a_caller_with_its_own(harness, monkeypatch, operation):
    monkeypatch.setattr(gw, "AGENT_API_KEY", None)
    before = _post(harness, operation, _payload(operation), {"X-API-Key": CALLER_KEY})
    assert before.status_code == 500 and harness["sent"] == [], "flag off: the service key is still what is sent"

    monkeypatch.setenv(FLAG, "1")
    res = _post(harness, operation, _payload(operation), {"X-API-Key": CALLER_KEY})
    assert res.status_code == 200, res.text
    assert _upstream_headers(harness) == {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}


def test_the_flag_is_read_per_call(harness, monkeypatch):
    seen = []
    for value in ("1", None, "on", "0"):
        _set_flag(monkeypatch, value)
        harness["sent"].clear()
        _post(harness, "submit_payment", _payload("submit_payment"), {"X-API-Key": CALLER_KEY})
        seen.append(_upstream_headers(harness)["X-API-Key"])
    assert seen == [CALLER_KEY, SERVICE_KEY, CALLER_KEY, SERVICE_KEY]


@pytest.mark.parametrize("value", ON_VALUES)
def test_flag_reader_on_values(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert gw._invoke_forwards_caller_key() is True


@pytest.mark.parametrize("value", OFF_VALUES, ids=[repr(v) for v in OFF_VALUES])
def test_flag_reader_is_off_unless_explicitly_switched_on(monkeypatch, value):
    _set_flag(monkeypatch, value)
    assert gw._invoke_forwards_caller_key() is False


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_an_upstream_refusal_of_the_callers_key_is_the_callers_answer(harness, monkeypatch, operation, status):
    monkeypatch.setenv(FLAG, "1")
    harness["respond"] = lambda: _FakeResponse(status, {"detail": "fixture refusal"})
    res = _post(harness, operation, _payload(operation), {"X-API-Key": CALLER_KEY})
    assert res.status_code == status
    assert res.json() == {"detail": {"detail": "fixture refusal"}}
    assert len(harness["sent"]) == 1, "one upstream call: no retry with this service's key"


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag_off", "flag_on"])
def test_no_key_value_reaches_the_response_or_the_log(harness, monkeypatch, caplog, flag):
    _set_flag(monkeypatch, flag)
    caplog.set_level(logging.DEBUG)
    bodies = []
    for operation in MONEY_OPERATIONS:
        for headers in ({"X-API-Key": CALLER_KEY}, {"X-API-Key": UNKNOWN_KEY}, {"Authorization": f"Bearer {CALLER_KEY}"}):
            bodies.append(_post(harness, operation, _payload(operation), headers).text)
    harness["respond"] = lambda: _FakeResponse(403, {"detail": "fixture refusal"})
    bodies.append(_post(harness, "create_order", _payload("create_order"), {"X-API-Key": CALLER_KEY}).text)
    text = "\n".join(bodies) + "\n" + caplog.text
    for key in ALL_KEYS:
        assert key not in text


# ── the helper and its call sites ──────────────────────────────────────────────


def _proxy(**kwargs):
    return asyncio.run(gw._proxy_agent_api("POST", "/agent/v1/payments", {"order_id": SERVICE_ORDER}, **kwargs))


@pytest.mark.parametrize(
    "kwargs,flag,expected",
    [
        ({}, None, SERVICE_KEY_HEADERS),
        ({}, "1", SERVICE_KEY_HEADERS),
        ({"caller_api_key": None}, "1", SERVICE_KEY_HEADERS),
        ({"caller_api_key": ""}, "1", SERVICE_KEY_HEADERS),
        ({"caller_api_key": "   "}, "1", SERVICE_KEY_HEADERS),
        ({"caller_api_key": CALLER_KEY}, None, SERVICE_KEY_HEADERS),
        ({"caller_api_key": CALLER_KEY}, "0", SERVICE_KEY_HEADERS),
        ({"caller_api_key": CALLER_KEY}, "1", {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}),
        ({"caller_api_key": f"  {CALLER_KEY} "}, "1", {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}),
        ({"caller_api_key": CALLER_KEY, "checkout_token": CHECKOUT_TOKEN}, "1", TOKEN_HEADERS),
        ({"caller_api_key": CALLER_KEY, "checkout_token": "  "}, "1", {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}),
        ({"checkout_token": CHECKOUT_TOKEN}, "1", TOKEN_HEADERS),
    ],
)
def test_proxy_credential_precedence(harness, monkeypatch, kwargs, flag, expected):
    _set_flag(monkeypatch, flag)
    assert _proxy(**kwargs) == {"ok": True, "upstream": "fixture"}
    assert _upstream_headers(harness) == expected


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(gw))


def _calls_to(tree: ast.AST, name: str) -> List[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _keyword(call: ast.Call, name: str) -> Optional[str]:
    for kw in call.keywords:
        if kw.arg == name:
            return ast.unparse(kw.value)
    return None


def test_every_proxy_call_passes_the_callers_key_on():
    """A new money handler that forgets the argument would lend this service's key again."""
    calls = _calls_to(_module_tree(), "_proxy_agent_api")
    assert len(calls) >= len(gw.INVOKE_MONEY_OPERATIONS)
    for call in calls:
        assert _keyword(call, "caller_api_key") == "caller_api_key", ast.unparse(call)
        assert _keyword(call, "checkout_token") == "checkout_token", ast.unparse(call)


def test_the_dispatcher_hands_every_money_handler_the_key_the_gate_read():
    tree = _module_tree()
    handlers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name != "_proxy_agent_api" and _calls_to(fn, "_proxy_agent_api")
    }
    assert len(handlers) == len(gw.INVOKE_MONEY_OPERATIONS), handlers
    dispatcher = next(
        fn for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "invoke_shop_operation"
    )
    for handler in sorted(handlers):
        calls = _calls_to(dispatcher, handler)
        assert calls, handler
        for call in calls:
            assert _keyword(call, "caller_api_key") == "request_api_key(http_request)", ast.unparse(call)
        assert len(_calls_to(tree, handler)) == len(calls), f"{handler} is called outside the dispatcher"


def test_the_flag_is_only_read_by_the_proxy():
    """Non-money operations never reach the proxy, so the flag cannot change them."""
    tree = _module_tree()
    readers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) and _calls_to(fn, "_invoke_forwards_caller_key")
    }
    assert readers == {"_proxy_agent_api"}
    source = textwrap.dedent(inspect.getsource(gw._invoke_forwards_caller_key))
    assert "_FORWARD_CALLER_KEY_ENV" in source and gw._FORWARD_CALLER_KEY_ENV == FLAG


# ══ harness 2: the upstream call lands on the real /agent/v1|v2 routes ════════


class _LoopbackClient:
    """Delivers the proxy's request to the same app, as the loopback HTTP call does in a deployment."""

    def __init__(self, app: FastAPI, state: Dict[str, Any]) -> None:
        self._app = app
        self._state = state

    async def request(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self._state["sent"].append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url=LOOPBACK_BASE) as client:
            return await client.request(method, url, json=json, headers=headers)


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(GATE, raising=False)
    monkeypatch.setattr(gw, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gw, "AGENT_API_KEY", SERVICE_KEY)
    monkeypatch.setattr(gw, "AGENT_API_BASE", LOOPBACK_BASE)
    # This service's key is an ordinary agent key here (no internal trusted key configured).
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", ())

    orders = _orders()
    state: Dict[str, Any] = {
        "sent": [],
        "orders": orders,
        "authenticated": [],     # agent ids the upstream auth dependency admitted, in order
        "rate_used": defaultdict(int),
        "quota_used": defaultdict(int),
        "stats": [],
        "order_updates": [],
        "intents": [],
        "governed": [],
        "db_calls": [],
    }

    # Nothing in these flows may reach the database: every read the routes make is faked below.
    for name in ("execute", "execute_many", "fetch_one", "fetch_all", "fetch_val", "transaction"):
        if hasattr(shared_database, name):
            def _spy(*args, _name=name, **kwargs):
                state["db_calls"].append(_name)
                raise AssertionError(f"database.{_name} called")

            monkeypatch.setattr(shared_database, name, _spy)

    monkeypatch.setattr(agents_db, "get_agent_by_key", _get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent_by_key", _get_agent_by_key)

    async def check_rate_limit(agent_id, rate_limit=None):
        limit = int(rate_limit or 100)
        used = state["rate_used"][agent_id]
        if used >= limit:
            return False, used, limit
        state["rate_used"][agent_id] = used + 1
        return True, used, limit

    async def check_daily_quota(agent_id, daily_quota=None):
        quota = int(daily_quota or 1000)
        used = state["quota_used"][agent_id]
        if used >= quota:
            return False, used, quota
        state["quota_used"][agent_id] = used + 1
        state["authenticated"].append(agent_id)
        return True, used, quota

    async def update_agent_stats(agent_id, **kwargs):
        state["stats"].append(agent_id)

    monkeypatch.setattr(agent_auth, "check_rate_limit", check_rate_limit)
    monkeypatch.setattr(agent_auth, "check_daily_quota", check_daily_quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", update_agent_stats)

    async def get_order(order_id):
        row = orders.get(str(order_id))
        return dict(row) if row else None

    async def update_order(order_id, fields):
        state["order_updates"].append(str(order_id))
        return True

    for module in (agent_api, payments, agent_v2):
        monkeypatch.setattr(module, "get_order", get_order)
    monkeypatch.setattr(agent_api, "update_order", update_order)
    monkeypatch.setattr(agent_api, "emit_payment_offer_analytics_event", lambda **_k: None)
    monkeypatch.setattr("mvp.events.emit_best_effort", lambda **_k: None)

    async def log_agent_request(**_kwargs):
        return None

    monkeypatch.setattr(agent_api, "log_agent_request", log_agent_request)

    # Governance: the real validate_request, its policy read from this table instead of the database.
    governance = governance_module.agent_governance

    async def get_policy(agent_id, *, fail_closed=False):
        state["governed"].append(agent_id)
        return {
            "agent_id": agent_id,
            "max_requests_per_minute": 1000,
            "max_error_rate": 0.1,
            "status": GOVERNANCE_STATUS.get(agent_id, "active"),
        }

    monkeypatch.setattr(governance, "_get_policy", get_policy)
    monkeypatch.setattr(governance, "_metrics_store", defaultdict(governance_module.AgentMetrics))

    # Past the caller checks. create_order: the quote-first rule answers a quote-less order with
    # 400 QUOTE_REQUIRED_BEFORE_PURCHASE (the real refusal, after the merchant check).
    async def require_quote(*, merchant_id):
        return True, {"merchant_id": merchant_id}

    monkeypatch.setattr(quote_first_enforcement, "should_require_quote_for_order_create", require_quote)

    # submit_payment: the first step after the merchant check is the store lookup.
    async def get_primary_store(merchant_id):
        return {"platform": "fixture_platform_without_a_direct_path", "merchant_id": merchant_id}

    monkeypatch.setattr(payments, "get_primary_store", get_primary_store)

    # create_payment_link: the hosted-checkout intent, after ownership, governance and order state.
    async def create_checkout_intent_route(*, req, context, agent_user):
        state["intents"].append({"agent_id": context.agent_id, "currencies": [item.currency for item in req.items]})
        return {
            "checkout_url": "https://checkout.example.test/s/fixture",
            "checkout_token": "ctok_fixture_session",
            "intent_id": "ci_fixture",
            "expires_at": None,
        }

    monkeypatch.setattr(agent_v2, "create_checkout_intent_route", create_checkout_intent_route)

    app = FastAPI()
    app.include_router(gw.router)
    app.include_router(agent_api.router)
    app.include_router(payments.router)
    app.include_router(agent_v2.router)

    async def get_client():
        return _LoopbackClient(app, state)

    monkeypatch.setattr(gw, "_get_shared_upstream_http_client", get_client)
    state["client"] = TestClient(app, raise_server_exceptions=False)
    yield state
    assert state["db_calls"] == [], "a faked flow reached the database"


def _invoke(loop, operation: str, key: str, **payload_kwargs):
    return loop["client"].post(
        INVOKE,
        json={"operation": operation, "payload": _payload(operation, **payload_kwargs)},
        headers={"X-API-Key": key},
    )


def _upstream_error(res) -> Any:
    """The upstream route's `detail`, as the proxy relays it: {"detail": {"detail": <upstream detail>}}."""
    return res.json()["detail"]["detail"]


def _assert_past_the_caller_checks(res, operation: str) -> None:
    """The upstream handler ran its body: the caller's identity, scope and ownership were accepted."""
    if operation == "create_order":
        assert res.status_code == 400, res.text
        assert _upstream_error(res)["error"] == "QUOTE_REQUIRED_BEFORE_PURCHASE"
    elif operation == "create_payment_link":
        assert res.status_code == 200, res.text
        assert res.json()["checkout_session"]["hosted_url"] == "https://checkout.example.test/s/fixture"
    elif operation == "submit_payment":
        assert res.status_code == 409, res.text
        assert _upstream_error(res)["error"] == "UNSUPPORTED_COMMERCE_PATH"
    elif operation == "record_payment_offer_evidence":
        assert res.status_code == 200, res.text
        assert res.json()["recorded"] is True
    else:
        raise AssertionError(operation)


# ── a caller in good standing acts as itself ──────────────────────────────────


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_off_the_upstream_route_sees_this_services_agent(loop, operation):
    res = _invoke(loop, operation, CALLER_KEY, order_id=SERVICE_ORDER)
    _assert_past_the_caller_checks(res, operation)
    assert loop["authenticated"] == [SERVICE_AGENT]
    assert loop["rate_used"] == {SERVICE_AGENT: 1}
    assert loop["sent"][0]["headers"] == SERVICE_KEY_HEADERS


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_the_upstream_route_sees_the_caller(loop, monkeypatch, operation):
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, CALLER_KEY, order_id=CALLER_ORDER)
    _assert_past_the_caller_checks(res, operation)
    assert loop["authenticated"] == [CALLER_AGENT]
    assert loop["rate_used"] == {CALLER_AGENT: 1} and loop["quota_used"] == {CALLER_AGENT: 1}
    assert loop["stats"] == [CALLER_AGENT], "the request is credited to the caller's agent"
    assert SERVICE_AGENT not in loop["rate_used"]


@pytest.mark.parametrize("market", NON_US_MARKETS)
def test_flag_on_the_payment_link_is_priced_in_the_orders_market_currency(loop, monkeypatch, market):
    """The hosted page is built from the order: its currency is the order market's, for the caller too."""
    loop["orders"][CALLER_ORDER] = _order(CALLER_ORDER, agent_id=CALLER_AGENT, market=market)
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=CALLER_ORDER)
    assert res.status_code == 200, res.text
    assert loop["intents"] == [{"agent_id": CALLER_AGENT, "currencies": [MARKET_CURRENCY[market]]}]
    assert MARKET_CURRENCY[market] != "USD"


@pytest.mark.parametrize("market,sent_market", [("GB", "JP"), ("JP", "GB")])
def test_flag_on_a_payment_in_another_markets_currency_is_refused_for_the_caller_too(loop, monkeypatch, market, sent_market):
    loop["orders"][CALLER_ORDER] = _order(CALLER_ORDER, agent_id=CALLER_AGENT, market=market)
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "submit_payment", CALLER_KEY, order_id=CALLER_ORDER, market=sent_market)
    assert res.status_code == 409, res.text
    detail = _upstream_error(res)
    assert detail["error"] == "QUOTE_MISMATCH"
    assert (detail["order_currency"], detail["expected_currency"]) == (MARKET_CURRENCY[market], MARKET_CURRENCY[sent_market])


# ── NEWLY REFUSED 1: a merchant outside the caller's allowed_merchants ────────

OUT_OF_SCOPE = [
    # (key, operation, merchant, order): the caller may not act for this merchant.
    ("scoped_key", CALLER_KEY, "create_order", OTHER_MERCHANT, None),
    ("scoped_key", CALLER_KEY, "submit_payment", OTHER_MERCHANT, OTHER_MERCHANT_ORDER),
    ("scoped_key", CALLER_KEY, "record_payment_offer_evidence", OTHER_MERCHANT, OTHER_MERCHANT_ORDER),
    # A new self-serve key has an EMPTY list: it may act for no merchant at all.
    ("new_self_serve_key", NEW_SELF_SERVE_KEY, "create_order", MERCHANT, None),
    ("new_self_serve_key", NEW_SELF_SERVE_KEY, "submit_payment", MERCHANT, SERVICE_ORDER),
    ("new_self_serve_key", NEW_SELF_SERVE_KEY, "record_payment_offer_evidence", MERCHANT, SERVICE_ORDER),
]


@pytest.mark.parametrize(
    "key,operation,merchant,order", [case[1:] for case in OUT_OF_SCOPE],
    ids=[f"{case[0]}-{case[2]}" for case in OUT_OF_SCOPE],
)
def test_newly_refused_a_merchant_outside_the_callers_allowed_merchants_is_403(loop, monkeypatch, key, operation, merchant, order):
    market = "JP" if merchant == OTHER_MERCHANT else "GB"
    kwargs = {"merchant_id": merchant, "market": market, **({"order_id": order} if order else {})}

    before = _invoke(loop, operation, key, **kwargs)
    _assert_past_the_caller_checks(before, operation)       # served today, as this service's agent
    assert loop["authenticated"] == [SERVICE_AGENT]

    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, key, **kwargs)
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": {"detail": "Not authorized for this merchant"}}
    assert loop["authenticated"] == [SERVICE_AGENT, AGENTS[key]["agent_id"]]
    assert loop["order_updates"] == ([order] if operation == "record_payment_offer_evidence" else []), "only the flag-off call wrote"


def test_newly_refused_a_payment_link_for_another_merchants_order_is_403(loop, monkeypatch):
    """The payment-link route checks the owner; the scoped caller does not own this order either."""
    before = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=OTHER_MERCHANT_ORDER)
    assert before.status_code == 200, before.text
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=OTHER_MERCHANT_ORDER)
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": {"detail": "Not authorized for this order"}}
    assert len(loop["intents"]) == 1, "only the flag-off call opened a hosted page"


# ── NEWLY REFUSED 2: the caller's own rate limit and daily quota ──────────────


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_newly_refused_a_caller_over_its_own_rate_limit_gets_its_own_429(loop, monkeypatch, operation):
    limit = AGENTS[LIMITED_KEY]["rate_limit"]
    limited_agent = AGENTS[LIMITED_KEY]["agent_id"]

    # Today: never limited by its own number; every call counts against this service's agent.
    for _ in range(limit + 2):
        _assert_past_the_caller_checks(_invoke(loop, operation, LIMITED_KEY), operation)
    assert loop["rate_used"] == {SERVICE_AGENT: limit + 2}

    monkeypatch.setenv(FLAG, "1")
    loop["orders"][SERVICE_ORDER]["agent_id"] = limited_agent   # its own order: ownership is not the subject here
    statuses = []
    for _ in range(limit + 2):
        res = _invoke(loop, operation, LIMITED_KEY)
        statuses.append(res.status_code)
    assert statuses[limit:] == [429, 429], statuses
    assert 429 not in statuses[:limit]
    assert res.json() == {"detail": {"detail": f"Rate limit exceeded: {limit}/{limit} requests per minute"}}
    assert loop["rate_used"] == {SERVICE_AGENT: limit + 2, limited_agent: limit}, "counted against the caller, not this service"

    # Another caller is not slowed by that caller's limit.
    loop["orders"][SERVICE_ORDER]["agent_id"] = CALLER_AGENT
    _assert_past_the_caller_checks(_invoke(loop, operation, CALLER_KEY), operation)


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_newly_refused_a_caller_over_its_own_daily_quota_gets_its_own_429(loop, monkeypatch, operation):
    quota = AGENTS[QUOTA_KEY]["daily_quota"]
    quota_agent = AGENTS[QUOTA_KEY]["agent_id"]

    for _ in range(quota + 1):
        _assert_past_the_caller_checks(_invoke(loop, operation, QUOTA_KEY), operation)

    monkeypatch.setenv(FLAG, "1")
    loop["orders"][SERVICE_ORDER]["agent_id"] = quota_agent
    statuses = [_invoke(loop, operation, QUOTA_KEY).status_code for _ in range(quota)]
    assert 429 not in statuses
    res = _invoke(loop, operation, QUOTA_KEY)
    assert res.status_code == 429, res.text
    assert res.json() == {"detail": {"detail": f"Daily quota exceeded: {quota}/{quota} requests"}}
    assert loop["quota_used"][quota_agent] == quota


def test_the_callers_429_arrives_without_the_upstream_rate_limit_headers(loop, monkeypatch):
    """Pinned as it is: the proxy relays status and body, not the upstream X-RateLimit-* headers."""
    monkeypatch.setenv(FLAG, "1")
    loop["rate_used"][AGENTS[LIMITED_KEY]["agent_id"]] = AGENTS[LIMITED_KEY]["rate_limit"]
    res = _invoke(loop, "create_order", LIMITED_KEY)
    assert res.status_code == 429
    assert not [name for name in res.headers if name.lower().startswith(("x-ratelimit", "x-quota", "retry-after"))]


# ── NEWLY REFUSED 3: a suspended, blocked or deactivated caller ───────────────

GOVERNED = [(SUSPENDED_KEY, "Agent temporarily suspended due to policy violations."),
            (BLOCKED_KEY, "Agent access blocked. Contact support for details.")]


@pytest.mark.parametrize("key,message", GOVERNED, ids=["suspended", "blocked"])
@pytest.mark.parametrize("operation", ["create_order", "create_payment_link"])
def test_newly_refused_a_suspended_or_blocked_caller_is_403(loop, monkeypatch, operation, key, message):
    agent_id = AGENTS[key]["agent_id"]
    before = _invoke(loop, operation, key)
    _assert_past_the_caller_checks(before, operation)        # today: governed as this service's agent
    assert loop["governed"] == [SERVICE_AGENT]

    monkeypatch.setenv(FLAG, "1")
    loop["orders"][SERVICE_ORDER]["agent_id"] = agent_id        # its own order: reach the governance check
    res = _invoke(loop, operation, key)
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": {"detail": message}}
    assert loop["governed"] == [SERVICE_AGENT, agent_id]
    assert len(loop["intents"]) == (1 if operation == "create_payment_link" else 0), "only the flag-off call got a page"


@pytest.mark.parametrize("key", [SUSPENDED_KEY, BLOCKED_KEY], ids=["suspended", "blocked"])
@pytest.mark.parametrize("operation", ["submit_payment", "record_payment_offer_evidence"])
def test_a_suspended_or_blocked_caller_is_still_served_where_the_upstream_route_has_no_governance_check(loop, monkeypatch, operation, key):
    """Pinned as it is, NOT a goal: /agent/v1/payments and the evidence route never ask
    governance, so forwarding the caller's key does not stop a suspended caller there."""
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, key)
    _assert_past_the_caller_checks(res, operation)
    assert loop["authenticated"] == [AGENTS[key]["agent_id"]]
    assert loop["governed"] == []


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_a_deactivated_callers_key_is_refused_at_the_door_whatever_the_flag(loop, monkeypatch, operation):
    for flag in (None, "1"):
        _set_flag(monkeypatch, flag)
        res = _invoke(loop, operation, DEACTIVATED_KEY)
        assert res.status_code == 401, res.text
        assert res.json()["detail"]["error"] == "CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION"
    assert loop["sent"] == []


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_newly_refused_a_deactivated_caller_is_403_when_the_money_gate_is_switched_off(loop, monkeypatch, operation):
    monkeypatch.setenv(GATE, "0")
    before = _invoke(loop, operation, DEACTIVATED_KEY)
    _assert_past_the_caller_checks(before, operation)        # the gate's rollback state lends this service's key

    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, DEACTIVATED_KEY)
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": {"detail": "Agent is deactivated"}}
    assert loop["authenticated"] == [SERVICE_AGENT]


@pytest.mark.parametrize("key,detail", [(UNKNOWN_KEY, "Invalid API Key"), (MALFORMED_KEY, "Invalid API Key format")],
                         ids=["unknown", "malformed"])
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_newly_refused_a_bad_key_is_401_when_the_money_gate_is_switched_off(loop, monkeypatch, operation, key, detail):
    monkeypatch.setenv(GATE, "0")
    _assert_past_the_caller_checks(_invoke(loop, operation, key), operation)

    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, key)
    assert res.status_code == 401, res.text
    assert res.json() == {"detail": {"detail": detail}}
    assert loop["authenticated"] == [SERVICE_AGENT]


# ── NEWLY REFUSED 4: an order created before the change ───────────────────────
# Created through /invoke while the flag was off, so it is owned by this service's agent. The
# same caller comes back with its own key after the flag is switched on.


def test_newly_refused_a_payment_link_for_an_order_created_before_the_change_is_403(loop, monkeypatch):
    before = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=SERVICE_ORDER)
    assert before.status_code == 200, before.text
    assert loop["intents"] == [{"agent_id": SERVICE_AGENT, "currencies": ["GBP"]}]

    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=SERVICE_ORDER)
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": {"detail": "Not authorized for this order"}}
    assert len(loop["intents"]) == 1, "no hosted page for an order the caller does not own"


@pytest.mark.parametrize("operation", ["submit_payment", "record_payment_offer_evidence"])
def test_an_order_created_before_the_change_is_still_payable_by_a_caller_in_the_merchants_scope(loop, monkeypatch, operation):
    """Pinned as it is, NOT a goal: these two upstream routes compare the order's MERCHANT with
    the caller's scope and never the order's owner. So the in-flight order keeps working here, and
    so does any other order of a merchant in the caller's scope (another caller's included)."""
    monkeypatch.setenv(FLAG, "1")
    for key in (CALLER_KEY, OTHER_CALLER_KEY):
        res = _invoke(loop, operation, key, order_id=SERVICE_ORDER)
        _assert_past_the_caller_checks(res, operation)
    assert loop["authenticated"] == [CALLER_AGENT, AGENTS[OTHER_CALLER_KEY]["agent_id"]]
    if operation == "record_payment_offer_evidence":
        assert loop["order_updates"] == [SERVICE_ORDER, SERVICE_ORDER], "evidence was written on an order neither caller owns"


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag_off", "flag_on"])
def test_an_order_created_before_the_change_cannot_be_read_back_with_the_callers_own_key(loop, monkeypatch, flag):
    """Not through /invoke (it has no order read): the caller's own GET on the order route. The
    order is owned by this service's agent, so the answer is 403 before and after; the flag only
    stops NEW orders from being created that way."""
    _set_flag(monkeypatch, flag)
    res = loop["client"].get(f"/agent/v1/orders/{SERVICE_ORDER}", headers={"X-API-Key": CALLER_KEY})
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": "Not authorized for this order"}

    own = loop["client"].get(f"/agent/v1/orders/{CALLER_ORDER}", headers={"X-API-Key": CALLER_KEY})
    assert own.status_code == 200, own.text
    assert own.json()["order"]["currency"] == MARKET_CURRENCY["GB"] == "GBP"


def test_newly_allowed_a_payment_link_for_the_callers_own_order(loop, monkeypatch):
    """The other side of ownership: an order the caller created with its own key (on the keyed
    API) was refused through /invoke, because this service's agent does not own it."""
    before = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=CALLER_ORDER)
    assert before.status_code == 403, before.text
    assert before.json() == {"detail": {"detail": "Not authorized for this order"}}

    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "create_payment_link", CALLER_KEY, order_id=CALLER_ORDER)
    assert res.status_code == 200, res.text
    assert loop["intents"] == [{"agent_id": CALLER_AGENT, "currencies": ["GBP"]}]


def test_flag_on_another_callers_order_gets_no_payment_link(loop, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, "create_payment_link", OTHER_CALLER_KEY, order_id=CALLER_ORDER)
    assert res.status_code == 403, res.text
    assert loop["intents"] == []


# ══ flag on: only a key with the money gate's format goes upstream ════════════
# With the money gate switched off, any value a caller put in X-API-Key used to reach the proxy
# and be sent on; a value that could not be sent as a header failed while the request was built
# and the 502 quoted it back. The proxy now applies the gate's own format rule first.

# Raw header bytes, as an ASGI server would hand them over (decoded as latin-1 by the framework).
BAD_FORMAT_KEYS: Dict[str, bytes] = {
    "twenty_thousand_chars": b"ak_live_" + b"a" * 20000,
    "cr_lf_then_a_header": CALLER_KEY.encode() + b"\r\nX-Checkout-Token: injected",
    "lf_inside": b"ak_live_" + b"1" * 32 + b"\n" + b"1" * 32,
    "tab_inside": b"ak_live_" + b"1" * 32 + b"\t" + b"1" * 32,
    "nul_at_the_end": CALLER_KEY.encode() + b"\x00",
    "del_at_the_end": CALLER_KEY.encode() + b"\x7f",
    "non_ascii": b"ak_live_\xe9\xe9",
    "good_shape_then_non_ascii": CALLER_KEY.encode() + b"\xe9",
    "two_keys_comma_joined": CALLER_KEY.encode() + b", " + OTHER_CALLER_KEY.encode(),
    "uppercase_hex": b"ak_live_" + b"A" * 64,
    "one_char_short": CALLER_KEY.encode()[:-1],
    "one_char_long": CALLER_KEY.encode() + b"1",
    "wrong_prefix": b"sk_live_" + b"1" * 64,
    "short_text": MALFORMED_KEY.encode(),
}
BAD_FORMAT_IDS = sorted(BAD_FORMAT_KEYS)


def _raw_invoke(h, operation: str, raw_headers: List[tuple]):
    status, content = _raw_asgi_post(h["app"], operation, _payload(operation), raw_headers)
    return status, content


@pytest.mark.parametrize("shape", BAD_FORMAT_IDS)
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_key_without_the_gates_format_is_401_at_the_proxy_and_is_not_echoed(
    harness, monkeypatch, caplog, operation, shape
):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(GATE, "0")
    caplog.set_level(logging.DEBUG)
    raw = BAD_FORMAT_KEYS[shape]
    for header in ((b"x-api-key", raw), (b"authorization", b"Bearer " + raw)):
        harness["sent"].clear()
        status, content = _raw_invoke(harness, operation, [header])
        assert status == 401, (header[0], content)
        # The whole body is fixed: none of the caller's bytes can be in it.
        assert content == b'{"detail":{"detail":"Invalid API Key format"}}', header[0]
        assert json.loads(content) == BAD_FORMAT_ANSWER
        assert harness["sent"] == [], "nothing goes upstream, on any key"
    assert raw.decode("latin-1").strip() not in caplog.text
    assert "injected" not in caplog.text
    assert harness["db_calls"] == []


@pytest.mark.parametrize("shape", BAD_FORMAT_IDS)
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_off_a_key_without_the_gates_format_is_served_as_before(harness, monkeypatch, operation, shape):
    """Flag off, money gate off: the request sent before the flag existed (this service's key)."""
    monkeypatch.setenv(GATE, "0")
    status, content = _raw_invoke(harness, operation, [(b"x-api-key", BAD_FORMAT_KEYS[shape])])
    assert status == 200, content
    assert _upstream_headers(harness) == SERVICE_KEY_HEADERS


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("shape", BAD_FORMAT_IDS)
def test_the_money_gate_refuses_a_key_without_its_format_before_the_proxy_whatever_the_flag(
    harness, monkeypatch, shape, flag
):
    _set_flag(monkeypatch, flag)
    for operation in MONEY_OPERATIONS:
        status, content = _raw_invoke(harness, operation, [(b"x-api-key", BAD_FORMAT_KEYS[shape])])
        assert status == 401, content
        assert json.loads(content)["detail"]["error"] == gw.CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION
        assert harness["sent"] == []


@pytest.mark.parametrize("shape", BAD_FORMAT_IDS)
def test_the_proxy_refuses_a_key_without_the_gates_format_before_it_needs_any_key_of_its_own(
    harness, monkeypatch, shape
):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setattr(gw, "AGENT_API_KEY", None)
    key = BAD_FORMAT_KEYS[shape].decode("latin-1")
    with pytest.raises(gw.HTTPException) as refused:
        _proxy(caller_api_key=key)
    assert refused.value.status_code == 401
    assert refused.value.detail == {"detail": "Invalid API Key format"}
    assert harness["sent"] == []


def test_the_proxys_bad_format_answer_is_a_fresh_copy_each_time(harness, monkeypatch):
    """The answer is built from a module constant; a handler that changed one answer's detail
    must not change the next one."""
    monkeypatch.setenv(FLAG, "1")
    details = []
    for _ in range(2):
        with pytest.raises(gw.HTTPException) as refused:
            _proxy(caller_api_key=MALFORMED_KEY)
        details.append(refused.value.detail)
        refused.value.detail["detail"] = "changed by a handler"
    assert details[0] is not details[1]
    assert gw._FORWARD_CALLER_KEY_BAD_FORMAT_DETAIL == {"detail": "Invalid API Key format"}


@pytest.mark.parametrize(
    "key,expected",
    [
        (CALLER_KEY, True),
        ("ak_" + "0" * 64, True),
        (UNKNOWN_KEY, True),
        (INTERNAL_KEY, True),
        (MALFORMED_KEY, False),
        ("", False),
        ("ak_live_" + "a" * 20000, False),
        (CALLER_KEY + "\r\nX-Checkout-Token: injected", False),
        ("ak_live_\xe9\xe9", False),
        (INTERNAL_KEY + "x", False),
        (INTERNAL_KEY + "\xe9", False),
    ],
)
def test_the_format_rule(harness, key, expected):
    assert gw._agent_key_has_money_gate_format(key) is expected


def _function(tree: ast.Module, name: str) -> ast.AST:
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def test_the_gate_and_the_proxy_read_one_format_rule():
    """One rule, not two: the key regex is named in the rule only, and both the money gate and
    the proxy call the rule. A second copy could drift and let the proxy send a value the gate
    calls malformed."""
    tree = _module_tree()
    rule = "_agent_key_has_money_gate_format"
    for reader in ("_money_operation_refusal_reason", "_proxy_agent_api"):
        assert len(_calls_to(_function(tree, reader), rule)) == 1, reader
    regex_readers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(n, ast.Name) and n.id == "_AGENT_API_KEY_RE" for n in ast.walk(node))
    ]
    assert regex_readers == [rule]


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_flag_on_a_bad_format_key_is_401_through_the_real_upstream_routes_too(loop, monkeypatch, operation):
    """Same answer as the upstream route gave for a malformed key, now without the upstream call."""
    monkeypatch.setenv(GATE, "0")
    monkeypatch.setenv(FLAG, "1")
    res = _invoke(loop, operation, MALFORMED_KEY)
    assert res.status_code == 401, res.text
    assert res.json() == BAD_FORMAT_ANSWER
    assert loop["sent"] == [] and loop["authenticated"] == []


# ══ a non-ASCII key with an internal trusted key configured ═══════════════════
# The trusted-key check compared two str in constant time, which raises on a non-ASCII
# character: with any trusted key configured, a key carrying one byte >= 0x80 raised out of the
# money gate (and out of the keyed routes' auth dependency) as a 500. It compares bytes now.

NON_ASCII_KEYS: Dict[str, bytes] = {
    "non_ascii": b"ak_live_\xe9\xe9",
    "good_shape_then_non_ascii": CALLER_KEY.encode() + b"\xe9",
    "trusted_key_then_non_ascii": INTERNAL_KEY.encode() + b"\xe9",
    "one_high_byte": b"\xff",
}


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("shape", sorted(NON_ASCII_KEYS))
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_a_non_ascii_key_gets_the_money_gates_refusal_not_a_500_with_a_trusted_key_configured(
    harness, monkeypatch, operation, shape, flag
):
    assert agent_auth._INTERNAL_TRUSTED_API_KEYS == (INTERNAL_KEY,)
    _set_flag(monkeypatch, flag)
    for header in ((b"x-api-key", NON_ASCII_KEYS[shape]), (b"authorization", b"Bearer " + NON_ASCII_KEYS[shape])):
        status, content = _raw_invoke(harness, operation, [header])
        assert status == 401, (header[0], content)
        assert json.loads(content)["detail"] == {
            "error": gw.CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION,
            "message": (
                "This operation creates or changes an order or a payment. Send an active agent "
                "API key (X-API-Key or Authorization: Bearer) or a checkout token (X-Checkout-Token)."
            ),
            "operation": operation,
        }
        assert harness["sent"] == []
    assert harness["db_calls"] == []


@pytest.mark.parametrize("shape", sorted(NON_ASCII_KEYS))
def test_a_non_ascii_key_is_the_keyed_routes_normal_401_with_a_trusted_key_configured(loop, monkeypatch, shape):
    """The same helper runs in the keyed routes' auth dependency: called directly (not through
    /invoke) with a non-ASCII key they answer their malformed-key 401, not a 500."""
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (INTERNAL_KEY,))
    status, content = _raw_asgi_post(
        loop["client"].app, "", {}, [(b"x-api-key", NON_ASCII_KEYS[shape])],
        path="/agent/v1/orders/payment-offer-evidence",
        raw_body=_payload("record_payment_offer_evidence"),
    )
    assert status == 401, content
    assert json.loads(content) == {"detail": "Invalid API Key format"}
    assert loop["authenticated"] == []


@pytest.mark.parametrize(
    "trusted,candidate,expected",
    [
        ((INTERNAL_KEY,), INTERNAL_KEY, True),
        ((INTERNAL_KEY,), f"  {INTERNAL_KEY} ", True),
        (("other-fixture-key", INTERNAL_KEY), INTERNAL_KEY, True),
        ((INTERNAL_KEY,), INTERNAL_KEY + "x", False),
        ((INTERNAL_KEY,), INTERNAL_KEY[:-1], False),
        ((INTERNAL_KEY,), "ak_live_\xe9\xe9", False),
        ((INTERNAL_KEY,), INTERNAL_KEY + "\xe9", False),
        ((INTERNAL_KEY,), "中文", False),
        ((INTERNAL_KEY,), "\ud800", False),
        (("fixture-k\xe9y",), "fixture-k\xe9y", True),
        (("fixture-k\xe9y",), "fixture-key", False),
        (("fixture-k\xe9y",), CALLER_KEY, False),
        ((INTERNAL_KEY,), "", False),
        ((INTERNAL_KEY,), None, False),
        ((), INTERNAL_KEY, False),
        ((), "ak_live_\xe9\xe9", False),
    ],
)
def test_the_trusted_key_check_answers_for_any_text_and_never_raises(monkeypatch, trusted, candidate, expected):
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", trusted)
    assert agent_auth._is_internal_trusted_api_key(candidate) is expected


def test_the_other_readers_of_the_trusted_key_check_do_not_raise_on_a_non_ascii_key(monkeypatch):
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (INTERNAL_KEY,))
    monkeypatch.setattr(agent_auth, "get_agent_by_key", _get_agent_by_key)
    for key in ("ak_live_\xe9\xe9", CALLER_KEY + "\xe9", INTERNAL_KEY + "\xe9"):
        assert asyncio.run(agent_auth.resolve_issuing_agent_id(key)) is None
        assert asyncio.run(agent_auth._is_service_caller(key)) is False
    assert asyncio.run(agent_auth.resolve_issuing_agent_id(INTERNAL_KEY)) is None
    assert asyncio.run(agent_auth._is_service_caller(INTERNAL_KEY)) is True


# ══ pinned as it is: an empty X-API-Key header ahead of a real one ════════════
# The key reader takes the FIRST X-API-Key header. When that one is empty the request counts as
# carrying no key, whatever a later header says: the money gate refuses it, and with that gate
# switched off it is served on this service's key like any keyless request, flag on or off.

@pytest.mark.parametrize("flag", [None, "1"], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("empty", [b"", b"   "], ids=["empty", "blank"])
@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_pinned_an_empty_key_header_ahead_of_a_real_one_is_a_keyless_request(harness, monkeypatch, operation, empty, flag):
    _set_flag(monkeypatch, flag)
    duplicate = [(b"x-api-key", empty), (b"x-api-key", CALLER_KEY.encode())]

    status, content = _raw_invoke(harness, operation, duplicate)
    assert status == 401, content
    assert json.loads(content)["detail"]["error"] == gw.CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION
    assert harness["sent"] == []

    monkeypatch.setenv(GATE, "0")
    status, content = _raw_invoke(harness, operation, duplicate)
    assert status == 200, content
    assert _upstream_headers(harness) == SERVICE_KEY_HEADERS, "keyless: the caller's later key is not read"


@pytest.mark.parametrize("operation", MONEY_OPERATIONS)
def test_pinned_a_real_key_header_ahead_of_an_empty_one_is_that_key(harness, monkeypatch, operation):
    monkeypatch.setenv(FLAG, "1")
    status, content = _raw_invoke(
        harness, operation, [(b"x-api-key", CALLER_KEY.encode()), (b"x-api-key", b"")]
    )
    assert status == 200, content
    assert _upstream_headers(harness) == {"Content-Type": "application/json", "X-API-Key": CALLER_KEY}
