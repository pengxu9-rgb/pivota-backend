"""The shared Shopify-edge budget (migration 251) on the PRODUCTION dialect.

    DATABASE_URL=postgresql://<user>@localhost:5432/pivota_<name>_dialect_check \\
        .venv/bin/python -m pytest tests/test_shopify_edge_pacer_postgres.py

WHAT ONLY POSTGRES CAN SHOW:
  * CATALOG PARITY. Production deploys skip db/migrations/, so `ensure_table()` IS the production
    schema. The self-heal build and the migration are compared through the catalog.
  * The lease statement as pivota-pg runs it: `clock_timestamp()`, GREATEST, the ON CONFLICT upsert
    under concurrent writers from two processes, and the one-statement-per-lease cost.

Every dialect-agnostic case of tests/test_shopify_edge_pacer.py is imported below, so the dialect
gate (which collects only `*_postgres.py`) runs them on Postgres too.

THIS MODULE MUST NOT IMPORT `main`. It drops and recreates ONLY its own table.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
# This module DROPS its table: a throwaway database only. The CI database is pivota_dialect_check.
_THROWAWAY = any(marker in _DBNAME for marker in ("dialect_check", "_test", "test_"))

pytestmark = pytest.mark.skipif(
    not (_IS_PG and _THROWAWAY),
    reason=(
        "needs a Postgres DATABASE_URL naming a THROWAWAY database (…dialect_check…, …_test, "
        "test_…) — this is the production-dialect gate; see the module docstring"
    ),
)

if _IS_PG and _THROWAWAY:
    from tests.test_shopify_edge_pacer import *  # noqa: F401,F403,E402
    # A star-import skips underscore names; the autouse fixture is resolved by NAME in this module.
    from tests.test_shopify_edge_pacer import _clean  # noqa: F401,E402
    from tests.test_shopify_edge_pacer import assert_one_shared_budget, run_two_processes  # noqa: E402

_MIGRATION = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations" / "251_crawl_egress_pacer.sql"
_TABLE = "crawl_egress_pacer"

_CATALOG_SQL = """
    SELECT column_name, data_type, is_nullable, COALESCE(column_default, '') AS column_default
      FROM information_schema.columns
     WHERE table_schema = current_schema() AND table_name = :t
     ORDER BY ordinal_position
"""
_KEY_SQL = """
    SELECT pg_get_constraintdef(c.oid) AS def
      FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
     WHERE t.relname = :t
     ORDER BY 1
"""


async def _catalog(database):
    cols = [tuple(r) for r in await database.fetch_all(_CATALOG_SQL, {"t": _TABLE})]
    keys = [r[0] for r in await database.fetch_all(_KEY_SQL, {"t": _TABLE})]
    return cols, keys


async def test_the_self_heal_table_is_the_migrations_table_in_the_catalog() -> None:
    import db.crawl_egress_pacer as table
    from db.database import database
    from db.sql_migrations import split_statements

    await database.connect()
    try:
        await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        for statement in split_statements(_MIGRATION.read_text()):
            await database.execute(statement)
        from_migration = await _catalog(database)

        await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        table.reset_for_tests()
        assert await table.ensure_table()
        from_self_heal = await _catalog(database)
        assert from_self_heal == from_migration
        assert from_migration[1] == ["PRIMARY KEY (bucket)"]
    finally:
        await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        table.reset_for_tests()
        await database.disconnect()


async def test_the_lease_statement_on_postgres() -> None:
    import db.crawl_egress_pacer as table
    from db.database import database

    await database.connect()
    try:
        await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        table.reset_for_tests()
        start1, now1 = await table.lease_slots("pg_bucket", slots=10, rate_per_s=2.0)
        start2, _ = await table.lease_slots("pg_bucket", slots=10, rate_per_s=2.0)
        assert abs(start1 - now1) < 0.05
        assert start2 == pytest.approx(start1 + 5.0, abs=1e-6), "leases abut; no overlap"
        await database.execute(
            f"UPDATE {_TABLE} SET next_free_epoch = 0 WHERE bucket = 'pg_bucket'")
        start3, now3 = await table.lease_slots("pg_bucket", slots=1, rate_per_s=2.0)
        assert start3 >= now3 - 0.01, "an idle bucket restarts at now: no stored burst"
        row = await database.fetch_one(f"SELECT leases FROM {_TABLE} WHERE bucket = 'pg_bucket'")
        assert int(row[0]) == 3, "one statement per lease"
    finally:
        await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        table.reset_for_tests()
        await database.disconnect()


def test_two_processes_share_one_budget_postgres(tmp_path) -> None:
    run = run_two_processes(tmp_path, DATABASE_URL)
    assert_one_shared_budget(run)
