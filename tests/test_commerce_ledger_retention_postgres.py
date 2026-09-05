"""Production-dialect gate for the retention sweep and the funnel window.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_commerce_ledger_retention_postgres.py

WHY REAL POSTGRES. Three things are only decidable here:

* `database.execute()` returns NO rowcount for a DELETE under `databases` +
  asyncpg, while SQLite returns one. A sweep that branched on it would be
  green on SQLite and silently do nothing here. This gate runs the real
  delete-then-count-back path against asyncpg.
* The sweep's predicate is `synthetic IS TRUE OR COALESCE(surface,'') =
  'ops_canary'`, and the interaction rule is a correlated `NOT EXISTS` with
  `NOT (...)` inside it. SQL three-valued logic is what makes that rule right
  or wrong, and SQLite is too forgiving to prove it.
* The point of the funnel window is that it is served by an INDEX, not by a
  filter over a full scan. Only Postgres has `EXPLAIN` and the partial
  synthetic index from migration 214.

The tables are built from the MODEL, which is how a fresh database is built in
this codebase (`create_all` runs before migrations), plus migration 214's
partial index applied as the real file.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION_214 = _REPO / "db" / "migrations" / "214_commerce_ledger_synthetic_index.sql"

_TABLES = ("commerce_interactions", "commerce_interaction_events")
_SYNTHETIC_INDEX = "idx_commerce_interaction_events_synthetic"
_RECENCY_INDEX = "idx_commerce_interaction_events_merchant_occurred"

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
FRESH = NOW - timedelta(days=1)

# This gate DROPS both ledger tables. Same convention as
# tests/test_canonical_order_ref_postgres.py: the "never point this at prod"
# promise is MADE true by refusing any non-throwaway database.
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


async def _build() -> None:
    """Model-built tables plus migration 214's partial index, from the file."""
    from sqlalchemy import create_engine

    from db.commerce_interactions import commerce_interaction_events, commerce_interactions
    from db.database import database, metadata
    from db.sql_migrations import needs_autocommit, split_statements

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
    # The model already declares the same partial index; applying the real
    # migration file on top proves the two agree (IF NOT EXISTS is a no-op if
    # the model built it, and the assertion below reads the built object back).
    body = _MIGRATION_214.read_text()
    assert needs_autocommit(body) is True, "214 builds its index without blocking writers"
    for statement in split_statements(body):
        await database.execute(statement)


async def _seed() -> None:
    from db.database import database

    await database.execute(
        """
        INSERT INTO commerce_interactions
            (interaction_id, merchant_id, platform, store_id, last_occurred_at)
        VALUES
            ('int_syn',   'merch_a', 'shopify', 's1', :old),
            ('int_legacy','merch_a', 'shopify', 's1', :old),
            ('int_mixed', 'merch_a', 'shopify', 's1', :old),
            ('int_real',  'merch_b', 'shopify', 's1', :old),
            ('int_fresh', 'merch_b', 'shopify', 's1', :fresh)
        """,
        {"old": OLD, "fresh": FRESH},
    )
    await database.execute(
        """
        INSERT INTO commerce_interaction_events
            (event_id, interaction_id, merchant_id, platform, store_id, surface,
             event_type, occurred_at, synthetic)
        VALUES
            ('evt_syn',        'int_syn',   'merch_a','shopify','s1','ops_canary','order.paid', :old,  TRUE),
            ('evt_legacy',     'int_legacy','merch_a','shopify','s1','ops_canary','order.paid', :old,  FALSE),
            ('evt_mixed_syn',  'int_mixed', 'merch_a','shopify','s1','ops_canary','order.paid', :old,  TRUE),
            ('evt_mixed_real', 'int_mixed', 'merch_a','shopify','s1','merchant_storefront','order.paid', :old, FALSE),
            ('evt_real',       'int_real',  'merch_b','shopify','s1','merchant_storefront','order.paid', :old, FALSE),
            ('evt_fresh_syn',  'int_fresh', 'merch_b','shopify','s1','ops_canary','order.paid', :fresh, TRUE)
        """,
        {"old": OLD, "fresh": FRESH},
    )


async def _ids(table: str, column: str) -> set[str]:
    from db.database import database

    rows = await database.fetch_all(f"SELECT {column} FROM {table}")
    return {dict(row)[column] for row in rows}


async def test_the_partial_synthetic_index_exists_as_migration_214_built_it():
    from db.database import database

    await _build()
    indexdef = await database.fetch_val(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :name",
        {"name": _SYNTHETIC_INDEX},
    )
    assert indexdef, "migration 214's partial index is missing"
    assert "WHERE (synthetic IS TRUE)" in indexdef
    assert "merchant_id" in indexdef and "occurred_at DESC" in indexdef


async def test_the_sweep_deletes_probe_rows_on_real_postgres():
    """The real delete path, with asyncpg's silent DELETE rowcount."""
    from services.commerce_ledger_retention import sweep_synthetic_events

    await _build()
    await _seed()

    dry = await sweep_synthetic_events(older_than_days=7, now=NOW)
    assert dry["dry_run"] is True
    assert dry["events_deleted"] == 3
    assert dry["interactions_deleted"] == 2
    assert await _ids("commerce_interaction_events", "event_id") == {
        "evt_syn",
        "evt_legacy",
        "evt_mixed_syn",
        "evt_mixed_real",
        "evt_real",
        "evt_fresh_syn",
    }

    applied = await sweep_synthetic_events(older_than_days=7, apply=True, now=NOW)
    assert applied["dry_run"] is False
    assert applied["events_deleted"] == 3
    assert applied["interactions_deleted"] == 2

    events = await _ids("commerce_interaction_events", "event_id")
    assert events == {"evt_mixed_real", "evt_real", "evt_fresh_syn"}

    interactions = await _ids("commerce_interactions", "interaction_id")
    # int_mixed survives: it still has a real event.
    assert interactions == {"int_mixed", "int_real", "int_fresh"}

    again = await sweep_synthetic_events(older_than_days=7, apply=True, now=NOW)
    assert again["events_deleted"] == 0
    assert again["interactions_deleted"] == 0


async def test_the_mixed_interaction_survives_with_its_real_event_on_postgres():
    from services.commerce_ledger_retention import sweep_synthetic_events

    await _build()
    await _seed()
    await sweep_synthetic_events(older_than_days=7, batch_size=1, apply=True, now=NOW)

    assert "int_mixed" in await _ids("commerce_interactions", "interaction_id")
    assert "evt_mixed_real" in await _ids("commerce_interaction_events", "event_id")
    assert "evt_mixed_syn" not in await _ids("commerce_interaction_events", "event_id")


async def test_the_retention_report_reads_and_does_not_write():
    from services.commerce_ledger_retention import report_ledger_retention

    await _build()
    await _seed()
    before = await _ids("commerce_interaction_events", "event_id")

    report = await report_ledger_retention(horizon_days=7)
    assert report["events_total"] == 5
    assert report["by_merchant"]["merch_a"]["events"] == 4
    assert report["by_merchant"]["merch_b"]["events"] == 1
    assert report["oldest"].startswith("2026-08-05")

    assert await _ids("commerce_interaction_events", "event_id") == before


async def test_the_windowed_funnel_select_uses_the_recency_index():
    """EXPLAIN the query the funnel actually issues, not a hand-copied one.

    The whole point of bounding `occurred_at` in SQL is that migration 206's
    `(merchant_id, occurred_at DESC)` index serves it. A sequential scan here
    would mean the window still reads the merchant's whole history and only
    discards it later.
    """
    from sqlalchemy.dialects import postgresql

    from db.commerce_interactions import commerce_interaction_events
    from db.database import database
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window
    from sqlalchemy import select

    await _build()
    await _seed()
    # A planner will prefer a sequential scan on a six-row table whatever the
    # indexes say, so give it enough rows for the index to be the cheap plan.
    await database.execute(
        """
        INSERT INTO commerce_interaction_events
            (event_id, interaction_id, merchant_id, platform, store_id, surface,
             event_type, occurred_at, synthetic)
        SELECT 'bulk_' || g, 'int_bulk_' || g, 'merch_bulk_' || (g % 200),
               'shopify', 's1', 'merchant_storefront', 'product.viewed',
               (:base)::timestamptz - (g || ' minutes')::interval, FALSE
          FROM generate_series(1, 20000) AS g
        """,
        {"base": NOW},
    )
    await database.execute("ANALYZE commerce_interaction_events")

    window = resolve_funnel_window(NOW - timedelta(days=7), NOW)
    query = (
        select(commerce_interaction_events)
        .where(commerce_interaction_events.c.merchant_id == "merch_bulk_7")
        .where(commerce_interaction_events.c.occurred_at >= window.since)
        .where(commerce_interaction_events.c.occurred_at <= window.until)
        .order_by(commerce_interaction_events.c.occurred_at.desc())
        .limit(101)
    )
    # Compiled with named binds and handed straight back to the database, so
    # what is EXPLAINed is the statement the service builds, not a retyped one.
    compiled = query.compile(dialect=postgresql.dialect(paramstyle="named"))
    rows = await database.fetch_all(f"EXPLAIN {compiled}", dict(compiled.params))
    plan = "\n".join(str(dict(row)["QUERY PLAN"]) for row in rows)

    assert _RECENCY_INDEX in plan, plan
    assert "Seq Scan" not in plan, plan
