"""APScheduler instance + lifecycle for the auto-re-audit cron.

Started in main.startup_event, stopped in main.shutdown_event. The
scheduler runs in-process (not a separate worker) — fine at current
scale. If the audit pipeline ever needs to outlive the API process or
scale horizontally, swap to arq / celery / Redis-backed scheduler.

Job registration happens at start-up time. Currently registers:
- `daily_audit_check` — fires at 03:00 UTC daily, calls
  jobs/scheduled_audit_job.run_scheduled_audits which queries
  catalog_merchants for due re-audits.
- `audit_run_worker_tick` — fires every 10 seconds, calls
  services/audit_run_worker.run_audit_worker_tick which drains
  STAGE_QUEUED rows from merchant_audit_runs (P2.2).
- `audit_run_lease_reaper` — fires every 60 seconds, calls
  services/audit_run_worker.run_stale_lease_reaper_tick to
  release expired worker leases as a backstop.
- `executor_run_worker_tick` — fires every 5 seconds, calls
  services/executor_run_worker.run_executor_worker_tick which
  drains STAGE_QUEUED rows from executor_runs (P3.2). Faster
  cadence than audit_run_worker because executor agents are
  individual short-lived actions, not multi-minute audits.
- `executor_run_lease_reaper` — fires every 60 seconds, calls
  services/executor_run_worker.run_executor_lease_reaper_tick.
- `verification_run_worker_tick` — fires every 30 seconds, calls
  services/verification_run_worker.run_verification_worker_tick
  which drains pending verification_runs rows (P5.2). Slower
  cadence than executor worker because verifiers are not latency-
  critical.
- `verification_run_lease_reaper` — fires every 60 seconds.

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

        # Index health consolidation: reads quality snapshots, seed audit,
        # identity graph, and offer presence; writes index_pipeline_state +
        # domain_extractor_baselines. Offset 1h from daily_audit_check
        # to avoid Postgres query contention.
        from jobs.nightly_index_health_job import run_nightly_index_health
        scheduler.add_job(
            run_nightly_index_health,
            "cron",
            hour=4,
            minute=0,
            id="nightly_index_health",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        # P2.2: drive queued audit_runs through the async lifecycle.
        # No production traffic flows here until P2.3 ships POST
        # /api/audits, so the tick is a safe no-op until then. 10s
        # interval keeps queue latency low without hammering the DB.
        from services.audit_run_worker import (
            run_audit_worker_tick,
            run_stale_lease_reaper_tick,
        )
        scheduler.add_job(
            run_audit_worker_tick,
            "interval",
            seconds=10,
            id="audit_run_worker_tick",
            replace_existing=True,
            # Don't queue up multiple ticks if one runs long; one
            # tick already drains MAX_RUNS_PER_TICK runs.
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            run_stale_lease_reaper_tick,
            "interval",
            seconds=60,
            id="audit_run_lease_reaper",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # P3.2: drive queued executor_runs through the durable work
        # queue. No production traffic flows here until P3.3 migrates
        # the dispatcher to enqueue instead of fire-and-forget. 5s
        # interval (faster than audit worker) since executor agents
        # are short-lived (seconds, not minutes).
        from services.executor_run_worker import (
            run_executor_worker_tick,
            run_executor_lease_reaper_tick,
        )
        scheduler.add_job(
            run_executor_worker_tick,
            "interval",
            seconds=5,
            id="executor_run_worker_tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            run_executor_lease_reaper_tick,
            "interval",
            seconds=60,
            id="executor_run_lease_reaper",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # P5.2: drive verification_runs through the durable queue.
        # P5.3: import services.verifiers to trigger register_verifier
        # side-effects (each verifier module registers itself at
        # import time).
        from services.verification_run_worker import (
            run_verification_worker_tick,
            run_verification_lease_reaper_tick,
        )
        try:
            import services.verifiers  # noqa: F401 — side-effect import
        except Exception as exc:  # noqa: BLE001
            # If verifier imports break, the worker still ticks
            # against an empty registry — better than scheduler
            # init failing entirely.
            logger.warning(
                "audit_scheduler: verifier registration failed: %s",
                exc,
            )
        scheduler.add_job(
            run_verification_worker_tick,
            "interval",
            seconds=30,
            id="verification_run_worker_tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            run_verification_lease_reaper_tick,
            "interval",
            seconds=60,
            id="verification_run_lease_reaper",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # ===== v1.3 monetization cron jobs =====
        # Stage 1: T6 GMV aggregation + T5 reservation reaper run ACTIVE.
        # Stage 4: T7 invoice generation + T8 partner settlement registered
        # PAUSED (next_run_time=None) so promotion is a one-line resume call
        # rather than a code-change + redeploy cycle. See
        # docs/monetization/deploy/STAGE_1_SHADOW_MODE_ROLLOUT.md §0.

        from services.metering_service import expire_stale_reservations
        from services.gmv_aggregation_service import aggregate_daily
        from services.invoice_generation_service import run_billing_cycle
        from services.partner_settlement_service import run_settlement
        from db.database import database

        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        async def _run_gmv_aggregation_yesterday() -> None:
            """T6 daily wrapper: aggregate yesterday's edges into gmv_attribution_daily."""
            yesterday = (_dt.now(_tz.utc) - _td(days=1)).date()
            rows = await aggregate_daily(yesterday)
            logger.info("audit_scheduler: gmv_aggregation_daily for %s -> %d rollup rows", yesterday, rows)

        async def _run_billing_cycle_previous_month() -> None:
            """T7 monthly wrapper: invoice the previous calendar month.

            Picks the calendar month that ENDED before today. E.g., run on
            2026-07-02 -> period = 2026-06-01..2026-06-30. Idempotent on
            billing_runs.idempotency_key.
            """
            today = _dt.now(_tz.utc).date()
            first_of_this_month = today.replace(day=1)
            last_of_prev_month = first_of_this_month - _td(days=1)
            period_start = last_of_prev_month.replace(day=1)
            period_end = last_of_prev_month
            run_id = await run_billing_cycle(period_start, period_end)
            logger.info("audit_scheduler: invoice_generation_monthly %s..%s -> billing_run_id=%s",
                        period_start, period_end, run_id)

        async def _run_partner_settlement_latest() -> None:
            """T8 monthly wrapper: settle the latest completed billing_runs row.

            Pairs with T7 — runs on day 3 (one day after T7's day 2) so the
            invoice cycle has finished. Picks the most recent billing_runs
            row whose status='completed' and runs settlement against it.
            """
            row = await database.fetch_one(
                "SELECT id, period_start, period_end FROM billing_runs "
                "WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
            )
            if not row:
                logger.info("audit_scheduler: partner_settlement_monthly: no completed billing_runs row; skipping")
                return
            billing_run_id = int(row["id"])
            n_payouts = await run_settlement(billing_run_id)
            logger.info(
                "audit_scheduler: partner_settlement_monthly billing_run_id=%s -> %d payout rows",
                billing_run_id, n_payouts,
            )

        # T5 — credit reservation reaper, every 5 minutes (ACTIVE).
        # No-op when no reservations are outstanding (current state in
        # production), so safe to run continuously from Stage 1 onward.
        scheduler.add_job(
            expire_stale_reservations,
            "interval",
            minutes=5,
            id="metering_expire_reservations",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )

        # T6 — GMV aggregation, daily at 02:00 UTC (ACTIVE).
        # Aggregates the previous UTC day's stamped attribution edges into
        # gmv_attribution_daily. Output feeds T7's monthly invoice run.
        scheduler.add_job(
            _run_gmv_aggregation_yesterday,
            "cron",
            hour=2,
            minute=0,
            id="gmv_aggregation_daily",
            replace_existing=True,
            misfire_grace_time=900,
            coalesce=True,
            max_instances=1,
        )

        # T7 — invoice generation, monthly on day 2 03:00 UTC (PAUSED).
        # Registered paused so Stage 4 promotion is `scheduler.resume_job(
        # "invoice_generation_monthly")` — no code change, no redeploy.
        scheduler.add_job(
            _run_billing_cycle_previous_month,
            "cron",
            day=2,
            hour=3,
            minute=0,
            id="invoice_generation_monthly",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
            next_run_time=None,  # paused — see Stage 1 §0 and Stage 4 promotion
        )

        # T8 — partner settlement, monthly on day 3 04:00 UTC (PAUSED).
        # Day after T7 so the invoice cycle has finalized. Same paused
        # pattern as T7 — Stage 4 resume call enables.
        scheduler.add_job(
            _run_partner_settlement_latest,
            "cron",
            day=3,
            hour=4,
            minute=0,
            id="partner_settlement_monthly",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
            next_run_time=None,  # paused — see Stage 1 §0 and Stage 4 promotion
        )

        scheduler.start()
        _SCHEDULER = scheduler
        logger.info(
            "audit_scheduler: started with daily_audit_check (03:00 UTC) "
            "+ nightly_index_health (04:00 UTC) "
            "+ audit_run_worker_tick (10s) "
            "+ audit_run_lease_reaper (60s) "
            "+ executor_run_worker_tick (5s) "
            "+ executor_run_lease_reaper (60s) "
            "+ verification_run_worker_tick (30s) "
            "+ verification_run_lease_reaper (60s) "
            "+ metering_expire_reservations (5min, ACTIVE) "
            "+ gmv_aggregation_daily (02:00 UTC, ACTIVE) "
            "+ invoice_generation_monthly (day 2 03:00 UTC, PAUSED) "
            "+ partner_settlement_monthly (day 3 04:00 UTC, PAUSED)"
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
