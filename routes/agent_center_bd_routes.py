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

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, validator

from services.agent_center_bd_report_service import (
    build_structured_report,
    run_bd_probes,
    run_brand_report,
)
from utils.auth import get_current_employee

router = APIRouter(prefix="/api/agent-center/bd", tags=["agent-center", "bd"])
logger = logging.getLogger(__name__)


_HARD_MAX_RUNS = 8


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
                f"prose. Check that PIVOTA_AGENT_INTERNAL_API_KEY and "
                f"the upstream's GEMINI_API_KEY are configured. "
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


_BRAND_REPORT_HARD_MAX_PRODUCTS = 5


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
    return {
        "status": "ok",
        "discovery": _build_discovery_block(discovered),
        "brand_report": out,
    }


@router.post("/cold-start-audit/export")
async def cold_start_audit_export(
    body: BdColdStartAuditRequest,
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
    from services.agent_center_bd_report_service import render_brand_markdown
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

    markdown = render_brand_markdown(out, discovery=discovered)

    # Filename-safe brand slug. Strip non-alphanum, lowercase.
    brand = discovered.get("merchant_name") or "brand"
    slug = _re.sub(r"[^A-Za-z0-9]+", "-", brand).strip("-").lower() or "brand"
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
