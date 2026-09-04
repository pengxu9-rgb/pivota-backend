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

import httpx
import pytest

from services import merchant_ucp_checkout as muc
from services.merchant_ucp_checkout import MerchantUcpError

PROFILE = "https://ucp.pivota.cc/.well-known/ucp-agent"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", PROFILE)
    monkeypatch.setenv("MERCHANT_UCP_CHECKOUT_ENABLED", "1")


class _Capture:
    """Records the exact JSON body posted (and how the client was constructed), and replays a
    canned RPC response with a chosen HTTP status.

    `real_hostname_guard=True` leaves `validate_merchant_domain` ALONE. The default identity stub
    is a convenience for the wire-shape tests, but it also means those tests prove nothing about
    the guard — the transport tests below turn it back on, which is the only way a mutant that
    deletes the call can be seen. The DNS half stays stubbed either way: it is a live-network
    dependency, not the thing under test, and it keeps its own coverage in
    tests/test_agent_card_issuance.py.
    """

    def __init__(self, rpc, status=200):
        self.rpc = rpc
        self.status = status
        self.body = None
        self.url = None
        self.client_kwargs = None

    def install(self, monkeypatch, *, real_hostname_guard=False):
        capture = self

        class _Resp:
            # `headers` is set per instance, not as a class attribute: a shared mutable would
            # leak between the two POSTs of a single hop. And it is a real `httpx.Headers`, not
            # a dict — production looks the key up case-insensitively, so a plain lowercase dict
            # would let a `"location"` -> `"Location"` mutant look killed when prod is fine.
            status_code = capture.status

            def __init__(self):
                self.headers = httpx.Headers({})

            def json(self):
                return capture.rpc

        class _Client:
            def __init__(self, *a, **k):
                capture.client_kwargs = k

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                capture.url = url
                capture.body = json
                return _Resp()

        monkeypatch.setattr(muc.httpx, "AsyncClient", _Client)
        if not real_hostname_guard:
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
    # OUR misconfiguration — our agent profile, our discovery handshake. This used to be flagged
    # `caller_fault`, which the route maps to 422: the agent was told ITS request was invalid
    # because OUR profile was unreachable, and went debugging a request that was fine. `our_fault`
    # is the 502-with-a-generic-detail path, and the two must not be conflated again.
    assert excinfo.value.our_fault is True
    assert excinfo.value.caller_fault is False


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
@pytest.mark.parametrize("unset", [None, "", "   "])
async def test_an_unset_profile_falls_back_to_the_one_we_actually_SERVE(monkeypatch, unset):
    """REPLACES a refusal, deliberately.

    The old behaviour refused the whole mint when `UCP_AGENT_PROFILE_URL` was unset, and put the
    variable's NAME in the 502 body — so one forgotten variable took the rail down, and told an
    external caller a piece of our configuration in exchange for nothing it could act on. There
    is a profile we serve; it is the default. The env var still overrides for a second serving
    host or a staging profile, which the test below pins.
    """
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    if unset is None:
        monkeypatch.delenv("UCP_AGENT_PROFILE_URL", raising=False)
    else:
        monkeypatch.setenv("UCP_AGENT_PROFILE_URL", unset)

    await muc.get_checkout("cosrx.com", "chk_1")

    assert _args(cap)["meta"]["ucp-agent"]["profile"] == (
        "https://ucp.pivota.cc/.well-known/ucp-agent"
    )


@pytest.mark.asyncio
async def test_the_profile_env_var_still_overrides_the_default(monkeypatch):
    """The default must not become a hardcode: a second serving host (mcp.pivota.cc) or a
    staging profile is exactly what the variable is for."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", "https://mcp.pivota.cc/.well-known/ucp-agent")

    await muc.get_checkout("cosrx.com", "chk_1")

    assert _args(cap)["meta"]["ucp-agent"]["profile"] == (
        "https://mcp.pivota.cc/.well-known/ucp-agent"
    )


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


@pytest.mark.asyncio
async def test_an_isError_payload_whose_own_status_says_ERROR_is_a_refusal(monkeypatch):
    """THE DANGEROUS SIBLING of the test above, and the reason `ucp.status` is the admission test.

    The first cut returned any dict carrying `messages` or `ucp`. That admits this payload — the
    same envelope, `ucp.status: "error"` — and this envelope still carries `totals`, which is
    exactly what `resolve_merchant_quote` reads. Returned rather than raised, it mints a REAL,
    SPENDABLE card capped against a checkout the merchant has already declined. A card cannot be
    un-issued; a payload that says it failed is a refusal.
    """
    payload = {
        "ucp": {"version": "2026-04-08", "status": "error"},
        "messages": [{"type": "error", "code": "out_of_stock", "content": "prose we do not echo"}],
        "currency": "USD",
        "totals": [{"type": "total", "amount": 2317}],
    }
    _Capture(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": True,
            },
        }
    ).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.create_checkout("judydoll.com", line_items=[{"variant_id": "1", "quantity": 1}])

    # The merchant's machine-readable reason survives; its prose does not.
    assert "out_of_stock" in str(excinfo.value)
    assert "prose we do not echo" not in str(excinfo.value)
    assert excinfo.value.caller_fault is True


@pytest.mark.asyncio
async def test_a_merchant_ERROR_payload_never_becomes_a_quote(monkeypatch):
    """The same defect at the OTHER end, driven through the real `resolve_merchant_quote`.

    Both ends are fixed because either one alone leaves the hole open: the transport only sees
    the isError flavour, and a merchant that sets `ucp.status: "error"` WITHOUT isError reaches
    the quote reader untouched. `_pick_total` would have found 2317 in this payload and capped a
    card on it. Refused BEFORE the totals are read.
    """
    from services.agent_card_issuance import MerchantQuoteError, resolve_merchant_quote

    payload = {
        "ucp": {"version": "2026-04-08", "status": "error"},
        "messages": [{"type": "error", "code": "checkout_expired"}],
        "currency": "USD",
        "totals": [{"type": "total", "amount": 2317}],
    }
    _Capture(_ok(payload)).install(monkeypatch)

    with pytest.raises(MerchantQuoteError) as excinfo:
        await resolve_merchant_quote("cosrx.com", "chk_1")

    # NOT the caller's fault: their request may have been perfectly well formed and the merchant
    # still declined. 502 points them at the merchant instead of at their own body.
    assert excinfo.value.caller_fault is False


@pytest.mark.asyncio
async def test_a_success_status_payload_still_quotes_normally(monkeypatch):
    """The POSITIVE counterpart: the new check must not refuse the shape it was aimed past.

    Without this, deleting the totals-reading entirely would look like a pass.
    """
    from services.agent_card_issuance import resolve_merchant_quote

    payload = {
        "ucp": {"version": "2026-04-08", "status": "success"},
        "currency": "USD",
        "totals": [{"type": "total", "amount": 2317}],
    }
    _Capture(_ok(payload)).install(monkeypatch)

    quote = await resolve_merchant_quote("cosrx.com", "chk_1")
    assert quote["total_minor"] == 2317 and quote["currency"] == "USD"


def test_message_codes_reads_codes_and_never_content():
    assert muc.message_codes({"messages": [{"code": "a"}, {"code": "b"}]}) == ["a", "b"]
    # Shapes that name no code are skipped, not guessed at or stringified.
    assert muc.message_codes({"messages": [{"content": "x"}, "not-a-dict", {"code": ""}]}) == []
    assert muc.message_codes({}) == []


# --------------------------------------------------------------------------------------
# The transport guards — each one had a surviving mutant
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redirects_are_not_followed(monkeypatch):
    """A 30x to somewhere else is not a merchant door — and following one would take the two
    guards above with it, since they validated the ORIGINAL host."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc.get_checkout("cosrx.com", "chk_1")

    assert cap.client_kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_a_redirect_is_surfaced_as_a_failure_not_followed_into_a_payload(monkeypatch):
    """The response body here is a PERFECTLY GOOD checkout payload. If the status check went
    away, this test would happily receive it — which is the mutant that survived."""
    _Capture(_ok({"id": "chk_1"}), status=302).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("cosrx.com", "chk_1")
    assert "302" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_merchant_5xx_is_surfaced(monkeypatch):
    _Capture(_ok({"id": "chk_1"}), status=503).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("cosrx.com", "chk_1")
    assert "503" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["127.0.0.1", "shop.example.com:8443", "localhost", "svc.internal", "shop.example.com/evil"],
)
async def test_the_REAL_hostname_guard_runs_before_any_request(monkeypatch, bad):
    """Driven through the REAL `validate_merchant_domain`, which every other test in this file
    stubs to identity — so deleting the call from the transport survived the whole suite. The
    host is caller input that we fetch; this is the SSRF surface, not a formatting nicety.

    These are the cases the HOSTNAME guard alone must catch. The exotic literals it deliberately
    passes (`0x7f.0.0.1`, `127.1`, `localtest.me`) are the DNS guard's job — one mechanism each,
    and `resolves_only_public` keeps its coverage in tests/test_agent_card_issuance.py.
    """
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch, real_hostname_guard=True)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout(bad, "chk_1")

    assert cap.body is None, "nothing may reach the wire"
    assert excinfo.value.caller_fault is True


@pytest.mark.asyncio
async def test_the_REAL_hostname_guard_still_admits_a_normal_merchant(monkeypatch):
    """The positive counterpart: a guard that refused everything would pass the row above."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch, real_hostname_guard=True)

    await muc.get_checkout("Shop.Example.COM.", "chk_1")

    assert cap.url == "https://shop.example.com/api/ucp/mcp", "normalized, then pinned"


@pytest.mark.asyncio
async def test_update_checkout_is_write_gated_too(monkeypatch):
    """`create_checkout`'s gate had a test; `update_checkout`'s did not, and deleting it
    survived. Both are mutations of a real merchant's checkout."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)
    monkeypatch.delenv("MERCHANT_UCP_CHECKOUT_ENABLED", raising=False)

    with pytest.raises(MerchantUcpError):
        await muc.update_checkout(
            "cosrx.com", "chk_1", line_items=[{"variant_id": "4242", "quantity": 1}]
        )
    assert cap.body is None, "a write must not reach the merchant with the flag off"


# --------------------------------------------------------------------------------------
# The tool allowlist — the money hop is refused structurally, not by docstring
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["complete_checkout", "cancel_checkout", "delete_everything", ""])
async def test_only_allowlisted_tools_reach_the_wire(monkeypatch, tool):
    """`complete_checkout` is the MONEY hop and is deliberately unbuilt — the credential question
    is unsettled. The module docstring said so, but a docstring does not stop a call: it is one
    string away from every send site here. Refused before any I/O, so widening it is a decision
    rather than a diff."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc._call_tool("cosrx.com", tool, {"meta": muc.build_meta()})

    assert cap.body is None, "an unlisted tool must not produce a request at all"
    # OURS, not the caller's: no external request can name a tool, so this is our bug.
    assert excinfo.value.our_fault is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_checkout", "create_checkout", "update_checkout",
                                  "search_catalog", "lookup_catalog", "get_product"])
async def test_the_allowlisted_tools_do_go_through(monkeypatch, tool):
    """The positive counterpart — an allowlist that refused everything would pass the row above,
    and would silently break the three send sites in this module."""
    cap = _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    await muc._call_tool("cosrx.com", tool, {"meta": muc.build_meta()})

    assert cap.body["params"]["name"] == tool


def test_the_transport_is_private_to_this_module():
    """Renamed from `call_tool`: every send site is in this file, next to the allowlist and the
    two SSRF guards. A public name invites an import that inherits none of those rules."""
    assert not hasattr(muc, "call_tool")


# --------------------------------------------------------------------------------------
# Attribution: the referring domain lands in the MERCHANT'S order record
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "https://agent.pivota.cc",          # a URL, not a hostname
        "agent.pivota.cc/path",             # a path
        "agent.pivota.cc:8443",             # a port
        "localhost",
        "127.0.0.1",
        "not a domain at all",
        "-bad.example.com",
    ],
)
def test_an_unusable_referring_domain_falls_back_to_the_default(bad):
    """It is a read-only snapshot on the merchant's order and a reconciliation key on ours. An
    unattributable order is recoverable; a mis-attributed one is not, so anything that is not a
    bare hostname is replaced rather than sent."""
    assert muc.build_attribution(None, referring_domain=bad)["referring_domain"] == "agent.pivota.cc"


def test_a_real_referring_domain_is_kept_and_normalized():
    """The positive counterpart: a validator that refused everything would pass the row above,
    and would silently pin every order to the default."""
    out = muc.build_attribution(None, referring_domain="Shop.Example.COM.")
    assert out["referring_domain"] == "shop.example.com"
    assert muc.build_attribution(None)["referring_domain"] == "agent.pivota.cc"


# --------------------------------------------------------------------------------------
# The ONE redirect we follow: apex <-> www
#
# A merchant whose door lives on `www` answers the apex with a 301. Refusing every 30x recorded
# those merchants as dead: robinsons.com.sg 301s to www.robinsons.com.sg and scored
# `search_failed / HTTP 301` in the 2026-09-04 SG sweep, while the www host prices a real cart.
# The fix must stay narrow — a redirect target is merchant-controlled input, so every test below
# that widens the hop also asserts the hop is REFUSED when it leaves the sibling shape.
# --------------------------------------------------------------------------------------


class _Redirector:
    """Replays a per-URL script: {url: (status, headers, rpc)}. Records every (url, body) posted,
    in order, so a test can prove a second hop happened, prove it went where it should, AND prove
    it carried the same bytes as the first."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    @property
    def urls(self):
        return [u for u, _ in self.calls]

    @property
    def bodies(self):
        return [b for _, b in self.calls]

    def install(self, monkeypatch, *, resolves=lambda d: True, real_hostname_guard=True):
        outer = self

        class _Resp:
            def __init__(self, status, headers, rpc):
                self.status_code = status
                # real httpx.Headers: case-insensitive, exactly like production
                self.headers = httpx.Headers(headers)
                self._rpc = rpc

            def json(self):
                return self._rpc

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                outer.calls.append((url, json))
                if url not in outer.script:
                    raise AssertionError(f"unscripted POST to {url}")
                entry = outer.script[url]
                if isinstance(entry, BaseException):
                    raise entry
                return _Resp(*entry)

        monkeypatch.setattr(muc.httpx, "AsyncClient", _Client)
        if not real_hostname_guard:
            monkeypatch.setattr(muc, "validate_merchant_domain", lambda d: d)
        monkeypatch.setattr(muc, "resolves_only_public", resolves)
        return outer


APEX = "https://robinsons.com.sg/api/ucp/mcp"
WWW = "https://www.robinsons.com.sg/api/ucp/mcp"


@pytest.mark.asyncio
async def test_apex_to_www_redirect_is_followed_once_and_returns_the_payload(monkeypatch):
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WWW], "must hop exactly once, to the www sibling"


@pytest.mark.asyncio
async def test_www_to_apex_redirect_is_followed_too(monkeypatch):
    """The sibling relation runs both ways — a corpus may hold either host."""
    r = _Redirector({
        WWW: (301, {"location": APEX}, None),
        APEX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("www.robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [WWW, APEX]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location, why",
    [
        ("https://evil.example.com/api/ucp/mcp", "a different registrable domain"),
        ("https://shop.robinsons.com.sg/api/ucp/mcp", "a different subdomain, not the sibling"),
        ("http://www.robinsons.com.sg/api/ucp/mcp", "downgraded to http"),
        ("https://www.robinsons.com.sg/evil", "off the pinned path"),
        ("https://www.robinsons.com.sg:8443/api/ucp/mcp", "a port of its own"),
        ("", "no Location at all"),
    ],
)
async def test_any_other_redirect_is_still_refused(monkeypatch, location, why):
    """Each of these serves a PERFECTLY GOOD checkout payload at the redirect target. If the
    sibling check went away, the caller would happily fetch it — that is the whole SSRF surface
    the guards close, so every one must still surface as a failure."""
    r = _Redirector({
        APEX: (301, {"location": location}, None),
        # scripted so a wrong follow reaches a real payload rather than an AssertionError
        "https://evil.example.com/api/ucp/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://shop.robinsons.com.sg/api/ucp/mcp": (200, {}, _ok({"id": "pwned"})),
        "http://www.robinsons.com.sg/api/ucp/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://www.robinsons.com.sg/evil": (200, {}, _ok({"id": "pwned"})),
        "https://www.robinsons.com.sg:8443/api/ucp/mcp": (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "301" in str(excinfo.value), why
    assert r.urls == [APEX], f"must not have fetched the redirect target: {why}"


@pytest.mark.asyncio
async def test_the_sibling_host_is_put_through_the_public_address_guard(monkeypatch):
    """The sibling is merchant-controlled input like any other host. A 301 pointing at a name
    that resolves somewhere private must be refused, not trusted because a redirect named it."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch, resolves=lambda d: not d.startswith("www."))

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "301" in str(excinfo.value)
    assert r.urls == [APEX], "the guard must run BEFORE the second fetch"


@pytest.mark.asyncio
async def test_the_hop_is_not_a_chain(monkeypatch):
    """One hop only. A sibling that redirects again is a loop, not a door."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (301, {"location": APEX}, None),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "301" in str(excinfo.value)
    assert r.urls == [APEX, WWW], "exactly two fetches, then stop"


@pytest.mark.asyncio
async def test_a_redirect_cannot_smuggle_a_disallowed_tool_because_nothing_is_fetched(
    monkeypatch,
):
    """The allowlist is checked before ANY I/O, so the redirect path cannot be a way in.

    The `assert r.calls == []` is the point of this test and the reason it is not just a copy of
    `test_only_allowlisted_tools_reach_the_wire`: without it the test passed on the exception
    alone, which `_Redirector` would also raise for an unscripted URL — a pass that proved
    nothing about ordering. Scripting BOTH hosts removes that accident, so the empty call list
    is the only thing left that can fail.
    """
    r = _Redirector(
        {
            APEX: (301, {"location": WWW}, None),
            WWW: (200, {}, _ok({"id": "pwned"})),
        }
    ).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc._call_tool("robinsons.com.sg", "complete_checkout", {})

    assert "complete_checkout" in str(excinfo.value)
    assert r.calls == [], "the allowlist must refuse before a single request is made"


# --------------------------------------------------------------------------------------
# Follow-ups from the review of #2044. Each test below kills a mutant that survived that
# commit's own suite — i.e. behaviour the code had but nothing held.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_retry_url_is_rebuilt_and_never_taken_from_the_location(monkeypatch):
    """THE property the whole SSRF argument rests on, and #2044 shipped without binding it.

    Every test in that commit used a Location byte-identical to the URL we rebuild, so
    `client.post(resp.headers["location"])` passed all 69 of them. Here the Location carries
    userinfo and a query string: if the header were fetched verbatim the request would go to
    the decorated URL (and `_Redirector` would raise on it as unscripted). Asserting the clean
    rebuilt URL is what makes "the merchant chooses whether we hop, never where" a tested claim.
    """
    decorated = "https://user:pw@www.robinsons.com.sg/api/ucp/mcp?ref=evil#frag"
    r = _Redirector({
        APEX: (301, {"location": decorated}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WWW], "the retry must go to the REBUILT url, not the Location"


@pytest.mark.asyncio
async def test_the_retry_carries_the_identical_body(monkeypatch):
    """This file's stated purpose is to assert the bytes on the wire; the hop's bytes were not.
    A mutant retrying with `json={}` passed #2044's whole suite."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    await muc.get_checkout("robinsons.com.sg", "chk_1")

    first, second = r.bodies
    assert second == first, "the hop must replay the same request, not a fresh one"
    assert second["params"]["arguments"]["meta"], "and it must still carry meta"
    assert second["params"]["name"] == "get_checkout"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_every_apex_to_www_redirect_status_is_followed(monkeypatch, status):
    """Only 301 was bound, so narrowing the branch to `== 301` survived. 302/307/308 are all
    ordinary apex<->www shapes — exactly the merchants this lane exists to stop scoring dead."""
    r = _Redirector({
        APEX: (status, {"location": WWW}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WWW]


@pytest.mark.asyncio
async def test_a_303_is_refused_because_the_write_already_happened(monkeypatch):
    """303 See Other means the POST WAS PROCESSED — re-POSTing is how you build the merchant a
    second cart. It is a 3xx, so the old `300 <= code < 400` bound would have re-sent it."""
    r = _Redirector({
        APEX: (303, {"location": WWW}, None),
        WWW: (200, {}, _ok({"id": "duplicate-cart"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "303" in str(excinfo.value)
    assert r.urls == [APEX], "a 303 must never be re-POSTed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 503])
async def test_a_location_on_a_non_redirect_status_is_never_followed(monkeypatch, status):
    """Widening the bound to `>= 200` or `< 500` survived #2044's suite, because every
    non-redirect test had an empty header dict and short-circuited on the missing Location."""
    r = _Redirector({
        APEX: (status, {"location": WWW}, _ok({"id": "apex"})),
        WWW: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    if status == 200:
        got = await muc.get_checkout("robinsons.com.sg", "chk_1")
        assert got["id"] == "apex"
    else:
        with pytest.raises(MerchantUcpError) as excinfo:
            await muc.get_checkout("robinsons.com.sg", "chk_1")
        assert str(status) in str(excinfo.value)
    assert r.urls == [APEX], "a Location header outside a redirect status means nothing"


@pytest.mark.asyncio
async def test_a_trailing_dot_fqdn_location_is_normalised_and_followed(monkeypatch):
    """`urlsplit` lowercases a hostname but does NOT strip the root dot, so the `.rstrip(".")`
    is load-bearing — yet dropping it survived. A fully-qualified `www.host.` is legal."""
    r = _Redirector({
        APEX: (301, {"location": "https://www.robinsons.com.sg./api/ucp/mcp"}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WWW], "the dot-stripped sibling is what we rebuild"


@pytest.mark.asyncio
async def test_the_sibling_is_refused_when_the_hostname_guard_rejects_it(monkeypatch):
    """Deleting `validate_merchant_domain(sibling) == sibling` survived #2044's suite: every
    test there used a sibling that guard accepts. The sibling of `www.com` is the bare TLD
    `com`, which it rejects — a single-label intranet-shaped name is precisely what the guard
    exists to refuse, and without it we would POST to `https://com/api/ucp/mcp`."""
    apex_com = "https://www.com/api/ucp/mcp"
    bare_tld = "https://com/api/ucp/mcp"
    assert muc.validate_merchant_domain("com") is None, "premise: the guard rejects a bare TLD"

    r = _Redirector({
        apex_com: (301, {"location": bare_tld}, None),
        bare_tld: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)  # real hostname guard by default

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("www.com", "chk_1")

    assert "301" in str(excinfo.value)
    assert r.urls == [apex_com], "the hostname guard must run BEFORE the second fetch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location, why",
    [
        ("https://[oops/api/ucp/mcp", "urlsplit raises Invalid IPv6 URL"),
        ("https://www.robinsons.com.sg:abc/api/ucp/mcp", "the port cannot be cast to int"),
    ],
)
async def test_a_malformed_location_is_refused_not_raised(monkeypatch, location, why):
    """Both `except ValueError` branches were reachable but unexecuted. A merchant must not be
    able to turn a redirect into an unhandled exception in our transport."""
    r = _Redirector({APEX: (301, {"location": location}, None)}).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "301" in str(excinfo.value), why
    assert r.urls == [APEX]


@pytest.mark.asyncio
async def test_a_root_location_on_the_sibling_is_refused_today(monkeypatch):
    """A DELIBERATE call, pinned so it stays deliberate.

    The retry URL is rebuilt, so the path check buys no security — it bounds when we spend a
    second request. Every apex<->www redirect seen in the 2026-09-03 and 2026-09-04 sweeps was
    path-preserving, so we hop only on that shape. If a real merchant ever redirects to root,
    change `_sibling_host` and this test together, on evidence.
    """
    r = _Redirector({
        APEX: (301, {"location": "https://www.robinsons.com.sg/"}, None),
        WWW: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "301" in str(excinfo.value)
    assert r.urls == [APEX]


@pytest.mark.asyncio
async def test_a_failure_after_the_hop_names_the_host_that_actually_failed(monkeypatch):
    """An operator reading `domain=robinsons.com.sg: ConnectTimeout` curls the apex, gets an
    instant 301, and concludes the log is lying. The failure belongs to the host we fetched."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (503, {}, None),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    msg = str(excinfo.value)
    assert "503" in msg
    assert "www.robinsons.com.sg" in msg, "the error must name the hopped host, not the apex"
    assert r.urls == [APEX, WWW]


@pytest.mark.asyncio
async def test_a_transport_error_after_the_hop_is_logged_against_the_hopped_host(
    monkeypatch,
):
    """Same attribution, on the exception path rather than the status path.

    The logger call is captured directly rather than through `caplog`: this repo's logger does
    not propagate to the root, so a caplog assertion here would pass vacuously on an empty
    record list no matter what the code logged.
    """
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: httpx.ConnectError("boom"),
    }).install(monkeypatch)
    warnings = []
    monkeypatch.setattr(
        muc.logger, "warning", lambda fmt, *a: warnings.append(fmt % a)
    )

    with pytest.raises(MerchantUcpError):
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert r.urls == [APEX, WWW]
    assert len(warnings) == 1
    assert "host=www.robinsons.com.sg" in warnings[0], (
        "the warning must carry the host we actually fetched"
    )
    assert "domain=robinsons.com.sg" in warnings[0], (
        "and still name the host the caller asked for"
    )
