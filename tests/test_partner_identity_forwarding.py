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

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import routes.agent_auth as agent_auth
import routes.agent_forwarded_owner as forwarded
import routes.agent_shop_gateway as gw
import routes.agent_v2 as agent_v2
from main import app
from services import assertion_replay_guard
from services.bound_issuing_assertion import sign_bound_issuing_agent_assertion
from services.commerce_attribution_service import PVT_CLICK_ID
from services.issuing_agent_assertion import sign_issuing_agent_assertion
from services.outbound_links_service import parse_and_verify_redirect_token

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


def send(world, method: str, path: str, key: str, body: Optional[Dict[str, Any]] = None,
         assertion: Optional[str] = None):
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
        quotes.append(context.agent_id)
        return {"quote_id": "q_1", "pricing": {"total": "10.00"}, "currency": "USD"}

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
    monkeypatch.setattr(agent_v2, "validate_request_compat", allow)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", allow)
    monkeypatch.setattr(refund_policy, "refuse_agent_refund_execution", lambda: None)
    forwarded.FORWARDED_OWNER_OUTCOMES.clear()
    return {
        "client": TestClient(app), "charged": charged, "orders": orders, "created": created,
        "refunds": refunds, "intents": intents, "quotes": quotes,
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
