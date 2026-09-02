"""Postgres-dialect gate for the `dead_quality_component` canary.

The canary's SQL is Postgres-only three ways — `jsonb_array_elements`, the
LATERAL join, and the `details::jsonb` cast — none of which a SQLite suite can
execute. An untyped/undialected statement that only fails at PREPARE time is
exactly the defect class that took the canonical feed down in pivota-backend#1588
(a `concat` parameter Postgres could not type, shipped green past two reviews and
a full SQLite suite).

So this executes the real statement from `_CHECKS` against a real engine, and
also proves the check DETECTS a dead component and CLEARS when the component is
alive — a canary that cannot answer both ways is not a canary.

RUNNING THIS. Skipped unless DATABASE_URL points at a Postgres:

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        pytest tests/test_dead_quality_component_canary_postgres.py

Never point this at prod: it writes rows into product_quality_snapshot.
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

# This gate needs ROWS (the check is an aggregate over component scores), unlike
# the PREPARE-only gates beside it. So it recreates the table rather than using
# CREATE TABLE IF NOT EXISTS: the real product_quality_snapshot carries NOT NULL
# identity columns this test does not populate, and an IF NOT EXISTS against a
# pre-existing real schema would fail on INSERT for a confusing reason.
#
# DROP is gated on the database NAME so this can never run anywhere but a
# throwaway. The module docstring says "never point this at prod"; this makes it
# true rather than merely stated.
DDL = """
DROP TABLE IF EXISTS product_quality_snapshot;
CREATE TABLE product_quality_snapshot (
  id serial primary key,
  merchant_id text,
  platform text,
  snapshot_date timestamp,
  details json
);
"""

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to recreate product_quality_snapshot in database {dbname!r} — "
            f"this gate drops and recreates that table and must only run against a "
            f"throwaway (e.g. pivota_dialect_check)"
        )


def _check(name: str):
    from services.catalog_invariant_checks import _CHECKS

    for c in _CHECKS:
        if c["name"] == name:
            return c
    raise AssertionError(f"check {name!r} not registered")


def _engine():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))


def _seed(
    conn,
    *,
    rows: int,
    summary_score: float,
    description_score: float = 100.0,
    attributes_score: float = 100.0,
    platform: str = "external_seed",
    merchant: str | None = None,
    days_ago: int = 0,
    reset: bool = True,
):
    """`days_ago` > 30 puts the rows OUTSIDE the check's recent window, which is
    how a test gives a lane HISTORY for a component without also giving it a
    recent non-zero row (that would simply make the component alive)."""
    if reset:
        conn.execute(text("DELETE FROM product_quality_snapshot"))
    for _ in range(rows):
        details = {
            "components": [
                {"name": "title", "score": 100.0},
                {"name": "description", "score": description_score},
                {"name": "summary", "score": summary_score},
                {"name": "attributes", "score": attributes_score},
                {"name": "price", "score": 100.0},
            ]
        }
        conn.execute(
            text(
                "INSERT INTO product_quality_snapshot "
                "(merchant_id, platform, snapshot_date, details) "
                "VALUES (:m, :p, NOW() - make_interval(days => :d), CAST(:dt AS json))"
            ),
            {"m": merchant or f"merch_{platform}", "p": platform, "d": int(days_ago),
             "dt": json.dumps(details)},
        )


def test_canary_sql_executes_on_postgres():
    """PREPARE-time gate: the statement must compile and run at all."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        conn.execute(text("DELETE FROM product_quality_snapshot"))
        conn.execute(text(chk["count_sql"])).scalar()
        conn.execute(text(chk["sample_sql"])).fetchall()
    eng.dispose()


def test_canary_fires_on_a_dead_component():
    """A lane that USED TO score `summary` and now scores it zero on 250
    recent rows: that is the defect this check exists for."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=0.0)
        _seed(conn, rows=1, summary_score=80.0, days_ago=60, reset=False)  # history
        count = conn.execute(text(chk["count_sql"])).scalar()
        names = [r[0] for r in conn.execute(text(chk["sample_sql"])).fetchall()]
    eng.dispose()
    assert count == 1, f"expected the dead component to be counted, got {count}"
    assert names == ["external_seed:summary"]


def test_a_lane_that_never_produced_the_component_is_not_a_dead_component():
    """The 2026-09-01 false alarm. Merchant-sync payloads carry no seed data and
    no size/usage keys, so `attributes` is structurally zero for them; that is
    a scorer-design question, not an outage. No history in the lane => silent."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=100.0, attributes_score=0.0, platform="wix")
        count = conn.execute(text(chk["count_sql"])).scalar()
    eng.dispose()
    assert count == 0


def test_another_lanes_history_does_not_make_this_lanes_zero_a_death():
    """History is PER LANE. The seed lane scoring `attributes` on thousands of
    rows says nothing about whether Wix ever could — and the window is per
    lane too, so a chatty lane cannot push a quiet lane's rows out of it and
    then be judged on the quiet lane's behalf."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=100.0, attributes_score=0.0, platform="wix")
        _seed(conn, rows=300, summary_score=100.0, attributes_score=100.0,
              platform="external_seed", reset=False)
        count = conn.execute(text(chk["count_sql"])).scalar()
    eng.dispose()
    assert count == 0


def test_one_chatty_merchant_cannot_convict_its_lane():
    """The window is per (platform, merchant), not per platform. A Wix store
    rescored 2,500 times with `summary` at zero must not read as "wix:summary
    is dead" while another Wix merchant is scoring it 90 right now — and that
    healthier merchant's rows are exactly what would otherwise satisfy the
    history EXISTS. Healthy rows go in FIRST (lower ids), chatty rows on top."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=300, summary_score=90.0, platform="wix", merchant="wix_healthy")
        _seed(conn, rows=2500, summary_score=0.0, platform="wix", merchant="wix_chatty",
              reset=False)
        count = conn.execute(text(chk["count_sql"])).scalar()
    eng.dispose()
    assert count == 0


def test_a_lane_dead_across_every_merchant_is_still_caught():
    """The verdict grain is the LANE: two merchants both at zero on a component
    the lane used to produce is the outage, and each contributes to the >=200
    floor."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=1, summary_score=80.0, platform="wix", merchant="wix_a", days_ago=60)
        _seed(conn, rows=120, summary_score=0.0, platform="wix", merchant="wix_a", reset=False)
        _seed(conn, rows=120, summary_score=0.0, platform="wix", merchant="wix_b", reset=False)
        count = conn.execute(text(chk["count_sql"])).scalar()
        names = [r[0] for r in conn.execute(text(chk["sample_sql"])).fetchall()]
    eng.dispose()
    assert count == 1
    assert names == ["wix:summary"]


def test_malformed_historical_details_cannot_cost_the_check_its_verdict():
    """The history EXISTS reads the platform's whole history, so one old row
    with a non-numeric score or a non-array `components` used to raise — and a
    check that raises reports {"error": ...} instead of a verdict, the failure
    mode #2007 exists to stop. Such rows must count as nothing, not as an
    exception."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=1, summary_score=80.0, days_ago=60)
        _seed(conn, rows=250, summary_score=0.0, reset=False)
        for bad in (
            {"components": [{"name": "summary", "score": "n/a"}]},
            {"components": [{"name": "summary", "score": True}]},
            {"components": {"summary": 1}},
            "just a string",
        ):
            conn.execute(
                text(
                    "INSERT INTO product_quality_snapshot "
                    "(merchant_id, platform, snapshot_date, details) "
                    "VALUES ('merch_external_seed', 'external_seed', "
                    "NOW() - INTERVAL '200 days', CAST(:dt AS json))"
                ),
                {"dt": json.dumps(bad)},
            )
        count = conn.execute(text(chk["count_sql"])).scalar()
        names = [r[0] for r in conn.execute(text(chk["sample_sql"])).fetchall()]
    eng.dispose()
    # The malformed rows are neither evidence of life ("n/a" is not > 0) nor a
    # crash: the genuinely dead component is still reported.
    assert count == 1
    assert names == ["external_seed:summary"]


def test_the_window_is_per_lane_not_global():
    """A lane with history that goes dead is still caught even when another
    lane out-writes it by an order of magnitude — the failure mode of the
    first version was that the global LIMIT 2000 became one lane's rows."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        # ORDER IS THE TEST. The dead lane's rows go in FIRST (lower ids), then
        # the chatty lane writes 2,100 rows on top. A global "most recent 2,000"
        # window now contains only Wix rows and never sees the dead lane; a
        # per-lane window still does. Inserted the other way round the dead
        # rows sit inside the global window too and the mutant survives.
        _seed(conn, rows=1, summary_score=80.0, platform="external_seed", days_ago=60)
        _seed(conn, rows=250, summary_score=0.0, platform="external_seed", reset=False)
        _seed(conn, rows=2100, summary_score=100.0, attributes_score=0.0, platform="wix", reset=False)
        count = conn.execute(text(chk["count_sql"])).scalar()
        names = [r[0] for r in conn.execute(text(chk["sample_sql"])).fetchall()]
    eng.dispose()
    assert count == 1
    assert names == ["external_seed:summary"]


def test_canary_is_silent_when_every_component_is_alive():
    """The other half. A check that only ever fires is noise, not signal."""
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=70.0)
        count = conn.execute(text(chk["count_sql"])).scalar()
    eng.dispose()
    assert count == 0


def test_small_samples_cannot_manufacture_a_violation():
    """A quiet period must not read as a dead component.

    The >=200 floor exists so that two rows scoring zero on a component do not
    trip an alarm whose default threshold is 0.
    """
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=5, summary_score=0.0)
        count = conn.execute(text(chk["count_sql"])).scalar()
    eng.dispose()
    assert count == 0


def test_threshold_is_zero_and_named():
    chk = _check("dead_quality_component")
    assert chk["default_threshold"] == 0
    assert chk["env"] == "CATALOG_INVARIANT_DEAD_COMPONENT_THRESHOLD"


@pytest.mark.asyncio
async def test_sample_rows_carry_subject_key_through_the_production_reader():
    """The tests above read samples by POSITION (`r[0]`), which is not how the
    runner reads them: run_catalog_invariant_checks reads `r["subject_key"]`,
    and on asyncpg a `databases` Record raises KeyError for a column the row
    does not carry. This sample_sql projected `name` until 2026-09-02, so the
    daily sweep on worker logged a KeyError traceback for this check instead
    of its samples — with the positional tests green. Read it the way the
    runner does, through the production client."""
    from databases import Database

    from services.catalog_invariant_checks import SAMPLE_KEY_COLUMN

    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=0.0)
        _seed(conn, rows=1, summary_score=80.0, days_ago=60, reset=False)  # history
    eng.dispose()

    db = Database(DATABASE_URL.replace("postgres://", "postgresql://", 1))
    await db.connect()
    try:
        rows = await db.fetch_all(chk["sample_sql"])
        names = [r[SAMPLE_KEY_COLUMN] for r in rows]
    finally:
        await db.disconnect()
    assert names == ["external_seed:summary"]
