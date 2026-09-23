"""Execute migration 234 and the retailer ingest ledger's real SQL on Postgres.

What only Postgres can prove: the partial unique index makes re-enqueueing an open cohort a no-op,
`FOR UPDATE SKIP LOCKED` + a lease hands one due job to one drainer, an EXPIRED lease makes the job
claimable again (a killed execution is retried instead of stranded), approval merges exclusions
into JSONB options, and a closed job no longer blocks a fresh enqueue of the same cohort.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from db import retailer_ingest as ledger

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")
SOURCE = "test_retailer_ingest_ledger"
DOMAIN = "retailer-ingest-ledger.example"


@pytest.fixture
async def db():
    import asyncpg
    from databases import Database

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("synthetic ledger tests require a test database")
    connection = await asyncpg.connect(URL)
    try:
        await connection.execute((Path(__file__).resolve().parents[1] /
                                  "db/migrations/234_retailer_ingest_pipeline.sql").read_text())
    finally:
        await connection.close()
    database = Database(URL)
    await database.connect()
    try:
        yield database
    finally:
        await database.execute("DELETE FROM retailer_ingest_runs WHERE job_id IN "
                               "(SELECT id FROM retailer_ingest_jobs WHERE source=:s)", {"s": SOURCE})
        await database.execute("DELETE FROM retailer_ingest_jobs WHERE source=:s", {"s": SOURCE})
        await database.disconnect()


OPTIONS = {"vendors": ["3CE"], "only_category": "beauty/makeup/lip", "lip_title_evidence": True}


async def _enqueue(db, **kw):
    return await ledger.enqueue_job(domain=DOMAIN, brand=kw.pop("brand", "3CE"), options=kw.pop("options", OPTIONS),
                                    source=SOURCE, db=db, **kw)


async def _only_ours_due(db):
    """Other test modules may have due jobs; make ours the only claimable ones."""
    await db.execute("UPDATE retailer_ingest_jobs SET next_run_at = NOW() + interval '1 day' "
                     "WHERE source IS DISTINCT FROM :s AND status IN ('queued','apply_due')", {"s": SOURCE})


async def test_reenqueueing_an_open_cohort_is_a_no_op(db):
    first = await _enqueue(db)
    assert first and await _enqueue(db) is None
    # a different scope is a different cohort
    assert await _enqueue(db, options={**OPTIONS, "only_category": None})


async def test_approval_bookkeeping_is_not_scope(db):
    assert ledger.scope_key(DOMAIN, "3CE", OPTIONS) == ledger.scope_key(
        DOMAIN, "3CE", {**OPTIONS, "exclude_handles": ["x"], "accepted_flags": ["k"]})


async def test_one_claim_per_due_job_and_an_expired_lease_is_reclaimed(db):
    await _only_ours_due(db)
    job_id = await _enqueue(db)
    claimed = await ledger.claim_due_job(lease_seconds=3600, db=db)
    assert claimed["id"] == job_id and claimed["options"]["vendors"] == ["3CE"]
    assert await ledger.claim_due_job(lease_seconds=3600, db=db) is None  # leased
    await db.execute("UPDATE retailer_ingest_jobs SET lease_until = NOW() - interval '1 second' WHERE id=:id",
                     {"id": job_id})
    assert (await ledger.claim_due_job(lease_seconds=3600, db=db))["id"] == job_id


async def test_a_run_and_a_transition_round_trip(db):
    await _only_ours_due(db)
    job_id = await _enqueue(db)
    run_id = await ledger.start_run(job_id=job_id, stage="dry_run", image_sha="sha", execution="exec-1", db=db)
    await ledger.finish_run(run_id, outcome="held", flags=[{"key": "k", "severity": "block"}],
                            checks={"kept": 8}, db=db)
    await ledger.transition(job_id, status="held", reason="1 blocking flag", run_id=run_id, db=db)
    [run] = await ledger.job_runs(job_id, db=db)
    assert run["outcome"] == "held" and run["finished_at"] is not None
    row = await db.fetch_one("SELECT status, last_run_id, lease_until FROM retailer_ingest_jobs WHERE id=:id",
                             {"id": job_id})
    assert row["status"] == "held" and row["last_run_id"] == run_id and row["lease_until"] is None
    assert await ledger.claim_due_job(lease_seconds=60, db=db) is None  # held is not due


async def test_approval_merges_exclusions_and_makes_the_job_due(db):
    await _only_ours_due(db)
    job_id = await _enqueue(db)
    await ledger.transition(job_id, status="held", reason="flag", run_id=None, db=db)
    assert await ledger.approve(job_id, approved_by="peng", exclude_handles=["3ce-tone-up-tint-40ml"],
                                accepted_flags=[], db=db)
    assert not await ledger.approve(job_id, approved_by="peng", exclude_handles=[], accepted_flags=[], db=db)
    claimed = await ledger.claim_due_job(lease_seconds=60, db=db)
    assert claimed["status"] == "apply_due"
    assert claimed["options"]["exclude_handles"] == ["3ce-tone-up-tint-40ml"]


async def test_a_closed_job_no_longer_blocks_a_fresh_enqueue(db):
    job_id = await _enqueue(db, brand="ESPOIR", options={"vendors": ["Espoir"]})
    await ledger.transition(job_id, status="done", reason="applied", run_id=None, db=db)
    assert await _enqueue(db, brand="ESPOIR", options={"vendors": ["Espoir"]})


async def test_a_bad_status_is_refused_by_the_schema(db):
    job_id = await _enqueue(db, brand="HERA", options={"vendors": ["HERA"]})
    with pytest.raises(Exception):
        await ledger.transition(job_id, status="processing", reason="x", run_id=None, db=db)
