"""The partner adjustment adapter on real Postgres: the edge closed by the real closure, the
shared refund UPDATE, the metadata append, the row lock, and the daily rollup recompute.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_adjust_test \\
        .venv/bin/python -m pytest tests/test_partner_order_adjustments_postgres.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("gmv_attribution_daily", "commerce_attribution_edges", "surface_click_events", "invoices",
           "billing_runs")
_ROLLUP_DDL = (Path(__file__).resolve().parent.parent / "db/migrations/110_gmv_attribution_daily.sql")


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.sql_migrations import split_statements

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (surface_click_events, commerce_attribution_edges):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    # Migration 109's columns (its channel_partners FK is not what is under test), and the unique
    # guard the closure's ON CONFLICT targets.
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN channel_partner_id BIGINT, ADD COLUMN take_rate_applied_bp SMALLINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    await database.execute(
        "CREATE UNIQUE INDEX uq_edges_merchant_external_order "
        "ON commerce_attribution_edges (merchant_id, external_order_id)"
    )
    ddl = _ROLLUP_DDL.read_text(encoding="utf-8").replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", ""
    )
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    # Only the columns the invoiced-day guard reads (the real table carries Stripe ids and more).
    await database.execute(
        "CREATE TABLE invoices (id BIGSERIAL PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, "
        "billing_period_start DATE NOT NULL, billing_period_end DATE NOT NULL, status TEXT)"
    )
    await database.execute(
        "CREATE TABLE billing_runs (id BIGSERIAL PRIMARY KEY, period_start TIMESTAMPTZ NOT NULL, "
        "period_end TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'running')"
    )


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from services import commerce_attribution_service as cas
    from services import gmv_aggregation_service as gmv

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _no_event(*a, **k):
        return {"interaction_id": "int_stub"}

    async def _flat_rate(merchant_id):
        return 1000  # 10%; the promo lookup reads tables this test does not build

    monkeypatch.setattr(cas, "record_commerce_event_best_effort", _no_event)
    monkeypatch.setattr(gmv, "_take_rate_bp_for_merchant", _flat_rate)
    await _build_schema(database)
    try:
        yield database
    finally:
        # DROP, not DELETE: this file rebuilds these tables in a reduced shape (no migration-only
        # indexes or generated columns, no channel_partners FK). Leaving that behind would hand
        # later gate files a table `metadata.create_all(checkfirst=True)` skips. Absent tables are
        # rebuilt whole by whoever needs them next.
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001 -- best-effort cleanup must not mask the test result
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


YESTERDAY = datetime.now(timezone.utc) - timedelta(days=3)


async def _close_partner_edge(db, purchase_id="rp_abc", order="ord_1", gross=4500, currency="USD"):
    """A partner-reported edge written by the REAL closure, as the Reap lane writes it."""
    from services.commerce_attribution_service import close_external_order_conversion

    await close_external_order_conversion(
        merchant_id="brand.example", click_id="clk_1", external_order_id=order,
        gross_amount_cents=gross, currency=currency, converted_at=YESTERDAY,
        trusted_partner_provenance={"partner_reported": True, "purchase_id": purchase_id,
                                    "reap_checkout_id": "chk_1"},
        converting_shop_domain="brand.example",
    )
    # Bill it on an earlier day, as a real order would have been before its refund arrives.
    await db.execute("UPDATE commerce_attribution_edges SET created_at = :d", {"d": YESTERDAY})
    from services.gmv_aggregation_service import aggregate_daily

    await aggregate_daily(YESTERDAY.date())


async def _edge(db):
    row = await db.fetch_one("SELECT * FROM commerce_attribution_edges")
    return dict(row)


async def _rollup(db):
    row = await db.fetch_one(
        "SELECT gross_attributed_gmv_cents g, refund_amount_cents r, net_attributed_gmv_cents n, "
        "take_amount_cents t FROM gmv_attribution_daily WHERE date = :d", {"d": YESTERDAY.date()})
    return dict(row) if row else None


async def test_a_refund_reverses_the_edge_and_the_billed_day(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    assert await _rollup(db) == {"g": 4500, "r": 0, "n": 4500, "t": 450}

    r = await record_partner_order_adjustment(
        partner="reap", purchase_id="rp_abc", event_id="evt_1", kind="refund",
        currency="USD", amount_minor=1500, occurred_at=YESTERDAY + timedelta(days=2))
    assert (r.status, r.applied_minor, r.rollup_recomputed) == ("applied", 1500, True)

    e = await _edge(db)
    assert e["refund_amount_cents"] == 1500 and e["refund_count"] == 1
    assert e["latest_refund_id"] == "reap:evt_1"
    meta = e["metadata"] if isinstance(e["metadata"], dict) else json.loads(e["metadata"])
    assert [a["event_id"] for a in meta["partner_adjustments"]] == ["evt_1"]
    assert meta["partner_provenance"]["purchase_id"] == "rp_abc"  # untouched by the append
    # The day the order was billed on now bills the net, not the gross.
    assert await _rollup(db) == {"g": 4500, "r": 1500, "n": 3000, "t": 300}


async def test_a_redelivered_event_is_applied_once(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    kw = dict(partner="reap", purchase_id="rp_abc", event_id="evt_1", kind="refund",
              currency="USD", amount_minor=1500)
    assert (await record_partner_order_adjustment(**kw)).status == "applied"
    assert (await record_partner_order_adjustment(**kw)).status == "replayed"
    e = await _edge(db)
    assert e["refund_amount_cents"] == 1500 and e["refund_count"] == 1
    meta = e["metadata"] if isinstance(e["metadata"], dict) else json.loads(e["metadata"])
    assert len(meta["partner_adjustments"]) == 1


async def test_a_cancellation_after_a_partial_refund_takes_the_rest(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                          kind="refund", currency="USD", amount_minor=1000)
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_2",
                                              kind="cancellation", currency="USD")
    assert r.applied_minor == 3500
    assert (await _edge(db))["refund_amount_cents"] == 4500
    assert await _rollup(db) == {"g": 4500, "r": 4500, "n": 0, "t": 0}


async def test_an_over_refund_is_refused_and_leaves_no_trace(db):
    from services.partner_order_adjustments import AdjustmentRefused, record_partner_order_adjustment

    await _close_partner_edge(db)
    before = await _edge(db)
    with pytest.raises(AdjustmentRefused) as e:
        await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_9",
                                              kind="refund", currency="USD", amount_minor=4501)
    assert e.value.code == "exceeds_remaining"
    after = await _edge(db)
    assert after["refund_amount_cents"] == 0 and after["refund_ids"] in ([], "[]")
    assert after["updated_at"] == before["updated_at"]


async def test_dry_run_writes_nothing(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500, apply=False)
    assert r.status == "would_apply"
    assert (await _edge(db))["refund_amount_cents"] == 0
    assert (await _rollup(db))["r"] == 0


async def test_an_unknown_purchase_has_no_edge(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_other", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=100)
    assert r.status == "no_edge"


async def test_a_non_partner_edge_is_never_touched(db):
    """A webhook-closed edge with the same purchase id text in metadata but no partner flag."""
    from services.commerce_attribution_service import close_external_order_conversion
    from services.partner_order_adjustments import record_partner_order_adjustment

    await close_external_order_conversion(
        merchant_id="brand.example", click_id="clk_1", external_order_id="ord_w",
        gross_amount_cents=4500, currency="USD",
        trusted_partner_provenance={"purchase_id": "rp_abc"},  # partner_reported absent
    )
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=100)
    assert r.status == "no_edge"


async def test_the_edge_lock_serialises_two_refunds_that_together_exceed_the_gross(db):
    """A second connection holds the edge's row lock and spends most of the remainder. The
    adapter must WAIT for it and then check against what is left, not what was left when it
    started. Without FOR UPDATE both would pass the check and the edge would go negative."""
    import asyncio

    import asyncpg

    from services.partner_order_adjustments import AdjustmentRefused, record_partner_order_adjustment

    await _close_partner_edge(db)
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT 1 FROM commerce_attribution_edges FOR UPDATE")
        task = asyncio.ensure_future(record_partner_order_adjustment(
            partner="reap", purchase_id="rp_abc", event_id="evt_late", kind="refund",
            currency="USD", amount_minor=1000))
        await asyncio.sleep(0.5)
        assert not task.done(), "the adapter did not wait for the edge's row lock"
        await other.execute(
            "UPDATE commerce_attribution_edges SET refund_amount_cents = 4000, "
            "refund_ids = '[\"reap:evt_first\"]'::jsonb")
        await tx.commit()
        with pytest.raises(AdjustmentRefused) as e:
            await asyncio.wait_for(task, timeout=10)
        assert e.value.code == "exceeds_remaining"
    finally:
        await other.close()
    assert (await _edge(db))["refund_amount_cents"] == 4000



async def test_a_refund_after_the_promo_ended_never_raises_the_billed_take(db, monkeypatch):
    """Review of #2271 (P1): the day was rolled up at the 5% promo rate; the promo has since ended.
    The refund's re-roll must keep 5%, never re-rate the day at today's 10%."""
    from services import gmv_aggregation_service as gmv
    from services.partner_order_adjustments import record_partner_order_adjustment

    rates = iter([gmv.PROMO_TAKE_RATE_BP, gmv.STANDARD_TAKE_RATE_BP, gmv.STANDARD_TAKE_RATE_BP])

    async def flipping(merchant_id):
        return next(rates)

    monkeypatch.setattr(gmv, "_take_rate_bp_for_merchant", flipping)
    await _close_partner_edge(db)
    assert await _rollup(db) == {"g": 4500, "r": 0, "n": 4500, "t": 225}
    await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                          kind="refund", currency="USD", amount_minor=500)
    assert await _rollup(db) == {"g": 4500, "r": 500, "n": 4000, "t": 200}
    stored_bp = await db.fetch_val("SELECT take_rate_bp FROM gmv_attribution_daily")
    assert stored_bp == gmv.PROMO_TAKE_RATE_BP  # the stored rate, not only the amount, is kept


async def test_an_invoiced_day_is_left_as_billed_and_flagged(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status) "
        "VALUES ('brand.example', :s, :e, 'paid')",
        {"s": YESTERDAY.date() - timedelta(days=5), "e": YESTERDAY.date() + timedelta(days=5)},
    )
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500)
    assert (r.status, r.rollup, r.rollup_recomputed) == ("applied", "invoiced_period_manual_credit", False)
    assert (await _edge(db))["refund_amount_cents"] == 1500  # the truth is recorded on the edge
    assert await _rollup(db) == {"g": 4500, "r": 0, "n": 4500, "t": 450}  # billing untouched


async def test_a_void_invoice_does_not_block_the_reroll(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status) "
        "VALUES ('brand.example', :s, :e, 'void')",
        {"s": YESTERDAY.date(), "e": YESTERDAY.date()},
    )
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500)
    assert r.rollup == "recomputed"
    assert (await _rollup(db))["r"] == 1500


async def test_the_invoice_check_fails_closed(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await db.execute("DROP TABLE invoices")
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500)
    assert (r.status, r.rollup) == ("applied", "invoice_check_failed")
    assert (await _rollup(db))["r"] == 0


async def test_a_reroll_waits_for_another_reroll_of_the_same_day(db):
    """Review of #2271 (P2): two roll-ups of one day must not interleave read-then-upsert."""
    import asyncio

    import asyncpg

    from services.gmv_aggregation_service import recompute_for_date

    await _close_partner_edge(db)
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute(
            "SELECT pg_advisory_xact_lock(hashtext('gmv_attribution_daily:' || CAST($1::date AS TEXT)))",
            YESTERDAY.date())
        task = asyncio.ensure_future(recompute_for_date(YESTERDAY.date(), "brand.example"))
        await asyncio.sleep(0.5)
        assert not task.done(), "the re-roll did not wait for the day's lock"
        await tx.commit()
        await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()



async def test_a_day_a_billing_run_covers_is_left_alone_before_its_invoice_exists(db):
    """Re-review of #2271: the run is inserted before it reads the rollup; its invoice commits only
    after the Stripe calls. The guard must see the run, not wait for the invoice."""
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await db.execute("INSERT INTO billing_runs (period_start, period_end, status) VALUES (:s, :e, 'running')",
                     {"s": YESTERDAY - timedelta(days=5), "e": YESTERDAY + timedelta(days=5)})
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500)
    assert r.rollup == "invoiced_period_manual_credit"
    assert (await _rollup(db))["r"] == 0


async def test_a_cancelled_billing_run_does_not_block(db):
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    await db.execute("INSERT INTO billing_runs (period_start, period_end, status) VALUES (:s, :e, 'cancelled')",
                     {"s": YESTERDAY - timedelta(days=5), "e": YESTERDAY + timedelta(days=5)})
    r = await record_partner_order_adjustment(partner="reap", purchase_id="rp_abc", event_id="evt_1",
                                              kind="refund", currency="USD", amount_minor=1500)
    assert r.rollup == "recomputed"


async def test_a_replay_heals_a_failed_reroll(db, monkeypatch):
    """Re-review of #2271: the first re-roll fails; re-running the same event retries it."""
    from services import gmv_aggregation_service as gmv
    from services.partner_order_adjustments import record_partner_order_adjustment

    await _close_partner_edge(db)
    real = gmv.recompute_for_date
    calls = {"n": 0}

    async def fail_once(day, merchant_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return await real(day, merchant_id)

    monkeypatch.setattr(gmv, "recompute_for_date", fail_once)
    kw = dict(partner="reap", purchase_id="rp_abc", event_id="evt_1", kind="refund", currency="USD",
              amount_minor=1500)
    first = await record_partner_order_adjustment(**kw)
    assert (first.status, first.rollup) == ("applied", "recompute_failed")
    assert (await _rollup(db))["r"] == 0
    second = await record_partner_order_adjustment(**kw)
    assert (second.status, second.rollup) == ("replayed", "recomputed")
    assert await _rollup(db) == {"g": 4500, "r": 1500, "n": 3000, "t": 300}
