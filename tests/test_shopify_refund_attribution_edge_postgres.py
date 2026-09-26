"""A Shopify refunds/create must reach the attribution edge its order's orders/paid closed.

orders/paid closes an external edge through close_external_order_conversion, keyed by
(subject merchant, Shopify order id) with a synthetic `ext_...` order_id. That edge bills its gross
in gmv_attribution_daily. Before this, the Shopify refund webhook wrote only the commerce event
ledger, so a refunded order was invoiced on its full gross.

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_shopify_refund_edge_test \\
        python -m pytest tests/test_shopify_refund_attribution_edge_postgres.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

# A local throwaway (`..._test`) or the CI gate's own database (postgres-dialect-gate.yml).
_SAFE_DB_MARKERS = ("_test", "dialect_check")
# Every table this file builds, reduced to what is under test. All are DROPPED in teardown.
_TABLES = (
    "gmv_attribution_daily",
    "commerce_attribution_edges",
    "surface_click_events",
    "invoices",
    "billing_runs",
    "orders",
)
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"

MERCHANT = "m_shop_refund"
SHOPIFY_ORDER_ID = "5550001"
N_DAYS_AGO = 6
BILLED_AT = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(
    days=N_DAYS_AGO
)
BILLED_DAY = BILLED_AT.date()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _drop_tables(database):
    for table in _TABLES:
        try:
            await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        except Exception:  # noqa: BLE001
            continue


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.sql_migrations import split_statements

    await _drop_tables(database)
    dialect = postgresql.dialect()
    for table in (surface_click_events, commerce_attribution_edges):
        await database.execute(str(CreateTable(table).compile(dialect=dialect)))
        # The close's ON CONFLICT (merchant_id, external_order_id) needs the unique index.
        for index in table.indexes:
            await database.execute(str(CreateIndex(index).compile(dialect=dialect)))
    # Migration 109's columns; its channel_partners FK is not what is under test.
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN channel_partner_id BIGINT, ADD COLUMN take_rate_applied_bp SMALLINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    ddl = (_MIGRATIONS / "110_gmv_attribution_daily.sql").read_text(encoding="utf-8").replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", ""
    )
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    # invoices and billing_runs as production has them: the recompute's "already billed" check.
    billing_core = (_MIGRATIONS / "113_billing_core.sql").read_text(encoding="utf-8")
    for table in ("invoices", "billing_runs"):
        await database.execute(
            re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", billing_core, re.S).group(0)
        )
    # Migration 119's invoices.billing_run_id marks a GMV invoice, the only kind that freezes a day.
    await database.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_run_id BIGINT")
    for name in (
        "120_invoices_billing_period_to_date.sql",
        "121_billing_runs_period_to_date.sql",
        "122_billing_runs_partial_failed_status.sql",
    ):
        for stmt in split_statements((_MIGRATIONS / name).read_text(encoding="utf-8")):
            await database.execute(stmt)
    # The webhook branch looks the Shopify order up in orders first; an external order has no row.
    await database.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, shopify_order_id TEXT)")


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
        await _drop_tables(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _click(db, *, click_id="clk_shop_1", merchant_id=MERCHANT, context=None):
    await db.execute(
        "INSERT INTO surface_click_events (click_id, merchant_id, surface, agent_id, "
        " impression_count, click_count, context) "
        "VALUES (:c, :m, 'offers.resolve', 'agent_1', 0, 1, CAST(:ctx AS JSONB))",
        {"c": click_id, "m": merchant_id, "ctx": json.dumps(context or {})},
    )


async def _paid_order_billed_days_ago(
    db, *, merchant_id=MERCHANT, order_id=SHOPIFY_ORDER_ID, gross_cents=12_000, currency="USD",
    click_id="clk_shop_1", converting_shop_domain="shop.myshopify.com",
):
    """orders/paid's close, N days ago, with its day already rolled up by the daily job."""
    from services.commerce_attribution_service import close_external_order_conversion
    from services.gmv_aggregation_service import aggregate_daily

    closed = await close_external_order_conversion(
        merchant_id=merchant_id,
        click_id=click_id,
        external_order_id=order_id,
        gross_amount_cents=gross_cents,
        currency=currency,
        converting_shop_domain=converting_shop_domain,
    )
    assert closed is not None and closed["replayed"] is False
    # The close stamps created_at = now; this edge was closed N days ago.
    await db.execute(
        "UPDATE commerce_attribution_edges SET created_at = :t WHERE edge_id = :e",
        {"t": BILLED_AT, "e": closed["edge_id"]},
    )
    await aggregate_daily(BILLED_DAY)
    return closed


def _refund_payload(*, refund_id=880001, order_id=SHOPIFY_ORDER_ID, transactions=None):
    """A Shopify refunds/create body: a Refund object."""
    return {
        "id": refund_id,
        "order_id": int(order_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "refund_line_items": [],
        "transactions": transactions
        if transactions is not None
        else [{"id": refund_id + 1, "kind": "refund", "status": "success", "amount": "30.00", "currency": "USD"}],
    }


async def _deliver(payload, *, merchant_id=MERCHANT, webhook_id="wh_1", monkeypatch):
    """The refunds/create branch of the real Shopify webhook processor.

    Stubs only what reads tables unrelated to the edge: the PCS event store (never a duplicate,
    so a redelivery reaches the branch again, as one under a new webhook id does), the canonical
    dual-write, the order event log, and the Pivota-order refund path.
    """
    from fastapi import BackgroundTasks

    import routes.refund_webhook_routes as refund_webhook_routes
    import routes.webhook_routes as webhook_routes

    async def _not_duplicate(**kwargs):
        return False, None

    async def _noop(*a, **k):
        return {}

    async def _no_pivota_order(refund_event, merchant_id):
        return {"status": "order_not_found"}

    monkeypatch.setattr(webhook_routes, "ingest_shopify_webhook", _not_duplicate)
    monkeypatch.setattr(webhook_routes, "ingest_shopify_commerce_event_best_effort", _noop)
    monkeypatch.setattr(webhook_routes, "log_order_event", _noop)
    monkeypatch.setattr(refund_webhook_routes, "process_platform_refund", _no_pivota_order)
    body = json.dumps(payload).encode()
    return await webhook_routes._process_shopify_webhook_event(
        merchant_id=merchant_id,
        payload=body,
        data=payload,
        topic="refunds/create",
        shop_domain="shop.myshopify.com",
        got_canon="shop.myshopify.com",
        occurred_at=None,
        x_shopify_webhook_id=webhook_id,
        background_tasks=BackgroundTasks(),
        signature_verified=True,
    )


async def _rollup(db, day=BILLED_DAY, merchant_id=MERCHANT):
    row = await db.fetch_one(
        "SELECT gross_attributed_gmv_cents g, refund_amount_cents r, net_attributed_gmv_cents n, "
        "take_amount_cents t FROM gmv_attribution_daily WHERE date = :d AND merchant_id = :m",
        {"d": day, "m": merchant_id},
    )
    return dict(row) if row else None


async def _edge(db, edge_id):
    row = await db.fetch_one(
        "SELECT refund_amount_cents, refund_count, refunded_amount, refund_ids "
        "FROM commerce_attribution_edges WHERE edge_id = :e",
        {"e": edge_id},
    )
    row = dict(row)
    if isinstance(row["refund_ids"], str):
        row["refund_ids"] = json.loads(row["refund_ids"])
    return row


# --- The webhook: refunds/create -> the edge -> its billed day ---------------------------------


async def test_a_refund_of_an_order_from_n_days_ago_lowers_that_days_refund_net_and_take(db, monkeypatch):
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}

    result = await _deliver(_refund_payload(), monkeypatch=monkeypatch)

    assert result == {"status": "success", "topic": "refunds/create"}
    edge = await _edge(db, closed["edge_id"])
    assert edge["refund_amount_cents"] == 3_000
    assert edge["refund_ids"] == ["shopify:880001"]
    assert await _rollup(db) == {"g": 12_000, "r": 3_000, "n": 9_000, "t": 900}
    # The refund bills on the order's day, never on the day it arrived.
    assert await _rollup(db, datetime.now(timezone.utc).date()) is None


async def test_a_redelivered_refund_does_not_double_count(db, monkeypatch):
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)

    await _deliver(_refund_payload(), webhook_id="wh_1", monkeypatch=monkeypatch)
    await _deliver(_refund_payload(), webhook_id="wh_2", monkeypatch=monkeypatch)
    await _deliver(_refund_payload(), webhook_id="wh_1", monkeypatch=monkeypatch)

    edge = await _edge(db, closed["edge_id"])
    assert edge["refund_amount_cents"] == 3_000
    assert edge["refund_count"] == 1
    assert str(edge["refunded_amount"]) == "30.00"
    assert await _rollup(db) == {"g": 12_000, "r": 3_000, "n": 9_000, "t": 900}


async def test_two_refunds_of_one_order_add_up_and_stop_at_its_gross(db, monkeypatch):
    """Each Shopify refund is its own id. The last one is capped at what is left of the gross, so
    it cannot eat into another order's net in the same rollup group."""
    await _click(db)
    await _click(db, click_id="clk_shop_2")
    first = await _paid_order_billed_days_ago(db)
    other = await _paid_order_billed_days_ago(db, order_id="5550002", gross_cents=5_000, click_id="clk_shop_2")
    assert await _rollup(db) == {"g": 17_000, "r": 0, "n": 17_000, "t": 1_700}

    await _deliver(_refund_payload(refund_id=880001), monkeypatch=monkeypatch)
    over = [{"id": 990001, "kind": "refund", "status": "success", "amount": "100.00", "currency": "USD"}]
    await _deliver(_refund_payload(refund_id=880003, transactions=over), monkeypatch=monkeypatch)

    assert (await _edge(db, first["edge_id"]))["refund_amount_cents"] == 12_000
    assert (await _edge(db, first["edge_id"]))["refund_ids"] == ["shopify:880001", "shopify:880003"]
    assert (await _edge(db, other["edge_id"]))["refund_amount_cents"] == 0
    assert await _rollup(db) == {"g": 17_000, "r": 12_000, "n": 5_000, "t": 500}


async def test_only_money_that_moved_is_applied_and_its_id_stays_free(db, monkeypatch):
    """A pending transaction has moved no money. Recording 0 under the refund's id would shadow it."""
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)
    transactions = [
        {"id": 1, "kind": "refund", "status": "pending", "amount": "30.00", "currency": "USD"},
        {"id": 2, "kind": "sale", "status": "success", "amount": "120.00", "currency": "USD"},
    ]

    await _deliver(_refund_payload(transactions=transactions), monkeypatch=monkeypatch)

    edge = await _edge(db, closed["edge_id"])
    assert edge["refund_amount_cents"] == 0
    assert edge["refund_ids"] == []
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}


async def test_a_refund_in_another_currency_than_the_gross_is_not_subtracted(db, monkeypatch):
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)
    eur = [{"id": 3, "kind": "refund", "status": "success", "amount": "30.00", "currency": "EUR"}]

    await _deliver(_refund_payload(transactions=eur), monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 0
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}


async def test_a_zero_decimal_currency_refund_matches_the_gross_it_reverses(db, monkeypatch):
    """The close stores Shopify's gross as major × 100 in every currency, and so does the shared
    refund UPDATE. Passing the refund in MAJOR units keeps them in the same unit: a full JPY refund
    nets the day to zero, not to minus 99 times the order."""
    from services.commerce_attribution_service import shopify_order_total_to_cents

    gross_cents, currency = shopify_order_total_to_cents({"total_price": "5000", "currency": "JPY"})
    await _click(db)
    closed = await _paid_order_billed_days_ago(db, gross_cents=gross_cents, currency=currency)
    jpy = [{"id": 4, "kind": "refund", "status": "success", "amount": "5000", "currency": "JPY"}]

    await _deliver(_refund_payload(transactions=jpy), monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == gross_cents == 500_000
    assert await _rollup(db) == {"g": 500_000, "r": 500_000, "n": 0, "t": 0}


async def test_an_unattributed_order_and_another_merchants_order_are_untouched(db, monkeypatch):
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)

    # A refund of an order Pivota never attributed: no edge, no write.
    await _deliver(_refund_payload(order_id="5559999"), monkeypatch=monkeypatch)
    # The same Shopify order id reported by a different merchant's webhook.
    await _deliver(_refund_payload(refund_id=880005), merchant_id="m_someone_else", monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 0
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}


async def test_a_seller_keyed_edge_is_found_through_the_converting_merchant(db, monkeypatch):
    """A seller-keyed click re-subjects the edge to its seller_ref. When the sale happened on
    another store (seller mismatch) the edge still bills, under the seller, and the store whose
    webhook reports the refund is only in metadata.converting_merchant_id."""
    await _click(db, merchant_id="m_anchor", context={"seller_ref": "m_seller", "seed_kind": "cross"})
    closed = await _paid_order_billed_days_ago(db)
    assert closed["merchant_id"] == "m_seller" and closed["seller_mismatch"] is True
    assert await _rollup(db, merchant_id="m_seller") == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}

    await _deliver(_refund_payload(), monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 3_000
    assert await _rollup(db, merchant_id="m_seller") == {"g": 12_000, "r": 3_000, "n": 9_000, "t": 900}


async def test_an_invoiced_day_keeps_its_rollup_and_the_edge_keeps_the_refund(db, monkeypatch):
    await _click(db)
    closed = await _paid_order_billed_days_ago(db)
    period = {"s": BILLED_DAY - timedelta(days=3), "e": BILLED_DAY + timedelta(days=3)}
    # A GMV invoice: written by a completed billing run.
    run_id = await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, 'run-shop-refund', 'completed') RETURNING id",
        period,
    )
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, status, billing_run_id) "
        "VALUES (:m, :s, :e, 'finalized', :r)",
        {"m": MERCHANT, "r": run_id, **period},
    )

    await _deliver(_refund_payload(), monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 3_000
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}


async def test_a_replay_retries_a_re_roll_that_failed(db, monkeypatch):
    from services import commerce_attribution_service as cas
    from services import gmv_aggregation_service as gmv

    await _click(db)
    closed = await _paid_order_billed_days_ago(db)
    real = gmv.recompute_days_for_edges

    async def _failed_once(edges):
        return {}

    monkeypatch.setattr(cas, "recompute_days_for_edges", _failed_once)
    await _deliver(_refund_payload(), monkeypatch=monkeypatch)
    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 3_000
    assert await _rollup(db) == {"g": 12_000, "r": 0, "n": 12_000, "t": 1_200}

    monkeypatch.setattr(cas, "recompute_days_for_edges", real)
    await _deliver(_refund_payload(), webhook_id="wh_retry", monkeypatch=monkeypatch)

    assert (await _edge(db, closed["edge_id"]))["refund_amount_cents"] == 3_000
    assert await _rollup(db) == {"g": 12_000, "r": 3_000, "n": 9_000, "t": 900}
