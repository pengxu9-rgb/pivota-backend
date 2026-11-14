"""One quote backs at most one order per agent, on the production dialect.

    createdb oopq_test_dialect_check
    DATABASE_URL=postgresql://localhost/oopq_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_order_create_one_order_per_quote_postgres.py

The cases and the harness are tests/order_create_one_per_quote_cases.py, shared with the SQLite
file. WHY POSTGRES: the guarantee is two primary keys (the quote claim's, then the order's)
refusing a second writer, which is a property of the real engine under real connections; the
racing case here gives every racer its own pooled connection.

It writes only rows of its own fixture merchant and quote ids, deletes them after every case, and
re-creates `order_quote_claims` after the one case that drops it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

from order_create_one_per_quote_cases import *  # noqa: E402,F401,F403
from order_create_one_per_quote_cases import world  # noqa: E402,F401

_MIGRATION = Path(__file__).resolve().parent.parent / "db" / "migrations" / "284_order_quote_claims.sql"
_DOWN = _MIGRATION.parent / "down" / "284_order_quote_claims_down.sql"


async def _fingerprint():
    from db.database import database

    columns = [
        tuple(dict(r._mapping).values())
        for r in await database.fetch_all(
            "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'order_quote_claims' "
            "ORDER BY column_name"
        )
    ]
    indexes = sorted(
        " ".join(r["indexdef"].split())
        for r in await database.fetch_all(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'order_quote_claims'"
        )
    )
    return columns, indexes


async def test_migration_284_builds_the_table_boot_builds_and_is_safe_to_run_twice(world):
    from db.database import database, engine, metadata
    from db.orders import order_quote_claims
    from db.sql_migrations import needs_autocommit, split_statements

    body = _MIGRATION.read_text(encoding="utf-8")
    assert not needs_autocommit(body)
    assert "schema-guard-exempt:" in body

    declared = await _fingerprint()
    assert len(declared[0]) == 4 and len(declared[1]) == 1

    for statement in split_statements(_DOWN.read_text(encoding="utf-8")):
        if statement.strip():
            await database.execute(statement)
    assert (await _fingerprint()) == ([], [])
    for _ in range(2):
        for statement in split_statements(body):
            if statement.strip():
                await database.execute(statement)
    assert await _fingerprint() == declared

    metadata.create_all(engine, tables=[order_quote_claims])
    assert await _fingerprint() == declared
