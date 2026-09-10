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


def test_a_STALE_ALIAS_left_in_the_table_is_drift(pg_engine):
    """The direction the first version of the drift check could not see: it iterated the CODE's
    alias map, so an alias row the table has and the code does not was invisible.

    That row is not inert. It says "merge this", and once the gateway reads this table it will —
    which is exactly how seven INTENTIONALLY_DISTINCT paths were collapsed on 2026-09-10. A path
    this repo has since declared a GAP is the case that matters, so it is named in the output."""
    with pg_engine.begin() as conn:
        _seed_from_code(conn)
        _row(conn, "beauty/skincare/sets", alias_of="beauty/sets/gift-set")
    out = _drift()
    assert out["count"] > 0
    stale = out["detail"]["stale_table_aliases"]
    assert any("beauty/skincare/sets" in x for x in stale), out["detail"]
    assert any("declared GAP" in x for x in stale), "a retracted merge must say so"


def test_an_alias_the_code_ALSO_has_is_not_reported_as_stale(pg_engine):
    """The control. Without it, "every alias is stale" would pass the test above."""
    with pg_engine.begin() as conn:
        _seed_from_code(conn)
    out = _drift()
    assert out["detail"]["stale_table_aliases"] == []
    assert out["count"] == 0


# --- the seeder's APPLY path, executed --------------------------------------------------------


def _run_seeder(apply: bool):
    """Execute the seeder's apply path against OUR OWN connection.

    NOTHING TESTED THIS BEFORE, and that is how a `NameError` shipped: `TAXONOMY_GAPS` was used in
    the retraction branch and never imported. `--dry-run` never reaches that branch, so the
    cautious path an operator runs first could not see it; under `--apply` every upsert ran, the
    script died before printing its report, and the stale alias row it claimed to retract survived.

    Calls `run_seed(db, ...)` and NOT `main()`. main() connects and disconnects the shared global
    `database`, and these gate files share one database — a test that closes the global seam
    breaks whichever unrelated module runs next. It did: an earlier version of this helper made
    tests/test_commerce_ledger_retention_postgres.py count 6 events where it expected 5.
    """
    import importlib.util
    from pathlib import Path

    from databases import Database

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_seed_taxonomy_under_test", root / "scripts" / "seed_category_taxonomy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def go():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            return await module.run_seed(db, apply=apply)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def test_the_seeder_APPLY_path_runs_without_crashing(pg_engine):
    """The regression test for the NameError. `--dry-run` passing proves nothing about `--apply`."""
    with pg_engine.begin() as conn:
        _reset(conn)
    report = _run_seeder(apply=True)
    assert report["insert"] > 100, report
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM category_taxonomy")).scalar()
    assert n > 100, "the seeder reported success but wrote almost nothing: %s" % n


def test_the_seeder_RETRACTS_a_stale_merge_instruction(pg_engine):
    """A path this repo now declares a GAP, sitting in the table as an alias, says "merge this" to
    anyone reading the shared vocabulary. Retracting it cannot orphan a row — it only stops a
    rewrite — so it is the one deletion the seeder is allowed to perform."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/sets/gift-set", is_leaf=True)
        _row(conn, "beauty/skincare/sets", alias_of="beauty/sets/gift-set")
        assert conn.execute(
            text("SELECT alias_of FROM category_taxonomy WHERE path='beauty/skincare/sets'")
        ).scalar() == "beauty/sets/gift-set"

    _run_seeder(apply=True)

    with pg_engine.begin() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM category_taxonomy"
                 " WHERE path='beauty/skincare/sets' AND alias_of IS NOT NULL")
        ).scalar()
    assert left == 0, "the merge instruction survived the retraction"


def test_the_seeder_does_NOT_delete_a_canonical_row_it_does_not_recognise(pg_engine):
    """The control, and the reason the retraction is scoped rather than a general delete. A
    canonical path this repo does not know may be one the gateway wrote; removing it would orphan
    its rows, which is the failure the shared table exists to prevent."""
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _row(conn, "beauty/oral-care/toothpaste", is_leaf=True)

    _run_seeder(apply=True)

    with pg_engine.begin() as conn:
        survived = conn.execute(
            text("SELECT count(*) FROM category_taxonomy WHERE path='beauty/oral-care/toothpaste'")
        ).scalar()
    assert survived == 1, "a foreign canonical row was deleted"


def test_a_stale_alias_the_code_simply_FORGOT_is_also_drift(pg_engine):
    """Not only the declared-GAP case. Any alias in the table that the code does not have is a
    live merge instruction nobody owns; narrowing the check to GAPS alone would let a forgotten
    one sit there being acted on. (Kills the mutant that adds `and src in TAXONOMY_GAPS`.)

    ⚠️ This also means the table currently cannot hold a gateway-authored alias without going red.
    That is correct TODAY — PIVOTA-Agent has no reader or writer for this table, so every row in
    it was written by this repo's seeder. When the gateway gains a write path, this check needs a
    foreign-author allowance, and that is a deliberate decision, not an oversight."""
    with pg_engine.begin() as conn:
        _seed_from_code(conn)
        _row(conn, "beauty/oral-care/rinse", alias_of="beauty/skincare/cleanse/cleanser")
    out = _drift()
    assert out["count"] > 0
    stale = out["detail"]["stale_table_aliases"]
    assert any("beauty/oral-care/rinse" in x for x in stale), out["detail"]
    assert not any("declared GAP" in x for x in stale), (
        "this path is not a declared gap; only retracted merges should say so"
    )
