"""Order-create replay is the calling agent's own; a checkout token reaches only its own order.

Two holes, pinned here:

1. Order-create replay matched on merchant + a caller-chosen key (idempotency key, quote id,
   session id). Every REST partner reaches the same merchants, so a short key like "1" that partner
   A used handed partner B A's order (totals, items, PSP client secret). The replay lookup now runs
   the real SQL scoped to `orders.agent_id` (exercised below on an in-memory SQLite copy of
   `orders`), and the idempotency cache key is namespaced by agent.
2. A checkout token (put in the hosted checkout URL) was scoped by path prefix, so it could list the
   agent's order events, read any of its orders, cancel and refund. It now reaches an exact route
   list, and on order-id routes only the order it is bound to.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import db.orders as orders_module
import routes.agent_auth as agent_auth
from db.orders import orders

AGENT_A = "agent_aaaaaaaa"
AGENT_B = "agent_bbbbbbbb"
AGENT_C = "agent_cccccccc"
MERCHANT = "merch_shared01"
T0 = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Replay lookup on a real table
# ---------------------------------------------------------------------------


def _order_row(
    order_id: str,
    agent_id: Optional[str],
    *,
    minutes: int = 0,
    idempotency_key: Optional[str] = None,
    quote_id: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    intent_id: Optional[str] = None,
    psp_used: Optional[str] = "stripe",
    deleted: bool = False,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if idempotency_key:
        metadata["idempotency_key"] = idempotency_key
    if quote_id:
        metadata["pricing_quote"] = {"quote_id": quote_id}
    if intent_id:
        metadata["intent_id"] = intent_id
    return {
        "order_id": order_id,
        "merchant_id": MERCHANT,
        "agent_id": agent_id,
        "customer_email": f"{order_id}@buyer.example",
        "shipping_address": {},
        "items": [],
        "subtotal": 10,
        "total": 10,
        "currency": "USD",
        "client_secret": f"cs_secret_{order_id}",
        "psp_used": psp_used,
        "agent_session_id": agent_session_id,
        "metadata": metadata,
        "created_at": T0 + timedelta(minutes=minutes),
        "is_deleted": deleted,
    }


def _install_table(monkeypatch: pytest.MonkeyPatch, rows: List[Dict[str, Any]]):
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    orders.to_metadata(meta)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(meta.tables["orders"].insert(), rows)

    async def fetch_one(query, values=None):
        with engine.connect() as conn:
            row = conn.execute(sa.text(query), values or {}).mappings().first()
        if row is None:
            return None
        out = dict(row)
        if isinstance(out.get("metadata"), str):
            out["metadata"] = json.loads(out["metadata"])
        return out

    monkeypatch.setattr(orders_module.database, "fetch_one", fetch_one)
    return engine


SHARED_KEY_ROWS = [
    # Partner A used the short key "1", quote "q_shared" and session "sess-42" first...
    _order_row("ord_a_key", AGENT_A, minutes=1, idempotency_key="1"),
    _order_row("ord_a_quote", AGENT_A, minutes=2, quote_id="q_shared"),
    _order_row("ord_a_sess", AGENT_A, minutes=3, agent_session_id="sess-42"),
    # ...and partner B used the same values earlier.
    _order_row("ord_b_key", AGENT_B, minutes=0, idempotency_key="1"),
    _order_row("ord_b_quote", AGENT_B, minutes=0, quote_id="q_shared"),
    _order_row("ord_b_sess", AGENT_B, minutes=0, agent_session_id="sess-42"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup",
    [{"idempotency_key": "1"}, {"quote_id": "q_shared"}, {"agent_session_id": "sess-42"}],
    ids=["idempotency_key", "quote_id", "agent_session_id"],
)
async def test_same_key_two_agents_each_gets_only_its_own_order(monkeypatch, lookup) -> None:
    _install_table(monkeypatch, SHARED_KEY_ROWS)

    for agent, prefix in ((AGENT_A, "ord_a_"), (AGENT_B, "ord_b_")):
        found = await orders_module.find_replayable_order_for_create(
            merchant_id=MERCHANT, agent_id=agent, **lookup
        )
        assert found is not None
        assert found["agent_id"] == agent
        assert found["order_id"].startswith(prefix)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup",
    [{"idempotency_key": "1"}, {"quote_id": "q_shared"}, {"agent_session_id": "sess-42"}],
    ids=["idempotency_key", "quote_id", "agent_session_id"],
)
async def test_key_used_only_by_another_agent_is_a_fresh_create(monkeypatch, lookup) -> None:
    _install_table(monkeypatch, SHARED_KEY_ROWS)

    found = await orders_module.find_replayable_order_for_create(
        merchant_id=MERCHANT, agent_id=AGENT_C, **lookup
    )

    assert found is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "", "   "])
async def test_no_calling_agent_replays_nothing(monkeypatch, agent_id) -> None:
    # Includes an order whose agent is blank: a blank caller must not match it either.
    _install_table(monkeypatch, SHARED_KEY_ROWS + [_order_row("ord_blank", "", minutes=9, idempotency_key="1")])

    found = await orders_module.find_replayable_order_for_create(
        merchant_id=MERCHANT, agent_id=agent_id, idempotency_key="1"
    )

    assert found is None


_PRE_FIX_IDEMPOTENCY_QUERY = """
    SELECT *
    FROM orders
    WHERE merchant_id = :merchant_id
      AND COALESCE(is_deleted, FALSE) = FALSE
      AND metadata ->> 'idempotency_key' = :match_value
    ORDER BY created_at DESC
    LIMIT 1
"""


@pytest.mark.asyncio
async def test_same_agent_replay_returns_the_row_the_pre_fix_lookup_returned(monkeypatch) -> None:
    # One agent only: the scoped lookup must pick exactly the row the unscoped one did
    # (latest, non-deleted), so a partner's own retries replay byte-identically.
    rows = [
        _order_row("ord_old", AGENT_A, minutes=0, idempotency_key="k"),
        _order_row("ord_new", AGENT_A, minutes=5, idempotency_key="k"),
        _order_row("ord_deleted", AGENT_A, minutes=9, idempotency_key="k", deleted=True),
    ]
    engine = _install_table(monkeypatch, rows)
    with engine.connect() as conn:
        before = dict(
            conn.execute(
                sa.text(_PRE_FIX_IDEMPOTENCY_QUERY), {"merchant_id": MERCHANT, "match_value": "k"}
            ).mappings().first()
        )
    before["metadata"] = json.loads(before["metadata"])

    after = await orders_module.find_replayable_order_for_create(
        merchant_id=MERCHANT, agent_id=AGENT_A, idempotency_key="k"
    )

    assert after == before
    assert after["order_id"] == "ord_new"


@pytest.mark.asyncio
async def test_checkout_token_replay_is_narrowed_to_its_intent(monkeypatch) -> None:
    rows = [
        _order_row("ord_api_key", AGENT_A, minutes=5, idempotency_key="k"),
        _order_row("ord_intent_1", AGENT_A, minutes=1, idempotency_key="k", intent_id="ci_1"),
    ]
    _install_table(monkeypatch, rows)

    under_intent = await orders_module.find_replayable_order_for_create(
        merchant_id=MERCHANT, agent_id=AGENT_A, idempotency_key="k", intent_id="ci_1"
    )
    other_intent = await orders_module.find_replayable_order_for_create(
        merchant_id=MERCHANT, agent_id=AGENT_A, idempotency_key="k", intent_id="ci_2"
    )

    assert under_intent["order_id"] == "ord_intent_1"
    assert other_intent is None


def test_idempotency_cache_key_is_namespaced_by_agent_and_token_intent() -> None:
    import routes.agent_api as agent_api

    key_a = agent_api._order_create_idempotency_cache_key("1", agent_id=AGENT_A, intent_id=None)
    key_b = agent_api._order_create_idempotency_cache_key("1", agent_id=AGENT_B, intent_id=None)
    key_token = agent_api._order_create_idempotency_cache_key("1", agent_id=AGENT_A, intent_id="ci_1")

    assert key_a == f"agent:{AGENT_A}:1"
    assert key_b == f"agent:{AGENT_B}:1"
    assert key_token == f"agent:{AGENT_A}:intent:ci_1:1"
    assert len({key_a, key_b, key_token}) == 3
    assert agent_api._order_create_idempotency_cache_key("1", agent_id=None, intent_id=None) is None
    assert agent_api._order_create_idempotency_cache_key("", agent_id=AGENT_A, intent_id=None) is None


@pytest.mark.asyncio
async def test_idempotency_cache_written_by_one_agent_is_not_read_by_another(monkeypatch) -> None:
    import routes.agent_api as agent_api

    stored: Dict[str, Any] = {}

    class _Store:
        async def put(self, *, scope, key, value):
            stored[(scope, key)] = value

        async def get(self, *, scope, key):
            value = stored.get((scope, key))
            return SimpleNamespace(value=value) if value is not None else None

    monkeypatch.setattr(agent_api, "_AGENT_ORDER_IDEMPOTENCY_STORE", _Store())
    await agent_api._cache_agent_order_create_response_best_effort(
        "1", {"status": "success", "order_id": "ord_a"}, agent_id=AGENT_A, intent_id=None
    )

    own = agent_api._order_create_idempotency_cache_key("1", agent_id=AGENT_A, intent_id=None)
    other = agent_api._order_create_idempotency_cache_key("1", agent_id=AGENT_B, intent_id=None)
    assert (await agent_api._AGENT_ORDER_IDEMPOTENCY_STORE.get(scope="order_create", key=own)).value[
        "order_id"
    ] == "ord_a"
    assert await agent_api._AGENT_ORDER_IDEMPOTENCY_STORE.get(scope="order_create", key=other) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_id,intent_id,order_agent,order_intent,replayed",
    [
        (AGENT_A, None, AGENT_A, None, True),  # the owner's own pre-fix entry: replayed as cached
        (AGENT_B, None, AGENT_A, None, False),  # another agent's entry: a miss
        (AGENT_A, "ci_1", AGENT_A, "ci_1", True),  # a token's entry for its own intent
        (AGENT_A, "ci_2", AGENT_A, "ci_1", False),  # same agent, another token's session
        (AGENT_A, None, None, None, False),  # the order is gone or has no agent
    ],
)
async def test_pre_fix_cache_entry_under_the_bare_key_replays_only_for_its_owner(
    monkeypatch, agent_id, intent_id, order_agent, order_intent, replayed
) -> None:
    import routes.agent_api as agent_api

    cached = {"status": "success", "order_id": "ord_legacy", "merchant_id": MERCHANT, "client_secret": "cs_x"}
    stored = {("order_create", "1"): cached}

    class _Store:
        async def get(self, *, scope, key):
            value = stored.get((scope, key))
            return SimpleNamespace(value=value) if value is not None else None

    async def get_order(order_id):
        if order_agent is None:
            return None
        return {"order_id": order_id, "agent_id": order_agent, "metadata": {"intent_id": order_intent}}

    monkeypatch.setattr(agent_api, "_AGENT_ORDER_IDEMPOTENCY_STORE", _Store())
    monkeypatch.setattr(agent_api, "get_order", get_order)

    record = await agent_api._get_cached_agent_order_create_response("1", agent_id=agent_id, intent_id=intent_id)

    if replayed:
        assert record.value is cached
    else:
        assert record is None


def test_replay_owner_reads_agent_and_checkout_token_intent() -> None:
    import routes.agent_api as agent_api

    api_key_ctx = SimpleNamespace(agent_id=AGENT_A)
    token_ctx = SimpleNamespace(agent_id=AGENT_A, checkout_token_payload={"intent_id": "ci_1"})

    assert agent_api._order_create_replay_owner(api_key_ctx) == {"agent_id": AGENT_A, "intent_id": None}
    assert agent_api._order_create_replay_owner(token_ctx) == {"agent_id": AGENT_A, "intent_id": "ci_1"}


# ---------------------------------------------------------------------------
# 2. Checkout token: exact routes, own order only
# ---------------------------------------------------------------------------

SECRET = "test-checkout-token-secret-scope"
INTENT = "ci_token_intent"
OTHER_INTENT = "ci_other_intent"

TOKEN_ORDERS: Dict[str, Dict[str, Any]] = {
    # Created by the hosted page under this token.
    "ord_mine": {"order_id": "ord_mine", "agent_id": AGENT_A, "merchant_id": MERCHANT,
                 "metadata": {"intent_id": INTENT}},
    # A re-quote under the same token creates a second order for the same session.
    "ord_mine_2": {"order_id": "ord_mine_2", "agent_id": AGENT_A, "merchant_id": MERCHANT,
                   "metadata": json.dumps({"intent_id": INTENT})},
    # The order the intent was minted for (resume): bound by the token's order_id claim.
    "ord_resumed": {"order_id": "ord_resumed", "agent_id": AGENT_A, "merchant_id": MERCHANT,
                    "metadata": {}},
    # Same agent, other buyers.
    "ord_other_intent": {"order_id": "ord_other_intent", "agent_id": AGENT_A, "merchant_id": MERCHANT,
                         "metadata": {"intent_id": OTHER_INTENT}},
    "ord_api_key": {"order_id": "ord_api_key", "agent_id": AGENT_A, "merchant_id": MERCHANT,
                    "metadata": {}},
    # Another agent's order that carries this token's intent id: still not the token's.
    "ord_b_same_intent": {"order_id": "ord_b_same_intent", "agent_id": AGENT_B, "merchant_id": MERCHANT,
                          "metadata": {"intent_id": INTENT}},
}


def _mint(**extra: Any) -> str:
    from routes.agent_checkout_intents import mint_checkout_token

    payload = {
        "agent_id": AGENT_A,
        "intent_id": INTENT,
        "merchant_ids": [MERCHANT],
        "scopes": ["checkout"],
        "items": [{"merchant_id": MERCHANT, "product_id": "p_item", "variant_id": "v_item", "quantity": 1}],
    }
    payload.update(extra)
    return mint_checkout_token(payload, ttl_seconds=600)


# product_group_members, on a real SQLite table: the token's item p_item is in grp_item with another
# listing of the same product by its merchant (p_item_alt) and one by another merchant.
CATALOG_MEMBERS = [
    ("grp_item", MERCHANT, "p_item"),
    ("grp_item", MERCHANT, "p_item_alt"),
    ("grp_item", "merch_other", "p_other_seller"),
    ("grp_unrelated", MERCHANT, "p_unrelated"),
    ("grp_foreign", "merch_other", "p_foreign"),
]


def _install_catalog(monkeypatch: pytest.MonkeyPatch, *, broken: bool = False):
    import db.database as db_database
    from sqlalchemy.pool import StaticPool

    # One shared in-memory database: the routed app reads it from TestClient's worker thread.
    engine = sa.create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE product_group_members (product_group_id TEXT, merchant_id TEXT, platform TEXT,"
            " platform_product_id TEXT, is_primary BOOLEAN)"
        ))
        for group_id, merchant_id, product_id in CATALOG_MEMBERS:
            conn.execute(
                sa.text("INSERT INTO product_group_members VALUES (:g, :m, 'shopify', :p, 0)"),
                {"g": group_id, "m": merchant_id, "p": product_id},
            )

    async def fetch_all(query, values=None):
        if broken:
            raise RuntimeError("db down")
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text(query), values or {}).mappings().all()]

    async def fetch_one(query, values=None):
        rows = await fetch_all(query, values)
        return rows[0] if rows else None

    monkeypatch.setattr(db_database.database, "fetch_all", fetch_all)
    monkeypatch.setattr(db_database.database, "fetch_one", fetch_one)


def _ok(context: agent_auth.AgentContext = Depends(agent_auth.get_agent_context)):
    return {"ok": True, "agent_id": context.agent_id}


# Every route a checkout-token prefix used to reach, real templates (see the drift test below).
ALLOWED = [
    ("POST", "/agent/v1/quotes/preview"),
    ("POST", "/agent/v1/orders/create"),
    ("POST", "/agent/v1/orders/payment-offer-evidence"),
    ("POST", "/agent/v1/payments"),
    ("POST", "/agent/v1/orders/{order_id}/confirm-payment"),
    ("GET", "/agent/v1/orders/{order_id}/track"),
    ("GET", "/agent/v1/checkout/prefill"),
    # The /agent/v2 twins the gateway's token-only invoke operations call.
    ("POST", "/agent/v2/quotes/preview"),
    ("POST", "/agent/v2/orders"),
    ("POST", "/agent/v2/payments/checkout-sessions"),
    ("GET", "/agent/v2/orders/{order_id}/tracking"),
    # Read-only catalog lookups of the gateway's offer rewrite, bound to the token's own items.
    ("GET", "/agent/v1/product-groups/{product_group_id}"),
    ("GET", "/agent/v1/products/{merchant_id}/{product_id}"),
]
# Routes that carry the order id in the path and are checked in the auth dependency.
PATH_BOUND = [
    "/agent/v1/orders/{order_id}/confirm-payment",
    "/agent/v1/orders/{order_id}/track",
    "/agent/v2/orders/{order_id}/tracking",
]
DENIED = [
    ("GET", "/agent/v1/orders/events"),
    ("GET", "/agent/v1/orders"),
    ("GET", "/agent/v1/orders/{order_id}"),
    ("POST", "/agent/v1/orders/{order_id}/refund"),
    ("POST", "/agent/v1/orders/{order_id}/cancel"),
    ("POST", "/agent/v1/orders/{order_id}/reviews/invitations"),
    ("POST", "/agent/v1/buyers/merge"),
    ("POST", "/agent/v1/payments/authorize"),
    ("GET", "/agent/v1/payments/{payment_id}"),
    # v2 siblings of the allowlisted routes, and search: a token is not a catalog credential.
    ("GET", "/agent/v2/orders/{order_id}"),
    ("POST", "/agent/v2/orders/{order_id}/refunds"),
    ("GET", "/agent/v2/quotes/{quote_id}"),
    ("POST", "/agent/v2/products/search"),
    ("GET", "/agent/v2/merchants/capabilities"),
    ("GET", "/agent/v1/products/search"),
    # Other catalog reads stay closed: only the two the offer rewrite needs are open.
    ("GET", "/agent/v1/product-groups/resolve"),
    ("GET", "/agent/v1/product-groups/resolve-by-product-id"),
]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", SECRET)
    monkeypatch.delenv("AGENT_CHECKOUT_TOKEN_SECRET", raising=False)

    async def get_agent(agent_id):
        return {"agent_id": agent_id, "agent_name": "Agent", "is_active": True, "allowed_merchants": None}

    async def check_rate_limit(*_a, **_k):
        return True, 0, 100

    async def check_daily_quota(*_a, **_k):
        return True, 0, 1000

    async def update_agent_stats(*_a, **_k):
        return None

    async def get_order(order_id):
        return TOKEN_ORDERS.get(order_id)

    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    monkeypatch.setattr(agent_auth, "check_rate_limit", check_rate_limit)
    monkeypatch.setattr(agent_auth, "check_daily_quota", check_daily_quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", update_agent_stats)
    monkeypatch.setattr(orders_module, "get_order", get_order)
    _install_catalog(monkeypatch)

    app = FastAPI()
    router = APIRouter()
    # Order matters as in the real app: the literal /orders/events is registered before /orders/{id}.
    for method, path in DENIED + ALLOWED:
        router.add_api_route(path, _ok, methods=[method])
    app.include_router(router)
    return TestClient(app)


def _call(client: TestClient, method: str, path: str, token: str, **ids: str):
    url = path.format(
        order_id=ids.get("order_id", "ord_mine"),
        payment_id="pay_1",
        quote_id="q_1",
        product_group_id=ids.get("product_group_id", "grp_item"),
        merchant_id=ids.get("merchant_id", MERCHANT),
        product_id=ids.get("product_id", "p_item"),
    )
    return client.request(method, url, headers={"X-Checkout-Token": token}, json={})


@pytest.mark.parametrize("method,path", ALLOWED, ids=[f"{m} {p}" for m, p in ALLOWED])
def test_token_reaches_each_allowlisted_route_for_its_own_order(client, method, path) -> None:
    resp = _call(client, method, path, _mint())

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == AGENT_A


@pytest.mark.parametrize("order_id", ["ord_mine", "ord_mine_2"])
@pytest.mark.parametrize("path", PATH_BOUND)
def test_token_reaches_every_order_created_under_its_intent(client, path, order_id) -> None:
    method = "POST" if path.endswith("/confirm-payment") else "GET"

    resp = _call(client, method, path, _mint(), order_id=order_id)

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("path", ["/agent/v1/orders/{order_id}/track", "/agent/v2/orders/{order_id}/tracking"])
def test_token_minted_for_an_order_reaches_that_order(client, path) -> None:
    token = _mint(order_id="ord_resumed")

    resp = _call(client, "GET", path, token, order_id="ord_resumed")

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("method,path", DENIED, ids=[f"{m} {p}" for m, p in DENIED])
def test_token_is_refused_on_lists_events_refund_cancel_and_other_routes(client, method, path) -> None:
    resp = _call(client, method, path, _mint())

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Checkout token not authorized for this endpoint"


@pytest.mark.parametrize("order_id", ["ord_other_intent", "ord_api_key", "ord_b_same_intent", "ord_missing"])
@pytest.mark.parametrize("path", PATH_BOUND)
def test_token_is_refused_on_another_order(client, path, order_id) -> None:
    method = "POST" if path.endswith("/confirm-payment") else "GET"

    resp = _call(client, method, path, _mint(), order_id=order_id)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Checkout token not authorized for this order"


@pytest.mark.parametrize("scopes", [["checkout"], ["checkout", "full"], ["checkout", "agent_api"], None, []])
def test_every_checkout_token_is_scoped_whatever_its_scopes_claim(client, scopes) -> None:
    resp = _call(client, "GET", "/agent/v1/orders/events", _mint(scopes=scopes))

    assert resp.status_code == 403


def test_allowlist_matches_the_real_route_templates() -> None:
    """Every allowlisted (method, template) and every denied one in this file exists in the app.

    A renamed route would otherwise fall off the allowlist (breaking checkout) or the denied list
    here would test a route that no longer exists.
    """
    import routes.agent_api as agent_api
    import routes.agent_checkout_intents as intents
    import routes.agent_payment_sdk as payments
    import routes.agent_sdk_fixed as sdk_fixed
    import routes.agent_v2 as agent_v2
    import routes.quote_routes as quotes

    real = set()
    for router in (agent_api.router, intents.router, payments.router, quotes.router, agent_v2.router, sdk_fixed.router):
        for route in router.routes:
            for method in getattr(route, "methods", None) or ():
                real.add((method, route.path))

    assert set(agent_auth.CHECKOUT_TOKEN_ROUTES) == set(ALLOWED)
    assert set(ALLOWED) <= real
    assert set(DENIED) <= real
    path_bound = {("POST" if p.endswith("/confirm-payment") else "GET", p) for p in PATH_BOUND}
    assert {k for k, v in agent_auth.CHECKOUT_TOKEN_ROUTES.items() if v == "path"} == path_bound


def test_the_allowlist_holds_no_search_route() -> None:
    assert not [k for k in agent_auth.CHECKOUT_TOKEN_ROUTES if "search" in k[1]]


@pytest.mark.parametrize(
    "order,bound",
    [
        (TOKEN_ORDERS["ord_mine"], True),
        (TOKEN_ORDERS["ord_mine_2"], True),
        (TOKEN_ORDERS["ord_other_intent"], False),
        (TOKEN_ORDERS["ord_api_key"], False),
        (TOKEN_ORDERS["ord_b_same_intent"], False),
        (None, False),
        ({"order_id": "x", "agent_id": AGENT_A, "metadata": "not json"}, False),
    ],
)
def test_checkout_token_may_use_order(order, bound) -> None:
    payload = {"agent_id": AGENT_A, "intent_id": INTENT}

    assert agent_auth.checkout_token_may_use_order(payload, order) is bound


def test_token_with_no_intent_and_no_order_claim_is_bound_to_nothing() -> None:
    payload = {"agent_id": AGENT_A}
    order = {"order_id": "o", "agent_id": AGENT_A, "metadata": {"intent_id": ""}}

    assert agent_auth.checkout_token_may_use_order(payload, order) is False


# Handlers that take the order id in the body enforce the same binding after loading the order.


def _token_ctx(*, can_access: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=AGENT_A,
        allowed_merchants=[MERCHANT],
        can_access_merchant=lambda _m: can_access,
        checkout_token_payload={"agent_id": AGENT_A, "intent_id": INTENT},
    )


@pytest.fixture()
def handler_orders(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_api as agent_api
    import routes.agent_payment_sdk as payments

    async def get_order(order_id):
        order = TOKEN_ORDERS.get(order_id)
        return dict(order, payment_status="paid") if order else None

    async def update_order(order_id, _updates):
        return dict(TOKEN_ORDERS.get(order_id) or {})

    monkeypatch.setattr(agent_api, "get_order", get_order)
    monkeypatch.setattr(agent_api, "update_order", update_order)
    monkeypatch.setattr(payments, "get_order", get_order)
    monkeypatch.setattr("mvp.events.emit_best_effort", lambda **_k: None)
    return agent_api, payments


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", ["ord_other_intent", "ord_b_same_intent", "ord_missing"])
async def test_payments_refuses_a_token_on_another_order(handler_orders, order_id) -> None:
    _, payments = handler_orders
    req = payments.PaymentRequest(order_id=order_id, payment_method={"type": "card"})

    with pytest.raises(HTTPException) as exc:
        await payments.create_payment(req, BackgroundTasks(), context=_token_ctx(), x_hil_approval=None)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Checkout token not authorized for this order"


@pytest.mark.asyncio
async def test_payments_lets_a_token_through_on_its_own_order(handler_orders) -> None:
    _, payments = handler_orders
    req = payments.PaymentRequest(order_id="ord_mine", payment_method={"type": "card"})

    with pytest.raises(HTTPException) as exc:
        await payments.create_payment(req, BackgroundTasks(), context=_token_ctx(), x_hil_approval=None)

    # Past the binding check: the next guard (order already paid) answers.
    assert exc.value.status_code == 400
    assert exc.value.detail == "Order already paid"


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", ["ord_other_intent", "ord_b_same_intent", "ord_missing"])
async def test_payment_offer_evidence_refuses_a_token_on_another_order(handler_orders, order_id) -> None:
    agent_api, _ = handler_orders
    from models.order import RecordPaymentOfferEvidenceRequest

    with pytest.raises(HTTPException) as exc:
        await agent_api.agent_record_payment_offer_evidence(
            RecordPaymentOfferEvidenceRequest(order_id=order_id), context=_token_ctx()
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Checkout token not authorized for this order"


@pytest.mark.asyncio
async def test_payment_offer_evidence_lets_a_token_through_on_its_own_order(handler_orders) -> None:
    agent_api, _ = handler_orders
    from models.order import RecordPaymentOfferEvidenceRequest

    with pytest.raises(HTTPException) as exc:
        await agent_api.agent_record_payment_offer_evidence(
            RecordPaymentOfferEvidenceRequest(order_id="ord_mine"), context=_token_ctx(can_access=False)
        )

    # Past the binding check: the merchant check answers.
    assert exc.value.status_code == 403
    assert exc.value.detail == "Not authorized for this merchant"


def test_api_key_callers_are_not_touched_by_the_order_binding() -> None:
    ctx = SimpleNamespace(agent_id=AGENT_A)

    agent_auth.ensure_checkout_token_order_access(ctx, TOKEN_ORDERS["ord_other_intent"])
    agent_auth.ensure_checkout_token_order_access(ctx, None)


@pytest.mark.asyncio
async def test_intent_minted_for_an_order_binds_its_token_to_that_order(monkeypatch) -> None:
    import routes.agent_checkout_intents as intents

    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", SECRET)

    async def execute(*_a, **_k):
        return None

    monkeypatch.setattr(intents.database, "execute", execute)
    ctx = SimpleNamespace(agent_id=AGENT_A, can_access_merchant=lambda _m: True)
    req = intents.CreateCheckoutIntentRequest(
        items=[{"product_id": "p1", "merchant_id": MERCHANT, "quantity": 1}],
        order_id="ord_resumed",
    )

    out = await intents.create_checkout_intent(req, context=ctx, agent_user=None)

    claims = agent_auth.verify_checkout_token_claims(out["checkout_token"])
    assert claims["order_id"] == "ord_resumed"
    assert claims["intent_id"] == out["intent_id"]


# /agent/v2: checkout-session creation takes the order id in the body; order create stamps the intent.


@pytest.fixture()
def v2_handler(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_v2 as agent_v2

    async def get_order(order_id):
        return TOKEN_ORDERS.get(order_id)

    async def past_the_binding(*_a, **_k):
        raise HTTPException(status_code=418, detail="past the binding check")

    monkeypatch.setattr(agent_v2, "get_order", get_order)
    monkeypatch.setattr(agent_v2, "validate_request_compat", past_the_binding)
    return agent_v2


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", ["ord_other_intent", "ord_api_key", "ord_b_same_intent", "ord_missing"])
async def test_v2_checkout_session_refuses_a_token_on_another_order(v2_handler, order_id) -> None:
    body = v2_handler.CreateCheckoutSessionBody(order_id=order_id)

    with pytest.raises(HTTPException) as exc:
        await v2_handler.create_checkout_session_v2(body, context=_token_ctx(), agent_user=None)

    # 403 for a missing order too: a token cannot tell which order ids exist.
    assert exc.value.status_code == 403
    assert exc.value.detail == "Checkout token not authorized for this order"


@pytest.mark.asyncio
@pytest.mark.parametrize("order_id", ["ord_mine", "ord_mine_2"])
async def test_v2_checkout_session_lets_a_token_through_on_its_own_order(v2_handler, order_id) -> None:
    body = v2_handler.CreateCheckoutSessionBody(order_id=order_id)

    with pytest.raises(HTTPException) as exc:
        await v2_handler.create_checkout_session_v2(body, context=_token_ctx(), agent_user=None)

    assert exc.value.status_code == 418


@pytest.mark.asyncio
async def test_v2_checkout_session_is_unchanged_for_api_key_callers(v2_handler) -> None:
    body = v2_handler.CreateCheckoutSessionBody(order_id="ord_missing")
    ctx = SimpleNamespace(agent_id=AGENT_A)

    with pytest.raises(HTTPException) as exc:
        await v2_handler.create_checkout_session_v2(body, context=ctx, agent_user=None)

    assert exc.value.status_code == 404


def _v2_order_harness(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_v2 as agent_v2
    from services.quote_service import QuoteSnapshot

    async def load_quote(self: Any, *, quote_id: str) -> QuoteSnapshot:
        now = datetime.now(timezone.utc)
        return QuoteSnapshot(
            quote_id=quote_id,
            merchant_id=MERCHANT,
            agent_id=AGENT_A,
            expires_at=now + timedelta(minutes=10),
            status="active",
            engine="shopify_storefront_cart",
            engine_ref="cart_1",
            request_fingerprint="fp",
            request_json={"items": [{"product_id": "p1", "variant_id": "v1", "quantity": 1}]},
            snapshot_json={"currency": "USD"},
            quote_hash_sha256="h",
            debug_id="d",
        )

    captured: Dict[str, Any] = {}

    async def create_order(**kwargs: Any) -> Dict[str, Any]:
        captured["metadata"] = kwargs["order_request"].metadata
        captured["context"] = kwargs["context"]
        return {"status": "success", "order_id": "ord_mine"}

    async def get_order(order_id):
        return dict(TOKEN_ORDERS.get(order_id) or {}, items=[], currency="USD", status="pending")

    monkeypatch.setattr(agent_v2.QuoteService, "load_active_quote_or_raise", load_quote)
    monkeypatch.setattr(agent_v2, "agent_v1_create_order", create_order)
    monkeypatch.setattr(agent_v2, "get_order", get_order)
    body = agent_v2.CreateOrderBody(
        quote_id="q_1",
        buyer_context={
            "customer_email": "buyer@example.com",
            "shipping_address": {
                "name": "Buyer",
                "address_line1": "1 Main St",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
        },
        metadata={"intent_id": OTHER_INTENT},
    )
    return agent_v2, body, captured


@pytest.mark.asyncio
async def test_v2_order_create_under_a_token_carries_the_token_intent_not_the_body_one(monkeypatch) -> None:
    agent_v2, body, captured = _v2_order_harness(monkeypatch)
    ctx = _token_ctx()

    await agent_v2.create_order_v2(body, BackgroundTasks(), context=ctx, agent_user=None)

    assert captured["metadata"]["intent_id"] == INTENT
    assert captured["context"] is ctx


@pytest.mark.asyncio
async def test_v2_order_create_by_an_api_key_keeps_its_metadata(monkeypatch) -> None:
    agent_v2, body, captured = _v2_order_harness(monkeypatch)
    ctx = SimpleNamespace(agent_id=AGENT_A)

    await agent_v2.create_order_v2(body, BackgroundTasks(), context=ctx, agent_user=None)

    assert captured["metadata"]["intent_id"] == OTHER_INTENT



# Read-only catalog lookups (the gateway's offer rewrite): only what the token's own checkout concerns.

GROUP_ROUTE = "/agent/v1/product-groups/{product_group_id}"
PRODUCT_ROUTE = "/agent/v1/products/{merchant_id}/{product_id}"


@pytest.mark.parametrize("group_id", ["grp_item"])
def test_token_reads_the_product_group_of_its_own_item(client, group_id) -> None:
    resp = _call(client, "GET", GROUP_ROUTE, _mint(), product_group_id=group_id)

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("group_id", ["grp_unrelated", "grp_foreign", "grp_missing"])
def test_token_is_refused_a_product_group_its_items_are_not_in(client, group_id) -> None:
    resp = _call(client, "GET", GROUP_ROUTE, _mint(), product_group_id=group_id)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Checkout token not authorized for this product"


@pytest.mark.parametrize("product_id", ["p_item", "p_item_alt"])
def test_token_reads_its_item_and_its_merchant_s_other_listings_in_that_group(client, product_id) -> None:
    resp = _call(client, "GET", PRODUCT_ROUTE, _mint(), merchant_id=MERCHANT, product_id=product_id)

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "merchant_id,product_id",
    [
        (MERCHANT, "p_unrelated"),        # its merchant, another group
        (MERCHANT, "p_nowhere"),          # its merchant, in no group
        ("merch_other", "p_other_seller"),  # same group, another merchant
        ("merch_other", "p_item"),        # its item id under another merchant
        ("external_seed", "p_item"),
    ],
)
def test_token_is_refused_other_products(client, merchant_id, product_id) -> None:
    resp = _call(client, "GET", PRODUCT_ROUTE, _mint(), merchant_id=merchant_id, product_id=product_id)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Checkout token not authorized for this product"


@pytest.mark.parametrize("route,ids", [
    (GROUP_ROUTE, {"product_group_id": "grp_item"}),
    (PRODUCT_ROUTE, {"merchant_id": MERCHANT, "product_id": "p_item_alt"}),
])
@pytest.mark.parametrize("claims", [
    {"items": []},
    {"items": None},
    {"merchant_ids": []},
    {"merchant_ids": ["merch_other"]},
    {"items": [{"merchant_id": "merch_other", "product_id": "p_item"}]},
])
def test_a_token_whose_items_bind_nothing_reads_nothing(client, route, ids, claims) -> None:
    resp = _call(client, "GET", route, _mint(**claims), **ids)

    assert resp.status_code == 403


@pytest.mark.parametrize("route,ids", [
    (GROUP_ROUTE, {"product_group_id": "grp_item"}),
    (PRODUCT_ROUTE, {"merchant_id": MERCHANT, "product_id": "p_item_alt"}),
])
def test_a_catalog_lookup_failure_refuses(client, monkeypatch, route, ids) -> None:
    _install_catalog(monkeypatch, broken=True)

    resp = _call(client, "GET", route, _mint(), **ids)

    assert resp.status_code == 403


def test_the_token_s_own_item_needs_no_catalog_lookup(client, monkeypatch) -> None:
    _install_catalog(monkeypatch, broken=True)

    resp = _call(client, "GET", PRODUCT_ROUTE, _mint(), merchant_id=MERCHANT, product_id="p_item")

    assert resp.status_code == 200, resp.text


def test_catalog_binding_reads_at_most_a_bounded_number_of_items() -> None:
    items = [{"merchant_id": MERCHANT, "product_id": f"p{i}"} for i in range(100)]
    pairs = agent_auth._checkout_token_item_products({"merchant_ids": [MERCHANT], "items": items})

    assert len(pairs) == agent_auth._CHECKOUT_TOKEN_MAX_CATALOG_ITEMS


# A token that mints a new intent (v2 checkout-sessions) never outlives itself.


def _intent_req(intents):
    return intents.CreateCheckoutIntentRequest(
        items=[{"product_id": "p1", "merchant_id": MERCHANT, "quantity": 1}],
        order_id="ord_resumed",
    )


@pytest.fixture()
def intents_module(monkeypatch: pytest.MonkeyPatch):
    import routes.agent_checkout_intents as intents

    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("CHECKOUT_TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("CHECKOUT_INTENT_TTL_SECONDS", "3600")
    inserted: List[Any] = []

    async def execute(query, *_a, **_k):
        inserted.append(query)
        return None

    monkeypatch.setattr(intents.database, "execute", execute)
    return intents


@pytest.mark.asyncio
async def test_a_token_re_minted_under_a_token_expires_no_later_than_its_parent(intents_module) -> None:
    import time as _time

    parent_exp = int(_time.time()) + 120
    ctx = SimpleNamespace(
        agent_id=AGENT_A,
        can_access_merchant=lambda _m: True,
        checkout_token_payload={"agent_id": AGENT_A, "intent_id": INTENT, "exp": parent_exp},
    )

    out = await intents_module.create_checkout_intent(_intent_req(intents_module), context=ctx, agent_user=None)

    claims = agent_auth.verify_checkout_token_claims(out["checkout_token"])
    assert claims["exp"] <= parent_exp
    assert claims["parent_exp"] == parent_exp
    assert out["expires_at"] <= parent_exp


@pytest.mark.asyncio
async def test_a_chain_of_re_mints_only_shrinks(intents_module) -> None:
    import time as _time

    exp = int(_time.time()) + 300
    for _ in range(3):
        ctx = SimpleNamespace(
            agent_id=AGENT_A,
            can_access_merchant=lambda _m: True,
            checkout_token_payload={"agent_id": AGENT_A, "intent_id": INTENT, "exp": exp},
        )
        out = await intents_module.create_checkout_intent(_intent_req(intents_module), context=ctx, agent_user=None)
        next_exp = agent_auth.verify_checkout_token_claims(out["checkout_token"])["exp"]
        assert next_exp <= exp
        exp = next_exp


@pytest.mark.asyncio
async def test_an_api_key_caller_mints_a_full_lifetime_token(intents_module) -> None:
    import time as _time

    before = int(_time.time())
    ctx = SimpleNamespace(agent_id=AGENT_A, can_access_merchant=lambda _m: True)

    out = await intents_module.create_checkout_intent(_intent_req(intents_module), context=ctx, agent_user=None)

    claims = agent_auth.verify_checkout_token_claims(out["checkout_token"])
    assert claims["exp"] >= before + 3600
    assert "parent_exp" not in claims
    assert out["expires_at"] >= before + 3600


def test_mint_exp_cap_is_a_ceiling(monkeypatch) -> None:
    import time as _time
    from routes.agent_checkout_intents import mint_checkout_token

    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", SECRET)
    cap = int(_time.time()) + 10

    capped = agent_auth.verify_checkout_token_claims(mint_checkout_token({"agent_id": AGENT_A}, 3600, exp_cap=cap))
    uncapped = agent_auth.verify_checkout_token_claims(mint_checkout_token({"agent_id": AGENT_A}, 60, exp_cap=cap + 3600))

    assert capped["exp"] == cap
    assert uncapped["exp"] <= int(_time.time()) + 60
