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
- `external_seed_catalog_materialization` — fires every 15 minutes,
  materializes newly crawled external_product_seeds into guarded catalog
  mirrors without public-serving promotion.
- `settlement_file_generate` — in-process monthly day-5 UTC cron,
  generates prior-month partner settlement files from snapshots.
- `settlement_file_transfer` — in-process monthly day-10 UTC cron,
  transfers prior-month pending settlement files through Stripe Connect.

Best-effort: scheduler init failure logs a warning but does not crash
the API. The audit endpoints still work; only the cron is degraded.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


_SCHEDULER = None  # type: Optional[object]

# Boot diagnostics surfaced by /__scheduler_health. APScheduler's `running`
# property is `state != STOPPED`, which is TRUE even when PAUSED — so a paused
# scheduler (every job next_run_time=None, nothing fires) reports "running:True"
# and looks healthy. These capture the REAL state + any boot exception so the
# silent-stall class (all 26 jobs next_run=None, 2026-07) is visible without
# log-spelunking on Railway (where INFO logs are filtered).
_BOOT_ERROR = None  # type: Optional[str]
_BOOT_DIAG = None   # type: Optional[dict]

_STATE_NAMES = {0: "STOPPED", 1: "RUNNING", 2: "PAUSED"}


def scheduler_diagnostics() -> dict:
    """Read-only report of the scheduler's real runtime state for
    /__scheduler_health — state name, boot error, worker gate, and how many
    registered jobs actually have a next_run_time (0 => firing nothing)."""
    sched = _SCHEDULER
    state = getattr(sched, "state", None) if sched is not None else None
    jobs = []
    fireable = 0
    try:
        for job in (sched.get_jobs() if sched is not None else []):
            nrt = getattr(job, "next_run_time", None)
            if nrt is not None:
                fireable += 1
    except Exception:  # noqa: BLE001
        pass
    return {
        "state": state,
        "state_name": _STATE_NAMES.get(state, "UNKNOWN") if state is not None else "NOT_STARTED",
        "boot_error": _BOOT_ERROR,
        "boot_diag": _BOOT_DIAG,
        "worker_enabled": _queue_worker_enabled(),
        "fireable_job_count": fireable,
    }


def _queue_worker_enabled() -> bool:
    """Whether THIS process should drain the shared async-run queues
    (audit / executor / verification worker ticks + their lease reapers).

    Production and staging SHARE one Postgres (single-DB tenancy by design), and
    these workers claim work by polling the shared tables with NO environment
    filter. So a staging service running them POACHES production-enqueued runs it
    can't complete — it lacks prod secrets (e.g. PROMOTIONS_ADMIN_KEY) and may run
    older code — then silently fails them (0 probes, "succeeded" empty audit).
    Gate the drainers so only the production worker claims the shared queue.

    Fail-safe toward ENABLED: only disable when this is clearly a staging/preview
    service, or AUDIT_WORKER_ENABLED is explicitly false — so a detection miss
    can never accidentally stop the PRODUCTION worker (worst case = no change).
    """
    override = (os.getenv("AUDIT_WORKER_ENABLED") or "").strip().lower()
    if override:
        return override in ("1", "true", "yes", "on")
    service = (os.getenv("RAILWAY_SERVICE_NAME") or "").lower()
    env = (os.getenv("RAILWAY_ENVIRONMENT") or "").lower()
    if "staging" in service or "preview" in service:
        return False
    if env in ("staging", "preview", "development", "dev"):
        return False
    return True


def get_scheduler():
    """Return the module-level scheduler instance (or None if not
    started). Tests can monkey-patch this."""
    return _SCHEDULER


async def start_scheduler() -> None:
    """Initialize APScheduler + register all cron jobs. Idempotent —
    safe to call multiple times (subsequent calls are no-ops)."""
    global _SCHEDULER, _BOOT_ERROR, _BOOT_DIAG
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
        # Only the production worker drains the SHARED async-run queues; a
        # staging service on the same DB must not poach prod-enqueued runs.
        worker_enabled = _queue_worker_enabled()
        if not worker_enabled:
            logger.warning(
                "audit_scheduler: shared-queue worker ticks DISABLED on this "
                "service (RAILWAY_SERVICE_NAME=%r RAILWAY_ENVIRONMENT=%r) — it "
                "will NOT drain prod audit/executor/verification queues. Set "
                "AUDIT_WORKER_ENABLED=true to override.",
                os.getenv("RAILWAY_SERVICE_NAME"),
                os.getenv("RAILWAY_ENVIRONMENT"),
            )

        def _add_job(*args, **kwargs):
            # Register a scheduled job ONLY on the production worker. Prod+staging
            # share one Postgres, so a staging service must NOT double-fire these
            # singleton prod crons (daily audit check, settlement, billing,
            # reconcile, backfills) or drain the shared queues. Gates everything
            # uniformly — the explicit `if worker_enabled:` blocks below are now
            # redundant but harmless.
            if worker_enabled:
                scheduler.add_job(*args, **kwargs)

        # Register the daily check that picks up due merchants. Hour
        # chosen to land off-peak (most merchants in US/EU; 03:00 UTC
        # = 23:00 EDT / 04:00 CET).
        from jobs.scheduled_audit_job import run_scheduled_audits
        _add_job(
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
        _add_job(
            run_nightly_index_health,
            "cron",
            hour=4,
            minute=0,
            id="nightly_index_health",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        # Outcome aggregation: roll the decision -> order -> paid/refund loop into
        # per-merchant + per-product outcome metrics (aggregated_outcomes). Daily at
        # 05:00 UTC (after nightly_index_health). Min-sample-gated, so it surfaces
        # nothing until real transaction volume exists — a safe no-op pre-launch.
        from services.outcome_aggregation_service import refresh_all_outcomes
        # _add_job (NOT scheduler.add_job) so staging — which shares prod's
        # Postgres — does not double-fire this singleton cron. It used the raw
        # add_job and slipped the env gate every other job respects.
        _add_job(
            refresh_all_outcomes,
            "cron",
            hour=5,
            minute=0,
            id="outcome_aggregation_daily",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        # T2-2b external-conversion polling floor: for merchants we hold
        # `read_orders` for but that have NOT registered an `orders/paid`
        # webhook (App-Store installs; the connected TEST merchant), poll recent
        # Shopify orders, recover the T2-1 `pivota_click_id` from note_attributes
        # and close each conversion via the SAME idempotent primitive the webhook
        # uses. OFF BY DEFAULT — poll_external_conversions_batch no-ops unless
        # EXTERNAL_CONVERSION_POLLER_ENABLED is set, so deploying never starts
        # autonomous Shopify polling (mirrors catalog_onboard_queue). Every
        # 15 min; scoped to merchants with a click in the last 30 days.
        from services.external_conversion_poller import poll_external_conversions_batch
        _add_job(
            poll_external_conversions_batch,
            "interval",
            minutes=15,
            id="external_conversion_poll",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )

        # W7 audit-health: the alarm the no-fallback main line assumes. Computes
        # run-failure + honest-failure (brief unavailable_*) rates over a rolling
        # window and alerts on breach. Observational — no merchant-facing effect.
        # Hourly.
        from services.audit_health_metrics import run_audit_health_tick
        _add_job(
            run_audit_health_tick,
            "interval",
            minutes=60,
            id="audit_health_tick",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )

        # W7 stability canary: auto-detect same-basis re-runs (any merchant) inside a
        # tight window and assert they land within tolerance — validates W2 / catches
        # measurement-system noise. Twice daily so a 48h-window pair is reliably caught
        # (create_alert dedups a persistent breach).
        #
        # CRON trigger, NOT `interval` — same restart-starvation reasoning as the
        # catalog_row_trust backfill below: a 12h interval job's first fire is
        # start+12h, reset on every redeploy, so on a service that recycles more
        # often than every 12h it would never fire. Fixed 05:30/17:30 UTC slots.
        from services.audit_stability_canary import run_stability_canary
        _add_job(
            run_stability_canary,
            "cron",
            hour="5,17",
            minute=30,
            id="audit_stability_canary",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

        # Catalog-coverage unattended growth: drain the catalog-onboard queue
        # (curated brands + audit-discovered competitors → commerce-index anchors).
        # OFF BY DEFAULT — run_catalog_onboard_queue no-ops unless
        # CATALOG_ONBOARD_ENABLED is set, so deploying never starts autonomous
        # catalog writes; flip the flag to enable. 30-min interval.
        from jobs.catalog_onboard_job import run_catalog_onboard_queue
        _add_job(
            run_catalog_onboard_queue,
            "interval",
            minutes=30,
            id="catalog_onboard_queue_drain",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )

        # P2.2: drive queued audit_runs through the async lifecycle.
        # No production traffic flows here until P2.3 ships POST
        # /api/audits, so the tick is a safe no-op until then. 10s
        # interval keeps queue latency low without hammering the DB.
        from services.audit_run_worker import (
            run_audit_worker_tick,
            run_stale_lease_reaper_tick,
            run_abandoned_run_reaper_tick,
        )
        if worker_enabled:
            _add_job(
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
            _add_job(
                run_stale_lease_reaper_tick,
                "interval",
                seconds=60,
                id="audit_run_lease_reaper",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            # Terminal backstop: force-fail runs abandoned past an
            # absolute age (no live lease). Catches the URL-audit wedge's
            # bare-asyncio orphans and pre-lease-era runs that the
            # lease reaper above (re-queue only) can't reach. 5-min
            # cadence is plenty — these are a tail-cleanup, not hot path.
            _add_job(
                run_abandoned_run_reaper_tick,
                "interval",
                minutes=5,
                id="audit_run_abandoned_reaper",
                replace_existing=True,
                misfire_grace_time=60,
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
        if worker_enabled:
            _add_job(
                run_executor_worker_tick,
                "interval",
                seconds=5,
                id="executor_run_worker_tick",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            _add_job(
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
        if worker_enabled:
            _add_job(
                run_verification_worker_tick,
                "interval",
                seconds=30,
                id="verification_run_worker_tick",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            _add_job(
                run_verification_lease_reaper_tick,
                "interval",
                seconds=60,
                id="verification_run_lease_reaper",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )

        # External crawl/backfill sessions write external_product_seeds first.
        # This catch-up materializes only missing catalog mirrors in small
        # batches. It mints sig_* for new catalog rows, but public PDP serving
        # remains gated by nightly_index_health and agent_pdp_view.
        from jobs.external_seed_catalog_materialization_job import (
            run_external_seed_catalog_materialization_tick,
        )
        _add_job(
            run_external_seed_catalog_materialization_tick,
            "interval",
            minutes=15,
            id="external_seed_catalog_materialization",
            replace_existing=True,
            misfire_grace_time=300,
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
        from services import settlement_file_service
        from jobs.stamp_attribution_reaper_job import run_stamp_attribution_reaper_tick
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

        async def _generate_prior_month_for_all_partners() -> None:
            """PR #8 day-5 wrapper: generate prior-month settlement files."""
            today = _dt.now(_tz.utc).date()
            prior_month = (today.replace(day=1) - _td(days=1)).replace(day=1)
            file_ids = await settlement_file_service.generate_for_all_active_partners(
                prior_month
            )
            logger.info(
                "audit_scheduler: settlement_file_generate %s -> %d file rows",
                prior_month,
                len(file_ids),
            )

        async def _transfer_prior_month_for_all_partners() -> None:
            """PR #8 day-10 wrapper: transfer prior-month pending settlement files."""
            today = _dt.now(_tz.utc).date()
            prior_month = (today.replace(day=1) - _td(days=1)).replace(day=1)
            results = await settlement_file_service.transfer_all_pending_for_month(
                prior_month
            )
            logger.info(
                "audit_scheduler: settlement_file_transfer %s -> %d file rows",
                prior_month,
                len(results),
            )

        # T5 — credit reservation reaper, every 5 minutes (ACTIVE).
        # No-op when no reservations are outstanding (current state in
        # production), so safe to run continuously from Stage 1 onward.
        _add_job(
            expire_stale_reservations,
            "interval",
            minutes=5,
            id="metering_expire_reservations",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )

        # T9 — stamping reaper, every 5 minutes (ACTIVE).
        # Catches up on paid orders whose synchronous T9 stamp silently
        # failed (psp_payment_finalizer.py:226-238 swallows stamping
        # exceptions so payment success isn't blocked by observability
        # hiccups). Without this, an unstamped paid edge is invisible to
        # T6 forever. 2-minute grace + 24h window are encoded in the
        # job's SQL query.
        _add_job(
            run_stamp_attribution_reaper_tick,
            "interval",
            minutes=5,
            id="stamp_attribution_reaper",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
            max_instances=1,
        )

        # T6 — GMV aggregation, daily at 02:00 UTC (ACTIVE).
        # Aggregates the previous UTC day's stamped attribution edges into
        # gmv_attribution_daily. Output feeds T7's monthly invoice run.
        _add_job(
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
        _add_job(
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
        _add_job(
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

        # PR #8 — settlement file generation, monthly on day 5 02:00 UTC.
        # Generates DB-only settlement_files rows for the previous calendar
        # month. Safe in staging because no external call is made.
        _add_job(
            _generate_prior_month_for_all_partners,
            "cron",
            day=5,
            hour=2,
            minute=0,
            id="settlement_file_generate",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        # PR #8 — Stripe Connect transfer, monthly on day 10 02:00 UTC.
        # Registered in-process alongside the audit scheduler; the service
        # itself gates real Stripe transfers to production unless an explicit
        # staging override env var is set.
        _add_job(
            _transfer_prior_month_for_all_partners,
            "cron",
            day=10,
            hour=2,
            minute=0,
            id="settlement_file_transfer",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        # C1 Phase 2d — catalog_row_trust defensive backfill, every 6h.
        # Catches rows missed by the dual-write producer hooks (Phase 2b/2c).
        # Idempotent UPSERT means idle runs (no drift) emit zero writes.
        # Disable via CATALOG_TRUST_BACKFILL_ENABLED=false without a deploy.
        #
        # CRON trigger (fixed 6-hourly slots), NOT `interval`. An interval job's
        # first fire is `process_start + interval`, and every restart re-registers
        # it (replace_existing) resetting that timer to now+interval. This service
        # redeploys on push + recycles far more often than every 6h, so a 6h
        # interval job NEVER reached its first fire — the backfill effectively
        # never ran (stale trust rows sat blocked for days; see
        # docs/catalog_row_trust_freshness_crawled_finding_2026-07-13.md in the
        # PIVOTA-Agent repo). A cron trigger fires at wall-clock slots regardless
        # of restart cadence; the job only needs to be alive at (or within
        # misfire_grace_time of) a slot.
        # ADR-012 Phase 0b — daily serving-surface invariant sweep. CRON
        # trigger for the same restart-starvation reason as the trust backfill.
        from jobs.catalog_invariant_sweep_job import run_catalog_invariant_sweep_tick
        _add_job(
            run_catalog_invariant_sweep_tick,
            "cron",
            hour=4,
            minute=50,  # after nightly_index_health (04:00) has reconciled IPS
            id="catalog_invariant_sweep",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        from jobs.catalog_row_trust_backfill_cron import (
            run_catalog_row_trust_backfill_tick,
        )
        _add_job(
            run_catalog_row_trust_backfill_tick,
            "cron",
            hour="*/6",
            minute=17,  # off the top of the hour to avoid contending with :00 crons
            id="catalog_row_trust_backfill",
            replace_existing=True,
            misfire_grace_time=1800,  # fire up to 30min late (post-restart) not skip
            coalesce=True,
            max_instances=1,
        )

        # ADR-012 Phase 1 slice 1 — agent_pdp_view convergent reconciler.
        # Sweeps stale/missing view rows back to catalog truth via the
        # canonical refresh primitive; bounded per run, stalest-first, with
        # a post-pass drift alarm (also on /__catalog_health). CRON trigger
        # for the same restart-starvation reason as the trust backfill; :43
        # offsets it from the :17 trust slots and the :00 crons so the two
        # 6-hourly catalog sweeps never contend on the shared Postgres.
        # Disable via AGENT_PDP_VIEW_RECONCILE_ENABLED=false without a deploy.
        from jobs.agent_pdp_view_reconciler_cron import (
            run_agent_pdp_view_reconcile_tick,
        )
        _add_job(
            run_agent_pdp_view_reconcile_tick,
            "cron",
            hour="*/6",
            minute=43,
            id="agent_pdp_view_reconcile",
            replace_existing=True,
            misfire_grace_time=1800,
            coalesce=True,
            max_instances=1,
        )

        # P1.13 — converge catalog_products.pdp_will_render with the live
        # predicate. #1604 shipped the column and both refresh entry points with
        # NO CALLER on purpose (per-writer invalidation was measured at ~14min
        # per 1,000-product sync and produces a biased trickle); this is the
        # drift reconciler that module asked for.
        #
        # :53 keeps it clear of the :17 trust slots, :43 agent_pdp_view and the
        # :00 crons — the three 6-hourly catalog sweeps must not contend on the
        # shared Postgres. Disable via PDP_WILL_RENDER_RECONCILE_ENABLED=false.
        from jobs.pdp_renderability_reconciler_cron import (
            run_pdp_will_render_reconcile_tick,
        )
        _add_job(
            run_pdp_will_render_reconcile_tick,
            "cron",
            hour="*/6",
            minute=53,
            id="pdp_will_render_reconcile",
            replace_existing=True,
            misfire_grace_time=1800,
            coalesce=True,
            max_instances=1,
        )

        # Onboarding→audit readiness: drain quality-backfill jobs enqueued at the
        # end of catalog sync. The scorer is deterministic (no LLM), so a 30s
        # interval is cheap; this populates product_quality_snapshot so a freshly
        # synced merchant becomes v3-audit-ready without any manual step.
        from services.product_quality_backfill_service import (
            process_next_quality_backfill_job,
        )
        _add_job(
            process_next_quality_backfill_job,
            "interval",
            seconds=30,
            id="quality_backfill_drain_tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # Pending-payment reconcile sweep: safety net behind the Stripe webhook.
        # Re-checks orders still awaiting_payment but already carrying a PSP
        # reference and finalizes the ones the PSP says actually succeeded, so a
        # missing/mis-correlated webhook can't strand a real charge. 5min keeps
        # recovery latency low without hammering the PSP API. The tick is DORMANT
        # unless PAYMENT_RECONCILE_SWEEP_ENABLED is set — it auto-finalizes
        # payments, and staging shares the prod DB, so enable deliberately.
        from services.payment_reconcile import run_payment_reconcile_tick
        _add_job(
            run_payment_reconcile_tick,
            "interval",
            seconds=300,
            id="payment_reconcile_tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )

        # ADR-010 D-2 Phase B: weekly catalog identity-reconcile sweep —
        # classify -> propose -> auto-apply ONLY the mechanical allowlist
        # (same_url_dup, junk_url) -> review batches for the rest -> alert
        # if a duplication gauge rises. DORMANT unless
        # ENABLE_IDENTITY_RECONCILE_SWEEP is set (the tick checks the flag
        # itself); staging shares the prod DB, so enable deliberately.
        # docs/plans/adr010_d2_catalog_reconciliation_at_scale.md.
        from services.identity_reconcile_sweep import (
            run_identity_reconcile_sweep_tick,
        )
        _add_job(
            run_identity_reconcile_sweep_tick,
            "cron",
            day_of_week="mon",
            hour=4,
            minute=30,
            id="identity_reconcile_sweep",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=21600,  # fire up to 6h late rather than skip a week
        )

        scheduler.start()
        _SCHEDULER = scheduler

        # SELF-HEAL + DIAGNOSE the silent-stall class. `scheduler.start()` can
        # leave the scheduler in STATE_PAUSED (its `running` property is still
        # True), or otherwise fail to compute next_run_time for its jobs — in
        # both cases every job has next_run_time=None and NOTHING ever fires,
        # while /__scheduler_health reports "running:True". Detect it, resume if
        # paused, and record the outcome for the health endpoint.
        _BOOT_ERROR = None
        try:
            from apscheduler.schedulers.base import STATE_PAUSED

            resumed = False
            if getattr(scheduler, "state", None) == STATE_PAUSED:
                logger.warning(
                    "audit_scheduler: came up PAUSED (running property masks it) "
                    "— resuming so jobs actually fire"
                )
                scheduler.resume()
                resumed = True

            all_jobs = scheduler.get_jobs()
            fireable = [j for j in all_jobs if getattr(j, "next_run_time", None) is not None]
            _BOOT_DIAG = {
                "state": getattr(scheduler, "state", None),
                "resumed_from_paused": resumed,
                "job_count": len(all_jobs),
                "fireable_job_count": len(fireable),
            }
            if all_jobs and not fireable:
                # Registered but not schedulable — the exact silent stall. Surface
                # loudly; logs are filtered on prod so the health endpoint carries it.
                logger.error(
                    "audit_scheduler: %d jobs registered but NONE have a "
                    "next_run_time (state=%s) — scheduler is firing NOTHING",
                    len(all_jobs), getattr(scheduler, "state", None),
                )
        except Exception as diag_exc:  # noqa: BLE001 — diagnostics must never break boot
            logger.warning("audit_scheduler: self-heal/diag failed: %s", diag_exc)

        logger.info(
            "audit_scheduler: started with daily_audit_check (03:00 UTC) "
            "+ nightly_index_health (04:00 UTC) "
            "+ audit_run_worker_tick (10s) "
            "+ audit_run_lease_reaper (60s) "
            "+ executor_run_worker_tick (5s) "
            "+ executor_run_lease_reaper (60s) "
            "+ verification_run_worker_tick (30s) "
            "+ verification_run_lease_reaper (60s) "
            "+ external_seed_catalog_materialization (15min, ACTIVE) "
            "+ metering_expire_reservations (5min, ACTIVE) "
            "+ stamp_attribution_reaper (5min, ACTIVE) "
            "+ gmv_aggregation_daily (02:00 UTC, ACTIVE) "
            "+ invoice_generation_monthly (day 2 03:00 UTC, PAUSED) "
            "+ partner_settlement_monthly (day 3 04:00 UTC, PAUSED) "
            "+ settlement_file_generate (day 5 02:00 UTC, ACTIVE) "
            "+ settlement_file_transfer (day 10 02:00 UTC, ACTIVE) "
            "+ catalog_row_trust_backfill (cron */6h :17, ACTIVE) "
            "+ agent_pdp_view_reconcile (cron */6h :43, ACTIVE) "
            "+ payment_reconcile_tick (5min, flag-gated PAYMENT_RECONCILE_SWEEP_ENABLED) "
            "+ identity_reconcile_sweep (Mon 04:30 UTC, flag-gated ENABLE_IDENTITY_RECONCILE_SWEEP)"
        )
    except Exception as exc:  # noqa: BLE001
        _BOOT_ERROR = repr(exc)
        logger.warning(
            "audit_scheduler: start failed (continuing degraded): %s",
            exc,
        )


async def restart_scheduler() -> dict:
    """Force a clean re-init of the scheduler without a redeploy: shut down the
    current instance (if any), clear the module state, and re-run
    start_scheduler(). Returns the post-restart diagnostics. Admin-triggered
    recovery lever for the silent-stall class."""
    global _SCHEDULER
    old = _SCHEDULER
    if old is not None:
        try:
            old.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit_scheduler: shutdown during restart failed: %s", exc)
    _SCHEDULER = None
    await start_scheduler()
    return scheduler_diagnostics()


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
