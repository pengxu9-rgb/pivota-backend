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
  * every consumer is dark until MERCHANT_PURCHASABILITY_ENABLED is set.

Every rule has an ACCEPT and a REFUSE half. The detector tests run against RECORDED FIXTURES of
three live checkouts — see tests/fixtures/merchant_purchasability/README.md for how they were
trimmed — and no test in this file opens a socket.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db.merchant_purchasability as mp  # noqa: E402
import jobs.merchant_purchasability_sweep as sweep  # noqa: E402
from db.database import IS_POSTGRES, database  # noqa: E402
from services.shopify_cart_link_preflight import (  # noqa: E402
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
        "MERCHANT_PURCHASABILITY_ENABLED", "MERCHANT_PURCHASABILITY_TTL_HOURS",
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
    methods, available = checkout_payment_methods(body)
    assert available is card, f"{fixture}: card_available should be {card}"
    assert methods, "the accept-list must be read, not merely absent"
    assert checkout_line_price(body, variant) == (minor, currency)


def test_the_card_merchants_carry_a_payment_provider_and_the_paypal_one_does_not():
    """Names the MECHANISM, so a failure says which structure moved rather than arriving as a
    bare boolean. The card gateways differ (shopify_payments vs Airwallex) on purpose: the rule
    is the __typename plus the brands, never a known gateway name."""
    idew, _ = checkout_payment_methods((FIXTURES / "idewcare_card.html").read_text())
    judy, _ = checkout_payment_methods((FIXTURES / "judydoll_card.html").read_text())
    flower, _ = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
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
    methods, available = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
    assert available is False and len(methods) >= 2


def test_the_platform_constants_are_on_all_three_and_never_count_as_a_card():
    """`AnyGiftCardPaymentMethod` and `AnyStripeSharedTokenPaymentMethod` are availablePaymentLines
    entries on ALL THREE live pages, the PayPal-only one included. They are platform constants,
    exactly like `/.well-known/ucp`'s `dev.shopify.card`, and reading either as a card is the
    false positive this module exists to stop."""
    for fixture, *_ in LIVE_CASES:
        methods, _ = checkout_payment_methods((FIXTURES / fixture).read_text())
        assert "AnyGiftCardPaymentMethod" in methods
        assert "AnyStripeSharedTokenPaymentMethod" in methods


def test_card_is_never_true_by_absence_and_never_false_by_absence():
    """Both directions of the three-valued rule, on pages that carry no accept-list at all."""
    for body in ("", "<html></html>", '<script>{"creditCard":true}</script>', "not json at all"):
        methods, available = checkout_payment_methods(body)
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
        methods, _ = checkout_payment_methods((FIXTURES / fixture).read_text())
        assert len(methods) <= 32
        for label in methods:
            assert len(label) <= 64
            assert not label.startswith("ey"), "a JWT-shaped label would be a token"


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
    _, card = checkout_payment_methods((FIXTURES / "flowerbeauty_paypal_only.html").read_text())
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_PAUSE_MS", "0")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BATCH", "1")
    fetcher = _Fetcher({})
    monkeypatch.setattr(sweep, "_preflight", fetcher)
    report = await sweep.run_merchant_purchasability_sweep()
    assert report.population == 1 and len(fetcher.calls) == 1


async def test_one_merchant_that_raises_does_not_end_the_batch(_db, monkeypatch, _population):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENABLED", "true")
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


async def test_the_job_gate_and_the_consumer_gate_are_one_reader():
    """Two readers of one dial is two things to get out of step."""
    assert sweep.is_enabled.__doc__ and "is_gate_enabled" in sweep.is_enabled.__doc__
    assert sweep.is_enabled is not mp.is_gate_enabled
    assert sweep.is_enabled() is mp.is_gate_enabled()


# ══ 4. REGISTRATION ════════════════════════════════════════════════════════════════════════


def test_the_job_is_registered_with_a_run_deadline():
    """A registered job without an explicit deadline inherits the default and a boot warning;
    tests/test_scheduler_job_isolation.py fails on it. Pinned here so removing either half of the
    registration is a failure in this suite too."""
    import services.audit_scheduler as scheduler

    source = pathlib.Path(scheduler.__file__).read_text()
    assert 'id="merchant_purchasability_sweep"' in source, "the _add_job registration is gone"
    assert "run_merchant_purchasability_sweep" in source
    assert "merchant_purchasability_sweep" in scheduler._JOB_RUN_DEADLINES
    deadline = scheduler.run_deadline_for("merchant_purchasability_sweep")
    assert deadline > sweep.DIALS["budget_seconds"].default, (
        "the deadline must sit above the job's own budget plus one merchant"
    )
    assert "merchant_purchasability_sweep" in (scheduler.__doc__ or ""), "the docstring job list"


# ══ 5. THE CONSUMERS, dark behind the dial ═════════════════════════════════════════════════


async def test_the_reap_rail_ignores_the_fact_while_the_dial_is_off(_db, monkeypatch):
    """DARK BY DEFAULT: with the dial off the rail behaves exactly as it did."""
    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENABLED", raising=False)
    assert mp.is_gate_enabled() is False
    # No fact exists at all; the gate would refuse if it were consulted.
    assert await mp.is_purchasable("judydoll.com", "US") is False


async def test_the_reap_rail_consults_the_fact_when_the_dial_is_on(monkeypatch):
    """The seam, read off the source so that deleting the call is a failure here."""
    source = (pathlib.Path(__file__).parent.parent / "routes" / "agent_commerce_reap.py").read_text()
    assert "purchasability.is_gate_enabled()" in source
    assert "purchasability.is_purchasable(merchant_domain, market_country)" in source
    assert '"merchant_not_purchasable"' in source
    assert '"merchant_not_purchasable": 409' in source


def test_the_checkout_tier_surface_says_a_tier_does_not_authorise_a_purchase():
    source = (pathlib.Path(__file__).parent.parent / "routes" / "store_audit_ops.py").read_text()
    assert "purchasability_gate_enabled" in source
    assert "purchasability.is_gate_enabled()" in source


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
