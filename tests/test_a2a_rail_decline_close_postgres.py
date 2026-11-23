"""The decline close's tests on Postgres: the SAME tests as tests/test_a2a_rail_decline_close.py (one source,
dialect-aware), collected here so the Postgres dialect gate runs them, plus the schema parity check.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_a2a_rail_decline_close_postgres.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_a2a_rail_decline_close import *  # noqa: F401,F403 - the tests
from tests.test_a2a_rail_decline_close import _db, _dials  # noqa: F401 - the autouse fixtures

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]

pytestmark = pytest.mark.skipif(
    not _IS_PG or not any(m in _DBNAME or m in DATABASE_URL for m in _SAFE_DB_MARKERS),
    reason="needs a throwaway Postgres DATABASE_URL",
)

_COLUMNS_SQL = """
    SELECT column_name, data_type, is_nullable FROM information_schema.columns
     WHERE table_name = 'a2a_rail_manual_resolution_audit' ORDER BY column_name
"""
_CHECKS_SQL = """
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conrelid = to_regclass('a2a_rail_manual_resolution_audit') ORDER BY 1
"""


async def test_migration_296_and_the_boot_heal_build_the_same_table_and_trigger():
    from db.database import database
    import db.a2a_rail_ledger as ledger

    await database.execute("DROP TABLE IF EXISTS a2a_rail_manual_resolution_audit")
    await ledger.ensure_manual_resolution_audit_schema()
    healed = ([dict(r) for r in await database.fetch_all(_COLUMNS_SQL)],
              [r["def"] for r in await database.fetch_all(_CHECKS_SQL)])
    assert await ledger.manual_resolution_audit_ready()
    await database.execute("DROP TABLE a2a_rail_manual_resolution_audit")
    migration = (Path(__file__).resolve().parents[1] / "db/migrations/296_a2a_rail_manual_resolution_audit.sql")
    for statement in _split(migration.read_text("utf-8")):
        await database.execute(statement)
    migrated = ([dict(r) for r in await database.fetch_all(_COLUMNS_SQL)],
                [r["def"] for r in await database.fetch_all(_CHECKS_SQL)])
    assert migrated == healed
    assert await ledger.manual_resolution_audit_ready()
    down = (Path(__file__).resolve().parents[1] / "db/migrations/down/296_a2a_rail_manual_resolution_audit_down.sql")
    for statement in _split(down.read_text("utf-8")):
        await database.execute(statement)
    assert not await ledger.manual_resolution_audit_ready()
    await ledger.ensure_manual_resolution_audit_schema()


def _split(sql):
    """Statements of a migration file, keeping a $fn$ function body whole."""
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    out, buf, in_body = [], [], False
    for line in lines:
        buf.append(line)
        if "$fn$" in line:
            in_body = not in_body if line.count("$fn$") == 1 else in_body
        if line.rstrip().endswith(";") and not in_body:
            text = "\n".join(buf).strip()
            if text:
                out.append(text)
            buf = []
    return out
