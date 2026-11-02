"""The deal decision's reads on real Postgres (services/deal_lane_decision.py): TIMESTAMPTZ and NUMERIC
come back as asyncpg types, the join reads only verified rows, and stop rows and opt-outs hold at issue
and click time. The rules themselves: tests/test_deal_lane_decision.py.

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55433/pivota_deal_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_lane_decision_postgres.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG_275 = Path(__file__).resolve().parent.parent / "db" / "migrations" / "275_deal_lane_tables.sql"
_TABLES = ("observed_store_offer_verifications", "observed_store_offers", "deal_lane_stops", "deal_lane_optout")
REF = {"deal_id": "dlr_a", "variant_id": "4401", "market": "US"}


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write deal rows in {dbname!r}; throwaway only")


@pytest.fixture
async def db(monkeypatch):
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for stmt in split_statements(_MIG_275.read_text()):  # IF NOT EXISTS throughout
        await database.execute(stmt)

    async def _clean():
        for table in _TABLES:
            await database.execute(f"DELETE FROM {table}")

    await _clean()
    monkeypatch.setenv("DEAL_LANE_EMIT_ENABLED", "true")
    monkeypatch.setenv("DEAL_LANE_DISCOUNT_PARAM_ENABLED", "true")
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


def _ctx(**overrides):
    from services.deal_lane_decision import DealContext

    base = dict(
        seller_id="seller_a", variant_id="4401", platform="shopify", quantity=1, pin_country="US",
        observed_unit_price=Decimal("40"), observed_currency="USD",
    )
    base.update(overrides)
    return DealContext(**base)


async def _seed(db, deal_id="dlr_a", code="GLOW10", discount="4.00", state="checkout_verified", unit="40.00"):
    from db.deal_lane import observed_store_offer_key

    now = datetime.now(timezone.utc)
    await db.execute(
        """
        INSERT INTO observed_store_offers
          (deal_id, deal_key, seller_id, market, code, kind, value, currency, source, eligibility_known, observed_at)
        VALUES (:deal_id, :deal_key, 'seller_a', 'US', :code, 'percent', 10, 'USD', 'merchant_published', TRUE, :now)
        """,
        {
            "deal_id": deal_id,
            "code": code,
            "now": now,
            "deal_key": observed_store_offer_key(
                seller_id="seller_a", market="US", code=code, platform_rule_ref=None, program_id=None
            ),
        },
    )
    verified = state == "checkout_verified"
    await db.execute(
        """
        INSERT INTO observed_store_offer_verifications
          (deal_id, variant_id, state, verified_at, expires_at, verified_via, authorized_by,
           ref_unit_price, ref_subtotal, ref_discount_amount, ref_currency, ref_automatic_discounts)
        VALUES (:deal_id, '4401', :state, :verified_at, :expires_at, :via, :auth, CAST(:unit AS NUMERIC), 40.00,
                CAST(:discount AS NUMERIC), 'USD', '[]'::jsonb)
        """,
        {
            "deal_id": deal_id,
            "state": state,
            "verified_at": now - timedelta(hours=1) if verified else None,
            "expires_at": now + timedelta(hours=23) if verified else None,
            "via": "shopify_storefront_connected" if verified else None,
            "auth": "connected" if verified else None,
            "discount": discount,
            "unit": unit,
        },
    )


async def test_resolve_reads_postgres_types_and_picks_the_largest_allocation(db):
    from services.deal_lane_decision import resolve_deal_decision

    await _seed(db)
    await _seed(db, "dlr_b", code="FIVE", discount="2.00")
    await _seed(db, "dlr_c", code="BIG", discount="9.00", state="unverified")
    decision, reason = await resolve_deal_decision(_ctx())
    assert reason == "ok"
    assert (decision.deal_id, decision.code, decision.estimated_effective_price) == ("dlr_a", "GLOW10", Decimal("36.00"))
    assert decision.valid_until.tzinfo is not None
    assert decision.reference() == REF


async def test_stop_rows_and_opt_outs_hold_at_issue_and_click_time(db):
    from services.deal_lane_decision import recheck_deal_reference, resolve_deal_decision

    await _seed(db)
    click_ctx = _ctx(observed_unit_price=None, observed_currency=None)
    assert (await recheck_deal_reference(REF, click_ctx))[1] == "ok"
    await db.execute("INSERT INTO deal_lane_stops (stop_id, market, stop_trigger) VALUES ('dlr_s1', 'US', 'absolute')")
    assert await resolve_deal_decision(_ctx()) == (None, "market_stopped")
    assert await recheck_deal_reference(REF, click_ctx) == (None, "market_stopped")
    # A stop in another market changes nothing here.
    await db.execute(
        "UPDATE deal_lane_stops SET cleared_at = NOW(), cleared_by = 'operator_a' WHERE stop_id = 'dlr_s1'"
    )
    await db.execute("INSERT INTO deal_lane_stops (stop_id, market, stop_trigger) VALUES ('dlr_s2', 'SG', 'rate')")
    assert (await resolve_deal_decision(_ctx()))[1] == "ok"
    await db.execute(
        "INSERT INTO deal_lane_optout (seller_id, requested_at, recorded_by) VALUES ('seller_a', NOW(), 'operator_a')"
    )
    assert await recheck_deal_reference(REF, click_ctx) == (None, "seller_opted_out")


async def test_a_verification_set_back_to_unverified_drops_the_code_at_click_time(db):
    from services.deal_lane_decision import recheck_deal_reference

    await _seed(db)
    click_ctx = _ctx(observed_unit_price=None, observed_currency=None)
    assert (await recheck_deal_reference(REF, click_ctx))[1] == "ok"
    # What D3's invalidation does on a price, sale or automatic-discount change.
    await db.execute(
        "UPDATE observed_store_offer_verifications SET state = 'unverified' WHERE deal_id = 'dlr_a'"
    )
    assert await recheck_deal_reference(REF, click_ctx) == (None, "no_deal")


async def test_a_nan_amount_postgres_stores_is_refused_and_never_hides_a_good_deal(db):
    """NUMERIC stores 'NaN', and 'NaN' > 0 is true in Postgres, so the verified-is-complete CHECK
    lets it in. The decision must refuse it without raising."""
    from services.deal_lane_decision import recheck_deal_reference, resolve_deal_decision

    await _seed(db)
    await _seed(db, "dlr_nan", code="NANCODE", discount="NaN")
    await _seed(db, "dlr_nan_unit", code="NANUNIT", unit="NaN")
    decision, reason = await resolve_deal_decision(_ctx())
    assert (reason, decision.deal_id) == ("ok", "dlr_a")
    click_ctx = _ctx(observed_unit_price=None, observed_currency=None)
    for deal_id in ("dlr_nan", "dlr_nan_unit"):
        ref = {"deal_id": deal_id, "variant_id": "4401", "market": "US"}
        assert await recheck_deal_reference(ref, click_ctx) == (None, "no_emittable_deal:reference_cart_incomplete")
