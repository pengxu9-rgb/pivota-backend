"""The merchant-side UCP checkout caller, pinned to a LIVE-PROBED wire shape.

PROVENANCE. Every `required` array and argument name asserted here was read out of a live
`tools/list` against cosrx's own UCP door on 2026-08-31 (endpoint discovered from
`https://cosrx.com/.well-known/ucp` -> `cosrx-renewal.myshopify.com/api/ucp/mcp`, UCP version
`2026-08-25`). The raw response is not committed; re-probe with:

    curl -s -L https://cosrx.com/.well-known/ucp
    curl -s -X POST https://cosrx.com/api/ucp/mcp -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

WHY THIS FILE EXISTS AT ALL. The card rail shipped a get_checkout body with no `meta` and the
argument named `checkout_id`, and every route test passed because they all stub
`resolve_merchant_quote`. A stub cannot fail a wire shape. So these tests assert the bytes on the
wire, and each one asserts the WRONG shape is refused too — a pin that only checks the right
answer lets the wrong one creep back beside it.
"""

from __future__ import annotations

import json

import pytest

from services import merchant_ucp_checkout as muc
from services.merchant_ucp_checkout import MerchantUcpError

PROFILE = "https://ucp.pivota.cc/.well-known/ucp-agent"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", PROFILE)
    monkeypatch.setenv("MERCHANT_UCP_CHECKOUT_ENABLED", "1")


class _Capture:
    """Records the exact JSON body posted, and replays a canned RPC response."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.body = None
        self.url = None

    def install(self, monkeypatch):
        capture = self

        class _Resp:
            status_code = 200

            def json(self):
                return capture.rpc

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                capture.url = url
                capture.body = json
                return _Resp()

        monkeypatch.setattr(muc.httpx, "AsyncClient", _Client)
        monkeypatch.setattr(muc, "validate_merchant_domain", lambda d: d)
        monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
        return capture


def _ok(payload):
    return {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": payload}}


def _args(capture):
    return capture.body["params"]["arguments"]


# --------------------------------------------------------------------------------------
# The probed shape
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_checkout_sends_the_probed_required_arguments(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.create_checkout(
        "cosrx.com", line_items=[{"variant_id": "4242", "quantity": 2}], click_id="clk_9"
    )

    assert cap.url == "https://cosrx.com/api/ucp/mcp"
    assert cap.body["method"] == "tools/call"
    assert cap.body["params"]["name"] == "create_checkout"
    args = _args(cap)
    # Live: create_checkout required == ["meta", "checkout"]
    assert set(args) == {"meta", "checkout"}
    # Live: meta required == ["ucp-agent"], ucp-agent required == ["profile"]
    assert args["meta"] == {"ucp-agent": {"profile": PROFILE}}
    # Live: checkout.required == ["line_items"]
    assert "line_items" in args["checkout"]


@pytest.mark.asyncio
async def test_get_checkout_argument_is_id_not_checkout_id(monkeypatch):
    """The exact defect the probe caught in the merged card rail."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.get_checkout("cosrx.com", "chk_1")

    args = _args(cap)
    # Live: get_checkout required == ["meta", "id"]
    assert set(args) == {"meta", "id"}
    assert args["id"] == "chk_1"
    assert "checkout_id" not in args, "the merchant's argument is `id`; `checkout_id` is refused"


@pytest.mark.asyncio
async def test_update_checkout_carries_id_at_top_level_and_items_in_checkout(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.update_checkout(
        "cosrx.com",
        "chk_1",
        line_items=[{"variant_id": "4242", "quantity": 1}],
        address={"address_country": "US", "postal_code": "94111"},
        line_item_ids=["gid://shopify/CartLine/l1?cart=c1"],
    )

    args = _args(cap)
    # Live: update_checkout required == ["meta", "checkout", "id"] — id is TOP level, not nested
    assert set(args) == {"meta", "checkout", "id"}
    assert args["id"] == "chk_1"
    assert "line_items" in args["checkout"], "line_items is required on update too"


@pytest.mark.asyncio
async def test_line_item_nests_the_variant_under_item_id(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.create_checkout("cosrx.com", line_items=[{"variant_id": "4242", "quantity": 3}])

    items = _args(cap)["checkout"]["line_items"]
    # Live: line item required == ["item", "quantity"], item required == ["id"] — and the id is
    # the ProductVariant GID (probed 2026-09-02: the bare number is refused as "not a valid
    # ProductVariant GID"). Every id we hold is the bare number, so the caller wraps it.
    assert items == [{"item": {"id": "gid://shopify/ProductVariant/4242"}, "quantity": 3}]
    assert "variant_id" not in items[0], "our field name must not reach the wire"


@pytest.mark.asyncio
async def test_a_variant_id_that_is_already_a_gid_is_sent_as_given(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    gid = "gid://shopify/ProductVariant/51086327742680"

    await muc.create_checkout("cosrx.com", line_items=[{"variant_id": gid, "quantity": 1}])

    assert _args(cap)["checkout"]["line_items"][0]["item"]["id"] == gid


def test_variant_gid_wraps_only_bare_numbers():
    assert muc._variant_gid("51086327742680") == "gid://shopify/ProductVariant/51086327742680"
    assert muc._variant_gid("gid://shopify/ProductVariant/1") == "gid://shopify/ProductVariant/1"
    assert muc._variant_gid("") == ""
    # Not numeric, not a GID: left alone for the merchant to name, never double-wrapped or guessed.
    assert muc._variant_gid("SKU-RED-30ML") == "SKU-RED-30ML"


@pytest.mark.asyncio
async def test_address_maps_onto_fulfillment_methods_destinations(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.update_checkout(
        "cosrx.com",
        "chk_1",
        line_items=[{"variant_id": "4242", "quantity": 1}],
        address={
            "street_address": "123 Main St",
            "address_locality": "San Francisco",
            "address_region": "CA",
            "postal_code": "94111",
            "address_country": "US",
        },
        line_item_ids=["gid://shopify/CartLine/l1?cart=c1"],
    )

    methods = _args(cap)["checkout"]["fulfillment"]["methods"]
    assert methods[0]["type"] == "shipping"
    # Live (2026-09-02): the method schema REQUIRES line_item_ids — the merchant's own CartLine
    # ids from the create response, not our variant ids.
    assert methods[0]["line_item_ids"] == ["gid://shopify/CartLine/l1?cart=c1"]
    dest = methods[0]["destinations"][0]
    assert dest["address_country"] == "US"
    assert dest["street_address"] == "123 Main St"
    # Not the flat/GA4 spellings a plausible guess would reach for.
    for invented in ("line1", "city", "state", "country", "zip"):
        assert invented not in dest


# --------------------------------------------------------------------------------------
# Attribution — the monetization bet
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_stamps_attribution_with_the_probed_field_names(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.create_checkout(
        "cosrx.com", line_items=[{"variant_id": "4242", "quantity": 1}], click_id="clk_9"
    )

    attribution = _args(cap)["checkout"]["attribution"]
    assert attribution["click_id_tag"] == "pvt_click_id"
    assert attribution["click_id_value"] == "clk_9"
    assert attribution["utm_source"] == "pivota"
    assert attribution["referring_domain"] == "agent.pivota.cc"
    # The tag/value PAIR is the merchant's shape; a bare `click_id` is not a field it declares.
    assert "click_id" not in attribution


@pytest.mark.asyncio
async def test_attribution_is_stamped_even_with_no_click_id(monkeypatch):
    """`order.attribution` snapshots the checkout's, so an unstamped create is unrecoverable.

    Origination evidence must not be contingent on having a click id — without this the whole
    take-rate case rests on a field we sometimes omit.
    """
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.create_checkout("cosrx.com", line_items=[{"variant_id": "4242", "quantity": 1}])

    attribution = _args(cap)["checkout"]["attribution"]
    assert attribution["utm_source"] == "pivota"
    assert "click_id_value" not in attribution


@pytest.mark.asyncio
async def test_payment_is_never_sent_on_create_or_update(monkeypatch):
    """Deliberate narrowing: the merchant's schema PERMITS payment here, we refuse to send it.

    Recorded as a choice so it does not read as an oversight, and so completing a checkout stays
    a decision someone makes rather than something this module can drift into.
    """
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.create_checkout("cosrx.com", line_items=[{"variant_id": "4242", "quantity": 1}])
    assert "payment" not in _args(cap)["checkout"]

    await muc.update_checkout(
        "cosrx.com", "chk_1", line_items=[{"variant_id": "4242", "quantity": 1}]
    )
    assert "payment" not in _args(cap)["checkout"]


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_jsonrpc_error_on_http_200_is_surfaced_not_read_as_empty(monkeypatch):
    """The live failure the merged rail mis-diagnosed, reproduced verbatim."""
    _Capture(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32001,
                "message": "UCP discovery failed",
                "data": {
                    "code": "invalid_profile_url",
                    "content": "Unable to fetch agent profile: Missing profile uri",
                },
            },
        }
    ).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("cosrx.com", "chk_1")

    assert "invalid_profile_url" in str(excinfo.value)
    assert excinfo.value.rpc_code == -32001
    # OUR misconfiguration, not the merchant being down — a 502 would send the caller hunting
    # the wrong system.
    assert excinfo.value.caller_fault is True


@pytest.mark.asyncio
async def test_text_content_payload_is_parsed_when_structured_is_absent(monkeypatch):
    cap = _Capture(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({"id": "chk_7"})}]},
        }
    ).install(monkeypatch)

    out = await muc.get_checkout("cosrx.com", "chk_7")
    assert out == {"id": "chk_7"}


@pytest.mark.asyncio
async def test_missing_profile_refuses_before_calling_the_merchant(monkeypatch):
    """A dead/absent pointer fails the whole call merchant-side; refuse locally instead."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    monkeypatch.delenv("UCP_AGENT_PROFILE_URL", raising=False)

    with pytest.raises(MerchantUcpError):
        await muc.get_checkout("cosrx.com", "chk_1")
    assert cap.body is None, "nothing may reach the merchant without a profile"


@pytest.mark.asyncio
async def test_write_ops_are_flag_gated_but_reads_are_not(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    monkeypatch.delenv("MERCHANT_UCP_CHECKOUT_ENABLED", raising=False)

    with pytest.raises(MerchantUcpError):
        await muc.create_checkout("cosrx.com", line_items=[{"variant_id": "4242"}])
    assert cap.body is None

    # get_checkout is a read the merged card rail already depends on — gating it here would
    # break a shipped path rather than protect one.
    await muc.get_checkout("cosrx.com", "chk_1")
    assert cap.body is not None


@pytest.mark.parametrize("bad", [{}, {"variant_id": ""}, {"variant_id": "4242", "quantity": 0}])
def test_line_items_without_usable_identity_are_refused(bad):
    """Rows with no storefront variant identity belong on the referral rail, not here."""
    with pytest.raises(MerchantUcpError) as excinfo:
        muc.build_line_items([bad])
    assert excinfo.value.caller_fault is True


def test_a_destination_without_a_country_cannot_be_priced():
    with pytest.raises(MerchantUcpError):
        muc.build_destination({"postal_code": "94111"})


@pytest.mark.asyncio
async def test_ssrf_guards_run_before_any_request(monkeypatch):
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: False)

    with pytest.raises(MerchantUcpError):
        await muc.get_checkout("cosrx.com", "chk_1")
    assert cap.body is None



@pytest.mark.asyncio
async def test_pricing_a_destination_without_line_item_ids_is_refused_before_the_wire(monkeypatch):
    """The merchant rejects a fulfillment method without line_item_ids; refusing here names the
    fix (carry create_checkout's line_items[].id) instead of shipping a request we know fails."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.update_checkout(
            "cosrx.com",
            "chk_1",
            line_items=[{"variant_id": "4242", "quantity": 1}],
            address={"address_country": "US", "postal_code": "94111"},
        )

    assert excinfo.value.caller_fault is True
    assert "line_item_ids" in str(excinfo.value)
    assert cap.body is None, "nothing must reach the merchant"


@pytest.mark.asyncio
async def test_a_tool_result_flagged_isError_is_surfaced_with_the_merchants_text(monkeypatch):
    """Reproduced live 2026-09-02: a schema violation comes back as a SUCCESSFUL tool call with
    result.isError=true and a plain-text chunk. Before this, that read as "carried no checkout
    payload" and the merchant's exact diagnosis was thrown away."""
    _Capture(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Invalid arguments: object at `/checkout/fulfillment/methods/0` "
                        "is missing required properties: line_item_ids",
                    }
                ],
                "isError": True,
            },
        }
    ).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("cosrx.com", "chk_1")

    assert "missing required properties: line_item_ids" in str(excinfo.value)
    assert excinfo.value.caller_fault is True


@pytest.mark.asyncio
async def test_an_isError_result_carrying_a_checkout_payload_is_returned_not_raised(monkeypatch):
    """Probed live 2026-09-02 on judydoll.com: create_checkout answers isError=true whose text is
    a full UCP payload (ucp.status "success") with the refusal in `messages[]` — the same shape
    a sold-out line comes back in without isError. Callers read `messages`; do not throw it away."""
    payload = {
        "ucp": {"version": "2026-04-08", "status": "success"},
        "messages": [{"type": "error", "code": "some_merchant_reason", "content": "explained here"}],
    }
    _Capture(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": True}}
    ).install(monkeypatch)

    out = await muc.create_checkout("judydoll.com", line_items=[{"variant_id": "1", "quantity": 1}])

    assert out["messages"][0]["code"] == "some_merchant_reason"
    assert out.get("id") is None
