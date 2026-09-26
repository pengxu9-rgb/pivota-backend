"""The two taxonomy-reachability invariants, EXECUTED against Postgres.

WHY THIS FILE EXISTS. Review of #2161 found surviving mutants — notably `IN` -> `NOT IN` — because
every test of this check asserted on the SQL as a STRING. A string-presence test cannot tell a
working predicate from an inverted one, and a check that silently counts 0 everywhere looks exactly
like a healthy catalog. That is the failure mode this check exists to detect, reproduced in its own
test. So each predicate is proved BOTH ways: a row that must be counted, and a row that must not.

Round 2 went further and inverted round 1's fix. `lower()`/`btrim` were removed ENTIRELY, because
recall does neither: its prefix test is a case-sensitive `LIKE` and #2122's is a case-sensitive
`SUBSTR` comparison. A mixed-case path therefore has NO door, and the round-1 fixture that asserted
it was merely "degraded" pinned the wrong cohort. Four mutants that survived round 2 are pinned
here: `round`->`int` in the share, the reachability test made vacuous, the denominator losing
`serving_eligible`, and a blank path against the off-taxonomy predicate.

Postgres gate because the predicates use production-dialect SQL (`btrim(x, '/')`,
`string_to_array`) that SQLite will not execute.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only — never hand-roll DDL
for a table `db.catalog` owns. And this file DELETEs FROM catalog_products, so it refuses to run
against a database whose name does not look like a throwaway (`_assert_throwaway_database`); 13
sibling gate files carry that guard and the first version of this one did not. `_reset` also
clears index_pipeline_state wholesale, not just catalog_products.
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

ANCESTOR_ONLY = "serving_eligible_ancestor_only_taxonomy_node"
OFF_TAXONOMY = "serving_eligible_off_taxonomy_path"
NO_PATH = "serving_eligible_with_no_category_path"


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


def _sql(name):
    import services.catalog_invariant_checks as m

    return {
        "reachable": m._PREFIX_REACHABLE_SQL,
        "ancestor_only": m._ANCESTOR_ONLY_SQL,
        "off_taxonomy": m._OFF_TAXONOMY_SQL,
        "no_path": m._NO_PATH_SQL,
    }[name]


def _matched(conn, name):
    """Rows the module's OWN predicate selects, executed — not a string the test re-types, which is
    how the inverted-`IN` mutant survived round 1."""
    from sqlalchemy import text

    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM catalog_products cp"
                " JOIN index_pipeline_state ips ON ips.content_key = cp.content_key"
                " WHERE ips.serving_eligible AND (%s)" % _sql(name)
            )
        ).scalar()
        or 0
    )


def _bucket(conn, path, serving=True):
    """Which of the four cohorts a single path falls into. Exactly one, always."""
    _reset(conn)
    _product(conn, pk="probe", ck="ck_probe", category_path=path, serving=serving)
    hits = [n for n in ("reachable", "ancestor_only", "off_taxonomy", "no_path")
            if _matched(conn, n)]
    return hits


# --- the partition itself ---------------------------------------------------------------------


def test_every_path_lands_in_EXACTLY_ONE_cohort(pg_engine):
    """THE LOAD-BEARING PROPERTY. Three enforcing shares over overlapping or incomplete cohorts are
    three numbers that mean nothing. Round 1 shipped predicates where a mixed-case leaf fell into
    NO cohort and was reported healthy."""
    paths = [
        "beauty/makeup/lip/lipstick",   # leaf
        "beauty/makeup/lip/lip_oil",    # deeper than a leaf, still prefix-matched
        "beauty/makeup/lip",            # leaf-parent
        "beauty/makeup",                # pure ancestor
        "beauty",                       # root ancestor
        "beauty/skincare/tone/toner",   # a real leaf since 2026-09-10 (industry standard)
        "beauty/skincare/treat/toner",  # the sibling typo, now that tone/ is canonical
        "Beauty/Makeup",                # mixed case
        "Beauty/Makeup/Lip/Lipstick",   # mixed-case leaf
        "/beauty/makeup/lip/lipstick",  # leading slash
        "beauty/makeup/",               # trailing slash, depth 2
        "beauty/makeup/lip/",           # trailing slash, depth 3
        "beauty//makeup",               # doubled slash
        "fashion/apparel",              # ancestor that a depth-1 prefix reaches
        "wellness/supplements",         # wholly foreign
        "   ",                          # blank
        None,                           # null
    ]
    with pg_engine.begin() as conn:
        for path in paths:
            hits = _bucket(conn, path)
            assert len(hits) == 1, "%r landed in %r, not exactly one cohort" % (path, hits)


def test_the_cohorts_match_what_RECALL_does_not_what_looks_tidy(pg_engine):
    """Round 2's finding, pinned as a table. Each expectation is what the row's actual door is —
    `LIKE :prefix` (pivot_query_service.py:1144) and #2122's SUBSTR ancestor test at :1205-1208,
    both case-sensitive, neither trimming."""
    expected = {
        # reached directly: earns the +90
        "beauty/makeup/lip/lipstick": "reachable",
        "beauty/makeup/lip/lip_oil": "reachable",   # deeper than a leaf; round 1 called it off-taxonomy
        "fashion/apparel": "reachable",             # 'fashion/' IS a query prefix
        # ancestor only: #2122 can admit it, it never earns the depth score
        "beauty/makeup/lip": "ancestor_only",
        "beauty/makeup": "ancestor_only",
        "beauty": "ancestor_only",
        # NO door at all
        # ⚠️ FLIPPED 2026-09-10. `tone/toner` is the CANONICAL leaf — Google Product Taxonomy
        # 5976 and Shopify hb-3-2-9-17 both make Toners & Astringents a direct child of Skin
        # Care, and it is what PIVOTA-Agent has written since 2026-08-04. This file previously
        # asserted it was off-taxonomy, which is how 315 deliberate rows got called corrupt.
        "beauty/skincare/tone/toner": "reachable",
        # ...and `treat/toner` is STILL reachable, by the wrong door. `treat` is the parent of
        # exfoliant/mask/serum/treatment, so the path matches `LIKE 'beauty/skincare/treat/%'` and
        # scores +90 on a SERUM query — while a TONER query now builds `beauty/skincare/tone/` and
        # misses it entirely. ⚠️ THIS COHORT IS INVISIBLE TO ALL FOUR INVARIANTS: "reachable by a
        # prefix that means something else" is a fifth state none of them names, and the ~316 rows
        # on this path are in it until PIVOTA-Agent's reconciler converges them. Recorded here
        # rather than papered over, because a partition that looks exhaustive is not the same as
        # one that is complete.
        "beauty/skincare/treat/toner": "reachable",
        "beauty/makeup/lips/lip-gloss": "off_taxonomy",
        "wellness/supplements": "off_taxonomy",
        # ...and these are why lower()/btrim had to go. A case-sensitive LIKE does not match
        # 'Beauty/...', and the SUBSTR ancestor test does not either, so there is no door.
        "Beauty/Makeup": "off_taxonomy",
        "Beauty/Makeup/Lip/Lipstick": "off_taxonomy",
        "/beauty/makeup/lip/lipstick": "off_taxonomy",
        "beauty/makeup/": "off_taxonomy",
        # a depth-3 path WITH the slash does satisfy 'beauty/makeup/lip/%' — the % matches empty
        "beauty/makeup/lip/": "reachable",
        # no path at all: a different door again (brand-gated missing_taxonomy escape)
        "   ": "no_path",
        None: "no_path",
    }
    with pg_engine.begin() as conn:
        for path, want in expected.items():
            assert _bucket(conn, path) == [want], "%r: expected %s" % (path, want)


def test_a_row_that_is_not_serving_eligible_is_in_no_cohort(pg_engine):
    """These checks are about the SERVED surface. A dark row on an ancestor node is not this
    defect, and counting it would make the shares describe a population nobody queries."""
    with pg_engine.begin() as conn:
        assert _bucket(conn, "beauty/makeup", serving=False) == []


# --- the runner: the share arithmetic, executed ------------------------------------------------


def _run(name):
    """Run the registered runner against the real database, through `databases` — the same client
    the sweep uses. Not a stubbed `db`: a fake returning numbers would prove the arithmetic and
    nothing about the SQL."""
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
    """Three ancestor-only rows out of four serving-eligible is 750 tenths, whatever the row count.

    This is the point of the reshape: round 1 enforced at the measured 4,588 rows, which one
    promotion moves to 4,589."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="a", ck="cka", category_path="beauty/makeup")
        _product(conn, pk="b", ck="ckb", category_path="beauty/skincare")
        _product(conn, pk="c", ck="ckc", category_path="beauty")
        _product(conn, pk="d", ck="ckd", category_path="beauty/makeup/lip/lipstick")
    out = _run(ANCESTOR_ONLY)
    assert out["count"] == 750, out
    assert out["detail"]["matched_rows"] == 3
    assert out["detail"]["serving_eligible_rows"] == 4
    assert out["detail"]["share_pct"] == 75.0
    assert out["sample_keys"], "a breaching check with no samples names nothing to look at"


def test_the_share_ROUNDS_rather_than_truncating(pg_engine):
    """Kills `round` -> `int`, which survived round 2 because every fixture used a ratio exact in
    tenths. 2 of 3 is 66.67% = 667 tenths; truncation gives 666, and a ratchet that is silently one
    tenth low is a ratchet with a hidden extra tenth of headroom, every run, forever."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="a", ck="cka", category_path="beauty/makeup")
        _product(conn, pk="b", ck="ckb", category_path="beauty/skincare")
        _product(conn, pk="c", ck="ckc", category_path="beauty/makeup/lip/lipstick")
    assert _run(ANCESTOR_ONLY)["count"] == 667


def test_the_denominator_counts_only_the_SERVED_set(pg_engine):
    """Kills the mutant that drops `WHERE ips.serving_eligible` from the denominator, which survived
    round 2 entirely. Dark rows would inflate the denominator, understate every share, and the
    ratchet would never trip — green over broken, in the detector built to catch green over broken.

    One ancestor row, one leaf row, and EIGHT dark rows: the share is 500, not 100."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="a", ck="cka", category_path="beauty/makeup")
        _product(conn, pk="b", ck="ckb", category_path="beauty/makeup/lip/lipstick")
        for i in range(8):
            _product(conn, pk="d%d" % i, ck="ckd%d" % i,
                     category_path="beauty/makeup", serving=False)
    out = _run(ANCESTOR_ONLY)
    assert out["detail"]["serving_eligible_rows"] == 2, out["detail"]
    assert out["count"] == 500


def test_the_share_is_STABLE_when_the_catalogue_merely_grows(pg_engine):
    """Why a count was the wrong shape: doubling the catalogue at a constant mix must not move the
    number. Under the old count-based check it doubled it and tripped."""
    with pg_engine.begin() as conn:
        _reset(conn)
        for i in range(2):
            _product(conn, pk="i%d" % i, ck="cki%d" % i, category_path="beauty/makeup")
            _product(conn, pk="l%d" % i, ck="ckl%d" % i, category_path="beauty/makeup/lip/lipstick")
    small = _run(ANCESTOR_ONLY)
    with pg_engine.begin() as conn:
        for i in range(2, 8):
            _product(conn, pk="i%d" % i, ck="cki%d" % i, category_path="beauty/makeup")
            _product(conn, pk="l%d" % i, ck="ckl%d" % i, category_path="beauty/makeup/lip/lipstick")
    big = _run(ANCESTOR_ONLY)
    assert small["count"] == big["count"] == 500
    assert big["detail"]["matched_rows"] == 4 * small["detail"]["matched_rows"]


def test_an_EMPTY_serving_set_is_reported_as_empty_not_as_zero_percent(pg_engine):
    """0 of 0 is not a clean bill of health — nothing is being served. Same shape as an expiry alarm
    reading 0% of 0 expiring rows as healthy."""
    with pg_engine.begin() as conn:
        _reset(conn)
    out = _run(ANCESTOR_ONLY)
    assert out["count"] == 0
    assert out["detail"]["serving_set_empty"] is True
    assert out["detail"]["share_pct"] is None


def test_the_runner_proves_the_partition_adds_up(pg_engine):
    """`partition_is_exhaustive` is the runtime version of the disjointness test: if a future edit
    makes the four predicates overlap or leave a gap, this says so in the log line of every run
    rather than waiting for someone to re-derive it."""
    with pg_engine.begin() as conn:
        _reset(conn)
        for i, path in enumerate([
            "beauty/makeup/lip/lipstick", "beauty/makeup", "beauty/skincare/tone/toner",
            None, "Beauty/Makeup",
        ]):
            _product(conn, pk="p%d" % i, ck="ck%d" % i, category_path=path)
    out = _run(ANCESTOR_ONLY)
    d = out["detail"]
    assert d["partition_is_exhaustive"] is True, d
    assert d["partition_total"] == d["serving_eligible_rows"] == 5
    assert d["buckets"] == {
        # lipstick leaf + tone/toner (a real leaf since 2026-09-10)
        "prefix_reachable": 2,
        "ancestor_only": 1,
        "off_taxonomy": 1,   # the mixed-case row: case-sensitive LIKE gives it no door
        "no_path": 1,
    }, d["buckets"]


def test_a_BLANK_path_is_no_path_and_not_off_taxonomy(pg_engine):
    """Kills the mutant that drops the `<> ''` guard, which round 2 found was caught only by a
    string assertion — no PG test ever ran a blank path against the off-taxonomy predicate."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="blank", ck="ck_blank", category_path="   ")
        assert _matched(conn, "off_taxonomy") == 0
        assert _matched(conn, "no_path") == 1
    assert _run(NO_PATH)["count"] == 1000
    assert _run(OFF_TAXONOMY)["count"] == 0


def test_all_three_checks_are_registered_as_ENFORCING(pg_engine):
    """`warn_only` at threshold 0 would print the real number every run and alarm on nothing — a
    metric wearing a detector's name, which is the category error this work exists to remove."""
    for name in (ANCESTOR_ONLY, OFF_TAXONOMY, NO_PATH):
        c = _check(name)
        assert not c.get("warn_only"), "%s is warn_only" % name
        assert c["count_sql"] is None and c["sample_sql"] is None, (
            "%s must not restate the share in SQL" % name
        )
        assert c["default_threshold"] > 0, name
