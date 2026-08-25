"""Production-dialect gate for the destination-liveness columns (migration 200) and their writer.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_external_seed_destination_liveness_postgres.py

WHY THIS MUST RUN ON REAL POSTGRES. Every guarantee in this lane is server-side and a SQLite
stand-in accepts all of it while proving nothing:

* the `ck_external_product_seeds_destination_verdict` CHECK — the only thing stopping a typo'd
  verdict from being stored and then silently failing every `IN (...)` predicate that reads it;
* `destination_verdict = CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN :verdict ELSE
  destination_verdict END` — the guard that stops "we could not look" from erasing "we looked
  and it was gone". The stubbed suite asserts the statement TEXT because it cannot execute it;
  only this file proves the expression is valid SQL and evaluates the way the text implies;
* the same CASE over `destination_checked_at` and `destination_http_status`;
* `updated_at = NOW()` and the `source_system` scoping on the mirror withdrawal;
* `ORDER BY destination_checked_at NULLS FIRST` — SQLite's NULL ordering is not Postgres's, and
  this ordering is what makes the sweep reach the never-verified corpus first.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_REPO = Path(__file__).resolve().parent.parent
_MIGRATION = _REPO / "db/migrations/200_external_seed_destination_liveness.sql"

# This gate DROPS `external_product_seeds` and `catalog_products`, so it must be incapable of
# running anywhere but a throwaway. `skipif(_IS_PG)` only asks "is this Postgres" — pointed at
# staging it would destroy the seed corpus. Convention copied from
# tests/test_card_rail_outcomes_postgres.py: the docstring's "never point this at prod" has to
# be MADE true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop external_product_seeds in database {dbname!r} — this gate drops "
            f"and recreates it and must only run against a throwaway (e.g. pivota_dialect_check)"
        )


# The columns the lane needs from the base table. The migration is additive (`ALTER TABLE IF
# EXISTS ... ADD COLUMN`), so the base table has to exist first — this is the subset of
# db/migrations/044 the writer and the sweep query actually touch.
_BASE_TABLE = """
CREATE TABLE external_product_seeds (
  id TEXT PRIMARY KEY,
  market TEXT NOT NULL DEFAULT 'US',
  domain TEXT NULL,
  destination_url TEXT NOT NULL,
  canonical_url TEXT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_MIRROR_TABLE = """
CREATE TABLE catalog_products (
  product_key TEXT PRIMARY KEY,
  source_ref TEXT NULL,
  source_system TEXT NULL,
  suppressed_at TIMESTAMPTZ NULL,
  suppression_reason TEXT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    # BEFORE connecting: refusing to open the connection at all is stronger than refusing to
    # drop once we are already in.
    _assert_throwaway_database()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    from db.sql_migrations import split_statements

    await database.execute("DROP TABLE IF EXISTS external_product_seeds CASCADE")
    await database.execute("DROP TABLE IF EXISTS catalog_products CASCADE")
    await database.execute(_BASE_TABLE)
    await database.execute(_MIRROR_TABLE)

    # Apply the DDL UNDER TEST, not a hand-written copy of it — a fixture that redeclared the
    # columns would be testing the fixture. Applied TWICE: the migration has to be idempotent
    # because schema_guard runs the same statements on every boot.
    sql = _MIGRATION.read_text()
    for _ in range(2):
        for statement in split_statements(sql):
            await database.execute(statement)

    yield database

    await database.execute("DROP TABLE IF EXISTS external_product_seeds CASCADE")
    await database.execute("DROP TABLE IF EXISTS catalog_products CASCADE")
    if not was_connected:
        await database.disconnect()


async def _seed(database, seed_id: str, **cols) -> None:
    row = {
        "id": seed_id,
        "destination_url": f"https://brand.com/products/{seed_id}",
        "canonical_url": f"https://brand.com/products/{seed_id}",
        "domain": "brand.com",
        "status": "active",
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_http_status": None,
        "destination_failure_streak": 0,
    }
    row.update(cols)
    await database.execute(
        """
        INSERT INTO external_product_seeds
            (id, destination_url, canonical_url, domain, status,
             destination_checked_at, destination_verdict, destination_http_status,
             destination_failure_streak)
        VALUES (:id, :destination_url, :canonical_url, :domain, :status,
                :destination_checked_at, :destination_verdict, :destination_http_status,
                :destination_failure_streak)
        """,
        row,
    )


async def _read(database, seed_id: str):
    return dict(
        await database.fetch_one(
            "SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id}
        )
    )


# --------------------------------------------------------------------------- the CHECK

@pytest.mark.asyncio
async def test_the_check_accepts_every_verdict_the_classifier_can_produce(_db):
    from services import external_seed_destination_liveness as liveness

    for i, verdict in enumerate(liveness.ALL_VERDICTS):
        await _seed(_db, f"eps_ok_{i}", destination_verdict=verdict)
    stored = await _db.fetch_all(
        "SELECT destination_verdict FROM external_product_seeds WHERE id LIKE 'eps_ok_%'"
    )
    assert {r["destination_verdict"] for r in stored} == set(liveness.ALL_VERDICTS)


@pytest.mark.asyncio
async def test_the_check_refuses_a_verdict_outside_the_vocabulary(_db):
    """Without the constraint, a typo is stored and then fails every `IN (...)` read silently."""
    with pytest.raises(Exception) as caught:
        await _seed(_db, "eps_bad", destination_verdict="deed_404")
    assert "ck_external_product_seeds_destination_verdict" in str(caught.value)


@pytest.mark.asyncio
async def test_null_is_allowed_because_never_verified_is_the_whole_corpus_today(_db):
    await _seed(_db, "eps_null", destination_verdict=None)
    assert (await _read(_db, "eps_null"))["destination_verdict"] is None


# --------------------------------------------------------------------------- the writer

@pytest.mark.asyncio
async def test_an_unverifiable_observation_writes_no_destination_fact(_db):
    """The CASE guards, executed rather than pattern-matched.

    The stubbed suite can only assert that the statement TEXT contains the CASE. This proves
    the expression is valid Postgres AND that it evaluates the way the text implies: a seed
    sitting at a confirmed 404 keeps its verdict, its status code and its clock when the next
    probe is a bot challenge — which is what stops one 429 from clearing `destination_dead`.
    """
    from services import external_seed_destination_liveness as liveness

    checked = NOW - timedelta(days=1)
    await _seed(
        _db,
        "eps_dead",
        destination_checked_at=checked,
        destination_verdict=liveness.VERDICT_DEAD_404,
        destination_http_status=404,
        destination_failure_streak=2,
    )

    await liveness.record_destination_observation(
        "eps_dead",
        liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, 429, None, "bot_challenge"),
        now=NOW,
    )

    row = await _read(_db, "eps_dead")
    assert row["destination_verdict"] == liveness.VERDICT_DEAD_404
    assert row["destination_http_status"] == 404
    assert row["destination_checked_at"] == checked
    assert row["destination_failure_streak"] == 2


@pytest.mark.asyncio
async def test_a_corroborated_dead_observation_advances_the_streak_and_the_clock(_db):
    from services import external_seed_destination_liveness as liveness

    await _seed(
        _db,
        "eps_adv",
        destination_checked_at=NOW - timedelta(days=3),
        destination_verdict=liveness.VERDICT_DEAD_404,
        destination_http_status=404,
        destination_failure_streak=1,
    )

    result = await liveness.record_destination_observation(
        "eps_adv",
        liveness.DestinationObservation(
            liveness.VERDICT_DEAD_404, 404, None, corroborated=True
        ),
        now=NOW,
    )

    row = await _read(_db, "eps_adv")
    assert row["destination_failure_streak"] == 2
    assert row["destination_checked_at"] == NOW
    assert result["retire"] is True


@pytest.mark.asyncio
async def test_an_uncorroborated_dead_observation_holds_the_streak_on_real_postgres(_db):
    from services import external_seed_destination_liveness as liveness

    await _seed(
        _db,
        "eps_uncorr",
        destination_checked_at=NOW - timedelta(days=3),
        destination_verdict=liveness.VERDICT_DEAD_404,
        destination_failure_streak=1,
    )

    result = await liveness.record_destination_observation(
        "eps_uncorr",
        liveness.DestinationObservation(
            liveness.VERDICT_DEAD_404, 404, None, corroborated=False
        ),
        now=NOW,
    )

    assert (await _read(_db, "eps_uncorr"))["destination_failure_streak"] == 1
    assert result["retire"] is False


@pytest.mark.asyncio
async def test_a_live_observation_resets_the_streak(_db):
    from services import external_seed_destination_liveness as liveness

    await _seed(
        _db,
        "eps_back",
        destination_checked_at=NOW - timedelta(days=3),
        destination_verdict=liveness.VERDICT_DEAD_404,
        destination_failure_streak=1,
    )
    await liveness.record_destination_observation(
        "eps_back", liveness.DestinationObservation(liveness.VERDICT_LIVE, 200, None), now=NOW
    )
    row = await _read(_db, "eps_back")
    assert row["destination_failure_streak"] == 0
    assert row["destination_verdict"] == liveness.VERDICT_LIVE


# --------------------------------------------------------------------------- retirement

@pytest.mark.asyncio
async def test_retirement_withdraws_only_the_mirror_rows_from_this_door(_db):
    """`source_ref` alone is not the link; `(source_ref, source_system)` is.

    An unscoped UPDATE would also suppress a row written by a different door that happens to
    carry the same `source_ref` value — see services/external_offer_dual_write.
    """
    from services import external_seed_destination_liveness as liveness
    from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM

    await _seed(_db, "eps_retire")
    for key, system in (
        ("prod::mirror", MIRROR_SOURCE_SYSTEM),
        ("prod::other_door", "some_other_intake_v3"),
    ):
        await _db.execute(
            "INSERT INTO catalog_products (product_key, source_ref, source_system, updated_at) "
            "VALUES (:k, :ref, :sys, :ts)",
            {"k": key, "ref": "eps_retire", "sys": system, "ts": NOW - timedelta(days=5)},
        )

    await liveness.retire_seed_for_dead_destination(
        "eps_retire",
        liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True),
        now=NOW,
    )

    seed = await _read(_db, "eps_retire")
    assert seed["status"] == "inactive"
    assert "auto-retired" in (seed["notes"] or "")

    mine = dict(
        await _db.fetch_one(
            "SELECT * FROM catalog_products WHERE product_key = 'prod::mirror'", {}
        )
    )
    assert mine["suppressed_at"] is not None
    assert mine["suppression_reason"] == liveness.SUPPRESSION_REASON
    assert mine["updated_at"] > NOW - timedelta(days=5), "an incremental consumer must see it"

    theirs = dict(
        await _db.fetch_one(
            "SELECT * FROM catalog_products WHERE product_key = 'prod::other_door'", {}
        )
    )
    assert theirs["suppressed_at"] is None, "another door's row must not be touched"


@pytest.mark.asyncio
async def test_the_retirement_note_appends_rather_than_clobbering(_db):
    from services import external_seed_destination_liveness as liveness

    await _seed(_db, "eps_note")
    await _db.execute(
        "UPDATE external_product_seeds SET notes = :n WHERE id = 'eps_note'",
        {"n": "curated by hand"},
    )
    await liveness.retire_seed_for_dead_destination(
        "eps_note",
        liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, None, corroborated=True),
        now=NOW,
    )
    notes = (await _read(_db, "eps_note"))["notes"]
    assert "curated by hand" in notes and "auto-retired" in notes


# --------------------------------------------------------------------------- the sweep queue

@pytest.mark.asyncio
async def test_the_sweep_queue_puts_never_verified_rows_first(_db):
    """`NULLS FIRST` is not SQLite's default ordering, and it is the whole point of the queue:
    every row in prod is NULL today, so a queue that sorted them last would never reach them."""
    from services import external_seed_destination_liveness as liveness

    await _seed(_db, "eps_recent", destination_checked_at=NOW - timedelta(days=1))
    await _seed(_db, "eps_old", destination_checked_at=NOW - timedelta(days=30))
    await _seed(_db, "eps_never", destination_checked_at=None)

    candidates = await liveness.get_sweep_candidates(10)
    assert [c["id"] for c in candidates] == ["eps_never", "eps_old", "eps_recent"]


@pytest.mark.asyncio
async def test_the_sweep_queue_skips_retired_seeds(_db):
    from services import external_seed_destination_liveness as liveness

    await _seed(_db, "eps_active")
    await _seed(_db, "eps_inactive", status="inactive")
    candidates = await liveness.get_sweep_candidates(10)
    assert [c["id"] for c in candidates] == ["eps_active"]
