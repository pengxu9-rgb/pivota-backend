"""The catalog_sync_jobs claim, driven against real Postgres.

tests/services/test_catalog_sync_job_claim.py runs the same statements on the
suite's default SQLite database, where two claims can never actually overlap —
SQLite serialises every writer, so "only one wins" holds there by construction.
The two properties that matter in production are Postgres-only:

  * ROW-LOCK RE-CHECK. Under READ COMMITTED, a second `UPDATE ... WHERE status =
    'pending'` against a row another transaction is mid-way through claiming
    BLOCKS on the row lock, then re-evaluates its WHERE against the committed
    row and matches nothing. That is the whole lock; it is exercised here with
    a real second connection holding the row.
  * ONE CLOCK. The columns are `timestamp without time zone`, so a server-side
    CURRENT_TIMESTAMP lands in the SESSION time zone. Production is UTC; a
    developer's Postgres often is not. The claim stamps `started_at` from the
    same naive-UTC clock the stale reaper compares against — pinned here under
    a non-UTC session, where a CURRENT_TIMESTAMP `started_at` reads as hours in
    the future (and the reaper never fires) or the past (and it fires on a live
    run).

Shares the dialect gate's one database with every other *_postgres file: the
table comes from the model via ensure_model_tables (create-if-missing), and only
ROWS are cleaned, by this file's merchant prefix.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgres")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

_PREFIX = "csjclaimpg"


@pytest.fixture
async def db():
    from db.catalog import catalog_sync_events, catalog_sync_jobs
    from db.database import database
    from tests.model_schema import ensure_model_tables

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables((catalog_sync_jobs, catalog_sync_events))
    await _reset(database)
    try:
        yield database
    finally:
        await _reset(database)
        if not was_connected:
            await database.disconnect()


async def _reset(database) -> None:
    for table in ("catalog_sync_jobs", "catalog_sync_events"):
        await database.execute(
            f"DELETE FROM {table} WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
        )


async def _make_job(merchant_id: str) -> str:
    from services.catalog_sync_service import create_catalog_sync_job

    job = await create_catalog_sync_job(
        merchant_id=merchant_id,
        connector="shopify",
        mode="reconcile",
        scope={"platform": "shopify"},
        requested_by="test",
    )
    return str(job["job_id"])


def _raw_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def test_a_claim_racing_an_uncommitted_claim_blocks_then_loses(db) -> None:
    """The mutant this kills: dropping `AND status = 'pending'` from the claim.

    A second connection claims the row inside an open transaction. Our claim
    must block on that row lock and — once the other side commits — come back
    with NOTHING, not a second copy of the job to ingest.
    """
    import asyncpg

    from services.catalog_sync_service import claim_catalog_sync_job, get_catalog_sync_job

    job_id = await _make_job(f"{_PREFIX}_race")
    rival = await asyncpg.connect(_raw_dsn())
    try:
        tx = rival.transaction()
        await tx.start()
        moved = await rival.fetch(
            "UPDATE catalog_sync_jobs SET status = 'running', started_at = $2 "
            "WHERE job_id = $1 AND status = 'pending' RETURNING job_id",
            job_id, datetime.utcnow(),
        )
        assert len(moved) == 1

        ours = asyncio.ensure_future(claim_catalog_sync_job(job_id))
        await asyncio.sleep(0.5)
        assert not ours.done(), "the claim did not wait for the rival's row lock"

        await tx.commit()
        assert await asyncio.wait_for(ours, timeout=10) is None
    finally:
        await rival.close()

    assert (await get_catalog_sync_job(job_id))["status"] == "running"


async def test_a_claim_racing_a_rolled_back_claim_wins(db) -> None:
    """The other half of the re-check: a rival that ROLLS BACK leaves the row
    pending, and the blocked claim must then take it rather than give up."""
    import asyncpg

    from services.catalog_sync_service import claim_catalog_sync_job

    job_id = await _make_job(f"{_PREFIX}_rollback")
    rival = await asyncpg.connect(_raw_dsn())
    try:
        tx = rival.transaction()
        await tx.start()
        await rival.execute(
            "UPDATE catalog_sync_jobs SET status = 'running' WHERE job_id = $1", job_id,
        )
        ours = asyncio.ensure_future(claim_catalog_sync_job(job_id))
        await asyncio.sleep(0.5)
        assert not ours.done()
        await tx.rollback()
        claimed = await asyncio.wait_for(ours, timeout=10)
    finally:
        await rival.close()

    assert claimed is not None and claimed["job_id"] == job_id


async def test_the_claim_stamps_started_at_in_utc_under_a_non_utc_session(db) -> None:
    """Mutant: `started_at = CURRENT_TIMESTAMP`. Under Asia/Shanghai that stores
    local time, 8h ahead of the UTC cutoff the reaper compares against."""
    from services.catalog_sync_service import claim_catalog_sync_job

    job_id = await _make_job(f"{_PREFIX}_tz")
    async with db.connection() as conn:
        await conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        try:
            claimed = await claim_catalog_sync_job(job_id)
        finally:
            await conn.execute("RESET TIME ZONE")

    assert claimed is not None
    drift = abs((claimed["started_at"] - datetime.utcnow()).total_seconds())
    assert drift < 120, f"started_at is {drift:.0f}s away from UTC now"


async def test_the_stale_reaper_leaves_a_live_run_alone_under_a_non_utc_session(db) -> None:
    """End to end under the skewed session: a run claimed a moment ago must not
    be requeued, and one claimed hours ago must be."""
    from services.catalog_sync_service import (
        claim_catalog_sync_job,
        get_catalog_sync_job,
        requeue_stale_catalog_sync_jobs,
    )

    live = await _make_job(f"{_PREFIX}_live")
    dead = await _make_job(f"{_PREFIX}_dead")
    async with db.connection() as conn:
        await conn.execute("SET TIME ZONE 'America/Los_Angeles'")
        try:
            await claim_catalog_sync_job(live)
            await claim_catalog_sync_job(dead)
            await db.execute(
                "UPDATE catalog_sync_jobs SET started_at = :t WHERE job_id = :j",
                {"t": datetime.utcnow() - timedelta(hours=3), "j": dead},
            )
            requeued = await requeue_stale_catalog_sync_jobs(stale_after_seconds=3600)
        finally:
            await conn.execute("RESET TIME ZONE")

    assert requeued == 1, "the reaper's count must be real on Postgres (execute() returns None)"
    assert (await get_catalog_sync_job(live))["status"] == "running"
    assert (await get_catalog_sync_job(dead))["status"] == "pending"


async def test_claim_next_hands_one_job_to_exactly_one_of_two_drainers(db) -> None:
    """Two drainers (a worker revision swap briefly runs two processes) racing
    over a single pending row: one runs it, the other gets None."""
    import asyncpg

    from services.catalog_sync_service import claim_next_catalog_sync_job

    job_id = await _make_job(f"{_PREFIX}_two")
    rival = await asyncpg.connect(_raw_dsn())
    try:
        tx = rival.transaction()
        await tx.start()
        await rival.execute(
            "UPDATE catalog_sync_jobs SET status = 'running' "
            "WHERE job_id = $1 AND status = 'pending'", job_id,
        )
        ours = asyncio.ensure_future(claim_next_catalog_sync_job())
        await asyncio.sleep(0.5)
        await tx.commit()
        result = await asyncio.wait_for(ours, timeout=10)
    finally:
        await rival.close()

    # Either our candidate SELECT already saw it running (None, no claim) or it
    # picked it while the rival held the lock and then lost the conditional
    # UPDATE (None). What must never happen is a second copy.
    assert result is None or result["job_id"] != job_id


async def test_a_failed_drain_job_keeps_jsonb_objects_and_stays_pollable(db, monkeypatch) -> None:
    """Postgres half of the review's regression: the raw claim row's JSONB
    columns arrive as `str`, and writing them back through the table stored
    JSON *strings*. Checked at the column (`jsonb_typeof`), not just through the
    Python read-back."""
    import services.catalog_sync_service as css
    from routes.catalog_routes import _job_response

    job = await css.create_catalog_sync_job(
        merchant_id=f"{_PREFIX}_fail",
        connector="shopify",
        mode="reconcile",
        scope={"platform": "shopify", "limit": 7},
        requested_by="test",
    )

    async def _boom(**_kwargs):
        raise RuntimeError("ingest blew up")

    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _boom)
    claimed = await css.claim_next_catalog_sync_job()
    assert claimed is not None and claimed["job_id"] == job["job_id"]
    with pytest.raises(RuntimeError):
        await css.run_claimed_catalog_sync_job(claimed)

    types = await db.fetch_one(
        "SELECT status, jsonb_typeof(scope_json::jsonb) AS scope_t, "
        "jsonb_typeof(stats_json::jsonb) AS stats_t FROM catalog_sync_jobs WHERE job_id = :j",
        {"j": job["job_id"]},
    )
    assert dict(types) == {"status": "failed", "scope_t": "object", "stats_t": "object"}
    assert _job_response(await css.get_catalog_sync_job(job["job_id"])).status == "failed"
