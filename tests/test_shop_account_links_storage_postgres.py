"""Migration 277 on real Postgres: it builds exactly what `create_all` builds at boot (prod gets the
tables from create_all, every migrating environment from 277), it is idempotent, its down file drops
everything, and the partial unique indexes behave as the SQLite tests say.

Needs a throwaway Postgres DATABASE_URL (the dialect gate's). Skipped otherwise."""

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
_MIG_277 = _MIG / "277_shop_account_links.sql"
_DOWN_277 = _MIG / "down" / "277_shop_account_links_down.sql"
_SCHEMA_MODEL = f"shopmodel_{os.getpid()}"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _tables():
    from db import shop_account_links as sal

    return (sal.shop_account_claims, sal.shop_account_links, sal.shop_link_attempts, sal.shop_checkouts)


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _run_file(database, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text()):
        await database.execute(stmt)


@pytest.fixture
async def db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Only this file uses these tables. A sibling's create_all may have built them from the model;
    # rebuild them from the migration so the parity test reads what the migration makes.
    for table in reversed(_tables()):
        await database.execute(f"DROP TABLE IF EXISTS {table.name} CASCADE")
    await _run_file(database, _MIG_277)
    try:
        yield database
    finally:
        for table in reversed(_tables()):
            if await database.fetch_val("SELECT to_regclass(:t)", {"t": table.name}) is not None:
                await database.execute(f"DELETE FROM {table.name}")
        await database.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_MODEL} CASCADE")
        if not was_connected:
            await database.disconnect()


async def _describe(db, schema: str):
    def unqualified(text):
        return text.replace(f"{schema}.", "").replace("public.", "") if text else text

    out = {}
    for table in _tables():
        columns = await db.fetch_all(
            "SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale, "
            "is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t",
            {"s": schema, "t": table.name},
        )
        constraints = await db.fetch_all(
            "SELECT c.conname, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
            "JOIN pg_class r ON r.oid = c.conrelid JOIN pg_namespace n ON n.oid = r.relnamespace "
            "WHERE n.nspname = :s AND r.relname = :t",
            {"s": schema, "t": table.name},
        )
        indexes = await db.fetch_all(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t",
            {"s": schema, "t": table.name},
        )
        out[table.name] = {
            "columns": {r["column_name"]: tuple(r[k] for k in r.keys() if k != "column_name") for r in columns},
            "constraints": {r["conname"]: unqualified(r["def"]) for r in constraints},
            "indexes": {r["indexname"]: unqualified(r["indexdef"]) for r in indexes},
        }
    return out


async def test_migration_277_builds_exactly_what_create_all_builds_at_boot(db):
    from sqlalchemy import create_engine

    from db.database import metadata

    await db.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_MODEL} CASCADE")
    await db.execute(f"CREATE SCHEMA {_SCHEMA_MODEL}")
    engine = create_engine(DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        with engine.begin() as conn:
            conn = conn.execution_options(schema_translate_map={None: _SCHEMA_MODEL})
            metadata.create_all(conn, tables=list(_tables()))
    finally:
        engine.dispose()

    from_migration = await _describe(db, "public")
    from_model = await _describe(db, _SCHEMA_MODEL)
    assert all(from_migration[t]["columns"] for t in from_migration)
    for table in from_migration:
        assert from_migration[table] == from_model[table], table


async def test_migration_277_is_idempotent_and_its_down_file_drops_everything(db):
    await _run_file(db, _MIG_277)
    await _run_file(db, _DOWN_277)
    await _run_file(db, _DOWN_277)
    for table in _tables():
        assert await db.fetch_val("SELECT to_regclass(:t)", {"t": table.name}) is None
    await _run_file(db, _MIG_277)


async def _refused(coro, constraint: str):
    with pytest.raises(Exception) as excinfo:
        await coro
    assert constraint in str(excinfo.value)


_SUB = "a" * 64


async def _claim(db, account="acct_1"):
    await db.execute(
        "INSERT INTO shop_account_claims (shop_sub_hash, pivota_account_id) VALUES (:s, :a)",
        {"s": _SUB, "a": account},
    )


async def _link(db, link_id="lnk_1", client="cli_1", status="active", account="acct_1"):
    revoked = status == "revoked"
    await db.execute(
        """
        INSERT INTO shop_account_links
          (link_id, pivota_account_id, agent_client_id, shop_sub_hash, claimed_sub_hash, scope,
           refresh_token_ct, status, revoked_at)
        VALUES (:id, :account, :client, :sub, :claimed, 'openid personal_agent', :ct, :status, :revoked_at)
        """,
        {
            "id": link_id,
            "account": account,
            "client": client,
            "sub": _SUB,
            "claimed": None if revoked else _SUB,
            "ct": None if revoked else "tc1.k1.x",
            "status": status,
            "revoked_at": NOW if revoked else None,
        },
    )


async def test_one_live_link_per_account_and_client_on_postgres(db):
    await _claim(db)
    await _link(db, "lnk_old", status="revoked")
    await _link(db)
    await _refused(_link(db, "lnk_2"), "uq_shop_account_links_active_account_client")
    await _link(db, "lnk_3", client="cli_2")


async def test_a_claim_is_one_account_per_shop_account_on_postgres(db):
    await _claim(db)
    await _refused(_claim(db, "acct_2"), "shop_account_claims_pkey")


async def test_the_claim_foreign_key_on_postgres(db):
    """A live link needs its own account's claim; the claim cannot be released under a live
    link; a revoked tombstone (claimed_sub_hash NULL) does not hold it."""
    await _refused(_link(db), "fk_shop_account_links_claim")
    await _claim(db)
    await _refused(_link(db, "lnk_x", account="acct_2"), "fk_shop_account_links_claim")
    await _link(db)
    release = "DELETE FROM shop_account_claims WHERE shop_sub_hash = :s"
    await _refused(db.execute(release, {"s": _SUB}), "fk_shop_account_links_claim")
    await db.execute(
        "UPDATE shop_account_links SET status = 'revoked', refresh_token_ct = NULL, claimed_sub_hash = NULL, "
        "revoked_at = :now",
        {"now": NOW},
    )
    await db.execute(release, {"s": _SUB})


async def test_the_completion_guard_on_postgres(db):
    await db.execute(
        """
        INSERT INTO shop_checkouts (checkout_id_hash, pivota_account_id, agent_client_id, shop_domain, market)
        VALUES (:h, 'acct_1', 'cli_1', 'example.myshopify.com', 'US')
        """,
        {"h": "b" * 64},
    )
    await _refused(
        db.execute(
            "UPDATE shop_checkouts SET completion_attempted_at = :now WHERE checkout_id_hash = :h",
            {"now": NOW, "h": "b" * 64},
        ),
        "ck_shop_checkouts_completion_needs_mandate",
    )
    claim = (
        "UPDATE shop_checkouts SET completion_attempted_at = :now, mandate_hash = :m "
        "WHERE checkout_id_hash = :h AND completion_attempted_at IS NULL RETURNING checkout_id_hash"
    )
    args = {"now": NOW, "m": "c" * 64, "h": "b" * 64}
    assert await db.fetch_val(claim, args) == "b" * 64
    assert await db.fetch_val(claim, args) is None


async def test_attempt_expiry_and_connect_fields_on_postgres(db):
    insert = (
        "INSERT INTO shop_link_attempts (attempt_id, ticket_hash, pivota_account_id, agent_client_id, "
        "created_at, expires_at, state_hash) VALUES (:id, :t, 'acct_1', 'cli_1', :c, :e, :s)"
    )
    await _refused(
        db.execute(insert, {"id": "a1", "t": "1" * 64, "c": NOW, "e": NOW, "s": None}),
        "ck_shop_link_attempts_expiry_after_creation",
    )
    await _refused(
        db.execute(insert, {"id": "a2", "t": "2" * 64, "c": NOW, "e": NOW + timedelta(minutes=10), "s": "s" * 64}),
        "ck_shop_link_attempts_connect_fields_together",
    )
    await _refused(
        db.execute(insert, {"id": "a3", "t": "raw-ticket", "c": NOW, "e": NOW + timedelta(minutes=10), "s": None}),
        "ck_shop_link_attempts_ticket_hash_len",
    )
