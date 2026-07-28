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


def _seed(conn, *, rows: int, summary_score: float, description_score: float = 100.0):
    conn.execute(text("DELETE FROM product_quality_snapshot"))
    for _ in range(rows):
        details = {
            "components": [
                {"name": "title", "score": 100.0},
                {"name": "description", "score": description_score},
                {"name": "summary", "score": summary_score},
                {"name": "price", "score": 100.0},
            ]
        }
        conn.execute(
            text(
                "INSERT INTO product_quality_snapshot (snapshot_date, details) "
                "VALUES (NOW(), CAST(:d AS json))"
            ),
            {"d": json.dumps(details)},
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
    chk = _check("dead_quality_component")
    _assert_throwaway_database()
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(text(DDL))
        _seed(conn, rows=250, summary_score=0.0)
        count = conn.execute(text(chk["count_sql"])).scalar()
        names = [r[0] for r in conn.execute(text(chk["sample_sql"])).fetchall()]
    eng.dispose()
    assert count == 1, f"expected the dead component to be counted, got {count}"
    assert names == ["summary"]


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
