"""Production-dialect gate for migration 215 (collector token registry).

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_collector_token_registry_postgres.py

`create_all` runs BEFORE migrations here, so a fresh database gets these two
tables from db/merchant_collector_tokens.py and an existing one from the
migration. Build both ways on real Postgres, read information_schema, diff.
Then exercise the revocation UPDATE whose rowcount the registry relies on.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate"
)

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db" / "migrations" / "215_merchant_collector_token_registry.sql"
_DOWN = _REPO / "db" / "migrations" / "down" / "215_merchant_collector_token_registry_down.sql"
_TABLES = ("merchant_collector_tokens", "merchant_collector_token_policy")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip("refusing to drop tables outside a throwaway database")


async def _drop_all():
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(_DOWN.read_text()):
        await database.execute(statement)


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    yield
    await _drop_all()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _shapes():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = current_schema() AND table_name = ANY(:names)
         ORDER BY table_name, column_name
        """,
        {"names": list(_TABLES)},
    )
    shapes = {}
    for row in rows:
        default = str(row["column_default"] or "").lower()
        # `now()` vs `CURRENT_TIMESTAMP` and `1` vs `'1'::integer` are the
        # same default rendered by two authors; compare the meaning.
        default = default.replace("current_timestamp", "now()")
        default = default.replace("'1'::integer", "1")
        shapes[(row["table_name"], row["column_name"])] = (
            row["data_type"], row["character_maximum_length"], row["is_nullable"], default,
        )
    return shapes


async def _build_from_model():
    from sqlalchemy import create_engine

    from db.database import metadata
    from db.merchant_collector_tokens import (
        merchant_collector_token_policy,
        merchant_collector_tokens,
    )

    await _drop_all()
    engine = create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        metadata.create_all(
            engine, tables=[merchant_collector_tokens, merchant_collector_token_policy], checkfirst=True
        )
    finally:
        engine.dispose()


async def _build_from_migration():
    from db.database import database
    from db.sql_migrations import split_statements

    await _drop_all()
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)


async def test_model_and_migration_build_the_same_tables():
    await _build_from_model()
    from_model = await _shapes()
    await _build_from_migration()
    from_migration = await _shapes()
    assert {t for t, _ in from_model} == set(_TABLES)
    assert from_model == from_migration, {
        k: (from_model.get(k), from_migration.get(k))
        for k in set(from_model) | set(from_migration)
        if from_model.get(k) != from_migration.get(k)
    }
    assert from_model[("merchant_collector_tokens", "allowed_origins")][0] == "jsonb"
    assert from_model[("merchant_collector_token_policy", "min_token_version")][3] == "1"


async def test_partial_expiring_index_exists_from_the_migration():
    from db.database import database

    await _build_from_migration()
    indexdef = await database.fetch_val(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'idx_merchant_collector_tokens_expiring'"
    )
    assert indexdef and "WHERE (revoked_at IS NULL)" in indexdef


async def test_revocation_rowcount_and_generation_bump_on_real_postgres(monkeypatch):
    """The first version of revoke_token read the UPDATE's return as a
    rowcount. SQLite returns one; asyncpg through `databases` does not, so on
    production every revocation reported False while the row WAS revoked.
    This gate is what found it; keep it on the production dialect."""
    from services import merchant_collector_token_registry as registry
    from services.merchant_web_collector_service import issue_web_collector_token

    monkeypatch.setenv("MERCHANT_WEB_COLLECTOR_SIGNING_SECRET", "pg-gate-secret-that-is-long-enough-0123456789")
    await _build_from_migration()
    now = datetime.now(timezone.utc)
    issued = issue_web_collector_token(
        merchant_id="m_pg", store_id="s_pg", platform="woocommerce",
        allowed_origins=["https://pg.example.com"], now=now,
    )
    await registry.register_issued_token(issued=issued, merchant_id="m_pg", store_id="s_pg")
    row = await registry.fetch_token(issued["jti"])
    assert row["allowed_origins"] == ["https://pg.example.com"]
    assert row["expires_at"] == now + timedelta(days=90)

    assert await registry.revoke_token(jti=issued["jti"], merchant_id="m_pg", reason="leaked") is True
    assert await registry.revoke_token(jti=issued["jti"], merchant_id="m_pg", reason="leaked") is False

    result = await registry.revoke_store_tokens(store_id="s_pg", merchant_id="m_pg", reason="rotate")
    assert result["min_token_version"] == 2
    assert await registry.current_store_token_version("s_pg") == 2
    result = await registry.revoke_store_tokens(store_id="s_pg", merchant_id="m_pg", reason="rotate")
    assert result["min_token_version"] == 3
