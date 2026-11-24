"""The decline close's tests on Postgres: the SAME tests as tests/test_a2a_rail_decline_close.py (one source,
dialect-aware), collected here so the Postgres dialect gate runs them, plus the schema parity check.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_dialect_check \\
        pytest tests/test_a2a_rail_decline_close_postgres.py
"""

from __future__ import annotations

import os

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



async def test_migration_296_builds_the_table_and_trigger_and_its_down_removes_them():
    from db.database import database
    import db.a2a_rail_ledger as ledger
    from tests.test_a2a_rail_decline_close import _DOWN, _apply_migration_296, _split

    assert await ledger.manual_resolution_audit_ready()  # the fixture applied 296
    for statement in _split(_DOWN.read_text("utf-8")):
        await database.execute(statement)
    assert not await ledger.manual_resolution_audit_ready()
    row = await database.fetch_one("SELECT NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = "
                                   "'prevent_a2a_rail_manual_resolution_audit_mutation') AS gone")
    assert row["gone"]
    await _apply_migration_296()
    assert await ledger.manual_resolution_audit_ready()
