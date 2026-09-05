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

import json as _json

import httpx
import pytest

from services import merchant_ucp_checkout as muc
from services.merchant_ucp_checkout import MerchantUcpError

PROFILE = "https://ucp.pivota.cc/.well-known/ucp-agent"


class _Streamed:
    """Async context manager standing in for `httpx.AsyncClient.stream`, yielding the scripted
    body through `aiter_bytes()` in two chunks so a byte counter has something to count."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("UCP_AGENT_PROFILE_URL", PROFILE)
    monkeypatch.setenv("MERCHANT_UCP_CHECKOUT_ENABLED", "1")
    monkeypatch.setenv("MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED", "1")


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
                # The real RPC, serialised: `_read_bounded` rebuilds the body from the streamed
                # bytes, so a stub streaming a placeholder would lose the scripted response.
                try:
                    self.content = _json.dumps(capture.rpc).encode()
                except Exception:
                    self.content = b"{}"

            async def aiter_bytes(self):
                mid = max(1, len(self.content) // 2)
                yield self.content[:mid]
                yield self.content[mid:]

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

            def stream(self, method, url, *, json=None, headers=None):
                capture.url = url
                if json is not None:
                    capture.body = json
                return _Streamed(_Resp())

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
        self.calls = []  # (method, url, body)
        self.get_headers = []

    @property
    def urls(self):
        return [u for _, u, _ in self.calls]

    @property
    def bodies(self):
        return [b for m, _, b in self.calls if m == "POST"]

    @property
    def methods(self):
        return [m for m, _, _ in self.calls]

    def install(self, monkeypatch, *, resolves=lambda d: True, real_hostname_guard=True):
        outer = self

        class _Resp:
            def __init__(self, status, headers, rpc):
                self.status_code = status
                # real httpx.Headers: case-insensitive, exactly like production
                self.headers = httpx.Headers(headers)
                self._rpc = rpc
                # Real bytes, so the size cap in `_discover_endpoint` sees a real length. A stub
                # reporting b"" would make the cap untestable and always-passing.
                try:
                    self.content = _json.dumps(rpc).encode()
                except Exception:
                    self.content = b"{}"

            async def aiter_bytes(self):
                if isinstance(self._rpc, BaseException):
                    raise self._rpc
                mid = max(1, len(self.content) // 2)
                yield self.content[:mid]
                yield self.content[mid:]

            def json(self):
                # A real httpx response RAISES json.JSONDecodeError on a non-JSON body. A stub
                # that returns the raw string instead tests a different branch than the one it
                # is named for — so a scripted exception here is replayed.
                if isinstance(self._rpc, BaseException):
                    raise self._rpc
                return self._rpc

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                outer.calls.append(("POST", url, json))
                if url not in outer.script:
                    raise AssertionError(f"unscripted POST to {url}")
                entry = outer.script[url]
                if isinstance(entry, BaseException):
                    raise entry
                return _Resp(*entry)

            def stream(self, method, url, *, json=None, headers=None):
                """`_read_bounded` streams; the stub must too, or every read is unexercised."""
                if method == "GET":
                    outer.get_headers.append(headers)
                outer.calls.append((method, url, json))
                if url not in outer.script:
                    raise AssertionError(f"unscripted {method} to {url}")
                entry = outer.script[url]
                if isinstance(entry, BaseException):
                    raise entry
                return _Streamed(_Resp(*entry))

            async def get(self, url, headers=None):
                # `headers` is captured, not ignored: the discovery GET must send
                # `Accept-Encoding: identity`, and a stub that silently dropped it would let a
                # mutant removing that header pass.
                outer.get_headers.append(headers)
                outer.calls.append(("GET", url, None))
                if url not in outer.script:
                    raise AssertionError(f"unscripted GET to {url}")
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
WK = "https://robinsons.com.sg/.well-known/ucp"
WK_WWW = "https://www.robinsons.com.sg/.well-known/ucp"


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
    """Widening the hop bound to `>= 200` or `< 500` survived #2044's suite, because every
    non-redirect test had an empty header dict and short-circuited on the missing Location.

    A 403 additionally triggers ENDPOINT DISCOVERY (it is in `_DISCOVER_STATUSES`), so the
    well-known GET is scripted absent here — the point being that a Location header still never
    causes a sibling hop, whatever else the status sets in motion.
    """
    r = _Redirector({
        APEX: (status, {"location": WWW}, _ok({"id": "apex"})),
        WWW: (200, {}, _ok({"id": "pwned"})),
        WK: (404, {}, None),
    }).install(monkeypatch)

    if status == 200:
        got = await muc.get_checkout("robinsons.com.sg", "chk_1")
        assert got["id"] == "apex"
    else:
        with pytest.raises(MerchantUcpError) as excinfo:
            await muc.get_checkout("robinsons.com.sg", "chk_1")
        assert str(status) in str(excinfo.value)

    assert WWW not in r.urls, "a Location outside a redirect status must never be followed"
    assert [u for m, u, _ in r.calls if m == "POST"] == [APEX]


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


# --------------------------------------------------------------------------------------
# Endpoint discovery. The pinned `/api/ucp/mcp` is a SHOPIFY convention; Wix serves a real
# door at `https://www.wixapis.com/ecom/ucp/<siteId>/mcp` and answers the pinned path 403, so
# every Wix merchant was recorded as having no door. Discovery reads the merchant's profile
# and re-validates whatever it names — host-first, path deliberately unpinned.
# --------------------------------------------------------------------------------------

WIX = "https://www.wixapis.com/ecom/ucp/53b84487-site/mcp"


def _profile(endpoint, transport="mcp"):
    return {"ucp": {"version": "2026-04-08",
                    "services": {"dev.ucp.shopping": [
                        {"transport": transport, "endpoint": endpoint}]}}}


@pytest.mark.asyncio
async def test_a_wix_door_is_reached_through_discovery(monkeypatch):
    """The whole point: before this, `sgbeauty.com.sg` answered the pinned path 403 and was
    filed as having no door, while serving a perfectly good one on another host."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.calls == [("POST", APEX, r.bodies[0]), ("GET", WK, None), ("POST", WIX, r.bodies[1])]
    assert r.bodies[1] == r.bodies[0], "discovery must replay the same request"


@pytest.mark.asyncio
async def test_discovery_is_not_attempted_when_the_pinned_path_answers(monkeypatch):
    """A Shopify merchant must cost exactly one request, as before. Scripting the well-known
    absent means a stray discovery would raise `unscripted GET`, not pass quietly."""
    r = _Redirector({APEX: (200, {}, _ok({"id": "chk_1"}))}).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.methods == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_does_not_trigger_discovery(monkeypatch, status):
    """A merchant having a bad day is not a merchant whose door is elsewhere."""
    r = _Redirector({APEX: (status, {}, None)}).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert str(status) in str(excinfo.value)
    assert r.methods == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint, why",
    [
        ("http://www.wixapis.com/ecom/ucp/x/mcp", "http downgrade"),
        ("https://user:pw@www.wixapis.com/ecom/ucp/x/mcp", "carries userinfo"),
        ("https://www.wixapis.com:8443/ecom/ucp/x/mcp", "a port of its own"),
        ("https://localhost/ecom/ucp/x/mcp", "not a fetchable public hostname"),
        ("https://127.0.0.1/ecom/ucp/x/mcp", "a bare IP literal"),
        ("", "empty"),
        (None, "absent"),
    ],
)
async def test_a_discovered_endpoint_that_fails_validation_is_refused(
    monkeypatch, endpoint, why
):
    """The profile is merchant-controlled input we would then FETCH. Each endpoint here serves a
    good payload at the target, so a missing check would fetch it — and the original 403 must
    surface instead, because a refused discovery is indistinguishable from no door."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(endpoint)),
        "http://www.wixapis.com/ecom/ucp/x/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://user:pw@www.wixapis.com/ecom/ucp/x/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://www.wixapis.com:8443/ecom/ucp/x/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://localhost/ecom/ucp/x/mcp": (200, {}, _ok({"id": "pwned"})),
        "https://127.0.0.1/ecom/ucp/x/mcp": (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value), why
    assert r.methods == ["POST", "GET"], f"must not have fetched the endpoint: {why}"


@pytest.mark.asyncio
async def test_the_discovered_host_is_put_through_the_public_address_guard(monkeypatch):
    """`_validated_endpoint` calls both guards; this one binds the DNS half. The endpoint is a
    well-formed public-looking name that resolves somewhere it must not."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch, resolves=lambda d: "wixapis" not in d)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value)
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile, why",
    [
        ({"ucp": {"services": {"dev.ucp.shopping": [{"transport": "embedded",
                                                     "endpoint": WIX}]}}}, "no mcp transport"),
        ({"ucp": {"services": {}}}, "no shopping service"),
        ({"not": "a profile"}, "unrecognised document"),
        ("<html>nope</html>", "HTML, not a profile"),
    ],
)
async def test_a_profile_naming_no_usable_endpoint_leaves_the_original_status(
    monkeypatch, profile, why
):
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, profile),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value), why
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_a_merchant_with_no_profile_reports_its_own_status_not_a_discovery_error(
    monkeypatch,
):
    """Discovery is a fallback. A merchant with genuinely no door must look like itself."""
    r = _Redirector({APEX: (404, {}, None), WK: (404, {}, None)}).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_discovery_does_not_re_post_the_endpoint_we_already_tried(monkeypatch):
    """cosrx answers the apex directly though its profile names another host; the mirror case is
    a profile naming the pinned URL we just got a 403 from. Sending it twice is pure waste."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(APEX)),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value)
    assert r.methods == ["POST", "GET"], "the identical endpoint must not be re-POSTed"


@pytest.mark.asyncio
async def test_discovery_follows_the_apex_to_www_redirect_on_the_profile(monkeypatch):
    """The same hosting quirk that hid Robinsons hides profiles too."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (301, {"location": WK_WWW}, None),
        WK_WWW: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WK, WK_WWW, WIX]


@pytest.mark.asyncio
async def test_a_failure_at_the_discovered_endpoint_names_that_host(monkeypatch):
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (503, {}, None),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    msg = str(excinfo.value)
    assert "503" in msg and "www.wixapis.com" in msg


def test_validated_endpoint_rebuilds_the_url_dropping_fragment_and_query(monkeypatch):
    """The query is DROPPED, not carried. Neither Shopify's `/api/ucp/mcp` nor Wix's
    `/ecom/ucp/<siteId>/mcp` uses one, and a merchant-chosen query was half of the
    forced-request primitive an arbitrary path opened up."""
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
    assert muc._validated_endpoint(
        "https://www.wixapis.com/ecom/ucp/x/mcp?v=1#frag"
    ) == "https://www.wixapis.com/ecom/ucp/x/mcp"
    assert muc._validated_endpoint("https://WWW.WIXAPIS.COM./ecom/ucp/x/mcp") == (
        "https://www.wixapis.com/ecom/ucp/x/mcp"
    )


@pytest.mark.parametrize(
    "path, why",
    [
        ("/ecom/ucp/x", "does not end in /mcp — an arbitrary endpoint"),
        ("/", "bare root"),
        ("/mcp/../../admin", "dot segments, and does not end in /mcp"),
        ("/ecom/ucp/x\x00/mcp", "a NUL byte — httpx raises InvalidURL, which is NOT an HTTPError"),
        ("/ecom/ucp/\u00e9/mcp", "non-ascii"),
        ("/a" * 400 + "/mcp", "over-long"),
    ],
)
def test_validated_endpoint_refuses_a_path_it_should_not_fetch(monkeypatch, path, why):
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
    assert muc._validated_endpoint(f"https://www.wixapis.com{path}") is None, why


def test_validated_endpoint_refuses_a_label_idna_cannot_encode(monkeypatch):
    """`_DOMAIN_RE` permits a 64-character label; IDNA caps at 63, so `getaddrinfo` raises
    UnicodeError — a ValueError, NOT the OSError `resolves_only_public` catches. Unguarded this
    escaped `_discover_endpoint`'s "never raises" contract all the way to an unhandled 500."""
    host = "a" * 64 + ".example.com"
    assert muc.validate_merchant_domain(host) == host, "premise: the regex lets it through"
    assert muc._validated_endpoint(f"https://{host}/mcp") is None



# --------------------------------------------------------------------------------------
# Discovery must NEVER add a failure mode. Two reviewers independently found it did: a
# malformed profile and an unbuildable URL both escaped to an unhandled 500, while
# `_discover_endpoint`'s docstring promised a merchant "still reports its OWN status".
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile, why",
    [
        ({"ucp": "2026-04-08"}, "truthy STRING at ucp — a real shape to serve"),
        ({"ucp": ["x"]}, "list at ucp"),
        ({"ucp": 1}, "int at ucp"),
        ({"ucp": {"services": "x"}}, "truthy string at services"),
        ({"ucp": {"services": ["x"]}}, "list at services"),
        ({"ucp": {"services": {"dev.ucp.shopping": "x"}}}, "string where entries belong"),
        ({"ucp": {"services": {"dev.ucp.shopping": ["x", 1, None]}}}, "junk entries"),
    ],
)
async def test_a_malformed_profile_reports_the_merchants_own_status_not_a_500(
    monkeypatch, profile, why
):
    """`(profile.get("ucp") or {}).get(...)` reads as defensive but raises AttributeError on a
    TRUTHY non-dict. The only production caller catches MerchantUcpError, so each of these turned
    a clean refusal into an unhandled 500 with a traceback."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, profile),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value), why
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_a_profile_body_that_is_not_json_reports_the_merchants_own_status(monkeypatch):
    """Written from the WRITER: a real httpx response raises on `.json()` for an HTML body. The
    earlier 'HTML, not a profile' case handed the parser a str and so exercised a different
    branch entirely."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, json.JSONDecodeError("Expecting value", "<html>", 0)),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_an_endpoint_httpx_could_not_build_is_refused_before_the_post(monkeypatch):
    """`httpx.InvalidURL` is NOT a subclass of `httpx.HTTPError`, so it would escape the only net
    `_call_tool` has. `_validated_endpoint` refuses the shapes that provoke it."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, _profile("https://www.wixapis.com/ecom/ucp/x\x00y/mcp")),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.methods == ["POST", "GET"], "the unbuildable URL must never reach client.post"


@pytest.mark.asyncio
async def test_an_arbitrary_path_endpoint_is_refused(monkeypatch):
    """The widening that mattered. A free path turned this into "POST anywhere public and read
    the status back"; the `/mcp` suffix is what keeps it a UCP door."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, _profile("https://api.pivota.cc/admin/scheduler/jobs")),
        "https://api.pivota.cc/admin/scheduler/jobs": (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.methods == ["POST", "GET"], "must not have POSTed to the arbitrary path"


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["hostname", "dns"])
async def test_the_profile_hop_sibling_is_put_through_both_guards(monkeypatch, guard):
    """The POST-path hop had both guards bound; the PROFILE-path hop had neither. Asymmetric
    coverage on the same mechanism is exactly where a mutant lives."""
    script = {
        APEX: (404, {}, None),
        WK: (301, {"location": WK_WWW}, None),
        WK_WWW: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }
    if guard == "dns":
        r = _Redirector(script).install(monkeypatch, resolves=lambda d: not d.startswith("www."))
    else:
        r = _Redirector(script).install(monkeypatch)
        monkeypatch.setattr(
            muc, "validate_merchant_domain", lambda d: None if d.startswith("www.") else d
        )

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.urls == [APEX, WK], f"the {guard} guard must stop the profile hop"


@pytest.mark.asyncio
async def test_the_apex_endpoint_is_deduped_after_a_www_hop(monkeypatch):
    """The dedup compared only against the host we last fetched. After an apex->www hop a profile
    naming the APEX slipped through, re-POSTed the URL that had just redirected, and reported the
    resulting 301 against the sibling — a wasted request and a misleading diagnosis."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (404, {}, None),
        WK_WWW: (200, {}, _profile(APEX)),
        WK: (200, {}, _profile(APEX)),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value), "the sibling's own 404, not a re-POSTed 301"
    assert r.urls.count(APEX) == 1, "the apex must not be POSTed twice"


# --------------------------------------------------------------------------------------
# The three follow-ups from the security review: an independent kill switch, a bound on the
# profile body, and the accepted-TOCTOU note that had gone stale.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_is_fail_closed_without_its_own_flag(monkeypatch):
    """The suite's autouse fixture arms discovery, which is exactly why this test unsets it: an
    always-armed fixture would hide the fail-closed default, and the default IS the control.

    `get_checkout` writes nothing and so never checks `MERCHANT_UCP_CHECKOUT_ENABLED` — that flag
    is not a kill switch for this capability. Without its own, the widening would ship with none.
    """
    monkeypatch.delenv("MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED", raising=False)
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value)
    assert r.methods == ["POST"], "disarmed, a 403 must not cost the host a discovery GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "off", "no", "", "  "])
async def test_the_discovery_flag_is_fail_closed_for_every_falsey_spelling(
    monkeypatch, value
):
    monkeypatch.setenv("MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED", value)
    r = _Redirector({APEX: (403, {}, None)}).install(monkeypatch)

    with pytest.raises(MerchantUcpError):
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert r.methods == ["POST"]


@pytest.mark.asyncio
async def test_the_flag_is_read_per_call_so_it_kills_without_a_redeploy(monkeypatch):
    """Read per call, not frozen at import — the same property the seed-variant sourcing lane
    documents. A switch you must redeploy to flip is not a kill switch."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")
    assert got["id"] == "chk_1"

    monkeypatch.setenv("MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED", "0")
    with pytest.raises(MerchantUcpError):
        await muc.get_checkout("robinsons.com.sg", "chk_1")


@pytest.mark.asyncio
async def test_an_oversized_profile_is_refused_before_it_is_parsed(monkeypatch):
    """A UCP profile is a few KB. Without a cap the only bound on what a hostile host can make us
    buffer and hand to a JSON parser is the 12 s timeout."""
    fat = {"ucp": {"pad": "x" * (muc._MAX_PROFILE_BYTES + 1),
                   "services": {"dev.ucp.shopping": [{"transport": "mcp", "endpoint": WIX}]}}}
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, fat),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value)
    assert r.methods == ["POST", "GET"], "the endpoint inside an oversized profile is never used"


@pytest.mark.asyncio
async def test_a_profile_just_under_the_cap_is_still_used(monkeypatch):
    """The positive counterpart: a cap that refused everything would pass the test above while
    breaking the feature."""
    pad = "x" * (muc._MAX_PROFILE_BYTES // 2)
    ok_profile = {"ucp": {"pad": pad,
                          "services": {"dev.ucp.shopping": [
                              {"transport": "mcp", "endpoint": WIX}]}}}
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, ok_profile),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    got = await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert got["id"] == "chk_1"
    assert r.urls == [APEX, WK, WIX]


# --------------------------------------------------------------------------------------
# Round-three review. Two reviewers independently found a THIRD escape of "discovery must
# not add a failure mode", plus the same defect one line above the code that fixed it.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "err, why",
    [
        (RecursionError("maximum recursion depth exceeded"), "b'[' * 1200 is 2.4KB and blows the parser"),
        (MemoryError(), "a BaseException — a decompressed body can still get here"),
        (RuntimeError("boom"), "the whole RuntimeError branch was outside the old tuple"),
    ],
)
async def test_a_profile_that_breaks_the_parser_is_not_a_500(monkeypatch, err, why):
    """DEPTH, not size, is the parser hazard, and the size cap cannot bound it. The old
    enumerated tuple `(HTTPError, InvalidURL, ValueError, TypeError)` contained no RuntimeError,
    so a 2.4KB body three orders of magnitude under the cap reached the caller as a 500."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, err),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value), why
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
async def test_the_discovery_get_refuses_compression(monkeypatch):
    """`resp.content` is the DECOMPRESSED body, so a byte cap applied after the fact bounds
    nothing: a ~1KB gzip bomb materialises a gigabyte first, and the MemoryError is a
    BaseException. Refusing the encoding is what bounds the read."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert r.get_headers, "the discovery GET must be made"
    assert all(h and h.get("Accept-Encoding") == "identity" for h in r.get_headers)


@pytest.mark.asyncio
async def test_a_64_character_label_in_the_callers_own_domain_is_not_a_500(monkeypatch):
    """The SAME UnicodeError the diff absorbed in `_validated_endpoint`, one function above and
    left unwrapped. `_DOMAIN_RE` admits a 64-char label, IDNA caps at 63, getaddrinfo raises
    UnicodeError — a ValueError, not the OSError the guard catches. Reachable from a request
    body with NO flag armed, so this test deliberately does not arm discovery."""
    monkeypatch.delenv("MERCHANT_UCP_ENDPOINT_DISCOVERY_ENABLED", raising=False)
    _Redirector({}).install(monkeypatch, resolves=_raise_unicode)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("a" * 64 + ".com", "chk_1")

    assert "does not resolve to a public address" in str(excinfo.value)


def _raise_unicode(domain):
    raise UnicodeError("encoding with 'idna' codec failed (label empty or too long)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint, why",
    [
        ({"url": WIX}, "a dict where a string belongs"),
        (5, "an int"),
        ([WIX], "a list"),
        (True, "a bool"),
    ],
)
async def test_a_non_string_endpoint_is_refused_not_raised(monkeypatch, endpoint, why):
    """`ucp`, `services` and `entry` were each isinstance-pinned; `endpoint` was not, and the two
    guards that cover it are in different functions — so dropping BOTH survived every test.
    `urlsplit(x.strip())` on a non-string is an AttributeError, i.e. the same 500 class again."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (200, {}, {"ucp": {"services": {"dev.ucp.shopping": [
            {"transport": "mcp", "endpoint": endpoint}]}}}),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value), why
    assert r.methods == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 404, 500])
async def test_a_profile_served_on_a_non_200_is_not_used(monkeypatch, status):
    """The no-profile test scripts a 404 with a `None` body, which yields None whether or not the
    status is checked — so it could never see this. Here the body is a VALID profile."""
    r = _Redirector({
        APEX: (404, {}, None),
        WK: (status, {}, _profile(WIX)),
        WK_WWW: (status, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert WIX not in r.urls, "a profile the merchant did not serve with 200 must not be used"


def test_validated_endpoint_refuses_paths_a_target_could_renormalise(monkeypatch):
    """The `/mcp` suffix is not a path bound: httpx forwards the path verbatim, so a target that
    collapses `%2e%2e` or strips `;` parameters AFTER routing receives our POST somewhere that
    does not end `/mcp`. `/xmcp` is the plain suffix-vs-segment case."""
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
    for path in (
        "/admin/jobs/%2e%2e/%2e%2e/mcp",
        "/admin/jobs/../mcp",
        "/admin/jobs;/mcp",
        "/admin//mcp",
        "/xmcp",
        "/API/UCP/%2E%2E/mcp",
    ):
        assert muc._validated_endpoint(f"https://www.wixapis.com{path}") is None, path
    # the positive counterpart — both real platform paths must still be admitted
    for good in ("/api/ucp/mcp", "/ecom/ucp/53b84487-eb3f-4f9d-a68b-583b9677dc65/mcp"):
        assert muc._validated_endpoint(f"https://www.wixapis.com{good}") == (
            f"https://www.wixapis.com{good}"
        ), good


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429])
async def test_an_auth_or_ratelimit_status_does_not_trigger_discovery(monkeypatch, status):
    """The trigger set's upper bound was unbound — adding 401 to `_DISCOVER_STATUSES` survived.
    A merchant refusing us is not a merchant whose door is elsewhere."""
    r = _Redirector({APEX: (status, {}, None)}).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert str(status) in str(excinfo.value)
    assert r.methods == ["POST"]


def test_the_two_discovery_helpers_never_raise(monkeypatch):
    """Turns the equivalence claim into a test. Two mutants survive by being harmless ONLY while
    both helpers are total; nothing pinned that, so a regression would quietly make the `try`
    placement load-bearing again and turn a merchant profile back into a 500."""
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
    for profile in (
        None, 3, [], "x", {"ucp": "x"}, {"ucp": ["x"]}, {"ucp": {"services": "x"}},
        {"ucp": {"services": {"dev.ucp.shopping": "x"}}},
        {"ucp": {"services": {"dev.ucp.shopping": [None, 1, {"transport": "mcp"}]}}},
        {"ucp": {"services": {"dev.ucp.shopping": [{"transport": "mcp", "endpoint": {"a": 1}}]}}},
    ):
        assert muc._endpoint_from_profile(profile) is None or isinstance(
            muc._endpoint_from_profile(profile), str
        )
    for url in (
        None, 5, [], b"https://x.com/mcp", "", "   ", "https://[oops/mcp",
        "https://x.com:abc/mcp", "https://" + "a" * 64 + ".com/mcp",
        "https://x.com/" + "a" * 900 + "/mcp", "https://x.com/\x00/mcp",
    ):
        assert muc._validated_endpoint(url) is None or isinstance(
            muc._validated_endpoint(url), str
        )


# --------------------------------------------------------------------------------------
# Round four. The gzip "bound" from round three did not bound anything: httpx decodes on the
# RESPONSE header, so `Accept-Encoding: identity` is advisory and the cap ran after inflation.
# Measured: 20,420 bytes on the wire -> 20,971,530 bytes in `.content`.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_compressed_bomb_is_stopped_by_the_read_not_the_header(monkeypatch):
    """The read stops at the ceiling regardless of what the merchant sends, because
    `_read_bounded` streams and counts DECODED bytes. Counting wire bytes would bound the wrong
    quantity — 256KB of gzip is ~256MB inflated."""
    fat = {"ucp": {"pad": "x" * (muc._MAX_PROFILE_BYTES * 2),
                   "services": {"dev.ucp.shopping": [{"transport": "mcp", "endpoint": WIX}]}}}
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (200, {}, fat),
        WIX: (200, {}, _ok({"id": "pwned"})),
    }).install(monkeypatch)

    infos = []
    monkeypatch.setattr(muc.logger, "info", lambda fmt, *a: infos.append(fmt % a))

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "403" in str(excinfo.value)
    assert WIX not in r.urls, "an over-limit profile must not yield an endpoint"
    # Bind the DIAGNOSTIC, not just the outcome. An over-limit body is empty, so `resp.json()`
    # would fail and discovery would refuse anyway — dropping the `over_limit` check changes no
    # behaviour and survived as an equivalent mutant. What it buys is an operator being told the
    # profile was too large rather than that it was unparseable, and that is what this pins.
    assert any("exceeded" in m and "262144" in m for m in infos), infos


@pytest.mark.asyncio
async def test_an_oversized_merchant_POST_response_is_refused(monkeypatch):
    """The ceiling applies to the POSTs too, not just the profile GET. The discovered POST is on a
    host the MERCHANT named, so leaving that read unbounded would be new exposure from discovery."""
    r = _Redirector({
        APEX: (200, {}, {"jsonrpc": "2.0", "id": 1,
                         "result": {"structuredContent": {"pad": "x" * (muc._MAX_PROFILE_BYTES * 2)}}}),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "exceeded" in str(excinfo.value)


@pytest.mark.asyncio
async def test_both_discovery_gets_refuse_compression(monkeypatch):
    """The header is only a courtesy now, but pin it on BOTH GETs. The sibling GET was the half
    left unbound, and it is the worse half: the merchant controls the 301, so 'apex redirects you
    to its own www, www serves the bomb' is the path an attacker would actually take."""
    r = _Redirector({
        APEX: (403, {}, None),
        WK: (301, {"location": WK_WWW}, None),
        WK_WWW: (200, {}, _profile(WIX)),
        WIX: (200, {}, _ok({"id": "chk_1"})),
    }).install(monkeypatch)

    await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert len(r.get_headers) == 2, "both the apex and the sibling profile GET"
    assert all(h and h.get("Accept-Encoding") == "identity" for h in r.get_headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result, why",
    [
        ({"isError": True, "content": [{"type": "text", "text": '{"messages": 5}'}]}, "int messages"),
        ({"isError": True, "content": [{"type": "text", "text": '{"messages": true}'}]}, "bool messages"),
        ({"isError": True, "content": [{"type": "text", "text": '{"messages": 1.5}'}]}, "float messages"),
        ({"isError": True, "content": [{"type": "text", "text": '{"messages": "x"}'}]}, "string messages"),
        ({"isError": True, "content": 5}, "int content"),
        ({"isError": True, "content": "x"}, "string content"),
        ({"isError": True, "content": {"a": 1}}, "dict content"),
    ],
)
async def test_a_non_list_member_in_the_rpc_result_is_never_a_500(monkeypatch, result, why):
    """The FOURTH escape of the invariant, in `_unwrap`/`message_codes` rather than discovery:
    `for entry in 5` is a TypeError and nothing on the route translates it.

    The assertion is the invariant itself, not a specific outcome — returning normally and
    refusing with MerchantUcpError are both fine; ANY other exception is the bug. Writing it as
    `pytest.raises(MerchantUcpError)` would have been wrong in the other direction, since after
    the fix several of these no longer raise at all.

    Pre-existing on the pinned POST — but discovery makes any public MCP server a merchant names
    into a second source of the same response, so it is fixed here rather than filed.
    """
    rpc = {"jsonrpc": "2.0", "id": 1, "result": result}
    _Redirector({APEX: (200, {}, rpc)}).install(monkeypatch)

    try:
        await muc.get_checkout("robinsons.com.sg", "chk_1")
    except MerchantUcpError:
        pass
    except Exception as exc:  # noqa: BLE001 — the whole point is to catch the wrong class
        pytest.fail(f"{why}: merchant input produced {type(exc).__name__}, not MerchantUcpError")


@pytest.mark.asyncio
async def test_a_non_dict_line_item_is_never_a_500(monkeypatch):
    """The same truthy-non-dict construct the round-one fix named, 250 lines above it and left
    alone: `(raw or {}).get(...)` raises AttributeError on `["x"]`. Not HTTP-reachable today —
    create_checkout has no production caller — which is exactly why it is worth pinning before
    one appears."""
    _Capture(_ok({"id": "chk_1"})).install(monkeypatch)

    try:
        await muc.create_checkout("cosrx.com", line_items=[["x"]])
    except MerchantUcpError:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"a non-dict line item produced {type(exc).__name__}")


def test_percent_encoded_delimiters_are_refused(monkeypatch):
    """A target that decodes BEFORE routing sees `?` `#` NUL `\\` and `.` where we saw an opaque
    path; `%25` lets it double-decode its way to any of them."""
    monkeypatch.setattr(muc, "resolves_only_public", lambda d: True)
    for tok in ("%2e", "%2f", "%3f", "%23", "%00", "%5c", "%25", "%2E", "%3F"):
        assert muc._validated_endpoint(f"https://www.wixapis.com/a{tok}b/mcp") is None, tok


@pytest.mark.asyncio
async def test_the_hopped_pinned_url_is_also_deduped(monkeypatch):
    """The dedup has two halves; only the apex half was bound. This is the www half — a profile
    naming the pinned URL on the host we hopped TO, which is the case the fix's own comment
    describes."""
    r = _Redirector({
        APEX: (301, {"location": WWW}, None),
        WWW: (404, {}, None),
        WK: (200, {}, _profile(WWW)),
        WK_WWW: (200, {}, _profile(WWW)),
    }).install(monkeypatch)

    with pytest.raises(MerchantUcpError) as excinfo:
        await muc.get_checkout("robinsons.com.sg", "chk_1")

    assert "404" in str(excinfo.value)
    assert r.urls.count(WWW) == 1, "the hopped pinned url must not be POSTed twice"
