"""Scheduler health endpoint.

Reports whether the APScheduler instance from services.audit_scheduler
booted successfully and which jobs are registered. Mounted at
GET /__scheduler_health (double-underscore prefix marks it as an
internal/operational endpoint, mirroring /__build).

Why this exists: app-level logger.info calls are filtered on Railway
prod (root logger defaults to WARNING; no setup_structured_logging
call in main), so the scheduler success log line never surfaced in
log fetches. This endpoint reports state directly so operators don't
have to log-spelunk to confirm worker boot.

Reads from services.audit_scheduler.get_scheduler() — does not start
or stop anything; pure read-only state report.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

router = APIRouter(tags=["scheduler-health"])


@router.get("/__scheduler_health")
async def scheduler_health() -> Dict[str, Any]:
    """Report whether the audit_scheduler is running + its registered
    jobs. 200 in all cases — the response shape carries the verdict.

    Returns:
        running: True if get_scheduler() returns a non-None instance
            AND that instance reports .running == True.
        job_count: number of registered jobs (0 when scheduler not
            running).
        jobs: list of {id, next_run_time (ISO 8601 or null), trigger}
            for each registered job.
    """
    try:
        from services.audit_scheduler import get_scheduler
    except Exception as exc:  # noqa: BLE001
        return {
            "running": False,
            "reason": f"audit_scheduler module import failed: {exc!s}",
            "job_count": 0,
            "jobs": [],
        }

    # Real runtime state — state_name distinguishes RUNNING from PAUSED, which
    # the `running` flag below CANNOT (it is `state != STOPPED`, so a paused
    # scheduler that fires nothing still reports running:True).
    diag: Dict[str, Any] = {}
    try:
        from services.audit_scheduler import scheduler_diagnostics
        diag = scheduler_diagnostics()
    except Exception as exc:  # noqa: BLE001
        diag = {"diag_error": str(exc)}

    scheduler = get_scheduler()
    if scheduler is None:
        return {
            "running": False,
            "reason": (
                "get_scheduler() returned None — start_scheduler() never "
                "ran or its boot raised silently"
            ),
            "job_count": 0,
            "jobs": [],
            **diag,
        }

    # APScheduler instance present; report registered jobs.
    jobs: List[Dict[str, Any]] = []
    try:
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run_time": (
                    job.next_run_time.isoformat()
                    if job.next_run_time is not None else None
                ),
                "trigger": str(job.trigger),
            })
    except Exception as exc:  # noqa: BLE001
        return {
            "running": bool(getattr(scheduler, "running", False)),
            "reason": f"get_jobs() raised: {exc!s}",
            "job_count": 0,
            "jobs": [],
            **diag,
        }

    return {
        "running": bool(getattr(scheduler, "running", False)),
        "job_count": len(jobs),
        "jobs": jobs,
        **diag,
    }
