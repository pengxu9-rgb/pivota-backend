"""Order create never takes the order's Pivota-account owner, or a checkout token's intent, from the caller.

`create_new_order` copies the caller's order metadata and fills the orders.buyer_id,
agent_scoped_buyer_ref and intent_id columns from it. Every agent entry point forwards caller
metadata (`/agent/v1/orders/create` directly, `/agent/v2/orders` and `/agent/v2/commerce/checkouts`
through it), so:
- a caller-set `buyer_id` put the order into that Pivota account's /buyer/v1/orders, across
  partners, and `agent_scoped_buyer_ref` named that account's pairwise ref;
- a caller-set `intent_id` on an API-key create made the order one that checkout token may read
  and pay for (routes.agent_auth.checkout_token_may_use_order reads metadata.intent_id).

Now `buyer_id` and `agent_scoped_buyer_ref` are server-only order metadata (only the buyer's own
save writes them), and only a checkout-token caller puts an intent on the order: its token's.
The server-set fields still land: the agent, the token's intent, the verified agent user ref and
the X-Buyer-Ref.

Harness: tests/order_create_one_per_quote_cases.py (the real routes over ASGI, the real
agent_create_order / create_new_order / db.orders.create_order, real order rows on SQLite).
All data is placeholder.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from order_create_one_per_quote_cases import (  # noqa: F401
    AGENT_A,
    MERCHANT,
    Answer,
    World,
    _Context,
    shipping_address,
    world,
)

FORGED_BUYER = "u_placeholder0000000000"
FORGED_SCOPED = "bref_placeholder_forged"
FORGED_INTENT = "ci_placeholder_forged"
TOKEN_INTENT = "ci_placeholder_token"
VERIFIED_REF = "https://issuer.placeholder.example:user_one"
HEADER_BUYER_REF = "guest_placeholder_1"

FORGED = {"buyer_id": FORGED_BUYER, "agent_scoped_buyer_ref": FORGED_SCOPED, "intent_id": FORGED_INTENT}


class _TokenContext(_Context):
    """A checkout-token caller of AGENT_A, as get_agent_context builds it from a valid token."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id)
        self.checkout_token_payload = {
            "agent_id": agent_id,
            "intent_id": TOKEN_INTENT,
            "merchant_ids": [MERCHANT],
            "scopes": ["checkout"],
            "items": [],
        }


async def _post(
    w: World,
    path: str,
    payload: Dict[str, Any],
    *,
    token: bool = False,
    agent_user_ref: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Answer:
    """POST through the real app as AGENT_A: an API-key caller, or a checkout-token caller."""
    import httpx

    from routes.agent_auth import get_agent_context
    from routes.agent_user_auth import AgentUserContext, get_agent_user_context

    async def context() -> _Context:
        return _TokenContext(AGENT_A) if token else _Context(AGENT_A)

    async def agent_user() -> Optional[AgentUserContext]:
        return AgentUserContext(agent_user_ref=agent_user_ref) if agent_user_ref else None

    w.app.dependency_overrides[get_agent_context] = context
    w.app.dependency_overrides[get_agent_user_context] = agent_user
    try:
        transport = httpx.ASGITransport(app=w.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(path, json=payload, headers=headers)
    finally:
        w.app.dependency_overrides.pop(get_agent_context, None)
        w.app.dependency_overrides.pop(get_agent_user_context, None)
    await w.settle()
    answer = Answer(response.status_code, response.json())
    assert answer.status == 200, answer.body
    return answer


def _v1(quote_id: str, w: World, metadata: Dict[str, Any]) -> Dict[str, Any]:
    q = w.quotes.quotes[quote_id]
    return {
        "merchant_id": MERCHANT,
        "customer_email": "buyer@example.com",
        "quote_id": quote_id,
        "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1}],
        "shipping_address": shipping_address(q["country"]),
        "currency": q["currency"],
        "metadata": {"market": q["market"], **metadata},
    }


def _v2(quote_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "quote_id": quote_id,
        "buyer_context": {"customer_email": "buyer@example.com", "buyer_ref": HEADER_BUYER_REF},
        "request_context": {"country": "US"},
        "metadata": metadata,
    }


async def _only_order(w: World, quote_id: str) -> Dict[str, Any]:
    rows = await w.orders_for(quote_id)
    assert len(rows) == 1, rows
    return rows[0]


def _assert_no_forged_owner(order: Dict[str, Any]) -> None:
    assert order.get("buyer_id") is None, order.get("buyer_id")
    assert order.get("agent_scoped_buyer_ref") is None, order.get("agent_scoped_buyer_ref")
    for key in ("buyer_id", "agent_scoped_buyer_ref"):
        assert key not in order["metadata"], sorted(order["metadata"])


def _assert_server_fields(order: Dict[str, Any]) -> None:
    assert order.get("agent_id") == AGENT_A
    assert order["metadata"].get("agent_id") == AGENT_A
    assert order.get("agent_user_ref") == VERIFIED_REF
    assert order["metadata"].get("agent_user_ref") == VERIFIED_REF
    assert order["metadata"].get("buyer_ref") == HEADER_BUYER_REF


# -- /agent/v1/orders/create ----------------------------------------------------------------------


async def test_v1_api_key_caller_sets_no_owner_and_no_intent(world: World) -> None:
    w = world
    quote_id = w.quotes.add()
    answer = await _post(
        w, "/agent/v1/orders/create", _v1(quote_id, w, FORGED),
        agent_user_ref=VERIFIED_REF, headers={"X-Buyer-Ref": HEADER_BUYER_REF},
    )
    assert answer.status == 200, answer.body
    order = await _only_order(w, quote_id)
    _assert_no_forged_owner(order)
    assert order.get("intent_id") is None
    assert "intent_id" not in order["metadata"]
    _assert_server_fields(order)


async def test_v1_token_caller_gets_its_tokens_intent_and_no_forged_owner(world: World) -> None:
    w = world
    quote_id = w.quotes.add()
    await _post(
        w, "/agent/v1/orders/create", _v1(quote_id, w, FORGED), token=True,
        agent_user_ref=VERIFIED_REF, headers={"X-Buyer-Ref": HEADER_BUYER_REF},
    )
    order = await _only_order(w, quote_id)
    _assert_no_forged_owner(order)
    assert order.get("intent_id") == TOKEN_INTENT
    assert order["metadata"].get("intent_id") == TOKEN_INTENT
    _assert_server_fields(order)


async def test_v1_caller_metadata_other_than_the_owner_keys_is_kept(world: World) -> None:
    w = world
    quote_id = w.quotes.add()
    answer = await _post(w, "/agent/v1/orders/create", _v1(quote_id, w, {**FORGED, "partner_note": "kept"}))
    assert answer.status == 200, answer.body
    order = await _only_order(w, quote_id)
    assert order["metadata"].get("partner_note") == "kept"
    assert order["metadata"].get("market") == "US"


# -- /agent/v2/orders -----------------------------------------------------------------------------


async def test_v2_api_key_caller_sets_no_owner_and_no_intent(world: World) -> None:
    w = world
    quote_id = w.quotes.add()
    answer = await _post(w, "/agent/v2/orders", _v2(quote_id, FORGED), agent_user_ref=VERIFIED_REF)
    assert answer.status == 200, answer.body
    order = await _only_order(w, quote_id)
    _assert_no_forged_owner(order)
    assert order.get("intent_id") is None
    assert "intent_id" not in order["metadata"]
    _assert_server_fields(order)


async def test_v2_token_caller_gets_its_tokens_intent_and_no_forged_owner(world: World) -> None:
    w = world
    quote_id = w.quotes.add()
    await _post(w, "/agent/v2/orders", _v2(quote_id, FORGED), token=True, agent_user_ref=VERIFIED_REF)
    order = await _only_order(w, quote_id)
    _assert_no_forged_owner(order)
    assert order.get("intent_id") == TOKEN_INTENT
    _assert_server_fields(order)


# -- /agent/v2/commerce/checkouts -----------------------------------------------------------------


@pytest.fixture
def commerce(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    """The commerce lane's own surroundings: an execute-ready merchant, its store, the ledger.

    The lane's request has no quote_id, and agent create always requires a live quote, so today
    the real create refuses it (QUOTE_REQUIRED_BEFORE_PURCHASE). The hand-off to the REAL
    agent_create_order is given a quote, so the lane's own metadata (`**body.metadata` first)
    still goes through the real v1 create, create_new_order and the order row.
    """
    import routes.agent_commerce as agent_commerce

    async def ready(_merchant_id: str) -> Dict[str, Any]:
        return {"execute_status": "ready", "primary_platform": "shopify"}

    async def store(_merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "store_id": "store_oopq"}

    async def record(**_kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_placeholder", "event_id": "evt_placeholder"}

    real_create = agent_commerce.agent_create_order
    world.commerce_quote_id = world.quotes.add()

    async def create_with_a_quote(*, order_request: Any, **kwargs: Any) -> Any:
        order_request.quote_id = world.commerce_quote_id
        return await real_create(order_request=order_request, **kwargs)

    monkeypatch.setattr(agent_commerce, "upsert_merchant_commerce_readiness_state", ready)
    monkeypatch.setattr(agent_commerce, "get_primary_store", store)
    monkeypatch.setattr(agent_commerce, "record_commerce_event", record)
    monkeypatch.setattr(agent_commerce, "agent_create_order", create_with_a_quote)
    return world


def _checkout(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "merchant_id": MERCHANT,
        "interaction_id": "int_placeholder",
        "customer_email": "buyer@example.com",
        "buyer_ref": HEADER_BUYER_REF,
        "shipping_address": {
            "name": "Fixture Buyer",
            "address_line1": "1 Example Street",
            "city": "Exampleton",
            "postal_code": "00001",
            "country": "US",
        },
        "items": [{"product_id": "prod_oopq_1", "variant_id": "var_oopq_1", "quantity": 1, "unit_price": 9.09}],
        "metadata": metadata,
    }


async def test_commerce_checkout_sets_no_owner_and_no_intent(commerce: World) -> None:
    w = commerce
    answer = await _post(w, "/agent/v2/commerce/checkouts", _checkout(FORGED), agent_user_ref=VERIFIED_REF)
    assert answer.status == 200, answer.body
    order = await _only_order(w, w.commerce_quote_id)
    _assert_no_forged_owner(order)
    assert order.get("intent_id") is None
    assert "intent_id" not in order["metadata"]
    _assert_server_fields(order)


# -- the strip list itself ------------------------------------------------------------------------


def test_the_owner_keys_are_server_only_order_metadata() -> None:
    from routes.order_routes import _SERVER_ONLY_ORDER_METADATA_KEYS, _strip_server_only_order_metadata

    assert {"buyer_id", "agent_scoped_buyer_ref"} <= set(_SERVER_ONLY_ORDER_METADATA_KEYS)
    metadata = {**FORGED, "agent_id": AGENT_A}
    _strip_server_only_order_metadata(metadata, merchant_id=MERCHANT)
    # intent_id is not server-only at this layer: a checkout-token create carries its token's.
    assert metadata == {"intent_id": FORGED_INTENT, "agent_id": AGENT_A}
