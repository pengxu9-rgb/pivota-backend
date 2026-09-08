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
    correct. Resolving the variant first makes the prices agree to the cent."""
    _chain(monkeypatch, details=FENTY_DETAILS_STANDARD_AVAILABLE)
    got = _run(rc.resolve_our_row(
        merchant_domain="fentybeauty.com", product_name="Fenty Eau de Parfum",
        variant_title="Standard", our_price=140.00,
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


# --- a single-value axis is determined ----------------------------------------------------------
#
# Measured on flowerbeauty.com "Petal Pout Lip Color": Reap indexes the per-shade page as its own
# product, with ONE `Shade` axis carrying ONE value, `"Flamingo Flirt - Cream"`. Our row's title
# is "Flamingo Flirt" — no finish suffix — so exact label matching refuses a row that has exactly
# one possible answer. Refusing there protects nothing.

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
    got = rc.select_option_ids(FLOWER_DETAILS["products"][0],
                               rc.variant_title_tokens("Flamingo Flirt"))
    assert got.ok and got.option_ids == ["opt_flamingo"]
    # `chosen` records REAP's label, not our title — which is what makes the substitution check
    # downstream compare against the thing we actually asked Reap for.
    assert got.chosen == {"Shade": "Flamingo Flirt - Cream"}


def test_a_single_value_axis_needs_no_title_at_all():
    got = rc.select_option_ids(FLOWER_DETAILS["products"][0], ["anything"])
    assert got.ok and got.option_ids == ["opt_flamingo"]


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
        variant_title="Flamingo Flirt", our_price=8.00,
    ))
    assert got.ok and got.variant_id == "var_flamingo"
    assert got.price == (8.0, "USD") and got.price_disagrees is False
    assert fake.variant_body == {"productId": "prd_petalpout", "optionIds": ["opt_flamingo"]}


def test_the_substitution_guard_compares_against_reaps_label_not_our_title(monkeypatch):
    """Because we send Reap's optionId, the response echoes Reap's label. Comparing it against
    OUR title would refuse every correctly-resolved single-value axis — the guard has to check
    what we asked for, which is the label, not what our catalog happens to call it."""
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
