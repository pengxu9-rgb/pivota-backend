"""
Merchant self-service AI Commerce Readiness audit.

Pairs the BD-side audit engine (`services.agent_center_bd_report_service.
run_brand_report`) with merchant-auth so onboarded merchants can run the
same multi-SKU audit on their own catalog from inside the merchants
portal — no BD/ops handoff needed.

Surface:
  - POST /api/merchant-center/audit/ai-commerce-readiness
    body:    { product_keys: List[str] (1-5), max_runs?: int = 3 }
    auth:    merchant JWT (Bearer); token must carry role="merchant" and
             merchant_id claim
    returns: { brand_report: <run_brand_report output>,
               rate_limit_remaining: int }
    errors:
      401 — no/invalid token (handled by get_current_user upstream)
      403 — token's role isn't "merchant"
      422 — product_keys empty or > 5 (Pydantic validation)
      404 — any product_key in the list isn't owned by this merchant
      429 — per-merchant audit budget exhausted (2 / 24h)

Cost guard. Multi-SKU audits cost ~9 grounded Gemini calls per product
× up to 5 products = up to 45 calls per audit. Per-merchant rate limit
caps at 2 audits / 24h → 90 calls/day worst case. Bounded enough for
MVP; replace with a per-tier quota table when billing tiers exist.

Cross-tenant guard. The catalog lookup is `WHERE merchant_id = current
AND product_key IN (...)`. A product_key that exists globally but is
owned by a different merchant won't load — surfaced as 404 alongside
genuinely missing keys.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from db.catalog import catalog_products
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from services.agent_center_bd_report_service import run_brand_report
from utils.auth import get_current_merchant
from utils.logger import logger

router = APIRouter(
    prefix="/api/merchant-center/audit",
    tags=["merchant-center", "ai-readiness-audit"],
)


# Per-merchant rate-limit storage. In-memory dict keyed by merchant_id;
# values are deque[float] of timestamps for runs in the trailing window.
# Mirrors the simple pattern in middleware/rate_limiter.py — swap to
# Redis when we need cross-instance state. For now the audit endpoint
# is low-traffic enough that a single-process counter is fine.
_AUDIT_RATE_WINDOW_S = 24 * 60 * 60   # 24 hours
_AUDIT_RATE_MAX = 2                   # audits per merchant per window
_audit_run_history: Dict[str, List[float]] = {}


def _check_audit_rate_limit(merchant_id: str) -> int:
    """Returns remaining quota (>=0) if allowed, raises 429 if exceeded.
    Pure side effect of recording the audit timestamp on success."""
    now = time.time()
    history = _audit_run_history.setdefault(merchant_id, [])
    # Drop expired entries
    cutoff = now - _AUDIT_RATE_WINDOW_S
    history[:] = [ts for ts in history if ts > cutoff]
    if len(history) >= _AUDIT_RATE_MAX:
        next_reset_in = int(_AUDIT_RATE_WINDOW_S - (now - history[0]))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": (
                    f"Daily audit limit reached "
                    f"({_AUDIT_RATE_MAX} per 24h)."
                ),
                "limit": _AUDIT_RATE_MAX,
                "window_seconds": _AUDIT_RATE_WINDOW_S,
                "next_reset_in_seconds": max(0, next_reset_in),
            },
        )
    history.append(now)
    return _AUDIT_RATE_MAX - len(history)


class MerchantSelfAuditRequest(BaseModel):
    """1–5 of the merchant's own product_keys. Vendor / type / pdp_url
    are not in the request — they come from catalog_products so the
    merchant can never audit URLs that aren't theirs."""

    product_keys: List[str] = Field(..., min_length=1, max_length=5)
    max_runs: int = Field(3, ge=1, le=5)


@router.post("/ai-commerce-readiness")
async def run_merchant_self_audit(
    body: MerchantSelfAuditRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    remaining = _check_audit_rate_limit(merchant_id)

    # 1. Look up products. WHERE merchant_id=current AND product_key IN
    #    keys handles the cross-tenant guard implicitly — products owned
    #    by another merchant simply aren't loaded.
    query = (
        select(
            catalog_products.c.product_key,
            catalog_products.c.title,
            catalog_products.c.brand,
            catalog_products.c.product_type,
            catalog_products.c.canonical_url,
        )
        .where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.product_key.in_(body.product_keys),
        )
    )
    rows = await database.fetch_all(query)
    found_keys = {r["product_key"] for r in rows}
    missing = [k for k in body.product_keys if k not in found_keys]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": (
                    f"{len(missing)} product_key(s) not found for this "
                    f"merchant."
                ),
                "missing_product_keys": missing,
            },
        )
    # Reject any product missing the canonical_url field — the audit
    # probe requires a buyer-facing URL to score attribution against.
    no_url = [
        r["product_key"] for r in rows if not (r["canonical_url"] or "").strip()
    ]
    if no_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    "Some selected products have no canonical_url in "
                    "the catalog. Run SKU Match first to populate it, "
                    "or pick different products."
                ),
                "product_keys_missing_canonical_url": no_url,
            },
        )

    # 2. Resolve merchant display name + domain for the report header.
    merchant = await get_merchant_onboarding(merchant_id) or {}
    merchant_name = (
        merchant.get("business_name")
        or merchant.get("legal_name")
        or merchant.get("store_url")
        or merchant_id
    )
    merchant_domain = merchant.get("store_url") or None

    # 3. Build the products list run_brand_report expects.
    products = [
        {
            "title": r["title"],
            "vendor": r["brand"],
            "product_type": r["product_type"],
            "pdp_url": r["canonical_url"],
        }
        for r in rows
    ]

    logger.info(
        "merchant_self_audit_start merchant_id=%s sku_count=%d max_runs=%d",
        merchant_id, len(products), body.max_runs,
    )
    try:
        brand_report = await run_brand_report(
            merchant_name=str(merchant_name),
            merchant_domain=merchant_domain,
            products=products,
            provider="gemini",
            max_runs=body.max_runs,
        )
    except ValueError as exc:
        # run_brand_report's input validators (e.g. "products capped at 5")
        # — surface as 422 since these are client-supplied bounds.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    logger.info(
        "merchant_self_audit_done merchant_id=%s succeeded=%d failed=%d",
        merchant_id,
        (brand_report.get("aggregate") or {}).get("products_succeeded", 0),
        (brand_report.get("aggregate") or {}).get("products_failed", 0),
    )

    return {
        "brand_report": brand_report,
        "rate_limit_remaining": remaining,
    }
