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
    count_runs_in_window,
    record_audit_run_completed,
    record_audit_run_started,
    recent_runs_for_merchant,
)
from db.merchant_onboarding import get_merchant_onboarding
from services.agent_center_bd_report_service import run_brand_report
from services.catalog_identity import make_content_key
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
            "rate_limit_remaining": None,
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
        "brand_report": row.get("report_jsonb"),
        # Rate-limit headroom isn't tracked through the new pipeline;
        # callers using ?via=async_pipeline don't get this field.
        "rate_limit_remaining": None,
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

    if via == "async_pipeline":
        return await _run_async_pipeline_compat(
            body=body, merchant_id=merchant_id, response=response,
        )

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
    try:
        brand_report = await run_brand_report(
            merchant_name=str(merchant_name),
            merchant_domain=merchant_domain,
            products=products,
            provider="gemini",
            max_runs=body.max_runs,
            prior_runs=prior_runs,
            integration_state=integration_state,
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
                "refusing to render merchant-facing prose. Check that "
                "PIVOTA_AGENT_INTERNAL_API_KEY and the upstream's GEMINI_API_KEY "
                "are configured. Re-run the audit once the upstream is real."
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
        "brand_report": brand_report,
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


# ---------------------------------------------------------------------------
# PR-6: human task queue endpoints
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_merchant_tasks(
    status_filter: Optional[str] = None,
    limit: int = 50,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """List tasks for this merchant, newest first.

    Query params:
      - `status_filter`: comma-separated list (e.g. 'pending,in_progress')
        — defaults to open work (pending + in_progress). Pass 'done,dismissed'
        for archive view; pass 'all' for everything.
      - `limit`: 1-200, default 50.
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

    tasks = await list_tasks_for_merchant(
        merchant_id=merchant_id,
        status_filter=statuses,
        limit=limit,
    )
    return {
        "merchant_id": merchant_id,
        "count": len(tasks),
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
