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


# --- nothing in this file may reach the network -------------------------------------------------


@pytest.fixture(autouse=True)
def _no_unpatched_httpx(request, monkeypatch):
    """Make an un-patched `httpx.AsyncClient` construction raise, in EVERY test in this file.

    This is here because it already happened. A probe of this module forgot to patch the
    transport and sent five read-only GETs to `sandbox.api.reap.global` with a dummy key: 401s,
    no PII and no real credential, so no harm done -- but the only thing standing between that
    and a test that POSTs is which line the author forgot. A test that forgets the recorder
    should fail loudly on its own machine, not quietly reach a partner.

    `wire` (and any test that patches `httpx.AsyncClient` itself) replaces this with the
    recorder, so the guard costs nothing where the transport is already faked. Where it is not,
    the error names the fixture to use.

    `mock_httpx` IS THE ONE EXEMPTION, and it has to be. That fixture builds a REAL
    `httpx.AsyncClient` over a `MockTransport` -- no sockets, but httpx's own response decoding,
    which is the thing those tests are about -- by SUBCLASSING whatever `httpx.AsyncClient` is at
    the time. An autouse fixture runs first, so the subclass would inherit from this guard and
    raise in its own constructor: the guard would not be catching a mistake, it would be breaking
    the one set of tests that exercises the real client. Skipping it there is not a hole, because
    `mock_httpx` installs a transport that cannot reach the network either.
    """
    if "mock_httpx" in request.fixturenames:
        return None

    import httpx

    class _NetworkForbidden:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "this test constructed a real httpx.AsyncClient — it would have reached the "
                "network. Use the `wire` fixture, or monkeypatch rc._post / rc._get."
            )

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden)
    return _NetworkForbidden


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
    assert not got.ok and got.reason == "axes_not_determined_by_title"
    # It used to report the axis it DID match here. It no longer can: "Standard" is ONE part and
    # this product has TWO axes, so the title's parts are not candidates at all (see
    # `test_a_title_with_the_wrong_number_of_parts_matches_nothing_at_all`). The refusal is
    # unchanged -- a title that names one of two axes never resolved this product.
    assert got.unmatched_axes == ["Size", "Color"]
    assert got.chosen == {}
    # A title that DOES name both axes still resolves, which is the property that matters.
    both = rc.select_option_ids(product, rc.variant_title_tokens("Standard / Rose"))
    assert both.ok and both.option_ids == ["opt_standard", "opt_rose"]


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
        #: The chunk_size `_read_bounded` asked for, and the largest chunk it was actually handed.
        #: Recorded because the SIZE OF ONE CHUNK is what the cap is enforced in steps of -- a
        #: reader that omits chunk_size gets whatever httpx decodes in one go.
        self.requested_chunk_size = "_MISSING"
        self.largest_chunk = 0

    async def aiter_bytes(self, chunk_size=None):
        self.requested_chunk_size = chunk_size if chunk_size is not None else "_MISSING"
        step = max(1, int(chunk_size)) if chunk_size else self._chunk_size
        for start in range(0, len(self.content), step):
            chunk = self.content[start:start + step]
            self.bytes_yielded += len(chunk)
            self.largest_chunk = max(self.largest_chunk, len(chunk))
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

    def stream(self, method, url, json=None, params=None, headers=None):
        # `params` is the only addition to the base's recorder: `_get` hands its query to httpx
        # to encode rather than pasting it into the URL, and a test has to be able to see it.
        _Recorder.calls.append({"method": method, "url": url, "body": json, "params": params,
                                "headers": headers, "timeout": self.timeout,
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
        == "response_axes_differ"


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


# --- a single-value axis is compared EXACTLY, and bridged by an alias ------------------------------
#
# Measured on flowerbeauty.com "Petal Pout Lip Color": Reap indexes the per-shade page as its own
# product, with ONE `Shade` axis carrying ONE value, `"Flamingo Flirt - Cream"`. Our row's title
# is "Flamingo Flirt" — no finish suffix — so exact label matching REFUSES it.
#
# CORRECTED TWICE, and the second correction reverses the first. This section originally said a
# single-value axis is "determined" and that refusing there "protects nothing", while the code
# accepted such an axis without looking at our title at all. It then said the right answer was to
# loosen the comparison to containment. Both were wrong, and the second cost three review rounds:
# substring containment bought "150ml" for a "50ml" row, and whole-token subset bought a $39
# travel spray for a $78 duo-set row and a "7 oz" bottle for a "1.7 oz" one.
#
# The rule is now EXACT on every axis, and the flowerbeauty row is resolved by an explicit alias
# (`accept_variant_labels`) — a human asserting that two names are one object, which is the
# judgement no string rule was able to make. Refusing without one is correct, not a gap.

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


def test_an_axis_with_one_value_whose_label_differs_needs_an_alias():
    """The real flowerbeauty.com row: our title is a PREFIX of Reap's label, which carries a
    finish suffix our catalog does not store. Two successive rules tried to bridge that gap by
    string cleverness and each was measured buying a different physical object, so the gap is now
    bridged by DATA — and only by data."""
    without = rc.select_option_ids(FLOWER_DETAILS["products"][0],
                                   rc.variant_title_tokens("Flamingo Flirt"))
    assert not without.ok and without.reason == "sole_label_differs:Shade"

    got = rc.select_option_ids(FLOWER_DETAILS["products"][0],
                               rc.variant_title_tokens("Flamingo Flirt"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok and got.option_ids == ["opt_flamingo"]
    # `chosen` records REAP's label, because Reap's optionId is what we send and Reap's label is
    # what the response echoes. Sound because that exact string was asserted by a human.
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
    assert got.reason == "sole_label_differs:Shade"
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
    assert got.reason == "sole_label_differs:Size"
    assert got.option_ids == []
    assert "Size" not in got.chosen

    # And the circularity it produced: had it been accepted, the guard could never have refused.
    reap_answered_mini = {"id": "var_mini", "options": [{"name": "Size", "value": "Mini"}]}
    assert rc.variant_matches_request(reap_answered_mini, {"Size": "Mini"}) is None


def test_a_title_broader_than_reaps_label_is_refused_too():
    """INVERTED. This asserted that containment the other way round was fine — our longer title
    against Reap's shorter label. That is the direction round 3 measured buying a $39 travel spray
    for a $78 duo-set row, because a superset title "covered" whatever Reap named. Both directions
    are refusals now, and both are an alias away from resolving."""
    product = json.loads(json.dumps(FLOWER_DETAILS["products"][0]))
    product["options"][0]["values"][0]["label"] = "Flamingo Flirt"
    got = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt Cream Finish"))
    assert not got.ok and got.reason == "sole_label_differs:Shade"
    assert got.candidates == [{"axis": "Shade", "label": "Flamingo Flirt"}]


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
        variant_title="Flamingo Flirt",
        accept_variant_labels=["Flamingo Flirt - Cream"]))
    assert titled.ok and titled.single_value_axis_accepted_without_title is False


def test_a_single_value_axis_still_reports_unavailability():
    """Matching decides WHICH value, never whether Reap will sell it. Reached here through an
    alias, which is the only way this label resolves now — and an alias must not smuggle a row
    past the availability flag either."""
    product = json.loads(json.dumps(FLOWER_DETAILS["products"][0]))
    product["options"][0]["values"][0]["available"] = False
    got = rc.select_option_ids(product, ["Flamingo Flirt"],
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok and got.unavailable_axes == ["Shade"]


def test_the_substitution_guard_compares_against_reaps_label_not_our_title(monkeypatch):
    """Because we send Reap's optionId, the response echoes Reap's label, so the guard compares
    the response to THAT label rather than to our catalog's title.

    CORRECTED TWICE. The original docstring said this made the guard "compare against what we
    actually asked for", which was false while the label had never been checked against our title
    — `chosen` was Reap's own answer and the guard compared Reap to Reap. The second version said
    the check "establishes that Reap's label and our title describe the same thing", which was
    true only as far as a fuzzy rule can establish anything, and that rule was measured buying
    three different wrong objects. What makes `chosen` trustworthy NOW is that the label either
    equals our title exactly or was named verbatim in `accept_variant_labels` by a human. What
    this test proves is the narrow thing it always proved: a DIFFERENT label coming back is
    caught."""
    substituted = json.loads(json.dumps(FLOWER_VARIANT))
    substituted["options"] = [{"name": "Shade", "value": "Petal Pink - Matte"}]
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=substituted)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt",
        accept_variant_labels=["Flamingo Flirt - Cream"],
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
        accept_variant_labels=["Flamingo Flirt - Cream"],
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
    "variant:response_axes_differ", "variant:response_duplicate_axis_name:Size",
    "variant:label_too_long_to_compare:Size", "options:label_too_long_to_compare:Size",
    "details:PRODUCT_NOT_FOUND", "single_variant_product_has_no_variant_id",
    "resolved_id_not_in_reap_namespace", "reap_status_503",
    "transport_error:ReadTimeout", "reap_client_not_configured",
    # WP1's refusals, listed HERE rather than in their own test so they inherit the
    # placeholder check and the distinctness check above along with everything else.
    "hosted_url_not_allowed", "reap_status_400", "reap_status_403", "reap_status_404",
    "unparseable_response",
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
        brand="Flower Beauty", category="lip color", variant_title="Flamingo Flirt",
        accept_variant_labels=["Flamingo Flirt - Cream"]))
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
        our_price=8.00, accept_variant_labels=["Flamingo Flirt - Cream"],
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
    """The cheap check: a declared length can refuse before any body is read at all. Nothing
    upstream limits what a partner returns, and parsing a multi-megabyte document allocates the
    object graph on top of the bytes, in a serving path."""
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


# --- P0: EXACT label matching, and the alias that replaces fuzziness --------------------------------
#
# Three review rounds each found this one comparison buying a different physical object. The rule
# was the defect, not any particular version of it:
#
#   round 1  the label was not compared to our title at all
#   round 2  raw SUBSTRING containment   our "50ml"                 -> Reap "150ml"
#   round 3  whole-token subset + half   our "Travel Spray Duo Set" -> Reap "Travel"   ($78 -> $39)
#                                        our "1.7 oz"               -> Reap "7 oz"
#                                        our "Set"                  -> Reap "Gift Set"
#                                        our "Red / 50ml"           -> Reap "50ml Travel Red Edition"
#
# So matching is now EXACT on every axis, and a legitimate mismatch is handled by DATA --
# `accept_variant_labels` -- which is a human asserting that two names are one object.

def _sole(label, option_id="opt_only", available=True, axis="Size"):
    return {"id": "prd_x", "options": [{"name": axis, "values": [
        {"optionId": option_id, "label": label, "available": available}]}]}


MUST_REFUSE = [
    # round 2's substring failures
    ("50ml", "150ml"), ("Red", "Fired Brick"), ("Mini", "Minimalist Set"),
    ("M", "Jumbo"), ("S", "Standard"),
    # a generic minority token, and our title as a strict prefix
    ("Cream", "Flamingo Flirt - Cream"), ("Flamingo", "Flamingo Flirt - Cream"),
    # the measured row the first relaxation was written for. It refuses now, ON PURPOSE, and the
    # alias test below is where it resolves.
    ("Flamingo Flirt", "Flamingo Flirt - Cream"),
    # round 3: the REVERSE direction, which had no coverage condition at all
    ("Travel Spray Duo Set", "Travel"), ("Mini Duo Set", "Mini"), ("50ml Refill Pack", "50ml"),
    # round 3: exactly-half let one generic token carry a two-token label
    ("Set", "Gift Set"), ("Mini", "Mini Set"), ("100ml", "100ml Refill"),
    # round 3: a decimal point split into two tokens, so the quantity was shared
    ("1.7 oz", "7 oz"), ("0.5 oz", "5 oz"),
    # round 3: a multi-axis title's tokens merged into one bag
    ("Red / 50ml", "50ml Travel Red Edition"),
    ("50/50 Blend", "50 ml"), ("3.38 fl.oz / 100mL", "100ml Refill"),
    # plain near-misses, including the one-letter case
    ("Large", "OS"), ("O", "OS"),
    # and the non-Latin pairs, which must stay refused now that _norm keeps them distinct
    ("標準", "ミニ"), ("ゴールド", "コールド"),
]


@pytest.mark.parametrize("our_title,reap_label", MUST_REFUSE)
def test_a_sole_label_that_is_not_our_title_is_refused(our_title, reap_label):
    """THE TABLE. Every row is a measured or constructed wrong purchase from one of the three
    fuzzy rules. None of them may resolve without an explicit alias."""
    got = rc.select_option_ids(_sole(reap_label), rc.variant_title_tokens(our_title))
    assert not got.ok, f"{our_title!r} must not resolve {reap_label!r}"
    assert got.option_ids == []
    assert got.chosen == {}


@pytest.mark.parametrize("our_title,reap_label", MUST_REFUSE)
def test_every_refusal_in_the_table_is_actionable(our_title, reap_label):
    """A single-value mismatch refuses as `sole_label_differs` and carries Reap's label, so an
    operator can decide whether to store it as an alias without going back to the sandbox."""
    got = rc.select_option_ids(_sole(reap_label), rc.variant_title_tokens(our_title))
    assert got.reason == "sole_label_differs:Size"
    assert got.candidates == [{"axis": "Size", "label": reap_label}]


MUST_ACCEPT = [
    ("OS", "OS"),
    ("os ", " OS"),                                   # case and surrounding whitespace
    ("Crème", "Creme"),                               # Latin accents still fold
    ("3.38 fl.oz / 100mL", "3.38 fl.oz / 100mL"),     # the whole title, decimals intact
]


@pytest.mark.parametrize("our_title,reap_label", MUST_ACCEPT)
def test_a_sole_label_equal_to_our_title_resolves(our_title, reap_label):
    """The other half of the table. Exact does not mean byte-identical: `_norm` still folds case,
    whitespace, punctuation and Latin accents."""
    got = rc.select_option_ids(_sole(reap_label), rc.variant_title_tokens(our_title))
    assert got.ok, f"{our_title!r} must resolve {reap_label!r}"
    assert got.option_ids == ["opt_only"]
    # REAP's label, as always -- but SANITISED: control characters stripped, whitespace collapsed,
    # capped. `chosen` is echoed into reason strings and shown to humans, so it is cleaned once on
    # the way in rather than at each of the places that read it.
    assert got.chosen == {"Size": reap_label.strip()}


def test_a_multi_axis_title_matches_a_single_valued_axis_and_a_multi_valued_one():
    """One rule for both branches, exercised on one product: `Color` carries a single value and
    `Size` carries several, and the same title resolves both by equality."""
    product = {"id": "prd_x", "options": [
        {"name": "Color", "values": [
            {"optionId": "opt_black", "label": "Black Wash", "available": True}]},
        {"name": "Size", "values": [
            {"optionId": "opt_l", "label": "Large", "available": True},
            {"optionId": "opt_xxl", "label": "XXLarge", "available": True}]},
    ]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black Wash / XXLarge"))
    assert got.ok
    assert got.option_ids == ["opt_black", "opt_xxl"]
    assert got.chosen == {"Color": "Black Wash", "Size": "XXLarge"}


# --- the alias hook: a human asserting two names are one object ---------------------------------

def test_an_alias_resolves_the_row_that_exact_matching_refuses():
    """THE DOCUMENTED EXAMPLE. Our title "Flamingo Flirt"; Reap's sole label
    "Flamingo Flirt - Cream", which carries a finish suffix our catalog does not store. Without
    the alias it refuses (it is in MUST_REFUSE above); with it, it resolves."""
    got = rc.select_option_ids(
        _sole("Flamingo Flirt - Cream", option_id="opt_flamingo"),
        rc.variant_title_tokens("Flamingo Flirt"),
        accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok and got.option_ids == ["opt_flamingo"]
    assert got.chosen == {"Size": "Flamingo Flirt - Cream"}


def test_an_alias_joins_the_title_rather_than_replacing_it():
    """A row can carry an alias for one axis and still match another axis by its own title. If
    aliases REPLACED the title's candidates, supplying one would break every axis it does not
    name — a silent regression on rows that were resolving fine."""
    got = rc.select_option_ids(_sole("OS"), rc.variant_title_tokens("OS"),
                               accept_variant_labels=["Some Other Shade"])
    assert got.ok and got.chosen == {"Size": "OS"}

    product = {"id": "prd_x", "options": [
        {"name": "Shade", "values": [
            {"optionId": "opt_s", "label": "Flamingo Flirt - Cream", "available": True}]},
        {"name": "Size", "values": [
            {"optionId": "opt_a", "label": "Full Size", "available": True},
            {"optionId": "opt_b", "label": "Mini", "available": True}]},
    ]}
    both = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt / Full Size"),
                                accept_variant_labels=["Flamingo Flirt - Cream"])
    assert both.ok
    assert both.option_ids == ["opt_s", "opt_a"]   # one by alias, one by our own title


def test_an_alias_does_not_rescue_a_broken_title():
    """A title of "!!!" is a caller bug whether or not an alias is also supplied: the caller
    believes it constrained the resolution and it did not. Refusing on the title is not something
    an alias may switch off."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"),
                               rc.variant_title_tokens("!!!"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert not got.ok and got.reason == "variant_title_unusable"


def test_an_alias_is_compared_exactly_like_any_other_candidate():
    """An alias widens WHICH label we accept by exactly one string. It is not a pattern, and it
    does not make neighbouring labels acceptable."""
    alias = ["Flamingo Flirt - Cream"]
    assert rc.select_option_ids(_sole("Flamingo Flirt - Matte"),
                                rc.variant_title_tokens("Flamingo Flirt"),
                                accept_variant_labels=alias).ok is False
    assert rc.select_option_ids(_sole("Flamingo Flirt - Cream Deluxe"),
                                rc.variant_title_tokens("Flamingo Flirt"),
                                accept_variant_labels=alias).ok is False


def test_an_alias_works_on_a_multi_value_axis_too():
    """"on any axis", as specified — the alias joins the candidate set, and the candidate set is
    what both branches test against."""
    product = {"id": "prd_x", "options": [{"name": "Shade", "values": [
        {"optionId": "opt_a", "label": "Flamingo Flirt - Cream", "available": True},
        {"optionId": "opt_b", "label": "Petal Pink - Matte", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok and got.option_ids == ["opt_a"]


@pytest.mark.parametrize("alias", ["", "   ", "!!!", " - ", None, "Default Title"])
def test_an_unusable_alias_is_ignored_not_treated_as_a_wildcard(alias):
    """An alias that normalises to nothing must not become a blank key that matches a blank label,
    and Shopify's placeholder is not an assertion about anything."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"),
                               rc.variant_title_tokens("Flamingo Flirt"),
                               accept_variant_labels=[alias])
    assert not got.ok and got.reason == "sole_label_differs:Size"


def test_an_alias_does_not_bypass_the_availability_refusal():
    """An alias says "this label names our object". It says nothing about whether Reap will sell
    it, and it must not be a way around the guard that stops us calling /variant for a value Reap
    would substitute."""
    got = rc.select_option_ids(
        _sole("Flamingo Flirt - Cream", available=False),
        rc.variant_title_tokens("Flamingo Flirt"),
        accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok                               # it MATCHED
    assert got.unavailable_axes == ["Size"]     # and is still flagged unavailable
    assert got.chosen_available == {"Size": False}


def test_an_alias_does_not_bypass_the_substitution_guard():
    """`chosen` still records Reap's label, so the response is still checked against it. An alias
    changes which label we ask for, never whether we verify what came back."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"),
                               rc.variant_title_tokens("Flamingo Flirt"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.chosen == {"Size": "Flamingo Flirt - Cream"}
    substituted = {"id": "var_x", "options": [{"name": "Size", "value": "Petal Pink - Matte"}]}
    assert rc.variant_matches_request(substituted, got.chosen) == \
        "substituted_on_axis:Size:asked=Flamingo Flirt - Cream:got=Petal Pink - Matte"


# --- end to end, because a matcher test is not a purchase --------------------------------------

def _one_axis_chain(monkeypatch, *, label, price, variant_label=None):
    search = {"products": [{"id": "prd_e", "merchant": {"name": "example.com"},
                            "name": "Rose Serum"}], "warnings": []}
    details = {"products": [{"id": "prd_e", "merchant": {"name": "example.com"},
                             "name": "Rose Serum",
                             "options": [{"name": "Size", "values": [
                                 {"optionId": "opt_e", "label": label,
                                  "available": True}]}]}], "errors": []}
    variant = {"id": "var_e",
               "options": [{"name": "Size", "value": variant_label or label}],
               "price": {"amount": price, "currency": "USD"}, "available": True}
    return _chain(monkeypatch, search=search, details=details, variant=variant)


def test_the_duo_set_row_does_not_buy_the_single_travel_spray(monkeypatch):
    """ROUND 3's headline reproduction. Our row is a $78 "Travel Spray Duo Set"; Reap's only value
    is "Travel" at $39. The token rule accepted it because our tokens were a superset of Reap's,
    and the only signal was `price_disagrees` — which callers are explicitly told is as likely to
    be our staleness as theirs."""
    fake = _one_axis_chain(monkeypatch, label="Travel", price=39.0)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rose Serum",
        variant_title="Travel Spray Duo Set", our_price=78.00, currency="USD"))
    assert not got.ok
    assert got.reason == "options:sole_label_differs:Size"
    assert got.variant_id is None
    assert fake.variant_body is None
    assert got.candidates == [{"axis": "Size", "label": "Travel"}]


def test_the_1_7_oz_row_does_not_buy_the_7_oz_bottle(monkeypatch):
    """The decimal-point split, end to end: "1.7 oz" became {1, 7, oz} and shared the token `7`
    with "7 oz" — a 4x quantity, accepted in both directions."""
    fake = _one_axis_chain(monkeypatch, label="7 oz", price=60.0)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rose Serum",
        variant_title="1.7 oz", our_price=30.00, currency="USD"))
    assert not got.ok and got.reason == "options:sole_label_differs:Size"
    assert fake.variant_body is None


def test_the_150ml_row_still_does_not_buy_the_50ml_bottle(monkeypatch):
    """Round 2's reproduction, kept: three times the product at the same price is the shape a
    price check cannot catch."""
    fake = _one_axis_chain(monkeypatch, label="150ml", price=30.0)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rose Serum",
        variant_title="50ml", our_price=30.00, currency="USD"))
    assert not got.ok and got.reason == "options:sole_label_differs:Size"
    assert fake.variant_body is None


def test_an_alias_resolves_the_row_end_to_end(monkeypatch):
    """The remedy, all the way through: refused without the alias, resolved with it."""
    fake = _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS,
                  variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt", our_price=8.00, currency="USD",
        accept_variant_labels=["Flamingo Flirt - Cream"]))
    assert got.ok and got.variant_id == "var_flamingo"
    assert got.price == (8.0, "USD") and got.price_disagrees is False
    assert fake.variant_body == {"productId": "prd_petalpout", "optionIds": ["opt_flamingo"]}


def test_without_the_alias_the_same_row_refuses_and_says_what_to_store(monkeypatch):
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt", our_price=8.00, currency="USD"))
    assert not got.ok and got.reason == "options:sole_label_differs:Shade"
    assert got.candidates == [{"axis": "Shade", "label": "Flamingo Flirt - Cream"}]


def test_the_substitution_guard_still_fires_on_an_aliased_row(monkeypatch):
    """An alias must not become a way to accept whatever comes back. Reap answers with a DIFFERENT
    shade; the guard compares against the label we asked for and refuses."""
    substituted = json.loads(json.dumps(FLOWER_VARIANT))
    substituted["options"] = [{"name": "Shade", "value": "Petal Pink - Matte"}]
    _chain(monkeypatch, search=FLOWER_SEARCH, details=FLOWER_DETAILS, variant=substituted)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt",
        accept_variant_labels=["Flamingo Flirt - Cream"]))
    assert not got.ok
    assert got.reason.startswith("variant:substituted_on_axis:Shade:asked=Flamingo Flirt - Cream")


def test_the_sole_label_refusal_is_distinct_from_an_undetermined_axis():
    """Two different problems needing two different actions: one is an alias away from resolving,
    the other means our title does not pin the axis at all and no alias fixes it."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "opt_a", "label": "Small", "available": True},
        {"optionId": "opt_b", "label": "Large", "available": True}]}]}
    undetermined = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    assert undetermined.reason == "axes_not_determined_by_title"
    assert undetermined.candidates == []

    sole = rc.select_option_ids(_sole("Small"), rc.variant_title_tokens("Medium"))
    assert sole.reason == "sole_label_differs:Size"


def test_a_mixed_refusal_is_reported_as_undetermined_not_as_a_sole_label():
    """When only SOME unmatched axes are single-valued, an alias would not be enough, so the
    reason must not suggest one. The labels are still carried, because they are still useful."""
    product = {"id": "prd_x", "options": [
        {"name": "Shade", "values": [{"optionId": "o1", "label": "Cream", "available": True}]},
        {"name": "Size", "values": [
            {"optionId": "o2", "label": "Small", "available": True},
            {"optionId": "o3", "label": "Large", "available": True}]},
    ]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    assert got.reason == "axes_not_determined_by_title"
    assert got.unmatched_axes == ["Shade", "Size"]
    assert got.candidates == [{"axis": "Shade", "label": "Cream"}]


def test_the_sole_label_reason_has_its_own_actionable_copy():
    text = rc.explain_refusal("options:sole_label_differs:Shade")
    assert "Unrecognised refusal" not in text
    assert "alias" in text.lower()
    assert text != rc.explain_refusal("options:axes_not_determined_by_title")


# --- P0: fragments that cannot identify a variant --------------------------------------------------

def test_a_bare_slash_is_not_a_separator():
    """CORRECTED. A numeric-fragment FILTER used to paper over this: "50/50 Blend" was split on
    the bare slash and the fragment `50` was then thrown away for being all digits. The filter was
    the wrong fix and it cost most of a clothing catalog (see the separator table below). The
    right fix is that a bare slash was never a separator — Shopify joins options with " / ", and
    "50/50 Blend", "1/2 oz" and "S/M" are each ONE label."""
    assert rc.variant_title_tokens("50/50 Blend") == ["50/50 Blend"]
    assert rc.variant_title_tokens("1/2 oz") == ["1/2 oz"]
    assert rc.variant_title_tokens("S/M") == ["S/M"]
    assert rc.variant_title_tokens("3.38 fl.oz / 100mL") == \
        ["3.38 fl.oz / 100mL", "3.38 fl.oz", "100mL"]
    assert rc.variant_title_tokens("Black/White Stripe / L") == \
        ["Black/White Stripe / L", "Black/White Stripe", "L"]


def test_a_purely_numeric_whole_title_is_still_a_candidate():
    """The drop applies to FRAGMENTS only. A row whose entire title is "50" is a real value, and
    dropping it would leave nothing to match on."""
    assert rc.variant_title_tokens("50") == ["50"]
    got = rc.select_option_ids(_sole("50"), rc.variant_title_tokens("50"))
    assert got.ok


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
                                      {"Size": "Standard"}) == "response_axes_differ"


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
    assert not got.ok and got.reason == "variant:response_axes_differ"


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
    the label had silently become three tokens instead of one. Exact matching runs on these
    strings, so a spurious space is a label that can never match anything.

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


# ==================================================================================================
# THIRD-ROUND ADVERSARIAL REVIEW, PR #2140.
# ==================================================================================================


# --- P2-5: the cap is enforced in bounded STEPS, not on a number computed too late -----------------
#
# `aiter_bytes()` with no chunk_size lets httpx decode as much as each network read yields. Measured
# by the reviewer: a 16 KiB compressed read decoded to a SINGLE 16.8 MiB chunk -- eight times the
# cap -- and the whole allocation had already happened by the time `len(chunk)` could look at it.
# The bound was on a number, not on memory. These tests drive a REAL httpx client through
# MockTransport, so they exercise httpx's own decoding rather than the fake's slicing.

def _gzip_body(raw: bytes) -> bytes:
    import gzip
    return gzip.compress(raw)


@pytest.fixture
def mock_httpx(monkeypatch):
    """A real `httpx.AsyncClient` over `MockTransport`. No network, no sockets — but httpx's own
    response decoding, which is the thing under test here."""
    import httpx

    state = {"body": b"", "headers": {}, "status": 200}
    real_client = httpx.AsyncClient

    def handler(request):
        return httpx.Response(state["status"], headers=state["headers"], content=state["body"])

    class _Client(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return state


def test_a_compressible_body_is_read_in_bounded_steps(mock_httpx):
    """THE P2-5 REPRODUCTION. 16 MiB of zeros compresses to a few KiB, so without an explicit
    chunk_size httpx hands the whole decoded 16 MiB over in one piece — eight times the cap,
    already allocated. With chunk_size=65536 the worst case is the cap plus one chunk."""
    raw = b"\0" * (16 * 1024 * 1024)
    mock_httpx["body"] = _gzip_body(raw)
    mock_httpx["headers"] = {"content-encoding": "gzip"}

    seen = []
    original = rc._read_bounded

    async def watching(response, *, max_bytes=rc.MAX_RESPONSE_BYTES):
        real_iter = response.aiter_bytes

        def spy(*a, **kw):
            inner = real_iter(*a, **kw)

            async def gen():
                async for chunk in inner:
                    seen.append(len(chunk))
                    yield chunk
            return gen()
        response.aiter_bytes = spy
        return await original(response, max_bytes=max_bytes)

    import services.reap_agentic_client as module
    module._read_bounded = watching
    try:
        got = _run(rc.search_products(query="x"))
    finally:
        module._read_bounded = original

    assert not got.ok and got.error == "response_too_large"
    assert seen, "the body was never iterated"
    assert max(seen) <= 65536, f"largest decoded chunk was {max(seen)}"
    # And it aborted: it did not decode all 16 MiB before objecting.
    assert sum(seen) <= rc.MAX_RESPONSE_BYTES + 65536


def test_a_normal_body_still_round_trips_through_a_real_client(mock_httpx):
    """The control on the same transport: chunking must not break an ordinary response."""
    mock_httpx["body"] = json.dumps({"products": [], "warnings": ["OK"]}).encode()
    mock_httpx["headers"] = {}
    got = _run(rc.search_products(query="x"))
    assert got.ok and got.warnings == ["OK"]


def test_the_bounded_reader_asks_for_a_fixed_chunk_size(wire):
    """Asserted at the call, because the argument is the whole mechanism and it is invisible in
    the result: a reader that omits it looks identical until a compressible body arrives."""
    _run(rc.search_products(query="x"))
    assert wire.last_response.requested_chunk_size == 65536
    assert wire.last_response.largest_chunk <= 65536


# --- P2-6: Hebrew and Arabic points -----------------------------------------------------------------

@pytest.mark.parametrize("pointed,plain", [
    ("שָׁלוֹם", "שלום"),   # HE shalom
    ("בְּרֵאשִׁית", "בראשית"),
    ("كِتَاب", "كتاب"),         # AR kitab
    ("ٰاللّه", "الله"),
])
def test_a_pointed_spelling_equals_its_unpointed_one(pointed, plain):
    """These marks are neither stripped nor matched by `\\w`, so a pointed label did not merely
    fail to equal its unpointed spelling — it SHATTERED into one token per letter (שָׁלוֹם became
    `ש לו ם`). Under exact matching that can only cause a refusal, never a wrong accept, but it is
    a refusal nobody could diagnose from the reason string."""
    assert rc._norm(pointed) == rc._norm(plain) != ""
    assert " " not in rc._norm(pointed)


def test_pointed_labels_still_differ_when_the_letters_differ():
    """The control: folding the points must not fold the words together."""
    assert rc._norm("שלום") != rc._norm("שלוש")


@pytest.mark.parametrize("pointed,plain", [
    # U+0670 ARABIC LETTER SUPERSCRIPT ALEF, INTERIOR. A leading or trailing one is absorbed by
    # `.strip()` whether or not it is folded, so only an interior mark discriminates — the same
    # trap that hid the Cyrillic range in the previous round.
    ("رٰحمن", "رحمن"),
    ("الٰله", "الله"),
    # And an interior harakat, for the same reason.
    ("كتَاب", "كتاب"),
    # An interior Hebrew point.
    ("שלֹום", "שלום"),
])
def test_an_interior_point_is_folded_not_turned_into_a_space(pointed, plain):
    """An unfolded mark is not a word character, so it becomes a SPACE and splits one label into
    two tokens. Under exact matching that is a refusal rather than a wrong accept — but it is a
    refusal with no diagnosable cause."""
    assert rc._norm(pointed) == rc._norm(plain)
    assert " " not in rc._norm(pointed)


# --- P0-2: a decimal point is part of its number ------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1.7 oz", "1.7 oz"),
    ("0.5 oz", "0.5 oz"),
    ("3.38 fl.oz", "3.38 fl oz"),     # the dot after a LETTER still splits
    ("1,5 ml", "1,5 ml"),             # a comma decimal, as Europe writes it
    ("Serum 1.5", "serum 1.5"),
    (".5 oz", "5 oz"),                # a leading dot has no digit before it
    ("5. oz", "5 oz"),                # a trailing dot has no digit after it
])
def test_a_decimal_between_digits_survives_normalisation(text, expected):
    assert rc._norm(text) == expected


def test_a_decimal_quantity_is_not_confused_with_its_fraction():
    """The reproduction: `[^\\w]+ -> " "` made "1.7 oz" into {1, 7, oz}, which shared the token 7
    with "7 oz" — a 4x quantity that the token rule read as overlap, in BOTH directions."""
    assert rc._norm("1.7 oz") != rc._norm("7 oz")
    assert rc._norm("0.5 oz") != rc._norm("5 oz")


def test_product_name_matching_is_protected_by_the_same_rule():
    """Product names were already matched exactly, so this was a latent equality bug there too:
    "Serum 1.5" and "Serum 15" both normalised to token sets that a careless comparison would
    conflate, and the exact comparison would have equated nothing while looking right."""
    payload = {"products": [{"id": "prd_a", "merchant": {"name": "x.com"}, "name": "Serum 1.5"}]}
    assert not rc.match_product(payload, merchant_domain="x.com", product_name="Serum 15").ok
    assert rc.match_product(payload, merchant_domain="x.com", product_name="Serum 1.5").ok


# --- "Default Title": Shopify's placeholder is not a title ---------------------------------------------

@pytest.mark.parametrize("title", ["Default Title", "default title", "DEFAULT TITLE",
                                   "  Default Title  "])
def test_shopifys_default_title_placeholder_counts_as_no_title(title):
    """It is the string Shopify writes when a product has NO variants. Treating it as a real
    variant title refused every such row against Reap's real sole label."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"), rc.variant_title_tokens(title))
    assert got.ok
    assert got.single_value_axis_accepted_without_title is True


def test_default_title_on_a_multi_value_product_still_refuses():
    """It is "no title", so it gets no-title treatment — including the refusal when there is
    actually something to choose between."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "o1", "label": "Small", "available": True},
        {"optionId": "o2", "label": "Large", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Default Title"))
    assert not got.ok and got.reason == "no_variant_title_supplied"


def test_default_title_does_not_match_a_label_that_says_default_title():
    """It is dropped as a candidate, not kept as one: a Reap label literally reading "Default
    Title" is matched structurally (one axis, one value) rather than by string equality, and on a
    multi-value axis it matches nothing."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "o1", "label": "Default Title", "available": True},
        {"optionId": "o2", "label": "Large", "available": True}]}]}
    assert not rc.select_option_ids(product, rc.variant_title_tokens("Default Title")).ok


@pytest.mark.parametrize("title", [None, "", "   ", "\t\n"])
def test_whitespace_only_titles_are_no_title(title):
    """Documented as consistent with None rather than left to be inferred: they take the
    structural untitled path, they do NOT refuse as `variant_title_unusable`."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"), rc.variant_title_tokens(title))
    assert got.ok and got.single_value_axis_accepted_without_title is True


@pytest.mark.parametrize("title", ["!!!", "/", " - ", "---"])
def test_punctuation_only_titles_are_still_unusable_not_absent(title):
    """The distinction that must survive the Default Title change: the caller believes it
    constrained the resolution, so silently resolving anyway is worse than refusing."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"), rc.variant_title_tokens(title))
    assert not got.ok and got.reason == "variant_title_unusable"


# --- timeouts: bool and underscores ---------------------------------------------------------------------

def test_a_boolean_timeout_is_refused(wire):
    """`isinstance(True, int)` is True, so a bool reached `float()` and became a ONE SECOND
    timeout — from a caller that meant "yes, use a timeout". Same family as the quantity bug."""
    for value in (True, False):
        with pytest.raises(rc.ReapRequestError):
            _run(rc.search_products(query="x", timeout_seconds=value))
    assert wire.calls == []


@pytest.mark.parametrize("value", ["hello", [30], {"seconds": 30}])
def test_a_non_numeric_timeout_argument_is_refused(wire, value):
    with pytest.raises(rc.ReapRequestError):
        _run(rc.search_products(query="x", timeout_seconds=value))
    assert wire.calls == []


@pytest.mark.parametrize("value", ["1_0", "1_000", "3_0.5"])
def test_an_env_timeout_with_underscores_is_ignored(value, monkeypatch):
    """`float("1_0")` is 10.0. Python's numeric-literal underscores are not a format anyone writes
    in an env var on purpose, so "1_0" quietly meaning ten is a misreading of a typo."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", value)
    assert rc._env_timeout_floor() is None


def test_a_plain_env_timeout_is_still_honoured(monkeypatch):
    """The control."""
    monkeypatch.setenv("REAP_API_TIMEOUT_SECONDS", "30.5")
    assert rc._env_timeout_floor() == 30.5


# --- the candidate builder, as a unit ---------------------------------------------------------------------

def test_label_candidates_separates_the_title_from_the_aliases():
    whole, parts, aliases, supplied = rc.label_candidates(
        ["Flamingo Flirt"], ["Flamingo Flirt - Cream", "", "!!!"])
    # A title that did not split has no parts at all: the whole title is the only reading of it.
    assert whole == "flamingo flirt" and parts == []
    assert aliases == ["flamingo flirt cream"]
    assert supplied is True


def test_label_candidates_holds_the_whole_title_back_when_the_title_split():
    """The distinction that keeps parts from wandering: once a title has parts, the whole is a
    FALLBACK for the last axis rather than a competitor for any axis."""
    whole, parts, aliases, supplied = rc.label_candidates(
        rc.variant_title_tokens("Black / M"))
    assert whole == "black m"
    assert parts == ["black", "m"]
    assert aliases == [] and supplied is True


def test_label_candidates_reports_the_placeholder_as_no_title():
    whole, parts, aliases, supplied = rc.label_candidates(["Default Title"])
    assert whole is None and parts == [] and aliases == [] and supplied is False


def test_label_candidates_reports_a_broken_title_as_supplied():
    whole, parts, _, supplied = rc.label_candidates(["!!!"])
    assert whole is None and parts == [] and supplied is True


def test_label_candidates_deduplicates():
    whole, parts, aliases, _ = rc.label_candidates(["OS", "os", " o s "], ["OS", "OS"])
    assert whole == "os"          # the title split, so the whole is held back
    assert parts == ["o s"]       # "os" deduplicated against the whole
    assert aliases == ["os"]


# ==================================================================================================
# FOURTH-ROUND REVIEW: the recall regression that exact matching left behind.
# ==================================================================================================
#
# Round 3 replaced fuzzy matching with exact matching and, as part of that, dropped "/"-parts that
# were shorter than two characters or purely numeric. Those filters were a LEFTOVER of the fuzzy
# rule -- there a short fragment could match INSIDE a longer label, so dropping it protected
# something. Under exact matching a part can only match a label it EQUALS, so they protected
# nothing and cost most of a clothing catalog. Measured on Color=[Black,White] x Size=[S,M,L,XL]:
#
#     "Black / M"  -> tokens ['Black / M', 'Black']  -> REFUSED
#     "White / S"  -> REFUSED          "M / Black" -> REFUSED
#     "Red / 7"    -> REFUSED          "Black / 32" -> REFUSED
#     "Black / XL" -> resolved, because XL happens to be two letters
#
# The right fix is the separator, not a filter: Shopify joins options with " / " (whitespace both
# sides) and a BARE slash is part of a label.

def _axes(*specs):
    """A product from (axis_name, [labels]) pairs."""
    return {"id": "prd_x", "options": [
        {"name": name, "values": [
            {"optionId": f"opt_{name}_{v}".lower().replace(" ", "_"),
             "label": v, "available": True}
            for v in labels]}
        for name, labels in specs]}


APPAREL = _axes(("Color", ["Black", "White"]), ("Size", ["S", "M", "L", "XL"]))
SHOE = _axes(("Color", ["Red", "Blue"]), ("Size", ["6", "7", "8"]))
TROUSER = _axes(("Color", ["Black", "Navy"]), ("Waist", ["30", "32", "34"]))
THREE_AXIS = _axes(("Color", ["Black", "White"]), ("Size", ["S", "M"]),
                   ("Length", ["Regular", "Tall"]))
SIZE_ONLY = _axes(("Size", ["S", "M", "L"]))
SHOE_SIZE_ONLY = _axes(("Size", ["6", "7", "8"]))


SEPARATOR_TABLE = [
    # title,                      expected candidates
    ("Black / M",                 ["Black / M", "Black", "M"]),
    ("Red / 7",                   ["Red / 7", "Red", "7"]),
    ("M",                         ["M"]),
    ("S/M",                       ["S/M"]),
    ("50/50 Blend",               ["50/50 Blend"]),
    ("1/2 oz",                    ["1/2 oz"]),
    ("3.38 fl.oz / 100mL",        ["3.38 fl.oz / 100mL", "3.38 fl.oz", "100mL"]),
    ("Black/White Stripe / L",    ["Black/White Stripe / L", "Black/White Stripe", "L"]),
    ("Black / M / Tall",          ["Black / M / Tall", "Black", "M", "Tall"]),
    ("  Black  /  M  ",           ["Black  /  M", "Black", "M"]),      # padding is tolerated
    ("Black / M / Black",         ["Black / M / Black", "Black", "M"]),   # de-duplicated
    ("",                          []),
    (None,                        []),
]


@pytest.mark.parametrize("title,expected", SEPARATOR_TABLE)
def test_the_separator_is_a_slash_with_whitespace_on_both_sides(title, expected):
    """THE TOKEN TABLE. The whole title is always first; each " / "-part follows, trimmed,
    de-duplicated, order preserved. No length filter and no numeric filter."""
    assert rc.variant_title_tokens(title) == expected


MUST_RESOLVE = [
    ("Black / M", APPAREL, ["opt_color_black", "opt_size_m"]),
    ("White / S", APPAREL, ["opt_color_white", "opt_size_s"]),
    # Order-insensitive: the parts are matched to axes by VALUE, not by position.
    ("M / Black", APPAREL, ["opt_color_black", "opt_size_m"]),
    ("Black / XL", APPAREL, ["opt_color_black", "opt_size_xl"]),
    # A numeric size is a real size.
    ("Red / 7", SHOE, ["opt_color_red", "opt_size_7"]),
    ("Black / 32", TROUSER, ["opt_color_black", "opt_waist_32"]),
    # Three axes.
    ("Black / M / Tall", THREE_AXIS, ["opt_color_black", "opt_size_m", "opt_length_tall"]),
    # Single-axis products, including the one-character and numeric cases the filters killed.
    ("M", SIZE_ONLY, ["opt_size_m"]),
    ("S", SIZE_ONLY, ["opt_size_s"]),
    ("7", SHOE_SIZE_ONLY, ["opt_size_7"]),
]


@pytest.mark.parametrize("title,product,expected_ids", MUST_RESOLVE)
def test_the_common_apparel_title_shapes_resolve(title, product, expected_ids):
    got = rc.select_option_ids(product, rc.variant_title_tokens(title))
    assert got.ok, f"{title!r} must resolve: {got.reason}"
    assert sorted(got.option_ids) == sorted(expected_ids)


def test_a_single_axis_label_that_contains_the_separator_resolves_via_the_whole_title():
    """The reason the whole title is a candidate at all: " / " inside a label is not a separator,
    and only the whole title can match it."""
    product = _axes(("Size", ["3.38 fl.oz / 100mL", "1.7 fl.oz / 50mL"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("3.38 fl.oz / 100mL"))
    assert got.ok and got.chosen == {"Size": "3.38 fl.oz / 100mL"}


def test_a_single_axis_label_with_a_bare_slash_resolves():
    """"S/M" is one label. It was never two."""
    product = _axes(("Size", ["S/M", "L/XL"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("S/M"))
    assert got.ok and got.chosen == {"Size": "S/M"}


MUST_STILL_REFUSE = [
    # The title names one axis of two — no alias fixes that.
    ("Black", APPAREL, "axes_not_determined_by_title"),
    # A size this product does not sell.
    ("Black / XXS", APPAREL, "axes_not_determined_by_title"),
]


@pytest.mark.parametrize("title,product,reason", MUST_STILL_REFUSE)
def test_an_underdetermined_title_still_refuses(title, product, reason):
    got = rc.select_option_ids(product, rc.variant_title_tokens(title))
    assert not got.ok and got.reason == reason


def test_a_title_with_the_wrong_number_of_parts_matches_nothing_at_all():
    """THE COST OF THE ONE-PART-PER-AXIS RULE, stated as a test rather than discovered later. A
    one-part title against a two-axis product used to report which axis it HAD matched
    (`chosen == {"Color": "Black"}`); now its parts are not candidates at all, so it reports
    nothing matched.

    The refusal is the same either way -- "Black" cannot determine Size, and no reading of it
    ever resolved this product. What is lost is a diagnostic, and what is bought is that a title's
    halves can never be pooled and drawn from against a product whose axes they do not describe.
    That pooling is what let "Black/White Stripe / XL" resolve a plain "Black"."""
    got = rc.select_option_ids(APPAREL, rc.variant_title_tokens("Black"))
    assert not got.ok
    assert got.reason == "axes_not_determined_by_title"
    assert got.unmatched_axes == ["Color", "Size"]
    assert got.chosen == {}


def test_a_two_part_title_is_not_pooled_against_a_three_axis_product():
    """FEWER parts than axes is just as much a mismatch as more. "Black / M" against
    Color x Size x Length describes a different product shape from ours, and partially assigning
    two of its three axes would report a confidence the title does not support. Either way it
    refuses; the assertion is that NOTHING was matched."""
    got = rc.select_option_ids(THREE_AXIS, rc.variant_title_tokens("Black / M"))
    assert not got.ok
    assert got.chosen == {}
    assert got.option_ids == []
    assert got.unmatched_axes == ["Color", "Size", "Length"]


def test_a_two_part_title_is_not_pooled_against_a_one_axis_product():
    """The rule's whole point. "Black / M" offers `Black` and `M`; a one-axis Color product must
    not be allowed to draw `Black` out of that pair, because our row said Black AND M."""
    product = _axes(("Color", ["Black", "White"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / M"))
    assert not got.ok
    assert got.option_ids == []


# --- the new ambiguity rules that paying for order-insensitivity requires ---------------------------

def test_one_part_may_not_satisfy_two_axes():
    """Because every part is now offered to EVERY axis, a title can be readable two ways. "Red / M"
    against a Color axis that carries a label "M" AND a Size axis that carries "M" has no correct
    answer, so it refuses and names BOTH axes rather than picking by axis order."""
    product = _axes(("Color", ["Red", "M"]), ("Size", ["M"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Red / M"))
    assert not got.ok
    assert got.reason == "ambiguous_on_axis:Color,Size"
    assert got.option_ids == []


def test_two_parts_may_not_satisfy_one_axis():
    """The other direction: both parts name the same axis and nothing names the other."""
    product = _axes(("Color", ["Black", "White"]), ("Size", ["S", "M"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / White"))
    assert not got.ok and got.reason == "ambiguous_on_axis:Color"


def test_the_whole_title_may_not_supplement_a_partial_parts_reading():
    """A product whose Size axis happens to carry a label reading "Black / M". The part `Black`
    settles Color, and the whole title then equals the Size label — so a rule that only checked
    "is one axis left?" resolved Color=Black AND Size="Black / M", reading the word Black once as
    a colour and again as half of a size. The whole title is an ALTERNATIVE reading of the entire
    title, not a supplement to a partial one."""
    product = _axes(("Color", ["Black", "White"]), ("Size", ["Black / M", "L"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / M"))
    assert not got.ok
    assert got.unmatched_axes == ["Size"]
    assert got.option_ids == []


def test_the_whole_title_may_not_settle_one_axis_of_two():
    """With nothing settled by parts, a two-axis product cannot be resolved from the whole title
    either: it would pin one axis and leave the other undetermined."""
    product = _axes(("Color", ["Black / M", "White"]), ("Size", ["S", "L"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / M"))
    assert not got.ok and got.option_ids == []


def test_the_whole_title_is_used_when_it_is_the_last_axis_left():
    """The legitimate case: parts settle every other axis, and the remaining one carries a label
    that contains the separator."""
    product = _axes(("Color", ["Black", "White"]),
                    ("Size", ["3.38 fl.oz / 100mL", "1.7 fl.oz / 50mL"]))
    # Only the Color part matches; the Size label can only be matched by the whole title, and it
    # is not equal to it — so this refuses rather than guessing.
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / 3.38 fl.oz / 100mL"))
    assert not got.ok

    single = _axes(("Size", ["3.38 fl.oz / 100mL", "Other"]))
    assert rc.select_option_ids(single,
                                rc.variant_title_tokens("3.38 fl.oz / 100mL")).ok


def test_an_alias_obeys_the_same_one_axis_rule():
    """Aliases behave like parts: exact, on any ONE axis, and ambiguous if they claim two."""
    product = _axes(("Color", ["Special Edition"]), ("Size", ["Special Edition"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / M"),
                               accept_variant_labels=["Special Edition"])
    assert not got.ok and got.reason == "ambiguous_on_axis:Color,Size"


def test_an_alias_settles_one_axis_while_the_title_settles_the_other():
    """The everyday case the alias hook exists for, now on a two-axis product."""
    product = _axes(("Shade", ["Flamingo Flirt - Cream"]), ("Size", ["Full Size", "Mini"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt / Full Size"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok
    assert got.chosen == {"Shade": "Flamingo Flirt - Cream", "Size": "Full Size"}


# --- the round-3 refusals, re-asserted against the new separator ------------------------------------

@pytest.mark.parametrize("title,label", [
    ("50/50 Blend", "50"),      # the bare-slash fragment `50` no longer exists
    ("1/2 oz", "oz"),
    ("1/2 oz", "2 oz"),
])
def test_a_bare_slash_fragment_cannot_match_a_label(title, label):
    """These are the cases the numeric/length filters were covering for. The separator fix removes
    the fragments entirely, so the filters are not needed to stop them."""
    got = rc.select_option_ids(
        {"id": "prd_x", "options": [{"name": "Size", "values": [
            {"optionId": "opt_only", "label": label, "available": True}]}]},
        rc.variant_title_tokens(title))
    assert not got.ok and got.reason == "sole_label_differs:Size"


# --- F1: a bare-slash fragment that EXACTLY equals a partner label ---------------------------------
#
# The worst of the round-4 findings, because exact matching was supposed to have ended this class.
# Splitting on a bare slash manufactured a candidate that was a real, different product:
#
#     our "1/2 oz"  -> fragment "2 oz"  -> Reap's "2 oz"   $22 row, $88 variant, guard GREEN
#     our "1/4 ct"  -> fragment "4 ct"  -> Reap's "4 ct"
#     our "A/B Duo" -> fragment "B Duo" -> Reap's "B Duo"
#
# The fragment equalled the label, so nothing downstream could object. The separator fix removes
# the fragment; there is no rule that has to catch it afterwards.

F1_PAIRS = [
    ("1/2 oz", "2 oz"), ("1/2 oz", "oz"),
    ("1/4 ct", "4 ct"),
    ("A/B Duo", "B Duo"),
    ("1/2 oz", "1 oz"),
]


@pytest.mark.parametrize("our_title,reap_label", F1_PAIRS)
def test_a_bare_slash_never_manufactures_a_candidate(our_title, reap_label):
    got = rc.select_option_ids(_sole(reap_label), rc.variant_title_tokens(our_title))
    assert not got.ok, f"{our_title!r} must not resolve {reap_label!r}"
    assert got.reason == "sole_label_differs:Size"
    assert got.option_ids == []


def test_the_half_ounce_row_does_not_buy_the_two_ounce_bottle(monkeypatch):
    """F1 end to end. $22 half-ounce row, $88 two-ounce variant, and every guard green because
    the candidate our own tokeniser invented was exactly the label Reap returned."""
    fake = _one_axis_chain(monkeypatch, label="2 oz", price=88.0)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rose Serum",
        variant_title="1/2 oz", our_price=22.00, currency="USD"))
    assert not got.ok
    assert got.reason == "options:sole_label_differs:Size"
    assert fake.variant_body is None


# --- F2: a label that contains a bare slash must stay one label -------------------------------------

def test_a_label_containing_a_bare_slash_is_matched_whole():
    """F2. "Black/White Stripe / XL" split on every slash gave ['Black','White Stripe'] — so the
    REAL label "Black/White Stripe" was never a candidate, while plain "Black" was, and a product
    carrying both resolved the wrong colour."""
    product = _axes(("Color", ["Black", "Black/White Stripe"]), ("Size", ["M", "XL"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black/White Stripe / XL"))
    assert got.ok
    assert got.chosen == {"Color": "Black/White Stripe", "Size": "XL"}
    assert "opt_color_black" not in got.option_ids


def test_an_ombre_label_is_not_read_as_its_first_colour():
    product = _axes(("Color", ["Red", "Red/Blue Ombre"]), ("Size", ["S", "M"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Red/Blue Ombre / M"))
    assert got.ok
    assert got.chosen == {"Color": "Red/Blue Ombre", "Size": "M"}


def test_the_striped_row_resolves_end_to_end_and_not_as_plain_black(monkeypatch):
    search = {"products": [{"id": "prd_s", "merchant": {"name": "example.com"},
                            "name": "Rugby Shirt"}], "warnings": []}
    product = _axes(("Color", ["Black", "Black/White Stripe"]), ("Size", ["M", "XL"]))
    product["id"] = "prd_s"
    product["merchant"] = {"name": "example.com"}
    product["name"] = "Rugby Shirt"
    details = {"products": [product], "errors": []}
    variant = {"id": "var_s", "options": [{"name": "Color", "value": "Black/White Stripe"},
                                          {"name": "Size", "value": "XL"}],
               "price": {"amount": 60.0, "currency": "USD"}, "available": True}
    _chain(monkeypatch, search=search, details=details, variant=variant)
    got = _run(rc.resolve_our_row(
        merchant_domain="example.com", product_name="Rugby Shirt",
        variant_title="Black/White Stripe / XL", our_price=60.00, currency="USD"))
    assert got.ok and got.variant_id == "var_s"
    assert got.matched_options == {"Color": "Black/White Stripe", "Size": "XL"}


# --- F3: `accept_variant_labels` is a Sequence[str], and a str IS one ------------------------------

def test_a_bare_string_alias_is_wrapped_not_iterated():
    """F3, and the annotation permits it: `Sequence[str]` is satisfied by a `str`, so
    `accept_variant_labels="OS"` iterated the STRING into the aliases 'o' and 's'. Under the new
    separator rule a one-character label is legitimate — S, M, L are sizes — so 's' is a real
    alias for a real Small, and a One-Size row would have resolved a Small."""
    product = _axes(("Size", ["S", "M", "OS"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("One Size"),
                               accept_variant_labels="OS")
    assert got.ok
    assert got.chosen == {"Size": "OS"}          # not "S"


def test_a_bare_string_alias_cannot_reach_a_single_character_label():
    """The same bug from the other side: the characters of the string must not become keys."""
    product = _axes(("Size", ["S", "M", "L"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("One Size"),
                               accept_variant_labels="OS")
    assert not got.ok
    assert got.option_ids == []


@pytest.mark.parametrize("alias", [41669483823149, {"label": "OS"}, ["OS"], None, object()])
def test_a_non_string_alias_is_dropped_not_stringified(alias):
    """`str(...)` of an int or a dict produces a key nobody asserted. An alias is a human saying
    "this exact string names our object"; anything else is not that."""
    product = _axes(("Size", ["S", "OS"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("One Size"),
                               accept_variant_labels=[alias])
    assert not got.ok


def test_a_one_character_alias_is_kept():
    """Explicitly NOT filtered: under the current separator rule "M" is a real label, and a
    previous round's short-string filter is exactly what cost a clothing catalog."""
    product = _axes(("Size", ["S", "M", "L"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"),
                               accept_variant_labels=["M"])
    assert got.ok and got.chosen == {"Size": "M"}


def test_too_many_aliases_is_refused_rather_than_truncated():
    """Silently using the first 32 of 500 is a worse answer than refusing: the caller would not
    know which of its assertions were in force."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.select_option_ids(_sole("OS"), rc.variant_title_tokens("One Size"),
                             accept_variant_labels=[f"L{i}" for i in range(33)])
    assert "32" in str(exc.value)


def test_the_alias_cap_admits_exactly_thirty_two():
    """The boundary, so the cap is a cap and not an off-by-one."""
    aliases = [f"L{i}" for i in range(31)] + ["OS"]
    assert len(aliases) == 32
    got = rc.select_option_ids(_sole("OS"), rc.variant_title_tokens("One Size"),
                               accept_variant_labels=aliases)
    assert got.ok


def test_an_over_long_alias_is_dropped():
    """Dropped rather than truncated: a truncated alias is a key the caller never supplied, and it
    could match something."""
    long_alias = "X" * 129
    product = _axes(("Size", [long_alias]))
    assert not rc.select_option_ids(product, rc.variant_title_tokens("One Size"),
                                    accept_variant_labels=[long_alias]).ok
    ok_alias = "X" * 128
    product_ok = _axes(("Size", [ok_alias]))
    assert rc.select_option_ids(product_ok, rc.variant_title_tokens("One Size"),
                                accept_variant_labels=[ok_alias]).ok


# --- F5: partner text is sanitised before it is echoed ---------------------------------------------

def test_a_newline_in_an_axis_name_cannot_forge_a_log_line():
    """F5. The axis name and the label are partner-controlled and this module echoes both into
    `reason`, which is logged. A newline turns one reason into two lines."""
    product = {"id": "prd_x", "options": [{"name": "Size\nInjected: all clear",
                                           "values": [{"optionId": "o1", "label": "OS",
                                                       "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    assert "\n" not in (got.reason or "")
    assert "\n" not in json.dumps(got.candidates)


def test_control_characters_are_stripped_from_labels_and_axis_names():
    product = {"id": "prd_x", "options": [{"name": "Si\x00ze",
                                           "values": [{"optionId": "o1",
                                                       "label": "O\x07S\x1b[31m",
                                                       "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    flat = (got.reason or "") + json.dumps(got.candidates)
    for bad in ("\x00", "\x07", "\x1b"):
        assert bad not in flat


def test_an_enormous_axis_name_is_refused_and_its_reason_is_capped():
    """Past the COMPARISON bound it refuses outright — capping a string we then compare is what
    made two different names collide (F2). The reason line is still display-capped."""
    product = {"id": "prd_x", "options": [{"name": "S" * 10_000,
                                           "values": [{"optionId": "o1", "label": "OS",
                                                       "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    assert not got.ok and got.reason.startswith("label_too_long_to_compare:")
    assert len(got.reason) < 200


def test_an_enormous_label_is_refused_rather_than_truncated():
    got = rc.select_option_ids(_sole("L" * 10_000), rc.variant_title_tokens("Medium"))
    assert not got.ok and got.reason.startswith("label_too_long_to_compare:")


def test_a_merely_long_axis_name_is_capped_only_for_display():
    """Under the comparison bound it still resolves, and only the REPORTED copy is capped."""
    name = "S" * 200
    product = {"id": "prd_x", "options": [{"name": name, "values": [
        {"optionId": "o1", "label": "OS", "available": True},
        {"optionId": "o2", "label": "M", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    assert not got.ok and got.reason == "axes_not_determined_by_title"
    assert got.unmatched_axes == [name[:64]]
    assert len(got.unmatched_axes[0]) == 64


def test_the_candidate_list_is_bounded():
    """A product with a hundred single-value axes must not turn one refusal into a hundred-entry
    payload."""
    product = _axes(*[(f"Axis{i}", [f"Label{i}"]) for i in range(40)])
    got = rc.select_option_ids(product, ["Nothing At All"])
    assert not got.ok
    assert len(got.candidates) <= 10


def test_a_newline_in_a_returned_variant_value_cannot_forge_a_log_line():
    """The same sanitiser on the other side of the wire: `got[axis]` is interpolated into the
    substitution refusal."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "Mini\nInjected: all clear"}]}
    reason = rc.variant_matches_request(variant, {"Size": "Standard"})
    assert reason is not None and "\n" not in reason


def test_a_sanitised_axis_name_still_matches_between_the_two_sides():
    """The caps and the cleaning have to be the SAME on both sides, or `chosen` and `got` would
    stop agreeing on the key and every comparison would report a missing axis."""
    product = {"id": "prd_x", "options": [{"name": " Size ", "values": [
        {"optionId": "o1", "label": "OS", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("OS"))
    assert got.ok and got.chosen == {"Size": "OS"}
    variant = {"id": "var_x", "options": [{"name": "Size\t", "value": " OS "}]}
    assert rc.variant_matches_request(variant, got.chosen) is None


def test_a_partner_error_code_is_sanitised_before_it_becomes_a_reason():
    payload = {"products": [], "errors": [
        {"productId": "prd_x", "code": "GONE\nInjected: all clear"}]}
    _product, err = rc.details_for(payload, "prd_x")
    assert err is not None and "\n" not in err


# --- M10: "Default Title" is matched EXACTLY, not by prefix -------------------------------------------

def test_a_real_title_beginning_with_default_is_not_the_placeholder():
    """M10. Loosening the placeholder test to `startswith("default")` would make "Default Rose" —
    a real shade — take the no-title structural path, resolving whatever the product's sole value
    happens to be."""
    got = rc.select_option_ids(_sole("Flamingo Flirt - Cream"),
                               rc.variant_title_tokens("Default Rose"))
    assert not got.ok
    assert got.reason == "sole_label_differs:Size"
    assert got.single_value_axis_accepted_without_title is False


def test_a_real_title_beginning_with_default_still_matches_its_own_label():
    product = _axes(("Shade", ["Default Rose", "Default Blue"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Default Rose"))
    assert got.ok and got.chosen == {"Shade": "Default Rose"}


def test_an_alias_beginning_with_default_is_kept():
    """M10's twin in the alias filter."""
    got = rc.select_option_ids(_sole("Default Rose"), rc.variant_title_tokens("Flamingo"),
                               accept_variant_labels=["Default Rose"])
    assert got.ok and got.chosen == {"Size": "Default Rose"}


# --- M23: availability is reported for an alias match on a MULTI-value axis ----------------------------

def test_an_alias_match_on_a_multi_value_axis_still_reports_unavailability():
    """M23. The availability guard was only tested through the single-value branch. An alias can
    equally select an unavailable value on an axis with siblings — which is exactly where Reap
    substitutes — so the flag has to survive that path too."""
    product = {"id": "prd_x", "options": [{"name": "Shade", "values": [
        {"optionId": "opt_a", "label": "Flamingo Flirt - Cream", "available": False},
        {"optionId": "opt_b", "label": "Petal Pink - Matte", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt"),
                               accept_variant_labels=["Flamingo Flirt - Cream"])
    assert got.ok and got.option_ids == ["opt_a"]
    assert got.unavailable_axes == ["Shade"]
    assert got.chosen_available == {"Shade": False}


def test_an_alias_selected_unavailable_value_stops_the_chain_before_variant(monkeypatch):
    """And it reaches the refusal that matters: `/variant` is not called for a value Reap would
    substitute, however the value was selected."""
    details = json.loads(json.dumps(FLOWER_DETAILS))
    details["products"][0]["options"][0]["values"][0]["available"] = False
    fake = _chain(monkeypatch, search=FLOWER_SEARCH, details=details, variant=FLOWER_VARIANT)
    got = _run(rc.resolve_our_row(
        merchant_domain="flowerbeauty.com", product_name="Petal Pout Lip Color",
        variant_title="Flamingo Flirt",
        accept_variant_labels=["Flamingo Flirt - Cream"]))
    assert not got.ok
    assert got.reason.startswith("options:value_unavailable_at_reap:Shade")
    assert fake.variant_body is None


# --- an alias can also make a resolvable axis ambiguous, which fails closed ---------------------------

def test_a_value_with_no_option_id_is_refused_rather_than_sent_as_blank():
    """There is nothing to send for such a value: an empty `optionId` in the request body is a
    422 at best and, at worst, a request that means something we did not intend. It was the one
    guard in this function with no test, found by a mutation sweep."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "", "label": "OS", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("OS"))
    assert not got.ok and got.reason == "value_has_no_option_id:Size"
    assert got.option_ids == []

    multi = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "   ", "label": "OS", "available": True},
        {"optionId": "opt_m", "label": "M", "available": True}]}]}
    assert rc.select_option_ids(multi, rc.variant_title_tokens("OS")).reason == \
        "value_has_no_option_id:Size"


def test_the_partner_text_sanitiser_maps_absent_to_empty():
    """Where the B8 "a null label is not the string None" property actually lives now. The
    conditional in `_label_of` that used to carry it was inert once this function existed, so it
    was removed and the property is pinned here."""
    assert rc._safe_partner_text(None, 128) == ""
    assert rc._label_of({"label": None}) == ""
    assert rc._label_of({}) == ""
    assert rc._label_of(None) == ""
    assert "None" not in rc._label_of({"label": None})


def test_an_alias_can_turn_a_resolvable_axis_into_an_ambiguous_one():
    """Worth stating because it is the one way an alias makes things WORSE, and it is the safe
    direction: adding an alias that happens to equal a sibling label means two candidates name one
    axis, and the refusal is `ambiguous_on_axis` rather than a guess."""
    product = _axes(("Shade", ["Flamingo Flirt", "Petal Pink"]))
    without = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt"))
    assert without.ok

    with_alias = rc.select_option_ids(product, rc.variant_title_tokens("Flamingo Flirt"),
                                      accept_variant_labels=["Petal Pink"])
    assert not with_alias.ok and with_alias.reason == "ambiguous_on_axis:Shade"


# ==================================================================================================
# FIFTH-ROUND REVIEW. The matcher's wrong-object class is closed; these are the three blockers
# BEHIND it -- in the guard that checks the echo, and in the alias seam.
# ==================================================================================================


# --- F1: the echo must describe the selection, axis for axis ---------------------------------------
#
# `got` was a dict comprehension, so a response carrying the SAME axis twice collapsed and the LAST
# entry won. Measured: product Size=[M,L], we send M's optionId, partner answers 200 with
# [{"Size","L"},{"Size","M"}] -> the guard saw M and PASSED. Reversing the two entries made it
# fire, so whether we accepted a wrong variant depended on the partner's list order.

@pytest.mark.parametrize("order", [["L", "M"], ["M", "L"]])
def test_a_duplicated_axis_in_the_response_is_refused_in_either_order(order):
    """Both orders, because order-dependence WAS the bug: one of these two passed and the other
    refused, for the same response content."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": order[0]},
                                          {"name": "Size", "value": order[1]}]}
    assert rc.variant_matches_request(variant, {"Size": "M"}) == \
        "response_duplicate_axis_name:Size"


def test_an_axis_duplicated_only_by_whitespace_is_still_a_duplicate():
    """"Size" beside "Size " collapsed the same way, because both clean to the same key."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "L"},
                                          {"name": "Size ", "value": "M"}]}
    assert rc.variant_matches_request(variant, {"Size": "M"}) == \
        "response_duplicate_axis_name:Size"


def test_an_extra_axis_in_the_response_is_refused():
    """An axis we never selected means the echo is not a description of our selection."""
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "M"},
                                          {"name": "Color", "value": "Red"}]}
    assert rc.variant_matches_request(variant, {"Size": "M"}) == "response_axes_differ"


def test_a_missing_axis_in_the_response_is_refused():
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "M"}]}
    assert rc.variant_matches_request(variant, {"Size": "M", "Color": "Red"}) == \
        "response_axes_differ"


def test_an_exactly_matching_axis_set_still_passes():
    """The control: a rule that refused every echo would pass all four negatives above."""
    variant = {"id": "var_x", "options": [{"name": "Color", "value": "Red"},
                                          {"name": "Size", "value": "M"}]}
    assert rc.variant_matches_request(variant, {"Size": "M", "Color": "Red"}) is None


def test_the_duplicated_axis_reaches_the_chain_as_a_refusal(monkeypatch):
    """End to end: we send M's optionId and the partner echoes both L and M."""
    product = {"id": "prd_d", "merchant": {"name": "example.com"}, "name": "Tee",
               "options": [{"name": "Size", "values": [
                   {"optionId": "opt_m", "label": "M", "available": True},
                   {"optionId": "opt_l", "label": "L", "available": True}]}]}
    search = {"products": [{"id": "prd_d", "merchant": {"name": "example.com"},
                            "name": "Tee"}], "warnings": []}
    variant = {"id": "var_d", "options": [{"name": "Size", "value": "L"},
                                          {"name": "Size", "value": "M"}],
               "price": {"amount": 20.0, "currency": "USD"}, "available": True}
    _chain(monkeypatch, search=search, details={"products": [product], "errors": []},
           variant=variant)
    got = _run(rc.resolve_our_row(merchant_domain="example.com", product_name="Tee",
                                  variant_title="M", our_price=20.00, currency="USD"))
    assert not got.ok
    assert got.reason == "variant:response_duplicate_axis_name:Size"
    assert got.variant_id is None


# --- F2: matching uses FULL strings; only display is capped -----------------------------------------
#
# `chosen` held the display-capped label and the guard compared capped-to-capped, so two labels
# sharing a 128-character prefix truncated EQUAL and a substitution passed. The caps agreeing on
# both sides is what created the collision.

#: Two limited-edition names that are identical for the first 128 characters — the display cap —
#: and differ only after it. Under the old rule both truncated to the same string.
LONG_ONE = "L" * 128 + "ONE"
LONG_TWO = "L" * 128 + "TWO"


def test_two_long_labels_sharing_a_prefix_do_not_compare_equal():
    assert len(LONG_ONE) == 131
    assert LONG_ONE[:128] == LONG_TWO[:128]      # the collision the caps used to create
    assert LONG_ONE != LONG_TWO
    got = rc.select_option_ids(_sole(LONG_ONE), [LONG_ONE])
    assert got.ok
    # `chosen` carries the FULL label -- capping it here is the bug.
    assert got.chosen["Size"] == LONG_ONE and len(got.chosen["Size"]) == 131
    substituted = {"id": "var_x", "options": [{"name": "Size", "value": LONG_TWO}]}
    reason = rc.variant_matches_request(substituted, got.chosen)
    assert reason is not None and reason.startswith("substituted_on_axis:Size")


def test_two_long_axis_names_sharing_a_prefix_are_not_one_axis():
    """The same collision on the key rather than the value: two axis names sharing a 64-character
    prefix capped to the same string, so the response looked like it answered our axis."""
    name_one = "A" * 64 + "ONE"
    name_two = "A" * 64 + "TWO"
    assert name_one[:64] == name_two[:64]
    product = {"id": "prd_x", "options": [{"name": name_one, "values": [
        {"optionId": "o1", "label": "M", "available": True}]}]}
    got = rc.select_option_ids(product, ["M"])
    assert got.ok and got.chosen == {name_one: "M"}
    echoed_other_axis = {"id": "var_x", "options": [{"name": name_two, "value": "M"}]}
    assert rc.variant_matches_request(echoed_other_axis, got.chosen) == "response_axes_differ"


def test_a_long_label_that_matches_exactly_still_resolves():
    """The control: length alone must not refuse. 300 characters is fine; it is only past the
    COMPARISON bound that we stop."""
    label = "R" * 300
    got = rc.select_option_ids(_sole(label), [label])
    assert got.ok and got.chosen == {"Size": label}
    echoed = {"id": "var_x", "options": [{"name": "Size", "value": label}]}
    assert rc.variant_matches_request(echoed, got.chosen) is None


def test_a_label_past_the_comparison_bound_refuses():
    got = rc.select_option_ids(_sole("Z" * 1025), ["Z" * 1025])
    assert not got.ok and got.reason.startswith("label_too_long_to_compare:")


def test_a_label_exactly_at_the_comparison_bound_is_allowed():
    """The boundary, so 1024 is a bound and not an off-by-one."""
    label = "Z" * 1024
    got = rc.select_option_ids(_sole(label), [label])
    assert got.ok


def test_the_response_side_has_the_same_comparison_bound():
    variant = {"id": "var_x", "options": [{"name": "Size", "value": "Z" * 1025}]}
    assert rc.variant_matches_request(variant, {"Size": "M"}).startswith(
        "label_too_long_to_compare:")


def test_the_display_copy_is_capped_while_the_matching_copy_is_not():
    """The two forms, side by side. `chosen` is for comparing; `chosen_display` is for printing."""
    got = rc.select_option_ids(_sole(LONG_ONE), [LONG_ONE])
    assert got.chosen["Size"] == LONG_ONE
    assert got.chosen_display["Size"] == LONG_ONE[:128]
    assert len(got.chosen_display["Size"]) == 128


def test_the_resolution_reports_the_display_copy(monkeypatch):
    """`matched_options` is a REPORTING field — the probe script prints it — so it carries the
    capped copy, while the guard upstream compared the full one."""
    product = {"id": "prd_l", "merchant": {"name": "example.com"}, "name": "Tee",
               "options": [{"name": "Size", "values": [
                   {"optionId": "opt_one", "label": LONG_ONE, "available": True}]}]}
    search = {"products": [{"id": "prd_l", "merchant": {"name": "example.com"},
                            "name": "Tee"}], "warnings": []}
    variant = {"id": "var_l", "options": [{"name": "Size", "value": LONG_ONE}],
               "price": {"amount": 20.0, "currency": "USD"}, "available": True}
    _chain(monkeypatch, search=search, details={"products": [product], "errors": []},
           variant=variant)
    got = _run(rc.resolve_our_row(merchant_domain="example.com", product_name="Tee",
                                  variant_title=LONG_ONE, our_price=20.00, currency="USD"))
    assert got.ok
    assert got.matched_options == {"Size": LONG_ONE[:128]}


# --- F3: a discarded part is still evidence ----------------------------------------------------------
#
# When the parts-count rule discards the title's parts they stop SELECTING — which is right — but
# they also stopped being looked at, so an alias could settle an axis to a value our own row names
# AGAINST. The identical contradiction was already refused when the parts were usable.

def test_an_alias_may_not_contradict_a_discarded_part():
    """One axis Size=[M,L]; our title says L; the alias says M. Before: ok=True, Size=M."""
    got = rc.select_option_ids(_axes(("Size", ["M", "L"])),
                               rc.variant_title_tokens("Black / L"),
                               accept_variant_labels=["M"])
    assert not got.ok and got.reason == "ambiguous_on_axis:Size"
    assert got.option_ids == []


def test_the_realistic_volume_contradiction_is_refused():
    """Size=["50ml","150ml"], title "Red / 50ml", alias "150ml" resolved the 150ml — three times
    the product, chosen against what our own row said."""
    got = rc.select_option_ids(_axes(("Size", ["50ml", "150ml"])),
                               rc.variant_title_tokens("Red / 50ml"),
                               accept_variant_labels=["150ml"])
    assert not got.ok and got.reason == "ambiguous_on_axis:Size"


def test_an_alias_that_agrees_with_the_discarded_part_still_resolves():
    """The contradiction check must fire on DISAGREEMENT only. Here the alias names the same value
    the title does, so there is nothing to disagree about."""
    got = rc.select_option_ids(_axes(("Size", ["M", "L"])),
                               rc.variant_title_tokens("Black / L"),
                               accept_variant_labels=["L"])
    assert got.ok and got.chosen == {"Size": "L"}


def test_a_discarded_part_still_never_selects_anything():
    """The other half: evidence, not selection. Without an alias the same title resolves nothing,
    because a two-part title against a one-axis product has its parts discarded."""
    got = rc.select_option_ids(_axes(("Size", ["M", "L"])),
                               rc.variant_title_tokens("Black / L"))
    assert not got.ok and got.reason == "axes_not_determined_by_title"
    assert got.option_ids == []


def test_a_contradiction_on_a_second_axis_is_caught_too():
    """Three parts against two axes: the parts are discarded, two aliases settle both axes, and
    the discarded `L` contradicts the alias `M` on Size."""
    product = _axes(("Color", ["Black", "White"]), ("Size", ["M", "L"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / L / Tall"),
                               accept_variant_labels=["Black", "M"])
    assert not got.ok and got.reason == "ambiguous_on_axis:Size"


def test_the_whole_title_reading_is_also_subject_to_contradiction():
    """The whole-title path settles an axis too, so it gets the same check."""
    product = _axes(("Size", ["Black / L", "M"]))
    got = rc.select_option_ids(product, rc.variant_title_tokens("Black / L"),
                               accept_variant_labels=["M"])
    assert not got.ok and got.reason == "ambiguous_on_axis:Size"


# --- F4: both reported lists are bounded and sanitised -------------------------------------------------

def test_both_reported_lists_are_capped():
    """The comment claimed the payload was bounded while `unmatched_axes` was not."""
    product = _axes(*[(f"Axis{i}", [f"Lab{i}"]) for i in range(40)])
    got = rc.select_option_ids(product, ["nothing at all"])
    assert not got.ok
    assert len(got.candidates) == 10
    assert len(got.unmatched_axes) == 10


def test_an_axis_name_is_cleaned_on_BOTH_sides_so_the_two_still_agree():
    """`chosen`'s key and the response's key have to be produced by the SAME cleaning, or a
    perfectly good match reads as `response_axes_differ`. This is the reason the request side
    cleans the axis name rather than merely stripping it: an interior newline or zero-width space
    survives `.strip()`, and then only one of the two sides has it."""
    product = {"id": "prd_x", "options": [{"name": "Si\nze", "values": [
        {"optionId": "o1", "label": "M", "available": True},
        {"optionId": "o2", "label": "L", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("M"))
    assert got.ok and got.chosen == {"Si ze": "M"}
    echoed = {"id": "var_x", "options": [{"name": "Si\nze", "value": "M"}]}
    assert rc.variant_matches_request(echoed, got.chosen) is None


def test_two_axis_names_differing_only_by_an_invisible_are_one_axis():
    """And the fail-closed half: cleaning makes them collide, and a collision is a refusal. Left
    uncleaned they would look like two distinct axes and both would be sent."""
    product = {"id": "prd_x", "options": [
        {"name": "Size", "values": [{"optionId": "o1", "label": "M", "available": True}]},
        {"name": "Size​", "values": [{"optionId": "o2", "label": "L", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("M"))
    assert not got.ok and got.reason == "duplicate_axis_name:Size"


def test_unmatched_axis_entries_are_sanitised():
    product = {"id": "prd_x", "options": [
        {"name": "Si\nze", "values": [{"optionId": "o1", "label": "A", "available": True},
                                      {"optionId": "o2", "label": "B", "available": True}]}]}
    got = rc.select_option_ids(product, ["nothing"])
    assert got.unmatched_axes == ["Si ze"]
    assert "\n" not in json.dumps(got.unmatched_axes)


# --- F5: "Default Title" is the placeholder, not a prefix ------------------------------------------------

def test_a_title_that_merely_starts_with_default_title_is_a_real_title():
    """F5. The exact-equality test on the TITLE side had no killing test — `startswith` survived.
    "Default Title Deluxe" is a real variant name and must not take the no-title path."""
    got = rc.select_option_ids(_sole("Mini"), rc.variant_title_tokens("Default Title Deluxe"))
    assert not got.ok
    assert got.reason == "sole_label_differs:Size"
    assert got.single_value_axis_accepted_without_title is False


def test_default_title_deluxe_still_matches_its_own_label():
    got = rc.select_option_ids(_sole("Default Title Deluxe"),
                               rc.variant_title_tokens("Default Title Deluxe"))
    assert got.ok and got.chosen == {"Size": "Default Title Deluxe"}


# --- F6: invisible format characters ----------------------------------------------------------------------

@pytest.mark.parametrize("ch,name", [
    ("‮", "RIGHT-TO-LEFT OVERRIDE"),
    ("​", "ZERO WIDTH SPACE"),
    ("﻿", "BYTE ORDER MARK"),
    ("⁦", "LEFT-TO-RIGHT ISOLATE"),
    ("⁩", "POP DIRECTIONAL ISOLATE"),
    ("‏", "RIGHT-TO-LEFT MARK"),
])
def test_invisible_format_characters_never_reach_a_reason_or_a_candidate(ch, name):
    """C0/C1 stripping missed these entirely. U+202E reverses the rendering of everything after it
    in whatever reads our log; the rest are invisible splitters."""
    product = {"id": "prd_x", "options": [{"name": f"Si{ch}ze", "values": [
        {"optionId": "o1", "label": f"O{ch}S", "available": True}]}]}
    got = rc.select_option_ids(product, rc.variant_title_tokens("Medium"))
    flat = (got.reason or "") + json.dumps(got.candidates) + json.dumps(got.unmatched_axes)
    assert ch not in flat, name


def test_an_invisible_split_does_not_stop_a_label_matching():
    """Decided deliberately and done ONCE, in `_norm`: a zero-width space is not part of what a
    merchant is selling, so "O<ZWSP>S" and "OS" are the same label. Before, `\\w` turned it into a
    SPACE and the label normalised to "o s", which refused an identical-looking row."""
    assert rc._norm("O​S") == rc._norm("OS") == "os"
    got = rc.select_option_ids(_sole("O​S"), rc.variant_title_tokens("OS"))
    assert got.ok


def test_stripping_invisibles_cannot_merge_two_visibly_different_labels():
    """The control that matters: removing something invisible must not make two visible strings
    equal. A real space still separates tokens."""
    assert rc._norm("OS") != rc._norm("O S")
    assert rc._norm("AB") != rc._norm("A B")
    product = _axes(("Size", ["OS", "O S"]))
    # Two genuinely different labels stay different, so a title naming one does not claim both.
    assert rc.select_option_ids(product, rc.variant_title_tokens("OS")).ok


# --- F7: a non-string label is no label ---------------------------------------------------------------------

@pytest.mark.parametrize("label", [0, 7, 3.5, True, False, {"text": "M"}, ["M"]])
def test_a_non_string_label_is_treated_as_absent(label):
    """`_label_of({"label": 0})` gave "0" while `_norm(0)` gives "" — the same value was a label to
    one half of the module and nothing to the other."""
    assert rc._label_of({"label": label}) == ""


def test_a_sole_value_with_a_numeric_label_is_not_auto_filled():
    """The consequence of that split: the untitled structural path read `_label_of` and accepted a
    value the matcher considered unlabelled, recording "0" in `chosen` as the thing we asked for."""
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "o1", "label": 0, "available": True}]}]}
    got = rc.select_option_ids(product, [])
    assert not got.ok
    assert got.chosen == {}
    assert got.candidates == []          # not reported either


def test_a_numeric_label_is_unmatchable_from_a_title():
    product = {"id": "prd_x", "options": [{"name": "Size", "values": [
        {"optionId": "o1", "label": 7, "available": True},
        {"optionId": "o2", "label": "M", "available": True}]}]}
    assert not rc.select_option_ids(product, rc.variant_title_tokens("7")).ok
    assert rc.select_option_ids(product, rc.variant_title_tokens("M")).ok


def test_a_string_label_that_looks_numeric_still_works():
    """The control — a shoe size "7" as a STRING is a real label and must keep resolving."""
    got = rc.select_option_ids(_axes(("Size", ["6", "7", "8"])), rc.variant_title_tokens("7"))
    assert got.ok and got.chosen == {"Size": "7"}

# ==============================================================================================
# ENROLLMENTS, CHECKOUTS AND THE POLLING READS  (WP1)
# ==============================================================================================
#
# Same three kinds of test as above, and the same rule: assert on what reaches the TRANSPORT,
# not on what a builder returned. Every guard below was mutated one at a time and confirmed to
# kill at least one test; the table is in the PR body.
#
# The absence assertions here all carry a CONTROL -- a case that proves the mechanism doing the
# asserting is present and working -- because `assert "cardId" not in body` passes just as
# happily when the body is empty, when the call never happened, and when the test's own fixture
# is misspelt.

import os

ENROLLMENT_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
OTHER_UUID = "9c858901-8a57-4791-81fe-4c455b099bc9"

RETURN_URL = "https://agent.pivota.cc/reap/return?click=abc123"

#: Our ledger's enrollment row id. Idempotency material, never a body field.
ATTEMPT_ID = "enr-row-9f2a"

ENROLLMENT_CREATED = {
    "id": ENROLLMENT_UUID,
    "status": "REQUIRES_ACTION",
    "source": "EXTERNAL",
    "owner": {"type": "CLIENT_REFERENCE", "id": "cust_42"},
    "nextAction": {"type": "REDIRECT",
                   "url": "https://pay.prava.space/enroll/3fa85f64",
                   "expiresAt": "2026-09-17T21:00:00Z"},
}

ENROLLMENT_ACTIVE = {
    "id": ENROLLMENT_UUID,
    "status": "ACTIVE",
    "owner": {"type": "CLIENT_REFERENCE", "id": "cust_42"},
    "paymentMethod": {"type": "CARD", "network": "VISA", "last4": "4242",
                      "expiryMonth": 12, "expiryYear": 2029},
    "nextAction": None,
    "createdAt": "2026-09-17T20:00:00Z",
    "updatedAt": "2026-09-17T20:04:00Z",
}

CHECKOUT_CREATED = {
    "id": "chk_7f3a",
    "status": "REQUIRES_ACTION",
    "quoteId": "f1e2d3c4",
    "enrollmentId": ENROLLMENT_UUID,
    "amount": {"amount": 152.60, "currency": "USD"},
    "nextAction": {"type": "REDIRECT",
                   "url": "https://pay.prava.space/checkout/chk_7f3a",
                   "expiresAt": "2026-09-17T21:00:00Z"},
}

CHECKOUT_COMPLETED = {
    "id": "chk_7f3a",
    "status": "COMPLETED",
    "quoteId": "f1e2d3c4",
    "enrollmentId": ENROLLMENT_UUID,
    "orderId": "ord_991",
    "finalAmount": {"amount": 152.60, "currency": "USD"},
    "nextAction": None,
    "createdAt": "2026-09-17T20:10:00Z",
    "updatedAt": "2026-09-17T20:12:00Z",
}

#: A 400 that echoes the request back. Reap's `error.detail` is a free-form object in the spec,
#: so there is nothing stopping a partner putting the request in it -- and our request carries a
#: buyer's shipping address. This is the fixture the body-blind rule is measured against.
CHECKOUT_REJECTED_BODY = {
    "error": {
        "code": "AGENTIC_REQUEST_REJECTED",
        "message": "enrollment 3fa85f64 is not active for buyer at 900 Brannan St",
        "detail": {
            "code": "ENROLLMENT_NOT_ACTIVE",
            "request": {"shippingAddress": GOOD_ADDRESS, "email": "buyer@example.com"},
        },
    },
}


@pytest.fixture
def clean_env(monkeypatch):
    """The return-url allowlist is read from the environment on every call, so a value left over
    from another test would make these pass or fail for the wrong reason."""
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    monkeypatch.delenv("REAP_API_TIMEOUT_SECONDS", raising=False)
    return monkeypatch


# --- the enrollment branch we must NOT be able to build ----------------------------------------
#
# REAP_CARD and BIN_SPONSOR enroll an ALREADY-ISSUED card. That is the Program-Funded rail, which
# is dormant by design under "Pivota never deposits, prefunds, custodies, or is liable for a
# balance" (6 Sep). A default of EXTERNAL would be a suggestion; these are walls.

@pytest.mark.parametrize("source", ["REAP_CARD", "BIN_SPONSOR"])
def test_the_card_issuance_enrollment_branches_cannot_be_built(source, clean_env):
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL, source=source)
    assert "card-issuance" in str(exc.value)


@pytest.mark.parametrize("spelling", ["cardId", "card_id", "CardID"])
def test_a_card_id_is_refused_under_any_spelling(spelling, clean_env):
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(
            owner_id="cust_42", return_url=RETURN_URL, **{spelling: ENROLLMENT_UUID}
        )
    assert "card-issuance rail" in str(exc.value)


@pytest.mark.parametrize("source", ["EXTERNAL_V2", "SOMETHING_REAP_ADDS", "", None])
def test_an_unknown_source_is_refused_rather_than_defaulted(source, clean_env):
    """Not merely "not REAP_CARD". A typo, or a source Reap adds later, must also refuse: the
    builder emits EXTERNAL and nothing else, so anything it does not recognise is a request it
    cannot honestly build.

    Case is the one thing it IS forgiving about (`external` is accepted), because `source` is our
    own parameter rather than a value from a partner and the builder emits the canonical spelling
    regardless -- see the body assertion in `test_the_external_branch_is_the_one_that_does_build`.
    """
    with pytest.raises(rc.ReapRequestError):
        rc.build_enrollment_request(owner_id="c", return_url=RETURN_URL, source=source)


def test_the_external_branch_is_the_one_that_does_build(clean_env):
    """THE CONTROL for the three refusals above. Without it they would all still pass if the
    builder raised on everything, which is not a client."""
    body = rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL)
    assert body["source"] == "EXTERNAL"
    assert body["owner"] == {"type": "CLIENT_REFERENCE", "id": "cust_42"}
    assert body["presentation"] == {"type": "REDIRECT", "returnUrl": RETURN_URL}
    assert "cardId" not in json.dumps(body)


def test_an_enrollment_body_has_exactly_the_spec_keys(clean_env):
    body = rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL,
                                       email="buyer@example.com")
    assert set(body) == {"source", "owner", "presentation"}
    assert set(body["owner"]) == {"type", "id", "email"}
    assert set(body["presentation"]) == {"type", "returnUrl"}


def test_an_enrollment_without_an_email_omits_the_key_rather_than_sending_null(clean_env):
    """`email` is optional in the schema and is real buyer PII. A `null` would be a key we sent
    to a third party for no reason; absence is the honest encoding of "we did not supply one"."""
    body = rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL)
    assert "email" not in body["owner"]


def test_an_enrollment_needs_an_owner_id(clean_env):
    with pytest.raises(rc.ReapRequestError):
        rc.build_enrollment_request(owner_id="  ", return_url=RETURN_URL)


def test_an_unknown_enrollment_field_is_refused_not_dropped(clean_env):
    """Unlike the address builder, which DROPS keys outside its whitelist. The asymmetry is
    deliberate: a stray key on an address is a caller mistake with a known-good field set to fall
    back to, but a stray key on an enrollment is more likely somebody assembling a branch we
    refuse to support -- and Reap drops unknown keys with a 200, so dropping it here would let
    them build it, watch it succeed, and get an enrollment that is not what they asked for.

    (This test was written because the mutation sweep found the guard unprotected: with the
    `if unsupported: raise` removed, every other test in this file still passed.)
    """
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL,
                                    mandate={"type": "RECURRING"})
    assert "unsupported enrollment fields" in str(exc.value)
    assert "mandate" in str(exc.value)


# --- checkout creation: the field that disappeared ---------------------------------------------

def test_a_checkout_body_is_exactly_the_current_spec_shape(clean_env):
    body = rc.build_checkout_request(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                     return_url=RETURN_URL)
    assert set(body) == {"quoteId", "enrollmentId", "presentation"}
    assert body["quoteId"] == "f1e2d3c4"
    assert body["enrollmentId"] == ENROLLMENT_UUID


def test_the_owner_field_that_used_to_be_required_cannot_be_sent(clean_env):
    """`owner` was one of three REQUIRED fields on this body and is now absent from the schema
    entirely, with `info.version` unchanged at 1.0.0. Reap accepts unknown keys silently with a
    200 and drops them, so a body still sending it would look exactly like one that worked."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_checkout_request(
            quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID, return_url=RETURN_URL,
            owner={"type": "CLIENT_REFERENCE", "id": "cust_42"},
        )
    assert "no longer takes an `owner`" in str(exc.value)


def test_an_unknown_checkout_field_is_refused_too(clean_env):
    with pytest.raises(rc.ReapRequestError):
        rc.build_checkout_request(quote_id="q", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL, attribution={"src": "pivota"})


def test_a_checkout_enrollment_id_must_be_a_uuid(clean_env):
    """The spec patterns `enrollmentId` as a uuid in the request body. A quote id in that slot is
    the same class of mistake as #2136's storefront variant id -- a value from the wrong
    namespace in a field that accepts strings."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_checkout_request(quote_id="f1e2d3c4", enrollment_id="f1e2d3c4",
                                  return_url=RETURN_URL)
    assert "uuid" in str(exc.value)


# --- returnUrl: it is ours, and it may carry a query --------------------------------------------

def test_a_return_url_may_carry_a_query_string(clean_env):
    """It has to. There is NO attribution field in any Reap request body and agentic resources
    have no webhooks, so a click id on our own return URL is the only join we have between a
    checkout and the session that started it."""
    assert rc.validate_return_url("https://agent.pivota.cc/r?click=abc&x=1") \
        == "https://agent.pivota.cc/r?click=abc&x=1"


@pytest.mark.parametrize("url", [
    "http://agent.pivota.cc/r",                     # not https
    "https://evil.example/r",                       # not ours
    "https://agent.pivota.cc.evil.example/r",       # suffix confusion
    "https://notagent.pivota.cc/r",                 # no dot before the suffix
    "https://agent.pivota.cc@evil.example/r",       # userinfo: hostname is evil.example
    "",
])
def test_a_return_url_we_cannot_vouch_for_is_refused(url, clean_env):
    with pytest.raises(rc.ReapRequestError):
        rc.validate_return_url(url)


def test_userinfo_is_refused_even_when_the_HOST_is_one_of_ours(clean_env):
    """The case that makes the userinfo check load-bearing rather than redundant.

    `https://agent.pivota.cc@evil.example/r` is caught by the HOST rule alone -- `urlparse` reads
    its hostname as `evil.example`. Reverse it and the host rule passes: the hostname really is
    ours, and what is attacker-controlled is the credential prefix a human reads as the domain.
    The mutation sweep found this: with the userinfo check removed, every other return-url test
    still passed, so the guard was protected by nothing.
    """
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.validate_return_url("https://evil.example@agent.pivota.cc/r")
    assert "userinfo" in str(exc.value)


def test_a_subdomain_of_an_allowed_return_host_is_accepted(clean_env):
    """THE CONTROL for the refusals above: an exact-or-dot-suffix rule has to admit the dot-suffix
    case, or it is just an exact-match rule with extra code."""
    clean_env.setenv("REAP_RETURN_URL_HOSTS", "pivota.cc")
    assert rc.validate_return_url("https://agent.pivota.cc/r")


def test_the_return_url_allowlist_comes_from_the_environment(clean_env):
    clean_env.setenv("REAP_RETURN_URL_HOSTS", "staging.pivota.cc, other.example")
    assert rc.return_url_hosts() == ("staging.pivota.cc", "other.example")
    assert rc.validate_return_url("https://other.example/r")
    with pytest.raises(rc.ReapRequestError):
        # And the DEFAULT host is no longer allowed, which proves the env value replaced the
        # list rather than being appended to it.
        rc.validate_return_url("https://agent.pivota.cc/r")


def test_an_empty_allowlist_variable_means_the_default_not_nothing(clean_env):
    """An operator who clears a variable should get the shipped behaviour. A client that refused
    every enrollment because a variable was blank would be a config change that reads as an
    outage."""
    clean_env.setenv("REAP_RETURN_URL_HOSTS", "   ")
    assert rc.return_url_hosts() == rc.DEFAULT_RETURN_URL_HOSTS


# --- the hosted URL on the way IN: we hand this to a buyer ---------------------------------------

@pytest.mark.parametrize("url", [
    "https://pay.prava.space/checkout/x",
    "https://prava.space/x",
    "https://sandbox.api.reap.global/hosted/x",
])
def test_a_hosted_url_on_an_allowed_suffix_is_accepted(url):
    assert rc.hosted_url_is_allowed(url) is True


@pytest.mark.parametrize("url", [
    "http://pay.prava.space/x",                 # not https
    "https://evilprava.space/x",                # no dot before the suffix
    "https://prava.space.evil.example/x",       # suffix confusion
    "https://reap.global.evil.example/x",
    "https://evil.example/x",
    "https://prava.space@evil.example/x",       # userinfo, host is evil.example
    "https://evil.example@pay.prava.space/x",   # userinfo, host IS Reap's -- the reverse case
    "javascript:alert(1)",
    "",
    None,
])
def test_a_hosted_url_we_cannot_vouch_for_is_rejected(url):
    assert rc.hosted_url_is_allowed(url) is False


def test_a_response_carrying_a_foreign_hosted_url_is_refused_whole(wire, clean_env):
    """This is the one that matters. `nextAction.url` is a page we hand a BUYER, who types their
    own card into it. A response that is flagged but passed through still gets read -- that field
    is what the whole call was for -- so the refusal replaces the response and drops `data`."""
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"]["url"] = "https://evil.example/enroll/3fa85f64"
    wire.next_payload = payload
    got = _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    assert not got.ok
    assert got.error == "hosted_url_not_allowed"
    assert got.data == {}
    assert "evil.example" not in json.dumps(got.data)


def test_an_allowed_hosted_url_does_come_through(wire, clean_env):
    """THE CONTROL. Without it, a `_refuse_unsafe_hosted_url` that refused everything would pass
    the test above and break every real call."""
    wire.next_payload = ENROLLMENT_CREATED
    got = _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    assert got.ok
    assert got.data["nextAction"]["url"] == "https://pay.prava.space/enroll/3fa85f64"


def test_a_foreign_hosted_url_is_refused_on_the_checkout_leg_too(wire, clean_env):
    """`feedback: check whether a caller has the same defect`. The guard has four call sites and
    a guard applied at one of them is a guard that does not exist at the other three."""
    payload = json.loads(json.dumps(CHECKOUT_CREATED))
    payload["nextAction"]["url"] = "https://evilprava.space/checkout/chk_7f3a"
    wire.next_payload = payload
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                 return_url=RETURN_URL))
    assert not got.ok and got.error == "hosted_url_not_allowed"


def test_a_foreign_hosted_url_is_refused_on_the_polling_reads_too(wire, clean_env):
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"]["url"] = "https://evil.example/x"
    wire.next_payload = payload
    assert _run(rc.get_enrollment(ENROLLMENT_UUID)).error == "hosted_url_not_allowed"
    assert _run(rc.get_checkout("chk_7f3a")).error == "hosted_url_not_allowed"


def test_hosted_action_returns_the_url_and_its_expiry():
    assert rc.hosted_action(ENROLLMENT_CREATED) == (
        "https://pay.prava.space/enroll/3fa85f64", "2026-09-17T21:00:00Z")


def test_hosted_action_applies_the_same_suffix_rule():
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"]["url"] = "https://evil.example/x"
    assert rc.hosted_action(payload) is None


def test_hosted_action_is_none_when_there_is_no_action():
    assert rc.hosted_action(ENROLLMENT_ACTIVE) is None
    assert rc.hosted_action({}) is None
    assert rc.hosted_action(None) is None


def test_a_missing_expiry_is_none_rather_than_a_refusal():
    """The spec requires only `type` and `url` on a nextAction. A caller that read a missing
    expiry as "expired" would refuse every action Reap sends without one."""
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"].pop("expiresAt")
    assert rc.hosted_action(payload) == ("https://pay.prava.space/enroll/3fa85f64", None)


# --- path parameters: a caller-supplied id must not be able to move the request ------------------

@pytest.mark.parametrize("bad", [
    "../../admin",
    "abc/def",
    "abc?ownerId=someone-else",
    "abc#frag",
    "abc def",
    "%2e%2e%2fadmin",
    "a" * 65,
    "",
])
def test_an_id_that_could_alter_the_path_is_refused(bad, clean_env):
    with pytest.raises(rc.ReapRequestError):
        _run(rc.get_checkout(bad))


def test_a_well_formed_checkout_id_is_accepted(wire, clean_env):
    """THE CONTROL for the parametrised refusals: `_path_id` that refused everything would pass
    all eight of them."""
    wire.next_payload = CHECKOUT_COMPLETED
    assert _run(rc.get_checkout("chk_7f3a")).ok


def test_an_enrollment_path_id_must_be_a_uuid(clean_env):
    with pytest.raises(rc.ReapRequestError):
        _run(rc.get_enrollment("chk_7f3a"))


def test_a_quote_id_is_NOT_required_to_be_a_uuid(wire, clean_env):
    """SPEC WINS OVER THE PLAN HERE. The plan asked for a uuid shape on quote and checkout ids;
    the spec types every one of these path parameters as `string, minLength 1`, and a measured
    sandbox quote id is `f1e2d3c4` -- eight hex characters. Requiring a uuid would refuse real
    ids while looking like a tightening. The charset rule gives the property a path parameter
    actually needs (no `/`, `?`, `#` or escape) without inventing a format Reap does not use."""
    wire.next_payload = QUOTE_200
    assert _run(rc.get_quote("f1e2d3c4")).ok


def test_a_bad_id_refuses_BEFORE_anything_reaches_the_transport(wire, clean_env):
    """`feedback: trace the value to the sink`. A check that ran but let the request out anyway
    is not a check, and this is the assertion that distinguishes the two."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.get_checkout("abc?x=1"))
    assert wire.calls == []


def test_a_shipping_option_id_is_charset_checked(clean_env):
    with pytest.raises(rc.ReapRequestError):
        rc.build_shipping_option_request("ship std/../x")
    assert rc.build_shipping_option_request("ship_std") == {"shippingOptionId": "ship_std"}


# --- the GET seam: what actually reaches the wire ------------------------------------------------

def test_a_get_carries_the_version_header_and_the_key(wire, clean_env):
    wire.next_payload = ENROLLMENT_ACTIVE
    _run(rc.get_enrollment(ENROLLMENT_UUID))
    call = wire.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"https://sandbox.api.reap.global/agentic/enrollments/{ENROLLMENT_UUID}"
    assert call["headers"]["Reap-Version"] == "2025-02-14"
    assert call["headers"]["Authorization"] == "Bearer sk_test_key"


def test_a_get_carries_no_idempotency_key(wire, clean_env):
    """`/agentic/enrollments` is BOTH a create and a list. Keying off the path alone -- which is
    what `_IDEMPOTENT_PATHS` does -- would put an Idempotency-Key on the list too, asking a
    partner to replay a 24-hour-old page for a poll."""
    wire.next_payload = {"items": [], "nextCursor": None}
    _run(rc.list_enrollments(owner_id="cust_42"))
    assert "Idempotency-Key" not in wire.calls[0]["headers"]


def test_the_enrollment_CREATE_on_that_same_path_does_carry_one(wire, clean_env):
    """THE CONTROL for the test above: if `_headers` never attached a key at all, that test would
    pass and every retried create would mint a second enrollment."""
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    assert "Idempotency-Key" in wire.calls[0]["headers"]
    assert wire.calls[0]["method"] == "POST"


def test_a_get_sends_no_content_type(wire, clean_env):
    wire.next_payload = ENROLLMENT_ACTIVE
    _run(rc.get_enrollment(ENROLLMENT_UUID))
    assert "Content-Type" not in wire.calls[0]["headers"]


def test_the_owner_id_goes_in_params_not_pasted_into_the_url(wire, clean_env):
    """Handed to httpx to encode. An id string-formatted into the URL by us is the other half of
    the path-injection surface `_path_id` covers."""
    wire.next_payload = {"items": [], "nextCursor": None}
    _run(rc.list_enrollments(owner_id="cust 42/&x=1", limit=5))
    call = wire.calls[0]
    assert call["url"] == "https://sandbox.api.reap.global/agentic/enrollments"
    assert call["params"]["ownerId"] == "cust 42/&x=1"
    assert "?" not in call["url"]


def test_listing_enrollments_without_an_owner_is_refused_before_egress(wire, clean_env):
    """`ownerId` is `required: true` in the spec and its absence is a 422. A listing with no
    owner is not a broader listing, it is an error."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.list_enrollments(owner_id=""))
    assert wire.calls == []


def test_a_list_limit_is_clamped_to_the_specs_bounds(wire, clean_env):
    wire.next_payload = {"items": [], "nextCursor": None}
    _run(rc.list_enrollments(owner_id="cust_42", limit=5000))
    assert wire.calls[0]["params"]["limit"] == 100
    _run(rc.list_enrollments(owner_id="cust_42", limit=0))
    assert wire.calls[1]["params"]["limit"] == 1


def test_a_get_bounds_the_response_by_its_declared_length(wire, clean_env):
    """`_get` is a SECOND egress path, so the base's size-bound tests -- which drive `_post` --
    say nothing about it. `feedback: check whether a caller has the same defect, not whether it
    still compiles`: a bound applied at one verb and not the other is a bound that does not
    exist for half the module's traffic."""
    wire.next_headers = {"content-length": str(rc.MAX_RESPONSE_BYTES + 1)}
    wire.next_payload = CHECKOUT_COMPLETED
    got = _run(rc.get_checkout("chk_7f3a"))
    assert not got.ok and got.error == "response_too_large"
    assert got.data == {}


def test_a_get_bounds_the_response_by_the_bytes_actually_read(wire, clean_env):
    """The declared length is the cheap check; this is the one that cannot be lied about. A
    partner that omits or understates `content-length` must not get past the bound."""
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    got = _run(rc.get_checkout("chk_7f3a"))
    assert not got.ok and got.error == "response_too_large"


def test_a_get_of_a_normal_size_is_not_refused(wire, clean_env):
    """THE CONTROL for the two above: a bound that refused everything would pass both and break
    every poll."""
    wire.next_payload = CHECKOUT_COMPLETED
    assert _run(rc.get_checkout("chk_7f3a")).ok


def test_an_oversized_FAILURE_body_is_not_parsed_for_its_error_code(wire, clean_env):
    """The gap the extraction opened, and the reason `_error_codes` carries the bound itself.

    In `_post` the size check sits AFTER the status branch -- a 4xx returns before reaching it --
    so reading a failed body from inside that branch is outside the protection, and an error
    payload is no smaller than a success one. Bounding inside `_error_codes` closes it for both
    verbs without restructuring the base's `_post`."""
    wire.next_status = 400
    wire.next_content = b"x" * (rc.MAX_RESPONSE_BYTES + 1)
    wire.next_payload = CHECKOUT_REJECTED_BODY
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    assert not got.ok
    assert got.error_code is None and got.error_detail_code is None
    # THE CONTROL: the same body under the bound DOES yield its codes, so the None above is the
    # bound acting and not the extractor being broken.
    wire.next_content = None
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    assert got.error_detail_code == "ENROLLMENT_NOT_ACTIVE"


def test_a_get_reuses_the_bases_env_timeout_helper(monkeypatch):
    """Not a parallel copy. Two implementations of a floor rule drift, and the first version of
    `_get` already disagreed with `_env_timeout_floor` about a zero or negative value."""
    calls = []
    real = rc._env_timeout_floor
    monkeypatch.setattr(rc, "_env_timeout_floor", lambda: (calls.append(1), real())[1])
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")

    import httpx
    _Recorder.calls = []
    _Recorder.next_status = 200
    _Recorder.next_payload = CHECKOUT_COMPLETED
    _Recorder.next_headers = None
    _Recorder.next_content = None
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)

    _run(rc.get_checkout("chk_7f3a"))
    assert calls, "_get computed a timeout without going through _env_timeout_floor"


def test_an_unconfigured_client_makes_no_GET_either(monkeypatch):
    """The configuration boundary is per-verb, and `_get` is a second egress path. It having its
    own copy of this check is exactly why it needs its own test."""
    monkeypatch.delenv("REAP_API_BASE_URL", raising=False)
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    got = _run(rc._get("/agentic/enrollments"))
    assert not got.ok and got.error == "reap_client_not_configured"


def test_a_get_to_a_host_we_cannot_justify_raises_before_the_key_is_attached(monkeypatch):
    monkeypatch.setenv("REAP_API_BASE_URL", "https://evil.example")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    with pytest.raises(rc.ReapConfigError):
        _run(rc._get("/agentic/enrollments"))


# --- redirects are never followed ------------------------------------------------------------

def test_no_call_follows_redirects(wire, clean_env):
    """A 30x that httpx replayed would resend the `Authorization` header -- our API key -- to
    whatever host the `Location` named. The host allowlist guards the URL WE build; nothing would
    guard a URL a RESPONSE built.

    Asserted at the transport rather than by reading the source, and on both verbs, because this
    is a constructor argument and `_post` and `_get` build their own clients."""
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    wire.next_payload = ENROLLMENT_ACTIVE
    _run(rc.get_enrollment(ENROLLMENT_UUID))
    assert [c["follow_redirects"] for c in wire.calls] == [False, False]


# --- timeouts -----------------------------------------------------------------------------------

def test_a_checkout_create_gets_the_slow_path_bound(wire, clean_env):
    wire.next_payload = CHECKOUT_CREATED
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                            return_url=RETURN_URL))
    assert wire.calls[0]["timeout"] >= 30.0


def test_an_enrollment_create_is_a_FAST_path(wire, clean_env):
    """It is not talking to a merchant's commerce layer the way a quote is. Letting the 35 s
    bound leak onto it would put a 35 s hang in a serving path."""
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    assert wire.calls[0]["timeout"] == rc._DEFAULT_TIMEOUT_S
    assert wire.calls[0]["timeout"] != rc._QUOTE_TIMEOUT_S


def test_the_polling_reads_are_fast_too(wire, clean_env):
    wire.next_payload = CHECKOUT_COMPLETED
    _run(rc.get_checkout("chk_7f3a"))
    assert wire.calls[0]["timeout"] == rc._DEFAULT_TIMEOUT_S
    assert wire.calls[0]["timeout"] != rc._QUOTE_TIMEOUT_S


def test_an_env_timeout_is_a_floor_on_a_get_not_an_override(wire, clean_env):
    """A global 3 s would otherwise silently shorten every per-path default -- and the per-path
    defaults are MEASURED bounds. It can lengthen, never shorten."""
    clean_env.setenv("REAP_API_TIMEOUT_SECONDS", "3")
    wire.next_payload = CHECKOUT_COMPLETED
    _run(rc.get_checkout("chk_7f3a"))
    assert wire.calls[0]["timeout"] == rc._DEFAULT_TIMEOUT_S
    # Above the default, so it may raise the bound -- and does.
    clean_env.setenv("REAP_API_TIMEOUT_SECONDS", str(rc._DEFAULT_TIMEOUT_S + 20))
    _run(rc.get_checkout("chk_7f3a"))
    assert wire.calls[1]["timeout"] == rc._DEFAULT_TIMEOUT_S + 20


def test_an_explicit_timeout_still_wins_on_a_get(wire, clean_env):
    wire.next_payload = CHECKOUT_COMPLETED
    _run(rc.get_checkout("chk_7f3a", timeout_seconds=2.0))
    assert wire.calls[0]["timeout"] == 2.0


# --- idempotency: a retry must replay, not double-create -------------------------------------------

def test_a_checkout_retry_with_a_different_click_id_replays(wire, clean_env, monkeypatch):
    """THE CENTRAL IDEMPOTENCY TEST. Our `returnUrl` carries a click id, so a retry after an
    unknown outcome arrives with a DIFFERENT query string. A key derived from the whole body
    would hash differently and CREATE A SECOND CHECKOUT against the same quote -- the exact
    failure an idempotency key exists to prevent, reintroduced by the key itself."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    wire.next_payload = CHECKOUT_CREATED
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                            return_url="https://agent.pivota.cc/r?click=FIRST"))
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                            return_url="https://agent.pivota.cc/r?click=SECOND"))
    assert wire.calls[0]["headers"]["Idempotency-Key"] == wire.calls[1]["headers"]["Idempotency-Key"]


def test_a_different_quote_gets_a_different_checkout_key(wire, clean_env, monkeypatch):
    """THE CONTROL. Without it, an idempotency key that was a constant would pass the test
    above, and two different purchases would collapse into one."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    wire.next_payload = CHECKOUT_CREATED
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                            return_url=RETURN_URL))
    _run(rc.create_checkout(quote_id="OTHERQUOTE", enrollment_id=ENROLLMENT_UUID,
                            return_url=RETURN_URL))
    assert wire.calls[0]["headers"]["Idempotency-Key"] != wire.calls[1]["headers"]["Idempotency-Key"]


def test_a_different_enrollment_gets_a_different_checkout_key(wire, clean_env, monkeypatch):
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    wire.next_payload = CHECKOUT_CREATED
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                            return_url=RETURN_URL))
    _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=OTHER_UUID,
                            return_url=RETURN_URL))
    assert wire.calls[0]["headers"]["Idempotency-Key"] != wire.calls[1]["headers"]["Idempotency-Key"]


def test_the_checkout_key_is_derived_from_the_two_ids_and_nothing_else():
    assert rc.idempotency_material("/agentic/checkouts", {
        "quoteId": "q1", "enrollmentId": ENROLLMENT_UUID,
        "presentation": {"type": "REDIRECT", "returnUrl": "https://agent.pivota.cc/r?click=x"},
    }) == {"quoteId": "q1", "enrollmentId": ENROLLMENT_UUID}


def test_the_enrollment_key_carries_no_buyer_email(wire, clean_env, monkeypatch):
    """An idempotency key goes in a header on every retry, and the enrollment body carries the
    buyer's email. The key must not be a function of it -- so changing ONLY the email must not
    change the key, which is a stronger claim than the string not appearing in the hash."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL,
                              email="alice@example.com"))
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL,
                              email="bob@example.com"))
    assert wire.calls[0]["headers"]["Idempotency-Key"] == wire.calls[1]["headers"]["Idempotency-Key"]
    keys = json.dumps([c["headers"]["Idempotency-Key"] for c in wire.calls])
    assert "alice" not in keys and "example.com" not in keys
    # THE CONTROL: the email really was in the two bodies, so the equality above is about the
    # key's material and not about a body that never differed.
    assert wire.calls[0]["body"]["owner"]["email"] == "alice@example.com"
    assert wire.calls[1]["body"]["owner"]["email"] == "bob@example.com"


def test_a_different_owner_gets_a_different_enrollment_key(wire, clean_env, monkeypatch):
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_99", return_url=RETURN_URL))
    assert wire.calls[0]["headers"]["Idempotency-Key"] != wire.calls[1]["headers"]["Idempotency-Key"]


def test_a_quote_key_is_still_derived_from_the_whole_body():
    """The narrowing applies to checkouts and enrollments ONLY. Every field of a quote body
    changes what is quoted, so narrowing that one would make two different carts share a key."""
    body = {"items": [{"variantId": "var_x", "quantity": 1}], "email": "b@example.com"}
    assert rc.idempotency_material("/agentic/quotes", body) == body


# --- error codes: a narrow, shape-checked hole in the body-blind rule -------------------------------

def test_the_machine_readable_codes_survive_a_400(wire, clean_env):
    """`ENROLLMENT_NOT_ACTIVE` means "send the buyer back to the hosted card page" and
    `AGENTIC_RESOURCE_NOT_FOUND` means "this id is gone". A bare `reap_status_400` collapses both
    into "something went wrong" and leaves a buyer stuck."""
    wire.next_status = 400
    wire.next_payload = CHECKOUT_REJECTED_BODY
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    assert not got.ok and got.status == 400
    assert got.error_code == "AGENTIC_REQUEST_REJECTED"
    assert got.error_detail_code == "ENROLLMENT_NOT_ACTIVE"


def test_a_body_echoing_an_address_does_not_leak_through_the_code_extraction(wire, clean_env, caplog):
    """THE POINT OF THE SHAPE CHECK. `error.detail` is a free-form object in the spec, so nothing
    stops a partner putting our request in it -- and our request carries a buyer's address. Only
    two scalar fields are read, and `error.message` (free text, here naming a street) is not one
    of them."""
    wire.next_status = 400
    wire.next_payload = CHECKOUT_REJECTED_BODY
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    flat = json.dumps({"data": got.data, "error": got.error, "code": got.error_code,
                       "detail": got.error_detail_code})
    assert "Brannan" not in flat
    assert "buyer@example.com" not in flat
    assert "Brannan" not in caplog.text and "buyer@example.com" not in caplog.text
    # THE CONTROL: the address really was in the fixture, so the absences above are about the
    # extraction and not about a fixture that never had one.
    assert "Brannan" in json.dumps(CHECKOUT_REJECTED_BODY)


def test_an_error_code_that_is_not_code_shaped_is_dropped(wire, clean_env):
    """A partner value that is not `^[A-Z_]{3,64}$` is not a code, and carrying it out would make
    the field an arbitrary-content channel out of a body we promised not to read."""
    wire.next_status = 400
    wire.next_payload = {"error": {"code": "900 Brannan St, San Francisco",
                                   "detail": {"code": "x" * 200}}}
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    assert got.error_code is None and got.error_detail_code is None


def test_a_failure_with_no_error_block_leaves_the_codes_none(wire, clean_env):
    wire.next_status = 500
    wire.next_payload = {"oops": True}
    got = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                  return_url=RETURN_URL))
    assert got.error_code is None and got.error_detail_code is None
    assert got.error == "reap_status_500"


def test_a_404_on_a_polling_read_carries_its_code(wire, clean_env):
    wire.next_status = 404
    wire.next_payload = {"error": {"code": "AGENTIC_RESOURCE_NOT_FOUND", "message": "gone",
                                   "detail": None}}
    got = _run(rc.get_checkout("chk_7f3a"))
    assert got.error_code == "AGENTIC_RESOURCE_NOT_FOUND"
    assert got.data == {}


# --- the state machines: unknown is never a success -------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("ACTIVE", "active"),
    ("REQUIRES_ACTION", "pending"),
    ("FAILED", "dead"),
    ("EXPIRED", "dead"),
    ("REVOKED", "dead"),
])
def test_every_enrollment_status_the_spec_names_is_mapped(status, expected):
    assert rc.enrollment_state({"status": status}) == expected


@pytest.mark.parametrize("payload", [
    {"status": "SOMETHING_REAP_ADDED"},
    {"status": ""},
    {},
    None,
    {"status": 7},
    "not a dict",
])
def test_an_unrecognised_enrollment_status_is_unknown_not_active_and_not_dead(payload):
    """Reap changed a required field on this surface between two reads of a document whose
    version did not move. A mapping that folded an unknown string into `dead` would retire a live
    enrollment; one that folded it into `active` would send a buyer to a checkout that cannot
    complete. `unknown` is a state a caller has to handle, which is the honest answer."""
    assert rc.enrollment_state(payload) == "unknown"


@pytest.mark.parametrize("status,expected", [
    ("REQUIRES_ACTION", "awaiting_buyer"),
    ("PROCESSING", "processing"),
    ("COMPLETED", "completed"),
    ("FAILED", "failed"),
    ("EXPIRED", "expired"),
])
def test_every_checkout_status_the_spec_names_is_mapped(status, expected):
    assert rc.checkout_state({"status": status}) == expected


@pytest.mark.parametrize("payload", [{"status": "REFUNDED"}, {}, None, {"status": None}])
def test_an_unrecognised_checkout_status_is_unknown(payload):
    assert rc.checkout_state(payload) == "unknown"


def test_the_two_state_vocabularies_do_not_overlap():
    """They are different machines and a caller that mixed them up should not get a value that
    happens to be valid in the other. The only shared value is `unknown`, deliberately."""
    shared = set(rc.ENROLLMENT_STATES.values()) & set(rc.CHECKOUT_STATES.values())
    assert shared == set()


def test_processing_is_not_terminal_and_completed_is():
    """An `orderId` appears only on COMPLETED. A poller that stopped on PROCESSING would report
    a purchase that may still fail."""
    assert rc.checkout_is_terminal(CHECKOUT_COMPLETED) is True
    assert rc.checkout_is_terminal({"status": "PROCESSING"}) is False
    assert rc.checkout_is_terminal({"status": "REQUIRES_ACTION"}) is False


def test_an_unknown_status_is_not_terminal():
    """A status we cannot name is a reason to keep looking and tell a human, never a reason to
    stop and declare an outcome."""
    assert rc.checkout_is_terminal({"status": "REFUNDED"}) is False


# --- the detail code: a SECOND explainer, keyed differently --------------------------------------
#
# The ordering invariant on `REFUSAL_EXPLANATIONS`, the two-options distinctness, the placeholder
# sweep and the unknown-reason fallback are all pinned ABOVE by the base's own four tests, and
# WP1's new reasons were added to that file's `ALL_REASONS` list so they inherit every one of
# them. What is left to test here is the part the base does not have.

def test_a_detail_code_explains_what_to_DO_about_it():
    """`explain_refusal` answers "what does this reason mean"; this answers "what do I do about
    the machine-readable code inside the failure". `ENROLLMENT_NOT_ACTIVE` is the one that
    matters: the move is to send the buyer back to the hosted page, and creating the checkout
    again will not help."""
    text = rc.explain_detail_code("ENROLLMENT_NOT_ACTIVE")
    assert text and "hosted card page" in text


def test_an_unknown_detail_code_gets_None_rather_than_a_guess():
    """None, not a fallback sentence. `explain_refusal` can fall back because our own reason
    vocabulary is closed; `error.detail` is a free-form object at the partner's discretion, so
    an unrecognised code has no honest explanation to offer."""
    assert rc.explain_detail_code("SOMETHING_REAP_ADDED") is None
    assert rc.explain_detail_code(None) is None
    assert rc.explain_detail_code("") is None


def test_the_two_explainers_stay_separate():
    """`explain_refusal` keeps ONE parameter and returns a string -- the base's signature, with
    four tests pinning it. Widening it to take a detail code would have made those tests assert
    less than they read as asserting."""
    import inspect
    assert list(inspect.signature(rc.explain_refusal).parameters) == ["reason"]
    assert isinstance(rc.explain_refusal("transport_error:ReadTimeout"), str)


# --- shipping option selection --------------------------------------------------------------------

def test_selecting_a_shipping_option_posts_to_the_quotes_subpath(wire, clean_env):
    wire.next_payload = QUOTE_200
    _run(rc.select_shipping_option(quote_id="f1e2d3c4", shipping_option_id="ship_std"))
    call = wire.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://sandbox.api.reap.global/agentic/quotes/f1e2d3c4/shipping-option"
    assert call["body"] == {"shippingOptionId": "ship_std"}


def test_a_shipping_option_call_carries_no_idempotency_key(wire, clean_env):
    """The spec marks `Idempotency-Key` required on exactly three POSTs -- quotes, checkouts and
    enrollments -- and this is not one of them. Selecting an option is idempotent by nature: it
    sets a field."""
    wire.next_payload = QUOTE_200
    _run(rc.select_shipping_option(quote_id="f1e2d3c4", shipping_option_id="ship_std"))
    assert "Idempotency-Key" not in wire.calls[0]["headers"]


def test_a_quote_id_in_the_shipping_path_is_validated(wire, clean_env):
    with pytest.raises(rc.ReapRequestError):
        _run(rc.select_shipping_option(quote_id="f1/../x", shipping_option_id="ship_std"))
    assert wire.calls == []


def test_the_updated_quote_comes_back_with_a_new_breakdown(wire, clean_env):
    """The response IS the quote. A caller that read the total from the quote it already had
    would report the pre-shipping number."""
    updated = json.loads(json.dumps(QUOTE_200))
    updated["amountBreakdown"]["shipping"] = {"amount": 9.99, "currency": "USD"}
    updated["amountBreakdown"]["finalAmount"] = {"amount": 162.59, "currency": "USD"}
    wire.next_payload = updated
    got = _run(rc.select_shipping_option(quote_id="f1e2d3c4", shipping_option_id="ship_std"))
    assert got.ok and rc.quote_total(got.data) == (162.59, "USD")


# --- the whole sequence, at the transport ------------------------------------------------------

def test_enroll_then_poll_then_checkout_reaches_the_paths_in_order(wire, clean_env):
    wire.next_payload = ENROLLMENT_CREATED
    created = _run(rc.create_enrollment(attempt_id=ATTEMPT_ID, owner_id="cust_42", return_url=RETURN_URL))
    assert rc.enrollment_state(created.data) == "pending"
    assert rc.hosted_action(created.data)[0].startswith("https://pay.prava.space/")

    wire.next_payload = ENROLLMENT_ACTIVE
    polled = _run(rc.get_enrollment(ENROLLMENT_UUID))
    assert rc.enrollment_state(polled.data) == "active"
    assert rc.hosted_action(polled.data) is None       # nothing left for a human to do

    wire.next_payload = CHECKOUT_CREATED
    checkout = _run(rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                       return_url=RETURN_URL))
    assert rc.checkout_state(checkout.data) == "awaiting_buyer"

    wire.next_payload = CHECKOUT_COMPLETED
    done = _run(rc.get_checkout("chk_7f3a"))
    assert rc.checkout_state(done.data) == "completed"
    assert rc.checkout_is_terminal(done.data)
    assert done.data["orderId"] == "ord_991"

    assert [(c["method"], c["url"].rsplit("reap.global", 1)[-1]) for c in wire.calls] == [
        ("POST", "/agentic/enrollments"),
        ("GET", f"/agentic/enrollments/{ENROLLMENT_UUID}"),
        ("POST", "/agentic/checkouts"),
        ("GET", "/agentic/checkouts/chk_7f3a"),
    ]


# --- the spec pin -------------------------------------------------------------------------------
#
# Reap changed a required field overnight with `info.version` still 1.0.0, and Reap accepts
# unknown keys silently with a 200 -- so a body carrying a field that has been removed looks
# exactly like a body that worked. The fixture is the only thing in a position to notice.
#
# THERE IS NO NETWORK CALL HERE. `scripts/ops/reap_spec_diff.py` fetches; these tests read the
# pinned file. A unit test that reached docs.reap.global would fail in CI for reasons that have
# nothing to do with this code, and a test that fails when a partner edits their docs is a test
# people learn to ignore.

SPEC_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                            "reap_openapi_agentic_2026_09_17.json")


def _spec():
    with open(SPEC_FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _request_schema(spec, path, method="post"):
    op = spec["paths"][path][method]
    return op["requestBody"]["content"]["application/json"]["schema"]


def _branches(schema):
    return schema["oneOf"] if "oneOf" in schema else [schema]


def _check_against(schema, body, *, label):
    """Required keys present, and no key outside `properties`. Deliberately not a full JSON
    Schema validator: those two properties are what our builders can get wrong, and a dependency
    that validated `format: uri` would test jsonschema rather than this module."""
    for branch in _branches(schema):
        properties = set(branch.get("properties") or {})
        required = set(branch.get("required") or ())
        if not required.issubset(set(body)):
            continue
        if set(body) - properties:
            continue
        # Recurse into the object-valued properties we actually emit.
        ok = True
        for key, value in body.items():
            sub = branch["properties"].get(key) or {}
            if isinstance(value, dict) and (sub.get("type") == "object" or "properties" in sub):
                sub_required = set(sub.get("required") or ())
                sub_properties = set(sub.get("properties") or {})
                if not sub_required.issubset(set(value)) or (set(value) - sub_properties):
                    ok = False
        if ok:
            return
    raise AssertionError(f"{label}: no branch of the pinned schema accepts {sorted(body)}")


def test_the_enrollment_body_validates_against_the_pinned_spec(clean_env):
    spec = _spec()
    schema = _request_schema(spec, "/agentic/enrollments")
    for body in (
        rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL),
        rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL,
                                    email="buyer@example.com"),
    ):
        _check_against(schema, body, label="enrollment")


def test_the_checkout_body_validates_against_the_pinned_spec(clean_env):
    spec = _spec()
    schema = _request_schema(spec, "/agentic/checkouts")
    body = rc.build_checkout_request(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                                     return_url=RETURN_URL)
    _check_against(schema, body, label="checkout")


def test_the_quote_body_validates_against_the_pinned_spec():
    spec = _spec()
    schema = _request_schema(spec, "/agentic/quotes")
    body = rc.build_quote_request(items=[{"variantId": "var_x", "quantity": 1}],
                                  email="b@example.com", shipping_address=GOOD_ADDRESS)
    _check_against(schema, body, label="quote")


def test_the_shipping_option_body_validates_against_the_pinned_spec():
    spec = _spec()
    schema = _request_schema(spec, "/agentic/quotes/{id}/shipping-option")
    _check_against(schema, rc.build_shipping_option_request("ship_std"), label="shipping-option")


def test_the_checker_actually_rejects_a_wrong_body():
    """THE CONTROL for the four tests above. `_check_against` returning None unconditionally
    would pass all of them, and an absence assertion passes when the mechanism is absent too."""
    schema = _request_schema(_spec(), "/agentic/checkouts")
    with pytest.raises(AssertionError):
        _check_against(schema, {"quoteId": "q"}, label="missing required")
    with pytest.raises(AssertionError):
        _check_against(schema, {"quoteId": "q", "enrollmentId": ENROLLMENT_UUID,
                                "presentation": {"type": "REDIRECT", "returnUrl": RETURN_URL},
                                "owner": {"type": "CLIENT_REFERENCE", "id": "x"}},
                       label="the field that was removed")


def test_the_pinned_spec_still_says_owner_is_gone_from_checkout_creation():
    """The fact this whole package was rebuilt around. If a future re-pin brings `owner` back,
    this fails and somebody reads the diff instead of discovering it in production."""
    schema = _request_schema(_spec(), "/agentic/checkouts")
    assert set(schema["required"]) == {"quoteId", "enrollmentId", "presentation"}
    assert "owner" not in (schema.get("properties") or {})


def test_the_pinned_spec_still_requires_an_idempotency_key_on_the_three_creates():
    spec = _spec()
    for path in ("/agentic/quotes", "/agentic/checkouts", "/agentic/enrollments"):
        names = {p["name"] for p in spec["paths"][path]["post"].get("parameters", [])
                 if p.get("required")}
        assert "Idempotency-Key" in names, path
        assert path in rc._IDEMPOTENT_PATHS


def test_the_pinned_spec_still_requires_an_owner_id_on_the_enrollment_list():
    spec = _spec()
    params = {p["name"]: p for p in spec["paths"]["/agentic/enrollments"]["get"]["parameters"]}
    assert params["ownerId"]["required"] is True


def test_every_server_the_spec_names_passes_our_host_allowlist():
    """The `servers` block has GROWN since this client was written -- `sg.sandbox.api` and
    `sg.prod.api` are new -- and the allowlist has to still admit every one of them, or an
    operator pointing at a real Reap host gets a ReapConfigError."""
    for server in _spec()["servers"]:
        assert rc.validate_base_url(server["url"]) == server["url"]


#: Every schema path in the pinned fixture that can carry a URL, and what guards it. Derived by
#: walking the fixture for `format: uri` and for string properties whose name ends in `url`.
#:
#: A RATCHET, not documentation. The test below re-derives the set from the fixture and compares:
#: a re-pin that introduces a url-bearing field nobody classified FAILS, which is the only way a
#: new inbound URL gets a decision rather than a default. The categories are deliberately coarse
#: and the uncomfortable one is `UNGUARDED` — naming it is the point.
URL_PATHS_AND_GUARDS = {
    # Inbound hosted pages: a buyer types a card into these. `_refuse_unsafe_hosted_url` on the
    # call, `hosted_action` on the read, `hosted_url_is_allowed` underneath both.
    "POST /agentic/enrollments RESPONSE nextAction.url": "create_enrollment",
    "GET /agentic/enrollments/{id} RESPONSE nextAction.url": "get_enrollment",
    "GET /agentic/enrollments RESPONSE items[].nextAction.url": "list_enrollments",
    "POST /agentic/checkouts RESPONSE nextAction.url": "create_checkout",
    "GET /agentic/checkouts/{id} RESPONSE nextAction.url": "get_checkout",
    # Outbound, and OURS: validated before egress.
    "POST /agentic/enrollments REQUEST presentation.returnUrl": "validate_return_url",
    "POST /agentic/checkouts REQUEST presentation.returnUrl": "validate_return_url",
    # Endpoints this module does not call at all. The mandates rail and enrollment revocation
    # are the card-ISSUANCE side, dormant by design; no code path here can reach them.
    "GET /agentic/mandates/{id} RESPONSE nextAction.url": "NOT CALLED",
    "GET /agentic/mandates/{id} RESPONSE merchant.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/pause RESPONSE nextAction.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/pause RESPONSE merchant.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/resume RESPONSE nextAction.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/resume RESPONSE merchant.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/cancel RESPONSE nextAction.url": "NOT CALLED",
    "POST /agentic/mandates/{id}/cancel RESPONSE merchant.url": "NOT CALLED",
    "POST /agentic/enrollments/{id}/revoke RESPONSE nextAction.url": "NOT CALLED",
    # Inbound MEDIA urls, and these are UNGUARDED today. They are not card-entry pages, so the
    # hosted-URL rule does not apply to them -- but they are partner-controlled strings that a
    # UI would put in an `img src`, and nothing in this module vets them. Out of scope for WP1
    # and written down here rather than left to be discovered: whoever renders a product image
    # owns this, and this line is the handover.
    "POST /agentic/products/search RESPONSE products.imageUrl": "UNGUARDED (media)",
    "POST /agentic/products/details RESPONSE products.media.url": "UNGUARDED (media)",
    "POST /agentic/products/details RESPONSE products.defaultVariant.media.url":
        "UNGUARDED (media)",
    "POST /agentic/products/variant RESPONSE media.url": "UNGUARDED (media)",
}


def _url_bearing_paths(spec):
    """Re-derive the url-bearing fields from the fixture, as field names rather than positions."""
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("format") == "uri" or (
                    node.get("type") == "string"
                    and path.rsplit(".", 1)[-1].lower().endswith("url")):
                found.add(path)
            for key, value in node.items():
                if key in ("properties", "items", "oneOf", "anyOf", "allOf"):
                    walk(value, path)
                elif key not in ("description", "example", "title", "format", "pattern"):
                    walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for value in node:
                walk(value, path)

    for path, item in spec["paths"].items():
        for method, op in item.items():
            body = (((op.get("requestBody") or {}).get("content") or {})
                    .get("application/json") or {}).get("schema")
            if body:
                walk(body, f"{method.upper()} {path} REQUEST")
            for resp in (op.get("responses") or {}).values():
                schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
                if schema:
                    walk(schema, f"{method.upper()} {path} RESPONSE")
    return found


def test_every_url_bearing_field_in_the_pinned_spec_has_been_classified():
    """THE RATCHET. Reap moved a required field once without saying so; the next thing they move
    could be a new URL we hand a buyer. Counting them is not enough — this compares the SET, so
    a re-pin that adds one fails here and somebody has to decide what guards it."""
    def field_of(entry):
        """`"POST /x RESPONSE nextAction.url"` and `"POST /x RESPONSE.nextAction.url"` -> the
        same key. The table is written for a human; the walker emits dotted paths."""
        for marker in (" RESPONSE", " REQUEST"):
            if marker in entry:
                head, _, tail = entry.partition(marker)
                return f"{head}{marker} {tail.lstrip(' .').replace('items[].', '')}"
        return entry

    derived = {field_of(d) for d in _url_bearing_paths(_spec())}
    declared = {field_of(k) for k in URL_PATHS_AND_GUARDS}
    unclassified = derived - declared
    assert not unclassified, (
        f"url-bearing schema fields with no entry in URL_PATHS_AND_GUARDS: {sorted(unclassified)}"
    )


def test_the_url_ratchet_actually_bites():
    """THE CONTROL. A derivation that quietly returned nothing would make the ratchet above pass
    forever — which is the failure mode of every "assert not unexpected" test, and the one this
    file's own notes warn about: an absence assertion passes when the mechanism is absent too."""
    spec = json.loads(json.dumps(_spec()))
    schema = spec["paths"]["/agentic/checkouts"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    schema["properties"]["receiptUrl"] = {"type": "string", "format": "uri"}
    derived = _url_bearing_paths(spec)
    assert any("receiptUrl" in d for d in derived)
    # And it really is absent from the pinned one, so the ratchet passing is meaningful.
    assert not any("receiptUrl" in d for d in _url_bearing_paths(_spec()))


def test_every_inbound_hosted_url_path_is_guarded_by_a_call_that_exists():
    """The five guarded entries must name real functions. A table that drifts from the module is
    worse than no table: it reads as assurance."""
    for path, guard in URL_PATHS_AND_GUARDS.items():
        if guard in ("NOT CALLED",) or guard.startswith("UNGUARDED"):
            continue
        assert callable(getattr(rc, guard, None)), f"{path} names a guard that does not exist"


def test_the_mandates_rail_really_is_uncalled():
    """The `NOT CALLED` rows above, checked rather than asserted. `mandates` is the card-issuance
    side and nothing in this module may reach it — that is the 6 Sep constraint, not a scoping
    choice, so a new caller should have to delete this test on purpose."""
    import inspect
    source = inspect.getsource(rc)
    assert "/agentic/mandates" not in source
    assert "/revoke" not in source


def test_the_pinned_spec_covers_every_agentic_path_this_module_calls():
    """A path we call that is not in the pin is a path the diff script would never check."""
    pinned = set(_spec()["paths"])
    for path in ("/agentic/enrollments", "/agentic/enrollments/{id}", "/agentic/checkouts",
                 "/agentic/checkouts/{id}", "/agentic/quotes", "/agentic/quotes/{id}",
                 "/agentic/quotes/{id}/shipping-option", "/agentic/products/search",
                 "/agentic/products/details", "/agentic/products/variant"):
        assert path in pinned, path


def test_the_diff_script_reports_a_removed_required_field():
    """The script's own guard, on a synthetic change rather than the network. If `owner` came
    back tomorrow, this is the shape of the line an operator would see."""
    import importlib.util

    spec_diff_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "scripts", "ops", "reap_spec_diff.py")
    spec = importlib.util.spec_from_file_location("reap_spec_diff", spec_diff_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pinned = _spec()
    live = json.loads(json.dumps(pinned))
    schema = live["paths"]["/agentic/checkouts"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["required"] = ["quoteId", "presentation"]
    schema["properties"].pop("enrollmentId")

    problems = module.diff(pinned, live)
    assert any("REQUIRED FIELD REMOVED" in p and "enrollmentId" in p for p in problems)
    # THE CONTROL: an unchanged spec must diff clean, or "it reported a problem" means nothing.
    assert module.diff(pinned, json.loads(json.dumps(pinned))) == []


# ==============================================================================================
# REVIEW ROUND 1: the findings, each with the test that would have caught it
# ==============================================================================================


# --- P0: the idempotency key was a double-charge edge -------------------------------------------
#
# The buckets are WALL-CLOCK ALIGNED, not relative to the first attempt. Measured on the code as
# it shipped: t=239999 and t=240002 -- three seconds apart -- produced different keys for an
# identical (quoteId, enrollmentId). A retry after an unknown outcome that happens to straddle
# an edge creates a SECOND CHECKOUT on the same quote, which is the failure the key exists to
# prevent, reintroduced by the key itself.

BUCKET = rc._IDEMPOTENCY_BUCKET_S


def _keys(wire):
    return [c["headers"].get("Idempotency-Key") for c in wire.calls]


def _create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID, return_url=RETURN_URL):
    return _run(rc.create_checkout(quote_id=quote_id, enrollment_id=enrollment_id,
                                   return_url=return_url))


def test_a_checkout_key_does_not_change_across_a_bucket_edge(wire, clean_env, monkeypatch):
    """THE P0, at the transport. Three seconds apart, either side of a wall-clock bucket
    boundary. If these differ, Reap sees two distinct requests and creates two checkouts against
    one quote — and the buyer's card is charged twice."""
    wire.next_payload = CHECKOUT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: float(BUCKET) - 1.0)
    _create_checkout()
    monkeypatch.setattr(rc.time, "time", lambda: float(BUCKET) + 2.0)
    _create_checkout()
    assert _keys(wire)[0] == _keys(wire)[1]


@pytest.mark.parametrize("later", [1.0, BUCKET, BUCKET * 7, 24 * 3600 - 1])
def test_a_checkout_key_is_stable_for_the_whole_partner_retention_window(
        wire, clean_env, monkeypatch, later):
    """Reap retains a key for 24 h, and a replay returning the SAME checkout is exactly what we
    want for the whole of it. There is nothing to protect against on the far side: a quote is
    single-use and expires in about five minutes, so a replayed key cannot return a checkout
    against a live quote."""
    wire.next_payload = CHECKOUT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _create_checkout()
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0 + later)
    _create_checkout()
    assert _keys(wire)[0] == _keys(wire)[1]


def test_the_checkout_key_has_no_time_component_at_all(clean_env):
    """Directly, not through the transport: the same material at two arbitrary clocks."""
    material = {"quoteId": "q", "enrollmentId": ENROLLMENT_UUID}
    assert rc.idempotency_key("checkouts", material, now=1.0, bucket_seconds=None) == \
           rc.idempotency_key("checkouts", material, now=9e9, bucket_seconds=None)


@pytest.mark.parametrize("field,value", [
    ("quote_id", "SOMEOTHERQUOTE"),
    ("enrollment_id", OTHER_UUID),
])
def test_a_different_checkout_gets_a_different_key(wire, clean_env, monkeypatch, field, value):
    """THE CONTROL for the three above. A key that had become a constant would satisfy every
    stability assertion and collapse two different purchases into one."""
    wire.next_payload = CHECKOUT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _create_checkout()
    _create_checkout(**{field: value})
    assert _keys(wire)[0] != _keys(wire)[1]


def test_the_click_id_on_our_return_url_is_not_in_the_checkout_key(wire, clean_env, monkeypatch):
    """The other half of the same failure: our returnUrl carries a click id, so a retry arrives
    with a different query string. Body-derived material would hash differently."""
    wire.next_payload = CHECKOUT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _create_checkout(return_url="https://agent.pivota.cc/r?click=FIRST")
    _create_checkout(return_url="https://agent.pivota.cc/r?click=SECOND")
    assert _keys(wire)[0] == _keys(wire)[1]
    # THE CONTROL: the two returnUrls really did differ in the bodies that were sent.
    assert wire.calls[0]["body"]["presentation"]["returnUrl"] != \
           wire.calls[1]["body"]["presentation"]["returnUrl"]


# --- the enrollment key: unbucketed, but NOT keyed on the owner alone -----------------------------

def test_an_enrollment_key_is_stable_across_a_bucket_edge_for_one_attempt(
        wire, clean_env, monkeypatch):
    wire.next_payload = ENROLLMENT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: float(BUCKET) - 1.0)
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id=ATTEMPT_ID))
    monkeypatch.setattr(rc.time, "time", lambda: float(BUCKET) + 2.0)
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id=ATTEMPT_ID))
    assert _keys(wire)[0] == _keys(wire)[1]


def test_a_new_attempt_id_gets_a_new_enrollment(wire, clean_env, monkeypatch):
    """Why the owner alone is not enough, and why this is not symmetric with the checkout case.

    Reap retains a key for 24 hours; a hosted enrollment link expires in about fifteen minutes.
    Keying on the owner alone would replay the same DEAD enrollment — with its expired
    `nextAction.url` — to every later attempt by that buyer for the rest of the day, and there
    would be no way to hand them a working link."""
    wire.next_payload = ENROLLMENT_CREATED
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id="attempt-1"))
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id="attempt-2"))
    assert _keys(wire)[0] != _keys(wire)[1]


def test_an_enrollment_cannot_be_created_without_an_attempt_id(wire, clean_env):
    with pytest.raises(TypeError):
        _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL))
    assert wire.calls == []


@pytest.mark.parametrize("bad", ["", "  ", "has space", "a/b", "x" * 65, "buyer@example.com"])
def test_an_attempt_id_is_validated_as_an_opaque_id(wire, clean_env, bad):
    """It reaches a partner inside a header, so it must not be an email or anything else that
    identifies a person, and it must not be unbounded."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id=bad))
    assert wire.calls == []


def test_the_attempt_id_is_not_sent_in_the_body(wire, clean_env):
    """It is idempotency material, not a field. The schema has no place for it, and Reap drops
    unknown keys silently — so sending it would look exactly like it worked."""
    wire.next_payload = ENROLLMENT_CREATED
    _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL, attempt_id=ATTEMPT_ID))
    assert set(wire.calls[0]["body"]) == {"source", "owner", "presentation"}
    assert ATTEMPT_ID not in json.dumps(wire.calls[0]["body"])
    # THE CONTROL: it really did reach the key.
    assert wire.calls[0]["headers"]["Idempotency-Key"]


def test_a_quote_key_is_still_time_bucketed(wire, clean_env, monkeypatch):
    """The bucket was the right answer FOR QUOTES and stays. A quote's `expiresAt` is ~5 minutes
    against a 24 h retention, so the same cart tomorrow must not replay a long-dead quote."""
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0)
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    monkeypatch.setattr(rc.time, "time", lambda: 1_000_000.0 + 24 * 3600)
    _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}], email="b@example.com"))
    assert _keys(wire)[0] != _keys(wire)[1]


def test_only_the_quote_path_is_bucketed(clean_env):
    """Pinned as a set, so adding a create without deciding which side it is on is a failing
    test rather than a default."""
    assert set(rc._UNBUCKETED_IDEMPOTENT_PATHS) == {"/agentic/checkouts", "/agentic/enrollments"}
    assert set(rc._IDEMPOTENT_PATHS) - set(rc._UNBUCKETED_IDEMPOTENT_PATHS) == {"/agentic/quotes"}


# --- P1: list_enrollments was a FIFTH nextAction site, and it was unguarded ------------------------

LIST_WITH_POISONED_ITEM = {
    "items": [
        {"id": ENROLLMENT_UUID, "status": "ACTIVE", "owner": {"type": "CLIENT_REFERENCE",
                                                              "id": "cust_42"},
         "nextAction": None},
        {"id": OTHER_UUID, "status": "REQUIRES_ACTION",
         "owner": {"type": "CLIENT_REFERENCE", "id": "cust_42"},
         "nextAction": {"type": "REDIRECT", "url": "https://evil.example/collect-card"}},
    ],
    "nextCursor": None,
}


def test_a_poisoned_item_in_a_LIST_refuses_the_whole_response(wire, clean_env):
    """The pinned spec gives every `items[]` element its own `nextAction.url`, and this call was
    the one path that never went through the guard: the response came back ok=True with the URL
    intact in `.data`. A guard applied to four of five sites is not a guard."""
    wire.next_payload = LIST_WITH_POISONED_ITEM
    got = _run(rc.list_enrollments(owner_id="cust_42"))
    assert not got.ok and got.error == "hosted_url_not_allowed"
    assert got.data == {}
    assert "evil.example" not in json.dumps(got.data)


def test_a_clean_list_still_comes_through(wire, clean_env):
    """THE CONTROL: a guard that refused every list would pass the test above and break polling."""
    clean = json.loads(json.dumps(LIST_WITH_POISONED_ITEM))
    clean["items"][1]["nextAction"]["url"] = "https://pay.prava.space/enroll/x"
    wire.next_payload = clean
    got = _run(rc.list_enrollments(owner_id="cust_42"))
    assert got.ok and len(got.data["items"]) == 2


@pytest.mark.parametrize("action", [
    [{"type": "REDIRECT", "url": "https://pay.prava.space/x"}],   # a LIST of actions
    "https://pay.prava.space/x",                                   # a bare string
    123,
])
def test_a_next_action_that_is_not_an_object_is_refused(wire, clean_env, action):
    """The old check read `isinstance(action, dict) and not allowed(...)`, which means "vet it if
    it is a dict" and therefore "pass it on if it is not" — backwards for an untrusted value. A
    list of actions sailed straight through."""
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"] = action
    wire.next_payload = payload
    got = _run(rc.create_enrollment(owner_id="cust_42", return_url=RETURN_URL,
                                    attempt_id=ATTEMPT_ID))
    assert not got.ok and got.error == "hosted_url_not_allowed"


def test_a_null_next_action_is_not_a_violation(wire, clean_env):
    """THE CONTROL for the parametrisation above. `nextAction: null` is the normal shape of an
    ACTIVE enrollment, and refusing it would refuse every successful poll."""
    wire.next_payload = ENROLLMENT_ACTIVE
    assert _run(rc.get_enrollment(ENROLLMENT_UUID)).ok


def test_every_wrapped_call_refuses_a_poisoned_action(wire, clean_env):
    """All five sites, in one place, so adding a sixth without wrapping it is visible."""
    poisoned = json.loads(json.dumps(ENROLLMENT_CREATED))
    poisoned["nextAction"]["url"] = "https://evil.example/x"
    for call in (
        lambda: rc.create_enrollment(owner_id="c", return_url=RETURN_URL, attempt_id=ATTEMPT_ID),
        lambda: rc.get_enrollment(ENROLLMENT_UUID),
        lambda: rc.list_enrollments(owner_id="c"),
        lambda: rc.create_checkout(quote_id="q", enrollment_id=ENROLLMENT_UUID,
                                   return_url=RETURN_URL),
        lambda: rc.get_checkout("chk_7f3a"),
    ):
        wire.next_payload = poisoned
        assert _run(call()).error == "hosted_url_not_allowed"


# --- P1: a sanitised parse validating a raw string ------------------------------------------------
#
# `urlsplit` STRIPS tab, CR and LF out of the components it returns. A validator that parses and
# then approves the RAW string has therefore checked a different string from the one it hands on.
# Measured: `https://agent.pivota.cc/r?cid=1\r\nX: y` parsed to a clean host, passed every check,
# and came back with the CRLF still in it.

CRLF_RETURN_URL = "https://agent.pivota.cc/r?cid=1\r\nX: y"
CRLF_HOSTED_URL = "https://sandbox.collect.prava.space/x\r\nX: y"


@pytest.mark.parametrize("bad", [
    CRLF_RETURN_URL,
    "https://agent.pivota.cc/r\nX: y",
    "https://agent.pivota.cc/r\tx",
    "https://agent.pivota.cc/r x",
    "https://agent.pivota.cc/\x00r",
    "https://agent.pivota.cc/r\x7f",
])
def test_a_return_url_containing_a_control_character_is_refused(bad, clean_env):
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.validate_return_url(bad)
    assert "control characters" in str(exc.value)


@pytest.mark.parametrize("bad", [CRLF_HOSTED_URL, "https://pay.prava.space/x\ny",
                                 "https://pay.prava.space/x\tz", "https://pay.prava.space/a b"])
def test_a_hosted_url_containing_a_control_character_is_rejected(bad):
    assert rc.hosted_url_is_allowed(bad) is False


@pytest.mark.parametrize("good", [
    "https://agent.pivota.cc/r",
    "https://agent.pivota.cc/r?click=abc&x=1",
    "https://agent.pivota.cc/deep/path?a=1#frag",
    # An UPPERCASE SCHEME is the discriminator. Schemes are case-insensitive and `urlparse`
    # lowercases the one it reports, so this passes every check -- but `parsed.geturl()`
    # reassembles it lowercased and is therefore a DIFFERENT STRING from the input. Without this
    # case the reviewer's `return parsed.geturl()` mutant is indistinguishable from the real
    # thing for every URL the suite uses, and it survived 369 tests on exactly that.
    "HTTPS://agent.pivota.cc/r?click=abc",
])
def test_what_is_validated_is_byte_for_byte_what_is_returned(good, clean_env):
    """The invariant the CRLF finding was really about: we must not validate one string and hand
    on another. Rejecting control characters is what makes it achievable; this is what pins it."""
    assert rc.validate_return_url(good) == good


def test_a_crlf_hosted_url_never_reaches_a_buyer(wire, clean_env):
    """End to end, at the seam that matters: `hosted_action` is what an operator prints and a
    caller redirects to."""
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"]["url"] = CRLF_HOSTED_URL
    assert rc.hosted_action(payload) is None
    wire.next_payload = payload
    got = _run(rc.create_enrollment(owner_id="c", return_url=RETURN_URL, attempt_id=ATTEMPT_ID))
    assert not got.ok and got.error == "hosted_url_not_allowed"


# --- P2: nextAction.type, and the port ------------------------------------------------------------

@pytest.mark.parametrize("action", [
    {"url": "https://pay.prava.space/x"},                              # no type at all
    {"type": "REVEAL_PAN", "url": "https://pay.prava.space/x"},        # the issuance surface
    {"type": "redirect", "url": "https://pay.prava.space/x"},          # not the enum spelling
    {"type": None, "url": "https://pay.prava.space/x"},
])
def test_hosted_action_refuses_anything_that_is_not_a_REDIRECT(action):
    """The partner's ISSUANCE rail has a reveal-PAN surface. The caller's next move with this
    value is to show it to a buyer as a link, and "it had a url in it" is not a reason to."""
    assert rc.hosted_action({"nextAction": action}) is None


def test_hosted_action_still_returns_a_real_redirect():
    """THE CONTROL."""
    assert rc.hosted_action(ENROLLMENT_CREATED) == (
        "https://pay.prava.space/enroll/3fa85f64", "2026-09-17T21:00:00Z")


def test_a_non_redirect_action_refuses_the_whole_response(wire, clean_env):
    payload = json.loads(json.dumps(ENROLLMENT_CREATED))
    payload["nextAction"]["type"] = "REVEAL_PAN"
    wire.next_payload = payload
    got = _run(rc.create_enrollment(owner_id="c", return_url=RETURN_URL, attempt_id=ATTEMPT_ID))
    assert not got.ok and got.error == "hosted_url_not_allowed"


@pytest.mark.parametrize("url,ok", [
    ("https://pay.prava.space/x", True),
    ("https://pay.prava.space:443/x", True),
    ("https://pay.prava.space:8443/x", False),
    ("https://pay.prava.space:22/x", False),
    ("https://pay.prava.space:0/x", False),
    ("https://pay.prava.space:notanumber/x", False),
])
def test_a_hosted_url_is_pinned_to_the_default_port(url, ok):
    """An explicit odd port on a host that otherwise looks right is the shape of a URL that wants
    to reach something else on that machine."""
    assert rc.hosted_url_is_allowed(url) is ok


# --- the small ones -------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [" COMPLETED", "COMPLETED ", "completed", "Completed",
                                    "\tCOMPLETED"])
def test_a_status_that_is_not_the_exact_enum_value_is_unknown(status):
    """PINNED, in the safe direction. The partner has only ever sent exact uppercase values, so a
    near-miss is not a spelling to absorb — it is a signal that something upstream is not what we
    think it is. `unknown` is non-terminal: it keeps a poller polling and tells a human, where
    normalising would have resolved an unexpected payload straight to COMPLETED."""
    assert rc.checkout_state({"status": status}) == "unknown"
    assert rc.checkout_is_terminal({"status": status}) is False


def test_the_exact_enum_value_still_maps():
    """THE CONTROL for the strictness above."""
    assert rc.checkout_state({"status": "COMPLETED"}) == "completed"
    assert rc.enrollment_state({"status": "ACTIVE"}) == "active"


@pytest.mark.parametrize("limit", ["abc", object(), [1]])
def test_a_non_numeric_list_limit_is_a_ReapRequestError(wire, clean_env, limit):
    """It used to escape as a bare ValueError while every other caller error in this module is a
    ReapRequestError — so a caller catching the module's own exception type missed it."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.list_enrollments(owner_id="cust_42", limit=limit))
    assert wire.calls == []


@pytest.mark.parametrize("value", ["ABCDE\n", "AB\nCD", "ENROLLMENT_NOT_ACTIVE\n"])
def test_an_error_code_with_a_trailing_newline_is_not_a_code(wire, clean_env, value):
    """`match` plus `$` accepts a trailing newline, so a partner value could carry one into a log
    line. `fullmatch` is the whole-string comparison this always meant."""
    wire.next_status = 400
    wire.next_payload = {"error": {"code": value, "detail": {"code": value}}}
    got = _create_checkout()
    assert got.error_code is None and got.error_detail_code is None


def test_the_id_patterns_do_not_accept_a_trailing_newline():
    """`match` plus `$` accepts one; `fullmatch` does not. Asserted on the patterns directly,
    because `_path_id` strips surrounding whitespace BEFORE validating — so for ids the newline
    never reaches the regex, and the property that matters there is the one below: what is
    validated is what is sent."""
    assert rc._OPAQUE_ID_RE.fullmatch("chk_7f3a\n") is None
    assert rc._UUID_RE.fullmatch(ENROLLMENT_UUID + "\n") is None
    assert rc._ERROR_CODE_RE.fullmatch("ABCDE\n") is None
    # THE CONTROL: the same values without the newline do match.
    assert rc._OPAQUE_ID_RE.fullmatch("chk_7f3a")
    assert rc._UUID_RE.fullmatch(ENROLLMENT_UUID)
    assert rc._ERROR_CODE_RE.fullmatch("ABCDE")


def test_an_id_is_normalised_before_it_is_validated_and_used(wire, clean_env):
    """`_path_id` strips, then validates, then returns the STRIPPED value — so the string that
    passed the check is the string that goes in the URL. That is the same invariant as
    `validate_return_url`'s, reached the other way: URLs refuse whitespace because stripping
    cannot fix an interior control character, ids strip because the whitespace can only be
    surrounding."""
    wire.next_payload = CHECKOUT_COMPLETED
    _run(rc.get_checkout("  chk_7f3a\n"))
    assert wire.calls[0]["url"].endswith("/agentic/checkouts/chk_7f3a")
    assert rc.build_shipping_option_request(" ship_std ") == {"shippingOptionId": "ship_std"}


@pytest.mark.parametrize("bad", ["chk\n7f3a", "chk 7f3a", "chk\t7f3a"])
def test_an_id_with_an_INTERIOR_control_character_is_refused(bad, clean_env):
    """Stripping cannot save this one, and it is the case that could alter a request line."""
    with pytest.raises(rc.ReapRequestError):
        _run(rc.get_checkout(bad))


@pytest.mark.parametrize("bad", ["cust 42", "cust\t42", "cust\r\n42", "cust\x0042"])
def test_an_owner_id_with_whitespace_or_controls_is_refused(bad, clean_env):
    """It travels in a query string on the list endpoint and inside an Idempotency-Key header —
    two places a raw control character has no business being."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(owner_id=bad, return_url=RETURN_URL)
    assert "whitespace or controls" in str(exc.value)


def test_an_ordinary_owner_id_is_still_accepted(clean_env):
    """THE CONTROL."""
    body = rc.build_enrollment_request(owner_id="cust_42-abc", return_url=RETURN_URL)
    assert body["owner"]["id"] == "cust_42-abc"


@pytest.mark.parametrize("owner_id", [{"a": 1}, ["x"], 42, object()])
def test_a_non_string_owner_id_is_refused_not_stringified(clean_env, owner_id):
    """`str({"a": 1})` sent `"{'a': 1}"` to a partner as a customer identifier — a
    plausible-looking id that will never join back to anything."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(owner_id=owner_id, return_url=RETURN_URL)
    assert "must be a string" in str(exc.value)


@pytest.mark.parametrize("email", ["a@b@c", "@b.com", "a@", "a b@c.com",
                                   "a@b", "a@b.com\r\nX: y", "nope"])
def test_an_email_that_is_not_an_address_is_refused(clean_env, email):
    """`"@" in address` fired on nothing a caller is likely to get wrong — the reviewer's mutant
    that deleted the check survived every test. This is not RFC validation and does not try to
    be; it is the set of shapes that are definitely not an address, on a value that is REAL BUYER
    PII being prefilled onto a third party's page."""
    with pytest.raises(rc.ReapRequestError) as exc:
        rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL, email=email)
    assert "not an email address" in str(exc.value)


@pytest.mark.parametrize("email", ["a@b.com", "buyer+tag@sub.example.co.uk", "x.y@z.io"])
def test_a_real_email_is_still_accepted(clean_env, email):
    """THE CONTROL: a check that refused everything would pass the test above and break every
    enrollment that prefills an address."""
    body = rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL, email=email)
    assert body["owner"]["email"] == email


def test_surrounding_whitespace_on_an_email_is_stripped_not_refused(clean_env):
    """Same rule as the ids: strip, then validate, then send the stripped value. Interior
    whitespace is a different thing and is refused above — this only forgives the shape a copy
    and paste produces."""
    body = rc.build_enrollment_request(owner_id="cust_42", return_url=RETURN_URL,
                                       email="  a@b.com  ")
    assert body["owner"]["email"] == "a@b.com"


def test_a_get_wraps_a_bare_string_warning_exactly_as_post_does(wire, clean_env):
    """`_post` wraps it; `_get` dropped it. Two verbs disagreeing about the same partner field is
    how one of them silently stops reporting a signal the other reports — and the signal here is
    MERCHANT_NOT_FOUND."""
    wire.next_payload = {"items": [], "nextCursor": None, "warnings": "MERCHANT_NOT_FOUND"}
    got = _run(rc.list_enrollments(owner_id="cust_42"))
    assert got.warnings == ["MERCHANT_NOT_FOUND"]


# --- the operator script's FAILURE paths ------------------------------------------------------------
#
# `_report_failure` called `explain_refusal(..., detail_code=...)`, which takes no such argument,
# so EVERY failure path in the script raised TypeError -- and the failure paths are exactly the
# ones nobody exercises until something has already gone wrong. Dry-run coverage said nothing
# about them.

def _purchase_steps():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "ops", "reap_purchase_steps.py")
    spec = importlib.util.spec_from_file_location("reap_purchase_steps", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("result", [
    rc.ReapResponse(ok=False, status=400, error="reap_status_400",
                    error_code="AGENTIC_REQUEST_REJECTED",
                    error_detail_code="ENROLLMENT_NOT_ACTIVE"),
    rc.ReapResponse(ok=False, status=404, error="reap_status_404",
                    error_code="AGENTIC_RESOURCE_NOT_FOUND"),
    rc.ReapResponse(ok=False, error="transport_error:ReadTimeout"),
    rc.ReapResponse(ok=False, error="hosted_url_not_allowed"),
    rc.ReapResponse(ok=False, error="something_reap_invented"),
])
def test_the_operator_script_can_report_every_failure_shape(capsys, result):
    _purchase_steps()._report_failure(rc, result)
    out = capsys.readouterr().out
    assert result.error in out
    # Printed as text, not one character per line — the other half of the bug.
    assert "\n  A\n" not in out


def test_the_operator_script_prints_what_to_do_about_ENROLLMENT_NOT_ACTIVE(capsys):
    """The one detail code that tells an operator what to DO, and the reason
    `explain_detail_code` exists. It had no caller outside tests."""
    _purchase_steps()._report_failure(rc, rc.ReapResponse(
        ok=False, status=400, error="reap_status_400",
        error_code="AGENTIC_REQUEST_REJECTED", error_detail_code="ENROLLMENT_NOT_ACTIVE"))
    assert "hosted card page" in capsys.readouterr().out


def test_the_operator_script_never_prints_a_response_body(capsys):
    """It has no body to print — but the assertion is cheap and this is the script an operator
    runs while something is going wrong."""
    _purchase_steps()._report_failure(rc, rc.ReapResponse(
        ok=False, status=400, error="reap_status_400", data={"shippingAddress": GOOD_ADDRESS}))
    assert "Brannan" not in capsys.readouterr().out


def test_the_operator_script_writes_its_state_file_owner_only(tmp_path):
    """It holds a buyer's enrollment and checkout ids and our client reference for them, and it
    is written by an operator on a shared box at whatever umask that box happens to have."""
    target = tmp_path / "nested" / "state.json"
    _purchase_steps()._save(str(target), {"enrollment_id": ENROLLMENT_UUID})
    assert (target.stat().st_mode & 0o777) == 0o600


def test_the_operator_script_CREATES_the_state_file_already_locked_down(tmp_path, monkeypatch):
    """Asserted on the `os.open` MODE, not on the resulting file, and that is deliberate.

    The final mode is also produced by the `chmod` on the next line, so a file-mode assertion
    cannot tell the two apart — the reviewer's mutant that opened 0644 survived, because the
    chmod put it right a microsecond later. What that microsecond is, though, is a window in
    which the file exists world-readable, and the only place that window is observable is the
    call itself. Same reasoning as asserting `follow_redirects` at the constructor."""
    module = _purchase_steps()
    seen = []
    real_open = os.open
    monkeypatch.setattr(module.os, "open",
                        lambda path, flags, mode=0o777: (seen.append(mode),
                                                         real_open(path, flags, mode))[1])
    module._save(str(tmp_path / "state.json"), {"a": 1})
    assert seen == [0o600]


def test_the_operator_script_tightens_an_existing_loose_state_file(tmp_path):
    """THE CONTROL for the mode above: creating with 0600 does nothing if the file already
    exists, and `O_CREAT` does not re-apply the mode to one that does."""
    target = tmp_path / "state.json"
    target.write_text("{}")
    target.chmod(0o644)
    _purchase_steps()._save(str(target), {"a": 1})
    assert (target.stat().st_mode & 0o777) == 0o600


# --- the spec-diff script's branch pairing ------------------------------------------------------

def test_an_inserted_oneOf_branch_does_not_misalign_every_later_branch():
    """`zip` paired the before/after required-sets BY POSITION, so inserting one branch — exactly
    what a partner does when they add an enrollment source — shifted everything after it and
    reported a required-field change on each, burying the one real difference."""
    module = _spec_diff()
    pinned = _spec()
    live = json.loads(json.dumps(pinned))
    schema = live["paths"]["/agentic/enrollments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["oneOf"].insert(0, {"type": "object", "properties": {"source": {"type": "string"}},
                               "required": ["source"]})
    problems = module.diff(pinned, live)
    added = [p for p in problems if "schema branch ADDED" in p or "REQUIRED FIELD" in p]
    assert added, problems
    # The real point: it does NOT claim every pre-existing branch changed.
    assert sum("REQUIRED FIELD REMOVED" in p for p in problems) <= 1, problems


def _spec_diff():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "ops", "reap_spec_diff.py")
    spec = importlib.util.spec_from_file_location("reap_spec_diff_2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the network guard itself ---------------------------------------------------------------------

def test_an_unpatched_httpx_client_cannot_be_constructed():
    """A probe of this module once forgot to patch the transport and sent five read-only GETs to
    sandbox.api.reap.global with a dummy key. No PII and no real credential, so no harm — but the
    only thing between that and a POST is which line the author forgot. The autouse fixture makes
    the mistake loud."""
    import httpx
    with pytest.raises(AssertionError) as exc:
        httpx.AsyncClient()
    assert "would have reached the network" in str(exc.value)


def test_the_guard_does_not_block_the_recorder(wire, clean_env):
    """THE CONTROL: `wire` replaces the guard, so everything that fakes the transport still
    works. A guard that broke those would have been reverted within a day."""
    wire.next_payload = CHECKOUT_COMPLETED
    assert _run(rc.get_checkout("chk_7f3a")).ok


# ==============================================================================================
# REBASE ONTO THE STREAMING BASE: `_get` adopts `_read_bounded`
# ==============================================================================================
#
# The base rewrote `_post` to stream, because `client.post` does not return until the whole body
# is already in memory -- so a size check after it declines to PARSE a body it has fully
# allocated, which is the part that costs. `_get` is the module's other egress path and gets the
# same treatment through the SAME helper, not a second copy of the bound.


def test_a_get_STREAMS_rather_than_buffering(wire, clean_env):
    """The recorder only implements `stream`, so a `_get` that called `client.get` would fail
    loudly — but assert it explicitly, because "the fake happens not to have that method" is an
    accident of the fake and not a property of the module."""
    wire.next_payload = CHECKOUT_COMPLETED
    _run(rc.get_checkout("chk_7f3a"))
    assert wire.calls[0]["method"] == "GET"
    assert wire.last_response.bytes_yielded == len(wire.last_response.content)


def test_a_get_stops_reading_AT_the_cap_rather_than_finishing_first(wire, clean_env):
    """THE DIFFERENCE BETWEEN A REAL BOUND AND A COSMETIC ONE, on the GET path.

    The body arrives in chunks and lies about its length: `content-length` says it is small, so
    the cheap check passes and the read starts. What stops it is the cumulative count, and
    `bytes_yielded` is how we tell "refused after reading all of it" from "refused partway".
    A mutant that deletes the running total leaves this test reading the whole 3 MB."""
    oversized = b"z" * (rc.MAX_RESPONSE_BYTES + 512 * 1024)
    wire.next_headers = {"content-length": "37"}          # a lie, and a small one
    wire.next_content = oversized
    wire.next_chunk_size = 64 * 1024
    got = _run(rc.get_checkout("chk_7f3a"))
    assert not got.ok and got.error == "response_too_large"
    assert got.data == {}
    # Stopped at the cap, not at the end of the body.
    assert wire.last_response.bytes_yielded <= rc.MAX_RESPONSE_BYTES + rc._READ_CHUNK_BYTES
    assert wire.last_response.bytes_yielded < len(oversized)
    # And in bounded STEPS. `_read_bounded` now passes an explicit `chunk_size`, because httpx
    # decides the step otherwise and a decompressed body can arrive in one enormous piece -- a
    # cap checked between chunks does nothing if there is only one chunk. The base pins this for
    # the POST path; the GET path is a second caller of the same helper and gets the same check.
    assert wire.last_response.requested_chunk_size == rc._READ_CHUNK_BYTES
    assert wire.last_response.largest_chunk <= rc._READ_CHUNK_BYTES


def test_a_get_still_refuses_on_an_honest_declared_length(wire, clean_env):
    """The cheap check is still worth having: it refuses before a single byte of body is read."""
    wire.next_headers = {"content-length": str(rc.MAX_RESPONSE_BYTES + 1)}
    wire.next_payload = CHECKOUT_COMPLETED
    got = _run(rc.get_checkout("chk_7f3a"))
    assert not got.ok and got.error == "response_too_large"
    assert wire.last_response.bytes_yielded == 0


def test_both_verbs_share_one_bounded_reader(wire, clean_env):
    """Not two copies of the rule. `_read_bounded` is tested as a unit by the base; this pins
    that both egress paths actually route through it, which is the claim that matters."""
    import inspect
    source = inspect.getsource(rc._get)
    assert "_read_bounded" in source
    assert "client.stream" in source
    # And no second implementation of the bound crept back in alongside it.
    assert "aiter_bytes" not in source


# --- the 4xx body: read on two legs, never touched anywhere else ---------------------------------
#
# A REAL DISAGREEMENT BETWEEN TWO GOOD RULES, and this is where it is settled. The base's rule is
# that a failure body is never pulled off the socket, so a partner payload echoing a buyer's
# address cannot enter the process. WP1 needs the opposite on two legs, because
# `ENROLLMENT_NOT_ACTIVE` versus `AGENTIC_RESOURCE_NOT_FOUND` is the difference between "send the
# buyer back to the card page" and "start again", and `reap_status_400` leaves a buyer stuck.
#
# Scoping by path gets both, and it lands where the risk is: the request body that can carry a
# SHIPPING ADDRESS is the quote, and that is exactly the one still never read.

def test_a_quote_failure_body_is_still_never_read(wire, clean_env):
    """The base's property, unchanged, on the endpoint whose request carries an address."""
    wire.next_status = 422
    wire.next_content = json.dumps({"error": {"code": "VALIDATION_FAILED"},
                                    "echo": {"shippingAddress": GOOD_ADDRESS}}).encode()
    got = _run(rc.request_quote(items=[{"variantId": "var_x", "quantity": 1}],
                                email="b@example.com"))
    assert not got.ok and got.status == 422 and got.data == {}
    assert wire.last_response.bytes_yielded == 0
    # Not even the code, on this path. That is the trade, stated out loud.
    assert got.error_code is None


@pytest.mark.parametrize("path_call", [
    lambda: rc.create_checkout(quote_id="f1e2d3c4", enrollment_id=ENROLLMENT_UUID,
                               return_url=RETURN_URL),
    lambda: rc.get_checkout("chk_7f3a"),
    lambda: rc.create_enrollment(owner_id="c", return_url=RETURN_URL, attempt_id=ATTEMPT_ID),
    lambda: rc.get_enrollment(ENROLLMENT_UUID),
    lambda: rc.list_enrollments(owner_id="c"),
])
def test_the_enrollment_and_checkout_legs_DO_carry_their_codes(wire, clean_env, path_call):
    """The other half of the trade, on every leg that has a state machine to drive."""
    wire.next_status = 400
    wire.next_content = json.dumps(CHECKOUT_REJECTED_BODY).encode()
    got = _run(path_call())
    assert got.error_code == "AGENTIC_REQUEST_REJECTED"
    assert got.error_detail_code == "ENROLLMENT_NOT_ACTIVE"
    # And still nothing else from that body, which echoes an address.
    flat = json.dumps({"data": got.data, "error": got.error})
    assert "Brannan" not in flat


def test_the_product_endpoints_never_read_a_failure_body(wire, clean_env):
    """`search`, `details` and `variant` have no state machine and no code a caller acts on."""
    for call in (lambda: rc.search_products(query="x"),
                 lambda: rc.product_details(["prd_x"]),
                 lambda: rc.resolve_variant(product_id="prd_x", option_ids=["opt_x"])):
        wire.next_status = 400
        wire.next_content = json.dumps(CHECKOUT_REJECTED_BODY).encode()
        got = _run(call())
        assert got.error_code is None and got.error_detail_code is None
        assert wire.last_response.bytes_yielded == 0


def test_which_paths_read_a_failure_body_is_pinned():
    """A set, not a habit: adding an endpoint without deciding which side of this line it is on
    should be a failing test rather than a default."""
    assert set(rc._ERROR_CODE_PATH_PREFIXES) == {"/agentic/enrollments", "/agentic/checkouts"}
    for path in ("/agentic/enrollments", "/agentic/enrollments/abc", "/agentic/checkouts",
                 "/agentic/checkouts/abc"):
        assert rc._reads_error_codes(path)
    for path in ("/agentic/quotes", "/agentic/quotes/abc", "/agentic/products/search",
                 "/agentic/products/details", "/agentic/products/variant",
                 "/agentic/quotes/abc/shipping-option"):
        assert not rc._reads_error_codes(path)


def test_an_oversized_failure_body_yields_no_codes_even_where_we_do_read(wire, clean_env):
    """One bound per response, and it applies to failure bodies too. We would rather lose a code
    than read an unbounded body to find one."""
    wire.next_status = 400
    # Comfortably over the cap rather than a byte over it: the read stops at a CHUNK boundary,
    # so a body that fits in the chunk straddling the cap is legitimately read in full. Sizing
    # the fixture to the guard's real granularity is the difference between testing the bound
    # and testing the chunk size.
    wire.next_content = b"{" + b"z" * (rc.MAX_RESPONSE_BYTES + 512 * 1024)
    wire.next_chunk_size = 64 * 1024
    got = _run(rc.get_checkout("chk_7f3a"))
    assert got.error_code is None and got.error_detail_code is None
    assert wire.last_response.bytes_yielded < len(wire.last_response.content)
    # THE CONTROL: the same call under the cap does produce codes.
    wire.next_content = json.dumps(CHECKOUT_REJECTED_BODY).encode()
    assert _run(rc.get_checkout("chk_7f3a")).error_detail_code == "ENROLLMENT_NOT_ACTIVE"


@pytest.mark.parametrize("raw", [None, b"", b"not json", b'"a string"', b'{"error": "nope"}'])
def test_the_error_code_reader_has_no_codes_without_bytes(raw):
    """The contract, pinned by test rather than by mutant.

    `_error_codes(None)` is the over-cap case and must yield nothing. It happens that deleting
    the explicit `if not raw` changes no behaviour — the `except` below catches the resulting
    AttributeError and returns the same pair — so a mutant on that line is EQUIVALENT and would
    survive any test. Asserting the contract directly is what actually holds it, whichever line
    ends up implementing it."""
    assert rc._error_codes(raw) == (None, None)


def test_the_bounded_reader_closes_its_iterator_on_the_early_return(wire, clean_env):
    """The leak the base hit and fixed: abandoning a suspended async generator leaves its cleanup
    to the garbage collector, and under asyncio that surfaces as "Task was destroyed but it is
    pending". `_get` must not reintroduce it — the whole point of this path is that it stops
    reading, so it is the path that abandons a generator mid-iteration."""
    closed = []

    class _WatchedResponse(_FakeResponse):
        def aiter_bytes(self, chunk_size=None):
            # `chunk_size` is forwarded, not swallowed: `_read_bounded` now passes an explicit
            # one, and a fake that ignored it would let a mutant deleting that argument survive
            # here even though the base has its own test for it.
            outer = super().aiter_bytes(chunk_size=chunk_size)

            class _Watched:
                async def __anext__(self):
                    return await outer.__anext__()

                def __aiter__(self):
                    return self

                async def aclose(self):
                    closed.append(True)
                    await outer.aclose()

            return _Watched()

    response = _WatchedResponse(200, content=b"z" * 5000, chunk_size=64)
    assert _run(rc._read_bounded(response, max_bytes=100)) is None
    assert closed == [True], "the iterator was abandoned rather than closed"


# --- the operator scripts after the exact-label change ---------------------------------------------
#
# The base replaced the fuzzy sole-label rule with EXACT matching plus explicit aliases, so a row
# whose Reap label carries a merchant suffix now REFUSES (`options:sole_label_differs`) instead of
# being guessed at. That is the right call -- guessing is what bought the wrong bottle -- but it
# means the probe script needs a way to supply the alias, or the refusal is a dead end.

import sys as _sys


def _resolve_script():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "ops", "reap_resolve_and_quote.py")
    spec = importlib.util.spec_from_file_location("reap_resolve_and_quote", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_resolve_script_can_supply_variant_label_aliases(monkeypatch):
    """`--alias` is repeatable and reaches `resolve_our_row` as `accept_variant_labels`. Without
    it there is no way to act on a `sole_label_differs` refusal from the command line."""
    seen = {}

    async def fake_resolve(**kwargs):
        seen.update(kwargs)
        return rc.VariantResolution(ok=False, reason="search:merchant_not_in_results")

    module = _resolve_script()
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.setattr(rc, "resolve_our_row", fake_resolve)
    monkeypatch.setattr(_sys, "argv", ["prog", "--alias", "Flamingo Flirt - Cream",
                                       "--alias", "Flamingo Flirt Cream"])
    module.main()
    assert seen["accept_variant_labels"] == ("Flamingo Flirt - Cream", "Flamingo Flirt Cream")
    # THE CONTROL: with no flag it passes an empty tuple rather than omitting the argument, so
    # the module's default and the script's default cannot drift apart unnoticed.
    seen.clear()
    monkeypatch.setattr(_sys, "argv", ["prog"])
    module.main()
    assert seen["accept_variant_labels"] == ()


def test_a_sole_label_refusal_prints_the_alias_to_re_run_with(monkeypatch, capsys):
    """The refusal carries Reap's label in `candidates`, and that label is the exact string to
    pass back. Printing the flag spelled out is the difference between a dead end and a loop an
    operator can close in one more run."""
    async def fake_resolve(**kwargs):
        return rc.VariantResolution(
            ok=False, reason="options:sole_label_differs:Shade",
            candidates=[{"axis": "Shade", "label": "Flamingo Flirt - Cream"}])

    module = _resolve_script()
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.setattr(rc, "resolve_our_row", fake_resolve)
    monkeypatch.setattr(_sys, "argv", ["prog"])
    module.main()
    out = capsys.readouterr().out
    assert "--alias 'Flamingo Flirt - Cream'" in out
    # And it says to check they are the same physical thing first: the whole reason exact
    # matching replaced the fuzzy rule is that a confident guess bought the wrong bottle.
    assert "same physical thing" in out


def test_the_purchase_steps_script_has_no_alias_option_because_it_resolves_nothing():
    """Deliberately ABSENT, not overlooked. `reap_purchase_steps.py` starts from a `--quote-id`
    that a previous run produced; it never calls `resolve_our_row`, so an `--alias` there would
    be a flag that silently does nothing -- which is worse than not having one."""
    import inspect
    source = inspect.getsource(_purchase_steps())
    assert "resolve_our_row" not in source
    assert "accept_variant_labels" not in source
