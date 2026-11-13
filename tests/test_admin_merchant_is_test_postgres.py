"""`merchant_onboarding.is_test` (migration 282) and its admin route on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_m05_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_admin_merchant_is_test_postgres.py

Runs every case in tests/test_admin_merchant_is_test.py on Postgres, plus what only Postgres can say:
  * migration 282 is idempotent and makes the column it says (BOOLEAN NOT NULL DEFAULT FALSE,
    existing rows FALSE), and its down file removes it;
  * schema_guard's boot heal adds the same column where the migration never ran (prod fast mode);
  * two writers of the same value: the conditional UPDATE lets exactly one of them change the row.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

from tests.test_admin_merchant_is_test import *  # noqa: E402,F401,F403 - the cases, on this dialect
from tests.test_admin_merchant_is_test import (  # noqa: E402
    M_TEST,
    _MIGRATION,
    database,
    rows,  # noqa: F401 - fixture
)


async def _column_shape() -> dict:
    row = await database.fetch_one(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'merchant_onboarding' "
        "AND column_name = 'is_test'"
    )
    return dict(row) if row else {}


@contextlib.asynccontextmanager
async def _column_removed_only_inside_a_rolled_back_transaction():
    """Patch, never drop: a case that needs merchant_onboarding without is_test removes it inside a
    transaction that is always rolled back. Postgres DDL is transactional, so no other connection
    (or later file in the dialect gate's one shared database) ever sees the table without the
    column, pass or fail. Everything inside runs on `database` in this task, so it shares the
    transaction; probe with _column_shape() (information_schema), not _has_column(), whose failing
    SELECT would abort the transaction."""
    tx = await database.transaction()
    try:
        yield
    finally:
        await tx.rollback()
    assert (await _column_shape())["data_type"] == "boolean", "the rollback must restore is_test"


async def test_migration_282_is_idempotent_and_makes_the_column_it_says(rows):
    from db.sql_migrations import split_statements

    for _ in range(2):
        for statement in split_statements(_MIGRATION.read_text()):
            await database.execute(statement)
    assert await _column_shape() == {
        "data_type": "boolean",
        "is_nullable": "NO",
        "column_default": "false",
    }
    row = await database.fetch_one(
        "SELECT is_test FROM merchant_onboarding WHERE merchant_id = :m", {"m": M_TEST}
    )
    assert row["is_test"] is False


async def test_down_migration_drops_the_column_and_282_puts_it_back(rows):
    from db.sql_migrations import split_statements

    down = _MIGRATION.parent / "down/282_merchant_onboarding_is_test_down.sql"
    async with _column_removed_only_inside_a_rolled_back_transaction():
        for _ in range(2):  # the down is idempotent too
            for statement in split_statements(down.read_text()):
                await database.execute(statement)
        assert await _column_shape() == {}
        for statement in split_statements(_MIGRATION.read_text()):
            await database.execute(statement)
        assert (await _column_shape())["data_type"] == "boolean"


def _boot_heal_statement() -> str:
    """The is_test heal exactly as db/schema_guard.py carries it.

    Read from the source rather than by running ensure_required_schema_light(): on a fresh database
    that function's Postgres branch stops at an earlier index on a table this file does not create
    (it is one try-block, as its own comments say), long before the merchant_onboarding heals. In
    production those tables exist. tests/test_schema_guard_migration_coverage.py pins that the heal
    exists; this pins that it runs, through the same guard wrapper the boot uses."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "db/schema_guard.py").read_text()
    found = re.findall(
        r"ALTER TABLE IF EXISTS merchant_onboarding\s+ADD COLUMN IF NOT EXISTS is_test [^;]*;", source
    )
    assert len(found) == 1, found
    return found[0]


async def test_boot_heal_adds_the_column_when_the_migration_never_ran(rows):
    from db.schema_guard import check_required_schema, guarded_add_columns

    async with _column_removed_only_inside_a_rolled_back_transaction():
        await database.execute("ALTER TABLE merchant_onboarding DROP COLUMN is_test")
        assert await _column_shape() == {}
        assert "is_test" in (await check_required_schema()).get("merchant_onboarding", [])

        for _ in range(2):  # and a second boot is a no-op
            for statement in guarded_add_columns(_boot_heal_statement()):
                await database.execute(statement)

        assert await _column_shape() == {
            "data_type": "boolean",
            "is_nullable": "NO",
            "column_default": "false",
        }
        assert "merchant_onboarding" not in await check_required_schema()
        row = await database.fetch_one(
            "SELECT is_test FROM merchant_onboarding WHERE merchant_id = :m", {"m": M_TEST}
        )
        assert row["is_test"] is False


async def test_two_writers_of_the_same_value_change_the_row_once(rows):
    """The second UPDATE blocks on the first's row lock, re-checks `is_test <> true` once it
    commits, and matches nothing: one audit row, never two."""
    import asyncpg

    from services.merchant_is_test_flag import SET_IS_TEST_SQL

    sql = SET_IS_TEST_SQL.replace(":v", "$1").replace(":m", "$2")
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    first = await asyncpg.connect(url)
    second = await asyncpg.connect(url)
    try:
        tx = first.transaction()
        await tx.start()
        assert await first.fetchval(sql, True, M_TEST) == M_TEST
        racing = asyncio.ensure_future(second.fetchval(sql, True, M_TEST))
        await asyncio.sleep(0.3)
        assert not racing.done(), "the second writer must wait on the first's row lock"
        await tx.commit()
        assert await asyncio.wait_for(racing, 5) is None
    finally:
        await first.close()
        await second.close()


def test_scratch_database_name_is_a_throwaway():
    # These cases insert and delete rows and run DDL: never point them at a database that matters.
    name = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    assert "dialect_check" in name or "test" in name, name
