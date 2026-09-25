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


def _install(monkeypatch, *, queue: List[Dict[str, Any]], run) -> None:
    """Wire the drain to an in-memory queue."""

    async def _claim_next() -> Optional[Dict[str, Any]]:
        return queue.pop(0) if queue else None

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setattr(drain, "run_claimed_catalog_sync_job", run)
    monkeypatch.delenv("CATALOG_SYNC_DRAIN_ENABLED", raising=False)
    monkeypatch.delenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", raising=False)


async def test_drain_runs_one_queued_job_per_tick_by_default(monkeypatch) -> None:
    """One per fire, like catalog_import_drain_tick: the run deadline bounds the
    TICK, so a second job in the same tick would inherit the first one's time."""
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        return {**job, "status": "completed"}

    queue = [_job("csj_1"), _job("csj_2")]
    _install(monkeypatch, queue=queue, run=_run)

    processed = await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_1"]
    assert [p["status"] for p in processed] == ["completed"]
    assert [j["job_id"] for j in queue] == ["csj_2"], "the rest must stay claimable"

    await drain.run_catalog_sync_drain_tick()
    assert ran == ["csj_1", "csj_2"]


async def test_drain_stops_when_the_queue_is_empty(monkeypatch) -> None:
    """An empty queue must cost one claim, not `max_jobs` of them."""
    claims = {"n": 0}

    async def _claim_next():
        claims["n"] += 1
        return None

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "5")

    assert await drain.run_catalog_sync_drain_tick() == []
    assert claims["n"] == 1


async def test_one_failing_job_does_not_abandon_the_rest_of_the_queue(monkeypatch) -> None:
    """The failure mode the incident had: one bad merchant must not stop the others.

    `run_claimed_catalog_sync_job` re-raises after recording `status='failed'`
    on the row, so the drain has to absorb that or a single poisoned job would
    take the whole tick down with it.
    """
    ran: List[str] = []

    async def _run(job):
        ran.append(str(job["job_id"]))
        if job["job_id"] == "csj_bad":
            raise RuntimeError("duplicate key value violates unique constraint")
        return {**job, "status": "completed"}

    queue = [_job("csj_bad"), _job("csj_good")]
    _install(monkeypatch, queue=queue, run=_run)
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "2")

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
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", "2")

    with pytest.raises(asyncio.CancelledError):
        await drain.run_catalog_sync_drain_tick()

    assert ran == ["csj_1"], "the tick kept claiming work after being cancelled"


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


# --- kill switch ------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "true", "1", "on", "garbage"])
def test_the_drain_is_on_unless_explicitly_switched_off(monkeypatch, raw) -> None:
    """ON by default: an env wipe must not silently disarm the drain."""
    if raw:
        monkeypatch.setenv("CATALOG_SYNC_DRAIN_ENABLED", raw)
    else:
        monkeypatch.delenv("CATALOG_SYNC_DRAIN_ENABLED", raising=False)
    assert drain.catalog_sync_drain_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "OFF"])
async def test_the_kill_switch_stops_the_drain_before_it_claims(monkeypatch, raw) -> None:
    claims = {"n": 0}

    async def _claim_next():
        claims["n"] += 1
        return _job("csj_should_not_run")

    async def _run(_job):
        raise AssertionError("ran a job with the kill switch off")

    monkeypatch.setattr(drain, "claim_next_catalog_sync_job", _claim_next)
    monkeypatch.setattr(drain, "run_claimed_catalog_sync_job", _run)
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_ENABLED", raw)

    assert await drain.run_catalog_sync_drain_tick() == []
    assert claims["n"] == 0, "a disabled drain still claimed a row (it would strand it running)"


# --- stale reaper -------------------------------------------------------------


async def test_the_reaper_requeues_with_the_default_window(monkeypatch) -> None:
    calls: List[Dict[str, Any]] = []

    async def _requeue_stale(**kwargs):
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)
    monkeypatch.delenv("CATALOG_SYNC_DRAIN_STALE_AFTER_SECONDS", raising=False)

    assert await drain.run_catalog_sync_stale_reaper_tick() == 2
    assert calls == [{
        "stale_after_seconds": drain.DEFAULT_STALE_AFTER_SECONDS,
        "limit": drain.DEFAULT_STALE_REQUEUE_LIMIT,
    }]


async def test_the_reaper_runs_with_the_kill_switch_off(monkeypatch) -> None:
    """Flipping the switch is a new revision, and that restart is what strands
    a row in `running`. Gating recovery on the same switch would strand it."""
    calls = {"n": 0}

    async def _requeue_stale(**_kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(drain, "requeue_stale_catalog_sync_jobs", _requeue_stale)
    monkeypatch.setenv("CATALOG_SYNC_DRAIN_ENABLED", "false")

    await drain.run_catalog_sync_stale_reaper_tick()
    assert calls["n"] == 1


def test_the_stale_window_outlives_every_run_the_scheduler_permits() -> None:
    """No heartbeat: the reaper measures from the CLAIM. If its window were not
    longer than the tick's deadline plus the cancel grace, it would requeue a
    run that is still ingesting and a second runner would ingest the same
    merchant concurrently. Raising the deadline without the window fails here.
    """
    import services.audit_scheduler as sched
    from services.scheduler_job_runner import DEFAULT_CANCEL_GRACE_SECONDS

    deadline = sched.run_deadline_for("catalog_sync_drain_tick")
    assert drain.DEFAULT_STALE_AFTER_SECONDS > deadline + DEFAULT_CANCEL_GRACE_SECONDS


def test_the_give_up_window_allows_more_than_one_stale_recovery() -> None:
    """Giving up before the reaper has had a chance to retry once would turn
    every process death into a permanent failure."""
    from services.catalog_sync_service import CATALOG_SYNC_GIVE_UP_AFTER_SECONDS

    assert CATALOG_SYNC_GIVE_UP_AFTER_SECONDS > 2 * drain.DEFAULT_STALE_AFTER_SECONDS


# --- registration -------------------------------------------------------------


class _RecordingScheduler:
    def __init__(self, *a, **k):
        self.added: List[Dict[str, Any]] = []

    def add_job(self, func, *a, **k):
        self.added.append({"id": k.get("id"), "func": func, "args": a, "kwargs": k})

    def start(self):
        pass

    def get_jobs(self):
        return []


async def test_both_ticks_are_registered_on_the_scheduler(monkeypatch) -> None:
    """The runner only counts if something actually fires it."""
    import services.audit_scheduler as sched

    for k in ("AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", "web")
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    rec = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)

    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    by_id = {j["id"]: j for j in rec.added}

    tick = by_id["catalog_sync_drain_tick"]
    assert tick["func"].__wrapped__ is drain.run_catalog_sync_drain_tick
    assert tick["args"] == ("interval",) and tick["kwargs"]["seconds"] == 30
    assert tick["kwargs"]["max_instances"] == 1

    reaper = by_id["catalog_sync_stale_reaper"]
    assert reaper["func"].__wrapped__ is drain.run_catalog_sync_stale_reaper_tick
    assert reaper["args"] == ("interval",) and reaper["kwargs"]["seconds"] == 300


# --- the runner's own cancellation handling --------------------------------


async def test_a_cancelled_run_requeues_its_row_instead_of_stranding_it(monkeypatch) -> None:
    """A run cut by the scheduler deadline puts its own row back to `pending`
    before the cancellation propagates, rather than leaving it `running` until
    the stale reaper's window (hours) elapses.
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
