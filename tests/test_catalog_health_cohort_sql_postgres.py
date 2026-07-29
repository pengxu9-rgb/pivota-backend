"""Postgres gate for /__catalog_health's _COHORT_SQL — the foreign_market split.

The 2026-07-29 change joins index_pipeline_state into the cohort funnel and
carves `foreign_market` (blocker_code = 'no_us_offer') out of
`quality_blocked`. That is live Postgres SQL with a CASE whose arm ORDER is
load-bearing — a mock-replay test (test_catalog_health_blocker_histogram)
cannot see a wrong join key, a fanout, or a precedence slip, which is exactly
the class of defect the 2026-07-26 feed outage taught us SQLite/mocks wave
through. So the statement itself runs here, on the production dialect.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` + DELETE only —
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

_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS index_pipeline_state (
  content_key text, serving_eligible boolean, index_eligible boolean,
  blocker_code text, blocker_detail text,
  content_quality_score double precision, quality_scored_at timestamp
);
CREATE TABLE IF NOT EXISTS catalog_row_trust (
  subject_type text, subject_key text, serving_decision text,
  serving_reason_codes text[]
);
"""


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

    for t in ("catalog_products", "index_pipeline_state", "catalog_row_trust"):
        conn.execute(text(f"DELETE FROM {t}"))


def _seed(conn, *, pk, ck, decision="blocked", reasons=("INDEX_NOT_SERVING_ELIGIBLE",),
          blocker=None, with_ips=True):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, source_system) "
            "VALUES (:pk, 'external_seed', 'external_seed', :pk, :pk, :ck, "
            "        'external_referral', 'external_product_seeds_mirror_v1')"
        ),
        {"pk": pk, "ck": ck},
    )
    conn.execute(
        text("INSERT INTO catalog_row_trust (subject_type, subject_key, "
             "serving_decision, serving_reason_codes) "
             "VALUES ('product', :pk, :dec, :reasons)"),
        {"pk": pk, "dec": decision, "reasons": list(reasons)},
    )
    if with_ips:
        conn.execute(
            text("INSERT INTO index_pipeline_state (content_key, serving_eligible, "
                 "blocker_code) VALUES (:ck, false, :blocker) ON CONFLICT DO NOTHING"),
            {"ck": ck, "blocker": blocker},
        )


def _cohorts(conn):
    from sqlalchemy import text

    from routes.__catalog_health import _COHORT_SQL

    out = {}
    for r in conn.execute(text(_COHORT_SQL)).mappings():
        out[r["cohort"]] = out.get(r["cohort"], 0) + int(r["cnt"])
    return out


def test_no_us_offer_key_is_foreign_market_not_quality_blocked(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, pk="pk_foreign", ck="ck_foreign", blocker="no_us_offer")
        assert _cohorts(conn) == {"foreign_market": 1}


def test_other_blockers_stay_quality_blocked(pg_engine):
    """The split must carve out EXACTLY no_us_offer — a low_quality key is the
    recoverable backlog the bucket exists to count."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, pk="pk_lowq", ck="ck_lowq", blocker="low_quality")
        assert _cohorts(conn) == {"quality_blocked": 1}


def test_missing_ips_row_stays_quality_blocked(pg_engine):
    """A trust-blocked row with NO index_pipeline_state row keeps its
    trust-derived bucket — the LEFT JOIN must not invent or drop rows."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, pk="pk_noips", ck="ck_noips", with_ips=False)
        assert _cohorts(conn) == {"quality_blocked": 1}


def test_retirement_precedence_beats_the_foreign_market_arm(pg_engine):
    """CASE order is load-bearing: a tombstoned row whose key ALSO carries
    no_us_offer is retired_by_design, not foreign_market."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, pk="pk_tomb", ck="ck_tomb",
              reasons=("ROW_TOMBSTONED", "INDEX_NOT_SERVING_ELIGIBLE"),
              blocker="no_us_offer")
        assert _cohorts(conn) == {"retired_by_design": 1}


def test_public_row_is_unaffected_by_the_ips_join(pg_engine):
    """One row per product, still: the added join must not fan out a public
    row into extra cohort counts."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, pk="pk_pub", ck="ck_pub", decision="public", reasons=(),
              blocker="none")
        assert _cohorts(conn) == {"public": 1}
