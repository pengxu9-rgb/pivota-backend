"""
Merchant self-service AI Commerce Readiness audit.

Pairs the BD-side audit engine (`services.agent_center_bd_report_service.
run_brand_report`) with merchant-auth so onboarded merchants can run the
same multi-SKU audit on their own catalog from inside the merchants
portal — no BD/ops handoff needed.

Surface:
  - POST /api/merchant-center/audit/ai-commerce-readiness
    body:    { product_keys: List[str] (1-50), max_runs?: int = 3 }
    auth:    merchant JWT (Bearer); token must carry role="merchant" and
             merchant_id claim
    returns: { brand_report: <run_brand_report output>,
               rate_limit_remaining: int }
    errors:
      401 — no/invalid token (handled by get_current_user upstream)
      403 — token's role isn't "merchant"
      422 — product_keys empty or > 50 (Pydantic validation)
      402 — insufficient credits (post-preview gate, spec §I)
      404 — any product_key in the list isn't owned by this merchant
      429 — per-merchant audit budget exhausted (2 / 24h, free tier only)

Cost guard. Spec §I — credit-balance pre-flight (POST /api/audits/preview)
is the authoritative cost gate. Legacy 9-probes-per-product mode is
capped at 50 SKUs → up to 450 grounded Gemini calls; v3 per-SKU mode
runs ~40 prompts per SKU × 50 SKUs → up to 2,000 calls per audit. Free
tier keeps the 2-audits-per-24h rate limit alongside the credit gate;
paid tiers bypass the rate limit and use credits only.

Cross-tenant guard. The catalog lookup is `WHERE merchant_id = current
AND product_key IN (...)`. A product_key that exists globally but is
owned by a different merchant won't load — surfaced as 404 alongside
genuinely missing keys.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from db.catalog import catalog_products
from db.apm_config import (
    ApmConfigValidationError,
    get_apm_config,
    upsert_apm_config,
)
from db.database import database
from db.merchant_audit_runs import (
    count_runs_for_merchant_by_subject,
    count_runs_in_window,
    fetch_audit_run_by_id,
    record_audit_run_completed,
    record_audit_run_started,
    recent_runs_for_merchant,
)
from db.merchant_onboarding import get_merchant_onboarding
from services.agent_center_bd_report_service import (
    apply_buyer_path_verdict_to_brand_report,
    run_brand_report,
    run_wedge_hero_sku_intelligence,
    sanitize_report_for_merchant,
)
from services.catalog_identity import make_content_key
from services.catalog_sync_service import make_pivota_canonical_fields
from services.merchant_audit_readiness import assess_merchant_audit_readiness
from services.merchant_credit_balance_service import get_balance
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


def _detect_mock_per_product(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the per-product reports whose upstream returned mock /
    synthetic fallback data. Used by the merchant audit endpoint to
    refuse shipping fabricated prose.

    Three pollution sources in the audit pipeline:

      1. `_local_mock_result` in services.agent_center_llm_client
         fires when this backend's PIVOTA_AGENT_INTERNAL_API_KEY is
         unset (returns provider="local_mock_no_internal_key")
      2. Upstream Pivota-Agent service's mock fires when its own
         Gemini key is unset (provider="mock_fallback_no_gemini_key")
      3. Explicit provider="mock" via pivota_agent_center_mock_gemini
         flag

    Each produces per_product upstream_status with is_real=False.
    Without this guard, the audit pipeline would render full
    diagnostic prose (verdict.explanation, plain_summary, action_items)
    against the synthetic data — the merchant sees a fabricated
    report that looks identical to a real run.

    Conservative default: missing/malformed `upstream_status` is
    treated as REAL (is_real defaults True). Don't reject audits
    just because a per-product report lost its status field — that's
    a different bug class.
    """
    per_product = brand_report.get("per_product") or []
    return [
        p for p in per_product
        if isinstance(p, dict)
        and not (p.get("upstream_status") or {}).get("is_real", True)
    ]


class MerchantSelfAuditRequest(BaseModel):
    """1–5 of the merchant's own products. Vendor / type / pdp_url are
    not in the request — they come from catalog_products so the
    merchant can never audit URLs that aren't theirs."""

    products: List[ProductRef] = Field(..., min_length=1, max_length=5)
    max_runs: int = Field(3, ge=1, le=5)


def _dominant_platform_from_catalog_rows(rows: List[Any]) -> str:
    platforms = []
    for row in rows or []:
        try:
            platform = str(row["platform"] or "").strip()
        except (KeyError, TypeError):
            platform = ""
        if platform:
            platforms.append(platform)
    if not platforms:
        return "shopify"
    return Counter(platforms).most_common(1)[0][0]


async def _enforce_legacy_audit_readiness(
    *, merchant_id: str, product_rows: List[Any],
) -> None:
    """Block legacy merchant audits until the same readiness deps are present.

    The canonical async POST /api/audits already has this gate. This legacy
    endpoint has two launch arms that must also fail closed so older clients
    cannot produce the false all-blocked first audit the readiness probe exists
    to prevent.
    """
    platform = _dominant_platform_from_catalog_rows(product_rows)
    readiness = await assess_merchant_audit_readiness(merchant_id, platform)
    if readiness.get("ready"):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "merchant_not_audit_ready",
            "message": (
                "Your catalog is still being prepared for audit (content "
                "quality backfill in progress). This usually completes within "
                "1-2 minutes of a product sync - please try again shortly."
            ),
            "blocking_gaps": readiness.get("blocking_gaps") or [],
            "counts": readiness.get("counts") or {},
            "platform": platform,
            "retry_after_seconds": 60,
        },
    )


class ApmConfigureRequest(BaseModel):
    enabled: bool
    cadence_days: int
    scope: Dict[str, Any] = Field(default_factory=dict)


# P2.4: poll budget for the opt-in async-pipeline compat path.
#
# P5.8.6 update: dropped from 30s → 5s. The operational review found
# that 30s polls were the dominant cause of connection-pool exhaustion
# under modest legacy traffic — each poll held an HTTP handler + DB
# connection for the full window. With the new async pipeline, audits
# rarely complete in <30s anyway (worker tick is 10s + 6 stages); the
# 5s budget reliably returns 202 fast, letting the merchant portal
# poll GET /api/audits/{id} from the client side without tying up
# server resources.
_COMPAT_POLL_BUDGET_SECONDS = 5
_COMPAT_POLL_INTERVAL_SECONDS = 1.0


async def _run_async_pipeline_compat(
    *,
    body: "MerchantSelfAuditRequest",
    merchant_id: str,
    response: Response,
    rate_limit_remaining: Optional[int] = None,
) -> Dict[str, Any]:
    """P2.4 compat shim: route the legacy synchronous endpoint through
    the new async pipeline. Enqueue + poll for up to 30s. If the run
    completes within the budget, return the legacy response shape
    (clients see no behavior change). Otherwise return 202 + run_id
    for the caller to poll via GET /api/audits/{run_id}.

    The legacy route's product-resolution logic still runs in this
    code path — the new pipeline only takes product_keys, so we
    have to convert (platform, source_product_id) refs to keys
    here. P3 will unify this resolution step.
    """
    import asyncio as _asyncio
    from db.merchant_audit_runs import (
        enqueue_audit_run,
        fetch_audit_run_by_id,
        find_in_flight_by_idempotency_key,
        STAGE_COMPLETED, STAGE_FAILED, STAGE_CANCELLED,
    )
    from services.idempotency import compute_audit_idempotency_key

    # Resolve (platform, source_product_id) refs to product_keys —
    # the new pipeline indexes by product_key.
    refs = [(p.platform, p.source_product_id) for p in body.products]
    rows = await database.fetch_all(
        select(
            catalog_products.c.product_key,
            catalog_products.c.platform,
            catalog_products.c.source_product_id,
        ).where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.platform.in_([p for p, _ in refs]),
            catalog_products.c.source_product_id.in_(
                [s for _, s in refs],
            ),
        )
    )
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
    product_keys = [r["product_key"] for r in rows]

    await _enforce_legacy_audit_readiness(
        merchant_id=merchant_id,
        product_rows=rows,
    )

    # Idempotency: if an in-flight run exists for the same
    # (merchant, product_keys, window) tuple, return its run_id
    # instead of enqueueing a duplicate.
    idempotency_key = compute_audit_idempotency_key(
        merchant_id=merchant_id, product_keys=product_keys,
        subject_type="merchant",
    )
    run_id = await find_in_flight_by_idempotency_key(
        idempotency_key=idempotency_key,
    )
    if not run_id:
        run_id = await enqueue_audit_run(
            merchant_id=merchant_id,
            product_keys=product_keys,
            subject_type="merchant",
            idempotency_key=idempotency_key,
            requested_by_user_id=merchant_id,
        )
    if not run_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Failed to enqueue audit run via async pipeline."
            ),
        )

    # Poll until terminal or budget elapsed.
    deadline = (
        _asyncio.get_event_loop().time()
        + _COMPAT_POLL_BUDGET_SECONDS
    )
    row: Optional[Dict[str, Any]] = None
    while _asyncio.get_event_loop().time() < deadline:
        row = await fetch_audit_run_by_id(run_id=run_id)
        if row and row.get("stage") in {
            STAGE_COMPLETED, STAGE_FAILED, STAGE_CANCELLED,
        }:
            break
        await _asyncio.sleep(_COMPAT_POLL_INTERVAL_SECONDS)

    if row is None or row.get("stage") not in {
        STAGE_COMPLETED, STAGE_FAILED, STAGE_CANCELLED,
    }:
        # Still in-flight at deadline — return 202 + run_id so
        # the caller can poll GET /api/audits/{id}. Legacy clients
        # that don't expect 202 will see audit_run_id in the body
        # and can either retry or migrate.
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            "brand_report": None,
            "rate_limit_remaining": rate_limit_remaining,
            "executors": None,
            "tasks": None,
            "audited_via_pivota_canonical": [],
            "audit_run_id": run_id,
            "compat_status": "in_flight_at_poll_deadline",
            "compat_poll_endpoint": f"/api/audits/{run_id}",
        }

    if row.get("stage") == STAGE_FAILED:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": (
                    row.get("error_message")
                    or "Audit pipeline failed"
                ),
                "audit_run_id": run_id,
                "error_jsonb": row.get("error_jsonb"),
            },
        )
    if row.get("stage") == STAGE_CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Audit was cancelled",
                "audit_run_id": run_id,
            },
        )

    # COMPLETED — reshape into the legacy response.
    return {
        "brand_report": sanitize_report_for_merchant(row.get("report_jsonb")),
        # P1-1: rate-limit check now runs before the via branch, so
        # async-compat callers get the same quota visibility the
        # sync arm has always emitted.
        "rate_limit_remaining": rate_limit_remaining,
        # Executors + tasks summaries live in partial_result_jsonb.
        "executors": (
            (row.get("partial_result_jsonb") or {})
            .get("materializing")
        ),
        "tasks": (
            (row.get("partial_result_jsonb") or {})
            .get("materializing")
        ),
        "audited_via_pivota_canonical": (
            row.get("audited_via_pivota_canonical") or []
        ),
        "audit_run_id": run_id,
    }


@router.post("/ai-commerce-readiness")
async def run_merchant_self_audit(
    body: MerchantSelfAuditRequest,
    response: Response,
    via: Optional[str] = None,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Synchronous AI Commerce Readiness audit.

    P2.4 deprecation: this endpoint is the legacy synchronous path.
    The canonical successor is the async lifecycle exposed at
    POST /api/audits + GET /api/audits/{run_id}. New integrations
    should target the async endpoints; existing callers continue
    to work unchanged.

    Opt-in async route: pass `?via=async_pipeline` to enqueue the
    audit through the new pipeline + poll for up to 30s. If the run
    completes within the budget, the response shape is identical to
    the synchronous path (legacy callers can opt in transparently).
    If the run is still in-flight at the budget, the response is
    202 Accepted with `audit_run_id` for the caller to poll via
    GET /api/audits/{run_id}.
    """
    # P2.4: deprecation signaling. RFC 8594 (Sunset) + RFC 8288
    # (Link). Sunset date is intentionally distant — we want at
    # least 6 months for clients to migrate before retiring this
    # endpoint. Telemetry on this endpoint will tell us when it
    # safe to flip.
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Sat, 01 Nov 2026 00:00:00 GMT"
    response.headers["Link"] = (
        '</api/audits>; rel="successor-version", '
        '</api/audits>; rel="alternate"; '
        'title="Async audit lifecycle (POST + GET poll)"'
    )

    # P1-1: enforce the daily-cap rate limit BEFORE branching to the
    # async-compat path. Originally _check_audit_rate_limit only ran
    # in the sync arm below; adding `?via=async_pipeline` was enough
    # to bypass the cap entirely. Both paths must share the same
    # quota window so a noisy client can't burn provider tokens by
    # opting into the compat shim.
    remaining = await _check_audit_rate_limit(merchant_id)

    if via == "async_pipeline":
        return await _run_async_pipeline_compat(
            body=body, merchant_id=merchant_id, response=response,
            rate_limit_remaining=remaining,
        )

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

    await _enforce_legacy_audit_readiness(
        merchant_id=merchant_id,
        product_rows=rows,
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
                # Stage 1 (mig 083): if this row predates content_key
                # (NULL), back-fill at the same time. Brand+title only;
                # the SELECT above doesn't pull GTIN. Caller is the
                # audit codepath, not an ingest — backfill_content_key
                # script will fill GTIN where it exists from elsewhere.
                # `or None` keeps the UPDATE a no-op when brand or
                # title are empty (make_content_key returns None then).
                _lazy_content_key = make_content_key(
                    r["brand"], r["title"], None
                )
                _update_values = {
                    "pivota_signature_id": fields["pivota_signature_id"],
                    "pivota_canonical_url": pivota_url,
                    "pivota_signature_minted_at": pivota_minted_at,
                }
                if _lazy_content_key:
                    _update_values["content_key"] = _lazy_content_key
                await database.execute(
                    catalog_products.update()
                    .where(
                        catalog_products.c.merchant_id == merchant_id,
                        catalog_products.c.platform == r["platform"],
                        catalog_products.c.source_product_id == r["source_product_id"],
                    )
                    .values(**_update_values)
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

    # Phase 0: integration-state-aware actions. Compute ONCE per audit
    # (state is merchant-level, not per-product) so the engine can
    # decide whether to surface "Complete Pivota integration" as the
    # #1 critical action. Best-effort — any lookup failure produces
    # an empty state, which means the integration action surfaces by
    # default (better to over-prompt onboarding than to mislead a
    # half-integrated merchant into thinking they're done).
    try:
        from services.merchant_integration_state import get_integration_state
        integration_state = await get_integration_state(merchant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "integration_state lookup failed for merchant %s: %s",
            merchant_id, exc,
        )
        integration_state = None
    # P1-3: scope every probe call inside this legacy-sync audit with
    # audit_run_id + merchant_id so the probe telemetry rows
    # (db/llm_probe_runs) carry attribution. Pre-fix, run_brand_report
    # ran outside any audit_telemetry context — probe rows had
    # audit_run_id=None / merchant_id=None and per-run cost rollups
    # missed all live legacy traffic. The new pipeline already wraps
    # via audit_run_worker; this brings the sync arm up to parity.
    from services.audit_telemetry_context import audit_telemetry
    try:
        async with audit_telemetry(
            run_id=run_id, merchant_id=merchant_id,
        ):
            brand_report = await run_brand_report(
                merchant_name=str(merchant_name),
                merchant_domain=merchant_domain,
                products=products,
                provider="gemini",
                max_runs=body.max_runs,
                prior_runs=prior_runs,
                integration_state=integration_state,
                merchant_id=merchant_id,
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

    # Mock-data guard: refuse to ship merchant-facing audit prose
    # against synthetic fallback data. See _detect_mock_per_product
    # for the full rationale + the three pollution sources.
    mock_per_product = _detect_mock_per_product(brand_report)
    if mock_per_product:
        first_reason = (
            (mock_per_product[0].get("upstream_status") or {}).get("reason")
            or "Upstream returned mock data."
        )
        await record_audit_run_completed(
            run_id=run_id,
            status="failed",
            error_message=f"upstream_mock_fallback: {first_reason}",
        )
        logger.error(
            "merchant audit refusing to ship mock-derived prose for "
            "merchant=%s; %d products had upstream_status.is_real=False",
            merchant_id, len(mock_per_product),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Audit pipeline upstream returned synthetic fallback data; "
                "refusing to render merchant-facing prose. Check that the "
                "probe-auth secret (PROMOTIONS_ADMIN_KEY — preferred; or "
                "AGENT_API_KEY / PIVOTA_AGENT_INTERNAL_API_KEY) and the "
                "upstream's GEMINI_API_KEY are configured. Re-run the audit "
                "once the upstream is real."
            ),
        )

    # Phase B: verify Gemini's `competitors_named` self-report against
    # the actual cited articles. Mutates each playbook action's
    # `evidence.co_occurrence_verification` with the result. Best-effort
    # — wrapped errors don't fail the audit. Total wall time is bounded
    # by the slowest fetch (5s) regardless of how many articles, since
    # verifications run in parallel.
    try:
        from services.co_occurrence_finder import (
            verify_brand_report_co_occurrence,
        )
        await verify_brand_report_co_occurrence(
            brand_report,
            merchant_brand=str(merchant_name),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("co-occurrence verification failed: %s", exc)

    # Phase D: auto-submit Pivota canonical PDP URLs to Google Indexing
    # API for merchants who have granted GSC access. Best-effort — any
    # failure is recorded in gsc_url_submissions with last_status='error'
    # and surfaced in merchant_view.tracking.gsc_submission_status.
    # Audit response succeeds either way. Submissions run in parallel;
    # total wall time bounded by slowest Google call (~2s) regardless
    # of N.
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
        # Refresh the aggregate state AFTER submissions ran, then
        # mutate every per_product merchant_view.tracking so the
        # response reflects what just happened (not pre-submit state).
        submission_state = await get_gsc_submission_state(merchant_id)
        for report in (brand_report.get("per_product") or []):
            if not isinstance(report, dict):
                continue
            mv = report.get("merchant_view") or {}
            tracking = mv.get("tracking") or {}
            tracking["gsc_submission_status"] = submission_state
            mv["tracking"] = tracking
    except Exception as exc:  # noqa: BLE001
        logger.warning("gsc auto-submit failed for merchant=%s: %s", merchant_id, exc)
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

    # PR-4a: dispatch executor agents in the background. Closes the
    # loop on advisory actions: instead of just rendering "submit your
    # sitemap to GSC" in the report, the GscUrlSubmissionAgent
    # actually does it. Best-effort + fire-and-forget — agent failures
    # don't affect the audit response.
    executor_summary = None
    try:
        import asyncio as _asyncio
        from services.executor_agents.base import ExecutorContext
        from services.executor_agents.dispatcher import dispatch_agents
        ctx = ExecutorContext(
            merchant_id=merchant_id,
            parent_audit_run_id=run_id,
            audit_report=brand_report,
        )
        # Fire-and-forget: don't block the response on executor work
        # (GSC submissions can take ~30s for large catalogs).
        _asyncio.create_task(
            dispatch_agents(ctx),
            name=f"executor-dispatch-{run_id}",
        )
        executor_summary = {
            "queued": True,
            "poll_via_executor_runs_table": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "executor dispatch enqueue failed for merchant=%s: %s",
            merchant_id, exc,
        )

    # PR-6: materialize action_items as tracked merchant_tasks so the
    # operator gets a queue (status: pending → in_progress → done)
    # instead of advisory text they have to remember. Best-effort.
    tasks_summary = None
    try:
        from services.task_queue_service import materialize_tasks_from_audit
        if run_id:
            tasks_summary = await materialize_tasks_from_audit(
                merchant_id=merchant_id,
                audit_run_id=run_id,
                audit_report=brand_report,
                integration_state=integration_state,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "task materialization failed for merchant=%s: %s",
            merchant_id, exc,
        )

    return {
        "brand_report": sanitize_report_for_merchant(brand_report),
        "rate_limit_remaining": remaining,
        "executors": executor_summary,
        "tasks": tasks_summary,
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


# Free URL-audit wedge (Tier 1) per-merchant allowance — the cost guard on the
# free tier. Env-overridable so it can be tuned (or lifted) without a deploy; a
# value <= 0 LIFTS the cap entirely (unlimited).
#
# TESTING (2026-06-03): defaulted to 0 (unlimited) while the founder validates
# the wedge pre-launch. RESTORE before onboarding real merchants — set env
# FREE_URL_AUDITS_PER_MERCHANT=2, or revert this default to 2 — otherwise every
# merchant gets unlimited free audits (Gemini cost + no upsell pressure).
_FREE_URL_AUDITS_PER_MERCHANT = int(
    _os.getenv("FREE_URL_AUDITS_PER_MERCHANT", "0")
)
# Two sequential upstream runs keep a real evidence floor instead of binary
# 1-sample verdicts; the LLM probe timeout is sized for grounded searches.
_WEDGE_MAX_RUNS = int(_os.getenv("WEDGE_MAX_RUNS", "2"))
_WEDGE_RUN_STALE_TTL_S = int(_os.getenv("WEDGE_RUN_STALE_TTL_S", "900"))


class MerchantUrlAuditRequest(BaseModel):
    """POST /url-readiness body — merchant-CURATED Tier-1 wedge.

    The merchant gives us their brand site + the specific product URLs they
    want audited (their own hero SKUs). We FETCH each URL for clean, real data
    (Shopify `.json` / PDP JSON-LD + OpenGraph) and audit exactly those — we do
    NOT auto-discover or guess which products to audit (auto-discovery was the
    #1 source of bad audits). The merchant knows their catalog; nobody guesses.
    """

    product_urls: List[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description=(
            "1–5 product page URLs the merchant wants audited (their hero "
            "SKUs). We fetch each for clean title / vendor / type data."
        ),
    )
    website: Optional[str] = Field(
        default=None,
        max_length=2000,
        description=(
            "Brand storefront URL, for brand-level context. Defaults to the "
            "merchant's onboarding store_url when omitted."
        ),
    )
    brand: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Brand name. Derived from the site domain / fetched product "
            "vendors when omitted."
        ),
    )


# ---------------------------------------------------------------------------
# Wedge honesty post-processing (Phase A). The legacy brand-report engine adds
# a canned `industry_context` template and buries a self-undermining "we did
# not verify whether those sources mention your brand" hedge inside each
# verdict. For the free wedge we DON'T touch the engine — we post-process the
# assembled report so it ships honest, sample-scoped prose, and we state the
# real verification limitation ONCE, upfront, in a `methodology` disclosure
# (built by the handler) instead of as a caveat that undercuts every line.
# Engine-level verdict/credibility fixes are Phases B–C.
# ---------------------------------------------------------------------------

# Matches the buried "we did not verify whether those sources mention <brand>"
# hedge with whatever connector precedes it (period / em-dash / semicolon).
_HEDGE_CONNECTED_RE = re.compile(
    r"\s*[—;.]\s*[Ww]e did not verify whether those sources mention "
    r"(?:your brand or products|your brand|the brand)\.?",
)
# Same clause as a standalone capitalized sentence (no leading connector).
_HEDGE_STANDALONE_RE = re.compile(
    r"\s*[Ww]e did not verify whether those sources mention "
    r"(?:your brand or products|your brand|the brand)\.?",
)


def _strip_unverified_hedge(text: str) -> str:
    """Remove the buried 'we did not verify whether those sources mention…'
    hedge from a verdict explanation, leaving clean sample-scoped prose. The
    same limitation is stated upfront in the report's `methodology` block."""
    if not text or "did not verify" not in text:
        return text
    s = _HEDGE_CONNECTED_RE.sub(".", text)
    s = _HEDGE_STANDALONE_RE.sub("", s)
    # Inline variant: "...grounded their answers in third-party sources we did
    # not verify." → end the sentence cleanly.
    s = s.replace("third-party sources we did not verify.", "third-party sources.")
    s = s.replace(" we did not verify.", ".")
    # Normalize whitespace + collapse any doubled period left by the excision.
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\.\s*\.", ".", s)
    s = re.sub(r"\s+\.", ".", s)
    return s


def _scrub_wedge_report_in_place(node: Any) -> None:
    """Recursively drop canned `industry_context` and strip the unverified
    hedge from every string in the assembled wedge report."""
    if isinstance(node, dict):
        node.pop("industry_context", None)
        for k, v in list(node.items()):
            if isinstance(v, str) and "did not verify" in v:
                node[k] = _strip_unverified_hedge(v)
            else:
                _scrub_wedge_report_in_place(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str) and "did not verify" in v:
                node[i] = _strip_unverified_hedge(v)
            else:
                _scrub_wedge_report_in_place(v)


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    """Best-effort registrable host (sans www) from a URL or bare domain."""
    if not url:
        return None
    candidate = url if "://" in url else f"https://{url}"
    try:
        netloc = (urlparse(candidate).netloc or "").lower()
    except ValueError:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _brand_name_from_domain(domain: Optional[str]) -> Optional[str]:
    """'bblab.shop' → 'Bblab'. Last-resort brand label when nothing else is
    available; the merchant can override via `brand`."""
    if not domain:
        return None
    label = domain.split(".")[0].replace("-", " ").strip()
    return label.title() or None


def _is_wedge_run_stale(requested_at: Any) -> bool:
    if not requested_at:
        return False
    try:
        requested = datetime.fromisoformat(str(requested_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=timezone.utc)
    age_s = (
        datetime.now(timezone.utc) - requested.astimezone(timezone.utc)
    ).total_seconds()
    return age_s > _WEDGE_RUN_STALE_TTL_S


def _wedge_product_has_attributes(product: Dict[str, Any]) -> bool:
    attrs = product.get("attributes_raw")
    if not isinstance(attrs, dict):
        return False
    return any(value not in (None, "", [], {}) for value in attrs.values())


def _select_wedge_hero_product(
    audit_products: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not audit_products:
        return None
    for idx, product in enumerate(audit_products):
        if _wedge_product_has_attributes(product):
            hero = dict(product)
            hero["_wedge_hero_index"] = idx
            return hero
    hero = dict(audit_products[0])
    hero["_wedge_hero_index"] = 0
    return hero


@router.post("/url-readiness")
async def run_merchant_url_audit(
    body: MerchantUrlAuditRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Free URL-audit wedge (Tier 1), merchant-CURATED + ASYNC. The merchant
    gives us their brand site + up to 5 product URLs (their own hero SKUs); we
    FETCH each for clean, real data and audit exactly those — NO catalog sync,
    NO auto-discovery.

    The grounded probes can run several MINUTES via the upstream (it serializes
    grounded calls), far past any client timeout. So this kicks the audit off
    in the BACKGROUND and returns a `run_id` immediately (status='running');
    the client polls GET /url-readiness/{run_id} until it's done. Request
    duration no longer bounds the audit.

    Free allowance: the first N URL audits per merchant run without a credit
    debit; beyond that returns 402. The cap (+ the per-URL fetch) is checked
    synchronously, before the run is recorded.
    """
    from services.bd_cold_start_service import fetch_curated_audit_product

    # 1. Free-allowance: the first N URL audits per merchant run free. Beyond
    #    that we do NOT hard-block a merchant who can pay — we METER the audit
    #    against their credit balance (the credit pre-flight + debit happens
    #    after products resolve, below). A credited merchant must not be locked
    #    out of a feature they can afford; the hard 402 only fires for a
    #    free-tier merchant with no credits to cover the run.
    used = await count_runs_for_merchant_by_subject(
        merchant_id=merchant_id, subject_type="merchant_url",
    )
    over_free = _FREE_URL_AUDITS_PER_MERCHANT > 0 and used >= _FREE_URL_AUDITS_PER_MERCHANT

    # 2. Fetch each merchant-provided product URL into a clean audit product.
    #    We audit exactly what the merchant chose — no discovery, no guessing.
    fetched = await asyncio.gather(
        *[fetch_curated_audit_product(u) for u in body.product_urls]
    )
    audit_products: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, str]] = []
    for raw_url, (product, reason) in zip(body.product_urls, fetched):
        if product:
            audit_products.append(product)
        else:
            unresolved.append(
                {"url": raw_url, "reason": reason or "could not resolve"}
            )
    if not audit_products:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "no_products_resolved",
                "message": (
                    "We couldn't read a product from any of the URLs you "
                    "provided. Make sure each link opens a single product page."
                ),
                "unresolved": unresolved,
            },
        )

    # 2b. Credit metering once the free allowance is used up. Price the wedge
    #     with the shared per-probe model (the background run fans Gemini
    #     grounded probes, _WEDGE_MAX_RUNS per RESOLVED product) so it bills
    #     identically to the audit cost path. Pre-flight the balance here so a
    #     short merchant gets a clear 402 BEFORE we record/launch the run; the
    #     actual debit happens once the run_id exists (idempotent on it).
    metered_credits = 0
    metered_cogs: Any = 0
    if over_free:
        from services.credit_consumption_service import estimate_probe_credits

        wedge_probe_count = len(audit_products) * max(1, _WEDGE_MAX_RUNS)
        metered_credits, metered_cogs = estimate_probe_credits(
            [("gemini", wedge_probe_count, True)]
        )
        balance = await get_balance(merchant_id)
        available = int(balance.get("credits") or 0)
        paid_tier = str(balance.get("plan_tier") or "free").lower() != "free"
        if metered_credits > available and not paid_tier:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "insufficient_credits",
                    "message": (
                        f"You've used your {_FREE_URL_AUDITS_PER_MERCHANT} free "
                        f"URL audits. This audit needs {metered_credits} credits "
                        f"and you have {available}. Top up credits, or connect "
                        "your store for the full per-SKU audit."
                    ),
                    "required": metered_credits,
                    "available": available,
                    "free_audits_allowed": _FREE_URL_AUDITS_PER_MERCHANT,
                    "free_audits_used": used,
                },
            )

    # 3. Resolve brand context: explicit brand/website, else onboarding, else
    #    derive from the fetched product vendors / the site domain.
    onboarding = await get_merchant_onboarding(merchant_id) or {}
    website = (body.website or onboarding.get("store_url") or "").strip() or None
    merchant_domain = _domain_from_url(website)
    if not merchant_domain and audit_products:
        merchant_domain = _domain_from_url(audit_products[0]["pdp_url"])
    merchant_name = (
        (body.brand or "").strip()
        or (onboarding.get("business_name") or "").strip()
        or next(
            (p["vendor"] for p in audit_products if p.get("vendor")), None
        )
        or _brand_name_from_domain(merchant_domain)
        or "your brand"
    )

    # Ensure each product carries a brand for the upstream's vendor-anchored
    # buyer-intent query. Prefer the fetched Shopify vendor; fall back to the
    # resolved merchant brand when absent. Title stays clean (no brand prefix -
    # the upstream prepends the vendor, so prefixing here double-brands).
    brand_for_vendor = (
        merchant_name if merchant_name and merchant_name != "your brand" else None
    )
    if brand_for_vendor:
        for p in audit_products:
            if not (p.get("vendor") or "").strip():
                p["vendor"] = brand_for_vendor

    # 4. Record the run; subject_type marks it for the free-allowance count.
    run_id = await record_audit_run_started(
        merchant_id=merchant_id,
        product_keys=[p["pdp_url"] for p in audit_products],
        subject_type="merchant_url",
    )
    if not run_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the audit (storage unavailable). Re-try.",
        )

    # 5. The result fields known immediately (no LLM yet). Stored with the
    #    report when the background run completes, and echoed in the 202 so the
    #    client can render "auditing N products…" right away.
    base_payload = {
        "audit_run_id": run_id,
        "audited_url": website,
        "tier": "url_wedge",
        "audited_products": [
            {
                "title": p["title"],
                "raw_title": p.get("raw_title"),
                "pdp_url": p["pdp_url"],
                "vendor": p.get("vendor"),
            }
            for p in audit_products
        ],
        "methodology": {
            "model": "merchant_curated",
            "products_audited": len(audit_products),
            "products_requested": len(body.product_urls),
            "queries_per_product": _WEDGE_MAX_RUNS,
            "what_we_checked": (
                "For each product URL you gave us, we ran AI shopping-agent "
                "(Gemini grounded search) buyer-intent queries and checked "
                "whether your own URL was cited in the answer."
            ),
            "limitations": [
                "This is a small free sample (a few queries per product), "
                "not an exhaustive measurement.",
                "We have not yet verified whether the third-party sources "
                "Gemini cited actually mention your brand — so a low score "
                "here means 'not found in this sample', not a definitive "
                "'invisible'.",
                "Connect your store for a deeper, verified, full-catalog "
                "audit with availability and serving data.",
            ],
            "unresolved_urls": unresolved,
        },
        "free_audits_allowed": (
            _FREE_URL_AUDITS_PER_MERCHANT
            if _FREE_URL_AUDITS_PER_MERCHANT > 0 else None
        ),
        # A credit-metered run doesn't consume a free slot.
        "free_audits_used": used if over_free else used + 1,
        "free_audits_remaining": (
            None if _FREE_URL_AUDITS_PER_MERCHANT <= 0
            else 0 if over_free
            else max(0, _FREE_URL_AUDITS_PER_MERCHANT - (used + 1))
        ),
        "billing_mode": "credits" if over_free else "free",
        "credits_charged": metered_credits if over_free else 0,
    }

    # 6a. Debit credits for a metered run (idempotent on run_id; the free path
    #     debits nothing). Done after the run is recorded so the debit shares
    #     the run id; if scheduling the work then fails, we refund (below).
    if over_free and metered_credits > 0:
        from services import credit_consumption_service as _ccs
        try:
            await _ccs.consume(
                merchant_id,
                "audit",
                idempotency_key=f"url_wedge:{run_id}",
                credits=metered_credits,
                usd_cogs=metered_cogs,
            )
        except Exception as exc:  # noqa: BLE001 - surface as payment error
            logger.warning(
                "url-wedge credit debit failed merchant_id=%s run_id=%s: %s",
                merchant_id, run_id, exc,
            )
            await record_audit_run_completed(
                run_id=run_id, status="failed",
                error_message="credit_debit_failed",
            )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "credit_debit_failed",
                    "message": "Could not debit credits for this audit. Re-try.",
                },
            ) from exc

    # 6b. Kick the audit off in the background; the client polls for the result.
    try:
        _schedule_wedge_audit(
            _run_wedge_audit_background(
                run_id=run_id,
                merchant_id=merchant_id,
                merchant_name=merchant_name,
                merchant_domain=merchant_domain,
                audit_products=audit_products,
                base_payload=base_payload,
            )
        )
    except Exception:
        # Couldn't even launch the work — refund the metered debit so the
        # merchant isn't charged for an audit that never ran.
        if over_free and metered_credits > 0:
            from services import credit_consumption_service as _ccs
            try:
                await _ccs.refund(
                    merchant_id, "audit", metered_credits,
                    source_event_id=f"url_wedge_refund:{run_id}",
                    usd_cogs=metered_cogs,
                )
            except Exception:  # noqa: BLE001 - best-effort refund
                logger.warning(
                    "url-wedge refund failed run_id=%s", run_id, exc_info=True,
                )
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message="schedule_failed",
        )
        raise

    return {"status": "running", "run_id": run_id, "brand_report": None, **base_payload}


@router.get("/url-readiness/{run_id}")
async def get_merchant_url_audit(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Poll a free URL-audit wedge run kicked off by POST /url-readiness.

    Returns `{status: 'running'}` until the background audit finishes, then the
    full result (`status: 'succeeded'` + brand_report + methodology + …) or
    `{status: 'failed', error}`. Scoped to the calling merchant + the wedge
    subject_type so it can't read another merchant's or a synced run.
    """
    row = await fetch_audit_run_by_id(run_id=run_id)
    if (
        not row
        or row.get("merchant_id") != merchant_id
        or row.get("subject_type") != "merchant_url"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "audit_run_not_found", "message": "No such audit run."},
        )
    run_status = row.get("status") or "running"
    if run_status == "succeeded":
        # report_jsonb holds the full result payload assembled by the runner.
        return sanitize_report_for_merchant(row.get("report_jsonb")) or {
            "status": "succeeded", "run_id": run_id, "brand_report": None,
        }
    if run_status == "failed":
        err = row.get("error_message") or ""
        msg = (
            "The audit pipeline returned fallback data; please re-run."
            if err == "upstream_mock_fallback"
            else (err or "Audit failed. Please re-run.")
        )
        return {"status": "failed", "run_id": run_id, "error": msg}
    if run_status == "running" and _is_wedge_run_stale(row.get("requested_at")):
        try:
            await record_audit_run_completed(
                run_id=run_id,
                status="failed",
                error_message="audit_timed_out_stale",
            )
        except Exception:  # noqa: BLE001 — best-effort stale cleanup
            logger.warning("Failed to mark stale wedge run failed: %s", run_id)
        return {
            "status": "failed",
            "run_id": run_id,
            "error": "This audit didn't finish in time — please re-run.",
        }
    return {"status": "running", "run_id": run_id}


# Keep references to in-flight background audits so the event loop doesn't GC
# them mid-run (asyncio only holds weak refs to bare tasks).
_WEDGE_BG_TASKS: set = set()


def _schedule_wedge_audit(coro) -> None:
    """Schedule a background wedge-audit coroutine on the running loop and hold
    a reference until it finishes. Indirection point so tests can stub the
    background run without patching the global asyncio.create_task."""
    task = asyncio.create_task(coro)
    _WEDGE_BG_TASKS.add(task)
    task.add_done_callback(_WEDGE_BG_TASKS.discard)


async def _run_wedge_audit_background(
    *,
    run_id: str,
    merchant_id: str,
    merchant_name: str,
    merchant_domain: Optional[str],
    audit_products: List[Dict[str, Any]],
    base_payload: Dict[str, Any],
) -> None:
    """Run the wedge brand report OFF the request path and persist the full,
    client-ready result into the run's report_jsonb. Never raises — failures
    are recorded as status='failed' so the poller surfaces them cleanly."""
    from services.audit_telemetry_context import audit_telemetry
    try:
        prior_runs = await recent_runs_for_merchant(
            merchant_id=merchant_id, limit=5,
        )
        prior_runs = [r for r in prior_runs if r.get("run_id") != run_id]
        async with audit_telemetry(run_id=run_id, merchant_id=merchant_id):
            brand_report = await run_brand_report(
                merchant_name=merchant_name,
                merchant_domain=merchant_domain,
                products=audit_products,
                coverage_profile="pilot_gemini",
                merchant_id=merchant_id,
                audit_run_id=run_id,
                prior_runs=prior_runs,
                # Bounded concurrency + parallel scan modes still help when the
                # upstream has spare throughput; async removes the hard ceiling.
                product_concurrency=min(len(audit_products), 3),
                parallel_scan_modes=True,
                max_runs=_WEDGE_MAX_RUNS,
            )
    except Exception as exc:  # noqa: BLE001 — runner must not crash the loop
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc)[:2000],
        )
        return

    # Mock-data guard (same protection as /ai-commerce-readiness).
    if _detect_mock_per_product(brand_report):
        await record_audit_run_completed(
            run_id=run_id, status="failed",
            error_message="upstream_mock_fallback",
        )
        return

    # Honesty post-processing (Phase A): strip the canned industry_context +
    # the buried "we did not verify…" hedge.
    _scrub_wedge_report_in_place(brand_report)

    try:
        hero_product = _select_wedge_hero_product(audit_products)
        sku_intelligence = await run_wedge_hero_sku_intelligence(
            hero_product=hero_product or {},
            merchant_id=merchant_id,
            run_id=run_id,
            # Hero SKU gets grounded ChatGPT as the second engine; the brand
            # report above stays pilot_gemini so ChatGPT cost is hero-only.
            coverage_profile="us_shopper",
            prompts_per_sku=14,
        )
    except Exception as exc:  # noqa: BLE001 — wedge SKU block must degrade
        sku_intelligence = {
            "is_empty": True,
            "error_note": str(exc)[:500],
        }

    apply_buyer_path_verdict_to_brand_report(brand_report, sku_intelligence)

    agg = brand_report.get("aggregate") or {}
    verdict_labels = [
        (p.get("verdict") or {}).get("label") or ""
        for p in (brand_report.get("per_product") or [])
    ]
    full_payload = {
        "status": "succeeded",
        "run_id": run_id,
        "brand_report": brand_report,
        "sku_intelligence": sku_intelligence,
        **base_payload,
    }
    # Layer 1 output-quality gate: fail loud on statable contradictions (e.g. the
    # merchant's own host as a controller) — log + degrade the affected surface to
    # an honest fallback before the report is ever persisted/shown. Never raises.
    try:
        from services.audit_invariants import enforce_audit_invariants
        enforce_audit_invariants(full_payload, run_id=run_id, merchant_id=merchant_id)
        # Layer 2 (default-off): an LLM review gate over the prose surfaces that
        # passed Layer 1; flags-or-fails withhold the surface (the deterministic
        # next_best_action remains). No key / flag off → no-op.
        from services.audit_review_gate import apply_audit_review_gate
        await apply_audit_review_gate(full_payload, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the gate must not crash the audit runner
        logger.warning("audit invariant gate skipped: %s", exc)
    await record_audit_run_completed(
        run_id=run_id, status="succeeded",
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=agg.get("avg_visibility"),
        attribution_score_avg=agg.get("avg_attribution"),
        category_visibility_score_avg=agg.get("avg_category_visibility"),
        report_jsonb=full_payload,
    )


@router.post("/configure-apm")
async def configure_merchant_apm(
    body: ApmConfigureRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Persist the merchant's self-service APM opt-in settings."""
    try:
        config = await upsert_apm_config(
            merchant_id=merchant_id,
            enabled=body.enabled,
            cadence_days=body.cadence_days,
            scope=body.scope,
        )
    except ApmConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc

    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="merchant not found",
        )
    return config


@router.get("/apm-config")
async def read_merchant_apm_config(
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Return the merchant's current APM settings."""
    config = await get_apm_config(merchant_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="APM config not found",
        )
    return config


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


@router.get("/outcomes")
async def get_merchant_outcomes_endpoint(
    window: str = "all_time",
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """This merchant's transaction OUTCOMES — what actually happened after agents routed
    buyers here: orders that completed payment, the refund rate (only once enough orders
    exist to be meaningful), and attributed GMV. The proprietary trust signal.

    Honest by construction: `refund_rate` is null and `min_sample_met` is false until the
    merchant has at least the minimum number of transacted orders — we never imply a return
    rate from a handful of sales. Counts and GMV are always real.

    `window`: 'all_time' (default) or 'trailing_90d'.
    """
    if window not in ("all_time", "trailing_90d"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window must be 'all_time' or 'trailing_90d'",
        )
    from services.outcome_aggregation_service import get_merchant_outcomes

    row = await get_merchant_outcomes(merchant_id, window_key=window)
    if not row:
        # No transacted orders yet — return an explicit zero-state, not an error.
        return {
            "merchant_id": merchant_id,
            "window": window,
            "has_outcomes": False,
            "outcomes": {
                "transacted_count": 0, "paid_count": 0, "refunded_count": 0,
                "refund_rate": None, "gmv_cents": 0, "min_sample_met": False,
            },
        }
    return {"merchant_id": merchant_id, "window": window, "has_outcomes": True, "outcomes": row}


@router.get("/funnel")
async def get_merchant_funnel(
    channel: Optional[str] = None,
    window_days: int = 30,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """PR-5: stage-level conversion funnel for this merchant.

    Query params:
      - `channel`: filter to one source_channel (e.g. 'ai_agent').
        Omit to count across all channels.
      - `window_days`: trailing window for the rollup. Default 30.
        Capped at 365.

    Response shape:
      {
        "merchant_id": str,
        "source_channel": str | null,
        "window_days": int,
        "total_events": int,
        "stages": [
          {"stage": "impression", "count": 1234, "conversion_to_next": 0.30, "drop_off_pct": 0.70},
          ...
        ],
        "channel_breakdown": [
          {"source_channel": "ai_agent", "total_events": 1000},
          ...
        ]
      }

    `channel_breakdown` lets the operator see which channels are
    active before drilling into one. When the response's `stages` are
    all zero counts, the merchant has no tracked events in this
    window — likely PR-5 just shipped and the funnel is still
    populating.
    """
    if window_days <= 0 or window_days > 365:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_days must be between 1 and 365",
        )

    from services.funnel_analytics import (
        channel_breakdown as _channel_breakdown,
        compute_funnel,
    )
    funnel = await compute_funnel(
        merchant_id=merchant_id,
        source_channel=channel,
        window_days=window_days,
    )
    breakdown = await _channel_breakdown(
        merchant_id=merchant_id,
        window_days=window_days,
    )
    funnel["channel_breakdown"] = breakdown
    return funnel


@router.get("/serving-status")
async def get_serving_status(
    sku_keys: Optional[str] = None,
    limit: int = 200,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Per-SKU AI-agent visibility (serving-eligibility) for this merchant.

    The merchant's catalog can be fully synced yet 0% discoverable by shopping
    agents (blocked at no_image / low_quality / etc.); the audit alone doesn't
    say so. This endpoint makes that visible — and, when a product is blocked,
    gives a PLAIN-ENGLISH reason (never the internal blocker_code / metric names).

    Query params:
      - `sku_keys`: optional comma-separated `platform:source_product_id`
        composites (or bare product_keys). When given, returns status for those
        SKUs (unknown refs are reported, not 404'd). When omitted, returns a
        blocked-and-pending-first page of the catalog (capped by `limit`).
      - `limit`: cap on the returned list when `sku_keys` is omitted (max 1000).

    The `summary` is always computed over the merchant's FULL catalog, so the
    portal can render "X of Y SKUs agent-visible" regardless of the list slice.

    Response:
      {
        "merchant_id": str,
        "summary": {"total": int, "eligible": int, "blocked": int, "pending": int},
        "skus": [
          {"sku_key", "product_key", "title", "agent_visible": bool,
           "serving_eligible": bool|null, "blocker_code": str|null,
           "blocker_reason": str|null}, ...
        ]
      }
    """
    from services.serving_status_service import (
        get_serving_summary,
        list_serving_status,
        serving_status_for_sku_keys,
    )

    capped_limit = max(1, min(int(limit or 200), 1000))
    summary = await get_serving_summary(merchant_id)
    if sku_keys:
        # Bound per-request work: a merchant could pass thousands of comma-
        # separated refs. Cap to the same ceiling as the catalog-wide list.
        refs = [s for s in sku_keys.split(",") if s.strip()][:1000]
        skus = await serving_status_for_sku_keys(merchant_id, refs)
    else:
        skus = await list_serving_status(merchant_id, limit=capped_limit)
    return {"merchant_id": merchant_id, "summary": summary, "skus": skus}


# ---------------------------------------------------------------------------
# PR-6: human task queue endpoints
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_merchant_tasks(
    status_filter: Optional[str] = None,
    limit: int = 50,
    parent_audit_run_id: Optional[str] = None,
    include_history: bool = False,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """List tasks for this merchant, newest first.

    Query params:
      - `status_filter`: comma-separated list (e.g. 'pending,in_progress')
        — defaults to open work (pending + in_progress). Pass 'done,dismissed'
        for archive view; pass 'all' for everything.
      - `limit`: 1-200, default 50.
      - `parent_audit_run_id`: scope to one audit run.
      - `include_history`: true preserves the pre-PR flat all-runs list.
    """
    if limit <= 0 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 200",
        )
    from db.merchant_tasks import list_tasks_for_merchant

    if status_filter is None:
        statuses = None  # default in accessor: pending + in_progress
    elif status_filter.strip().lower() == "all":
        statuses = []  # empty list = no filter (all statuses)
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
    elif include_history:
        tasks_scope = "history"
        scoped_parent_audit_run_id = None
    else:
        tasks_scope = "latest_completed"
        latest_audit_run_id = await _latest_completed_audit_run_id_for_tasks(
            merchant_id
        )
        if latest_audit_run_id is None:
            return {
                "merchant_id": merchant_id,
                "count": 0,
                "latest_audit_run_id": None,
                "tasks_scope": tasks_scope,
                "tasks": [],
            }
        scoped_parent_audit_run_id = latest_audit_run_id

    tasks = await list_tasks_for_merchant(
        merchant_id=merchant_id,
        status_filter=statuses,
        limit=limit,
        parent_audit_run_id=scoped_parent_audit_run_id,
    )
    return {
        "merchant_id": merchant_id,
        "count": len(tasks),
        "latest_audit_run_id": latest_audit_run_id,
        "tasks_scope": tasks_scope,
        "tasks": tasks,
    }


class _TaskStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(pending|in_progress|done|failed)$")
    assigned_to_human: Optional[str] = Field(None, max_length=200)
    evidence: Optional[Dict[str, Any]] = None


@router.patch("/tasks/{task_id}")
async def update_merchant_task(
    task_id: str,
    body: _TaskStatusUpdate,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Update a task's status. Operator can mark it in_progress (taking
    ownership), done (completed work), or failed (couldn't complete).
    For dismissal use POST /tasks/{id}/dismiss instead — that requires
    a reason for the audit trail.

    Cross-merchant access guard: fetches the task first, 404s if it
    doesn't belong to the auth'd merchant.
    """
    from db.merchant_tasks import fetch_task, update_task_status

    task = await fetch_task(task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("merchant_id") != merchant_id:
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


class _TaskDismissBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.post("/tasks/{task_id}/dismiss")
async def dismiss_merchant_task(
    task_id: str,
    body: _TaskDismissBody,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Dismiss a task with an operator-supplied reason. Stored in
    dismissed_reason for audit trail. Terminal (can't be undone via
    update_task_status — would need a fresh task)."""
    from db.merchant_tasks import dismiss_task, fetch_task

    task = await fetch_task(task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=404, detail="task not found")

    ok = await dismiss_task(task_id=task_id, reason=body.reason)
    if not ok:
        raise HTTPException(
            status_code=500, detail="task dismiss failed (DB error)",
        )
    updated = await fetch_task(task_id=task_id)
    return {"task": updated}


@router.get("/executor-runs")
async def list_merchant_executor_runs(
    agent_name: Optional[str] = None,
    limit: int = 20,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """List executor agent runs for this merchant, newest first.
    Powers the "what Pivota did for you this week" activity feed
    (PR-4 executor agents).

    Query params:
      - `agent_name`: filter to one agent (e.g. 'gsc_url_submission_loop',
        'sitemap_freshness_monitor', 'content_brief_generator'). Omit
        to see all agents.
      - `limit`: 1-100, default 20.
    """
    if limit <= 0 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100",
        )
    from db.executor_runs import (
        recent_runs_for_agent,
        recent_runs_for_merchant as recent_executor_runs_for_merchant,
    )
    if agent_name:
        # Filter at SQL by agent_name; no per-merchant filter, so we
        # filter in Python afterward (small N — 50-100 entries max).
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
