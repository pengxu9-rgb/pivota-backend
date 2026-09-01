"""Out-of-band drainer for the `catalog_sync_jobs` queue.

WHY THIS EXISTS. A catalog ingest used to be driven by whoever created the job
row — either inline in the request, or from a FastAPI `BackgroundTasks` task.
`BackgroundTasks` runs AFTER the response has been sent, inside the same
process, with no supervision:

  * nothing retries it. If the task raises, the exception surfaces in the ASGI
    handler long after the caller received `200 {"catalog_ingest_queued": true}`,
    and the only durable trace is `catalog_sync_jobs.status='failed'` — which is
    exactly how the 2026-08-29 "second merchant ingested zero rows" incident was
    found;
  * nothing survives the process. A Cloud Run revision swap, a scale-down or an
    OOM between the response and the task's completion drops the work silently
    and strands the row in `pending` (never started) or `running` (started,
    never finished), where no later boot goes looking for it;
  * the caller gets no handle. A 200 was returned whether the ingest later
    succeeded or failed.

This tick is the runner the work should always have had. It is the same shape as
the quality-backfill drain (`services.product_quality_backfill_service.
process_next_quality_backfill_job`), registered in `services.audit_scheduler`
alongside it, and it drains the queue that `create_catalog_sync_job` writes. Job
creation stays in the request; running it does not, and the caller polls
`GET /v1/catalog/sync/jobs/{job_id}` for the outcome.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from services.catalog_sync_service import (
    claim_next_catalog_sync_job,
    requeue_stale_catalog_sync_jobs,
    run_claimed_catalog_sync_job,
)


logger = logging.getLogger(__name__)


# Jobs drained per tick, serially. A catalog ingest walks a merchant's whole
# products_cache and writes the catalog tree, so a handful per 30s tick is
# already well ahead of how fast merchants press "Sync products"; the cap keeps
# one tick bounded well inside the scheduler's run deadline.
DEFAULT_MAX_JOBS_PER_TICK = 3

# A run stranded in `running` past this is assumed dead (the process that
# claimed it is gone) and goes back to `pending`. Must stay comfortably LONGER
# than the slowest legitimate run, or this would requeue jobs that are still
# working and duplicate their ingest.
DEFAULT_STALE_AFTER_SECONDS = 3600


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("catalog_sync_drain: %s=%r is not an int; using %d", name, raw, default)
        return default
    return value if value > 0 else default


async def run_catalog_sync_drain_tick() -> List[Dict[str, Any]]:
    """Claim and run up to N pending catalog sync jobs. Returns the finished rows.

    Never raises for a job-level failure: `run_claimed_catalog_sync_job` has
    already recorded `status='failed'` and `error_message` on the row, and a
    raise here would take down the whole tick and leave the remaining queue
    undrained.

    A deadline cut still stops the tick: the scheduler cancels an over-running
    job, and `asyncio.CancelledError` is a BaseException, so the `except
    Exception` below does not catch it. The job runner requeues its own row on
    the way out.
    """
    max_jobs = _int_env("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", DEFAULT_MAX_JOBS_PER_TICK)
    stale_after = _int_env("CATALOG_SYNC_DRAIN_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS)

    # Recover rows a dead process left mid-flight, BEFORE claiming new work —
    # otherwise a stranded row waits behind the whole live queue.
    try:
        requeued = await requeue_stale_catalog_sync_jobs(stale_after_seconds=stale_after)
        if requeued:
            logger.warning(
                "catalog_sync_drain: requeued %d catalog sync job(s) stranded in "
                "running for more than %ds", requeued, stale_after,
            )
    except Exception:  # noqa: BLE001 - recovery must never stop the drain
        logger.warning("catalog_sync_drain: stale requeue failed", exc_info=True)

    processed: List[Dict[str, Any]] = []
    for _ in range(max_jobs):
        claimed: Optional[Dict[str, Any]] = await claim_next_catalog_sync_job()
        if claimed is None:
            break

        job_id = str(claimed.get("job_id") or "")
        merchant_id = str(claimed.get("merchant_id") or "")
        logger.info(
            "catalog_sync_drain: running job=%s merchant=%s connector=%s",
            job_id, merchant_id, claimed.get("connector"),
        )
        try:
            result = await run_claimed_catalog_sync_job(claimed)
            processed.append(result)
        except Exception:  # noqa: BLE001 - the row carries the failure
            logger.exception(
                "catalog_sync_drain: job=%s merchant=%s failed", job_id, merchant_id,
            )
            processed.append({"job_id": job_id, "merchant_id": merchant_id, "status": "failed"})

    return processed
