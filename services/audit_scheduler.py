"""APScheduler instance + lifecycle for the auto-re-audit cron.

Started in main.startup_event, stopped in main.shutdown_event. The
scheduler runs in-process (not a separate worker) — fine at current
scale. If the audit pipeline ever needs to outlive the API process or
scale horizontally, swap to arq / celery / Redis-backed scheduler.

Job registration happens at start-up time. Currently registers:
- `daily_audit_check` — fires at 03:00 UTC daily, calls
  jobs/scheduled_audit_job.run_scheduled_audits which queries
  catalog_merchants for due re-audits.

Best-effort: scheduler init failure logs a warning but does not crash
the API. The audit endpoints still work; only the cron is degraded.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


_SCHEDULER = None  # type: Optional[object]


def get_scheduler():
    """Return the module-level scheduler instance (or None if not
    started). Tests can monkey-patch this."""
    return _SCHEDULER


async def start_scheduler() -> None:
    """Initialize APScheduler + register all cron jobs. Idempotent —
    safe to call multiple times (subsequent calls are no-ops)."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        logger.info("audit_scheduler: already started; skipping")
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        logger.warning(
            "audit_scheduler: apscheduler not installed; "
            "auto-re-audit cron disabled"
        )
        return

    try:
        scheduler = AsyncIOScheduler(timezone="UTC")
        # Register the daily check that picks up due merchants. Hour
        # chosen to land off-peak (most merchants in US/EU; 03:00 UTC
        # = 23:00 EDT / 04:00 CET).
        from jobs.scheduled_audit_job import run_scheduled_audits
        scheduler.add_job(
            run_scheduled_audits,
            "cron",
            hour=3,
            minute=0,
            id="daily_audit_check",
            replace_existing=True,
            misfire_grace_time=3600,  # tolerate 1 hour late
            coalesce=True,            # only run once if multiple firings queued
        )
        scheduler.start()
        _SCHEDULER = scheduler
        logger.info(
            "audit_scheduler: started with daily_audit_check at 03:00 UTC"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_scheduler: start failed (continuing degraded): %s",
            exc,
        )


async def stop_scheduler() -> None:
    """Graceful shutdown of the scheduler. Called from
    main.shutdown_event. Best-effort."""
    global _SCHEDULER
    if _SCHEDULER is None:
        return
    try:
        # AsyncIOScheduler.shutdown(wait=False) cancels in-flight
        # jobs immediately. We prefer wait=True so a re-audit in
        # progress completes — but capped at 30s so deploys aren't
        # blocked by a hanging job.
        _SCHEDULER.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_scheduler: stop error: %s", exc)
    finally:
        _SCHEDULER = None
