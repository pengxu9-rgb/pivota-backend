"""The deal decision (ADR-027 D4, D7; services/deal_lane_decision.py): the pure rules one mutation at a
time, then the reads on the suite's SQLite file. The reads on real Postgres:
tests/test_deal_lane_decision_postgres.py."""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import deal_lane_decision as dld  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
ON = dld.DealSwitches(emit_enabled=True, discount_param_enabled=True)


def _candidate(**overrides) -> dld.DealCandidate:
    base = dict(
        deal_id="deal_a",
        seller_id="seller_a",
        market="US",
        code="GLOW10",
        kind="percent",
        source="merchant_published",
        currency="USD",
        new_customer_only=False,
        once_per_customer=False,
        segment_limited=False,
        usage_limited=False,
        eligibility_known=True,
        starts_at=None,
        ends_at=None,
        variant_id="4401",
        state="checkout_verified",
        verified_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=22),
        ref_unit_price=Decimal("40.00"),
        ref_subtotal=Decimal("40.00"),
        ref_discount_amount=Decimal("4.00"),
        ref_currency="USD",
        ref_automatic_discounts=[],
    )
    base.update(overrides)
    return dld.DealCandidate(**base)


def _ctx(**overrides) -> dld.DealContext:
    base = dict(
        seller_id="seller_a",
        variant_id="4401",
        platform="shopify",
        quantity=1,
        pin_country="US",
        observed_unit_price=Decimal("40"),
        observed_currency="USD",
    )
    base.update(overrides)
    return dld.DealContext(**base)


def _decide(candidates=None, ctx=None, switches=ON, *, require_price_match=True):
    return dld.decide(
        [_candidate()] if candidates is None else candidates,
        ctx or _ctx(),
        switches,
        NOW,
        require_price_match=require_price_match,
    )


# ---- the happy path -------------------------------------------------------------------------------


def test_a_verified_published_code_rides_a_pinned_one_unit_cart():
    decision, reason = _decide()
    assert reason == "ok"
    assert (decision.deal_id, decision.variant_id, decision.market, decision.code) == ("deal_a", "4401", "US", "GLOW10")
    assert (decision.estimated_effective_price, decision.discount_amount, decision.automatic_amount) == (
        Decimal("36.00"), Decimal("4.00"), Decimal("0"),
    )
    assert (decision.currency, decision.ref_unit_price) == ("USD", Decimal("40.00"))
    assert decision.valid_until == NOW + timedelta(hours=22)
    assert (decision.basis, decision.lane) == ("checkout_verified", "shopify_cart_permalink")


def test_the_token_reference_never_carries_the_code():
    decision, _ = _decide()
    assert decision.reference() == {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    assert "GLOW10" not in repr(decision.reference())


def test_a_deal_that_ends_before_its_verification_expires_is_valid_until_it_ends():
    decision, _ = _decide([_candidate(ends_at=NOW + timedelta(hours=3))])
    assert decision.valid_until == NOW + timedelta(hours=3)


def test_the_lowest_effective_price_wins_before_the_larger_code():
    # A smaller code on a variant whose automatic discount still applies is the cheaper buyer price.
    small_code = _candidate(deal_id="deal_b", code="FIVE", ref_discount_amount=Decimal("2"),
                            ref_automatic_discounts=[{"title": "Autumn", "type": "line", "amount": "6"}], ref_subtotal=Decimal("34"))
    big_code = _candidate(deal_id="deal_a", code="TEN", ref_discount_amount=Decimal("4"))
    decision, _ = _decide([big_code, small_code])
    assert (decision.code, decision.estimated_effective_price) == ("FIVE", Decimal("32"))


def test_at_equal_price_the_larger_code_then_the_later_expiry_then_the_fresher_check_win():
    automatic = [{"title": "Autumn", "type": "line", "amount": "2"}]
    smaller_code = _candidate(deal_id="deal_a", code="TWO", ref_discount_amount=Decimal("2"),
                              ref_automatic_discounts=automatic)
    larger_code = _candidate(deal_id="deal_z", code="FOUR")
    assert _decide([smaller_code, larger_code])[0].code == "FOUR"
    early = _candidate(deal_id="deal_a", code="EARLY", expires_at=NOW + timedelta(hours=5))
    late = _candidate(deal_id="deal_z", code="LATE", expires_at=NOW + timedelta(hours=6))
    assert _decide([early, late])[0].code == "LATE"
    stale = _candidate(deal_id="deal_a", code="STALE", verified_at=NOW - timedelta(hours=3),
                       expires_at=NOW + timedelta(hours=6))
    fresh = _candidate(deal_id="deal_z", code="FRESH", verified_at=NOW - timedelta(hours=1),
                       expires_at=NOW + timedelta(hours=6))
    assert _decide([stale, fresh])[0].code == "FRESH"
    twin_a, twin_b = _candidate(deal_id="deal_b", code="B"), _candidate(deal_id="deal_a", code="A")
    assert _decide([twin_a, twin_b])[0].deal_id == "deal_a"


def test_a_larger_code_beats_a_later_expiry_and_a_later_expiry_beats_a_fresher_check():
    # Neighbouring rank keys pulling in opposite directions: the earlier key decides.
    larger_sooner = _candidate(deal_id="deal_z", code="BIG", ref_discount_amount=Decimal("4"),
                               expires_at=NOW + timedelta(hours=2))
    smaller_later = _candidate(deal_id="deal_a", code="SMALL", ref_discount_amount=Decimal("2"),
                               ref_automatic_discounts=[{"title": "A", "type": "line", "amount": "2"}],
                               expires_at=NOW + timedelta(hours=20))
    assert _decide([smaller_later, larger_sooner])[0].code == "BIG"
    later_staler = _candidate(deal_id="deal_z", code="LATER", verified_at=NOW - timedelta(hours=3),
                              expires_at=NOW + timedelta(hours=20))
    sooner_fresher = _candidate(deal_id="deal_a", code="FRESHER", verified_at=NOW - timedelta(minutes=5),
                                expires_at=NOW + timedelta(hours=10))
    assert _decide([sooner_fresher, later_staler])[0].code == "LATER"


def test_unreadable_rows_are_reported_not_hidden():
    assert dld.decide([], _ctx(), ON, NOW, require_price_match=True, unreadable_rows=1) == (
        None, "no_emittable_deal:row_unreadable",
    )
    decision, reason = dld.decide([_candidate()], _ctx(), ON, NOW, require_price_match=True, unreadable_rows=2)
    assert (reason, decision.deal_id) == ("ok", "deal_a")


# ---- the effective price (D4) ---------------------------------------------------------------------


@pytest.mark.parametrize("subtotal", ["40.00", "36.00"])
def test_the_effective_price_is_the_unit_price_less_every_allocation_whatever_the_subtotal(subtotal):
    # Whether the platform's subtotal already includes the allocation must not change the answer.
    decision, _ = _decide([_candidate(ref_subtotal=Decimal(subtotal))])
    assert decision.estimated_effective_price == Decimal("36.00")


def test_order_and_same_currency_automatic_allocations_count():
    automatic = [{"title": "A", "type": "order", "amount": "1", "currency": "USD"},
                 {"title": "B", "type": "line", "amount": "1"}]
    decision, _ = _decide([_candidate(ref_automatic_discounts=automatic, ref_subtotal=Decimal("38"))])
    assert (decision.automatic_amount, decision.estimated_effective_price) == (Decimal("2"), Decimal("34.00"))


def test_automatic_allocations_are_part_of_the_price_never_the_codes_saving():
    candidate = _candidate(ref_automatic_discounts=[{"title": "Autumn", "type": "line", "amount": "2.00"}], ref_subtotal=Decimal("38"))
    decision, _ = _decide([candidate])
    assert (decision.estimated_effective_price, decision.discount_amount, decision.automatic_amount) == (
        Decimal("34.00"), Decimal("4.00"), Decimal("2.00"),
    )


@pytest.mark.parametrize("subtotal", ["5", "35.99", "40.01", "100"])
def test_a_subtotal_outside_effective_to_unit_is_not_a_one_unit_cart(subtotal):
    assert _decide([_candidate(ref_subtotal=Decimal(subtotal))]) == (
        None, "no_emittable_deal:reference_cart_inconsistent",
    )


@pytest.mark.parametrize(
    "automatic",
    [
        None, "[]", [5], [{"title": "x"}],
        [{"type": "line", "amount": "NaN"}], [{"type": "line", "amount": "-1"}],
        [{"type": "line", "amount": "Infinity"}],
        [{"amount": "2"}],  # untyped
        [{"type": "shipping", "amount": "2"}],  # a shipping allocation is never off the unit price
        [{"type": "order", "amount": "2", "currency": "SGD"}],  # another currency
    ],
)
def test_an_unreadable_automatic_list_is_refused(automatic):
    assert _decide([_candidate(ref_automatic_discounts=automatic)]) == (
        None, "no_emittable_deal:reference_cart_incomplete",
    )


@pytest.mark.parametrize("column", ["ref_unit_price", "ref_subtotal", "ref_discount_amount"])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_a_non_finite_amount_is_refused_and_never_raises(column, value):
    assert _decide([_candidate(**{column: value})]) == (None, "no_emittable_deal:reference_cart_incomplete")
    # And it never hides a good deal for the same variant.
    decision, reason = _decide([_candidate(deal_id="deal_bad", **{column: value}), _candidate()])
    assert (reason, decision.deal_id) == ("ok", "deal_a")


def test_a_non_finite_observed_price_is_not_a_price():
    assert _decide(ctx=_ctx(observed_unit_price=Decimal("NaN"))) == (None, "no_emittable_deal:price_unobserved")


# ---- the TTL ceiling (D3) -------------------------------------------------------------------------


def test_a_stored_expiry_cannot_outlive_the_ttl(monkeypatch):
    monkeypatch.delenv("DEAL_VERIFY_TTL_HOURS", raising=False)
    old = _candidate(verified_at=NOW - timedelta(days=30), expires_at=NOW + timedelta(hours=1))
    assert _decide([old]) == (None, "no_emittable_deal:verification_past_ttl")
    long_lived = _candidate(verified_at=NOW - timedelta(hours=2), expires_at=NOW + timedelta(hours=30))
    assert _decide([long_lived])[0].valid_until == NOW + timedelta(hours=22)


def test_lowering_the_ttl_reaches_rows_already_stored(monkeypatch):
    assert _decide()[1] == "ok"  # verified 2h ago
    monkeypatch.setenv("DEAL_VERIFY_TTL_HOURS", "1")
    assert _decide() == (None, "no_emittable_deal:verification_past_ttl")
    monkeypatch.setenv("DEAL_VERIFY_TTL_HOURS", "3")
    assert _decide()[0].valid_until == NOW + timedelta(hours=1)


# ---- every rule, one at a time --------------------------------------------------------------------


@pytest.mark.parametrize(
    "switches,reason",
    [
        (dld.DealSwitches(emit_enabled=False, discount_param_enabled=True), "emit_off"),
        # With the parameter off, `/r` would not carry the code: the lane may not show the deal.
        (dld.DealSwitches(emit_enabled=True, discount_param_enabled=False), "discount_param_off"),
        (dataclasses.replace(ON, market_stopped=True), "market_stopped"),
        (dataclasses.replace(ON, seller_opted_out=True), "seller_opted_out"),
    ],
)
def test_switches(switches, reason):
    assert _decide(switches=switches) == (None, reason)


@pytest.mark.parametrize(
    "ctx,reason",
    [
        (dict(platform="wix"), "not_shopify"),
        (dict(platform=None), "not_shopify"),
        (dict(variant_id=None), "no_cart_variant"),
        (dict(variant_id="  "), "no_cart_variant"),
        (dict(quantity=2), "quantity_not_one"),
        (dict(seller_id=None), "no_seller"),
        # No pin = the market was defaulted, not declared (`_cart_country_pin`).
        (dict(pin_country=None), "market_not_pinned"),
        (dict(pin_country="ZZ"), "market_not_pinned"),
        # Listed but not served by the registry (acquisition-only today).
        (dict(pin_country="AU"), "market_not_served"),
        (dict(pin_country="JP"), "market_not_served"),
    ],
)
def test_the_link_must_be_a_pinned_one_unit_shopify_cart(ctx, reason):
    assert _decide(ctx=_ctx(**ctx)) == (None, reason)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        (dict(seller_id="seller_b"), "other_seller"),
        (dict(variant_id="4402"), "other_variant"),
        (dict(market="SG"), "other_market"),
        (dict(market=None), "other_market"),
        (dict(source="authorized_feed"), "source_not_on_this_lane"),  # network link only (Q14)
        (dict(source="channel_exclusive"), "source_not_on_this_lane"),  # Phase 3 (D8)
        (dict(source="checkout_automatic", code=None), "source_not_on_this_lane"),
        (dict(code=" "), "no_code"),
        (dict(kind="free_shipping"), "kind_not_emitted"),
        (dict(kind="bxgy"), "kind_not_emitted"),
        (dict(eligibility_known=False), "eligibility_limited_or_unknown"),
        (dict(new_customer_only=True), "eligibility_limited_or_unknown"),
        (dict(once_per_customer=True), "eligibility_limited_or_unknown"),
        (dict(segment_limited=True), "eligibility_limited_or_unknown"),
        (dict(usage_limited=True), "eligibility_limited_or_unknown"),
        (dict(state="unverified"), "not_verified"),
        (dict(state="failed"), "not_verified"),
        (dict(expires_at=NOW), "verification_expired"),
        (dict(expires_at=None), "verification_expired"),
        (dict(verified_at=NOW + timedelta(minutes=1)), "verification_expired"),
        (dict(starts_at=NOW + timedelta(minutes=1)), "not_started"),
        (dict(ends_at=NOW), "ended"),
        (dict(ref_currency="SGD"), "currency_not_market"),
        (dict(ref_currency="SGD", currency=None), "currency_not_market"),  # the registry's currency
        (dict(ref_currency="SGD", currency="SGD"), "currency_not_market"),
        (dict(ref_currency=None), "currency_not_market"),
        (dict(currency="EUR"), "currency_not_market"),
        (dict(ref_discount_amount=Decimal("0")), "reference_cart_incomplete"),
        (dict(ref_discount_amount=Decimal("40.00")), "reference_cart_incomplete"),  # nothing left to pay
        (dict(ref_subtotal=None), "reference_cart_incomplete"),
        (dict(ref_unit_price=None), "reference_cart_incomplete"),
        (dict(ref_discount_amount=Decimal("-1")), "reference_cart_incomplete"),
    ],
)
def test_a_deal_that_breaks_one_rule_is_not_emitted(overrides, reason):
    assert _decide([_candidate(**overrides)]) == (None, f"no_emittable_deal:{reason}")


def test_the_boundaries_are_inclusive_at_the_start():
    assert _decide([_candidate(verified_at=NOW, expires_at=NOW + timedelta(hours=1))])[1] == "ok"
    assert _decide([_candidate(starts_at=NOW)])[1] == "ok"


def test_the_row_market_is_compared_normalised():
    assert _decide([_candidate(market=" us ")])[0].market == "US"


def test_a_served_market_other_than_us_uses_its_own_currency():
    ctx = _ctx(pin_country="SG", observed_currency="SGD")
    decision, reason = _decide([_candidate(market="SG", currency="SGD", ref_currency="SGD")], ctx=ctx)
    assert (reason, decision.currency) == ("ok", "SGD")


def test_a_naive_now_from_a_caller_is_refused():
    with pytest.raises(ValueError):
        dld.decide([_candidate()], _ctx(), ON, NOW.replace(tzinfo=None), require_price_match=True)


def test_naive_timestamps_from_the_database_are_utc():
    naive = _candidate(verified_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
                       expires_at=(NOW + timedelta(hours=1)).replace(tzinfo=None))
    assert _decide([naive])[1] == "ok"


@pytest.mark.parametrize(
    "ctx,reason",
    [
        (dict(observed_unit_price=None), "price_unobserved"),
        (dict(observed_currency=None), "price_unobserved"),
        (dict(observed_unit_price=Decimal("39.99")), "price_changed"),
        (dict(observed_currency="SGD"), "price_changed"),
        (dict(observed_unit_price="garbage"), "price_unobserved"),
    ],
)
def test_at_issue_time_the_observed_price_must_equal_the_reference_cart(ctx, reason):
    assert _decide(ctx=_ctx(**ctx)) == (None, f"no_emittable_deal:{reason}")


def test_an_equal_price_in_another_notation_still_matches():
    for price in (Decimal("40.0000"), "40", 40, " usd "):
        ctx = _ctx(observed_unit_price=price) if price != " usd " else _ctx(observed_currency=" usd ")
        assert _decide(ctx=ctx)[1] == "ok", price


def test_at_click_time_the_price_is_not_required():
    ctx = _ctx(observed_unit_price=None, observed_currency=None)
    assert _decide(ctx=ctx, require_price_match=False)[1] == "ok"


def test_rejections_are_reported_together_and_no_candidate_is_no_deal():
    reason = _decide([_candidate(state="failed"), _candidate(deal_id="deal_b", kind="gift")])[1]
    assert reason == "no_emittable_deal:kind_not_emitted,not_verified"
    assert _decide([]) == (None, "no_deal")


# ---- references -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "deal_a",
        {"deal_id": "deal_a", "variant_id": "4401"},
        {"deal_id": "deal_a", "variant_id": "4401", "market": "US", "code": "GLOW10"},
        {"deal_id": "", "variant_id": "4401", "market": "US"},
    ],
)
def test_a_reference_is_exactly_deal_variant_market(value):
    assert dld.parse_deal_reference(value) is None


# ---- the reads, on the suite's SQLite file --------------------------------------------------------


@pytest.fixture
async def db(monkeypatch):
    from db import deal_lane
    from db.database import database, engine, metadata

    tables = [
        deal_lane.observed_store_offers,
        deal_lane.observed_store_offer_verifications,
        deal_lane.deal_lane_stops,
        deal_lane.deal_lane_optout,
    ]
    metadata.create_all(engine, tables=tables)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _clean():
        for table in reversed(tables):
            await database.execute(table.delete())

    await _clean()
    monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "true")
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "true")
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _seed(database, deal_id="deal_a", *, code="GLOW10", discount="4.00", **verification):
    from db.deal_lane import observed_store_offer_key, observed_store_offer_verifications, observed_store_offers

    await database.execute(
        observed_store_offers.insert().values(
            deal_id=deal_id,
            deal_key=observed_store_offer_key(
                seller_id="seller_a", market="US", code=code, platform_rule_ref=None, program_id=None
            ),
            seller_id="seller_a", market="US", code=code, kind="percent", value=10, currency="USD",
            source="merchant_published", eligibility_known=True, observed_at=NOW - timedelta(days=1),
        )
    )
    row = dict(
        deal_id=deal_id, variant_id="4401", state="checkout_verified",
        verified_at=datetime.now(timezone.utc) - timedelta(hours=1),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=23),
        verified_via="shopify_storefront_connected", authorized_by="connected",
        ref_unit_price=Decimal("40.00"), ref_subtotal=Decimal("40.00"),
        ref_discount_amount=Decimal(discount), ref_currency="USD", ref_automatic_discounts=[],
    )
    row.update(verification)
    await database.execute(observed_store_offer_verifications.insert().values(**row))


async def test_resolve_reads_the_verified_deal(db):
    await _seed(db)
    await _seed(db, "deal_b", code="FIVE", discount="2.00")
    decision, reason = await dld.resolve_deal_decision(_ctx())
    assert (reason, decision.code, decision.estimated_effective_price) == ("ok", "GLOW10", Decimal("36.00"))


async def test_resolve_refuses_a_price_that_moved_since_the_reference_cart(db):
    await _seed(db)
    assert await dld.resolve_deal_decision(_ctx(observed_unit_price=Decimal("41"))) == (
        None, "no_emittable_deal:price_changed",
    )
    assert await dld.resolve_deal_decision(_ctx(observed_unit_price=None)) == (
        None, "no_emittable_deal:price_unobserved",
    )


@pytest.mark.parametrize("which", ["fetch_one", "fetch_all"])
async def test_a_failed_read_is_no_deal_never_an_exception(db, monkeypatch, which):
    await _seed(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(dld.database, which, _boom)
    assert await dld.resolve_deal_decision(_ctx()) == (None, "read_failed")
    ref = {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    assert await dld.recheck_deal_reference(ref, _ctx()) == (None, "read_failed")


async def test_a_row_this_code_cannot_parse_is_row_unreadable_not_read_failed(db, monkeypatch):
    await _seed(db)
    real = dld._candidate

    def _breaks_on_one(row):
        if row["deal_id"] == "deal_a":
            raise KeyError("renamed_column")
        return real(row)

    monkeypatch.setattr(dld, "_candidate", _breaks_on_one)
    assert await dld.resolve_deal_decision(_ctx()) == (None, "no_emittable_deal:row_unreadable")


async def test_a_naive_now_is_refused_before_any_read(db):
    with pytest.raises(ValueError):
        await dld.resolve_deal_decision(_ctx(), now=NOW.replace(tzinfo=None))


async def test_resolve_ignores_rows_that_are_not_verified(db):
    await _seed(db, state="unverified", verified_at=None, expires_at=None, verified_via=None,
                authorized_by=None)
    assert await dld.resolve_deal_decision(_ctx()) == (None, "no_deal")


async def test_a_stop_row_or_an_opt_out_stops_resolve_and_recheck(db):
    from db.deal_lane import deal_lane_optout, deal_lane_stops

    await _seed(db)
    ref = {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    await db.execute(deal_lane_stops.insert().values(stop_id="stop_1", market="US", stop_trigger="absolute"))
    assert await dld.resolve_deal_decision(_ctx()) == (None, "market_stopped")
    assert await dld.recheck_deal_reference(ref, _ctx()) == (None, "market_stopped")
    await db.execute(
        deal_lane_stops.update().values(cleared_at=datetime.now(timezone.utc), cleared_by="operator_a")
    )
    assert (await dld.resolve_deal_decision(_ctx()))[1] == "ok"
    await db.execute(
        deal_lane_optout.insert().values(seller_id="seller_a", requested_at=NOW, recorded_by="operator_a")
    )
    assert await dld.resolve_deal_decision(_ctx()) == (None, "seller_opted_out")
    assert await dld.recheck_deal_reference(ref, _ctx()) == (None, "seller_opted_out")


async def test_a_stopped_market_skips_the_candidate_read(db, monkeypatch):
    from db.deal_lane import deal_lane_stops

    await db.execute(deal_lane_stops.insert().values(stop_id="stop_1", market="US", stop_trigger="manual"))

    async def _no_candidate_read(*args, **kwargs):
        raise AssertionError("read candidates for a stopped market")

    monkeypatch.setattr(dld.database, "fetch_all", _no_candidate_read)
    assert await dld.resolve_deal_decision(_ctx()) == (None, "market_stopped")


async def test_recheck_reads_only_the_referenced_deal(db):
    await _seed(db)
    await _seed(db, "deal_b", code="FIVE", discount="2.00")
    ctx = _ctx(observed_unit_price=None, observed_currency=None)
    decision, reason = await dld.recheck_deal_reference({"deal_id": "deal_b", "variant_id": "4401", "market": "US"}, ctx)
    assert (reason, decision.code) == ("ok", "FIVE")
    assert await dld.recheck_deal_reference(
        {"deal_id": "deal_gone", "variant_id": "4401", "market": "US"}, ctx
    ) == (None, "no_deal")


async def test_recheck_refuses_a_reference_for_another_variant_or_market(db):
    await _seed(db)
    assert await dld.recheck_deal_reference(
        {"deal_id": "deal_a", "variant_id": "4402", "market": "US"}, _ctx()
    ) == (None, "reference_other_variant")
    assert await dld.recheck_deal_reference(
        {"deal_id": "deal_a", "variant_id": "4401", "market": "SG"}, _ctx()
    ) == (None, "reference_other_market")
    assert await dld.recheck_deal_reference({"deal_id": "deal_a"}, _ctx()) == (None, "bad_reference")


async def test_a_lapsed_verification_drops_the_code_at_click_time(db):
    await _seed(db, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                verified_at=datetime.now(timezone.utc) - timedelta(hours=25))
    ref = {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    assert await dld.recheck_deal_reference(ref, _ctx()) == (None, "no_emittable_deal:verification_expired")


async def test_flipping_a_flag_off_stops_links_already_issued(db, monkeypatch):
    await _seed(db)
    ref = {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    assert (await dld.recheck_deal_reference(ref, _ctx()))[1] == "ok"
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "false")
    assert await dld.recheck_deal_reference(ref, _ctx()) == (None, "discount_param_off")


@pytest.mark.parametrize(
    "env,ctx",
    [
        ({"DEAL_LANE_EMIT_ENABLED": "false"}, {}),
        ({}, {"platform": "wix"}),
        ({}, {"pin_country": None}),
    ],
)
async def test_nothing_is_read_while_off_or_off_lane(monkeypatch, env, ctx):
    monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "true")
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    async def _no_reads(*args, **kwargs):
        raise AssertionError("the deal decision read the database")

    monkeypatch.setattr(dld.database, "fetch_all", _no_reads)
    monkeypatch.setattr(dld.database, "fetch_one", _no_reads)
    decision, reason = await dld.resolve_deal_decision(_ctx(**ctx))
    assert decision is None and reason in {"emit_off", "not_shopify", "market_not_pinned"}
    ref = {"deal_id": "deal_a", "variant_id": "4401", "market": "US"}
    if ctx.get("pin_country", "US") is not None:
        assert (await dld.recheck_deal_reference(ref, _ctx(**ctx)))[0] is None


def test_every_flag_defaults_off_so_the_module_is_dark(monkeypatch):
    monkeypatch.delenv("DEAL_LANE_EMIT_ENABLED", raising=False)
    monkeypatch.delenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", raising=False)
    assert dld.current_switches() == dld.DealSwitches(emit_enabled=False, discount_param_enabled=False)
