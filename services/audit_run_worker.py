"""Phase 2.2 — async audit-run worker loop.

Drives queued audits through the 9-stage state machine introduced in
P2.1 (db/merchant_audit_runs.py). Driven by APScheduler at a 10s
interval. Each tick claims at most N runs, processes them serially in
this process, and yields. Multiple worker processes coexist safely
via the SKIP LOCKED claim pattern.

Stages owned by this worker:
  queued
   → discovering   (resolve merchant + product rows from catalog)
   → probing       (run_brand_report — the LLM-heavy stage)
   → scoring       (aggregate, included in run_brand_report today)
   → materializing (dispatch executor agents + materialize tasks)
   → verifying     (co-occurrence + GSC URL submission)
   → completed | failed | cancelled

Production traffic does not flow through this worker until P2.3 ships
POST /api/audits to enqueue rows in stage='queued'. Until then the
worker ticks against an empty queue, which is a safe no-op.

Resume semantics:
  - claim_next_pending_run picks up rows in ANY active stage where the
    lease has expired (not just queued). On worker crash mid-stage, a
    sibling worker resumes from the last persisted stage.
  - Cost trade-off: stages are not strictly idempotent (run_brand_report
    triggers fresh LLM calls). For now, resume re-runs the in-flight
    stage from scratch. P2.5 cost telemetry will let us decide whether
    fine-grained per-stage idempotency is worth the complexity.
"""

from __future__ import annotations

import logging
import asyncio
import os
import socket
import traceback
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Worker identity. Combines hostname + pid + a random suffix so two
# pods on the same node + workers respawning on the same pid don't
# collide. The lease arbitration only compares string equality.
WORKER_ID = (
    f"{socket.gethostname()}_"
    f"{os.getpid()}_"
    f"{uuid.uuid4().hex[:8]}"
)


# Cap per-tick processing so one tick can't monopolize the asyncio
# loop. With 10s tick interval + ~3 runs per tick, sustained load
# capacity is ~18 runs/min per worker. Scale horizontally past that.
MAX_RUNS_PER_TICK = 3

# Long stages (probing, materializing) extend the lease before
# starting since run_brand_report can take several minutes for a
# 5-product cold-start audit with grounded category visibility.
LONG_STAGE_LEASE_SECONDS = 900  # 15 minutes


async def process_one_audit_run() -> bool:
    """Claim + process exactly one queued (or stale-leased) run.
    Returns True iff a run was claimed (regardless of success), so the
    tick loop can decide whether to look for more.
    """
    from db import merchant_audit_runs as mar

    claimed = await mar.claim_next_pending_run(worker_id=WORKER_ID)
    if claimed is None:
        return False

    run_id = claimed["run_id"]
    merchant_id = claimed["merchant_id"]
    product_keys: List[str] = claimed.get("product_keys") or []
    starting_stage = claimed.get("stage") or mar.STAGE_QUEUED
    launch_options = (
        (claimed.get("partial_result_jsonb") or {}).get("launch") or {}
        if isinstance(claimed.get("partial_result_jsonb"), dict) else {}
    )

    logger.info(
        "audit_run_worker: claimed run_id=%s merchant=%s "
        "stage=%s products=%d",
        run_id, merchant_id, starting_stage, len(product_keys),
    )

    # current_stage tracks the worker's last successful transition so
    # the failure path knows where to record error_jsonb.
    current_stage = starting_stage
    brand_report: Optional[Dict[str, Any]] = None
    products: List[Dict[str, Any]] = []
    pivota_url_used: List[str] = []
    merchant_name: str = merchant_id
    merchant_domain: Optional[str] = None
    integration_state: Optional[Dict[str, Any]] = None

    # P2.5b: scope every probe call inside this audit run with the
    # audit_run_id + merchant_id so probe call sites can attribute
    # cost telemetry without threading params through 4–6 stack frames.
    from services.audit_telemetry_context import audit_telemetry
    async with audit_telemetry(
        run_id=run_id, merchant_id=merchant_id,
    ):
        return await _process_one_audit_run_inner(
            run_id=run_id,
            merchant_id=merchant_id,
            product_keys=product_keys,
            current_stage=current_stage,
            brand_report=brand_report,
            products=products,
            pivota_url_used=pivota_url_used,
            merchant_name=merchant_name,
            merchant_domain=merchant_domain,
            integration_state=integration_state,
            launch_options=launch_options,
        )


def _run_errored(run: Any) -> bool:
    """True when a grounded run carried an upstream error instead of an answer.
    The gateway returns HTTP 200 with `raw` prefixed `__error__:` (e.g. an
    OpenAI 429 quota error) rather than raising, so such a run is NOT real
    grounded evidence."""
    if not isinstance(run, dict):
        return False
    if run.get("error"):
        return True
    raw = run.get("raw")
    return isinstance(raw, str) and raw.startswith("__error__")


def _all_per_sku_probes_failed(
    probe_runs_by_sku: Dict[str, Any],
) -> bool:
    """True when the producer collected ZERO *successful* grounded runs and at
    least one probe failed — e.g. the probe-auth key is missing, or every run
    came back as an upstream error (OpenAI 429 quota).

    A successful probe returns raw_runs carrying real answers; a failed probe
    returns status='probe_failed' with 0 raw_runs, and a wholesale upstream
    failure returns HTTP 200 with raw_runs that are all `__error__:`-prefixed
    (usage.succeeded_runs==0). Counting only SUCCESSFUL runs — not raw_runs
    length — catches both shapes: the audit has nothing real to score and must
    NOT finalize as a 'succeeded' empty audit that reads to the merchant as
    'invisible in AI'. When at least one provider succeeded (e.g. Gemini works
    while ChatGPT 429s), this returns False and the run finalizes normally on
    the real evidence.
    """
    saw_failure = False
    total_success_runs = 0
    saw_any_payload = False
    for payloads in (probe_runs_by_sku or {}).values():
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            saw_any_payload = True
            if str(payload.get("status") or "").lower() == "probe_failed":
                saw_failure = True
                continue
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            succ = usage.get("succeeded_runs")
            failed = usage.get("failed_runs")
            raw_runs = [r for r in (payload.get("raw_runs") or []) if isinstance(r, dict)]
            if isinstance(succ, int) or isinstance(failed, int):
                s = int(succ or 0)
                f = int(failed or 0)
                total_success_runs += s
                if s == 0 and f > 0:
                    saw_failure = True
            else:
                # Older gateway response without per-run health: infer from the
                # `__error__:` markers on the raw runs themselves.
                ok = sum(1 for r in raw_runs if not _run_errored(r))
                total_success_runs += ok
                if raw_runs and ok == 0:
                    saw_failure = True
    return saw_any_payload and saw_failure and total_success_runs == 0


def _first_probe_failure_reason(probe_runs_by_sku: Dict[str, Any]) -> str:
    for payloads in (probe_runs_by_sku or {}).values():
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            err = str(payload.get("error") or "").strip()
            if err:
                return err[:300]
    return "all grounded probes failed"


async def _process_one_audit_run_inner(
    *,
    run_id: str,
    merchant_id: str,
    product_keys: List[str],
    current_stage: str,
    brand_report: Optional[Dict[str, Any]],
    products: List[Dict[str, Any]],
    pivota_url_used: List[str],
    merchant_name: str,
    merchant_domain: Optional[str],
    integration_state: Optional[Dict[str, Any]],
    launch_options: Optional[Dict[str, Any]] = None,
) -> bool:
    """The actual stage-driving body, scoped inside an
    audit_telemetry() context manager so probe call sites can read
    audit_run_id + merchant_id via contextvars.
    """
    from db import merchant_audit_runs as mar
    launch_options = launch_options or {}

    # URL-audit (wedge) runs carry synthetic products in the launch payload —
    # pasted product URLs with no synced catalog. Discovery builds products
    # from these instead of catalog rows, and materialize/verify are minimized
    # (no executors, no catalog-coupled evidence/projection writes, no GSC).
    synthetic_products = launch_options.get("synthetic_products") or []
    is_synthetic = bool(synthetic_products)

    # ----- P0-2 resume rehydrate -----
    # claim_next_pending_run will hand the worker a row at ANY active
    # stage, not just queued (so the stale-lease reaper can recover a
    # crashed worker). But local-memory state (brand_report, products,
    # merchant_name, etc.) starts fresh at default — so before the
    # original fix, resuming at probing meant re-running probes with
    # products=[] (empty audit), and resuming at scoring / materializing
    # / verifying meant the `brand_report is not None` guards skipped
    # every block and the run sat at stage=verifying forever.
    #
    # Strategy:
    #   - Resume at PROBING: re-run discovery (cheap, no LLM) to
    #     rehydrate products + merchant state, then continue. The
    #     probing work itself was either incomplete (lease expired
    #     before run_brand_report finished) or completed but not yet
    #     persisted, so it MUST be re-run; we can't dedupe at the
    #     probe layer today.
    #   - Resume at SCORING / MATERIALIZING / VERIFYING: brand_report
    #     was never persisted before the lease expired (today, it
    #     only lands on the row at _record_final_report_fields, which
    #     runs late inside the verifying block). Without brand_report
    #     we can't continue safely — silent skips of the guarded
    #     blocks would leave the run terminal-empty. Fail the run
    #     with a clear error so the merchant sees the issue and can
    #     retry, instead of staying stuck or completing with no work.
    if current_stage in (
        mar.STAGE_PROBING,
        mar.STAGE_SCORING,
        mar.STAGE_MATERIALIZING,
        mar.STAGE_VERIFYING,
    ):
        logger.info(
            "audit_run_worker: resume claim at stage=%s run_id=%s "
            "(stale-lease replay)", current_stage, run_id,
        )
        if current_stage == mar.STAGE_PROBING:
            try:
                if is_synthetic:
                    (
                        merchant_name,
                        merchant_domain,
                        products,
                        pivota_url_used,
                        integration_state,
                    ) = await _resolve_synthetic_url_products(
                        launch_options=launch_options,
                        merchant_id=merchant_id,
                    )
                else:
                    (
                        merchant_name,
                        merchant_domain,
                        products,
                        pivota_url_used,
                        integration_state,
                    ) = await _resolve_merchant_and_products(
                        merchant_id=merchant_id, product_keys=product_keys,
                    )
                logger.info(
                    "audit_run_worker: rehydrated discovery for "
                    "run_id=%s products=%d", run_id, len(products),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "audit_run_worker: discovery rehydrate failed "
                    "for run_id=%s — failing run", run_id,
                )
                await _fail_run_and_refund(
                    run_id=run_id,
                    merchant_id=merchant_id,
                    launch_options=launch_options,
                    from_stage=mar.STAGE_PROBING,
                    error_jsonb={
                        "stage": "probing_resume_rehydrate",
                        "message": (
                            f"discovery rehydrate failed on stale-lease "
                            f"replay: {str(exc)[:200]}"
                        ),
                    },
                    reason="resume_rehydrate_failed",
                )
                return True
        else:
            # Cannot reconstruct brand_report; fail cleanly (and refund —
            # the merchant is asked to re-submit, which charges again).
            await _fail_run_and_refund(
                run_id=run_id,
                merchant_id=merchant_id,
                launch_options=launch_options,
                from_stage=current_stage,
                error_jsonb={
                    "stage": f"{current_stage}_resume_unsupported",
                    "message": (
                        f"Worker crashed mid-pipeline at stage="
                        f"{current_stage}. brand_report is not "
                        "persisted before record_final_report_fields, "
                        "so the run cannot be safely resumed without "
                        "re-running probing (which would double LLM "
                        "cost on an audit that was already past that "
                        "stage). Please re-submit the audit."
                    ),
                },
                reason="resume_unsupported",
            )
            return True

    async def _check_cancellation_and_finalize(*, at_stage: str) -> bool:
        """If the run has been cancelled (cancel_audit_run set
        cancelled_at on this row), finalize it to STAGE_CANCELLED and
        return True so the caller can bail. Returns False when no
        cancellation has been requested.

        Race-safe: a sibling worker that stole the lease can also race
        the transition_stage; one will win, the other will see
        ok=False and bail. Either outcome leaves the row at the
        terminal cancelled stage (the only goal here)."""
        try:
            row = await mar.fetch_audit_run_by_id(run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit_run_worker: cancellation poll fetch failed "
                "run_id=%s: %s", run_id, str(exc)[:200],
            )
            return False
        if row is None or row.get("cancelled_at") is None:
            return False
        # Use a finalize transition (no completed_at side-effects from
        # outside; transition_stage sets completed_at for terminal
        # stages itself).
        ok = await mar.transition_stage(
            run_id=run_id,
            from_stage=at_stage,
            to_stage=mar.STAGE_CANCELLED,
            worker_id=WORKER_ID,
        )
        if ok and _should_refund_cancelled_launch(row, at_stage=at_stage):
            await _refund_launch_debits(
                merchant_id=merchant_id,
                run_id=run_id,
                launch_options=launch_options,
                reason=f"cancelled_{at_stage}",
            )
        logger.info(
            "audit_run_worker: cancellation detected at stage=%s "
            "run_id=%s finalized=%s",
            at_stage, run_id, ok,
        )
        return True

    try:
        # ----- queued → discovering -----
        if current_stage == mar.STAGE_QUEUED:
            if await _check_cancellation_and_finalize(at_stage=mar.STAGE_QUEUED):
                return True
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_QUEUED,
                to_stage=mar.STAGE_DISCOVERING,
                worker_id=WORKER_ID,
            )
            if not ok:
                logger.info(
                    "audit_run_worker: lost lease at queued→discovering "
                    "for run_id=%s (cancelled or stolen)", run_id,
                )
                return True
            current_stage = mar.STAGE_DISCOVERING

        # ----- discovering: resolve merchant + product catalog rows -----
        if current_stage == mar.STAGE_DISCOVERING:
            if await _check_cancellation_and_finalize(
                at_stage=mar.STAGE_DISCOVERING,
            ):
                return True
            await mar.extend_lease(run_id=run_id, worker_id=WORKER_ID)
            if is_synthetic:
                (
                    merchant_name,
                    merchant_domain,
                    products,
                    pivota_url_used,
                    integration_state,
                ) = await _resolve_synthetic_url_products(
                    launch_options=launch_options,
                    merchant_id=merchant_id,
                )
                # The audit IS the index-build motion: auto-seed each pasted
                # product into the commerce index as an OBSERVED, unclaimed
                # catalog row (flag-gated, best-effort — never breaks the audit).
                # This is what gives a URL-audited product a durable canonical
                # record the merchant can later attach proof/claims to.
                await _seed_url_audit_index(
                    merchant_id=merchant_id,
                    synthetic_items=launch_options.get("synthetic_products") or [],
                )
            else:
                (
                    merchant_name,
                    merchant_domain,
                    products,
                    pivota_url_used,
                    integration_state,
                ) = await _resolve_merchant_and_products(
                    merchant_id=merchant_id, product_keys=product_keys,
                )
            await mar.record_partial_result(
                run_id=run_id, worker_id=WORKER_ID,
                partial_result_jsonb={
                    "discovering": {
                        "merchant_name": merchant_name,
                        "merchant_domain": merchant_domain,
                        "products_resolved": len(products),
                        "audited_via_pivota_canonical": pivota_url_used,
                    },
                },
            )
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_DISCOVERING,
                to_stage=mar.STAGE_PROBING,
                worker_id=WORKER_ID,
            )
            if not ok:
                return True
            current_stage = mar.STAGE_PROBING

        # ----- probing: run_brand_report (LLM-heavy) -----
        if current_stage == mar.STAGE_PROBING:
            if await _check_cancellation_and_finalize(
                at_stage=mar.STAGE_PROBING,
            ):
                return True
            await mar.extend_lease(
                run_id=run_id, worker_id=WORKER_ID,
                lease_seconds=LONG_STAGE_LEASE_SECONDS,
            )
            from services.agent_center_bd_report_service import (
                run_brand_report,
                run_per_sku_audit_probe_fanout,
            )
            # P5.8.6c: lease-heartbeat task. Original code called
            # extend_lease ONCE at the start of probing (15min
            # lease). For a 5-product cold-start audit with
            # grounded category visibility, run_brand_report
            # routinely runs >15min; lease expires mid-probe,
            # reaper releases, sibling worker reclaims, RE-RUNS
            # all the LLM probes from scratch — doubles LLM cost
            # + the original worker's results vanish (subsequent
            # guarded writes fail). Heartbeat: extend every 5min,
            # cancel when run_brand_report returns. Per
            # feedback_llm_call_multipliers.md this is the
            # category PR #278 hit.
            heartbeat_task = asyncio.create_task(
                _lease_heartbeat(
                    run_id=run_id,
                    interval_seconds=300,  # 5 min
                    lease_seconds=LONG_STAGE_LEASE_SECONDS,
                ),
                name=f"audit-lease-heartbeat-{run_id[:8]}",
            )
            try:
                audit_mode = _launch_audit_mode(launch_options)
                coverage_profile = (
                    launch_options.get("coverage_profile")
                    or "us_shopper"
                )
                audit_tier = _launch_audit_tier(launch_options)
                prompts_per_sku = _launch_prompts_per_sku(
                    launch_options, audit_tier
                )
                prior_runs = await mar.recent_runs_for_merchant(
                    merchant_id=merchant_id, limit=5,
                )
                prior_runs = [
                    row for row in prior_runs
                    if row.get("run_id") != run_id
                ]
                if audit_mode == "per_sku":
                    from config.settings import settings

                    probe_runs_by_sku = await run_per_sku_audit_probe_fanout(
                        merchant_id=merchant_id,
                        audit_run_id=run_id,
                        products=products,
                        coverage_profile=coverage_profile,
                        providers=launch_options.get("providers"),
                        model_overrides=launch_options.get("model_overrides"),
                        prompts_per_sku=prompts_per_sku,
                        # Merchant-input prompt slots (debited as prompt credits
                        # at enqueue). Probe them so they aren't billed-but-dropped.
                        custom_prompts=launch_options.get("custom_prompts"),
                        # Per-SKU merchant prompts (custom_prompts_by_url):
                        # probed inside their SKU's context + pinned into its
                        # basis for week-over-week tracking.
                        custom_prompts_by_sku=launch_options.get(
                            "custom_prompts_by_sku"
                        ),
                        # LLM value-prop extraction -> probe winnable SPECIFIC
                        # discovery prompts, not just generic heads. Default ON
                        # (settings.prompt_gen_enabled, env-killable); a launch
                        # option still overrides explicitly either way. Was
                        # opt-in — which left the paid catalog audit running on
                        # deterministic templates only.
                        winnable_prompts=bool(
                            launch_options.get(
                                "winnable_prompts",
                                getattr(settings, "prompt_gen_enabled", True),
                            )
                        ),
                        # Explicit re-audit refresh: regenerate the prompt basis
                        # instead of pinning a prior run's frozen query set.
                        # Default False -> every existing audit pins as before.
                        refresh_prompt_basis=bool(
                            launch_options.get("refresh_prompt_basis", False)
                        ),
                        # Depth tier: decides the probe budget above AND scopes
                        # basis pinning (a deep run never reuses a standard
                        # basis — tier switch = baseline reset).
                        audit_tier=audit_tier,
                        # Merchant/BD-declared competitors: lead the deep-tier
                        # anchor list ahead of the answer harvest (sanitized in
                        # the fanout; inert on standard runs).
                        declared_competitors=launch_options.get(
                            "declared_competitors"
                        ),
                    )
                    await mar.record_partial_result(
                        run_id=run_id,
                        worker_id=WORKER_ID,
                        partial_result_jsonb={
                            "per_sku_probe_runs": probe_runs_by_sku,
                            "probing": {
                                "audit_mode": "per_sku",
                                "audit_tier": audit_tier,
                                "prompts_per_sku": prompts_per_sku,
                                "per_sku_probe_payloads_persisted": sum(
                                    len(v) for v in probe_runs_by_sku.values()
                                ),
                                "sku_count": len(probe_runs_by_sku),
                            },
                        },
                    )
                    # Honesty gate: if EVERY probe failed (e.g. the probe-auth
                    # key is missing on this worker), there is zero grounded
                    # evidence. Do NOT finalize a 'succeeded' empty audit that
                    # reads as "merchant invisible" — fail loudly + refund the
                    # debited credits, like the mock-fallback guard below.
                    if _all_per_sku_probes_failed(probe_runs_by_sku):
                        reason = _first_probe_failure_reason(probe_runs_by_sku)
                        await _fail_run_and_refund(
                            run_id=run_id,
                            merchant_id=merchant_id,
                            launch_options=launch_options,
                            from_stage=mar.STAGE_PROBING,
                            error_jsonb={
                                "code": "probe_infra_failure",
                                "stage": "probing",
                                "message": (
                                    "All grounded probes failed — no AI-citation "
                                    "evidence collected, so this audit is not "
                                    "finalized as complete (a zero result here "
                                    "would falsely read as 'invisible in AI'). "
                                    "Most likely the probe-auth secret "
                                    "(PROMOTIONS_ADMIN_KEY) is unset on this "
                                    "worker. Re-run once it's configured."
                                ),
                                "reason": reason,
                            },
                            reason="probe_infra_failure",
                        )
                        logger.error(
                            "audit_run_worker: all per-SKU probes failed "
                            "run_id=%s merchant=%s reason=%s",
                            run_id, merchant_id, reason,
                        )
                        return True
                    brand_report = await run_brand_report(
                        merchant_name=str(merchant_name),
                        merchant_domain=merchant_domain,
                        products=products,
                        coverage_profile=coverage_profile,
                        providers=launch_options.get("providers"),
                        model_overrides=launch_options.get("model_overrides"),
                        prompts_per_sku=prompts_per_sku,
                        max_runs=prompts_per_sku,
                        integration_state=integration_state,
                        audit_mode="per_sku",
                        merchant_id=merchant_id,
                        audit_run_id=run_id,
                        prior_runs=prior_runs,
                        verify_providers=launch_options.get("verify_providers"),
                        # Surface the merchant's custom prompts as a per-lane
                        # results section (they were already probed above).
                        custom_prompts=launch_options.get("custom_prompts"),
                    )
                else:
                    brand_report = await run_brand_report(
                        merchant_name=str(merchant_name),
                        merchant_domain=merchant_domain,
                        products=products,
                        coverage_profile=coverage_profile,
                        providers=launch_options.get("providers"),
                        model_overrides=launch_options.get("model_overrides"),
                        prompts_per_sku=launch_options.get("prompts_per_sku"),
                        max_runs=3,
                        integration_state=integration_state,
                        audit_mode=audit_mode,
                        merchant_id=merchant_id,
                        audit_run_id=run_id,
                        prior_runs=prior_runs,
                    )
            finally:
                heartbeat_task.cancel()
                # Don't await — fire-and-forget cancellation.
                # The task's cleanup is best-effort.

            mock_reports = _detect_mock_audit_output(brand_report or {})
            if mock_reports:
                first_reason = (
                    (mock_reports[0].get("upstream_status") or {}).get("reason")
                    or "Upstream returned mock data."
                )
                await _fail_run_and_refund(
                    run_id=run_id,
                    merchant_id=merchant_id,
                    launch_options=launch_options,
                    from_stage=mar.STAGE_PROBING,
                    error_jsonb={
                        "code": "upstream_mock_fallback",
                        "stage": "probing",
                        "message": (
                            "Audit pipeline upstream returned synthetic "
                            "fallback data; refusing to persist a completed "
                            "merchant audit."
                        ),
                        "reason": first_reason,
                        "mock_reports_count": len(mock_reports),
                    },
                    reason="upstream_mock_fallback",
                )
                logger.error(
                    "audit_run_worker: refusing mock-derived audit "
                    "run_id=%s merchant=%s mock_reports=%d reason=%s",
                    run_id, merchant_id, len(mock_reports), first_reason,
                )
                return True
            aggregate = brand_report.get("aggregate") or {}
            await mar.record_partial_result(
                run_id=run_id, worker_id=WORKER_ID,
                partial_result_jsonb={
                    "probing": {
                        "audit_mode": brand_report.get("audit_mode") or "legacy",
                        "products_succeeded": aggregate.get("products_succeeded"),
                        "products_failed": aggregate.get("products_failed"),
                        "avg_visibility": aggregate.get("avg_visibility"),
                        "avg_attribution": aggregate.get("avg_attribution"),
                    },
                },
            )
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_PROBING,
                to_stage=mar.STAGE_SCORING,
                worker_id=WORKER_ID,
            )
            if not ok:
                return True
            current_stage = mar.STAGE_SCORING

        # ----- scoring: aggregate is already in run_brand_report's
        # output; this stage marks "report assembly done". Kept as a
        # discrete transition so the GET /api/audits/{id} timeline
        # has a clear marker between probing + materializing. -----
        if current_stage == mar.STAGE_SCORING:
            if await _check_cancellation_and_finalize(
                at_stage=mar.STAGE_SCORING,
            ):
                return True
            # Surface the scoring stage in partial_result_jsonb so
            # GET /api/audits/{id} has a marker between probing and
            # materializing — without this, the per-stage progress
            # UI shows a gap (Gate 5 run c7164016... was missing the
            # scoring key entirely). Best-effort: failure here doesn't
            # block the transition since record_partial_result is its
            # own try/except.
            if brand_report is not None:
                _agg = brand_report.get("aggregate") or {}
                await mar.record_partial_result(
                    run_id=run_id, worker_id=WORKER_ID,
                    partial_result_jsonb={
                        "scoring": {
                            "verdict_label": brand_report.get(
                                "verdict_label",
                            ),
                            "products_succeeded": _agg.get(
                                "products_succeeded",
                            ),
                            "products_failed": _agg.get(
                                "products_failed",
                            ),
                            "avg_visibility": _agg.get(
                                "avg_visibility",
                            ),
                            "avg_attribution": _agg.get(
                                "avg_attribution",
                            ),
                        },
                    },
                )
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_SCORING,
                to_stage=mar.STAGE_MATERIALIZING,
                worker_id=WORKER_ID,
            )
            if not ok:
                return True
            current_stage = mar.STAGE_MATERIALIZING

        # ----- materializing: tasks + executor dispatch -----
        if current_stage == mar.STAGE_MATERIALIZING and brand_report is not None:
            if await _check_cancellation_and_finalize(
                at_stage=mar.STAGE_MATERIALIZING,
            ):
                return True
            await mar.extend_lease(
                run_id=run_id, worker_id=WORKER_ID,
                lease_seconds=LONG_STAGE_LEASE_SECONDS,
            )
            # Persist report_jsonb BEFORE dispatch. Executor agents are enqueued
            # here but the executor_run_worker re-fetches report_jsonb from this
            # row at claim time; it isn't stored inline in the queue and doesn't
            # otherwise land until the verifying stage below. Without this, a
            # claimed executor run reads a NULL report and silently skips (the
            # content-brief agent's failed-query extraction returns [] →
            # status='skipped', no task, no error). Writing it before any
            # executor row exists closes the race. Best-effort — dispatch still
            # runs if it fails.
            await mar.persist_report_jsonb(
                run_id=run_id,
                worker_id=WORKER_ID,
                report_jsonb=brand_report,
            )
            if is_synthetic:
                # W5.4: URL-audit runs DELIVER the full URL-tier executor set —
                # content briefs + competitor insights (off the pasted URL's OWN
                # report) PLUS canonical_pdp_enrichment + gsc_url_submission_loop,
                # which act on the url_audit SEED now that P3 mints its Pivota
                # canonical identity (fatten the thin PDP → index_eligible; submit
                # the canonical URL). sitemap_freshness stays excluded (no
                # connected storefront). Each seed-aware agent self-gates in
                # should_run + the global kill-switch still governs them all.
                from services.executor_agents.dispatcher import (
                    URL_AUDIT_EXECUTORS,
                )

                tasks_summary = await _materialize_tasks_and_executors(
                    merchant_id=merchant_id,
                    run_id=run_id,
                    brand_report=brand_report,
                    integration_state=integration_state,
                    agent_names=URL_AUDIT_EXECUTORS,
                    dispatch_only=True,
                )
            else:
                tasks_summary = await _materialize_tasks_and_executors(
                    merchant_id=merchant_id,
                    run_id=run_id,
                    brand_report=brand_report,
                    integration_state=integration_state,
                )
            await mar.record_partial_result(
                run_id=run_id, worker_id=WORKER_ID,
                partial_result_jsonb={"materializing": tasks_summary},
            )
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_MATERIALIZING,
                to_stage=mar.STAGE_VERIFYING,
                worker_id=WORKER_ID,
            )
            if not ok:
                return True
            current_stage = mar.STAGE_VERIFYING

        # ----- verifying: co-occurrence + GSC URL submission +
        # P4.3 shadow-write evidence_items + readiness_findings -----
        if current_stage == mar.STAGE_VERIFYING and brand_report is not None:
            if await _check_cancellation_and_finalize(
                at_stage=mar.STAGE_VERIFYING,
            ):
                return True
            if is_synthetic:
                # URL-audit minimal completion: skip canonical-evidence,
                # verifiers, audience projections, and verification-enqueue —
                # all catalog-coupled, and the /url-readiness GET reads
                # report_jsonb directly (no projection needed). Crucially, the
                # post-processing block below FAILS the whole run if projection
                # build or verification-enqueue raises (which they can on
                # synthetic product_keys), so synthetic runs must not enter it.
                await _record_final_report_fields(
                    run_id=run_id,
                    brand_report=brand_report,
                    pivota_url_used=pivota_url_used,
                )
                # W5 P4.2: submit the seeded url_audit canonical URLs (the only
                # place a url_audit run does so — it never enters _run_verifiers).
                # Self-gates on GSC_PIVOTA_SUBMIT_ENABLED so it lands INERT today.
                await _submit_url_audit_seed_canonical_urls(
                    merchant_id=merchant_id,
                    synthetic_products=synthetic_products,
                    run_id=run_id,
                )
                cost_summary = await _aggregate_cost_summary_for_run(
                    run_id=run_id, brand_report=brand_report,
                )
                await mar.record_partial_result(
                    run_id=run_id, worker_id=WORKER_ID,
                    partial_result_jsonb={"verifying": {"skipped": "url_audit"}},
                )
                ok = await mar.transition_stage(
                    run_id=run_id,
                    from_stage=mar.STAGE_VERIFYING,
                    to_stage=mar.STAGE_COMPLETED,
                    worker_id=WORKER_ID,
                    cost_summary_jsonb=cost_summary,
                )
                if ok:
                    from services.agent_center_bd_report_service import (
                        clear_synthetic_sku_contexts,
                    )
                    clear_synthetic_sku_contexts(
                        [
                            str(p.get("sku_key") or "")
                            for p in synthetic_products
                        ],
                        merchant_id,
                    )
                logger.info(
                    "audit_run_worker: completed url-audit run_id=%s "
                    "merchant=%s products=%d", run_id, merchant_id,
                    len(synthetic_products),
                )
                return True
            # P4.3: derive canonical evidence + findings from the
            # brand_report and persist into the new tables. Best-
            # effort — failures inside the builder don't poison the
            # rest of verifying. Phase 6 will retire the legacy
            # report_jsonb once consumers migrate.
            try:
                from services.audit_evidence_builder import (
                    persist_canonical_evidence,
                )
                # P5.8.1/2: pass merchant_id so canonical rows carry
                # tenant scope at the column level + idempotency keys
                # prevent doubling on worker crash + reclaim.
                canonical_summary = await persist_canonical_evidence(
                    audit_run_id=run_id,
                    brand_report=brand_report,
                    merchant_id=merchant_id,
                )
                logger.info(
                    "audit_run_worker: canonical evidence persisted "
                    "for run_id=%s: %s", run_id, canonical_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audit_run_worker: persist_canonical_evidence "
                    "raised for run_id=%s: %s", run_id, exc,
                )
                canonical_summary = {
                    "evidence_items_inserted": 0,
                    "findings_inserted": 0,
                    "error": str(exc)[:200],
                }

            verify_summary = await _run_verifiers(
                merchant_id=merchant_id,
                run_id=run_id,
                brand_report=brand_report,
                merchant_name=merchant_name,
            )
            # P4.3: surface canonical-evidence write counts in the
            # verifying partial_result so GET /api/audits/{id} can
            # show whether the shadow-write succeeded.
            verify_summary["canonical_evidence"] = canonical_summary
            await mar.record_partial_result(
                run_id=run_id, worker_id=WORKER_ID,
                partial_result_jsonb={"verifying": verify_summary},
            )

            # Persist final aggregate fields the legacy history endpoint
            # reads (visibility/attribution/category/verdict_labels +
            # report_jsonb) so old consumers still see a populated
            # row even before they migrate to the new fetch shape.
            await _record_final_report_fields(
                run_id=run_id,
                brand_report=brand_report,
                pivota_url_used=pivota_url_used,
            )
            cost_summary = await _aggregate_cost_summary_for_run(
                run_id=run_id, brand_report=brand_report,
            )

            # P5.8.6c: post-completion durability. Previously
            # projection-build + verification-enqueue ran AFTER
            # transition_stage(completed). If the worker crashed
            # between transition + enqueue, the audit was "done"
            # but NO verifications ever fired (and the projection
            # cache stayed empty). Nothing back-filled because the
            # reaper only reclaims active stages.
            #
            # Fix: do projection-build + verification-enqueue
            # BEFORE the transition_stage(completed). The work is
            # idempotent (P5.8.2 made evidence/findings/actions
            # idempotent; upsert_projection has UNIQUE
            # (audit_run_id, audience); enqueue_verifications uses
            # idempotency keys). If the worker crashes mid-step,
            # the reaper releases the lease, a sibling worker
            # reclaims at stage=verifying, re-runs the side effects
            # (cheap dedupe-noops), then transitions cleanly to
            # completed.
            # P1-4: track post-processing success. Before this fix,
            # both `build_and_persist_all_projections` and
            # `enqueue_verifications_for_completed_audit` were
            # wrapped in `except Exception: pass`-style swallows.
            # The run still transitioned to STAGE_COMPLETED with an
            # empty audit_projections row and no verification work
            # queued. Client GETs with `?audience=merchant` then
            # 409'd because the projection wasn't built; verifiers
            # never ran. Codex P1-4: treat required post-processing
            # as part of the completion contract — if either side
            # effect fails, FAIL the run with a clear error_jsonb so
            # the merchant sees the failure and can re-submit.
            post_processing_errors: List[str] = []

            try:
                from services.audit_projection_builder import (
                    build_and_persist_all_projections,
                )
                proj_summary = await build_and_persist_all_projections(
                    audit_run_id=run_id,
                )
                logger.info(
                    "audit_run_worker: projections built for run_id=%s: %s",
                    run_id, proj_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audit_run_worker: projection build raised for "
                    "run_id=%s: %s", run_id, exc,
                )
                post_processing_errors.append(
                    f"build_and_persist_all_projections: {str(exc)[:200]}"
                )

            try:
                from services.audit_verification_enqueuer import (
                    enqueue_verifications_for_completed_audit,
                )
                from datetime import datetime as _dt, timezone as _tz
                verifications_summary = (
                    await enqueue_verifications_for_completed_audit(
                        audit_run_id=run_id,
                        merchant_id=merchant_id,
                        product_keys=product_keys,
                        completed_at=_dt.now(_tz.utc),
                    )
                )
                logger.info(
                    "audit_run_worker: verifications enqueued "
                    "for run_id=%s: %s", run_id, verifications_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audit_run_worker: verification enqueue raised "
                    "for run_id=%s: %s", run_id, exc,
                )
                post_processing_errors.append(
                    "enqueue_verifications_for_completed_audit: "
                    f"{str(exc)[:200]}"
                )

            if post_processing_errors:
                # Post-processing failure → run fails. The audit's
                # report_jsonb / per-product fields are already
                # persisted via _record_final_report_fields above,
                # so the merchant's GET still surfaces the canonical
                # shape; what's missing is the audience projections
                # and the queued verifications. Re-submitting the
                # audit is the right recovery path; the idempotency
                # key changes per the 5-minute window so a re-submit
                # within that window will dedupe to this failed run
                # (need force=true to bypass).
                await _fail_run_and_refund(
                    run_id=run_id,
                    merchant_id=merchant_id,
                    launch_options=launch_options,
                    from_stage=mar.STAGE_VERIFYING,
                    cost_summary_jsonb=cost_summary,
                    error_jsonb={
                        "stage": "verifying_post_processing",
                        "message": (
                            "Audit ran successfully but post-processing "
                            "(projection build + verification enqueue) "
                            "raised. The canonical report is in "
                            "report_jsonb but audience projections are "
                            "not built. Re-submit with force=true to "
                            "retry."
                        ),
                        "errors": post_processing_errors,
                    },
                    # The run is surfaced to the merchant as FAILED (the
                    # poller reads terminal status), and the prescribed
                    # recovery — re-submit — charges again. Refund so one
                    # delivered report never costs two runs' credits.
                    reason="verifying_post_processing",
                )
                logger.warning(
                    "audit_run_worker: failing run_id=%s due to %d "
                    "post-processing errors", run_id,
                    len(post_processing_errors),
                )
                return True

            # Only NOW transition to completed. If we reach this
            # line, projections are warm + verifications are
            # enqueued. The transition is the atomic "this audit
            # is done" commit point.
            ok = await mar.transition_stage(
                run_id=run_id,
                from_stage=mar.STAGE_VERIFYING,
                to_stage=mar.STAGE_COMPLETED,
                worker_id=WORKER_ID,
                cost_summary_jsonb=cost_summary,
            )
            if not ok:
                return True
            current_stage = mar.STAGE_COMPLETED

        logger.info(
            "audit_run_worker: completed run_id=%s merchant=%s",
            run_id, merchant_id,
        )
        return True

    except Exception as exc:  # noqa: BLE001 — top-level worker handler
        logger.exception(
            "audit_run_worker: failed run_id=%s at stage=%s",
            run_id, current_stage,
        )
        # Best-effort transition to FAILED + refund (charged iff
        # delivered). If the lease was lost in the meantime the
        # transition returns False and no refund fires from here —
        # the worker that owns the run (or the abandoned reaper)
        # handles its terminal state and refund instead.
        await _fail_run_and_refund(
            run_id=run_id,
            merchant_id=merchant_id,
            launch_options=launch_options,
            from_stage=current_stage,
            error_jsonb={
                "stage": current_stage,
                "message": str(exc)[:1000],
                "traceback_truncated": traceback.format_exc()[:2000],
            },
            reason="worker_exception",
        )
        return True


async def run_audit_worker_tick() -> None:
    """APScheduler-callable tick. Drains up to MAX_RUNS_PER_TICK runs
    serially, then yields control. Failures inside one run don't
    block subsequent runs in the same tick.
    """
    for _ in range(MAX_RUNS_PER_TICK):
        try:
            processed = await process_one_audit_run()
        except Exception:  # noqa: BLE001
            logger.exception("audit_run_worker: tick handler error")
            return
        if not processed:
            return  # queue empty


async def run_stale_lease_reaper_tick() -> None:
    """APScheduler-callable backstop reaper. claim_next_pending_run
    already tolerates stale leases inline, so this exists mostly to
    surface telemetry + guarantee progress when a worker hangs in a
    way that prevents it from re-claiming.
    """
    try:
        from db.merchant_audit_runs import release_stale_leases
        n = await release_stale_leases()
        if n > 0:
            logger.info(
                "audit_run_worker: reaper released %d stale leases", n,
            )
    except Exception:  # noqa: BLE001
        logger.exception("audit_run_worker: reaper failed")


async def run_abandoned_run_reaper_tick() -> None:
    """APScheduler-callable terminal reaper. Force-fails runs stuck
    status='running' past an absolute age with no live lease — the
    abandoned-asyncio URL-audit wedge runs and pre-lease-era orphans
    that release_stale_leases() (lease-expiry only, re-queue only)
    can't reach. Without this a dead run sits 'running' forever (we
    observed 10- and 40-day-old rows in prod).
    """
    try:
        from db.merchant_audit_runs import fail_abandoned_runs
        reaped = await fail_abandoned_runs()
        if reaped:
            logger.info(
                "audit_run_worker: reaper failed %d abandoned runs",
                len(reaped),
            )
        # Charged iff delivered: a reaped run delivered nothing, so its
        # launch debit is refunded. The per-run+kind refund key makes
        # this a no-op for any run another path already refunded.
        for row in reaped:
            run_id = str(row.get("run_id") or "")
            merchant_id = str(row.get("merchant_id") or "")
            launch = row.get("launch_options")
            if not run_id or not merchant_id:
                continue
            await _refund_launch_debits(
                merchant_id=merchant_id,
                run_id=run_id,
                launch_options=launch if isinstance(launch, dict) else {},
                reason="audit_abandoned_reaped",
            )
    except Exception:  # noqa: BLE001
        logger.exception("audit_run_worker: abandoned-run reaper failed")


# =====================================================================
# Internal helpers — pipeline stage implementations
# =====================================================================


def _launch_audit_mode(launch_options: Dict[str, Any]) -> str:
    mode = str((launch_options or {}).get("audit_mode") or "legacy").strip().lower()
    return "per_sku" if mode == "per_sku" else "legacy"


def _launch_audit_tier(launch_options: Dict[str, Any]) -> str:
    """Depth tier for this run — the PERSISTED launch tier, trusted as-is.

    AUDIT_DEEP_TIER_ENABLED is enforced at the LAUNCH boundary
    (routes/audit_runs_routes._resolve_request_audit_tier, a 422 before any
    debit), not re-checked here: the launch was gate-checked AND billed at the
    deep budget, so a claim-time degrade (flag flipped between enqueue and
    claim, or a replica missing the env var — the PIVOTA_AGENT_INTERNAL_API_KEY
    incident's exact drift shape) would silently deliver a standard run against
    a deep debit. Unknown/absent values normalize to standard."""
    from services.prompt_basis import normalize_audit_tier

    return normalize_audit_tier((launch_options or {}).get("audit_tier"))


def _launch_prompts_per_sku(
    launch_options: Dict[str, Any], audit_tier: str = "standard"
) -> int:
    """Explicit prompts_per_sku (internal/testing knob) wins; otherwise the
    tier decides (standard 40, deep 80). Merchants never set a raw count.

    CAUTION for the slice that wires audit_tier into a launch route:
    routes/audit_runs_routes.py today ALWAYS persists prompts_per_sku (Pydantic
    default 40), which this precedence reads as an operator override — a deep
    launch through that route unchanged would probe 40 prompts while pinning a
    deep-scoped basis. That route must omit prompts_per_sku when it equals the
    standard default (or stop sending it) before it sends audit_tier."""
    from services.prompt_basis import prompts_per_sku_for_tier

    default = prompts_per_sku_for_tier(audit_tier)
    try:
        return max(1, int((launch_options or {}).get("prompts_per_sku") or default))
    except (TypeError, ValueError):
        return default


def _launch_debit_items(launch_options: Dict[str, Any]) -> List[Dict[str, Any]]:
    launch = launch_options or {}
    debited = launch.get("debited")
    if isinstance(debited, list) and debited:
        return [item for item in debited if isinstance(item, dict)]
    fallback: List[Dict[str, Any]] = []
    for kind, key in (
        ("audit", "estimated_audit_credits"),
        ("prompt", "estimated_prompt_credits"),
        ("execution", "estimated_execution_credits"),
    ):
        try:
            amount = int(launch.get(key) or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            fallback.append({
                "kind": kind,
                "amount": amount,
                "replay": False,
                "purchased_credits": 0,
            })
    return fallback


async def _refund_launch_debits(
    *,
    merchant_id: str,
    run_id: str,
    launch_options: Dict[str, Any],
    reason: str,
) -> None:
    """Refund every launch debit for a run that delivered nothing.

    Idempotent PER RUN + CREDIT KIND: the ledger dedupes on source_event_id,
    and the key deliberately excludes `reason` so two different failure paths
    reaching the same run (e.g. the generic worker handler AND the abandoned
    reaper) can both call this safely — the second call is a no-op instead of
    a double refund. `reason` is logged for diagnosis, never keyed.

    Best-effort per item: one kind's refund failing must not block the rest
    (nor mask the failure path that triggered the refund).
    """
    items = _launch_debit_items(launch_options)
    if not items:
        logger.info(
            "audit_run_worker: no launch debit metadata to refund "
            "run_id=%s reason=%s",
            run_id, reason,
        )
        return
    from services.merchant_credit_balance_service import credit
    for item in reversed(items):
        if item.get("replay"):
            continue
        kind = str(item.get("kind") or "").strip()
        try:
            amount = int(item.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if not kind or amount <= 0:
            continue
        try:
            purchased_credits = int(item.get("purchased_credits") or 0)
        except (TypeError, ValueError):
            purchased_credits = 0
        try:
            await credit(
                merchant_id,
                kind,  # type: ignore[arg-type]
                amount,
                source_event_id=f"refund:audit_run:{run_id}:{kind}",
                usd_cogs=0,
                purchased_credits=purchased_credits,
            )
            logger.info(
                "audit_run_worker: refunded %d %s credits run_id=%s "
                "reason=%s", amount, kind, run_id, reason,
            )
        except Exception:  # noqa: BLE001 — refund must not mask the failure
            logger.exception(
                "audit_run_worker: refund failed run_id=%s kind=%s "
                "reason=%s — RECONCILE MANUALLY", run_id, kind, reason,
            )


async def _fail_run_and_refund(
    *,
    run_id: str,
    merchant_id: str,
    launch_options: Dict[str, Any],
    from_stage: str,
    error_jsonb: Dict[str, Any],
    reason: str,
    cost_summary_jsonb: Optional[Dict[str, Any]] = None,
) -> bool:
    """THE single failure exit for a worker-processed audit run.

    Transitions the run to FAILED and refunds its launch debits — enforcing
    the billing invariant: a merchant is charged iff a completed report was
    delivered. Every failure path in this module MUST exit through here (a
    source-scan test enforces it) so no future failure branch can silently
    keep the merchant's credits.

    Refunds only when THIS worker won the terminal transition — if another
    worker already finalized the run, that worker owns (or owned) the refund,
    and the refund key (per run+kind) makes a double call a no-op anyway.
    Returns whether the transition succeeded.
    """
    from db import merchant_audit_runs as mar

    ok = False
    try:
        ok = await mar.transition_stage(
            run_id=run_id,
            from_stage=from_stage,
            to_stage=mar.STAGE_FAILED,
            worker_id=WORKER_ID,
            error_jsonb=error_jsonb,
            cost_summary_jsonb=cost_summary_jsonb,
        )
    except Exception as inner:  # noqa: BLE001 — best-effort terminal write
        logger.warning(
            "audit_run_worker: failed persisting failed-state for "
            "run_id=%s reason=%s: %s", run_id, reason, inner,
        )
    if ok:
        await _refund_launch_debits(
            merchant_id=merchant_id,
            run_id=run_id,
            launch_options=launch_options,
            reason=reason,
        )
    return ok


def _has_recorded_probe_payloads(partial_result_jsonb: Any) -> bool:
    if not isinstance(partial_result_jsonb, dict):
        return False
    payload = partial_result_jsonb.get("per_sku_probe_runs")
    if isinstance(payload, dict) and any(payload.values()):
        return True
    probing = partial_result_jsonb.get("probing")
    if isinstance(probing, dict):
        try:
            return int(probing.get("per_sku_probe_payloads_persisted") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def _should_refund_cancelled_launch(
    row: Dict[str, Any],
    *,
    at_stage: str,
) -> bool:
    if at_stage in {"queued", "discovering"}:
        return True
    if at_stage == "probing":
        return not _has_recorded_probe_payloads(
            (row or {}).get("partial_result_jsonb")
        )
    return False


def _mock_provider_reason(provider: Any) -> Optional[str]:
    value = str(provider or "").strip().lower()
    if not value:
        return None
    if value == "mock" or value.startswith("mock_"):
        return value
    if value.startswith("local_mock"):
        return value
    return None


def _detect_mock_provider_markers(value: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_reason = _mock_provider_reason(key)
            if key_reason:
                found.append({
                    "upstream_status": {
                        "is_real": False,
                        "reason": key_reason,
                        "provider": key,
                    },
                })
            if key in {
                "provider",
                "_provider",
                "visibility_provider",
                "attribution_provider",
            }:
                reason = _mock_provider_reason(item)
                if reason:
                    found.append({
                        "upstream_status": {
                            "is_real": False,
                            "reason": reason,
                            "provider": item,
                        },
                    })
            if isinstance(item, (dict, list)):
                found.extend(_detect_mock_provider_markers(item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                found.extend(_detect_mock_provider_markers(item))
    return found


def _detect_mock_audit_output(brand_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detect explicit mock fallback output on legacy and v3 report shapes."""
    detected: List[Dict[str, Any]] = []
    try:
        from routes.merchant_audit_routes import _detect_mock_per_product
        detected.extend(_detect_mock_per_product(brand_report or {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: legacy mock detector failed: %s",
            str(exc)[:200],
        )
    detected.extend(_detect_mock_provider_markers(brand_report or {}))
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in detected:
        status = item.get("upstream_status") if isinstance(item, dict) else {}
        reason = (status or {}).get("reason") or str(item)[:200]
        if reason in seen:
            continue
        seen.add(reason)
        out.append(item)
    return out


async def _seed_url_audit_index(
    *, merchant_id: str, synthetic_items: List[Dict[str, Any]],
) -> None:
    """Best-effort: auto-seed the commerce index from a URL audit. UNCONDITIONAL
    on the url_audit path (W5 P2 — seeding is the main line, no longer flag-gated).
    For each pasted product, upsert an OBSERVED, unclaimed catalog_products seed
    keyed on the BRAND's surface (canonical_url — the brand site even when the
    pasted page is a retailer channel). The seed's (platform='url_audit',
    source_product_id) is what the per-SKU report re-derives deterministically so
    the merchant can attach proof/claims to it. NEVER raises — an intake failure
    must not break a live audit (the upsert is itself best-effort; this guard is
    belt-and-suspenders)."""
    try:
        from services.audit_index_intake import upsert_audited_sku_to_index
    except Exception as exc:  # noqa: BLE001 — import must never break the audit
        logger.warning(
            "audit_run_worker: url-audit index seed setup failed: %s", str(exc)[:200],
        )
        return
    for item in synthetic_items or []:
        try:
            # Seed the brand's own product surface: for a retail-channel paste,
            # canonical_url is the brand site (pdp_url is the retailer page). The
            # report re-derives source_product_id from this same URL, so they agree.
            seed_url = (
                str((item or {}).get("canonical_url") or "").strip()
                or str((item or {}).get("pdp_url") or "").strip()
                or None
            )
            if not seed_url:
                continue
            await upsert_audited_sku_to_index(merchant_id, {**item, "pdp_url": seed_url})
        except Exception as exc:  # noqa: BLE001 — best-effort, never break the audit
            logger.warning(
                "audit_run_worker: url-audit index seed failed for %s: %s",
                (item or {}).get("sku_key"), str(exc)[:200],
            )


async def _submit_url_audit_seed_canonical_urls(
    *, merchant_id: str, synthetic_products: List[Dict[str, Any]], run_id: str,
) -> None:
    """W5 P4.2: auto-submit each url_audit SEED's Pivota canonical URL to Google's
    Indexing API (Pivota credential) on the synthetic completion path.

    url_audit runs never reach _run_verifiers (they complete synthetically), and
    submit_audit_canonical_urls only collects url_source='pivota_canonical_pdp'
    URLs, which url_audit per-SKU reports never carry — so without this hook a
    seed's canonical URL is minted (P3) but never submitted. This closes that gap
    and gives gsc_url_submission_loop its first rows to re-drive.

    INERT until GSC_PIVOTA_SUBMIT_ENABLED flips (P5): the early gate here AND
    submit_pivota_canonical_urls both self-gate on settings.gsc_pivota_submit_enabled
    (returns [] today). Best-effort — never breaks a completed run. Idempotent:
    URLs are deduped (per run by the read below + across runs by the
    gsc_url_submissions ON CONFLICT (merchant_id, url) upsert)."""
    try:
        from config.settings import settings

        # Replicate the callee's gate up front so the DB reads below don't run
        # while the feature is inert (belt-and-suspenders — the submit itself
        # also short-circuits on this flag).
        if not settings.gsc_pivota_submit_enabled:
            return
        from services.audit_index_intake import (
            PLATFORM_URL_AUDIT,
            stable_source_id,
        )

        # Re-derive each seed's source_product_id from the SAME brand-surface URL
        # the seed was keyed on (canonical_url, else pdp_url — see
        # _seed_url_audit_index), then read the STORED pivota_canonical_url from
        # catalog_products. Reading the stored URL (vs re-minting a sig) means we
        # only submit seeds that ACTUALLY exist (intake on) — never a fabricated
        # URL for an unseeded SKU.
        source_ids: List[str] = []
        seen_sids: set = set()
        for item in synthetic_products or []:
            seed_url = (
                str((item or {}).get("canonical_url") or "").strip()
                or str((item or {}).get("pdp_url") or "").strip()
                or None
            )
            sid = stable_source_id(seed_url) if seed_url else None
            if sid and sid not in seen_sids:
                seen_sids.add(sid)
                source_ids.append(sid)
        if not source_ids:
            return

        from db.database import database
        from db.catalog import catalog_products

        rows = await database.fetch_all(
            catalog_products.select().where(
                catalog_products.c.merchant_id == merchant_id,
                catalog_products.c.platform == PLATFORM_URL_AUDIT,
                catalog_products.c.source_product_id.in_(source_ids),
            )
        )
        urls: List[str] = []
        for r in rows or []:
            url = (r["pivota_canonical_url"] or "").strip()
            if url:
                urls.append(url)
        if not urls:
            return

        from services.gsc_integration import submit_pivota_canonical_urls

        await submit_pivota_canonical_urls(
            merchant_id=merchant_id, urls=urls, audit_run_id=run_id,
        )
        logger.info(
            "audit_run_worker: url-audit seed submit fired for run_id=%s "
            "merchant=%s urls=%d", run_id, merchant_id, len(urls),
        )
    except Exception as exc:  # noqa: BLE001 — never break a completed run
        logger.warning(
            "audit_run_worker: url-audit seed submit failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )


async def _resolve_synthetic_url_products(
    *, launch_options: Dict[str, Any], merchant_id: str,
) -> tuple:
    """Discovery stage for URL-audit (wedge) runs: build `products` from the
    pasted-URL products persisted in launch.synthetic_products — NO catalog
    lookup — and register each as a synthetic SKU context so the per_sku
    fan-out + report-assembly loop resolve it via load_sku_context().

    Re-runnable on stale-lease resume: launch.synthetic_products is persisted,
    so this re-registers the same contexts deterministically. Returns the same
    5-tuple as _resolve_merchant_and_products. integration_state is None — the
    materialize stage is skipped for synthetic runs anyway.
    """
    from services.agent_center_bd_report_service import (
        register_synthetic_sku_contexts,
    )
    from services.product_identity_i18n import resolve_synthetic_items_inplace

    items = launch_options.get("synthetic_products") or []
    # US-primary market: a Korean-titled PDP yields Korean buyer probes and an
    # empty attribute graph. Resolve the English identity FIRST (flag-gated,
    # fail-safe) and mutate item['title'] in place, so the registered context
    # (probe identity) and the products list (attribute graph) both pick it up.
    # Idempotent on resume: an already-English title short-circuits at the gate.
    await resolve_synthetic_items_inplace(items, merchant_id)
    register_synthetic_sku_contexts(items, merchant_id)
    products: List[Dict[str, Any]] = []
    for item in items:
        sku_key = str((item or {}).get("sku_key") or "").strip()
        if not sku_key:
            continue
        pdp_url = str(item.get("pdp_url") or "").strip() or None
        products.append({
            "product_key": item.get("product_key"),
            "sku_key": sku_key,
            "title": item.get("title"),
            "vendor": item.get("vendor"),
            "product_type": item.get("product_type"),
            "pdp_url": pdp_url,
            "canonical_url": pdp_url,
            "url_source": "merchant_url",
        })
    merchant_name = str(
        launch_options.get("merchant_name") or merchant_id
    )
    merchant_domain = launch_options.get("merchant_domain")
    return merchant_name, merchant_domain, products, [], None


async def _resolve_merchant_and_products(
    *, merchant_id: str, product_keys: List[str],
) -> tuple:
    """Discovery stage: load merchant onboarding + the catalog rows
    for product_keys + derive the audit-time pdp_url for each (with
    Pivota canonical fallback + lazy mint).

    Returns (merchant_name, merchant_domain, products, pivota_url_used,
    integration_state). Mirrors the same URL-resolution chain the
    legacy `/ai-commerce-readiness` route uses today (P2.4 will unify
    them under a single helper).
    """
    from db.database import database
    from db.catalog import catalog_products
    from db.merchant_onboarding import get_merchant_onboarding
    from services.catalog_sync_service import (
        make_pivota_canonical_fields,
    )
    from routes.merchant_audit_routes import _derive_canonical_url

    merchant = await get_merchant_onboarding(merchant_id) or {}
    merchant_name = (
        merchant.get("business_name")
        or merchant.get("legal_name")
        or merchant.get("store_url")
        or merchant_id
    )
    merchant_domain = (merchant.get("store_url") or "").strip() or None

    if not product_keys:
        return merchant_name, merchant_domain, [], [], None

    rows = await database.fetch_all(
        catalog_products.select().where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.product_key.in_(product_keys),
        )
    )

    products: List[Dict[str, Any]] = []
    pivota_url_used: List[str] = []
    for r in rows or []:
        pdp_url = (r["canonical_url"] or "").strip()
        url_source = "merchant_canonical_url"
        if not pdp_url:
            derived = _derive_canonical_url(
                merchant_domain=merchant_domain,
                product_payload=r["product_payload"],
            ) or ""
            if derived:
                pdp_url = derived
                url_source = "derived_from_handle"
        try:
            pivota_minted_at = r["pivota_signature_minted_at"]
        except (KeyError, IndexError):
            pivota_minted_at = None
        if not pdp_url:
            pivota_url = (r["pivota_canonical_url"] or "").strip()
            if not pivota_url:
                fields = make_pivota_canonical_fields(
                    merchant_id, r["platform"], r["source_product_id"],
                )
                pivota_url = fields["pivota_canonical_url"]
                pivota_minted_at = fields["pivota_signature_minted_at"]
                await database.execute(
                    catalog_products.update()
                    .where(
                        catalog_products.c.merchant_id == merchant_id,
                        catalog_products.c.platform == r["platform"],
                        catalog_products.c.source_product_id
                            == r["source_product_id"],
                    )
                    .values(
                        pivota_signature_id=fields["pivota_signature_id"],
                        pivota_canonical_url=pivota_url,
                        pivota_signature_minted_at=pivota_minted_at,
                    )
                )
            pdp_url = pivota_url
            url_source = "pivota_canonical_pdp"
            pivota_url_used.append(r["product_key"])
        products.append({
            "product_key": r["product_key"],
            # Do NOT alias sku_key = product_key here. catalog_skus.sku_key is
            # minted as `<product_key>::v::<variant_id>` (see
            # services/catalog_variant_promoter.py), never the bare product_key.
            # Pre-setting sku_key short-circuits _sku_keys_for_per_sku_mode()
            # (it returns early when any sku_key is present, skipping the
            # catalog_skus lookup), so load_sku_context() then queries
            # `WHERE sku_key = <product_key>`, finds no row, and every per-SKU
            # dimension comes back null with missing_inputs=["catalog_skus"].
            # Leaving sku_key unset lets per-SKU expansion resolve the real
            # variant sku_keys from catalog_skus.
            "title": r["title"],
            "vendor": r["brand"],
            "product_type": r["product_type"],
            "pdp_url": pdp_url,
            "pivota_signature_minted_at": pivota_minted_at,
            "url_source": url_source,
        })

    integration_state: Optional[Dict[str, Any]] = None
    try:
        from services.merchant_integration_state import (
            get_integration_state,
        )
        integration_state = await get_integration_state(merchant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: integration_state lookup failed for "
            "merchant_id=%s: %s", merchant_id, exc,
        )

    return (
        merchant_name, merchant_domain, products, pivota_url_used,
        integration_state,
    )


async def _materialize_tasks_and_executors(
    *,
    merchant_id: str,
    run_id: str,
    brand_report: Dict[str, Any],
    integration_state: Optional[Dict[str, Any]],
    agent_names: Optional[Any] = None,
    dispatch_only: bool = False,
) -> Dict[str, Any]:
    """Materializing stage: task queue + executor dispatch. Unlike
    the legacy route which fires-and-forgets executor agents, the
    worker AWAITS them — the audit isn't 'completed' until materializing
    work has resolved (so GET /api/audits/{id} reflects the truth)."""
    summary: Dict[str, Any] = {
        "tasks_materialized": 0, "executors_dispatched": 0,
    }
    # W5: dispatch_only (URL-audit) skips task-queue materialization + outreach
    # reverification — the url-audit's advisory plan already lives in each
    # per_sku report's next_best_action, and those paths are connected-store
    # oriented. Only the report-only executor dispatch below runs.
    if not dispatch_only:
        try:
            from services.task_queue_service import (
                materialize_tasks_from_audit,
                reverify_outreach_records,
            )
            tasks_summary = await materialize_tasks_from_audit(
                merchant_id=merchant_id,
                audit_run_id=run_id,
                audit_report=brand_report,
                integration_state=integration_state,
            )
            if isinstance(tasks_summary, dict):
                summary["tasks_materialized"] = (
                    tasks_summary.get("created_count")
                    or tasks_summary.get("count")
                    or 0
                )
            # Outreach Step 2 — close the loop: flip any pitched host that now
            # cites us to 'cited' (the proof). Best-effort; never sinks the audit.
            outreach_summary = await reverify_outreach_records(
                merchant_id=merchant_id, run_id=run_id, audit_report=brand_report,
            )
            if isinstance(outreach_summary, dict) and outreach_summary.get("flipped"):
                summary["outreach_cited"] = outreach_summary["flipped"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit_run_worker: task materialization failed "
                "for run_id=%s: %s", run_id, exc,
            )

    try:
        from services.executor_agents.base import ExecutorContext
        from services.executor_agents.dispatcher import dispatch_agents
        ctx = ExecutorContext(
            merchant_id=merchant_id,
            parent_audit_run_id=run_id,
            audit_report=brand_report,
        )
        result = await dispatch_agents(ctx, agent_names=agent_names)
        if isinstance(result, dict):
            summary["executors_dispatched"] = (
                result.get("dispatched_count")
                or result.get("count")
                or len(result.get("runs") or [])
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: executor dispatch failed "
            "for run_id=%s: %s", run_id, exc,
        )

    return summary


async def _run_verifiers(
    *,
    merchant_id: str,
    run_id: str,
    brand_report: Dict[str, Any],
    merchant_name: str,
) -> Dict[str, Any]:
    """Verifying stage: co-occurrence + GSC URL auto-submit. Each
    verifier is wrapped — failures shouldn't poison sibling verifiers.
    Phase 5 will add the remaining 5 verifiers (PDP renders, sitemap
    inclusion, Pivota internal retrieval, frontend agent cite, public
    LLM citation movement)."""
    summary: Dict[str, Any] = {
        "co_occurrence_verified": False,
        "gsc_submitted": False,
    }
    try:
        from services.co_occurrence_finder import (
            verify_brand_report_co_occurrence,
        )
        await verify_brand_report_co_occurrence(
            brand_report, merchant_brand=str(merchant_name),
        )
        summary["co_occurrence_verified"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: co-occurrence verification failed "
            "for run_id=%s: %s", run_id, exc,
        )

    try:
        from services.gsc_integration import (
            get_gsc_submission_state,
            submit_audit_canonical_urls,
        )
        await submit_audit_canonical_urls(
            merchant_id=merchant_id,
            brand_report=brand_report,
            audit_run_id=run_id,
        )
        submission_state = await get_gsc_submission_state(merchant_id)
        # Mirror submission state into per_product tracking so the
        # cached report reflects post-submit truth.
        for report in (brand_report.get("per_product") or []):
            if not isinstance(report, dict):
                continue
            mv = report.get("merchant_view") or {}
            tracking = mv.get("tracking") or {}
            tracking["gsc_submission_status"] = submission_state
            mv["tracking"] = tracking
        summary["gsc_submitted"] = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: GSC auto-submit failed for run_id=%s: %s",
            run_id, exc,
        )

    return summary


async def _record_final_report_fields(
    *,
    run_id: str,
    brand_report: Dict[str, Any],
    pivota_url_used: List[str],
) -> None:
    """Write the legacy aggregate columns (visibility_score_avg etc.)
    + report_jsonb so dual-key consumers see a populated row even
    before they migrate to the new fetch shape. Best-effort."""
    try:
        # Layer 1 output-quality gate before the report is persisted. The
        # canonical per-SKU report does not currently carry the wedge's
        # aggregate.buyer_path_verdict / sku_intelligence surfaces, so this is a
        # safe no-op today that activates (degrade + alert) the moment a
        # controller/ownership surface is added to this path. Never raises.
        try:
            from services.audit_invariants import enforce_audit_invariants
            enforce_audit_invariants({"brand_report": brand_report}, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — gate must not block persistence
            logger.warning(
                "audit_run_worker: invariant gate skipped for run_id=%s: %s",
                run_id, exc,
            )
        from db.merchant_audit_runs import record_audit_run_completed
        aggregate = brand_report.get("aggregate") or {}
        per_product = brand_report.get("per_product") or []
        verdict_labels = [
            ((p.get("verdict") or {}).get("label") or "")
            for p in per_product
        ]
        # per_sku runs carry no legacy `aggregate`; their run-level scores live on
        # brand_rollup.run_scores (set in run_brand_report). Fall back to those so
        # the score columns aren't NULL and the run-over-run trend works. Legacy
        # runs keep using `aggregate` unchanged.
        run_scores = (brand_report.get("brand_rollup") or {}).get("run_scores") or {}
        await record_audit_run_completed(
            run_id=run_id,
            # transition_stage flips status='succeeded' itself; passing
            # it here too is harmless and mirrors the legacy contract.
            status="succeeded",
            verdict_labels=[v for v in verdict_labels if v],
            visibility_score_avg=aggregate.get(
                "avg_visibility", run_scores.get("avg_visibility"),
            ),
            attribution_score_avg=aggregate.get(
                "avg_attribution", run_scores.get("avg_attribution"),
            ),
            category_visibility_score_avg=aggregate.get(
                "avg_category_visibility", run_scores.get("avg_category_visibility"),
            ),
            audited_via_pivota_canonical=pivota_url_used,
            report_jsonb=brand_report,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: legacy aggregate write failed "
            "for run_id=%s: %s", run_id, exc,
        )


def _placeholder_cost_summary(
    brand_report: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Fallback shape when llm_probe_runs has no rows for this audit
    (e.g., the orchestrator's record_probe_run wiring hasn't shipped
    yet, or every probe failed before recording). Coarse counters
    only — no per-call cost truth."""
    if not brand_report:
        return None
    aggregate = brand_report.get("aggregate") or {}
    providers = list(brand_report.get("providers") or [])
    if not providers:
        seen = set()
        for item in brand_report.get("per_product") or []:
            if not isinstance(item, dict):
                continue
            for provider in item.get("providers") or []:
                if provider and provider not in seen:
                    seen.add(provider)
                    providers.append(provider)
    return {
        "providers": providers,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "estimated_cost_usd": None,
        "products_probed": aggregate.get("products_succeeded"),
        "products_failed": aggregate.get("products_failed"),
        "provider_models": brand_report.get("provider_models") or {},
        "model_is_override": bool(brand_report.get("model_is_override")),
        "_telemetry_source": "placeholder_no_probe_runs_recorded",
    }


def _compute_cost_summary(
    brand_report: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Sync wrapper kept for backwards compat with tests that
    reference the helper directly. Returns the placeholder shape
    when called synchronously; the worker's actual completion path
    calls _aggregate_cost_summary_for_run for the real rollup.
    """
    return _placeholder_cost_summary(brand_report)


async def _aggregate_cost_summary_for_run(
    *,
    run_id: str,
    brand_report: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Async helper that actually queries llm_probe_runs for the
    real per-provider cost rollup. Falls back to the placeholder
    shape when no rows exist (telemetry not yet wired into every
    probe call site, or the audit failed before any probe ran).
    """
    try:
        from db.llm_probe_runs import aggregate_cost_for_run
        rollup = await aggregate_cost_for_run(audit_run_id=run_id)
        if rollup is not None:
            rollup["provider_models"] = (brand_report or {}).get("provider_models") or {}
            rollup["model_is_override"] = bool(
                (brand_report or {}).get("model_is_override")
            )
            return rollup
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: cost rollup failed for run_id=%s: %s",
            run_id, exc,
        )
    return _placeholder_cost_summary(brand_report)


async def _lease_heartbeat(
    *,
    run_id: str,
    interval_seconds: int,
    lease_seconds: int,
) -> None:
    """P5.8.6c: keep the audit_run's lease fresh while a long
    stage (probing) runs. Cancels cleanly via task.cancel() when
    the stage completes.

    Why an asyncio task and not periodic in-band calls:
      - run_brand_report is one big async call; we can't
        interleave extend_lease without restructuring the report
        builder.
      - Cancelling the heartbeat via task.cancel() in a `finally`
        is the standard pattern.

    Stops if extend_lease returns False (lease already stolen).
    No point hammering UPDATE if we've lost the row — the worker's
    final transition_stage will also fail, signaling correctly.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        try:
            from db import merchant_audit_runs as mar
            ok = await mar.extend_lease(
                run_id=run_id, worker_id=WORKER_ID,
                lease_seconds=lease_seconds,
            )
            if not ok:
                logger.warning(
                    "_lease_heartbeat: lost lease for run_id=%s; "
                    "stopping heartbeat", run_id,
                )
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_lease_heartbeat: extend raised for run_id=%s: %s; "
                "continuing", run_id, str(exc)[:200],
            )
