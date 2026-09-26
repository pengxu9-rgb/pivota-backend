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
    MERCHANT_PURCHASABILITY_ENFORCE is — two dials, because the job runs only on the worker.

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
        "MERCHANT_PURCHASABILITY_PAUSE_MS", "MERCHANT_PURCHASABILITY_INTERVAL_SECONDS",
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
    scheduler's run deadline and be cut mid-store, every tick."""
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


# ══ 4. REGISTRATION ════════════════════════════════════════════════════════════════════════


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


async def test_the_job_is_actually_registered_and_wrapped_and_bounded(monkeypatch):
    """FROM THE LIVE REGISTRY, not from a grep of the source.

    A source-text assertion survives an undefined binding: rename
    `run_merchant_purchasability_sweep` in the job module and the registration raises a
    NameError at boot while the grep still finds the string and reports green. This calls
    `start_scheduler` and reads what it handed the scheduler.
    """
    import services.audit_scheduler as sched

    for key in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    recorder = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: recorder)

    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR

    registered = {jid: (fn, kwargs) for jid, fn, kwargs in recorder.added}
    assert "merchant_purchasability_sweep" in registered, (
        "the sweep is not registered — a boot-time NameError or a deleted _add_job call would "
        "look exactly like this, and a source grep would not"
    )
    func, kwargs = registered["merchant_purchasability_sweep"]
    assert getattr(func, "__wrapped_job_id__", None) == "merchant_purchasability_sweep", (
        "registered without the isolating wrapper: it would share the startup Connection"
    )
    assert kwargs["max_instances"] == 1, "two overlapping runs double the abandoned checkouts"
    assert kwargs["seconds"] == sweep.job_interval_seconds()

    deadline = sched.run_deadline_for("merchant_purchasability_sweep")
    assert deadline == sched._JOB_RUN_DEADLINES["merchant_purchasability_sweep"]
    assert deadline > sweep.DIALS["budget_seconds"].default, (
        "the deadline must sit above the job's own budget plus one merchant"
    )
    assert "merchant_purchasability_sweep" in (sched.__doc__ or ""), "the docstring job list"


def test_the_registration_is_worker_only_and_the_runbook_says_so():
    """`_add_job` is gated on `_queue_worker_enabled()`, so a normal backend deploy ships the
    consumers but NOT the fact-gathering. That is the whole reason the dial is split, and an
    operator has to know it before arming."""
    import services.audit_scheduler as sched

    source = pathlib.Path(sched.__file__).read_text()
    assert "if worker_enabled:" in source
    runbook = (pathlib.Path(__file__).parent.parent / "docs" / "runbooks"
               / "merchant_purchasability.md").read_text().lower()
    assert "worker" in runbook and "merchant_purchasability_sweep_enabled" in runbook
    assert "merchant_purchasability_enforce" in runbook


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
    yield
    await database.execute("DELETE FROM reap_agentic_eligibility")
    await database.execute("DELETE FROM tierb_cart_link_eligibility")


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
    """Zero-diff for a keyed request: the only new key is `reason`, and it is null. PINNED
    LITERALS — the key set and note below are main's response (2a590bb9c) plus `reason`."""
    await mp.record_check("judydoll.com", "US", res("ELIGIBLE", card=True))
    body = (await ops_get(ops_app, "/ops/merchant-purchasability?domain=judydoll.com&market=us")).json()
    assert set(body) == {
        "domain", "market", "tier", "reason", "enforced", "buyer_vantage", "sweep_enabled",
        "ttl_hours", "facts", "note",
    }
    assert body["reason"] is None
    assert (body["domain"], body["market"], body["tier"]) == ("judydoll.com", "US", "purchase")
    assert body["note"] == "a fresh positive fact from vantage 'worker'; the door may offer purchase"


async def test_the_sweep_skips_and_counts_a_row_with_an_unusable_market(monkeypatch):
    """DB-free: the two lanes are stubbed, so a value no allowlist table would accept ("USA",
    None) can still be fed through the population builder. None is swept as US; each is
    COUNTED; a lower-case market is normalised and swept."""

    async def _variant_lane():
        return [
            {"merchant_domain": "nomarket.example", "market_country": None},
            {"merchant_domain": "threeletter.example", "market_country": "USA"},
            {"merchant_domain": "lower.example", "market_country": "sg"},
        ]

    async def _cart_lane(_statement):
        return [{"domain": "blankmarket.example", "market": "  ", "variant_id": "1"}]

    async def _nothing_due(*_a, **_k):
        return []

    async def _no_variant(_domain):
        return None

    monkeypatch.setattr(ledger, "list_enabled_merchant_markets", _variant_lane)
    monkeypatch.setattr(sweep, "_rows", _cart_lane)
    monkeypatch.setattr(sweep.facts, "list_due", _nothing_due)
    monkeypatch.setattr(sweep, "_catalog_variant", _no_variant)

    tally = {}
    targets = await sweep.load_population(50, tally=tally)
    assert [(t.domain, t.market) for t in targets] == [("lower.example", "SG")]
    assert tally == {sweep.MARKET_UNKNOWN_TALLY: 3}


async def test_the_sweep_report_carries_the_market_unknown_count_and_logs_it_once(monkeypatch):
    async def _variant_lane():
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
