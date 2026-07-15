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
import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
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
    enqueue_audit_run_with_replay,
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
from services.agent_readiness_score import compute_agent_readiness_score
from services.audit_index_intake import (
    resolve_seed_vendor,
    upsert_audited_sku_to_index,
)
from services.catalog_identity import make_content_key
from services.catalog_sync_service import make_pivota_canonical_fields
from services.merchant_audit_readiness import assess_merchant_audit_readiness
from services.credit_consumption_service import (
    consume as consume_credits,
    estimate_probe_credits,
    merchant_is_paid_tier,
    refund as refund_credits,
)
from services.merchant_credit_balance_service import (
    InsufficientCreditsError,
    get_balance,
)
from services.merchant_narrative_builder import (
    annotate_outreach_moves_with_pitch_paths,
)
from services.report_summary_builder import build_report_summary
from services.provider_credit_rates import credits_for_tokens
from services.report_deck_builder import (
    DECK_LLM_PROVIDER,
    DECK_TOKEN_PRICE_MULTIPLE,
    build_report_deck,
    generate_executive_summary,
)
from services.llm_providers.deepseek_probe import (
    DeepseekProbeError,
    answer_grounded_question,
)
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
# Tiered product cap (founder decision 2026-07-15: 5 was too small — a >5-SKU
# merchant rotated product sets between runs, so the weekly re-audit compared
# different sets and week-over-week reads were ambiguous). Free keeps the
# original COGS guard; paid fits the whole tracked portfolio in ONE set.
# Engine-side _BRAND_REPORT_MAX_PRODUCTS=50 stays the hard ceiling.
_WEDGE_MAX_PRODUCTS_FREE = int(_os.getenv("WEDGE_MAX_PRODUCTS_FREE", "5"))
_WEDGE_MAX_PRODUCTS_PAID = int(_os.getenv("WEDGE_MAX_PRODUCTS_PAID", "20"))
# Per-SKU merchant prompts (custom_prompts_by_url): capped per product AND
# across the whole set so a 20-product paid run can't fan 20×N extra grounded
# probes per provider. Each one is billed like any probed prompt (counted into
# per_provider_probes below) and pinned into its SKU's basis on the next run.
_WEDGE_CUSTOM_PROMPTS_PER_SKU = int(
    _os.getenv("WEDGE_CUSTOM_PROMPTS_PER_SKU", "2")
)
_WEDGE_CUSTOM_PROMPTS_TOTAL = int(
    _os.getenv("WEDGE_CUSTOM_PROMPTS_TOTAL", "10")
)

# Per-product (per-SKU) URL audit: each pasted URL is audited as its own SKU
# through the durable per-SKU pipeline. prompts_per_sku at 18 (vs the readiness
# default of 40) crosses the sidewalk-lane budget threshold (>=14) so the
# attribute-driven niche/longtail lanes — "where you can win" — actually run
# instead of being budgeted out at 8. Verify (DeepSeek, ~25% sample) adds the
# answer-quality signal (which cited answers actually hold up) that the
# readiness audit has. Still Gemini-only to bound cost; all tunable via env.
# Trimmed 18 -> 14: with LLM value-prop discovery prompts now filling the set,
# 18 padded runs with low-value generic filler and pushed runtime past the point
# where the upstream drops grounding on later SKUs (the "0/N appears nowhere"
# false negative). Fewer, higher-value queries = shorter runs that keep
# grounding. Env-overridable.
_WEDGE_PROMPTS_PER_SKU = int(_os.getenv("WEDGE_PROMPTS_PER_SKU", "14"))
_WEDGE_COVERAGE_PROFILE = _os.getenv("WEDGE_COVERAGE_PROFILE", "pilot_gemini")
_WEDGE_PROVIDERS = [
    p.strip()
    for p in _os.getenv("WEDGE_PROVIDERS", "gemini").split(",")
    if p.strip()
]
_WEDGE_VERIFY_PROVIDERS = [
    p.strip()
    for p in _os.getenv("WEDGE_VERIFY_PROVIDERS", "deepseek").split(",")
    if p.strip()
]
# Providers added for PAID-tier merchants only (cross-model divergence). The
# free wedge stays on _WEDGE_PROVIDERS (Gemini) to bound the absorbed cost.
# ChatGPT isn't plan-gated (ADR-005) — this is a cost choice, env-tunable.
_WEDGE_PAID_PROVIDERS = [
    p.strip()
    for p in _os.getenv("WEDGE_PAID_PROVIDERS", "chatgpt").split(",")
    if p.strip()
]


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
        max_length=20,
        description=(
            "Product page URLs to audit — up to 5 on the free plan, up to "
            "20 on paid plans (tier enforced in the handler; the schema cap "
            "is the paid ceiling). Each URL is audited as its own "
            "per-product report."
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
    custom_prompts: Optional[List[str]] = Field(
        default=None,
        max_length=10,
        description=(
            "Up to 10 buyer prompts the merchant wants to test (one per "
            "entry). Probed once (brand-level, like the readiness audit) and "
            "surfaced as 'Your prompts' — whether AI cited you, the sources it "
            "grounded in, and which competitors it named instead."
        ),
    )
    custom_prompts_by_url: Optional[Dict[str, List[str]]] = Field(
        default=None,
        description=(
            "Merchant prompts attached to a SPECIFIC product: keyed by the "
            "exact URL from product_urls (2 per product, 10 total by "
            "default; env-tunable). Unlike the brand-level custom_prompts "
            "panel, these are probed INSIDE that product's audit context, so "
            "the results join its per-prompt table / win plan / evidence "
            "chain, and they're pinned into that product's measurement basis "
            "— your niche gets tracked week over week on re-runs."
        ),
    )
    refresh: bool = Field(
        default=False,
        description=(
            "Regenerate the prompt basis instead of reusing (pinning) the "
            "prior run's frozen query set. Default False keeps re-audit scores "
            "comparable (W2 pinning); set True when a re-audit must reflect "
            "product/evidence changes in the probed queries — e.g. after new "
            "grounded attributes become available. Costs the basis-generation "
            "LLM calls the pinned path skips."
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


def _synthetic_url_sku_key(merchant_id: str, pdp_url: str) -> str:
    """Deterministic synthetic SKU/product key for a pasted URL (no catalog).

    Namespaced `urlwedge:` so it can never collide with a real catalog key, and
    deterministic on (merchant_id, pdp_url) so re-auditing the same URL reuses
    the key — which is what lets W2 prompt-basis pinning (and Tier-1 retailer-
    evidence recycling) find the prior run's basis and keep re-audit scores
    comparable. The prefix is shared with services/prompt_basis, which routes a
    SKU's prior-basis scan to merchant_url runs by this namespace. (The per-run
    probe rows are still namespaced by audit_run_id, so no cross-run bleed.)"""
    from services.prompt_basis import URL_WEDGE_SKU_PREFIX

    digest = hashlib.sha1(
        f"{merchant_id}|{(pdp_url or '').strip().lower()}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{URL_WEDGE_SKU_PREFIX}{digest}"


# Merchant-facing model display names for the honest methodology rewrite.
_PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "gemini": "Gemini",
    "chatgpt": "ChatGPT",
    "openai": "ChatGPT",
    "deepseek": "DeepSeek",
    "claude": "Claude",
}


def _humanize_provider_list(providers: List[str]) -> str:
    """"gemini","chatgpt" -> "Gemini and ChatGPT" (Oxford-free, ≤3 reads clean)."""
    names = [
        _PROVIDER_DISPLAY_NAMES.get(p, p.title()) for p in providers
    ]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


# Natural reading order for the header (primary engine first), unknown providers
# tacked on alphabetically. Keeps the label from reading in raw alphabetical id
# order ("ChatGPT + Gemini") when Gemini is the base engine.
_PROVIDER_DISPLAY_ORDER = ["gemini", "chatgpt", "deepseek", "claude"]


def _ordered_for_display(providers: List[str]) -> List[str]:
    return sorted(
        providers,
        key=lambda p: (
            _PROVIDER_DISPLAY_ORDER.index(p)
            if p in _PROVIDER_DISPLAY_ORDER
            else len(_PROVIDER_DISPLAY_ORDER),
            p,
        ),
    )


def _grounded_search_label(providers: List[str]) -> str:
    """The header parenthetical, owned by the backend so the frontend renders it
    verbatim (no client-side provider-name mapping / fallback):
    ["chatgpt","gemini"] -> "Gemini + ChatGPT grounded search". Empty list ->
    "" so the caller can omit the parenthetical rather than fabricate one."""
    names = [
        _PROVIDER_DISPLAY_NAMES.get(p, p.title())
        for p in _ordered_for_display(providers)
    ]
    if not names:
        return ""
    return f"{' + '.join(names)} grounded search"


def _provider_run_summary(
    prompts_by_provider: Dict[str, int], order: List[str]
) -> str:
    """Display-ready per-model run counts, e.g. "Gemini 7 · ChatGPT 10", in
    natural engine order. Empty when nothing ran."""
    return " · ".join(
        f"{_PROVIDER_DISPLAY_NAMES.get(p, p.title())} "
        f"{int(prompts_by_provider.get(p) or 0)}"
        for p in _ordered_for_display(order)
    )


def _measured_probe_coverage(
    per_sku_reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Derive HONEST run coverage from the completed per-SKU results, so the
    methodology header reflects what providers actually ran — not the static
    `_WEDGE_PROMPTS_PER_SKU` budget claimed before any probe fired.

    Reads each SKU's `citation_by_provider[*].prompts` (the real per-provider
    run count, failed/coverage-unavailable providers excluded). Returns:
      - providers_ran:      sorted provider ids that produced ≥1 scored run
      - prompts_by_provider {provider: total prompts across audited SKUs}
      - queries_per_product the honest per-product query count = the fullest
                            single-provider coverage a product received (all
                            providers share the generated query set, so the max
                            is the distinct set actually probed; a provider that
                            dropped runs to timeouts sits below it).
    Empty dict when nothing measurable ran (caller keeps the planned budget).
    """
    prompts_by_provider: Dict[str, int] = {}
    per_sku_max: List[int] = []
    for report in per_sku_reports or []:
        cbp = (report or {}).get("citation_by_provider") or {}
        sku_max = 0
        for provider, entry in cbp.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "probe_failed" or entry.get(
                "coverage_unavailable"
            ):
                continue
            n = int(entry.get("prompts") or 0)
            if n <= 0:
                continue
            prompts_by_provider[provider] = (
                prompts_by_provider.get(provider, 0) + n
            )
            sku_max = max(sku_max, n)
        if sku_max:
            per_sku_max.append(sku_max)
    if not prompts_by_provider:
        return {}
    return {
        "providers_ran": sorted(prompts_by_provider),
        "prompts_by_provider": prompts_by_provider,
        "queries_per_product": max(per_sku_max) if per_sku_max else 0,
    }


def _providers_attempted(per_sku_reports: List[Dict[str, Any]]) -> bool:
    """True when at least one SKU carries a per-provider citation entry (scored
    OR failed). Lets the reshape distinguish "providers ran and all failed"
    (report coverage-unavailable) from "no per-provider signal at all" (legacy
    report shape — leave the planned methodology untouched)."""
    for report in per_sku_reports or []:
        cbp = (report or {}).get("citation_by_provider") or {}
        if any(isinstance(entry, dict) for entry in cbp.values()):
            return True
    return False


def _with_pitch_paths(where_losing: Dict[str, Any]) -> Dict[str, Any]:
    """Serve-time registry annotation for the get-cited section (wave-1 C1)."""
    if isinstance(where_losing, dict):
        annotate_outreach_moves_with_pitch_paths(where_losing.get("outreach_moves"))
    return where_losing


def _shape_url_audit_response(row: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a completed per_sku run row into the URL-audit response envelope.

    report_jsonb is the per_sku brand_report (per_sku_reports + brand_rollup +
    authority_map). The base payload (audited_products, methodology,
    free_audits_*, billing) was echoed into launch.wedge_base_payload at
    enqueue. Catalog-only dimensions are flagged unavailable so the client
    renders the connect-store funnel rather than misleading low scores.

    The persisted methodology carries the PLANNED query budget
    (`_WEDGE_PROMPTS_PER_SKU`); before returning we overwrite it with the
    MEASURED coverage read back from the per-SKU results, so the header reports
    what actually ran (real query count + real model names) instead of a static
    promise. The planned budget is preserved as `queries_per_product_target`.
    """
    run_id = row.get("run_id")
    report = row.get("report_jsonb")
    report = report if isinstance(report, dict) else {}
    partial = row.get("partial_result_jsonb")
    launch = (partial.get("launch") or {}) if isinstance(partial, dict) else {}
    base = launch.get("wedge_base_payload") or {}
    brand_rollup = report.get("brand_rollup") or {}
    out: Dict[str, Any] = {
        "status": "succeeded",
        "run_id": run_id,
        "audit_run_id": run_id,
        "tier": "url_per_sku",
        "per_sku_reports": report.get("per_sku_reports") or [],
        "brand_rollup": brand_rollup,
        "authority_map": report.get("authority_map") or {},
        "where_you_can_win": (
            brand_rollup.get("where_you_can_win")
            or report.get("where_you_can_win")
        ),
        "suggested_prompts": report.get("suggested_prompts"),
        # Merchant's own test prompts ("Your prompts"), probed once brand-level.
        "custom_prompts": report.get("custom_prompts") or [],
        # Competitive landscape + off-platform outreach moves (who AI cites
        # instead of you, and the pitch/engage/list actions to get cited there).
        # Outreach moves get their registry pitch paths stamped at SERVE time
        # (wave-1 C1) — historical runs pick up newly curated recipients /
        # submission URLs without a re-run.
        "where_youre_losing": _with_pitch_paths(
            (report.get("merchant_narrative") or {}).get("where_youre_losing") or {}
        ),
        # The full merchant-grade narrative the pipeline already computes:
        # headline story, what's working, where you're losing, prioritized
        # actions, and the answer-quality (verify) summary. This is the insight
        # layer that turns the per-product scores into a decision.
        "merchant_narrative": report.get("merchant_narrative") or None,
        "brand_report": report,
        "catalog_dimensions_available": False,
    }
    if isinstance(base, dict):
        for key, value in base.items():
            out.setdefault(key, value)

    # Report Summary Contract v1 (dark, additive) — the condensed layer the
    # 3-page portal view / PPT export / homepage hero will consume. No
    # renderer reads it yet; a build failure must never sink the response.
    try:
        # URL wedge = no connected catalog -> routability signals (serving
        # eligibility / orderability) are unmeasurable; exclude them from the
        # displayed weakest-link score (calibration decision a — same reason
        # this envelope stamps catalog_dimensions_available=False above).
        out["report_summary"] = build_report_summary(
            report, unmeasured_dimensions=("routability",)
        )
    except Exception:  # noqa: BLE001
        logger.warning("report_summary build failed", exc_info=True)
        out["report_summary"] = None

    # Honest coverage: overwrite the static planned budget in the persisted
    # methodology with what the providers ACTUALLY ran (read from the per-SKU
    # results). Keeps the header's "N buyer-intent queries (X grounded search)"
    # in sync with the per-model counts shown in the body.
    per_sku = out.get("per_sku_reports") or []
    measured = _measured_probe_coverage(per_sku)
    if measured:
        methodology = dict(out.get("methodology") or {})
        planned = methodology.get("queries_per_product")
        if planned is not None:
            methodology["queries_per_product_target"] = planned
        methodology["queries_per_product"] = measured["queries_per_product"]
        methodology["providers_ran"] = measured["providers_ran"]
        methodology["prompts_by_provider"] = measured["prompts_by_provider"]
        # Display-ready strings the header renders verbatim — the backend is the
        # single source of truth for model naming, so the frontend never
        # re-derives a label or falls back to a generic one.
        methodology["grounded_search_label"] = _grounded_search_label(
            measured["providers_ran"]
        )
        methodology["provider_run_summary"] = _provider_run_summary(
            measured["prompts_by_provider"], measured["providers_ran"]
        )
        n = measured["queries_per_product"]
        provider_label = _humanize_provider_list(measured["providers_ran"])
        methodology["what_we_checked"] = (
            "Each product URL you gave us is audited on its own: we ran "
            f"{n} AI shopping-agent buyer-intent quer{'y' if n == 1 else 'ies'} "
            f"per product on {provider_label} (grounded search) and checked "
            "whether your URL is cited, which competitors are cited instead, "
            "and on which sources."
        )
        out["methodology"] = methodology
    elif _providers_attempted(per_sku):
        # A "succeeded" run where every provider came back failed / coverage-
        # unavailable. Don't let the static planned methodology assert a
        # fabricated "14 (Gemini)" — report coverage as unavailable so the
        # header reflects that nothing scored. (When there's NO per-provider
        # signal at all — a legacy report shape — we leave the planned
        # methodology untouched rather than wrongly zeroing a real run.)
        methodology = dict(out.get("methodology") or {})
        planned = methodology.get("queries_per_product")
        if planned is not None:
            methodology["queries_per_product_target"] = planned
        methodology["queries_per_product"] = 0
        methodology["providers_ran"] = []
        methodology["prompts_by_provider"] = {}
        # Clear the planned label the base_payload set — nothing scored, so a
        # "Gemini + ChatGPT grounded search" string here would contradict the
        # "none returned a result" body. Empty → the frontend omits it.
        methodology["grounded_search_label"] = ""
        methodology["provider_run_summary"] = ""
        methodology["coverage_unavailable"] = True
        methodology["what_we_checked"] = (
            "We attempted grounded buyer-intent queries on the models you "
            "selected, but none returned a scored result on this run (the "
            "providers errored or were rate-limited). Re-run to measure "
            "coverage."
        )
        out["methodology"] = methodology
    return out


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
    gives us their brand site + product URLs (5 free / 20 paid tiers); we
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
    from services.idempotency import compute_audit_idempotency_key

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

    # 1b. Tiered product cap — checked BEFORE the per-URL network fetch
    #     (review A1: gating after the fetch spent 6-20 outbound requests on a
    #     request we were about to 422). Balance resolved once here and reused
    #     for provider selection + metering below.
    balance = await get_balance(merchant_id)
    paid_tier = str(balance.get("plan_tier") or "free").lower() != "free"
    tier_cap = _WEDGE_MAX_PRODUCTS_PAID if paid_tier else _WEDGE_MAX_PRODUCTS_FREE
    if len(body.product_urls) > tier_cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "product_cap_exceeded",
                "message": (
                    f"Your plan audits up to {tier_cap} products per run "
                    f"(you sent {len(body.product_urls)}). "
                    + (
                        "Upgrade to track up to "
                        f"{_WEDGE_MAX_PRODUCTS_PAID} products in one "
                        "comparable weekly set."
                        if not paid_tier
                        else "Split the set or contact us to raise the limit."
                    )
                ),
                "cap": tier_cap,
                "paid_cap": _WEDGE_MAX_PRODUCTS_PAID,
                "upgrade_path": None if paid_tier else "/dashboard/billing",
            },
        )

    # 1c. Per-SKU merchant prompts: validate + clean BEFORE the per-URL network
    #     fetch (same review-A1 rule as the tier cap — no outbound spend on a
    #     request we're about to 422). Keys must be URLs from this request;
    #     caps are explicit 422s, not silent truncation, so the merchant never
    #     pays for prompts that quietly didn't run.
    submitted_urls = set(body.product_urls)
    custom_by_url_clean: Dict[str, List[str]] = {}
    for _url, _prompts in (body.custom_prompts_by_url or {}).items():
        if _url not in submitted_urls:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "custom_prompts_url_mismatch",
                    "message": (
                        "custom_prompts_by_url keys must be URLs from "
                        f"product_urls (got a key not in the set: {_url})."
                    ),
                },
            )
        _seen_pu: set = set()
        _clean_pu: List[str] = []
        for _p in _prompts or []:
            _t = str(_p or "").strip()
            if _t and _t.lower() not in _seen_pu:
                _seen_pu.add(_t.lower())
                _clean_pu.append(_t)
        if len(_clean_pu) > _WEDGE_CUSTOM_PROMPTS_PER_SKU:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "custom_prompts_per_product_cap",
                    "message": (
                        f"Up to {_WEDGE_CUSTOM_PROMPTS_PER_SKU} prompts per "
                        f"product (got {len(_clean_pu)} for {_url})."
                    ),
                    "cap_per_product": _WEDGE_CUSTOM_PROMPTS_PER_SKU,
                },
            )
        if _clean_pu:
            custom_by_url_clean[_url] = _clean_pu
    total_sku_customs = sum(len(v) for v in custom_by_url_clean.values())
    if total_sku_customs > _WEDGE_CUSTOM_PROMPTS_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "custom_prompts_total_cap",
                "message": (
                    f"Up to {_WEDGE_CUSTOM_PROMPTS_TOTAL} per-product prompts "
                    f"across the set (got {total_sku_customs})."
                ),
                "cap_total": _WEDGE_CUSTOM_PROMPTS_TOTAL,
            },
        )

    # 2. Fetch each merchant-provided product URL into a clean audit product.
    #    We audit exactly what the merchant chose — no discovery, no guessing.
    fetched = await asyncio.gather(
        *[fetch_curated_audit_product(u) for u in body.product_urls]
    )
    audit_products: List[Dict[str, Any]] = []
    # The submitted URL each resolved product came from (aligned with
    # audit_products) — the fetch may normalize pdp_url, but the merchant's
    # custom_prompts_by_url keys are the URLs they SUBMITTED.
    requested_urls: List[str] = []
    unresolved: List[Dict[str, str]] = []
    for raw_url, (product, reason) in zip(body.product_urls, fetched):
        if product:
            audit_products.append(product)
            requested_urls.append(raw_url)
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

    # Per-SKU merchant prompts re-keyed from the submitted URL to the minted
    # synthetic sku_key — the shape the probe fan-out consumes. Built from
    # RESOLVED products only, so prompts on an unresolved URL are neither
    # probed nor billed (the URL itself is reported in `unresolved`). Two
    # submitted URLs minting the same key (duplicate product) merge with a
    # case-insensitive dedupe so billing can't exceed what actually runs.
    custom_prompts_by_sku: Dict[str, List[str]] = {}
    for p, requested_url in zip(audit_products, requested_urls):
        sku_customs = custom_by_url_clean.get(requested_url) or []
        if not sku_customs:
            continue
        key = _synthetic_url_sku_key(merchant_id, p["pdp_url"])
        merged = custom_prompts_by_sku.setdefault(key, [])
        seen_merged = {m.lower() for m in merged}
        for text in sku_customs:
            if text.lower() not in seen_merged:
                seen_merged.add(text.lower())
                merged.append(text)

    # NOTE: the commerce-index auto-seed runs in the
    # BACKGROUND task (_run_wedge_audit_background), not here, so it adds no
    # latency to this request — each seed is a catalog upsert + agent_pdp_view
    # refresh and they must not block the 202.

    # 2b. Credit metering once the free allowance is used up. Price the wedge
    #     with the shared per-probe model (the background run fans Gemini
    #     grounded probes, _WEDGE_MAX_RUNS per RESOLVED product) so it bills
    #     identically to the audit cost path. Pre-flight the balance here so a
    #     short merchant gets a clear 402 BEFORE we record/launch the run; the
    #     actual debit happens once the run_id exists (idempotent on it).
    metered_credits = 0
    metered_cogs: Any = 0
    # Custom prompts (max 10) are probed ONCE brand-level — like the readiness
    # audit — and surfaced as "Your prompts". Dedupe + trim here so both the
    # cost estimate and the launch payload use the same clean list.
    custom_prompts_clean: List[str] = []
    _seen_cp: set = set()
    for _p in (body.custom_prompts or []):
        _t = str(_p or "").strip()
        if _t and _t.lower() not in _seen_cp:
            _seen_cp.add(_t.lower())
            custom_prompts_clean.append(_t)
    # Multi-model for PAID tiers: paid merchants also get ChatGPT for cross-model
    # divergence ("Gemini cites you, ChatGPT doesn't"); the free wedge stays
    # Gemini-only to bound the absorbed cost. Resolve the balance once here and
    # reuse it for the metering gate below.
    providers_for_launch = list(_WEDGE_PROVIDERS)
    if paid_tier:
        for prov in _WEDGE_PAID_PROVIDERS:
            if prov and prov not in providers_for_launch:
                providers_for_launch.append(prov)

    if over_free:
        from services.credit_consumption_service import estimate_probe_credits

        # Per-SKU pricing: each resolved URL is probed prompts_per_sku times per
        # provider; brand-level custom prompts add ONE probe each per provider,
        # and per-SKU merchant prompts (custom_prompts_by_url) add one probe
        # each per provider inside their product's context. Price against the
        # SAME provider set + probe count the worker will actually run so
        # billing matches cost (only RESOLVED products' prompts count).
        per_provider_probes = (
            len(audit_products) * max(1, _WEDGE_PROMPTS_PER_SKU)
            + len(custom_prompts_clean)
            + sum(len(v) for v in custom_prompts_by_sku.values())
        )
        wedge_probe_count = per_provider_probes * max(1, len(providers_for_launch))
        metered_credits, metered_cogs = estimate_probe_credits(
            [(prov, per_provider_probes, True)
             for prov in (providers_for_launch or ["gemini"])]
        )
        available = int(balance.get("credits") or 0)
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
    # buyer-intent query AND the index seed. An explicitly-declared brand wins
    # (a store-less brand pointing at a RETAILER PDP knows its brand; that page's
    # JSON-LD vendor is often the retailer / marketplace seller, not the brand),
    # else the fetched vendor, else the resolved merchant brand. Title stays clean
    # (no brand prefix - the upstream prepends the vendor, so prefixing
    # double-brands).
    brand_for_vendor = (
        merchant_name if merchant_name and merchant_name != "your brand" else None
    )
    for p in audit_products:
        resolved_vendor = resolve_seed_vendor(
            fetched_vendor=p.get("vendor"),
            declared_brand=body.brand,
            fallback_brand=brand_for_vendor,
        )
        if resolved_vendor:
            p["vendor"] = resolved_vendor

    # 4. Mint a synthetic per-product identity per URL. URL audits have NO
    #    synced catalog, so each pasted URL becomes a synthetic SKU (namespaced
    #    `urlwedge:*`) that the durable per-SKU pipeline probes via a registered
    #    synthetic context. Deterministic on (merchant_id, pdp_url) so a re-run
    #    reuses the same key (and the dedup window catches double-submits).
    synthetic_products: List[Dict[str, Any]] = []
    for p in audit_products:
        key = _synthetic_url_sku_key(merchant_id, p["pdp_url"])
        # Retail-channel de-conflation: if the merchant gave their own brand site
        # (website) and this pasted URL is on a DIFFERENT host, it's a retail
        # channel (e.g. the brand's product page on oliveyoung). First-party
        # citation ("is your OWN url cited?") must then be measured against the
        # brand site, not the retailer — otherwise AI citing the retailer reads
        # as first-party endorsement instead of distribution. We keep pdp_url as
        # the pasted page (for product data) but point canonical_url at the brand
        # site. When the pasted URL IS the brand's own site, canonical = the
        # product page itself.
        pdp_host = _domain_from_url(p["pdp_url"])
        is_retail_channel = bool(
            merchant_domain and pdp_host and pdp_host != merchant_domain
        )
        canonical_url = website if (is_retail_channel and website) else p["pdp_url"]
        # The SUBJECT of the audit is the BRAND's product, not the retailer.
        # When the pasted page is a retail channel, its scraped attributes are
        # only REFERRING CONTEXT (the brand's own page is the canonical target);
        # when it's the brand's own page, the attributes are first-party product
        # data. Marking the source lets the report/brief/UI frame retail as "a
        # channel where the product appears" instead of treating the retailer as
        # the product's identity.
        product_data_source = "retail_channel" if is_retail_channel else "own_pdp"
        synthetic_products.append({
            "sku_key": key,
            "product_key": key,
            "title": p["title"],
            "raw_title": p.get("raw_title"),
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type"),
            "pdp_url": p["pdp_url"],
            "canonical_url": canonical_url,
            "retail_channel_host": pdp_host if is_retail_channel else None,
            "product_data_source": product_data_source,
            "attributes_raw": p.get("attributes_raw"),
        })

    # 5. Result fields known immediately (echoed in the 202 + persisted in the
    #    launch so the GET reshape can echo them on completion).
    base_payload = {
        "audited_url": website,
        "tier": "url_per_sku",
        "audited_products": [
            {
                "title": p["title"],
                "raw_title": p.get("raw_title"),
                "pdp_url": p["pdp_url"],
                "vendor": p.get("vendor"),
                "sku_key": sp["sku_key"],
                # Where the product data came from: the brand's own page
                # ("own_pdp") vs a retail listing read only as context
                # ("retail_channel", with the channel host). Lets the UI say
                # "read from your Olive Young listing — connect your store for
                # brand-grounded data" instead of implying the retailer is you.
                "data_source": sp.get("product_data_source"),
                "retail_channel_host": sp.get("retail_channel_host"),
                # ARO: the merchant-facing agent-readability diagnostic + the
                # actionable gaps (the value the brand pays to watch). Pure +
                # compute-on-read from signals already fetched; safe on any input.
                "agent_readiness": compute_agent_readiness_score(p),
            }
            for p, sp in zip(audit_products, synthetic_products)
        ],
        "methodology": {
            "model": "merchant_curated_per_sku",
            "products_audited": len(audit_products),
            "products_requested": len(body.product_urls),
            # Pre-run PLANNED budget + the models we're about to launch. On a
            # successful run `_shape_url_audit_response` overwrites both with the
            # MEASURED coverage; this copy is what the "running" poll and the
            # rare all-providers-failed path show, so it must name the real
            # launch set (not a hardcoded "Gemini") and read as a plan ("up to").
            "queries_per_product": _WEDGE_PROMPTS_PER_SKU,
            # Display-ready header label from the PLANNED launch set — the
            # measured reshape overwrites it; this is what a legacy report shape
            # (no per-provider signal) falls back to, still naming real models.
            "grounded_search_label": _grounded_search_label(providers_for_launch),
            "what_we_checked": (
                "Each product URL you gave us is audited on its own: we run up "
                f"to {_WEDGE_PROMPTS_PER_SKU} AI shopping-agent buyer-intent "
                "queries per product on "
                f"{_humanize_provider_list(providers_for_launch)} (grounded "
                "search) and check whether your URL is cited, which competitors "
                "are cited instead, and on which sources."
            ),
            "limitations": [
                "Catalog-only signals (availability, variants, structured "
                "routing) need a connected store — connect to unlock the full "
                "per-SKU score and execution.",
                "Citation results reflect this grounded sample, not an "
                "exhaustive measurement.",
            ],
            "unresolved_urls": unresolved,
        },
        "free_audits_allowed": (
            _FREE_URL_AUDITS_PER_MERCHANT
            if _FREE_URL_AUDITS_PER_MERCHANT > 0 else None
        ),
        "free_audits_used": used if over_free else used + 1,
        "free_audits_remaining": (
            None if _FREE_URL_AUDITS_PER_MERCHANT <= 0
            else 0 if over_free
            else max(0, _FREE_URL_AUDITS_PER_MERCHANT - (used + 1))
        ),
        "billing_mode": "credits" if over_free else "free",
        "credits_charged": metered_credits if over_free else 0,
        "catalog_dimensions_available": False,
    }

    # 6. Debit credits for a metered run BEFORE enqueue, keyed on the
    #    deterministic idempotency_key (NOT a run_id). Debiting before enqueue
    #    avoids a race where the worker claims the queued run and starts probing
    #    before a failed debit can mark it failed. consume() is idempotent on
    #    the key, so a double-submit within the window charges once.
    idempotency_key = compute_audit_idempotency_key(
        merchant_id=merchant_id,
        product_keys=[sp["product_key"] for sp in synthetic_products],
        subject_type="merchant_url",
    )
    if over_free and metered_credits > 0:
        from services import credit_consumption_service as _ccs
        try:
            await _ccs.consume(
                merchant_id,
                "audit",
                idempotency_key=f"url_wedge:{idempotency_key}",
                credits=metered_credits,
                usd_cogs=metered_cogs,
            )
        except Exception as exc:  # noqa: BLE001 - surface as payment error
            logger.warning(
                "url-wedge credit debit failed merchant_id=%s: %s",
                merchant_id, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "credit_debit_failed",
                    "message": "Could not debit credits for this audit. Re-try.",
                },
            ) from exc

    # 7. Enqueue on the DURABLE per-SKU worker (not a bare asyncio task — that
    #    was the source of orphaned runs that never completed). subject_type
    #    keeps the free-allowance count + GET scoping; the worker builds
    #    products from launch.synthetic_products, runs per-SKU citation probes
    #    (Gemini-only, prompts_per_sku), and finalizes via the minimal
    #    no-executor verify path.
    run_id, was_existing = await enqueue_audit_run_with_replay(
        merchant_id=merchant_id,
        product_keys=[sp["product_key"] for sp in synthetic_products],
        subject_type="merchant_url",
        idempotency_key=idempotency_key,
        request_options_jsonb={
            "launch": {
                "audit_mode": "per_sku",
                "coverage_profile": _WEDGE_COVERAGE_PROFILE,
                "providers": providers_for_launch,
                "verify_providers": list(_WEDGE_VERIFY_PROVIDERS),
                "prompts_per_sku": _WEDGE_PROMPTS_PER_SKU,
                "custom_prompts": custom_prompts_clean,
                # Per-SKU merchant prompts (custom_prompts_by_url re-keyed to
                # sku_key): probed inside their SKU's context and pinned into
                # its basis — the merchant's niche tracks week over week.
                "custom_prompts_by_sku": custom_prompts_by_sku,
                # Probe winnable SPECIFIC discovery prompts (LLM value-prop
                # extraction), not just generic category heads — this is the
                # demand a store-less brand can realistically win in AI.
                "winnable_prompts": True,
                # Opt-in basis refresh (default False): regenerate the prompt
                # basis for this run instead of pinning a prior run's, so a
                # re-audit reflects newly-grounded attributes in the probed set.
                "refresh_prompt_basis": bool(body.refresh),
                "synthetic_products": synthetic_products,
                "merchant_name": merchant_name,
                "merchant_domain": merchant_domain,
                "billing_mode": "credits" if over_free else "free",
                "estimated_audit_credits": int(metered_credits),
                "wedge_base_payload": base_payload,
            }
        },
    )
    if not run_id:
        # Couldn't persist the run — refund the debit so the merchant isn't
        # charged for an audit that never started.
        if over_free and metered_credits > 0:
            from services import credit_consumption_service as _ccs
            try:
                await _ccs.refund(
                    merchant_id, "audit", metered_credits,
                    source_event_id=f"url_wedge_refund:{idempotency_key}",
                    usd_cogs=metered_cogs,
                )
            except Exception:  # noqa: BLE001 - best-effort refund
                logger.warning(
                    "url-wedge refund failed merchant_id=%s", merchant_id,
                    exc_info=True,
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start the audit (storage unavailable). Re-try.",
        )

    base_payload["audit_run_id"] = run_id
    return {"status": "running", "run_id": run_id, "brand_report": None, **base_payload}


async def _merchant_audit_context(merchant_id: str) -> Dict[str, Any]:
    """Subscription tier + store-connection state, so the results page doesn't
    push the free-sample / connect-store / buy-credits funnel at a merchant who
    is already subscribed and connected. Best-effort; defaults are conservative
    (treat as free + unconnected) so the funnel only HIDES on a confirmed signal."""
    ctx: Dict[str, Any] = {
        "plan_tier": "free", "is_paid": False, "store_connected": False,
    }
    try:
        bal = await get_balance(merchant_id)
        tier = str(bal.get("plan_tier") or "free").lower()
        ctx["plan_tier"] = tier
        ctx["is_paid"] = tier != "free"
        ctx["credits"] = int(bal.get("credits") or 0)
    except Exception:  # noqa: BLE001 - context is advisory; never block the GET
        logger.warning("merchant_audit_context: balance lookup failed", exc_info=True)
    try:
        from services.merchant_integration_state import get_integration_state
        st = await get_integration_state(merchant_id) or {}
        ctx["store_connected"] = bool(st.get("store_platform_integrated"))
    except Exception:  # noqa: BLE001
        logger.warning("merchant_audit_context: integration lookup failed", exc_info=True)
    return ctx


@router.get("/url-readiness/{run_id}")
async def get_merchant_url_audit(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
    summary_only: bool = False,
) -> Dict[str, Any]:
    """Poll a free URL-audit wedge run kicked off by POST /url-readiness.

    Returns `{status: 'running'}` until the background audit finishes, then the
    full result (`status: 'succeeded'` + brand_report + methodology + …) or
    `{status: 'failed', error}`. Scoped to the calling merchant + the wedge
    subject_type so it can't read another merchant's or a synced run.

    `summary_only=true` returns just {status, run_id, audit_run_id,
    report_summary} — for surfaces (the portal homepage hero) that only need
    the condensed contract and must not pull the multi-hundred-KB full report.
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
        # report_jsonb is the per_sku brand_report (per_sku_reports + brand_rollup
        # + authority_map). Reshape it into the URL-audit envelope the client
        # expects (status/run_id/per_sku_reports/methodology/…).
        shaped = _shape_url_audit_response(row)
        if summary_only:
            return {
                "status": "succeeded",
                "run_id": run_id,
                "audit_run_id": run_id,
                "report_summary": shaped.get("report_summary"),
            }
        # merchant_context lets the page stop pushing the "free sample / connect
        # your store / buy credits" funnel at a subscribed, connected merchant.
        shaped["merchant_context"] = await _merchant_audit_context(merchant_id)
        return shaped
    if run_status == "failed":
        err = row.get("error_message") or ""
        msg = (
            "The audit pipeline returned fallback data; please re-run."
            if err == "upstream_mock_fallback"
            else (err or "Audit failed. Please re-run.")
        )
        return {"status": "failed", "run_id": run_id, "error": msg}
    # NOTE: no inline stale-timeout fail here. URL audits now run on the durable
    # worker (per_sku), which legitimately runs several minutes; the old 15-min
    # inline check would false-fail a healthy, actively-leased run (split-brain
    # with the worker). Truly-abandoned runs are reaped by
    # fail_abandoned_runs() (30-min TTL, no-live-lease) instead.
    return {"status": "running", "run_id": run_id}


_SHARE_LINKS_ENABLED = (
    _os.getenv("AUDIT_SHARE_LINKS_ENABLED", "false").strip().lower() == "true"
)
_SHARE_LINK_TTL_DAYS = int(_os.getenv("AUDIT_SHARE_LINK_TTL_DAYS", "30"))
# PUBLIC share = ALLOWLIST, not denylist (security review round 2: a
# denylist on an unauthenticated surface ships every future key by default,
# and the first version leaked registry pitch_recipient emails through
# where_youre_losing.outreach_moves[] and pitch_targets[]). Only these
# top-level keys may leave the building:
_SHARE_ALLOWED_TOP_KEYS = (
    "status",
    "run_id",
    "audit_run_id",
    "tier",
    "per_sku_reports",
    "brand_rollup",
    "authority_map",
    "where_you_can_win",
    "suggested_prompts",
    "where_youre_losing",
    "merchant_narrative",
    "catalog_dimensions_available",
    "methodology",
    "audited_products",
    "report_summary",
)

# Curated outreach routing is for the MERCHANT, never the open web. These
# key names are scrubbed RECURSIVELY from the whole allowlisted payload so
# no present-or-future nesting (moves, pitch_targets, win-plan folds) can
# resurface them.
_SHARE_SCRUB_KEYS = frozenset(
    {"pitch_recipient", "pitch_email", "pitch_submission_url", "pitch_draft"}
)


def _deep_scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _deep_scrub(v)
            for k, v in node.items()
            if k not in _SHARE_SCRUB_KEYS
        }
    if isinstance(node, list):
        return [_deep_scrub(item) for item in node]
    return node


def _redact_shared_report(shaped: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        k: _deep_scrub(v)
        for k, v in shaped.items()
        if k in _SHARE_ALLOWED_TOP_KEYS
    }
    out["shared_view"] = True
    return out


@router.post("/url-readiness/{run_id}/share")
async def create_url_audit_share_link(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Mint (or return the existing) read-only share token for a completed
    run. Idempotent per run: one live token at a time; revoke to rotate."""
    if not _SHARE_LINKS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "share_links_disabled"},
        )
    row = await fetch_audit_run_by_id(run_id=run_id)
    if (
        not row
        or row.get("merchant_id") != merchant_id
        or row.get("subject_type") != "merchant_url"
        or (row.get("status") or "") != "succeeded"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "audit_run_not_found"},
        )
    from db.database import database

    try:
        existing = await database.fetch_one(
            """
            SELECT token, expires_at FROM audit_share_tokens
            WHERE run_id = :r AND merchant_id = :m
              AND revoked_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC LIMIT 1
            """,
            {"r": run_id, "m": merchant_id},
        )
    except Exception:  # noqa: BLE001 — table not provisioned yet -> clean 503
        logger.warning("audit_share_tokens mint lookup failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "share_links_unavailable"},
        )
    if existing:
        token = existing["token"]
        expires_at = existing["expires_at"]
    else:
        import secrets
        from datetime import timedelta as _td

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + _td(days=_SHARE_LINK_TTL_DAYS)
        await database.execute(
            """
            INSERT INTO audit_share_tokens (token, run_id, merchant_id, expires_at)
            VALUES (:t, :r, :m, :e)
            """,
            {"t": token, "r": run_id, "m": merchant_id, "e": expires_at},
        )
    return {
        "token": token,
        "share_path": f"/share/r/{token}",
        "expires_at": str(expires_at),
    }


@router.delete("/url-readiness/{run_id}/share")
async def revoke_url_audit_share_link(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    if not _SHARE_LINKS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "share_links_disabled"},
        )
    from db.database import database

    await database.execute(
        """
        UPDATE audit_share_tokens SET revoked_at = now()
        WHERE run_id = :r AND merchant_id = :m AND revoked_at IS NULL
        """,
        {"r": run_id, "m": merchant_id},
    )
    # Revocation is immediate: drop the (tiny, 60s-TTL) public response cache
    # wholesale rather than letting a revoked link serve until expiry.
    _SHARE_CACHE.clear()
    return {"revoked": True}


# Public, UNAUTHENTICATED read of a shared report. Separate router so the
# merchant-auth dependency of `router` never applies here.
public_share_router = APIRouter(prefix="/api/public", tags=["public-share"])


# 60s in-memory response cache: a leaked/hammered valid token replays a
# cached body instead of re-running full report shaping every hit (review
# P2 — unauthenticated CPU amplification). Tiny + bounded; revocation takes
# effect within the TTL.
_SHARE_CACHE: "Dict[str, Tuple[float, bytes]]" = {}
_SHARE_CACHE_TTL_S = 60.0
_SHARE_CACHE_MAX = 256


@public_share_router.get("/audit-share/{token}")
async def read_shared_audit(token: str) -> Response:
    if not _SHARE_LINKS_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    import time as _time

    cached = _SHARE_CACHE.get(token)
    if cached and cached[0] > _time.monotonic():
        return Response(
            content=cached[1],
            media_type="application/json",
            headers={"X-Robots-Tag": "noindex, nofollow"},
        )
    from db.database import database

    try:
        row = await database.fetch_one(
            """
            SELECT run_id FROM audit_share_tokens
            WHERE token = :t AND revoked_at IS NULL AND expires_at > now()
            """,
            {"t": token},
        )
    except Exception:  # noqa: BLE001 — table not provisioned yet -> clean 503
        logger.warning("audit_share_tokens lookup failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "share_links_unavailable"},
        )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    run = await fetch_audit_run_by_id(run_id=row["run_id"])
    if not run or (run.get("status") or "") != "succeeded":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    shaped = _redact_shared_report(_shape_url_audit_response(run))
    import json as _json

    body = _json.dumps(shaped, default=str).encode()
    if len(_SHARE_CACHE) >= _SHARE_CACHE_MAX:
        _SHARE_CACHE.clear()
    _SHARE_CACHE[token] = (_time.monotonic() + _SHARE_CACHE_TTL_S, body)
    return Response(
        content=body,
        media_type="application/json",
        headers={"X-Robots-Tag": "noindex, nofollow"},
    )


@router.post("/url-readiness/{run_id}/deck")
async def export_url_audit_deck(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
) -> Response:
    """Export a completed URL-audit run as a leadership deck (PPTX).

    Tiering + billing (PR-4):
      - Free tier: a single watermarked preview slide (cover + score). No LLM
        runs and nothing is billed — the preview is the distribution hook.
      - Paid tier: the full deck. Its one LLM step (the executive-summary
        slide) bills on ACTUAL token usage at DECK_TOKEN_PRICE_MULTIPLE (1.6x
        measured token COGS -> credits, ceil, min 1). When the LLM is
        unavailable the deck ships without that slide and costs 0 credits —
        never charge for work that didn't run.
      - Idempotency: one charge per run (key report_deck:{run_id}); re-exports
        replay the debit instead of double-charging.
    402 on insufficient credits; 409 when the run has no renderable summary;
    503 when python-pptx isn't installed on the serving image.
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
    if (row.get("status") or "") != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "audit_not_finished",
                "message": "The audit hasn't finished yet — export once it succeeds.",
            },
        )
    shaped = _shape_url_audit_response(row)
    summary = shaped.get("report_summary")
    if not isinstance(summary, dict) or not (
        (summary.get("score") or {}).get("display") is not None
        or (summary.get("verdict") or {}).get("headline")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "summary_unavailable",
                "message": "This run has no report summary to export — re-run the audit.",
            },
        )

    paid = await merchant_is_paid_tier(merchant_id)
    billing_mode = "preview_only"
    credits_charged = 0
    executive_bullets = None
    llm_tokens = None

    if paid:
        billing_mode = "included"
        # The deck's only LLM step. Failure -> deck ships without the slide,
        # nothing billed (never charge for work that didn't run).
        try:
            generated = await generate_executive_summary(summary)
        except Exception:  # noqa: BLE001
            logger.warning("deck executive summary failed", exc_info=True)
            generated = None
        if generated:
            executive_bullets, in_tok, out_tok = generated
            llm_tokens = (in_tok, out_tok)

    # Render BEFORE any debit: if python-pptx is missing this 503s with no
    # money moved ("never charge for work that didn't run" applies to the
    # deck itself, not just the LLM step — review fix, PR #1411 round 2).
    deck = build_report_deck(
        summary,
        executive_bullets=executive_bullets,
        preview_only=not paid,
    )
    if deck is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "deck_renderer_unavailable",
                "message": "Deck export isn't available on this deployment yet.",
            },
        )

    if paid and llm_tokens:
        in_tok, out_tok = llm_tokens
        credits, usd_cogs = credits_for_tokens(
            DECK_LLM_PROVIDER,
            input_tokens=in_tok,
            output_tokens=out_tok,
            multiple=DECK_TOKEN_PRICE_MULTIPLE,
        )
        if credits > 0:
            try:
                result = await consume_credits(
                    merchant_id,
                    "report_deck_export",
                    f"report_deck:{run_id}",
                    credits=credits,
                    usd_cogs=usd_cogs,
                )
            except InsufficientCreditsError:
                # NOTE: fires for merchants the balance layer treats as
                # non-overage; plan-tier merchants may instead accrue overage
                # per the billing system's paid-tier semantics.
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "code": "insufficient_credits",
                        "message": (
                            "Not enough credits to export the deck — top up "
                            "or upgrade your plan."
                        ),
                        "credits_required": credits,
                    },
                )
            billing_mode = "metered"
            credits_charged = int(result.get("credits") or 0)
    filename = f"pivota-ai-readiness-{run_id}.pptx"
    return Response(
        content=deck,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Pivota-Billing-Mode": billing_mode,
            "X-Pivota-Credits-Charged": str(credits_charged),
        },
    )


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
    # P1: auto-seed the commerce index with the audited SKUs — the audit IS the
    # index-build motion. Runs HERE, off the request path, so it never adds
    # latency to POST /url-readiness. Best-effort + UNCONDITIONAL (W5 P2 — seeding
    # is the main line, no longer flag-gated); observed/unclaimed seeds, un-served
    # until they graduate or are claimed. Seeded before the report so a probe
    # failure below still leaves the index populated.
    try:
        for _seed_product in audit_products:
            await upsert_audited_sku_to_index(merchant_id, _seed_product)
    except Exception:  # noqa: BLE001 — intake must never break the audit
        logger.warning("audit_index_intake: seed loop failed", exc_info=False)

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
        # W4: the default-off LLM review gate (Layer 2) is removed. Prose
        # trustworthiness is now enforced upstream at generation — the brief's
        # closed-world entity manifest + grounding validator — and detected here
        # by the rendered-copy invariant (W7.1), so a post-hoc LLM re-read added
        # cost and a second failure mode without unique coverage.
    except Exception as exc:  # noqa: BLE001 — the invariant gate must not crash the audit runner
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


@router.get("/tracking")
async def get_merchant_visibility_tracking(
    limit: int = 50,
    subject_type: str = "merchant",
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Visibility-over-time series for this merchant (the W2 payoff / retention
    chart). Brand-level visibility / attribution / category scores across the
    merchant's completed audits, plus per-provider lines — each point tagged with
    its measurement basis so the chart connects ONLY comparable (same pinned prompt
    set) points and breaks where the basis changed. Empty/baseline until there are
    >=2 same-basis runs to trend.

    Each point also discloses its SKU coverage (sku_count / attempted_sku_count /
    panel_id — every brand score is an AVERAGE over that run's measured products),
    `panel_changes` marks checks whose measured SKU set differs from the previous
    one ("tracked products changed", distinct from a prompt refresh), and `per_sku`
    carries one mini-series per measured product (same basis-comparability rule)
    so the chart can offer a per-SKU lens without a second query.

    `subject_type` scopes the series to one run kind, mirroring /history:
    "merchant" (per-SKU catalog audits, default) or "merchant_url" (the
    URL-visibility wedge) — the two never mix in one trend because their
    scores are measured on different subjects."""
    if limit <= 0 or limit > 50:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 50",
        )
    if subject_type not in ("merchant", "merchant_url"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject_type must be 'merchant' or 'merchant_url'",
        )
    from db.merchant_audit_runs import score_history_for_merchant
    from services.audit_tracking_series import build_tracking_series

    rows = await score_history_for_merchant(
        merchant_id=merchant_id, limit=limit, subject_type=subject_type,
    )
    series = build_tracking_series(rows)
    return {"merchant_id": merchant_id, **series}


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
        # Viewing a specific (e.g. historical) run: scope tasks to that run so a
        # past report doesn't show today's whole queue.
        tasks_scope = "explicit_run"
        scoped_parent_audit_run_id = explicit_run_id
        latest_audit_run_id = explicit_run_id
    else:
        # DEFAULT = the persistent cross-audit workspace (page-usability Step 1).
        # One living list of open work across every audit + the merchant's own
        # standing tasks. This is now honest: the audit-completion reconciliation
        # (materialize_tasks_from_audit) closes prior-run pending tasks the latest
        # audit dropped, so "across all audits" no longer means "accumulating
        # clutter". `include_history` kept as an explicit alias for back-compat.
        tasks_scope = "persistent"
        scoped_parent_audit_run_id = None
        # Surface the latest completed run id for display (header/trend), without
        # scoping the list to it.
        latest_audit_run_id = await _latest_completed_audit_run_id_for_tasks(
            merchant_id
        )
        # Lazy, idempotent backlog dedup: the persistent scope surfaced an
        # accumulated pile of identical pending tasks the old latest_completed
        # scope was hiding. Collapse duplicates on read (best-effort) so the queue
        # self-heals without waiting for the next audit's reconciliation.
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
        # With no run scope (parent=None) the query already returns every task
        # incl. standing NULL-parent ones; include_unscoped only matters when
        # scoping TO a run.
        include_unscoped=(scoped_parent_audit_run_id is not None),
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


# ---------------------------------------------------------------------
# ADR-006 Phase 3 — per-SKU request_indexing trigger + status read-back.
# Consumes cta.target_sku_key from the per-SKU next_best_action and submits
# the SKU's Pivota *canonical* PDP URL under the Pivota-owned credential.
# Flag-gated (gsc_pivota_submit_enabled, default off) inside the service, so
# this stays inert — and the portal keeps the self-serve checklist — until
# ADR-006 Phase-1 validation passes and the flag is flipped (Phase 4).
# ---------------------------------------------------------------------


class _RequestIndexingBody(BaseModel):
    target_sku_key: str = Field(..., min_length=1, max_length=256)
    audit_run_id: Optional[str] = None


@router.post("/sku/request-indexing")
async def request_sku_indexing_endpoint(
    body: _RequestIndexingBody,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Submit this SKU's Pivota canonical PDP URL to Google's Indexing API
    under the Pivota credential. Idempotent (won't re-submit an in-flight URL).
    merchant_id comes from the token, and the canonical URL is resolved scoped
    to that merchant, so a merchant can only act on its own SKUs."""
    sku_key = (body.target_sku_key or "").strip()
    if not sku_key:
        raise HTTPException(status_code=422, detail="target_sku_key is required")
    from services.pivota_indexing_request import request_sku_indexing

    return await request_sku_indexing(
        sku_key, merchant_id, audit_run_id=body.audit_run_id,
    )


@router.get("/sku/indexing-status")
async def sku_indexing_status_endpoint(
    target_sku_key: str,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Per-SKU read-back of the Pivota-page indexing status
    (submitted/pending/indexed) for the portal's status chip."""
    sku_key = (target_sku_key or "").strip()
    if not sku_key:
        raise HTTPException(status_code=422, detail="target_sku_key is required")
    from services.pivota_indexing_request import get_sku_indexing_status

    return await get_sku_indexing_status(sku_key, merchant_id)


class _NicheContentBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=400)
    sku_key: Optional[str] = None
    sku_name: Optional[str] = None
    why_you_fit: Optional[str] = None
    kind: str = "content"  # content | defend


@router.post("/tasks/niche-content")
async def create_niche_content_task(
    body: _NicheContentBody,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Phase 3: graduate a winnable-niche 'create the answer' action (from the
    Where-you-can-win view) into a tracked distribution task in the merchant's
    queue. Idempotent — a second click on the same niche returns the existing
    pending task instead of stacking duplicates."""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=422, detail="query is required")
    from db.merchant_tasks import (
        find_pending_supersede_candidates,
        record_task_created,
    )

    is_defend = (body.kind or "").strip().lower() == "defend"
    lever = "niche_defend" if is_defend else "niche_content"
    title = (
        f"Defend the niche: {q}" if is_defend
        else f"Create the answer AI cites for: {q}"
    )
    existing = await find_pending_supersede_candidates(
        merchant_id=merchant_id, lever=lever, title=title,
    )
    if existing:
        return {"status": "exists", "task_id": existing[0].get("task_id"), "title": title}

    sku = (body.sku_name or "").strip()
    fit = (body.why_you_fit or "").strip()
    if is_defend:
        task_body = (
            "A competitor moved into a niche you'd won"
            + (f" with {sku}" if sku else "")
            + ". Strengthen your page section + FAQ for this query (fresher detail, "
            "proof, reviews) and re-publish, then re-run the audit to win it back."
        )
    else:
        task_body = (
            "This is a winnable niche — no brand owns the answer yet"
            + (f", and {sku} is a strong match" if sku else "")
            + (f" (you fit: {fit})" if fit else "")
            + ". Write a focused page section + FAQ answering this query so AI starts "
            "citing you, then re-run the audit to confirm it landed."
        )
    task_id = await record_task_created(
        merchant_id=merchant_id,
        title=title,
        body=task_body,
        severity="high" if is_defend else "medium",
        lever=lever,
        assigned_to_agent="niche_targeting",
        evidence={
            "kind": lever,
            "query": q,
            "sku_key": body.sku_key,
            "sku_name": body.sku_name,
            "why_you_fit": fit or None,
        },
    )
    if not task_id:
        raise HTTPException(status_code=500, detail="could not create task")
    return {"status": "success", "task_id": task_id, "title": title}


class _OutreachPitchBody(BaseModel):
    host: str = Field(..., min_length=1, max_length=256)
    query: str = Field(..., min_length=1, max_length=400)
    state: str = "draft_ready"  # draft_ready | submission_only
    tier: Optional[int] = None
    recipient_email: Optional[str] = None
    submission_url: Optional[str] = None
    sku_key: Optional[str] = None
    sku_title: Optional[str] = None
    channel: Optional[str] = None  # mailto | submission_form
    audit_run_id: Optional[str] = None


@router.post("/tasks/outreach-pitch")
async def mark_outreach_pitch_sent(
    body: _OutreachPitchBody,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Outreach lifecycle Step 1: a merchant marks a win-plan pitch SENT to an
    independent host. Persists a tracked outreach record (merchant_task,
    lever='outreach_pitch') keyed to (host, query) so the NEXT audit can
    re-verify whether that host now cites the merchant — the closed loop that
    proves the lift. Idempotent: a second mark on the same host+query returns the
    existing record. merchant_id is from the token, so a merchant only records its
    own outreach. The sent-state lives in evidence_jsonb.outreach.status (the task
    `status` enum has no 'sent'); created as a pending tracked row."""
    host = (body.host or "").strip().lower()
    query = (body.query or "").strip()
    if not host or not query:
        raise HTTPException(status_code=422, detail="host and query are required")
    from datetime import datetime, timezone

    from db.merchant_tasks import (
        find_pending_supersede_candidates,
        record_task_created,
    )

    lever = "outreach_pitch"
    title = f"Pitch sent: {host} — {query}"[:500]
    existing = await find_pending_supersede_candidates(
        merchant_id=merchant_id, lever=lever, title=title,
    )
    if existing:
        return {"status": "exists", "task_id": existing[0].get("task_id"), "title": title}

    state = (body.state or "draft_ready").strip().lower()
    channel = body.channel or ("submission_form" if state == "submission_only" else "mailto")
    sku = (body.sku_title or "").strip()
    task_body = (
        f"You pitched {host} to get cited for “{query}”"
        + (f" (for {sku})" if sku else "")
        + f". We'll re-check on your next audit and tell you if {host} starts "
        "citing you for this query."
    )
    task_id = await record_task_created(
        merchant_id=merchant_id,
        title=title,
        body=task_body,
        severity="medium",
        lever=lever,
        parent_audit_run_id=body.audit_run_id,
        assigned_to_human="merchant",
        evidence={
            "kind": "outreach_pitch",
            "outreach": {
                "host": host,
                "tier": body.tier,
                "recipient_email": body.recipient_email,
                "submission_url": body.submission_url,
                "query": query,
                "sku_key": body.sku_key,
                "sku_title": body.sku_title,
                "state": state,
                "channel": channel,
                "status": "sent",
                "sent_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    if not task_id:
        raise HTTPException(status_code=500, detail="could not record outreach")
    return {"status": "success", "task_id": task_id, "title": title}


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


# ── "Ask about this product" — grounded freeform Q&A over a completed audit ───
# A merchant types a question; we answer it using ONLY the audit data already
# computed for that run, via Deepseek (ungrounded — it reasons over the slice we
# pass, never the live web, so it can't invent outside facts). This is the
# generative companion to the deterministic per-product card; the structured
# numbers remain the source of truth and the answer is rendered as an "AI
# summary".
#
# Metered (category="prompt") via the canonical credit_consumption_service:
# priced as one ungrounded Deepseek probe (~1 credit). Free-tier merchants are
# gated up-front (402) when they can't afford it; paid tiers run on overage like
# the audit path. We charge AFTER a successful answer — a failed Deepseek call
# costs the merchant nothing. Idempotent on (merchant, run, product, question)
# so re-asking the same question doesn't double-charge.

_ASK_CONTEXT_MAX_CHARS = 8000

# One ungrounded Deepseek chat completion. Priced through the shared per-probe
# model so an "ask" bills consistently with the audit's Deepseek verify probes.
_ASK_PROBE_SPEC = [("deepseek", 1, False)]


def _ask_report_root(report: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], List[Any]]:
    """per_sku_reports + merchant_narrative may sit at the top level (per_sku
    runs) or nested under brand_report (legacy wedge). Normalize both."""
    if not isinstance(report, dict):
        return None, []
    brand_report = report.get("brand_report")
    brand_report = brand_report if isinstance(brand_report, dict) else {}
    per_sku = report.get("per_sku_reports") or brand_report.get("per_sku_reports") or []
    narrative = report.get("merchant_narrative") or brand_report.get("merchant_narrative")
    return (narrative if isinstance(narrative, dict) else None), (per_sku if isinstance(per_sku, list) else [])


def _ask_find_sku(per_sku: List[Any], product_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """Match a SKU by product_key/sku_key. product_key has two coexisting
    formats sharing only the sig_<hex>; fall back to that shared token."""
    if not product_key:
        return None
    sig = None
    match = re.search(r"sig_[0-9a-f]+", str(product_key))
    if match:
        sig = match.group(0)
    for r in per_sku:
        if not isinstance(r, dict):
            continue
        keys = str(r.get("product_key") or "") + " " + str(r.get("sku_key") or "")
        if product_key in (r.get("product_key"), r.get("sku_key")):
            return r
        if sig and sig in keys:
            return r
    return None


def _ask_real_brief(sku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Only feed the real LLM strategic brief — never the deterministic
    boilerplate fallback (mirrors the frontend suppression rule)."""
    nba = sku.get("next_best_action") or {}
    brief = nba.get("strategic_brief")
    if not isinstance(brief, dict):
        return None
    outcome = (nba.get("brief_debug") or {}).get("outcome")
    moves = list(brief.get("first_moves") or []) + list(brief.get("traffic_strategy") or [])
    has_object_move = any(isinstance(m, dict) for m in moves)
    if outcome == "llm" or (outcome is None and not has_object_move):
        return brief
    return None


def _ask_sku_slice(sku: Dict[str, Any]) -> Dict[str, Any]:
    pc = sku.get("product_competitiveness") or {}
    disc = pc.get("discovery") or {}
    ca = sku.get("channel_appearance") or {}
    per_prompt = (sku.get("opportunity") or {}).get("per_prompt") or []
    slice_: Dict[str, Any] = {
        "name": (sku.get("identity") or {}).get("name") or sku.get("sku_title"),
        "verdict": (sku.get("band_display") or {}).get("label") or sku.get("band"),
    }
    if pc:
        slice_["discovery"] = {
            "searches_tested": disc.get("total"),
            "independently_recommended": disc.get("appeared_recommended"),
            "found_via_listing": disc.get("appeared_listing"),
            "by_model": disc.get("by_model") or pc.get("by_model"),
            "missed_searches": [q for q in (disc.get("missed") or []) if isinstance(q, str)][:5],
            "ai_recommends_instead": [
                c.get("name") for c in (disc.get("top_competitors") or []) if isinstance(c, dict)
            ][:6],
            "grounding_unavailable": pc.get("grounding_unavailable"),
        }
    if ca:
        slice_["channels"] = {
            "own_site_cited": ca.get("own_site_cited"),
            "brand_mentioned_count": ca.get("brand_mentioned_count"),
            "total_queries": ca.get("total_queries"),
            "where_ai_sends_buyers": [
                {"host": c.get("host"), "type": c.get("type_label"), "cited": c.get("cited_query_count")}
                for c in (ca.get("channels") or [])
                if isinstance(c, dict) and not c.get("is_own_site")
            ][:6],
        }
    what_ai_said = [
        {
            "query": r.get("query"),
            "ai_answer": (r.get("cited_evidence") or {}).get("excerpt"),
            "ai_named_instead": (r.get("substitution") or {}).get("substituted_by"),
        }
        for r in per_prompt
        if isinstance(r, dict) and (r.get("cited_evidence") or {}).get("excerpt")
    ]
    if what_ai_said:
        slice_["what_ai_actually_said"] = what_ai_said[:4]
    brief = _ask_real_brief(sku)
    if brief:
        slice_["plan"] = {
            "your_angle": brief.get("your_angle"),
            "why_you_lose": brief.get("why_you_lose"),
            "the_call": brief.get("core_decision"),
            "first_moves": [m for m in (brief.get("first_moves") or []) if isinstance(m, str)][:5],
        }
    return slice_


def _build_ask_context(report: Dict[str, Any], product_key: Optional[str]) -> Dict[str, Any]:
    narrative, per_sku = _ask_report_root(report)
    ctx: Dict[str, Any] = {}
    if narrative:
        ww = narrative.get("whats_working") or {}
        wl = narrative.get("where_youre_losing") or {}
        ctx["overview"] = {
            "headline": narrative.get("headline_story"),
            "whats_working": ww.get("summary") if isinstance(ww, dict) else None,
            "where_youre_losing": wl.get("summary") if isinstance(wl, dict) else None,
            "top_actions": [
                a.get("headline")
                for a in (narrative.get("prioritized_actions") or [])
                if isinstance(a, dict) and a.get("headline")
            ][:5],
            "honest_limits": [l for l in (narrative.get("honest_limits") or []) if isinstance(l, str)][:3],
        }
    sku = _ask_find_sku(per_sku, product_key)
    if isinstance(sku, dict):
        ctx["product"] = _ask_sku_slice(sku)
    elif per_sku:
        ctx["products"] = [
            {
                "name": (r.get("identity") or {}).get("name") or r.get("sku_title"),
                "verdict": (r.get("band_display") or {}).get("label") or r.get("band"),
            }
            for r in per_sku
            if isinstance(r, dict)
        ][:10]
    return ctx


_ASK_SYSTEM_PROMPT = (
    "You are Pivota's AI-commerce-readiness assistant, helping a merchant "
    "understand their audit. Answer the QUESTION using ONLY the facts in "
    "CONTEXT (a JSON slice of their audit). Rules: never invent competitors, "
    "hosts, brands, numbers, or recommendations that are not in CONTEXT; if the "
    "answer is not in the data, say you don't have that in this audit and point "
    "them to what they could check or re-run; never contradict the numbers in "
    "CONTEXT; be concrete, plain-English, and under 120 words; no markdown "
    'headers. Respond as JSON: {"answer": "<your answer>"}.'
)


class MerchantAuditAskRequest(BaseModel):
    """POST /ask body — a grounded freeform question about a completed audit."""

    run_id: str = Field(..., min_length=8, max_length=128, description="Audit run to ground the answer in.")
    question: str = Field(..., min_length=3, max_length=500, description="The merchant's freeform question.")
    product_key: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Optional SKU to focus on; omit for a brand-level answer.",
    )


@router.post("/ask")
async def answer_merchant_audit_question(
    body: MerchantAuditAskRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Answer a freeform merchant question grounded in their completed audit.

    Cross-tenant safe: the run must belong to the calling merchant. Ungrounded
    Deepseek reasons only over the audit slice we build — it cannot reach the
    web — so the answer stays faithful to the report. Metered (category="prompt",
    ~1 credit); free tiers gated up-front, charged only on a successful answer.
    """
    run = await fetch_audit_run_by_id(run_id=body.run_id)
    if not run or run.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=404, detail="Audit run not found.")
    report = run.get("report_jsonb")
    if not isinstance(report, dict):
        raise HTTPException(
            status_code=409,
            detail="This audit doesn't have a report to answer from yet.",
        )

    context = _build_ask_context(report, body.product_key)
    if not context:
        raise HTTPException(
            status_code=409,
            detail="This audit doesn't have enough detail to answer questions yet.",
        )

    # Cost gate. Price one ungrounded Deepseek probe; gate free tiers up-front so
    # we never do the work for a merchant who can't pay. Paid tiers run on
    # overage (like the audit path), so they skip the pre-flight block.
    cost_credits, _ = estimate_probe_credits(_ASK_PROBE_SPEC)
    is_paid = await merchant_is_paid_tier(merchant_id)
    if cost_credits > 0 and not is_paid:
        balance = await get_balance(merchant_id)
        if int(balance.get("credits") or 0) < cost_credits:
            raise HTTPException(
                status_code=402,
                detail="Not enough credits to ask a question. Top up to continue.",
            )

    context_json = json.dumps(context, default=str)[:_ASK_CONTEXT_MAX_CHARS]
    user_message = (
        f"CONTEXT:\n{context_json}\n\nQUESTION: {body.question.strip()}\n\n"
        'Answer as JSON: {"answer": "..."}.'
    )

    try:
        answer = await answer_grounded_question(
            system_prompt=_ASK_SYSTEM_PROMPT,
            user_message=user_message,
        )
    except DeepseekProbeError as exc:
        # No charge on failure — the merchant gets nothing, so they pay nothing.
        logger.warning("merchant audit ask failed (run=%s): %s", body.run_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Couldn't generate an answer right now — please try again.",
        )

    if not answer:
        answer = (
            "I don't have enough in this audit to answer that confidently. Try "
            "asking about your discovery results, the competitors AI named, or "
            "where AI sends buyers — or re-run the audit for fresh data."
        )

    # Charge on success. Idempotent on (merchant, run, product, question) so a
    # retry of the same question replays the debit rather than double-charging.
    charged = 0
    if cost_credits > 0:
        idem = "ask:" + hashlib.sha256(
            "|".join(
                [
                    merchant_id,
                    body.run_id,
                    body.product_key or "",
                    body.question.strip(),
                ]
            ).encode("utf-8")
        ).hexdigest()
        try:
            result = await consume_credits(
                merchant_id, "prompt", idem, probes=_ASK_PROBE_SPEC,
            )
            charged = int(result.get("credits") or 0)
        except InsufficientCreditsError:
            # Rare race (balance dropped after the pre-flight gate, or a paid
            # tier with no overage room). The answer is already produced — don't
            # punish the merchant for our race; log and return it uncharged.
            logger.warning(
                "merchant audit ask: debit raced insufficient (merchant=%s run=%s)",
                merchant_id, body.run_id,
            )

    return {"answer": answer, "grounded": True, "credits_charged": charged}


# ── "Start here" actions — turn a prioritized_actions move into a real follow-up ─
# Clicking an action (1) creates a tracked merchant_task (the durable follow-up,
# visible in the plan, markable done — the hook a future service-connection picks
# up to auto-distribute) and (2) best-effort drafts the deliverable via grounded
# Deepseek (the comparison copy / outreach template), so Pivota does the "create"
# part. Drafting is metered (category="prompt", ~1 credit, charge-on-success); the
# task is always created even if drafting is skipped (no credits) or fails.

_ACTION_DRAFT_SYSTEM_PROMPT = (
    "You are Pivota's content assistant, drafting a ready-to-use deliverable so a "
    "merchant can execute one audit action. Use ONLY the facts in CONTEXT (their "
    "audit). For a comparison action, draft the comparison-page copy: a short "
    "intro, 3-4 honest comparison points vs the named competitor, and a 'why buy "
    "direct' close. For an outreach or review action, draft a short, friendly "
    "outreach / review-request template. Be specific to the merchant's product and "
    "the competitors/sources named in CONTEXT; never invent facts, certifications, "
    "or numbers that aren't there. Plain text, ready to paste, under 220 words, no "
    'markdown headers. Return JSON: {"answer": "<the draft>"}.'
)


def _action_product_key_by_title(report: Dict[str, Any], sku_title: Optional[str]) -> Optional[str]:
    """Resolve a prioritized_action's sku_title to a product_key for grounding."""
    if not sku_title:
        return None
    _, per_sku = _ask_report_root(report)
    target = sku_title.strip().lower()
    for r in per_sku:
        if not isinstance(r, dict):
            continue
        name = str((r.get("identity") or {}).get("name") or r.get("sku_title") or "").strip().lower()
        if name and (name == target or target in name or name in target):
            return r.get("product_key") or r.get("sku_key")
    return None


# Outreach drafts target EXTERNAL channels (the third-party sources AI cites),
# not the merchant's own page — agents discount first-party copy. The system
# prompt is chosen by channel kind so the draft is the right artifact to SEND.
_OUTREACH_SYSTEM_PROMPTS: Dict[str, str] = {
    "review": (
        "You are Pivota's outreach assistant. Draft a concise, genuine note the "
        "merchant can send to the TARGET CHANNEL — an independent review site/"
        "aggregator that AI engines trust — to earn an honest review or get listed "
        "in the right category roundup. Lead with what makes the product genuinely "
        "notable (from CONTEXT), offer samples and any real proof, and ask for "
        "review/inclusion. Specific, warm, non-spammy, under 150 words."
    ),
    "editorial": (
        "You are Pivota's outreach assistant. Draft a short pitch email to the "
        "editorial desk at the TARGET CHANNEL (a publisher AI cites) proposing the "
        "merchant's product for a relevant roundup or feature. Hook + why it fits "
        "their audience + offer samples/data. Specific, non-spammy, under 150 words."
    ),
    "creator": (
        "You are Pivota's outreach assistant. Draft a short, friendly outreach "
        "message to a creator / KOL (on the TARGET CHANNEL/platform) proposing a "
        "product gifting or review collaboration. Personal, specific to the "
        "product's hook from CONTEXT, low-pressure. Under 120 words."
    ),
    "community": (
        "You are Pivota's community assistant. Draft an authentic, value-first post "
        "for the TARGET CHANNEL (e.g. a relevant subreddit or forum) that genuinely "
        "helps people choosing in this category and mentions the product only where "
        "truly relevant — NOT an ad. Tell the merchant to disclose their brand "
        "affiliation honestly. Under 160 words."
    ),
    "retailer": (
        "You are Pivota's distribution assistant. Draft a short, concrete checklist "
        "to get the product listed and win the buy-path on the TARGET CHANNEL (a "
        "marketplace AI routes buyers to): accurate title/images/availability, "
        "reviews to build, and a why-buy-direct reason for the brand's own page. "
        "Under 140 words."
    ),
}
_OUTREACH_SYSTEM_PROMPTS["default"] = _OUTREACH_SYSTEM_PROMPTS["review"]

# Map a channel lever / type to the outreach kind that picks the draft prompt.
_OUTREACH_KIND_BY_LEVER: Dict[str, str] = {
    "editorial_outreach": "editorial",
    "creator_partnership": "creator",
    "community": "community",
    "wholesale_onboarding": "retailer",
    "marketplace_listing": "retailer",
    "research": "review",
}
_OUTREACH_KIND_BY_TYPE: Dict[str, str] = {
    "review_aggregator": "review",
    "review_site": "review",
    "creator_platform": "creator",
    "editorial": "editorial",
    "beauty": "editorial",
    "community": "community",
    "forum": "community",
    "marketplace": "retailer",
    "retailer": "retailer",
}


def _outreach_kind(lever: Optional[str], channel_type: Optional[str]) -> str:
    if channel_type and channel_type in _OUTREACH_KIND_BY_TYPE:
        return _OUTREACH_KIND_BY_TYPE[channel_type]
    if lever and lever in _OUTREACH_KIND_BY_LEVER:
        return _OUTREACH_KIND_BY_LEVER[lever]
    return "default"


class MerchantAuditActionStartRequest(BaseModel):
    """POST /actions/start body — graduate a 'Start here' move (or an external
    channel outreach) into a tracked, Pivota-drafted follow-up."""

    run_id: str = Field(..., min_length=8, max_length=128)
    headline: str = Field(..., min_length=3, max_length=400)
    first_move: Optional[str] = Field(default=None, max_length=600)
    sku_title: Optional[str] = Field(default=None, max_length=400)
    growth_phase: Optional[str] = Field(default=None, max_length=64)
    primary_gap: Optional[str] = Field(default=None, max_length=64)
    # External-channel outreach context. When channel_host/channel_lever is set,
    # we draft the outreach artifact to SEND to that third-party source (pitch /
    # review request / community post / KOL DM) instead of first-party copy.
    channel_host: Optional[str] = Field(default=None, max_length=256)
    channel_lever: Optional[str] = Field(default=None, max_length=64)
    channel_type: Optional[str] = Field(default=None, max_length=64)
    query: Optional[str] = Field(default=None, max_length=300)


@router.post("/actions/start")
async def start_merchant_audit_action(
    body: MerchantAuditActionStartRequest,
    merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Create a tracked follow-up task for an audit action and (best-effort) draft
    its deliverable. Cross-tenant safe; idempotent on (lever, headline)."""
    run = await fetch_audit_run_by_id(run_id=body.run_id)
    if not run or run.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=404, detail="Audit run not found.")
    report = run.get("report_jsonb")
    if not isinstance(report, dict):
        raise HTTPException(
            status_code=409, detail="This audit doesn't have a report to act on yet.",
        )

    from db.merchant_tasks import (
        find_pending_supersede_candidates,
        record_task_created,
    )

    is_outreach = bool(body.channel_host or body.channel_lever)
    lever = "outreach" if is_outreach else ((body.growth_phase or "audit_action").strip() or "audit_action")
    title = body.headline.strip()[:300]

    existing = await find_pending_supersede_candidates(
        merchant_id=merchant_id, lever=lever, title=title,
    )
    if existing:
        ev = existing[0].get("evidence_jsonb") or existing[0].get("evidence") or {}
        prior_draft = ev.get("draft") if isinstance(ev, dict) else None
        return {
            "status": "exists",
            "task_id": existing[0].get("task_id"),
            "draft": prior_draft,
            "credits_charged": 0,
        }

    # Metered draft of the deliverable, grounded in this action's SKU.
    # Billing invariant (charged iff delivered): CHARGE FIRST, generate
    # second, refund if generation fails. The previous order (generate →
    # charge, swallowing InsufficientCreditsError) handed out free drafts
    # whenever the balance moved between the pre-check and the charge.
    draft: Optional[str] = None
    charged = 0
    product_key = _action_product_key_by_title(report, body.sku_title)
    context = _build_ask_context(report, product_key)
    cost_credits, _ = estimate_probe_credits(_ASK_PROBE_SPEC)
    can_draft = bool(context)
    draft_idem = "action_draft:" + hashlib.sha256(
        "|".join([merchant_id, body.run_id, title, body.channel_host or ""]).encode("utf-8")
    ).hexdigest()
    if can_draft and cost_credits > 0:
        try:
            res = await consume_credits(
                merchant_id, "prompt", draft_idem, probes=_ASK_PROBE_SPEC,
            )
            charged = int(res.get("credits") or 0)
        except InsufficientCreditsError:
            can_draft = False  # can't pay for a draft — still create the task

    if can_draft:
        ctx_json = json.dumps(context, default=str)[:_ASK_CONTEXT_MAX_CHARS]
        if is_outreach:
            system_prompt = _OUTREACH_SYSTEM_PROMPTS[
                _outreach_kind(body.channel_lever, body.channel_type)
            ]
            user_message = (
                f"CONTEXT (the merchant's audit):\n{ctx_json}\n\n"
                + (f"TARGET CHANNEL: {body.channel_host}\n" if body.channel_host else "")
                + (f"CHANNEL TYPE: {body.channel_type}\n" if body.channel_type else "")
                + (f"SHOPPER QUERY: {body.query.strip()}\n" if body.query else "")
                + 'Draft the outreach to send. Return JSON {"answer": "..."}.'
            )
        else:
            system_prompt = _ACTION_DRAFT_SYSTEM_PROMPT
            user_message = (
                f"CONTEXT (the merchant's audit):\n{ctx_json}\n\nACTION: {title}\n"
                + (f"FIRST MOVE: {body.first_move.strip()}\n" if body.first_move else "")
                + 'Draft the deliverable to execute this action. Return JSON {"answer": "..."}.'
            )
        try:
            draft = await answer_grounded_question(
                system_prompt=system_prompt, user_message=user_message,
            )
        except DeepseekProbeError as exc:
            logger.warning("action draft failed (run=%s): %s", body.run_id, exc)
            draft = None
        if not draft and charged > 0:
            # Charged but nothing delivered — refund (idempotent per action).
            try:
                await refund_credits(
                    merchant_id, "prompt", charged,
                    source_event_id=f"refund:{draft_idem}",
                )
                charged = 0
            except Exception:  # noqa: BLE001 — surface, don't mask the miss
                logger.exception(
                    "action draft refund failed merchant_id=%s run=%s — "
                    "RECONCILE MANUALLY", merchant_id, body.run_id,
                )

    task_body = (body.first_move or "").strip() or title
    if draft:
        task_body = (f"{task_body}\n\n— Pivota draft —\n{draft}")[:4000]

    task_id = await record_task_created(
        merchant_id=merchant_id,
        title=title,
        body=task_body,
        severity="high",
        lever=lever,
        parent_audit_run_id=body.run_id,
        evidence={
            "kind": "outreach" if is_outreach else "audit_action",
            "headline": title,
            "first_move": body.first_move,
            "growth_phase": body.growth_phase,
            "primary_gap": body.primary_gap,
            "sku_title": body.sku_title,
            "product_key": product_key,
            "channel_host": body.channel_host,
            "channel_lever": body.channel_lever,
            "channel_type": body.channel_type,
            "query": body.query,
            "draft": draft,
        },
    )
    if not task_id:
        raise HTTPException(status_code=500, detail="Could not create the follow-up task.")
    return {"status": "success", "task_id": task_id, "draft": draft, "credits_charged": charged}
