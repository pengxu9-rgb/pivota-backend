"""The pending -> running transition is a LOCK, driven against a real database.

`run_catalog_sync_job` used to blind-write `status='running'` and ingest. That
was safe only while exactly one runner ever touched a row. Now a request handler
enqueues and an out-of-band drain tick runs, so two runners can reach the same
row, and the transition has to be a conditional UPDATE that hands the row to
exactly one of them.

These exercise the REAL SQL against the real `catalog_sync_jobs` DDL — a fake
claim in the caller's test proves nothing about the statement that does the
locking.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.catalog import catalog_sync_jobs
from db.database import database
from services.catalog_sync_service import (
    claim_catalog_sync_job,
    claim_next_catalog_sync_job,
    create_catalog_sync_job,
    get_catalog_sync_job,
    requeue_catalog_sync_job,
    requeue_stale_catalog_sync_jobs,
)
from tests.model_schema import ensure_model_tables

_PREFIX = "csjclaim"


@pytest.fixture(autouse=True)
async def _db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables((catalog_sync_jobs,))
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


async def _reset() -> None:
    await database.execute(
        "DELETE FROM catalog_sync_jobs WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
    )


async def _make_job(merchant_id: str) -> str:
    job = await create_catalog_sync_job(
        merchant_id=merchant_id,
        connector="shopify",
        mode="reconcile",
        scope={"platform": "shopify"},
        requested_by="test",
    )
    return str(job["job_id"])


async def test_a_new_job_is_pending_and_claiming_it_marks_it_running() -> None:
    job_id = await _make_job(f"{_PREFIX}_a")
    assert (await get_catalog_sync_job(job_id))["status"] == "pending"

    claimed = await claim_catalog_sync_job(job_id)

    assert claimed is not None
    assert claimed["job_id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["started_at"] is not None
    assert (await get_catalog_sync_job(job_id))["status"] == "running"


async def test_only_one_claim_wins() -> None:
    """The mutant this kills: dropping `AND status = 'pending'` from the UPDATE.

    Without it both callers get a row back and both ingest the same merchant.
    """
    job_id = await _make_job(f"{_PREFIX}_b")

    first = await claim_catalog_sync_job(job_id)
    second = await claim_catalog_sync_job(job_id)

    assert first is not None
    assert second is None, "a second caller claimed a job that was already running"


async def test_concurrent_claims_do_not_both_win() -> None:
    job_id = await _make_job(f"{_PREFIX}_c")

    results = await asyncio.gather(*(claim_catalog_sync_job(job_id) for _ in range(4)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} runners claimed the same job"


async def test_a_completed_job_cannot_be_claimed_again() -> None:
    job_id = await _make_job(f"{_PREFIX}_d")
    await database.execute(
        "UPDATE catalog_sync_jobs SET status = 'completed' WHERE job_id = :j", {"j": job_id}
    )

    assert await claim_catalog_sync_job(job_id) is None


async def test_claim_next_takes_the_oldest_pending_job_and_skips_running_ones() -> None:
    older = await _make_job(f"{_PREFIX}_e1")
    newer = await _make_job(f"{_PREFIX}_e2")
    # Order by created_at explicitly; two rows written in the same tick would
    # otherwise leave the "oldest" assertion resting on insertion order.
    await database.execute(
        "UPDATE catalog_sync_jobs SET created_at = :t WHERE job_id = :j",
        {"t": datetime.utcnow() - timedelta(hours=1), "j": older},
    )

    first = await claim_next_catalog_sync_job()
    assert first is not None and first["job_id"] == older

    second = await claim_next_catalog_sync_job()
    assert second is not None and second["job_id"] == newer

    # Nothing pending is left — the two running rows must NOT be re-claimed.
    assert await claim_next_catalog_sync_job() is None


async def test_claim_next_on_an_empty_queue_returns_none() -> None:
    assert await claim_next_catalog_sync_job() is None


async def test_requeue_puts_a_running_job_back_and_leaves_others_alone() -> None:
    job_id = await _make_job(f"{_PREFIX}_f")
    await claim_catalog_sync_job(job_id)

    assert await requeue_catalog_sync_job(job_id) is True
    row = await get_catalog_sync_job(job_id)
    assert row["status"] == "pending"
    assert row["started_at"] is None

    # Already pending — a second requeue must be a no-op, not a status rewrite.
    assert await requeue_catalog_sync_job(job_id) is False


async def test_stale_running_rows_are_requeued_and_fresh_ones_are_not() -> None:
    """A row stranded by a dead process is recoverable; a live run is untouched.

    The `started_at < cutoff` predicate is the whole guard: without it this
    would requeue jobs that are still working and duplicate their ingest.
    """
    stale = await _make_job(f"{_PREFIX}_g_stale")
    fresh = await _make_job(f"{_PREFIX}_g_fresh")
    await claim_catalog_sync_job(stale)
    await claim_catalog_sync_job(fresh)
    await database.execute(
        "UPDATE catalog_sync_jobs SET started_at = :t WHERE job_id = :j",
        {"t": datetime.utcnow() - timedelta(hours=6), "j": stale},
    )

    requeued = await requeue_stale_catalog_sync_jobs(stale_after_seconds=3600)

    assert requeued == 1
    assert (await get_catalog_sync_job(stale))["status"] == "pending"
    assert (await get_catalog_sync_job(fresh))["status"] == "running"


async def test_stale_requeue_never_touches_a_pending_job() -> None:
    """`pending` rows have no `started_at`; only `running` rows are recoverable."""
    job_id = await _make_job(f"{_PREFIX}_h")

    assert await requeue_stale_catalog_sync_jobs(stale_after_seconds=3600) == 0
    assert (await get_catalog_sync_job(job_id))["status"] == "pending"
