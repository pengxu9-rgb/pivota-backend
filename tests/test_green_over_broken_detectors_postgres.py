"""The two taxonomy-reachability invariants, EXECUTED against Postgres.

WHY THIS FILE EXISTS. Review of #2161 found surviving mutants — notably `IN` -> `NOT IN` — because
every test of this check asserted on the SQL as a STRING. A string-presence test cannot tell a
working predicate from an inverted one, and a check that silently counts 0 everywhere looks exactly
like a healthy catalog. That is the failure mode this check exists to detect, reproduced in its own
test. So each predicate is proved BOTH ways: a row that must be counted, and a row that must not.

The second review round killed a further mutant this file had missed: dropping `lower()` passed
both unit and Postgres, because no fixture used a mixed-case path. There is one now.

Postgres gate because the predicates use production-dialect SQL (`btrim(x, '/')`,
`string_to_array`) that SQLite will not execute.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only — never hand-roll DDL
for a table `db.catalog` owns. And this file DELETEs FROM catalog_products, so it refuses to run
against a database whose name does not look like a throwaway (`_assert_throwaway_database`); 13
sibling gate files carry that guard and the first version of this one did not.
"""

from __future__ import annotations

import asyncio
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to DELETE FROM catalog_products in {dbname!r}")


# index_pipeline_state is owned by a migration, not by db.catalog metadata. Minimal shape,
# additive only.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key text PRIMARY KEY);
ALTER TABLE index_pipeline_state ADD COLUMN IF NOT EXISTS serving_eligible boolean;
"""

INTERIOR = "serving_eligible_on_interior_taxonomy_node"
OFF_TAXONOMY = "serving_eligible_off_taxonomy_path"


def _check(name):
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == name:
            return c
    raise AssertionError("%s is not registered" % name)


@pytest.fixture(scope="module")
def pg_engine():
    _assert_throwaway_database()
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

    for t in ("catalog_products", "index_pipeline_state"):
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


def _matched(conn, predicate):
    """Rows the predicate selects. The predicate is the module's own constant, executed — not a
    string the test re-types, which is how the inverted-`IN` mutant survived the first round."""
    from sqlalchemy import text

    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM catalog_products cp"
                " JOIN index_pipeline_state ips ON ips.content_key = cp.content_key"
                " WHERE ips.serving_eligible AND (%s)" % predicate
            )
        ).scalar()
        or 0
    )


def _interior_sql():
    from services.catalog_invariant_checks import _INTERIOR_HANDICAP_SQL

    return _INTERIOR_HANDICAP_SQL


def _off_taxonomy_sql():
    from services.catalog_invariant_checks import _OFF_TAXONOMY_SQL

    return _OFF_TAXONOMY_SQL


# --- predicate 1: interior taxonomy node ------------------------------------------------------


def test_interior_node_predicate_counts_an_interior_row(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_interior", ck="ck_i", category_path="beauty/makeup")
        assert _matched(conn, _interior_sql()) == 1


def test_interior_node_predicate_does_not_count_a_leaf_row(pg_engine):
    """The control that makes the test above able to fail. An `IN` -> `NOT IN` mutant passes the
    first test and dies here."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_leaf", ck="ck_l", category_path="beauty/makeup/lip/lipstick")
        assert _matched(conn, _interior_sql()) == 0


def test_a_MIXED_CASE_interior_path_is_still_counted(pg_engine):
    """Kills the `lower()` mutant, which survived unit AND Postgres in the first round because
    every fixture path was already lowercase. A writer that stores 'Beauty/Makeup' produces a row
    with exactly the same handicap and must not vanish from the count."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_case", ck="ck_c", category_path="Beauty/Makeup")
        assert _matched(conn, _interior_sql()) == 1


def test_a_two_segment_LEAF_is_not_counted(pg_engine):
    """Routability is not a depth: `fashion/shoes` is a 2-segment leaf that prefix recall reaches."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_shoes", ck="ck_s", category_path="fashion/shoes")
        assert _matched(conn, _interior_sql()) == 0


def test_null_and_blank_paths_are_counted(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_null", ck="ck_n", category_path=None)
        _product(conn, pk="p_blank", ck="ck_b", category_path="   ")
        assert _matched(conn, _interior_sql()) == 2


def test_a_trailing_slash_does_not_hide_a_TRULY_interior_row(pg_engine):
    """`beauty/makeup/` is 2 segments: no prefix `beauty/makeup/<leaf-parent>/` can match it, so it
    is handicapped exactly as `beauty/makeup` is."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_slash", ck="ck_sl", category_path="beauty/makeup/")
        assert _matched(conn, _interior_sql()) == 1


def test_a_DEPTH_3_path_with_a_trailing_slash_is_NOT_counted(pg_engine):
    """The partner of the test above, and the review finding that produced it.

    Recall's prefix is exactly the leaf's parent plus a slash — 'beauty/makeup/lip/'. A row stored
    as 'beauty/makeup/lip/' satisfies `LIKE 'beauty/makeup/lip/%'` (the % matches the empty string),
    earns the +90 depth score, and is NOT handicapped. btrim()ing the slash away made the first
    version count it as the interior node 'beauty/makeup/lip'. Zero prod rows are in this state
    today; the predicate should still say what it means."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_d3", ck="ck_d3", category_path="beauty/makeup/lip/")
        assert _matched(conn, _interior_sql()) == 0


def test_a_row_that_is_not_serving_eligible_is_not_counted(pg_engine):
    """The check is about the SERVED surface. A dark row on an interior node is not this defect."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_dark", ck="ck_d", category_path="beauty/makeup", serving=False)
        assert _matched(conn, _interior_sql()) == 0


# --- predicate 2: off-taxonomy path -----------------------------------------------------------


def test_off_taxonomy_counts_a_sibling_typo(pg_engine):
    """The real prod cohort: 315 rows at `beauty/skincare/tone/toner` against the taxonomy's
    `beauty/skincare/treat/toner`. One wrong segment, and no category recall at all."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_typo", ck="ck_t", category_path="beauty/skincare/tone/toner")
        assert _matched(conn, _off_taxonomy_sql()) == 1


def test_off_taxonomy_does_not_count_a_real_leaf(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_real", ck="ck_r", category_path="beauty/skincare/treat/toner")
        assert _matched(conn, _off_taxonomy_sql()) == 0


def test_off_taxonomy_does_not_count_an_INTERIOR_row(pg_engine):
    """The two cohorts must be disjoint, or the same row is reported twice under two names and the
    shares no longer add up to anything. An interior node is an ancestor of a real leaf; that is a
    different, milder defect with a different fix (#2122 can rescue it)."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_int", ck="ck_int", category_path="beauty/makeup")
        assert _matched(conn, _off_taxonomy_sql()) == 0
        assert _matched(conn, _interior_sql()) == 1


def test_off_taxonomy_does_not_count_a_NULL_path(pg_engine):
    """A NULL path belongs to the interior/NULL cohort, not this one — again, disjointness."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_nul", ck="ck_nul", category_path=None)
        assert _matched(conn, _off_taxonomy_sql()) == 0


def test_off_taxonomy_is_case_insensitive_too(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="p_tc", ck="ck_tc", category_path="Beauty/Skincare/Tone/Toner")
        assert _matched(conn, _off_taxonomy_sql()) == 1


# --- the runner: the share arithmetic, executed ------------------------------------------------


def _run(name):
    """Run the registered runner against the real database, through `databases` — the same client
    the sweep uses. Not a stubbed `db` object: a fake that returns numbers would prove the
    arithmetic and nothing about the SQL."""
    from databases import Database

    async def go():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            return await _check(name)["runner"](db)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def test_the_runner_reports_a_SHARE_not_a_row_count(pg_engine):
    """Three interior rows out of four serving-eligible is 750 tenths, whatever the row count.

    This is the whole point of the reshape: the first version enforced at the measured 4,588 rows,
    which one promotion moves to 4,589. A share does not move when the catalogue merely grows."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="a", ck="cka", category_path="beauty/makeup")
        _product(conn, pk="b", ck="ckb", category_path="beauty/skincare")
        _product(conn, pk="c", ck="ckc", category_path=None)
        _product(conn, pk="d", ck="ckd", category_path="beauty/makeup/lip/lipstick")
    out = _run(INTERIOR)
    assert out["count"] == 750, out
    assert out["detail"]["matched_rows"] == 3
    assert out["detail"]["serving_eligible_rows"] == 4
    assert out["detail"]["share_pct"] == 75.0
    assert out["sample_keys"], "a breaching check with no samples names nothing to look at"


def test_the_share_is_STABLE_when_the_catalogue_merely_grows(pg_engine):
    """The control for the test above, and the reason a count was the wrong shape. Doubling the
    catalogue at a constant mix must not move the number; under the old count-based check it
    doubled it and tripped."""
    with pg_engine.begin() as conn:
        _reset(conn)
        for i in range(2):
            _product(conn, pk="i%d" % i, ck="cki%d" % i, category_path="beauty/makeup")
            _product(conn, pk="l%d" % i, ck="ckl%d" % i, category_path="beauty/makeup/lip/lipstick")
    small = _run(INTERIOR)
    with pg_engine.begin() as conn:
        for i in range(2, 8):
            _product(conn, pk="i%d" % i, ck="cki%d" % i, category_path="beauty/makeup")
            _product(conn, pk="l%d" % i, ck="ckl%d" % i, category_path="beauty/makeup/lip/lipstick")
    big = _run(INTERIOR)
    assert small["count"] == big["count"] == 500
    assert big["detail"]["matched_rows"] == 4 * small["detail"]["matched_rows"]


def test_an_EMPTY_serving_set_is_reported_as_empty_not_as_zero_percent(pg_engine):
    """0 of 0 is not a clean bill of health — nothing is being served. Same defect shape as the
    relgraph expiry alarm reading 0% of 0 expiring rows as healthy."""
    with pg_engine.begin() as conn:
        _reset(conn)
    out = _run(INTERIOR)
    assert out["count"] == 0
    assert out["detail"]["serving_set_empty"] is True
    assert out["detail"]["share_pct"] is None


def test_the_off_taxonomy_runner_executes_and_is_disjoint(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="t1", ck="ckt1", category_path="beauty/skincare/tone/toner")
        _product(conn, pk="t2", ck="ckt2", category_path="beauty/makeup")
        _product(conn, pk="t3", ck="ckt3", category_path="beauty/makeup/lip/lipstick")
        _product(conn, pk="t4", ck="ckt4", category_path="beauty/skincare/treat/toner")
    off = _run(OFF_TAXONOMY)
    interior = _run(INTERIOR)
    assert off["detail"]["matched_rows"] == 1
    assert interior["detail"]["matched_rows"] == 1
    assert off["count"] == 250 and interior["count"] == 250


def test_both_checks_are_registered_as_ENFORCING(pg_engine):
    """`warn_only` at threshold 0 would print the real number every run and alarm on nothing — a
    metric wearing a detector's name, which is the category error this work exists to remove."""
    for name in (INTERIOR, OFF_TAXONOMY):
        c = _check(name)
        assert not c.get("warn_only"), "%s is warn_only" % name
        assert c["count_sql"] is None and c["sample_sql"] is None, (
            "%s must not restate the share in SQL" % name
        )
        assert c["default_threshold"] > 0, name
