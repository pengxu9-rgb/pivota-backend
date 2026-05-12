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

    try:
        # ----- queued → discovering -----
        if current_stage == mar.STAGE_QUEUED:
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
            await mar.extend_lease(run_id=run_id, worker_id=WORKER_ID)
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
            await mar.extend_lease(
                run_id=run_id, worker_id=WORKER_ID,
                lease_seconds=LONG_STAGE_LEASE_SECONDS,
            )
            from services.agent_center_bd_report_service import (
                run_brand_report,
            )
            brand_report = await run_brand_report(
                merchant_name=str(merchant_name),
                merchant_domain=merchant_domain,
                products=products,
                provider="gemini",
                max_runs=3,
                integration_state=integration_state,
            )
            aggregate = brand_report.get("aggregate") or {}
            await mar.record_partial_result(
                run_id=run_id, worker_id=WORKER_ID,
                partial_result_jsonb={
                    "probing": {
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
            await mar.extend_lease(
                run_id=run_id, worker_id=WORKER_ID,
                lease_seconds=LONG_STAGE_LEASE_SECONDS,
            )
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

        # ----- verifying: co-occurrence + GSC URL submission -----
        if current_stage == mar.STAGE_VERIFYING and brand_report is not None:
            verify_summary = await _run_verifiers(
                merchant_id=merchant_id,
                run_id=run_id,
                brand_report=brand_report,
                merchant_name=merchant_name,
            )
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
        # Best-effort transition to FAILED. If the lease was lost in
        # the meantime this also returns False — that's fine, the
        # next worker that picks it up will see the stale lease and
        # restart the active stage.
        try:
            await mar.transition_stage(
                run_id=run_id,
                from_stage=current_stage,
                to_stage=mar.STAGE_FAILED,
                worker_id=WORKER_ID,
                error_jsonb={
                    "stage": current_stage,
                    "message": str(exc)[:1000],
                    "traceback_truncated": traceback.format_exc()[:2000],
                },
            )
        except Exception as inner:  # noqa: BLE001
            logger.warning(
                "audit_run_worker: secondary failure persisting "
                "failed-state for run_id=%s: %s", run_id, inner,
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


# =====================================================================
# Internal helpers — pipeline stage implementations
# =====================================================================


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
    from db.catalog_products import catalog_products
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
) -> Dict[str, Any]:
    """Materializing stage: task queue + executor dispatch. Unlike
    the legacy route which fires-and-forgets executor agents, the
    worker AWAITS them — the audit isn't 'completed' until materializing
    work has resolved (so GET /api/audits/{id} reflects the truth)."""
    summary: Dict[str, Any] = {
        "tasks_materialized": 0, "executors_dispatched": 0,
    }
    try:
        from services.task_queue_service import (
            materialize_tasks_from_audit,
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
        result = await dispatch_agents(ctx)
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
        from db.merchant_audit_runs import record_audit_run_completed
        aggregate = brand_report.get("aggregate") or {}
        per_product = brand_report.get("per_product") or []
        verdict_labels = [
            ((p.get("verdict") or {}).get("label") or "")
            for p in per_product
        ]
        await record_audit_run_completed(
            run_id=run_id,
            # transition_stage flips status='succeeded' itself; passing
            # it here too is harmless and mirrors the legacy contract.
            status="succeeded",
            verdict_labels=[v for v in verdict_labels if v],
            visibility_score_avg=aggregate.get("avg_visibility"),
            attribution_score_avg=aggregate.get("avg_attribution"),
            category_visibility_score_avg=aggregate.get(
                "avg_category_visibility",
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
    return {
        "providers": [],
        "total_input_tokens": None,
        "total_output_tokens": None,
        "estimated_cost_usd": None,
        "products_probed": aggregate.get("products_succeeded"),
        "products_failed": aggregate.get("products_failed"),
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
            return rollup
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_run_worker: cost rollup failed for run_id=%s: %s",
            run_id, exc,
        )
    return _placeholder_cost_summary(brand_report)
