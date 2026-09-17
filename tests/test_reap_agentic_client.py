"""Tests for the Reap agentic client.

The weight here is deliberately not on "does a request body have the right keys". It is on the
JOIN -- the only part of the module with judgement in it -- and on the seams, because the
failure that killed PR #2136 was not a wrong line of logic. It was a module whose every unit
test passed against assumptions the module itself had invented, with nothing anywhere in a
position to compare them to the real API.

So three kinds of test:

  1. THE TRAPS, reproduced from measured sandbox data. The $140 gift-tray bundle that a
     price-proximity rule would select over the $140 perfume. The availability-ordered
     `defaultVariant` that a fallback would substitute for the variant we asked for. Another
     merchant's listing of the same branded product.
  2. THE NAMESPACE GUARD, which is the specific mistake #2136 made: our storefront variant id
     sent where Reap's `var_...` belongs.
  3. THE SEAM, driven end to end through a fake transport, asserting on what reaches the wire --
     path, headers, body -- rather than on what a builder returned. A builder that produces a
     correct `Reap-Version` header proves nothing if `_post` never calls it.
"""

from __future__ import annotations

import json

import pytest

from services import reap_agentic_client as rc


# --- fixtures, shaped from the published OpenAPI and from real sandbox responses -------------

FENTY_SEARCH = {
    "id": "srch_1",
    "products": [
        {
            "id": "prd_dfe0ceb2123640d496edcd7c21b94c0b",
            "merchant": {"name": "fentybeauty.com"},
            "name": "Fenty Eau de Parfum",
            "priceRange": {"min": {"amount": 39.0, "currency": "USD"},
                           "max": {"amount": 140.0, "currency": "USD"}},
            "available": True,
            # Availability-ordered: Standard is unavailable, so Reap previews Mini.
            "previewVariant": {"id": "var_bb8b0001", "name": "Mini",
                               "price": {"amount": 95.0, "currency": "USD"}, "available": True},
        },
        {
            # THE TRAP. A bundle, priced at exactly our row's $140.00.
            "id": "prd_05de098d000000000000000000000000",
            "merchant": {"name": "fentybeauty.com"},
            "name": "Fenty Eau de Parfum 75ML + Decorative Logo Tray",
            "priceRange": {"min": {"amount": 140.0, "currency": "USD"},
                           "max": {"amount": 140.0, "currency": "USD"}},
            "previewVariant": {"id": "var_bundle01",
                               "price": {"amount": 140.0, "currency": "USD"}, "available": True},
        },
        {
            # Another retailer's listing of the same branded item.
            "id": "prd_otherretailer0000000000000000",
            "merchant": {"name": "sephora.com"},
            "name": "Fenty Eau de Parfum",
            "priceRange": {"min": {"amount": 140.0, "currency": "USD"},
                           "max": {"amount": 140.0, "currency": "USD"}},
        },
    ],
    "pagination": {"nextCursor": None, "hasNextPage": False, "returnedCount": 3},
    "warnings": [],
}

FENTY_DETAILS = {
    "products": [
        {
            "id": "prd_dfe0ceb2123640d496edcd7c21b94c0b",
            "merchant": {"name": "fentybeauty.com"},
            "name": "Fenty Eau de Parfum",
            "media": [],
            "options": [
                {"name": "Size", "values": [
                    {"optionId": "opt_standard", "label": "Standard", "available": False},
                    {"optionId": "opt_mini", "label": "Mini", "available": True},
                    {"optionId": "opt_travel", "label": "Travel", "available": True},
                ]},
            ],
            # The one a fallback would take. It is NOT the storefront default.
            "defaultVariant": {
                "id": "var_bb8b0001", "name": "Mini",
                "options": [{"name": "Size", "value": "Mini"}],
                "price": {"amount": 95.0, "currency": "USD"},
                "available": True, "requiresShipping": True, "media": [],
            },
        },
    ],
    "errors": [],
}

# The same product with Standard AVAILABLE. Needed because the real fixture above now stops at
# `select_option_ids` -- Reap marks Standard unavailable, and calling /variant for an unavailable
# value is what produces the substitution. The guards that live AFTER that call still have to be
# exercised, so they get a product Reap would actually resolve.
FENTY_DETAILS_STANDARD_AVAILABLE = json.loads(json.dumps(FENTY_DETAILS))
FENTY_DETAILS_STANDARD_AVAILABLE["products"][0]["options"][0]["values"][0]["available"] = True

STANDARD_VARIANT = {
    "id": "var_standard140", "name": "Standard",
    "options": [{"name": "Size", "value": "Standard"}],
    "price": {"amount": 140.0, "currency": "USD"},
    "available": False, "requiresShipping": True, "media": [],
}

GOOD_ADDRESS = {
    "firstName": "Test", "lastName": "Buyer", "phone": "+14155550123",
    "addressLine1": "900 Brannan St", "city": "San Francisco",
    "region": "CA", "postalCode": "94103", "country": "US",
}


# --- the join: merchant scoping --------------------------------------------------------------

def test_a_different_retailers_listing_of_the_same_product_is_refused():
    """Reap's index is multi-merchant. Sephora's "Fenty Eau de Parfum" has the identical name and
    the identical $140.00 price as our row, and matching on either would quote the wrong door --
    wrong price basis, wrong fulfilment, wrong attribution."""
    match = rc.match_product(FENTY_SEARCH, merchant_domain="sephora.com",
                             product_name="Fenty Eau de Parfum")
    assert match.ok and match.product_id == "prd_otherretailer0000000000000000"

    ours = rc.match_product(FENTY_SEARCH, merchant_domain="fentybeauty.com",
                            product_name="Fenty Eau de Parfum")
    assert ours.ok and ours.product_id == "prd_dfe0ceb2123640d496edcd7c21b94c0b"


def test_a_merchant_absent_from_the_results_refuses_rather_than_taking_the_first_hit():
    match = rc.match_product(FENTY_SEARCH, merchant_domain="flowerbeauty.com",
                             product_name="Fenty Eau de Parfum")
    assert not match.ok and match.reason == "merchant_not_in_results"


@pytest.mark.parametrize("reap_name,our_domain,expected", [
    ("fentybeauty.com", "fentybeauty.com", True),
    ("cosrx.com", "https://www.cosrx.com/products/x", True),
    ("cosrx.com", "WWW.COSRX.COM", True),
    ("fentybeauty.com", "sephora.com", False),
    # The substring bug the first version of this function had: stem-in-name matching accepted
    # every one of these. `merchant.name` is a DOMAIN, so the comparison is exact.
    ("notcosrx.com", "cosrx.com", False),
    ("cosrx.com.evil.example", "cosrx.com", False),
    ("shop.cosrx.com", "cosrx.com", False),
    ("", "cosrx.com", False),
    ("cosrx.com", "", False),
])
def test_merchant_domain_matching_is_exact(reap_name, our_domain, expected):
    assert rc.merchant_domain_matches(reap_name, our_domain) is expected


# --- the join: the bundle trap ---------------------------------------------------------------

def test_the_140_dollar_bundle_does_not_win_on_an_exact_price_tie():
    """Our row is $140.00. So is the gift-tray bundle, and it is on the RIGHT merchant -- so the
    merchant filter cannot save us here and the name comparison has to. This is the case that
    would defeat a "closest price" or "best prefix" rule."""
    match = rc.match_product(FENTY_SEARCH, merchant_domain="fentybeauty.com",
                             product_name="Fenty Eau de Parfum")
    assert match.ok
    assert match.product_id == "prd_dfe0ceb2123640d496edcd7c21b94c0b"
    assert "Tray" not in (match.product_name or "")


def test_two_products_with_the_same_name_on_the_same_merchant_refuse():
    payload = json.loads(json.dumps(FENTY_SEARCH))
    payload["products"][1]["name"] = "Fenty Eau de Parfum"  # now a genuine collision
    match = rc.match_product(payload, merchant_domain="fentybeauty.com",
                             product_name="Fenty Eau de Parfum")
    assert not match.ok and match.reason == "ambiguous_name_match"
    assert len(match.candidates) == 2


def test_a_near_miss_name_refuses_and_reports_what_it_saw():
    match = rc.match_product(FENTY_SEARCH, merchant_domain="fentybeauty.com",
                             product_name="Fenty Eau de Parfum 50ml")
    assert not match.ok and match.reason == "no_exact_name_match"
    assert any("Tray" in (c["name"] or "") for c in match.candidates)


# --- the join: options, and the absence of a default fallback --------------------------------

def test_our_variant_title_selects_the_option_id_for_that_axis():
    product = FENTY_DETAILS["products"][0]
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard"))
    assert got.ok
    assert got.option_ids == ["opt_standard"]
    assert got.chosen == {"Size": "Standard"}


def test_an_unmatched_axis_refuses_and_names_the_axis():
    """The defaulting temptation, made concrete. Refusing produces a referral link; defaulting
    produces a quote for a different physical object at a different price."""
    product = FENTY_DETAILS["products"][0]
    got = rc.select_option_ids(product, rc.variant_title_tokens("100ml"))
    assert not got.ok
    assert got.reason == "axes_not_determined_by_title"
    assert got.unmatched_axes == ["Size"]


def test_a_second_axis_that_the_title_does_not_name_refuses():
    product = json.loads(json.dumps(FENTY_DETAILS["products"][0]))
    # TWO values, so the axis is genuinely undetermined. A single-value axis is determined by
    # construction and is handled by its own rule below.
    product["options"].append({"name": "Color", "values": [
        {"optionId": "opt_rose", "label": "Rose", "available": True},
        {"optionId": "opt_blue", "label": "Blue", "available": True},
    ]})
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard"))
    assert not got.ok and got.unmatched_axes == ["Color"]
    # It reports the axis it DID match, so a human can see how far it got.
    assert got.chosen == {"Size": "Standard"}


def test_a_shopify_style_two_axis_title_resolves_both():
    product = json.loads(json.dumps(FENTY_DETAILS["products"][0]))
    product["options"].append({"name": "Color", "values": [
        {"optionId": "opt_rose", "label": "Rose", "available": True},
        {"optionId": "opt_blue", "label": "Blue", "available": True},
    ]})
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard / Rose"))
    assert got.ok and got.option_ids == ["opt_standard", "opt_rose"]


def test_an_unavailable_option_is_still_selected_here_and_refused_one_layer_up():
    """Standard is `available: false` at Reap while the merchant's own storefront still lists it.
    One third-party negative is not a delisting, so the MATCHER must still select it rather than
    quietly move to an available sibling -- that substitution is exactly the $95-for-$140 error.

    Corrected claim: this is not "merely reported". `select_option_ids` selects and flags; the
    refusal happens in `resolve_our_row`, which will not call `/variant` for a flagged axis
    (`test_an_unavailable_chosen_value_refuses_without_calling_variant`). The distinction matters
    because the flag is what the refusal is built on, not a note beside it."""
    product = FENTY_DETAILS["products"][0]
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard"))
    assert got.ok and got.option_ids == ["opt_standard"]
    assert got.unavailable_axes == ["Size"]


def test_a_single_variant_product_refuses_rather_than_sending_an_empty_option_list():
    got = rc.select_option_ids({"id": "prd_x", "options": []}, ["Standard"])
    assert not got.ok and got.reason == "product_has_no_option_axes"


# --- the namespace guard: #2136's actual mistake ----------------------------------------------

def test_a_storefront_variant_id_is_refused_as_a_reap_variant_id():
    """`41669483823149` is the Shopify id our 8 Sep backfill recovered. PR #2136 sent exactly
    this value to Reap under a field name it had guessed, and nothing in that module was in a
    position to notice. Here it is a build-time refusal, before any egress."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_quote_items([{"variantId": "41669483823149", "quantity": 1}])
    assert "namespace" in str(exc.value)


def test_a_real_reap_variant_id_is_accepted():
    assert rc.build_quote_items([{"variantId": "var_standard140", "quantity": 2}]) == [
        {"variantId": "var_standard140", "quantity": 2}
    ]


@pytest.mark.parametrize("quantity", [0, -1])
def test_a_non_positive_quantity_is_refused_not_corrected(quantity):
    with pytest.raises(rc.ReapRequestError):
        rc.build_quote_items([{"variantId": "var_x", "quantity": quantity}])


def test_a_resolved_id_from_the_wrong_namespace_refuses(monkeypatch):
    """Belt and braces on the other side of the wire: if Reap ever returns an id that is not a
    `var_...`, we refuse rather than pass it into a quote."""
    async def fake(path, body, **kw):
        if path.endswith("/search"):
            return rc.ReapResponse(ok=True, status=200, data=FENTY_SEARCH)
        if path.endswith("/details"):
            return rc.ReapResponse(ok=True, status=200, data=FENTY_DETAILS_STANDARD_AVAILABLE)
        return rc.ReapResponse(ok=True, status=200, data={"id": "41669483823149"})
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(merchant_domain="fentybeauty.com",
                                  product_name="Fenty Eau de Parfum", variant_title="Standard"))
    assert not got.ok and got.reason == "resolved_id_not_in_reap_namespace"


# --- the batch-with-errors shape --------------------------------------------------------------

def test_a_product_reported_under_errors_is_not_read_as_merely_absent():
    """details is a BATCH: it returns 200 with a per-product `errors` array, so a failure arrives
    inside a successful response. Reading only `products` loses the code Reap gave us."""
    payload = {"products": [], "errors": [
        {"productId": "prd_x", "code": "PRODUCT_UNAVAILABLE", "message": "gone"}]}
    product, err = rc.details_for(payload, "prd_x")
    assert product is None and err == "PRODUCT_UNAVAILABLE"


def test_a_product_in_neither_array_is_distinguishable_from_an_error():
    product, err = rc.details_for({"products": [], "errors": []}, "prd_x")
    assert product is None and err == "product_not_in_response"


# --- configuration is the safety boundary ------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://sandbox.api.reap.global/v1",
    "https://prod.api.reap.global",
    "https://mx.sandbox.api.reap.global",
    "https://mx.prod.api.reap.global",
])
def test_the_four_hosts_the_spec_names_are_accepted(url):
    assert rc.validate_base_url(url) == url


@pytest.mark.parametrize("url", [
    "http://sandbox.api.reap.global/v1",       # not https
    "https://api.eu.reap.so/v2",               # a domain I invented in the predecessor
    "https://api.reapfin.com/v1",              # likewise
    "https://reap.global.evil.example/v1",     # suffix confusion
    "https://evilreap.global/v1",              # no dot before the suffix
])
def test_a_host_we_cannot_justify_fails_the_url_check(url):
    """RENAMED. This was called `..._is_refused_before_the_key_is_attached`, which claimed
    something it does not test: it calls `validate_base_url` directly and never goes near `_post`,
    where the key is actually attached. Replacing `validate_base_url()` with `base_url()` inside
    `_post` left this test green while our API key went to the typo'd host. The claim in the old
    name is now made — at the transport — by
    `test_a_host_outside_the_allowlist_is_refused_at_the_transport_with_no_key_sent`."""
    with pytest.raises(rc.ReapConfigError):
        rc.validate_base_url(url)


def test_the_refusal_message_never_contains_the_key(monkeypatch):
    monkeypatch.setenv("REAP_API_KEY", "sk_live_do_not_leak_me")
    with pytest.raises(rc.ReapConfigError) as exc:
        rc.validate_base_url("https://evil.example/v1")
    assert "do_not_leak_me" not in str(exc.value)


def test_an_unset_client_makes_no_request(monkeypatch):
    monkeypatch.delenv("REAP_API_BASE_URL", raising=False)
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    got = _run(rc._post("/agentic/quotes", {"items": []}))
    assert not got.ok and got.error == "reap_client_not_configured"


# --- the seam: what actually reaches the wire ---------------------------------------------------

class _FakeResponse:
    """A STREAMING response, because that is what `_post` now opens.

    `aiter_bytes` yields the body in chunks rather than handing it over whole, so a test can
    measure what `_read_bounded` actually reads. `chunk_size` is deliberately small: the
    cumulative check only means anything if the body arrives in more than one piece, and a fake
    that yields everything at once would let a mutant deleting the running total survive.
    """

    def __init__(self, status=200, payload=None, headers=None, content=None, chunk_size=64 * 1024):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        # `headers` is what the DECLARED-length check reads; the bytes from `aiter_bytes` are what
        # the cumulative check reads. The real httpx streaming response carries both.
        self.headers = headers if headers is not None else {}
        self.content = (content if content is not None
                        else json.dumps(self._payload).encode("utf-8"))
        self._chunk_size = max(1, int(chunk_size))
        #: How many bytes the caller actually pulled. A real bound STOPS READING; this is how a
        #: test tells "refused after reading it all" from "refused partway".
        self.bytes_yielded = 0

    async def aiter_bytes(self):
        for start in range(0, len(self.content), self._chunk_size):
            chunk = self.content[start:start + self._chunk_size]
            self.bytes_yielded += len(chunk)
            yield chunk

    def json(self):
        return self._payload


class _StreamContext:
    """What `client.stream(...)` returns: an async context manager over the response."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *a):
        return False


class _Recorder:
    """Stands in for httpx.AsyncClient and records the real call."""

    calls = []

    def __init__(self, *a, **kw):
        # Recorded on the INSTANCE and echoed into every call this client makes, because the
        # timeout is chosen in `_post` and handed to the constructor -- asserting on what
        # `default_timeout_for` RETURNS proves only that the helper is right, not that anything
        # calls it. A mutation that ignored the helper in `_post` survived a test that checked
        # only the helper.
        self.timeout = kw.get("timeout")
        # A2. Same reasoning for redirects: the flag is a CONSTRUCTOR argument, so the only place
        # it can be observed is here. `_MISSING` rather than None, so "not passed at all" is
        # distinguishable from "passed as None" and a test can pin the explicit False.
        self.follow_redirects = kw.get("follow_redirects", "_MISSING")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None, headers=None):
        _Recorder.calls.append({"method": method, "url": url, "body": json, "headers": headers,
                                "timeout": self.timeout,
                                "follow_redirects": self.follow_redirects})
        response = _FakeResponse(_Recorder.next_status, _Recorder.next_payload,
                                 headers=_Recorder.next_headers,
                                 content=_Recorder.next_content,
                                 chunk_size=_Recorder.next_chunk_size)
        _Recorder.last_response = response
        return _StreamContext(response)


@pytest.fixture
def wire(monkeypatch):
    import httpx
    _Recorder.calls = []
    _Recorder.next_status = 200
    _Recorder.next_payload = {}
    _Recorder.next_headers = None
    _Recorder.next_content = None
    _Recorder.next_chunk_size = 64 * 1024
    _Recorder.last_response = None
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    # The env var is a FLOOR on the timeout, and a value inherited from the developer's shell
    # would silently raise every bound this file asserts on. Cleared so the tests measure the
    # module, not the machine.
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    return _Recorder


def test_every_request_carries_the_required_version_header(wire):
    """Required on EVERY agentic endpoint and enum-pinned to one value in the spec. Its absence
    was one of the four defects that made the predecessor unable to return 200."""
    _run(rc.search_products(query="Fenty Eau de Parfum"))
    assert wire.calls[0]["headers"]["Reap-Version"] == "2025-02-14"
    assert wire.calls[0]["url"] == "https://sandbox.api.reap.global/agentic/products/search"


def test_the_path_is_the_agentic_one_and_not_the_bare_one(wire):
    """`POST /quotes` -- what the predecessor addressed -- is a 404 ROUTE_NOT_FOUND."""
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["url"].endswith("/agentic/quotes")


def test_a_quote_carries_an_idempotency_key_and_a_search_does_not(wire):
    """Required by the spec on creates only. A key on a read is noise; its ABSENCE on a create is
    a duplicate quote every time a response is lost."""
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    _run(rc.search_products(query="x"))
    assert "Idempotency-Key" in wire.calls[0]["headers"]
    assert "Idempotency-Key" not in wire.calls[1]["headers"]


def test_the_idempotency_key_is_stable_for_the_same_cart(wire, monkeypatch):
    # Time is frozen rather than left to the wall clock: the key is bucketed by time, so two real
    # calls that straddle a bucket boundary would fail this test roughly once in every few
    # thousand runs. A rare flake in a test about retries is worse than no test.
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    for _ in range(2):
        _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["headers"]["Idempotency-Key"] == wire.calls[1]["headers"]["Idempotency-Key"]
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 2}], email="b@example.com"))
    assert wire.calls[2]["headers"]["Idempotency-Key"] != wire.calls[0]["headers"]["Idempotency-Key"]


def test_the_quote_body_is_exactly_the_spec_shape(wire):
    _run(rc.request_quote(
        items=[{"variantId": "var_standard140", "quantity": 1}],
        email="buyer@example.com", shipping_address=GOOD_ADDRESS,
    ))
    body = wire.calls[0]["body"]
    assert set(body) == {"items", "email", "shippingAddress"}
    assert body["items"] == [{"variantId": "var_standard140", "quantity": 1}]
    # The fields the predecessor invented must not be present at ANY level: Reap accepts unknown
    # keys silently with a 200 and drops them, so a stray key is invisible in production.
    flat = json.dumps(body)
    for invented in ("ucpItemId", "source", "CLIENT_SUPPLIED_UCP", "attribution", "merchant"):
        assert invented not in flat


def test_an_address_key_outside_the_spec_is_dropped_not_forwarded(wire):
    _run(rc.request_quote(
        items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com",
        shipping_address={**GOOD_ADDRESS, "dateOfBirth": "1990-01-01", "notes": "leak me"},
    ))
    flat = json.dumps(wire.calls[0]["body"])
    assert "dateOfBirth" not in flat and "leak me" not in flat


def test_an_incomplete_address_refuses_rather_than_sending_a_partial(wire):
    with pytest.raises(rc.ReapRequestError) as exc:
        _run(rc.request_quote(
            items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com",
            shipping_address={"city": "San Francisco", "country": "US"},
        ))
    assert "firstName" in str(exc.value)
    assert wire.calls == []


def test_a_4xx_returns_a_result_and_does_not_carry_the_partner_body(wire, caplog):
    wire.next_status = 422
    wire.next_payload = {"errors": [{"path": "items.0.variantId", "message": "..."},
                                    {"echo": {"shippingAddress": GOOD_ADDRESS}}]}
    got = _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert not got.ok and got.status == 422 and got.error == "reap_status_422"
    assert got.data == {}
    assert "Brannan" not in caplog.text


def test_search_warnings_are_surfaced_rather_than_swallowed_by_a_200(wire):
    """`MERCHANT_NOT_FOUND` arrives as a warning on an otherwise clean 200 for every
    merchantPreference value tried in sandbox. A caller reading only the status records a
    success for a search that ignored its merchant scope."""
    wire.next_payload = {"products": [], "warnings": ["MERCHANT_NOT_FOUND: fentybeauty.com"]}
    got = _run(rc.search_products(query="x", merchant_name="fentybeauty.com"))
    assert got.ok and got.warnings == ["MERCHANT_NOT_FOUND: fentybeauty.com"]


# --- the whole chain --------------------------------------------------------------------------

def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _chain(monkeypatch, *, variant=None, details=None, search=None):
    async def fake(path, body, **kw):
        if path.endswith("/search"):
            return rc.ReapResponse(ok=True, status=200, data=search or FENTY_SEARCH)
        if path.endswith("/details"):
            return rc.ReapResponse(ok=True, status=200, data=details or FENTY_DETAILS)
        if path.endswith("/variant"):
            fake.variant_body = body
            return rc.ReapResponse(ok=True, status=200, data=variant or STANDARD_VARIANT)
        raise AssertionError(f"unexpected path {path}")
    fake.variant_body = None
    monkeypatch.setattr(rc, "_post", fake)
    return fake


def test_the_chain_resolves_our_standard_row_to_reaps_standard_variant(monkeypatch):
    fake = _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
    ))
    assert got.ok
    assert got.variant_id == "var_standard140"
    assert got.price == (140.0, "USD")
    assert got.matched_options == {"Size": "Standard"}
    assert got.chosen_available == {"Size": True}
    assert got.resolved_at is not None
    # It asked for the option we matched, not for a default.
    assert fake.variant_body == {"productId": "prd_dfe0ceb2123640d496edcd7c21b94c0b",
                                 "optionIds": ["opt_standard"]}


def test_our_140_row_does_not_disagree_with_reaps_140_standard(monkeypatch):
    """The whole point of the rebuild's mapping rule. Naively comparing our row to what Reap
    SHOWS (`previewVariant` $95.00, availability-ordered) reads as 32% staleness on a row that is
    correct. Resolving the variant first makes the prices agree to the cent.

    `currency` is supplied because agreement now requires it: an amount without a currency is not
    a price, and this module will not confirm a row from a bare number."""
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00, currency="USD",
    ))
    assert got.ok and got.price_disagrees is False
    # `available` is Reap's view of the variant WE ASKED FOR, taken from the response to a
    # request we verified — not from whatever came back.
    assert got.available is False


def test_a_genuine_price_disagreement_is_reported_and_not_acted_on(monkeypatch):
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=99.00,
    ))
    assert got.ok is True and got.price_disagrees is True
    assert got.variant_id == "var_standard140"  # reported, still resolved -- the caller decides


def test_the_chain_never_falls_back_to_the_default_variant(monkeypatch):
    """The single most important negative in this file. `defaultVariant` in the details fixture
    is the $95 Mini. A title we cannot match must produce a refusal, NOT that id."""
    fake = _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="100ml Bottle",
    ))
    assert not got.ok
    assert got.reason == "options:axes_not_determined_by_title"
    assert got.variant_id is None
    assert fake.variant_body is None  # it never even asked


def test_a_missing_variant_title_refuses_instead_of_resolving_something(monkeypatch):
    _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
    ))
    assert not got.ok and got.reason == "options:no_variant_title_supplied"


def test_a_wrong_merchant_stops_the_chain_before_details(monkeypatch):
    calls = []

    async def fake(path, body, **kw):
        calls.append(path)
        return rc.ReapResponse(ok=True, status=200, data=FENTY_SEARCH)
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard",
    ))
    assert not got.ok and got.reason == "search:merchant_not_in_results"
    assert calls == ["/agentic/products/search"]


# --- the substitution: a 200 that did not do what we asked -------------------------------------
#
# Measured 8 Sep, and the single most dangerous thing found in the sandbox. Resolving an option
# value whose `available` flag is false returns 200 with a DIFFERENT variant and no warning.

MINI_SUBSTITUTED = {
    "id": "var_bb8b0001", "name": "Mini",
    "options": [{"name": "Size", "value": "Mini"}],
    "price": {"amount": 95.0, "currency": "USD"},
    "available": True, "requiresShipping": True, "media": [],
}


def test_a_substituted_variant_is_refused_even_though_reap_returned_200(monkeypatch):
    """We ask for Standard ($140, unavailable). Reap answers 200 with Mini ($95). Trusting the
    status quotes the buyer a different physical object at a 32% lower price, with a clean
    success in hand and nothing in the response saying otherwise."""
    _chain(monkeypatch, variant=MINI_SUBSTITUTED, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
    ))
    assert not got.ok
    assert got.reason == "variant:substituted_on_axis:Size:asked=Standard:got=Mini"
    assert got.variant_id is None


def test_the_substitution_check_names_what_was_asked_and_what_came_back():
    """The refusal has to be legible to a human at a glance: this is a partner-behaviour finding,
    and a bare `variant_mismatch` would send someone back to the sandbox to rediscover it."""
    reason = rc.variant_matches_request(MINI_SUBSTITUTED, {"Size": "Standard"})
    assert reason == "substituted_on_axis:Size:asked=Standard:got=Mini"


def test_a_response_missing_the_axis_entirely_is_also_refused():
    assert rc.variant_matches_request({"id": "var_x", "options": []}, {"Size": "Standard"}) \
        == "response_missing_axis:Size"


def test_a_variant_that_does_match_passes_the_check():
    assert rc.variant_matches_request(STANDARD_VARIANT, {"Size": "Standard"}) is None


def test_the_check_is_case_and_punctuation_insensitive():
    """Reap echoes the label it holds, which need not be byte-identical to the one we sent."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "STANDARD"}]}
    assert rc.variant_matches_request(variant, {"Size": "Standard"}) is None


def test_every_axis_is_checked_not_just_the_first():
    variant = {"id": "var_x", "options": [
        {"name": "Size", "value": "Standard"}, {"name": "Color", "value": "Blue"}]}
    assert rc.variant_matches_request(variant, {"Size": "Standard", "Color": "Rose"}) \
        == "substituted_on_axis:Color:asked=Rose:got=Blue"


# --- the option-less product: the one legitimate read of defaultVariant --------------------------

SINGLE_VARIANT_DETAILS = {
    "products": [{
        "id": "prd_single", "merchant": {"name": "cosrx.com"},
        "name": "Snail Mucin Essence", "media": [], "options": [],
        "defaultVariant": {"id": "var_only", "name": "Default",
                           "options": [], "price": {"amount": 31.80, "currency": "USD"},
                           "available": True, "media": []},
    }],
    "errors": [],
}

SINGLE_VARIANT_SEARCH = {
    "id": "qry_2",
    "products": [{"id": "prd_single", "merchant": {"name": "cosrx.com"},
                  "name": "Snail Mucin Essence",
                  "priceRange": {"min": {"amount": 31.8, "currency": "USD"},
                                 "max": {"amount": 31.8, "currency": "USD"}}}],
    "pagination": {"nextCursor": None, "hasNextPage": False, "returnedCount": 1},
    "warnings": [],
}


def test_an_option_less_product_uses_its_default_variant_without_calling_variant(monkeypatch):
    """`/agentic/products/variant` cannot serve this case at all: an empty `optionIds` is a 422.
    With no sibling variants there is no availability ordering and so no substitution possible --
    which is why this read of `defaultVariant` is not the fallback the module refuses elsewhere."""
    fake = _chain(monkeypatch, search=SINGLE_VARIANT_SEARCH, details=SINGLE_VARIANT_DETAILS)
    got = _run(rc.resolve_our_row(
        merchant_domain="cosrx.com", product_name="Snail Mucin Essence", our_price=31.80,
        currency="USD",
    ))
    assert got.ok and got.variant_id == "var_only"
    assert got.single_variant_product is True
    assert got.price == (31.80, "USD") and got.price_disagrees is False
    assert fake.variant_body is None  # never called — an empty optionIds is a 422


def test_an_option_less_product_needs_no_variant_title(monkeypatch):
    """There is nothing for a title to disambiguate, so requiring one would refuse every
    single-variant product on the index."""
    _chain(monkeypatch, search=SINGLE_VARIANT_SEARCH, details=SINGLE_VARIANT_DETAILS)
    got = _run(rc.resolve_our_row(merchant_domain="cosrx.com", product_name="Snail Mucin Essence"))
    assert got.ok


def test_a_product_with_axes_still_refuses_without_a_title(monkeypatch):
    """The single-variant path must not become a hole in the rule it sits next to."""
    _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum"))
    assert not got.ok and got.reason == "options:no_variant_title_supplied"


def test_a_single_variant_product_without_a_reap_id_refuses(monkeypatch):
    details = json.loads(json.dumps(SINGLE_VARIANT_DETAILS))
    details["products"][0]["defaultVariant"]["id"] = "41669483823149"
    _chain(monkeypatch, search=SINGLE_VARIANT_SEARCH, details=details)
    got = _run(rc.resolve_our_row(merchant_domain="cosrx.com", product_name="Snail Mucin Essence"))
    assert not got.ok and got.reason == "single_variant_product_has_no_variant_id"


# --- timeouts: a bound only a live call could find ------------------------------------------------

def test_a_quote_is_GIVEN_a_timeout_long_enough_for_a_real_quote(wire):
    """Quotes take 13-16 s measured across nine merchants, because Reap is talking to the
    merchant's own commerce layer while we wait. The 12 s default this module shipped with would
    have timed out EVERY quote while every test stayed green.

    This asserts on the value that reaches the transport, not on what the helper returns. An
    earlier version checked `default_timeout_for(...) >= 30` and a mutant that ignored the helper
    inside `_post` survived it -- the helper was right and nothing used it."""
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["timeout"] >= 30.0


def test_a_product_call_is_given_the_shorter_timeout(wire):
    """Same assertion from the other side: the quote bound must not leak onto the product
    endpoints. RAISED 12 -> 25 on 18 Sep: a live sandbox run had one `search` exceed 12 s and one
    `/variant` exceed 30 s, each ending the whole resolution as `transport_error:ReadTimeout` --
    a refusal that reads like a matching failure and is not one."""
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["timeout"] == 25.0


def test_the_product_timeout_covers_what_the_live_run_measured(wire):
    """Pinned as a number, not as "greater than the old one": the point of the change is that 12 s
    was BELOW an observed call and 25 s is above it. A mutant that nudged it back under would
    otherwise pass a `> 12` assertion."""
    assert rc._DEFAULT_TIMEOUT_S >= 25.0
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["timeout"] >= 25.0


def test_the_two_paths_really_do_get_different_timeouts(wire):
    _run(rc.search_products(query="x"))
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["timeout"] < wire.calls[1]["timeout"]


def test_an_explicit_timeout_still_wins(wire):
    _run(rc.search_products(query="x", timeout_seconds=3.0))
    assert wire.calls[0]["timeout"] == 3.0


# --- 503 on a quote: a per-merchant signal, not an outage -------------------------------------------

def test_a_503_on_a_quote_is_flagged_as_probably_not_completable(wire):
    """Measured: the two merchants of nine that 503 are the two that are not UCP merchants, and
    Reap's coverage is scoped to UCP merchants. A serving path that reads this as an outage
    retries a merchant that will never quote."""
    wire.next_status = 503
    got = _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert not got.ok and got.status == 503
    assert got.merchant_probably_not_completable is True


def test_a_503_on_a_product_endpoint_is_not_read_as_a_merchant_verdict(wire):
    """The inference is about quoting. A 503 on search is an outage like any other, and treating
    it as a merchant verdict would suppress merchants on an unrelated failure."""
    wire.next_status = 503
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.merchant_probably_not_completable is False


def test_other_failures_are_not_flagged_as_merchant_verdicts(wire):
    wire.next_status = 400  # AGENTIC_REQUEST_REJECTED — e.g. a non-domestic shipping address
    got = _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert got.merchant_probably_not_completable is False


# --- the amount breakdown's one uneven field ----------------------------------------------------

QUOTE_200 = {
    "id": "f1e2d3c4", "shippingOptions": [
        {"id": "ship_std", "name": "Standard", "selected": True,
         "price": {"amount": 0.0, "currency": "USD"}}],
    "amountBreakdown": {
        "itemsSubtotal": {"amount": 140.0, "currency": "USD"},
        "shipping": {"amount": 0.0, "currency": "USD"},
        # Nested ONE LEVEL DEEPER than its siblings.
        "tax": {"amount": {"amount": 12.60, "currency": "USD"}, "includedInPrices": False},
        "discounts": [], "additionalCharges": [],
        "finalAmount": {"amount": 152.60, "currency": "USD"},
    },
    "expiresAt": "2026-09-08T21:00:00Z",
}


def test_the_total_is_read_from_final_amount():
    assert rc.quote_total(QUOTE_200) == (152.60, "USD")


def test_tax_is_read_from_the_level_it_actually_lives_at():
    """`itemsSubtotal.amount`, `shipping.amount` and `finalAmount.amount` are flat; tax is
    `tax.amount.amount`. A uniform rule over the breakdown silently mis-reads it."""
    assert rc.quote_tax(QUOTE_200) == (12.60, "USD")


def test_a_missing_tax_block_is_none_rather_than_zero():
    """Absent tax and zero tax are different claims, and a total that quietly reads absent as
    zero is the shape of a money bug."""
    payload = json.loads(json.dumps(QUOTE_200))
    payload["amountBreakdown"].pop("tax")
    assert rc.quote_tax(payload) is None


def test_amounts_are_read_as_major_units_not_minor():
    """Decimal major units as JSON numbers, not minor-unit integers. Reading 152.60 as cents
    would be a 100x error in the direction that looks plausible."""
    total = rc.quote_total(QUOTE_200)
    assert total and abs(total[0] - 152.60) < 0.001


def test_a_boolean_is_not_accepted_as_an_amount():
    """`isinstance(True, int)` is True in Python, so a bool would otherwise become 1.0."""
    assert rc._price_of({"price": {"amount": True, "currency": "USD"}}) is None


# --- the unavailable option: refuse BEFORE /variant, and say why -------------------------------
#
# The real Fenty row. Reap marks Size=Standard `available: false`, and asking `/variant` for it
# returns 200 with the Mini sibling. So the id we want cannot be fetched at all, and the correct
# move is to stop before the call rather than to inspect what comes back.

def test_an_unavailable_chosen_value_refuses_without_calling_variant(monkeypatch):
    fake = _chain(monkeypatch)   # FENTY_DETAILS: Standard available=False, as measured
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
    ))
    assert not got.ok
    assert got.reason == "options:value_unavailable_at_reap:Size:Standard"
    assert fake.variant_body is None       # never asked — Reap would have substituted
    assert got.variant_id is None


def test_the_unavailable_refusal_is_distinguishable_from_a_failure_to_resolve(monkeypatch):
    """Commercially these are different facts. "Reap will not sell this today" leaves our row
    correct and the referral link working; "we could not match it" is our problem to fix. A
    single generic refusal would collapse them and invite someone to 'correct' a right row."""
    _chain(monkeypatch)
    unavailable = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard"))
    unmatchable = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="100ml"))
    assert unavailable.reason.startswith("options:value_unavailable_at_reap")
    assert unmatchable.reason == "options:axes_not_determined_by_title"
    assert unavailable.available is False


def test_the_availability_of_the_value_we_asked_for_is_carried_out(monkeypatch):
    """The previous version's docstring claimed availability was "reported, not enforced" while
    nothing reported it: the flag was read and dropped, and the `available` a caller saw came
    from the SUBSTITUTED variant. An unavailable Standard surfaced as an available Mini."""
    _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard"))
    assert got.chosen_available == {"Size": False}


def test_select_option_ids_reports_availability_per_axis():
    got = rc.select_option_ids(FENTY_DETAILS["products"][0], ["Standard"])
    assert got.ok
    assert got.chosen_available == {"Size": False}
    assert got.unavailable_axes == ["Size"]


def test_an_available_value_is_not_flagged():
    got = rc.select_option_ids(FENTY_DETAILS["products"][0], ["Mini"])
    assert got.ok and got.unavailable_axes == [] and got.chosen_available == {"Size": True}


def test_a_value_with_no_availability_flag_is_not_treated_as_unavailable():
    """Absent is not False. `available` is optional in the schema, and defaulting a missing flag
    to unavailable would refuse every product that omits it."""
    product = json.loads(json.dumps(FENTY_DETAILS["products"][0]))
    product["options"][0]["values"][0].pop("available")
    got = rc.select_option_ids(product, ["Standard"])
    assert got.ok and got.unavailable_axes == [] and got.chosen_available == {"Size": None}


# --- Reap's ids are session handles, not identity ------------------------------------------------

def test_a_resolution_is_stamped_with_when_it_was_minted(monkeypatch):
    """Five searches for the same product on one day returned five different `prd_...` ids, each
    with its own `var_...` set. They stay quotable for hours, so they are durable handles — but
    they are not an identity, and anything that stores one on a catalog row is storing a session
    handle in a column that reads like a foreign key. The timestamp is what lets a caller see how
    old a handle is."""
    import time as _time
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    before = _time.time()
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard"))
    assert got.ok and before <= got.resolved_at <= _time.time()


# --- the idempotency key must not replay an EXPIRED quote -------------------------------------

def test_a_retry_seconds_later_replays_the_same_quote():
    """The property we want: a retry after a lost response must not mint a second quote."""
    body = {"items": [{"variantId": "var_x", "quantity": 1}], "email": "b@example.com"}
    assert rc.idempotency_key("quotes", body, now=1_000_000.0) == \
           rc.idempotency_key("quotes", body, now=1_000_010.0)


def test_the_same_cart_tomorrow_is_not_treated_as_a_retry():
    """Reap retains a key for 24 h; a quote's `expiresAt` is about 5 minutes. A key derived from
    the body ALONE replays a long-dead quote — the caller gets a 200 carrying an expired
    `expiresAt` and prices that may have moved. This is the failure the first version had."""
    body = {"items": [{"variantId": "var_x", "quantity": 1}], "email": "b@example.com"}
    day = 24 * 60 * 60
    assert rc.idempotency_key("quotes", body, now=1_000_000.0) != \
           rc.idempotency_key("quotes", body, now=1_000_000.0 + day)


def test_the_bucket_is_shorter_than_a_quotes_lifetime():
    """A replayed key must only ever be able to return a quote still inside its ~5 minute
    window, so the bucket has to be shorter than that — not merely shorter than 24 h."""
    assert 0 < rc._IDEMPOTENCY_BUCKET_S < 300


def test_different_carts_never_share_a_key():
    a = {"items": [{"variantId": "var_x", "quantity": 1}], "email": "b@example.com"}
    b = {"items": [{"variantId": "var_x", "quantity": 2}], "email": "b@example.com"}
    assert rc.idempotency_key("quotes", a, now=1.0) != rc.idempotency_key("quotes", b, now=1.0)


# --- a single-value axis is compared, not assumed ------------------------------------------------
#
# Measured on flowerbeauty.com "Petal Pout Lip Color": Reap indexes the per-shade page as its own
# product, with ONE `Shade` axis carrying ONE value, `"Flamingo Flirt - Cream"`. Our row's title
# is "Flamingo Flirt" — no finish suffix — so EXACT label matching refuses a row that has exactly
# one possible answer, and loosening it to containment is right.
#
# CORRECTED. This section used to say a single-value axis is "determined" and that refusing there
# "protects nothing", and the code accepted such an axis without looking at our title at all.
# That is false, and B1 is what it cost: one value on an axis means there is nothing to choose
# BETWEEN, not that Reap's one value is the thing our row describes. The measured counter-example
# is a `Size` axis carrying only `Mini` ($95) against our $140 Standard row — accepted, resolved,
# and then invisible to the substitution guard, because `chosen` had recorded Reap's own label.

FLOWER_DETAILS = {
    "products": [{
        "id": "prd_petalpout", "merchant": {"name": "flowerbeauty.com"},
        "name": "Petal Pout Lip Color", "media": [],
        "options": [{"name": "Shade", "values": [
            {"optionId": "opt_flamingo", "label": "Flamingo Flirt - Cream", "available": True},
        ]}],
        "defaultVariant": {"id": "var_flamingo", "name": "Flamingo Flirt - Cream",
                           "options": [{"name": "Shade", "value": "Flamingo Flirt - Cream"}],
                           "price": {"amount": 8.00, "currency": "USD"},
                           "available": True, "media": []},
    }],
    "errors": [],
}

FLOWER_SEARCH = {
    "id": "qry_3",
    "products": [{"id": "prd_petalpout", "merchant": {"name": "flowerbeauty.com"},
                  "name": "Petal Pout Lip Color",
                  "priceRange": {"min": {"amount": 8.0, "currency": "USD"},
                                 "max": {"amount": 8.0, "currency": "USD"}}}],
    "pagination": {"nextCursor": None, "hasNextPage": False, "returnedCount": 1},
    "warnings": [],
}

FLOWER_VARIANT = {
    "id": "var_flamingo", "name": "Flamingo Flirt - Cream",
    "options": [{"name": "Shade", "value": "Flamingo Flirt - Cream"}],
    "price": {"amount": 8.00, "currency": "USD"}, "available": True, "media": [],
}


def test_an_axis_with_one_value_is_determined_even_when_the_label_differs():
    """The real case, and the reason the comparison is containment rather than equality: our
    title is a PREFIX of Reap's label, which carries a finish suffix our catalog does not store."""
    got = rc.select_option_ids(FLOWER_DETAILS["products"][0],
                               rc.variant_title_tokens("Flamingo Flirt"))
    assert got.ok and got.option_ids == ["opt_flamingo"]
    # `chosen` records REAP's label, because Reap's optionId is what we send and Reap's label is
    # what the response echoes. That is sound only because the label was checked against our title
    # first — before the B1 fix nothing compared them, and `chosen` made the guard downstream
    # compare Reap to Reap.
    assert got.chosen == {"Shade": "Flamingo Flirt - Cream"}
    assert got.single_value_axis_accepted_without_title is False


def test_a_single_value_axis_is_still_compared_to_our_title():
    """B1, INVERTED. This test used to assert the opposite -- that a single-value axis "needs no
    title at all" -- and it was the false claim that let the defect through review.

    A single-value axis is determined in the sense that there is nothing to choose BETWEEN. It is
    not determined in the sense that matters: it does not establish that Reap's one value is the
    thing our row describes. Accepting it unseen meant our title never constrained the result, and
    `chosen` then recorded Reap's own label, so `variant_matches_request` compared Reap against
    Reap and could not refuse anything."""
    got = rc.select_option_ids(FLOWER_DETAILS["products"][0], ["anything"])
    assert not got.ok
    assert got.reason == "axes_not_determined_by_title"
    assert got.unmatched_axes == ["Shade"]


def test_the_single_value_axis_hole_would_have_resolved_the_95_dollar_mini_for_our_140_row():
    """THE B1 REPRODUCTION, and the reason this is not a style point. Reap indexes this product
    with one `Size` axis carrying one value, `Mini` at $95. Our row is the $140 Standard.

    Before the fix this returned ok=True with `opt_mini`, and `chosen` recorded "Mini" — so the
    substitution guard was then handed "Mini" as the thing we had asked for, saw "Mini" come back,
    and reported a match. The $95-for-$140 error the whole file is built to prevent, reached
    without any substitution taking place, because the request had been rewritten to agree with
    the answer."""
    mini_only = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"label": "Mini", "optionId": "opt_mini", "available": True}]}]}
    got = rc.select_option_ids(mini_only, rc.variant_title_tokens("Standard"))
    assert not got.ok
    assert got.reason == "axes_not_determined_by_title"
    assert got.option_ids == []
    assert "Size" not in got.chosen

    # And the circularity it produced: had it been accepted, the guard could never have refused.
    reap_answered_mini = {"id": "var_mini", "options": [{"name": "Size", "value": "Mini"}]}
    assert rc.variant_matches_request(reap_answered_mini, {"Size": "Mini"}) is None


def test_a_single_value_axis_accepts_when_reaps_label_is_inside_our_title():
    """Containment BOTH ways round. Our catalog sometimes carries the longer string: a row titled
    "Flamingo Flirt Cream Finish" against Reap's bare "Flamingo Flirt" is the same product."""
    product = json.loads(json.dumps(FLOWER_DETAILS["products"][0]))
    product["options"][0]["values"][0]["label"] = "Flamingo Flirt"
    got = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt Cream Finish"))
    assert got.ok and got.option_ids == ["opt_flamingo"]


def test_a_single_value_axis_with_no_title_resolves_only_when_there_is_one_axis():
    """The one shape where a missing title is not a refusal: one axis, one value, so there is
    exactly one variant and nothing to disambiguate — structurally the same case as a product
    with no axes at all. The acceptance is FLAGGED, because it is the one acceptance in this
    function that our title did not constrain."""
    got = rc.select_option_ids(FLOWER_DETAILS["products"][0], [])
    assert got.ok and got.option_ids == ["opt_flamingo"]
    assert got.single_value_axis_accepted_without_title is True


def test_a_second_single_value_axis_still_needs_a_title():
    """Two single-value axes is no longer "one variant, nothing to choose": it is a product whose
    identity our untitled row does not pin, and the structural argument does not reach it."""
    product = json.loads(json.dumps(FLOWER_DETAILS["products"][0]))
    product["options"].append({"name": "Size", "values": [
        {"optionId": "opt_full", "label": "Full Size", "available": True}]})
    got = rc.select_option_ids(product, [])
    assert not got.ok and got.reason == "no_variant_title_supplied"


def test_a_multi_value_axis_with_no_title_still_refuses():
    got = rc.select_option_ids(FENTY_DETAILS["products"][0], [])
    assert not got.ok and got.reason == "no_variant_title_supplied"


def test_the_untitled_single_axis_acceptance_is_visible_on_the_resolution(monkeypatch):
    """The flag has to survive to `resolve_our_row`'s caller, which is the only thing that can
    decide whether an unconstrained acceptance is good enough for what it is about to do."""
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color"))
    assert got.ok and got.single_value_axis_accepted_without_title is True

    titled = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt"))
    assert titled.ok and titled.single_value_axis_accepted_without_title is False


def test_a_single_value_axis_still_reports_unavailability():
    """The rule determines WHICH value, not whether Reap will sell it."""
    product = json.loads(json.dumps(FLOWER_DETAILS["products"][0]))
    product["options"][0]["values"][0]["available"] = False
    got = rc.select_option_ids(product, ["Flamingo Flirt"])
    assert got.ok and got.unavailable_axes == ["Shade"]


def test_the_flowerbeauty_row_resolves_end_to_end(monkeypatch):
    fake = _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS,
                  variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt", our_price=8.00, currency="USD",
    ))
    assert got.ok and got.variant_id == "var_flamingo"
    assert got.price == (8.0, "USD") and got.price_disagrees is False
    assert fake.variant_body == {"productId": "prd_petalpout", "optionIds": ["opt_flamingo"]}


def test_the_substitution_guard_compares_against_reaps_label_not_our_title(monkeypatch):
    """Because we send Reap's optionId, the response echoes Reap's label, so the guard compares
    the response to THAT label rather than to our catalog's title — otherwise every correctly
    resolved single-value axis would be refused on the finish suffix alone.

    CORRECTED. The old docstring said this made the guard "compare against what we actually asked
    for", and that was false in the case it was written to justify: when the label had never been
    checked against our title, `chosen` was Reap's answer and the guard compared Reap to Reap. The
    guard is only meaningful because `select_option_ids` now establishes, before writing `chosen`,
    that Reap's label and our title describe the same thing. What this test proves is narrower
    than the old claim: that a DIFFERENT label coming back is still caught."""
    substituted = json.loads(json.dumps(FLOWER_VARIANT))
    substituted["options"] = [{"name": "Shade", "value": "Petal Pink - Matte"}]
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=substituted)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt",
    ))
    assert not got.ok
    assert got.reason.startswith("variant:substituted_on_axis:Shade:asked=Flamingo Flirt - Cream")


# --- search is query-sensitive, and that bites before any matcher runs ---------------------------
#
# Measured on one product: "Flower Beauty lip color" returns flowerbeauty.com; "Petal Pout Lip
# Color" -- the bare product name this module used to send -- returns two hits, NEITHER from
# flowerbeauty.com; "Flower Beauty Petal Pout" returns six, none; the fully specific "Petal Pout
# Lip Color Flamingo Flirt" returns zero. On the strength of the bare name alone I previously
# concluded flowerbeauty.com was not in Reap's index at all. It is.

def test_the_brand_led_phrasing_is_tried_first():
    assert rc.search_queries(product_name="Petal Pout Lip Color", brand="Flower Beauty")[0] \
        == "Flower Beauty Petal Pout Lip Color"


def test_the_bare_name_is_still_tried_as_a_fallback():
    assert "Petal Pout Lip Color" in rc.search_queries(
        product_name="Petal Pout Lip Color", brand="Flower Beauty")


def test_a_brand_and_category_phrasing_is_the_last_resort():
    queries = rc.search_queries(product_name="Petal Pout Lip Color", brand="Flower Beauty",
                                category="lip color")
    assert queries[-1] == "Flower Beauty lip color"


def test_queries_are_deduplicated_without_reordering():
    """With no brand there is only one phrasing, and trying it twice spends a ~9 s search slot
    to learn nothing."""
    assert rc.search_queries(product_name="Snail Mucin") == ["Snail Mucin"]


def test_a_first_query_that_misses_the_merchant_is_retried_with_another(monkeypatch):
    """The whole point. A `merchant_not_in_results` on one phrasing is evidence about the QUERY
    at least as much as about the index."""
    seen = []

    async def fake(path, body, **kw):
        if path.endswith("/search"):
            seen.append(body["query"])
            # The bare name misses the merchant; the brand-led one finds it.
            if body["query"] == "Petal Pout Lip Color":
                return rc.ReapResponse(ok=True, status=200, data={
                    "products": [{"id": "prd_other", "merchant": {"name": "someothershop.com"},
                                  "name": "Petal Pout Lip Color"}],
                    "warnings": []})
            return rc.ReapResponse(ok=True, status=200, data=FLOWER_SEARCH)
        if path.endswith("/details"):
            return rc.ReapResponse(ok=True, status=200, data=FLOWER_DETAILS)
        return rc.ReapResponse(ok=True, status=200, data=FLOWER_VARIANT)
    monkeypatch.setattr(rc, "_post", fake)

    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", variant_title="Flamingo Flirt", our_price=8.00,
    ))
    assert got.ok and got.variant_id == "var_flamingo"
    assert seen[0] == "Flower Beauty Petal Pout Lip Color"
    assert got.queries_tried == ["Flower Beauty Petal Pout Lip Color"]


def test_the_bare_name_alone_would_have_missed_this_row(monkeypatch):
    """The regression this guards: without a brand there is one phrasing, and it is the one
    measured to miss. The refusal must record what was tried so the reason is legible."""
    async def fake(path, body, **kw):
        return rc.ReapResponse(ok=True, status=200, data={
            "products": [{"id": "prd_other", "merchant": {"name": "someothershop.com"},
                          "name": "Petal Pout Lip Color"}], "warnings": []})
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt"))
    assert not got.ok and got.reason == "search:merchant_not_in_results"
    assert got.queries_tried == ["Petal Pout Lip Color"]


def test_the_search_budget_is_bounded(monkeypatch):
    """Search takes up to ~9 s. Three attempts is already a ~27 s worst case before the quote's
    own 13-16 s, so the loop must not simply try everything."""
    calls = []

    async def fake(path, body, **kw):
        calls.append(body.get("query"))
        return rc.ReapResponse(ok=True, status=200, data={"products": [], "warnings": []})
    monkeypatch.setattr(rc, "_post", fake)
    _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", category="lip color", max_search_attempts=2))
    assert len(calls) == 2


def test_a_transport_failure_does_not_burn_the_phrasing_budget(monkeypatch):
    """A 500 or a timeout is not a phrasing problem; retrying phrasings spends the budget on the
    same error and delays the refusal by two more search timeouts."""
    calls = []

    async def fake(path, body, **kw):
        calls.append(body.get("query"))
        return rc.ReapResponse(ok=False, error="transport_error:ReadTimeout")
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", category="lip color"))
    assert not got.ok and got.reason == "transport_error:ReadTimeout"
    assert len(calls) == 1


# --- the subdomain merchants: a mapping the CALLER supplies, not a looser comparison -------------
#
# 8 of 78 merchant names in Reap's index carry a subdomain (`us.refybeauty.com`,
# `shop.simon.com`, `jbbwell.myshopify.com`, ...), each failing against its apex spelling.

def test_an_alias_lets_a_subdomain_merchant_match(monkeypatch):
    payload = {"products": [{"id": "prd_x", "merchant": {"name": "us.refybeauty.com"},
                             "name": "Wet Brush"}], "warnings": []}
    match = rc.match_product(payload, merchant_domain="refybeauty.com",
                             product_name="Wet Brush",
                             also_accept_domains=["us.refybeauty.com"])
    assert match.ok and match.product_id == "prd_x"


def test_without_the_alias_the_subdomain_merchant_still_refuses():
    """The comparison itself stays exact. Loosening it to admit `us.refybeauty.com` for
    `refybeauty.com` would also admit `notcosrx.com` for `cosrx.com`."""
    payload = {"products": [{"id": "prd_x", "merchant": {"name": "us.refybeauty.com"},
                             "name": "Wet Brush"}], "warnings": []}
    match = rc.match_product(payload, merchant_domain="refybeauty.com", product_name="Wet Brush")
    assert not match.ok and match.reason == "merchant_not_in_results"


def test_an_alias_does_not_admit_a_confusable():
    payload = {"products": [{"id": "prd_x", "merchant": {"name": "cosrx.com.evil.io"},
                             "name": "Snail Mucin"}], "warnings": []}
    match = rc.match_product(payload, merchant_domain="cosrx.com", product_name="Snail Mucin",
                             also_accept_domains=["us.cosrx.com", "shop.cosrx.com"])
    assert not match.ok


# --- every refusal has copy, and none of it is a placeholder ------------------------------------
#
# This exists because the probe script held these explanations in a DICT LITERAL, I added a
# second "options" key to it, and Python silently kept the last one — so the careful paragraph I
# had just written was dead and the script printed "See below." for the one refusal that most
# needs explaining. No linter we run flags a duplicate key in a dict literal. The table is now an
# ordered list, which cannot hide one, and these tests keep the copy tracking the vocabulary.

#: Every reason string `resolve_our_row` can return, gathered by hand from the module. If a new
#: refusal is added without copy, `test_every_reason_has_its_own_explanation` fails.
ALL_REASONS = [
    "search:no_products_returned", "search:no_exact_name_match", "search:ambiguous_name_match",
    "search:merchant_not_in_results", "search:product_id_not_in_reap_namespace",
    "options:product_has_no_option_axes", "options:no_variant_title_supplied",
    "options:axes_not_determined_by_title", "options:ambiguous_on_axis:Size",
    "options:duplicate_axis_name:Size",
    "options:value_unavailable_at_reap:Size:Standard",
    "response_too_large",
    "variant:substituted_on_axis:Size:asked=Standard:got=Mini",
    "variant:response_missing_axis:Size",
    "details:PRODUCT_NOT_FOUND", "single_variant_product_has_no_variant_id",
    "resolved_id_not_in_reap_namespace", "reap_status_503",
    "transport_error:ReadTimeout", "reap_client_not_configured",
]


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_every_reason_has_its_own_explanation(reason):
    text = rc.explain_refusal(reason)
    assert text and "Unrecognised refusal" not in text
    # The placeholder that actually shipped, plus the usual stand-ins. Note "..." is NOT in this
    # list: legitimate copy names Reap's id prefixes as `var_...` and `prd_...`, and a check that
    # fires on those trains people to weaken the check rather than fix the copy.
    for placeholder in ("See below", "TODO", "TBD", "FIXME", "XXX"):
        assert placeholder not in text


def test_the_two_options_refusals_do_not_share_copy():
    """The duplicate-key bug in one assertion. These say opposite things — one means our row is
    right and Reap declines to sell it, the other means our title is inadequate — so identical
    copy for both is the failure, not merely untidy."""
    unavailable = rc.explain_refusal("options:value_unavailable_at_reap:Size:Standard")
    unmatched = rc.explain_refusal("options:axes_not_determined_by_title")
    assert unavailable != unmatched
    assert "our row is right" in unavailable
    assert "ours\nto fix" in unmatched


def test_a_specific_reason_beats_its_step_prefix():
    """Ordering, not dict lookup: `options:value_unavailable_at_reap` must not be answered by a
    generic `options:` entry that happens to be listed first."""
    assert rc.explain_refusal("options:value_unavailable_at_reap:Size:Standard") \
        != rc.explain_refusal("options:no_variant_title_supplied")


def test_no_two_entries_share_a_prefix_relationship_in_the_wrong_order():
    """A longer prefix listed AFTER a shorter one it extends is unreachable — the list form's
    equivalent of the duplicate key, and just as silent."""
    prefixes = [p for p, _ in rc.REFUSAL_EXPLANATIONS]
    for i, longer in enumerate(prefixes):
        for shorter in prefixes[:i]:
            assert not longer.startswith(shorter), \
                f"{longer!r} is unreachable: {shorter!r} is listed before it"


def test_an_unknown_reason_says_so_rather_than_guessing():
    assert "Unrecognised" in rc.explain_refusal("something_new_we_have_not_seen")
    assert "Unrecognised" in rc.explain_refusal(None)


# --- the third phrasing is the recall play, and it must not need a category ----------------------
#
# Measured on our real tier-A row. flowerbeauty.com / "Petal Pout Lip Color": the brand-led and
# bare-name phrasings BOTH missed the merchant, and only "Flower Beauty lip color" surfaced it.
# An earlier version generated that slot only when the caller supplied `category` — so a caller
# without one (most of them) lost the phrasing most likely to work, and a resolver holding the
# right rule refused the row anyway.

def test_there_is_no_brand_only_rung():
    """MEASURED, not reasoned. An earlier version filled the third slot with the brand alone when
    no category was given, and I described it in a docstring as returning "a slice of that
    merchant's catalogue" — a prediction written as an observation. Ten live passes across four
    brands: "Flower Beauty" -> 5 hits, none theirs, the same resellers every pass; "Refy" -> 3,
    none theirs; "Fenty Beauty" -> ZERO, while "Fenty Eau de Parfum" returns seven products;
    "COSRX" -> its own store on one pass of two, and not the product we wanted.

    Reap's search is PRODUCT-TEXT retrieval, not merchant retrieval — there is no merchant
    dimension on it anywhere, which is also why `merchantPreference` is non-functional. A rung
    that cannot fire is worse than no rung: it costs a ~9 s search and reads like coverage."""
    assert rc.search_queries(product_name="Petal Pout Lip Color", brand="Flower Beauty") == [
        "Flower Beauty Petal Pout Lip Color", "Petal Pout Lip Color"]


def test_a_category_still_wins_the_third_slot_when_given():
    queries = rc.search_queries(product_name="Petal Pout Lip Color", brand="Flower Beauty",
                                category="lip color")
    assert queries[-1] == "Flower Beauty lip color"


def test_no_brand_means_no_broad_phrasing():
    """A bare category or a lone product name gives nothing to broaden with, and sending the
    product name twice spends a ~9 s search slot to learn nothing."""
    assert rc.search_queries(product_name="Petal Pout Lip Color") == ["Petal Pout Lip Color"]
    assert rc.search_queries(product_name="Petal Pout Lip Color", category="lip color") \
        == ["Petal Pout Lip Color"]


def test_a_caller_without_a_category_is_told_its_recall_was_degraded(monkeypatch):
    """A short ladder must not produce a refusal that reads like an absence. The rung that found
    flowerbeauty.com on both live runs is `<brand> <category>`; without it, `merchant_not_in_
    results` says very little — and that is exactly the inference that got that merchant written
    off in a report."""
    async def fake(path, body, **kw):
        # Resellers, not the merchant — which is exactly what a bare-brand query returns.
        return rc.ReapResponse(ok=True, status=200, data={"products": [
            {"id": "prd_r", "merchant": {"name": "emergscent.com"},
             "name": "Petal Pout Lip Color"}], "warnings": []})
    monkeypatch.setattr(rc, "_post", fake)

    short = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", variant_title="Flamingo Flirt"))
    full = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", category="lip color", variant_title="Flamingo Flirt"))
    category_only = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        category="lip color", variant_title="Flamingo Flirt"))

    assert short.reason == "search:merchant_not_in_results" and short.recall_degraded is True
    assert full.reason == "search:merchant_not_in_results" and full.recall_degraded is False
    # A category with no brand is just as short a ladder: the rung is `<brand> <category>`, so
    # either half missing means it was never sent. A flag keyed on the category alone reads as
    # healthy here and is wrong in the direction that hides the problem.
    assert category_only.recall_degraded is True
    assert category_only.queries_tried == ["Petal Pout Lip Color"]


def test_the_degraded_flag_is_not_set_on_a_success(monkeypatch):
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", category="lip color", variant_title="Flamingo Flirt"))
    assert got.ok and got.recall_degraded is False


def test_the_default_budget_is_large_enough_for_all_three_phrasings():
    """A three-phrasing strategy behind a two-attempt cap is a strategy that does not exist, and
    which rung succeeds varies between runs — so the cap must clear the whole ladder."""
    assert rc.MAX_SEARCH_ATTEMPTS >= 3
    assert len(rc.search_queries(product_name="x", brand="b", category="c")) <= rc.MAX_SEARCH_ATTEMPTS


def test_the_full_ladder_is_sent_when_earlier_rungs_miss(monkeypatch):
    """Reap's search is non-deterministic, and two runs of the SAME row against the SAME code an
    hour apart succeeded on DIFFERENT rungs — the second phrasing once, the third the next time.
    So the later phrasings are not tie-breakers for an unusual row; they are what makes any given
    run land at all. A resolver that stops early is not trading a little recall, it is
    coin-flipping."""
    seen = []

    async def fake(path, body, **kw):
        if path.endswith("/search"):
            seen.append(body["query"])
            if body["query"] != "Flower Beauty lip color":
                return rc.ReapResponse(ok=True, status=200, data={"products": [], "warnings": []})
            return rc.ReapResponse(ok=True, status=200, data=FLOWER_SEARCH)
        if path.endswith("/details"):
            return rc.ReapResponse(ok=True, status=200, data=FLOWER_DETAILS)
        return rc.ReapResponse(ok=True, status=200, data=FLOWER_VARIANT)
    monkeypatch.setattr(rc, "_post", fake)

    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        brand="Flower Beauty", category="lip color", variant_title="Flamingo Flirt",
        our_price=8.00,
    ))
    assert got.ok and got.variant_id == "var_flamingo"
    assert seen == ["Flower Beauty Petal Pout Lip Color", "Petal Pout Lip Color",
                    "Flower Beauty lip color"]


# ==================================================================================================
# ADVERSARIAL REVIEW, PR #2140. Each block below reproduces one finding first and then pins the
# fix. They are grouped by finding id so a reviewer can match them to the report.
# ==================================================================================================


# --- A1: the host allowlist, AT THE TRANSPORT ------------------------------------------------------
#
# The allowlist existed and was tested — against `validate_base_url` in isolation. Nothing tested
# that `_post` calls it, so swapping `validate_base_url()` for `base_url()` on the line above the
# request left every test in this file green while `Authorization: Bearer <our key>` was sent to
# whatever host REAP_API_BASE_URL named. A guard nothing exercises at the seam it protects is a
# guard that can be deleted by accident.

def test_a_host_outside_the_allowlist_is_refused_at_the_transport_with_no_key_sent(wire, monkeypatch):
    monkeypatch.setenv("REAP_API_BASE_URL", "https://evil.example")
    with pytest.raises(rc.ReapConfigError):
        _run(rc.search_products(query="Fenty Eau de Parfum"))
    # Nothing left the process at all — not a request that was later discarded.
    assert wire.calls == []
    # And the key is nowhere in anything the transport saw, by value.
    assert "sk_test_key" not in json.dumps(wire.calls)


def test_a_host_outside_the_allowlist_is_refused_on_a_quote_too(wire, monkeypatch):
    """The quote path builds a body carrying a buyer email before it reaches `_post`. The refusal
    still has to land before egress, or that body goes to the unallowed host as well."""
    monkeypatch.setenv("REAP_API_BASE_URL", "https://reap.global.evil.example")
    with pytest.raises(rc.ReapConfigError):
        _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}],
                              email="buyer@example.com"))
    assert wire.calls == []
    assert "buyer@example.com" not in json.dumps(wire.calls)


def test_the_key_and_the_version_header_reach_the_transport_on_an_allowed_host(wire):
    """The positive half. Without it, a mutation that refused EVERYTHING would pass the two
    negatives above — an absence assertion also passes when the mechanism is absent."""
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["headers"]["Authorization"] == "Bearer sk_test_key"
    assert wire.calls[0]["headers"]["Reap-Version"] == rc.REAP_VERSION
    assert wire.calls[0]["url"].startswith("https://sandbox.api.reap.global/")


def test_the_key_never_appears_in_the_url_or_the_body(wire):
    """It is a header, and only a header. A credential in a path segment or a query string lands
    in every access log between here and the partner."""
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert "sk_test_key" not in wire.calls[0]["url"]
    assert "sk_test_key" not in json.dumps(wire.calls[0]["body"])


# --- A2: redirects are not followed ----------------------------------------------------------------

def test_redirects_are_explicitly_not_followed(wire):
    """Passed explicitly rather than left to httpx's default, and asserted from the CONSTRUCTOR
    kwargs, which is the only place it can be observed. Following a 3xx re-POSTs the body — which
    on a quote carries a buyer's shipping address — to whatever `Location` names, and the host
    allowlist was checked against the URL we chose, never against the one a redirect hands us."""
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["follow_redirects"] is False


def test_every_path_gets_the_same_redirect_policy(wire):
    _run(rc.search_products(query="x"))
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert [c["follow_redirects"] for c in wire.calls] == [False, False]


# --- A4: userinfo in the base URL ------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://u:p@sandbox.api.reap.global",
    "https://u:p@sandbox.api.reap.global/v1",
    "https://token@prod.api.reap.global",
    "https://u:p@evil.example",
])
def test_userinfo_in_the_base_url_is_refused(url):
    """`hostname` on all but the last of these IS an allowlisted Reap host, so every other check
    in `validate_base_url` passed it. httpx then derives `Authorization: Basic <userinfo>` from
    the URL and REPLACES our Bearer header with it: the Reap call 401s and the userinfo we did not
    write travels in place of the key we did."""
    with pytest.raises(rc.ReapConfigError):
        rc.validate_base_url(url)


def test_the_userinfo_refusal_does_not_echo_the_credential():
    """The userinfo is itself a secret. Naming it in the message puts it in the log that the
    message exists to produce."""
    with pytest.raises(rc.ReapConfigError) as exc:
        rc.validate_base_url("https://admin:hunter2@sandbox.api.reap.global")
    assert "hunter2" not in str(exc.value)


def test_userinfo_is_refused_at_the_transport_before_the_key_is_attached(wire, monkeypatch):
    monkeypatch.setenv("REAP_API_BASE_URL", "https://u:p@sandbox.api.reap.global")
    with pytest.raises(rc.ReapConfigError):
        _run(rc.search_products(query="x"))
    assert wire.calls == []


# --- B3: normalisation must not erase non-ASCII ----------------------------------------------------
#
# `_norm` was `[^a-z0-9]+ -> " "` over `.lower()`, which DELETES every non-ASCII character. So any
# two purely non-Latin labels both normalised to "" and compared EQUAL. `variant_matches_request`
# — the most important guard in the file — reported a match for 標準 against ミニ, and for Чёрный
# against Розовый. Silent, and worst exactly where labels are never ASCII.

@pytest.mark.parametrize("asked,got", [
    ("標準", "ミニ"),          # JP: Standard vs Mini
    ("チェリー", "ピーチ"),      # JP: Cherry vs Peach
    ("Чёрный", "Розовый"),   # RU: Black vs Pink
    ("블랙", "화이트"),          # KR: Black vs White
])
def test_two_different_non_ascii_labels_are_not_a_match(asked, got):
    variant = {"id": "var_x", "options": [{"name": "Size", "value": got}]}
    assert rc.variant_matches_request(variant, {"Size": asked}) == \
        f"substituted_on_axis:Size:asked={asked}:got={got}"


@pytest.mark.parametrize("label", ["標準", "Чёрный", "블랙", "Crème"])
def test_a_non_ascii_label_survives_normalisation_as_itself(label):
    """The other half: they must not merely differ from each other, they must be non-empty. Two
    labels that both normalise to "" differ from nothing and match everything."""
    assert rc._norm(label) != ""


def test_the_same_non_ascii_label_still_matches_itself():
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "標準"}]}
    assert rc.variant_matches_request(variant, {"Size": "標準"}) is None


def test_accents_fold_so_a_decomposed_spelling_matches_a_precomposed_one():
    """"Crème Brûlée" used to normalise to "cr me br l e" and did NOT match "Creme Brulee" — the
    opposite failure from the same rule, refusing a row that was right."""
    assert rc._norm("Crème Brûlée") == rc._norm("Creme Brulee") == "creme brulee"
    variant = {"id": "var_x", "options": [{"name": "Shade", "value": "Crème Brûlée"}]}
    assert rc.variant_matches_request(variant, {"Shade": "Creme Brulee"}) is None


def test_an_empty_normalisation_is_never_a_match_on_either_side():
    """The guard at the COMPARISON SITE, not in `_norm`. Punctuation-only labels still normalise
    to "" under the new rule, and `"" == ""` is how the whole non-ASCII class of bug passed."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "---"}]}
    assert rc.variant_matches_request(variant, {"Size": "***"}) is not None
    assert rc.variant_matches_request(variant, {"Size": "Standard"}) is not None
    assert rc.variant_matches_request({"id": "var_x", "options": [{"name": "S", "value": "Mini"}]},
                                      {"S": "!!!"}) is not None


def test_a_non_ascii_option_axis_still_selects_by_label():
    """End of the same thread at the other end of the join: with `_norm` erasing them, every
    non-ASCII label matched every non-ASCII title here too."""
    product = {"id": "prd_jp", "options": [{"name": "サイズ", "values": [
        {"optionId": "opt_std", "label": "標準", "available": True},
        {"optionId": "opt_mini", "label": "ミニ", "available": True},
    ]}]}
    assert rc.select_option_ids(product, ["標準"]).option_ids == ["opt_std"]
    assert rc.select_option_ids(product, ["ミニ"]).option_ids == ["opt_mini"]
    assert not rc.select_option_ids(product, ["トラベル"]).ok


def test_ascii_normalisation_is_unchanged():
    """The fix must not move the cases the module already gets right."""
    assert rc._norm("Fenty Eau de Parfum") == "fenty eau de parfum"
    assert rc._norm("Flamingo Flirt - Cream") == "flamingo flirt cream"
    assert rc._norm("  STANDARD  ") == "standard"
    assert rc._norm("75ML + Tray") == "75ml tray"
    assert rc._norm(None) == "" and rc._norm("") == ""


# --- B4: a price is an amount AND a currency -------------------------------------------------------

def test_reaps_euro_price_does_not_agree_with_our_dollar_price():
    """The reproduction. €140.00 vs $140.00 reported agreement — and silence here is the answer
    that means "our row is confirmed", so the one comparison a human relies on to catch a
    mispriced row was the one guaranteed to pass."""
    assert rc._disagrees((140.0, "EUR"), 140.00, "USD") is True
    assert rc._disagrees((140.0, "USD"), 140.00, "USD") is False


@pytest.mark.parametrize("reap_currency,our_currency", [
    ("", "USD"), ("USD", ""), ("", ""), ("USD", None), (None, "USD"),
])
def test_an_unknown_currency_on_either_side_is_a_disagreement_not_an_agreement(
        reap_currency, our_currency):
    """Unknown is not agreement. Two bare numbers establish nothing, and reporting nothing is
    reporting that the row checks out."""
    assert rc._disagrees((140.0, reap_currency), 140.00, our_currency) is True


def test_currency_case_and_padding_do_not_create_a_false_mismatch():
    assert rc._disagrees((140.0, " usd "), 140.00, "USD") is False


def test_no_price_on_either_side_is_still_not_a_disagreement():
    """A caller that passed no `our_price` asked for no comparison at all, which is different from
    a comparison it cannot make."""
    assert rc._disagrees(None, 140.00, "USD") is False
    assert rc._disagrees((140.0, "USD"), None, "USD") is False


def test_a_currency_mismatch_is_reported_separately_from_a_stale_amount():
    """Different problems, different fixes: no refresh of our amount will make EUR agree with USD,
    and collapsing them into one flag invites someone to "correct" a row whose number is right."""
    assert rc._currency_mismatch((140.0, "EUR"), "USD") is True
    assert rc._currency_mismatch((149.0, "USD"), "USD") is False
    # Unknown is an unknown, not a mismatch — that is `_disagrees`' job.
    assert rc._currency_mismatch((140.0, ""), "USD") is False
    assert rc._currency_mismatch(None, "USD") is False


def test_the_currency_mismatch_reaches_the_resolution(monkeypatch):
    eur = json.loads(json.dumps(STANDARD_VARIANT))
    eur["price"] = {"amount": 140.0, "currency": "EUR"}
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE, variant=eur)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00, currency="USD"))
    assert got.ok
    assert got.currency_mismatch is True
    assert got.price_disagrees is True


def test_a_row_with_no_currency_is_not_silently_confirmed(monkeypatch):
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00))
    assert got.ok and got.price_disagrees is True and got.currency_mismatch is False


def test_the_single_variant_path_compares_currency_too(monkeypatch):
    """The option-less branch has its own `_disagrees` call site. Fixing one and not the other is
    how a guard ends up half-applied."""
    details = json.loads(json.dumps(SINGLE_VARIANT_DETAILS))
    details["products"][0]["defaultVariant"]["price"] = {"amount": 31.80, "currency": "EUR"}
    _chain(monkeypatch, search=SINGLE_VARIANT_SEARCH, details=details)
    got = _run(rc.resolve_our_row(merchant_domain="cosrx.com", product_name="Snail Mucin Essence",
                                  our_price=31.80, currency="USD"))
    assert got.ok and got.price_disagrees is True and got.currency_mismatch is True


# --- B5: two values matching one axis, under the same label ----------------------------------------

def test_two_values_with_the_same_normalised_label_refuse_rather_than_taking_the_last():
    """"Standard" and "STANDARD!" normalise alike, so the old rule ("refuse only if the labels
    DIFFER") saw no conflict and silently kept the LAST one — a coin flip between two distinct
    optionIds, decided by Reap's ordering. They are different variants."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": "Standard", "available": True},
        {"optionId": "opt_b", "label": "STANDARD!", "available": True},
    ]}]}
    got = rc.select_option_ids(product, ["Standard"])
    assert not got.ok
    assert got.reason == "ambiguous_on_axis:Size"
    assert got.option_ids == []


def test_two_byte_identical_labels_on_one_axis_also_refuse():
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": "Standard", "available": True},
        {"optionId": "opt_b", "label": "Standard", "available": True},
    ]}]}
    assert rc.select_option_ids(product, ["Standard"]).reason == "ambiguous_on_axis:Size"


def test_two_different_labels_matching_one_title_still_refuse():
    """The case the old rule did catch. It must keep working."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": "Standard", "available": True},
        {"optionId": "opt_b", "label": "Rose", "available": True},
    ]}]}
    assert rc.select_option_ids(product, ["Standard", "Rose"]).reason == "ambiguous_on_axis:Size"


def test_one_matching_value_among_several_is_not_ambiguous():
    """The control. A rule that refused every axis with more than one value would pass the three
    tests above and break the module."""
    got = rc.select_option_ids(FENTY_DETAILS["products"][0], ["Standard"])
    assert got.ok and got.option_ids == ["opt_standard"]


# --- B6: two axes with the same name ---------------------------------------------------------------

def test_two_axes_with_the_same_name_refuse():
    """`chosen` is a dict keyed on the axis name, and so is the `got` map in
    `variant_matches_request`. Two axes called "Size" collapse to ONE entry — last write wins —
    while both optionIds are still sent. The guard then checks one axis and cannot see the other,
    and the one it checks may not be the one we picked. Reproduced: option_ids had two entries and
    `chosen` had one, and the substitution guard passed on a response naming a single axis."""
    product = {"id": "prd_x", "options": [
        {"name": "Size", "values": [{"optionId": "opt_a", "label": "Standard", "available": True},
                                    {"optionId": "opt_a2", "label": "Mini", "available": True}]},
        {"name": "Size", "values": [{"optionId": "opt_b", "label": "Rose", "available": True},
                                    {"optionId": "opt_b2", "label": "Blue", "available": True}]},
    ]}
    got = rc.select_option_ids(product, ["Standard", "Rose"])
    assert not got.ok
    assert got.reason.startswith("duplicate_axis_name")
    assert got.option_ids == []


def test_two_unnamed_axes_collide_on_the_same_placeholder_and_refuse():
    """A missing name becomes "?", so two of them collide exactly as two "Size" axes do. The
    placeholder must not become a way past the guard."""
    product = {"id": "prd_x", "options": [
        {"values": [{"optionId": "opt_a", "label": "Standard", "available": True},
                    {"optionId": "opt_a2", "label": "Mini", "available": True}]},
        {"values": [{"optionId": "opt_b", "label": "Rose", "available": True},
                    {"optionId": "opt_b2", "label": "Blue", "available": True}]},
    ]}
    assert rc.select_option_ids(product, ["Standard", "Rose"]).reason.startswith(
        "duplicate_axis_name")


def test_distinct_axis_names_are_not_refused():
    """The control: the guard must fire on duplicates only."""
    product = json.loads(json.dumps(FENTY_DETAILS["products"][0]))
    product["options"].append({"name": "Color", "values": [
        {"optionId": "opt_rose", "label": "Rose", "available": True},
        {"optionId": "opt_blue", "label": "Blue", "available": True}]})
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard / Rose"))
    assert got.ok and got.option_ids == ["opt_standard", "opt_rose"]


# --- B8: a null label is no label, not the string "None" -------------------------------------------

def test_a_null_label_on_a_single_value_axis_does_not_become_the_word_None():
    """`str(value.get("label"))` produced the four-character string "None", which went into
    `chosen`, became what the substitution guard checked the response against, and would have been
    shown to a human as the option we picked."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": None, "available": True}]}]}
    got = rc.select_option_ids(product, ["None"])
    assert not got.ok
    assert got.reason == "axes_not_determined_by_title"
    assert "None" not in json.dumps(got.chosen)


def test_a_null_label_is_refused_even_with_no_title_to_compare():
    """The untitled single-axis acceptance is structural, so nothing else would have stopped this
    one: it would have sent an optionId and recorded "None" as the label we asked for."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": None, "available": True}]}]}
    got = rc.select_option_ids(product, [])
    assert not got.ok and got.chosen == {}


@pytest.mark.parametrize("label", [None, "", "   "])
def test_a_blank_label_never_reaches_chosen(label):
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": label, "available": True},
        {"optionId": "opt_b", "label": "Mini", "available": True}]}]}
    got = rc.select_option_ids(product, ["Mini"])
    assert got.ok and got.chosen == {"Size": "Mini"}


# --- C1: untrusted JSON where a dict was assumed ----------------------------------------------------

@pytest.mark.parametrize("merchant", [
    "fentybeauty.com",                 # a bare string
    ["fentybeauty.com"],               # a list
    [{"name": "fentybeauty.com"}],     # a list of the right-shaped thing
    42,
    True,
])
def test_a_truthy_non_dict_merchant_refuses_instead_of_raising(merchant):
    """`(p.get("merchant") or {}).get("name")` is only safe for a dict or a falsy value. A truthy
    non-dict raised AttributeError all the way out of `resolve_our_row` — a crash, in a module
    where every other malformation is a refusal, triggered by data a partner controls."""
    payload = {"products": [{"id": "prd_x", "merchant": merchant, "name": "Fenty Eau de Parfum"}]}
    match = rc.match_product(payload, merchant_domain="fentybeauty.com",
                             product_name="Fenty Eau de Parfum")
    assert not match.ok and match.reason == "merchant_not_in_results"


def test_one_malformed_row_does_not_hide_a_good_one():
    """Skip the row, do not abandon the response. The old code took the whole search down with
    the first bad entry."""
    payload = {"products": [
        {"id": "prd_bad", "merchant": "fentybeauty.com", "name": "Fenty Eau de Parfum"},
        {"id": "prd_good", "merchant": {"name": "fentybeauty.com"},
         "name": "Fenty Eau de Parfum"},
    ]}
    match = rc.match_product(payload, merchant_domain="fentybeauty.com",
                             product_name="Fenty Eau de Parfum")
    assert match.ok and match.product_id == "prd_good"


def test_a_malformed_merchant_does_not_crash_the_whole_chain(monkeypatch):
    async def fake(path, body, **kw):
        return rc.ReapResponse(ok=True, status=200, data={
            "products": [{"id": "prd_x", "merchant": ["fentybeauty.com"],
                          "name": "Fenty Eau de Parfum"}], "warnings": []})
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(merchant_domain="fentybeauty.com",
                                  product_name="Fenty Eau de Parfum", variant_title="Standard"))
    assert not got.ok and got.reason == "search:merchant_not_in_results"


# --- C2: the response is bounded ---------------------------------------------------------------------

def test_a_declared_content_length_over_the_cap_is_refused_without_parsing(wire):
    """The cheap check. Nothing upstream limits what a partner returns, and `resp.json()` on a
    multi-megabyte document allocates the parsed graph on top of the bytes, in a serving path."""
    wire.next_headers = {"content-length": str(rc.MAX_RESPONSE_BYTES + 1)}
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"
    assert got.data == {}


def test_a_body_over_the_cap_is_refused_even_with_no_content_length(wire):
    """The check that cannot be lied about. A declared length is a claim; the bytes are the fact,
    and a chunked response declares nothing at all."""
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"


def test_a_body_over_the_cap_is_refused_even_when_the_header_understates_it(wire):
    wire.next_headers = {"content-length": "12"}
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    assert _run(rc.search_products(query="x")).error == "response_too_large"


def test_a_normal_sized_response_is_not_refused(wire):
    """The control. A cap of zero would pass all three tests above."""
    wire.next_payload = {"products": [], "warnings": []}
    wire.next_headers = {"content-length": "31"}
    got = _run(rc.search_products(query="x"))
    assert got.ok and got.error is None


def test_a_non_numeric_content_length_does_not_crash_the_parse(wire):
    """A header is partner input too. `int("banana")` here would be an exception in a serving
    path, which is a worse outcome than the size it was checking."""
    wire.next_headers = {"content-length": "banana"}
    assert _run(rc.search_products(query="x")).ok is True


# --- C3: REAP_API_TIMEOUT_SECONDS is a floor, never a ceiling -------------------------------------------

def test_a_low_env_timeout_cannot_shorten_a_quote(wire, monkeypatch):
    """The reproduction. The env var sat AHEAD of the per-path default, so setting it to 10 — a
    reasonable-looking number — cut the quote's 35 s budget to 10 s, and quotes take 13-16 s
    measured. Every quote would have timed out; no test could have seen it, because the value only
    appears at the transport."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "10")
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["timeout"] == 35.0


def test_the_env_timeout_may_raise_a_bound(wire, monkeypatch):
    """A floor is still useful: an operator on a slow link can give every path more room."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "60")
    _run(rc.search_products(query="x"))
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert wire.calls[0]["timeout"] == 60.0
    assert wire.calls[1]["timeout"] == 60.0


@pytest.mark.parametrize("value", ["30s", "", "   ", "abc", "0", "-5", "nan-ish"])
def test_an_unusable_env_timeout_is_ignored_rather_than_fatal(wire, monkeypatch, value):
    """`float("30s")` raised ValueError out of `_post`: a typo in an env var became an exception
    in a serving path, on every request, until someone noticed. Ignored, and the per-path default
    stands."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", value)
    got = _run(rc.search_products(query="x"))
    assert got.ok
    assert wire.calls[0]["timeout"] == rc._DEFAULT_TIMEOUT_S == 25.0


def test_an_explicit_timeout_still_beats_the_env_floor(wire, monkeypatch):
    """The caller's own argument is a decision about one call, and it wins over an ambient one."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "60")
    _run(rc.search_products(query="x", timeout_seconds=3.0))
    assert wire.calls[0]["timeout"] == 3.0


# --- C4: `warnings` must be a list of strings ----------------------------------------------------------

def test_a_bare_string_warning_is_wrapped_not_spelled_out_one_letter_at_a_time(wire):
    """A string is iterable, so `[str(w) for w in data["warnings"]]` turned "MERCHANT_NOT_FOUND"
    into eighteen single-character warnings. Not merely ugly: MERCHANT_NOT_FOUND is the signal
    that a search silently ignored its merchant scope, and nothing reading these would have
    recognised it spelled one letter per entry."""
    wire.next_payload = {"products": [], "warnings": "MERCHANT_NOT_FOUND"}
    got = _run(rc.search_products(query="x"))
    assert got.ok and got.warnings == ["MERCHANT_NOT_FOUND"]


@pytest.mark.parametrize("value", [None, 42, {"code": "MERCHANT_NOT_FOUND"}, True])
def test_a_warnings_field_of_the_wrong_shape_yields_no_warnings(wire, value):
    """A dict is iterable too, and would have produced its KEYS as warnings."""
    wire.next_payload = {"products": [], "warnings": value}
    got = _run(rc.search_products(query="x"))
    assert got.ok and got.warnings == []


def test_a_real_warnings_list_is_unchanged(wire):
    """The control."""
    wire.next_payload = {"products": [], "warnings": ["A", "B"]}
    assert _run(rc.search_products(query="x")).warnings == ["A", "B"]


def test_every_warning_is_a_string(wire):
    wire.next_payload = {"products": [], "warnings": [{"code": "X"}, 7, "Y"]}
    got = _run(rc.search_products(query="x"))
    assert all(isinstance(w, str) for w in got.warnings) and len(got.warnings) == 3


# --- C6: a quantity is a count -------------------------------------------------------------------------

def test_a_boolean_quantity_is_refused_rather_than_silently_becoming_one():
    """`int(True)` is 1, so a boolean landed in a purchase quantity as "one item"."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_quote_items([{"variantId": "var_x", "quantity": True}])
    assert "int" in str(exc.value)


@pytest.mark.parametrize("quantity", [2.7, 1.5, 0.9])
def test_a_fractional_quantity_is_refused_rather_than_truncated(quantity):
    """`int(2.7)` is 2, so a caller that computed a fractional quantity got a cart line one unit
    short of what it meant, with no error anywhere. Buyers pay for these."""
    with pytest.raises(rc.ReapRequestError):
        rc.build_quote_items([{"variantId": "var_x", "quantity": quantity}])


@pytest.mark.parametrize("quantity", ["2", None, [2], {"n": 2}])
def test_a_non_integer_quantity_is_refused(quantity):
    with pytest.raises(rc.ReapRequestError):
        rc.build_quote_items([{"variantId": "var_x", "quantity": quantity}])


def test_an_integer_quantity_and_the_default_still_work():
    """The control — a rule that refused everything would pass all of the above."""
    assert rc.build_quote_items([{"variantId": "var_x", "quantity": 3}]) == \
        [{"variantId": "var_x", "quantity": 3}]
    assert rc.build_quote_items([{"variantId": "var_x"}]) == \
        [{"variantId": "var_x", "quantity": 1}]


def test_the_env_timeout_helper_reports_no_floor_for_a_useless_value(monkeypatch):
    """Tested against the HELPER, not through the transport, on purpose. At `_post` the `> 0` test
    is inert -- the floor is applied with `max()` against a positive default, so a zero or negative
    value could not lower anything and no wire assertion can observe the difference. The contract
    "a floor, or None" is real at this level, and it is what the next caller of this helper will
    rely on."""
    for value in ("0", "0.0", "-5", "-0.1", "abc", "30s", "", "   "):
        monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", value)
        assert rc._env_timeout_floor() is None, value


def test_the_env_timeout_helper_reports_a_usable_floor(monkeypatch):
    """The control."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "45.5")
    assert rc._env_timeout_floor() == 45.5
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    assert rc._env_timeout_floor() is None


# ==================================================================================================
# SECOND-ROUND ADVERSARIAL REVIEW, PR #2140. Same shape as the block above: reproduce, then pin.
# ==================================================================================================


# --- P0: whole-token matching on a single-value axis ------------------------------------------------
#
# The first fix compared our title to Reap's sole label with RAW SUBSTRING containment, which
# ignores token boundaries completely. Every pair below resolved ok=True with a PASSING
# substitution guard, because `chosen` recorded Reap's label and Reap then echoed it back.

def _sole(label, option_id="opt_only", available=True):
    return {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": option_id, "label": label, "available": available}]}]}


@pytest.mark.parametrize("our_title,reap_label", [
    ("50ml", "150ml"),            # a 3x quantity, and the prices can plausibly agree
    ("Red", "Fired Brick"),
    ("Mini", "Minimalist Set"),
    ("M", "Jumbo"),               # one letter, inside a word
    ("S", "Standard"),
    ("Tan", "Titanium"),
    ("S/M", "Mini"),              # via the 1-char `/` fragments this used to produce
])
def test_a_substring_of_reaps_label_is_not_a_match(our_title, reap_label):
    got = rc.select_option_ids(_sole(reap_label), rc.variant_title_tokens(our_title))
    assert not got.ok, f"{our_title!r} must not resolve {reap_label!r}"
    assert got.reason == "axes_not_determined_by_title"
    assert got.option_ids == []
    assert got.chosen == {}


def test_the_150ml_row_buys_the_right_bottle_end_to_end(monkeypatch):
    """THE P0 REPRODUCTION. Our row is "Rose Serum / 50ml / $30 USD". Reap's only value is
    `150ml`, also $30 — three times the product at the same price, which is exactly the shape a
    price check cannot catch. Before the fix: ok=True, var_150, price_disagrees=False."""
    search = {"products": [{"id": "prd_rose", "merchant": {"name": "example.com"},
                            "name": "Rose Serum"}], "warnings": []}
    details = {"products": [{"id": "prd_rose", "merchant": {"name": "example.com"},
                             "name": "Rose Serum",
                             "options": [{"name": "Size", "values": [
                                 {"optionId": "opt_150", "label": "150ml",
                                  "available": True}]}]}], "errors": []}
    variant = {"id": "var_150", "options": [{"name": "Size", "value": "150ml"}],
               "price": {"amount": 30.0, "currency": "USD"}, "available": True}
    fake = _chain(monkeypatch, search=search, details=details, variant=variant)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rose Serum",
        variant_title="50ml", our_price=30.00, currency="USD"))
    assert not got.ok
    assert got.reason == "options:axes_not_determined_by_title"
    assert got.variant_id is None
    assert fake.variant_body is None          # it never even asked


# The three reference examples that define the rule. Named as such so the rule is testable in one
# place rather than inferred from the call site.

def test_the_rule_accepts_our_title_as_a_majority_of_reaps_tokens():
    """REFERENCE 1. {flamingo, flirt} of {flamingo, flirt, cream}: 2 of 3."""
    assert rc.title_matches_sole_label("Flamingo Flirt", "Flamingo Flirt - Cream") is True


def test_the_rule_accepts_an_exact_match():
    """REFERENCE 2. Equal sets — the degenerate case, which must not fall through the subset
    arithmetic."""
    assert rc.title_matches_sole_label("OS", "OS") is True


def test_the_rule_refuses_a_generic_minority_token():
    """REFERENCE 3, and the one that whole-token SUBSET alone would still have let through:
    {cream} IS a subset of {flamingo, flirt, cream}. 1 of 3 is not half, so it refuses. "Cream" is
    a finish, not a shade, and it identifies nothing."""
    assert rc.title_matches_sole_label("Cream", "Flamingo Flirt - Cream") is False


def test_the_rule_accepts_a_title_more_specific_than_reaps_label():
    """Containment the other way round: our catalog sometimes carries the longer string, and a
    title that COVERS the whole label is not a weaker claim than one that equals it."""
    assert rc.title_matches_sole_label("Flamingo Flirt Cream Finish", "Flamingo Flirt") is True


def test_the_rule_refuses_an_empty_side():
    assert rc.title_matches_sole_label("", "Flamingo Flirt") is False
    assert rc.title_matches_sole_label("Flamingo Flirt", "") is False
    assert rc.title_matches_sole_label("!!!", "Flamingo Flirt") is False


@pytest.mark.parametrize("our_title,reap_label", [
    ("50", "150ml"),      # a bare number inside an alphanumeric token
    ("50", "50ml"),       # the same number, still not the same token
    ("150", "1500ml"),
])
def test_a_numeric_fragment_never_matches_inside_a_longer_token(our_title, reap_label):
    """True by construction under whole-token comparison rather than as a separate rule: `{"50"}`
    is simply not a subset of `{"150ml"}`. Tested anyway, because it is the property that stops a
    quantity being silently multiplied."""
    assert rc.title_matches_sole_label(our_title, reap_label) is False


def test_the_real_flowerbeauty_row_still_resolves(monkeypatch):
    """The regression the whole rule has to keep: tightening this must not refuse the measured
    row it was relaxed for in the first place."""
    fake = _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS,
                  variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt", our_price=8.00, currency="USD"))
    assert got.ok and got.variant_id == "var_flamingo"
    assert fake.variant_body == {"productId": "prd_petalpout", "optionIds": ["opt_flamingo"]}


# --- P0: one-character `/` fragments ----------------------------------------------------------------

def test_one_character_slash_fragments_are_dropped():
    """"S/M" produced the fragments `S` and `M`, and a single letter cannot identify a variant
    under any rule. The WHOLE title is exempt — a row whose entire title is "S" is a real size,
    and dropping it would leave nothing to match on."""
    assert rc.variant_title_tokens("S/M") == ["S/M"]
    assert rc.variant_title_tokens("S") == ["S"]
    assert rc.variant_title_tokens("Standard / Rose") == ["Standard / Rose", "Standard", "Rose"]


def test_the_whole_title_is_always_the_first_candidate():
    """M28. `out = [raw] if raw not in parts else []` -> `[]` survived the first sweep. A
    single-axis label can legitimately contain a slash, and then the whole title is the ONLY
    candidate that can match it exactly."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_both", "label": "Standard / Rose", "available": True},
        {"optionId": "opt_other", "label": "Petite / Blue", "available": True},
    ]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard / Rose"))
    assert got.ok and got.option_ids == ["opt_both"]


# --- P0 related: the multi-value branch stays EXACT ---------------------------------------------------

def test_a_multi_value_axis_does_not_accept_a_partial_token_match():
    """The asymmetry is deliberate. With siblings present, the near-miss strings a relaxed rule
    would confuse are exactly the ones on the axis: `50ml` beside `150ml`."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_50", "label": "50ml", "available": True},
        {"optionId": "opt_150", "label": "150ml", "available": True},
    ]}]}
    assert rc.select_option_ids(product, ["50ml"]).option_ids == ["opt_50"]
    assert rc.select_option_ids(product, ["150ml"]).option_ids == ["opt_150"]
    # A title that is merely a subset of a sibling's tokens matches NEITHER.
    assert not rc.select_option_ids(product, ["ml"]).ok


def test_a_multi_value_axis_refuses_a_suffixed_label():
    """What exact matching costs, stated as a test so nobody "fixes" it: with siblings present we
    refuse rather than guess which one our shorter title meant."""
    product = {"id": "prd_x", "options": [{"name": "Shade", "values": [
        {"optionId": "opt_a", "label": "Flamingo Flirt - Cream", "available": True},
        {"optionId": "opt_b", "label": "Petal Pink - Matte", "available": True},
    ]}]}
    assert not rc.select_option_ids(product, ["Flamingo Flirt"]).ok


# --- P1: the C1 sweep missed three iterations over partner JSON -----------------------------------

@pytest.mark.parametrize("payload", [
    {"products": 7},
    {"products": "prd_x"},
    {"products": True},
    {"products": {"id": "prd_x"}},
])
def test_a_non_list_products_field_does_not_raise(payload):
    """`data.get("products") or []` is falsy-safe but not TYPE-safe: a truthy non-iterable raised
    TypeError straight out of `resolve_our_row`. The per-element isinstance never ran, because the
    `for` itself is what failed."""
    product, err = rc.details_for(payload, "prd_x")
    assert product is None and err == "product_not_in_response"


@pytest.mark.parametrize("errors", [True, 7, "PRODUCT_UNAVAILABLE", {"code": "X"}])
def test_a_non_list_errors_field_does_not_raise(errors):
    product, err = rc.details_for({"products": [], "errors": errors}, "prd_x")
    assert product is None and err == "product_not_in_response"


@pytest.mark.parametrize("options", [3, True, "Size", {"name": "Size"}])
def test_a_non_list_options_field_in_a_variant_does_not_raise(options):
    """In the substitution guard, which is the one place in this module a crash is least
    affordable: an exception here is a refusal that never happens."""
    assert rc.variant_matches_request({"id": "var_x", "options": options},
                                      {"Size": "Standard"}) == "response_missing_axis:Size"


def test_a_malformed_details_payload_does_not_crash_the_chain(monkeypatch):
    async def fake(path, body, **kw):
        if path.endswith("/search"):
            return rc.ReapResponse(ok=True, status=200, data=FENTY_SEARCH)
        return rc.ReapResponse(ok=True, status=200, data={"products": 7, "errors": True})
    monkeypatch.setattr(rc, "_post", fake)
    got = _run(rc.resolve_our_row(merchant_domain="fentybeauty.com",
                                  product_name="Fenty Eau de Parfum", variant_title="Standard"))
    assert not got.ok and got.reason == "details:product_not_in_response"


def test_a_malformed_variant_payload_does_not_crash_the_chain(monkeypatch):
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE,
           variant={"id": "var_x", "options": 3})
    got = _run(rc.resolve_our_row(merchant_domain="fentybeauty.com",
                                  product_name="Fenty Eau de Parfum", variant_title="Standard"))
    assert not got.ok and got.reason == "variant:response_missing_axis:Size"


# --- P2: kana voicing marks are not diacritics -------------------------------------------------------

@pytest.mark.parametrize("voiced,unvoiced", [
    ("ゴールド", "コールド"),     # gold vs cold
    ("パール", "ハール"),         # pearl vs (unvoiced)
    ("ビッグ", "ヒック"),
])
def test_kana_voicing_survives_normalisation(voiced, unvoiced):
    """Stripping EVERY combining mark after NFKD is right for Latin accents and wrong for kana:
    the dakuten and handakuten change the CONSONANT. ゴールド (gold) and コールド (cold) normalised
    alike, so the substitution guard reported "no substitution" for a gold-for-cold swap."""
    assert rc._norm(voiced) != rc._norm(unvoiced)
    assert rc.variant_matches_request(
        {"options": [{"name": "Color", "value": unvoiced}]}, {"Color": voiced}
    ) == f"substituted_on_axis:Color:asked={voiced}:got={unvoiced}"


def test_the_same_voiced_kana_still_matches_itself():
    """The control: the fix must not make kana stop matching themselves."""
    assert rc.variant_matches_request(
        {"options": [{"name": "Color", "value": "ゴールド"}]}, {"Color": "ゴールド"}) is None


def test_a_decomposed_dakuten_still_equals_its_precomposed_form():
    """The recomposition half. A merchant sending a decomposed dakuten (カ + U+3099) means the
    same character as the precomposed ガ, and NFC puts it back together."""
    decomposed = "ガ" + "ールド"     # KA + COMBINING VOICED SOUND MARK
    assert rc._norm(decomposed) == rc._norm("ガールド")


def test_the_normalised_form_carries_no_loose_combining_marks():
    """The assertion that actually pins the NFC step, and the reason it needed finding: the pair
    tests above pass WITHOUT recomposition, by accident. NFKD leaves the dakuten as a separate
    U+3099, `\\w` does not match it, and it therefore becomes a SPACE -- so ゴールド normalised to
    `'コ ールト'`. That still differs from コールド, so the substitution guard looked fine, while
    the label had silently become three tokens instead of one. Whole-token matching runs on these
    strings, so a spurious space is a wrong answer waiting to happen.

    Pinned as an exact string: four characters, voicing intact, no spaces."""
    assert rc._norm("ゴールド") == "ゴールド"
    assert " " not in rc._norm("ゴールド")
    assert len(rc._norm("ゴールド").split()) == 1


def test_an_interior_cyrillic_combining_mark_is_folded_not_turned_into_a_space():
    """The Cyrillic half of the strip range (U+0483-U+0489), which the ё tests never reach -- ё
    decomposes with U+0308, a LATIN combining mark. A mark that is not folded away becomes a
    space, which splits one token into two; only an INTERIOR mark shows the difference, because a
    trailing one is absorbed by `.strip()` either way."""
    assert rc._norm("а҆б") == "аб"
    assert rc._norm("а҃б") == "аб"


def test_latin_accents_still_fold():
    """The other half of the same rule, unchanged: Latin combining marks ARE diacritics."""
    assert rc._norm("Crème Brûlée") == rc._norm("Creme Brulee") == "creme brulee"
    assert rc._norm("Chloé") == rc._norm("Chloe")


def test_cyrillic_and_cjk_still_behave():
    assert rc._norm("標準") != rc._norm("ミニ")
    assert rc._norm("Чёрный") != rc._norm("Розовый")


def test_full_width_still_folds_to_ascii():
    """NFKD's compatibility folding is still wanted: a merchant's full-width spelling must match
    our ASCII one."""
    assert rc._norm("１５０ＭＬ") == rc._norm("150ml") == "150ml"


# --- P2: timeout bounds ------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "nan", "NaN"])
def test_a_non_finite_env_timeout_is_ignored(value):
    """`float("inf")` parses, is > 0, and `max(35.0, inf)` is `inf` — so this gave EVERY path an
    infinite read timeout and a hung partner socket would hold a worker until something else
    killed it. `nan` failed the `> 0` test by accident rather than by design."""
    import os
    os.environ["REAP_API_TIMEOUT_SECONDS"] = value
    try:
        assert rc._env_timeout_floor() is None
    finally:
        os.environ.pop("REAP_API_TIMEOUT_SECONDS", None)


def test_a_huge_env_timeout_is_clamped_not_honoured(wire, monkeypatch):
    """`=600` applied to ALL paths, and one resolution makes several calls, so a single row could
    have occupied a worker for the better part of an hour."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "600")
    assert rc._env_timeout_floor() == rc._MAX_ENV_TIMEOUT_S == 120.0
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["timeout"] == 120.0


def test_a_floor_below_the_cap_is_honoured_exactly(wire, monkeypatch):
    """The control — a clamp that returned the cap unconditionally would pass the test above."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "90")
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["timeout"] == 90.0


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_a_non_positive_explicit_timeout_is_refused_not_ignored(wire, value):
    """`if timeout_seconds:` treated 0 and 0.0 as "not supplied" and silently fell through to the
    per-path default, so a caller asking for no timeout got 25 s. It is a caller bug, and a
    request we quietly reshape is worse than one we refuse."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.search_products(query="x", timeout_seconds=value))
    assert wire.calls == []


def test_a_non_finite_explicit_timeout_is_refused(wire):
    with pytest.raises(rc.ReapRequestError):
        _run(rc.search_products(query="x", timeout_seconds=float("inf")))
    assert wire.calls == []


# --- P1: the four guards the first sweep could not kill ----------------------------------------------

def test_a_title_of_only_punctuation_does_not_match_every_single_value_label():
    """M5. `wanted` is built with an `if _norm(w)` filter. Drop it and a title of "--" yields one
    candidate that normalises to "", which the old `in` comparison accepted against ANY label —
    so the most degenerate possible title resolved every single-value product on the index."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"), rc.variant_title_tokens("--"))
    assert not got.ok
    assert got.reason == "variant_title_unusable"
    assert got.chosen == {}


def test_an_empty_our_domain_does_not_match_a_merchantless_product():
    """M9. `bool(ours)` in `merchant_domain_matches`. Without it, a row with no merchant domain
    matches a Reap product that also reports no merchant name — "" == "" — and the merchant
    filter, which is the correctness boundary of this whole module, passes on absence."""
    assert rc.merchant_domain_matches("", "") is False
    assert rc.merchant_domain_matches(None, None) is False
    payload = {"products": [{"id": "prd_x", "merchant": {}, "name": "Snail Mucin"}]}
    match = rc.match_product(payload, merchant_domain="", product_name="Snail Mucin")
    assert not match.ok and match.reason == "merchant_not_in_results"


def test_a_search_with_no_product_name_refuses_before_comparing_anything():
    """M10. Without the `no_product_name_supplied` return, `wanted` is "" and the exact-name
    comparison below compares "" against every candidate's normalised name — which matches any
    product whose name is also blank, on the right merchant."""
    payload = {"products": [{"id": "prd_x", "merchant": {"name": "cosrx.com"}, "name": ""}]}
    match = rc.match_product(payload, merchant_domain="cosrx.com", product_name="")
    assert not match.ok and match.reason == "no_product_name_supplied"
    assert match.product_id is None


# --- P2: a supplied title that normalises to nothing --------------------------------------------------

@pytest.mark.parametrize("title", ["!!!", "/", " - ", "---", "***"])
def test_a_supplied_but_unusable_title_refuses_rather_than_taking_the_untitled_path(title):
    """Passing no title ASSERTS "this row has no variants". A title that was supplied and
    normalises to nothing asserts the opposite — the caller believes it constrained the
    resolution — so falling through to the structural acceptance would resolve the product on an
    assertion nobody made."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"), rc.variant_title_tokens(title))
    assert not got.ok
    assert got.reason == "variant_title_unusable"
    assert got.single_value_axis_accepted_without_title is False


def test_the_unusable_title_refusal_reaches_the_resolution(monkeypatch):
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="!!!"))
    assert not got.ok and got.reason == "options:variant_title_unusable"


def test_genuinely_no_title_still_takes_the_structural_path(monkeypatch):
    """The control, and the distinction being drawn: None and "" are still the untitled path."""
    for title in (None, ""):
        _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=FLOWER_VARIANT)
        got = _run(rc.resolve_our_row(
            merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
            variant_title=title))
        assert got.ok, title
        assert got.single_value_axis_accepted_without_title is True


def test_the_unusable_title_reason_has_its_own_copy():
    text = rc.explain_refusal("options:variant_title_unusable")
    assert "Unrecognised refusal" not in text
    # It must NOT collapse into the no-title copy: they are different caller mistakes.
    assert text != rc.explain_refusal("options:no_variant_title_supplied")


# --- P2: the response bound is real, not cosmetic ------------------------------------------------------

def test_an_oversized_body_stops_being_read_partway(wire):
    """C2 made real. `client.post` does not return until the whole body is in memory, so the old
    checks declined to PARSE a body they had already fully allocated — the expensive half had
    happened. Streaming plus `_read_bounded` stops the read at the cap, which is what the
    `bytes_yielded` assertion measures."""
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES * 4)
    wire.next_chunk_size = 64 * 1024
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"
    read = wire.last_response.bytes_yielded
    assert read <= rc.MAX_RESPONSE_BYTES + 64 * 1024
    assert read < len(wire.next_content)          # it did NOT read the whole thing


def test_a_lying_content_length_does_not_defeat_the_bound(wire):
    """The declared length is a CLAIM made by the host we are defending against. The cumulative
    count is the fact. A mutant that keeps only the declared check dies here."""
    wire.next_headers = {"content-length": "12"}
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"


def test_an_absent_content_length_does_not_defeat_the_bound(wire):
    """Chunked responses declare nothing at all."""
    wire.next_headers = {}
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    assert _run(rc.search_products(query="x")).error == "response_too_large"


def test_a_declared_oversize_is_refused_before_any_body_is_read(wire):
    """The cheap check keeps its point: it can refuse before a single byte of body arrives."""
    wire.next_headers = {"content-length": str(rc.MAX_RESPONSE_BYTES + 1)}
    wire.next_content = b"x" * 10
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"
    assert wire.last_response.bytes_yielded == 0


def test_a_body_of_exactly_the_cap_is_accepted(wire):
    """THE BOUNDARY, at exactly MAX_RESPONSE_BYTES rather than near it. `>` versus `>=` is a
    one-character mutation that no approximate test can see, and it fails in the direction that
    refuses legitimate responses. The body is padded to the cap to the byte."""
    prefix = b'{"products":[],"warnings":[],"pad":"'
    suffix = b'"}'
    payload = prefix + b"y" * (rc.MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(payload) == rc.MAX_RESPONSE_BYTES
    wire.next_content = payload
    got = _run(rc.search_products(query="x"))
    assert got.ok and got.data.get("products") == []


def test_one_byte_over_the_cap_is_refused(wire):
    """The other side of the same boundary, so the pair pins the comparison from both directions
    rather than just asserting that something large is refused."""
    prefix = b'{"products":[],"warnings":[],"pad":"'
    suffix = b'"}'
    payload = prefix + b"y" * (rc.MAX_RESPONSE_BYTES + 1 - len(prefix) - len(suffix)) + suffix
    assert len(payload) == rc.MAX_RESPONSE_BYTES + 1
    wire.next_content = payload
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "response_too_large"


def test_the_request_still_reaches_the_wire_unchanged_through_the_stream(wire):
    """`_post`'s signature and result shape are unchanged by the streaming rewrite; so is what it
    sends. The method is now explicit, so it is asserted."""
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    call = wire.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/agentic/quotes")
    assert call["headers"]["Authorization"] == "Bearer sk_test_key"
    assert call["body"]["items"] == [{"variantId": "var_x", "quantity": 1}]


def test_a_4xx_body_is_never_read_at_all(wire):
    """Stronger than the old "the body is not returned": on an error status the body is not even
    pulled off the socket, so a partner error payload echoing a buyer's address never enters this
    process."""
    wire.next_status = 422
    wire.next_content = json.dumps({"echo": {"shippingAddress": GOOD_ADDRESS}}).encode()
    got = _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}],
                                email="b@example.com"))
    assert not got.ok and got.status == 422 and got.data == {}
    assert wire.last_response.bytes_yielded == 0


def test_the_bounded_reader_is_reusable_on_its_own(wire):
    """`_read_bounded` is the seam the stacked branch's `_get` should adopt rather than growing a
    second copy of the bound, so it is tested as a unit: response in, bytes or None out."""
    small = _FakeResponse(200, {"a": 1})
    assert _run(rc._read_bounded(small)) == small.content
    big = _FakeResponse(200, content=b"z" * 5000)
    assert _run(rc._read_bounded(big, max_bytes=100)) is None


def test_an_unparseable_body_is_still_reported_as_such(wire):
    """The rewrite swapped `resp.json()` for `json.loads`, so the failure mode has to survive."""
    wire.next_content = b"<html>not json</html>"
    got = _run(rc.search_products(query="x"))
    assert not got.ok and got.error == "unparseable_response"


# --- prose corrections carry assertions where they can ------------------------------------------------

def test_the_inert_guards_are_gone_and_the_behaviour_is_not():
    """Two guards were removed as inert rather than annotated. Behaviour must be unchanged, which
    is the only thing that makes "inert" a true description rather than a convenient one."""
    # `label and` in the multi-value branch: a blank label still never matches.
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_blank", "label": "", "available": True},
        {"optionId": "opt_mini", "label": "Mini", "available": True}]}]}
    assert rc.select_option_ids(product, ["Mini"]).option_ids == ["opt_mini"]
    assert not rc.select_option_ids(product, ["Standard"]).ok
    # `not ours` in `_disagrees`: unknown on either side is still a disagreement.
    assert rc._disagrees((140.0, "USD"), 140.00, "") is True
    assert rc._disagrees((140.0, ""), 140.00, "USD") is True
    assert rc._disagrees((140.0, ""), 140.00, "") is True
    assert rc._disagrees((140.0, "USD"), 140.00, "USD") is False
