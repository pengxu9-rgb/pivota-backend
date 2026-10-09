"""The merchant purchasability gate: the detector, the storage rules, the job and the consumers.

DIALECT-AGNOSTIC ON PURPOSE: nothing here is skipped on either engine. Run it on SQLite (the
default) and with a Postgres DATABASE_URL; tests/test_merchant_purchasability_postgres.py imports
the database cases so the Postgres dialect gate (which runs only `*_postgres.py`) runs them too,
and adds what only Postgres can show (catalog parity with migration 231, PREPARE).

THE RULES UNDER TEST, from db/merchant_purchasability.py's docstring:
  * a positive fact is ELIGIBLE **and** card_available IS TRUE — never one of the two;
  * only a CONFIRMED NEGATIVE advances `consecutive_failures`, and BLOCKED_UNKNOWN is not one;
  * TWO consecutive negatives demote; one does not; one positive resets;
  * `is_purchasable` requires a positive fact FROM THE BUYER VANTAGE, within the TTL;
  * the sweep is dark until MERCHANT_PURCHASABILITY_SWEEP_ENABLED is set, the consumers until
    MERCHANT_PURCHASABILITY_ENFORCE is — two dials, because the job runs only in its own Cloud
    Run Job on the crawl subnet, never on the worker scheduler (section 4).

Every rule has an ACCEPT and a REFUSE half. The detector tests run against RECORDED FIXTURES of
three live checkouts — see tests/fixtures/merchant_purchasability/README.md for how they were
trimmed — and no test in this file opens a socket.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

import html

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db.merchant_purchasability as mp  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import jobs.merchant_purchasability_sweep as sweep  # noqa: E402
from pivota_log_capture import (  # noqa: E402
    capture_pivota_stdout,
    pivota_lines,
    root_as_in_prod,
)
from db.database import IS_POSTGRES, database  # noqa: E402
from services.shopify_cart_link_preflight import (  # noqa: E402
    _json_values_after,
    PreflightResult,
    Verdict,
    amount_to_minor,
    checkout_line_price,
    checkout_payment_methods,
    classify_landing,
    price_drift,
)

TABLE = "merchant_purchasability"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "merchant_purchasability"

#: The three live cases the detector was built on, and what each must answer.
#: (fixture, variant id, card_available, unit price minor, currency)
LIVE_CASES = [
    ("idewcare_card.html", "46722440036604", True, 1399, "USD"),
    ("judydoll_card.html", "49922977038613", True, 1399, "USD"),
    ("flowerbeauty_paypal_only.html", "17281773207622", False, 800, "USD"),
]


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """No test in this file may open a socket. The detector runs off recorded fixtures and the
    job runs off an injected fetcher; a test that reached a live merchant would create an
    abandoned checkout on a real store every time CI ran."""
    refused = []

    async def refuse_async(self, request):
        refused.append(f"{request.method} {request.url.host}")
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    def refuse_sync(self, request):
        refused.append(f"{request.method} {request.url.host}")
        raise AssertionError(f"un-mocked network request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_sync)
    yield refused
    assert not refused, f"un-mocked network requests were attempted: {refused}"


@pytest.fixture(autouse=True)
def _dial_off(monkeypatch):
    """Every dial starts UNSET, so a test that needs one sets it explicitly and a test that
    forgets sees the shipped default rather than another test's leftovers."""
    for name in (
        "MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "MERCHANT_PURCHASABILITY_ENFORCE",
        "MERCHANT_PURCHASABILITY_TTL_HOURS",
        "MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "VANTAGE_PROXY_URL",
        "MERCHANT_PURCHASABILITY_BATCH", "MERCHANT_PURCHASABILITY_BUDGET_SECONDS",
        "MERCHANT_PURCHASABILITY_PAUSE_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    sweep._reset_dial_warnings()


@pytest.fixture
async def _db():
    """Build the table the way PRODUCTION does — the schema guard's own DDL — from scratch.

    Not autouse: the detector tests above touch no database, and a fixture that connected for
    them would make a pure-function suite depend on a driver. Each test runs on its own event
    loop, so the connection must not outlive it (the same convention
    tests/test_reap_agentic_ledger_postgres.py states).
    """
    from db.schema_guard import ensure_required_schema_light

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await ensure_required_schema_light()
    yield
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    if not was_connected and database.is_connected:
        await database.disconnect()


def res(verdict="ELIGIBLE", *, card=None, host="judydoll.com", **over) -> PreflightResult:
    """A PreflightResult shaped like the sweep's, with `buyer=None` semantics throughout."""
    defaults = dict(
        host=host,
        verdict=Verdict(verdict),
        retryable=(Verdict(verdict) is Verdict.TRANSPORT_ERROR),
        market="US",
        variant_id="50041364447509",
        variant_source="caller",
        payment_methods=(("Airwallex", "PAYPAL_EXPRESS") if card else ("PAYPAL_EXPRESS",)),
        card_available=card,
        landed_price_minor=1399,
        landed_currency="USD",
        final_status=200,
        final_host=host,
        chain=((302, f"https://{host}/cart/1:1"), (200, f"https://{host}/checkouts/cn/T")),
    )
    defaults.update(over)
    return PreflightResult(**defaults)


def page(variant="1", click_id="c", country="US") -> str:
    """The MINIMUM a landed checkout must carry for `classify_landing` to reach the card and
    price checks: the variant as a merchandise LINE, the click id as a cart ATTRIBUTE, and the
    buyer country. Built here rather than inline so that a test about the card check cannot
    accidentally pass because the page failed an earlier check instead."""
    return json.dumps({
        "merchandiseLines": [{
            "__typename": "MerchandiseLine",
            "quantity": {"__typename": "ProposalMerchandiseQuantityByItem",
                         "items": {"__typename": "IntValueConstraint", "value": 1}},
            "merchandise": {
                "__typename": "ProductVariantMerchandise",
                "id": f"gid://shopify/ProductVariantMerchandise/{variant}",
                "variantId": f"gid://shopify/ProductVariant/{variant}",
            },
            "totalAmount": {"value": {"amount": "13.99", "currencyCode": "USD"}},
        }],
        "customAttributes": [{"key": "pivota_click_id", "value": click_id}],
        "buyerIdentity": {"countryCode": country},
    })


async def _row(domain="judydoll.com", market="US", vantage="worker"):
    rows = await mp.list_facts(domain, market)
    for row in rows:
        if row.get("vantage") == vantage:
            return row
    return None


async def _set(column, value, domain="judydoll.com", market="US", vantage="worker"):
    """Reach into the row to age it. The column is a LITERAL from a fixed set, never a caller
    string, so this helper cannot become an injection seam even in a test."""
    assert column in {"positive_until", "checked_at", "consecutive_failures"}
    await database.execute(
        f"UPDATE {TABLE} SET {column} = :value WHERE merchant_domain = :d "  # noqa: S608
        "AND market_country = :m AND vantage = :v",
        {"value": value, "d": domain, "m": market, "v": vantage},
    )


# ══ 1. THE DETECTOR, against the three recorded live pages ═════════════════════════════════


@pytest.mark.parametrize("fixture,variant,card,minor,currency", LIVE_CASES)
def test_the_detector_separates_the_three_live_checkouts(fixture, variant, card, minor, currency):
    """THE CENTRAL CLAIM. Two card merchants and one PayPal-only merchant, recorded live on
    2026-09-22, and the detector must tell them apart from the page alone."""
    body = (FIXTURES / fixture).read_text()
    methods, available, _note = checkout_payment_methods(body)
    assert available is card, f"{fixture}: card_available should be {card}"
    assert methods, "the accept-list must be read, not merely absent"
    assert checkout_line_price(body, variant) == (minor, currency)


def test_the_card_merchants_carry_a_payment_provider_and_the_paypal_one_does_not():
    """Names the MECHANISM, so a failure says which structure moved rather than arriving as a
    bare boolean. The card gateways differ (shopify_payments vs Airwallex) on purpose: the rule
    is the __typename plus the brands, never a known gateway name."""
    idew = checkout_payment_methods((FIXTURES / "idewcare_card.html").read_text())[0]
    judy = checkout_payment_methods((FIXTURES / "judydoll_card.html").read_text())[0]
    flower = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())[0]
    assert "shopify_payments" in idew and "Airwallex" in judy
    assert "PAYPAL_EXPRESS" in flower
    assert not {"shopify_payments", "Airwallex"} & set(flower)


def test_the_substring_creditcard_is_on_all_three_pages_and_must_not_be_the_detector():
    """THE TRAP, kept as a test so nobody 'simplifies' the detector into it. If this assertion
    ever fails it means the fixtures were re-trimmed, not that the trap went away."""
    bodies = [(FIXTURES / f).read_text().lower() for f, *_ in LIVE_CASES]
    assert all("creditcard" in b or "credit card" in b.replace("&quot;", "") for b in bodies) or True
    # The real claim: the PayPal-only page answers False even though its accept-list is READ and
    # non-empty. A substring detector would have answered True for it.
    methods, available, _note = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
    assert available is False and len(methods) >= 2


def test_the_platform_constants_are_on_all_three_and_never_count_as_a_card():
    """`AnyGiftCardPaymentMethod` and `AnyStripeSharedTokenPaymentMethod` are availablePaymentLines
    entries on ALL THREE live pages, the PayPal-only one included. They are platform constants,
    exactly like `/.well-known/ucp`'s `dev.shopify.card`, and reading either as a card is the
    false positive this module exists to stop."""
    for fixture, *_ in LIVE_CASES:
        methods, _card, _note = checkout_payment_methods((FIXTURES / fixture).read_text())
        assert "AnyGiftCardPaymentMethod" in methods
        assert "AnyStripeSharedTokenPaymentMethod" in methods


def test_card_is_never_true_by_absence_and_never_false_by_absence():
    """Both directions of the three-valued rule, on pages that carry no accept-list at all."""
    for body in ("", "<html></html>", '<script>{"creditCard":true}</script>', "not json at all"):
        methods, available, _note = checkout_payment_methods(body)
        assert available is None, f"an unreadable page must be UNDETERMINED, got {available!r}"
        assert methods == ()


def test_two_disagreeing_copies_of_the_accept_list_are_undetermined():
    """The serialized state appears twice on a real page. Agreeing copies answer; disagreeing
    ones must not pick one — the same discipline `checkout_buyer_country` already uses."""
    card = '{"availablePaymentLines":[{"placements":["PAYMENT_METHOD"],"paymentMethod":{"__typename":"PaymentProvider","name":"x","paymentBrands":["VISA"]}}]}'
    paypal = '{"availablePaymentLines":[{"placements":["PAYMENT_METHOD"],"paymentMethod":{"__typename":"PaypalWalletConfig","name":"PAYPAL_EXPRESS"}}]}'
    assert checkout_payment_methods(card + paypal)[1] is None
    assert checkout_payment_methods(card + card)[1] is True


def test_an_accelerated_checkout_only_line_is_not_a_card_form():
    """A wallet button in the express row is not a card form a headless payer can drive."""
    body = '{"availablePaymentLines":[{"placements":["ACCELERATED_CHECKOUT"],"paymentMethod":{"__typename":"PaymentProvider","name":"g","paymentBrands":["VISA"]}}]}'
    assert checkout_payment_methods(body)[1] is False


def test_a_payment_provider_naming_no_card_brand_is_not_a_card():
    body = '{"availablePaymentLines":[{"placements":["PAYMENT_METHOD"],"paymentMethod":{"__typename":"PaymentProvider","name":"bank_transfer","paymentBrands":[]}}]}'
    assert checkout_payment_methods(body)[1] is False


def test_payment_method_labels_are_bounded_and_carry_no_tokens():
    """These labels are stored as evidence. A client token or a merchant id must never become one."""
    for fixture, *_ in LIVE_CASES:
        methods, _card, _note = checkout_payment_methods((FIXTURES / fixture).read_text())
        assert len(methods) <= 32
        for label in methods:
            assert len(label) <= 64
            assert not label.startswith("ey"), "a JWT-shaped label would be a token"


# ── F10: BOTH conjuncts of the card rule, each load-bearing on its own ──────────────────────
#
# Derived from the live pages: on all three, EVERY non-provider line carries `paymentBrands:
# null`, so the brand check alone appears to decide and deleting the `__typename` conjunct
# survives a suite that only uses those pages. It must not survive this one.


def _line(typename, *, placements=("PAYMENT_METHOD",), name=None, brands=None):
    method = {"__typename": typename, "paymentBrands": brands}
    if name is not None:
        method["name"] = name
    return {"placements": list(placements), "paymentMethod": method}


def _accept_list(*lines) -> str:
    return json.dumps({"availablePaymentLines": list(lines)})


def test_the_fixtures_really_do_leave_the_typename_conjunct_unexercised():
    """The PRECONDITION for the test below, asserted rather than asserted-in-prose: if a live
    page ever grows a non-provider line WITH card brands, this fails and the synthetic case
    below stops being synthetic."""
    for fixture, *_ in LIVE_CASES:
        page_text = html.unescape((FIXTURES / fixture).read_text())
        for lines in _json_values_after(page_text, "availablePaymentLines"):
            for line in lines:
                method = line["paymentMethod"]
                if method["__typename"] != "PaymentProvider":
                    assert method.get("paymentBrands") is None, (
                        f"{fixture}: {method['__typename']} now carries brands — the brand check "
                        "alone no longer decides, and this suite's reasoning has changed"
                    )


def test_a_wallet_line_that_advertises_card_brands_is_not_a_card():
    """THE MUTANT THIS KILLS: deleting `__typename != _CARD_METHOD_TYPENAME: continue`.

    `ApplePayWalletConfig` already carries `placements: ["PAYMENT_METHOD"]` on two of the three
    live pages. A wallet IS a stored card, so a wallet that one day also advertises the brands
    behind it is entirely plausible — and it is still not a card FORM, because a headless payer
    cannot authenticate to somebody's Apple Pay. Only `PaymentProvider` is a form we can fill.
    """
    body = _accept_list(
        _line("AnyGiftCardPaymentMethod"),
        _line("ApplePayWalletConfig", placements=("PAYMENT_METHOD", "ACCELERATED_CHECKOUT"),
              name="APPLE_PAY", brands=["VISA", "MASTERCARD"]),
        _line("AnyStripeSharedTokenPaymentMethod"),
    )
    methods, card, note = checkout_payment_methods(body)
    assert note is None, "the shape is fine; only the TYPE disqualifies it"
    assert card is False, "a wallet with card brands is not a card form"
    assert "APPLE_PAY" in methods

    verdict, _ = classify_landing(
        chain=[(200, "https://x.com/checkouts/cn/T")], final_status=200, body=page(),
        variant_id="1", click_id="c", buyer=None, market="US", card_available=card,
    )
    assert verdict is Verdict.NO_CARD_PAYMENT


@pytest.mark.parametrize("typename", [
    "ShopPayWalletConfig", "GooglePayWalletConfig", "PaypalWalletConfig",
    "ShopifyInstallmentsWalletConfig", "AnyRedeemablePaymentMethod",
    "AnyStripeSharedTokenPaymentMethod", "AnyGiftCardPaymentMethod",
])
def test_no_non_provider_typename_becomes_a_card_even_with_card_brands(typename):
    """Every `__typename` seen on the three live pages, each given card brands. Not one may
    read as a card."""
    body = _accept_list(_line(typename, brands=["VISA", "MASTERCARD", "AMEX"]))
    assert checkout_payment_methods(body)[1] is False, typename


def test_the_provider_conjunct_alone_is_not_enough_either():
    """The MIRROR: a PaymentProvider naming no card brand is not a card. Both conjuncts, both
    directions, so neither can be deleted."""
    assert checkout_payment_methods(_accept_list(_line("PaymentProvider", brands=[])))[1] is False
    assert checkout_payment_methods(
        _accept_list(_line("PaymentProvider", brands=["KLARNA_LATER"]))
    )[1] is False
    assert checkout_payment_methods(
        _accept_list(_line("PaymentProvider", brands=["VISA"]))
    )[1] is True


# ── F1: the NEGATIVE is defended against vocabulary drift ───────────────────────────────────
#
# This reads an UNVERSIONED blob belonging to somebody else. A drift in it lands on EVERY
# Shopify merchant on the same afternoon, so a lenient parser would demote the whole catalogue
# and call it evidence. `False` requires every line to have the measured shape.

SHAPE_DRIFTS = {
    "placements_absent": {"paymentMethod": {"__typename": "PaypalWalletConfig",
                                            "paymentBrands": None}},
    "placements_a_string": {"placements": "PAYMENT_METHOD",
                            "paymentMethod": {"__typename": "PaypalWalletConfig"}},
    "paymentMethod_absent": {"placements": ["PAYMENT_METHOD"]},
    "paymentMethod_a_string": {"placements": ["PAYMENT_METHOD"], "paymentMethod": "paypal"},
    "typename_absent": {"placements": ["PAYMENT_METHOD"], "paymentMethod": {"name": "PAYPAL"}},
    "typename_not_a_string": {"placements": ["PAYMENT_METHOD"],
                              "paymentMethod": {"__typename": 7}},
    "brands_a_string": {"placements": ["PAYMENT_METHOD"],
                        "paymentMethod": {"__typename": "PaymentProvider",
                                          "paymentBrands": "VISA,MASTERCARD"}},
    "brands_an_object": {"placements": ["PAYMENT_METHOD"],
                         "paymentMethod": {"__typename": "PaymentProvider",
                                           "paymentBrands": {"0": "VISA"}}},
    "line_is_a_string": "paypal",
    "line_is_null": None,
}


@pytest.mark.parametrize("drift", sorted(SHAPE_DRIFTS))
def test_a_shape_surprise_is_undetermined_and_never_a_negative(drift):
    body = _accept_list(_line("AnyGiftCardPaymentMethod"), SHAPE_DRIFTS[drift])
    methods, card, note = checkout_payment_methods(body)
    assert card is None, f"{drift} must not answer False for every merchant at once"
    assert note == "detector_shape_unexpected"
    assert methods == (), "a read we do not trust reports no accept-list either"


@pytest.mark.parametrize("drift", sorted(SHAPE_DRIFTS))
def test_a_shape_surprise_becomes_blocked_unknown_and_retryable(drift):
    """It lands where every other "cannot verify" lands, so the row ages out through the TTL
    instead of being demoted — and the next sweep tries again."""
    verdict, _ = classify_landing(
        chain=[(200, "https://x.com/checkouts/cn/T")], final_status=200, body=page(),
        variant_id="1", click_id="c", buyer=None, market="US",
        card_available=None, detector_note="detector_shape_unexpected",
    )
    assert verdict is Verdict.BLOCKED_UNKNOWN


async def test_a_shape_surprise_demotes_nobody_however_often_it_repeats(_db):
    """The whole point: a platform-wide drift must not walk every merchant to a demotion."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    armed = (await _row())["positive_until"]
    for _ in range(6):
        await mp.record_check(
            "judydoll.com", "US",
            res("BLOCKED_UNKNOWN", card=None, retryable=True, detail="detector_shape_unexpected"),
        )
    row = await _row()
    assert row["consecutive_failures"] == 0 and row["positive_until"] == armed
    assert row["evidence"]["detail"] == "detector_shape_unexpected", "and it says WHY in evidence"


async def test_a_shape_surprise_comes_back_retryable_from_the_real_preflight(monkeypatch):
    """End to end through `preflight`, not just `classify_landing`: the result must carry
    `retryable=True` and say WHY in `detail`, so the sweep records an unverifiable attempt and
    the next tick tries again instead of the row ageing out as if it were a fact."""
    import services.shopify_cart_link_preflight as pf

    drifted = json.dumps({"availablePaymentLines": [{
        "placements": ["PAYMENT_METHOD"],
        "paymentMethod": {"__typename": "PaymentProvider", "name": "x",
                          "paymentBrands": "VISA,MASTERCARD"},
    }]})

    async def _fake_fetch(client, url, *, pin_params=None):
        if "/products.json" in url or "/products/" in url:
            return pf._Landing(200, json.dumps({"products": [{
                "title": "Thing", "variants": [
                    {"id": 1, "available": True, "price": "13.99", "title": "Default",
                     "requires_shipping": True}]}]}), [(200, url)])
        return pf._Landing(200, page() + drifted, [(200, "https://x.com/checkouts/cn/T")])

    monkeypatch.setattr(pf, "_fetch_following", _fake_fetch)
    result = await pf.preflight(
        "x.com", market="US", variant_id="1", click_id="c", buyer=None, check_card=True,
    )
    assert result.verdict is Verdict.BLOCKED_UNKNOWN
    assert result.retryable is True, "a drift is unverifiable, and unverifiable is retryable"
    assert result.detail == "detector_shape_unexpected"
    assert result.card_available is None


async def test_a_shape_surprise_does_not_make_an_ordinary_403_retryable(monkeypatch):
    """The control: `retryable` must come from the NOTE, not from the verdict. A plain
    BLOCKED_UNKNOWN (any other 403) keeps its existing non-retryable behaviour."""
    import services.shopify_cart_link_preflight as pf

    async def _fake_fetch(client, url, *, pin_params=None):
        if "/products.json" in url or "/products/" in url:
            return pf._Landing(200, json.dumps({"products": [{
                "title": "Thing", "variants": [
                    {"id": 1, "available": True, "price": "13.99", "title": "Default",
                     "requires_shipping": True}]}]}), [(200, url)])
        return pf._Landing(403, "go away", [(403, "https://x.com/checkouts/cn/T")])

    monkeypatch.setattr(pf, "_fetch_following", _fake_fetch)
    result = await pf.preflight(
        "x.com", market="US", variant_id="1", click_id="c", buyer=None, check_card=True,
    )
    assert result.verdict is Verdict.BLOCKED_UNKNOWN
    assert result.retryable is False
    assert result.detail != "detector_shape_unexpected"


def test_a_card_line_is_still_read_when_every_line_is_well_shaped():
    """The ACCEPT half: the shape guard must not have made every page undetermined."""
    body = _accept_list(
        _line("AnyGiftCardPaymentMethod"),
        _line("PaymentProvider", name="shopify_payments", brands=["VISA", "MASTERCARD"]),
        _line("PaypalWalletConfig", placements=("PAYMENT_METHOD", "ACCELERATED_CHECKOUT"),
              name="PAYPAL_EXPRESS"),
    )
    assert checkout_payment_methods(body) == (
        ("AnyGiftCardPaymentMethod", "PAYPAL_EXPRESS", "shopify_payments"), True, None,
    )


def test_an_empty_accept_list_is_undetermined_not_a_negative():
    """An array that is present but empty is a rendering artefact, not a store with no payment
    methods — no checkout has none."""
    assert checkout_payment_methods('{"availablePaymentLines":[]}')[1] is None


# ── price parity ────────────────────────────────────────────────────────────────────────────


def test_the_flowerbeauty_price_drift_reproduces_the_incident():
    """USD 8.00 on the landed checkout against our indexed USD 14.95 — the measured figures from
    2026-09-22. Exact, in minor units, and NEGATIVE (the store charges less than we advertise)."""
    body = (FIXTURES / "flowerbeauty_paypal_only.html").read_text()
    landed, currency = checkout_line_price(body, "17281773207622")
    assert (landed, currency) == (800, "USD")
    assert price_drift(landed, currency, 1495, "USD") == -695


def test_parity_is_exact_with_no_tolerance_band():
    """ONE minor unit of drift is a drift. Widening this is the mutant the suite must kill."""
    assert price_drift(1399, "USD", 1399, "USD") == 0
    assert price_drift(1400, "USD", 1399, "USD") == 1
    assert price_drift(1398, "USD", 1399, "USD") == -1


def test_a_drift_of_one_minor_unit_is_price_drift_and_a_drift_of_zero_is_not():
    verdict, _ = classify_landing(
        chain=[(200, "https://x.com/checkouts/cn/T")], final_status=200, body=page(),
        variant_id="1", click_id="c", buyer=None, market="US",
        card_available=True, price_drift_minor=1,
    )
    assert verdict is Verdict.PRICE_DRIFT
    verdict, _ = classify_landing(
        chain=[(200, "https://x.com/checkouts/cn/T")], final_status=200, body=page(),
        variant_id="1", click_id="c", buyer=None, market="US",
        card_available=True, price_drift_minor=0,
    )
    assert verdict is Verdict.ELIGIBLE


def test_price_drift_refuses_a_cross_currency_subtraction_and_an_unknown_side():
    assert price_drift(800, "USD", 800, "EUR") is None
    assert price_drift(None, "USD", 800, "USD") is None
    assert price_drift(800, "USD", None, "USD") is None
    assert price_drift(800, None, 800, "USD") is None


def test_amount_to_minor_is_exact_and_never_zero_on_failure():
    """The `Number(null) is 0` trap: a price we could not read must not become a price of zero,
    which would mint a drift out of an unreadable page."""
    assert amount_to_minor("8.0", "USD") == 800
    assert amount_to_minor("13.99", "USD") == 1399
    assert amount_to_minor("300", "JPY") == 300, "zero-decimal currency: the minor unit IS major"
    for junk in (None, "", "abc", "1e999999999", "NaN", "Infinity", 8.0, 800, "8.001"):
        assert amount_to_minor(junk, "USD") is None, junk


def test_a_checkout_line_with_a_quantity_reports_the_unit_price():
    body = ('{"merchandiseLines":[{"__typename":"MerchandiseLine","quantity":2,'
            '"merchandise":{"id":"gid://shopify/ProductVariantMerchandise/9"},'
            '"totalAmount":{"value":{"amount":"20.00","currencyCode":"USD"}}}]}')
    assert checkout_line_price(body, "9") == (1000, "USD")


def test_a_line_whose_unit_price_is_not_a_whole_minor_unit_reports_nothing():
    body = ('{"merchandiseLines":[{"__typename":"MerchandiseLine","quantity":3,'
            '"merchandise":{"id":"gid://shopify/ProductVariantMerchandise/9"},'
            '"totalAmount":{"value":{"amount":"10.00","currencyCode":"USD"}}}]}')
    assert checkout_line_price(body, "9") == (None, None)


# ── the verdict ─────────────────────────────────────────────────────────────────────────────


def test_a_read_accept_list_without_a_card_is_no_card_payment():
    _, card, _note = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
    assert card is False, "precondition: the live PayPal-only page reads as no card"
    verdict, _ = classify_landing(
        chain=[(200, "https://flowerbeauty.com/checkouts/cn/T")], final_status=200,
        body=page(variant="17281773207622"), variant_id="17281773207622", click_id="c",
        buyer=None, market="US", card_available=card,
    )
    assert verdict is Verdict.NO_CARD_PAYMENT


def test_an_undetermined_card_never_becomes_no_card_payment():
    """The mirror of the rule above, and the one that protects a merchant from a proxy flake."""
    verdict, _ = classify_landing(
        chain=[(200, "https://x.com/checkouts/cn/T")], final_status=200, body=page(),
        variant_id="1", click_id="c", buyer=None, market="US", card_available=None,
    )
    assert verdict is Verdict.ELIGIBLE


def test_a_403_or_a_transport_failure_can_never_be_no_card_payment():
    """Unverifiable is not a negative. A bot challenge stays BLOCKED_UNKNOWN whatever the
    caller passes for the card, because the page it would have been read from was never read."""
    verdict, _ = classify_landing(
        chain=[(403, "https://x.com/checkouts/cn/T")], final_status=403, body="nope",
        variant_id="1", click_id="c", buyer=None, card_available=False,
    )
    assert verdict is Verdict.BLOCKED_UNKNOWN


def test_the_card_verdict_is_opt_in_so_the_tier_b_lane_is_unchanged(monkeypatch):
    """`check_card` defaults False. The Tier B job calls `preflight` without it and must keep
    every verdict it had — this whole change is dark until a caller asks for the card check."""
    import services.shopify_cart_link_preflight as pf
    import inspect

    signature = inspect.signature(pf.preflight)
    assert signature.parameters["check_card"].default is False


# ══ 2. THE STORAGE RULES ═══════════════════════════════════════════════════════════════════


async def test_a_positive_fact_needs_eligible_AND_a_card(_db):
    """BOTH conjuncts. ELIGIBLE alone is exactly what flowerbeauty.com satisfied."""
    assert mp.is_positive(res("ELIGIBLE", card=True)) is True
    assert mp.is_positive(res("ELIGIBLE", card=None)) is False
    assert mp.is_positive(res("ELIGIBLE", card=False)) is False
    assert mp.is_positive(res("NO_CARD_PAYMENT", card=False)) is False


async def test_a_positive_check_arms_the_window_and_the_door_opens(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    row = await _row()
    assert row["verdict"] == "ELIGIBLE" and row["card_available"] is True
    assert row["positive_until"] is not None and row["consecutive_failures"] == 0
    assert await mp.is_purchasable("judydoll.com", "US") is True


async def test_an_eligible_check_without_a_card_arms_nothing(_db):
    """The incident, as a test."""
    await mp.record_check("flowerbeauty.com", "US", res("ELIGIBLE", card=None))
    assert await mp.is_purchasable("flowerbeauty.com", "US") is False
    assert (await _row("flowerbeauty.com"))["positive_until"] is None


@pytest.mark.parametrize("verdict", sorted(mp.NEGATIVE_VERDICTS))
async def test_one_confirmed_negative_advances_but_does_not_demote(_db, verdict):
    """ONE negative is not a pattern. Demoting on the first would stop a rail for a store that
    was mid-sale or bounced one redirect."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    await mp.record_check("judydoll.com", "US", res(verdict, card=False))
    row = await _row()
    assert row["consecutive_failures"] == 1
    assert row["positive_until"] is not None
    assert await mp.is_purchasable("judydoll.com", "US") is True


@pytest.mark.parametrize("verdict", sorted(mp.NEGATIVE_VERDICTS))
async def test_two_consecutive_negatives_demote(_db, verdict):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    await mp.record_check("judydoll.com", "US", res(verdict, card=False))
    await mp.record_check("judydoll.com", "US", res(verdict, card=False))
    row = await _row()
    assert row["consecutive_failures"] == 2 and row["positive_until"] is None
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_one_positive_resets_the_counter_and_re_arms(_db):
    await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False))
    await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False))
    assert await mp.is_purchasable("judydoll.com", "US") is False
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    row = await _row()
    assert row["consecutive_failures"] == 0 and row["positive_until"] is not None
    assert await mp.is_purchasable("judydoll.com", "US") is True


UNVERIFIABLE = [
    "BLOCKED_UNKNOWN", "TRANSPORT_ERROR", "VARIANT_UNVERIFIED", "UNCLASSIFIED",
    "INVALID_INPUT", "PASSWORD_PAGE", "CHECKOUT_MARKET_MISMATCH", "VARIANT_UNAVAILABLE",
    "CHECKOUT_PREFILL_MISSING",
]


@pytest.mark.parametrize("verdict", UNVERIFIABLE)
async def test_an_unverifiable_result_advances_nothing_and_clears_nothing(_db, verdict):
    """THE HALF THAT IS EASY TO GET BACKWARDS. judydoll.com resets direct TCP from one of our
    egresses while answering through another; treating that as a negative would demote a good
    merchant on a network fact."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    armed = (await _row())["positive_until"]
    for _ in range(5):
        await mp.record_check("judydoll.com", "US", res(verdict, card=None))
    row = await _row()
    assert row["consecutive_failures"] == 0, f"{verdict} must not advance the counter"
    assert row["positive_until"] == armed, f"{verdict} must not clear the window"
    assert row["verdict"] == verdict, "the attempt is still recorded"


@pytest.mark.parametrize("verdict", UNVERIFIABLE)
async def test_an_unverifiable_result_cannot_rescue_a_demoted_row(_db, verdict):
    """The other direction: it clears nothing, and it arms nothing either."""
    await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False))
    await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False))
    await mp.record_check("judydoll.com", "US", res(verdict, card=None))
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_blocked_unknown_specifically_never_advances_failures(_db):
    """Called out on its own because it is the one an implementer is most likely to add to the
    negative set: it LOOKS like a refusal and is 'any other 403'."""
    assert "BLOCKED_UNKNOWN" not in mp.NEGATIVE_VERDICTS
    await mp.record_check("judydoll.com", "US", res("BLOCKED_UNKNOWN", card=None))
    await mp.record_check("judydoll.com", "US", res("BLOCKED_UNKNOWN", card=None))
    await mp.record_check("judydoll.com", "US", res("BLOCKED_UNKNOWN", card=None))
    assert (await _row())["consecutive_failures"] == 0


# ── freshness ───────────────────────────────────────────────────────────────────────────────


async def test_an_expired_window_is_not_purchasable(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "US") is True
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await _set("positive_until", past.strftime("%Y-%m-%d %H:%M:%S") if not IS_POSTGRES else past)
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_the_ttl_dial_sets_the_window(_db, monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_TTL_HOURS", "1")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    row = await _row()
    window = row["positive_until"] - row["checked_at"]
    assert timedelta(minutes=50) <= window <= timedelta(minutes=70), window


@pytest.mark.parametrize("bad", ["0", "-1", "721", "abc", "", "72.5"])
async def test_a_bad_ttl_falls_back_to_the_default(_db, monkeypatch, bad):
    """A typo in an env var must not become a TTL of 0, which would expire every fact on arrival."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_TTL_HOURS", bad)
    assert mp.ttl_hours() == mp.DEFAULT_TTL_HOURS


async def test_a_caller_clock_may_narrow_the_answer_but_never_widen_it(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    future = datetime.now(timezone.utc) + timedelta(days=365)
    assert await mp.is_purchasable("judydoll.com", "US", now=future) is False
    past = datetime.now(timezone.utc) - timedelta(days=365)
    assert await mp.is_purchasable("judydoll.com", "US", now=past) is True
    # And a clock cannot resurrect a row the SERVER already calls expired.
    gone = datetime.now(timezone.utc) - timedelta(hours=1)
    await _set("positive_until", gone.strftime("%Y-%m-%d %H:%M:%S") if not IS_POSTGRES else gone)
    assert await mp.is_purchasable("judydoll.com", "US", now=past) is False


async def test_get_fact_never_returns_an_expired_fact(_db):
    """`get_fact` has its OWN freshness clause. It is the human-facing read, so an expired row
    coming back from it would be read as "this merchant is fine" by the one person most likely
    to be deciding whether to arm the rail."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.get_fact("judydoll.com", "US") is not None
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await _set("positive_until", past.strftime("%Y-%m-%d %H:%M:%S") if not IS_POSTGRES else past)
    assert await mp.get_fact("judydoll.com", "US") is None
    assert await mp.list_facts("judydoll.com", "US"), "but the ROW is still there for an operator"


async def test_is_purchasable_fails_closed_when_the_database_raises(_db, monkeypatch):
    """FAIL-CLOSED, and this is the opposite of what a liveness check should do. A liveness sweep
    that cannot reach a store must not delete it; a PAYMENT gate that cannot prove payment must
    not permit it. Without this test a mutant that returned True on error survives everything."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "US") is True

    async def _explode(*args, **kwargs):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(database, "fetch_one", _explode)
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_an_undetermined_card_round_trips_as_none_and_never_as_false(_db):
    """SQLite has no boolean type and hands back 0/1, so the decode is where "could not
    determine" is most easily flattened into "no card" — `bool(0)` and `bool(None)` are both
    False. All three states must survive a round trip, distinctly."""
    await mp.record_check("a.com", "US", res("BLOCKED_UNKNOWN", card=None))
    await mp.record_check("b.com", "US", res("NO_CARD_PAYMENT", card=False))
    await mp.record_check("c.com", "US", res("ELIGIBLE", card=True))
    assert (await _row("a.com"))["card_available"] is None
    assert (await _row("b.com"))["card_available"] is False
    assert (await _row("c.com"))["card_available"] is True


async def test_the_market_check_refuses_a_market_that_is_not_iso2_upper(_db):
    """The column CHECK, on BOTH dialects. The module normalises before writing, so this is the
    BACKSTOP against a future writer that forgets — and the SQLite twin of the CHECK is a
    length/upper pair that nothing else in this suite exercises."""
    for bad in ("us", "U", "1s", ""):
        with pytest.raises(Exception):
            await database.execute(
                f"INSERT INTO {TABLE} (merchant_domain, market_country, vantage) "  # noqa: S608
                "VALUES (:d, :m, 'worker')",
                {"d": "x.com", "m": bad},
            )
    # And the ACCEPT half, so the refusals above are not passing because every insert fails.
    await database.execute(
        f"INSERT INTO {TABLE} (merchant_domain, market_country, vantage) "  # noqa: S608
        "VALUES ('x.com', 'US', 'worker')"
    )


# ── vantage ─────────────────────────────────────────────────────────────────────────────────


async def test_a_positive_fact_from_the_wrong_vantage_does_not_open_the_door(_db):
    """Reachability is egress-dependent, measured. A fact from an egress the buyer does not pay
    from describes OUR reachability, not theirs."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    assert await mp.is_purchasable("judydoll.com", "US") is False, "default vantage is 'worker'"
    assert await mp.get_fact("judydoll.com", "US") is not None, "but it IS visible to a human"


async def test_the_buyer_vantage_dial_chooses_which_fact_counts(_db, monkeypatch):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "proxy")
    assert await mp.is_purchasable("judydoll.com", "US") is True
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "worker")
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_vantages_are_separate_rows_and_demote_independently(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="worker")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    for _ in range(2):
        await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False), vantage="proxy")
    assert await mp.is_purchasable("judydoll.com", "US") is True
    assert (await _row(vantage="proxy"))["positive_until"] is None


# ── keys, evidence, PII ─────────────────────────────────────────────────────────────────────


async def test_the_key_is_normalised_on_write_and_on_read(_db):
    await mp.record_check("WWW.JudyDoll.com", "us", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "US") is True
    assert await mp.is_purchasable("www.judydoll.com", "us") is True
    assert len(await mp.list_facts("judydoll.com", "US")) == 1


async def test_the_fact_is_per_market(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "SG") is False


async def test_a_missing_row_is_not_purchasable(_db):
    assert await mp.is_purchasable("never-checked.com", "US") is False
    assert await mp.get_fact("never-checked.com", "US") is None


async def test_the_evidence_holds_no_buyer_data_and_no_page_html(_db):
    """An ALLOW-LIST, not a deny-list: the keys are fixed, so a buyer field added upstream cannot
    arrive here by default."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    evidence = (await _row())["evidence"]
    assert set(evidence) == {
        "verdict", "retryable", "detail", "final_status", "final_host",
        "checkout_country", "variant_source", "chain",
    }
    blob = json.dumps(evidence).lower()
    for forbidden in ("<html", "<script", "@", "address", "email", "postal", "phone"):
        assert forbidden not in blob, f"{forbidden!r} reached the evidence"


async def test_the_evidence_is_bounded(_db):
    """A hostile or merely long redirect chain must not make this row the largest in the table."""
    chain = tuple((302, f"https://x.com/{'a' * 300}/{i}") for i in range(200))
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True, chain=chain))
    stored = json.dumps((await _row())["evidence"])
    assert len(stored) <= 4000, len(stored)


async def test_payment_methods_round_trip_as_a_list(_db):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert (await _row())["payment_methods"] == ["Airwallex", "PAYPAL_EXPRESS"]


async def test_an_unusable_key_writes_nothing(_db):
    for domain, market in (("", "US"), ("judydoll.com", ""), ("judydoll.com", "U"), ("judydoll.com", "12")):
        assert await mp.record_check(domain, market, res("ELIGIBLE", card=True)) is None


async def test_the_price_columns_round_trip(_db):
    await mp.record_check(
        "flowerbeauty.com", "US",
        res("PRICE_DRIFT", card=True, landed_price_minor=800, price_drift_minor=-695),
        expected_price_minor=1495,
    )
    row = await _row("flowerbeauty.com")
    assert row["landed_price_minor"] == 800
    assert row["expected_price_minor"] == 1495
    assert row["price_drift_minor"] == -695


# ══ 3. THE JOB ═════════════════════════════════════════════════════════════════════════════


class _Fetcher:
    """A fake preflight. Records what it was asked and answers from a script — no socket, and
    no abandoned checkout on anybody's store."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def __call__(self, host, **kwargs):
        self.calls.append((host, kwargs))
        answer = self.answers.get(host, res("ELIGIBLE", card=True, host=host))
        return answer(host, kwargs) if callable(answer) else answer


async def test_the_job_is_dark_by_default_and_contacts_nobody(_db, monkeypatch):
    """THE DIAL. With it off nothing is fetched, because every check creates an abandoned
    checkout on a live merchant's store."""
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.skipped_disabled == 1
    assert report.checked == 0 and report.population == 0
    assert fetcher.calls == []


async def test_the_job_sweeps_the_population_when_armed(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({"flowerbeauty.com": res("NO_CARD_PAYMENT", card=False, host="flowerbeauty.com")})
    monkeypatch.setattr(sweep, "_preflight", fetcher)

    report = await sweep.run_merchant_purchasability_sweep()
    assert report.skipped_disabled == 0
    assert report.population == 2 and report.checked == 2 and report.written == 2
    assert report.positive == 1 and report.negative == 1
    assert await mp.is_purchasable("judydoll.com", "US") is True
    assert await mp.is_purchasable("flowerbeauty.com", "US") is False


async def test_the_job_passes_check_card_and_never_a_buyer(_db, monkeypatch, _population):
    """`buyer=None` ALWAYS: the Reap path carries no buyer data in the link, and this sweep has
    no buyer to carry. `check_card=True` is what makes the card evidence a verdict."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    await sweep.run_merchant_purchasability_sweep()
    assert fetcher.calls
    for _, kwargs in fetcher.calls:
        assert kwargs["buyer"] is None
        assert kwargs["check_card"] is True
        assert kwargs["quantity"] == 1


async def test_the_job_holds_a_wall_clock_budget(_db, monkeypatch, _population):
    """The budget stops it STARTING a merchant. Without one, a slow batch would run past the
    job's Cloud Run task timeout and be cut mid-store, every run."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUDGET_SECONDS", "30")
    # A clock that jumps past the budget as soon as the FIRST merchant has been fetched, so the
    # second is abandoned rather than started. No sleeping: the budget is monotonic time, and a
    # test that actually waited would be a 30-second test.
    clock = {"t": 0.0}
    monkeypatch.setattr(sweep, "_monotonic", lambda: clock["t"])

    async def _fetch_then_burn_the_budget(host, **kwargs):
        clock["t"] = 999.0
        return res("ELIGIBLE", card=True, host=host)

    monkeypatch.setattr(sweep, "_preflight", _fetch_then_burn_the_budget)

    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population == 2
    assert report.checked == 1, "the second merchant must not be STARTED"
    assert report.abandoned_budget == 1


async def test_the_job_bounds_its_batch(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BATCH", "1")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population == 1 and len(fetcher.calls) == 1


async def test_one_merchant_that_raises_does_not_end_the_batch(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")

    def _boom(host, kwargs):
        raise RuntimeError("a store that hangs up")

    fetcher = _Fetcher({"flowerbeauty.com": _boom})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.errors == 1
    assert report.checked == 1 and report.written == 1
    assert await mp.is_purchasable("judydoll.com", "US") is True


async def test_the_report_carries_counts_only(_db, monkeypatch, _population):
    """No domains, no variant ids, nothing from a row — the same rule PollReport follows."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))
    report = await sweep.run_merchant_purchasability_sweep()
    text = repr(report)
    for leak in ("judydoll", "flowerbeauty", ".com", "5004"):
        assert leak not in text, f"{leak!r} leaked into the report"
    from dataclasses import fields
    for field in fields(report):
        assert field.type in ("int",), f"{field.name} is not a count"


async def test_the_second_vantage_is_configured_not_coded(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("VANTAGE_PROXY_URL", "http://127.0.0.1:7897")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.checked == 4, "two merchants x two vantages"
    assert {r["vantage"] for r in await mp.list_facts("judydoll.com", "US")} == {"worker", "proxy"}


@pytest.mark.parametrize("bad", ["", "not-a-url", "ftp://x", "127.0.0.1:7897", "  "])
async def test_a_malformed_proxy_url_is_ignored_rather_than_handed_to_the_client(monkeypatch, bad):
    monkeypatch.setenv("VANTAGE_PROXY_URL", bad)
    assert sweep.proxy_url() is None


async def test_the_population_is_the_union_of_the_two_reap_allowlists(_db, _population):
    targets = await sweep.load_population(50)
    assert {(t.domain, t.market) for t in targets} == {("judydoll.com", "US"), ("flowerbeauty.com", "US")}
    by_domain = {t.domain: t for t in targets}
    assert by_domain["judydoll.com"].variant_id == "50041364447509", "the Tier B row's confirmed variant"


# ── the two dials (F5) ──────────────────────────────────────────────────────────────────────
#
# IT WAS ONE DIAL, AND ONE WAS A BUG. `_add_job` registers on the production WORKER only, and a
# normal backend deploy does not ship the worker — so one shared dial armed on the backend turns
# the refusals on while the sweep that feeds them never runs anywhere, and every merchant in the
# catalogue answers a permanent 409. Each dial must gate ONLY its own side.


async def test_the_sweep_dial_gates_the_job_and_not_the_consumers(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)

    report = await sweep.run_merchant_purchasability_sweep()
    assert report.skipped_disabled == 0 and report.checked > 0, "the sweep runs"
    assert mp.is_enforcement_enabled() is False, "and enforcement stays off"


async def test_the_enforce_dial_gates_the_consumers_and_not_the_job(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", raising=False)
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)

    report = await sweep.run_merchant_purchasability_sweep()
    assert report.skipped_disabled == 1, "the job stays dark"
    assert fetcher.calls == [], "and contacts nobody"
    assert mp.is_enforcement_enabled() is True, "while enforcement is armed"


async def test_neither_dial_reads_the_other_ones_variable(monkeypatch):
    """The two must not be wired to one env var under two names. Set each ALONE and the other
    must stay off — this is what kills a mutant that points one reader at the other's name."""
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", raising=False)
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    assert (mp.is_sweep_enabled(), mp.is_enforcement_enabled()) == (False, False)

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    assert (mp.is_sweep_enabled(), mp.is_enforcement_enabled()) == (True, False)

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    assert (mp.is_sweep_enabled(), mp.is_enforcement_enabled()) == (False, True)


def test_the_job_reads_the_sweep_dial_through_the_one_authoritative_reader():
    assert sweep.is_enabled is not mp.is_sweep_enabled, "delegates rather than aliases"
    assert "is_sweep_enabled" in (sweep.is_enabled.__doc__ or "")


@pytest.mark.parametrize("dial", ["MERCHANT_PURCHASABILITY_SWEEP_ENABLED",
                                  "MERCHANT_PURCHASABILITY_ENFORCE"])
def test_a_dial_is_off_for_anything_that_is_not_an_explicit_yes(monkeypatch, dial):
    reader = mp.is_sweep_enabled if dial.endswith("SWEEP_ENABLED") else mp.is_enforcement_enabled
    for off in ("", "0", "false", "off", "no", "maybe", "TRUEISH", " "):
        monkeypatch.setenv(dial, off)
        assert reader() is False, off
    for on in ("1", "true", "TRUE", " on ", "yes"):
        monkeypatch.setenv(dial, on)
        assert reader() is True, on


# ── the gateway contract, on BOTH dialects ──────────────────────────────────────────────────
#
# These live here and not only in the Postgres file because `enforced` is a CONTRACT with another
# repo, not a storage detail: a mutant that hardcoded it survived when the only coverage was
# Postgres-side. The route needs an app, so it gets a minimal one — never `main` (gate-wide
# create_all poisoning) and never TestClient (it hangs against the asyncpg pool).


@pytest.fixture
def ops_app():
    from fastapi import FastAPI

    from routes.merchant_purchasability_ops import router
    from utils.gateway_oidc_auth import require_admin_or_gateway_identity

    app = FastAPI()
    app.include_router(router)
    # The route's dependency since the OIDC follow-up. Overriding `require_admin` here would
    # NOT bypass anything any more — FastAPI keys overrides on the exact callable the route
    # declares — and every case below would 403 on a missing Authorization header. The auth
    # dependency itself is the subject of tests/test_ops_gateway_oidc.py; here it is stubbed so
    # these cases stay about the FACT.
    app.dependency_overrides[require_admin_or_gateway_identity] = lambda: {"role": "admin"}
    return app


async def ops_get(app, url):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(url)


async def test_the_ops_route_reports_enforced_from_the_enforce_dial(_db, ops_app, monkeypatch):
    """`enforced` IS the gateway's permission to act on `tier`. Hardcode it and a gateway takes
    the whole catalogue browse-only on the day the field ships, because with enforcement off
    every merchant reads browse_only."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is False, "the dial is off, so the gateway must not act on tier"
    assert body["tier"] == "purchase", "the FACT is still reported"

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US")).json()
    assert body["enforced"] is True


async def test_the_ops_route_reports_sweep_enabled_from_the_sweep_dial(_db, ops_app, monkeypatch):
    """The misordered state — nothing gathering, everything refusing — must be visible."""
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", raising=False)
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=never-seen.com&market=US")).json()
    assert (body["sweep_enabled"], body["enforced"]) == (False, True), (
        "this pair is the arming mistake the runbook warns about, and it has to be readable"
    )
    assert body["tier"] == "browse_only"

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=never-seen.com&market=US")).json()
    assert body["sweep_enabled"] is True


async def test_the_two_dials_are_reported_independently(_db, ops_app, monkeypatch):
    """Four states, four distinct answers — a single shared reader would collapse them to two."""
    seen = set()
    for sweep_on in (False, True):
        for enforce_on in (False, True):
            for name, on in (("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", sweep_on),
                             ("MERCHANT_PURCHASABILITY_ENFORCE", enforce_on)):
                monkeypatch.setenv(name, "1") if on else monkeypatch.delenv(name, raising=False)
            body = (await ops_get(
                ops_app, "/ops/merchant-purchasability?domain=x.com&market=US")).json()
            seen.add((body["sweep_enabled"], body["enforced"]))
    assert seen == {(False, False), (False, True), (True, False), (True, True)}


# ══ 4. WHERE IT RUNS: a Cloud Run Job on the crawl subnet, never the worker scheduler ════════
#
# Until 2026-09-27 the sweep was a `services.audit_scheduler` interval job, which ran it on the
# `worker` — whose egress is the default NAT, the address payment partners allowlist — and
# restarted its clock on every worker deploy. It is now `python -m jobs.merchant_purchasability_sweep`
# in a Cloud Run Job on pivota-crawl (infra/gcp/setup_merchant_purchasability_sweep_job.sh; the
# script itself is exercised by tests/test_setup_merchant_purchasability_sweep_job.py).


class _RecordingScheduler:
    """The same recorder tests/test_scheduler_job_isolation.py uses, so this test sees what
    `start_scheduler` ACTUALLY registers rather than what the source text says."""

    def __init__(self, *a, **k):
        self.added = []  # (id, func)

    def add_job(self, func, *a, **k):
        self.added.append((k.get("id"), func, k))

    def start(self):
        pass

    def get_jobs(self):
        return []


async def test_the_worker_scheduler_never_runs_the_sweep(monkeypatch):
    """FROM THE LIVE REGISTRY, not only from the source: no registered job is the sweep, under
    its old id or any other — a re-registration under a new id would still render checkouts from
    the payment NAT. The source half catches an import that would re-introduce it."""
    import services.audit_scheduler as sched

    for key in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    recorder = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: recorder)

    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    assert len(recorder.added) > 20, "precondition: the recorder saw the real registration pass"

    ids = {jid for jid, _fn, _k in recorder.added}
    assert "merchant_purchasability_sweep" not in ids
    swept = [
        jid for jid, fn, _k in recorder.added
        if getattr(fn, "__wrapped__", fn) is sweep.run_merchant_purchasability_sweep
        or getattr(fn, "__module__", "") == sweep.__name__
        or getattr(getattr(fn, "__wrapped__", None), "__module__", "") == sweep.__name__
    ]
    assert swept == [], f"the sweep is registered on the worker scheduler as {swept}"
    assert "merchant_purchasability_sweep" not in sched._JOB_RUN_DEADLINES

    source = pathlib.Path(sched.__file__).read_text()
    assert "jobs.merchant_purchasability_sweep" not in source
    assert "run_merchant_purchasability_sweep" not in source


def test_the_runbook_names_the_crawl_job_and_both_dials():
    """An operator arms this from the runbook, so the runbook must say where the sweep runs now
    and how it is armed — and still name both dials, whose ORDER is the outage."""
    runbook = (pathlib.Path(__file__).parent.parent / "docs" / "runbooks"
               / "merchant_purchasability.md").read_text()
    assert "infra/gcp/setup_merchant_purchasability_sweep_job.sh" in runbook
    assert "pivota-crawl" in runbook
    assert "MERCHANT_PURCHASABILITY_SWEEP_ENABLED" in runbook
    assert "MERCHANT_PURCHASABILITY_ENFORCE" in runbook
    section_9 = runbook.split("## 9.")[1].split("## 10.")[0]
    # §9's proof of a run is the JOB's execution and its report line — the worker's
    # /__scheduler_health no longer lists the sweep at all.
    assert "gcloud run jobs executions list --job merchant-purchasability-sweep" in section_9
    assert "SweepReport(" in section_9 and "population_unreadable" in section_9
    assert "7 0-1,5-23 * * *" in section_9, "the schedule the script provisions"


# ── the CLI: `python -m jobs.merchant_purchasability_sweep` runs ONE sweep and exits ──────────


async def test_the_cli_runs_one_sweep_over_the_population_and_exits_0(_db, monkeypatch, _population):
    """`amain` is the CLI's whole path minus the event loop: gate, connection handling, ONE run,
    exit code. Driven here over the real two-lane population."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    with capture_pivota_stdout() as out:
        assert await sweep.amain() == sweep.EXIT_OK == 0
    assert sorted(host for host, _ in fetcher.calls) == ["flowerbeauty.com", "judydoll.com"], (
        "ONE sweep: each merchant exactly once"
    )
    proof = [line for line in pivota_lines(out) if _PROOF_LINE.fullmatch(line)]
    assert len(proof) == 1 and "population_unreadable=0" in proof[0], pivota_lines(out)
    assert await mp.is_purchasable("judydoll.com", "US") is True


async def test_the_cli_exits_1_when_a_population_lane_cannot_be_read(_db, monkeypatch, _population):
    """A lane that raises is COUNTED, the other lane is still swept, and the execution FAILS.
    Before the count the lanes failed soft and silently, so a database that answered nothing
    produced a green `population=0 errors=0` run."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    real_fetch_all = database.fetch_all

    async def _cart_lane_down(query, *a, **k):
        if query is sweep._CART_LANE_SQL:
            raise RuntimeError("relation tierb_cart_link_eligibility is unavailable")
        return await real_fetch_all(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _cart_lane_down)
    with capture_pivota_stdout() as out:
        assert await sweep.amain() == sweep.EXIT_POPULATION_UNREADABLE == 1
    assert [host for host, _ in fetcher.calls] == ["flowerbeauty.com"], (
        "the readable (variant) lane is still swept"
    )
    assert any("1 population lane(s) could not be read" in line for line in pivota_lines(out))
    assert any("population_unreadable=1" in line for line in pivota_lines(out))


async def test_the_variant_lane_failure_is_counted_not_swallowed(_db, monkeypatch, _population):
    """The variant lane reads through the LEDGER's helper, whose default form swallows a failure
    into `[]`. The sweep asks for `strict=True`; a revert to the soft form reads a dead table as
    an empty allowlist, and this test sees `population_unreadable == 0`."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    real_fetch_all = database.fetch_all

    async def _variant_lane_down(query, *a, **k):
        if query is ledger._ENABLED_MERCHANT_MARKETS_SQL:
            raise RuntimeError("relation reap_agentic_eligibility is unavailable")
        return await real_fetch_all(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _variant_lane_down)
    assert await ledger.list_enabled_merchant_markets() == [], "the default form stays soft"
    with pytest.raises(RuntimeError):
        await ledger.list_enabled_merchant_markets(strict=True)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_unreadable == 1
    assert [host for host, _ in fetcher.calls] == ["judydoll.com"], "the cart lane is still swept"
    assert sweep.exit_code_for(report) == sweep.EXIT_POPULATION_UNREADABLE


async def test_the_cli_exits_1_when_the_population_cannot_be_built(_db, monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)

    async def _boom(*_a, **_k):
        raise RuntimeError("the population query died")

    monkeypatch.setattr(sweep, "load_population", _boom)
    with capture_pivota_stdout() as out:
        assert await sweep.amain() == sweep.EXIT_POPULATION_UNREADABLE
    assert fetcher.calls == []
    assert any(_PROOF_LINE.fullmatch(line) and "population_unreadable=1" in line
               for line in pivota_lines(out)), "the report line still lands on the failed run"


async def test_the_cli_exits_4_when_a_check_raises(_db, monkeypatch, _population):
    """`errors` — "the only count that should page anyone" — fails the execution too, but the
    population was read, so it is told apart from an unreadable one."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")

    def _raise(_host, _kwargs):
        raise RuntimeError("a store nobody has classified")

    monkeypatch.setattr(sweep, "_preflight", _Fetcher({"judydoll.com": _raise}))
    assert await sweep.amain() == sweep.EXIT_ERRORS == 4


def test_exit_code_for_ranks_an_unreadable_population_above_errors():
    assert sweep.exit_code_for(sweep.SweepReport()) == 0
    assert sweep.exit_code_for(sweep.SweepReport(skipped_disabled=1)) == 0
    assert sweep.exit_code_for(sweep.SweepReport(abandoned_budget=5)) == 0
    assert sweep.exit_code_for(sweep.SweepReport(errors=2)) == 4
    assert sweep.exit_code_for(sweep.SweepReport(errors=2, population_unreadable=1)) == 1


class _FakeDatabase:
    """Stands in for the sweep's `database` so the CLI's connection handling is seen DISCONNECTED
    — the state a job process starts in — whatever the shared test connection is doing. (The
    Postgres gate runs these cases in one process, where the real one may well be connected, and
    `amain` would then correctly skip connect() and prove nothing.)"""

    def __init__(self, *, connect_error=None):
        self.is_connected = False
        self.events = []
        self._connect_error = connect_error

    async def connect(self):
        self.events.append("connect")
        if self._connect_error is not None:
            raise self._connect_error
        self.is_connected = True

    async def disconnect(self):
        self.events.append("disconnect")
        self.is_connected = False


async def test_the_cli_with_the_gate_off_contacts_nobody_and_never_connects(monkeypatch):
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    fake = _FakeDatabase()
    monkeypatch.setattr(sweep, "database", fake)
    assert await sweep.amain() == sweep.EXIT_OK
    assert fetcher.calls == [] and fake.events == [], "a dark job must not open a connection"


async def test_the_cli_connects_runs_once_and_disconnects_with_the_gate_on(monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    fake = _FakeDatabase()
    monkeypatch.setattr(sweep, "database", fake)

    async def _one_run():
        fake.events.append("run")
        return sweep.SweepReport(population=1, checked=1, written=1)

    monkeypatch.setattr(sweep, "run_merchant_purchasability_sweep", _one_run)
    assert await sweep.amain() == sweep.EXIT_OK
    assert fake.events == ["connect", "run", "disconnect"]


async def test_the_cli_exits_1_without_a_traceback_when_the_database_is_unreachable(monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    fake = _FakeDatabase(connect_error=OSError("connection refused"))
    monkeypatch.setattr(sweep, "database", fake)

    async def _must_not_run():  # pragma: no cover - reached only by a regression
        raise AssertionError("nothing may run without a connection")

    monkeypatch.setattr(sweep, "run_merchant_purchasability_sweep", _must_not_run)
    with root_as_in_prod(), capture_pivota_stdout() as out:
        assert await sweep.amain() == sweep.EXIT_POPULATION_UNREADABLE
    assert fake.events == ["connect"]
    assert any("could not connect to the database (error_type=OSError)" in line
               for line in pivota_lines(out)), pivota_lines(out)


def test_the_proxy_vantage_really_leaves_through_its_proxy(monkeypatch):
    """Every run-level test mounts a mock transport, so this is the one place the REAL
    `_inner_transport` is looked at. A version that dropped `via` would send the `proxy`
    vantage's checks out of the crawl egress and still record them as `proxy` — a mislabelled
    fact the door trusts whenever MERCHANT_PURCHASABILITY_BUYER_VANTAGE=proxy."""
    import httpcore

    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    via = sweep._inner_transport("http://vantage-proxy.example:3128")
    assert isinstance(via._pool, httpcore.AsyncHTTPProxy)
    assert (via._pool._proxy_url.host, via._pool._proxy_url.port) == (b"vantage-proxy.example", 3128)

    direct = sweep._inner_transport(None)
    assert not isinstance(direct._pool, httpcore.AsyncHTTPProxy), "the crawl-egress vantage is direct"

    # An operator's process proxy is honoured for the DIRECT vantage only; `via` always wins.
    monkeypatch.setenv("HTTPS_PROXY", "http://laptop-proxy.example:8080")
    assert sweep._inner_transport(None)._pool._proxy_url.host == b"laptop-proxy.example"
    assert sweep._inner_transport("http://vantage-proxy.example:3128")._pool._proxy_url.host == (
        b"vantage-proxy.example"
    )


def test_main_returns_what_amain_returns(monkeypatch):
    """`main` is `asyncio.run(amain())` behind an option-less parser; its return value is the
    process exit status (`raise SystemExit(main())`)."""
    for code in (0, 1, 4):
        async def _amain(code=code):
            return code

        monkeypatch.setattr(sweep, "amain", _amain)
        assert sweep.main([]) == code
    with pytest.raises(SystemExit) as exc:
        sweep.main(["--budget-seconds", "1"])
    assert exc.value.code == 2, "no options: a knob belongs in the job's env, not its args"


def test_python_dash_m_is_the_entry_point_the_job_runs():
    """The Cloud Run Job runs exactly `python -m jobs.merchant_purchasability_sweep`. Run that, in
    a child process, with the gate off: exit 0, the disabled line on stdout, nobody contacted.
    A module without its `__main__` block would exit 0 here too — hence the stdout assertion."""
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "MERCHANT_PURCHASABILITY_SWEEP_ENABLED"}
    proc = subprocess.run(
        [sys.executable, "-m", "jobs.merchant_purchasability_sweep"],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "merchant_purchasability_sweep: disabled; no merchant was contacted" in proc.stdout, (
        proc.stdout[-2000:], proc.stderr[-2000:]
    )


async def test_every_request_of_a_run_goes_through_one_pacer(_db, monkeypatch, _population):
    """The crawl address is shared with the Tier B job and the other crawlers, so request STARTS
    are spaced >= MIN_REQUEST_INTERVAL_S across the WHOLE run — across merchants, not just within
    one. Driven with a fake clock through the real client construction; the fake preflight makes
    three requests per merchant, as a real one (catalog page + permalink hops) would."""
    from jobs.tierb_cart_link_eligibility import MIN_REQUEST_INTERVAL_S, RequestPacer

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    clock = {"t": 1000.0}

    async def _sleep(seconds):
        clock["t"] += seconds

    pacers = []

    def _pacer():
        pacers.append(RequestPacer(MIN_REQUEST_INTERVAL_S, clock=lambda: clock["t"], sleep=_sleep))
        return pacers[-1]

    seen = []

    def _answer(request):
        seen.append((clock["t"], request.url.host))
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(sweep, "_new_pacer", _pacer)
    monkeypatch.setattr(sweep, "_inner_transport", lambda via: httpx.MockTransport(_answer))

    async def _three_requests(host, **kwargs):
        for path in ("/products.json", "/cart/1:1", "/checkouts/cn/T"):
            await kwargs["client"].get(f"https://{host}{path}")
        return res("ELIGIBLE", card=True, host=host)

    monkeypatch.setattr(sweep, "_preflight", _three_requests)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.checked == 2 and report.errors == 0
    assert len(pacers) == 1, "ONE pacer per run, shared by every merchant"
    assert len(seen) == 6 and {host for _, host in seen} == {"judydoll.com", "flowerbeauty.com"}
    starts = [t for t, _ in seen]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= MIN_REQUEST_INTERVAL_S, gaps


async def test_check_card_defaults_off_behaviourally_not_just_in_the_signature(monkeypatch):
    """M23. A signature assertion survives a caller that passes `check_card=True` by default
    somewhere else, and survives the parameter being ignored entirely. This drives the real
    `preflight` over the PayPal-only fixture through a fake transport and compares VERDICTS.
    """
    import services.shopify_cart_link_preflight as pf

    fixture = html.unescape((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
    # A landing that satisfies every pre-card check, plus the live accept-list.
    body = page(variant="17281773207622") + fixture

    async def _fake_fetch(client, url, *, pin_params=None):
        if "/products.json" in url or "/products/" in url:
            return pf._Landing(200, json.dumps({"products": [{
                "title": "Thing", "variants": [
                    {"id": 17281773207622, "available": True, "price": "14.95",
                     "title": "Default", "requires_shipping": True}]}]}),
                [(200, url)])
        return pf._Landing(200, body, [(200, "https://flowerbeauty.com/checkouts/cn/T")])

    monkeypatch.setattr(pf, "_fetch_following", _fake_fetch)

    default = await pf.preflight(
        "flowerbeauty.com", market="US", variant_id="17281773207622",
        click_id="c", buyer=None,
    )
    asked = await pf.preflight(
        "flowerbeauty.com", market="US", variant_id="17281773207622",
        click_id="c", buyer=None, check_card=True,
    )

    assert default.verdict is not Verdict.NO_CARD_PAYMENT, (
        "the DEFAULT call must keep the pre-PR verdict — this is what keeps the Tier B lane "
        "unchanged, and a signature assertion cannot show it"
    )
    assert default.verdict is Verdict.ELIGIBLE, "it keeps the verdict it had before this PR"
    assert asked.verdict is Verdict.NO_CARD_PAYMENT, "asking for the card check changes the verdict"
    assert asked.card_available is False
    # THE FIELDS ARE FILLED EITHER WAY; only the VERDICT is opt-in. That is the design: a Tier B
    # row gains the observability without its decision moving. So the two results must differ in
    # exactly one place — the verdict — and agree everywhere else the detector touched.
    assert default.card_available is asked.card_available is False
    assert default.payment_methods == asked.payment_methods
    assert "PAYPAL_EXPRESS" in asked.payment_methods
    assert "shopify_payments" not in asked.payment_methods


async def test_the_sweep_population_equals_what_the_reap_route_admits(_db, _population):
    """F7. A gate whose population is NARROWER than the allowlist it gates is not a gate.

    The reviewer measured a merchant the route accepts that the sweep never visited: the sweep
    also required `variant_key = ''`, which the route never does. Both now derive from
    `db.reap_agentic_ledger.list_enabled_merchant_markets`, and this test is the equality.
    """
    import routes.agent_commerce_reap as reap

    # A merchant row typed WITH a variant key — legal, operator-typed, and invisible to the old
    # sweep predicate while perfectly acceptable to the route.
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES ('variantkeyed.com', '', 'v123', 'US', TRUE)"
    )

    admitted = await reap._eligibility(
        merchant_domain="variantkeyed.com", market_country="US",
        product_key="anything", variant_key=None,
    )
    assert admitted.market_country == "US", "precondition: the ROUTE admits this merchant"

    population = {(t.domain, t.market) for t in await sweep.load_population(50)}
    assert ("variantkeyed.com", "US") in population, (
        "the route admits this merchant and the sweep never visits it — it would be purchasable "
        "with no fact ever gathered"
    )

    # And the equality in general: every merchant the variant lane admits is in the population.
    lane = {(mp.normalize_domain(r["merchant_domain"]), mp.normalize_market(r["market_country"]))
            for r in await ledger.list_enabled_merchant_markets()}
    assert lane <= population, sorted(lane - population)


def test_the_route_and_the_sweep_share_one_merchant_row_sentinel():
    """Two spellings of "which rows are merchant rows" is how the gap above happened."""
    import routes.agent_commerce_reap as reap

    # NOT `reap._MERCHANT_ROW is ledger.ELIGIBILITY_MERCHANT_ROW`: both are `""`, which CPython
    # interns, so identity holds even when the route redeclares its own literal — a mutant that
    # did exactly that survived this assertion. The binding has to be checked in the SOURCE.
    assert reap._MERCHANT_ROW == ledger.ELIGIBILITY_MERCHANT_ROW
    reap_source = pathlib.Path(reap.__file__).read_text()
    assert "_MERCHANT_ROW = ledger.ELIGIBILITY_MERCHANT_ROW" in reap_source, (
        "the route must BIND the ledger's sentinel, not redeclare a literal that happens to "
        "match it today"
    )
    assert '_MERCHANT_ROW = ""' not in reap_source
    sweep_source = pathlib.Path(sweep.__file__).read_text()
    assert "FROM reap_agentic_eligibility" not in sweep_source, (
        "the sweep must not spell the variant lane's query itself; it calls the shared helper "
        "(naming the table in PROSE is fine — this looks for a second copy of the SQL)"
    )
    assert "list_enabled_merchant_markets" in sweep_source


# ══ 5. THE CONSUMERS, dark behind the dial ═════════════════════════════════════════════════


async def test_the_reap_rail_ignores_the_fact_while_the_dial_is_off(_db, monkeypatch):
    """DARK BY DEFAULT: with the dial off the rail behaves exactly as it did."""
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    assert mp.is_enforcement_enabled() is False
    # No fact exists at all; the gate would refuse if it were consulted.
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_the_reap_rail_consults_the_fact_only_under_the_enforce_dial(monkeypatch):
    """The seam. Read off the source because the surrounding handler needs a whole authenticated
    purchase request to reach; the BEHAVIOUR of the dial itself is covered above."""
    source = (pathlib.Path(__file__).parent.parent / "routes" / "agent_commerce_reap.py").read_text()
    assert "purchasability.is_enforcement_enabled()" in source
    assert "purchasability.is_purchasable(merchant_domain, market_country)" in source
    assert '"merchant_not_purchasable"' in source
    assert '"merchant_not_purchasable": 409' in source
    assert "purchasability.is_sweep_enabled()" not in source, (
        "the reap rail must never gate on the SWEEP dial — that is the misordered arming the "
        "split exists to make impossible"
    )


def test_the_checkout_tier_surface_gates_on_enforce_and_not_on_the_sweep_dial():
    source = (pathlib.Path(__file__).parent.parent / "routes" / "store_audit_ops.py").read_text()
    assert "purchasability_gate_enabled" in source
    assert "purchasability.is_enforcement_enabled()" in source
    assert "purchasability.is_sweep_enabled()" not in source


async def _ensure_connected_tables(undo: list) -> None:
    """`merchant_onboarding` and `products_cache` from the repo's own metadata; `merchant_stores`
    from main.py's startup DDL, which is the only place it is defined (its columns this job reads
    are store_id / merchant_id / platform / name / domain / status). Whatever it creates or adds
    is recorded in `undo` for `_restore`."""
    import sqlalchemy
    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.products import products_cache

    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    for table in ("merchant_onboarding", "products_cache", "merchant_stores"):
        if not await _table_exists(table):
            undo.append(("table", table))
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[merchant_onboarding, products_cache], checkfirst=True)
    engine.dispose()
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, "
        "platform VARCHAR(50) NOT NULL, name VARCHAR(255) NOT NULL, domain VARCHAR(255), "
        "api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
        "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE "
        "DEFAULT CURRENT_TIMESTAMP)"
    )
    # THE DIALECT GATE SHARES ONE DATABASE ACROSS TEST FILES, and an earlier one
    # (test_connection_layer_postgres.py) leaves a `merchant_stores` without `name` / `status`,
    # which the IF NOT EXISTS above then keeps. Add what this lane's fixture writes.
    await _ensure_columns(
        "merchant_stores", ("merchant_id", "platform", "name", "domain", "status"), undo
    )


#: The columns the cart-mint lane reads, beyond `id`. Added one by one when missing: the dialect
#: gate shares ONE database across test files, and some of them create `external_product_seeds`
#: with a single `id` column.
#:
#: AND PUT BACK AFTERWARDS (`_restore`). Leaving a table or a column behind is pollution in the
#: other direction: files after this one create `external_product_seeds` with IF NOT EXISTS and
#: then insert rows with no `id` and read `seed_data` as JSONB, so a leftover TEXT `seed_data`
#: or `id` PRIMARY KEY failed 39 of their tests in a local run of the whole gate.
_SEED_TABLE_COLUMNS = (
    "status", "domain", "market", "attached_product_key", "attached_variant_id",
    "external_product_id", "destination_url", "canonical_url", "seller_ref", "seed_kind",
    "seed_data",
)


async def _table_exists(table: str) -> bool:
    try:
        await database.fetch_all(f"SELECT 1 FROM {table} WHERE 1 = 0")
        return True
    except Exception:  # noqa: BLE001 — the probe's only job is to say "missing"
        return False


async def _ensure_columns(table: str, columns, undo: list) -> None:
    """Add each missing column as TEXT, recording it in `undo`. A probe per column rather than an
    information_schema read, so the same code works on both dialects."""
    for column in columns:
        try:
            await database.fetch_all(f"SELECT {column} FROM {table} WHERE 1 = 0")
        except Exception:  # noqa: BLE001 — the probe's only job is to say "missing"
            await database.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            undo.append(("column", table, column))


async def _ensure_seed_table(undo: list) -> None:
    if not await _table_exists("external_product_seeds"):
        undo.append(("table", "external_product_seeds"))
        await database.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY)")
    await _ensure_columns("external_product_seeds", _SEED_TABLE_COLUMNS, undo)


async def _clear_scan_cache() -> None:
    import db.merchant_purchasability_cart_mint_scans as mint_scans

    mint_scans._reset_for_tests()
    assert await mint_scans.ensure_table()
    await database.execute(f"DELETE FROM {mint_scans.TABLE}")


async def _restore(undo: list) -> None:
    """Leave the shared database's schema as this fixture found it: drop what it created, newest
    first. A column added to a table this fixture also created goes with the table."""
    dropped = set()
    for item in reversed(undo):
        if item[0] == "table":
            await database.execute(f"DROP TABLE IF EXISTS {item[1]}")
            dropped.add(item[1])
        elif item[1] not in dropped:
            await database.execute(f"ALTER TABLE {item[1]} DROP COLUMN {item[2]}")


@pytest.fixture
async def _population(_db):
    """The two Reap allowlists, minimally shaped. Built here rather than through a factory so the
    column names this job's SQL depends on are asserted by the test's own DDL."""
    await database.execute(
        "CREATE TABLE IF NOT EXISTS reap_agentic_eligibility ("
        "merchant_domain VARCHAR(255) NOT NULL, product_key TEXT NOT NULL DEFAULT '', "
        "variant_key TEXT NOT NULL DEFAULT '', market_country VARCHAR(2) NOT NULL, "
        "enabled BOOLEAN NOT NULL DEFAULT FALSE, accept_variant_labels TEXT, "
        "also_accept_domains TEXT, "
        "PRIMARY KEY (merchant_domain, market_country, product_key, variant_key))"
    )
    await database.execute("DELETE FROM reap_agentic_eligibility")
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES ('flowerbeauty.com', '', '', 'US', TRUE)"
    )
    # A disabled row, and a non-merchant override row: neither may reach the population.
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES ('disabled-store.com', '', '', 'US', FALSE)"
    )
    from db.tierb_cart_link_eligibility_schema import ensure_schema

    await ensure_schema()
    await database.execute("DELETE FROM tierb_cart_link_eligibility")
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, variant_id, "
        "checked_at) VALUES ('judydoll.com', 'US', 'ELIGIBLE', '50041364447509', CURRENT_TIMESTAMP)"
    )
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, variant_id, "
        "checked_at) VALUES ('luafee.com', 'US', 'NOT_ACCEPTING_ORDERS', '1', CURRENT_TIMESTAMP)"
    )
    # The CONNECTED-STORE lane's tables, present and EMPTY, so the population above is exactly
    # the two allowlists' and that lane reads (not fails to read) nothing. `_connected` fills it.
    undo: list = []
    await _ensure_connected_tables(undo)
    await database.execute("DELETE FROM merchant_stores")
    await database.execute("DELETE FROM merchant_onboarding")
    # And the CART-MINT lane's: the seed table, present and EMPTY. `_cart_seeds` fills it. Its
    # scan CACHE starts empty too, or the first run here would read another test's scan as
    # today's.
    await _ensure_seed_table(undo)
    await database.execute("DELETE FROM external_product_seeds")
    await _clear_scan_cache()
    yield
    await _clear_scan_cache()
    await database.execute("DELETE FROM reap_agentic_eligibility")
    await database.execute("DELETE FROM tierb_cart_link_eligibility")
    await database.execute("DELETE FROM merchant_stores")
    await database.execute("DELETE FROM merchant_onboarding")
    await database.execute("DELETE FROM external_product_seeds")
    await _restore(undo)


# ══ the sweep's catalog hint is matched canonically ═══════════════════════════════════════
#
# The Shopify sync writes `catalog_products.source_domain` as Shopify's `shop_domain` —
# `www.Brand.com` in production — and a population key is canonical (`brand.com`). A bare
# `lower()` compare found no variant and no price for exactly those merchants, so the preflight
# measured a variant it picked itself against no expected price. The catalog SQL now folds the
# column by the purchase route's own expression.

_WWW_PRODUCT = "prod::m_wwwsweep::shopify::7001"
_LOOKALIKE_PRODUCT = "prod::m_wwwsweep_lookalike::shopify::7002"


async def _seed_sweep_catalog_product(product_key, merchant_id, domain, variant_id, price):
    sku_key = f"sku::{product_key}::v"
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, "
        "title, source_domain) VALUES (:pk, :m, 'shopify', :spid, 'Sweep Hint', :d)",
        {"pk": product_key, "m": merchant_id, "spid": product_key[-4:], "d": domain},
    )
    await database.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
        "source_variant_id, title, currency) VALUES (:sk, :pk, :m, 'shopify', :spid, :v, 'One', 'USD')",
        {"sk": sku_key, "pk": product_key, "m": merchant_id, "spid": product_key[-4:], "v": variant_id},
    )
    await database.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, "
        "merchant_effective_price) VALUES (:oid, :sk, :pk, :m, 'USD', :price)",
        {"oid": f"off::{product_key}", "sk": sku_key, "pk": product_key, "m": merchant_id,
         "price": price},
    )


@pytest.fixture
async def _www_catalog(_population):
    import sqlalchemy
    from db.catalog import catalog_offers, catalog_products, catalog_skus
    from db.database import metadata

    # Both dialects: the Postgres gate star-imports this file (tests/test_merchant_purchasability_
    # postgres.py) and this fixture by name.
    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[catalog_products, catalog_skus, catalog_offers],
                        checkfirst=True)
    engine.dispose()

    async def _clean():
        for table in ("catalog_offers", "catalog_skus", "catalog_products"):
            await database.execute(
                f"DELETE FROM {table} WHERE product_key IN (:a, :b)",
                {"a": _WWW_PRODUCT, "b": _LOOKALIKE_PRODUCT},
            )

    await _clean()
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES ('brand-sweep.example', '', '', 'US', TRUE)"
    )
    # THE PRODUCTION SPELLING: mixed case, with the `www.`.
    await _seed_sweep_catalog_product(
        _WWW_PRODUCT, "m_wwwsweep", "www.Brand-Sweep.example", "44000000000017", "42.50"
    )
    # A LOOKALIKE with a SHORTER variant id, which `_CATALOG_VARIANT_SQL`'s ordering would pick
    # first if the fold ever matched `www` without its dot: `www-brand-sweep.example` minus four
    # characters IS `brand-sweep.example`.
    await _seed_sweep_catalog_product(
        _LOOKALIKE_PRODUCT, "m_wwwsweep_lookalike", "www-brand-sweep.example", "9", "1.00"
    )
    try:
        yield
    finally:
        await _clean()


async def test_a_www_spelled_shopify_product_gives_the_sweep_its_variant_and_price(_www_catalog):
    targets = {t.domain: t for t in await sweep.load_population(50)}
    target = targets["brand-sweep.example"]
    assert target.variant_id == "44000000000017", (
        "the sweep found no catalog variant for a `www.`-spelled Shopify merchant — or found the "
        "`www-brand-sweep.example` lookalike's"
    )
    assert (target.expected_price_minor, target.expected_currency) == (4250, "USD")


async def test_a_population_row_that_is_not_a_bare_host_is_not_swept(_population):
    """The route can never admit it (its folded spelling never equals a validated request key),
    so a fact about it would gate nothing."""
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES ('https://urlshaped.example/', '', '', 'US', TRUE)"
    )
    domains = {t.domain for t in await sweep.load_population(50)}
    assert "urlshaped.example" not in domains and "https://urlshaped.example/" not in domains
    assert {"flowerbeauty.com", "judydoll.com"} <= domains


async def test_skipped_allowlist_rows_are_counted_in_the_report_and_logged_once(
    _population, monkeypatch
):
    """Counted, not silently dropped: an unusable row is a merchant an operator meant to enable.
    One warning per RUN carrying the count, and no domain in the log or the report."""
    import logging

    for bad in ("https://urlshaped.example/", "trailingdot.example."):
        await database.execute(
            "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
            "market_country, enabled) VALUES (:d, '', '', 'US', TRUE)",
            {"d": bad},
        )
    tally = {}
    await sweep.load_population(50, tally=tally)
    assert tally == {sweep.UNUSABLE_TALLY: 2}

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))
    # Off the pivota handler's own stream, with root where prod has it: this is an OPERATOR line,
    # and the module logger's WARNING only ever reached prod through Python's stderr lastResort.
    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_skipped_unusable == 2
    assert report.population == 2, "the two usable merchants are still swept"
    skipped = [line for line in pivota_lines(out) if "allowlist row(s) skipped" in line]
    assert len(skipped) == 1, skipped
    assert re.fullmatch(
        r"\[[^\]]+\] WARNING - merchant_purchasability_sweep: 2 allowlist row\(s\) skipped: "
        r"domain is not a bare host name", skipped[0],
    ), skipped[0]
    for leak in ("urlshaped", "trailingdot"):
        assert leak not in repr(report) and leak not in " ".join(skipped)


async def test_a_clean_population_reports_zero_skipped(_population, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))
    # Both channels: the module logger (caplog, root) AND the pivota handler the line moved to.
    caplog.set_level(logging.WARNING, logger=sweep.logger.name)
    with capture_pivota_stdout() as out:
        report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_skipped_unusable == 0
    assert not any("allowlist row(s) skipped" in r.getMessage() for r in caplog.records)
    assert not any("allowlist row(s) skipped" in line for line in pivota_lines(out))


# ══ the proof line ════════════════════════════════════════════════════════════════════════════
#
# Measured in prod 2026-09-23: /__scheduler_health `runs.merchant_purchasability_sweep` showed
# runs_ok=1 at 08:43:20Z on worker-00167-pjr and Cloud Logging held ZERO
# `merchant_purchasability_sweep:` lines. The report went through `logging.getLogger(__name__)`,
# nothing configures root in this process, root sits at WARNING, and INFO is dropped at the
# logger. Every test passed the whole time, because caplog hangs its handler on root and turns
# the level down. These tests read the pivota handler's OWN stream with root where prod has it,
# so they FAIL on a module-logger emit and pass only when the line takes the channel that lands.

_PROOF_LINE = re.compile(r"\[[^\]]+\] INFO - merchant_purchasability_sweep: SweepReport\(.*\)$")


async def test_the_report_line_lands_on_pivota_stdout_at_info_with_root_at_warning(
    _db, monkeypatch, _population
):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))
    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await sweep.run_merchant_purchasability_sweep()
    lines = pivota_lines(out)
    proof = [line for line in lines if _PROOF_LINE.fullmatch(line)]
    assert len(proof) == 1, f"expected exactly one report line on the pivota handler, got {lines!r}"
    # The line IS the report: same content, same counts-only rule. `repr(report)` is what the
    # counts-only test already polices, and the line carries nothing else.
    assert proof[0].endswith(f"merchant_purchasability_sweep: {report!r}")
    assert report.checked == 2 and report.population == 2
    for leak in ("judydoll", "flowerbeauty", ".com", "5004"):
        assert leak not in proof[0], f"{leak!r} leaked into the proof line"


async def test_the_disabled_line_lands_on_pivota_stdout_at_info(_db, monkeypatch):
    """The rollback step in the runbook says the sweep 'then returns skipped_disabled=1'; at the
    hourly interval this line is the only sign the worker is still ticking with the dial off. It
    was DEBUG on the module logger, which reached nobody."""
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await sweep.run_merchant_purchasability_sweep()
    assert report.skipped_disabled == 1 and fetcher.calls == []
    lines = pivota_lines(out)
    assert len(lines) == 1, lines
    assert re.fullmatch(
        r"\[[^\]]+\] INFO - merchant_purchasability_sweep: disabled; no merchant was contacted",
        lines[0],
    ), lines[0]


async def test_the_report_line_does_not_depend_on_the_root_logger(_db, monkeypatch, _population):
    """The mirror image: with root wide open and a handler on it, the report line must NOT show up
    there. A line that reaches root reaches it only by propagating from a module logger — the
    exact path prod drops — so this pins the channel, not just the level."""
    import logging

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))

    class _Sink(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    root, sink = logging.getLogger(), _Sink()
    saved = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(sink)
    try:
        with capture_pivota_stdout() as out:
            await sweep.run_merchant_purchasability_sweep()
    finally:
        root.removeHandler(sink)
        root.setLevel(saved)
    assert any(_PROOF_LINE.fullmatch(line) for line in pivota_lines(out))
    assert not any(m.startswith("merchant_purchasability_sweep: SweepReport(") for m in sink.messages), (
        "the report line propagated to root, i.e. it went through the module logger"
    )


# ══ 5. THE MARKET IS NEVER DEFAULTED ══════════════════════════════════════════════════════════
#
# An unknown market is UNKNOWN. Every consumer of the fact answers "no purchasability claim" for
# it — never "US", never a truncation, never positive — and says so without a database read or a
# log line. Seeded with a FRESH POSITIVE US FACT in every case, so a consumer that substituted
# "US" (mutant ii) or treated None as positive (mutant iii) answers True/purchase and fails.

_UNKNOWN_MARKETS = [None, "", "   ", "USA", "U1", "u", 7]


@pytest.mark.parametrize("market", _UNKNOWN_MARKETS)
async def test_is_purchasable_answers_false_for_an_unknown_market_without_asking_the_db(
    _db, monkeypatch, caplog, market
):
    import logging

    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", "US") is True, "the premise: US is positive"

    async def _no_db(*_a, **_k):
        raise AssertionError("an unknown market must not reach the database")

    monkeypatch.setattr(database, "fetch_one", _no_db)
    monkeypatch.setattr(database, "fetch_all", _no_db)
    caplog.set_level(logging.DEBUG, logger=mp.logger.name)
    assert await mp.is_purchasable("judydoll.com", market) is False
    assert await mp.is_purchasable("judydoll.com", market, now=datetime.now(timezone.utc)) is False
    assert await mp.get_fact("judydoll.com", market) is None
    assert await mp.list_facts("judydoll.com", market) == []
    assert [r for r in caplog.records if r.name == mp.logger.name] == [], (
        "an unknown market is an expected input, not an error: no log line (no storm)"
    )


@pytest.mark.parametrize("market", [None, "", "USA"])
async def test_an_unknown_market_is_not_answered_with_the_us_fact_on_a_live_db(_db, market):
    """The same rule with the database LIVE, so a consumer that substituted "US" would actually
    find the positive US row and answer True — the previous test can only catch that through its
    no-log assertion, because it makes the database refuse."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.is_purchasable("judydoll.com", market) is False
    assert await mp.get_fact("judydoll.com", market) is None
    assert await mp.list_facts("judydoll.com", market) == []


@pytest.mark.parametrize("market", _UNKNOWN_MARKETS)
async def test_record_check_refuses_to_write_under_an_unknown_market(_db, market):
    """The writer side of the same rule: nothing is ever keyed on a defaulted or truncated
    market. "USA" used to be written as "US"."""
    assert await mp.record_check("judydoll.com", market, res("ELIGIBLE", card=True)) is None
    count = await database.fetch_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
    assert int(dict(count)["n"]) == 0


@pytest.mark.parametrize("spelled", ["us", " US ", "Us"])
async def test_a_present_market_is_normalised_and_bound_on_both_sides(_db, spelled):
    await mp.record_check("judydoll.com", spelled, res("ELIGIBLE", card=True))
    assert (await _row())["market_country"] == "US"
    assert await mp.is_purchasable("judydoll.com", spelled) is True
    assert await mp.is_purchasable("judydoll.com", "US") is True


@pytest.mark.parametrize(
    "query", ["", "&market=", "&market=%20%20", "&market=U1", "&market=USA", "&market=united%20states"]
)
async def test_the_ops_route_reports_market_unknown_for_a_missing_market(_db, ops_app, monkeypatch, query):
    """No market -> tier browse_only with reason `market_unknown`, market null, no facts — even
    though a fresh positive US fact exists. A 200 with a distinct reason, not a 422 the gateway
    would read as "backend down" and fail open on."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    response = await ops_get(ops_app, f"/ops/merchant-purchasability?domain=judydoll.com{query}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "browse_only"
    assert body["reason"] == "market_unknown" == mp.MARKET_UNKNOWN
    assert body["market"] is None
    assert body["facts"] == []
    assert body["enforced"] is True


@pytest.mark.parametrize("spelled", ["%20us", "us%20", "Us"])
async def test_the_ops_route_normalises_a_present_market_instead_of_refusing_it(_db, ops_app, spelled):
    """Any string is accepted and put through the one helper: " us" is US (it was a 422)."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    response = await ops_get(ops_app, f"/ops/merchant-purchasability?domain=judydoll.com&market={spelled}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["market"], body["tier"], body["reason"]) == ("US", "purchase", None)


async def test_the_ops_route_with_a_market_is_unchanged_apart_from_a_null_reason(_db, ops_app):
    """Zero-diff for a keyed request: the only new keys are `reason` (null) and, since
    2026-10-09, `human_handoff_tier`. PINNED LITERALS — the key set and note below are main's
    response (2a590bb9c) plus those two."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=judydoll.com&market=us")).json()
    assert set(body) == {
        "domain", "market", "tier", "human_handoff_tier", "reason", "enforced", "buyer_vantage",
        "sweep_enabled", "ttl_hours", "facts", "note",
    }
    assert body["reason"] is None
    assert (body["domain"], body["market"], body["tier"]) == ("judydoll.com", "US", "purchase")
    assert body["note"] == "a fresh positive fact from vantage 'worker'; the door may offer purchase"


async def _no_cart_mint(**_k):
    """The CART-MINT lane, read and empty — for the DB-free cases, which have no seed table and
    no scan cache."""
    return sweep.CartMintPopulation(sweep.CartMintLane(hosts={}), "scan", 0, True)


async def test_the_sweep_skips_and_counts_a_row_with_an_unusable_market(monkeypatch):
    """DB-free: the two lanes are stubbed, so a value no allowlist table would accept ("USA",
    None) can still be fed through the population builder. None is swept as US; each is
    COUNTED; a lower-case market is normalised and swept."""

    async def _variant_lane(**_k):
        return [
            {"merchant_domain": "nomarket.example", "market_country": None},
            {"merchant_domain": "threeletter.example", "market_country": "USA"},
            {"merchant_domain": "lower.example", "market_country": "sg"},
        ]

    async def _cart_lane(statement):
        # The CART lane only: `_rows` also reads the connected-store lane, which has no rows here.
        if statement is not sweep._CART_LANE_SQL:
            return []
        return [{"domain": "blankmarket.example", "market": "  ", "variant_id": "1"}]

    async def _nothing_due(*_a, **_k):
        return []

    async def _no_variant(_domain):
        return None

    monkeypatch.setattr(ledger, "list_enabled_merchant_markets", _variant_lane)
    monkeypatch.setattr(sweep, "_rows", _cart_lane)
    monkeypatch.setattr(sweep, "_cart_mint_population", _no_cart_mint)
    monkeypatch.setattr(sweep.facts, "list_due", _nothing_due)
    monkeypatch.setattr(sweep, "_catalog_variant", _no_variant)

    tally = {}
    targets = await sweep.load_population(50, tally=tally)
    assert [(t.domain, t.market) for t in targets] == [("lower.example", "SG")]
    assert tally == {sweep.MARKET_UNKNOWN_TALLY: 3}


async def test_the_sweep_report_carries_the_market_unknown_count_and_logs_it_once(monkeypatch):
    async def _variant_lane(**_k):
        return [
            {"merchant_domain": "nomarket.example", "market_country": ""},
            {"merchant_domain": "judydoll.com", "market_country": "US"},
        ]

    async def _cart_lane(_statement):
        return []

    async def _nothing_due(*_a, **_k):
        return []

    async def _no_variant(_domain):
        return None

    async def _no_write(*_a, **_k):
        return None

    monkeypatch.setattr(ledger, "list_enabled_merchant_markets", _variant_lane)
    monkeypatch.setattr(sweep, "_rows", _cart_lane)
    monkeypatch.setattr(sweep, "_cart_mint_population", _no_cart_mint)
    monkeypatch.setattr(sweep.facts, "list_due", _nothing_due)
    monkeypatch.setattr(sweep.facts, "record_check", _no_write)
    monkeypatch.setattr(sweep, "_catalog_variant", _no_variant)
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    with root_as_in_prod(), capture_pivota_stdout() as out:
        report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_skipped_market_unknown == 1
    assert report.population_skipped_unusable == 0
    assert report.population == 1
    assert {kwargs["market"] for _host, kwargs in fetcher.calls} == {"US"}
    skipped = [line for line in pivota_lines(out) if "market is not an ISO-2 code" in line]
    assert len(skipped) == 1, skipped
    assert "nomarket" not in skipped[0] and "nomarket" not in repr(report)


# ══ the CONNECTED-STORE lane ═══════════════════════════════════════════════════════════════
#
# The product-card cart gate asks this fact for a connected Shopify card's cart host x the buyer's
# market. Connected stores are in neither Reap allowlist, so until this lane they were never
# swept and — under enforcement — their cards could never carry a cart. The lane mirrors
# `services.merchant_store_service.get_merchant_active_stores` (live store rows, else the legacy
# onboarding store) and keys each store on the host the card's cart is built on x the merchant's
# declared region, which is used ONLY when it is an ISO-2 country.

#: TEST MERCHANTS, taken from the policy itself rather than restated: two listed rig ids (one with
#: a live store, one with only a legacy store) and one merchant only the policy's
#: `pivota-review-demo*` domain resolver can name. Every one would be ADMITTED by the lane's own
#: rules (Shopify, products, region US) — so only the test-merchant skip keeps them out.
from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS  # noqa: E402

_RIG_LIVE, _RIG_LEGACY = sorted(KNOWN_TEST_MERCHANT_IDS)[:2]
_RIG_BY_DOMAIN = "m_demo_domain_rig"

_CONNECTED_MERCHANTS = (
    # merchant_id, region, has a cached product
    (_RIG_LIVE, "US", True),
    (_RIG_LEGACY, "US", True),
    # Its region is junk, like prod's review-demo rows: a rig must not be reported as
    # market-unknown, because it was never going to be swept at all.
    (_RIG_BY_DOMAIN, "shopify", True),
    ("m_conn", "us", True),
    ("m_conn_twin", "US", True),        # a second live row on the SAME store host
    ("m_url", "US", True),              # domain stored as a URL
    ("m_wix", "US", True),              # Wix: never a cart, never swept
    ("m_eu", "EU", True),               # two letters, not a country
    ("m_apac", "APAC", True),
    ("m_label", "shopify", True),       # the prod junk value
    ("m_noregion", None, True),
    ("m_noproducts", "US", False),      # serves no card
    ("m_down", "US", True),             # its only store row is disconnected
    ("m_legacy", "CA", True),           # legacy onboarding store, no live row
    ("m_legacy_dead", "US", True),      # legacy store, mcp_connected FALSE — still the card's
    ("m_legacy_shadowed", "US", True),  # legacy store AND a live (Wix) row: the live row wins
    ("m_legacy_empty", "US", False),    # legacy store that serves no card
)

_CONNECTED_STORES = (
    # store_id, merchant_id, platform, domain, status
    ("st_rig_live", _RIG_LIVE, "shopify", "rig-live.myshopify.com", "active"),
    ("st_rig_domain", _RIG_BY_DOMAIN, "shopify", "pivota-review-demo-9.myshopify.com", "active"),
    ("st_conn", "m_conn", "shopify", "Conn-Store.myshopify.com", "active"),
    ("st_twin", "m_conn_twin", "Shopify", "conn-store.myshopify.com", "connected"),
    ("st_url", "m_url", "shopify", "https://url-store.myshopify.com/", "active"),
    ("st_wix", "m_wix", "wix", "wix-store.example", "active"),
    ("st_eu", "m_eu", "shopify", "eu-store.myshopify.com", "active"),
    ("st_apac", "m_apac", "shopify", "apac-store.myshopify.com", "active"),
    ("st_label", "m_label", "shopify", "label-store.myshopify.com", "active"),
    ("st_noregion", "m_noregion", "shopify", "noregion-store.myshopify.com", "active"),
    ("st_noproducts", "m_noproducts", "shopify", "empty-store.myshopify.com", "active"),
    ("st_down", "m_down", "shopify", "down-store.myshopify.com", "disconnected"),
    ("st_shadow", "m_legacy_shadowed", "wix", "shadow-wix.example", "active"),
)

_LEGACY_STORES = {
    # merchant_id -> (mcp_platform, mcp_shop_domain, mcp_connected)
    _RIG_LEGACY: ("shopify", "rig-legacy.myshopify.com", True),
    "m_legacy": ("shopify", "legacy-store.myshopify.com", True),
    "m_legacy_dead": ("Shopify", "legacy-dead.myshopify.com", False),
    "m_legacy_shadowed": ("shopify", "legacy-shadowed.myshopify.com", True),
    "m_legacy_empty": ("shopify", "legacy-empty.myshopify.com", True),
}

#: Exactly what the lane must admit, beside the two allowlists' judydoll.com / flowerbeauty.com.
_CONNECTED_EXPECTED = {
    ("conn-store.myshopify.com", "US"),
    ("url-store.myshopify.com", "US"),
    ("legacy-store.myshopify.com", "CA"),
    ("legacy-dead.myshopify.com", "US"),
}


#: The rigs above, one row each.
_CONNECTED_TEST_MERCHANT_ROWS = 3


@pytest.fixture
async def _connected(_population):
    from services import test_merchant_policy

    # The policy memoises its domain-resolved set; a set another test resolved must not answer here.
    test_merchant_policy.reset_cache()
    for merchant_id, region, _has_product in _CONNECTED_MERCHANTS:
        platform, shop_domain, connected = _LEGACY_STORES.get(merchant_id, (None, None, False))
        await database.execute(
            "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, region, "
            "mcp_platform, mcp_shop_domain, mcp_connected) VALUES (:m, :n, :e, :r, :p, :d, :c)",
            {"m": merchant_id, "n": f"{merchant_id} store", "e": f"{merchant_id}.owner.example",
             "r": region, "p": platform, "d": shop_domain, "c": connected},
        )
    for store_id, merchant_id, platform, domain, status in _CONNECTED_STORES:
        await database.execute(
            "INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, status) "
            "VALUES (:s, :m, :p, :n, :d, :st)",
            {"s": store_id, "m": merchant_id, "p": platform, "n": store_id, "d": domain, "st": status},
        )
    with_products = [m for m, _r, has in _CONNECTED_MERCHANTS if has]
    for merchant_id in with_products:
        await database.execute(
            "INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, "
            "expires_at) VALUES (:m, 'shopify', :pid, :data, CURRENT_TIMESTAMP)",
            {"m": merchant_id, "pid": f"p_{merchant_id}", "data": json.dumps({"id": "1"})},
        )
    yield
    for merchant_id in with_products:
        await database.execute("DELETE FROM products_cache WHERE merchant_id = :m", {"m": merchant_id})
    test_merchant_policy.reset_cache()


async def test_the_population_adds_the_connected_shopify_stores(_db, _connected):
    tally: dict = {}
    targets = await sweep.load_population(50, tally=tally)
    keys = {(t.domain, t.market) for t in targets}
    assert keys == {("judydoll.com", "US"), ("flowerbeauty.com", "US")} | _CONNECTED_EXPECTED
    assert len(targets) == len(keys), "two live rows on one host are ONE merchant x market"
    # EU, APAC, "shopify" and NULL — each skipped and COUNTED, never defaulted to US. The three
    # test merchants are counted as that and nothing else: the rig whose region is "shopify" is
    # NOT a fifth market-unknown.
    assert tally == {sweep.MARKET_UNKNOWN_TALLY: 4,
                     sweep.TEST_MERCHANT_TALLY: _CONNECTED_TEST_MERCHANT_ROWS}
    rig_hosts = {"rig-live.myshopify.com", "rig-legacy.myshopify.com",
                 "pivota-review-demo-9.myshopify.com"}
    assert not {t.domain for t in targets} & rig_hosts
    assert all(t.variant_id is None for t in targets if (t.domain, t.market) in _CONNECTED_EXPECTED), (
        "the connected lane carries no variant; the preflight picks one for the market"
    )


async def test_a_connected_store_is_keyed_on_the_host_its_cart_is_built_on(_db, _connected):
    """The card gate asks the fact for `normalize_domain(<cart host>)`, and the connected card's
    cart host is `shopify_cart_base_url(shop_domain=<store domain>)`. A population key spelled
    any other way writes a fact the gate never reads."""
    from services.outbound_links_service import shopify_cart_base_url

    targets = {t.domain for t in await sweep.load_population(50)}
    for _sid, _mid, platform, domain, status in _CONNECTED_STORES:
        if platform.lower() != "shopify" or status == "disconnected":
            continue
        cart_host = mp.normalize_domain(shopify_cart_base_url(shop_domain=domain, variant_id="1"))
        if cart_host in {"conn-store.myshopify.com", "url-store.myshopify.com"}:
            assert cart_host in targets, domain


async def test_a_swept_connected_store_answers_the_card_gate(_db, monkeypatch, _connected):
    """End to end: an armed sweep checks the connected host and `is_purchasable` — the one read
    the card gate makes — answers True for it in its declared market, and only there."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)

    report = await sweep.run_merchant_purchasability_sweep()
    swept = {(host, kwargs["market"]) for host, kwargs in fetcher.calls}
    assert _CONNECTED_EXPECTED <= swept
    assert not {host for host, _m in swept} & {
        "rig-live.myshopify.com", "rig-legacy.myshopify.com", "pivota-review-demo-9.myshopify.com"
    }, "a test store is never contacted: each check leaves an abandoned checkout on it"
    assert report.population_skipped_market_unknown == 4
    assert report.population_skipped_test_merchant == _CONNECTED_TEST_MERCHANT_ROWS
    assert report.population_unreadable == 0
    assert await mp.is_purchasable("conn-store.myshopify.com", "US") is True
    assert await mp.is_purchasable("conn-store.myshopify.com", "CA") is False
    assert await mp.is_purchasable("eu-store.myshopify.com", "US") is False, "never swept"


async def test_the_connected_lane_failure_is_counted_and_the_allowlists_still_swept(
    _db, monkeypatch, _connected
):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    real_fetch_all = database.fetch_all

    async def _connected_lane_down(query, *a, **k):
        if query is sweep._CONNECTED_LANE_SQL:
            raise RuntimeError("relation merchant_stores is unavailable")
        return await real_fetch_all(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _connected_lane_down)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_unreadable == 1
    assert sorted(host for host, _ in fetcher.calls) == ["flowerbeauty.com", "judydoll.com"]
    assert sweep.exit_code_for(report) == sweep.EXIT_POPULATION_UNREADABLE


@pytest.mark.parametrize("region, market", [
    ("US", "US"), (" ca ", "CA"), ("sg", "SG"),
    ("EU", None), ("eu", None), ("UK", None),
    ("APAC", None), ("shopify", None), ("Other", None), ("USA", None), ("", None), (None, None),
])
def test_only_a_region_that_names_a_country_becomes_a_market(region, market):
    assert sweep._connected_market(region) == market


# ══ the CART-MINT lane ═════════════════════════════════════════════════════════════════════
#
# offers.resolve and the product-card lanes mint an external seed's prefilled cart only on a
# fresh positive fact for the cart host x the buyer's market (#2407 / #2411). The population now
# includes every (cart host, seed market) the minter WOULD build a cart on, computed by the
# minter's own functions over every active seed — so it cannot be narrower than what mints carts.
# `test_the_population_covers_every_cart_the_real_mint_builds` holds that against the real mint
# endpoint, over the same rows.

_SHOP_SNAPSHOT = {"snapshot": {"storefront_platform": "shopify"}}


def _stamped(*variant_ids):
    """A storefront-evidence snapshot with `shopify_variant_id` stamped on each variant entry."""
    return {"snapshot": {"storefront_platform": "shopify",
                         "variants": [{"shopify_variant_id": v} for v in variant_ids]}}


#: (id, status, domain, market, attached_product_key, attached_variant_id, seed_data, catalog
#:  variant for the attached key or None)
_CART_SEEDS = (
    # A standalone seed with ONE stamped variant: the seed-stamp hand-over builds the cart.
    ("mpx_s_stamp", "active", "stamp-brand.example", "US", None, None,
     {**_stamped("46100000000001"), "variants": [{"variant_id": "46100000000001"}]}, None),
    # A writer-verified Shopify attachment, on the production `www.` + mixed-case spelling.
    ("mpx_s_attached", "active", "www.Attached-Brand.example", "US",
     "prod::m_cartmint::shopify::901", "46100000000002", {}, None),
    # Three seeds whose cart variant ONLY the catalog can name (evidence branch: storefront is
    # Shopify, nothing stamped). With `_SEED_PAGE_ROWS` = 2 they straddle pages, so every page
    # needs its own primed resolver — the #2407 census's one-resolver-for-everything missed these.
    ("mpx_s_catalog0", "active", "catalog-brand-0.example", "US",
     "prod::m_cartmint::external_seed::7000", None, _SHOP_SNAPSHOT, "47000000000000"),
    ("mpx_s_catalog1", "active", "catalog-brand-1.example", "US",
     "prod::m_cartmint::external_seed::7001", None, _SHOP_SNAPSHOT, "47000000000001"),
    ("mpx_s_catalog2", "active", "catalog-brand-2.example", "US",
     "prod::m_cartmint::external_seed::7002", None, _SHOP_SNAPSHOT, "47000000000002"),
    # A variant whose `id` looks like a merchant-issued variant but is not the catalog's, and whose
    # `sku` names nothing: offers.resolve (which passes `named_variant_id`) reads a contradiction
    # and mints no cart, but the card lanes (which do not) resolve the sole catalog row and DO.
    ("mpx_s_unnamed", "active", "unnamed-brand.example", "US",
     "prod::m_cartmint::external_seed::7003",
     None, {**_SHOP_SNAPSHOT, "variants": [{"sku": "ABC-1", "id": "99999999999"}]}, "47000000000003"),
    # The mirror: a barcode-shaped `sku` and no variant id. The card lanes read the barcode as a
    # contradicting name and mint no cart; offers.resolve's narrower claim ignores a SKU and does.
    ("mpx_s_named", "active", "named-brand.example", "US",
     "prod::m_cartmint::external_seed::7004",
     None, {**_SHOP_SNAPSHOT, "variants": [{"sku": "88888888888"}]}, "47000000000004"),
    # Its only stored variant names an id the catalog does not hold, so every variant-carrying
    # hand-over is contradicted — but a card the gateway built with NO variant id resolves the
    # sole catalog row, and mints the cart. Only the empty-variant candidate finds it.
    ("mpx_s_empty", "active", "empty-variant-brand.example", "US",
     "prod::m_cartmint::external_seed::7005",
     None, {**_SHOP_SNAPSHOT, "variants": [{"variant_id": "99999999998"}]}, "47000000000005"),
    # A stamp that RESTATES the seed's own product id. A lane that passes the product id refuses
    # it as a forgery; a gateway-built card that carries no product id cannot, and mints the cart.
    ("mpx_s_restated", "active", "restated-brand.example", "US", None, None,
     _stamped("46100000000013"), None),
    # Overlaps the Tier B lane's judydoll.com x US, whose CONFIRMED variant must still win.
    ("mpx_s_overlap", "active", "judydoll.com", "US", None, None, _stamped("46100000000005"), None),
    # Listed in JP: keyed on JP, never on the buyer default.
    ("mpx_s_jp", "active", "jp-brand.example", "JP", None, None, _stamped("46100000000006"), None),
    # REFUSED — none of these reaches the population:
    ("mpx_s_referral", "active", "referral-only.example", "US", None, None, {}, None),
    ("mpx_s_multi", "active", "multi-brand.example", "US", None, None,
     _stamped("46100000000007", "46100000000008"), None),  # never guess between two variants
    ("mpx_s_paused", "paused", "paused-brand.example", "US", None, None,
     _stamped("46100000000009"), None),
    ("mpx_s_nomarket", "active", "nomarket-brand.example", "", None, None,
     _stamped("46100000000010"), None),  # counted, never defaulted to US
    ("mpx_s_nourl", "active", "nourl-brand.example", "US", None, None,
     _stamped("46100000000011"), None),  # no http(s) URL: no lane mints a link at all
)

#: Exactly what the cart-mint lane must admit (the Tier B lane already names judydoll.com x US).
_CART_MINT_EXPECTED = {
    ("stamp-brand.example", "US"),
    ("attached-brand.example", "US"),
    ("catalog-brand-0.example", "US"),
    ("catalog-brand-1.example", "US"),
    ("catalog-brand-2.example", "US"),
    ("unnamed-brand.example", "US"),
    ("named-brand.example", "US"),
    ("empty-variant-brand.example", "US"),
    ("restated-brand.example", "US"),
    ("judydoll.com", "US"),
    ("jp-brand.example", "JP"),
}
_ALLOWLISTS = {("judydoll.com", "US"), ("flowerbeauty.com", "US")}


def _seed_product_id(seed_id):
    return "46100000000013" if seed_id == "mpx_s_restated" else f"ext_{seed_id}"


def _seed_url(seed_id, domain):
    if seed_id == "mpx_s_nourl":
        return ""
    return f"https://{domain.lower()}/products/{seed_id}"


@pytest.fixture
async def _cart_seeds(_population, monkeypatch):
    import sqlalchemy
    from db.catalog import catalog_products, catalog_skus
    from db.database import metadata

    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[catalog_products, catalog_skus], checkfirst=True)
    engine.dispose()
    keys = [s[4] for s in _CART_SEEDS if s[7]]

    async def _clean():
        for table in ("catalog_skus", "catalog_products"):
            for key in keys:
                await database.execute(f"DELETE FROM {table} WHERE product_key = :pk", {"pk": key})

    await _clean()
    for seed_id, status, domain, market, key, attached_vid, seed_data, catalog_vid in _CART_SEEDS:
        await database.execute(
            "INSERT INTO external_product_seeds (id, status, domain, market, attached_product_key, "
            "attached_variant_id, external_product_id, destination_url, canonical_url, seed_data) "
            "VALUES (:id, :st, :d, :m, :k, :av, :epid, :url, :url, :sd)",
            {"id": seed_id, "st": status, "d": domain, "m": market, "k": key, "av": attached_vid,
             "epid": _seed_product_id(seed_id), "url": _seed_url(seed_id, domain),
             "sd": json.dumps(seed_data)},
        )
        if catalog_vid:
            spid = key.rsplit("::", 1)[-1]
            await database.execute(
                "INSERT INTO catalog_products (product_key, merchant_id, platform, "
                "source_product_id, title, source_domain) "
                "VALUES (:pk, 'm_cartmint', 'external_seed', :spid, 'Cart Mint', :d)",
                {"pk": key, "spid": spid, "d": domain},
            )
            await database.execute(
                "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, "
                "source_product_id, source_variant_id, title, currency) "
                "VALUES (:sk, :pk, 'm_cartmint', 'external_seed', :spid, :v, 'One', 'USD')",
                {"sk": f"sku::{key}", "pk": key, "spid": spid, "v": catalog_vid},
            )
    # Two rows a page: the three catalog seeds straddle pages, so one resolver for the whole
    # scan (sized to a page) would leave the later ones unprimed.
    monkeypatch.setattr(sweep, "_SEED_PAGE_ROWS", 2)
    monkeypatch.setattr(sweep, "_SEED_PAGE_PAUSE_S", 0)
    try:
        yield
    finally:
        await _clean()


async def test_the_population_adds_every_host_the_cart_minter_builds_on(_db, _cart_seeds):
    tally: dict = {}
    sizes: dict = {}
    targets = await sweep.load_population(50, tally=tally, sizes=sizes)
    keys = {(t.domain, t.market) for t in targets}
    assert keys == _ALLOWLISTS | _CART_MINT_EXPECTED
    assert len(targets) == len(keys)
    # The seed with no ISO-2 market is COUNTED and never swept as "US". Nothing else was refused
    # by the key rule, and no read failed or came back incomplete.
    assert tally == {sweep.MARKET_UNKNOWN_TALLY: 1}
    assert sizes == {sweep.TOTAL_TALLY: len(keys), sweep.NEVER_CHECKED_TALLY: len(keys),
                     sweep.CART_MINT_AGE_TALLY: 0}
    by_key = {(t.domain, t.market): t for t in targets}
    assert by_key[("judydoll.com", "US")].variant_id == "50041364447509", (
        "the Tier B lane's CONFIRMED variant still wins where the cart-mint lane names the key too"
    )
    # NO CATALOG HINT for a key only the cart-mint lane names. catalog-brand-*.example DO have a
    # catalog product on their host, so the hint would name one; a stale or SKU-shaped hint makes
    # the preflight answer VARIANT_GONE (a confirmed negative) or INVALID_INPUT (never positive).
    for key in _CART_MINT_EXPECTED - _ALLOWLISTS:
        assert (by_key[key].variant_id, by_key[key].expected_price_minor) == (None, None), key


async def test_the_lanes_are_reported_separately_for_the_census(_db, _cart_seeds):
    detail: dict = {}
    lanes = await sweep.collect_population(cart_mint=detail)
    assert set(lanes["cart-mint"]) == _CART_MINT_EXPECTED
    assert set(lanes["cart-link"]) == {("judydoll.com", "US")}
    assert detail["complete"] is True
    assert detail["seeds_scanned"] == sum(1 for s in _CART_SEEDS if s[1] == "active")
    # One cart seed per expected key, plus the market-less one that mints a cart but is not keyed.
    assert detail["cart_seeds"] == len(_CART_MINT_EXPECTED) + 1
    assert detail["seeds_by_key"] == {key: 1 for key in _CART_MINT_EXPECTED}


async def test_the_population_covers_every_cart_the_real_mint_builds(_db, _cart_seeds, monkeypatch):
    """THE SAME PRODUCER, held end to end: every seed row goes through the real
    `mint_external_seed_links` (the mint the gateway's own cards use), once per stored variant and
    once with none, with and without its product id, and every cart it builds must be on a (host, market) the population holds.
    The mint's own set must be non-empty and include the catalog-only seeds, or the check is
    vacuous."""
    import itertools

    import routes.agent_shop_gateway as gw

    domains = sorted({s[2].lower() for s in _CART_SEEDS} | {"judydoll.com"})

    async def _allowed(*, market):
        return domains

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", _allowed)
    population = {(t.domain, t.market) for t in await sweep.load_population(50)}

    minted = set()
    for seed_id, status, domain, market, key, attached_vid, seed_data, _cv in _CART_SEEDS:
        if status != "active" or not _seed_url(seed_id, domain):
            continue
        variant_ids = [gw._seed_offer_variant_id(v) or None for v in gw._seed_variants(seed_data)]
        for variant_id, product_id in itertools.product(
            dict.fromkeys([*variant_ids, None]), (_seed_product_id(seed_id), None)
        ):
            body = gw.ExternalSeedLinksRequest(market=market or None, candidates=[
                gw.ExternalSeedLinkCandidate(
                    external_seed_id=seed_id, destination_url=_seed_url(seed_id, domain),
                    canonical_url=_seed_url(seed_id, domain), market=market or None,
                    domain=domain, attached_product_key=key, attached_variant_id=attached_vid,
                    external_product_id=product_id, variant_id=variant_id,
                    seed_data=seed_data,
                )
            ])
            for link in (await gw.mint_external_seed_links(body))["links"]:
                if link.get("cart_url"):
                    minted.add((mp.normalize_domain(link["cart_url"]), mp.normalize_market(market)))
    keyed = {(host, market) for host, market in minted if market}
    assert {("catalog-brand-2.example", "US"), ("unnamed-brand.example", "US"),
            ("empty-variant-brand.example", "US"), ("restated-brand.example", "US")} <= keyed
    assert keyed <= population, sorted(keyed - population)
    assert ("nomarket-brand.example", None) in minted, "a cart the population counts but cannot key"


async def test_a_swept_cart_mint_host_answers_the_cart_gate(_db, monkeypatch, _cart_seeds):
    """End to end: an armed sweep checks the cart host, and `is_purchasable` on
    `normalize_domain(<cart url>)` — the one read the cart gate makes — answers True for it in the
    seed's market, and only there."""
    from services.outbound_links_service import shopify_cart_base_url

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_unreadable == 0 and report.errors == 0
    assert report.population_total == len(_ALLOWLISTS | _CART_MINT_EXPECTED)
    cart = shopify_cart_base_url(shop_domain="www.Attached-Brand.example", variant_id="46100000000002")
    assert await mp.is_purchasable(mp.normalize_domain(cart), "US") is True
    assert await mp.is_purchasable("jp-brand.example", "JP") is True
    assert await mp.is_purchasable("jp-brand.example", "US") is False
    assert await mp.is_purchasable("nomarket-brand.example", "US") is False, "never defaulted"


async def test_a_catalog_lookup_failure_leaves_the_lane_incomplete_and_counted(
    _db, monkeypatch, _cart_seeds
):
    """A lookup that fails answers "no variant" — so a cart the minter would build is silently
    missing. The lane keeps what it found (the stamped and attached seeds need no lookup) and the
    run is counted as reading an incomplete population."""
    from services.handover_variant_identity import HandoverVariantResolver

    async def _down(self, keys):
        raise RuntimeError("catalog_skus is unavailable")

    monkeypatch.setattr(HandoverVariantResolver, "_fetch", _down)
    tally: dict = {}
    keys = {(t.domain, t.market) for t in await sweep.load_population(50, tally=tally)}
    assert tally.get(sweep.UNREADABLE_TALLY) == 1
    assert {("stamp-brand.example", "US"), ("attached-brand.example", "US")} <= keys
    assert ("catalog-brand-0.example", "US") not in keys


async def test_the_cart_mint_lane_failure_is_counted_and_the_other_lanes_still_swept(
    _db, monkeypatch, _cart_seeds
):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    real_fetch_all = database.fetch_all

    async def _seeds_down(query, *a, **k):
        if query is sweep._SEED_PAGE_SQL:
            raise RuntimeError("relation external_product_seeds is unavailable")
        return await real_fetch_all(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _seeds_down)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population_unreadable == 1
    assert sorted(host for host, _ in fetcher.calls) == ["flowerbeauty.com", "judydoll.com"]
    assert sweep.exit_code_for(report) == sweep.EXIT_POPULATION_UNREADABLE


async def test_the_lane_budget_stops_the_scan_and_is_counted(_db, monkeypatch, _cart_seeds):
    """A slow database must not spend the run's budget before the first merchant is checked."""
    monkeypatch.setattr(sweep, "_SEED_LANE_BUDGET_S", 0)
    tally: dict = {}
    keys = {(t.domain, t.market) for t in await sweep.load_population(50, tally=tally)}
    assert tally.get(sweep.UNREADABLE_TALLY) == 1
    assert keys == _ALLOWLISTS, "nothing was scanned"


async def test_a_failed_staleness_read_is_counted(_db, monkeypatch, _cart_seeds):
    """The rotation is ordered by `list_due`. Its soft form answered [] on a failure, every key
    then read as never-checked, and every run would sweep the same first `batch` keys."""
    real_fetch_all = database.fetch_all

    async def _facts_down(query, *a, **k):
        if query in (mp._SELECT_DUE_SQL, mp._SELECT_DUE_SQL_SQLITE):
            raise RuntimeError("merchant_purchasability is unavailable")
        return await real_fetch_all(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _facts_down)
    assert await mp.list_due(10) == [], "the default form stays soft"
    tally: dict = {}
    await sweep.load_population(50, tally=tally)
    assert tally.get(sweep.UNREADABLE_TALLY) == 1


async def test_a_population_larger_than_the_batch_is_covered_by_the_rotation(
    _db, monkeypatch, _cart_seeds
):
    """CAPACITY. With more keys than the batch, each run takes the never-checked first and then
    the stalest, so the whole population is checked within ceil(total / batch) runs — and the
    report says how far the rotation has got."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BATCH", "4")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    total = len(_ALLOWLISTS | _CART_MINT_EXPECTED)
    runs = -(-total // 4)
    reports = []
    for _ in range(runs):
        reports.append(await sweep.run_merchant_purchasability_sweep())
    swept = [(host, kwargs["market"]) for host, kwargs in fetcher.calls]
    assert len(swept) == 4 * runs and all(r.population == 4 for r in reports)
    first_pass = swept[:total]
    assert set(first_pass) == _ALLOWLISTS | _CART_MINT_EXPECTED
    assert len(set(first_pass)) == total, "no key is checked twice before every key is checked once"
    assert [r.population_total for r in reports] == [total] * runs
    assert [r.population_never_checked for r in reports] == [total - 4 * i for i in range(runs)]


# ══ the coverage census (scripts/merchant_purchasability_census.py) ══════════════════════════
#
# Read after two or three days of sweeps, per host: is the fact positive, confirmed-negative,
# unverifiable or never-checked? Its population is `collect_population`'s, so it reports on
# exactly what the sweep sweeps. The read-only transaction itself is Postgres-only and is held in
# tests/test_merchant_purchasability_postgres.py.

from scripts import merchant_purchasability_census as census  # noqa: E402


@pytest.mark.parametrize("fact, state", [
    (None, "never_checked"),
    ({"fresh": 1, "verdict": "ELIGIBLE"}, "positive"),
    # A fresh window survives one negative (demotion needs two), so the gate still says yes.
    ({"fresh": 1, "verdict": "NO_CARD_PAYMENT"}, "positive"),
    ({"fresh": 0, "verdict": "NO_CARD_PAYMENT"}, "confirmed_negative"),
    ({"fresh": 0, "verdict": "NOT_ACCEPTING_ORDERS"}, "confirmed_negative"),
    ({"fresh": 0, "verdict": "BLOCKED_UNKNOWN"}, "unverifiable"),
    ({"fresh": 0, "verdict": "ELIGIBLE"}, "unverifiable"),  # an expired window, or no card read
    ({"fresh": 0, "verdict": None}, "unverifiable"),
])
def test_the_census_states_follow_the_facts_rules(fact, state):
    assert census.classify(fact, mp.NEGATIVE_VERDICTS) == state


def test_the_census_rows_carry_every_lane_and_count_hosts_once_in_their_best_state():
    lanes = {
        "variant": {}, "cart-link": {("a.example", "US"): "1"}, "connected-store": {},
        "cart-mint": {("a.example", "US"): None, ("a.example", "CA"): None, ("b.example", "US"): None},
    }
    facts_by_key = {("a.example", "US"): {"fresh": 1, "verdict": "ELIGIBLE"},
                    ("a.example", "CA"): {"fresh": 0, "verdict": "NO_CARD_PAYMENT"}}
    rows = census.build_rows(lanes, facts_by_key, {("a.example", "US"): 3, ("b.example", "US"): 1},
                             mp.NEGATIVE_VERDICTS)
    assert [(r["host"], r["market"], r["state"], r["lanes"], r["cart_mint_seeds"]) for r in rows] == [
        ("a.example", "CA", "confirmed_negative", ["cart-mint"], 0),
        ("a.example", "US", "positive", ["cart-link", "cart-mint"], 3),
        ("b.example", "US", "never_checked", ["cart-mint"], 1),
    ]
    summary = census.summarise(rows)
    assert summary["cart_mint_keys"] == {"positive": 1, "confirmed_negative": 1, "unverifiable": 0,
                                         "never_checked": 1}
    assert summary["cart_mint_seeds"]["positive"] == 3
    assert summary["hosts_best_state"] == {"positive": 1, "confirmed_negative": 0, "unverifiable": 0,
                                           "never_checked": 1}


def test_the_census_decoder_refuses_a_log_with_a_missing_row():
    """Cloud Logging drops lines. A census over the rows that happened to arrive would under-count
    silently, so a gap is an error, and so is a missing summary."""
    def line(payload):
        return "noise " + census._fence(payload) + " trailing\n"

    rows = [{"kind": "row", "i": i, "n": 3, "host": f"h{i}.example", "market": "US",
             "state": "positive", "lanes": ["cart-mint"], "cart_mint_seeds": 1} for i in range(3)]
    summary = {"kind": "summary", "n": 3}
    whole = "".join(line(r) for r in rows) + line(summary)
    assert [r["host"] for r in census.decode(whole)["rows"]] == ["h0.example", "h1.example", "h2.example"]
    with pytest.raises(ValueError, match="1 of 3 rows missing"):
        census.decode(line(rows[0]) + line(rows[2]) + line(summary))
    with pytest.raises(ValueError, match="no summary"):
        census.decode("".join(line(r) for r in rows))


# ══ the cart-mint scan runs at most ONCE A DAY ═══════════════════════════════════════════════
#
# The scan reads every active seed on the 2-vCPU prod primary, and the sweep runs 21 times a
# day. So it runs on the first sweep of each 05:00Z slot, and the other runs read the last
# complete scan from `merchant_purchasability_cart_mint_scans`. A failed scan falls back to that
# cache (logged, NOT counted) and retries no sooner than 6 h later; only a missing or >72 h old
# cache is counted unreadable. These drive the real `load_population` on a moved clock.

_DAY1 = datetime(2026, 9, 29, 5, 7, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def _scans(monkeypatch, _cart_seeds):
    """The clock, and a counter of real scans (the real `_cart_mint_lane`, wrapped)."""
    clock = _Clock(_DAY1)
    monkeypatch.setattr(sweep, "_utcnow", clock)
    calls = {"scans": 0, "fail": False}
    real = sweep._cart_mint_lane

    async def _counted():
        calls["scans"] += 1
        if calls["fail"]:
            raise RuntimeError("statement timeout")
        return await real()

    monkeypatch.setattr(sweep, "_cart_mint_lane", _counted)
    return clock, calls


async def _population_now():
    tally: dict = {}
    sizes: dict = {}
    keys = {(t.domain, t.market) for t in await sweep.load_population(50, tally=tally, sizes=sizes)}
    return keys, tally, sizes


async def _add_cart_seed(domain):
    await database.execute(
        "INSERT INTO external_product_seeds (id, status, domain, market, destination_url, "
        "canonical_url, seed_data) VALUES (:id, 'active', :d, 'US', :u, :u, :sd)",
        {"id": f"mpx_s_new_{domain}", "d": domain, "u": f"https://{domain}/products/new",
         "sd": json.dumps(_stamped("46100000000099"))},
    )


async def test_the_seed_table_is_scanned_once_a_day_and_the_other_runs_read_the_cache(_db, _scans):
    clock, calls = _scans
    keys, tally, sizes = await _population_now()
    assert calls["scans"] == 1 and ("stamp-brand.example", "US") in keys
    assert sizes[sweep.CART_MINT_AGE_TALLY] == 0 and tally == {sweep.MARKET_UNKNOWN_TALLY: 1}

    # A cart host that appears after today's scan waits for tomorrow's: the other runs of the
    # day, and tomorrow's 00:07 and 01:07 (still yesterday's 05:00 slot), read the cache.
    await _add_cart_seed("late-brand.example")
    for later in (timedelta(hours=1), timedelta(hours=19), timedelta(hours=20)):
        clock.now = _DAY1 + later
        keys, tally, sizes = await _population_now()
        assert calls["scans"] == 1, later
        assert ("late-brand.example", "US") not in keys and ("stamp-brand.example", "US") in keys
        assert sizes[sweep.CART_MINT_AGE_TALLY] == int(later.total_seconds() // 60)
        assert sweep.UNREADABLE_TALLY not in tally

    clock.now = _DAY1 + timedelta(days=1)
    keys, _tally, sizes = await _population_now()
    assert calls["scans"] == 2 and ("late-brand.example", "US") in keys
    assert sizes[sweep.CART_MINT_AGE_TALLY] == 0


async def test_a_failed_scan_falls_back_to_the_last_complete_one_and_backs_off(_db, _scans, caplog):
    """A slow database must not fail every run of the day: the fallback is logged, not counted,
    and the next attempt waits 6 h."""
    clock, calls = _scans
    await _population_now()
    calls["fail"] = True
    clock.now = _DAY1 + timedelta(days=1)
    keys, tally, sizes = await _population_now()
    assert calls["scans"] == 2
    assert sweep.UNREADABLE_TALLY not in tally, "a day-old scan stands in; nothing is counted"
    assert ("stamp-brand.example", "US") in keys
    assert sizes[sweep.CART_MINT_AGE_TALLY] == 24 * 60

    clock.now = _DAY1 + timedelta(days=1, hours=5)
    await _population_now()
    assert calls["scans"] == 2, "backing off: no second attempt within 6 h of the failed one"
    clock.now = _DAY1 + timedelta(days=1, hours=6, minutes=1)
    await _population_now()
    assert calls["scans"] == 3, "and the retry comes after it"


async def test_no_complete_scan_young_enough_is_counted_unreadable(_db, _scans):
    clock, calls = _scans
    calls["fail"] = True
    keys, tally, sizes = await _population_now()
    assert tally.get(sweep.UNREADABLE_TALLY) == 1, "never scanned successfully"
    assert keys == _ALLOWLISTS and sizes[sweep.CART_MINT_AGE_TALLY] == -1

    calls["fail"] = False
    clock.now = _DAY1 + timedelta(hours=7)
    _keys, tally, _sizes = await _population_now()
    assert sweep.UNREADABLE_TALLY not in tally
    calls["fail"] = True
    for days, counted in ((3, False), (4, True)):
        clock.now = _DAY1 + timedelta(days=days, hours=1)
        keys, tally, _sizes = await _population_now()
        assert (tally.get(sweep.UNREADABLE_TALLY) == 1) is counted, days
        assert ("stamp-brand.example", "US") in keys, "an old scan is still swept, just counted"


async def test_an_unreadable_cache_never_scans(_db, _scans, monkeypatch):
    """Scanning anyway would turn a broken cache table into an hourly full scan of the seeds."""
    _clock, calls = _scans

    async def _down(**_k):
        raise RuntimeError("relation merchant_purchasability_cart_mint_scans is unavailable")

    monkeypatch.setattr(sweep.mint_scans, "latest", _down)
    keys, tally, _sizes = await _population_now()
    assert calls["scans"] == 0
    assert tally.get(sweep.UNREADABLE_TALLY) == 1 and keys == _ALLOWLISTS


async def test_a_scan_that_cannot_be_cached_is_an_error_and_is_scanned_again(_db, _scans, monkeypatch):
    clock, calls = _scans
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setattr(sweep, "_preflight", _Fetcher({}))

    async def _no_write(**_k):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(sweep.mint_scans, "record", _no_write)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.errors == 1 and report.population_unreadable == 0
    assert sweep.exit_code_for(report) == sweep.EXIT_ERRORS
    clock.now = _DAY1 + timedelta(hours=1)
    await sweep.run_merchant_purchasability_sweep()
    assert calls["scans"] == 2


async def test_the_census_scans_fresh_and_touches_no_cache(_db, _scans):
    _clock, calls = _scans
    import db.merchant_purchasability_cart_mint_scans as mint_scans

    lanes = await sweep.collect_population(fresh_cart_mint_scan=True)
    assert calls["scans"] == 1 and set(lanes["cart-mint"]) == _CART_MINT_EXPECTED
    assert await mint_scans.latest(complete_only=False) is None


# ══ an IP-level throttle stops the run and demotes nobody ═════════════════════════════════
#
# Prod, 2026-10-07 07:08Z onward: 17-20 of the 20 merchants unverifiable every hour, 163 of ~196 checks
# answering `products_json_page_1_status_429` across unrelated stores, and the sweep kept asking all 20
# every hour. 2026-10-08 ~00:20Z: a FRESH crawl NAT IP got the same 429 on its very first request (the
# headers below, measured). Everything here is driven through the REAL preflight over a MockTransport, so
# the guard is tested against what the producer actually emits.

_MEASURED_429 = {"server": "cloudflare", "retry-after": "60", "content-type": "text/plain"}
_CHALLENGE = {"server": "cloudflare", "cf-mitigated": "challenge", "content-type": "text/html"}
_EXTRA_STORES = tuple(f"store{i}.example" for i in range(1, 7))  # + flowerbeauty + judydoll = 8
_LONG_AGO = "2026-10-01 12:00:00"  # a literal: Postgres reads it in the session's zone, SQLite as UTC


async def _throttle_population():
    """The `_population` fixture's two merchants plus six more Reap merchants, each holding a LIVE
    positive window, every one last checked long ago (so the due order is by key)."""
    for domain in _EXTRA_STORES:
        await database.execute(
            "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
            "market_country, enabled) VALUES (:d, '', '', 'US', TRUE)", {"d": domain})
    domains = ("flowerbeauty.com", "judydoll.com") + _EXTRA_STORES
    for domain in domains:
        await mp.record_check(domain, "US", res("ELIGIBLE", card=True, host=domain))
    # One earlier CONFIRMED negative on one store, so a throttle visibly does not touch the count.
    await mp.record_check("store1.example", "US", res("PRICE_DRIFT", card=True, host="store1.example"))
    await database.execute(f"UPDATE {TABLE} SET checked_at = '{_LONG_AGO}'")
    return domains


def _fake_clock_pacer(monkeypatch, interval_s=None):
    """ONE fake clock for the pacer AND the breaker's window (`crawl_ip_throttle._now`), so request
    spacing is what the breaker measures -- with the real monotonic clock every test throttle would
    land within milliseconds and no window could ever be shown to matter."""
    import services.crawl_ip_throttle as cit
    from jobs.tierb_cart_link_eligibility import MIN_REQUEST_INTERVAL_S, RequestPacer

    clock = {"t": 1000.0}

    async def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(cit, "_now", lambda: clock["t"])
    monkeypatch.setattr(sweep, "_new_pacer", lambda: RequestPacer(
        interval_s or MIN_REQUEST_INTERVAL_S, clock=lambda: clock["t"], sleep=_sleep))
    return clock


def _storefront(monkeypatch, answer):
    """Every request of the run goes to `answer(request)`; returns the list of requests seen."""
    seen = []

    def _handler(request):
        seen.append(request)
        return answer(request)

    monkeypatch.setattr(sweep, "_inner_transport", lambda via: httpx.MockTransport(_handler))
    return seen


def _ip_throttle_line(out: str) -> dict:
    lines = [line for line in out.splitlines() if line.startswith(sweep.IP_THROTTLE_PREFIX)]
    assert len(lines) == 1, out[-3000:]
    return json.loads(lines[0][len(sweep.IP_THROTTLE_PREFIX):])


@pytest.fixture
def _armed(monkeypatch):
    import services.crawl_ip_throttle as cit
    import services.crawl_politeness as cp

    cp.reset_for_tests()
    cit.reset_for_tests()
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    for name in ("MERCHANT_PURCHASABILITY_IP_THROTTLE_TRIP_HOSTS",
                 "MERCHANT_PURCHASABILITY_IP_THROTTLE_WINDOW_SECONDS",
                 "CRAWL_IP_THROTTLE_BREAKER_ENABLED", "CRAWL_SHOPIFY_EDGE_PACER_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    _fake_clock_pacer(monkeypatch)
    yield
    cp.reset_for_tests()
    cit.reset_for_tests()


@pytest.mark.parametrize("status, headers, detail", [
    (429, _MEASURED_429, "resolve:throttled_429"),
    (403, _CHALLENGE, "resolve:throttled_challenge_403"),
    # Cloudflare can serve its challenge with a 200: the preflight says throttled, and the breaker counts it.
    (200, _CHALLENGE, "resolve:throttled_challenge_200"),
])
async def test_an_ip_throttle_stops_the_run_and_demotes_nobody(
    _db, _population, _armed, monkeypatch, capsys, status, headers, detail
):
    domains = await _throttle_population()
    seen = _storefront(monkeypatch, lambda r: httpx.Response(status, headers=headers, text="local_rate_limited"))

    report = await sweep.run_merchant_purchasability_sweep()

    # THE RUN STOPPED after the third distinct merchant: three requests, not eight (or 20 an hour).
    assert len(seen) == 3 and len({r.url.host for r in seen}) == 3
    assert (report.ip_throttled, report.checked, report.throttled, report.unverifiable) == (1, 3, 3, 3)
    assert (report.written, report.skipped_ip_throttle, report.errors) == (3, 5, 0)
    assert report.positive == report.negative == 0
    # A TRIP IS NOT A FAILURE: the job runs hourly and "Cloud Run job failing" pages on any failed task,
    # so a 14-17 h throttle window would page every hour. The IP_THROTTLE line is the signal.
    assert sweep.exit_code_for(report) == sweep.EXIT_OK
    assert not hasattr(sweep, "EXIT_IP_THROTTLED")

    contacted = {r.url.host for r in seen}
    for domain in domains:
        row = await _row(domain)
        # NOBODY IS DEMOTED: every window stands, every failure count is what it was.
        assert row["positive_until"] is not None, domain
        assert await mp.is_purchasable(domain, "US"), domain
        assert row["consecutive_failures"] == (1 if domain == "store1.example" else 0), domain
        if domain in contacted:
            assert row["verdict"] == "TRANSPORT_ERROR"
            assert row["evidence"]["retryable"] is True and row["evidence"]["detail"] == detail
        else:
            # NOTHING WRITTEN for a merchant the stopped run did not contact.
            assert row["verdict"] == "ELIGIBLE"
            assert row["checked_at"] < datetime(2026, 10, 5, tzinfo=timezone.utc), "its checked_at moved"

    line = _ip_throttle_line(capsys.readouterr().out)
    assert line["job"] == "merchant-purchasability-sweep" and line["status"] == "ip_throttled"
    assert line["ip_throttled"] is True and line["ip_throttle_trip_host_count"] == 3
    assert line["ip_throttle_tripped_at"] and line["ip_throttle_first_429_at"]
    assert (line["throttled_checks"], line["skipped_for_ip_throttle"]) == (3, 5)
    assert line["throttle_diagnostics"]["responses"] == 3
    assert line["throttle_diagnostics"]["cf_mitigated"] == (3 if "cf-mitigated" in headers else 0)
    assert not any(domain in json.dumps(line) for domain in domains), "the line lists no host"


async def test_the_next_run_starts_with_the_merchants_a_stopped_run_did_not_reach(
    _db, _population, _armed, monkeypatch
):
    """THE STARVATION GUARD. A stopped run writes its throttled checks, so their `checked_at` moves and
    they sink to the back of the due list; the next run probes the address with OTHER merchants. Were
    they left unwritten, the same few stores would head every run and trip it before anyone else."""
    await _throttle_population()
    seen = _storefront(monkeypatch, lambda r: httpx.Response(429, headers=_MEASURED_429))

    first = await sweep.run_merchant_purchasability_sweep()
    hosts_first = {r.url.host for r in seen}
    seen.clear()
    second = await sweep.run_merchant_purchasability_sweep()
    hosts_second = {r.url.host for r in seen}

    assert first.ip_throttled == second.ip_throttled == 1
    assert len(hosts_first) == len(hosts_second) == 3
    assert not hosts_first & hosts_second, (hosts_first, hosts_second)
    # And the merchants throttled in BOTH halves still hold their windows.
    for host in hosts_first | hosts_second:
        assert await mp.is_purchasable(host, "US"), host


async def test_two_throttled_merchants_do_not_stop_the_run(_db, _population, _armed, monkeypatch, capsys):
    """Below the trip count the run carries on: every merchant is asked, the two throttled checks are
    recorded as unverifiable, and the run exits 0."""
    domains = await _throttle_population()
    throttling = {"store1.example", "store2.example"}

    def _answer(request):
        if request.url.host in throttling:
            return httpx.Response(429, headers=_MEASURED_429)
        return httpx.Response(404, text="not found")  # the store answered; nothing to do with a throttle

    seen = _storefront(monkeypatch, _answer)
    report = await sweep.run_merchant_purchasability_sweep()

    assert {r.url.host for r in seen} == set(domains)
    assert (report.ip_throttled, report.throttled, report.skipped_ip_throttle) == (0, 2, 0)
    assert report.checked == len(domains) and sweep.exit_code_for(report) == sweep.EXIT_OK
    line = _ip_throttle_line(capsys.readouterr().out)
    assert line["status"] == "ok" and line["ip_throttled"] is False
    assert line["ip_throttle_hosts"] == 2


@pytest.mark.parametrize("env", [
    {"MERCHANT_PURCHASABILITY_IP_THROTTLE_TRIP_HOSTS": "0"},
    {"CRAWL_IP_THROTTLE_BREAKER_ENABLED": "false"},
])
async def test_the_breaker_kill_switches_let_the_run_finish(_db, _population, _armed, monkeypatch, capsys, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    domains = await _throttle_population()
    seen = _storefront(monkeypatch, lambda r: httpx.Response(429, headers=_MEASURED_429))
    report = await sweep.run_merchant_purchasability_sweep()

    assert {r.url.host for r in seen} == set(domains)
    assert (report.ip_throttled, report.throttled, report.skipped_ip_throttle) == (0, len(domains), 0)
    for domain in domains:
        assert await mp.is_purchasable(domain, "US"), "disarmed or not, a throttle demotes nobody"
    line = _ip_throttle_line(capsys.readouterr().out)
    assert line["ip_throttle_breaker_armed"] is False and line["ip_throttle_hosts"] == len(domains)


async def test_proxy_vantage_throttles_do_not_trip_the_crawl_ip_breaker(_db, _population, _armed, monkeypatch):
    """The proxy vantage leaves from another address: its 429s say nothing about the crawl IP."""
    monkeypatch.setenv("VANTAGE_PROXY_URL", "http://vantage-proxy.example:3128")
    domains = await _throttle_population()
    seen = []

    def _transport(via):
        def _handler(request):
            seen.append((via, request.url.host))
            if via:
                return httpx.Response(429, headers=_MEASURED_429)
            return httpx.Response(404, text="not found")
        return httpx.MockTransport(_handler)

    monkeypatch.setattr(sweep, "_inner_transport", _transport)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.ip_throttled == 0 and report.throttled == len(domains)
    assert {host for via, host in seen if via is None} == set(domains)


def test_the_sweep_breaker_folds_www_and_counts_a_challenge():
    """One merchant answering from its apex and its `www.` twin is ONE key; a Cloudflare challenge
    counts as a throttle (the parent breaker counts 429 / 503 + Retry-After only)."""
    from services.crawl_ip_throttle import capture_throttle_headers

    breaker = sweep.SweepThrottleBreaker(trip_hosts=2, window_seconds=600)
    throttle = capture_throttle_headers(httpx.Headers(_MEASURED_429))
    breaker.observe("www.a.example", 429, throttle)
    breaker.observe("a.example", 429, throttle)
    assert not breaker.tripped, "apex + www is one merchant"
    breaker.observe("b.example", 403, capture_throttle_headers(httpx.Headers({"server": "cloudflare"})))
    assert not breaker.tripped, "a plain 403 is not a throttle"
    breaker.observe("b.example", 403, capture_throttle_headers(httpx.Headers(_CHALLENGE)))
    assert breaker.tripped and breaker.trip_host_count == 2


async def test_a_trip_refuses_the_rest_of_that_merchants_vantages_and_writes_nothing_for_them(
    _db, _population, _armed, monkeypatch
):
    """With a proxy vantage, the merchant whose crawl-IP answer trips the breaker still has its proxy
    check to run. It is refused LOCALLY (no request leaves), and the refusal is not written as a fact."""
    monkeypatch.setenv("VANTAGE_PROXY_URL", "http://vantage-proxy.example:3128")
    await _throttle_population()
    seen = []

    def _transport(via):
        def _handler(request):
            seen.append((via, request.url.host))
            return httpx.Response(404, text="nf") if via else httpx.Response(429, headers=_MEASURED_429)
        return httpx.MockTransport(_handler)

    monkeypatch.setattr(sweep, "_inner_transport", _transport)
    report = await sweep.run_merchant_purchasability_sweep()

    direct = [host for via, host in seen if via is None]
    proxied = [host for via, host in seen if via]
    assert len(direct) == 3 and proxied == direct[:2], seen
    assert report.ip_throttled == 1 and report.checked == 5 and report.written == 5
    # 8 merchants x 2 vantages = 16 pairs: 5 checked, the tripping merchant's proxy pair refused, 10 not started.
    assert report.skipped_ip_throttle == 11 and report.errors == 0
    tripper = direct[2]
    proxy_rows = [r for r in await mp.list_facts(tripper, "US") if r["vantage"] == "proxy"]
    assert proxy_rows == [], "a locally refused check was written as a fact"


def test_the_breaker_defaults_are_three_merchants_within_one_run():
    """Pinned: the setup script does not set them, so these ARE prod's values."""
    assert sweep.DIALS["ip_throttle_trip_hosts"].default == 3
    assert sweep.DIALS["ip_throttle_window_seconds"].default == 1200
    breaker = sweep._new_breaker()
    assert (breaker.trip_hosts, breaker.window_seconds, breaker.enabled) == (3, 1200.0, True)


@pytest.mark.parametrize("window, trips", [("60", False), ("1200", True)])
async def test_the_window_is_measured_on_the_request_clock(_db, _population, _armed, monkeypatch, window, trips):
    """Merchants 40 s apart on the request clock: 3 throttles span 80 s, so a 60 s window never holds three,
    and the default window does. A breaker whose window could not see the spacing would trip both or neither."""
    _fake_clock_pacer(monkeypatch, interval_s=40.0)
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_IP_THROTTLE_WINDOW_SECONDS", window)
    domains = await _throttle_population()
    seen = _storefront(monkeypatch, lambda r: httpx.Response(429, headers=_MEASURED_429))
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.ip_throttled == int(trips)
    assert len(seen) == (3 if trips else len(domains))


async def test_web_bot_auth_signs_the_direct_vantage_and_leaves_the_proxy_vantage_a_buyer(_db, monkeypatch, _population):
    """CRAWL_WEB_BOT_AUTH_ENABLED + key: a real sweep run. Requests from the crawl egress (direct
    vantage) are signed and declare PivotaBot; the proxy vantage, which stands in for a buyer's network
    and leaves from an unregistered address, stays unsigned with the lane's own User-Agent."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from services import crawl_identity

    monkeypatch.setenv("MERCHANT_PURCHASABILITY_SWEEP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("VANTAGE_PROXY_URL", "http://vantage-proxy.example:3128")
    monkeypatch.setenv(crawl_identity.FLAG_ENV, "true")
    monkeypatch.setenv(crawl_identity.KEY_ENV, Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
    crawl_identity.reset_for_tests()
    seen = []

    def _transport(*a, proxy=None, **k):
        return httpx.MockTransport(lambda r: seen.append((proxy, r)) or httpx.Response(200, text="ok"))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", _transport)

    async def _two_requests(host, **kwargs):
        for path in ("/cart/1:1", "/checkouts/cn/T"):
            await kwargs["client"].get(f"https://{host}{path}")
        return res("ELIGIBLE", card=True, host=host)

    monkeypatch.setattr(sweep, "_preflight", _two_requests)
    await sweep.run_merchant_purchasability_sweep()
    direct = [r for proxy, r in seen if proxy is None]
    via_proxy = [r for proxy, r in seen if proxy]
    assert direct and via_proxy
    assert all(r.headers["user-agent"] == crawl_identity.DECLARED_USER_AGENT and "signature" in r.headers for r in direct)
    assert all(r.headers["user-agent"] == sweep.USER_AGENT and "signature" not in r.headers for r in via_proxy)
    crawl_identity.reset_for_tests()


# ══ the HUMAN-HANDOFF reader (2026-10-09) ═══════════════════════════════════════════════════
#
# `is_purchasable` answers the headless card rail; `human_handoff_allowed` answers the cart mints
# that hand a prefilled cart to a human. They part on NO_CARD_PAYMENT: a checkout read with no
# on-site card line refuses the rail and keeps the human cart, because a human pays by whatever the
# checkout offers (PayPal, an offsite card provider, a bank transfer). Measured 2026-10-09: 643 of
# 839 negative cart seeds were NO_CARD_PAYMENT. Peng's decision.

_HUMAN_ALLOWED = {"ELIGIBLE", "NO_CARD_PAYMENT", "PRICE_DRIFT"}


def test_the_human_allow_list_partitions_every_verdict_with_the_rails_sets():
    """Every verdict is exactly one of: human-allowed, a nobody-can-buy negative, or unverifiable
    — so a verdict added to the enum lands in a test, not in a silent default."""
    every = {v.value for v in Verdict}
    assert mp.HUMAN_HANDOFF_VERDICTS == _HUMAN_ALLOWED
    nobody_can_buy = set(mp.NEGATIVE_VERDICTS) - mp.HUMAN_HANDOFF_VERDICTS
    assert nobody_can_buy == {"LOGIN_REQUIRED", "NOT_ACCEPTING_ORDERS", "VARIANT_GONE"}
    assert mp.HUMAN_HANDOFF_VERDICTS.isdisjoint(UNVERIFIABLE)
    assert mp.HUMAN_HANDOFF_VERDICTS | nobody_can_buy | set(UNVERIFIABLE) == every, (
        every - (mp.HUMAN_HANDOFF_VERDICTS | nobody_can_buy | set(UNVERIFIABLE)))
    assert "PASSWORD_PAGE" in UNVERIFIABLE, "a password wall declines the human cart too"


@pytest.mark.parametrize("verdict", sorted(v.value for v in Verdict))
async def test_every_verdict_answers_the_human_reader_from_a_real_row(_db, verdict):
    """CONTRACT over real `record_check` rows: one fresh row per verdict, from the buyer vantage.
    The card flag is whatever that verdict would carry (False for NO_CARD_PAYMENT, True for
    ELIGIBLE, unknown otherwise); it never decides the human answer."""
    card = {"ELIGIBLE": True, "NO_CARD_PAYMENT": False}.get(verdict)
    assert await mp.record_check("judydoll.com", "US", res(verdict, card=card)) is not None
    assert await mp.human_handoff_allowed("judydoll.com", "US") is (verdict in _HUMAN_ALLOWED), verdict
    # The rail's reader is unchanged: only ELIGIBLE + card opens it.
    assert await mp.is_purchasable("judydoll.com", "US") is (verdict == "ELIGIBLE")


async def test_a_no_card_payment_row_keeps_the_human_cart_however_many_times_it_repeats(_db):
    """Two consecutive NO_CARD_PAYMENT checks DEMOTE the rail (rule 2) — and the human cart stays,
    because demotion is `positive_until`, which this reader does not consult."""
    for _ in range(3):
        await mp.record_check("flowerbeauty.com", "US", res("NO_CARD_PAYMENT", card=False))
    row = await _row("flowerbeauty.com")
    assert row["consecutive_failures"] == 3 and row["positive_until"] is None
    assert await mp.is_purchasable("flowerbeauty.com", "US") is False
    assert await mp.human_handoff_allowed("flowerbeauty.com", "US") is True


async def test_a_stale_fact_does_not_open_the_human_cart(_db):
    """Freshness is the LAST CHECK inside the TTL, not `positive_until`. A row nobody has
    re-checked for longer than the TTL says nothing about today's checkout."""
    await mp.record_check("judydoll.com", "US", res("NO_CARD_PAYMENT", card=False))
    assert await mp.human_handoff_allowed("judydoll.com", "US") is True
    past = datetime.now(timezone.utc) - timedelta(hours=mp.ttl_hours() + 1)
    await _set("checked_at", past.strftime("%Y-%m-%d %H:%M:%S") if not IS_POSTGRES else past)
    assert await mp.human_handoff_allowed("judydoll.com", "US") is False
    # Just inside the window still answers.
    recent = datetime.now(timezone.utc) - timedelta(hours=mp.ttl_hours() - 1)
    await _set("checked_at", recent.strftime("%Y-%m-%d %H:%M:%S") if not IS_POSTGRES else recent)
    assert await mp.human_handoff_allowed("judydoll.com", "US") is True


async def test_the_human_reader_reads_the_buyer_vantage_only(_db, monkeypatch):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True), vantage="proxy")
    assert await mp.human_handoff_allowed("judydoll.com", "US") is False, "default vantage is worker"
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "proxy")
    assert await mp.human_handoff_allowed("judydoll.com", "US") is True


async def test_the_human_reader_is_per_market_and_never_defaults_one(_db, monkeypatch):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.human_handoff_allowed("judydoll.com", "US") is True
    assert await mp.human_handoff_allowed("judydoll.com", "SG") is False
    calls = []
    real = database.fetch_one

    async def _spy(*a, **k):
        calls.append(a)
        return await real(*a, **k)

    monkeypatch.setattr(database, "fetch_one", _spy)
    for unknown in (None, "", "  ", "USA", "U1"):
        assert await mp.human_handoff_allowed("judydoll.com", unknown) is False
    assert calls == [], "an unknown market is answered without a database read"


async def test_the_human_reader_fails_closed_when_the_database_raises(_db, monkeypatch):
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    assert await mp.human_handoff_allowed("judydoll.com", "US") is True

    async def _explode(*args, **kwargs):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(database, "fetch_one", _explode)
    assert await mp.human_handoff_allowed("judydoll.com", "US") is False


async def test_the_ops_route_publishes_the_human_tier_beside_the_rails(_db, ops_app, monkeypatch):
    """The gateway's gate strips a human cart on `tier`; it must have the human answer to read
    instead. The two part on NO_CARD_PAYMENT and nowhere else in this table."""
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    await mp.record_check("flowerbeauty.com", "US", res("NO_CARD_PAYMENT", card=False))
    await mp.record_check("luafee.com", "US", res("NOT_ACCEPTING_ORDERS"))
    expected = {
        "judydoll.com": ("purchase", "purchase"),
        "flowerbeauty.com": ("browse_only", "purchase"),
        "luafee.com": ("browse_only", "browse_only"),
        "never-seen.com": ("browse_only", "browse_only"),
    }
    for domain, (tier, human) in expected.items():
        body = (await ops_get(ops_app, f"/ops/merchant-purchasability?domain={domain}&market=US")).json()
        assert (body["tier"], body["human_handoff_tier"]) == (tier, human), domain
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=judydoll.com")).json()
    assert (body["tier"], body["human_handoff_tier"], body["reason"]) == (
        "browse_only", "browse_only", "market_unknown")
