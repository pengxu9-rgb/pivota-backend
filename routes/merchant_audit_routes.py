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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from db.catalog import catalog_products
from db.database import database
from db.merchant_audit_runs import (
    count_runs_in_window,
    record_audit_run_completed,
    record_audit_run_started,
    recent_runs_for_merchant,
)
from db.merchant_onboarding import get_merchant_onboarding
from services.agent_center_bd_report_service import run_brand_report
from services.catalog_sync_service import make_pivota_canonical_fields
from utils.auth import get_current_merchant
from utils.logger import logger

router = APIRouter(
    prefix="/api/merchant-center/audit",
    tags=["merchant-center", "ai-readiness-audit"],
)


# Per-merchant rate-limit window. Phase C-4 PR-C moved the storage
# from an in-memory dict to the `merchant_audit_runs` table — see
# db/merchant_audit_runs.py — so the cap survives restarts and works
# across multiple backend pods. The window + cap stay here because
# they're route-policy values, not DB schema.
#
# Both are env-configurable so testing / staging / per-tier policy
# changes can lift the cap without a code deploy. Defaults preserve
# the original 2-per-24h cost guard. NOTE: this is a quota cap
# (max audits per merchant per window), NOT a per-audit multiplier
# (each audit still costs ~9 grounded Gemini calls × N products).
# Lifting the cap scales total daily LLM cost linearly with merchant
# count × cap; lifting per-audit multipliers is the dangerous one
# (see feedback_llm_call_multipliers.md / PR #278 incident).
import os as _os
_AUDIT_RATE_WINDOW_S = int(_os.getenv("MERCHANT_AUDIT_RATE_WINDOW_SECONDS", str(24 * 60 * 60)))
_AUDIT_RATE_MAX = int(_os.getenv("MERCHANT_AUDIT_RATE_MAX", "2"))


def _derive_canonical_url(
    *, merchant_domain: Optional[str], product_payload: Any,
) -> Optional[str]:
    """Build a buyer-facing PDP URL from the merchant's store domain
    + the product handle stored in the raw catalog payload. Returns
    None if either input is missing/unusable.

    This is deterministic derivation, not a fallback that hides bugs:
    the catalog sync stores the full upstream payload (which always
    has `handle` for Shopify), and the merchant's store_url is set
    at onboarding. We just compose them — no DB lookups, no I/O.
    """
    if not merchant_domain:
        return None
    payload: Dict[str, Any]
    if isinstance(product_payload, dict):
        payload = product_payload
    elif isinstance(product_payload, str):
        try:
            import json as _json
            payload = _json.loads(product_payload)
            if not isinstance(payload, dict):
                return None
        except Exception:
            return None
    else:
        return None
    handle = (
        (payload.get("handle") or "").strip()
        if isinstance(payload.get("handle"), str)
        else ""
    )
    if not handle:
        return None
    domain = merchant_domain.strip().rstrip("/")
    # Tolerate either "shop.myshopify.com" or "https://shop.myshopify.com"
    if "://" not in domain:
        domain = f"https://{domain}"
    return f"{domain}/products/{handle}"


async def _check_audit_rate_limit(merchant_id: str) -> int:
    """Returns remaining quota (>=0) if allowed, raises 429 if exceeded.

    Phase C-4 PR-C: counts persisted runs in `merchant_audit_runs`
    instead of an in-memory deque. Window + cap unchanged
    (`_AUDIT_RATE_WINDOW_S`, `_AUDIT_RATE_MAX`). On DB error
    `count_runs_in_window` returns 0 — degraded persistence shouldn't
    lock merchants out of auditing.
    """
    used = await count_runs_in_window(
        merchant_id=merchant_id,
        window_seconds=_AUDIT_RATE_WINDOW_S,
    )
    if used >= _AUDIT_RATE_MAX:
        # Best-effort fetch of the oldest run-in-window so we can
        # tell the caller when their quota resets. Falls back to the
        # full window if we can't determine it.
        recent = await recent_runs_for_merchant(
            merchant_id=merchant_id, limit=_AUDIT_RATE_MAX,
        )
        next_reset_in = _AUDIT_RATE_WINDOW_S
        if recent:
            try:
                from datetime import datetime as _dt
                oldest_iso = recent[-1].get("requested_at")
                if oldest_iso:
                    oldest = _dt.fromisoformat(oldest_iso.replace("Z", "+00:00"))
                    next_reset_in = max(
                        0,
                        _AUDIT_RATE_WINDOW_S - int(time.time() - oldest.timestamp()),
                    )
            except Exception:  # noqa: BLE001 — best effort
                pass
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": (
                    f"Daily audit limit reached "
                    f"({_AUDIT_RATE_MAX} per 24h)."
                ),
                "limit": _AUDIT_RATE_MAX,
                "window_seconds": _AUDIT_RATE_WINDOW_S,
                "next_reset_in_seconds": next_reset_in,
            },
        )
    return _AUDIT_RATE_MAX - used - 1


class ProductRef(BaseModel):
    """A reference to one of the merchant's catalog products. The
    frontend has these from `getProducts()` (returns `platform` +
    `platform_product_id`); the backend resolves them to catalog rows
    via `(merchant_id, platform, source_product_id)`."""

    platform: str = Field(..., min_length=1)
    source_product_id: str = Field(..., min_length=1)


class MerchantSelfAuditRequest(BaseModel):
    """1–5 of the merchant's own products. Vendor / type / pdp_url are
    not in the request — they come from catalog_products so the
    merchant can never audit URLs that aren't theirs."""

    products: List[ProductRef] = Field(..., min_length=1, max_length=5)
    max_runs: int = Field(3, ge=1, le=5)


@router.post("/ai-commerce-readiness")
async def run_merchant_self_audit(
    body: MerchantSelfAuditRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    remaining = await _check_audit_rate_limit(merchant_id)

    # 1. Build the set of (platform, source_product_id) tuples the
    #    merchant asked for. WHERE merchant_id=current is the cross-
    #    tenant guard — even if the same source_product_id exists for
    #    a different merchant, the AND merchant_id=current filter
    #    excludes it.
    refs = [(p.platform, p.source_product_id) for p in body.products]
    query = (
        select(
            catalog_products.c.product_key,
            catalog_products.c.platform,
            catalog_products.c.source_product_id,
            catalog_products.c.title,
            catalog_products.c.brand,
            catalog_products.c.product_type,
            catalog_products.c.canonical_url,
            catalog_products.c.product_payload,
            catalog_products.c.pivota_canonical_url,
            catalog_products.c.pivota_signature_id,
            catalog_products.c.pivota_signature_minted_at,
        )
        .where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.platform.in_([p for p, _ in refs]),
            catalog_products.c.source_product_id.in_([s for _, s in refs]),
        )
    )
    rows = await database.fetch_all(query)
    # Reconcile what the merchant asked for vs what catalog returned.
    # IN-list filters can return products matching either column
    # independently; we re-check the (platform, source_product_id) pair.
    rows = [
        r for r in rows
        if (r["platform"], r["source_product_id"]) in set(refs)
    ]
    found_pairs = {(r["platform"], r["source_product_id"]) for r in rows}
    missing = [
        {"platform": p, "source_product_id": s}
        for (p, s) in refs if (p, s) not in found_pairs
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": (
                    f"{len(missing)} product(s) not found for this "
                    f"merchant."
                ),
                "missing_products": missing,
            },
        )

    # 2. Resolve merchant display name + domain for the report header.
    #    The store_url also drives canonical_url derivation below.
    merchant = await get_merchant_onboarding(merchant_id) or {}
    merchant_name = (
        merchant.get("business_name")
        or merchant.get("legal_name")
        or merchant.get("store_url")
        or merchant_id
    )
    merchant_domain = (merchant.get("store_url") or "").strip() or None

    # 3. Build the products list run_brand_report expects.
    #    Derivation order for the buyer-facing pdp_url that the audit
    #    probes against:
    #      (a) catalog_products.canonical_url if set (preferred — set
    #          by catalog sync when the upstream payload includes it)
    #      (b) "{store_domain}/products/{handle}" derived from
    #          product_payload.handle (Shopify-style; catalog stores
    #          the full raw payload so we always have this for synced
    #          merchants even if canonical_url wasn't extracted)
    #      (c) None → bail out with a 422 listing the SKUs that need
    #          attention
    # URL resolution per selected product, in priority order:
    #   (a) catalog_products.canonical_url       — merchant's own URL
    #   (b) {store_domain}/products/{handle}     — derived from Shopify payload
    #   (c) catalog_products.pivota_canonical_url — Pivota canonical PDP URL
    #       (lazily minted + persisted if missing — every onboarded
    #       merchant product gets one; see migration 071 / make_pivota_
    #       canonical_fields). This is the AI-channel surface Pivota
    #       hosts on agent.pivota.cc — works even for URL-less catalog
    #       rows (manual imports / seed data) because we OWN the URL.
    products: List[Dict[str, Any]] = []
    pivota_url_used: List[str] = []  # surfaced to caller as informational
    for r in rows:
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
        # PR-D: track the canonical sig's mint timestamp per product
        # so the engine can compute the indexing-arc phase. Default to
        # the catalog row's existing value; lazy-mint sets it below.
        # Bracket access (not .get) — the `databases` Record type
        # resolves attribute lookups against column names; calling
        # `r.get(...)` raised TypeError because there's no `get`
        # column. `r["pivota_signature_minted_at"]` returns None when
        # the column is NULL for this row.
        try:
            pivota_minted_at = r["pivota_signature_minted_at"]
        except (KeyError, IndexError):
            pivota_minted_at = None
        if not pdp_url:
            # Fallback (c): Pivota canonical PDP URL. Lazily mint the
            # sig + URL if the catalog row predates migration 071.
            pivota_url = (r["pivota_canonical_url"] or "").strip()
            if not pivota_url:
                fields = make_pivota_canonical_fields(
                    merchant_id, r["platform"], r["source_product_id"],
                )
                pivota_url = fields["pivota_canonical_url"]
                pivota_minted_at = fields["pivota_signature_minted_at"]
                # Persist back so subsequent audits + sitemap renderer
                # see the same sig. Single-row UPDATE; cheap. Phase
                # C-4 PR-D: also writes pivota_signature_minted_at so
                # the next audit's indexing-arc state is computed
                # from a real timestamp.
                await database.execute(
                    catalog_products.update()
                    .where(
                        catalog_products.c.merchant_id == merchant_id,
                        catalog_products.c.platform == r["platform"],
                        catalog_products.c.source_product_id == r["source_product_id"],
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
            # PR-D: pivota_signature_minted_at threads through to
            # build_structured_report so merchant_view.diagnosis.
            # indexing_arc_state can compute the real phase.
            "pivota_signature_minted_at": pivota_minted_at,
            # Phase C-4 (PR-B): pass url_source through to the engine so
            # per-product `merchant_view.headline.audited_via_pivota_canonical`
            # is accurate. Was previously stripped before calling
            # run_brand_report; now consumed by build_structured_report's
            # url_source kwarg.
            "url_source": url_source,
        })

    # All products always resolve to a URL now (Pivota canonical fallback
    # always succeeds), so no skipped_products list. The `pivota_url_used`
    # list informs the caller which SKUs were audited against the Pivota
    # canonical PDP rather than the merchant's own URL — useful so the
    # UI can flag "this score reflects Pivota canonical surface, not
    # your storefront."

    logger.info(
        "merchant_self_audit_start merchant_id=%s sku_count=%d max_runs=%d",
        merchant_id, len(products), body.max_runs,
    )
    # Phase C-4 PR-C: persist audit run lifecycle to merchant_audit_runs.
    # Best-effort — DB failures degrade to "no history captured" but
    # never fail the audit itself.
    product_keys = [r["product_key"] for r in rows]
    run_id = await record_audit_run_started(
        merchant_id=merchant_id,
        product_keys=product_keys,
    )
    # Pull the merchant's recent audit runs BEFORE the new row's
    # status flips to 'succeeded' — the trend in merchant_view.tracking
    # is "your last N audits", not including the one running now.
    prior_runs = await recent_runs_for_merchant(
        merchant_id=merchant_id, limit=5,
    )
    # Filter out the just-inserted running row so trend is purely
    # historical.
    if run_id:
        prior_runs = [r for r in prior_runs if r.get("run_id") != run_id]
    try:
        brand_report = await run_brand_report(
            merchant_name=str(merchant_name),
            merchant_domain=merchant_domain,
            products=products,
            provider="gemini",
            max_runs=body.max_runs,
            prior_runs=prior_runs,
        )
    except ValueError as exc:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc),
        )
        # run_brand_report's input validators (e.g. "products capped at 5")
        # — surface as 422 since these are client-supplied bounds.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception as exc:
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc),
        )
        raise
    aggregate = brand_report.get("aggregate") or {}
    per_product = brand_report.get("per_product") or []
    verdict_labels = [
        ((p.get("verdict") or {}).get("label") or "")
        for p in per_product
    ]
    await record_audit_run_completed(
        run_id=run_id,
        status="succeeded",
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=aggregate.get("avg_visibility"),
        attribution_score_avg=aggregate.get("avg_attribution"),
        category_visibility_score_avg=aggregate.get("avg_category_visibility"),
        audited_via_pivota_canonical=pivota_url_used,
        report_jsonb=brand_report,
    )
    logger.info(
        "merchant_self_audit_done merchant_id=%s succeeded=%d failed=%d run_id=%s",
        merchant_id,
        aggregate.get("products_succeeded", 0),
        aggregate.get("products_failed", 0),
        run_id or "(persistence-skipped)",
    )

    return {
        "brand_report": brand_report,
        "rate_limit_remaining": remaining,
        # product_keys whose audit URL was the Pivota canonical PDP
        # (not the merchant's own URL) — UI surfaces a note that the
        # score reflects Pivota's hosted surface, which is in the
        # 30-90 day Google indexing arc post-creation. Empty when the
        # merchant's own URLs covered every selected SKU.
        "audited_via_pivota_canonical": pivota_url_used,
        # Phase C-4 PR-C: run_id of the persisted audit row, lets
        # callers fetch this audit's history entry later.
        "audit_run_id": run_id,
    }


@router.get("/history")
async def get_merchant_audit_history(
    limit: int = 5,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Return the most recent audit runs for this merchant, newest
    first. Drives the merchants-portal trend / history view + the
    `merchant_view.tracking.history_link` payload.

    Trend-only fields (no full report_jsonb) — fetch a specific run
    via its `run_id` if the full report is needed.
    """
    if limit <= 0 or limit > 50:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 50",
        )
    runs = await recent_runs_for_merchant(
        merchant_id=merchant_id, limit=limit,
    )
    return {
        "merchant_id": merchant_id,
        "runs": runs,
        "rate_limit": {
            "max": _AUDIT_RATE_MAX,
            "window_seconds": _AUDIT_RATE_WINDOW_S,
            "used_in_window": await count_runs_in_window(
                merchant_id=merchant_id,
                window_seconds=_AUDIT_RATE_WINDOW_S,
            ),
        },
    }
