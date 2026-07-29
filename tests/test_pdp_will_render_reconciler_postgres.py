"""The pdp_will_render reconciler must actually converge the column.

#1604 shipped `catalog_products.pdp_will_render` and both refresh entry points
with NO CALLER, so the column is 100% NULL in prod and the serving-vs-render
invariant (779 rows `serving_eligible` that the gateway will not render) could
not be built on it. This job is the prerequisite.

🚨 THE TEST THAT MATTERS IS `test_drift_counts_a_null_column`. The column starts
NULL on every row, and `NULL != true` is NULL — which a WHERE clause discards. So
a `!=` drift predicate reports ZERO drift against a completely empty column: a
green light that means nothing has happened. That is the precise "no-op behind a
success signal" shape this codebase keeps producing, and it is the one way this
job could ship looking healthy while doing nothing.

Postgres gate because `pdp_will_render_expression` is correlated-EXISTS SQL that
SQLite cannot execute, and because `IS DISTINCT FROM` is the dialect behaviour
under test.

🚨 THESE GATE FILES SHARE ONE DATABASE. Use `metadata.create_all` and DELETE —
never hand-roll DDL for a table `db.catalog` owns.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

# pdp_will_render / pdp_will_render_computed_at come from a migration, not the
# MetaData Table def, so they are added explicitly the same way
# tests/test_pdp_renderability_store_postgres.py does.
_MIGRATION_COLUMNS = """
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render boolean;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS pdp_will_render_computed_at timestamptz;
"""

_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (
  external_product_id text, attached_product_key text, status text,
  merchant_id text, source text, product_key text, source_product_id text,
  seed_data jsonb, updated_at timestamptz, id text, title text
);
CREATE TABLE IF NOT EXISTS index_pipeline_state (
  content_key text, serving_eligible boolean, index_eligible boolean,
  blocker_code text, blocker_detail text,
  content_quality_score double precision, quality_scored_at timestamp
);
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
        for statement in filter(None, (s.strip() for s in _MIGRATION_COLUMNS.split(";"))):
            conn.execute(text(statement))
    yield engine
    engine.dispose()


def _seed(conn, *, pk, ck, serving_eligible=True, will_render=None):
    """One external-seed row with an ACTIVE seed on its route key, so the live
    expression evaluates True when serving_eligible is True."""
    from sqlalchemy import text

    conn.execute(text("DELETE FROM catalog_products"))
    conn.execute(text("DELETE FROM index_pipeline_state"))
    conn.execute(text("DELETE FROM external_product_seeds"))
    conn.execute(
        text(
            "INSERT INTO index_pipeline_state (content_key, serving_eligible) "
            "VALUES (:ck, :se)"
        ),
        {"ck": ck, "se": serving_eligible},
    )
    conn.execute(
        text(
            "INSERT INTO external_product_seeds (external_product_id, status) "
            "VALUES (:spid, 'active')"
        ),
        {"spid": pk},
    )
    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, pdp_will_render) "
            "VALUES (:pk, 'external_seed', 'external_seed', :pk, :pk, :ck, "
            "        'external_referral', :wr)"
        ),
        {"pk": pk, "ck": ck, "wr": will_render},
    )


def test_drift_counts_a_null_column(pg_engine):
    """THE load-bearing case.

    `NULL != true` is NULL, so a `!=` predicate discards the row and reports zero
    drift against a 100%-empty column. `IS DISTINCT FROM` must count it.
    """
    from jobs.pdp_renderability_reconciler_cron import drift_select

    with pg_engine.begin() as conn:
        _seed(conn, pk="pk_null", ck="ck_null", serving_eligible=True, will_render=None)
        drift = conn.execute(drift_select()).scalar()

    assert drift == 1, (
        "a NULL stored value must count as drift — if this is 0 the predicate is "
        "using != and the reconciler will report a converged column while never "
        "having written a single row"
    )


def test_no_drift_once_the_stored_value_matches(pg_engine):
    from jobs.pdp_renderability_reconciler_cron import drift_select

    with pg_engine.begin() as conn:
        _seed(conn, pk="pk_ok", ck="ck_ok", serving_eligible=True, will_render=True)
        drift = conn.execute(drift_select()).scalar()

    assert drift == 0, "a correct stored value must not be reported as drift"


def test_drift_catches_a_stale_disagreement(pg_engine):
    """The signal the whole design rests on: a writer changed the row and nothing
    recomputed the column."""
    from jobs.pdp_renderability_reconciler_cron import drift_select

    with pg_engine.begin() as conn:
        # serving_eligible FALSE => live expression is False, stored says True.
        _seed(conn, pk="pk_stale", ck="ck_stale", serving_eligible=False, will_render=True)
        drift = conn.execute(drift_select()).scalar()

    assert drift == 1


@pytest.mark.asyncio
async def test_reconcile_writes_and_converges(pg_engine):
    """End to end against the real driver.

    `_persist` passes plain SQL rather than `sa.text(...)` because asyncpg's
    `databases` wrapper rejects the latter with a params dict — a bug an earlier
    cut shipped past 32 green tests because the shim used psycopg2. So this
    drives `databases.Database`, not the sync engine.
    """
    from databases import Database

    from jobs.pdp_renderability_reconciler_cron import (
        count_pdp_will_render_drift,
        reconcile_pdp_will_render,
    )

    with pg_engine.begin() as conn:
        _seed(conn, pk="pk_conv", ck="ck_conv", serving_eligible=True, will_render=None)

    db = Database(DATABASE_URL, min_size=1, max_size=2)
    await db.connect()
    try:
        before = await count_pdp_will_render_drift(db)
        assert before["total"] == 1
        assert before["never_computed"] == 1

        result = await reconcile_pdp_will_render(db=db, limit=100)
        assert result["candidates"] == 1
        assert result["written"] == 1, "the UPDATE must actually land, not just be offered"

        after = await count_pdp_will_render_drift(db)
        assert after["total"] == 0, "one pass must converge a single-row table"
        assert after["never_computed"] == 0
        assert after["never_stamped"] == 0, "computed_at must be stamped with the value"
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_when_already_converged(pg_engine):
    """A reconciler that rewrites every row every pass is indistinguishable from
    one that is broken, and would mask the drift signal under constant churn."""
    from databases import Database

    from jobs.pdp_renderability_reconciler_cron import reconcile_pdp_will_render

    with pg_engine.begin() as conn:
        _seed(conn, pk="pk_idem", ck="ck_idem", serving_eligible=True, will_render=True)

    db = Database(DATABASE_URL, min_size=1, max_size=2)
    await db.connect()
    try:
        result = await reconcile_pdp_will_render(db=db, limit=100)
        assert result == {"candidates": 0, "written": 0}
    finally:
        await db.disconnect()
