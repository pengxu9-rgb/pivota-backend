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

This tick is the runner the work should always have had. It drains the queue
that `create_catalog_sync_job` writes and is registered in
`services.audit_scheduler` with the same shape as the lanes that already solved
this problem for their own tables: `catalog_import_drain_tick` +
`catalog_import_stale_reaper` (platform_import_tasks) and
`quality_backfill_drain_tick` (the queue this one feeds). Job creation stays in
the request; running it does not, and the caller polls
`GET /v1/catalog/sync/jobs/{job_id}` for the outcome.

Two ticks, as in the catalog-import lane:

  * `run_catalog_sync_drain_tick` — claims and runs ONE pending job per fire.
    Gated by the CATALOG_SYNC_DRAIN_ENABLED kill switch (ON by default).
  * `run_catalog_sync_stale_reaper_tick` — returns rows abandoned in `running`
    by a dead process to `pending`. Its own job because the drain tick can be
    busy for up to its whole deadline and recovery must not queue behind it;
    NOT gated on the kill switch, because flipping the switch is a new revision
    and that restart is exactly what strands a row.
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


# Jobs drained per tick, serially. ONE, like `catalog_import_drain_tick`: a job
# may re-pull the merchant's whole Shopify catalog before ingesting it, and the
# scheduler's run deadline bounds the TICK, not the job — so a second or third
# job in the same tick inherits whatever time the first one used and is the one
# that gets cut. One per 30s is 120/hour, far above how often anything enqueues
# (production: 3 `merchant_products_sync` jobs in total, the last on
# 2026-08-29).
DEFAULT_MAX_JOBS_PER_TICK = 1

# A run stranded in `running` past this (measured from its CLAIM) is assumed
# dead and goes back to `pending`. There is no heartbeat, so this must stay
# LONGER than any run the scheduler still permits — the tick's run deadline
# (`catalog_sync_drain_tick` in services.audit_scheduler._JOB_RUN_DEADLINES) plus
# the cancel grace a cut run gets to unwind. A shorter window would requeue a
# job that is still ingesting, and the second runner would ingest the same
# merchant concurrently. Twice the deadline also covers a run that was cut but
# did not unwind (an abandoned "zombie") for another full hour.
# tests/services/test_catalog_sync_drain.py pins the inequality.
DEFAULT_STALE_AFTER_SECONDS = 7200

DEFAULT_STALE_REQUEUE_LIMIT = 5


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


def catalog_sync_drain_enabled() -> bool:
    """CATALOG_SYNC_DRAIN_ENABLED is a KILL SWITCH, ON by default.

    The default lives in code, not in an env var, for the reason
    jobs.catalog_import_worker._catalog_import_drain_enabled gives: a deploy has
    silently wiped plain env vars before (2026-08-30), and a fix that is armed
    by an env var is disarmed by the next such deploy without anyone noticing.
    This lane only runs rows that a merchant, an admin or a Shopify webhook
    explicitly enqueued — and that used to run immediately in a BackgroundTask —
    so there is nothing for "dormant" to protect.

    Off means rows accumulate `pending` (nothing runs them); set it back to
    true and the drain resumes oldest-first.
    """
    raw = (os.getenv("CATALOG_SYNC_DRAIN_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


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
    if not catalog_sync_drain_enabled():
        return []
    max_jobs = _int_env("CATALOG_SYNC_DRAIN_MAX_JOBS_PER_TICK", DEFAULT_MAX_JOBS_PER_TICK)

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


async def run_catalog_sync_stale_reaper_tick() -> int:
    """Return `running` rows abandoned by a dead process to `pending`.

    A run cut by its DEADLINE recovers itself (the runner requeues on
    CancelledError). A process that simply dies — revision swap, scale-down,
    OOM — never reaches that handler, and without this the row would say
    `running` forever. Rows past the give-up window
    (services.catalog_sync_service.CATALOG_SYNC_GIVE_UP_AFTER_SECONDS) are
    failed instead, so a job that reliably kills its process is not re-run
    forever.

    Not gated on CATALOG_SYNC_DRAIN_ENABLED — see the module docstring. It makes
    no platform calls.
    """
    stale_after = _int_env("CATALOG_SYNC_DRAIN_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS)
    requeued = await requeue_stale_catalog_sync_jobs(
        stale_after_seconds=stale_after,
        limit=DEFAULT_STALE_REQUEUE_LIMIT,
    )
    if requeued:
        logger.warning(
            "catalog_sync_drain: requeued %d catalog sync job(s) stranded in "
            "running for more than %ds", requeued, stale_after,
        )
    return requeued
