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
    product["options"].append({"name": "Color", "values": [
        {"optionId": "opt_rose", "label": "Rose", "available": True},
    ]})
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard"))
    assert not got.ok and got.unmatched_axes == ["Color"]
    # It reports the axis it DID match, so a human can see how far it got.
    assert got.chosen == {"Size": "Standard"}


def test_a_shopify_style_two_axis_title_resolves_both():
    product = json.loads(json.dumps(FENTY_DETAILS["products"][0]))
    product["options"].append({"name": "Color", "values": [
        {"optionId": "opt_rose", "label": "Rose", "available": True},
    ]})
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard / Rose"))
    assert got.ok and got.option_ids == ["opt_standard", "opt_rose"]


def test_an_unavailable_option_is_still_selected_and_merely_reported():
    """Standard is `available: false` at Reap while the merchant's own storefront still lists it.
    One third-party negative is not a delisting, so this must not silently substitute an
    available sibling -- that substitution is exactly the $95-for-$140 error."""
    product = FENTY_DETAILS["products"][0]
    got = rc.select_option_ids(product, rc.variant_title_tokens("Standard"))
    assert got.ok and got.option_ids == ["opt_standard"]


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
            return rc.ReapResponse(ok=True, status=200, data=FENTY_DETAILS)
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
def test_a_host_we_cannot_justify_is_refused_before_the_key_is_attached(url):
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
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Recorder.calls.append({"url": url, "body": json, "headers": headers,
                                "timeout": self.timeout})
        payload = _Recorder.next_payload
        return _FakeResponse(_Recorder.next_status, payload)


@pytest.fixture
def wire(monkeypatch):
    import httpx
    _Recorder.calls = []
    _Recorder.next_status = 200
    _Recorder.next_payload = {}
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
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


def test_the_idempotency_key_is_stable_for_the_same_cart(wire):
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
    fake = _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
    ))
    assert got.ok
    assert got.variant_id == "var_standard140"
    assert got.price == (140.0, "USD")
    assert got.matched_options == {"Size": "Standard"}
    # It asked for the option we matched, not for a default.
    assert fake.variant_body == {"productId": "prd_dfe0ceb2123640d496edcd7c21b94c0b",
                                 "optionIds": ["opt_standard"]}


def test_our_140_row_does_not_disagree_with_reaps_140_standard(monkeypatch):
    """The whole point of the rebuild's mapping rule. Naively comparing our row to what Reap
    SHOWS (`previewVariant` $95.00, availability-ordered) reads as 32% staleness on a row that is
    correct. Resolving the variant first makes the prices agree to the cent."""
    _chain(monkeypatch)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
    ))
    assert got.ok and got.price_disagrees is False
    assert got.available is False  # Reap's view of that size. An observation, not a delisting.


def test_a_genuine_price_disagreement_is_reported_and_not_acted_on(monkeypatch):
    _chain(monkeypatch)
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
    _chain(monkeypatch, variant=MINI_SUBSTITUTED)
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


def test_a_product_call_is_given_the_short_timeout(wire):
    """Same assertion from the other side: the long bound must not leak onto the fast endpoints,
    where a 35 s hang would sit in a serving path."""
    _run(rc.search_products(query="x"))
    assert wire.calls[0]["timeout"] == 12.0


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
