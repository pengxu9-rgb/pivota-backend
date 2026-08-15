"""Admin endpoint to resume/pause a small allowlist of scheduler jobs.

The partner-settlement crons (T7 invoice generation, T8 partner settlement) are
registered *paused* (`next_run_time=None`) in services.audit_scheduler. Stage-4
promotion needs to resume them, and rollback needs to re-pause them — without a
redeploy and in an auditable way. This endpoint does exactly that, restricted to
an allowlist of settlement-related job ids so it can never touch unrelated crons.

Read-only listing lives at GET /__scheduler_health; this router adds the two
state-changing verbs behind admin auth.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from utils.auth import require_admin


logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin - Scheduler"])

# Only these jobs may be resumed/paused through this endpoint. Adding a job here
# is a deliberate act — it must be safe to start/stop from an operator call.
_MANAGEABLE_JOB_IDS = frozenset(
    {
        "invoice_generation_monthly",   # T7
        "partner_settlement_monthly",   # T8
        "settlement_file_generate",     # PR #8 day-5
        "settlement_file_transfer",     # PR #8 day-10 (Stripe Connect transfers)
    }
)


# Jobs an operator may force to run out-of-band (issue #1754). Each run goes
# through the same isolated, deadline-bounded path as the scheduler, so it is
# safe even while a scheduled run is wedged. All of these are idempotent
# sweeps/reapers; the money-path reconciler is the reason this exists.
_RUNNABLE_JOB_IDS = frozenset(
    {
        "payment_reconcile_tick",
        "stamp_attribution_reaper",
        "metering_expire_reservations",
        "audit_run_lease_reaper",
        "audit_run_abandoned_reaper",
        "executor_run_lease_reaper",
        "verification_run_lease_reaper",
        "external_conversion_poll",
    }
)


def _job_state(job: Any) -> dict[str, Any]:
    next_run = getattr(job, "next_run_time", None)
    return {
        "id": job.id,
        "paused": next_run is None,
        "next_run_time": next_run.isoformat() if next_run is not None else None,
        "trigger": str(getattr(job, "trigger", "")),
    }


@router.get("/admin/scheduler/jobs", response_model=None)
async def list_managed_scheduler_jobs(
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """List the manageable settlement jobs and their paused/next-run state."""

    from services.audit_scheduler import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None or not getattr(scheduler, "running", False):
        return JSONResponse(
            status_code=503,
            content={"error": "scheduler_not_running"},
        )

    jobs = []
    for job_id in sorted(_MANAGEABLE_JOB_IDS):
        job = scheduler.get_job(job_id)
        if job is not None:
            jobs.append(_job_state(job))
        else:
            jobs.append({"id": job_id, "paused": None, "next_run_time": None, "registered": False})
    return {"jobs": jobs}


# NOTE: these two are declared BEFORE the generic `/{job_id}/{action}` route
# below, which would otherwise capture "run-now" / "cancel-running" as an
# invalid action.
@router.get("/admin/scheduler/runs", response_model=None)
async def list_scheduler_runs(
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Per-job run registry WITH last error text (the public
    /__scheduler_health carries the same minus error text)."""
    from services.scheduler_job_runner import registry_snapshot, stalled_jobs

    return {"runs": registry_snapshot(include_error_text=True), "stalled": stalled_jobs()}


@router.post("/admin/scheduler/jobs/{job_id}/run-now", response_model=None)
async def run_scheduler_job_now(
    job_id: str,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Force ONE out-of-band run of an allowlisted job (issue #1754 — the
    payment reconciler must be forceable when its scheduled runs are wedged).
    Runs through the isolated, deadline-bounded runner; returns the outcome."""
    if job_id not in _RUNNABLE_JOB_IDS:
        return JSONResponse(
            status_code=403,
            content={"error": "job_not_runnable", "allowed_values": sorted(_RUNNABLE_JOB_IDS)},
        )
    from services.scheduler_job_runner import run_job_now

    try:
        outcome = await run_job_now(job_id)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"error": "job_not_registered", "job_id": job_id},
        )
    logger.warning(
        "scheduler job RUN-NOW by admin=%s -> job_id=%s outcome=%s",
        (current_admin or {}).get("email"), job_id, outcome.get("outcome"),
    )
    return outcome


@router.post("/admin/scheduler/jobs/{job_id}/cancel-running", response_model=None)
async def cancel_running_scheduler_job(
    job_id: str,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Cancel every in-flight run (and alive zombie) of a job. Any registered
    job — this only ever frees a slot, never starts work."""
    from services.scheduler_job_runner import cancel_running

    n = cancel_running(job_id)
    logger.warning(
        "scheduler job CANCEL-RUNNING by admin=%s -> job_id=%s cancelled=%d",
        (current_admin or {}).get("email"), job_id, n,
    )
    return {"job_id": job_id, "cancelled": n}


@router.post("/admin/scheduler/jobs/{job_id}/{action}", response_model=None)
async def manage_scheduler_job(
    job_id: str,
    action: str,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any] | JSONResponse:
    """Resume or pause an allowlisted scheduler job.

    action must be `resume` or `pause`. Only job ids in the settlement allowlist
    are accepted (403 otherwise) so this can never affect unrelated crons.
    Idempotent: resuming a running job / pausing a paused job is a safe no-op.
    """

    if action not in ("resume", "pause"):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_action", "allowed_values": ["resume", "pause"]},
        )
    if job_id not in _MANAGEABLE_JOB_IDS:
        return JSONResponse(
            status_code=403,
            content={
                "error": "job_not_manageable",
                "allowed_values": sorted(_MANAGEABLE_JOB_IDS),
            },
        )

    from services.audit_scheduler import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None or not getattr(scheduler, "running", False):
        return JSONResponse(
            status_code=503,
            content={"error": "scheduler_not_running"},
        )

    job = scheduler.get_job(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"error": "job_not_found", "job_id": job_id},
        )

    try:
        if action == "resume":
            scheduler.resume_job(job_id)
        else:
            scheduler.pause_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduler job %s failed for job_id=%s admin=%s: %s",
            action,
            job_id,
            (current_admin or {}).get("email"),
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "scheduler_action_failed", "message": str(exc)},
        )

    refreshed = scheduler.get_job(job_id)
    state = _job_state(refreshed) if refreshed is not None else {"id": job_id}
    logger.info(
        "scheduler job %s by admin=%s -> job_id=%s paused=%s next_run_time=%s",
        action,
        (current_admin or {}).get("email"),
        job_id,
        state.get("paused"),
        state.get("next_run_time"),
    )
    return {"action": action, **state}


@router.post("/admin/scheduler/restart", response_model=None)
async def restart_whole_scheduler(
    current_admin: dict = Depends(require_admin),
):
    """Force a clean re-init of the ENTIRE scheduler (recovery lever for the
    silent-stall class where all jobs came up with next_run_time=None). Shuts
    down the current instance and re-runs start_scheduler(); returns the
    post-restart diagnostics (state_name + fireable_job_count). Admin-only."""
    from services.audit_scheduler import restart_scheduler

    try:
        diag = await restart_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "scheduler restart failed admin=%s: %s",
            (current_admin or {}).get("email"), exc, exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "scheduler_restart_failed", "message": str(exc)},
        )
    logger.warning(
        "scheduler RESTART by admin=%s -> state=%s fireable=%s",
        (current_admin or {}).get("email"),
        diag.get("state_name"), diag.get("fireable_job_count"),
    )
    return {"restarted": True, **diag}
