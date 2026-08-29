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

from db.catalog import catalog_sync_events, catalog_sync_jobs
from db.database import database
from services.catalog_sync_service import (
    CATALOG_SYNC_GAVE_UP_ERROR,
    CATALOG_SYNC_GIVE_UP_AFTER_SECONDS,
    claim_catalog_sync_job,
    claim_next_catalog_sync_job,
    create_catalog_sync_job,
    get_catalog_sync_job,
    record_catalog_sync_event,
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
    await ensure_model_tables((catalog_sync_jobs, catalog_sync_events))
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


async def _reset() -> None:
    for table in ("catalog_sync_jobs", "catalog_sync_events"):
        await database.execute(
            f"DELETE FROM {table} WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
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


async def _age(job_id: str, *, created_hours: float, started_hours: float | None = None) -> None:
    now = datetime.utcnow()
    await database.execute(
        "UPDATE catalog_sync_jobs SET created_at = :c WHERE job_id = :j",
        {"c": now - timedelta(hours=created_hours), "j": job_id},
    )
    if started_hours is not None:
        await database.execute(
            "UPDATE catalog_sync_jobs SET started_at = :s WHERE job_id = :j",
            {"s": now - timedelta(hours=started_hours), "j": job_id},
        )


async def test_claim_next_skips_a_merchant_that_already_has_a_running_job() -> None:
    """Two concurrent ingests of ONE merchant race on the same child rows.

    The mutant this kills: dropping the NOT EXISTS from the candidate query —
    the drain would then start the merchant's second job while the first (an
    inline runner, or a not-yet-reaped zombie) is still writing.
    """
    busy_running = await _make_job(f"{_PREFIX}_i_busy")
    busy_pending = await _make_job(f"{_PREFIX}_i_busy")
    other = await _make_job(f"{_PREFIX}_i_other")
    await _age(busy_pending, created_hours=2)  # oldest pending — would win on age alone
    await claim_catalog_sync_job(busy_running)

    claimed = await claim_next_catalog_sync_job()

    assert claimed is not None and claimed["job_id"] == other
    assert (await get_catalog_sync_job(busy_pending))["status"] == "pending"
    # Nothing else is claimable while both merchants have a run in flight.
    assert await claim_next_catalog_sync_job() is None


async def test_a_job_created_claimed_is_never_offered_to_the_drain() -> None:
    """`claimed=True` is the inline-runner path (jobs/agentic_commerce_reconciliation):
    born `running`, so there is no window in which the drain can take it first."""
    job = await create_catalog_sync_job(
        merchant_id=f"{_PREFIX}_j",
        connector="shopify",
        mode="reconcile",
        scope={"platform": "shopify"},
        requested_by="test",
        claimed=True,
    )
    assert job["status"] == "running"
    assert job["started_at"] is not None
    assert await claim_next_catalog_sync_job() is None
    assert await claim_catalog_sync_job(str(job["job_id"])) is None


async def test_a_cancelled_requeue_records_why_and_a_fresh_claim_clears_it() -> None:
    job_id = await _make_job(f"{_PREFIX}_k")
    await claim_catalog_sync_job(job_id)

    assert await requeue_catalog_sync_job(job_id) is True
    assert (await get_catalog_sync_job(job_id))["error_message"] == "cancelled_catalog_sync_job_requeued"

    reclaimed = await claim_catalog_sync_job(job_id)
    assert reclaimed is not None and reclaimed["error_message"] is None


async def test_a_job_past_the_give_up_window_is_failed_not_requeued() -> None:
    """The poison-pill bound. Without it a job that always outlives the deadline
    (or kills its process) is re-pulled and re-ingested forever."""
    job_id = await _make_job(f"{_PREFIX}_l")
    await claim_catalog_sync_job(job_id)
    await _age(job_id, created_hours=CATALOG_SYNC_GIVE_UP_AFTER_SECONDS / 3600 + 1)

    assert await requeue_catalog_sync_job(job_id) is False

    row = await get_catalog_sync_job(job_id)
    assert row["status"] == "failed"
    assert row["error_message"] == CATALOG_SYNC_GAVE_UP_ERROR
    assert row["completed_at"] is not None
    assert await claim_next_catalog_sync_job() is None


async def test_a_job_inside_the_give_up_window_is_requeued() -> None:
    """The other side of the bound: the window must not fail a young job."""
    job_id = await _make_job(f"{_PREFIX}_m")
    await claim_catalog_sync_job(job_id)
    await _age(job_id, created_hours=CATALOG_SYNC_GIVE_UP_AFTER_SECONDS / 3600 - 1)

    assert await requeue_catalog_sync_job(job_id) is True
    assert (await get_catalog_sync_job(job_id))["status"] == "pending"


async def test_the_stale_reaper_gives_up_on_an_old_job_and_does_not_count_it() -> None:
    old = await _make_job(f"{_PREFIX}_n_old")
    young = await _make_job(f"{_PREFIX}_n_young")
    for job_id in (old, young):
        await claim_catalog_sync_job(job_id)
    await _age(old, created_hours=CATALOG_SYNC_GIVE_UP_AFTER_SECONDS / 3600 + 1, started_hours=3)
    await _age(young, created_hours=3, started_hours=3)

    requeued = await requeue_stale_catalog_sync_jobs(stale_after_seconds=3600)

    assert requeued == 1
    old_row = await get_catalog_sync_job(old)
    assert old_row["status"] == "failed" and old_row["error_message"] == CATALOG_SYNC_GAVE_UP_ERROR
    young_row = await get_catalog_sync_job(young)
    assert young_row["status"] == "pending"
    assert young_row["error_message"] == "stale_catalog_sync_job_requeued"


async def test_giving_up_on_a_webhook_job_settles_its_sync_event() -> None:
    """A webhook job carries the `catalog_sync_events` row that produced it; a
    job that is given up on must close that row too, or the event says
    `pending` forever — the exact residue the BackgroundTask shape left."""
    merchant = f"{_PREFIX}_o"
    event = await record_catalog_sync_event(
        merchant_id=merchant,
        connector="shopify",
        event_type="product_webhook",
        topic="products/update",
        payload_json={},
        source_ref=f"{merchant}:evt",
    )
    job = await create_catalog_sync_job(
        merchant_id=merchant,
        connector="shopify",
        mode="webhook",
        scope={"platform": "shopify", "catalog_sync_event_id": event["event_id"]},
        requested_by="shopify_webhook",
    )
    job_id = str(job["job_id"])
    await claim_catalog_sync_job(job_id)
    await _age(job_id, created_hours=CATALOG_SYNC_GIVE_UP_AFTER_SECONDS / 3600 + 1)

    assert await requeue_catalog_sync_job(job_id) is False

    row = await database.fetch_one(
        "SELECT status, error_message FROM catalog_sync_events WHERE event_id = :e",
        {"e": event["event_id"]},
    )
    assert dict(row)["status"] == "failed"
    assert dict(row)["error_message"] == CATALOG_SYNC_GAVE_UP_ERROR


async def _fail_through_a_real_claim(monkeypatch, merchant: str) -> str:
    """Enqueue, claim the way the drain does (raw `RETURNING *` row), and let the
    ingest raise — no mocked claim, no mocked failure write."""
    import services.catalog_sync_service as css

    job = await create_catalog_sync_job(
        merchant_id=merchant,
        connector="shopify",
        mode="reconcile",
        scope={"platform": "shopify", "limit": 7},
        requested_by="test",
    )

    async def _boom(**_kwargs):
        raise RuntimeError("duplicate key value violates unique constraint")

    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _boom)
    claimed = await claim_next_catalog_sync_job()
    assert claimed is not None and claimed["job_id"] == job["job_id"]
    with pytest.raises(RuntimeError):
        await css.run_claimed_catalog_sync_job(claimed)
    return str(job["job_id"])


async def test_a_job_that_fails_after_a_real_claim_is_failed_and_still_pollable(monkeypatch) -> None:
    """The regression the review caught: the failure path wrote the claim's raw
    row back through the table. On Postgres that re-encoded the JSONB columns as
    JSON strings and the poll route 500'd on the failed job; on SQLite the
    write raised and the row stayed `running`."""
    from routes.catalog_routes import _job_response

    job_id = await _fail_through_a_real_claim(monkeypatch, f"{_PREFIX}_p")

    row = await get_catalog_sync_job(job_id)
    assert row["status"] == "failed"
    assert "duplicate key" in row["error_message"]
    assert row["completed_at"] is not None
    assert isinstance(row["scope_json"], dict) and row["scope_json"]["limit"] == 7
    assert isinstance(row["stats_json"], dict)
    response = _job_response(row)  # the model GET /v1/catalog/sync/jobs/{id} returns
    assert response.status == "failed"


async def test_the_failure_write_does_not_overwrite_a_row_that_is_no_longer_running(monkeypatch) -> None:
    """If the ingest already stamped the row `completed` (or the reaper requeued
    it and another runner holds it), a late failure must not rewrite it."""
    import services.catalog_sync_service as css

    job_id = await _make_job(f"{_PREFIX}_q")
    claimed = await claim_catalog_sync_job(job_id)

    async def _complete_then_raise(**_kwargs):
        await database.execute(
            "UPDATE catalog_sync_jobs SET status = 'completed' WHERE job_id = :j", {"j": job_id}
        )
        raise RuntimeError("raised after the ingest's own completion write")

    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _complete_then_raise)
    with pytest.raises(RuntimeError):
        await css.run_claimed_catalog_sync_job(claimed)

    assert (await get_catalog_sync_job(job_id))["status"] == "completed"
