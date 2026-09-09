"""The shadow measurement, executed.

Shadow mode exists to produce ONE number — the rate at which enforcement would refuse a
checkout — and that number is the evidence the enforcement decision rests on. So the write and
the aggregation are not incidental plumbing here; they are the deliverable, and a `would_block`
that is stored wrong or aggregated wrong would arm (or veto) enforcement on a false premise.

Postgres-only because the migration's DDL is Postgres (JSONB, `NOW() - CAST(:window AS interval)`),
and because a SQLite stand-in would be testing a table this code never writes to in production.
"""

import datetime as dt
import json
import os

import pytest

from services import checkout_preflight as cp

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MIGRATION = "db/migrations/219_checkout_preflight_observations.sql"


async def _apply_migration(database):
    """Apply the DDL UNDER TEST, not a hand-written copy of it.

    A fixture that redeclared the columns would be testing the fixture — and this file's sibling
    (tests/test_backfill_variant_identity_skus_postgres.py) learned that the expensive way, with
    a hand-written table that had NOT NULLs prod does not have. Applied TWICE: schema_guard runs
    the same statements on every boot, so the migration has to be idempotent.
    """
    from db.sql_migrations import split_statements

    with open(MIGRATION, "r", encoding="utf-8") as fh:
        sql = fh.read()
    for _ in range(2):
        for statement in split_statements(sql):
            await database.execute(statement)


@pytest.fixture
async def db():
    from db.database import database
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _apply_migration(database)
    await database.execute("DELETE FROM checkout_preflight_observations")
    try:
        yield database
    finally:
        # Rows, not the table: the dialect gate shares ONE database across every _postgres.py
        # file, so dropping would hand the next file a missing table.
        await database.execute("DELETE FROM checkout_preflight_observations")
        if not was_connected and database.is_connected:
            await database.disconnect()


def _offer():
    return {
        "offer_id": "offer:pf:1", "sku_key": "ext:t::a::v:43062643884185",
        "product_key": "ext:t::a", "merchant_id": "m_seller",
        "currency": "USD", "merchant_effective_price": "24.00",
    }


async def test_the_migration_is_idempotent_and_the_table_is_usable(db):
    n = await db.fetch_val(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_name='checkout_preflight_observations'")
    assert n == 1


async def test_a_shadow_refusal_is_stored_with_would_block_true(db, monkeypatch):
    """The row has to record the decision ENFORCEMENT would have made, while the buyer was let
    through. If `would_block` were derived from the mode it would be false here, and the shadow
    report would show a 0% refusal rate no matter how bad the catalog was."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    v = cp.PreflightVerdict(
        outcome=cp.UNVERIFIABLE, reason=cp.R_UNVERIFIABLE, would_block=True,
        live_status="unverified", latency_ms=120, mode=cp.MODE_SHADOW,
        detail={"verify_reason": "timeout"},
    )
    await cp.record(v, _offer())
    row = dict(await db.fetch_one("SELECT * FROM checkout_preflight_observations"))
    assert row["mode"] == "shadow"
    assert row["outcome"] == cp.UNVERIFIABLE
    assert row["would_block"] is True
    assert row["reason"] == cp.R_UNVERIFIABLE
    assert row["sku_key"] == "ext:t::a::v:43062643884185"
    assert row["latency_ms"] == 120
    assert json.loads(row["detail"])["verify_reason"] == "timeout"
    assert row["price_verified"] is False


async def test_nothing_is_written_when_the_preflight_is_off(db, monkeypatch):
    monkeypatch.delenv("CHECKOUT_PREFLIGHT_MODE", raising=False)
    await cp.record(
        cp.PreflightVerdict(outcome=cp.BLOCK, reason=cp.R_GONE, would_block=True), _offer())
    assert await db.fetch_val("SELECT count(*) FROM checkout_preflight_observations") == 0


async def test_the_shadow_report_gives_the_refusal_rate_enforcement_would_produce(db, monkeypatch):
    """This aggregation IS the go/no-go for enforcement, so it is worth executing rather than
    trusting: three refusals and one pass must read as 75%, grouped by reason."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    for reason, blocked in (
        (cp.R_UNVERIFIABLE, True), (cp.R_UNVERIFIABLE, True),
        (cp.R_OUT_OF_STOCK, True), (cp.R_OK, False),
    ):
        await cp.record(
            cp.PreflightVerdict(
                outcome=(cp.OK if not blocked else cp.BLOCK), reason=reason,
                would_block=blocked, latency_ms=100, mode=cp.MODE_SHADOW),
            _offer())

    report = await cp.shadow_report(window_days=7)
    assert report["observations"] == 4
    assert report["would_block"] == 3
    assert report["would_block_rate"] == 0.75
    by = {r["reason"]: r for r in report["by_reason"]}
    assert int(by[cp.R_UNVERIFIABLE]["n"]) == 2
    assert int(by[cp.R_UNVERIFIABLE]["would_block"]) == 2
    assert int(by[cp.R_OK]["would_block"]) == 0


async def test_the_report_window_excludes_older_rows(db, monkeypatch):
    """A rate computed over all time would keep counting refusals from before a fix landed."""
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    await cp.record(
        cp.PreflightVerdict(outcome=cp.BLOCK, reason=cp.R_GONE, would_block=True,
                            mode=cp.MODE_SHADOW), _offer())
    await db.execute(
        "UPDATE checkout_preflight_observations SET created_at = :old",
        {"old": dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)})
    assert (await cp.shadow_report(window_days=7))["observations"] == 0
    assert (await cp.shadow_report(window_days=90))["observations"] == 1


async def test_an_empty_window_reports_no_rate_rather_than_zero(db):
    """0/0 must not read as "0% would be refused" — that is the number someone would arm
    enforcement on."""
    report = await cp.shadow_report(window_days=7)
    assert report["observations"] == 0
    assert report["would_block_rate"] is None


async def test_a_not_yet_checked_row_round_trips_and_lands_in_its_own_denominator(db, monkeypatch):
    """The new reason has to survive the INSERT and reach the report as ITSELF.

    Two ways this breaks that no in-memory test can see. The column is `VARCHAR(64) NOT NULL`
    with no CHECK today, but a future enum or length change would reject the value at write time
    — in prod, inside the one code path that exists to measure things, and `record` swallows its
    own failures, so the row would vanish silently. And the aggregation runs in SQL, so the split
    between the two denominators is only really exercised against a real database.
    """
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    for reason, blocked in (
        (cp.R_NOT_YET_CHECKED, True), (cp.R_NOT_YET_CHECKED, True), (cp.R_NOT_YET_CHECKED, True),
        (cp.R_GONE, True), (cp.R_OK, False),
    ):
        await cp.record(
            cp.PreflightVerdict(
                outcome=(cp.OK if not blocked else cp.UNVERIFIABLE), reason=reason,
                would_block=blocked, latency_ms=1, mode=cp.MODE_SHADOW),
            _offer())

    stored = await db.fetch_val(
        "SELECT count(*) FROM checkout_preflight_observations WHERE reason = :r",
        {"r": cp.R_NOT_YET_CHECKED})
    assert stored == 3, "the reason must survive the write unchanged"

    report = await cp.shadow_report(window_days=7)
    assert report["observations"] == 5
    # The raw rate still counts everything, and on a cold cache it is dominated by our own
    # homework — which is exactly why it must not be the only number in the report.
    assert report["would_block"] == 4
    assert report["would_block_rate"] == 0.8
    # The merchant's actual verdict: of the two that reached a merchant, one was refused.
    assert report["not_yet_checked"] == 3
    assert report["answered"] == 2
    assert report["would_block_rate_answered"] == 0.5
    assert report["not_yet_checked_rate"] == 0.6
