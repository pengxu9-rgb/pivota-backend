"""The keyed payment routes serve an order's OWNER, not everyone in its merchant's scope.

Three routes compared the caller's merchant scope with the order's merchant and never the caller
with the order's owner (orders.agent_id):

    POST /agent/v1/payments                         start or reuse a payment (returns its client secret)
    POST /agent/v1/orders/payment-offer-evidence    write the order's metadata
    GET  /agent/v1/payments/{payment_id}            read a payment

AGENT_PAYMENT_ORDER_OWNER_CHECK (off | observe | enforce, default off) adds the owner rule the
order read and the /agent/v2 routes already apply (routes.agent_forwarded_owner.order_access_allowed).

Everything here goes through the REAL routes with the real auth dependency (get_agent_context):
keys, an internal trusted key and signed checkout tokens are presented as headers. The second half
goes through POST /agent/shop/v1/invoke, whose proxied call is delivered to the same app, with
SHOP_INVOKE_FORWARD_CALLER_KEY off and on.

`run_matrix` is also what the flag-off comparison against the base commit runs (every caller x
every order x the three routes: status, body, writes, reads, PSP calls).

Keys, secrets, ids and names below are made up.
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
import textwrap
from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.agents as agents_db
import mvp.governance as mvp_governance
import routes.agent_api as agent_api
import routes.agent_auth as agent_auth
import routes.agent_forwarded_owner as forwarded_owner
import routes.agent_order_owner_check as owner_check
import routes.agent_payment_sdk as payments
import routes.agent_shop_gateway as gw
import routes.agent_v2 as agent_v2
import services.market_registry as market_registry
from db.database import database as shared_database
from utils.logger import logger as pivota_logger

FLAG = "AGENT_PAYMENT_ORDER_OWNER_CHECK"
FORWARD_FLAG = "SHOP_INVOKE_FORWARD_CALLER_KEY"
MONEY_GATE = "SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL"
PARTNER_FORWARDING_FLAG = "PARTNER_IDENTITY_FORWARDING_ENABLED"
INVOKE = "/agent/shop/v1/invoke"
LOOPBACK_BASE = "http://127.0.0.1:8080"

OFF_VALUES: List[Optional[str]] = [
    None, "", "off", "OFF", "0", "false", "no", "1", "true", "on", "yes", "enabled", "observed",
    "enforced", "enforce,observe", "observe enforce", "anything-else",
]
OBSERVE_VALUES = ["observe", "OBSERVE", " Observe "]
ENFORCE_VALUES = ["enforce", "ENFORCE", " Enforce "]

# Placeholder credentials: right shape, made-up values.
OWNER_KEY = "ak_live_" + "1" * 64
SCOPED_KEY = "ak_live_" + "2" * 64
UNRESTRICTED_KEY = "ak_live_" + "3" * 64
OUTSIDE_KEY = "ak_live_" + "4" * 64
SERVICE_KEY = "ak_live_" + "a" * 64
VOUCHER_KEY = "ak_live_" + "b" * 64
INTERNAL_KEY = "internal-fixture-key"
TOKEN_SECRET = "fixture-checkout-token-secret"

MERCHANT = "merch_fixture_shop"
OTHER_MERCHANT = "merch_fixture_elsewhere"

OWNER_AGENT = "agent_fixture_owner"
SCOPED_AGENT = "agent_fixture_scoped"
UNRESTRICTED_AGENT = "agent_fixture_unrestricted"
OUTSIDE_AGENT = "agent_fixture_outside"
THIRD_AGENT = "agent_fixture_third"
SERVICE_AGENT = "agent_fixture_service"
VOUCHER_AGENT = "agent_fixture_voucher"
INTERNAL_AGENT = agent_auth._build_internal_trusted_agent(INTERNAL_KEY)["agent_id"]


def _agent(agent_id: str, allowed_merchants: Optional[List[str]]) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "agent_name": "Fixture Agent",
        "is_active": True,
        "allowed_merchants": allowed_merchants,
        "rate_limit": 100000,
        "daily_quota": 1000000,
    }


AGENTS_BY_KEY: Dict[str, Dict[str, Any]] = {
    OWNER_KEY: _agent(OWNER_AGENT, [MERCHANT]),
    SCOPED_KEY: _agent(SCOPED_AGENT, [MERCHANT]),
    UNRESTRICTED_KEY: _agent(UNRESTRICTED_AGENT, None),
    OUTSIDE_KEY: _agent(OUTSIDE_AGENT, [OTHER_MERCHANT]),
    SERVICE_KEY: _agent(SERVICE_AGENT, None),
    VOUCHER_KEY: _agent(VOUCHER_AGENT, None),
}
AGENTS_BY_ID = {row["agent_id"]: row for row in AGENTS_BY_KEY.values()}

# Market -> the currency its orders are in. The registry is the one table; a market it does not
# hold (DE here) has its currency written on the order by whoever priced it, and an order with no
# market at all still has the currency it was priced in. Nothing below supplies a missing one.
MARKETS = ["US", "SG", "GB", "DE", "absent"]


def market_currency(market: str) -> str:
    if market == "DE":
        assert market_registry.market_or_none("DE") is None, "DE is now a registry market: read it from there"
        return "EUR"
    if market == "absent":
        return "JPY"
    return market_registry.currency_for(market)


def test_the_market_table_is_the_registrys_and_never_one_currency_for_all():
    assert [market_currency(m) for m in MARKETS] == ["USD", "SGD", "GBP", "EUR", "JPY"]


# ── orders ────────────────────────────────────────────────────────────────────

O_OWNER = "ORD_fixture_owner"
O_SCOPED = "ORD_fixture_scoped"
O_THIRD = "ORD_fixture_third"
O_INTERNAL = "ORD_fixture_internal_service"
O_SERVICE = "ORD_fixture_service_agent"
O_NULL = "ORD_fixture_no_owner"
O_BLANK = "ORD_fixture_blank_owner"
O_OUTSIDE = "ORD_fixture_owned_by_outside"
MISSING = "ORD_fixture_missing"
ORDER_OWNERS: Dict[str, Optional[str]] = {
    O_OWNER: OWNER_AGENT,
    O_SCOPED: SCOPED_AGENT,
    O_THIRD: THIRD_AGENT,
    O_INTERNAL: INTERNAL_AGENT,
    O_SERVICE: SERVICE_AGENT,
    O_NULL: None,
    O_BLANK: "  ",
    O_OUTSIDE: OUTSIDE_AGENT,
}
MATRIX_ORDERS = list(ORDER_OWNERS) + [MISSING]

LIVE_QUOTE_EXPIRY = "2099-01-01T00:00:00Z"
PAST_QUOTE_EXPIRY = "2001-01-01T00:00:00Z"


def client_secret_of(order_id: str) -> str:
    return f"pi_made_up_{order_id}_secret_made_up"


def intent_of(order_id: str) -> str:
    return f"pi_made_up_{order_id}"


def payment_id_of(order_id: str) -> str:
    return f"pay_made_up_{order_id}"


NEW_INTENT = "pi_made_up_new_surface"
NEW_CLIENT_SECRET = "pi_made_up_new_surface_secret_made_up"


def _order(
    order_id: str,
    *,
    agent_id: Optional[str],
    market: str = "GB",
    merchant_id: str = MERCHANT,
    with_surface: bool = True,
    payment_status: str = "processing",
    quote_expires_at: Optional[str] = LIVE_QUOTE_EXPIRY,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pricing_quote: Dict[str, Any] = {"quote_id": f"q_{order_id}", "live_validation": {"status": "validated"}}
    if quote_expires_at is not None:
        pricing_quote["expires_at"] = quote_expires_at
    row: Dict[str, Any] = {
        "order_id": order_id,
        "agent_id": agent_id,
        "merchant_id": merchant_id,
        "customer_email": "buyer@example.com",
        "currency": market_currency(market),
        "total": 25.0,
        "total_amount": 25.0,
        "status": "pending",
        "payment_status": payment_status,
        "items": [{"product_id": "prod_fixture", "merchant_id": merchant_id, "quantity": 1, "unit_price": 25.0}],
        "shipping_address": {} if market == "absent" else {"country": market},
        "metadata": {"pricing_quote": pricing_quote, **(metadata or {})},
        "created_at": "2026-01-01T00:00:00",
    }
    if with_surface:
        row.update(psp_used="stripe", payment_intent_id=intent_of(order_id), client_secret=client_secret_of(order_id))
    return row


def _orders() -> Dict[str, Dict[str, Any]]:
    return {order_id: _order(order_id, agent_id=owner) for order_id, owner in ORDER_OWNERS.items()}


def _payment_row(order: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "payment_id": payment_id_of(order["order_id"]),
        "order_id": order["order_id"],
        "payment_intent_id": intent_of(order["order_id"]),
        "status": "requires_action",
        "amount": 25.0,
        "currency": order["currency"],
        "psp_type": "stripe",
        "created_at": datetime(2026, 1, 1, 0, 0, 0),
        "updated_at": None,
        # payments.agent_id is whoever STARTED the payment, which with the check off can be an
        # agent that does not own the order. It is never the owner the status route judges.
        "agent_id": SCOPED_AGENT,
    }


# ── harness: the real routes, the real auth dependency, every read and write faked and recorded ─


class _Decision:
    decision = "allow"
    reason_codes: List[str] = []
    required_scopes: List[str] = []
    risk_tier = "low"


class _LoopbackClient:
    """Delivers the /invoke proxy's request to the same app, as the loopback call does when deployed."""

    def __init__(self, app: FastAPI, state: Dict[str, Any]) -> None:
        self._app = app
        self._state = state

    async def request(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self._state["upstream"].append({"method": method, "url": url, "headers": dict(headers or {})})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url=LOOPBACK_BASE) as client:
            return await client.request(method, url, json=json, headers=headers)


def _sql(query: Any) -> str:
    return " ".join(str(query).split())


def build_harness(monkeypatch, *, service_key_is_internal_trusted: bool = False) -> Dict[str, Any]:
    for name in (FLAG, FORWARD_FLAG, MONEY_GATE, PARTNER_FORWARDING_FLAG, "CHECKOUT_CANCEL_UPSTREAM_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", TOKEN_SECRET)
    trusted = (INTERNAL_KEY, SERVICE_KEY) if service_key_is_internal_trusted else (INTERNAL_KEY,)
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", trusted)

    orders = _orders()
    state: Dict[str, Any] = {
        "orders": orders,
        "writes": [],          # every write a route made, in order
        "reads": [],           # every database read a route made, in order
        "psp_calls": [],       # new payment surfaces asked of the PSP
        "upstream": [],        # what the /invoke proxy sent
        "authenticated": [],   # agent ids the auth dependency admitted
    }

    async def get_agent_by_key(api_key, metrics_out=None):
        return AGENTS_BY_KEY.get(api_key)

    async def get_agent(agent_id):
        row = AGENTS_BY_ID.get(agent_id)
        return dict(row) if row else None

    async def check_rate_limit(agent_id, rate_limit=None):
        return True, 0, int(rate_limit or 100)

    async def check_daily_quota(agent_id, daily_quota=None):
        state["authenticated"].append(agent_id)
        return True, 0, int(daily_quota or 1000)

    async def update_agent_stats(agent_id, **kwargs):
        return None

    monkeypatch.setattr(agents_db, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    monkeypatch.setattr(agent_auth, "check_rate_limit", check_rate_limit)
    monkeypatch.setattr(agent_auth, "check_daily_quota", check_daily_quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", update_agent_stats)

    async def get_order(order_id):
        row = orders.get(str(order_id))
        return copy.deepcopy(row) if row else None

    async def update_order(order_id, fields):
        fields = copy.deepcopy(fields)
        for event in [fields["metadata"].get("last_payment_offer_evidence")] + list(fields["metadata"].get("payment_offer_events") or []):
            if isinstance(event, dict):
                event.pop("recorded_at", None)
        state["writes"].append({"write": "update_order", "order_id": str(order_id), "fields": fields})
        return True

    for module in (agent_api, payments, agent_v2):
        monkeypatch.setattr(module, "get_order", get_order)
    monkeypatch.setattr(agent_api, "update_order", update_order)
    monkeypatch.setattr(agent_api, "emit_payment_offer_analytics_event", lambda **_k: None)

    async def log_agent_request(**_kwargs):
        return None

    monkeypatch.setattr(agent_api, "log_agent_request", log_agent_request)
    monkeypatch.setattr(payments, "log_agent_request", log_agent_request)

    async def fetch_one(query, values=None):
        sql = _sql(query)
        state["reads"].append({"sql": sql, "values": dict(values or {})})
        if "FROM payments p JOIN orders o" in sql:
            for order in orders.values():
                if payment_id_of(order["order_id"]) == (values or {}).get("payment_id"):
                    return {**_payment_row(order), "merchant_id": order["merchant_id"]}
            return None
        if "FROM payments WHERE idempotency_key" in sql:
            return None
        if sql.startswith("SELECT agent_id, metadata FROM orders"):
            order = orders.get(str((values or {}).get("order_id")))
            return {"agent_id": order["agent_id"], "metadata": copy.deepcopy(order["metadata"])} if order else None
        raise AssertionError(f"unexpected read: {sql}")

    async def execute(query, values=None):
        values = {k: v for k, v in dict(values or {}).items() if k != "created_at"}
        state["writes"].append({"write": "execute", "sql": _sql(query), "values": values})
        return 1

    def _refuse(name):
        def _spy(*args, **kwargs):
            raise AssertionError(f"database.{name} called")
        return _spy

    monkeypatch.setattr(shared_database, "fetch_one", fetch_one)
    monkeypatch.setattr(shared_database, "execute", execute)
    for name in ("execute_many", "fetch_all", "fetch_val", "transaction"):
        if hasattr(shared_database, name):
            monkeypatch.setattr(shared_database, name, _refuse(name))

    async def get_primary_store(merchant_id):
        return {"platform": "shopify", "store_id": "store_fixture", "merchant_id": merchant_id}

    async def no_runtime_psp(*args, **kwargs):
        return None

    async def select_psp(self, *, agent_id, merchant_id, amount, currency):
        return "stripe", {"route_id": "route_fixture"}

    async def get_merchant_onboarding(merchant_id):
        return {"merchant_id": merchant_id}

    async def create_payment_with_failover(*args, **kwargs):
        state["psp_calls"].append({"amount": str(kwargs.get("amount")), "currency": kwargs.get("currency"),
                                   "order_id": (kwargs.get("metadata") or {}).get("order_id")})
        intent = SimpleNamespace(id=NEW_INTENT, client_secret=NEW_CLIENT_SECRET, status="requires_action",
                                 raw_response={}, psp_type="stripe")
        return True, intent, None, "stripe"

    async def update_payment_info(**kwargs):
        state["writes"].append({"write": "update_payment_info", **kwargs})
        return True

    async def upsert_order_attribution_edge(**_kwargs):
        return None

    async def append_internal_fact_best_effort(**_kwargs):
        return None

    monkeypatch.setattr(payments, "get_primary_store", get_primary_store)
    monkeypatch.setattr(payments, "fetch_active_runtime_merchant_psp", no_runtime_psp)
    monkeypatch.setattr(payments.PaymentRoutingService, "select_psp", select_psp)
    monkeypatch.setattr(payments, "get_merchant_onboarding", get_merchant_onboarding)
    monkeypatch.setattr(payments, "create_payment_with_failover", create_payment_with_failover)
    monkeypatch.setattr(payments, "update_payment_info", update_payment_info)
    monkeypatch.setattr("services.commerce_attribution_service.upsert_order_attribution_edge", upsert_order_attribution_edge)
    monkeypatch.setattr("services.pcs_fact_ingest.append_internal_fact_best_effort", append_internal_fact_best_effort)
    monkeypatch.setattr("routes.order_routes._resolve_order_live_readiness_requirement", lambda *_a, **_k: False)
    monkeypatch.setattr("mvp.events.emit_best_effort", lambda **_k: None)
    monkeypatch.setattr("mvp.ledger_events.emit_ledger_event_best_effort", lambda **_k: None)
    monkeypatch.setattr(mvp_governance.governance, "evaluate", lambda *_a, **_k: _Decision())
    monkeypatch.setattr(mvp_governance.governance, "record_audit_event", lambda **_k: None)

    monkeypatch.setattr(gw, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gw, "AGENT_API_KEY", SERVICE_KEY)
    monkeypatch.setattr(gw, "AGENT_API_BASE", LOOPBACK_BASE)

    app = FastAPI()
    app.include_router(gw.router)
    app.include_router(agent_api.router)
    app.include_router(payments.router)
    app.include_router(agent_v2.router)

    async def get_client():
        return _LoopbackClient(app, state)

    monkeypatch.setattr(gw, "_get_shared_upstream_http_client", get_client)
    state["client"] = TestClient(app, raise_server_exceptions=False)
    return state


@pytest.fixture
def h(monkeypatch):
    return build_harness(monkeypatch)


@pytest.fixture
def owner_lines():
    """The owner check's log lines, read off the service logger without changing any level."""
    records: List[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if str(record.msg).startswith("order_owner_check"):
                records.append(record)

    handler = _Collect(level=logging.DEBUG)
    level_before = pivota_logger.level
    pivota_logger.addHandler(handler)
    try:
        yield records
    finally:
        pivota_logger.removeHandler(handler)
        assert pivota_logger.level == level_before


WATCHED_LOGGERS = ["", "pivota", "httpx", "httpcore", "asyncio", "uvicorn", "fastapi"]


@pytest.fixture(autouse=True)
def _no_logger_level_is_left_changed():
    before = {name: (logging.getLogger(name).level, logging.getLogger(name).disabled, len(logging.getLogger(name).handlers))
              for name in WATCHED_LOGGERS}
    yield
    after = {name: (logging.getLogger(name).level, logging.getLogger(name).disabled, len(logging.getLogger(name).handlers))
             for name in WATCHED_LOGGERS}
    assert after == before


def _set(monkeypatch, name: str, value: Optional[str]) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


# ── callers ───────────────────────────────────────────────────────────────────


def mint_token(*, agent_id: str = OWNER_AGENT, order_id: Optional[str] = None, intent_id: str = "ci_fixture") -> str:
    from routes.agent_checkout_intents import mint_checkout_token

    claims: Dict[str, Any] = {"agent_id": agent_id, "intent_id": intent_id, "merchant_ids": [MERCHANT], "scopes": ["checkout"]}
    if order_id:
        claims["order_id"] = order_id
    return mint_checkout_token(claims, ttl_seconds=600)


CALLERS = ["owner", "scoped", "unrestricted", "outside", "internal", "token_for_this_order", "token_for_another_order", "none"]
CALLER_AGENTS = {"owner": OWNER_AGENT, "scoped": SCOPED_AGENT, "unrestricted": UNRESTRICTED_AGENT,
                 "outside": OUTSIDE_AGENT, "internal": INTERNAL_AGENT}


def caller_headers(caller: str, order_id: str) -> Dict[str, str]:
    if caller == "owner":
        return {"X-API-Key": OWNER_KEY}
    if caller == "scoped":
        return {"Authorization": f"Bearer {SCOPED_KEY}"}
    if caller == "unrestricted":
        return {"X-API-Key": UNRESTRICTED_KEY}
    if caller == "outside":
        return {"X-API-Key": OUTSIDE_KEY}
    if caller == "internal":
        return {"X-API-Key": INTERNAL_KEY}
    if caller == "token_for_this_order":
        # A token of the owner agent, minted for exactly this order id.
        return {"X-Checkout-Token": mint_token(order_id=order_id)}
    if caller == "token_for_another_order":
        return {"X-Checkout-Token": mint_token(order_id="ORD_fixture_some_other_order")}
    if caller == "none":
        return {}
    raise AssertionError(caller)


ROUTES = ["payments_create", "payment_offer_evidence", "payment_status"]


def call(h, route: str, caller: str, order_id: str, *, headers: Optional[Dict[str, str]] = None, **body: Any):
    headers = caller_headers(caller, order_id) if headers is None else headers
    if route == "payments_create":
        return h["client"].post("/agent/v1/payments", headers=headers,
                                json={"order_id": order_id, "payment_method": {"type": "card"}, **body})
    if route == "payment_offer_evidence":
        return h["client"].post("/agent/v1/orders/payment-offer-evidence", headers=headers,
                                json={"order_id": order_id, "selected_payment_offer_id": "offer_fixture",
                                      "payment_method_evidence": {"psp": "stripe", "card_network": "fixturecard"}, **body})
    if route == "payment_status":
        return h["client"].get(f"/agent/v1/payments/{payment_id_of(order_id)}", headers=headers)
    raise AssertionError(route)


def _scrubbed(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: ("<time>" if k == "created_at" and route_time(v) else _scrubbed(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [_scrubbed(v) for v in body]
    return body


def route_time(value: Any) -> bool:
    return isinstance(value, str) and value[:2] == "20"


def run_matrix(h) -> List[Dict[str, Any]]:
    """Every caller x every order x the three routes: what was answered, written, read and asked of
    the PSP. Deterministic (the one clock value, the response's created_at, is scrubbed)."""
    records = []
    for route in ROUTES:
        for caller in CALLERS:
            for order_id in MATRIX_ORDERS:
                for key in ("writes", "reads", "psp_calls"):
                    h[key].clear()
                res = call(h, route, caller, order_id)
                try:
                    body = _scrubbed(res.json())
                except ValueError:
                    body = res.text
                records.append({
                    "route": route, "caller": caller, "order": order_id, "status": res.status_code, "body": body,
                    "writes": copy.deepcopy(h["writes"]), "reads": copy.deepcopy(h["reads"]),
                    "psp_calls": copy.deepcopy(h["psp_calls"]),
                })
    return records


# ── who is who: written out by hand, not computed from the rule under test ─────

OWNER, NOT_OWNER, OUT_OF_SCOPE, TOKEN_OK, TOKEN_NOT_BOUND, NO_CREDENTIAL = (
    "owner", "not_owner_in_scope", "out_of_scope", "token_bound", "token_not_bound", "no_credential",
)
RELATION: Dict[Tuple[str, str], str] = {}
for _order_id in ORDER_OWNERS:
    RELATION[("owner", _order_id)] = OWNER if _order_id == O_OWNER else NOT_OWNER
    RELATION[("scoped", _order_id)] = OWNER if _order_id == O_SCOPED else NOT_OWNER
    RELATION[("unrestricted", _order_id)] = NOT_OWNER
    RELATION[("outside", _order_id)] = OUT_OF_SCOPE          # also on the order it owns: its scope lost the merchant
    RELATION[("internal", _order_id)] = OWNER if _order_id == O_INTERNAL else NOT_OWNER
    # The token is the owner AGENT's, so it is bound only to that agent's order.
    RELATION[("token_for_this_order", _order_id)] = TOKEN_OK if _order_id == O_OWNER else TOKEN_NOT_BOUND
    RELATION[("token_for_another_order", _order_id)] = TOKEN_NOT_BOUND
    RELATION[("none", _order_id)] = NO_CREDENTIAL

OWNER_DENIED = {"detail": "Not authorized for this order"}
TOKEN_ORDER_DENIED = {"detail": "Checkout token not authorized for this order"}
TOKEN_ROUTE_DENIED = {"detail": "Checkout token not authorized for this endpoint"}
NO_KEY = {"detail": "Missing API Key. Please provide X-API-Key header"}
MERCHANT_DENIED = {
    "payments_create": {"detail": "Not authorized for this merchant"},
    "payment_offer_evidence": {"detail": "Not authorized for this merchant"},
    "payment_status": {"detail": "Not authorized"},
}
SERVED = "served"


def expected(route: str, relation: str, mode: str) -> Any:
    """SERVED, or (status, exact body)."""
    if relation == NO_CREDENTIAL:
        return 401, NO_KEY
    if relation in (TOKEN_OK, TOKEN_NOT_BOUND) and route == "payment_status":
        return 403, TOKEN_ROUTE_DENIED
    if relation == TOKEN_NOT_BOUND:
        return 403, TOKEN_ORDER_DENIED
    if relation == OUT_OF_SCOPE:
        return 403, MERCHANT_DENIED[route]
    if relation == NOT_OWNER and mode == "enforce":
        return 403, OWNER_DENIED
    return SERVED


def assert_served(h, route: str, order_id: str, res) -> None:
    order = h["orders"][order_id]
    assert res.status_code == 200, res.text
    body = res.json()
    if route == "payments_create":
        assert body["client_secret"] == client_secret_of(order_id)
        assert body["payment_intent_id"] == intent_of(order_id)
        assert body["currency"] == order["currency"]
        assert h["writes"] == [] and h["psp_calls"] == []
    elif route == "payment_offer_evidence":
        assert body["recorded"] is True and body["order_id"] == order_id
        assert [w["order_id"] for w in h["writes"]] == [order_id]
        assert h["writes"][0]["fields"]["metadata"]["selected_payment_offer_id"] == "offer_fixture"
    else:
        assert body["payment"]["order_id"] == order_id
        assert body["payment"]["currency"] == order["currency"]
        assert h["writes"] == []


def assert_carries_nothing_of_the_order(h, res, order_id: str) -> None:
    text = res.text
    order = h["orders"].get(order_id) or {}
    for secret in (client_secret_of(order_id), intent_of(order_id), payment_id_of(order_id), "_secret_", "pi_made_up",
                   NEW_CLIENT_SECRET, NEW_INTENT, str(order.get("agent_id") or "agent_fixture_never"), "25.0",
                   str(order.get("currency") or "CURRENCY_NEVER"), "q_ORD"):
        assert secret not in text, (secret, text)


def _reset(h) -> None:
    for key in ("writes", "reads", "psp_calls", "upstream", "authenticated"):
        h[key].clear()


# ══ the matrix: caller x owner x route x mode ═════════════════════════════════


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("route", ROUTES)
def test_who_is_served_and_who_is_refused(h, monkeypatch, owner_lines, route, mode):
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    for caller in CALLERS:
        for order_id in ORDER_OWNERS:
            _reset(h)
            owner_lines.clear()
            relation = RELATION[(caller, order_id)]
            res = call(h, route, caller, order_id)
            want = expected(route, relation, mode)
            where = (route, mode, caller, order_id, relation)
            if want == SERVED:
                assert_served(h, route, order_id, res)
            else:
                assert (res.status_code, res.json()) == want, (where, res.text)
                assert h["writes"] == [] and h["psp_calls"] == [], where
                assert_carries_nothing_of_the_order(h, res, order_id)
            lines = [r.getMessage() for r in owner_lines]
            if relation == NOT_OWNER and mode != "off":
                outcome = "refused" if mode == "enforce" else "would_refuse"
                assert len(lines) == 1, (where, lines)
                assert lines[0].startswith(f"order_owner_check outcome={outcome} mode={mode} route={route} "), lines
                assert owner_lines[0].levelno == logging.WARNING
                assert lines[0].endswith(f"caller_agent_id={CALLER_AGENTS[caller]}"), lines
            else:
                assert lines == [], (where, lines)


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("route", ROUTES)
def test_a_missing_order_answers_as_before_for_every_caller(h, monkeypatch, owner_lines, route, mode):
    """404 for a key, in scope or not, and the token refusal for a token: the owner check never
    answers before the not-found, so it tells nobody whether an order exists."""
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    not_found = {"detail": "Payment not found"} if route == "payment_status" else {"detail": "Order not found"}
    for caller in CALLERS:
        res = call(h, route, caller, MISSING)
        if caller == "none":
            want = (401, NO_KEY)
        elif caller.startswith("token") and route == "payment_status":
            want = (403, TOKEN_ROUTE_DENIED)
        elif caller.startswith("token"):
            want = (403, TOKEN_ORDER_DENIED)
        else:
            want = (404, not_found)
        assert (res.status_code, res.json()) == want, (caller, res.text)
    assert owner_lines == []


def test_the_matrix_covers_every_caller_and_owner_kind_asked_for():
    owners = set(ORDER_OWNERS.values())
    assert {OWNER_AGENT, SCOPED_AGENT, THIRD_AGENT, INTERNAL_AGENT, SERVICE_AGENT, None, "  ", OUTSIDE_AGENT} == owners
    assert set(RELATION.values()) == {OWNER, NOT_OWNER, OUT_OF_SCOPE, TOKEN_OK, TOKEN_NOT_BOUND, NO_CREDENTIAL}
    assert len(RELATION) == len(CALLERS) * len(ORDER_OWNERS)


# ══ the flag ═════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("value", OFF_VALUES, ids=[repr(v) for v in OFF_VALUES])
def test_mode_reader_is_off_unless_it_says_observe_or_enforce(monkeypatch, value):
    _set(monkeypatch, FLAG, value)
    assert owner_check.order_owner_check_mode() == "off"


@pytest.mark.parametrize("value,mode", [(v, "observe") for v in OBSERVE_VALUES] + [(v, "enforce") for v in ENFORCE_VALUES])
def test_mode_reader_active_values(monkeypatch, value, mode):
    monkeypatch.setenv(FLAG, value)
    assert owner_check.order_owner_check_mode() == mode


def test_the_flag_name_and_its_default():
    assert owner_check.ORDER_OWNER_CHECK_ENV == FLAG
    assert (owner_check.MODE_OFF, owner_check.MODE_OBSERVE, owner_check.MODE_ENFORCE) == ("off", "observe", "enforce")


def test_every_off_value_answers_writes_and_reads_exactly_as_the_flag_unset(h, monkeypatch, owner_lines):
    """With the flag off the three routes do what they did before it existed: same answers, same
    writes, same reads, same PSP calls, for every caller and every order."""
    monkeypatch.delenv(FLAG, raising=False)
    baseline = run_matrix(h)
    assert len(baseline) == len(ROUTES) * len(CALLERS) * len(MATRIX_ORDERS)
    for value in OFF_VALUES[1:]:
        monkeypatch.setenv(FLAG, value)
        assert run_matrix(h) == baseline, repr(value)
    assert owner_lines == []
    # Off is not a no-op harness: the same matrix differs once the check is enforced.
    monkeypatch.setenv(FLAG, "enforce")
    assert run_matrix(h) != baseline


def test_flag_off_makes_no_read_of_the_orders_owner(h, monkeypatch):
    res = call(h, "payment_status", "scoped", O_OWNER)
    assert res.status_code == 200
    assert len(h["reads"]) == 1 and "FROM payments p JOIN orders o" in h["reads"][0]["sql"]
    assert h["reads"][0]["sql"] == (
        "SELECT p.*, o.merchant_id FROM payments p JOIN orders o ON p.order_id = o.order_id "
        "WHERE p.payment_id = :payment_id"
    )


def test_observe_serves_exactly_what_off_serves(h, monkeypatch, owner_lines):
    monkeypatch.delenv(FLAG, raising=False)
    off = run_matrix(h)
    monkeypatch.setenv(FLAG, "observe")
    observed = run_matrix(h)
    # The only difference observe may make: the status route reads the order's owner.
    for before, after in zip(off, observed):
        owner_reads = [r for r in after["reads"] if r["sql"].startswith("SELECT agent_id, metadata FROM orders")]
        after = {**after, "reads": [r for r in after["reads"] if r not in owner_reads]}
        assert after == before
        assert len(owner_reads) <= 1 and (not owner_reads or after["route"] == "payment_status")
    not_owner_calls = sum(1 for (caller, order_id), rel in RELATION.items() if rel == NOT_OWNER) * len(ROUTES)
    assert len(owner_lines) == not_owner_calls
    assert {r.getMessage().split()[1] for r in owner_lines} == {"outcome=would_refuse"}


def test_the_mode_is_read_on_every_call(h, monkeypatch):
    assert call(h, "payments_create", "scoped", O_OWNER).status_code == 200
    monkeypatch.setenv(FLAG, "enforce")
    assert call(h, "payments_create", "scoped", O_OWNER).status_code == 403
    monkeypatch.setenv(FLAG, "observe")
    assert call(h, "payments_create", "scoped", O_OWNER).status_code == 200
    monkeypatch.delenv(FLAG)
    assert call(h, "payments_create", "scoped", O_OWNER).status_code == 200


# ══ the payment route: nothing of the order reaches a caller that does not own it ══════════════


NOT_OWNERS = [("scoped", O_OWNER), ("unrestricted", O_OWNER), ("internal", O_OWNER), ("owner", O_SCOPED),
              ("owner", O_INTERNAL), ("owner", O_SERVICE), ("owner", O_NULL), ("owner", O_BLANK), ("internal", O_NULL)]


@pytest.mark.parametrize("caller,order_id", NOT_OWNERS)
def test_enforce_the_reuse_path_hands_no_client_secret_or_intent_id_to_a_caller_that_is_not_the_owner(
    h, monkeypatch, caller, order_id
):
    # Off: the reuse path hands this caller the order's payment surface (the defect, as it is).
    served = call(h, "payments_create", caller, order_id)
    assert served.status_code == 200
    assert served.json()["client_secret"] == client_secret_of(order_id)
    assert served.json()["payment_intent_id"] == intent_of(order_id)

    monkeypatch.setenv(FLAG, "enforce")
    _reset(h)
    res = call(h, "payments_create", caller, order_id)
    assert res.status_code == 403
    assert res.json() == OWNER_DENIED
    assert set(res.json()) == {"detail"}
    assert_carries_nothing_of_the_order(h, res, order_id)
    for header, value in res.headers.items():
        assert "made_up" not in value and "secret" not in value.lower(), (header, value)
    # Refused before anything else was read for the order: no store, quote, idempotency or PSP step.
    assert h["reads"] == [] and h["writes"] == [] and h["psp_calls"] == []


@pytest.mark.parametrize("caller,order_id", NOT_OWNERS)
def test_enforce_no_new_payment_is_started_and_the_orders_surface_is_not_overwritten(h, monkeypatch, caller, order_id):
    """An order with no payment surface yet: the route would create one at the PSP and write its
    intent id and client secret over the order's."""
    owner = h["orders"][order_id]["agent_id"]
    h["orders"][order_id] = _order(order_id, agent_id=owner, with_surface=False, payment_status="unpaid")

    off = call(h, "payments_create", caller, order_id)
    assert off.status_code == 200 and off.json()["client_secret"] == NEW_CLIENT_SECRET
    assert [w["write"] for w in h["writes"]] == ["execute", "update_payment_info"]
    assert h["writes"][1]["client_secret"] == NEW_CLIENT_SECRET and len(h["psp_calls"]) == 1

    monkeypatch.setenv(FLAG, "enforce")
    _reset(h)
    res = call(h, "payments_create", caller, order_id)
    assert (res.status_code, res.json()) == (403, OWNER_DENIED)
    assert h["psp_calls"] == [] and h["writes"] == [] and h["reads"] == []
    assert_carries_nothing_of_the_order(h, res, order_id)


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_the_owner_still_starts_a_new_payment_in_every_mode(h, monkeypatch, mode):
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, with_surface=False, payment_status="unpaid")
    res = call(h, "payments_create", "owner", O_OWNER)
    assert res.status_code == 200, res.text
    assert res.json()["client_secret"] == NEW_CLIENT_SECRET and res.json()["currency"] == "GBP"
    assert h["psp_calls"] == [{"amount": "25.0", "currency": "GBP", "order_id": O_OWNER}]
    assert h["writes"][1]["order_id"] == O_OWNER and h["writes"][0]["values"]["agent_id"] == OWNER_AGENT


EARLY_ANSWERS = {
    "paid": (dict(payment_status="paid"), {}, 400, "Order already paid"),
    "amount_drift": ({}, {"expected_amount": 1.0}, 409, "QUOTE_MISMATCH"),
    "currency_drift": ({}, {"currency": "CHF"}, 409, "QUOTE_MISMATCH"),
    "quote_expired": (dict(quote_expires_at=PAST_QUOTE_EXPIRY), {}, 409, "QUOTE_EXPIRED"),
}


@pytest.mark.parametrize("case", sorted(EARLY_ANSWERS))
def test_enforce_the_refusal_comes_before_any_answer_that_describes_the_order(h, monkeypatch, case):
    """Paid or not, its total, its currency, its quote: the route says these before it reaches the
    merchant check. Off, a caller that is not the owner reads them. Enforced, it gets the one
    refusal; the owner and a caller outside the merchant's scope get what they got before."""
    order_kwargs, body, status, marker = EARLY_ANSWERS[case]
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, **order_kwargs)

    def answers():
        return {c: call(h, "payments_create", c, O_OWNER, **body) for c in ("owner", "scoped", "unrestricted", "outside")}

    off = answers()
    for caller, res in off.items():
        if caller == "outside" and case == "quote_expired":
            # The quote is judged after the merchant check, so this caller never reached it.
            assert (res.status_code, res.json()) == (403, MERCHANT_DENIED["payments_create"])
            continue
        assert res.status_code == status and marker in res.text, res.text

    monkeypatch.setenv(FLAG, "enforce")
    on = answers()
    for caller in ("scoped", "unrestricted"):
        assert (on[caller].status_code, on[caller].json()) == (403, OWNER_DENIED)
        assert marker not in on[caller].text
    for caller in ("owner", "outside"):
        assert (on[caller].status_code, on[caller].json()) == (off[caller].status_code, off[caller].json())


def test_enforce_an_unrestricted_caller_is_refused_on_an_order_whose_merchant_cannot_be_resolved(h, monkeypatch):
    row = _order(O_THIRD, agent_id=THIRD_AGENT)
    row["merchant_id"] = None
    row["items"] = []
    h["orders"][O_THIRD] = row
    assert call(h, "payments_create", "unrestricted", O_THIRD).json() == {"detail": "Order merchant could not be resolved"}
    monkeypatch.setenv(FLAG, "enforce")
    assert call(h, "payments_create", "unrestricted", O_THIRD).json() == OWNER_DENIED
    # A caller with a merchant list has no scope over it: the answer it had before.
    assert call(h, "payments_create", "scoped", O_THIRD).json() == {"detail": "Order merchant could not be resolved"}


# ══ the evidence route ═══════════════════════════════════════════════════════


@pytest.mark.parametrize("caller,order_id", NOT_OWNERS)
def test_enforce_evidence_is_not_written_on_an_order_the_caller_does_not_own(h, monkeypatch, caller, order_id):
    assert call(h, "payment_offer_evidence", caller, order_id).status_code == 200
    assert [w["order_id"] for w in h["writes"]] == [order_id], "off: the metadata of an order it does not own is rewritten"

    monkeypatch.setenv(FLAG, "enforce")
    _reset(h)
    res = call(h, "payment_offer_evidence", caller, order_id)
    assert (res.status_code, res.json()) == (403, OWNER_DENIED)
    assert h["writes"] == []
    assert_carries_nothing_of_the_order(h, res, order_id)


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_evidence_for_a_quote_with_no_order_is_not_an_order_and_is_untouched(h, monkeypatch, owner_lines, mode):
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    res = h["client"].post("/agent/v1/orders/payment-offer-evidence", headers={"X-API-Key": SCOPED_KEY},
                           json={"quote_id": "q_fixture", "merchant_id": MERCHANT, "payment_method_evidence": {"psp": "stripe"}})
    assert res.status_code == 200 and res.json()["recorded"] is True and res.json()["order_id"] is None
    assert h["writes"] == [] and owner_lines == []


def test_enforce_naming_ones_own_merchant_does_not_open_another_merchants_order(h, monkeypatch):
    """The evidence route judges the merchant the REQUEST names before the order's. Off, a caller
    scoped to its own merchant writes on any order by naming that merchant. Enforced, it must own it."""
    h["orders"][O_THIRD] = _order(O_THIRD, agent_id=THIRD_AGENT, merchant_id=MERCHANT)
    off = call(h, "payment_offer_evidence", "outside", O_THIRD, merchant_id=OTHER_MERCHANT)
    assert off.status_code == 200 and [w["order_id"] for w in h["writes"]] == [O_THIRD]
    monkeypatch.setenv(FLAG, "enforce")
    _reset(h)
    res = call(h, "payment_offer_evidence", "outside", O_THIRD, merchant_id=OTHER_MERCHANT)
    assert (res.status_code, res.json()) == (403, OWNER_DENIED) and h["writes"] == []


# ══ the status route ═════════════════════════════════════════════════════════


@pytest.mark.parametrize("caller,order_id", NOT_OWNERS)
def test_enforce_a_payment_is_not_read_by_a_caller_that_does_not_own_its_order(h, monkeypatch, caller, order_id):
    off = call(h, "payment_status", caller, order_id)
    assert off.status_code == 200 and off.json()["payment"]["amount"] == 25.0
    monkeypatch.setenv(FLAG, "enforce")
    res = call(h, "payment_status", caller, order_id)
    assert (res.status_code, res.json()) == (403, OWNER_DENIED)
    assert_carries_nothing_of_the_order(h, res, order_id)


def test_enforce_the_agent_that_started_a_payment_on_anothers_order_cannot_read_it_back(h, monkeypatch):
    """The owner judged is the ORDER's (orders.agent_id), not the payment row's agent."""
    monkeypatch.setenv(FLAG, "enforce")
    assert _payment_row(h["orders"][O_OWNER])["agent_id"] == SCOPED_AGENT
    assert call(h, "payment_status", "scoped", O_OWNER).json() == OWNER_DENIED
    assert call(h, "payment_status", "owner", O_OWNER).status_code == 200
    owner_read = h["reads"][-1]
    assert owner_read == {"sql": "SELECT agent_id, metadata FROM orders WHERE order_id = :order_id", "values": {"order_id": O_OWNER}}


def _break_the_owner_read(h, monkeypatch) -> None:
    served_read = shared_database.fetch_one

    async def fetch_one(query, values=None):
        if _sql(query).startswith("SELECT agent_id, metadata FROM orders"):
            raise RuntimeError(f"connection lost while reading {O_OWNER}")
        return await served_read(query, values)

    monkeypatch.setattr(shared_database, "fetch_one", fetch_one)


def test_observe_serves_the_status_route_when_the_owner_cannot_be_read(h, monkeypatch, owner_lines):
    """`observe` refuses nothing, also when its own extra read fails."""
    _break_the_owner_read(h, monkeypatch)
    monkeypatch.setenv(FLAG, "observe")
    for caller in ("owner", "scoped"):
        res = call(h, "payment_status", caller, O_OWNER)
        assert res.status_code == 200 and res.json()["payment"]["order_id"] == O_OWNER
    assert [r.getMessage() for r in owner_lines] == [
        "order_owner_check outcome=owner_read_failed mode=observe route=payment_status error=RuntimeError"
    ] * 2
    assert all(r.levelno == logging.WARNING for r in owner_lines)


def test_enforce_serves_nobody_on_the_status_route_when_the_owner_cannot_be_read(h, monkeypatch, owner_lines):
    _break_the_owner_read(h, monkeypatch)
    monkeypatch.setenv(FLAG, "enforce")
    for caller in ("owner", "scoped"):
        res = call(h, "payment_status", caller, O_OWNER)
        assert (res.status_code, res.json()) == (500, {"detail": "Failed to get payment status"})
    assert [r.getMessage() for r in owner_lines] == [
        "order_owner_check outcome=owner_read_failed mode=enforce route=payment_status error=RuntimeError"
    ] * 2
    monkeypatch.delenv(FLAG)
    assert call(h, "payment_status", "scoped", O_OWNER).status_code == 200, "off never makes that read"


def test_a_payment_whose_order_row_is_gone_has_no_owner(monkeypatch, owner_lines):
    import asyncio

    async def fetch_one(query, values=None):
        return None

    monkeypatch.setattr(shared_database, "fetch_one", fetch_one)
    asyncio.run(owner_check.check_payment_order_owner("ORD_fixture_gone", _ctx(OWNER_AGENT), mode="off"))
    asyncio.run(owner_check.check_payment_order_owner("ORD_fixture_gone", _ctx(OWNER_AGENT), mode="observe"))
    with pytest.raises(owner_check.HTTPException) as exc:
        asyncio.run(owner_check.check_payment_order_owner("ORD_fixture_gone", _ctx(OWNER_AGENT), mode="enforce"))
    assert (exc.value.status_code, exc.value.detail) == (403, "Not authorized for this order")
    assert [_fields(r)["owner_kind"] for r in owner_lines] == ["none", "none"]


def test_enforce_the_status_route_checks_scope_before_it_reads_the_owner(h, monkeypatch):
    monkeypatch.setenv(FLAG, "enforce")
    res = call(h, "payment_status", "outside", O_OWNER)
    assert (res.status_code, res.json()) == (403, {"detail": "Not authorized"})
    assert len(h["reads"]) == 1, "a caller outside the scope costs no extra read and learns nothing new"


# ══ markets and currencies ═══════════════════════════════════════════════════


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("market", MARKETS)
def test_the_owner_is_answered_in_the_currency_of_the_orders_market(h, monkeypatch, market, mode):
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    currency = market_currency(market)
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, market=market)

    reuse = call(h, "payments_create", "owner", O_OWNER, currency=currency, expected_amount=25.0)
    assert reuse.status_code == 200 and reuse.json()["currency"] == currency and reuse.json()["amount"] == 25.0
    status = call(h, "payment_status", "owner", O_OWNER)
    assert status.status_code == 200 and status.json()["payment"]["currency"] == currency
    evidence = call(h, "payment_offer_evidence", "owner", O_OWNER)
    assert evidence.status_code == 200

    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, market=market, with_surface=False, payment_status="unpaid")
    _reset(h)
    fresh = call(h, "payments_create", "owner", O_OWNER, currency=currency)
    assert fresh.status_code == 200 and fresh.json()["currency"] == currency
    assert h["psp_calls"] == [{"amount": "25.0", "currency": currency, "order_id": O_OWNER}]
    assert h["writes"][0]["values"]["currency"] == currency

    # Another market's currency on the request is refused by name, never relabelled.
    other = "USD" if currency != "USD" else "GBP"
    drift = call(h, "payments_create", "owner", O_OWNER, currency=other)
    assert drift.status_code == 409
    assert drift.json()["detail"]["expected_currency"] == other and drift.json()["detail"]["order_currency"] == currency


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("market", MARKETS)
def test_enforce_the_refusal_is_the_same_in_every_market_and_names_no_currency(h, monkeypatch, market, route):
    monkeypatch.setenv(FLAG, "enforce")
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, market=market)
    res = call(h, route, "scoped", O_OWNER)
    assert (res.status_code, res.json()) == (403, OWNER_DENIED)
    for code in ("USD", "SGD", "GBP", "EUR", "JPY"):
        assert code not in res.text


# ══ the rule itself ══════════════════════════════════════════════════════════


def _ctx(agent_id: Any, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, **extra)


@pytest.mark.parametrize("owner,caller,owns", [
    (OWNER_AGENT, OWNER_AGENT, True),
    (OWNER_AGENT, SCOPED_AGENT, False),
    (OWNER_AGENT, OWNER_AGENT.upper(), False),
    (OWNER_AGENT, OWNER_AGENT + " ", False),
    (MERCHANT, MERCHANT, True),          # an id is an id; the next case is the one that matters
    (None, OWNER_AGENT, False),
    ("", OWNER_AGENT, False),
    ("   ", OWNER_AGENT, False),
    (None, None, False),
    ("", "", False),
    (None, "", False),
    ("  ", "  ", False),
    (INTERNAL_AGENT, INTERNAL_AGENT, True),
    (INTERNAL_AGENT, "agent_internal_trusted_0000000000000000", False),
    (INTERNAL_AGENT, OWNER_AGENT, False),
    (OWNER_AGENT, INTERNAL_AGENT, False),
])
def test_caller_owns_order(monkeypatch, owner, caller, owns):
    monkeypatch.delenv(PARTNER_FORWARDING_FLAG, raising=False)
    assert owner_check.caller_owns_order({"agent_id": owner, "merchant_id": MERCHANT}, _ctx(caller)) is owns


def test_the_owner_is_never_compared_with_the_merchant(h, monkeypatch):
    """An agent whose id equals the order's merchant id does not own that merchant's orders."""
    monkeypatch.setenv(FLAG, "enforce")
    monkeypatch.setitem(AGENTS_BY_KEY, SCOPED_KEY, _agent(MERCHANT, [MERCHANT]))
    for route in ROUTES:
        res = call(h, route, "scoped", O_OWNER)
        assert (res.status_code, res.json()) == (403, OWNER_DENIED), route


def test_the_rule_is_the_one_the_other_routes_use(monkeypatch):
    seen = []

    def spy(order, context):
        seen.append((order.get("agent_id"), context.agent_id))
        return False

    monkeypatch.setattr(owner_check, "order_access_allowed", spy)
    assert owner_check.caller_owns_order({"agent_id": OWNER_AGENT}, _ctx(OWNER_AGENT)) is False
    assert seen == [(OWNER_AGENT, OWNER_AGENT)]
    assert owner_check.order_access_allowed is spy and forwarded_owner.order_access_allowed is not spy
    assert agent_v2.order_access_allowed is forwarded_owner.order_access_allowed


@pytest.mark.parametrize("route", ROUTES)
def test_the_vouching_service_keeps_the_orders_made_through_it_and_only_with_its_own_flag(h, monkeypatch, route):
    """What the shared rule adds to "the creating agent": with PARTNER_IDENTITY_FORWARDING_ENABLED
    on, the service that vouched for the owner is served on an order stamped as made through it."""
    monkeypatch.setenv(FLAG, "enforce")
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", VOUCHER_AGENT)
    stamp = {forwarded_owner.PARTNER_IDENTITY_METADATA_KEY: {"partner_agent_id": OWNER_AGENT, "via_agent_id": VOUCHER_AGENT, "op": "create_order"}}
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, metadata=stamp)
    voucher = {"X-API-Key": VOUCHER_KEY}

    assert call(h, route, "voucher", O_OWNER, headers=voucher).json() == OWNER_DENIED          # its flag is off
    monkeypatch.setenv(PARTNER_FORWARDING_FLAG, "1")
    assert call(h, route, "voucher", O_OWNER, headers=voucher).status_code == 200
    assert call(h, route, "voucher", O_SCOPED, headers=voucher).json() == OWNER_DENIED         # not stamped
    assert call(h, route, "scoped", O_OWNER).json() == OWNER_DENIED                            # not the voucher
    wrong = {forwarded_owner.PARTNER_IDENTITY_METADATA_KEY: {"partner_agent_id": SCOPED_AGENT, "via_agent_id": VOUCHER_AGENT}}
    h["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, metadata=wrong)
    assert call(h, route, "voucher", O_OWNER, headers=voucher).json() == OWNER_DENIED          # stamp names another owner


def test_check_order_owner_refuses_nothing_outside_enforce_and_rejects_an_unknown_route(owner_lines):
    order, stranger = {"agent_id": OWNER_AGENT}, _ctx(SCOPED_AGENT)
    for mode in ("off", "", "ENFORCE", "enforced", None):
        owner_check.check_order_owner(order, stranger, route="payments_create", mode=mode)
    assert owner_lines == []
    owner_check.check_order_owner(order, stranger, route="payments_create", mode="observe")
    assert len(owner_lines) == 1
    with pytest.raises(owner_check.HTTPException) as exc:
        owner_check.check_order_owner(order, stranger, route="payments_create", mode="enforce")
    assert (exc.value.status_code, exc.value.detail) == (403, "Not authorized for this order")
    with pytest.raises(ValueError):
        owner_check.check_order_owner(order, stranger, route="some_other_route", mode="enforce")


def test_the_refusal_is_the_one_the_routes_that_already_check_the_owner_give(h, monkeypatch):
    monkeypatch.setenv(FLAG, "enforce")
    ours = call(h, "payments_create", "scoped", O_OWNER)
    theirs = h["client"].post("/agent/v2/payments/checkout-sessions", headers={"X-API-Key": SCOPED_KEY},
                              json={"order_id": O_OWNER, "customer_email": "buyer@example.com"})
    assert (ours.status_code, ours.json()) == (theirs.status_code, theirs.json()) == (403, OWNER_DENIED)


# ══ the log line ═════════════════════════════════════════════════════════════


LINE_FIELDS = ["outcome", "mode", "route", "caller_kind", "owner_kind", "quote", "caller_agent_id"]
CLOSED_LABELS = {
    "outcome": {"would_refuse", "refused"},
    "mode": {"observe", "enforce"},
    "route": set(ROUTES),
    "caller_kind": {"agent", "internal_trusted", "service_agent", "checkout_token", "none"},
    "owner_kind": {"agent", "internal_trusted", "service_agent", "none"},
    "quote": {"live", "expired", "none"},
}


def _fields(record: logging.LogRecord) -> Dict[str, str]:
    name, *pairs = record.getMessage().split(" ")
    assert name == "order_owner_check"
    assert [p.split("=", 1)[0] for p in pairs] == LINE_FIELDS
    return dict(p.split("=", 1) for p in pairs)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_the_line_has_closed_labels_the_callers_own_id_and_nothing_of_the_order(h, monkeypatch, owner_lines, mode):
    monkeypatch.setenv(FLAG, mode)
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", SERVICE_AGENT)
    h["orders"][O_THIRD] = _order(O_THIRD, agent_id=THIRD_AGENT, quote_expires_at=PAST_QUOTE_EXPIRY)
    h["orders"][O_SCOPED] = _order(O_SCOPED, agent_id=SCOPED_AGENT, quote_expires_at=None)
    seen = set()
    for route in ROUTES:
        for caller, order_id in NOT_OWNERS + [("owner", O_THIRD), ("unrestricted", O_SERVICE)]:
            owner_lines.clear()
            call(h, route, caller, order_id)
            assert len(owner_lines) == 1
            fields = _fields(owner_lines[0])
            for name, allowed in CLOSED_LABELS.items():
                assert fields[name] in allowed, (name, fields)
            assert fields["caller_agent_id"] == CALLER_AGENTS[caller]
            line = owner_lines[0].getMessage()
            owner = str(h["orders"][order_id]["agent_id"] or "").strip()
            for never in (order_id, client_secret_of(order_id), intent_of(order_id), payment_id_of(order_id), "ak_live_",
                          INTERNAL_KEY, "made_up", MERCHANT):
                assert never not in line, (never, line)
            if owner and owner != CALLER_AGENTS[caller]:
                assert owner not in line
            seen.add((fields["caller_kind"], fields["owner_kind"], fields["quote"]))
    assert {("agent", "agent", "live"), ("agent", "internal_trusted", "live"), ("agent", "service_agent", "live"),
            ("agent", "none", "live"), ("internal_trusted", "agent", "live"), ("internal_trusted", "none", "live"),
            ("agent", "agent", "expired"), ("agent", "agent", "none")} <= seen


def test_the_outcomes_are_counted(h, monkeypatch):
    monkeypatch.setattr(owner_check, "ORDER_OWNER_CHECK_OUTCOMES", owner_check.Counter())
    monkeypatch.setenv(FLAG, "observe")
    call(h, "payments_create", "scoped", O_OWNER)
    call(h, "payments_create", "scoped", O_OWNER)
    monkeypatch.setenv(FLAG, "enforce")
    call(h, "payment_status", "owner", O_INTERNAL)
    call(h, "payment_status", "owner", O_OWNER)
    assert dict(owner_check.ORDER_OWNER_CHECK_OUTCOMES) == {
        ("would_refuse", "payments_create", "agent", "agent", "live"): 2,
        ("refused", "payment_status", "agent", "internal_trusted", "live"): 1,
    }


def test_the_line_is_a_warning_on_the_service_logger():
    """Plain module loggers lose INFO in a deployed service; this line must not be one of those."""
    assert owner_check.logger is pivota_logger and pivota_logger.name == "pivota"
    source = inspect.getsource(owner_check.check_order_owner)
    assert "logger.warning(" in source and "logger.info(" not in source


@pytest.mark.parametrize("raw,label", [
    (LIVE_QUOTE_EXPIRY, "live"), (PAST_QUOTE_EXPIRY, "expired"), ("2099-01-01T00:00:00", "live"),
    ("2001-01-01T00:00:00+00:00", "expired"), (None, "none"), ("", "none"), ("not a date", "none"), (12345, "none"),
])
def test_the_quote_label(raw, label):
    assert owner_check._quote_label({"metadata": {"pricing_quote": {"expires_at": raw}}}) == label
    assert owner_check._quote_label({"metadata": json.dumps({"pricing_quote": {"expires_at": raw}})}) == label


def test_the_quote_label_of_an_order_without_a_quote():
    for order in ({}, {"metadata": None}, {"metadata": "not json"}, {"metadata": {"pricing_quote": "x"}}, {"metadata": []}):
        assert owner_check._quote_label(order) == "none"


# ══ where the check sits in each route ═══════════════════════════════════════


def _function_source(module, name: str) -> str:
    return textwrap.dedent(inspect.getsource(getattr(module, name)))


def _owner_check_calls(source: str) -> List[ast.Call]:
    return [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id in ("check_order_owner", "check_payment_order_owner")]


@pytest.mark.parametrize("module,name,function,label", [
    (payments, "create_payment", "check_order_owner", "ROUTE_PAYMENTS_CREATE"),
    (agent_api, "agent_record_payment_offer_evidence", "check_order_owner", "ROUTE_PAYMENT_OFFER_EVIDENCE"),
    (payments, "get_payment_status", "check_payment_order_owner", None),
    (owner_check, "check_payment_order_owner", "check_order_owner", "ROUTE_PAYMENT_STATUS"),
])
def test_each_route_calls_the_check_once_with_its_own_label_and_the_mode_it_read(module, name, function, label):
    calls = _owner_check_calls(_function_source(module, name))
    assert [c.func.id for c in calls] == [function]
    keywords = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    mode = "mode" if module is owner_check else "owner_check_mode"
    assert keywords == ({"route": label, "mode": mode} if label else {"mode": mode})
    if label:
        assert getattr(owner_check, label) in ROUTES


def test_the_payment_route_checks_the_owner_before_it_looks_at_the_order():
    source = _function_source(payments, "create_payment")
    check = source.index("check_order_owner(")
    for later in ('order.get("payment_status") == "paid"', "is_cancelled_order(order)", "QUOTE_MISMATCH",
                  "get_primary_store(", "_ensure_agent_payment_order_has_live_quote(order)", "idempotency_key = :key",
                  "_build_existing_order_payment_surface(", "create_payment_with_failover(", "update_payment_info("):
        assert check < source.index(later), later
    for earlier in ("ensure_checkout_token_order_access(context, order)", 'detail="Order not found"'):
        assert source.index(earlier) < check, earlier


def test_the_evidence_route_checks_the_owner_after_the_merchant_and_before_the_write():
    source = _function_source(agent_api, "agent_record_payment_offer_evidence")
    check = source.index("check_order_owner(")
    assert source.index("Not authorized for this merchant") < check < source.index("update_order(")
    assert source.index("ensure_checkout_token_order_access(context, order)") < check


def test_the_status_route_checks_the_owner_after_the_merchant_and_before_the_answer():
    source = _function_source(payments, "get_payment_status")
    check = source.index("check_payment_order_owner(")
    assert source.index('detail="Not authorized"') < check < source.index('"status": "success"')


def test_only_these_three_routes_use_the_check():
    import pathlib

    root = pathlib.Path(owner_check.__file__).resolve().parent.parent
    users = sorted(
        str(path.relative_to(root)) for folder in ("routes", "services", "middleware", "db", "utils")
        for path in (root / folder).rglob("*.py")
        if "_order_owner(" in path.read_text(encoding="utf-8") and path.name != "agent_order_owner_check.py"
    )
    assert users == ["routes/agent_api.py", "routes/agent_payment_sdk.py"]


# ══ through POST /agent/shop/v1/invoke ════════════════════════════════════════
#
# The proxy sends a money operation upstream with THIS service's key unless the caller's own key is
# forwarded (SHOP_INVOKE_FORWARD_CALLER_KEY). So an order created through /invoke before that
# switch is owned by this service's agent, and a call through /invoke before the switch arrives as
# this service's agent.
#
# The rule for those orders is the owner rule and nothing added to it:
#   * while the proxy sends this service's key, the caller is this service's agent, which owns
#     them: they stay payable through /invoke;
#   * once the caller's own key is forwarded, the caller is not their owner and is refused, exactly
#     as the payment link for such an order already is. They are payable for the life of their
#     quote only, so the owner check is to be enforced after the forwarding switch has been on for
#     longer than that; `observe` shows the refusals that are left (owner_kind, quote=live).

INVOKE_ROUTE_OF = {"submit_payment": "payments_create", "record_payment_offer_evidence": "payment_offer_evidence"}
INVOKE_OPERATIONS = sorted(INVOKE_ROUTE_OF)


def test_these_are_the_invoke_operations_that_reach_the_checked_routes():
    assert set(INVOKE_OPERATIONS) < set(gw.INVOKE_MONEY_OPERATIONS)
    assert set(gw.INVOKE_MONEY_OPERATIONS) - set(INVOKE_OPERATIONS) == {"create_order", "create_payment_link"}


def invoke(h, operation: str, order_id: str, headers: Dict[str, str], *, market: str = "GB"):
    if operation == "submit_payment":
        payload = {"payment": {"order_id": order_id, "expected_amount": 25.0, "currency": market_currency(market),
                               "payment_method": "card"}}
    else:
        payload = {"order_id": order_id, "selected_payment_offer_id": "offer_fixture",
                   "payment_method_evidence": {"psp": "stripe", "card_network": "fixturecard"}}
    return h["client"].post(INVOKE, json={"operation": operation, "payload": payload}, headers=headers)


def assert_invoke_served(h, operation: str, order_id: str, res) -> None:
    assert res.status_code == 200, res.text
    if operation == "submit_payment":
        assert client_secret_of(order_id) in res.text
        assert h["orders"][order_id]["currency"] in res.text
    else:
        assert [w["order_id"] for w in h["writes"]] == [order_id]


def assert_invoke_refused(h, order_id: str, res) -> None:
    assert res.status_code == 403, res.text
    assert res.json() == {"detail": OWNER_DENIED}
    assert h["writes"] == [] and h["psp_calls"] == []
    assert_carries_nothing_of_the_order(h, res, order_id)


@pytest.fixture(params=["service_key_is_an_agent_key", "service_key_is_an_internal_trusted_key"])
def hi(request, monkeypatch):
    """The harness twice: this service's key as an ordinary agent's key, and as an internal trusted key."""
    trusted = request.param == "service_key_is_an_internal_trusted_key"
    state = build_harness(monkeypatch, service_key_is_internal_trusted=trusted)
    service_agent = agent_auth._build_internal_trusted_agent(SERVICE_KEY)["agent_id"] if trusted else SERVICE_AGENT
    state["service_agent"] = service_agent
    # Created through /invoke before the forwarding switch: owned by this service's agent.
    state["orders"][O_SERVICE] = _order(O_SERVICE, agent_id=service_agent)
    return state


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_forwarding_off_an_order_made_through_invoke_stays_payable_through_invoke(hi, monkeypatch, owner_lines, operation, mode):
    _set(monkeypatch, FLAG, None if mode == "off" else mode)
    for key in (OWNER_KEY, SCOPED_KEY):
        _reset(hi)
        res = invoke(hi, operation, O_SERVICE, {"X-API-Key": key})
        assert_invoke_served(hi, operation, O_SERVICE, res)
        assert hi["upstream"][0]["headers"]["X-API-Key"] == SERVICE_KEY
    assert owner_lines == [], "the caller upstream is this service's agent, the order's owner"


@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_forwarding_on_the_callers_own_order_is_served_and_another_callers_is_not(hi, monkeypatch, operation):
    monkeypatch.setenv(FORWARD_FLAG, "1")
    monkeypatch.setenv(FLAG, "enforce")
    res = invoke(hi, operation, O_OWNER, {"X-API-Key": OWNER_KEY})
    assert_invoke_served(hi, operation, O_OWNER, res)
    assert hi["upstream"][0]["headers"]["X-API-Key"] == OWNER_KEY
    _reset(hi)
    assert_invoke_refused(hi, O_OWNER, invoke(hi, operation, O_OWNER, {"X-API-Key": SCOPED_KEY}))
    assert_invoke_refused(hi, O_OWNER, invoke(hi, operation, O_OWNER, {"Authorization": f"Bearer {UNRESTRICTED_KEY}"}))


@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_forwarding_on_an_order_made_before_the_switch_is_refused_once_enforced_and_counted_while_observed(
    hi, monkeypatch, owner_lines, operation
):
    """The pre-switch order, the same caller, now arriving with its own key."""
    # Before the switch, enforced or not: served (previous test). After the switch, not enforced:
    monkeypatch.setenv(FORWARD_FLAG, "1")
    res = invoke(hi, operation, O_SERVICE, {"X-API-Key": OWNER_KEY})
    assert_invoke_served(hi, operation, O_SERVICE, res)
    assert owner_lines == []

    # Observed: still served, and the line says it is an in-flight order (quote=live).
    monkeypatch.setenv(FLAG, "observe")
    _reset(hi)
    res = invoke(hi, operation, O_SERVICE, {"X-API-Key": OWNER_KEY})
    assert_invoke_served(hi, operation, O_SERVICE, res)
    fields = _fields(owner_lines[0])
    assert len(owner_lines) == 1 and fields["outcome"] == "would_refuse" and fields["quote"] == "live"
    assert fields["route"] == INVOKE_ROUTE_OF[operation] and fields["caller_agent_id"] == OWNER_AGENT
    assert fields["owner_kind"] == ("internal_trusted" if hi["service_agent"].startswith("agent_internal_trusted_") else "agent")

    # Past its quote the order cannot be paid by anyone; the line then says quote=expired.
    hi["orders"][O_SERVICE] = _order(O_SERVICE, agent_id=hi["service_agent"], quote_expires_at=PAST_QUOTE_EXPIRY)
    owner_lines.clear()
    invoke(hi, operation, O_SERVICE, {"X-API-Key": OWNER_KEY})
    assert _fields(owner_lines[0])["quote"] == "expired"
    hi["orders"][O_SERVICE] = _order(O_SERVICE, agent_id=hi["service_agent"])

    # Enforced: refused, with the answer the payment link for such an order already gets.
    monkeypatch.setenv(FLAG, "enforce")
    _reset(hi)
    res = invoke(hi, operation, O_SERVICE, {"X-API-Key": OWNER_KEY})
    assert_invoke_refused(hi, O_SERVICE, res)
    link = hi["client"].post(INVOKE, headers={"X-API-Key": OWNER_KEY},
                             json={"operation": "create_payment_link", "payload": {"order_id": O_SERVICE, "customer_email": "buyer@example.com"}})
    assert (link.status_code, link.json()) == (res.status_code, res.json())


def test_an_order_past_its_quote_is_payable_by_nobody_so_the_wait_is_the_quotes_life(hi, monkeypatch):
    """Why waiting out the quote is enough: the payment route refuses the OWNER too once the
    order's quote has expired, before it reuses or creates a payment surface."""
    hi["orders"][O_SERVICE] = _order(O_SERVICE, agent_id=hi["service_agent"], quote_expires_at=PAST_QUOTE_EXPIRY)
    for flag in (None, "1"):
        _set(monkeypatch, FORWARD_FLAG, flag)
        res = invoke(hi, "submit_payment", O_SERVICE, {"X-API-Key": OWNER_KEY})
        assert res.status_code == 409 and res.json()["detail"]["detail"]["error"] == "QUOTE_EXPIRED"
        assert client_secret_of(O_SERVICE) not in res.text
    from services.quote_service import QuoteService

    monkeypatch.delenv("QUOTE_TTL_SECONDS", raising=False)
    assert QuoteService().ttl_seconds == 600


@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_forwarding_off_and_enforced_an_order_made_with_the_callers_own_key_is_refused_through_invoke(hi, monkeypatch, operation):
    """The other mix, and why the forwarding switch goes first: the order was created on the keyed
    API with the caller's own key, and /invoke still arrives as this service's agent."""
    monkeypatch.setenv(FLAG, "enforce")
    assert_invoke_refused(hi, O_OWNER, invoke(hi, operation, O_OWNER, {"X-API-Key": OWNER_KEY}))
    monkeypatch.setenv(FORWARD_FLAG, "1")
    assert_invoke_served(hi, operation, O_OWNER, invoke(hi, operation, O_OWNER, {"X-API-Key": OWNER_KEY}))


@pytest.mark.parametrize("forward", [None, "1"], ids=["forwarding_off", "forwarding_on"])
@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_enforced_a_keyless_call_with_the_money_gate_off_is_this_services_agent(hi, monkeypatch, operation, forward):
    monkeypatch.setenv(FLAG, "enforce")
    monkeypatch.setenv(MONEY_GATE, "0")
    _set(monkeypatch, FORWARD_FLAG, forward)
    assert_invoke_served(hi, operation, O_SERVICE, invoke(hi, operation, O_SERVICE, {}))
    _reset(hi)
    assert_invoke_refused(hi, O_OWNER, invoke(hi, operation, O_OWNER, {}))


@pytest.mark.parametrize("forward", [None, "1"], ids=["forwarding_off", "forwarding_on"])
@pytest.mark.parametrize("operation", INVOKE_OPERATIONS)
def test_enforced_a_checkout_token_through_invoke_reaches_its_own_order_only(hi, monkeypatch, operation, forward):
    monkeypatch.setenv(FLAG, "enforce")
    _set(monkeypatch, FORWARD_FLAG, forward)
    res = invoke(hi, operation, O_OWNER, {"X-Checkout-Token": mint_token(order_id=O_OWNER), "X-API-Key": SCOPED_KEY})
    assert_invoke_served(hi, operation, O_OWNER, res)
    _reset(hi)
    other = invoke(hi, operation, O_SCOPED, {"X-Checkout-Token": mint_token(order_id=O_OWNER)})
    assert other.status_code == 403 and other.json() == {"detail": TOKEN_ORDER_DENIED}
    assert hi["writes"] == []


@pytest.mark.parametrize("market", MARKETS)
def test_through_invoke_the_owner_pays_in_the_currency_of_the_orders_market(hi, monkeypatch, market):
    monkeypatch.setenv(FORWARD_FLAG, "1")
    monkeypatch.setenv(FLAG, "enforce")
    currency = market_currency(market)
    hi["orders"][O_OWNER] = _order(O_OWNER, agent_id=OWNER_AGENT, market=market)
    res = invoke(hi, "submit_payment", O_OWNER, {"X-API-Key": OWNER_KEY}, market=market)
    assert res.status_code == 200 and f'"currency":"{currency}"' in res.text.replace(" ", "")
    other_market = "US" if currency != "USD" else "GB"
    drift = invoke(hi, "submit_payment", O_OWNER, {"X-API-Key": OWNER_KEY}, market=other_market)
    assert drift.status_code == 409 and drift.json()["detail"]["detail"]["order_currency"] == currency
    refused = invoke(hi, "submit_payment", O_OWNER, {"X-API-Key": SCOPED_KEY}, market=market)
    assert refused.json() == {"detail": OWNER_DENIED} and currency not in refused.text


