"""O-07 against real Postgres: test traffic is stamped by the REAL producers and left out of GMV.

Producers driven: db.orders.create_order then services.commerce_attribution_service.
upsert_order_attribution_edge (the two calls routes.order_routes.create_new_order makes, in that
order, with the same metadata), and close_external_order_conversion (the Shopify orders/paid
webhook's and poller's close). The read is the real nightly roll-up (aggregate_daily) into
gmv_attribution_daily, plus the merchant monthly statement GMV read.

With TEST_TRAFFIC_STAMP_ENABLED unset every order counts, exactly as before (the differential).
With it 'true', a synthetic-marked, canary, rig-merchant or Shopify-test order is stamped and is
not in GMV, and a real order still is.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_test_traffic_dialect_check \\
        .venv/bin/python -m pytest tests/test_test_traffic_money_postgres.py
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

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = (
    "gmv_attribution_daily", "commerce_attribution_edges", "invoices", "billing_runs", "orders",
    "surface_click_events",
)
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"

REAL = "merch_shop_b"
RIG = "merch_rig_shop_a"  # added through PIVOTA_TEST_MERCHANT_IDS below
TODAY = datetime.now(timezone.utc).date()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_schema(database):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.commerce_attribution import commerce_attribution_edges, surface_click_events
    from db.orders import orders
    from db.sql_migrations import split_statements

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (commerce_attribution_edges, surface_click_events, orders):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    await database.execute(
        "ALTER TABLE commerce_attribution_edges "
        "ADD COLUMN channel_partner_id BIGINT, ADD COLUMN take_rate_applied_bp SMALLINT, "
        "ADD COLUMN refund_amount_cents BIGINT NOT NULL DEFAULT 0, ADD COLUMN refunded_at TIMESTAMPTZ"
    )
    # Migration 167's unique key: close_external_order_conversion's ON CONFLICT target.
    mig167 = (_MIGRATIONS / "167_commerce_attribution_edges_external_conversion.sql").read_text(encoding="utf-8")
    await database.execute(
        re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_attribution_edges_external_order.*?;", mig167, re.S).group(0)
    )
    ddl = (_MIGRATIONS / "110_gmv_attribution_daily.sql").read_text(encoding="utf-8").replace(
        "REFERENCES channel_partners(id) ON DELETE SET NULL", ""
    )
    for stmt in split_statements(ddl):
        await database.execute(stmt)
    billing_core = (_MIGRATIONS / "113_billing_core.sql").read_text(encoding="utf-8")
    await database.execute(
        re.search(r"CREATE TABLE IF NOT EXISTS invoices \(.*?\n\);", billing_core, re.S).group(0)
    )
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
    for k in ("TEST_TRAFFIC_STAMP_ENABLED", "ALLOW_TEST_PSP_PROBE", "TEST_PSP_PROBE_MERCHANTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", RIG)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _no_event(*a, **k):
        return {"interaction_id": "int_stub"}

    async def _flat_rate(merchant_id):
        return 1000

    async def _no_recompute(edges):
        return {}

    monkeypatch.setattr(cas, "record_commerce_event_best_effort", _no_event)
    monkeypatch.setattr(cas, "recompute_days_for_edges", _no_recompute)
    monkeypatch.setattr(gmv, "_take_rate_bp_for_merchant", _flat_rate)
    await _build_schema(database)
    try:
        yield database
    finally:
        for table in _TABLES:
            try:
                await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            except Exception:  # noqa: BLE001
                continue
        if not was_connected and database.is_connected:
            await database.disconnect()


def _order_data(merchant_id, metadata):
    return {
        "merchant_id": merchant_id,
        "customer_email": "buyer@example.com",
        "shipping_address": {"country": "US"},
        "items": [{"product_id": "p1", "quantity": 1, "unit_price": 50.0, "subtotal": 50.0}],
        "subtotal": 50.0,
        "total": 50.0,
        "currency": "USD",
        "agent_id": "agent_partner_a",
        "metadata": metadata,
    }


async def _place(db, merchant_id, metadata, gross_cents, *, synthetic=False):
    """create_order + upsert_order_attribution_edge, as create_new_order calls them, then the
    payment-time gross stamp. Returns the order id."""
    from db.orders import create_order
    from services import synthetic_call as sc
    from services.commerce_attribution_service import upsert_order_attribution_edge

    token = sc.set_synthetic_call(sc.SyntheticCall(caller="gw|eval", gw_request_id="r1")) if synthetic else None
    try:
        order_metadata = dict(metadata)
        order_id = await create_order(_order_data(merchant_id, order_metadata))
        await upsert_order_attribution_edge(
            order_id=order_id, merchant_id=merchant_id, metadata=order_metadata
        )
    finally:
        if token is not None:
            sc.reset_synthetic_call(token)
    await db.execute(
        "UPDATE commerce_attribution_edges SET gross_attributed_gmv_cents = :g WHERE order_id = :o",
        {"g": gross_cents, "o": order_id},
    )
    return order_id


async def _shopify_close(merchant_id, external_order_id, gross_cents, *, test_flag):
    from services.commerce_attribution_service import close_external_order_conversion

    return await close_external_order_conversion(
        merchant_id=merchant_id,
        click_id="clk_shop_" + external_order_id,
        external_order_id=external_order_id,
        gross_amount_cents=gross_cents,
        currency="USD",
        converted_at=datetime.now(timezone.utc),
        note_attrs_or_payload={"id": external_order_id, "financial_status": "paid", "test": test_flag},
        converting_shop_domain=None,
    )


async def _gmv_by_merchant(db):
    from services.gmv_aggregation_service import aggregate_daily

    await aggregate_daily(TODAY)
    rows = await db.fetch_all(
        "SELECT merchant_id, SUM(gross_attributed_gmv_cents) AS g FROM gmv_attribution_daily "
        "WHERE date = :d GROUP BY merchant_id",
        {"d": TODAY},
    )
    return {r["merchant_id"]: int(r["g"]) for r in rows}


async def _statement_gmv(db, merchant_id):
    from services.billing import monthly_brand_statements_service as mbs

    # the statement reads net_attributed_gmv_cents / gmv_channel (later migrations)
    for col in ("net_attributed_gmv_cents BIGINT", "gmv_channel TEXT"):
        await db.execute(f"ALTER TABLE commerce_attribution_edges ADD COLUMN IF NOT EXISTS {col}")
    await db.execute("UPDATE commerce_attribution_edges SET net_attributed_gmv_cents = gross_attributed_gmv_cents")
    row = await mbs._gmv_attribution_for_month(
        merchant_id=merchant_id,
        period_start=TODAY - timedelta(days=1),
        period_end=TODAY + timedelta(days=1),
    )
    return int(row["gmv_raw_total_cents"])


async def _scenario(db):
    real = await _place(db, REAL, {"pvt_click_id": "clk_real_1"}, 10_000)
    synthetic = await _place(db, REAL, {"pvt_click_id": "clk_syn_1"}, 2_000, synthetic=True)
    canary = await _place(db, REAL, {"pvt_click_id": "clk_canary_1", "ops_canary": True}, 3_000)
    rig = await _place(db, RIG, {"pvt_click_id": "clk_rig_1"}, 4_000)
    await _shopify_close(REAL, "5001", 500, test_flag=True)
    await _shopify_close(REAL, "5002", 700, test_flag=False)
    return {"real": real, "synthetic": synthetic, "canary": canary, "rig": rig}


async def _order_md(db, order_id):
    raw = await db.fetch_val("SELECT metadata FROM orders WHERE order_id = :o", {"o": order_id})
    return raw if isinstance(raw, dict) else json.loads(raw or "null")


async def test_flag_off_every_order_counts_as_before(db):
    ids = await _scenario(db)
    gmv = await _gmv_by_merchant(db)
    # before == after: nothing stamped, every order in GMV
    assert gmv == {REAL: 10_000 + 2_000 + 3_000 + 500 + 700, RIG: 4_000}
    assert await db.fetch_val(
        "SELECT COUNT(*) FROM commerce_attribution_edges WHERE metadata ? 'test_traffic'"
    ) == 0
    for order_id in ids.values():
        assert "test_traffic" not in (await _order_md(db, order_id) or {})


async def test_flag_on_test_orders_leave_gmv_and_a_real_order_stays(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    ids = await _scenario(db)
    gmv = await _gmv_by_merchant(db)
    assert gmv == {REAL: 10_000 + 700}  # the real order and the non-test Shopify order only

    assert "test_traffic" not in (await _order_md(db, ids["real"]) or {})
    assert (await _order_md(db, ids["synthetic"]))["test_traffic_reasons"] == ["synthetic_call"]
    assert (await _order_md(db, ids["canary"]))["test_traffic_reasons"] == ["ops_canary"]
    assert (await _order_md(db, ids["rig"]))["test_traffic_reasons"] == ["test_merchant"]

    reasons = {
        r["order_id"]: (r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"])).get(
            "test_traffic_reasons"
        )
        for r in await db.fetch_all("SELECT order_id, metadata FROM commerce_attribution_edges")
    }
    assert reasons[ids["real"]] is None
    assert reasons[ids["synthetic"]] == ["synthetic_call"]
    assert reasons[ids["canary"]] == ["ops_canary"]
    assert reasons[ids["rig"]] == ["test_merchant"]
    shopify = {k: v for k, v in reasons.items() if k not in ids.values()}
    assert sorted(map(str, shopify.values())) == ["None", "['shopify_test']"]


async def test_flag_on_statement_gmv_excludes_test_orders(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    await _scenario(db)
    assert await _statement_gmv(db, REAL) == 10_000 + 700


async def test_flag_off_statement_gmv_is_unchanged(db):
    await _scenario(db)
    assert await _statement_gmv(db, REAL) == 10_000 + 2_000 + 3_000 + 500 + 700


async def test_a_caller_supplied_stamp_never_takes_a_real_order_out_of_gmv(db, monkeypatch):
    # Flag off AND on: the stamp keys are stripped from caller metadata before any writer stamps.
    for flag in ("", "true"):
        monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", flag)
        await db.execute("DELETE FROM commerce_attribution_edges")
        await db.execute("DELETE FROM gmv_attribution_daily")
        await _place(
            db, REAL,
            {"pvt_click_id": "clk_forged_" + (flag or "off"), "test_traffic": True,
             "test_traffic_reasons": ["forged"]},
            9_000,
        )
        assert await _gmv_by_merchant(db) == {REAL: 9_000}
