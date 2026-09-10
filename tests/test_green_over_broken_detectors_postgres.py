"""The two green-over-broken invariants, EXECUTED against Postgres.

WHY THIS FILE EXISTS. Review of #2161 found three surviving mutants — `NOT EXISTS` -> `EXISTS`,
`IN` -> `NOT IN`, and `AND 1=0` — because every test of these two checks asserted on the SQL as a
STRING. A string-presence test cannot tell a working predicate from an inverted one, and a check
that silently counts 0 everywhere looks exactly like a healthy catalog. That is the failure mode
this whole branch exists to detect, reproduced in its own tests.

So each check is proved to count BOTH ways: a row that must be counted, and a row that must not.

Postgres gate because both predicates use production-dialect SQL (`btrim(x, '/')`, `interval`,
correlated EXISTS) that SQLite will not execute.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only — never hand-roll DDL
for a table `db.catalog` owns.
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

# index_pipeline_state is owned by a migration, not by db.catalog metadata; the relationship graph
# tables live in PIVOTA-Agent entirely. Minimal shapes, additive only.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
CREATE TABLE IF NOT EXISTS relationship_graph_routine_runs (run_id text PRIMARY KEY);
ALTER TABLE relationship_graph_routine_runs ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE relationship_graph_routine_runs ADD COLUMN IF NOT EXISTS applied_count integer;
ALTER TABLE relationship_graph_routine_runs ADD COLUMN IF NOT EXISTS completed_at timestamptz;
"""

INTERIOR = "serving_eligible_on_interior_taxonomy_node"
NOOP = "relationship_graph_runs_pass_without_applying"


def _check(name):
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == name:
            return c
    raise AssertionError("%s is not registered" % name)


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
    yield engine
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    for t in ("catalog_products", "index_pipeline_state", "relationship_graph_routine_runs"):
        conn.execute(text("DELETE FROM %s" % t))


def _product(conn, *, pk, ck, category_path, serving=True):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, title, category_path, content_key)"
            " VALUES (:pk, 'm', 'external_seed', :pk, 't', :cp, :ck)"
        ),
        {"pk": pk, "cp": category_path, "ck": ck},
    )
    conn.execute(
        text(
            "INSERT INTO index_pipeline_state (content_key, serving_eligible)"
            " VALUES (:ck, :se) ON CONFLICT (content_key) DO UPDATE"
            " SET serving_eligible = EXCLUDED.serving_eligible"
        ),
        {"ck": ck, "se": serving},
    )


def _count(conn, name):
    from sqlalchemy import text

    return int(conn.execute(text(_check(name)["count_sql"])).scalar() or 0)


# --- detector 1: interior taxonomy node -------------------------------------------------------


def test_interior_node_check_counts_an_interior_row(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_interior", ck="ck_i", category_path="beauty/makeup")
        assert _count(conn, INTERIOR) == 1


def test_interior_node_check_does_not_count_a_leaf_row(pg_engine):
    """The control that makes the test above able to fail. An `IN` -> `NOT IN` mutant passes the
    first test and dies here."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_leaf", ck="ck_l", category_path="beauty/makeup/lip/lipstick")
        assert _count(conn, INTERIOR) == 0


def test_a_two_segment_LEAF_is_not_counted(pg_engine):
    """Routability is not a depth: `fashion/shoes` is a 2-segment leaf that prefix recall reaches."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_shoes", ck="ck_s", category_path="fashion/shoes")
        assert _count(conn, INTERIOR) == 0


def test_null_and_blank_paths_are_counted(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_null", ck="ck_n", category_path=None)
        _product(conn, pk="p_blank", ck="ck_b", category_path="   ")
        assert _count(conn, INTERIOR) == 2


def test_a_trailing_slash_does_not_hide_an_interior_row(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_slash", ck="ck_sl", category_path="beauty/makeup/")
        assert _count(conn, INTERIOR) == 1


def test_a_row_that_is_not_serving_eligible_is_not_counted(pg_engine):
    """The check is about the SERVED surface. A dark row on an interior node is not this defect."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_dark", ck="ck_d", category_path="beauty/makeup", serving=False)
        assert _count(conn, INTERIOR) == 0


# --- detector 2: runs that pass without applying ----------------------------------------------


def _run(conn, *, run_id, status, applied, age_hours):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO relationship_graph_routine_runs (run_id, status, applied_count,"
            " completed_at) VALUES (:r, :s, :a, now() - make_interval(hours => :h))"
        ),
        {"r": run_id, "s": status, "a": applied, "h": age_hours},
    )


def test_noop_check_fires_when_a_recent_pass_applied_nothing(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _run(conn, run_id="r1", status="passed", applied=0, age_hours=2)
        _run(conn, run_id="r2", status="passed", applied=0, age_hours=200)
        assert _count(conn, NOOP) == 1


def test_noop_check_is_silent_when_a_run_applied_something(pg_engine):
    """The control. A `NOT EXISTS` -> `EXISTS` mutant passes the test above and dies here."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _run(conn, run_id="r1", status="passed", applied=0, age_hours=2)
        _run(conn, run_id="r2", status="passed", applied=17, age_hours=100)
        assert _count(conn, NOOP) == 0


def test_noop_check_is_silent_with_no_recent_passing_run(pg_engine):
    """Nothing has run lately, so there is no green ledger to contradict. An `AND 1=0` mutant on
    the first EXISTS passes this and dies on the firing test."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _run(conn, run_id="r1", status="passed", applied=0, age_hours=200)
        assert _count(conn, NOOP) == 0


def test_noop_check_ignores_failed_runs(pg_engine):
    """A failing run is already visible as a failure; this check is about a PASSING one."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _run(conn, run_id="r1", status="failed", applied=0, age_hours=2)
        assert _count(conn, NOOP) == 0


def test_a_null_applied_count_reads_as_zero(pg_engine):
    """coalesce, not a silent NULL comparison: `applied_count IS NULL > 0` is NULL, not false, and
    would make the NOT EXISTS behave in a way nobody intended."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _run(conn, run_id="r1", status="passed", applied=None, age_hours=2)
        assert _count(conn, NOOP) == 1


# --- both sample queries must be executable ---------------------------------------------------


def test_both_sample_queries_execute(pg_engine):
    """A check over threshold fetches samples. A sample_sql that raises would erase the finding
    from the report — the runner catches it as {"error": ...} and the count is lost."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_i", ck="ck_i", category_path="beauty/makeup")
        _run(conn, run_id="r1", status="passed", applied=0, age_hours=2)
        for name in (INTERIOR, NOOP):
            rows = conn.execute(text(_check(name)["sample_sql"])).fetchall()
            assert rows, "%s produced no sample rows" % name
