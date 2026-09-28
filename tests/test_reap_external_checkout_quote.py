"""The 2026-09-28 quote contract: `externalCheckout` cart-link quotes, `offerCode`, and the new
quote error vocabulary. Client level; no network (the recorder from test_reap_agentic_client).

Every cart link here comes from the REAL producers -- `services.reap_cart_link.validate_cart_link`
(what the ledger stores) and `services.outbound_links_service.build_shopify_cart_permalink` (what
the route builds) -- never a hand-typed string, so a guard is tested against what actually
reaches it.
"""

from __future__ import annotations

import json

import pytest

from services import reap_agentic_client as rc
from services import reap_cart_link as cl

from test_reap_agentic_client import (  # noqa: F401 -- fixtures are used by name
    GOOD_ADDRESS,
    _check_against,
    _no_unpatched_httpx,
    _request_schema,
    _run,
    _spec,
    clean_env,
    wire,
)

CLICK = "clk_9b1c3dcd4a854e26a5c8a295"


def _link(shop="judydoll.com", variant="50041364447509", qty=1, market="US", click=CLICK):
    """The canonical link as the ledger stores it: built as the route builds it, then through
    the validator that gates the row."""
    raw = f"https://{shop}/cart/{variant}:{qty}?attributes[pivota_click_id]={click}&country={market}"
    url = cl.validate_cart_link(raw, click_id=click, shop_domain=shop, market=market)
    assert url is not None
    return url


def _body(url=None, **kw):
    return rc.build_cart_link_quote_request(
        cart_url=url or _link(), email=kw.pop("email", "buyer@example.com"),
        shipping_address=kw.pop("shipping_address", GOOD_ADDRESS), **kw)


# --- the builder: accepting ----------------------------------------------------------------------

def test_the_cart_link_body_is_the_published_external_checkout_shape():
    url = _link()
    body = _body(url)
    assert set(body) == {"externalCheckout", "email", "shippingAddress"}
    assert body["externalCheckout"] == {"merchantDomain": "judydoll.com", "checkoutUrl": url}
    # Byte-for-byte: Reap "preserves it without rebuilding", which is what carries the click
    # attribute to the merchant's order.
    assert body["externalCheckout"]["checkoutUrl"] is url


def test_the_route_builders_link_goes_through_unchanged():
    """The route's producer, not the validator's canonical form: what the route stores is what
    the builder must accept and pass through."""
    from services.outbound_links_service import build_shopify_cart_permalink

    url = build_shopify_cart_permalink(shop_domain="jsmbeauty.sg", variant_id="51905273004353",
                                       click_id=CLICK, quantity=1, country="SG")
    assert cl.validate_cart_link(url, click_id=CLICK, shop_domain="jsmbeauty.sg", market="SG")
    body = _body(url)
    assert body["externalCheckout"] == {"merchantDomain": "jsmbeauty.sg", "checkoutUrl": url}


@pytest.mark.parametrize("shop", ["judydoll.com", "www.judydoll.com", "jsmbeauty.sg"])
def test_merchant_domain_is_always_the_urls_own_host(shop):
    """THE MISMATCH IS IMPOSSIBLE BY CONSTRUCTION: there is no argument for merchantDomain, and
    it is read from the same parse that validated the URL. Checked against an independent
    parse of what was sent."""
    from urllib.parse import urlsplit

    url = _link(shop=shop, market="SG" if shop.endswith(".sg") else "US")
    body = _body(url)
    sent = body["externalCheckout"]
    assert sent["merchantDomain"] == urlsplit(sent["checkoutUrl"]).netloc == shop


def test_the_builder_takes_no_merchant_domain_input():
    """A caller cannot supply a disagreeing domain -- the keyword does not exist."""
    with pytest.raises(TypeError):
        rc.build_cart_link_quote_request(cart_url=_link(), email="b@example.com",
                                         shipping_address=GOOD_ADDRESS,
                                         merchant_domain="jsmbeauty.sg")


def test_a_cart_link_body_validates_against_the_pinned_spec():
    schema = _request_schema(_spec(), "/agentic/quotes")
    _check_against(schema, _body(), label="cart-link quote")
    _check_against(schema, _body(offer_code="PEACHIE20"), label="cart-link quote with code")
    _check_against(schema, rc.build_quote_request(
        items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com",
        shipping_address=GOOD_ADDRESS, offer_code="SAVE10"), label="items quote with code")


def test_the_pinned_spec_forbids_mixing_the_two_branches():
    """Each branch names the other's key under `not.required`; a body carrying both `items` and
    `externalCheckout` is refused by Reap, and our two builders can never produce one."""
    schema = _request_schema(_spec(), "/agentic/quotes")
    titles = {b.get("title"): b for b in schema["oneOf"]}
    assert titles["Reap discovery"]["not"] == {"required": ["externalCheckout"]}
    assert titles["Checkout URL"]["not"] == {"required": ["items"]}
    assert "shippingAddress" in titles["Checkout URL"]["required"]


# --- the builder: refusing -----------------------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    # an extra query key (a discount, a ref) -- `extra_query_key`
    lambda u: u + "&discount=FREE",
    # buyer PII prefill -- `checkout_prefill`
    lambda u: u + "&checkout[email]=a@b.co",
    # two lines
    lambda u: u.replace(":1?", ":1,123:1?"),
    # no market pin
    lambda u: u.split("&country=")[0],
    # not https
    lambda u: u.replace("https://", "http://"),
])
def test_a_link_the_validator_refuses_is_refused_and_never_named(mutate):
    bad = mutate(_link())
    with pytest.raises(rc.ReapRequestError) as caught:
        _body(bad)
    assert "judydoll" not in str(caught.value) and CLICK not in str(caught.value)


def test_a_mixed_case_host_is_refused_rather_than_rewritten():
    """`cart_link_host` lowercases; sending `JudyDoll.com` in the URL beside `judydoll.com` as the
    domain would be a byte mismatch Reap may reject. The URL is never rebuilt, so it is refused."""
    url = _link().replace("judydoll.com", "JudyDoll.com", 1)
    assert cl.cart_link_line(url) is not None  # structurally fine...
    with pytest.raises(rc.ReapRequestError):
        _body(url)  # ...but not sendable


@pytest.mark.parametrize("address", [None, {}, {"city": "Singapore", "country": "SG"}])
def test_a_cart_link_quote_without_a_complete_address_is_refused(address):
    with pytest.raises(rc.ReapRequestError):
        _body(shipping_address=address)


def test_a_cart_link_quote_without_an_email_is_refused():
    with pytest.raises(rc.ReapRequestError):
        _body(email="")


# --- the offer code rule -------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", " ", "\t\n", "x" * 129, "PEACHIE\x0020", "SAVE\x7f", 20, b"X"])
def test_an_unsendable_offer_code_is_refused_on_both_branches(bad):
    with pytest.raises(rc.OfferCodeInvalid) as caught:
        rc.validate_offer_code(bad)
    assert caught.value.code == "offer_code_malformed"
    with pytest.raises(rc.ReapRequestError):
        _body(offer_code=bad)
    with pytest.raises(rc.ReapRequestError):
        rc.build_quote_request(items=[{"variantId": "var_x", "quantity": 1}],
                               email="b@example.com", offer_code=bad)


@pytest.mark.parametrize("good", ["PEACHIE20", "peachie20", "x" * 128, "A", " SAVE10 ", "10%OFF"])
def test_a_sendable_offer_code_is_sent_exactly_as_given(good):
    """Not stripped and not upper-cased: Reap matched both cases identically live, so case is the
    merchant's business; a code we altered is not the code the buyer typed."""
    assert rc.validate_offer_code(good) == good
    assert _body(offer_code=good)["offerCode"] == good
    assert rc.build_quote_request(items=[{"variantId": "var_x", "quantity": 1}],
                                  email="b@example.com", offer_code=good)["offerCode"] == good


def test_no_offer_code_means_no_key():
    assert "offerCode" not in _body()
    assert "offerCode" not in _body(offer_code=None)
    assert rc.validate_offer_code(None) is None


def test_the_offer_code_error_message_never_names_the_code():
    with pytest.raises(rc.OfferCodeInvalid) as caught:
        rc.validate_offer_code("SECRET\x01CODE")
    assert "SECRET" not in str(caught.value)


def test_the_idempotency_key_differs_with_and_without_a_code(wire, monkeypatch):
    """The re-quote without the code must be a NEW request, not a replay of the refused one --
    Reap caches a response under its key for 24 h."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    url = _link()
    _run(rc.request_cart_link_quote(cart_url=url, email="b@example.com",
                                    shipping_address=GOOD_ADDRESS, offer_code="PEACHIE20"))
    _run(rc.request_cart_link_quote(cart_url=url, email="b@example.com",
                                    shipping_address=GOOD_ADDRESS))
    _run(rc.request_cart_link_quote(cart_url=url, email="b@example.com",
                                    shipping_address=GOOD_ADDRESS, offer_code="PEACHIE20"))
    keys = [c["headers"]["Idempotency-Key"] for c in wire.calls]
    assert keys[0] != keys[1] and keys[0] == keys[2]
    assert wire.calls[0]["body"]["offerCode"] == "PEACHIE20"
    assert "offerCode" not in wire.calls[1]["body"]
    # Same on the items branch.
    for code in ("SAVE10", None):
        _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}],
                              email="b@example.com", offer_code=code))
    assert wire.calls[3]["headers"]["Idempotency-Key"] != wire.calls[4]["headers"]["Idempotency-Key"]


def test_the_cart_link_quote_goes_to_the_quote_path_with_the_slow_timeout(wire):
    _run(rc.request_cart_link_quote(cart_url=_link(), email="b@example.com",
                                    shipping_address=GOOD_ADDRESS))
    call = wire.calls[0]
    assert call["url"].endswith("/agentic/quotes") and call["method"] == "POST"
    assert call["timeout"] == rc._QUOTE_TIMEOUT_S
    assert "Idempotency-Key" in call["headers"]


def test_an_explicit_timeout_reaches_the_transport(wire):
    """The purchase service fits two quotes into one step budget by passing each its remaining
    time; that number has to be the one httpx is handed."""
    _run(rc.request_cart_link_quote(cart_url=_link(), email="b@example.com",
                                    shipping_address=GOOD_ADDRESS, timeout_seconds=23.5))
    assert wire.calls[0]["timeout"] == 23.5


# --- the error vocabulary, with bodies shaped exactly as Reap sends them -------------------------

def _fail(wire, status, code, detail=None, headers=None):
    wire.next_status = status
    wire.next_content = json.dumps(
        {"error": {"code": code, "message": "The request was rejected", "detail": detail}}
    ).encode()
    wire.next_headers = headers
    return _run(rc.request_cart_link_quote(cart_url=_link(), email="b@example.com",
                                           shipping_address=GOOD_ADDRESS, offer_code="PEACHIE20"))


@pytest.mark.parametrize("status,code,detail,kind,reason", [
    # live 2026-09-28: a bogus code, ~15.5 s after the merchant round trip
    (400, "OFFER_CODE_INVALID", None, "offer_code_invalid", None),
    (400, "OFFER_CODE_EXPIRED", None, "offer_code_expired", None),
    # live 2026-09-28: merchantDomain != URL host -> AGENTIC_REQUEST_REJECTED, detail null
    (400, "AGENTIC_REQUEST_REJECTED", None, "request_rejected", None),
    # the docs' spelling of the same mismatch
    (400, "CHECKOUT_URL_INVALID", {"reason": "MERCHANT_CONTEXT_UNVERIFIED"},
     "checkout_url_invalid", "merchant_context_unverified"),
    (400, "CHECKOUT_URL_INVALID", {"reason": "NOT_FOUND"}, "checkout_url_invalid", "not_found"),
    (400, "CHECKOUT_URL_INVALID", {"reason": "EXPIRED"}, "checkout_url_invalid", "expired"),
    (400, "CHECKOUT_URL_INVALID", {"reason": "INVALID"}, "checkout_url_invalid", "invalid"),
    (400, "CHECKOUT_URL_INVALID", {"reason": "SOMETHING_NEW"}, "checkout_url_invalid", "other"),
    (400, "CARD_PAYMENT_UNAVAILABLE", None, "card_payment_unavailable", None),
    (400, "QUOTE_UNFULFILLABLE", {"reason": "ITEMS_UNSHIPPABLE", "message": "Your cart has..."},
     "quote_unfulfillable", "items_unshippable"),
    (400, "QUOTE_UNFULFILLABLE", {"reason": "INVALID_PHONE", "message": "Phone is invalid"},
     "quote_unfulfillable", "invalid_phone"),
    (400, "QUOTE_UNFULFILLABLE", {"reason": "STATE_OR_PROVINCE_REQUIRED"},
     "quote_unfulfillable", "state_or_province_required"),
    (400, "QUOTE_UNFULFILLABLE", {"reason": "ADDRESS_LINE_2_REQUIRED"},
     "quote_unfulfillable", "address_line_2_required"),
    (503, "AGENTIC_SERVICE_UNAVAILABLE", None, "service_unavailable", None),
    (409, "VARIANT_UNAVAILABLE", None, "variant_unavailable", None),
])
def test_every_new_quote_code_classifies_to_its_own_kind(wire, status, code, detail, kind, reason):
    got = _fail(wire, status, code, detail)
    rejection = rc.classify_quote_rejection(got)
    assert rejection is not None
    assert (rejection.kind, rejection.reason) == (kind, reason)
    assert rejection.offer_code_rejected is (kind in ("offer_code_invalid", "offer_code_expired"))
    assert rejection.retryable is False
    assert rejection.error_code == (f"{kind}:{reason}" if reason else kind)
    assert len(rejection.error_code) <= 64 and rejection.error_code == rejection.error_code.lower()


def test_quote_temporarily_unavailable_is_retryable_records_retry_after_and_is_not_the_non_ucp_503(
    wire,
):
    got = _fail(wire, 503, "QUOTE_TEMPORARILY_UNAVAILABLE", headers={"retry-after": "7"})
    assert got.retry_after_seconds == 7
    assert got.merchant_probably_not_completable is False
    rejection = rc.classify_quote_rejection(got)
    assert rejection.retryable and rejection.retry_after_seconds == 7
    # The OTHER 503 keeps its old meaning.
    other = _fail(wire, 503, "AGENTIC_SERVICE_UNAVAILABLE")
    assert other.merchant_probably_not_completable is True
    assert rc.classify_quote_rejection(other).retryable is False


def test_the_client_never_sleeps_on_retry_after(wire, monkeypatch):
    import asyncio

    async def _boom(*a, **k):  # pragma: no cover -- the assertion is that it is never called
        raise AssertionError("slept on Retry-After inside a request")

    monkeypatch.setattr(asyncio, "sleep", _boom)
    got = _fail(wire, 503, "QUOTE_TEMPORARILY_UNAVAILABLE", headers={"retry-after": "3600"})
    assert got.retry_after_seconds == 3600


@pytest.mark.parametrize("header,expected", [
    ("30", 30), ("0", 0), ("999999", rc.MAX_RETRY_AFTER_S), ("-5", None), ("1.5", None),
    ("soon", None), ("", None), (None, None), ("٣٠", None),
    ("Thu, 01 Jan 1970 00:00:40 GMT", 30),   # HTTP-date, 30 s after `now` below
    ("Thu, 01 Jan 1970 00:00:05 GMT", 0),    # already past
])
def test_retry_after_is_parsed_bounded_and_never_trusted(header, expected):
    assert rc._retry_after_seconds(header, now=10.0) == expected


def test_an_unknown_or_missing_code_is_not_classified(wire):
    assert rc.classify_quote_rejection(_fail(wire, 400, "SOMETHING_ELSE")) is None
    wire.next_status = 400
    wire.next_content = b"not json"
    got = _run(rc.request_cart_link_quote(cart_url=_link(), email="b@example.com",
                                          shipping_address=GOOD_ADDRESS))
    assert got.error == "reap_status_400" and rc.classify_quote_rejection(got) is None
    assert rc.classify_quote_rejection(rc.ReapResponse(ok=True, status=200)) is None
    assert rc.classify_quote_rejection(
        rc.ReapResponse(ok=False, error="transport_error:ReadTimeout")) is None


def test_the_checkout_leg_carries_checkout_temporarily_unavailable_and_retry_after(wire, clean_env):
    wire.next_status = 503
    wire.next_content = json.dumps({"error": {"code": "CHECKOUT_TEMPORARILY_UNAVAILABLE",
                                              "message": "try again", "detail": None}}).encode()
    wire.next_headers = {"retry-after": "12"}
    got = _run(rc.create_checkout(quote_id="3ecfaedd-bc4b-4193-991f-67b18c91bbfc",
                                  enrollment_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
                                  return_url="https://agent.pivota.cc/reap/return?click=abc123"))
    assert got.error_code == "CHECKOUT_TEMPORARILY_UNAVAILABLE"
    assert got.retry_after_seconds == 12
    assert got.merchant_probably_not_completable is False


def test_the_seam_that_waited_for_a_field_name_is_gone():
    """The flat-field seam assumed ONE unpublished key; the real shape is nested. Nothing may
    reintroduce a second arming switch beside the env dials."""
    for name in ("CART_LINK_QUOTE_FIELD", "supports_cart_link_quote", "CartLinkQuoteUnsupported"):
        assert not hasattr(rc, name), name


# --- the verdict on the live 2026-09-28 quote shapes ---------------------------------------------

#: jsmbeauty.sg, sg.sandbox, 2026-09-28, verbatim amounts: 30 + 4 = 34 SGD, tax 2.48 INCLUDED.
SG_LIVE_QUOTE = {
    "id": "63676c27-9caf-4cb9-afca-985518a0ab09",
    "shippingOptions": [{"id": "ship_0", "name": "Standard (free delivery over $60)",
                         "selected": True, "price": {"amount": 4, "currency": "SGD"}}],
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 30, "currency": "SGD"},
        "shipping": {"amount": 4, "currency": "SGD"},
        "tax": {"amount": {"amount": 2.48, "currency": "SGD"}, "includedInPrices": True},
        "discounts": [], "additionalCharges": [],
        "finalAmount": {"amount": 34, "currency": "SGD"},
    },
    "expiresAt": "2026-09-28T11:08:24.848Z",
}
SG_ROW = {"currency": "SGD", "our_price_minor": 3000, "quantity": 1}


def test_a_tax_inclusive_quote_reconciles_without_adding_the_tax_twice():
    import services.reap_agentic_purchase as svc

    verdict = svc.verify_cart_link_quote(json.loads(json.dumps(SG_LIVE_QUOTE)), SG_ROW)
    assert verdict.ok, verdict
    assert (verdict.total_minor, verdict.shipping_minor, verdict.tax_minor) == (3400, 400, 248)


def test_the_same_tax_NOT_marked_included_does_not_reconcile():
    """THE CONTROL: only a literal `includedInPrices: true` excludes the tax line."""
    import services.reap_agentic_purchase as svc

    for flag in (None, False, "true", 1):
        quote = json.loads(json.dumps(SG_LIVE_QUOTE))
        if flag is None:
            del quote["amountBreakdown"]["tax"]["includedInPrices"]
        else:
            quote["amountBreakdown"]["tax"]["includedInPrices"] = flag
        verdict = svc.verify_cart_link_quote(quote, SG_ROW)
        assert not verdict.ok and verdict.last_error_code == "quote_total_not_reconciled", flag


def test_an_in_progress_idempotency_key_is_retryable_not_final(wire):
    got = _fail(wire, 409, "IDEMPOTENCY_REQUEST_IN_PROGRESS", detail={})
    rejection = rc.classify_quote_rejection(got)
    assert rejection.kind == "idempotency_request_in_progress" and rejection.retryable
