"""The funnel producer's INSERT against REAL Postgres.

`record_anonymous_funnel_run` writes ARRAY-typed `product_keys` and JSONB
`partial_result_jsonb`; neither binds on SQLite, so the sibling test file
hand-inserts rows and tests only the reuse logic. The INSERT itself — and the
two properties that make it safe to expose to an unauthenticated caller — can
only be checked here:

  1. the row is created UNOWNED and stays out of the worker's claim query, so
     an anonymous endpoint cannot spend model credits;
  2. concurrent intakes for the same domain do not race into two owners once
     the run is claimed.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest \
        tests/test_funnel_anonymous_run_producer_postgres.py
"""

from __future__ import annotations

import asyncio
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

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "db/migrations" / "210_audit_runs_anonymous_claim.sql"
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop merchant_audit_runs in database {dbname!r} — "
            f"throwaway only (e.g. pivota_dialect_check)"
        )


# Production's shape, including the worker-lease columns and the stage default
# the producer relies on to stay OUT of the queue.
_PROD_SHAPE = """
CREATE TABLE merchant_audit_runs (
  run_id               UUID PRIMARY KEY,
  merchant_id          TEXT NOT NULL,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at         TIMESTAMPTZ NULL,
  status               TEXT NOT NULL,
  product_keys         TEXT[] NOT NULL DEFAULT '{}',
  subject_type         TEXT NOT NULL DEFAULT 'merchant',
  stage                TEXT NOT NULL DEFAULT 'completed',
  stage_updated_at     TIMESTAMPTZ NULL,
  partial_result_jsonb JSONB NULL,
  claimed_by_worker    TEXT NULL,
  claimed_until        TIMESTAMPTZ NULL
)
"""


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS merchant_audit_runs")
    await database.execute(_PROD_SHAPE)
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    import db.merchant_audit_runs as mar
    mar._DDL_READY = True
    yield
    mar._DDL_READY = False
    if not was_connected and database.is_connected:
        await database.disconnect()


def _now():
    return datetime.now(timezone.utc)


async def test_the_producer_creates_an_unowned_row_carrying_its_domain():
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await mar.record_anonymous_funnel_run(domain="Anua.com")
    assert run_id, "the INSERT failed on the real dialect"
    row = dict(await database.fetch_one(
        "SELECT * FROM merchant_audit_runs WHERE run_id = :r", {"r": run_id}))
    assert row["merchant_id"] is None
    assert row["subject_type"] == mar.SUBJECT_TYPE_PUBLIC_FUNNEL
    assert mar.funnel_domain_of(row) == "anua.com"


async def test_the_producers_row_is_not_visible_to_the_worker_queue():
    """The safety property behind exposing this to anonymous callers: the row
    must never reach the model-spending lane. `stage` keeps its terminal
    default, which is what the worker's claim query filters on."""
    import db.merchant_audit_runs as mar
    from db.database import database

    await mar.record_anonymous_funnel_run(domain="anua.com")
    queued = await database.fetch_one(
        "SELECT count(*) AS n FROM merchant_audit_runs WHERE stage IN "
        "('queued','discovering','probing','scoring','materializing','verifying')"
    )
    assert dict(queued)["n"] == 0


async def test_a_produced_run_is_found_then_claimed_then_not_found_again():
    """The whole funnel round trip on the real dialect."""
    import db.merchant_audit_runs as mar

    run_id = await mar.record_anonymous_funnel_run(domain="anua.com")
    found = await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1))
    assert found and found["run_id"] == run_id

    assert await mar.claim_audit_run_for_merchant(
        run_id=run_id, merchant_id="m-1") is True
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1)) is None


async def test_concurrent_claims_of_a_produced_run_yield_one_owner():
    """Two visitors can hold the same run id — the domain is public and the
    run is reused per domain. Only one claim may win."""
    import db.merchant_audit_runs as mar
    from db.database import database

    run_id = await mar.record_anonymous_funnel_run(domain="anua.com")
    results = await asyncio.gather(*[
        mar.claim_audit_run_for_merchant(run_id=run_id, merchant_id=f"m-{i}")
        for i in range(8)
    ])
    assert sum(1 for r in results if r) == 1
    owners = await database.fetch_all(
        "SELECT DISTINCT merchant_id FROM merchant_audit_runs "
        "WHERE run_id = :r", {"r": run_id})
    assert len(owners) == 1


async def test_the_unclaimed_index_covers_the_producers_sweep():
    """The reuse lookup filters on `merchant_id IS NULL`, which is exactly
    what migration 210's partial index is for."""
    from db.database import database
    row = await database.fetch_one(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'idx_merchant_audit_runs_unclaimed'")
    assert row is not None
    assert "merchant_id IS NULL" in dict(row)["indexdef"]
