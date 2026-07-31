"""The invariant that finally sees `serving_eligible` rows the gateway won't render.

THE GAP IT CLOSES. All six pre-existing `catalog_invariant_checks` are anchored on
"trust says public", so a row the INDEX PIPELINE wants public — but that trust has
not (or not yet) promoted — is invisible to every one of them. Measured on prod
2026-07-29: **2,265 rows are `serving_eligible` while the gateway has no
resolvable content route**, and `public_not_renderable` counts exactly 1 of them,
because it asks the same predicate of a different set.

WHY THE THRESHOLD IS 4 AND NOT 2,265. Only 4 of those rows are CLEAN — neither
`suppressed_at` nor `suppression_reason` set — and 1,474 are on the
hard-coded-dark shopify lane (`MERCHANT_SYNCED_RENDERABLE_BY_PLATFORM['shopify'] = False`).
Blessing 2,265 would leave the check deaf to a 500-row regression. The module's
own convention is explicit that a threshold is the measured baseline of the
UNEXPLAINED residual.

Postgres gate because the predicate is correlated-EXISTS SQL that SQLite cannot
execute, and because a check that silently counts 0 everywhere would look exactly
like a healthy catalog — so the tests below prove it counts BOTH ways.

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
CREATE TABLE IF NOT EXISTS catalog_row_trust (
  subject_type text, subject_key text, serving_decision text,
  serving_reason_codes text[]
);
"""


def _check():
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == "serving_eligible_not_renderable":
            return c
    raise AssertionError("serving_eligible_not_renderable is not registered")


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

    for t in ("catalog_products", "index_pipeline_state", "external_product_seeds"):
        conn.execute(text(f"DELETE FROM {t}"))


def _row(conn, *, pk, ck="ck_x", platform="external_seed", serving=True,
         with_seed=False, suppression_reason=None, suppressed=False,
         merchant_id="external_seed"):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO index_pipeline_state (content_key, serving_eligible) "
             "VALUES (:ck, :se) ON CONFLICT DO NOTHING"),
        {"ck": ck, "se": serving},
    )
    if with_seed:
        conn.execute(
            text("INSERT INTO external_product_seeds (external_product_id, status) "
                 "VALUES (:spid, 'active')"),
            {"spid": pk},
        )
    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " content_key, catalog_track, suppression_reason, suppressed_at) "
            "VALUES (:pk, :mid, :plat, :pk, :pk, :ck, "
            "        'external_referral', :reason, "
            f"        {'NOW()' if suppressed else 'NULL'})"
        ),
        {"pk": pk, "mid": merchant_id, "plat": platform, "ck": ck,
         "reason": suppression_reason},
    )


def _count(conn):
    from sqlalchemy import text

    return conn.execute(text(_check()["count_sql"])).scalar()


def test_counts_a_serving_row_with_no_content_route(pg_engine):
    """The positive case — the whole reason the check exists."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_dead", ck="ck_dead", serving=True, with_seed=False)
        assert _count(conn) == 1


def test_does_not_count_a_row_that_renders(pg_engine):
    """The other direction. A check that only ever counts up is not a signal."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_live", ck="ck_live", serving=True, with_seed=True)
        assert _count(conn) == 0


def test_does_not_count_a_row_the_index_does_not_want_served(pg_engine):
    """`serving_eligible = false` + no route is not a defect, it is agreement."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_off", ck="ck_off", serving=False, with_seed=False)
        assert _count(conn) == 0


def test_excludes_already_retired_rows(pg_engine):
    """2,261 of the 2,265 on prod are tombstoned or suppressed.

    Those are decided, not drifting — counting them would drown the 4 that are
    genuinely unexplained.
    """
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_tomb", ck="ck_tomb", serving=True, with_seed=False,
             suppression_reason="cross_merchant_redundant_external_seed")
        assert _count(conn) == 0

        _reset(conn)
        _row(conn, pk="pk_supp", ck="ck_supp", serving=True, with_seed=False,
             suppressed=True)
        assert _count(conn) == 0


def test_excludes_the_hard_coded_dark_merchant_sync_lane(pg_engine):
    """shopify is excluded because its lane verdict is a deliberate False
    (7/7-500 measurement) — counting a constant as drift is noise. The wix case
    stays count==0 for a DIFFERENT reason since 2026-07-29: the wix verdict
    flipped True on the pilot's evidence, so a serving-eligible wix row on the
    merchant-synced lane is renderable and never enters the set at all.

    The rows here carry a REAL merchant_id: `_seed_routed_lane` claims any
    `merchant_id='external_seed'` row for the seed lane BEFORE the platform
    arms are consulted, so the fixture default would silently test the wrong
    lane (that misfit is pinned separately below)."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_shop", ck="ck_shop", platform="shopify",
             merchant_id="merch_lane_pin", serving=True, with_seed=False)
        assert _count(conn) == 0

        _reset(conn)
        _row(conn, pk="pk_wix", ck="ck_wix", platform="wix",
             merchant_id="merch_lane_pin", serving=True, with_seed=False)
        assert _count(conn) == 0


def test_counts_a_seed_lane_row_even_on_an_open_platform(pg_engine):
    """A `merchant_id='external_seed'` row is seed-routed no matter what its
    platform column says — the wix verdict being True cannot excuse a seed-lane
    row whose seed is missing. This is the row shape the 2026-07-29 flip PR
    tripped over in its own fixture; keep it counted on purpose."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_wix_seedlane", ck="ck_wix_seedlane", platform="wix",
             serving=True, with_seed=False)
        assert _count(conn) == 1


def test_sample_sql_returns_the_offending_keys(pg_engine):
    """A count with no sample is unactionable — the operator needs the rows."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, pk="pk_sample", ck="ck_sample", serving=True, with_seed=False)
        rows = conn.execute(text(_check()["sample_sql"])).fetchall()
    assert [r[0] for r in rows] == ["pk_sample"]


def test_threshold_is_the_measured_baseline_not_zero_and_not_the_raw_count():
    chk = _check()
    assert chk["default_threshold"] == 0, (
        "0 = the re-measured baseline after the four url_audit stubs were "
        "retired on 2026-07-29 (reason 'url_audit_stub_retired_20260729'). "
        "The threshold started at 4 — the clean unexplained residual at ship "
        "time — and the module convention makes lowering mandatory the moment "
        "the true count drops. If this assertion is being edited UPWARD, that "
        "is a regression being blessed, not a baseline being corrected."
    )
    assert chk["env"] == "CATALOG_INVARIANT_SERVING_NOT_RENDERABLE_THRESHOLD"


def test_it_asks_a_different_question_from_public_not_renderable():
    """Same predicate, different ANCHOR — that is the entire point.

    If someone 'deduplicates' these two checks, the serving_eligible-but-not-
    trust-public set goes unwatched again, which is the 2,264-row blind spot
    this closes.
    """
    from services.catalog_invariant_checks import _CHECKS

    names = {c["name"] for c in _CHECKS}
    assert {"public_not_renderable", "serving_eligible_not_renderable"} <= names
    a = [c for c in _CHECKS if c["name"] == "public_not_renderable"][0]
    b = _check()
    assert "catalog_row_trust" in a["count_sql"]
    assert "index_pipeline_state" in b["count_sql"]
    assert "catalog_row_trust" not in b["count_sql"]
