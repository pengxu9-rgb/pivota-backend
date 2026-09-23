"""The nightly stale-day sweep re-rolls any day whose edges changed after that day was rolled up.

gmv_attribution_daily buckets an edge by its UTC creation day, and the daily job rolls up only
yesterday. A refund writer re-rolls its edge's day itself (#2274), but only best-effort: a failure
is logged, the webhook still answers 2xx, and nothing retries it. A gross stamped at payment time
on an edge created at checkout re-rolls nothing at all. reroll_stale_days, run after the nightly
roll-up, finds those days by edges.updated_at > the day's rollup updated_at and re-rolls them
through recompute_days_for_edges, so an invoiced day stays as billed.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_rollup_sweep_test \\
        .venv/bin/python -m pytest tests/test_gmv_rollup_stale_day_sweep_postgres.py
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
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
_INDEX_MIGRATION = _MIGRATIONS / "237_commerce_attribution_edges_merchant_created_index.sql"
_INDEX_MIGRATION_DOWN = _MIGRATIONS / "down/237_commerce_attribution_edges_merchant_created_index_down.sql"
_INDEXES = {"idx_commerce_attribution_edges_merchant_created", "idx_commerce_attribution_edges_updated_at"}

MERCHANT = "m_sweep"
TODAY = datetime.now(timezone.utc).date()
DAY = TODAY - timedelta(days=2)  # a past day the nightly job has already rolled up


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


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
    # Migration 237 as the runner sends it: one statement at a time, outside a transaction.
    for stmt in split_statements(_INDEX_MIGRATION.read_text(encoding="utf-8")):
        await database.execute(stmt)
    ddl = (_MIGRATIONS / "110_gmv_attribution_daily.sql").read_text(encoding="utf-8").replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", ""
    )
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    # 110's trigger stamps NOW() on every UPDATE, which would undo _nightly's backdating. The upsert
    # sets updated_at = NOW() itself, so a roll-up writes the same value without it.
    await database.execute(
        "ALTER TABLE gmv_attribution_daily DISABLE TRIGGER trg_gmv_attribution_daily_updated_at"
    )
    billing_core = (_MIGRATIONS / "113_billing_core.sql").read_text(encoding="utf-8")
    await database.execute(
        re.search(r"CREATE TABLE IF NOT EXISTS invoices \(.*?\n\);", billing_core, re.S).group(0)
    )
    # Migration 119's invoices.billing_run_id: set on a GMV invoice, NULL on a mirrored subscription one.
    assert "ADD COLUMN IF NOT EXISTS billing_run_id BIGINT" in (
        _MIGRATIONS / "119_invoice_finalizing_status.sql"
    ).read_text(encoding="utf-8")
    await database.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_run_id BIGINT")
    for stmt in split_statements((_MIGRATIONS / "120_invoices_billing_period_to_date.sql").read_text(encoding="utf-8")):
        await database.execute(stmt)
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


async def _edge(db, *, edge_id, order_id, gross, created_at, merchant=MERCHANT, agent="agent_1", updated_at=None):
    """An edge as its writer left it: updated_at is its creation time until something changes it."""
    await db.execute(
        "INSERT INTO commerce_attribution_edges "
        "(edge_id, merchant_id, order_id, agent_id, gross_attributed_gmv_cents, currency, "
        " refund_ids, refund_count, refunded_amount, created_at, updated_at) "
        "VALUES (:edge_id, :merchant_id, :order_id, :agent, :gross, 'USD', "
        " '[]'::jsonb, 0, 0, :created_at, :updated_at)",
        {"edge_id": edge_id, "merchant_id": merchant, "order_id": order_id, "agent": agent,
         "gross": gross, "created_at": created_at, "updated_at": updated_at or created_at},
    )


async def _nightly(db, day=DAY):
    """The daily job's roll-up of `day`, as it ran at 02:00 UTC the morning after."""
    from services.gmv_aggregation_service import aggregate_daily

    await aggregate_daily(day)
    await db.execute(
        "UPDATE gmv_attribution_daily SET updated_at = :at WHERE date = :d",
        {"at": _at(day + timedelta(days=1), 2), "d": day},
    )


async def _rollup(db, day=DAY, merchant=MERCHANT):
    row = await db.fetch_one(
        "SELECT gross_attributed_gmv_cents g, refund_amount_cents r, net_attributed_gmv_cents n, "
        "take_rate_bp bp, take_amount_cents t, updated_at FROM gmv_attribution_daily "
        "WHERE date = :d AND merchant_id = :m",
        {"d": day, "m": merchant},
    )
    return dict(row) if row else None


def _sums(row):
    return None if row is None else {k: row[k] for k in ("g", "r", "n", "bp", "t")}


# --- The sweep heals what the writers left stale ---------------------------------------------


async def test_the_sweep_heals_a_refund_whose_own_recompute_failed(db, monkeypatch):
    from services import gmv_aggregation_service as gmv
    from services.commerce_attribution_service import attach_refund_to_attribution_edge

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    # The day was first billed on the promo rate. A re-roll must keep it (the upsert's rule), even
    # though _take_rate_bp_for_merchant answers 1000 now.
    await db.execute("UPDATE gmv_attribution_daily SET take_rate_bp = 500, take_amount_cents = 500")

    real = gmv.recompute_for_date

    async def _down(*a, **k):
        raise ConnectionError("db blip")

    monkeypatch.setattr(gmv, "recompute_for_date", _down)
    await attach_refund_to_attribution_edge(order_id="ord_1", refund_id="re_1", amount=Decimal("25.00"))
    # The refund stands on the edge, the webhook would have answered 2xx, and the day is stale.
    assert await db.fetch_val("SELECT refund_amount_cents FROM commerce_attribution_edges") == 2_500
    assert _sums(await _rollup(db)) == {"g": 10_000, "r": 0, "n": 10_000, "bp": 500, "t": 500}
    # The refund arrived at midnight; the sweep runs at 02:00.
    await db.execute(
        "UPDATE commerce_attribution_edges SET updated_at = :at", {"at": datetime.now(timezone.utc) - timedelta(hours=2)}
    )

    monkeypatch.setattr(gmv, "recompute_for_date", real)
    assert await gmv.reroll_stale_days() == {"stale_days": 1, "recomputed": 1}
    assert _sums(await _rollup(db)) == {"g": 10_000, "r": 2_500, "n": 7_500, "bp": 500, "t": 375}

    # Converged: the day's rows now post-date its edges, so the next night leaves it alone.
    assert await gmv.reroll_stale_days() == {"stale_days": 0}


@pytest.mark.parametrize("day_has_other_edges", [True, False], ids=["stale_row", "missing_row"])
async def test_the_sweep_heals_a_gross_stamped_after_the_checkout_day_was_rolled(db, day_has_other_edges):
    from services import gmv_aggregation_service as gmv
    from services.psp_payment_finalizer import stamp_gross_attributed_gmv

    # Checkout at 23:30 UTC on DAY (the next day in Asia), unpaid when DAY was rolled up.
    await _edge(db, edge_id="cae_late_pay", order_id="ord_late_pay", gross=None, created_at=_at(DAY, 23, 30))
    if day_has_other_edges:
        await _edge(db, edge_id="cae_paid", order_id="ord_paid", gross=4_000, created_at=_at(DAY, 9))
    await _nightly(db)
    before = _sums(await _rollup(db))
    assert before == ({"g": 4_000, "r": 0, "n": 4_000, "bp": 1000, "t": 400} if day_has_other_edges else None)

    # Payment lands today, and the finalizer stamps the gross on the checkout-day edge.
    await stamp_gross_attributed_gmv("ord_late_pay", subtotal=Decimal("60.00"))
    assert await db.fetch_val(
        "SELECT gross_attributed_gmv_cents FROM commerce_attribution_edges WHERE edge_id = 'cae_late_pay'"
    ) == 6_000
    assert _sums(await _rollup(db)) == before  # nothing re-rolled DAY

    assert await gmv.reroll_stale_days() == {"stale_days": 1, "recomputed": 1}
    gross = 10_000 if day_has_other_edges else 6_000
    assert _sums(await _rollup(db)) == {"g": gross, "r": 0, "n": gross, "bp": 1000, "t": gross // 10}
    assert await _rollup(db, DAY + timedelta(days=1)) is None  # the UTC day, not the payment day


async def test_the_sweep_heals_a_day_the_nightly_run_never_rolled(db):
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))

    assert await gmv.reroll_stale_days() == {"stale_days": 1, "recomputed": 1}
    assert _sums(await _rollup(db)) == {"g": 10_000, "r": 0, "n": 10_000, "bp": 1000, "t": 1_000}


@pytest.mark.parametrize("changed_before_rollup, stale", [(timedelta(minutes=30), True), (timedelta(hours=2), False)])
async def test_a_change_just_before_a_rollup_is_rerolled_in_case_the_rollup_missed_it(
    db, changed_before_rollup, stale
):
    """updated_at is stamped when the write's transaction starts (or from the app clock), not when
    it commits, so a roll-up that started in between post-dates a change it never read."""
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)  # rows stamped DAY+1 02:00
    await db.execute(
        "UPDATE commerce_attribution_edges SET refund_amount_cents = 2500, updated_at = :at",
        {"at": _at(DAY + timedelta(days=1), 2) - changed_before_rollup},
    )

    expected = {"stale_days": 1, "recomputed": 1} if stale else {"stale_days": 0}
    assert await gmv.reroll_stale_days() == expected
    assert _sums(await _rollup(db))["r"] == (2_500 if stale else 0)


async def _groups(db, day=DAY, merchant=MERCHANT):
    rows = await db.fetch_all(
        "SELECT agent_id, gross_attributed_gmv_cents g, net_attributed_gmv_cents n, take_amount_cents t "
        "FROM gmv_attribution_daily WHERE date = :d AND merchant_id = :m ORDER BY agent_id",
        {"d": day, "m": merchant},
    )
    return [(r["agent_id"], r["g"], r["n"], r["t"]) for r in rows]


async def test_an_edge_that_moved_agent_is_billed_once_after_the_reroll(db):
    """upsert_order_attribution_edge rewrites an edge's agent_id at payment time. A re-roll writes the
    new agent's group; the old group must be zeroed, or the day bills the same gross twice (review of
    #2283)."""
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _edge(db, edge_id="cae_2", order_id="ord_2", gross=3_000, created_at=_at(DAY, 13), agent="agent_3")
    await _nightly(db)
    assert await _groups(db) == [("agent_1", 10_000, 10_000, 1_000), ("agent_3", 3_000, 3_000, 300)]
    await db.execute(
        "UPDATE commerce_attribution_edges SET agent_id = 'agent_2', updated_at = :at WHERE edge_id = 'cae_1'",
        {"at": datetime.now(timezone.utc) - timedelta(hours=2)},
    )

    assert await gmv.reroll_stale_days() == {"stale_days": 1, "recomputed": 1}
    assert await _groups(db) == [
        ("agent_1", 0, 0, 0), ("agent_2", 10_000, 10_000, 1_000), ("agent_3", 3_000, 3_000, 300),
    ]
    # Converged: the zeroed row is not rewritten again.
    zeroed_at = await db.fetch_val(
        "SELECT updated_at FROM gmv_attribution_daily WHERE agent_id = 'agent_1'"
    )
    await gmv.recompute_for_date(DAY, MERCHANT)
    assert await db.fetch_val(
        "SELECT updated_at FROM gmv_attribution_daily WHERE agent_id = 'agent_1'"
    ) == zeroed_at


async def test_the_nightly_rollup_also_zeroes_a_group_its_edges_left(db):
    """The same double count through the all-merchant path: a refund's re-roll wrote yesterday early,
    then the edge moved agent before the nightly run."""
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _edge(db, edge_id="cae_o", order_id="ord_o", gross=2_000, created_at=_at(DAY, 12), merchant="m_other")
    await _nightly(db)
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_2' WHERE edge_id = 'cae_1'")

    await gmv.aggregate_daily(DAY)
    assert await _groups(db) == [("agent_1", 0, 0, 0), ("agent_2", 10_000, 10_000, 1_000)]
    assert await _groups(db, merchant="m_other") == [("agent_1", 2_000, 2_000, 200)]


async def test_a_moved_edge_on_an_invoiced_day_leaves_every_billed_row_alone(db):
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status, billing_run_id) "
        "VALUES (:m, :s, :e, 'finalized', 1)",
        {"m": MERCHANT, "s": DAY - timedelta(days=3), "e": DAY},
    )
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_2', updated_at = NOW()")

    assert await gmv.reroll_stale_days() == {"stale_days": 1, "invoiced_period_manual_credit": 1}
    await gmv.aggregate_daily(DAY)
    assert await _groups(db) == [("agent_1", 10_000, 10_000, 1_000)]


async def test_an_edge_that_moved_channel_partner_is_billed_once(db):
    """The group key is (agent, channel partner): a partner change leaves the old group too."""
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await db.execute("UPDATE commerce_attribution_edges SET channel_partner_id = 7")
    await _nightly(db)
    await db.execute("UPDATE commerce_attribution_edges SET channel_partner_id = NULL")

    await gmv.recompute_for_date(DAY, MERCHANT)
    rows = await db.fetch_all(
        "SELECT channel_partner_id p, gross_attributed_gmv_cents g FROM gmv_attribution_daily "
        "WHERE date = :d ORDER BY channel_partner_id NULLS FIRST", {"d": DAY},
    )
    assert [(r["p"], r["g"]) for r in rows] == [(None, 10_000), (7, 0)]


async def test_a_one_merchant_recompute_never_zeroes_another_merchants_rows(db):
    """From the re-review of #2283: replacing the zeroing's merchant filter with a no-op passed
    every other test."""
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _edge(db, edge_id="cae_o", order_id="ord_o", gross=2_000, created_at=_at(DAY, 12), merchant="m_other")
    await _nightly(db)
    # m_other's only edge stops billing (becomes inferred): its group is unformed, but only a roll-up
    # of m_other, or of the whole day, may zero it.
    await db.execute(
        "UPDATE commerce_attribution_edges SET metadata = '{\"inferred\": true}'::jsonb WHERE edge_id = 'cae_o'"
    )

    assert await gmv.recompute_for_date(DAY, MERCHANT) == gmv.RECOMPUTED
    assert await _groups(db, merchant="m_other") == [("agent_1", 2_000, 2_000, 200)]
    await gmv.aggregate_daily(DAY)
    assert await _groups(db, merchant="m_other") == [("agent_1", 0, 0, 0)]


async def test_the_zeroing_never_wipes_what_the_same_rollup_wrote_outside_one_transaction(db, monkeypatch):
    """From the re-review of #2283. If the statements ever run outside one server transaction (the
    databases shared-connection residual: autocommit after an out-of-order root rollback), each gets
    its own NOW(). A clock-based zeroing then wiped every row it had just written, and stamped them
    fresh so the sweep never healed the day."""
    import contextlib

    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _edge(db, edge_id="cae_2", order_id="ord_2", gross=3_000, created_at=_at(DAY, 13), agent="agent_3")

    @contextlib.asynccontextmanager
    async def _autocommit():
        yield

    monkeypatch.setattr(gmv.database, "transaction", _autocommit)
    result = await gmv._aggregate_for_date(DAY)

    assert (result["written"], result["zeroed"]) == (2, 0)
    assert await _groups(db) == [("agent_1", 10_000, 10_000, 1_000), ("agent_3", 3_000, 3_000, 300)]


async def test_a_moved_edge_is_billed_once_with_the_production_trigger_on(db):
    """From the re-review of #2283: 110's BEFORE UPDATE trigger on, as in prod, and nothing backdated."""
    from services import gmv_aggregation_service as gmv

    await db.execute("ALTER TABLE gmv_attribution_daily ENABLE TRIGGER trg_gmv_attribution_daily_updated_at")
    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _edge(db, edge_id="cae_2", order_id="ord_2", gross=3_000, created_at=_at(DAY, 13), agent="agent_3")
    await gmv.aggregate_daily(DAY)
    await gmv.aggregate_daily(DAY)  # a plain re-roll zeroes nothing
    assert await _groups(db) == [("agent_1", 10_000, 10_000, 1_000), ("agent_3", 3_000, 3_000, 300)]

    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_2' WHERE edge_id = 'cae_1'")
    await gmv.recompute_for_date(DAY, MERCHANT)
    assert await _groups(db) == [
        ("agent_1", 0, 0, 0), ("agent_2", 10_000, 10_000, 1_000), ("agent_3", 3_000, 3_000, 300),
    ]


async def test_a_zeroed_group_that_regains_its_edge_keeps_its_first_rate(db):
    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    await db.execute("UPDATE gmv_attribution_daily SET take_rate_bp = 500, take_amount_cents = 500")
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_2'")
    await gmv.recompute_for_date(DAY, MERCHANT)
    await db.execute("UPDATE commerce_attribution_edges SET agent_id = 'agent_1'")
    await gmv.recompute_for_date(DAY, MERCHANT)

    assert await _groups(db) == [("agent_1", 10_000, 10_000, 500), ("agent_2", 0, 0, 0)]


async def test_an_unfinished_run_also_blocks_the_zeroing(db):
    from services import gmv_aggregation_service as gmv

    await db.execute(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, 'open-run', 'partial_failed')",
        {"s": DAY - timedelta(days=5), "e": DAY + timedelta(days=1)},
    )
    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    await db.execute(
        "UPDATE commerce_attribution_edges SET agent_id = 'agent_2', updated_at = :at",
        {"at": datetime.now(timezone.utc) - timedelta(hours=2)},
    )

    assert await gmv.reroll_stale_days() == {"stale_days": 1, "invoiced_period_manual_credit": 1}
    assert await _groups(db) == [("agent_1", 10_000, 10_000, 1_000)]


async def test_an_unfinished_run_freezes_a_reroll_but_not_a_first_rollup(db):
    """#2280 freezes a day an unfinished billing run covers, for a RE-ROLL only: its draft already
    holds the day's lines. A day never rolled up has no lines in any draft, so the sweep writing it is
    a first roll-up, like the nightly job's (review of #2283)."""
    from services import gmv_aggregation_service as gmv

    await db.execute(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, 'open-run', 'failed')",
        {"s": DAY - timedelta(days=5), "e": DAY + timedelta(days=1)},
    )
    missed = DAY - timedelta(days=1)
    await _edge(db, edge_id="cae_missed", order_id="ord_m", gross=4_000, created_at=_at(missed, 12),
                updated_at=datetime.now(timezone.utc) - timedelta(hours=2))
    await _edge(db, edge_id="cae_rolled", order_id="ord_r", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    await db.execute(
        "UPDATE commerce_attribution_edges SET refund_amount_cents = 2500, updated_at = :at "
        "WHERE edge_id = 'cae_rolled'",
        {"at": datetime.now(timezone.utc) - timedelta(hours=2)},
    )

    assert await gmv.reroll_stale_days() == {
        "stale_days": 2, "recomputed": 1, "invoiced_period_manual_credit": 1,
    }
    assert _sums(await _rollup(db, missed))["g"] == 4_000  # first roll-up: written
    assert _sums(await _rollup(db))["r"] == 0  # re-roll under the unfinished run: frozen


async def test_two_missed_nightly_runs_still_heal_an_early_morning_edge(db):
    """An edge created at 01:00 on d is first eligible at d+1 02:00. With d+1 and d+2 missed, the
    d+3 02:00 run must still see it (review of #2283: a 3-day window started at d 02:00)."""
    from services import gmv_aggregation_service as gmv

    d = TODAY - timedelta(days=3)
    await _edge(db, edge_id="cae_early", order_id="ord_e", gross=5_000, created_at=_at(d, 0, 1))

    assert await gmv.reroll_stale_days(now=_at(d + timedelta(days=3), 2, 30)) == {
        "stale_days": 1, "recomputed": 1,
    }
    assert _sums(await _rollup(db, d))["g"] == 5_000


# --- ... and leaves billed, fresh and current days alone -------------------------------------


async def test_the_sweep_never_rewrites_an_invoiced_day(db, caplog):
    import logging

    from services import gmv_aggregation_service as gmv

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=10_000, created_at=_at(DAY, 12))
    # Another merchant on the day after the billed period: the sweep must go on and heal it.
    after = DAY + timedelta(days=1)
    await _edge(db, edge_id="cae_2", order_id="ord_2", gross=8_000, created_at=_at(after, 13), merchant="m_other")
    await _nightly(db)
    await _nightly(db, after)
    # A completed billing run invoiced MERCHANT for a period ending on DAY.
    run_id = await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, 'sweep-run', 'completed') RETURNING id",
        {"s": DAY - timedelta(days=3), "e": DAY},
    )
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status, billing_run_id) "
        "VALUES (:m, :s, :e, 'finalized', :run)",
        {"m": MERCHANT, "s": DAY - timedelta(days=3), "e": DAY, "run": run_id},
    )
    billed = await _rollup(db)
    # Both edges are refunded after the invoice went out.
    await db.execute(
        "UPDATE commerce_attribution_edges SET refund_amount_cents = 2500, updated_at = NOW()"
    )

    with caplog.at_level(logging.WARNING, logger="services.gmv_aggregation_service"):
        summary = await gmv.reroll_stale_days()

    assert summary == {"stale_days": 2, "invoiced_period_manual_credit": 1, "recomputed": 1}
    assert await _rollup(db) == billed  # untouched, updated_at included
    assert _sums(await _rollup(db, after, merchant="m_other")) == {"g": 8_000, "r": 2_500, "n": 5_500, "bp": 1000, "t": 550}
    assert any(
        "gmv_rollup_recompute_skipped_invoiced_day" in r.getMessage() and "cae_1" in r.getMessage()
        for r in caplog.records
    )


async def test_the_sweep_leaves_fresh_days_today_and_old_changes_alone(db):
    from services import gmv_aggregation_service as gmv

    # Rolled and unchanged since.
    await _edge(db, edge_id="cae_fresh", order_id="ord_fresh", gross=10_000, created_at=_at(DAY, 12))
    await _nightly(db)
    fresh = await _rollup(db)
    # Today's edge is tomorrow's nightly run's to roll.
    await _edge(db, edge_id="cae_today", order_id="ord_today", gross=3_000,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    # A change older than the lookback: stale, but outside what the nightly sweep retries.
    old_day = TODAY - timedelta(days=10)
    await _edge(db, edge_id="cae_old", order_id="ord_old", gross=5_000, created_at=_at(old_day, 12))
    await _nightly(db, old_day)
    await db.execute(
        "UPDATE commerce_attribution_edges SET refund_amount_cents = 1000, updated_at = :at WHERE edge_id = 'cae_old'",
        {"at": datetime.now(timezone.utc) - timedelta(days=gmv.STALE_SWEEP_LOOKBACK_DAYS, hours=1)},
    )

    assert await gmv.reroll_stale_days() == {"stale_days": 0}
    assert await _rollup(db) == fresh
    assert await _rollup(db, TODAY) is None
    assert _sums(await _rollup(db, old_day))["r"] == 0

    # After an outage, an operator widens the lookback by hand.
    assert await gmv.reroll_stale_days(lookback_days=14) == {"stale_days": 1, "recomputed": 1}
    assert _sums(await _rollup(db, old_day))["r"] == 1_000


async def test_the_sweep_rolls_the_oldest_stale_days_first_up_to_its_cap(db):
    from services import gmv_aggregation_service as gmv

    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    for n, day in enumerate((DAY, DAY - timedelta(days=5))):
        await _edge(db, edge_id=f"cae_{n}", order_id=f"ord_{n}", gross=1_000, created_at=_at(day, 12),
                    updated_at=two_hours_ago)

    assert await gmv.reroll_stale_days(max_days=1) == {"stale_days": 1, "recomputed": 1}
    assert await _rollup(db, DAY - timedelta(days=5)) is not None
    assert await _rollup(db, DAY) is None
    assert await gmv.reroll_stale_days(max_days=1) == {"stale_days": 1, "recomputed": 1}
    assert await _rollup(db, DAY) is not None


# --- The rollup's day predicate --------------------------------------------------------------


async def test_the_day_range_selects_exactly_the_utc_day_under_any_session_timezone(db):
    """The range replaced `(created_at AT TIME ZONE 'UTC')::date = :date`; it must pick the same rows."""
    from services.gmv_aggregation_service import _ROLLUP_QUERY

    edges = {
        "prev_last": (DAY - timedelta(days=1), 23, 59, 59, 999_999),
        "first": (DAY, 0, 0, 0, 0),
        "last": (DAY, 23, 59, 59, 999_999),
        "next_first": (DAY + timedelta(days=1), 0, 0, 0, 0),
    }
    for n, (edge_id, (d, h, mi, s, us)) in enumerate(edges.items()):
        at = datetime(d.year, d.month, d.day, h, mi, s, us, tzinfo=timezone.utc)
        await _edge(db, edge_id=edge_id, order_id=f"ord_{n}", gross=10 ** n, created_at=at)

    old_predicate = "SELECT SUM(gross_attributed_gmv_cents) FROM commerce_attribution_edges " \
                    "WHERE (created_at AT TIME ZONE 'UTC')::date = :date"
    for tz in ("Asia/Tokyo", "America/Los_Angeles", "UTC"):
        async with db.transaction():
            await db.execute(f"SET LOCAL TIME ZONE '{tz}'")
            rows = await db.fetch_all(_ROLLUP_QUERY, {"date": DAY, "merchant_id": MERCHANT})
            old = await db.fetch_val(old_predicate, {"date": DAY})
        assert [(r["date"], r["gross_sum"]) for r in rows] == [(DAY, 10 + 100)], tz
        assert old == 110, tz


async def test_a_one_merchant_rollup_reads_the_day_through_the_merchant_created_index(db):
    from services.gmv_aggregation_service import _ROLLUP_QUERY

    await _edge(db, edge_id="cae_1", order_id="ord_1", gross=1_000, created_at=_at(DAY, 12))
    async with db.transaction():
        # The table is tiny; forbid the seq scan so the plan shows what the predicate CAN use.
        await db.execute("SET LOCAL enable_seqscan = off")
        plan = await db.fetch_val(
            "EXPLAIN (FORMAT JSON) " + _ROLLUP_QUERY, {"date": DAY, "merchant_id": MERCHANT}
        )
    plan = json.loads(plan) if isinstance(plan, str) else plan

    def _nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from _nodes(child)

    conds = [
        n.get("Index Cond", "") for n in _nodes(plan[0]["Plan"])
        if n.get("Index Name") == "idx_commerce_attribution_edges_merchant_created"
    ]
    # Both columns in the index condition: the day is an index range, not a filter over every edge
    # of the merchant. `(created_at AT TIME ZONE 'UTC')::date = ...` puts only merchant_id here.
    assert conds and "merchant_id" in conds[0] and "created_at" in conds[0], plan


async def test_migration_237_builds_and_drops_its_indexes(db):
    from db.sql_migrations import needs_autocommit, split_statements

    names = "SELECT indexname FROM pg_indexes WHERE tablename = 'commerce_attribution_edges'"
    assert _INDEXES <= {r["indexname"] for r in await db.fetch_all(names)}
    assert needs_autocommit(_INDEX_MIGRATION.read_text(encoding="utf-8"))

    for stmt in split_statements(_INDEX_MIGRATION_DOWN.read_text(encoding="utf-8")):
        await db.execute(stmt)
    assert not _INDEXES & {r["indexname"] for r in await db.fetch_all(names)}
    # Re-runnable: IF NOT EXISTS on the way up.
    for _ in range(2):
        for stmt in split_statements(_INDEX_MIGRATION.read_text(encoding="utf-8")):
            await db.execute(stmt)
    assert _INDEXES <= {r["indexname"] for r in await db.fetch_all(names)}
