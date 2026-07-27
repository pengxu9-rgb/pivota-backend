"""Real-Postgres gate for the offer currency/market relabel backfill.

WHY THIS FILE EXISTS. The rest of this repo's suite runs on SQLite, and the
backfill's two statements are Postgres-only on three separate counts:

  * ``btrim()`` — Postgres-only (SQLite has ``trim()``; that asymmetry already
    broke the suite once, in #1568, in the opposite direction);
  * ``= ANY(:sources)`` array binding — no SQLite equivalent;
  * ``count(*) FILTER (WHERE ...)`` — the live/suppressed split added on
    2026-07-27.

None of those can be exercised by the existing string-assertion tests, which
only prove a substring is present. #1588 (untyped ``concat`` bind) and #1593
(a sliced ``AS anon_1`` tail) both shipped past green SQLite and past review,
and both were statements that Postgres simply refuses. So this file EXECUTES
both statements against a real engine on empty tables — the failure class is
PREPARE, and zero rows are enough to detect it.

RUNNING IT (discovered automatically by the ``tests/test_*_postgres.py`` CI job):

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_offer_currency_backfill_postgres.py

Never point this at prod: it runs the UPDATE. Against an empty table that is a
no-op, which is exactly why the fixture is empty and stays empty.
"""

from __future__ import annotations

import os

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

# external_product_seeds is a lightweight `table()` construct, not a MetaData
# Table, so `metadata.create_all` does not know it. Only the columns the
# backfill's correlated subquery touches are needed.
#
# CREATE TABLE IF NOT EXISTS IS NOT ENOUGH HERE, and assuming it was cost a red CI
# run. The gate workflow runs EVERY `tests/test_*_postgres.py` file in ONE pytest
# invocation against ONE database, and two sibling gates
# (`test_citation_read_surfaces_postgres.py`, `test_pdp_content_depth_postgres.py`)
# declare their own, DIFFERENT `external_product_seeds` — neither carries `id`,
# `domain` or `updated_at`. Whichever file runs first wins; the others' CREATE
# silently no-ops and their queries then die on `UndefinedColumnError`.
# Alphabetical collection puts the citation gate first, so this file passed in
# isolation locally and failed in CI: the exact "IF NOT EXISTS quietly did
# nothing" trap, one level down from the defects this gate exists to catch.
#
# So: ADD COLUMN IF NOT EXISTS for every column, and for the SIBLINGS' columns
# too, not just this gate's. Healing only our own columns is not enough — it fixes
# the case where a sibling created the table, but breaks the mirror case where
# THIS file creates it and the siblings' CREATE then no-ops (verified: running
# this file first turns the other two red). The siblings have no ALTERs of their
# own, so the union has to be guaranteed by whoever gets there first.
#
# Both siblings declare the same seven columns; ours are `id`, `domain`,
# `updated_at`. Additive and safe in either direction — every gate inserts with an
# explicit column list, so extra nullable columns are invisible to all of them.
#
# The real fix is one shared fixture in conftest instead of three files racing to
# define the same table. That is a refactor across gates this PR does not own; the
# union here makes the gate order-independent today, which is what unblocks it.
_SEED_COLUMNS = (
    # this gate's
    ("id", "text"),
    ("domain", "text"),
    ("updated_at", "timestamp"),
    # the sibling gates' (test_citation_read_surfaces / test_pdp_content_depth)
    ("external_product_id", "text"),
    ("attached_product_key", "text"),
    ("status", "text"),
    ("merchant_id", "text"),
    ("source", "text"),
    ("product_key", "text"),
    ("source_product_id", "text"),
)

_LIGHTWEIGHT_DDL = "CREATE TABLE IF NOT EXISTS external_product_seeds (id text);\n" + "\n".join(
    f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {typ};"
    for name, typ in _SEED_COLUMNS
)


@pytest.fixture(scope="module")
def pg_schema():
    import db.catalog  # noqa: F401  (registers catalog_offers on the shared MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _mod():
    import importlib

    return importlib.import_module("scripts.backfill_offer_market_currency")


@pytest.mark.asyncio
@pytest.mark.parametrize("live_only", [False, True])
async def test_domain_scan_prepares_and_executes(pg_schema, live_only):
    """Both scope renderings must PREPARE. The FILTER aggregate is the new risk."""
    from db.database import database

    mod = _mod()
    await database.connect()
    try:
        rows = await database.fetch_all(
            mod.domains_sql(live_only),
            {"sources": list(mod._SEED_SOURCES), "min_offers": 3},
        )
    finally:
        await database.disconnect()
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("live_only", [False, True])
async def test_update_prepares_and_executes(pg_schema, live_only):
    """The UPDATE ... RETURNING must PREPARE in both scope renderings.

    Empty table ⇒ zero rows written; the assertion is that Postgres accepted the
    statement at all. `RETURNING` + fetch_all is the shape the script relies on
    to count writes (`database.execute()` returns None for an UPDATE without
    RETURNING — that gotcha already reported 0 while writing 664 rows).
    """
    from db.database import database

    mod = _mod()
    await database.connect()
    try:
        rows = await database.fetch_all(
            mod.update_offers_sql(live_only),
            {
                "cur": "INR",
                "mkt": "IN",
                "sources": list(mod._SEED_SOURCES),
                "domain": "example-nonexistent.test",
            },
        )
    finally:
        await database.disconnect()
    assert rows == []
