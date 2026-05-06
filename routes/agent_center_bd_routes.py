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

from fastapi import APIRouter, Depends, HTTPException
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
    upstream_status = report.get("upstream_status") or {}
    if not upstream_status.get("is_real"):
        # Loud log when we shipped mock data despite a real-provider
        # request — most likely root cause is missing env var on either
        # backend (PIVOTA_AGENT_INTERNAL_API_KEY) or upstream
        # (GEMINI_API_KEY). BD runs are useless without real data; ops
        # should fix before any rep ships a report to a merchant.
        logger.warning(
            "BD report fell back to MOCK — requested=%s visibility=%s attribution=%s reason=%s",
            upstream_status.get("requested_provider"),
            upstream_status.get("visibility_provider"),
            upstream_status.get("attribution_provider"),
            upstream_status.get("reason"),
        )
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
