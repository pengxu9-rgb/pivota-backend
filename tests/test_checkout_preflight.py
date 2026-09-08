"""A preflight that fails open is not a preflight.

The thing under test is an ASYMMETRY: `services/live_offer_verification` resolves "we could not
ask the merchant" to *demote this search result*, and this module must resolve the same evidence
to *do not take the money*. Every test below exists because getting that backwards produces a
system that looks verified and is not.

The second thing under test is that shadow is genuinely inert for the buyer while still measuring:
`would_block` must be computed identically in shadow and enforce, or the number the enforcement
decision rests on is measuring the wrong thing.
"""

import os
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


def test_the_report_names_what_it_does_not_cover():
    """A refusal rate is only meaningful with its denominator named, and this one is narrower
    than "external offers" in two ways at once: three other hand-over paths publish the same
    pre-filled cart_url and are not gated, and within the gated lane only cart-prefilled
    handoffs are asked about. A reader taking it as "how often an external offer is stale"
    would be wrong twice, so the scope travels with the number."""
    assert "cart-prefilled" in cp.REPORT_SCOPE
    assert "not gated" in cp.REPORT_SCOPE


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
    """The other failure shape: few candidates, each slow. Zero seconds means the very first
    check is already over budget."""
    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_REQUEST_BUDGET_SECONDS", "0")
    assert _PreflightBudget().available() is False


def test_a_malformed_budget_falls_back_rather_than_raising(monkeypatch):
    """A typo in an env var must not take down offer resolution."""
    from routes.agent_shop_gateway import _PreflightBudget

    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MAX_PER_REQUEST", "not a number")
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_REQUEST_BUDGET_SECONDS", "")
    b = _PreflightBudget()
    assert b.available() is True
