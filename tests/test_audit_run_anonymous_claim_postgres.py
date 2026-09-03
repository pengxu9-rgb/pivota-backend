"""The anonymous-run claim against REAL Postgres — because the two properties
that matter cannot be tested anywhere else.

WHY THIS GATE EXISTS.

1. The claim is a RACE, and SQLite cannot run one. `databases` on SQLite
   serializes these writes, so an asyncio.gather of eight claimants there is
   eight SEQUENTIAL claims wearing a concurrency costume. That still catches a
   deleted `merchant_id IS NULL` guard (measured: 8 winners instead of 1), so
   this gate is NOT justified by that mutant — it is justified by the class
   SQLite structurally cannot reach: a guard that is correct sequentially and
   racy under real simultaneity, e.g. a SELECT-then-UPDATE pair, which passes
   every sequential test and hands two merchants the same run in production.
   Only genuinely concurrent transactions can fail that way.

2. The change is an ALTER on a table that ALREADY EXISTS in production with
   `merchant_id NOT NULL`. The fixture below therefore builds the table in its
   CURRENT production shape and then applies migration 210, so what runs here
   is the real upgrade path. Creating the table already-nullable would test a
   schema no deploy will ever perform.

`UPDATE ... RETURNING` is also asserted here rather than assumed: this is the
same idiom class as the #1960 `mark_revoked` defect, where a non-RETURNING
UPDATE answered None on asyncpg for success and no-match alike.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_audit_run_anonymous_claim_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "db/migrations" / "210_audit_runs_anonymous_claim.sql"
)

# Same convention as the sibling gates: this DROPs a table, so it must be
# incapable of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop merchant_audit_runs in database {dbname!r} — "
            f"throwaway only (e.g. pivota_dialect_check)"
        )


# The table AS PRODUCTION HAS IT TODAY: merchant_id NOT NULL. Migration 210 is
# then applied on top, so this gate exercises the upgrade, not a clean build.
_PROD_SHAPE = """
CREATE TABLE merchant_audit_runs (
  run_id       UUID PRIMARY KEY,
  merchant_id  TEXT NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status       TEXT NOT NULL,
  product_keys TEXT[] NOT NULL DEFAULT '{}'
)
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    await database.execute(_PROD_SHAPE)
    # A pre-existing owned run, so takeover is tested against a real neighbour.
    await database.execute(
        "INSERT INTO merchant_audit_runs (run_id, merchant_id, status) "
        "VALUES (:r, 'existing-merchant', 'succeeded')",
        {"r": str(uuid.uuid4())},
    )
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield
    mar._DDL_READY = False
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _anonymous_run() -> str:
    from db.database import database
    run_id = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO merchant_audit_runs (run_id, merchant_id, status) "
        "VALUES (:r, NULL, 'running')",
        {"r": run_id},
    )
    return run_id


async def test_the_migration_lifts_the_constraint_on_an_existing_table():
    """CREATE TABLE IF NOT EXISTS does nothing where the table already exists,
    which is every real environment — so the ALTER is the only thing that can
    make an anonymous insert possible."""
    from db.database import database
    row = await database.fetch_one(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'merchant_audit_runs' AND column_name = 'merchant_id'"
    )
    assert dict(row)["is_nullable"] == "YES"
    # ...and the existing row survived the migration untouched.
    kept = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs "
        "WHERE merchant_id = 'existing-merchant'"
    )
    assert dict(kept)["n"] == 1


async def test_the_migration_is_idempotent():
    from db.database import database
    from db.sql_migrations import split_statements
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)  # must not raise on a second pass


async def test_an_anonymous_run_can_be_inserted_and_claimed():
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await _anonymous_run()
    assert await mar.claim_audit_run_for_merchant(
        run_id=run_id, merchant_id="m-1"
    ) is True
    row = await database.fetch_one(
        "SELECT merchant_id, merchant_claimed_at FROM merchant_audit_runs "
        "WHERE run_id = :r", {"r": run_id},
    )
    assert dict(row)["merchant_id"] == "m-1"
    assert dict(row)["merchant_claimed_at"] is not None


async def test_a_claim_returns_False_on_asyncpg_when_nothing_matched():
    """The #1960 idiom check: a non-RETURNING UPDATE answers None on
    databases/asyncpg for success and no-match alike, which would make this
    function a constant. RETURNING is what makes False mean 'no row moved'."""
    import db.merchant_audit_runs as mar
    run_id = await _anonymous_run()
    assert await mar.claim_audit_run_for_merchant(
        run_id=run_id, merchant_id="m-1") is True
    assert await mar.claim_audit_run_for_merchant(
        run_id=run_id, merchant_id="m-2") is False
    assert await mar.claim_audit_run_for_merchant(
        run_id=str(uuid.uuid4()), merchant_id="m-3") is False


async def test_a_claim_never_takes_over_an_owned_run():
    import db.merchant_audit_runs as mar
    from db.database import database

    owned = await database.fetch_one(
        "SELECT run_id FROM merchant_audit_runs "
        "WHERE merchant_id = 'existing-merchant'"
    )
    assert await mar.claim_audit_run_for_merchant(
        run_id=str(dict(owned)["run_id"]), merchant_id="attacker"
    ) is False
    still = await database.fetch_one(
        "SELECT merchant_id FROM merchant_audit_runs WHERE run_id = :r",
        {"r": str(dict(owned)["run_id"])},
    )
    assert dict(still)["merchant_id"] == "existing-merchant"


async def test_concurrent_claimants_produce_exactly_one_winner():
    """Real simultaneity. A sequential run of this (which is all SQLite can
    give) would also pass against a SELECT-then-UPDATE implementation that
    hands two merchants the same run under load."""
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await _anonymous_run()
    results = await asyncio.gather(*[
        mar.claim_audit_run_for_merchant(run_id=run_id, merchant_id=f"m-{i}")
        for i in range(8)
    ])
    assert sum(1 for r in results if r) == 1, (
        f"exactly one claimant must win, got {sum(1 for r in results if r)}"
    )
    row = await database.fetch_one(
        "SELECT merchant_id FROM merchant_audit_runs WHERE run_id = :r",
        {"r": run_id},
    )
    assert dict(row)["merchant_id"] is not None


async def test_the_unclaimed_index_exists():
    """The funnel's only sweep is 'unclaimed runs, newest first'."""
    from db.database import database
    row = await database.fetch_one(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'idx_merchant_audit_runs_unclaimed'"
    )
    assert row is not None, "the partial index did not survive the migration"
    assert "merchant_id IS NULL" in dict(row)["indexdef"]
