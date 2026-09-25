"""Production-dialect gate for migration 216 (the canonical order_ref).

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_canonical_order_ref_postgres.py

WHY REAL POSTGRES. Three things are only decidable here:

* `create_all` runs BEFORE migrations in this codebase, so a fresh database
  gets both ledger tables from the SQLAlchemy model and an existing one gets
  `order_ref` from migration 216. The two must build the same column shape.
* The unique index is `(merchant_id, COALESCE(store_id, ''), order_ref) WHERE
  order_ref IS NOT NULL` — an expression index with a predicate. SQLite has no
  `pg_indexes` to read the built definition back from, so only here can the
  model's declaration and the migration's DDL be compared as built objects.
* The stitch that converges two authorities onto one interaction takes
  `pg_advisory_xact_lock` on the order_ref key, a branch that is dead on
  SQLite (`IS_POSTGRES` is False there). The convergence case is re-run here
  with that path live.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = _REPO / "db" / "migrations" / "216_commerce_ledger_canonical_order_ref.sql"
_MIGRATION_213 = (
    _REPO / "db" / "migrations" / "213_commerce_ledger_write_path_authority.sql"
)
_DOWN = (
    _REPO / "db" / "migrations" / "down" / "216_commerce_ledger_canonical_order_ref_down.sql"
)

_TABLES = ("commerce_interactions", "commerce_interaction_events")
_UNIQUE_INDEX = "idx_commerce_interactions_order_ref_unique"

# This gate DROPS both ledger tables. Same convention as
# tests/test_commerce_ledger_write_path_authority_postgres.py: the "never point
# this at prod" promise is MADE true by refusing any non-throwaway database.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the commerce ledger tables in database {dbname!r}; "
            "this gate must only run against a throwaway such as pivota_dialect_check"
        )


async def _drop_tables() -> None:
    from db.database import database

    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    yield
    await _drop_tables()
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _order_ref_shapes():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT table_name, data_type, character_maximum_length, is_nullable
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND column_name = 'order_ref'
           AND table_name = ANY(:tables)
         ORDER BY table_name
        """,
        {"tables": list(_TABLES)},
    )
    return {
        row["table_name"]: (
            row["data_type"],
            row["character_maximum_length"],
            row["is_nullable"],
        )
        for row in rows
    }


async def _index_defs():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = current_schema()
           AND tablename = ANY(:tables)
           AND indexdef LIKE '%order_ref%'
         ORDER BY indexname
        """,
        {"tables": list(_TABLES)},
    )
    return {row["indexname"]: row["indexdef"] for row in rows}


async def _build_from_model():
    from sqlalchemy import create_engine

    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import metadata

    await _drop_tables()
    sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url)
    try:
        metadata.create_all(
            engine,
            tables=[commerce_interactions, commerce_interaction_events],
            checkfirst=True,
        )
    finally:
        engine.dispose()


async def _build_from_migration():
    """The pre-216 shape is what the model builds minus order_ref.

    The down file removes the column and every index over it — which is also
    the only honest way to reach the 213-era shape from a model-built table —
    and then 213 and 216 are applied as the real files, in order. Applying the
    files rather than a hand-copied ALTER is the point of the gate.
    """
    from db.database import database
    from db.sql_migrations import split_statements

    await _build_from_model()
    for statement in split_statements(_DOWN.read_text()):
        await database.execute(statement)
    assert await _order_ref_shapes() == {}, "down migration must remove both columns"
    assert await _index_defs() == {}, "down migration must remove every order_ref index"
    # 216 sits on top of 213 in the real chain; applying it here proves the two
    # do not fight over commerce_interaction_events.
    for statement in split_statements(_MIGRATION_213.read_text()):
        await database.execute(statement)
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)


async def test_model_and_migration_build_the_same_order_ref_column():
    await _build_from_model()
    from_model = await _order_ref_shapes()
    await _build_from_migration()
    from_migration = await _order_ref_shapes()

    assert set(from_model) == set(_TABLES)
    assert from_model == from_migration, {"model": from_model, "migration": from_migration}
    for table in _TABLES:
        assert from_model[table] == ("character varying", 160, "YES")


async def test_model_and_migration_build_the_same_indexes():
    await _build_from_model()
    from_model = await _index_defs()
    await _build_from_migration()
    from_migration = await _index_defs()

    assert set(from_model) == set(from_migration), {
        "model": sorted(from_model),
        "migration": sorted(from_migration),
    }
    assert from_model == from_migration, {"model": from_model, "migration": from_migration}


async def test_the_unique_index_is_scoped_by_merchant_and_store():
    """The definition, read back from Postgres, not from the source that made it."""
    await _build_from_migration()
    defs = await _index_defs()
    indexdef = defs.get(_UNIQUE_INDEX)
    assert indexdef, sorted(defs)
    assert "CREATE UNIQUE INDEX" in indexdef
    assert "ON public.commerce_interactions" in indexdef
    assert "merchant_id" in indexdef
    assert "COALESCE(store_id" in indexdef
    assert "order_ref" in indexdef
    assert "WHERE (order_ref IS NOT NULL)" in indexdef


async def test_the_unique_index_refuses_a_second_row_for_one_canonical_order():
    from db.database import database

    await _build_from_migration()
    await database.execute(
        """
        INSERT INTO commerce_interactions
            (interaction_id, merchant_id, store_id, order_id, order_ref)
        VALUES ('int_a', 'm', 's', 'ord_1', 'pivota:ord_1')
        """
    )
    with pytest.raises(Exception):
        await database.execute(
            """
            INSERT INTO commerce_interactions
                (interaction_id, merchant_id, store_id, order_id, order_ref)
            VALUES ('int_b', 'm', 's', '6600123', 'pivota:ord_1')
            """
        )
    # A NULL order_ref is not a duplicate of anything — legacy rows are unaffected.
    await database.execute(
        """
        INSERT INTO commerce_interactions
            (interaction_id, merchant_id, store_id, order_id)
        VALUES ('int_c', 'm', 's', 'legacy_1'), ('int_d', 'm', 's', 'legacy_2')
        """
    )
    total = await database.fetch_val("SELECT count(*) FROM commerce_interactions")
    assert total == 3


async def test_two_authorities_converge_on_one_interaction_on_real_postgres():
    """The stitch, with the advisory-lock path live.

    Same purchase, two native order ids, no click id to tie them together. On
    SQLite `_event_write_lock` yields immediately; here it takes the
    `stitch|...|order_ref|...` advisory lock and re-reads, which is the branch
    a production race actually runs.
    """
    from db.database import IS_POSTGRES, database
    from services.commerce_interaction_service import record_commerce_event

    assert IS_POSTGRES is True
    await _build_from_migration()

    for index, (event_type, order_id, key) in enumerate(
        (
            ("payment.succeeded", "ord_1", "stripe:pi_1"),
            ("order.paid", "6600123", "shopify:6600123"),
        )
    ):
        await record_commerce_event(
            event_type=event_type,
            occurred_at=datetime(2026, 9, 4, 10, index, tzinfo=timezone.utc),
            upstream_idempotency_key=key,
            merchant_id="m",
            platform="shopify",
            store_id="s",
            order_id=order_id,
            order_ref="pivota:ord_1",
        )

    interactions = await database.fetch_all(
        "SELECT interaction_id, order_ref, store_id FROM commerce_interactions"
    )
    assert len(interactions) == 1, [dict(row) for row in interactions]
    assert dict(interactions[0])["order_ref"] == "pivota:ord_1"

    events = await database.fetch_all(
        "SELECT interaction_id, order_ref FROM commerce_interaction_events ORDER BY event_id"
    )
    assert len(events) == 2
    assert {dict(row)["order_ref"] for row in events} == {"pivota:ord_1"}
    assert len({dict(row)["interaction_id"] for row in events}) == 1


async def test_two_different_refs_on_one_store_stay_two_interactions_on_postgres():
    from db.database import database
    from services.commerce_interaction_service import record_commerce_event

    await _build_from_migration()
    for index, ref in enumerate(("pivota:ord_1", "pivota:ord_2")):
        await record_commerce_event(
            event_type="order.paid",
            occurred_at=datetime(2026, 9, 4, 11, index, tzinfo=timezone.utc),
            upstream_idempotency_key=f"order:{ref}",
            merchant_id="m",
            platform="shopify",
            store_id="s",
            order_ref=ref,
            order_id=f"native_{index}",
        )
    total = await database.fetch_val("SELECT count(*) FROM commerce_interactions")
    assert total == 2
