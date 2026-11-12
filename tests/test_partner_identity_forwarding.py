"""Partner identity on MCP money calls (FIX_PLAN O-04), through the real routes.

The gateway's commerce kernel calls these routes with its OWN service key, so before this every
MCP order, quote, hosted link and refund belonged to the gateway's agent. With
PARTNER_IDENTITY_FORWARDING_ENABLED on, a REQUEST-BOUND (v2) X-Pivota-Issuing-Agent from the
gateway makes the partner it names the route's AgentContext: the owner of what is written, and the
one whose scope, rate limit and quota apply. The token is bound to the method, path, resource id
and exact body bytes, lives 60 s, and is single use; a v2 header that fails any check refuses the
request (409), and a v1 token is never adopted on these routes.

Driven through the real app: the real get_agent_context (API key auth), the real
forwarded_owner_context dependency, the real verifier (services/bound_issuing_assertion.py), the
real nonce store (services/assertion_replay_guard.py, its in-process branch: no REDIS_URL here) and
the real route bodies. Stubbed: the key/agent lookups, the rate/quota counters, and the layer below
each route (agent_v1_create_order, which writes `context.agent_id` as orders.agent_id, the order
read, the checkout-intent mint, the refund executor).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import db.agents as db_agents
import routes.agent_auth as agent_auth
import routes.agent_forwarded_owner as forwarded
import routes.agent_shop_gateway as gw
import routes.agent_v2 as agent_v2
import services.issuing_agent_assertion as ia
from main import app
from services import assertion_replay_guard
from services.bound_issuing_assertion import sign_bound_issuing_agent_assertion
from services.commerce_attribution_service import PVT_CLICK_ID
from services.issuing_agent_assertion import sign_issuing_agent_assertion
from services.outbound_links_service import parse_and_verify_redirect_token

#: The REAL db.agents.get_agent, captured before any fixture stubs it (the P2-1 test reads through it).
REAL_DB_GET_AGENT = db_agents.get_agent

SECRET = "test_partner_identity_secret"
FLAG = "PARTNER_IDENTITY_FORWARDING_ENABLED"

GATEWAY_KEY = "ak_live_" + "0a" * 32
PARTNER_A_KEY = "ak_live_" + "1a" * 32
PARTNER_B_KEY = "ak_live_" + "2b" * 32
GATEWAY_AGENT = "agent_gateway_test"
PARTNER_A = "agent_partner_a"
PARTNER_B = "agent_partner_b"
INACTIVE = "agent_partner_inactive"

#: The gateway's PARTNER_IDENTITY_OPS (PIVOTA-Agent src/attribution/issuingAgentAssertion.js).
PARTNER_IDENTITY_OPS = (
    "preview_quote", "create_order", "submit_payment", "create_payment_link", "request_after_sales",
    "get_order_status", "external_seed_links.mint",
)

AGENTS: Dict[str, Dict[str, Any]] = {
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "agent_name": "Gateway", "is_active": True, "allowed_merchants": None},
    PARTNER_A: {"agent_id": PARTNER_A, "agent_name": "Partner A", "is_active": True,
                "allowed_merchants": ["m_shop_a"], "rate_limit": 60, "daily_quota": 1000},
    PARTNER_B: {"agent_id": PARTNER_B, "agent_name": "Partner B", "is_active": True,
                "allowed_merchants": ["m_shop_a"], "rate_limit": 60, "daily_quota": 1000},
    INACTIVE: {"agent_id": INACTIVE, "agent_name": "Off", "is_active": False},
}
KEYS = {GATEWAY_KEY: GATEWAY_AGENT, PARTNER_A_KEY: PARTNER_A, PARTNER_B_KEY: PARTNER_B}


def raw_json(body: Optional[Dict[str, Any]]) -> bytes:
    """The exact bytes sent: the gateway hashes the string it sends, so the tests do the same."""
    return b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")


def bound(sub: str, op: str, *, method: str, path: str, rid: Optional[str], raw: bytes,
          secret: str = SECRET, ts: Optional[int] = None, jti: Optional[str] = None, **override: Any) -> str:
    payload: Dict[str, Any] = {
        "v": 2, "kind": "agent", "sub": sub, "op": op, "ts": int(time.time()) if ts is None else ts,
        "jti": jti or secrets.token_urlsafe(18), "m": method, "p": path, "rid": rid,
        "bh": hashlib.sha256(raw).hexdigest(),
    }
    payload.update(override)
    return sign_bound_issuing_agent_assertion(payload, secret)


def v1(sub: str, op: str) -> str:
    return sign_issuing_agent_assertion({"v": 1, "kind": "agent", "sub": sub, "op": op, "ts": int(time.time())}, SECRET)


def _would_own(key: str, assertion: Optional[str]) -> str:
    """Who an order create made with this key and header is FOR, as the adoption rules decide it."""
    if key == GATEWAY_KEY and assertion and assertion.startswith("v2.") and os.getenv(FLAG):
        try:
            return json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "==")).get("sub") or GATEWAY_AGENT
        except ValueError:
            return GATEWAY_AGENT
    return KEYS.get(key, "")


def send(world, method: str, path: str, key: str, body: Optional[Dict[str, Any]] = None,
         assertion: Optional[str] = None):
    if world.get("auto_own_quote", True) and method == "POST" and path == ORDERS and body and body.get("quote_id"):
        # be#2770 (S-28): a quote is usable only by the agent that previewed it. By default each order
        # create stands for a quote its own owner previewed; the S-28 composition tests below turn this
        # off and preview through the real route instead.
        world["quote_owners"][body["quote_id"]] = _would_own(key, assertion)
    headers = {"X-API-Key": key}
    if assertion is not None:
        headers["X-Pivota-Issuing-Agent"] = assertion
    raw = raw_json(body)
    if raw:
        headers["Content-Type"] = "application/json"
    return world["client"].request(method, path, content=raw or None, headers=headers)


def as_partner(world, sub: str, op: str, method: str, path: str, body: Optional[Dict[str, Any]] = None,
               rid: Optional[str] = None, key: str = GATEWAY_KEY, **token: Any):
    """A request from the gateway key, signed for exactly this request (unless `token` overrides)."""
    return send(world, method, path, key, body,
                bound(sub, op, method=method, path=path, rid=rid, raw=raw_json(body), **token))


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    """Real auth over stubbed lookups, an in-memory order store under agent_v1_create_order."""
    charged: List[str] = []
    orders: Dict[str, Dict[str, Any]] = {}
    created: List[Dict[str, Any]] = []
    refunds: List[Dict[str, Any]] = []
    intents: List[Dict[str, Any]] = []
    quotes: List[str] = []
    quote_owners: Dict[str, str] = {}

    async def get_agent_by_key(api_key, metrics_out=None):
        agent_id = KEYS.get(api_key)
        return dict(AGENTS[agent_id]) if agent_id else None

    async def get_agent(agent_id):
        return dict(AGENTS[agent_id]) if agent_id in AGENTS else None

    async def rate(agent_id, rate_limit=None):
        charged.append(agent_id)
        return True, 1, int(rate_limit or 60)

    async def quota(agent_id, daily_quota=None):
        return True, 1, int(daily_quota or 1000)

    async def stats(agent_id, increment_requests=1, **_):
        return None

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    monkeypatch.setattr("db.agents.get_agent", get_agent)
    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    monkeypatch.setattr(agent_auth, "check_daily_quota", quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", stats)
    monkeypatch.setattr("utils.redis_client.get_redis_client", lambda: None)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv(FLAG, "1")
    assertion_replay_guard.reset_local_store()

    from services.quote_service import QuoteSnapshot

    async def load_quote(self, *, quote_id):
        now = datetime.now(timezone.utc)
        return QuoteSnapshot(
            quote_id=quote_id, merchant_id="m_shop_a", agent_id=None,
            expires_at=now + timedelta(minutes=10), status="active", engine="test", engine_ref="ref",
            request_fingerprint="fp", request_json={"items": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}]},
            snapshot_json={"currency": "USD"}, quote_hash_sha256="h", debug_id="d",
        )

    async def create_order(**kwargs):
        # What agent_create_order writes: orders.agent_id = context.agent_id, metadata as handed in.
        context = kwargs["context"]
        order_id = f"ORD_{len(orders) + 1}"
        orders[order_id] = {
            "order_id": order_id, "merchant_id": "m_shop_a", "agent_id": context.agent_id,
            "customer_email": "buyer@example.com", "items": [{"product_id": "p_1", "quantity": 1, "unit_price": "10.00"}],
            "shipping_address": {}, "subtotal": "10.00", "total": "10.00", "currency": "USD",
            "status": "pending", "payment_status": "awaiting_payment",
            "metadata": dict(kwargs["order_request"].metadata or {}),
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        }
        created.append({"order_id": order_id, "agent_id": context.agent_id,
                        "allowed_merchants": context.allowed_merchants,
                        "metadata": orders[order_id]["metadata"]})
        return {"status": "success", "order_id": order_id, "payment": {}}

    async def read_order(order_id):
        return orders.get(order_id)

    async def checkout_intent(**kwargs):
        intents.append({"agent_id": kwargs["context"].agent_id, "order_id": kwargs["req"].order_id})
        return {"intent_id": "ci_1", "checkout_session_id": "ci_1", "checkout_token": "tok_1",
                "checkout_url": "https://checkout.example.com/o", "expires_at": 1_900_000_000}

    async def refund(**kwargs):
        refunds.append({"order_id": kwargs["order_id"], "agent_id": kwargs["current_user"]["agent_id"]})
        return {"refund_id": "rf_1", "order_id": kwargs["order_id"], "refund_amount": "10.00"}

    async def preview(req, context):
        # What the real preview writes: quotes.agent_id = the context that previewed (be#2770 reads it).
        quotes.append(context.agent_id)
        quote_owners["q_1"] = context.agent_id
        return {"quote_id": "q_1", "pricing": {"total": "10.00"}, "currency": "USD"}

    async def quote_owner_row(quote_id):
        owner = quote_owners.get(quote_id)
        return {"quote_id": quote_id, "agent_id": owner, "snapshot_json": {}} if owner else None

    async def no_quote_row(quote_id):
        return None

    async def allow(*a, **k):
        return None

    import services.agent_governance as governance_module
    import services.agent_refund_policy as refund_policy

    monkeypatch.setattr(agent_v2.QuoteService, "load_active_quote_or_raise", load_quote)
    monkeypatch.setattr(agent_v2, "agent_v1_create_order", create_order)
    monkeypatch.setattr(agent_v2, "get_order", read_order)
    monkeypatch.setattr(agent_v2, "create_checkout_intent_route", checkout_intent)
    monkeypatch.setattr(agent_v2, "process_refund_route", refund)
    monkeypatch.setattr(agent_v2, "agent_v1_preview_quote", preview)
    monkeypatch.setattr(agent_v2, "get_quote", no_quote_row)
    monkeypatch.setattr("services.quote_service.get_quote_owner_row", quote_owner_row)
    monkeypatch.setattr(agent_v2, "validate_request_compat", allow)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", allow)
    monkeypatch.setattr(refund_policy, "refuse_agent_refund_execution", lambda: None)
    forwarded.FORWARDED_OWNER_OUTCOMES.clear()
    return {
        "client": TestClient(app), "charged": charged, "orders": orders, "created": created,
        "refunds": refunds, "intents": intents, "quotes": quotes, "quote_owners": quote_owners,
    }


ORDERS = "/agent/v2/orders"
PREVIEW = "/agent/v2/quotes/preview"
SESSIONS = "/agent/v2/payments/checkout-sessions"
ORDER_BODY = {
    "quote_id": "q_1",
    "buyer_context": {
        "customer_email": "buyer@example.com", "buyer_ref": "buyer_1",
        "shipping_address": {"name": "Buyer", "address_line1": "1 Example St", "city": "Example City",
                             "state": "CA", "postal_code": "94105", "country": "US"},
    },
    "metadata": {"agent_v2": {"hosted_checkout": True}},
}
PREVIEW_BODY = {"merchant_id": "m_shop_a", "offer_refs": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}]}


def create_as(world, sub: str, body: Optional[Dict[str, Any]] = None, **token: Any):
    body = body or ORDER_BODY
    return as_partner(world, sub, "create_order", "POST", ORDERS, body, rid=body.get("quote_id"), **token)


def rejected(res, reason: str) -> bool:
    detail = res.json().get("detail") if res.headers.get("content-type", "").startswith("application/json") else None
    return res.status_code == 409 and isinstance(detail, dict) and detail.get("reason") == reason


# --- two partners, one gateway: the orders are theirs -------------------------------------------


def test_two_partners_through_one_gateway_own_their_own_orders(world):
    ra = create_as(world, PARTNER_A)
    rb = create_as(world, PARTNER_B)
    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    a, b = world["created"]
    assert (a["agent_id"], b["agent_id"]) == (PARTNER_A, PARTNER_B)
    # The partner's own scope travels with it, not the gateway's unrestricted one.
    assert a["allowed_merchants"] == ["m_shop_a"]
    for row, partner in ((a, PARTNER_A), (b, PARTNER_B)):
        assert row["metadata"]["partner_agent_id"] == partner
        assert row["metadata"]["partner_identity"] == {
            "partner_agent_id": partner, "via_agent_id": GATEWAY_AGENT, "op": "create_order",
        }
    # Each partner pays its own rate limit, after the gateway's.
    assert world["charged"] == [GATEWAY_AGENT, PARTNER_A, GATEWAY_AGENT, PARTNER_B]
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 2


def test_the_quote_is_previewed_as_the_partner(world):
    res = as_partner(world, PARTNER_A, "preview_quote", "POST", PREVIEW, PREVIEW_BODY)
    assert res.status_code == 200, res.text
    assert world["quotes"] == [PARTNER_A]


# --- the binding: one token, one request ---------------------------------------------------------


def test_a_replayed_header_is_refused_and_writes_nothing_more(world):
    token = bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw_json(ORDER_BODY))
    first = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    again = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    assert first.status_code == 200, first.text
    assert rejected(again, "replay"), again.text
    assert [row["agent_id"] for row in world["created"]] == [PARTNER_A]
    assert forwarded.FORWARDED_OWNER_OUTCOMES["rejected_replay"] == 1


def test_a_body_swap_is_refused(world):
    other = {**ORDER_BODY, "buyer_context": {**ORDER_BODY["buyer_context"], "customer_email": "other@example.com"}}
    token = bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw_json(ORDER_BODY))
    res = send(world, "POST", ORDERS, GATEWAY_KEY, other, token)
    assert rejected(res, "body"), res.text
    assert world["created"] == []


def test_the_same_body_reserialised_is_a_different_body(world):
    # The hash is over the exact bytes: a key-order or whitespace change is a different request.
    token = bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw_json(ORDER_BODY))
    raw = json.dumps(ORDER_BODY, indent=1).encode("utf-8")
    res = world["client"].post(ORDERS, content=raw, headers={
        "X-API-Key": GATEWAY_KEY, "X-Pivota-Issuing-Agent": token, "Content-Type": "application/json"})
    assert rejected(res, "body"), res.text


def test_a_path_swap_is_refused(world):
    order_id = _partner_a_order(world)
    _partner_a_order(world)
    path_a, path_b = f"{ORDERS}/{order_id}/refunds", f"{ORDERS}/ORD_2/refunds"
    body = {"reason": "damaged"}
    token = bound(PARTNER_A, "request_after_sales", method="POST", path=path_a, rid=order_id, raw=raw_json(body))
    res = send(world, "POST", path_b, GATEWAY_KEY, body, token)
    assert rejected(res, "path"), res.text
    assert world["refunds"] == []


def test_the_path_is_bound_as_sent_percent_encoding_included(world):
    # An order id the gateway percent-encodes is signed as SENT; the decoded path is another request.
    sent = f"{ORDERS}/ORD%20X/refunds"
    body = {"reason": "damaged"}
    ok = send(world, "POST", sent, GATEWAY_KEY, body,
              bound(PARTNER_A, "request_after_sales", method="POST", path=sent, rid="ORD X", raw=raw_json(body)))
    assert ok.status_code == 404, ok.text  # adopted, then the route's own answer: no such order
    decoded = send(world, "POST", sent, GATEWAY_KEY, body,
                   bound(PARTNER_A, "request_after_sales", method="POST", path=f"{ORDERS}/ORD X/refunds",
                         rid="ORD X", raw=raw_json(body)))
    assert rejected(decoded, "path"), decoded.text


def test_a_resource_swap_is_refused(world):
    # The path and body are this request's, but the token names another resource.
    order_id = _partner_a_order(world)
    body = {"order_id": order_id}
    res = as_partner(world, PARTNER_A, "create_payment_link", "POST", SESSIONS, body, rid="ORD_other")
    assert rejected(res, "resource"), res.text
    other_quote = {**ORDER_BODY, "quote_id": "q_other"}
    token = bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw_json(other_quote))
    res = send(world, "POST", ORDERS, GATEWAY_KEY, other_quote, token)
    assert rejected(res, "resource"), res.text
    assert world["intents"] == [] and len(world["created"]) == 1


def test_a_method_swap_is_refused(world):
    res = create_as(world, PARTNER_A, m="PUT")
    assert rejected(res, "method"), res.text


@pytest.mark.parametrize("skew", [-61, 61, -3600, 3600])
def test_an_expired_or_future_header_is_refused(world, skew):
    res = create_as(world, PARTNER_A, ts=int(time.time()) + skew)
    assert rejected(res, "stale"), res.text
    assert world["created"] == []


def test_a_header_inside_the_window_is_accepted(world):
    assert create_as(world, PARTNER_A, ts=int(time.time()) - 50).status_code == 200


@pytest.mark.parametrize("label,kwargs,reason", [
    ("forged secret", {"secret": "not_the_secret"}, "bad_mac"),
    ("inactive partner", {"sub": INACTIVE}, "subject"),
    ("unknown agent", {"sub": "agent_nobody"}, "subject"),
    ("the gateway itself", {"sub": GATEWAY_AGENT}, "subject"),
    ("no nonce", {"jti": "short"}, "malformed"),
    ("version 1 inside a v2 envelope", {"v": 1}, "malformed"),
])
def test_an_assertion_that_does_not_verify_leaves_the_gateway_the_owner(world, label, kwargs, reason):
    # (Name kept from the v1 design. A v2 header that does not verify now REFUSES the request: it never
    # falls back to the gateway, so a captured or edited header cannot act, nor be retried as the gateway.)
    sub = kwargs.pop("sub", PARTNER_A)
    res = create_as(world, sub, **kwargs)
    assert rejected(res, reason), (label, res.text)
    assert world["created"] == [], label
    assert world["charged"] == [GATEWAY_AGENT], label
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {f"rejected_{reason}": 1}, label


@pytest.mark.parametrize("token,reason", [("v2.not.a-token", "bad_mac"), ("v2.two-parts", "malformed"),
                                          ("v2.a+b.c", "malformed")])
def test_garbage_in_the_v2_envelope_is_refused(world, token, reason):
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    assert rejected(res, reason), res.text
    assert world["created"] == []


def test_a_v1_token_is_never_adopted_on_a_money_route(world):
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, v1(PARTNER_A, "create_order"))
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == GATEWAY_AGENT
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"unbound_v1": 1}


ROUTES = [
    # (label, method, path template, route ops, body, rid)
    ("preview", "POST", PREVIEW, {"preview_quote"}, PREVIEW_BODY, None),
    ("orders", "POST", ORDERS, {"create_order"}, ORDER_BODY, "q_1"),
    ("get", "GET", f"{ORDERS}/ORD_1", {"get_order_status"}, None, "ORD_1"),
    ("tracking", "GET", f"{ORDERS}/ORD_1/tracking", {"get_order_status"}, None, "ORD_1"),
    ("sessions", "POST", SESSIONS, {"create_payment_link", "submit_payment"}, {"order_id": "ORD_1"}, "ORD_1"),
    ("refunds", "POST", f"{ORDERS}/ORD_1/refunds", {"request_after_sales"}, {"reason": "damaged"}, "ORD_1"),
]


@pytest.mark.parametrize("label,method,path,route_ops,body,rid", ROUTES, ids=[r[0] for r in ROUTES])
def test_every_route_refuses_a_token_for_every_other_money_op(world, label, method, path, route_ops, body, rid):
    """P2-1: a token signed for a harmless op (a quote preview, an order read) must never act on a
    route that writes, nor the reverse. Every other entry of PARTNER_IDENTITY_OPS, otherwise
    perfectly bound to this request, is refused before the route runs."""
    for op in PARTNER_IDENTITY_OPS:
        if op in route_ops:
            continue
        res = as_partner(world, PARTNER_A, op, method, path, body, rid=rid)
        assert rejected(res, "op"), (label, op, res.status_code, res.text)
    assert world["created"] == [] and world["refunds"] == [] and world["intents"] == [] and world["quotes"] == []
    assert set(forwarded.FORWARDED_OWNER_OUTCOMES) == {"rejected_op"}


@pytest.mark.parametrize("label,method,path,route_ops,body,rid",
                         [r for r in ROUTES if r[0] != "tracking"], ids=[r[0] for r in ROUTES if r[0] != "tracking"])
def test_every_route_adopts_its_own_op(world, label, method, path, route_ops, body, rid):
    if rid == "ORD_1":
        _partner_a_order(world)
    for op in sorted(route_ops):
        res = as_partner(world, PARTNER_A, op, method, path, body, rid=rid)
        assert res.status_code == 200, (label, op, res.text)


def test_an_agent_key_cannot_vouch_for_another_agent(world):
    res = create_as(world, PARTNER_A, key=PARTNER_B_KEY)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == PARTNER_B
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"ignored_not_service": 1}


def test_no_assertion_from_the_gateway_is_unchanged_and_counted(world):
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == GATEWAY_AGENT
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"absent": 1}


def test_a_partner_calling_with_its_own_key_is_unchanged_and_not_counted(world):
    res = send(world, "POST", ORDERS, PARTNER_A_KEY, ORDER_BODY)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == PARTNER_A
    assert "partner_identity" not in world["created"][0]["metadata"]
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}


def test_a_caller_cannot_plant_the_partner_stamp(world):
    planted = {**ORDER_BODY, "metadata": {
        "partner_identity": {"partner_agent_id": PARTNER_B, "via_agent_id": GATEWAY_AGENT, "op": "create_order"},
        "partner_agent_id": PARTNER_B,
    }}
    send(world, "POST", ORDERS, PARTNER_A_KEY, planted)
    create_as(world, PARTNER_A, body=planted)
    own, adopted = world["created"]
    assert "partner_identity" not in own["metadata"] and "partner_agent_id" not in own["metadata"]
    assert adopted["metadata"]["partner_identity"]["partner_agent_id"] == PARTNER_A
    assert adopted["metadata"]["partner_agent_id"] == PARTNER_A


def test_the_partners_own_rate_limit_refuses_the_call(world, monkeypatch):
    async def rate(agent_id, rate_limit=None):
        return (agent_id != PARTNER_A), 61, 60

    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    res = create_as(world, PARTNER_A)
    assert res.status_code == 429
    assert world["created"] == []


def test_the_partners_own_daily_quota_refuses_the_call(world, monkeypatch):
    async def quota(agent_id, daily_quota=None):
        return (agent_id != PARTNER_A), 1001, 1000

    monkeypatch.setattr(agent_auth, "check_daily_quota", quota)
    res = create_as(world, PARTNER_A)
    assert res.status_code == 429
    assert world["created"] == []
    assert create_as(world, PARTNER_B).status_code == 200


# --- follow-up calls on a partner's order ------------------------------------------------------


def _partner_a_order(world) -> str:
    res = create_as(world, PARTNER_A)
    assert res.status_code == 200, res.text
    return world["created"][-1]["order_id"]


def test_follow_ups_reach_the_order_only_as_its_owner(world):
    order_id = _partner_a_order(world)
    path = f"{ORDERS}/{order_id}"
    get_as = lambda sub, key=GATEWAY_KEY: as_partner(world, sub, "get_order_status", "GET", path, rid=order_id, key=key)  # noqa: E731

    assert get_as(PARTNER_A).status_code == 200
    assert get_as(PARTNER_B).status_code == 403
    assert send(world, "GET", path, PARTNER_A_KEY).status_code == 200, "the partner's own REST read sees its MCP order"
    assert send(world, "GET", path, PARTNER_B_KEY).status_code == 403
    assert get_as(PARTNER_A, key=PARTNER_B_KEY).status_code == 403
    # The gateway with no assertion (its flag turned back off): the order it vouched for stays reachable.
    assert send(world, "GET", path, GATEWAY_KEY).status_code == 200
    assert forwarded.FORWARDED_OWNER_OUTCOMES["via_access"] == 1


def test_a_hosted_link_and_a_strict_payment_both_open_the_session_as_the_partner(world):
    order_id = _partner_a_order(world)
    body = {"order_id": order_id}
    for op in ("create_payment_link", "submit_payment"):
        res = as_partner(world, PARTNER_A, op, "POST", SESSIONS, body, rid=order_id)
        assert res.status_code == 200, (op, res.text)
    res = as_partner(world, PARTNER_B, "create_payment_link", "POST", SESSIONS, body, rid=order_id)
    assert res.status_code == 403
    assert world["intents"] == [{"agent_id": PARTNER_A, "order_id": order_id}] * 2


def test_after_sales_is_requested_as_the_partner(world):
    order_id = _partner_a_order(world)
    path, body = f"{ORDERS}/{order_id}/refunds", {"reason": "damaged"}
    res = as_partner(world, PARTNER_A, "request_after_sales", "POST", path, body, rid=order_id)
    assert res.status_code == 200, res.text
    res = as_partner(world, PARTNER_B, "request_after_sales", "POST", path, body, rid=order_id)
    assert res.status_code == 403
    assert world["refunds"] == [{"order_id": order_id, "agent_id": PARTNER_A}]


def test_a_service_no_longer_allowed_to_vouch_loses_the_via_access(world, monkeypatch):
    order_id = _partner_a_order(world)
    monkeypatch.setattr(agent_auth, "_ISSUING_VOUCHER_AGENT_IDS_DEFAULT", frozenset())
    monkeypatch.delenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS")
    res = send(world, "GET", f"{ORDERS}/{order_id}", GATEWAY_KEY)
    assert res.status_code == 403


def test_a_stamp_planted_while_the_flag_was_off_grants_no_via_access(world, monkeypatch):
    # The v2 create strips a caller's stamp only with the flag on. One planted while it was off names
    # another partner than the order's owner, and so is not this service's record.
    monkeypatch.delenv(FLAG)
    planted = {**ORDER_BODY, "metadata": {
        "partner_identity": {"partner_agent_id": PARTNER_B, "via_agent_id": GATEWAY_AGENT, "op": "create_order"}}}
    assert send(world, "POST", ORDERS, PARTNER_A_KEY, planted).status_code == 200
    order_id = world["created"][-1]["order_id"]
    monkeypatch.setenv(FLAG, "1")
    assert send(world, "GET", f"{ORDERS}/{order_id}", GATEWAY_KEY).status_code == 403


async def test_a_checkout_token_context_is_never_replaced(world):
    from starlette.requests import Request

    raw = raw_json(ORDER_BODY)
    token = bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw)

    def request() -> Request:
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({
            "type": "http", "method": "POST", "path": ORDERS, "raw_path": ORDERS.encode(), "query_string": b"",
            "headers": [(b"x-api-key", GATEWAY_KEY.encode()), (b"x-pivota-issuing-agent", token.encode())],
        }, receive)

    req = request()
    context = agent_auth.AgentContext(dict(AGENTS[GATEWAY_AGENT]), req)
    context.checkout_token_payload = {"intent_id": "ci_1"}
    assert await forwarded.adopt_forwarded_owner(req, context, ops=("create_order",),
                                                 resource=("body", "quote_id")) is context
    del context.checkout_token_payload
    req = request()
    adopted = await forwarded.adopt_forwarded_owner(req, context, ops=("create_order",), resource=("body", "quote_id"))
    assert adopted.agent_id == PARTNER_A, "the same request without a token is adopted"


def test_the_via_access_does_not_reach_an_order_a_partner_made_over_its_own_key(world):
    assert send(world, "POST", ORDERS, PARTNER_A_KEY, ORDER_BODY).status_code == 200
    order_id = world["created"][-1]["order_id"]
    res = send(world, "GET", f"{ORDERS}/{order_id}", GATEWAY_KEY)
    assert res.status_code == 403


# --- flag off: today's behaviour, exactly ------------------------------------------------------


def test_flag_off_a_valid_assertion_changes_nothing(world, monkeypatch):
    monkeypatch.delenv(FLAG)
    planted = {**ORDER_BODY, "metadata": {"partner_agent_id": "kept_as_sent"}}
    for assertion in (bound(PARTNER_A, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw_json(planted)),
                      "v2.not.a-token", v1(PARTNER_A, "create_order")):
        res = send(world, "POST", ORDERS, GATEWAY_KEY, planted, assertion)
        assert res.status_code == 200, res.text
    assert {row["agent_id"] for row in world["created"]} == {GATEWAY_AGENT}
    for row in world["created"]:
        assert row["metadata"]["partner_agent_id"] == "kept_as_sent", "flag off never touches caller metadata"
        assert "partner_identity" not in row["metadata"]
    assert world["charged"] == [GATEWAY_AGENT] * 3
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}


def test_flag_off_the_gateway_has_no_via_access(world, monkeypatch):
    order_id = _partner_a_order(world)
    monkeypatch.delenv(FLAG)
    res = send(world, "GET", f"{ORDERS}/{order_id}", GATEWAY_KEY)
    assert res.status_code == 403


# --- the seed-link mint: a per-response click, recorded as issued to the partner ----------------

MINT_PATH = "/agent/shop/v1/attribution/external-seed-links"
MINT_BODY = {"market": "US", "tool": "find_products_multi", "candidates": [{
    "external_seed_id": "seed_1", "external_product_id": "ext_1",
    "destination_url": "https://example.com/products/serum", "market": "US", "tool": "*",
}]}


@pytest.fixture()
def mint(world, monkeypatch):
    batches: List[List[Any]] = []

    async def allowed(*, market):
        return ["example.com"]

    async def issue(clicks):
        batches.append(list(clicks))
        return len(clicks)

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", allowed)
    monkeypatch.setattr(gw, "issue_clicks", issue)

    def call(key, assertion=None):
        res = send(world, "POST", MINT_PATH, key, MINT_BODY, assertion)
        assert res.status_code == 200, res.text
        [link] = res.json()["links"]
        token = parse_qs(urlparse(link["external_redirect_url"]).query)["token"][0]
        return parse_and_verify_redirect_token(token)["ctx"][PVT_CLICK_ID], link

    def for_mint(sub, **kw):
        return bound(sub, kw.pop("op", "external_seed_links.mint"), method="POST", path=MINT_PATH, rid=None,
                     raw=raw_json(kw.pop("body", MINT_BODY)), **kw)

    return call, batches, for_mint


def test_each_partner_gets_its_own_click_recorded_to_it(mint):
    call, batches, for_mint = mint
    click_a, _ = call(GATEWAY_KEY, for_mint(PARTNER_A))
    click_b, _ = call(GATEWAY_KEY, for_mint(PARTNER_B))
    assert click_a != click_b
    assert [[(c.click_id, c.agent_id) for c in b] for b in batches] == [[(click_a, PARTNER_A)], [(click_b, PARTNER_B)]]


@pytest.mark.parametrize("token", [
    lambda f: f(PARTNER_A, secret="forged"),
    lambda f: f(PARTNER_A, ts=int(time.time()) - 3600),
    lambda f: f(PARTNER_A, op="create_order"),
    lambda f: f(PARTNER_A, body={**MINT_BODY, "market": "GB"}),
    lambda f: v1(PARTNER_A, "external_seed_links.mint"),
])
def test_a_mint_assertion_that_does_not_verify_records_nothing(mint, token):
    call, batches, for_mint = mint
    call(GATEWAY_KEY, token(for_mint))
    assert batches == []


def test_a_replayed_mint_assertion_records_once(mint):
    call, batches, for_mint = mint
    token = for_mint(PARTNER_A)
    call(GATEWAY_KEY, token)
    call(GATEWAY_KEY, token)
    assert len(batches) == 1


def test_a_partner_key_cannot_have_a_mint_credited_to_another_partner(mint):
    call, batches, for_mint = mint
    call(PARTNER_B_KEY, for_mint(PARTNER_A))
    # Recording is for the vouching service's bound assertion only: an agent's own key records nothing.
    assert batches == []


def test_mint_without_an_assertion_or_with_the_flag_off_records_nothing(mint, monkeypatch):
    call, batches, for_mint = mint
    call(GATEWAY_KEY)
    monkeypatch.delenv(FLAG)
    call(GATEWAY_KEY, for_mint(PARTNER_A))
    assert batches == []


def test_a_failed_click_write_still_serves_the_link(mint, monkeypatch):
    call, _, for_mint = mint

    async def boom(clicks):
        raise RuntimeError("db down")

    monkeypatch.setattr(gw, "issue_clicks", boom)
    click, link = call(GATEWAY_KEY, for_mint(PARTNER_A))
    assert click and link["external_redirect_url"]


# --- be#2770 (S-28): the quote must belong to the PARTNER when forwarding is on ----------------------


def _preview_as(world, sub: Optional[str]):
    if sub is None:
        return send(world, "POST", PREVIEW, GATEWAY_KEY, PREVIEW_BODY)
    return as_partner(world, sub, "preview_quote", "POST", PREVIEW, PREVIEW_BODY)


def test_a_quote_the_partner_previewed_is_used_by_that_partner(world):
    world["auto_own_quote"] = False
    assert _preview_as(world, PARTNER_A).status_code == 200
    assert world["quote_owners"]["q_1"] == PARTNER_A, "the forwarded preview stamps the PARTNER as the quote owner"
    res = create_as(world, PARTNER_A)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == PARTNER_A


def test_a_partners_quote_is_not_found_by_another_partner_nor_the_unsigned_gateway(world):
    world["auto_own_quote"] = False
    assert _preview_as(world, PARTNER_A).status_code == 200
    other = create_as(world, PARTNER_B)
    unsigned = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY)
    assert (other.status_code, unsigned.status_code) == (404, 404), (other.text, unsigned.text)
    assert "QUOTE_NOT_FOUND" in other.text and "QUOTE_NOT_FOUND" in unsigned.text
    assert world["created"] == []


def test_a_gateway_quote_is_not_found_by_an_adopted_partner(world):
    world["auto_own_quote"] = False
    assert _preview_as(world, None).status_code == 200
    assert world["quote_owners"]["q_1"] == GATEWAY_AGENT
    res = create_as(world, PARTNER_A)
    assert res.status_code == 404 and "QUOTE_NOT_FOUND" in res.text, res.text
    # ...and the unsigned gateway, which owns it, still may (today's MCP path, flag on or off).
    assert send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY).status_code == 200
    assert [row["agent_id"] for row in world["created"]] == [GATEWAY_AGENT]


# --- O-04 status review P1-7: an OAuth subject (the MCP OAuth door) --------------------------------
#
# The gateway signs a v2 `{kind: "oauth", iss, cid}` for every mcp_oauth caller (PIVOTA-Agent
# src/attribution/issuingAgentAssertion.js issuingSubjectFromInvokeContext). Only a CONFIDENTIAL client
# our authorization server provisioned to an active agent is a partner; every other client (the public
# connectors) is nobody's partner, and its call must stay the gateway's, exactly as before O-04, not 409.
# Driven through the real agent_for_oauth_client / is_confidential_client over a stubbed `database`.


OWN_ISSUER = "https://as.example.com"
PUBLIC_CLIENT = "mcpc_public_connector"
PARTNER_A_CLIENT = "mcpc_partner_a"
UNPROVISIONED_CLIENT = "mcpc_confidential_unprovisioned"
INACTIVE_CLIENT = "mcpc_inactive_agent"
OAUTH_CLIENTS = {
    # client_id: (token_endpoint_auth_method, client_secret_hash)
    PUBLIC_CLIENT: ("none", None),
    PARTNER_A_CLIENT: ("client_secret_basic", "fake_hash_a"),
    UNPROVISIONED_CLIENT: ("client_secret_post", "fake_hash_u"),
    INACTIVE_CLIENT: ("client_secret_basic", "fake_hash_i"),
}
OAUTH_REGISTRATIONS = {(OWN_ISSUER, PARTNER_A_CLIENT): PARTNER_A, (OWN_ISSUER, INACTIVE_CLIENT): INACTIVE}


class _OAuthTables:
    """mcp_oauth_clients + agent_oauth_clients, answering exactly the two queries ia sends."""

    def __init__(self):
        self.fail = False
        self.queries: List[str] = []

    async def fetch_one(self, sql, values):
        if self.fail:
            raise RuntimeError("db down")
        if sql == ia._CLIENT_AUTH_SQL:
            self.queries.append("client")
            row = OAUTH_CLIENTS.get(values["client_id"])
            return {"token_endpoint_auth_method": row[0], "client_secret_hash": row[1]} if row else None
        if sql == ia._ACTIVE_CLIENT_AGENT_SQL:
            self.queries.append("registration")
            agent_id = OAUTH_REGISTRATIONS.get((values["issuer"], values["client_id"]))
            return {"agent_id": agent_id} if agent_id else None
        raise AssertionError(f"unexpected query: {sql}")


@pytest.fixture()
def oauth(world, monkeypatch):
    tables = _OAuthTables()
    monkeypatch.setattr(ia, "database", tables)
    monkeypatch.setattr(ia, "_own_issuer", lambda: OWN_ISSUER)
    world["auto_own_quote"] = False  # each order below previews its quote through the real route first
    return tables


def bound_oauth(cid: str, op: str, *, method: str, path: str, rid: Optional[str], raw: bytes,
                iss: str = OWN_ISSUER, secret: str = SECRET, ts: Optional[int] = None,
                jti: Optional[str] = None) -> str:
    """The gateway's v2 OAuth payload, member for member (signBoundIssuingAgentAssertion): no `sub`."""
    payload: Dict[str, Any] = {
        "v": 2, "kind": "oauth", "iss": iss, "cid": cid, "op": op,
        "ts": int(time.time()) if ts is None else ts, "jti": jti or secrets.token_urlsafe(18),
        "m": method, "p": path, "rid": rid, "bh": hashlib.sha256(raw).hexdigest(),
    }
    return sign_bound_issuing_agent_assertion(payload, secret)


def as_oauth(world, cid: str, op: str, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             rid: Optional[str] = None, **token: Any):
    return send(world, method, path, GATEWAY_KEY, body,
                bound_oauth(cid, op, method=method, path=path, rid=rid, raw=raw_json(body), **token))


def oauth_preview_then_create(world, cid: str, **token: Any):
    preview = as_oauth(world, cid, "preview_quote", "POST", PREVIEW, PREVIEW_BODY)
    assert preview.status_code == 200, preview.text
    return as_oauth(world, cid, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1", **token)


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if k != "order_id"}


@pytest.mark.parametrize("cid,iss", [
    (PUBLIC_CLIENT, OWN_ISSUER),
    (UNPROVISIONED_CLIENT, OWN_ISSUER),
    (PARTNER_A_CLIENT, "https://foreign-as.example.com"),
], ids=["public", "confidential_unprovisioned", "foreign_issuer"])
def test_flag_on_an_unregistered_oauth_client_creates_its_order_as_today(world, oauth, cid, iss):
    # Today's behaviour = the gateway's call with no header at all: measured on the same app first.
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    baseline = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY)
    assert baseline.status_code == 200, baseline.text
    [today] = world["created"]
    world["created"].clear()
    world["charged"].clear()
    world["quote_owners"].clear()
    forwarded.FORWARDED_OWNER_OUTCOMES.clear()

    preview = send(world, "POST", PREVIEW, GATEWAY_KEY, PREVIEW_BODY,
                   bound_oauth(cid, "preview_quote", method="POST", path=PREVIEW, rid=None,
                               raw=raw_json(PREVIEW_BODY), iss=iss))
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY,
               bound_oauth(cid, "create_order", method="POST", path=ORDERS, rid="q_1",
                           raw=raw_json(ORDER_BODY), iss=iss))
    assert (preview.status_code, res.status_code) == (200, 200), (preview.text, res.text)
    assert world["quotes"] == [GATEWAY_AGENT], "the quote is the gateway's, as today"
    [row] = world["created"]
    assert _shape(row) == _shape(today)
    assert row["agent_id"] == GATEWAY_AGENT
    assert "partner_identity" not in row["metadata"] and "partner_agent_id" not in row["metadata"]
    assert world["charged"] == [GATEWAY_AGENT, GATEWAY_AGENT], "no partner is charged"
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"absent_oauth_unregistered": 2}


def test_a_registered_confidential_oauth_client_is_adopted(world, oauth):
    res = oauth_preview_then_create(world, PARTNER_A_CLIENT)
    assert res.status_code == 200, res.text
    assert world["quotes"] == [PARTNER_A]
    [row] = world["created"]
    assert row["agent_id"] == PARTNER_A
    assert row["allowed_merchants"] == ["m_shop_a"]
    assert row["metadata"]["partner_agent_id"] == PARTNER_A
    assert row["metadata"]["partner_identity"] == {
        "partner_agent_id": PARTNER_A, "via_agent_id": GATEWAY_AGENT, "op": "create_order",
    }
    assert world["charged"] == [GATEWAY_AGENT, PARTNER_A, GATEWAY_AGENT, PARTNER_A]
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"adopted": 2}
    assert "registration" in oauth.queries


def test_an_api_key_partner_through_the_gateway_has_partner_agent_id_set(world, oauth):
    preview = as_partner(world, PARTNER_B, "preview_quote", "POST", PREVIEW, PREVIEW_BODY)
    res = create_as(world, PARTNER_B)
    assert (preview.status_code, res.status_code) == (200, 200), (preview.text, res.text)
    [row] = world["created"]
    assert (row["agent_id"], row["metadata"]["partner_agent_id"]) == (PARTNER_B, PARTNER_B)
    assert oauth.queries == [], "an agent subject never reads the OAuth tables"


@pytest.mark.parametrize("label,token,reason", [
    ("forged", {"secret": "not_the_secret"}, "bad_mac"),
    ("expired", {"ts": int(time.time()) - 3600}, "stale"),
    ("future", {"ts": int(time.time()) + 3600}, "stale"),
], ids=["forged", "expired", "future"])
def test_an_unregistered_oauth_token_that_does_not_verify_is_still_refused(world, oauth, label, token, reason):
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    res = as_oauth(world, PUBLIC_CLIENT, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1", **token)
    assert rejected(res, reason), (label, res.text)
    assert world["created"] == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {f"rejected_{reason}": 1}


def test_an_unregistered_oauth_token_that_is_mismatched_is_still_refused(world, oauth):
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    raw = raw_json(ORDER_BODY)
    swapped_body = {**ORDER_BODY, "metadata": {"agent_v2": {"hosted_checkout": False}}}
    cases = [
        ("body", bound_oauth(PUBLIC_CLIENT, "create_order", method="POST", path=ORDERS, rid="q_1", raw=raw),
         swapped_body),
        ("op", bound_oauth(PUBLIC_CLIENT, "preview_quote", method="POST", path=ORDERS, rid="q_1", raw=raw),
         ORDER_BODY),
        ("path", bound_oauth(PUBLIC_CLIENT, "create_order", method="POST", path=PREVIEW, rid="q_1", raw=raw),
         ORDER_BODY),
        ("resource", bound_oauth(PUBLIC_CLIENT, "create_order", method="POST", path=ORDERS, rid="q_2", raw=raw),
         ORDER_BODY),
        ("method", bound_oauth(PUBLIC_CLIENT, "create_order", method="PUT", path=ORDERS, rid="q_1", raw=raw),
         ORDER_BODY),
    ]
    for reason, token, body in cases:
        res = send(world, "POST", ORDERS, GATEWAY_KEY, body, token)
        assert rejected(res, reason), (reason, res.text)
    assert world["created"] == []


@pytest.fixture()
def claims(monkeypatch):
    """Every nonce the real replay store is asked to claim (the real claim still runs)."""
    seen: List[str] = []
    real_claim = assertion_replay_guard.claim

    async def spy(jti, ttl, **kw):
        seen.append(jti)
        return await real_claim(jti, ttl, **kw)

    monkeypatch.setattr(assertion_replay_guard, "claim", spy)
    return seen


def test_a_replayed_unregistered_oauth_token_stays_the_gateways_and_claims_no_nonce(world, oauth, claims):
    # O-04 delta review P3-1: the replay of an unregistered token acts as the gateway, which the same key
    # gets with no header, so a burned nonce buys nothing and would only spend the store's capacity.
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    token = bound_oauth(PUBLIC_CLIENT, "create_order", method="POST", path=ORDERS, rid="q_1",
                        raw=raw_json(ORDER_BODY))
    first = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    second = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert [row["agent_id"] for row in world["created"]] == [GATEWAY_AGENT, GATEWAY_AGENT]
    assert "partner_agent_id" not in world["created"][1]["metadata"]
    assert claims == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"absent_oauth_unregistered": 2}


def test_an_unregistered_oauth_mint_claims_no_nonce(mint, oauth, claims):
    call, batches, _ = mint
    token = bound_oauth(PUBLIC_CLIENT, "external_seed_links.mint", method="POST", path=MINT_PATH, rid=None,
                        raw=raw_json(MINT_BODY))
    call(GATEWAY_KEY, token)
    call(GATEWAY_KEY, token)
    assert batches == [] and claims == []


def test_a_replayed_registered_oauth_token_is_still_refused(world, oauth, claims):
    # Replay protection stays for every subject that is adopted.
    preview = as_oauth(world, PARTNER_A_CLIENT, "preview_quote", "POST", PREVIEW, PREVIEW_BODY)
    assert preview.status_code == 200, preview.text
    token = bound_oauth(PARTNER_A_CLIENT, "create_order", method="POST", path=ORDERS, rid="q_1",
                        raw=raw_json(ORDER_BODY))
    first = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    second = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    assert first.status_code == 200, first.text
    assert rejected(second, "replay"), second.text
    assert [row["agent_id"] for row in world["created"]] == [PARTNER_A]
    assert len(claims) == 3 and claims[1] == claims[2]
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"adopted": 2, "rejected_replay": 1}


class _PivotaLines(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: List[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture()
def pivota_lines():
    """Lines on utils.logger ("pivota"): the logger whose INFO reaches prod (it does not propagate)."""
    handler = _PivotaLines()
    target = logging.getLogger("pivota")
    target.addHandler(handler)
    try:
        yield handler.lines
    finally:
        target.removeHandler(handler)


def test_an_unregistered_oauth_outcome_names_its_iss_and_cid(world, oauth, pivota_lines):
    # O-04 delta review P3-2: an issuer mismatch would turn every provisioned partner into this outcome;
    # the line names the pair so that shows. Both are public identifiers.
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    foreign = "https://foreign-as.example.com"
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY,
               bound_oauth(PARTNER_A_CLIENT, "create_order", method="POST", path=ORDERS, rid="q_1",
                           raw=raw_json(ORDER_BODY), iss=foreign))
    assert res.status_code == 200, res.text
    [line] = [x for x in pivota_lines if "outcome=absent_oauth_unregistered" in x]
    assert f"iss={foreign} cid={PARTNER_A_CLIENT}" in line
    assert SECRET not in line


def test_a_mint_assertion_not_credited_is_logged_through_the_pivota_logger(mint, pivota_lines):
    call, _, for_mint = mint
    call(GATEWAY_KEY, for_mint(PARTNER_A, secret="forged"))
    assert any("external_seed_links.partner_assertion_not_credited reason=bad_mac" in x for x in pivota_lines)


def test_a_registered_oauth_client_whose_agent_is_inactive_is_refused(world, oauth):
    # The registration is live (disabled_at NULL) but its agent is deactivated: a fault, not "nobody's".
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    res = as_oauth(world, INACTIVE_CLIENT, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1")
    assert rejected(res, "subject"), res.text
    assert world["created"] == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"rejected_subject": 1}


class _AgentsTableDown:
    """db.agents.database whose every read raises: db.agents.get_agent swallows it and answers None."""

    def __init__(self):
        self.reads: List[Dict[str, Any]] = []

    async def fetch_one(self, sql, values=None):
        self.reads.append(dict(values or {}))
        raise RuntimeError("agents table unreadable")


def test_a_registered_oauth_client_whose_agent_row_cannot_be_read_is_refused(world, oauth, monkeypatch, claims):
    # O-04 delta review P2-1: the REAL db.agents.get_agent over a database whose read raises. It answers
    # None, which before this fix made a provisioned partner's order the gateway's (200, no stamp).
    monkeypatch.setattr(db_agents, "get_agent", REAL_DB_GET_AGENT)
    down = _AgentsTableDown()
    monkeypatch.setattr(db_agents, "database", down)
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    res = as_oauth(world, PARTNER_A_CLIENT, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1")
    assert rejected(res, "subject"), res.text
    assert down.reads == [{"agent_id": PARTNER_A}], "the real agents-row read ran, for the registered agent"
    assert "registration" in oauth.queries
    assert world["created"] == [] and claims == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"rejected_subject": 1}


def test_an_unreadable_oauth_registration_is_refused_not_taken_as_nobodys(world, oauth):
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    oauth.fail = True
    res = as_oauth(world, PUBLIC_CLIENT, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1")
    assert rejected(res, "subject"), res.text
    assert world["created"] == []


def test_an_agent_subject_that_names_no_active_agent_is_still_refused(world, oauth):
    # Only the OAuth miss is "nobody's partner": the gateway signs kind=agent only for a key it verified.
    res = create_as(world, INACTIVE)
    assert rejected(res, "subject"), res.text


def test_a_v1_oauth_token_is_never_adopted_on_a_money_route(world, oauth):
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    token = sign_issuing_agent_assertion(
        {"v": 1, "kind": "oauth", "iss": OWN_ISSUER, "cid": PARTNER_A_CLIENT, "op": "create_order",
         "ts": int(time.time())}, SECRET)
    res = send(world, "POST", ORDERS, GATEWAY_KEY, ORDER_BODY, token)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == GATEWAY_AGENT
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"unbound_v1": 1}


def test_flag_off_an_oauth_token_changes_nothing(world, oauth, monkeypatch):
    monkeypatch.delenv(FLAG)
    world["quote_owners"]["q_1"] = GATEWAY_AGENT
    for cid in (PUBLIC_CLIENT, PARTNER_A_CLIENT):
        res = as_oauth(world, cid, "create_order", "POST", ORDERS, ORDER_BODY, rid="q_1")
        assert res.status_code == 200, res.text
    assert [row["agent_id"] for row in world["created"]] == [GATEWAY_AGENT, GATEWAY_AGENT]
    assert oauth.queries == [] and forwarded.FORWARDED_OWNER_OUTCOMES == {}


def test_an_unregistered_oauth_mint_serves_links_and_records_nothing(mint, oauth):
    call, batches, _ = mint
    _, link = call(GATEWAY_KEY, bound_oauth(PUBLIC_CLIENT, "external_seed_links.mint", method="POST",
                                            path=MINT_PATH, rid=None, raw=raw_json(MINT_BODY)))
    assert link["external_redirect_url"] and batches == []
    click, _ = call(GATEWAY_KEY, bound_oauth(PARTNER_A_CLIENT, "external_seed_links.mint", method="POST",
                                             path=MINT_PATH, rid=None, raw=raw_json(MINT_BODY)))
    assert [[(c.click_id, c.agent_id) for c in b] for b in batches] == [[(click, PARTNER_A)]]
