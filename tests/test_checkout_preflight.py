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


async def test_price_verified_is_never_true_while_the_source_cannot_carry_currency(monkeypatch):
    """The positive counterpart: if a future change starts setting price_verified from this
    source, this fails and the docstring's claim has to be revisited rather than quietly lost."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _stub_verdict(monkeypatch, status=lov.VERIFIED, reason="ok", in_stock=True,
                  live_price=Decimal("24.00"), live_currency="USD", price_verified=False)
    v = await cp.preflight(_offer())
    assert v.price_verified is False


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


async def test_off_writes_no_observation(monkeypatch):
    wrote = {"n": 0}

    class _Counting:
        async def execute(self, *a, **k):
            wrote["n"] += 1

    monkeypatch.setattr(cp, "database", _Counting())
    await cp.preflight_and_record(_offer())
    assert wrote["n"] == 0
