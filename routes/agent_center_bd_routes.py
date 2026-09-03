"""
Agent Center — BD external-merchant report HTTP route.

Wraps `services/agent_center_bd_report_service.py` so the employee-portal
UI can run a BD AI-visibility report against an external merchant's
product without spinning up a terminal session. The CLI
(`scripts/agent_center_bd_external_merchant.py`) consumes the same
service for terminal users.

Route:

  POST /api/agent-center/bd/external-merchant-report
       body: {
         merchant_name:    string,
         merchant_pdp_url: string,
         product_title:    string,
         product_vendor?:  string,
         product_type?:    string,
         provider?:        "mock" | "gemini",   # default "gemini"
         max_runs?:        int (1..8),          # default 3 (post-#280 guard)
       }
       returns: structured report (see build_structured_report)

Auth: `Depends(get_current_employee)`. BD reports include scoring
explanations meant for sales pitch decks — do not expose without auth.

Cost guard: this route synchronously calls Gemini twice. Conservative
default `max_runs=3` means ~6 grounded calls (~150k tokens) per
request. Hard cap at `max_runs=8` to make rate-limit accidents bounded.
For sustained BD volume, route to a worker pool (see
feedback_llm_call_multipliers.md / incident #280 — the same class of
risk that took backend down already).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, validator

from db.merchant_audit_runs import recent_runs_for_merchant
from services.agent_center_bd_report_service import (
    build_structured_report,
    run_bd_probes,
    run_brand_report,
)
from utils.auth import get_current_employee

router = APIRouter(prefix="/api/agent-center/bd", tags=["agent-center", "bd"])
logger = logging.getLogger(__name__)


_HARD_MAX_RUNS = 8
_TASK_SCOPE_RECENT_RUN_LIMIT = 20


async def _latest_completed_audit_run_id_for_tasks(
    merchant_id: str,
) -> Optional[str]:
    """Return newest succeeded audit run id for task scoping."""
    runs = await recent_runs_for_merchant(
        merchant_id=merchant_id,
        limit=_TASK_SCOPE_RECENT_RUN_LIMIT,
    )
    for run in runs or []:
        if run.get("status") == "succeeded" and run.get("run_id"):
            return str(run["run_id"])
    return None


def _prospect_merchant_id(domain_or_url: str) -> str:
    """Synthesize a stable, deterministic merchant_id for a cold-start
    prospect so the audit lifecycle can persist to merchant_audit_runs
    WITHOUT the prospect having actually onboarded as a merchant.

    Format: `prospect_<hex12>` where hex12 is the first 12 chars of
    SHA-1(normalized_domain). Same domain → same id across audits;
    different domains never collide. Used by PR-1a so a BD operator
    can re-audit gruns.co in 30 days and see the trend in
    merchant_view.tracking.history.
    """
    import hashlib
    import re as _re
    s = (domain_or_url or "").strip().lower()
    # Strip scheme + path so https://gruns.co/ and gruns.co produce
    # the same id.
    s = _re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    if not s:
        s = "_unknown_"
    return "prospect_" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _detect_mock_reports(reports: list) -> list:
    """Return per-product reports whose upstream returned mock /
    synthetic fallback data. Mirrors the merchant audit route's
    `_detect_mock_per_product` (PR #366) — applied here to BD
    routes to close the same fabrication-risk gap.

    Three pollution sources in the audit pipeline (same as merchant
    side):
      1. `_local_mock_result` in services.agent_center_llm_client
         fires when this backend's PIVOTA_AGENT_INTERNAL_API_KEY is
         unset (returns provider="local_mock_no_internal_key")
      2. Upstream Pivota-Agent service's mock fires when its own
         Gemini key is unset (provider="mock_fallback_no_gemini_key")
      3. Explicit provider="mock" via pivota_agent_center_mock_gemini
         flag

    Each produces upstream_status with is_real=False. Without this
    guard, BD routes would render full diagnostic prose against
    synthetic data — operator sees fabricated audit they might
    forward to a brand without realizing.

    Conservative default: missing/malformed `upstream_status` is
    treated as REAL (is_real defaults True). Only explicit False
    rejects.
    """
    return [
        r for r in (reports or [])
        if isinstance(r, dict)
        and not (r.get("upstream_status") or {}).get("is_real", True)
    ]


def _raise_mock_rejection(mock_reports: list, route_name: str) -> None:
    """Centralized 503 raise for mock-data rejection. Logs the same
    structured warning the merchant route uses + returns the same
    error shape so portal code can render a uniform mock banner."""
    first_reason = (
        (mock_reports[0].get("upstream_status") or {}).get("reason")
        or "Upstream returned mock data."
    )
    logger.error(
        "BD %s: refusing to ship mock-derived prose; "
        "%d products had upstream_status.is_real=False",
        route_name, len(mock_reports),
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "upstream_mock_fallback",
            "message": (
                f"Audit pipeline upstream returned synthetic fallback "
                f"data ({first_reason!s}); refusing to render BD-facing "
                f"prose. Check that the probe-auth secret "
                f"(PROMOTIONS_ADMIN_KEY — preferred; or AGENT_API_KEY / "
                f"PIVOTA_AGENT_INTERNAL_API_KEY) and the upstream's "
                f"GEMINI_API_KEY are configured. "
                f"Re-run the audit once the upstream is real."
            ),
        },
    )


class BdExternalMerchantReportRequest(BaseModel):
    merchant_name: str = Field(..., min_length=1, max_length=200)
    merchant_pdp_url: str = Field(..., min_length=8, max_length=2000)
    product_title: str = Field(..., min_length=1, max_length=400)
    product_vendor: Optional[str] = Field(None, max_length=200)
    product_type: Optional[str] = Field(None, max_length=120)
    provider: str = Field("gemini")
    max_runs: int = Field(3, ge=1, le=_HARD_MAX_RUNS)

    @validator("provider")
    def _provider_allowed(cls, v: str) -> str:
        if v not in {"gemini", "mock"}:
            raise ValueError("provider must be 'gemini' or 'mock'")
        return v

    @validator("merchant_pdp_url")
    def _url_looks_like_url(cls, v: str) -> str:
        s = v.strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError("merchant_pdp_url must start with http:// or https://")
        return s


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    logger.exception("Unhandled error in agent-center BD route")
    return HTTPException(status_code=500, detail="bd_report_internal_error")


@router.post("/external-merchant-report")
async def external_merchant_report(
    body: BdExternalMerchantReportRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Run a BD AI-visibility report against an external merchant's product.

    Synchronous. Real Gemini calls take 6-12s with grounding; the UI
    should display a clear "running probes…" state. If we ever surface
    BD reports to merchants directly, switch this to a queued
    BackgroundTask + polling pattern (same as demand-test routes)."""
    try:
        probes = await run_bd_probes(
            merchant_name=body.merchant_name,
            merchant_pdp_url=body.merchant_pdp_url,
            product_title=body.product_title,
            product_vendor=body.product_vendor,
            product_type=body.product_type,
            provider=body.provider,
            max_runs=body.max_runs,
        )
    except Exception as exc:
        raise _map_error(exc) from exc

    report = build_structured_report(
        merchant_name=body.merchant_name,
        merchant_pdp_url=body.merchant_pdp_url,
        product_title=body.product_title,
        product_vendor=body.product_vendor,
        product_type=body.product_type,
        visibility_result=probes["visibility"],
        attribution_result=probes["attribution"],
        category_visibility_result=probes.get("category_visibility"),
        provider=body.provider,
    )
    # Mock guard: refuse to ship BD-facing prose against synthetic
    # data. Treats single-product report as a list of one for the
    # shared helper. Mirrors PR #366's merchant-route guard.
    mock_reports = _detect_mock_reports([report])
    if mock_reports:
        _raise_mock_rejection(mock_reports, "external-merchant-report")
    upstream_status = report.get("upstream_status") or {}
    logger.info(
        "BD report generated: merchant=%s product=%s verdict=%s vis=%d attr=%d "
        "requested_provider=%s upstream_visibility=%s is_real=%s",
        body.merchant_name,
        body.product_title,
        report["verdict"]["label"],
        report["verdict"]["visibility_score"],
        report["verdict"]["attribution_score"],
        body.provider,
        upstream_status.get("visibility_provider"),
        upstream_status.get("is_real"),
    )
    return {"status": "ok", "report": report}


# ---------------------------------------------------------------------------
# Brand-level multi-product BD report (Phase 2b)
# ---------------------------------------------------------------------------


# Spec §I — launch cap raised from 5 → 50 SKUs because the
# credit-balance pre-flight (POST /api/audits/preview) is now the
# authoritative cost gate. Larger audits debit proportionally more
# audit_credits at launch via merchant_credit_balance_service.debit().
# The pre-flight surfaces estimated credits + sufficient/gaps so the
# merchant decides whether to launch.
_BRAND_REPORT_HARD_MAX_PRODUCTS = 50


class BdBrandReportProduct(BaseModel):
    title: str = Field(..., min_length=1, max_length=400)
    pdp_url: str = Field(..., min_length=8, max_length=2000)
    vendor: Optional[str] = Field(None, max_length=200)
    product_type: Optional[str] = Field(None, max_length=120)

    @validator("pdp_url")
    def _url_looks_like_url(cls, v: str) -> str:
        s = v.strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError("pdp_url must start with http:// or https://")
        return s


class BdBrandReportRequest(BaseModel):
    merchant_name: str = Field(..., min_length=1, max_length=200)
    merchant_domain: Optional[str] = Field(None, max_length=200)
    products: list[BdBrandReportProduct] = Field(
        ..., min_length=1, max_length=_BRAND_REPORT_HARD_MAX_PRODUCTS,
    )
    provider: str = Field("gemini")
    max_runs: int = Field(3, ge=1, le=_HARD_MAX_RUNS)
    include_category_visibility: bool = Field(True)
    # PR-8 (Option A step 3): opt-in to bd_brand_signals social
    # intelligence — 4-5 additional grounded Gemini calls per audit
    # to surface brand TikTok/Instagram presence + KOL endorsements
    # + competitive social comparison. Off by default until staging
    # load-test confirms the per-tenant semaphore handles the bump
    # (LLM-multiplier-safety rule, PR #278 incident).
    include_social_intelligence: bool = Field(False)

    @validator("provider")
    def _provider_allowed(cls, v: str) -> str:
        if v not in {"gemini", "mock"}:
            raise ValueError("provider must be 'gemini' or 'mock'")
        return v


@router.post("/brand-report")
async def brand_report(
    body: BdBrandReportRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Run BD probes against up to 5 products of one merchant. Returns
    per-product structured reports + brand-level aggregate verdict +
    cross-product competitor host frequency.

    Synchronous. Real Gemini calls take 6-12s per product × N products =
    can hit ~60s for 5 products. UI must show a clear progress state.
    Per-product failures are isolated — one failed product doesn't kill
    the whole brand run; failures are returned in `failed[]`.

    Cost: capped at 5 products × 3 scan modes × `max_runs` runs.
    Default 5 × 3 × 3 = 45 grounded Gemini calls per brand report
    (~1.1M tokens). Pydantic enforces both the product count and
    max_runs caps so cost is bounded by the request schema."""
    try:
        out = await run_brand_report(
            merchant_name=body.merchant_name,
            merchant_domain=body.merchant_domain,
            products=[p.dict() for p in body.products],
            provider=body.provider,
            max_runs=body.max_runs,
            include_category_visibility=body.include_category_visibility,
            include_social_intelligence=body.include_social_intelligence,
        )
    except Exception as exc:
        raise _map_error(exc) from exc

    # Mock guard: refuse to ship multi-product BD-facing prose against
    # synthetic data. Closes the parallel of PR #366's merchant-route
    # gap that was missed for BD routes.
    mock_reports = _detect_mock_reports(out.get("per_product") or [])
    if mock_reports:
        _raise_mock_rejection(mock_reports, "brand-report")

    # Operational signals — log brand-level verdict + how many products
    # succeeded. Helps diagnose when a brand pitch came back weak
    # because of probe failures vs because the merchant is genuinely
    # weak in AI search.
    agg = out.get("aggregate") or {}
    logger.info(
        "BD brand report: merchant=%s products_count=%s succeeded=%s "
        "failed=%s avg_visibility=%s avg_attribution=%s avg_category=%s "
        "verdict=%s",
        body.merchant_name,
        agg.get("products_count"),
        agg.get("products_succeeded"),
        agg.get("products_failed"),
        agg.get("avg_visibility"),
        agg.get("avg_attribution"),
        agg.get("avg_category_visibility"),
        agg.get("brand_verdict_label"),
    )
    return {"status": "ok", "brand_report": out}


# ---------------------------------------------------------------------------
# Cold-start audit — URL-only entry point for BD outreach
# ---------------------------------------------------------------------------


class BdColdStartAuditRequest(BaseModel):
    """URL-only entry point for BD cold outreach. The operator pastes
    the brand's homepage URL; backend orchestrates discovery via:

      1. Pivota-catalog-intelligence service (Puppeteer; primary
         when configured) — returns rich extracted catalog including
         variants, pricing, ad copy
      2. In-process sitemap + link discovery (fallback when
         catalog-intelligence is unreachable / unconfigured /
         returns empty for this site)

    All discovered products are upserted to `prospect_products` —
    every cold audit grows Pivota's catalog of D2C brand data.
    Subset (top max_products) is sent through run_brand_report.

    For BD doing cold calls to brands they've never engaged with,
    the existing /brand-report form requires title + pdp_url +
    vendor + type per product — 5-10 minutes of manual fetch-and-
    paste per target. This endpoint reduces that to one URL.
    """
    url: str = Field(..., min_length=8, max_length=2000)
    max_products: int = Field(3, ge=1, le=_BRAND_REPORT_HARD_MAX_PRODUCTS)
    market: str = Field("US", min_length=2, max_length=10)
    provider: str = Field("gemini")
    max_runs: int = Field(3, ge=1, le=_HARD_MAX_RUNS)
    include_category_visibility: bool = Field(True)
    # Catalog-extract-audit skill's "preflight before seed writes"
    # pattern. When True, the endpoint runs discovery only — returns
    # diagnostics + coverage stats + product list — without invoking
    # the LLM audit pipeline OR persisting to prospect_products.
    # Lets BD operators inspect extraction quality before committing
    # to a full audit run + backfill.
    dry_run: bool = Field(False)
    # PR-2: opt-in to background cohort audit. When True, after the
    # parent audit completes, the orchestrator queues audits of the
    # top-N competitor brands extracted from category_visibility_test
    # and persists them to competitor_audit_runs linked to this
    # parent run. Default OFF until staging load test confirms the
    # multi-LLM-call multiplier is bounded by the per-merchant
    # semaphore. Operator polls /api/agent-center/bd/cohort/{run_id}
    # to read results.
    audit_competitors: bool = Field(False)
    cohort_size: int = Field(3, ge=1, le=5)

    @validator("url")
    def _url_looks_like_url(cls, v: str) -> str:
        s = v.strip()
        if not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return s

    @validator("provider")
    def _provider_allowed(cls, v: str) -> str:
        if v not in {"gemini", "mock"}:
            raise ValueError("provider must be 'gemini' or 'mock'")
        return v


@router.post("/cold-start-audit")
async def cold_start_audit(
    body: BdColdStartAuditRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """URL-only BD audit. Auto-discovers brand + products, runs
    full brand report.

    Returns 422 with diagnostic when discovery fails entirely — BD
    operator falls back to /brand-report manual entry.
    """
    from services.bd_cold_start_service import (
        BrandDiscoveryError,
        discover_products_for_audit,
    )

    try:
        discovered = await discover_products_for_audit(
            body.url,
            max_products=body.max_products,
            market=body.market,
            persist=not body.dry_run,
        )
    except BrandDiscoveryError as exc:
        logger.warning(
            "BD cold-start audit: discovery failed for url=%s: %s",
            body.url, exc,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "discovery_failed",
                "message": str(exc),
                "fallback": (
                    "Use POST /api/agent-center/bd/brand-report with "
                    "manually-entered products."
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "BD cold-start audit: unexpected discovery error for url=%s",
            body.url,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected discovery error: {exc}",
        )

    if not discovered.get("products"):
        # Belt-and-suspenders.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "discovery_failed",
                "message": "Discovery returned zero products; nothing to audit.",
                "fallback": (
                    "Use POST /api/agent-center/bd/brand-report with "
                    "manually-entered products."
                ),
            },
        )

    # Dry-run short-circuit: return discovery + diagnostics without
    # running the LLM audit. catalog-extract-audit skill pattern —
    # operator inspects extraction quality before committing to a
    # full audit run.
    if body.dry_run:
        logger.info(
            "BD cold-start dry-run: url=%s brand=%s discovery=%s "
            "products_discovered=%s coverage=%s diagnostics=%s",
            body.url,
            discovered["merchant_name"],
            discovered["discovery_method"],
            discovered.get("products_discovered_total"),
            discovered.get("coverage"),
            discovered.get("diagnostics"),
        )
        return {
            "status": "ok",
            "dry_run": True,
            "discovery": _build_discovery_block(discovered),
        }

    # Cold-start targets are by definition NOT Pivota merchants —
    # they have no merchant_id, no Shopify OAuth, no PSP connection,
    # no GSC OAuth grant. So the integration_state is "totally
    # unintegrated" — which makes Phase 0's "Complete Pivota
    # integration" CTA the #1 prioritized action (the actual BD
    # pitch lever for cold outreach). Without this synthetic state,
    # the audit's merchant_view.actions falls back to legacy
    # strategic-tier templates only and BD operators don't see the
    # most important CTA.
    cold_start_integration_state: Dict[str, Any] = {
        "store_platform_integrated": False,
        "psp_integrated": False,
        "gsc_integrated": False,
        "fully_integrated": False,
        "missing_pieces": ["store_platform", "psp"],
        "integration_completed_at": None,
        "store_platform_name": None,
        "psp_provider": None,
        "store_connected_at": None,
    }

    # PR-1a (APM): persist cold-start audit to merchant_audit_runs
    # under a synthetic prospect id so a BD operator can re-audit
    # the same target in 30 days and see trend deltas WITHOUT the
    # prospect having onboarded. Best-effort — DB failures degrade
    # to "no history captured" but never fail the audit itself.
    from db.merchant_audit_runs import (
        record_audit_run_started,
        record_audit_run_completed,
        recent_runs_for_merchant,
    )
    synthetic_merchant_id = _prospect_merchant_id(
        discovered.get("merchant_domain") or body.url,
    )
    product_keys = [
        p.get("pdp_url") for p in (discovered.get("products") or [])
        if p.get("pdp_url")
    ]
    run_id = await record_audit_run_started(
        merchant_id=synthetic_merchant_id,
        product_keys=product_keys,
    )
    prior_runs = await recent_runs_for_merchant(
        merchant_id=synthetic_merchant_id, limit=5,
    )
    if run_id:
        prior_runs = [r for r in prior_runs if r.get("run_id") != run_id]

    try:
        out = await run_brand_report(
            merchant_name=discovered["merchant_name"],
            merchant_domain=discovered.get("merchant_domain"),
            products=discovered["products"],
            provider=body.provider,
            max_runs=body.max_runs,
            include_category_visibility=body.include_category_visibility,
            integration_state=cold_start_integration_state,
            prior_runs=prior_runs,
            merchant_id=synthetic_merchant_id,
        )
    except Exception as exc:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc)[:2000],
        )
        raise _map_error(exc) from exc

    # Mock guard: same protection as /brand-report — cold-start can
    # also receive mock-derived per-product reports if upstream
    # fell back. Reject before the operator sees fabricated prose.
    mock_reports = _detect_mock_reports(out.get("per_product") or [])
    if mock_reports:
        await record_audit_run_completed(
            run_id=run_id, status="failed",
            error_message="upstream_mock_fallback",
        )
        _raise_mock_rejection(mock_reports, "cold-start-audit")

    agg = out.get("aggregate") or {}
    per_product = out.get("per_product") or []
    verdict_labels = [
        ((p.get("verdict") or {}).get("label") or "")
        for p in per_product
    ]
    await record_audit_run_completed(
        run_id=run_id,
        status="succeeded",
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=agg.get("avg_visibility"),
        attribution_score_avg=agg.get("avg_attribution"),
        category_visibility_score_avg=agg.get("avg_category_visibility"),
        report_jsonb=out,
    )
    logger.info(
        "BD cold-start audit: url=%s brand=%s discovery=%s "
        "products_audited=%s products_discovered=%s "
        "products_persisted=%s fallback_used=%s verdict=%s "
        "prospect_id=%s run_id=%s",
        body.url,
        discovered["merchant_name"],
        discovered["discovery_method"],
        agg.get("products_count"),
        discovered.get("products_discovered_total"),
        discovered.get("products_persisted"),
        discovered.get("fallback_used"),
        agg.get("brand_verdict_label"),
        synthetic_merchant_id,
        run_id or "(persistence-skipped)",
    )

    # PR-2: optionally fan out to competitor cohort audits in the
    # background. Operator opts in via audit_competitors=true. Doesn't
    # block the response — returns immediately with status="cohort_queued"
    # and the operator polls /cohort/{run_id} for results.
    #
    # PR-2c: extract the parent's modal product_type and force it on
    # cohort audits so they run the SAME category queries as the
    # parent — apples-to-apples cross-brand mention matrix.
    cohort_status: Optional[Dict[str, Any]] = None
    if body.audit_competitors and run_id:
        competitor_brands = _aggregate_competitor_brands(per_product, top_n=body.cohort_size)
        parent_category = _extract_parent_modal_product_type(per_product)
        if competitor_brands:
            import asyncio as _asyncio
            from services.competitor_audit_orchestrator import enqueue_competitor_audits
            # Fire-and-forget background task. asyncio.create_task is
            # used (not BackgroundTasks) so the cohort audits keep
            # running even if the client disconnects after getting
            # the parent response.
            _asyncio.create_task(
                enqueue_competitor_audits(
                    parent_audit_run_id=run_id,
                    competitor_brands=competitor_brands,
                    market=body.market,
                    max_runs=body.max_runs,
                    cohort_size=body.cohort_size,
                    category_override=parent_category,
                ),
                name=f"cohort-{run_id}",
            )
            cohort_status = {
                "queued": True,
                "competitor_brands": competitor_brands,
                "category_override": parent_category,
                "parent_audit_run_id": run_id,
                "poll_url": f"/api/agent-center/bd/cohort/{run_id}",
            }
        else:
            cohort_status = {
                "queued": False,
                "reason": "no competitor brands extracted from audit",
            }

    # PR-6 cold-start parity: materialize action_items as tracked
    # merchant_tasks under the synthetic prospect id so the BD operator
    # gets a queue to work from instead of advisory text. Best-effort.
    tasks_summary = None
    if run_id:
        try:
            from services.task_queue_service import materialize_tasks_from_audit
            tasks_summary = await materialize_tasks_from_audit(
                merchant_id=synthetic_merchant_id,
                audit_run_id=run_id,
                audit_report=out,
                integration_state=cold_start_integration_state,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "BD cold-start task materialization failed for "
                "prospect=%s run_id=%s: %s",
                synthetic_merchant_id, run_id, exc,
            )

    # PR-4 cold-start parity: dispatch executor agents (GSC URL
    # submission, sitemap freshness, content brief generator). Each
    # agent's should_run() decides whether to fire — most won't on
    # cold-start because the prospect isn't onboarded, but content
    # brief generator typically runs on category_visibility failures.
    executor_summary = None
    if run_id:
        try:
            import asyncio as _asyncio_exec
            from services.executor_agents.base import ExecutorContext
            from services.executor_agents.dispatcher import dispatch_agents
            ctx = ExecutorContext(
                merchant_id=synthetic_merchant_id,
                parent_audit_run_id=run_id,
                audit_report=out,
            )
            _asyncio_exec.create_task(
                dispatch_agents(ctx),
                name=f"executor-dispatch-coldstart-{run_id}",
            )
            executor_summary = {
                "queued": True,
                "poll_via_executor_runs_table": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "BD cold-start executor dispatch failed for "
                "prospect=%s run_id=%s: %s",
                synthetic_merchant_id, run_id, exc,
            )

    return {
        "status": "ok",
        "discovery": _build_discovery_block(discovered),
        "brand_report": out,
        "cohort": cohort_status,
        # PR-6/UI-3: synthetic merchant_id BD frontend uses to query
        # /tasks and /executor-runs for this prospect. Stable across
        # re-audits of the same domain (sha1 hash of normalized
        # domain) so re-running on gruns.co always produces the same
        # prospect_id.
        "prospect_id": synthetic_merchant_id,
        "audit_run_id": run_id,
        "tasks": tasks_summary,
        "executors": executor_summary,
    }


def _extract_parent_modal_product_type(
    per_product: List[Dict[str, Any]],
) -> Optional[str]:
    """Find the most-frequent product_type across the parent audit's
    products. Used as the category_override for cohort competitor
    audits (PR-2c) so they run identical category queries.

    Returns None when no product has a product_type, or when the
    per_product list is empty/malformed. None means "let each cohort
    competitor use their own auto-inferred category" (legacy
    behavior).
    """
    if not per_product:
        return None
    counter: Dict[str, int] = {}
    for product_report in per_product:
        if not isinstance(product_report, dict):
            continue
        product = product_report.get("product") or {}
        pt = product.get("product_type")
        if isinstance(pt, str) and pt.strip():
            key = pt.strip()
            counter[key] = counter.get(key, 0) + 1
    if not counter:
        return None
    # Most frequent — ties broken by first-seen (sorted alphabetically
    # for determinism).
    return max(sorted(counter.items()), key=lambda kv: kv[1])[0]


def _aggregate_competitor_brands(
    per_product: List[Dict[str, Any]],
    *,
    top_n: int = 3,
) -> List[str]:
    """Union competitor brands across per-product reports, ordered by
    citation count. Returns the top-N brand names (strings) for the
    cohort orchestrator. Defensive: tolerates missing fields, garbage
    shapes, duplicate names with different casing.
    """
    counter: Dict[str, int] = {}
    for product_report in per_product:
        if not isinstance(product_report, dict):
            continue
        cv = product_report.get("category_visibility") or {}
        brands = cv.get("competitor_brands") or []
        if not isinstance(brands, list):
            continue
        for entry in brands:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = name.strip()
            counter[key] = counter.get(key, 0) + int(entry.get("times_cited") or 1)
    ordered = sorted(counter.items(), key=lambda kv: -kv[1])
    return [name for name, _ in ordered[:top_n]]


@router.get("/cohort/{parent_audit_run_id}")
async def get_cohort_audit_results(
    parent_audit_run_id: str,
    include_comparison: bool = False,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Return all competitor audit runs for a parent audit. Used by
    the BD portal cohort dashboard. Polls until the orchestrator
    finishes (status field on each entry shifts running → succeeded
    or failed).

    PR-2b: pass `include_comparison=true` to also receive the
    per-query breakdown + cross-brand mention matrix (joins this
    cohort's reports with the parent audit's report). Heavier
    payload — operators only enable when the cohort is fully
    completed (still_running == 0).
    """
    from db.competitor_audit_runs import cohort_for_parent_run
    # Always fetch with include_reports=True when comparison requested
    # (the cohort runs each carry their own report_jsonb that the
    # comparison helper needs to read).
    runs = await cohort_for_parent_run(
        parent_audit_run_id=parent_audit_run_id,
        include_reports=include_comparison,
    )
    completed = [r for r in runs if r.get("status") in ("succeeded", "failed")]
    response: Dict[str, Any] = {
        "parent_audit_run_id": parent_audit_run_id,
        "cohort_size": len(runs),
        "completed_count": len(completed),
        "still_running": len(runs) - len(completed),
        # Strip report_jsonb from the wire response — too large for
        # the polling case. The comparison block carries the parts
        # operators actually need.
        "runs": [
            {k: v for k, v in r.items() if k != "report_jsonb"}
            for r in runs
        ],
    }

    if include_comparison:
        # Fetch parent audit row to get its report_jsonb for the
        # comparison input. Best-effort: if missing, comparison is
        # cohort-only (no parent baseline, surfaces in summary).
        from db.merchant_audit_runs import merchant_audit_runs
        from db.database import database
        from services.cohort_comparison import build_cohort_comparison

        parent_report = None
        parent_label = "(parent unknown)"
        try:
            row = await database.fetch_one(
                merchant_audit_runs.select().where(
                    merchant_audit_runs.c.run_id == parent_audit_run_id,
                )
            )
            if row:
                parent_report = dict(row).get("report_jsonb")
                # Use merchant_id as label for the parent — for cold-
                # start prospects this is the prospect_<hash> id; the
                # frontend can pretty-print to brand name. For real
                # merchant audits, it's the merchant_id.
                parent_label = dict(row).get("merchant_id") or parent_label
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cohort comparison: parent fetch failed for %s: %s",
                parent_audit_run_id, exc,
            )

        # If the parent_report has a merchant_name, use it as the
        # human-friendly label.
        if isinstance(parent_report, dict):
            mn = parent_report.get("merchant_name")
            if isinstance(mn, str) and mn.strip():
                parent_label = mn.strip()

        response["comparison"] = build_cohort_comparison(
            parent_report=parent_report,
            parent_label=parent_label,
            cohort_runs=runs,
        )

    return response


@router.post("/cold-start-audit/export")
async def cold_start_audit_export(
    body: BdColdStartAuditRequest,
    format: str = "md",
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Response:
    """Same as /cold-start-audit but returns the report as a markdown
    file download instead of JSON. BD operators can then take the .md
    to a meeting, paste into a deck, or convert to PDF via any
    markdown processor (e.g. browser print on the rendered HTML).

    Wraps render_brand_markdown over the full brand_report.
    """
    from services.bd_cold_start_service import (
        BrandDiscoveryError,
        discover_products_for_audit,
    )
    from services.agent_center_bd_report_service import render_brand_markdown  # noqa: F401 (kept for backwards compat with v1 renderer fallback)
    from services.audit_markdown_renderer_v2 import select_renderer
    import re as _re

    try:
        discovered = await discover_products_for_audit(
            body.url,
            max_products=body.max_products,
            market=body.market,
            persist=not body.dry_run,
        )
    except BrandDiscoveryError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "discovery_failed", "message": str(exc)},
        )

    if not discovered.get("products"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "discovery_failed",
                "message": "Discovery returned zero products; nothing to export.",
            },
        )

    if body.dry_run:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "dry_run_not_exportable",
                "message": (
                    "Markdown export requires a real audit run. "
                    "Set dry_run=false."
                ),
            },
        )

    cold_start_integration_state: Dict[str, Any] = {
        "store_platform_integrated": False,
        "psp_integrated": False,
        "gsc_integrated": False,
        "fully_integrated": False,
        "missing_pieces": ["store_platform", "psp"],
        "integration_completed_at": None,
        "store_platform_name": None,
        "psp_provider": None,
        "store_connected_at": None,
    }

    # PR-1a (APM): same persistence as the JSON-returning cold-start
    # endpoint — both paths write to merchant_audit_runs so trend
    # tracking works regardless of which the operator uses. Best-
    # effort: DB failures don't block the export.
    from db.merchant_audit_runs import (
        record_audit_run_started,
        record_audit_run_completed,
        recent_runs_for_merchant,
    )
    synthetic_merchant_id = _prospect_merchant_id(
        discovered.get("merchant_domain") or body.url,
    )
    product_keys = [
        p.get("pdp_url") for p in (discovered.get("products") or [])
        if p.get("pdp_url")
    ]
    run_id = await record_audit_run_started(
        merchant_id=synthetic_merchant_id,
        product_keys=product_keys,
    )
    prior_runs = await recent_runs_for_merchant(
        merchant_id=synthetic_merchant_id, limit=5,
    )
    if run_id:
        prior_runs = [r for r in prior_runs if r.get("run_id") != run_id]

    try:
        out = await run_brand_report(
            merchant_name=discovered["merchant_name"],
            merchant_domain=discovered.get("merchant_domain"),
            products=discovered["products"],
            provider=body.provider,
            max_runs=body.max_runs,
            include_category_visibility=body.include_category_visibility,
            integration_state=cold_start_integration_state,
            prior_runs=prior_runs,
        )
    except Exception as exc:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc)[:2000],
        )
        raise _map_error(exc) from exc

    mock_reports = _detect_mock_reports(out.get("per_product") or [])
    if mock_reports:
        await record_audit_run_completed(
            run_id=run_id, status="failed",
            error_message="upstream_mock_fallback",
        )
        _raise_mock_rejection(mock_reports, "cold-start-audit/export")

    agg = out.get("aggregate") or {}
    verdict_labels = [
        ((p.get("verdict") or {}).get("label") or "")
        for p in (out.get("per_product") or [])
    ]
    await record_audit_run_completed(
        run_id=run_id,
        status="succeeded",
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=agg.get("avg_visibility"),
        attribution_score_avg=agg.get("avg_attribution"),
        category_visibility_score_avg=agg.get("avg_category_visibility"),
        report_jsonb=out,
    )

    # PR-9a: feature-flagged renderer selection. AUDIT_RENDERER_VERSION
    # env var picks v1 (default, existing) or v2 (polished structure
    # matching the hand-written Grüns PDF). Once v2 has soaked, the
    # default flips.
    # PR-9b: format query param picks markdown (default) / html / pdf.
    fmt = (format or "md").strip().lower()

    # Filename-safe brand slug. Strip non-alphanum, lowercase.
    brand = discovered.get("merchant_name") or "brand"
    slug = _re.sub(r"[^A-Za-z0-9]+", "-", brand).strip("-").lower() or "brand"

    if fmt in ("html", "pdf"):
        from services.audit_html_renderer import (
            html_to_pdf_bytes,
            render_brand_html_v2,
        )
        html_str = render_brand_html_v2(out, discovery=discovered)
        if fmt == "pdf":
            pdf_bytes = html_to_pdf_bytes(html_str)
            if pdf_bytes is not None:
                filename = f"{slug}-readiness-report.pdf"
                return Response(
                    content=pdf_bytes,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                    },
                )
            # PDF conversion not available (weasyprint not installed) —
            # fall back to HTML download with a header so client knows.
            logger.warning(
                "PDF conversion unavailable; falling back to HTML for "
                "cold-start audit export of %s",
                discovered.get("merchant_domain"),
            )
            filename = f"{slug}-readiness-report.html"
            return Response(
                content=html_str,
                media_type="text/html; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Pivota-PDF-Fallback": "weasyprint_not_installed",
                },
            )
        # fmt == "html"
        filename = f"{slug}-readiness-report.html"
        return Response(
            content=html_str,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # Default: markdown
    renderer = select_renderer()
    markdown = renderer(out, discovery=discovered)
    filename = f"{slug}-readiness-report.md"

    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _build_discovery_block(discovered: Dict[str, Any]) -> Dict[str, Any]:
    """Project the orchestrator's output to the response shape. Same
    structure for full-audit and dry-run responses so the BD portal
    can render the discovery panel uniformly."""
    return {
        "merchant_name": discovered["merchant_name"],
        "merchant_domain": discovered.get("merchant_domain"),
        "discovery_method": discovered["discovery_method"],
        "platform": discovered.get("platform"),
        "fallback_used": discovered.get("fallback_used"),
        "products_audited": [
            {"title": p["title"], "pdp_url": p["pdp_url"]}
            for p in discovered["products"]
        ],
        "products_discovered_total": discovered.get(
            "products_discovered_total",
        ),
        "products_persisted": discovered.get("products_persisted", 0),
        # catalog-extract-audit skill outputs — diagnostics + coverage
        # let BD operators judge whether the extraction is trustworthy
        # before relying on the verdict.
        "diagnostics": discovered.get("diagnostics"),
        "coverage": discovered.get("coverage"),
        # PR-B brand-level signals: Open Graph, Schema.org Organization,
        # social handles, sitemap structure, robots, SEO completeness.
        # Frontend BrandSnapshot component renders from this block.
        "brand_signals": discovered.get("brand_signals"),
        # PR-C Gemini-grounded brand context: retail presence, founder
        # story, press coverage. Each sub-field independently nullable.
        "brand_context": discovered.get("brand_context"),
        # PR-D Gemini-grounded social intelligence: own TikTok+IG
        # presence + KOL endorsements per platform + (optional)
        # competitive comparison.
        "social_intelligence": discovered.get("social_intelligence"),
    }


# ---------------------------------------------------------------------------
# UI-3: BD-side task queue + executor-runs read endpoints
# ---------------------------------------------------------------------------
#
# The merchant portal endpoints in routes/merchant_audit_routes.py are
# gated on get_current_merchant — they require a merchant JWT, which BD
# operators don't have. These BD wrappers take merchant_id as a query
# param (typically the prospect_<hash> id returned from cold-start
# audits) and gate on get_current_employee instead.
#
# Read endpoints are unchanged in semantics from the merchant route —
# same shape, same default filtering. Write endpoints (PATCH / dismiss)
# are also enabled so a BD operator can mark a prospect's tasks done /
# dismissed without onboarding the prospect first.


@router.get("/tasks")
async def bd_list_tasks(
    merchant_id: str,
    status_filter: Optional[str] = None,
    limit: int = 50,
    parent_audit_run_id: Optional[str] = None,
    include_history: bool = False,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """List tasks for any merchant (BD-side; no merchant auth needed).
    Pass `merchant_id=prospect_<hash>` to drive the BD task queue UI
    against a cold-start prospect.

    Default filter: open work (pending + in_progress). Pass
    `status_filter=all` for everything, or comma-list specific statuses.
    By default tasks are scoped to the latest completed audit; pass
    `include_history=true` for the previous flat all-runs list.
    """
    if limit <= 0 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 200",
        )
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="merchant_id query param required",
        )
    from db.merchant_tasks import list_tasks_for_merchant

    if status_filter is None:
        statuses = None
    elif status_filter.strip().lower() == "all":
        statuses = []
    else:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]

    explicit_run_id = (
        parent_audit_run_id.strip() if parent_audit_run_id else None
    )
    latest_audit_run_id: Optional[str] = None
    if explicit_run_id:
        tasks_scope = "explicit_run"
        scoped_parent_audit_run_id = explicit_run_id
        latest_audit_run_id = explicit_run_id
    else:
        # DEFAULT = persistent cross-audit workspace (mirrors the merchant route,
        # page-usability Step 1). The audit-completion reconciliation closes
        # prior-run pending tasks the latest audit dropped, so this is one clean
        # living list, not accumulating clutter. `include_history` kept as a
        # back-compat alias (same behavior now).
        tasks_scope = "persistent"
        scoped_parent_audit_run_id = None
        latest_audit_run_id = await _latest_completed_audit_run_id_for_tasks(
            merchant_id
        )
        # Lazy, idempotent backlog dedup (mirrors the merchant route): collapse
        # duplicate pending tasks on read so the persistent queue self-heals.
        try:
            from db.merchant_tasks import dedupe_pending_tasks

            await dedupe_pending_tasks(merchant_id=merchant_id)
        except Exception:  # noqa: BLE001
            logger.debug("dedupe_pending_tasks skipped", exc_info=True)

    tasks = await list_tasks_for_merchant(
        merchant_id=merchant_id,
        status_filter=statuses,
        limit=limit,
        parent_audit_run_id=scoped_parent_audit_run_id,
        # include_unscoped only matters when scoping TO a run; with parent=None
        # the query already returns standing NULL-parent tasks.
        include_unscoped=(scoped_parent_audit_run_id is not None),
    )
    return {
        "merchant_id": merchant_id,
        "count": len(tasks),
        "latest_audit_run_id": latest_audit_run_id,
        "tasks_scope": tasks_scope,
        "tasks": tasks,
    }


class _BdTaskStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(pending|in_progress|done|failed|ready_for_retest|verifying|verified|regressed)$")
    assigned_to_human: Optional[str] = Field(None, max_length=200)
    evidence: Optional[Dict[str, Any]] = None


@router.patch("/tasks/{task_id}")
async def bd_update_task(
    task_id: str,
    body: _BdTaskStatusUpdate,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Update a task's status. BD operator can take ownership
    (in_progress) or mark complete (done) on a prospect's tasks
    without merchant onboarding."""
    from db.merchant_tasks import fetch_task, update_task_status

    task = await fetch_task(task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    ok = await update_task_status(
        task_id=task_id,
        status=body.status,
        assigned_to_human=body.assigned_to_human,
        evidence=body.evidence,
    )
    if not ok:
        raise HTTPException(
            status_code=500, detail="task update failed (DB error)",
        )
    updated = await fetch_task(task_id=task_id)
    return {"task": updated}


class _BdTaskDismissBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.post("/tasks/{task_id}/dismiss")
async def bd_dismiss_task(
    task_id: str,
    body: _BdTaskDismissBody,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Dismiss a task with a reason. Terminal."""
    from db.merchant_tasks import dismiss_task, fetch_task

    task = await fetch_task(task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    ok = await dismiss_task(task_id=task_id, reason=body.reason)
    if not ok:
        raise HTTPException(
            status_code=500, detail="task dismiss failed (DB error)",
        )
    updated = await fetch_task(task_id=task_id)
    return {"task": updated}


@router.get("/executor-runs")
async def bd_list_executor_runs(
    merchant_id: str,
    agent_name: Optional[str] = None,
    limit: int = 20,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """List executor agent runs for any merchant (BD-side). Drives
    the "what Pivota did for this prospect" activity feed — surfaces
    GSC URL submissions, sitemap diffs, content briefs the agents
    auto-generated."""
    if limit <= 0 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100",
        )
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="merchant_id query param required",
        )
    from db.executor_runs import (
        recent_runs_for_agent,
        recent_runs_for_merchant as recent_executor_runs_for_merchant,
    )
    if agent_name:
        rows = await recent_runs_for_agent(agent_name=agent_name, limit=limit * 3)
        rows = [r for r in rows if r.get("merchant_id") == merchant_id][:limit]
    else:
        rows = await recent_executor_runs_for_merchant(
            merchant_id=merchant_id, limit=limit,
        )
    return {
        "merchant_id": merchant_id,
        "agent_name": agent_name,
        "count": len(rows),
        "runs": rows,
    }
