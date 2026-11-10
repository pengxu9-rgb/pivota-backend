"""M-05 phase B on real Postgres: an is_test merchant's orders leave GMV, through the real producers.

Reuses the O-07 money harness (tests/test_test_traffic_money_postgres.py): db.orders.create_order
plus upsert_order_attribution_edge, then the nightly roll-up (aggregate_daily) and the monthly
statement GMV read. The merchant is marked with the real writer
(services.merchant_is_test_flag.set_merchant_is_test), not by hand.

    DATABASE_URL=postgresql://localhost/pivota_m05_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_merchant_is_test_money_postgres.py
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

from tests.test_test_traffic_money_postgres import (  # noqa: E402
    REAL,
    _gmv_by_merchant,
    _order_md,
    _place,
    _statement_gmv,
    db,  # noqa: F401 - the harness's autouse fixture: builds and drops the money tables
)

FLAGGED = "merch_flagged_shop_c"
_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/282_merchant_onboarding_is_test.sql"
_OWN_TABLES = ("merchant_onboarding", "merchant_stores", "employee_audit_logs")


@pytest.fixture(autouse=True)
async def merchants(db, monkeypatch):  # noqa: F811 - depends on the harness fixture
    """merchant_onboarding (+ migration 282), merchant_stores (the demo-domain leg the cached
    attempt reads first) and employee_audit_logs, created here and dropped afterwards."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.merchant_onboarding import merchant_onboarding
    from db.reviews_center import employee_audit_logs
    from db.sql_migrations import split_statements
    from services import test_merchant_policy

    for k in ("MERCHANT_IS_TEST_FILTER_ENABLED",):
        monkeypatch.delenv(k, raising=False)
    for table in _OWN_TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (merchant_onboarding, employee_audit_logs):
        await db.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    await db.execute("CREATE TABLE merchant_stores (store_id TEXT PRIMARY KEY, merchant_id TEXT, domain TEXT)")
    for statement in split_statements(_MIGRATION.read_text()):
        await db.execute(statement)
    for mid in (REAL, FLAGGED):
        await db.execute(
            "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, status, operating_mode, apm_enabled)"
            " VALUES (:m, :n, 'owner@example.com', 'approved', 'storefront', FALSE)",
            {"m": mid, "n": f"Biz {mid}"},
        )
    test_merchant_policy.reset_cache()
    try:
        yield
    finally:
        test_merchant_policy.reset_cache()
        for table in _OWN_TABLES:
            await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _mark_flagged():
    from services.merchant_is_test_flag import set_merchant_is_test

    out = await set_merchant_is_test(
        merchant_id=FLAGGED, is_test=True, reason="internal rig", actor={"sub": "u_admin", "email": "admin@example.com"}
    )
    assert out["changed"] is True


async def _scenario(db):
    real = await _place(db, REAL, {"pvt_click_id": "clk_m05_real"}, 10_000)
    flagged = await _place(db, FLAGGED, {"pvt_click_id": "clk_m05_flagged"}, 4_000)
    return real, flagged


async def test_both_flags_on_an_is_test_merchants_order_leaves_gmv(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    real, flagged = await _scenario(db)
    assert (await _order_md(db, flagged))["test_traffic_reasons"] == ["test_merchant"]
    assert "test_traffic" not in (await _order_md(db, real) or {})
    edge_md = await db.fetch_val("SELECT metadata FROM commerce_attribution_edges WHERE order_id = :o", {"o": flagged})
    edge_md = edge_md if isinstance(edge_md, dict) else __import__("json").loads(edge_md)
    assert edge_md.get("test_traffic") is True
    assert await _gmv_by_merchant(db) == {REAL: 10_000}
    assert await _statement_gmv(db, FLAGGED) == 0
    assert await _statement_gmv(db, REAL) == 10_000


@pytest.mark.parametrize("filter_flag", ["", "false"])
async def test_filter_off_an_is_test_merchants_order_counts_as_before(db, monkeypatch, filter_flag):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    await _mark_flagged()
    real, flagged = await _scenario(db)
    assert "test_traffic" not in (await _order_md(db, flagged) or {})
    assert await _gmv_by_merchant(db) == {REAL: 10_000, FLAGGED: 4_000}


async def test_an_unmarked_merchant_counts_with_both_flags_on(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _scenario(db)
    assert await _gmv_by_merchant(db) == {REAL: 10_000, FLAGGED: 4_000}
