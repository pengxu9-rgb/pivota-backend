"""Production-dialect gate for the persisted row-grain renderability column.

TWO THINGS ONLY REAL POSTGRES CAN SETTLE HERE.

1. **PREPARE.** ``pdp_will_render_expression`` is a large composite with several
   correlated EXISTS legs; the compute SELECT wraps it and the UPDATE embeds it.
   This repo has shipped statements Postgres refused to prepare while a green
   SQLite suite passed them (#1588, #1593) — so the statements are EXECUTED here
   rather than compile-asserted.

2. **PARITY — the one that matters.** The column's entire justification is that
   there is ONE implementation of renderability. A persisted value that drifts
   from the live expression is the fourth twin under a different name, and the
   drift would be invisible because both sides look authoritative. So this
   asserts, row for row on real Postgres, that the persisted column equals the
   live expression — including on a fixture where the answer is genuinely mixed,
   because a column that is false for everything looks identical to a working
   one.

Run:

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_pdp_renderability_store_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import os

import pytest
from contextlib import asynccontextmanager

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

# Shared lightweight tables are created ADDITIVELY (CREATE IF NOT EXISTS with a
# minimal column set, then ADD COLUMN IF NOT EXISTS). The gate job runs every
# tests/test_*_postgres.py against ONE database in ONE pytest process, and the
# sibling gate files declare some of these with DIFFERENT column sets — a plain
# CREATE makes whichever module runs first the winner and the others fail on a
# missing column. Measured on this repo's own gate. The last four
# index_pipeline_state columns are the union the siblings need, so this file can
# never be the one that narrows a shared table.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (id text);
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_kind text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS merchant_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS source_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS domain text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data jsonb;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz;
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS pipeline_stage text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS last_extracted_at timestamptz;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS index_eligible boolean;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_code text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS blocker_detail text;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS content_quality_score double precision;
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS quality_scored_at timestamp;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render boolean;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render_computed_at timestamptz;
"""


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _insert_product(conn, *, pk, ck, sig, source_system, source_product_id, merchant="external_seed"):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, pivota_signature_id, source_system, catalog_track) "
            "VALUES (:pk, :m, 'external_seed', :spid, :pk, :ck, :sig, :ss, 'external_referral')"
        ),
        {"pk": pk, "m": merchant, "spid": source_product_id, "ck": ck, "sig": sig, "ss": source_system},
    )


@pytest.fixture(scope="module")
def seeded(pg_engine):
    """A fixture whose renderability answer is genuinely MIXED.

    Deliberately not all-true or all-false: a column that is false for
    everything is indistinguishable from a working one, so parity over a
    single-valued fixture proves nothing.

    Two rows share content_key `ck_split` and DISAGREE — one seed-routed and
    serving-eligible (renders), one with no seed at all (dead). That is the
    279-content_key case from prod, in miniature, and it is the case a
    content_key-grain column could not represent.
    """
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        for t in ("catalog_products", "external_product_seeds", "index_pipeline_state"):
            conn.execute(text(f"DELETE FROM {t}"))

        # serving-eligible key, two rows, only one of which has a content route
        conn.execute(text("INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES ('ck_split', true)"))
        _insert_product(conn, pk="pk_live", ck="ck_split", sig="sig_live",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_live")
        conn.execute(text(
            "INSERT INTO external_product_seeds (external_product_id, status, source_product_id) "
            "VALUES ('ext_live', 'active', 'ext_live')"
        ))
        _insert_product(conn, pk="pk_dead", ck="ck_split", sig="sig_dead",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_missing")

        # NOT serving-eligible: route resolves, serving gate refuses → dead
        conn.execute(text("INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES ('ck_blocked', false)"))
        _insert_product(conn, pk="pk_blocked", ck="ck_blocked", sig="sig_blocked",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_blocked")
        conn.execute(text(
            "INSERT INTO external_product_seeds (external_product_id, status, source_product_id) "
            "VALUES ('ext_blocked', 'active', 'ext_blocked')"
        ))

        # no index_pipeline_state row at all → serving gate fails closed
        _insert_product(conn, pk="pk_noips", ck="ck_noips", sig="sig_noips",
                        source_system="external_product_seeds_mirror_v1", source_product_id="ext_noips")
    return pg_engine


def test_compute_select_prepares_and_executes(pg_engine):
    """The composite is large and correlated; PREPARE is the failure class."""
    from services.pdp_renderability_store import _compute_select

    with pg_engine.connect() as conn:
        conn.execute(_compute_select()).fetchall()
        conn.execute(_compute_select(content_keys=["ck_split"])).fetchall()
        conn.execute(_compute_select(product_keys=["pk_live"])).fetchall()


def test_fixture_is_genuinely_mixed(seeded):
    """Guard the guard: parity over an all-false fixture proves nothing."""
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        values = {r._mapping["product_key"]: r._mapping["will_render"]
                  for r in conn.execute(_compute_select()).fetchall()}
    assert any(values.values()), f"fixture is all-false — parity would be vacuous: {values}"
    assert not all(values.values()), f"fixture is all-true — parity would be vacuous: {values}"


def test_row_grain_is_real_two_sigs_one_content_key_disagree(seeded):
    """The finding that moved this column off index_pipeline_state.

    Same content_key, one sig renders and its sibling does not. No single
    per-content_key value is correct for such a key — which is why the column
    lives on catalog_products (PK product_key) and not on index_pipeline_state
    (PK content_key).
    """
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        rows = {r._mapping["product_key"]: r._mapping["will_render"]
                for r in conn.execute(_compute_select(content_keys=["ck_split"])).fetchall()}
    assert rows["pk_live"] is True
    assert rows["pk_dead"] is False


def test_serving_gate_and_missing_ips_both_fail_closed(seeded):
    from services.pdp_renderability_store import _compute_select

    with seeded.connect() as conn:
        rows = {r._mapping["product_key"]: r._mapping["will_render"]
                for r in conn.execute(_compute_select()).fetchall()}
    assert rows["pk_blocked"] is False, "serving_eligible=false must not render"
    assert rows["pk_noips"] is False, "no index_pipeline_state row must fail CLOSED"


@asynccontextmanager
async def prod_db():
    """The PRODUCTION driver — `databases.Database` over asyncpg.

    ⚠️ USE THE GATE'S OWN PATTERN. NEVER HAND-ROLL A DB SHIM, AND NEVER HOLD THE
    CONNECTION ACROSS TESTS.

    An earlier cut wrapped SQLAlchemy's SYNC engine (psycopg2) in a bespoke async
    shim to call `_persist`. It passed 32/32 while the shipped write path RAISED
    on the production driver — `databases._build_query` hands a non-`str` with
    values to `query.values(**values)`, and `TextClause` has no `.values()`. The
    module would have shipped a silent no-op behind a fully green suite. A
    hand-rolled shim re-introduces the whole driver-difference class the gate
    exists to close, and does it invisibly, because it looks like MORE thorough
    testing rather than less.

    A second cut held one connection open in a module fixture. Under
    `asyncio_mode=auto` each test gets its OWN event loop, so every test after
    the first reused a connection bound to a dead loop. The gate files
    (`test_pdp_content_depth_postgres:99`) connect and disconnect INSIDE each
    test for exactly that reason — so this does too.
    """
    from db.database import database

    await database.connect()
    try:
        yield database
    finally:
        await database.disconnect()


def _reset(seeded, sql):
    from sqlalchemy import text

    with seeded.begin() as conn:
        conn.execute(text(sql))


def _read(seeded, sql):
    from sqlalchemy import text

    with seeded.connect() as conn:
        return conn.execute(text(sql)).fetchall()


@pytest.mark.asyncio
async def test_persist_runs_on_the_PRODUCTION_driver(seeded):
    """PARITY, executed through asyncpg — the assertion this column depends on.

    Two things at once: the persisted value equals the live expression row for
    row, AND the write path can actually EXECUTE where it will run.
    """
    from services.pdp_renderability_store import (
        COLUMN_COMPUTED_AT,
        COLUMN_WILL_RENDER,
        _compute_select,
        _persist,
    )

    _reset(seeded, f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = NULL, "
                   f"{COLUMN_COMPUTED_AT} = NULL")

    async with prod_db() as db:
        rows = await db.fetch_all(_compute_select())
        written = await _persist(rows, database=db)

    assert written == len(rows) > 0, "the write path must report what it wrote"

    with seeded.connect() as conn:
        live = {r._mapping["product_key"]: bool(r._mapping["will_render"])
                for r in conn.execute(_compute_select()).fetchall()}
    stored = dict(_read(seeded, f"SELECT product_key, {COLUMN_WILL_RENDER} FROM catalog_products"))
    unstamped = _read(seeded, f"SELECT count(*) FROM catalog_products "
                              f"WHERE {COLUMN_COMPUTED_AT} IS NULL")[0][0]

    assert stored == live, f"persisted column drifted from its source: {stored} != {live}"
    assert unstamped == 0, "every written row must carry a computed_at stamp"


@pytest.mark.asyncio
async def test_persist_updates_only_the_rows_it_was_given(seeded):
    """`WHERE tgt.product_key = src.pk` is load-bearing — an `OR TRUE` mutant set
    all 14,104 rows to one row's value, with parity still green because parity
    re-read the same corrupted table."""
    from services.pdp_renderability_store import COLUMN_WILL_RENDER, _persist

    _reset(seeded, f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = NULL")

    async with prod_db() as db:
        await _persist([{"product_key": "pk_dead", "will_render": True}], database=db)

    rows = dict(_read(seeded, f"SELECT product_key, {COLUMN_WILL_RENDER} FROM catalog_products"))
    assert rows["pk_dead"] is True
    others = {k: v for k, v in rows.items() if k != "pk_dead"}
    assert all(v is None for v in others.values()), f"scope leaked: {others}"


@pytest.mark.asyncio
async def test_persist_writes_the_computed_value_not_a_constant(seeded):
    from services.pdp_renderability_store import COLUMN_WILL_RENDER, _persist

    async with prod_db() as db:
        await _persist(
            [{"product_key": "pk_live", "will_render": True},
             {"product_key": "pk_dead", "will_render": False}],
            database=db,
        )
    rows = dict(_read(
        seeded,
        f"SELECT product_key, {COLUMN_WILL_RENDER} FROM catalog_products "
        "WHERE product_key IN ('pk_live','pk_dead')",
    ))
    assert rows == {"pk_live": True, "pk_dead": False}


@pytest.mark.asyncio
async def test_the_boolean_cast_is_required_by_the_real_driver(seeded):
    """The CAST in `_persist`'s VALUES list is falsifiable ONLY on asyncpg.

    psycopg2 interpolates client-side and never surfaces it; asyncpg does a real
    PREPARE and refuses with `column "pdp_will_render" is of type boolean but
    expression is of type text`. Asserting it against the production driver is
    what makes the CAST a guard rather than a comment.
    """
    import asyncpg

    sql = ("UPDATE catalog_products AS tgt SET pdp_will_render = src.wr "
           "FROM (VALUES (:pk_0, :wr_0)) AS src(pk, wr) WHERE tgt.product_key = src.pk")
    async with prod_db() as db:
        with pytest.raises(asyncpg.exceptions.DatatypeMismatchError):
            await db.execute(sql, {"pk_0": "pk_live", "wr_0": True})


@pytest.mark.asyncio
async def test_refresh_for_content_key_honours_its_scope(seeded):
    from services.pdp_renderability_store import (
        COLUMN_WILL_RENDER,
        refresh_for_content_key,
    )

    _reset(seeded, f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = NULL")

    async with prod_db() as db:
        n = await refresh_for_content_key("ck_blocked", database=db)

    assert n == 1, f"expected exactly the one ck_blocked row, wrote {n}"
    rows = dict(_read(seeded, f"SELECT product_key, {COLUMN_WILL_RENDER} FROM catalog_products"))
    assert rows["pk_blocked"] is False
    assert rows["pk_live"] is None, "rows outside the content_key must be untouched"


@pytest.mark.asyncio
async def test_persist_spans_chunks_correctly_on_the_real_driver(seeded):
    """Chunk COUNT is asserted engine-agnostically (see the sibling unit test —
    the property is statement count, and a row-count assertion cannot see it).

    What only the real driver can settle is that a multi-chunk write actually
    lands: bind types, the VALUES CAST and the RETURNING count all behave across
    a chunk boundary rather than just inside one statement.
    """
    from sqlalchemy import text

    from services.pdp_renderability_store import (
        _PERSIST_CHUNK_ROWS,
        COLUMN_WILL_RENDER,
        _persist,
    )

    n_rows = _PERSIST_CHUNK_ROWS + 5
    with seeded.begin() as conn:
        conn.execute(text(f"UPDATE catalog_products SET {COLUMN_WILL_RENDER} = NULL"))
        for i in range(n_rows):
            conn.execute(
                text("INSERT INTO catalog_products (product_key, merchant_id, platform, "
                     "source_product_id, title, catalog_track) VALUES (:pk, 'external_seed', "
                     "'external_seed', :pk, :pk, 'external_referral')"),
                {"pk": f"bulk_{i}"},
            )

    payload = [{"product_key": f"bulk_{i}", "will_render": True} for i in range(n_rows)]
    async with prod_db() as db:
        written = await _persist(payload, database=db)
    assert written == n_rows, "RETURNING count must survive a chunk boundary"

    got = _read(seeded, f"SELECT count(*) FROM catalog_products WHERE product_key LIKE 'bulk_%' "
                        f"AND {COLUMN_WILL_RENDER} IS TRUE")[0][0]
    assert got == n_rows, "chunk boundary dropped rows"

    _reset(seeded, "DELETE FROM catalog_products WHERE product_key LIKE 'bulk_%'")
