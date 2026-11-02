"""ADR-027 deal-lane storage on real Postgres: migration 275 builds exactly the tables db/deal_lane.py
declares (create_all builds the model at prod boot, so the two must agree), its constraints and
cascades hold, and hand-applied migration 276 adds the click-row columns in one bounded transaction.

276 runs against a stand-in surface_click_events in a private schema: the shared table stays as the
model declares it, because no backend code may name those columns before the hand apply.

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55433/pivota_deal_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_lane_storage_postgres.py
"""

from __future__ import annotations

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
_MIG = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIG_275 = _MIG / "275_deal_lane_tables.sql"
_DOWN_275 = _MIG / "down" / "275_deal_lane_tables_down.sql"
_MIG_276 = _MIG / "276_surface_click_events_discount_code.sql.disabled"
_DOWN_276 = _MIG / "down" / "276_surface_click_events_discount_code_down.sql"
_SCHEMA_276 = f"deal276_{os.getpid()}"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _tables():
    from db import deal_lane

    return (
        deal_lane.observed_store_offers,
        deal_lane.observed_store_offer_verifications,
        deal_lane.deal_program_permissions,
        deal_lane.deal_program_permission_hosts,
        deal_lane.deal_network_links,
        deal_lane.deal_verify_host_daily_carts,
        deal_lane.deal_lane_stops,
        deal_lane.deal_seller_optouts,
    )


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _run_file(database, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text()):
        await database.execute(stmt)


async def _drop_deal_tables(database) -> None:
    for table in reversed(_tables()):
        await database.execute(f"DROP TABLE IF EXISTS {table.name} CASCADE")


@pytest.fixture
async def db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Only this file uses these tables. A sibling's create_all may have built them from the model;
    # rebuild them from the migration so the parity test reads what the migration makes.
    await _drop_deal_tables(database)
    await _run_file(database, _MIG_275)
    try:
        yield database
    finally:
        for table in reversed(_tables()):
            await database.execute(f"DELETE FROM {table.name}")
        await database.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_276} CASCADE")
        if not was_connected:
            await database.disconnect()


async def _deal(database, deal_id="deal_1", **overrides):
    from db.deal_lane import observed_store_offer_key

    row = {
        "deal_id": deal_id,
        "seller_id": "seller_a",
        "market": "US",
        "code": "GLOW10",
        "program_id": None,
        "source": "merchant_published",
    }
    row.update(overrides)
    row["deal_key"] = observed_store_offer_key(
        seller_id=row["seller_id"], market=row["market"], code=row["code"], platform_rule_ref=None,
        program_id=row["program_id"],
    )
    await database.execute(
        """
        INSERT INTO observed_store_offers
          (deal_id, deal_key, seller_id, market, code, program_id, kind, value, currency, source, observed_at)
        VALUES (:deal_id, :deal_key, :seller_id, :market, :code, :program_id, 'percent', 10, 'USD', :source, :now)
        """,
        {**row, "now": NOW},
    )


async def _verification(database, deal_id="deal_1", variant_id="4401", **overrides):
    row = {
        "deal_id": deal_id,
        "variant_id": variant_id,
        "state": "checkout_verified",
        "verified_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
        "verified_via": "shopify_storefront_connected",
        "authorized_by": "connected",
        "ref_unit_price": 40,
        "ref_subtotal": 40,
        "ref_discount_amount": 4,
        "ref_currency": "USD",
    }
    row.update(overrides)
    await database.execute(
        """
        INSERT INTO observed_store_offer_verifications
          (deal_id, variant_id, state, verified_at, expires_at, verified_via, authorized_by,
           ref_unit_price, ref_subtotal, ref_discount_amount, ref_currency)
        VALUES (:deal_id, :variant_id, :state, :verified_at, :expires_at, :verified_via, :authorized_by,
                :ref_unit_price, :ref_subtotal, :ref_discount_amount, :ref_currency)
        """,
        row,
    )


async def _refused(coro, constraint: str):
    with pytest.raises(Exception) as excinfo:
        await coro
    assert constraint in str(excinfo.value)


# ---- the migration builds the model --------------------------------------------------------------


async def test_migration_275_builds_exactly_the_model_columns(db):
    for table in _tables():
        rows = await db.fetch_all(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t",
            {"t": table.name},
        )
        built = {r["column_name"]: r["is_nullable"] == "YES" for r in rows}
        declared = {c.name: bool(c.nullable) for c in table.columns}
        assert built == declared, table.name


async def test_migration_275_builds_the_model_constraints_and_indexes(db):
    from sqlalchemy import CheckConstraint

    for table in _tables():
        rows = await db.fetch_all(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid = to_regclass(:t)",
            {"t": table.name},
        )
        built = {r["conname"] for r in rows}
        declared = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
        assert built == declared, table.name
        index_rows = await db.fetch_all(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t",
            {"t": table.name},
        )
        built_indexes = {r["indexname"] for r in index_rows}
        assert {i.name for i in table.indexes} <= built_indexes, table.name


async def test_migration_275_is_idempotent_and_its_down_file_drops_everything(db):
    await _run_file(db, _MIG_275)
    await _run_file(db, _DOWN_275)
    for table in _tables():
        assert await db.fetch_val("SELECT to_regclass(:t)", {"t": table.name}) is None
    await _run_file(db, _MIG_275)


# ---- constraints on Postgres ---------------------------------------------------------------------


async def test_the_source_rules_hold_on_postgres(db):
    await _deal(db)
    await _refused(
        _deal(db, "deal_2", code="glow10"), "uq_observed_store_offers_deal_key"
    )
    await _refused(
        _deal(db, "deal_3", seller_id="s3", source="authorized_feed"),
        "ck_observed_store_offers_program_iff_feed",
    )
    await _refused(
        _deal(db, "deal_4", seller_id="s4", source="checkout_automatic"),
        "ck_observed_store_offers_code_iff_coded_source",
    )
    await _deal(db, "deal_5", seller_id="s5", source="authorized_feed", program_id="prog_1")


async def test_a_verified_row_must_carry_its_reference_cart_on_postgres(db):
    await _deal(db)
    await _verification(db)
    await _refused(
        _verification(db, variant_id="4402", ref_discount_amount=0),
        "ck_observed_store_offer_verifications_verified_is_complete",
    )
    await _refused(
        _verification(db, variant_id="4403", authorized_by="program:"),
        "ck_observed_store_offer_verifications_authorized_by",
    )
    await _verification(db, variant_id="4404", authorized_by="program:prog_1")


async def test_deleting_a_deal_deletes_its_verifications_and_links(db):
    await _deal(db, source="authorized_feed", program_id="prog_1")
    await _verification(db, authorized_by="program:prog_1")
    await db.execute(
        """
        INSERT INTO deal_network_links
          (link_row_id, deal_id, variant_id, market, program_id, tracking_link, tracking_generated_at,
           coded_link, coded_generated_at)
        VALUES ('nl_1', 'deal_1', '4401', 'US', 'prog_1', 'https://t.example/a', :now, 'https://t.example/b', :now)
        """,
        {"now": NOW},
    )
    await db.execute("DELETE FROM observed_store_offers WHERE deal_id = 'deal_1'")
    assert await db.fetch_val("SELECT count(*) FROM observed_store_offer_verifications") == 0
    assert await db.fetch_val("SELECT count(*) FROM deal_network_links") == 0


async def test_one_active_stop_per_market_on_postgres(db):
    await db.execute("INSERT INTO deal_lane_stops (stop_id, market, stop_trigger) VALUES ('s1', 'US', 'absolute')")
    await _refused(
        db.execute("INSERT INTO deal_lane_stops (stop_id, market, stop_trigger) VALUES ('s2', 'US', 'rate')"),
        "uq_deal_lane_stops_active_market",
    )
    await db.execute(
        "UPDATE deal_lane_stops SET cleared_at = :now, cleared_by = 'peng' WHERE stop_id = 's1'", {"now": NOW}
    )
    await db.execute("INSERT INTO deal_lane_stops (stop_id, market, stop_trigger) VALUES ('s3', 'US', 'rate')")


async def test_a_program_permission_cascades_to_its_hosts(db):
    await db.execute(
        """
        INSERT INTO deal_program_permissions
          (program_id, network, membership_status, permission_source, permission_ref, permission_dated,
           confirmed_at, confirmed_by, reconfirm_by)
        VALUES ('prog_1', 'network_a', 'active', 'program_terms', 'terms', :now, :now, 'peng', :later)
        """,
        {"now": NOW, "later": NOW + timedelta(days=90)},
    )
    await db.execute("INSERT INTO deal_program_permission_hosts (program_id, host) VALUES ('prog_1', 'shop.example')")
    await db.execute("DELETE FROM deal_program_permissions WHERE program_id = 'prog_1'")
    assert await db.fetch_val("SELECT count(*) FROM deal_program_permission_hosts") == 0


# ---- 276, against a stand-in click table -------------------------------------------------------


async def _columns_276(db):
    rows = await db.fetch_all(
        "SELECT column_name, data_type, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = 'surface_click_events' "
        "AND column_name IN ('discount_code', 'discount_carried')",
        {"s": _SCHEMA_276},
    )
    return {r["column_name"]: (r["data_type"], r["character_maximum_length"]) for r in rows}


async def _in_schema_276(db, path: Path):
    """The runbook's way: every statement of the file in ONE transaction (SET LOCAL needs it)."""
    from db.sql_migrations import split_statements

    async with db.transaction():
        await db.execute(f"SET LOCAL search_path TO {_SCHEMA_276}")
        for stmt in split_statements(path.read_text()):
            await db.execute(stmt)


async def test_migration_276_adds_the_click_columns_in_one_bounded_transaction(db):
    await db.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_276} CASCADE")
    await db.execute(f"CREATE SCHEMA {_SCHEMA_276}")
    await db.execute(f"CREATE TABLE {_SCHEMA_276}.surface_click_events (click_id VARCHAR(64) PRIMARY KEY)")
    await db.execute(f"INSERT INTO {_SCHEMA_276}.surface_click_events (click_id) VALUES ('clk_1')")

    await _in_schema_276(db, _MIG_276)
    assert await _columns_276(db) == {
        "discount_code": ("character varying", 255),
        "discount_carried": ("boolean", None),
    }
    # No backfill: an existing click carried no deal-lane code.
    row = await db.fetch_one(
        f"SELECT discount_code, discount_carried FROM {_SCHEMA_276}.surface_click_events"
    )
    assert (row["discount_code"], row["discount_carried"]) == (None, None)
    await _in_schema_276(db, _MIG_276)  # a retry after a lock timeout is safe

    await _in_schema_276(db, _DOWN_276)
    assert await _columns_276(db) == {}
    # The shared table is untouched.
    shared = await db.fetch_val(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'surface_click_events' AND column_name IN ('discount_code', 'discount_carried')"
    )
    assert shared == 0
