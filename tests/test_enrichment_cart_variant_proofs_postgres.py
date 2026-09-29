"""The enrichment cart proof table (migration 248) on the PRODUCTION dialect.

    DATABASE_URL=postgresql://<user>@localhost:5432/pivota_<name>_dialect_check \\
        .venv/bin/python -m pytest tests/test_enrichment_cart_variant_proofs_postgres.py

WHAT ONLY POSTGRES CAN SHOW:
  * CATALOG PARITY. Production deploys skip db/migrations/, so `ensure_table()` IS the production
    schema once a caller exists. The two builds are compared through the catalog (columns, CHECK
    and key definitions, indexes), not by reading SQL. The schema-guard coverage gate inspects
    `ADD COLUMN` only and cannot see a missing CREATE TABLE self-heal; this file is that check.
  * The TIMESTAMPTZ / BIGINT / BOOLEAN columns and the CHECKs, as Postgres enforces them.

Every dialect-agnostic case of tests/test_enrichment_cart_variant_proofs.py is imported below, so the
dialect gate (which collects only `*_postgres.py`) runs them on Postgres too.

THIS MODULE MUST NOT IMPORT `main` (see tests/test_merchant_purchasability_postgres.py for why). It
drops and recreates ONLY its own table, which nothing else in the gate creates.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
    from tests.test_enrichment_cart_variant_proofs import *  # noqa: F401,F403,E402
    # A star-import skips underscore names; the fixture is resolved by NAME in this module.
    from tests.test_enrichment_cart_variant_proofs import proof_db  # noqa: F401,E402

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION = _MIGRATIONS_DIR / "248_enrichment_cart_variant_proofs.sql"
_DOWN = _MIGRATIONS_DIR / "down" / "248_enrichment_cart_variant_proofs_down.sql"
_TABLE = "enrichment_cart_variant_proofs"


async def _connected():
    from db.database import database

    if not database.is_connected:
        await database.connect()
    return database


async def _apply_text(sql: str) -> None:
    from db.sql_migrations import split_statements

    database = await _connected()
    for statement in split_statements(sql):
        await database.execute(statement)


async def _fingerprint():
    database = await _connected()
    columns = await database.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default, ordinal_position
          FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = :t
         ORDER BY ordinal_position
        """,
        {"t": _TABLE},
    )
    constraints = await database.fetch_all(
        """
        SELECT c.conname AS name, CAST(c.contype AS text) AS kind, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = current_schema() AND t.relname = :t
        """,
        {"t": _TABLE},
    )
    indexes = await database.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t",
        {"t": _TABLE},
    )

    def norm(text):
        return " ".join((text or "").split())

    return (
        [tuple(dict(r).values()) for r in columns],
        # Unnamed CHECKs get numbered names that may differ between builds: compare by definition.
        sorted((r["kind"], norm(r["def"])) for r in constraints),
        sorted(r["name"] for r in constraints if r["name"].startswith("ck_")),
        {r["indexname"]: norm(r["indexdef"]) for r in indexes},
    )


@pytest.fixture
async def _clean():
    """Each test runs on its own event loop, so a connection this fixture opened is closed here."""
    import db.enrichment_cart_variant_proofs as proofs
    from db.database import database

    was_connected = database.is_connected
    await _connected()
    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    proofs._reset_for_tests()
    yield proofs
    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    proofs._reset_for_tests()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def test_the_self_heal_builds_what_migration_248_builds(_clean):
    proofs = _clean
    database = await _connected()
    await _apply_text(_MIGRATION.read_text(encoding="utf-8"))
    from_migration = await _fingerprint()
    columns, constraints, names, indexes = from_migration

    assert [c[0] for c in columns] == [
        "product_key", "sku_key", "shop_host", "handle", "shopify_product_id", "variant_id",
        "live_variant_count", "available", "live_price_minor", "currency", "source", "checked_at",
        "outcome", "created_at", "updated_at",
    ]
    types = {c[0]: c[1] for c in columns}
    assert types["product_key"] == types["sku_key"] == "text", "keys reach 214/255 chars: TEXT, not varchar(128)"
    assert types["checked_at"] == "timestamp with time zone"
    assert types["live_price_minor"] == "bigint" and types["available"] == "boolean"
    assert ("p", "PRIMARY KEY (product_key, sku_key)") in constraints
    assert sum(1 for kind, _ in constraints if kind == "c") == 5, constraints
    assert names == ["ck_enrichment_cart_variant_proofs_ok_has_evidence", "ck_enrichment_cart_variant_proofs_source"]
    assert any("'^[A-Z]{3}$'" in d for kind, d in constraints if kind == "c"), constraints
    assert list(indexes) == [f"{_TABLE}_pkey"]

    await _apply_text(_DOWN.read_text(encoding="utf-8"))
    assert (await _fingerprint())[0] == [], "the down migration removes the table"

    assert await proofs.ensure_table()
    assert await _fingerprint() == from_migration

    # And the migration re-applies over a self-healed database without error or change.
    await _apply_text(_MIGRATION.read_text(encoding="utf-8"))
    assert await _fingerprint() == from_migration
    assert await database.fetch_val(f"SELECT count(*) FROM {_TABLE}") == 0


async def test_the_parity_fingerprint_sees_a_missing_check(_clean):
    """THE CONTROL: a build without the currency CHECK must not compare equal, or the parity test
    above proves nothing about CHECKs."""
    migration = _MIGRATION.read_text(encoding="utf-8")
    await _apply_text(migration)
    full = await _fingerprint()
    database = await _connected()
    await database.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    narrowed = migration.replace(
        "currency            TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),",
        "currency            TEXT,",
    )
    assert narrowed != migration, "the control's edit no longer matches the migration text"
    await _apply_text(narrowed)
    assert (await _fingerprint())[1] != full[1]
