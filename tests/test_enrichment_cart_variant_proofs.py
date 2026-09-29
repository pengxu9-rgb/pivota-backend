"""db/enrichment_cart_variant_proofs.py -- the storefront proof table (migration 248).

Dialect-agnostic: runs on the suite's SQLite here, and on Postgres through
tests/test_enrichment_cart_variant_proofs_postgres.py, which star-imports this module.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import db.enrichment_cart_variant_proofs as proofs
from db.database import database

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIGRATION = _MIGRATIONS_DIR / "248_enrichment_cart_variant_proofs.sql"
_DOWN = _MIGRATIONS_DIR / "down" / "248_enrichment_cart_variant_proofs_down.sql"
_MIGRATION_249 = _MIGRATIONS_DIR / "249_enrichment_cart_variant_proofs_variant_title.sql"
_DOWN_249 = _MIGRATIONS_DIR / "down" / "249_enrichment_cart_variant_proofs_variant_title_down.sql"

#: A 214-character enrichment product_key (ingestion.derive_product_key's longest shape).
LONG_PK = "ext:" + "t" * 200 + "::578c4614"

#: What SQLite ("CHECK constraint failed", "NOT NULL constraint failed", "UNIQUE constraint failed")
#: and Postgres ("violates check constraint", "null value in column", "violates unique constraint")
#: say when a CONSTRAINT refused the row -- so a bind error cannot pass for a refusal.
_CONSTRAINT = r"(?i)constraint|null value"

_INSERT = """
INSERT INTO enrichment_cart_variant_proofs
       (product_key, sku_key, shop_host, handle, shopify_product_id, variant_id, live_variant_count,
        available, live_price_minor, currency, source, checked_at, outcome)
VALUES (:product_key, :sku_key, :shop_host, :handle, :shopify_product_id, :variant_id,
        :live_variant_count, :available, :live_price_minor, :currency, :source, :checked_at, :outcome)
"""


def _row(**over):
    row = {
        "product_key": "ext:ecvp-test-flat-blush-brush::4e837abf",
        "sku_key": "ext:ecvp-test-flat-blush-brush::4e837abf::v:52729903251478",
        "shop_host": "tartecosmetics.com",
        "handle": "flat-blush-brush",
        "shopify_product_id": "8123456789012",
        "variant_id": "52729903251478",
        "live_variant_count": 1,
        "available": True,
        "live_price_minor": 3400,
        "currency": "USD",
        "source": "products_json_v1",
        "checked_at": datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc),
        "outcome": "ok",
    }
    row.update(over)
    return row


def _statement_body(sql: str) -> str:
    """The CREATE statement alone, comments dropped and whitespace collapsed."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    return " ".join(" ".join(lines).split()).rstrip(";").strip()


def test_the_self_heal_statement_is_the_migrations_statement():
    migration = _statement_body(_MIGRATION.read_text(encoding="utf-8"))
    assert migration.startswith("CREATE TABLE IF NOT EXISTS enrichment_cart_variant_proofs (")
    assert _statement_body(proofs._CREATE_POSTGRES) == migration
    # Migration 249's ALTER is the self-heal's second Postgres statement, character for character.
    assert _statement_body(_MIGRATION_249.read_text(encoding="utf-8")) == _statement_body(
        proofs._ADD_VARIANT_TITLE_POSTGRES)
    # This run's dialect builds its own statements, and only those.
    if proofs.IS_POSTGRES:
        from db.schema_guard import guarded_statements

        assert proofs._DDL_STATEMENTS == guarded_statements(
            [proofs._CREATE_POSTGRES, proofs._ADD_VARIANT_TITLE_POSTGRES], postgres=True)
    else:
        assert proofs._DDL_STATEMENTS == [proofs._CREATE_SQLITE]


def test_the_sqlite_build_differs_from_postgres_only_in_the_currency_regex_and_249s_column():
    pg, lite = proofs._CREATE_POSTGRES, proofs._CREATE_SQLITE
    assert pg.count(proofs.POSTGRES_CURRENCY_CHECK) == 1
    assert lite != pg
    assert lite.count(proofs._SQLITE_VARIANT_TITLE_COLUMN) == 1 and "variant_title" not in pg
    assert lite.replace(proofs.SQLITE_CURRENCY_CHECK, proofs.POSTGRES_CURRENCY_CHECK).replace(
        proofs._SQLITE_VARIANT_TITLE_COLUMN, "") == pg
    # The column the SQLite build declares is the one migration 249 adds.
    assert "variant_title TEXT" in _MIGRATION_249.read_text(encoding="utf-8")


def test_the_source_check_is_the_verifiers_source_list():
    migration = _statement_body(_MIGRATION.read_text(encoding="utf-8"))
    listed = re.search(r"source IN \(([^)]*)\)", migration).group(1)
    assert tuple(v.strip().strip("'") for v in listed.split(",")) == proofs.PROOF_SOURCES


def test_the_migration_is_additive_and_the_down_drops_only_this_table():
    body = _statement_body(_MIGRATION.read_text(encoding="utf-8"))
    assert not re.search(r"\b(ALTER|DROP|DELETE|UPDATE|INSERT|TRUNCATE)\b", body, re.I)
    down = _statement_body(_DOWN.read_text(encoding="utf-8"))
    assert down == "DROP TABLE IF EXISTS enrichment_cart_variant_proofs"


def test_no_migration_after_249_touches_the_table():
    """A later migration that ALTERs this table must join the parity build in the _postgres twin."""
    later = sorted(
        p.name for p in _MIGRATIONS_DIR.glob("*.sql")
        if int(re.match(r"\d+", p.name).group(0)) > 248 and proofs.TABLE in p.read_text(encoding="utf-8")
    )
    assert later == [_MIGRATION_249.name], f"extend tests/test_enrichment_cart_variant_proofs_postgres.py for {later}"


def test_migration_249_only_adds_the_column_and_its_down_only_drops_it():
    body = _statement_body(_MIGRATION_249.read_text(encoding="utf-8"))
    assert body == "ALTER TABLE enrichment_cart_variant_proofs ADD COLUMN IF NOT EXISTS variant_title TEXT"
    down = _statement_body(_DOWN_249.read_text(encoding="utf-8"))
    assert down == "ALTER TABLE enrichment_cart_variant_proofs DROP COLUMN IF EXISTS variant_title"


def test_schema_guard_heals_migration_249s_column():
    guard = (Path(__file__).resolve().parent.parent / "db" / "schema_guard.py").read_text(encoding="utf-8")
    assert re.search(r"ALTER TABLE IF EXISTS enrichment_cart_variant_proofs\s+ADD COLUMN IF NOT EXISTS "
                     r"variant_title TEXT;", guard)


@pytest.fixture
async def proof_db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {proofs.TABLE}")
    proofs._reset_for_tests()
    assert await proofs.ensure_table()
    yield
    await database.execute(f"DROP TABLE IF EXISTS {proofs.TABLE}")
    proofs._reset_for_tests()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def test_ensure_table_is_idempotent_and_memoized(proof_db):
    assert await proofs.ensure_table()
    proofs._reset_for_tests()
    assert await proofs.ensure_table()


async def test_a_full_proof_round_trips_with_a_214_character_key(proof_db):
    pk = LONG_PK
    sku_key = pk + "::v:52729903251478"
    assert len(pk) == 214 and len(sku_key) > 128
    await database.execute(_INSERT, _row(product_key=pk, sku_key=sku_key))
    got = dict(await database.fetch_one(
        f"SELECT product_key, sku_key, variant_id, live_price_minor, currency, outcome, created_at, updated_at "
        f"FROM {proofs.TABLE} WHERE product_key = :pk", {"pk": pk}))
    assert got["product_key"] == pk and got["sku_key"] == sku_key
    assert (got["variant_id"], got["live_price_minor"], got["currency"], got["outcome"]) == (
        "52729903251478", 3400, "USD", "ok")
    assert got["created_at"] is not None and got["updated_at"] is not None


async def test_the_key_is_product_and_sku(proof_db):
    await database.execute(_INSERT, _row())
    with pytest.raises(Exception, match=_CONSTRAINT):
        await database.execute(_INSERT, _row(variant_id="52729903251479"))
    # Same product, another sku: a second row.
    await database.execute(_INSERT, _row(sku_key="ext:ecvp-test-flat-blush-brush::4e837abf::canonical"))
    count = await database.fetch_val(f"SELECT count(*) FROM {proofs.TABLE}")
    assert count == 2


@pytest.mark.parametrize("missing", ["variant_id", "live_variant_count", "available", "live_price_minor", "currency"])
async def test_an_ok_proof_must_carry_its_evidence(proof_db, missing):
    with pytest.raises(Exception, match=_CONSTRAINT):
        await database.execute(_INSERT, _row(**{missing: None}))


@pytest.mark.parametrize("over", [{"live_variant_count": 0}, {"live_price_minor": 0}])
async def test_an_ok_proof_has_a_variant_and_a_price(proof_db, over):
    with pytest.raises(Exception, match=_CONSTRAINT):
        await database.execute(_INSERT, _row(**over))


async def test_a_refusal_may_record_no_variant_and_no_price(proof_db):
    await database.execute(_INSERT, _row(outcome="variant_gone", live_variant_count=0, live_price_minor=0))
    assert await database.fetch_val(f"SELECT live_variant_count FROM {proofs.TABLE}") == 0


async def test_a_refusal_outcome_may_carry_no_evidence(proof_db):
    await database.execute(_INSERT, _row(
        outcome="revoked_404", variant_id=None, live_variant_count=None, available=None,
        live_price_minor=None, currency=None, shopify_product_id=None))
    assert await database.fetch_val(f"SELECT outcome FROM {proofs.TABLE}") == "revoked_404"


@pytest.mark.parametrize("over", [
    {"currency": "usd"}, {"currency": "US"}, {"currency": "USDT"}, {"currency": "1$!"}, {"currency": "U$D"},
    {"currency": "Usd"}, {"currency": "ÉUR"}, {"currency": ""},
    {"source": "anything"}, {"source": "PRODUCTS_JSON_V1"}, {"source": "anything", "outcome": "revoked_404"},
    {"live_variant_count": -1}, {"live_price_minor": -1},
    {"live_variant_count": -1, "outcome": "revoked_404"}, {"live_price_minor": -1, "outcome": "revoked_404"},
    {"shop_host": None}, {"handle": None}, {"source": None}, {"checked_at": None}, {"outcome": None},
])
async def test_the_table_refuses_a_malformed_row(proof_db, over):
    with pytest.raises(Exception, match=_CONSTRAINT):
        await database.execute(_INSERT, _row(**over))
