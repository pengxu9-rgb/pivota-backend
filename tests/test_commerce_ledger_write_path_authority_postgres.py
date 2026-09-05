"""Production-dialect gate for migrations 213/214 (ledger trust provenance).

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_commerce_ledger_write_path_authority_postgres.py

WHY REAL POSTGRES. `create_all` runs BEFORE migrations in this codebase, so a
fresh database gets `commerce_interaction_events` from the SQLAlchemy model and
an existing one gets the four provenance columns from migration 213. The two
must build the SAME column shape or the funnel's `synthetic IS FALSE` predicate
and the ledger INSERT behave differently per environment. SQLite has no
information_schema, no BOOLEAN, and no partial-index WHERE to compare, so the
comparison is only meaningful here: build the table both ways, read
information_schema, diff.
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

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db" / "migrations" / "213_commerce_ledger_write_path_authority.sql"
_INDEX_MIGRATION = _REPO / "db" / "migrations" / "214_commerce_ledger_synthetic_index.sql"
_DOWN = _REPO / "db" / "migrations" / "down" / "213_commerce_ledger_write_path_authority_down.sql"
_PROVENANCE_COLUMNS = ("write_path", "authority", "agent_identity_confidence", "synthetic")

# This gate DROPS commerce_interaction_events. Same convention as
# tests/test_card_rail_outcomes_postgres.py: the "never point this at prod"
# promise is MADE true by refusing any database that is not a throwaway.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop commerce_interaction_events in database {dbname!r}; "
            "this gate must only run against a throwaway such as pivota_dialect_check"
        )


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    yield
    await database.execute("DROP TABLE IF EXISTS commerce_interaction_events")
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _column_shapes():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT column_name, data_type, character_maximum_length, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'commerce_interaction_events'
           AND column_name = ANY(:names)
         ORDER BY column_name
        """,
        {"names": list(_PROVENANCE_COLUMNS)},
    )
    return {
        row["column_name"]: (
            row["data_type"],
            row["character_maximum_length"],
            row["is_nullable"],
            str(row["column_default"] or "").lower(),
        )
        for row in rows
    }


async def _build_from_model():
    from sqlalchemy import create_engine

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database, metadata

    await database.execute("DROP TABLE IF EXISTS commerce_interaction_events")
    sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url)
    try:
        metadata.create_all(engine, tables=[commerce_interaction_events], checkfirst=True)
    finally:
        engine.dispose()


async def _build_from_migration():
    """The pre-213 shape is whatever the model built minus the four columns;
    then the migration itself adds them back. Applying the real file, not a
    hand-copied ALTER, is the point of the gate."""
    from db.database import database
    from db.sql_migrations import split_statements

    await _build_from_model()
    for statement in split_statements(_DOWN.read_text()):
        await database.execute(statement)
    assert await _column_shapes() == {}, "down migration must remove all four columns"
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)


async def test_model_and_migration_build_the_same_provenance_columns():
    await _build_from_model()
    from_model = await _column_shapes()
    await _build_from_migration()
    from_migration = await _column_shapes()

    assert set(from_model) == set(_PROVENANCE_COLUMNS)
    assert from_model == from_migration, {"model": from_model, "migration": from_migration}
    assert from_model["synthetic"][0] == "boolean"
    assert from_model["synthetic"][2] == "NO"
    assert from_model["synthetic"][3] == "false"
    assert from_model["write_path"] == ("character varying", 48, "YES", "")
    assert from_model["authority"] == ("character varying", 16, "YES", "")
    assert from_model["agent_identity_confidence"] == ("character varying", 24, "YES", "")


async def test_migration_is_idempotent_on_a_table_that_already_has_the_columns():
    from db.database import database
    from db.sql_migrations import split_statements

    await _build_from_model()
    before = await _column_shapes()
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    assert await _column_shapes() == before


async def test_the_synthetic_partial_index_matches_the_funnel_predicate():
    from db.database import database
    from db.sql_migrations import needs_autocommit, split_statements

    await _build_from_model()
    body = _INDEX_MIGRATION.read_text()
    assert needs_autocommit(body), "214 must be recognised as a CONCURRENTLY file"
    for statement in split_statements(body):
        # Outside any explicit transaction the `databases` execute path is
        # autocommit for asyncpg, which is what CONCURRENTLY requires.
        await database.execute(statement)
    indexdef = await database.fetch_val(
        """
        SELECT indexdef FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname = 'idx_commerce_interaction_events_synthetic'
        """
    )
    assert indexdef, "partial index was not created"
    assert "WHERE (synthetic IS TRUE)" in indexdef
    assert "(merchant_id, occurred_at DESC)" in indexdef


async def test_a_pre_migration_row_is_not_a_probe_and_a_stamped_row_is():
    """The exclusion predicate the funnel uses, run on the real dialect."""
    from db.database import database

    await _build_from_migration()
    await database.execute(
        """
        INSERT INTO commerce_interaction_events
            (event_id, interaction_id, merchant_id, event_type, occurred_at, synthetic)
        VALUES
            ('evt_real',  'int_1', 'm', 'order.paid', now(), DEFAULT),
            ('evt_probe', 'int_1', 'm', 'order.paid', now(), TRUE)
        """
    )
    visible = await database.fetch_all(
        """
        SELECT event_id FROM commerce_interaction_events
         WHERE merchant_id = 'm'
           AND (synthetic IS NULL OR synthetic = false)
         ORDER BY event_id
        """
    )
    assert [row["event_id"] for row in visible] == ["evt_real"]
