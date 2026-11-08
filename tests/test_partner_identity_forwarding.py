"""Partner identity on MCP money calls (FIX_PLAN O-04), through the real routes.

The gateway's commerce kernel calls these routes with its OWN service key, so before this every
MCP order, quote, hosted link and refund belonged to the gateway's agent. With
PARTNER_IDENTITY_FORWARDING_ENABLED on, a REALLY signed X-Pivota-Issuing-Agent from the gateway
makes the partner it names the route's AgentContext: the owner of what is written, and the one
whose scope, rate limit and quota apply.

Driven through the real app: the real get_agent_context (API key auth), the real
forwarded_owner_context dependency, the real verifier (services/issuing_agent_assertion.py) and
the real route bodies. Stubbed: the key/agent lookups, the rate/quota counters, and the layer
below each route (agent_v1_create_order, which writes `context.agent_id` as orders.agent_id, the
order read, the checkout-intent mint, the refund executor).
"""

from __future__ import annotations

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

AGENTS: Dict[str, Dict[str, Any]] = {
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "agent_name": "Gateway", "is_active": True, "allowed_merchants": None},
    PARTNER_A: {"agent_id": PARTNER_A, "agent_name": "Partner A", "is_active": True,
                "allowed_merchants": ["m_shop_a"], "rate_limit": 60, "daily_quota": 1000},
    PARTNER_B: {"agent_id": PARTNER_B, "agent_name": "Partner B", "is_active": True,
                "allowed_merchants": ["m_shop_a"], "rate_limit": 60, "daily_quota": 1000},
    INACTIVE: {"agent_id": INACTIVE, "agent_name": "Off", "is_active": False},
}
KEYS = {GATEWAY_KEY: GATEWAY_AGENT, PARTNER_A_KEY: PARTNER_A, PARTNER_B_KEY: PARTNER_B}


def signed(sub: str, op: str, *, secret: str = SECRET, ts: Optional[int] = None, kind: str = "agent") -> str:
    return sign_issuing_agent_assertion(
        {"v": 1, "kind": kind, "sub": sub, "op": op, "ts": int(time.time()) if ts is None else ts}, secret
    )


def headers(key: str, assertion: Optional[str] = None) -> Dict[str, str]:
    out = {"X-API-Key": key}
    if assertion is not None:
        out["X-Pivota-Issuing-Agent"] = assertion
    return out


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
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv(FLAG, "1")

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


ORDER_BODY = {
    "quote_id": "q_1",
    "buyer_context": {
        "customer_email": "buyer@example.com", "buyer_ref": "buyer_1",
        "shipping_address": {"name": "Buyer", "address_line1": "1 Example St", "city": "Example City",
                             "state": "CA", "postal_code": "94105", "country": "US"},
    },
    "metadata": {"agent_v2": {"hosted_checkout": True}},
}


def create(world, key: str, assertion: Optional[str] = None, body: Optional[Dict[str, Any]] = None):
    return world["client"].post("/agent/v2/orders", json=body or ORDER_BODY, headers=headers(key, assertion))


# --- two partners, one gateway: the orders are theirs -------------------------------------------


def test_two_partners_through_one_gateway_own_their_own_orders(world):
    ra = create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order"))
    rb = create(world, GATEWAY_KEY, signed(PARTNER_B, "create_order"))
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
    res = world["client"].post(
        "/agent/v2/quotes/preview",
        json={"merchant_id": "m_shop_a", "offer_refs": [{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}]},
        headers=headers(GATEWAY_KEY, signed(PARTNER_A, "preview_quote")),
    )
    assert res.status_code == 200, res.text
    assert world["quotes"] == [PARTNER_A]


# --- spoofing: ownership is never taken from anything unverified -------------------------------


@pytest.mark.parametrize("label,assertion", [
    ("forged secret", lambda: signed(PARTNER_A, "create_order", secret="not_the_secret")),
    ("expired", lambda: signed(PARTNER_A, "create_order", ts=int(time.time()) - 3600)),
    ("future", lambda: signed(PARTNER_A, "create_order", ts=int(time.time()) + 3600)),
    ("another op", lambda: signed(PARTNER_A, "offers.resolve")),
    ("inactive partner", lambda: signed(INACTIVE, "create_order")),
    ("unknown agent", lambda: signed("agent_nobody", "create_order")),
    ("the gateway itself", lambda: signed(GATEWAY_AGENT, "create_order")),
    ("garbage", lambda: "v1.not.a-token"),
])
def test_an_assertion_that_does_not_verify_leaves_the_gateway_the_owner(world, label, assertion):
    res = create(world, GATEWAY_KEY, assertion())
    assert res.status_code == 200, (label, res.text)
    [row] = world["created"]
    assert row["agent_id"] == GATEWAY_AGENT, label
    assert "partner_identity" not in row["metadata"], label
    assert world["charged"] == [GATEWAY_AGENT], label
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"invalid": 1}, label


def test_an_agent_key_cannot_vouch_for_another_agent(world):
    res = create(world, PARTNER_B_KEY, signed(PARTNER_A, "create_order"))
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == PARTNER_B
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"ignored_not_service": 1}


def test_no_assertion_from_the_gateway_is_unchanged_and_counted(world):
    res = create(world, GATEWAY_KEY)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == GATEWAY_AGENT
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {"absent": 1}


def test_a_partner_calling_with_its_own_key_is_unchanged_and_not_counted(world):
    res = create(world, PARTNER_A_KEY)
    assert res.status_code == 200, res.text
    assert world["created"][0]["agent_id"] == PARTNER_A
    assert "partner_identity" not in world["created"][0]["metadata"]
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}


def test_a_caller_cannot_plant_the_partner_stamp(world):
    planted = {**ORDER_BODY, "metadata": {
        "partner_identity": {"partner_agent_id": PARTNER_B, "via_agent_id": GATEWAY_AGENT, "op": "create_order"},
        "partner_agent_id": PARTNER_B,
    }}
    create(world, PARTNER_A_KEY, body=planted)
    create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order"), body=planted)
    own, adopted = world["created"]
    assert "partner_identity" not in own["metadata"] and "partner_agent_id" not in own["metadata"]
    assert adopted["metadata"]["partner_identity"]["partner_agent_id"] == PARTNER_A
    assert adopted["metadata"]["partner_agent_id"] == PARTNER_A


def test_the_partners_own_rate_limit_refuses_the_call(world, monkeypatch):
    async def rate(agent_id, rate_limit=None):
        return (agent_id != PARTNER_A), 61, 60

    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    res = create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order"))
    assert res.status_code == 429
    assert world["created"] == []


def test_the_partners_own_daily_quota_refuses_the_call(world, monkeypatch):
    async def quota(agent_id, daily_quota=None):
        return (agent_id != PARTNER_A), 1001, 1000

    monkeypatch.setattr(agent_auth, "check_daily_quota", quota)
    res = create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order"))
    assert res.status_code == 429
    assert world["created"] == []
    assert create(world, GATEWAY_KEY, signed(PARTNER_B, "create_order")).status_code == 200


# --- follow-up calls on a partner's order ------------------------------------------------------


def _partner_a_order(world) -> str:
    assert create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order")).status_code == 200
    return world["created"][-1]["order_id"]


def test_follow_ups_reach_the_order_only_as_its_owner(world):
    order_id = _partner_a_order(world)
    c = world["client"]
    get = lambda h: c.get(f"/agent/v2/orders/{order_id}", headers=h)  # noqa: E731

    assert get(headers(GATEWAY_KEY, signed(PARTNER_A, "get_order_status"))).status_code == 200
    assert get(headers(GATEWAY_KEY, signed(PARTNER_B, "get_order_status"))).status_code == 403
    assert get(headers(PARTNER_A_KEY)).status_code == 200, "the partner's own REST read sees its MCP order"
    assert get(headers(PARTNER_B_KEY)).status_code == 403
    assert get(headers(PARTNER_B_KEY, signed(PARTNER_A, "get_order_status"))).status_code == 403
    # The gateway with no assertion (its flag turned back off): the order it vouched for stays reachable.
    assert get(headers(GATEWAY_KEY)).status_code == 200


def test_a_hosted_link_and_a_strict_payment_both_open_the_session_as_the_partner(world):
    order_id = _partner_a_order(world)
    c = world["client"]
    for op in ("create_payment_link", "submit_payment"):
        res = c.post("/agent/v2/payments/checkout-sessions", json={"order_id": order_id},
                     headers=headers(GATEWAY_KEY, signed(PARTNER_A, op)))
        assert res.status_code == 200, (op, res.text)
    res = c.post("/agent/v2/payments/checkout-sessions", json={"order_id": order_id},
                 headers=headers(GATEWAY_KEY, signed(PARTNER_B, "create_payment_link")))
    assert res.status_code == 403
    assert world["intents"] == [{"agent_id": PARTNER_A, "order_id": order_id}] * 2


def test_after_sales_is_requested_as_the_partner(world):
    order_id = _partner_a_order(world)
    c = world["client"]
    res = c.post(f"/agent/v2/orders/{order_id}/refunds", json={"reason": "damaged"},
                 headers=headers(GATEWAY_KEY, signed(PARTNER_A, "request_after_sales")))
    assert res.status_code == 200, res.text
    res = c.post(f"/agent/v2/orders/{order_id}/refunds", json={"reason": "damaged"},
                 headers=headers(GATEWAY_KEY, signed(PARTNER_B, "request_after_sales")))
    assert res.status_code == 403
    assert world["refunds"] == [{"order_id": order_id, "agent_id": PARTNER_A}]


def test_a_service_no_longer_allowed_to_vouch_loses_the_via_access(world, monkeypatch):
    order_id = _partner_a_order(world)
    monkeypatch.setattr(agent_auth, "_ISSUING_VOUCHER_AGENT_IDS_DEFAULT", frozenset())
    monkeypatch.delenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS")
    res = world["client"].get(f"/agent/v2/orders/{order_id}", headers=headers(GATEWAY_KEY))
    assert res.status_code == 403


async def test_a_checkout_token_context_is_never_replaced(world):
    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "POST", "path": "/agent/v2/orders", "query_string": b"",
        "headers": [(b"x-api-key", GATEWAY_KEY.encode()),
                    (b"x-pivota-issuing-agent", signed(PARTNER_A, "create_order").encode())],
    })
    context = agent_auth.AgentContext(dict(AGENTS[GATEWAY_AGENT]), request)
    context.checkout_token_payload = {"intent_id": "ci_1"}
    assert await forwarded.adopt_forwarded_owner(request, context, ops=("create_order",)) is context
    del context.checkout_token_payload
    adopted = await forwarded.adopt_forwarded_owner(request, context, ops=("create_order",))
    assert adopted.agent_id == PARTNER_A, "the same request without a token is adopted"


def test_the_via_access_does_not_reach_an_order_a_partner_made_over_its_own_key(world):
    assert create(world, PARTNER_A_KEY).status_code == 200
    order_id = world["created"][-1]["order_id"]
    res = world["client"].get(f"/agent/v2/orders/{order_id}", headers=headers(GATEWAY_KEY))
    assert res.status_code == 403


# --- flag off: today's behaviour, exactly ------------------------------------------------------


def test_flag_off_a_valid_assertion_changes_nothing(world, monkeypatch):
    monkeypatch.delenv(FLAG)
    planted = {**ORDER_BODY, "metadata": {"partner_agent_id": "kept_as_sent"}}
    res = create(world, GATEWAY_KEY, signed(PARTNER_A, "create_order"), body=planted)
    assert res.status_code == 200, res.text
    [row] = world["created"]
    assert row["agent_id"] == GATEWAY_AGENT
    assert row["metadata"]["partner_agent_id"] == "kept_as_sent", "flag off never touches caller metadata"
    assert "partner_identity" not in row["metadata"]
    assert world["charged"] == [GATEWAY_AGENT]
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}


def test_flag_off_the_gateway_has_no_via_access(world, monkeypatch):
    order_id = _partner_a_order(world)
    monkeypatch.delenv(FLAG)
    res = world["client"].get(f"/agent/v2/orders/{order_id}", headers=headers(GATEWAY_KEY))
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
        res = world["client"].post(MINT_PATH, json=MINT_BODY, headers=headers(key, assertion))
        assert res.status_code == 200, res.text
        [link] = res.json()["links"]
        token = parse_qs(urlparse(link["external_redirect_url"]).query)["token"][0]
        return parse_and_verify_redirect_token(token)["ctx"][PVT_CLICK_ID], link

    return call, batches


def test_each_partner_gets_its_own_click_recorded_to_it(mint):
    call, batches = mint
    click_a, _ = call(GATEWAY_KEY, signed(PARTNER_A, "external_seed_links.mint"))
    click_b, _ = call(GATEWAY_KEY, signed(PARTNER_B, "external_seed_links.mint"))
    assert click_a != click_b
    assert [[(c.click_id, c.agent_id) for c in b] for b in batches] == [[(click_a, PARTNER_A)], [(click_b, PARTNER_B)]]


@pytest.mark.parametrize("assertion", [
    lambda: signed(PARTNER_A, "external_seed_links.mint", secret="forged"),
    lambda: signed(PARTNER_A, "external_seed_links.mint", ts=int(time.time()) - 3600),
    lambda: signed(PARTNER_A, "create_order"),
])
def test_a_mint_assertion_that_does_not_verify_records_nothing(mint, assertion):
    call, batches = mint
    call(GATEWAY_KEY, assertion())
    assert batches == []


def test_a_partner_key_cannot_have_a_mint_credited_to_another_partner(mint):
    call, batches = mint
    call(PARTNER_B_KEY, signed(PARTNER_A, "external_seed_links.mint"))
    # Its own key names it: the click is issued to the caller, never to the agent it named.
    assert [{c.agent_id for c in b} for b in batches] == [{PARTNER_B}]


def test_mint_without_an_assertion_or_with_the_flag_off_records_nothing(mint, monkeypatch):
    call, batches = mint
    call(GATEWAY_KEY)
    monkeypatch.delenv(FLAG)
    call(GATEWAY_KEY, signed(PARTNER_A, "external_seed_links.mint"))
    assert batches == []


def test_a_failed_click_write_still_serves_the_link(mint, monkeypatch):
    call, _ = mint

    async def boom(clicks):
        raise RuntimeError("db down")

    monkeypatch.setattr(gw, "issue_clicks", boom)
    click, link = call(GATEWAY_KEY, signed(PARTNER_A, "external_seed_links.mint"))
    assert click and link["external_redirect_url"]
