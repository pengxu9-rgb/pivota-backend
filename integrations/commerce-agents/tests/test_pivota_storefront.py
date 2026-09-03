"""The backend against a scripted transport shaped like the gateway's records:
ids carry the merchant, a record with option-bearing variants is a family, the
cart refuses what the blueprint says it must, and checkout hands off one hosted
page per merchant with the variant the buyer chose."""

from __future__ import annotations

import json
from typing import Any

import pytest
from shopping_agent.backend import NotOffered, Unavailable
from shopping_agent.types import OrderStatus, SearchFilters

from pivota_storefront import (
    PivotaShoppingSession,
    PivotaStorefrontBackend,
    ProductRef,
    ToolCallError,
    decode_product_id,
    encode_product_id,
)
from pivota_storefront.transport import unwrap_tool_result

MERCHANT = "merch_c5e24a8d3738d73b"
SEARCH_ROW = {
    "id": "sig_615cde705e4be2eaf7eea5f25b391728",
    "product_id": "sig_615cde705e4be2eaf7eea5f25b391728",
    "merchant_id": "merch_obs_e644ed0256549e83",
    "merchant_name": "Good Molecules",
    "platform": "external_seed",
    "title": "Niacinamide Serum",
    "description": "Promote smooth, even skin.",
    "price": 6,
    "currency": "USD",
    "image_url": "https://cdn.example/n.jpg",
    "availability": "in_stock",
    "in_stock": True,
    "category_path": ["beauty", "skincare", "treat", "serum"],
    "brand": "Good Molecules",
}
FAMILY_RECORD = {
    "product_id": "9854988910809",
    "merchant_id": MERCHANT,
    "title": "Trail Tee",
    "brand": "ACME",
    "description": "A tee for trails.",
    "price": 24.0,
    "currency": "USD",
    "image_url": "https://cdn.example/tee.jpg",
    "variants": [
        {"variant_id": "v1", "title": "S", "price": 24.0, "inventory_quantity": 12, "options": {"size": "S"}},
        {"variant_id": "v2", "title": "M", "price": 24.0, "inventory_quantity": 0, "options": {"size": "M"}},
        {"variant_id": "v3", "title": "L", "price": 26.0, "inventory_quantity": 4, "options": {"size": "L"}},
    ],
}
PLAIN_RECORD = {
    "product_id": "111",
    "merchant_id": MERCHANT,
    "title": "Mug",
    "price": 9.5,
    "in_stock": True,
    "variants": [{"variant_id": "only", "price": 9.5, "inventory_quantity": 3, "options": {}}],
}


class ScriptedTransport:
    """Answers each tool from a queue per tool name and records every call."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], *, bearer: str | None = None) -> dict[str, Any]:
        self.calls.append((name, arguments, bearer))
        queue = self.script.get(name)
        if not queue:
            raise AssertionError(f"unscripted tool call: {name}")
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def session(**extra: Any) -> PivotaShoppingSession:
    return PivotaShoppingSession(session_id="sess-1", user_id="buyer-1", **extra)


# -- ids -------------------------------------------------------------------------


def test_ids_round_trip_and_reject_what_is_not_ours():
    family = ProductRef(MERCHANT, "9854988910809")
    variant = ProductRef(MERCHANT, "sig_abc", "v1")
    assert decode_product_id(encode_product_id(family)) == family
    assert decode_product_id(encode_product_id(variant)) == variant
    assert decode_product_id(encode_product_id(variant)).family == ProductRef(MERCHANT, "sig_abc")
    assert decode_product_id("prod::a::b::c") is None  # no merchant half
    assert decode_product_id("merch/") is None
    assert decode_product_id("merch/p#") is None
    assert decode_product_id(None) is None  # type: ignore[arg-type]


# -- search ----------------------------------------------------------------------


async def test_search_maps_rows_to_plain_products_whose_ids_carry_the_merchant():
    transport = ScriptedTransport({"search_catalog": [{"products": [SEARCH_ROW], "total": 1}]})
    backend = PivotaStorefrontBackend(transport)
    results = await backend.search_products(
        session(), "niacinamide serum", SearchFilters(max_price=10, category="serum"), limit=5
    )
    assert len(results) == 1
    product = results[0]
    assert product.product_id == "merch_obs_e644ed0256549e83/sig_615cde705e4be2eaf7eea5f25b391728"
    assert product.title == "Niacinamide Serum" and product.price == 6.0 and product.in_stock
    assert product.category == "beauty/skincare/treat/serum"
    assert product.attributes["merchant"] == "Good Molecules"
    assert not product.has_options
    name, args, _ = transport.calls[0]
    assert name == "search_catalog"
    assert args == {"query": "niacinamide serum", "page_size": 5, "currency": "USD", "category": "serum", "price_max": 10.0}


async def test_search_scoped_to_one_merchant_passes_it_and_clamps_the_page():
    transport = ScriptedTransport({"search_catalog": [{"products": []}]})
    backend = PivotaStorefrontBackend(transport, merchant_id=MERCHANT, max_page_size=10)
    assert await backend.search_products(session(), "tee", None, limit=99) == []
    _, args, _ = transport.calls[0]
    assert args["merchant_id"] == MERCHANT and args["page_size"] == 10


# -- details ---------------------------------------------------------------------


async def test_details_build_a_family_from_option_bearing_variants():
    transport = ScriptedTransport({"get_product": [FAMILY_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    family_id = f"{MERCHANT}/9854988910809"
    family = await backend.get_product_details(session(), family_id)
    assert family is not None
    assert family.product_id == family_id and family.has_options
    assert family.options == {"size": ["S", "M", "L"]}
    assert [v.product_id for v in family.variants] == [f"{family_id}#v1", f"{family_id}#v2", f"{family_id}#v3"]
    assert [v.in_stock for v in family.variants] == [True, False, True]
    assert family.price == 24.0  # lowest in-stock variant
    assert family.in_stock
    assert all(v.variant_of == family_id for v in family.variants)
    assert family.variants[2].option_values == {"size": "L"} and family.variants[2].price == 26.0
    _, args, _ = transport.calls[0]
    assert args == {"merchant_id": MERCHANT, "product_id": "9854988910809"}


async def test_a_variant_id_returns_that_variant_without_a_second_fetch():
    transport = ScriptedTransport({"get_product": [FAMILY_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    s = session()
    family_id = f"{MERCHANT}/9854988910809"
    await backend.get_product_details(s, family_id)
    variant = await backend.get_product_details(s, f"{family_id}#v3")
    assert variant is not None
    assert variant.product_id == f"{family_id}#v3" and variant.variant_of == family_id
    assert variant.option_values == {"size": "L"} and variant.price == 26.0
    assert variant.long_description == "A tee for trails."
    assert len(transport.calls) == 1
    assert await backend.get_product_details(s, f"{family_id}#nope") is None


async def test_details_treat_optionless_variants_as_a_plain_product():
    transport = ScriptedTransport({"get_product": [PLAIN_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    product = await backend.get_product_details(session(), f"{MERCHANT}/111")
    assert product is not None and not product.has_options and product.variants == []
    assert product.price == 9.5


async def test_details_on_an_unknown_or_refused_id_is_none():
    transport = ScriptedTransport(
        {"get_product": [ToolCallError("no such product", code="UNKNOWN_PRODUCT_ID", retriable=False)]}
    )
    backend = PivotaStorefrontBackend(transport)
    assert await backend.get_product_details(session(), "not-an-id") is None
    assert transport.calls == []
    assert await backend.get_product_details(session(), f"{MERCHANT}/404") is None


async def test_details_reraise_a_transient_refusal():
    transport = ScriptedTransport(
        {"get_product": [ToolCallError("merchant unreachable", code="MERCHANT_UNAVAILABLE", retriable=True)]}
    )
    backend = PivotaStorefrontBackend(transport)
    with pytest.raises(ToolCallError):
        await backend.get_product_details(session(), f"{MERCHANT}/1")


# -- disclosure ------------------------------------------------------------------


async def test_disclosure_is_authored_from_the_decision_block():
    record = {
        **FAMILY_RECORD,
        "decision": {
            "why_it_stands_out": ["Breathable knit", {"text": "Flat seams"}],
            "best_for": ["trail runs"],
            "evidence_profile": "grounded_verified",
        },
    }
    transport = ScriptedTransport({"get_product": [record]})
    backend = PivotaStorefrontBackend(transport)
    family_id = f"{MERCHANT}/9854988910809"
    disclosure = await backend.get_disclosure(session(), family_id)
    assert disclosure is not None
    assert disclosure.title == "Pivota Insights" and disclosure.product_id == family_id
    assert [(r.label, r.value) for r in disclosure.rows] == [
        ("Why it stands out", "Breathable knit"),
        ("Why it stands out", "Flat seams"),
        ("Best for", "trail runs"),
        ("Evidence profile", "grounded_verified"),
    ]
    _, args, _ = transport.calls[0]
    assert args["include"] == ["decision"]


async def test_no_decision_block_means_no_disclosure():
    transport = ScriptedTransport({"get_product": [FAMILY_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    assert await backend.get_disclosure(session(), f"{MERCHANT}/9854988910809") is None


# -- cart ------------------------------------------------------------------------


async def test_cart_refuses_a_family_and_names_its_in_stock_variants():
    transport = ScriptedTransport({"get_product": [FAMILY_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    family_id = f"{MERCHANT}/9854988910809"
    with pytest.raises(Unavailable) as excinfo:
        await backend.add_to_cart(session(), family_id, 1)
    message = str(excinfo.value)
    assert f"{family_id}#v1" in message and f"{family_id}#v3" in message
    assert f"{family_id}#v2" not in message  # out of stock is not offered
    assert (await backend.get_cart(session())).items == []


async def test_cart_takes_a_variant_and_refuses_an_out_of_stock_one():
    transport = ScriptedTransport({"get_product": [FAMILY_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    s = session()
    family_id = f"{MERCHANT}/9854988910809"
    cart = await backend.add_to_cart(s, f"{family_id}#v3", 2)
    assert [(i.product_id, i.quantity, i.price, i.option_values, i.variant_of) for i in cart.items] == [
        (f"{family_id}#v3", 2, 26.0, {"size": "L"}, family_id)
    ]
    with pytest.raises(Unavailable) as excinfo:
        await backend.add_to_cart(s, f"{family_id}#v2", 1)
    assert "in-stock variants" in str(excinfo.value) and f"{family_id}#v1" in str(excinfo.value)
    # A second add of the same line adds up; update and remove act on the line.
    cart = await backend.add_to_cart(s, f"{family_id}#v3", 1)
    assert cart.items[0].quantity == 3
    cart = await backend.update_cart_item(s, f"{family_id}#v3", 1)
    assert cart.items[0].quantity == 1
    cart = await backend.remove_from_cart(s, f"{family_id}#v3")
    assert cart.items == []
    assert len(transport.calls) == 1  # the family was fetched once for the session


async def test_cart_returns_copies_so_a_caller_cannot_edit_it_in_place():
    transport = ScriptedTransport({"get_product": [PLAIN_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    s = session()
    cart = await backend.add_to_cart(s, f"{MERCHANT}/111", 1)
    cart.items[0].quantity = 99
    assert (await backend.get_cart(s)).items[0].quantity == 1


async def test_cart_refuses_an_id_that_is_not_ours():
    backend = PivotaStorefrontBackend(ScriptedTransport({}))
    with pytest.raises(Unavailable):
        await backend.add_to_cart(session(), "9854988910809", 1)


# -- checkout handoff --------------------------------------------------------------


async def test_checkout_hands_off_one_hosted_page_per_merchant_with_the_chosen_variant():
    other = "merch_other"
    transport = ScriptedTransport(
        {
            "get_product": [FAMILY_RECORD, {**PLAIN_RECORD, "merchant_id": other}],
            "create_checkout_session": [
                {"checkout_session": {"checkout_session_id": "cs_1", "total": 52.0}},
                {"session_id": "cs_2"},
            ],
            "create_payment_link": [
                {"checkout_url": "https://checkout.stripe.com/c/pay/cs_1"},
                {"checkout_session": {"hosted_url": "https://checkout.stripe.com/c/pay/cs_2"}},
            ],
        }
    )
    backend = PivotaStorefrontBackend(transport)
    s = session(customer_email="buyer@example.com", bearer_token="tok")
    family_id = f"{MERCHANT}/9854988910809"
    await backend.add_to_cart(s, f"{family_id}#v3", 2)
    await backend.add_to_cart(s, f"{other}/111", 1)
    cart = await backend.get_cart(s)

    handoffs = await backend.checkout_handoff(s, cart)
    assert [(h.url, h.seller) for h in handoffs] == [
        ("https://checkout.stripe.com/c/pay/cs_1", MERCHANT),
        ("https://checkout.stripe.com/c/pay/cs_2", other),
    ]
    created = [(a, b) for (n, a, b) in transport.calls if n == "create_checkout_session"]
    assert created[0][0]["quote"] == {
        "merchant_id": MERCHANT,
        "items": [{"product_id": "9854988910809", "quantity": 2, "variant_id": "v3"}],
        "customer_email": "buyer@example.com",
    }
    assert created[1][0]["quote"]["items"] == [{"product_id": "111", "quantity": 1}]
    assert all(bearer == "tok" for _, bearer in created)
    links = [a for (n, a, _) in transport.calls if n == "create_payment_link"]
    assert links[0]["session_id"] == "cs_1" and links[0]["customer_email"] == "buyer@example.com"
    assert links[1]["session_id"] == "cs_2"
    assert links[0]["idempotency_key"] == created[0][0]["idempotency_key"] + "-link"
    assert len(created[0][0]["idempotency_key"]) >= 8

    # The same cart hands off again with the same keys: a replay, not a second order.
    transport.script["create_checkout_session"] = [{"session_id": "cs_1"}, {"session_id": "cs_2"}]
    transport.script["create_payment_link"] = [{"url": "https://x/1"}, {"url": "https://x/2"}]
    await backend.checkout_handoff(s, cart)
    created_again = [a for (n, a, _) in transport.calls if n == "create_checkout_session"][2:]
    assert [c["idempotency_key"] for c in created_again] == [c["idempotency_key"] for c, _ in created]


async def test_checkout_without_an_email_leaves_the_host_route_in_place():
    transport = ScriptedTransport({"get_product": [PLAIN_RECORD]})
    backend = PivotaStorefrontBackend(transport)
    s = session()
    await backend.add_to_cart(s, f"{MERCHANT}/111", 1)
    assert await backend.checkout_handoff(s, await backend.get_cart(s)) == []
    assert [n for (n, _, _) in transport.calls] == ["get_product"]


async def test_checkout_refuses_a_link_that_is_not_https():
    transport = ScriptedTransport(
        {
            "get_product": [PLAIN_RECORD],
            "create_checkout_session": [{"session_id": "cs_1"}],
            "create_payment_link": [{"checkout_url": "http://insecure/pay"}],
        }
    )
    backend = PivotaStorefrontBackend(transport)
    s = session(customer_email="b@example.com")
    await backend.add_to_cart(s, f"{MERCHANT}/111", 1)
    with pytest.raises(ToolCallError):
        await backend.checkout_handoff(s, await backend.get_cart(s))


# -- orders, policies, fulfillment --------------------------------------------------


async def test_get_order_maps_the_record_and_prefers_fulfillment_over_payment_status():
    record = {
        "order": {
            "order_id": "ord_1",
            "merchant_id": MERCHANT,
            "payment_status": "paid",
            "fulfillment_status": "fulfilled",
            "created_at": "2026-09-01T10:00:00Z",
            "currency": "USD",
            "amounts": {"total": 52.0},
            "line_items": [
                {"product_id": "9854988910809", "variant_id": "v3", "title": "Trail Tee", "quantity": 2, "price": 26.0, "options": {"size": "L"}}
            ],
            "tracking_url": "https://track.example/1",
        }
    }
    transport = ScriptedTransport({"get_order": [record]})
    backend = PivotaStorefrontBackend(transport)
    order = await backend.get_order(session(), "ord_1")
    assert order is not None
    assert order.status is OrderStatus.SHIPPED and order.total == 52.0
    assert order.placed_at.isoformat() == "2026-09-01T10:00:00+00:00"
    item = order.items[0]
    assert item.product_id == f"{MERCHANT}/9854988910809#v3" and item.variant_of == f"{MERCHANT}/9854988910809"
    assert item.option_values == {"size": "L"}
    assert order.tracking_url == "https://track.example/1"


async def test_get_order_not_found_is_none_and_listing_is_empty():
    transport = ScriptedTransport({"get_order": [ToolCallError("nope", code="ORDER_NOT_FOUND", retriable=False)]})
    backend = PivotaStorefrontBackend(transport)
    assert await backend.get_order(session(), "ord_x") is None
    assert await backend.get_orders(session()) == []
    assert await backend.search_policies(session(), "returns") == []
    with pytest.raises(NotOffered):
        await backend.get_fulfillment_options(session(), [f"{MERCHANT}/111"])


# -- transport unwrapping ------------------------------------------------------------


def test_unwrap_reads_the_gateway_envelope_and_raises_on_is_error():
    envelope = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"products": [1]})}]}}
    assert unwrap_tool_result(envelope) == {"products": [1]}
    refused = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps({"code": "UNKNOWN_PRODUCT_ID", "message": "no", "retriable": False})}],
        },
    }
    with pytest.raises(ToolCallError) as excinfo:
        unwrap_tool_result(refused)
    assert excinfo.value.code == "UNKNOWN_PRODUCT_ID" and excinfo.value.retriable is False
    with pytest.raises(ToolCallError) as rpc:
        unwrap_tool_result({"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "Tool not found"}})
    assert rpc.value.code == "-32602"


async def test_http_transport_posts_json_rpc_and_sends_the_bearer():
    import httpx

    from pivota_storefront import HttpMcpTransport

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpMcpTransport("https://door.example/mcp", bearer="door-token", client=client)
        assert await transport.call_tool("search_catalog", {"query": "x"}, bearer="buyer-token") == {"ok": True}
    assert seen["headers"]["authorization"] == "Bearer buyer-token"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {"name": "search_catalog", "arguments": {"query": "x"}}


async def test_http_transport_reads_an_event_stream_answer():
    import httpx

    from pivota_storefront import HttpMcpTransport

    frame = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"n": 1})}]}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"event: message\ndata: {frame}\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpMcpTransport("https://door.example/mcp", client=client)
        assert await transport.call_tool("get_order", {"order_id": "o"}) == {"n": 1}
