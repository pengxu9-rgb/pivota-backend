"""The catalog ingest runs out of band, not in a post-response BackgroundTask.

The 2026-08-29 incident: a second merchant's catalog sync wrote zero rows, and
the only trace was `catalog_sync_jobs.status='failed'` — because the ingest was
handed to FastAPI's `BackgroundTasks`, which runs after the response is already
sent, is never retried, and dies with the process. These tests pin the runner
that replaced it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.catalog_sync_drain as drain


def _job(job_id: str, merchant_id: str = "merch_x") -> Dict[str, Any]:
    return {
        "job_id": job_id,
        "merchant_id": merchant_id,
        "connector": "shopify",
        "mode": "reconcile",
        "scope_json": {"platform": "shopify"},
        "status": "running",
    }


def _install(monkeypatch, *, queue: List[Dict[str, Any]], run) -> List[str]:
    """Wire the drain to an in-memory queue. Returns the requeue-call log."""
    requeued: List[str] = []

    async def _claim_next() -> Optional[Dict[str, Any]]:
        return queue.pop(0) if queue else None

    async def _requeue_stale(**_kwargs) -> int:
        requeued.append("stale")
        return 0

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)
    monkeypatch.setattr(drain, "run_claimed_catalog_sync_job", run)
    return requeued


async def test_drain_runs_queued_jobs(monkeypatch) -> None:
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        return {**job, "status": "completed"}

    queue = [_job("csj_1"), _job("csj_2")]
    _install(monkeypatch, queue=queue, run=_run)

    processed = await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_1", "csj_2"]
    assert [p["status"] for p in processed] == ["completed", "completed"]


async def test_drain_stops_when_the_queue_is_empty(monkeypatch) -> None:
    """An empty queue must cost one claim, not `max_jobs` of them."""
    claims = {"n": 0}

    async def _claim_next():
        claims["n"] += 1
        return None

    async def _requeue_stale(**_kwargs):
        return 0

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)

    assert await drain.run_catalog_sync_drain_tick() == []
    assert claims["n"] == 1


async def test_one_failing_job_does_not_abandon_the_rest_of_the_queue(monkeypatch) -> None:
    """The failure mode the incident had: one bad merchant must not stop the others.

    `run_claimed_catalog_sync_job` re-raises after recording `status='failed'`
    on the row, so the drain has to absorb that or a single poisoned job would
    leave every later merchant's ingest undrained until the next tick — and
    forever, if it is always claimed first.
    """
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        if job["job_id"] == "csj_bad":
            raise RuntimeError("duplicate key value violates unique constraint")
        return {**job, "status": "completed"}

    queue = [_job("csj_bad"), _job("csj_good")]
    _install(monkeypatch, queue=queue, run=_run)

    processed = await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_bad", "csj_good"]
    assert [p["status"] for p in processed] == ["failed", "completed"]


async def test_cancellation_propagates_and_stops_the_tick(monkeypatch) -> None:
    """A deadline cut must stop the tick, not be recorded as a job failure.

    The scheduler bounds every run and cancels on expiry. Broadening the drain's
    `except Exception` to `except BaseException` would swallow that and keep
    claiming work past the deadline.
    """
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        raise asyncio.CancelledError()

    queue = [_job("csj_1"), _job("csj_2")]
    _install(monkeypatch, queue=queue, run=_run)

    with pytest.raises(asyncio.CancelledError):
        await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_1"], "the tick kept claiming work after being cancelled"


async def test_stale_running_rows_are_recovered_before_new_work(monkeypatch) -> None:
    """Rows stranded in `running` by a dead process are the BackgroundTask legacy.

    Nothing used to recover them: the task died with its process and the row
    said `running` forever. Recovery has to run BEFORE claiming, or a stranded
    row waits behind the entire live queue.
    """
    order: List[str] = []

    async def _claim_next():
        order.append("claim")
        return None

    async def _requeue_stale(*, stale_after_seconds):
        order.append(f"requeue:{stale_after_seconds}")
        return 2

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)

    await drain.run_catalog_sync_drain_tick()

    assert order == [f"requeue:{drain.DEFAULT_STALE_AFTER_SECONDS}", "claim"]


async def test_a_failing_stale_recovery_does_not_stop_the_drain(monkeypatch) -> None:
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        return {**job, "status": "completed"}

    async def _requeue_stale(**_kwargs):
        raise RuntimeError("db hiccup")

    queue = [_job("csj_1")]
    _install(monkeypatch, queue=queue, run=_run)
    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)

    processed = await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_1"]
    assert [p["status"] for p in processed] == ["completed"]


async def test_max_jobs_per_tick_is_honoured(monkeypatch) -> None:
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        return {**job, "status": "completed"}

    queue = [_job(f"csj_{i}") for i in range(10)]
    _install(monkeypatch, queue=queue, run=_run)
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "2")

    await drain.run_catalog_sync_drain_tick()

    assert len(ran) == 2
    assert len(queue) == 8, "the rest of the queue must stay claimable"


async def test_a_junk_max_jobs_env_falls_back_to_the_default(monkeypatch) -> None:
    """A typo'd env var must not silently drain zero jobs per tick."""
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "banana")
    assert drain._int_env("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", 3) == 3

    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "0")
    assert drain._int_env("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", 3) == 3


def test_the_drain_tick_is_registered_on_the_scheduler() -> None:
    """The runner only counts if something actually fires it.

    Pins the tick id against `_JOB_RUN_DEADLINES`, which `start_scheduler` uses
    to bound every run — a job registered without an entry there is a boot-time
    failure in tests/test_scheduler_job_isolation.py.
    """
    import services.audit_scheduler as sched

    assert "catalog_sync_drain_tick" in sched._JOB_RUN_DEADLINES
    assert sched.run_deadline_for("catalog_sync_drain_tick") > 0


# --- the runner's own cancellation handling --------------------------------


async def test_a_cancelled_run_requeues_its_row_instead_of_stranding_it(monkeypatch) -> None:
    """`running` is a dead end: no drainer ever looks at it again.

    So a run cut by the scheduler deadline has to put its own row back to
    `pending` before the cancellation propagates. This is the branch that keeps
    a cut ingest from leaving the merchant with an empty catalog forever.
    """
    import services.catalog_sync_service as css

    requeued: List[str] = []

    async def _sync(**_kwargs):
        raise asyncio.CancelledError()

    async def _requeue(job_id):
        requeued.append(job_id)
        return True

    async def _upsert(*_a, **_k):
        raise AssertionError("a cancelled run must not be recorded as 'failed'")

    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _sync)
    monkeypatch.setattr(css, "requeue_catalog_sync_job", _requeue)
    monkeypatch.setattr(css, "_upsert_by_pk", _upsert)

    with pytest.raises(asyncio.CancelledError):
        await css.run_claimed_catalog_sync_job(_job("csj_cut"))

    assert requeued == ["csj_cut"]


async def test_a_failing_requeue_does_not_swallow_the_cancellation(monkeypatch) -> None:
    import services.catalog_sync_service as css

    async def _sync(**_kwargs):
        raise asyncio.CancelledError()

    async def _requeue(_job_id):
        raise RuntimeError("db gone")

    monkeypatch.setattr(css, "sync_products_cache_to_catalog", _sync)
    monkeypatch.setattr(css, "requeue_catalog_sync_job", _requeue)

    with pytest.raises(asyncio.CancelledError):
        await css.run_claimed_catalog_sync_job(_job("csj_cut2"))
