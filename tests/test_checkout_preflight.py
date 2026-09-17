"""A preflight that fails open is not a preflight.

The thing under test is an ASYMMETRY: `services/live_offer_verification` resolves "we could not
ask the merchant" to *demote this search result*, and this module must resolve the same evidence
to *do not take the money*. Every test below exists because getting that backwards produces a
system that looks verified and is not.

The second thing under test is that shadow is genuinely inert for the buyer while still measuring:
`would_block` must be computed identically in shadow and enforce, or the number the enforcement
decision rests on is measuring the wrong thing.
"""

import asyncio
import asyncio.base_events
import logging
import os
import socket
from decimal import Decimal

import pytest

from services import checkout_preflight as cp
from services import live_offer_verification as lov

REAL_VID = "43062643884185"


def _offer(**kw):
    o = {
        "offer_id": "offer:test:1",
        "sku_key": "ext:brand-thing::abc12345::v:" + REAL_VID,
        "product_key": "ext:brand-thing::abc12345",
        "source_product_id": "brand-thing",
        "merchant_id": "m_seller",
        "currency": "USD",
        "merchant_effective_price": "24.00",
        "execution_spec": {"pdp_url": "https://brand.example/products/thing",
                           "variant_id": REAL_VID},
    }
    o.update(kw)
    return o


@pytest.fixture(autouse=True)
def _default_off(monkeypatch):
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_MODE", raising=False)
    # The fence's PRODUCTION default belongs here too. Leaving it to each test to remember means
    # a developer with the variable exported in their shell runs a different suite than CI does,
    # and the one thing this file must not get wrong is whether the default egresses.
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)


def _stub_verdict(monkeypatch, **kw):
    v = lov.Verdict(status=kw.pop("status", lov.VERIFIED), reason=kw.pop("reason", "ok"), **kw)

    async def _fake(offer, **_):
        return v

    monkeypatch.setattr(lov, "_check_one", _fake)
    return v


# ---------------------------------------------------------------------------
# The mode is the safety story
# ---------------------------------------------------------------------------


def test_it_is_off_unless_deliberately_armed():
    assert cp.mode() == cp.MODE_OFF
    assert cp.is_enabled() is False


@pytest.mark.parametrize("raw", ["", "  ", "yes", "true", "1", "on", "nonsense", "ENFORCE!"])
def test_an_unrecognised_mode_is_off_not_enforce(monkeypatch, raw):
    """A typo in the flag must not arm a gate that refuses purchases. `true` is specifically
    included: it is what someone reaching for the usual boolean-flag habit would set, and it is
    not one of the three modes."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", raw)
    assert cp.mode() == cp.MODE_OFF


@pytest.mark.parametrize("raw,expected",
                         [("shadow", cp.MODE_SHADOW), ("SHADOW", cp.MODE_SHADOW),
                          (" enforce ", cp.MODE_ENFORCE), ("off", cp.MODE_OFF)])
def test_the_three_modes_are_recognised(monkeypatch, raw, expected):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", raw)
    assert cp.mode() == expected


async def test_off_does_no_work_and_allows(monkeypatch):
    """Off means no egress at all — not "ask and ignore the answer"."""
    called = {"n": 0}

    async def _boom(offer, **_):
        called["n"] += 1
        raise AssertionError("preflight must not touch the merchant when off")

    monkeypatch.setattr(lov, "_check_one", _boom)
    v = await cp.preflight(_offer())
    assert v.outcome == cp.OK and v.reason == cp.R_DISABLED
    assert v.allows_checkout is True
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Fail-closed: the asymmetry with the search path
# ---------------------------------------------------------------------------


async def test_a_merchant_that_cannot_be_asked_blocks(monkeypatch):
    """live_offer_verification returns `unverified` for a timeout, a block, or a storefront it
    cannot read, and DEMOTES it. Here the same evidence must refuse the purchase: the alternative
    is charging someone for a thing we could not confirm exists."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _stub_verdict(monkeypatch, status=lov.UNVERIFIED, reason="timeout")
    v = await cp.preflight(_offer())
    assert v.outcome == cp.UNVERIFIABLE
    assert v.reason == cp.R_UNVERIFIABLE
    assert v.would_block is True
    assert v.allows_checkout is False


async def test_our_own_exception_also_blocks(monkeypatch):
    """Fail-closed applies to OUR failures too — an exception leaves us in the same epistemic
    position as a timeout, and a preflight that treats its own crash as success is worse than
    no preflight."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")

    async def _raise(offer, **_):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(lov, "_check_one", _raise)
    v = await cp.preflight(_offer())
    assert v.outcome == cp.UNVERIFIABLE and v.would_block is True
    assert v.allows_checkout is False


async def test_gone_and_out_of_stock_block_with_distinct_reasons(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _stub_verdict(monkeypatch, status=lov.GONE, reason="404")
    assert (await cp.preflight(_offer())).reason == cp.R_GONE

    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=False)
    v = await cp.preflight(_offer())
    assert v.outcome == cp.BLOCK and v.reason == cp.R_OUT_OF_STOCK


async def test_only_a_positive_verification_proceeds(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True)
    v = await cp.preflight(_offer())
    assert v.outcome == cp.OK and v.would_block is False and v.allows_checkout is True


async def test_an_id_we_minted_is_refused_before_any_request(monkeypatch):
    """An id derived from the product cannot be verified against the merchant by definition —
    asking would only establish that the PRODUCT exists, which is not the question."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    called = {"n": 0}

    async def _count(offer, **_):
        called["n"] += 1
        return lov.Verdict(status=lov.VERIFIED, reason="ok", in_stock=True)

    monkeypatch.setattr(lov, "_check_one", _count)
    o = _offer(execution_spec={"pdp_url": "https://brand.example/products/thing",
                               "variant_id": "brand-thing-default"})
    v = await cp.preflight(o)
    assert v.outcome == cp.BLOCK and v.reason == cp.R_NO_MERCHANT_VARIANT
    assert called["n"] == 0, "no request should be spent on an unverifiable identity"


async def test_a_suppressed_offer_is_refused_without_asking(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True)
    v = await cp.preflight(_offer(suppression_reason="withdrawn"))
    assert v.outcome == cp.BLOCK and v.reason == cp.R_SUPPRESSED


# ---------------------------------------------------------------------------
# Shadow must be inert for the buyer and honest for the measurement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,in_stock", [(lov.UNVERIFIED, None), (lov.GONE, None),
                                             (lov.VERIFIED, False)])
async def test_shadow_records_the_refusal_but_lets_the_buyer_through(monkeypatch, status, in_stock):
    """The whole point: `would_block` is the enforcing decision, computed identically in shadow,
    while `allows_checkout` stays True. If would_block were derived from the mode, the number the
    enforcement decision rests on would always be zero."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=status, reason="x", in_stock=in_stock)
    v = await cp.preflight(_offer())
    assert v.would_block is True
    assert v.allows_checkout is True, "shadow must never refuse a buyer"


async def test_the_same_evidence_gives_the_same_would_block_in_both_modes(monkeypatch):
    """A shadow measurement is only evidence for enforcement if the two agree on the verdict."""
    _stub_verdict(monkeypatch, status=lov.UNVERIFIED, reason="timeout")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    shadow = await cp.preflight(_offer())
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    enforce = await cp.preflight(_offer())
    assert shadow.outcome == enforce.outcome
    assert shadow.would_block == enforce.would_block is True
    # Each verdict answers under the mode it was COMPUTED under, not whatever the env says now —
    # the env is re-read every call so the flag can be flipped without a deploy, and a flip
    # between computing a verdict and acting on it must not change what the verdict meant.
    assert shadow.mode == cp.MODE_SHADOW and enforce.mode == cp.MODE_ENFORCE
    assert shadow.allows_checkout is True and enforce.allows_checkout is False


async def test_a_verdict_is_not_reinterpreted_by_a_later_flag_flip(monkeypatch):
    """The failure this pins: `allows_checkout` originally read mode() live, so arming
    enforcement between computing a shadow verdict and acting on it turned an observation into
    a refusal of a buyer who had already been told to proceed."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=lov.GONE, reason="404")
    v = await cp.preflight(_offer())
    assert v.allows_checkout is True
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    assert v.allows_checkout is True, "a computed verdict must not change meaning"


# ---------------------------------------------------------------------------
# Price is not verified, and must not pretend to be
# ---------------------------------------------------------------------------


async def test_a_price_mismatch_is_informational_and_never_blocks(monkeypatch):
    """`/products/<handle>.js` carries no currency code, so a live amount cannot establish price.
    Blocking on it would refuse a correct ¥ offer because the number differs from a $ quote —
    the yen-as-dollars class this repo has already fixed twice."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True,
                  live_price=Decimal("4500"), live_currency=None, price_changed=True,
                  price_verified=False)
    v = await cp.preflight(_offer())
    assert v.outcome == cp.OK, "a price move must not refuse a verified, in-stock item"
    assert v.price_moved is True
    assert v.price_verified is False, "price must never be claimed verified from this source"


async def test_price_verified_is_never_true_even_when_the_search_checker_says_so(monkeypatch):
    """The positive counterpart, and not a tautology: `_check_one` DOES set price_verified=True
    when /meta.json's shop currency matches the offer's. That is a currency inference about a
    minor-unit amount, not a quote from the merchant's checkout, so this module pins it False
    at the pass-through. The first version of this test stubbed False and asserted False."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True,
                  live_price=Decimal("24.00"), live_currency="USD", price_verified=True)
    v = await cp.preflight(_offer())
    assert v.price_verified is False


async def test_the_call_is_bounded_by_the_deadline_and_a_timeout_is_unverifiable(monkeypatch):
    """`max_wait` bounds only the politeness stall inside `_check_one`; robots, pacing and a
    redirect-chasing fetch each carry their own timeout, so a bare call could hold the money
    path for 10-14 s. The whole call is wrapped in the deadline; a timeout is UNVERIFIABLE,
    which blocks in enforce and is a would_block row in shadow."""
    import asyncio as _asyncio
    import time as _time

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_DEADLINE_SECONDS", "0.5")

    async def _slow(offer, **_):
        await _asyncio.sleep(5)
        return lov.Verdict(status=lov.VERIFIED, reason="ok", in_stock=True)

    monkeypatch.setattr(lov, "_check_one", _slow)
    t0 = _time.monotonic()
    v = await cp.preflight(_offer())
    assert _time.monotonic() - t0 < 2.0, "the deadline must bound the whole call"
    assert v.outcome == cp.UNVERIFIABLE and v.would_block is True
    assert v.allows_checkout is False


# ---------------------------------------------------------------------------
# Recording must never cost a purchase
# ---------------------------------------------------------------------------


async def test_a_failed_observation_write_does_not_break_the_checkout(monkeypatch):
    """In shadow the buyer is proceeding regardless, so a failed INSERT costs a data point and
    must not cost a purchase."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True)

    class _Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(cp, "database", _Broken())
    v = await cp.preflight_and_record(_offer())
    assert v.outcome == cp.OK and v.allows_checkout is True


async def test_the_observation_records_the_mode_the_verdict_was_computed_under(monkeypatch):
    """`record()` used to read `is_enabled()`/`mode()` from the live env. The env is re-read
    per call by design (flip without a roll), so an off->shadow flip between computing and
    recording wrote a `preflight_off` pass into the shadow report as if measured, and a
    shadow->off flip dropped a real row."""
    rows = []

    class _Capture:
        async def execute(self, sql, params):
            rows.append(dict(params))

    monkeypatch.setattr(cp, "database", _Capture())
    _stub_verdict(monkeypatch, status=lov.GONE, reason="404")

    # computed under shadow, env flipped to off before recording -> the row is still written, as shadow
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    v = await cp.preflight(_offer())
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "off")
    await cp.record(v, _offer())
    assert [r["mode"] for r in rows] == [cp.MODE_SHADOW]
    assert rows[0]["would_block"] is True

    # computed under off, env flipped to shadow before recording -> no fake pass is written
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "off")
    v = await cp.preflight(_offer())
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    await cp.record(v, _offer())
    assert len(rows) == 1

    # computed under shadow, env flipped to enforce before recording -> labelled shadow, not enforce
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    v = await cp.preflight(_offer())
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    await cp.record(v, _offer())
    assert [r["mode"] for r in rows] == [cp.MODE_SHADOW, cp.MODE_SHADOW]


async def test_off_writes_no_observation(monkeypatch):
    wrote = {"n": 0}

    class _Counting:
        async def execute(self, *a, **k):
            wrote["n"] += 1

    monkeypatch.setattr(cp, "database", _Counting())
    await cp.preflight_and_record(_offer())
    assert wrote["n"] == 0


# ---------------------------------------------------------------------------
# The call site: offers.resolve's external-seed lane
#
# This is the last moment we control before a buyer is handed a PRE-FILLED CART keyed on the
# variant id the 2026-09-08 backfill wrote. A wrong variant there does not merely bounce them;
# it puts the wrong thing in a real cart.
# ---------------------------------------------------------------------------


async def _allows(monkeypatch, **verdict_kw):
    from routes.agent_shop_gateway import _preflight_allows_external_offer

    async def _fake(offer):
        return cp.PreflightVerdict(**verdict_kw)

    monkeypatch.setattr(cp, "preflight_and_record", _fake)
    return await _preflight_allows_external_offer({"offer_id": "of:external_seed:s:1"})


async def test_the_lane_is_untouched_when_the_preflight_is_off(monkeypatch):
    """Off must cost the resolve path nothing — not even the call."""
    from routes.agent_shop_gateway import _preflight_allows_external_offer

    called = {"n": 0}

    async def _count(offer):
        called["n"] += 1
        return cp.PreflightVerdict(outcome=cp.BLOCK, reason=cp.R_GONE, would_block=True,
                                   mode=cp.MODE_ENFORCE)

    monkeypatch.setattr(cp, "preflight_and_record", _count)
    assert await _preflight_allows_external_offer({"offer_id": "x"}) is True
    assert called["n"] == 0


async def test_shadow_publishes_the_offer_it_would_have_refused(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    assert await _allows(monkeypatch, outcome=cp.BLOCK, reason=cp.R_GONE,
                         would_block=True, mode=cp.MODE_SHADOW) is True


async def test_enforce_drops_an_offer_the_merchant_no_longer_sells(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    assert await _allows(monkeypatch, outcome=cp.BLOCK, reason=cp.R_GONE,
                         would_block=True, mode=cp.MODE_ENFORCE) is False


async def test_enforce_publishes_a_verified_offer(monkeypatch):
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    assert await _allows(monkeypatch, outcome=cp.OK, reason=cp.R_OK,
                         would_block=False, mode=cp.MODE_ENFORCE) is True


async def test_a_structural_exception_follows_the_operators_instruction(monkeypatch):
    """`preflight` is total, so an exception here is structural. Under enforce the operator has
    said refuse what cannot be verified, and an exception IS "could not verify"; under shadow a
    measurement must never change what a buyer sees."""
    from routes.agent_shop_gateway import _preflight_allows_external_offer

    async def _raise(offer):
        raise RuntimeError("structural")

    monkeypatch.setattr(cp, "preflight_and_record", _raise)

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    assert await _preflight_allows_external_offer({"offer_id": "x"}) is True
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    assert await _preflight_allows_external_offer({"offer_id": "x"}) is False


async def test_the_report_names_what_it_does_not_cover(monkeypatch):
    """A refusal rate is only meaningful with its denominator named, and this one is narrower
    than "external offers" in several ways at once: three other hand-over paths publish the
    same pre-filled cart_url and are not gated; the gated population is a UNION, so it is
    neither "cart-prefilled only" nor "resolved-identity only"; and two classes inflate
    `would_block` for reasons about our own data rather than the merchant's stock. A reader
    taking the rate as "how often an external offer is stale" would be wrong several times.

    Asserted on the RETURNED REPORT, not on the constant: a first version checked
    `cp.REPORT_SCOPE` directly, so deleting `scope` from the output dict left it green while
    the number travelled naked.

    THE PHRASES ARE CHOSEN SO THE OLD STRING FAILS. Round 4 of review reverted `REPORT_SCOPE`
    verbatim to its pre-#2151 wording and 200 tests stayed green: the old assertion was
    `"cart-prefilled" in scope`, which the corrected string satisfied by NEGATING it
    ("NOT only cart-prefilled ones"). An assertion a sentence can satisfy by saying the
    opposite is not an assertion about meaning.
    """
    class _Rows:
        async def fetch_all(self, *a, **k):
            return []

    monkeypatch.setattr(cp, "database", _Rows())
    report = await cp.shadow_report(window_days=7)
    scope = report.get("scope", "")
    assert "scope" in report, "the report must carry its own denominator"
    assert "not gated" in scope, "the three ungated lanes are still named"
    assert "UNION" in scope, (
        "the gated population is a union of named-variant and would-build-a-cart handoffs; "
        "any string calling it one of the two alone describes a denominator we do not use")
    assert "group by reason" in scope, (
        "would_block is inflated by classes that never reach a merchant; the reader has to be "
        "told before the rate is read")
    # NOT `"cart-prefilled" not in scope` — this string names that phrase in order to deny it,
    # and an assertion that cannot tell naming from denying is the one round 4 reverted past.


# ---------------------------------------------------------------------------
# The per-request budget
# ---------------------------------------------------------------------------


def test_the_budget_bounds_a_wide_result_set(monkeypatch):
    """A resolve can consider 40-2,880 candidates. A per-call timeout alone leaves the request
    unbounded, so the count cap is what stops a wide result set from spending 2,880 x 4s."""
    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MAX_PER_REQUEST", "3")
    b = _PreflightBudget()
    for _ in range(3):
        assert b.available() is True
        b.spend()
    assert b.available() is False, "the count cap must stop the gate for the rest of the request"


def test_the_budget_bounds_a_few_slow_merchants(monkeypatch):
    """The other failure shape: few candidates, each slow.

    Asserted AFTER the first spend, not at construction. The clock now starts on the first
    question, so a freshly built budget has spent no time by definition — an earlier version of
    this test asserted `available() is False` straight after construction, which only held
    because the clock was (wrongly) already running through the request's own DB lanes."""
    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_REQUEST_BUDGET_SECONDS", "0")
    b = _PreflightBudget()
    assert b.available() is True, "nothing asked yet, so no time spent on the gate"
    b.spend()
    assert b.available() is False, "a zero-second budget is spent by the first question"


def test_a_malformed_budget_falls_back_rather_than_raising(monkeypatch):
    """A typo in an env var must not take down offer resolution."""
    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MAX_PER_REQUEST", "not a number")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_REQUEST_BUDGET_SECONDS", "")
    b = _PreflightBudget()
    assert b.available() is True


# ---------------------------------------------------------------------------
# The coverage denominator has to actually exist
# ---------------------------------------------------------------------------


def test_coverage_is_measured_against_the_population_the_gate_applies_to():
    """The denominator is `gated`, NOT `candidates`.

    Candidates counts every seed offer considered, including ones the gate is blind to by
    design — and those are the majority. A first version divided by candidates and also left
    memo hits out of the numerator, so a request whose six gated handoffs were all answered
    (one ask + five memo hits) reported 0.333, and an ungated request reported 0.000. Both
    errors push the same way: a working gate reads as absent, and week one of shadow would
    have been dismissed on the strength of it.

    #2151 renamed the denominator from `cart_prefilled` to `gated` because the gate moved off
    `cart_variant_id` and onto the UNION `_handover_id or _cart_vid` — a cart needs storefront
    evidence the merchant question does not, but the attach lane ships carts we have no catalog
    row for. `cart_prefilled` stays as its own counter and is strictly the SMALLER of the two:
    every prefilled cart is gated (which was FALSE for one commit, until round 3 of review made
    the gate a union rather than a swap), and on today's corpus almost nothing that is gated
    gets a cart.
    """
    from routes.agent_shop_gateway import preflight_coverage_fields

    f = preflight_coverage_fields({
        "candidates": 40, "gated": 6, "cart_prefilled": 2, "asked": 1, "memo_hits": 5,
        "skipped_by_budget": 0, "degraded_to_referral": 2,
    })
    assert f["preflight_answered_fraction"] == 1.0, (
        "six gated handoffs, all answered — dividing by candidates would say 0.15")
    assert f["preflight_gated"] == 6
    assert f["preflight_carts_built"] == 2, (
        "the cart count is its own fact, not the gate's denominator — and it is RENAMED, "
        "because its meaning changed and a silent redefinition reads as a regression")
    assert f["preflight_candidates"] == 40, "the wider count is still reported, just not the base"
    assert f["preflight_memo_hits"] == 5


def test_the_gate_denominator_is_not_the_cart_count():
    """The mutant this pins: revert `covered` to `cart_prefilled`.

    On the population this lane actually serves the two differ by two orders of magnitude —
    3,875 seeds have a resolvable merchant-issued variant and 0 have the stored storefront
    evidence a cart needs (prod, 2026-09-08) — so a coverage line built on the cart count
    reports "the gate applied to nothing" on a request where it applied to everything.
    """
    from routes.agent_shop_gateway import preflight_coverage_fields

    f = preflight_coverage_fields({
        "candidates": 10, "gated": 4, "cart_prefilled": 0, "asked": 4, "memo_hits": 0,
    })
    assert f, "a gated request must report coverage even when no cart could be built"
    assert f["preflight_answered_fraction"] == 1.0


def test_a_partly_covered_request_reports_the_shortfall():
    """The number has to be able to say "the gate covered half of this request", or the budget
    is invisible."""
    from routes.agent_shop_gateway import preflight_coverage_fields

    f = preflight_coverage_fields({
        "candidates": 40, "gated": 20, "cart_prefilled": 20, "asked": 8, "memo_hits": 2,
        "skipped_by_budget": 10, "degraded_to_referral": 0,
    })
    assert f["preflight_answered_fraction"] == 0.5
    assert f["preflight_skipped_by_budget"] == 10


def test_a_referral_only_request_reports_no_coverage_at_all():
    """Not 0.000 — nothing. The gate applied to no part of this request, and a fraction of zero
    reads as "the gate failed" rather than "the gate did not apply". Keying the early return on
    `candidates` put exactly that line on every referral-only resolve."""
    from routes.agent_shop_gateway import preflight_coverage_fields

    assert preflight_coverage_fields(
        {"candidates": 40, "gated": 0, "cart_prefilled": 0, "asked": 0, "memo_hits": 0}) == {}


def test_coverage_carries_the_mode_it_was_measured_under(monkeypatch):
    """A coverage number without its mode cannot be compared across a rollout."""
    from routes.agent_shop_gateway import preflight_coverage_fields

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    f = preflight_coverage_fields({"candidates": 2, "gated": 2, "cart_prefilled": 2, "asked": 2})
    assert f["preflight_mode"] == "shadow"


def test_the_scope_says_the_rate_is_over_questions_asked():
    """`would_block_rate` divides by asked, not by candidates. A reader who takes it as
    "how often a cart handoff is stale" is wrong whenever the budget or the memo bit."""
    assert "ASKED" in cp.REPORT_SCOPE
    assert "not over candidates" in cp.REPORT_SCOPE


# ---------------------------------------------------------------------------
# The budget clock must measure the gate, not the request
# ---------------------------------------------------------------------------


def test_the_clock_starts_at_the_first_question_not_at_construction(monkeypatch):
    """The budget is built at the top of the resolve, and 0.7-1.1s of the request's own
    fetch_all lanes run before the first preflight. A clock started at construction spent
    itself on work the gate did not do — and biased the measurement against exactly the
    population worth measuring, since a slow-DB request reached the retry lanes with the
    budget gone and the gate silently absent."""
    import time as _t

    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_REQUEST_BUDGET_SECONDS", "0.05")
    b = _PreflightBudget()
    _t.sleep(0.1)  # the request's own DB lanes, doing no preflight work
    assert b.available() is True, "the clock must not have been running before the first ask"
    b.spend()
    _t.sleep(0.1)
    assert b.available() is False, "once spent, the clock runs"


def test_the_coverage_docstring_describes_the_union_the_gate_actually_uses():
    """The docstring of a live function may not state as fact the thing round 3 called a
    safety regression.

    Round 5 found this paragraph still saying the gate keys on the resolved hand-over id alone,
    while its own test twin two hundred lines up said "the UNION" — code and test asserting
    opposite things about one line. An earlier fix attempt missed it because the `.replace` it
    used did not match and nothing asserted that it had, which is the same class of silence.
    """
    import inspect

    from routes.agent_shop_gateway import preflight_coverage_fields

    doc = inspect.getdoc(preflight_coverage_fields) or ""
    assert "UNION" in doc, "the gated population is a union, and the docstring has to say so"
    assert "_handover_id or _cart_vid" in doc, "named, so a reader can find it in the code"


# ---------------------------------------------------------------------------------------------
# The egress fence. `checkout_preflight` runs in `web`, which is on the `default` subnet, whose
# NAT holds the address payment partners allowlist.
# ---------------------------------------------------------------------------------------------

class _Boom(AssertionError):
    """Raised by the fake transport. Reaching it IS the failure."""


@pytest.mark.asyncio
async def test_the_preflight_does_not_touch_a_merchant_by_default(monkeypatch):
    """MUTANT: `cache_only=False`, or default `CHECKOUT_PREFLIGHT_ALLOW_EGRESS` to true.

    Pinned at the SOCKET — literally, via `socket.getaddrinfo` and the loop's `create_connection`,
    not only at `httpx.AsyncClient` and `crawl_politeness.before_request`. Review pointed out that
    those two are module attributes: a leak that binds the client at import
    (`from httpx import AsyncClient as _Client`) and paces itself inline is a real merchant fetch
    that walks straight past both seams. Name resolution does not.

    Asserting the keyword was passed would be weaker still — it would pass for a `_check_one` that
    accepted the flag and ignored it — and the whole point of this change is that no packet leaves
    `web` for a merchant.

    The politeness and robots calls matter as much as the fetch: they are outbound requests to
    the merchant's host too, which is why the fence sits above all three.
    """
    import httpx

    from services import checkout_preflight, live_offer_verification

    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    live_offer_verification.reset_for_tests()

    def explode(*a, **k):
        raise _Boom("the preflight opened an HTTP client from the money path")

    monkeypatch.setattr(httpx, "AsyncClient", explode)

    async def explode_async(*a, **k):
        raise _Boom("the preflight called out to the merchant's host")

    monkeypatch.setattr(
        live_offer_verification.crawl_politeness, "before_request", explode_async
    )

    # The layer neither of the above can be bypassed at: nothing reaches a merchant without
    # resolving its name first.
    def explode_dns(host, *a, **k):
        raise _Boom(f"the preflight resolved a merchant host: {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", explode_dns)

    async def explode_connect(self, protocol_factory, host=None, *a, **k):
        raise _Boom(f"the preflight opened a connection to {host!r}")

    monkeypatch.setattr(
        asyncio.base_events.BaseEventLoop, "create_connection", explode_connect
    )

    verdict = await checkout_preflight.preflight(_offer())
    assert verdict.outcome == checkout_preflight.UNVERIFIABLE
    assert verdict.reason == checkout_preflight.R_NOT_YET_CHECKED, (
        "a cold URL is a fact about US, not about the merchant")


@pytest.mark.asyncio
async def test_a_cold_url_is_not_reported_as_the_merchant_refusing(monkeypatch):
    """MUTANT: fold `no_cached_evidence` into R_UNVERIFIABLE.

    Both block, so no behaviour test separates them — only the REASON does, and the reason is the
    entire value of week one of shadow. Conflated, the report says the merchants refused
    everything when in fact we never asked, which is the same shape as the empty denominator this
    gate already shipped once.
    """
    from services import checkout_preflight, live_offer_verification

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)

    async def cold(offer, **kw):
        assert kw.get("cache_only") is True
        return live_offer_verification.Verdict(
            live_offer_verification.UNVERIFIED, "no_cached_evidence")

    async def merchant_silent(offer, **kw):
        return live_offer_verification.Verdict(
            live_offer_verification.UNVERIFIED, "http_503")

    monkeypatch.setattr(live_offer_verification, "_check_one", cold)
    assert (await checkout_preflight.preflight(_offer())).reason == checkout_preflight.R_NOT_YET_CHECKED

    monkeypatch.setattr(live_offer_verification, "_check_one", merchant_silent)
    assert (await checkout_preflight.preflight(_offer())).reason == checkout_preflight.R_UNVERIFIABLE


@pytest.mark.asyncio
async def test_a_lane_that_may_crawl_can_opt_in(monkeypatch):
    """The fence must not make the gate unusable from a process that SHOULD egress — the warm and
    measurement lanes run on `pivota-crawl`, whose NAT is not the payment address."""
    from services import checkout_preflight, live_offer_verification

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    seen = {}

    async def fake(offer, **kw):
        seen.update(kw)
        return live_offer_verification.Verdict(
            live_offer_verification.VERIFIED, "ok", in_stock=True)

    monkeypatch.setattr(live_offer_verification, "_check_one", fake)
    verdict = await checkout_preflight.preflight(_offer())
    assert seen.get("cache_only") is False, "an opted-in lane must be allowed to ask"
    assert verdict.outcome == checkout_preflight.OK


@pytest.mark.parametrize("raw,allowed", [
    ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
    ("false", False), ("0", False), ("", False), ("maybe", False), ("  ", False),
])
def test_the_fence_opens_only_on_an_affirmative_value(monkeypatch, raw, allowed):
    """A typo must fail CLOSED. `bool(os.getenv(...))` would open the fence on "false"."""
    from services import checkout_preflight

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raw)
    assert checkout_preflight.egress_allowed() is allowed


def test_the_fence_is_closed_when_the_variable_is_absent(monkeypatch):
    from services import checkout_preflight

    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    assert checkout_preflight.egress_allowed() is False


@pytest.mark.asyncio
async def test_a_warm_document_does_not_let_the_currency_lane_out(monkeypatch):
    """MUTANT: fence only the `doc is None` branch (the shipped first cut).

    THE COLD PATH WAS NEVER THE WHOLE FENCE. `_shop_currency` keys a DIFFERENT cache
    (`lov:cur:{host}`), so a warm DOCUMENT says nothing about whether that one is warm — and on a
    document hit the old code fell through to a `robots.txt` fetch and a `/meta.json` fetch,
    from `web`, on the payment NAT. Worse, the refresh is fire-and-forget, so it escaped the
    caller's `wait_for` and outlived the response.

    So this test warms the document cache first, which is the state the whole warm-lane plan
    creates on purpose, and then asserts nothing outbound happens.
    """
    import httpx

    from services import checkout_preflight, live_offer_verification as lov

    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    lov.reset_for_tests()

    # Storefront evidence on purpose: without it `_check_one` short-circuits on
    # `not_a_known_shopify_storefront` BEFORE reaching the currency lane, and the test would pass
    # without ever visiting the line the leak was on.
    offer = _offer(source={"seed_data": {"snapshot": {"storefront_platform": "shopify"}}})
    js_url, _ = lov._target(offer)
    assert js_url, "the fixture must be verifiable, or this test proves nothing"
    # The document must contain a MATCHING variant, in the parsed shape `_check_one` reads
    # (`shopify_variant_id`, a string). A non-matching one returns `variant_absent` at :454 —
    # before the currency lane — so the first draft of this test warmed a document that made the
    # leak unreachable and the mutant survived. The point of the test is the code AFTER the match.
    await lov._cache_put(lov._cache_key(js_url), {
        "variants": [{"shopify_variant_id": REAL_VID, "available": True,
                      "price": Decimal("24.00")}],
    }, ttl=300)

    opened = []
    real_client = httpx.AsyncClient

    def record_client(*a, **k):
        opened.append(k)
        return real_client(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", record_client)

    reached = []

    async def record_politeness(url, **k):
        reached.append(url)

    monkeypatch.setattr(lov.crawl_politeness, "before_request", record_politeness)

    resolved = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: resolved.append(host))

    verdict = await checkout_preflight.preflight(offer)
    # DRAIN, do not just yield once. The currency refresh is `asyncio.ensure_future`, so its body
    # has not run when `preflight` returns and a single `sleep(0)` is not enough to guarantee it
    # has — a version of this test that yielded once let the reintroduced leak pass.
    for _ in range(3):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            break
        await asyncio.wait(pending, timeout=1.0)

    assert opened == [], f"the preflight opened HTTP clients from the money path: {opened}"
    assert reached == [], f"the preflight contacted merchant hosts: {reached}"
    assert resolved == [], f"the preflight resolved merchant hostnames: {resolved}"
    assert verdict.reason != checkout_preflight.R_NOT_YET_CHECKED, (
        "the warm document must have been USED — otherwise the fence short-circuited above the "
        "currency lane and this test never reached the line the leak was on")


def test_the_report_separates_our_cold_cache_from_the_merchants_verdict():
    """MUTANT: drop `would_block_rate_answered`, or leave not_yet_checked in its denominator.

    With the fence closed `would_block_rate` is 1.0 BY CONSTRUCTION — the same
    empty-denominator shape this gate already shipped once, in a new form. The answered rate must
    drop `not_yet_checked` from BOTH sides; leaving it in the denominator would understate the
    merchant refusal rate by exactly the size of our own cold cache.
    """
    from services import checkout_preflight as cp

    rows = [
        {"reason": cp.R_NOT_YET_CHECKED, "n": 80, "would_block": 80},
        {"reason": cp.R_GONE, "n": 5, "would_block": 5},
        {"reason": cp.R_OK, "n": 15, "would_block": 0},
    ]
    out = cp._summarise_shadow_rows(rows, window_days=7)
    assert out["observations"] == 100
    assert out["would_block_rate"] == 0.85, "the raw rate still reports everything, unchanged"
    assert out["not_yet_checked"] == 80
    assert out["answered"] == 20
    assert out["would_block_rate_answered"] == 0.25, (
        "5 of the 20 questions that reached a merchant were refused")


def test_the_answered_rate_is_none_rather_than_zero_when_nothing_was_answered():
    """0/0 must not read as 'the merchants refused nothing' — that is the number someone arms
    enforcement on. This is the state on day one of shadow, so it is not hypothetical."""
    from services import checkout_preflight as cp

    out = cp._summarise_shadow_rows(
        [{"reason": cp.R_NOT_YET_CHECKED, "n": 40, "would_block": 40}], window_days=7)
    assert out["would_block_rate"] == 1.0
    assert out["answered"] == 0
    assert out["would_block_rate_answered"] is None
    assert out["not_yet_checked_rate"] == 1.0


@pytest.mark.asyncio
async def test_a_not_yet_checked_observation_is_actually_persisted(monkeypatch):
    """MUTANT: `record()` early-returns on R_NOT_YET_CHECKED, or rewrites it to R_UNVERIFIABLE.

    Both survive every behavioural test, because the outcome and `would_block` are identical
    either way — only the stored REASON differs, and that reason is the whole point of the split.
    Dropped, the report loses the coverage denominator; rewritten, our cold cache is laundered
    into the merchant refusal rate.
    """
    from services import checkout_preflight as cp

    captured = {}

    async def fake_execute(sql, params):
        captured.update(params)

    monkeypatch.setattr(cp.database, "execute", fake_execute)
    verdict = cp.PreflightVerdict(
        outcome=cp.UNVERIFIABLE, reason=cp.R_NOT_YET_CHECKED, would_block=True, mode="shadow")
    await cp.record(verdict, _offer())
    assert captured.get("reason") == cp.R_NOT_YET_CHECKED, (
        "the observation must carry the reason it was decided on")
    assert captured.get("would_block") is True


@pytest.mark.asyncio
async def test_enforcing_with_the_fence_closed_says_so_out_loud(monkeypatch, caplog):
    """MUTANT: drop the interlock warning.

    `mode` and the fence are independent env vars, both re-read per call, so enforce-with-a-cold-
    cache is one flag away and withdraws EVERY cart on no merchant evidence. In the log MESSAGE,
    not `extra=`: the root logger is WARNING in prod and `setup_structured_logging()` is never
    called, so structured fields are dark.
    """
    from services import checkout_preflight as cp

    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    _stub_verdict(monkeypatch, status=lov.UNVERIFIED, reason=lov.NO_CACHED_EVIDENCE)

    with caplog.at_level(logging.WARNING):
        await cp.preflight(_offer())
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "ENFORCING" in blob and "CHECKOUT_PREFLIGHT_ALLOW_EGRESS" in blob, (
        f"no interlock warning in the message text: {blob[:300]}")

    # ...and it does NOT fire in shadow, or it becomes noise nobody reads.
    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await cp.preflight(_offer())
    assert "ENFORCING" not in " ".join(r.getMessage() for r in caplog.records)


def test_the_answered_rate_excludes_every_reason_decided_without_a_merchant():
    """MUTANT: drop only `not_yet_checked` from the answered denominator.

    `no_merchant_issued_variant_id` is decided at step 1 with NO request made, and on the union
    population it dominates. Counting it as a merchant refusal is the same
    unreadable-by-construction defect one class down: on the shape prod produces in week one it
    reads 0.99 against a true merchant refusal rate of 0.20, and the reader who sees 0.99 vetoes
    enforcement — the exact decision this report exists to inform.
    """
    from services import checkout_preflight as cp

    rows = [
        {"reason": cp.R_NOT_YET_CHECKED, "n": 600, "would_block": 600},
        {"reason": cp.R_NO_MERCHANT_VARIANT, "n": 380, "would_block": 380},
        {"reason": cp.R_SUPPRESSED, "n": 10, "would_block": 10},
        {"reason": cp.R_GONE, "n": 2, "would_block": 2},
        {"reason": cp.R_OK, "n": 8, "would_block": 0},
    ]
    out = cp._summarise_shadow_rows(rows, window_days=7)
    assert out["observations"] == 1000
    assert out["would_block"] == 992
    assert out["would_block_rate"] == 0.992
    assert out["no_contact"] == 990
    assert out["answered"] == 10, "only 10 questions actually reached a merchant"
    assert out["would_block_rate_answered"] == 0.2, (
        "2 of those 10 were refused — not 0.99")


@pytest.mark.asyncio
async def test_the_blind_enforce_warning_returns_when_the_state_does(monkeypatch, caplog):
    """MUTANT: latch the warning once-ever instead of on the state.

    Opening the fence and closing it again re-enters the dangerous state. The message text is the
    only signal there is, so a latch that never resets means the second entry is silent — and
    after log retention there is nothing at all.
    """
    from services import checkout_preflight as cp

    _stub_verdict(monkeypatch, status=lov.UNVERIFIED, reason=lov.NO_CACHED_EVIDENCE)

    async def warns() -> bool:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await cp.preflight(_offer())
        return "ENFORCING" in " ".join(r.getMessage() for r in caplog.records)

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    assert await warns() is True, "first entry into the dangerous state must warn"
    assert await warns() is False, "and must not repeat on every request"

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", "true")
    assert await warns() is False, "safe again"

    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    assert await warns() is True, "re-entering the dangerous state must warn AGAIN"


@pytest.mark.asyncio
async def test_an_offer_with_no_askable_url_is_not_counted_as_a_merchant_refusal(monkeypatch):
    """MUTANT: leave `no_verifiable_url` inside R_UNVERIFIABLE.

    `_target` returns nothing for a PDP that is not `/products/<handle>`-shaped, or for a seed with
    no url at all, and it does so BEFORE the cache read — no merchant is contacted and none could
    be. Under the shipping config (fence closed, nothing warming the cache) every other
    document-requiring reason is unreachable, so this was the ONLY reason left in the answered
    denominator: the rate built to escape a by-construction 1.0 read 1.0 by construction, over
    rows where nobody was asked.
    """
    from services import checkout_preflight as cp

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    verdict = await cp.preflight(_offer(
        execution_spec={"pdp_url": "https://brand.example/collections/all", "variant_id": REAL_VID}))
    assert verdict.reason == cp.R_NO_VERIFIABLE_URL
    assert cp.R_NO_VERIFIABLE_URL in cp.NO_CONTACT_REASONS

    no_url = await cp.preflight(_offer(execution_spec={"variant_id": REAL_VID}))
    assert no_url.reason == cp.R_NO_VERIFIABLE_URL


def test_only_reasons_that_cannot_have_touched_a_merchant_are_no_contact():
    """MUTANT: add R_UNVERIFIABLE, R_OUT_OF_STOCK or R_GONE to NO_CONTACT_REASONS.

    The set decides the answered denominator, so a reason wrongly IN it shrinks the denominator and
    flatters the merchants; wrongly OUT keeps the by-construction defect. Both directions are
    pinned here because only the second was pinned before.
    """
    from services import checkout_preflight as cp

    for r in (cp.R_NOT_YET_CHECKED, cp.R_NO_MERCHANT_VARIANT, cp.R_NO_VERIFIABLE_URL,
              cp.R_SUPPRESSED, cp.R_DISABLED):
        assert r in cp.NO_CONTACT_REASONS, f"{r} is decided without contacting a merchant"
    for r in (cp.R_GONE, cp.R_OUT_OF_STOCK, cp.R_OK, cp.R_UNVERIFIABLE):
        assert r not in cp.NO_CONTACT_REASONS, (
            f"{r} requires a document, so a merchant was contacted to produce it")


def test_the_report_scope_names_the_set_that_defines_its_denominator():
    """MUTANT: delete the scope paragraph, or stop naming NO_CONTACT_REASONS in it.

    REPORT_SCOPE ships INSIDE the report and is the definition of the denominator the enforcement
    decision is read against. This file already pins a docstring this way for the coverage fields;
    the same precedent applies to the number that decides whether to arm.
    """
    from services import checkout_preflight as cp

    scope = cp.REPORT_SCOPE
    assert "NO_CONTACT_REASONS" in scope, "the reader must be able to find the set"
    assert "would_block_rate_answered" in scope
    assert "1.0 BY CONSTRUCTION" in scope, "the trap must be named, not implied"
    for r in sorted(cp.NO_CONTACT_REASONS - {cp.R_DISABLED}):
        assert r in scope, f"{r} is excluded from the denominator but not disclosed"


@pytest.mark.asyncio
async def test_the_warning_uses_the_mode_the_verdict_was_decided_on(monkeypatch, caplog):
    """MUTANT: `_warn_if_enforcing_blind` re-reads `mode()` instead of the passed value.

    Three lines above it the module says to read the flag ONCE, because a mid-call flip made a
    verdict and its record disagree. A warning that disagreed with the verdict printed beside it
    would be worse than no warning.
    """
    from services import checkout_preflight as cp

    _stub_verdict(monkeypatch, status=lov.UNVERIFIED, reason=lov.NO_CACHED_EVIDENCE)
    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_ALLOW_EGRESS", raising=False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")

    calls = []
    real = cp._warn_if_enforcing_blind
    monkeypatch.setattr(cp, "_warn_if_enforcing_blind",
                        lambda m: calls.append(m) or real(m))
    await cp.preflight(_offer())
    assert calls == ["shadow"], "the warning must be handed the mode the verdict used"

    # ...and it must USE that value rather than re-reading the env. Call the real function with
    # the mode the verdict was decided on while the env says something else: a re-read would warn,
    # the passed value must not. Replacing the whole function (as an earlier version of this test
    # did) cannot see the difference, which is why that mutant survived.
    monkeypatch.setattr(cp, "_warn_if_enforcing_blind", real)
    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    with caplog.at_level(logging.WARNING):
        real(cp.MODE_SHADOW)
    assert "ENFORCING" not in " ".join(r.getMessage() for r in caplog.records), (
        "the env said enforce, the verdict said shadow — the verdict wins")

    caplog.clear()
    monkeypatch.setattr(cp, "_WARNED_BLIND_ENFORCE", False)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    with caplog.at_level(logging.WARNING):
        real(cp.MODE_ENFORCE)
    assert "ENFORCING" in " ".join(r.getMessage() for r in caplog.records), (
        "and the passed enforce still warns even when the env has since flipped away")


@pytest.mark.asyncio
async def test_the_warning_can_never_break_the_checkout_it_describes(monkeypatch):
    """`preflight`'s docstring says it never raises. The warning sat outside the try."""
    from services import checkout_preflight as cp

    def boom(_mode):
        raise RuntimeError("logging blew up")

    monkeypatch.setattr(cp, "_warn_if_enforcing_blind", boom)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True)
    assert (await cp.preflight(_offer())).outcome == cp.OK


@pytest.mark.asyncio
async def test_the_run_id_reaches_the_observation(monkeypatch):
    """MUTANT: write NULL for run_id.

    A sweep that aborted part-way has already committed its rows. Without the tag those rows are
    indistinguishable from a good run inside the window, and the only remedy is to throw the whole
    window away.
    """
    from services import checkout_preflight as cp

    captured = {}

    async def fake_execute(sql, params):
        captured.update(params)

    monkeypatch.setattr(cp.database, "execute", fake_execute)
    await cp.record(
        cp.PreflightVerdict(outcome=cp.OK, reason=cp.R_OK, would_block=False, mode="shadow"),
        _offer(), source=cp.SOURCE_SWEEP, run_id="sweep-42")
    assert captured["run_id"] == "sweep-42"
    assert captured["source"] == cp.SOURCE_SWEEP
