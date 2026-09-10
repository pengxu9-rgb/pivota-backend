"""The shared `category_taxonomy` table, EXECUTED against Postgres.

This table exists because two repositories kept two vocabularies over one `category_path` column
and each read the other's deliberate writes as corruption. A test suite that only asserted on
Python dicts would prove nothing about the constraints that keep the shared vocabulary coherent —
the self-FK, the one-hop rule, the alias/leaf exclusion — so those are exercised for real.
"""

from __future__ import annotations

import asyncio
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="needs a Postgres DATABASE_URL — production-dialect gate"
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to DELETE FROM category_taxonomy in {dbname!r}")


@pytest.fixture(scope="module")
def pg_engine():
    _assert_throwaway_database()
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    conn.execute(text("DELETE FROM category_taxonomy"))


def _row(conn, path, *, label="X", is_leaf=False, alias_of=None):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO category_taxonomy (path, label, is_leaf, alias_of)"
            " VALUES (:p, :l, :f, :a)"
        ),
        {"p": path, "l": label, "f": is_leaf, "a": alias_of},
    )


# --- the constraints are real ------------------------------------------------------------------


def test_an_alias_cannot_point_at_a_path_that_does_not_exist(pg_engine):
    """The self-FK. 117 orphan paths existed in production precisely because nothing enforced
    that a spelling resolves to something."""
    from sqlalchemy.exc import IntegrityError

    with pg_engine.begin() as conn:
        _reset(conn)
        with pytest.raises(IntegrityError):
            _row(conn, "beauty/x/y", alias_of="beauty/does/not/exist")


def test_an_alias_cannot_also_be_a_leaf(pg_engine):
    """A path cannot be both a canonical destination and a spelling of another one; a resolver
    reading it would answer differently depending on which column it looked at."""
    from sqlalchemy.exc import IntegrityError

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/makeup/lip/gloss", is_leaf=True)
        with pytest.raises(IntegrityError):
            _row(conn, "beauty/makeup/lips/gloss", is_leaf=True,
                 alias_of="beauty/makeup/lip/gloss")


def test_an_alias_cannot_point_at_itself(pg_engine):
    from sqlalchemy.exc import IntegrityError

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/a/b", is_leaf=True)
        with pytest.raises(IntegrityError):
            conn.execute(
                __import__("sqlalchemy").text(
                    "UPDATE category_taxonomy SET alias_of = path WHERE path = 'beauty/a/b'"
                )
            )


def test_a_valid_alias_IS_accepted(pg_engine):
    """The control. Constraints that reject everything pass all three tests above."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/makeup/lip/gloss", is_leaf=True)
        _row(conn, "beauty/makeup/lips/lip-gloss", alias_of="beauty/makeup/lip/gloss")
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM category_taxonomy")).scalar() == 2


# --- the store ----------------------------------------------------------------------------------


def _load(refresh=True):
    from databases import Database

    from services.category_taxonomy_store import load

    async def go():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            return await load(db, refresh=refresh)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def test_the_store_partitions_leaves_interior_and_aliases(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty", is_leaf=False)
        _row(conn, "beauty/makeup/lip/gloss", label="Lip Gloss", is_leaf=True)
        _row(conn, "beauty/makeup/lips/lip-gloss", alias_of="beauty/makeup/lip/gloss")
    out = _load()
    assert out["leaves"] == frozenset({"beauty/makeup/lip/gloss"})
    assert out["interior"] == frozenset({"beauty"})
    assert out["aliases"] == {"beauty/makeup/lips/lip-gloss": "beauty/makeup/lip/gloss"}
    assert out["labels"]["beauty/makeup/lip/gloss"] == "Lip Gloss"


def test_an_EMPTY_table_raises_rather_than_falling_back(pg_engine):
    """THE POINT. A silent fallback to in-code constants would make a service that cannot see the
    shared vocabulary behave exactly like one that agrees with it — the defect this table exists
    to remove, reintroduced in the code that reads it."""
    from services.category_taxonomy_store import TaxonomyUnavailable

    with pg_engine.begin() as conn:
        _reset(conn)
    with pytest.raises(TaxonomyUnavailable):
        _load()


def test_an_alias_CHAIN_is_refused(pg_engine):
    """One hop only. A chain needs a recursive resolve on a read path and can cycle; the DB CHECK
    cannot express this, so the store does."""
    from services.category_taxonomy_store import TaxonomyUnavailable

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/a/b/c", is_leaf=True)
        _row(conn, "beauty/a/b/mid", alias_of="beauty/a/b/c")
        _row(conn, "beauty/a/b/far", alias_of="beauty/a/b/mid")
    with pytest.raises(TaxonomyUnavailable, match="alias chain"):
        _load()


def test_the_cache_is_used_and_can_be_refreshed(pg_engine):
    from services.category_taxonomy_store import reset_cache

    reset_cache()
    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/a/b/c", is_leaf=True)
    first = _load()
    with pg_engine.begin() as conn:
        _row(conn, "beauty/a/b/d", is_leaf=True)
    assert _load(refresh=True)["leaves"] == first["leaves"] | {"beauty/a/b/d"}


# --- the drift detector ---------------------------------------------------------------------


def _drift():
    from databases import Database

    from services.catalog_invariant_checks import _run_taxonomy_code_vs_table_drift

    async def go():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            return await _run_taxonomy_code_vs_table_drift(db)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _seed_from_code(conn):
    from services.category_path_aliases import ALIASES, ANCESTOR_NODES, TAXONOMY_LEAVES

    _reset(conn)
    for path in sorted(TAXONOMY_LEAVES):
        _row(conn, path, is_leaf=True)
    for path in sorted(ANCESTOR_NODES):
        _row(conn, path, is_leaf=False)
    for src, tgt in sorted(ALIASES.items()):
        _row(conn, src, alias_of=tgt)


def test_a_table_seeded_from_code_reports_NO_drift(pg_engine):
    with pg_engine.begin() as conn:
        _seed_from_code(conn)
    out = _drift()
    assert out["count"] == 0, out["detail"]


def test_a_path_the_CODE_has_and_the_table_lacks_is_drift(pg_engine):
    """The 2026-09-10 shape: this repo about to classify onto a path the other cannot read."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _seed_from_code(conn)
        conn.execute(text(
            "DELETE FROM category_taxonomy WHERE path = 'beauty/skincare/tone/toner'"
            " AND NOT EXISTS (SELECT 1 FROM category_taxonomy a"
            "                 WHERE a.alias_of = 'beauty/skincare/tone/toner')"
        ))
        conn.execute(text("DELETE FROM category_taxonomy WHERE alias_of = 'beauty/skincare/tone/toner'"))
        conn.execute(text("DELETE FROM category_taxonomy WHERE path = 'beauty/skincare/tone/toner'"))
    out = _drift()
    assert out["count"] > 0
    assert "beauty/skincare/tone/toner" in out["detail"]["in_code_not_in_table"]


def test_a_path_the_TABLE_has_and_the_code_lacks_is_drift(pg_engine):
    """The other direction, and the one a shared table makes possible: the gateway adds a category
    and this service silently cannot classify into it."""
    with pg_engine.begin() as conn:
        _seed_from_code(conn)
        _row(conn, "beauty/oral-care/toothpaste", is_leaf=True)
    out = _drift()
    assert out["count"] > 0
    assert "beauty/oral-care/toothpaste" in out["detail"]["in_table_not_in_code"]


def test_an_alias_resolving_TWO_WAYS_is_drift(pg_engine):
    """The subtlest and worst case: both sides know the spelling and repair it differently, so
    each undoes the other on every run."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _seed_from_code(conn)
        conn.execute(text(
            "UPDATE category_taxonomy SET alias_of = 'beauty/makeup/lip/oil'"
            " WHERE path = 'beauty/makeup/lips/lip-gloss'"
        ))
    out = _drift()
    assert out["count"] > 0
    assert any("lips/lip-gloss" in c for c in out["detail"]["alias_conflicts"]), out["detail"]


def test_an_UNREADABLE_table_is_reported_not_treated_as_agreement(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
    out = _drift()
    assert out["count"] == 1
    assert out["detail"]["table_readable"] is False
