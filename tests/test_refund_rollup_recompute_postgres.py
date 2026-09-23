"""A refund that arrives days after its order must reach that order's billed day.

gmv_attribution_daily buckets an edge by its UTC creation day, and the daily job only rolls up
yesterday. So every refund writer re-rolls the refunded edge's own day after its write commits:
attach_refund_to_attribution_edge (the Stripe refund and chargeback webhooks) and
RefundService.create_refund (merchant-initiated refunds). Without it, the invoice for that day
bills the pre-refund gross.

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_refund_rollup_test \\
        .venv/bin/python -m pytest tests/test_refund_rollup_recompute_postgres.py
"""

import asyncio
import os
import re
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
_TABLES = ("gmv_attribution_daily", "commerce_attribution_edges", "invoices", "billing_runs")
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_ROLLUP_DDL = _MIGRATIONS / "110_gmv_attribution_daily.sql"

MERCHANT = "m_late_refund"
N_DAYS_AGO = 5
BILLED_AT = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
    days=N_DAYS_AGO
)
BILLED_DAY = BILLED_AT.date()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges
    from db.sql_migrations import split_statements

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await database.execute(str(CreateTable(commerce_attribution_edges).compile(dialect=postgresql.dialect())))
    # Migration 109's columns; its channel_partners FK is not what is under test.
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN channel_partner_id BIGINT, ADD COLUMN take_rate_applied_bp SMALLINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    ddl = _ROLLUP_DDL.read_text(encoding="utf-8").replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", ""
    )
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    # invoices as production has it: migration 113's table, then 120's DATE periods.
    billing_core = (_MIGRATIONS / "113_billing_core.sql").read_text(encoding="utf-8")
    await database.execute(
        re.search(r"CREATE TABLE IF NOT EXISTS invoices \(.*?\n\);", billing_core, re.S).group(0)
    )
    # Migration 119's invoices.billing_run_id marks a GMV invoice (subscription invoices have none).
    # Only that statement: the rest of 119 alters billing tables this file does not build.
    migration_119 = (_MIGRATIONS / "119_invoice_finalizing_status.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS billing_run_id BIGINT" in migration_119
    await database.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_run_id BIGINT")
    for stmt in split_statements((_MIGRATIONS / "120_invoices_billing_period_to_date.sql").read_text(encoding="utf-8")):
        await database.execute(stmt)
    # billing_runs likewise: 113's table, 121's DATE periods, 122's partial_failed status.
    await database.execute(
        re.search(r"CREATE TABLE IF NOT EXISTS billing_runs \(.*?\n\);", billing_core, re.S).group(0)
    )
    for name in ("121_billing_runs_period_to_date.sql", "122_billing_runs_partial_failed_status.sql"):
        for stmt in split_statements((_MIGRATIONS / name).read_text(encoding="utf-8")):
            await database.execute(stmt)


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
        # DROP, not DELETE: these are reduced tables, and a later create_all(checkfirst=True)
        # in the same database would keep them instead of building the real ones.
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _billed_edge(db, *, edge_id="cae_1", order_id="ord_late", gross=10_000, created_at=BILLED_AT):
    """An edge created N days ago whose day the daily job has ALREADY rolled up."""
    await db.execute(
        "INSERT INTO commerce_attribution_edges "
        "(edge_id, merchant_id, order_id, agent_id, gross_attributed_gmv_cents, currency, "
        " refund_ids, refund_count, refunded_amount, created_at, updated_at) "
        "VALUES (:edge_id, :merchant_id, :order_id, 'agent_1', :gross, 'USD', "
        " '[]'::jsonb, 0, 0, :created_at, :created_at)",
        {"edge_id": edge_id, "merchant_id": MERCHANT, "order_id": order_id, "gross": gross,
         "created_at": created_at},
    )
    from services.gmv_aggregation_service import aggregate_daily

    await aggregate_daily(created_at.date())


async def _invoice_the_month(db, *, status="finalized", day=BILLED_DAY, merchant=MERCHANT, gmv=True):
    """A GMV invoice written by a (completed) billing run, or, with gmv=False, a subscription
    invoice the billing webhook mirrors into the same table with no billing_run_id."""
    run_id = None
    if gmv:
        run_id = await db.fetch_val(
            "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
            "VALUES (:s, :e, :k, 'completed') RETURNING id",
            {"s": day - timedelta(days=3), "e": day + timedelta(days=3), "k": f"inv-{merchant}-{status}-{day}"},
        )
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status, billing_run_id) "
        "VALUES (:m, :s, :e, :status, :r)",
        {"m": merchant, "s": day - timedelta(days=3), "e": day + timedelta(days=3), "status": status, "r": run_id},
    )


async def _start_billing_run(db, *, status="running", day=BILLED_DAY):
    await db.execute(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, :k, :status)",
        {"s": day - timedelta(days=3), "e": day + timedelta(days=3), "k": f"{day}-{status}", "status": status},
    )


async def _rollup(db, day=BILLED_DAY):
    row = await db.fetch_one(
        "SELECT gross_attributed_gmv_cents g, refund_amount_cents r, net_attributed_gmv_cents n, "
        "take_amount_cents t FROM gmv_attribution_daily WHERE date = :d AND merchant_id = :m",
        {"d": day, "m": MERCHANT},
    )
    return dict(row) if row else None


async def _edge_refund_cents(db, edge_id="cae_1"):
    return await db.fetch_val(
        "SELECT refund_amount_cents FROM commerce_attribution_edges WHERE edge_id = :e", {"e": edge_id}
    )


# --- The webhook writer: attach_refund_to_attribution_edge -----------------------------------


async def test_a_late_refund_reaches_the_edges_billed_day(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}

    result = await attach_refund_to_attribution_edge(
        order_id="ord_late", refund_id="re_1", amount=Decimal("25.00")
    )

    assert result is not None and result["edge_count"] == 1
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}
    # Only the edge's own day is billed; no row appears for the day the refund arrived.
    today = datetime.now(timezone.utc).date()
    assert await _rollup(db, today) is None


async def test_the_stripe_refund_webhook_finalizer_reaches_the_billed_day(db, monkeypatch):
    """The real Stripe refund finalizer, minor units in, with the order-level write stubbed."""
    import routes.webhook_routes as webhook_routes

    async def _finalized(order, **kwargs):
        return {"applied": True, "order_id": order["order_id"]}

    monkeypatch.setattr(webhook_routes, "finalize_refund_success", _finalized)
    monkeypatch.setenv("ATTRIBUTION_REVERSE_ON_REFUND", "true")
    await _billed_edge(db)

    await webhook_routes._finalize_stripe_refund_success(
        {"order_id": "ord_late", "merchant_id": MERCHANT, "currency": "USD"},
        refund_reference="re_hook",
        refund_amount_minor=4_000,
        currency="usd",
        refund_total=Decimal("40.00"),
    )

    assert await _rollup(db) == {"g": 10_000, "r": 4_000, "n": 6_000, "t": 600}


async def test_a_redelivered_refund_does_not_move_the_day_twice(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    for _ in range(2):
        await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_a_redelivery_heals_a_recompute_that_failed_the_first_time(db, monkeypatch):
    from services import gmv_aggregation_service as gmv
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    real = gmv.recompute_for_date

    async def _down(*a, **k):
        raise ConnectionError("db blip")

    monkeypatch.setattr(gmv, "recompute_for_date", _down)
    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}  # stale

    monkeypatch.setattr(gmv, "recompute_for_date", real)
    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_a_failed_recompute_never_fails_the_refund_or_undoes_it(db, monkeypatch):
    from services import gmv_aggregation_service as gmv
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)

    async def _down(*a, **k):
        raise ConnectionError("db blip")

    monkeypatch.setattr(gmv, "recompute_for_date", _down)
    result = await attach_refund_to_attribution_edge(
        order_id="ord_late", refund_id="re_1", amount=Decimal("25.00")
    )
    assert result is not None
    assert await _edge_refund_cents(db) == 2_500


async def test_a_failed_event_still_recomputes_the_day(db, monkeypatch):
    from services import commerce_attribution_service as cas

    async def _event_raises(*a, **k):
        raise RuntimeError("event sink down")

    monkeypatch.setattr(cas, "record_commerce_event_best_effort", _event_raises)
    await _billed_edge(db)
    with pytest.raises(RuntimeError):
        await cas.attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_an_edge_with_metadata_still_reaches_its_billed_day(db, monkeypatch):
    """Real edges carry metadata, and this stack returns JSONB as a str. The unstubbed event
    builder spreads it (`**metadata`) and raises TypeError, found 2026-09-23, not fixed here.
    The recompute must not depend on the event succeeding."""
    from services import commerce_attribution_service as cas

    monkeypatch.undo()  # the real event path, not the fixture's stub
    from services import gmv_aggregation_service as gmv

    async def _flat_rate(merchant_id):
        return 1000

    monkeypatch.setattr(gmv, "_take_rate_bp_for_merchant", _flat_rate)
    await _billed_edge(db)
    await db.execute(
        "UPDATE commerce_attribution_edges SET metadata = jsonb_build_object('source', 'agent')"
    )

    try:
        await cas.attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    except TypeError:
        pass  # the event bug above; the refund and the recompute must stand regardless

    assert await _edge_refund_cents(db) == 2_500
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_a_fanned_out_refund_recomputes_every_edges_own_day(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    earlier = BILLED_AT - timedelta(days=2)
    await _billed_edge(db, edge_id="cae_1")
    await _billed_edge(db, edge_id="cae_2", created_at=earlier)

    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("10.00"))

    assert await _rollup(db) == {"g": 10_000, "r": 1_000, "n": 9_000, "t": 900}
    assert await _rollup(db, earlier.date()) == {"g": 10_000, "r": 1_000, "n": 9_000, "t": 900}


async def test_the_edge_day_is_its_utc_day_not_the_session_day(db):
    """23:30 UTC is the next calendar day in Asia/Shanghai. The recompute must pick the UTC day
    the rollup bucketed the edge on, or it re-rolls a day the edge is not in."""
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    late_evening = BILLED_AT.replace(hour=23, minute=30)
    await db.execute("SET TIME ZONE 'Asia/Shanghai'")
    try:
        await _billed_edge(db, created_at=late_evening)
        await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
        assert await _rollup(db, BILLED_DAY) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}
    finally:
        await db.execute("SET TIME ZONE 'UTC'")


# --- A day an invoice already covers ---------------------------------------------------------


async def test_a_refund_on_an_invoiced_day_leaves_the_billed_rollup_and_keeps_the_refund(db, caplog):
    import logging

    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await _invoice_the_month(db)

    with caplog.at_level(logging.WARNING, logger="services.gmv_aggregation_service"):
        result = await attach_refund_to_attribution_edge(
            order_id="ord_late", refund_id="re_1", amount=Decimal("25.00")
        )

    assert result is not None
    assert await _edge_refund_cents(db) == 2_500  # the truth, for a manual credit
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}  # as invoiced
    # The manual-credit signal must name what to credit, not only the merchant and day.
    [flag] = [r.getMessage() for r in caplog.records if "skipped_invoiced_day" in r.getMessage()]
    assert "cae_1" in flag and MERCHANT in flag and BILLED_DAY.isoformat() in flag


async def test_a_voided_invoice_does_not_freeze_the_day(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await _invoice_the_month(db, status="void")
    await _invoice_the_month(db, merchant="m_someone_else")

    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))

    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


@pytest.mark.parametrize("status", ["running", "failed", "partial_failed"])
async def test_an_unfinished_billing_run_freezes_a_merchant_it_has_not_invoiced(db, status):
    """A failed attempt leaves Stripe draft items with no local record, and the resume reuses the
    draft. Re-rolling the day in between would make the resume bill against a draft that still holds
    the old lines, silently (review of #2280). Freezing keeps local and Stripe in agreement; the
    refund is logged for a manual credit like an invoiced day."""
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await _start_billing_run(db, status=status)

    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))

    assert await _edge_refund_cents(db) == 2_500
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}


async def test_a_completed_billing_run_that_did_not_invoice_the_merchant_does_not_freeze(db):
    """A completed run left no draft for a merchant it skipped (no Stripe customer, nothing to bill)."""
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await _start_billing_run(db, status="completed")
    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_the_nightly_first_rollup_is_not_frozen_by_an_unfinished_run(db):
    """Only a RE-ROLL checks unfinished runs. The nightly job must still write a day's first roll-up,
    or a run over an open month (the debug billing route) would stop the rest of it being billed."""
    from services.gmv_aggregation_service import aggregate_daily

    await _start_billing_run(db, status="running")
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, agent_id, gross_attributed_gmv_cents, "
        "currency, refund_ids, refund_count, refunded_amount, created_at, updated_at) VALUES ('cae_new', :m, 'ord_new', "
        "'agent_1', 10000, 'USD', '[]'::jsonb, 0, 0, :c, :c)", {"m": MERCHANT, "c": BILLED_AT})
    assert await aggregate_daily(BILLED_DAY) == 1
    assert (await _rollup(db))["g"] == 10_000


async def test_a_subscription_invoice_never_freezes_a_gmv_day(db):
    """The billing webhook mirrors subscription invoices into `invoices` over their own look-back
    periods (review of #2280): the nightly job and a re-roll must ignore them."""
    from services.commerce_attribution_service import attach_refund_to_attribution_edge
    from services.gmv_aggregation_service import aggregate_daily

    await _invoice_the_month(db, status="paid", gmv=False)
    await db.execute(
        "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, agent_id, gross_attributed_gmv_cents, "
        "currency, refund_ids, refund_count, refunded_amount, created_at, updated_at) VALUES ('cae_1', :m, 'ord_late', "
        "'agent_1', 10000, 'USD', '[]'::jsonb, 0, 0, :c, :c)", {"m": MERCHANT, "c": BILLED_AT})
    assert await aggregate_daily(BILLED_DAY) == 1
    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))
    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_an_invoice_being_written_blocks_the_reroll_until_it_commits_then_freezes_the_day(db):
    """The race this closes: the invoice run holds the day lock SHARED while it reads and bills;
    the re-roll takes it exclusively, waits for the invoice to commit, then finds it and leaves
    the day as billed. Before this, the re-roll could rewrite the day between the invoice run's
    read and its invoices row."""
    import asyncpg

    from services.gmv_aggregation_service import INVOICED_PERIOD_MANUAL_CREDIT, recompute_for_date

    await _billed_edge(db)
    await db.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 2500")
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute(
            "SELECT pg_advisory_xact_lock_shared(CAST(hashtext(CAST($1 AS text)) AS bigint))",
            f"gmv_attribution_daily:{BILLED_DAY.isoformat()}",
        )
        task = asyncio.ensure_future(recompute_for_date(BILLED_DAY, MERCHANT))
        await asyncio.sleep(0.5)
        assert not task.done(), "the re-roll did not wait for the invoice run's shared day lock"
        await other.execute(
            "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status, billing_run_id) "
            "VALUES ($1, $2, $3, 'draft', 1)",
            MERCHANT, BILLED_DAY - timedelta(days=1), BILLED_DAY + timedelta(days=1),
        )
        await tx.commit()
        outcome = await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()
    assert outcome == INVOICED_PERIOD_MANUAL_CREDIT
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}


async def test_a_reroll_behind_a_long_invoice_run_gives_up_instead_of_stalling(db, monkeypatch):
    """A refund webhook must not hang behind an invoice run holding the day across its Stripe
    calls: the re-roll's wait is bounded, reported as recompute_failed, and a redelivery retries."""
    import asyncpg

    from services import gmv_aggregation_service as gmv

    monkeypatch.setattr(gmv, "_RECOMPUTE_LOCK_TIMEOUT_QUERY", "SET LOCAL lock_timeout = '300ms'")
    await _billed_edge(db)
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute(
            "SELECT pg_advisory_xact_lock_shared(CAST(hashtext(CAST($1 AS text)) AS bigint))",
            f"gmv_attribution_daily:{BILLED_DAY.isoformat()}",
        )
        outcome = await asyncio.wait_for(
            gmv.recompute_days_for_edges([{"edge_id": "cae_1", "merchant_id": MERCHANT, "created_at": BILLED_AT}]),
            timeout=10,
        )
        await tx.rollback()
    finally:
        await other.close()
    assert outcome == {(MERCHANT, BILLED_DAY): gmv.RECOMPUTE_FAILED}


async def test_the_nightly_rollup_is_not_bounded_by_the_refund_timeout(db, monkeypatch):
    from services import gmv_aggregation_service as gmv

    monkeypatch.setattr(gmv, "_RECOMPUTE_LOCK_TIMEOUT_QUERY", "SELECT 'must not run'")
    seen = []
    real_execute = db.execute

    async def spy(query, values=None):
        seen.append(str(query))
        return await real_execute(query, values)

    monkeypatch.setattr(gmv.database, "execute", spy)
    await _billed_edge(db)
    await gmv.aggregate_daily(BILLED_DAY)
    assert not any("must not run" in q for q in seen)


async def test_a_cancelled_or_other_period_billing_run_does_not_freeze_the_day(db):
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await _start_billing_run(db, status="cancelled")
    await _start_billing_run(db, day=BILLED_DAY - timedelta(days=40))

    await attach_refund_to_attribution_edge(order_id="ord_late", refund_id="re_1", amount=Decimal("25.00"))

    assert await _rollup(db) == {"g": 10_000, "r": 2_500, "n": 7_500, "t": 750}


async def test_the_outcome_names_each_day_and_why(db, monkeypatch):
    from services.gmv_aggregation_service import recompute_days_for_edges

    other_day = BILLED_AT - timedelta(days=20)
    await _billed_edge(db, edge_id="cae_1")
    await _billed_edge(db, edge_id="cae_2", order_id="ord_other", created_at=other_day)
    await _invoice_the_month(db)
    edges = [dict(r) for r in await db.fetch_all("SELECT edge_id, merchant_id, created_at FROM commerce_attribution_edges")]

    assert await recompute_days_for_edges(edges) == {
        (MERCHANT, other_day.date()): "recomputed",
        (MERCHANT, BILLED_DAY): "invoiced_period_manual_credit",
    }


@pytest.mark.parametrize("table", ["invoices"])
async def test_an_invoice_check_that_fails_leaves_the_day_alone(db, table):
    """Fail closed: a day we could not check is a day we must not rewrite."""
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _billed_edge(db)
    await db.execute(f"DROP TABLE {table}")

    result = await attach_refund_to_attribution_edge(
        order_id="ord_late", refund_id="re_1", amount=Decimal("25.00")
    )

    assert result is not None and await _edge_refund_cents(db) == 2_500
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}


# --- The merchant-initiated writer: RefundService.create_refund -------------------------------


def _stub_refund_service_io(monkeypatch, service, *, psp_success=True):
    """Everything around the edge write, so the transaction and the edge write are real."""

    async def _none(*a, **k):
        return None

    async def _order(order_id):
        return {"order_id": order_id, "merchant_id": MERCHANT, "currency": "USD", "psp_used": "stripe"}

    async def _valid(order, amount):
        return {"valid": True}

    async def _psp(*a, **k):
        return {"success": True, "refund_id": "re_psp"} if psp_success else {"success": False, "error": "x"}

    monkeypatch.setattr(service, "_check_existing_refund", _none)
    monkeypatch.setattr(service, "_get_order_for_update", _order)
    monkeypatch.setattr(service, "_validate_refund", _valid)
    monkeypatch.setattr(service, "_create_refund_record", _none)
    monkeypatch.setattr(service, "_process_psp_refund", _psp)
    monkeypatch.setattr(service, "_update_refund_success", _none)
    monkeypatch.setattr(service, "_update_refund_failed", _none)
    monkeypatch.setattr(service, "_queue_for_retry", _none)


async def test_a_merchant_refund_reaches_the_edges_billed_day(db, monkeypatch):
    from services.refund_service import RefundService

    service = RefundService()
    _stub_refund_service_io(monkeypatch, service)
    await _billed_edge(db)

    result = await service.create_refund(order_id="ord_late", amount=30.0, reason="damaged")

    assert result["status"] == "success"
    assert await _rollup(db) == {"g": 10_000, "r": 3_000, "n": 7_000, "t": 700}


async def test_a_merchant_refund_recomputes_only_after_its_transaction_commits(db, monkeypatch):
    """At recompute time, a SECOND connection must already see the refund. Inside the refund's
    transaction it would not, and a failed recompute there would abort the transaction."""
    import asyncpg

    import services.refund_service as refund_module
    from services.refund_service import RefundService

    service = RefundService()
    _stub_refund_service_io(monkeypatch, service)
    await _billed_edge(db)
    seen_by_other_connection = []
    real = refund_module.recompute_days_for_edges

    async def _observed(rows):
        other = await asyncpg.connect(DATABASE_URL)
        try:
            seen_by_other_connection.append(
                await other.fetchval("SELECT refund_amount_cents FROM commerce_attribution_edges")
            )
        finally:
            await other.close()
        return await real(rows)

    monkeypatch.setattr(refund_module, "recompute_days_for_edges", _observed)
    await service.create_refund(order_id="ord_late", amount=30.0, reason="damaged")

    assert seen_by_other_connection == [3_000]
    assert await _rollup(db) == {"g": 10_000, "r": 3_000, "n": 7_000, "t": 700}


async def test_a_failed_merchant_refund_recompute_keeps_the_refund(db, monkeypatch):
    from services import gmv_aggregation_service as gmv
    from services.refund_service import RefundService

    service = RefundService()
    _stub_refund_service_io(monkeypatch, service)
    await _billed_edge(db)

    async def _down(*a, **k):
        raise ConnectionError("db blip")

    monkeypatch.setattr(gmv, "recompute_for_date", _down)
    result = await service.create_refund(order_id="ord_late", amount=30.0, reason="damaged")

    assert result["status"] == "success"
    assert await _edge_refund_cents(db) == 3_000


async def test_a_failed_psp_refund_touches_neither_edge_nor_day(db, monkeypatch):
    from services.refund_service import RefundService

    service = RefundService()
    _stub_refund_service_io(monkeypatch, service, psp_success=False)
    await _billed_edge(db)

    result = await service.create_refund(order_id="ord_late", amount=30.0, reason="damaged")

    assert result["status"] == "failed"
    assert await _edge_refund_cents(db) == 0
    assert await _rollup(db) == {"g": 10_000, "r": 0, "n": 10_000, "t": 1_000}


# --- The day lock ------------------------------------------------------------------------------


async def test_a_recompute_waits_for_another_recompute_of_the_same_day(db):
    """Two recomputes of one day must not interleave read and upsert: the one that read first
    could upsert last and write back a total missing the other's refund."""
    import asyncpg

    from services.gmv_aggregation_service import recompute_for_date

    await _billed_edge(db)
    other = await asyncpg.connect(DATABASE_URL)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute(
            "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST($1 AS text)) AS bigint))",
            f"gmv_attribution_daily:{BILLED_DAY.isoformat()}",
        )
        task = asyncio.ensure_future(recompute_for_date(BILLED_DAY, MERCHANT))
        await asyncio.sleep(0.5)
        assert not task.done(), "the recompute did not wait for the day's lock"
        # A refund lands while the recompute waits. It must read it once the lock frees.
        await other.execute("UPDATE commerce_attribution_edges SET refund_amount_cents = 1234")
        await tx.commit()
        await asyncio.wait_for(task, timeout=10)
    finally:
        await other.close()
    assert (await _rollup(db))["r"] == 1_234
