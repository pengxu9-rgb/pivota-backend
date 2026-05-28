"""Phase 2.3 — async audit_run endpoints.

The new canonical surface for the AI Commerce Readiness audit. The
legacy `/api/merchant-center/audit/ai-commerce-readiness` route stays
synchronous for backwards compat (P2.4 will turn it into a poll-then-
202 shim); these endpoints are the async + idempotent path.

Endpoints:
  POST   /api/audits           — enqueue a new audit run; returns 202
  GET    /api/audits/{run_id}  — poll for current state + partial result
  POST   /api/audits/{run_id}/cancel — request cancellation
  GET    /api/audits           — list recent runs for a merchant

Auth model (this PR):
  - All endpoints accept merchant JWT via Depends(get_current_merchant).
  - POST /api/audits enforces body.merchant_id == auth_merchant_id (so
    a merchant can't enqueue audits for another tenant). BD-employee
    submission for cold-start prospect audits is a follow-up — this
    PR keeps the auth model identical to the legacy route.

Idempotency:
  - POST /api/audits computes services.idempotency.compute_audit_idempotency_key
    and dedupes against in-flight runs (any active stage). Same body
    submitted twice within the 5-minute window returns the SAME
    run_id (200 OK with `idempotent_replay: true`). Different bodies
    enqueue separately.
  - `force=true` skips the dedupe check (same as the legacy route's
    "I really want to re-audit now" path).
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db.database import database
from db.merchant_audit_runs import (
    ACTIVE_STAGES,
    STAGE_QUEUED,
    cancel_audit_run,
    enqueue_audit_run_with_replay,
    fetch_audit_run_by_id,
    find_in_flight_by_idempotency_key,
    recent_runs_for_merchant,
)
from routes.merchant_audit_routes import _check_audit_rate_limit
from services.idempotency import compute_audit_idempotency_key
from services.merchant_credit_balance_service import (
    InsufficientCreditsError,
    credit,
    debit,
    get_balance,
)
from services.provider_credit_rates import (
    credits_for_probe,
    provider_default_grounded,
    provider_probe_cost_usd,
    provider_prompt_fraction,
)
from utils.auth import get_current_merchant

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/audits",
    tags=["audits-async"],
)


# =====================================================================
# Request / response schemas
# =====================================================================


class CreateAuditRequest(BaseModel):
    """POST /api/audits body. Same product reference shape as the
    legacy endpoint — list of `product_key` strings the merchant
    owns. Subject type defaults to merchant self-audit."""

    merchant_id: str = Field(
        ...,
        description=(
            "The tenant the audit runs against. Must match the auth "
            "context's merchant_id for merchant-tier callers."
        ),
    )
    product_keys: List[str] = Field(
        ..., min_length=1, max_length=5,
        description="1–5 product_key values from catalog_products.",
    )
    subject_type: str = Field(
        "merchant",
        description=(
            "merchant (self-audit) or cold_start (BD prospect). "
            "BD-employee auth required for cold_start."
        ),
    )
    force: bool = Field(
        False,
        description=(
            "When True, skips the in-flight idempotency dedupe and "
            "always enqueues a new run."
        ),
    )
    prompts_per_sku: int = Field(
        40,
        ge=1,
        le=200,
        description="Prompt fan-out estimate used for credit preview.",
    )
    custom_prompts: Optional[List[str]] = Field(
        default=None,
        max_length=10,
        description="Merchant-input prompt slots; each consumes prompt credits.",
    )
    providers: Optional[List[str]] = Field(
        default=None,
        max_length=4,
        description="LLM providers requested for the audit.",
    )


class AuditPreviewScope(BaseModel):
    sku_keys: Optional[List[str]] = Field(default=None, max_length=50)
    select_top_n_by_revenue: Optional[int] = Field(default=None, ge=1, le=50)


class AuditPreviewRequest(BaseModel):
    merchant_id: str
    scope: AuditPreviewScope
    prompts_per_sku: int = Field(default=40, ge=1, le=200)
    custom_prompts: Optional[List[str]] = Field(default=None, max_length=10)
    providers: Optional[List[str]] = Field(default=None, max_length=4)


class AuditRunCreated(BaseModel):
    """POST /api/audits 202 response."""

    run_id: str
    stage: str
    idempotent_replay: bool = False


class AuditRunDetail(BaseModel):
    """GET /api/audits/{run_id} response. Mirrors the canonical
    fetch_audit_run_by_id shape — see db/merchant_audit_runs.py."""

    run_id: str
    merchant_id: Optional[str]
    subject_type: Optional[str]
    stage: Optional[str]
    stage_updated_at: Optional[str]
    requested_at: Optional[str]
    completed_at: Optional[str]
    cancelled_at: Optional[str]
    product_keys: List[str]
    verdict_labels: List[str]
    visibility_score_avg: Optional[int]
    attribution_score_avg: Optional[int]
    category_visibility_score_avg: Optional[int]
    audited_via_pivota_canonical: List[str]
    partial_result_jsonb: Optional[Dict[str, Any]]
    report_jsonb: Optional[Dict[str, Any]]
    cost_summary_jsonb: Optional[Dict[str, Any]]
    error_jsonb: Optional[Dict[str, Any]]
    error_message: Optional[str]
    idempotency_key: Optional[str]


# =====================================================================
# Helpers
# =====================================================================


async def _missing_product_keys_for_merchant(
    *, merchant_id: str, product_keys: List[str],
) -> List[str]:
    """Return the subset of `product_keys` that do NOT exist for this
    merchant in catalog_products. Empty list ⇒ every key is valid +
    owned and the audit can be enqueued.

    Filters on `merchant_id == :merchant_id` so a product_key that
    exists for a DIFFERENT merchant still reports as missing — the
    function is a cross-tenant guard, not just an existence check.
    """
    if not product_keys:
        return []
    from db.catalog import catalog_products
    from db.database import database
    from sqlalchemy.sql import select as _select

    rows = await database.fetch_all(
        _select(catalog_products.c.product_key).where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.product_key.in_(list(product_keys)),
        )
    )
    found = {str(r[0]) for r in rows}
    return [k for k in product_keys if k not in found]


# =====================================================================
# Endpoints
# =====================================================================


_PREVIEW_CACHE_TTL_SECONDS = 60
_PREVIEW_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _normalize_nonempty(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values or []:
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _normalize_providers(values: Optional[List[str]]) -> List[str]:
    providers = [
        value.lower() for value in _normalize_nonempty(values)
    ]
    return providers or ["gemini"]


def _credit_requirements(
    *,
    sku_count: int,
    prompts_per_sku: int,
    providers: List[str],
    custom_prompts: Optional[List[str]] = None,
) -> Dict[str, int]:
    audit_required, _usd_cogs = _audit_metering(
        sku_count=sku_count,
        prompts_per_sku=prompts_per_sku,
        providers=providers,
    )
    return {
        "audit": int(audit_required),
        "prompt": len(_normalize_nonempty(custom_prompts)),
        "execution": 0,
    }


def _audit_metering(
    *,
    sku_count: int,
    prompts_per_sku: int,
    providers: List[str],
) -> Tuple[int, Decimal]:
    total_prompts = int(sku_count) * int(prompts_per_sku)
    credits_total = Decimal("0")
    usd_cogs_total = Decimal("0")
    for provider in providers:
        fraction = provider_prompt_fraction(provider)
        probe_count = int(math.ceil(float(Decimal(total_prompts) * fraction)))
        if probe_count <= 0:
            continue
        grounded = provider_default_grounded(provider)
        per_probe_credits = Decimal(str(credits_for_probe(
            provider,
            grounded=grounded,
        )))
        credits_total += per_probe_credits * probe_count
        usd_cogs_total += (
            provider_probe_cost_usd(provider, grounded=grounded)
            * Decimal(probe_count)
        )
    return int(math.ceil(float(credits_total))), usd_cogs_total


def _balance_public_shape(balance: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "credits": int(balance.get("credits") or 0),
        "allowance_credits": int(balance.get("allowance_credits") or 0),
        "plan_tier": str(balance.get("plan_tier") or "free"),
    }


def _credit_gaps(
    *, requirements: Dict[str, int], balance: Dict[str, Any],
) -> List[Dict[str, Any]]:
    required = sum(int(value) for value in requirements.values())
    available = int(balance.get("credits") or 0)
    if required <= available:
        return []
    return [{
        "kind": "credits",
        "required": required,
        "available": available,
        "short": required - available,
    }]


def _strip_brand_facing_internal_money(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_brand_facing_internal_money(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: Dict[str, Any] = {}
    for key, item in value.items():
        key_str = str(key)
        key_lower = key_str.lower()
        if (
            "usd" in key_lower
            or key_str in {"credit_to_usd", "provider_cost_fraction"}
        ):
            continue
        out[key] = _strip_brand_facing_internal_money(item)
    return out


def _preview_cache_key(
    *,
    merchant_id: str,
    sku_keys: List[str],
    prompts_per_sku: int,
    providers: List[str],
    custom_prompts: Optional[List[str]],
) -> str:
    raw = {
        "merchant_id": merchant_id,
        "sku_keys": sorted(sku_keys),
        "prompts_per_sku": int(prompts_per_sku),
        "providers": sorted(providers),
        "custom_prompts": _normalize_nonempty(custom_prompts),
    }
    return hashlib.sha256(repr(raw).encode("utf-8")).hexdigest()


async def _resolve_preview_sku_keys(
    *, merchant_id: str, scope: AuditPreviewScope,
) -> List[str]:
    requested = _normalize_nonempty(scope.sku_keys)
    top_n = scope.select_top_n_by_revenue
    if bool(requested) == bool(top_n):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "scope must include exactly one of sku_keys or "
                "select_top_n_by_revenue"
            ),
        )
    if requested:
        rows = await database.fetch_all(
            """
            SELECT sku_key
              FROM catalog_skus
             WHERE merchant_id = :merchant_id
               AND sku_key = ANY(:sku_keys)
            """,
            {"merchant_id": merchant_id, "sku_keys": requested},
        )
        found = {str(dict(row).get("sku_key") or "") for row in rows or []}
        missing = [key for key in requested if key not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": (
                        f"{len(missing)} SKU(s) not found for this merchant."
                    ),
                    "missing_sku_keys": missing,
                },
            )
        return requested

    rows = await database.fetch_all(
        """
        SELECT s.sku_key
          FROM catalog_skus s
          LEFT JOIN catalog_offers o ON o.sku_key = s.sku_key
         WHERE s.merchant_id = :merchant_id
         GROUP BY s.sku_key, s.updated_at
         ORDER BY MAX(COALESCE(
                    o.merchant_effective_price,
                    o.estimated_best_price,
                    o.list_price,
                    0
                  )) DESC,
                  s.updated_at DESC NULLS LAST,
                  s.sku_key ASC
         LIMIT :limit
        """,
        {"merchant_id": merchant_id, "limit": int(top_n or 0)},
    )
    resolved = [
        str(dict(row).get("sku_key") or "").strip()
        for row in rows or []
        if str(dict(row).get("sku_key") or "").strip()
    ]
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No SKUs found for this merchant.",
        )
    return resolved


async def _build_preview(
    *,
    merchant_id: str,
    sku_keys: List[str],
    prompts_per_sku: int,
    custom_prompts: Optional[List[str]],
    providers: List[str],
) -> Dict[str, Any]:
    cache_key = _preview_cache_key(
        merchant_id=merchant_id,
        sku_keys=sku_keys,
        prompts_per_sku=prompts_per_sku,
        providers=providers,
        custom_prompts=custom_prompts,
    )
    now = time.time()
    cached = _PREVIEW_CACHE.get(cache_key)
    if cached and now - cached[0] <= _PREVIEW_CACHE_TTL_SECONDS:
        cost_part = dict(cached[1])
    else:
        sku_count = len(sku_keys)
        total_prompts = sku_count * int(prompts_per_sku)
        estimated_audit_credits, _usd_cogs = _audit_metering(
            sku_count=sku_count,
            prompts_per_sku=prompts_per_sku,
            providers=providers,
        )
        prompts_cached = int(total_prompts * 0.2)
        cache_hit_rate = (
            round(prompts_cached / total_prompts, 4)
            if total_prompts else 0.0
        )
        cost_part = {
            "audit_run_id_preview": f"preview_{cache_key[:16]}",
            "sku_count": sku_count,
            "prompts_per_sku": int(prompts_per_sku),
            "total_prompts": total_prompts,
            "custom_prompt_slots_used": len(_normalize_nonempty(custom_prompts)),
            "estimated_cache_savings": {
                "prompts_cached": prompts_cached,
                "cache_hit_rate": cache_hit_rate,
            },
            "providers": providers,
            "estimated_audit_credits": estimated_audit_credits,
            "estimated_prompt_credits": len(_normalize_nonempty(custom_prompts)),
            "estimated_execution_credits": 0,
        }
        _PREVIEW_CACHE[cache_key] = (now, cost_part)

    balance = await get_balance(merchant_id)
    requirements = _credit_requirements(
        sku_count=int(cost_part["sku_count"]),
        prompts_per_sku=int(cost_part["prompts_per_sku"]),
        providers=list(cost_part["providers"]),
        custom_prompts=custom_prompts,
    )
    gaps = _credit_gaps(requirements=requirements, balance=balance)
    return {
        **cost_part,
        "merchant_id": merchant_id,
        "current_balance": _balance_public_shape(balance),
        "sufficient": not gaps,
        "gaps": gaps,
    }


@router.post("/preview")
async def preview_audit_run(
    body: AuditPreviewRequest,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Pure pre-flight cost/credit preview for SKU audit v3."""
    if body.merchant_id != auth_merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "merchant_id in body must match the authenticated "
                "merchant."
            ),
        )
    sku_keys = await _resolve_preview_sku_keys(
        merchant_id=body.merchant_id,
        scope=body.scope,
    )
    try:
        return await _build_preview(
            merchant_id=body.merchant_id,
            sku_keys=sku_keys,
            prompts_per_sku=body.prompts_per_sku,
            custom_prompts=body.custom_prompts,
            providers=_normalize_providers(body.providers),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AuditRunCreated,
)
async def create_audit_run(
    body: CreateAuditRequest,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> AuditRunCreated:
    """Enqueue an audit. The worker (P2.2) picks it up within the
    next 10s tick and drives it through the async lifecycle.
    Idempotent within a 5-minute window unless `force=true`.
    """
    # Cross-tenant guard: a merchant can only audit themselves.
    # Subject_type=cold_start is reserved for a future BD-employee
    # endpoint and rejected here.
    if body.subject_type != "merchant":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "subject_type='cold_start' requires BD-employee auth, "
                "which this endpoint does not yet support."
            ),
        )
    if body.merchant_id != auth_merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "merchant_id in body must match the authenticated "
                "merchant — cross-tenant audit submission is not "
                "permitted."
            ),
        )

    # P1-2: validate that every product_key is owned by the
    # authenticated merchant BEFORE enqueueing. The legacy
    # `/ai-commerce-readiness` route did the equivalent check inline
    # for (platform, source_product_id) refs; the async POST took
    # opaque product_key strings and never validated ownership, so
    # a merchant could enqueue a run referencing another merchant's
    # product_keys and the worker would silently audit on the empty
    # subset (or fewer products than requested) 30s later. Worse,
    # the daily-cap counter (P1-1) would still tick on the invalid
    # enqueue. Validate at the route so the merchant sees a 422
    # immediately, not a no-op audit much later.
    missing = await _missing_product_keys_for_merchant(
        merchant_id=auth_merchant_id,
        product_keys=body.product_keys,
    )
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    "One or more product_keys are not owned by this "
                    "merchant (or do not exist in catalog_products)."
                ),
                "missing_product_keys": sorted(missing),
            },
        )

    # Idempotency dedupe (unless force=true).
    debit_idempotency_key: Optional[str]
    if not body.force:
        idempotency_key = compute_audit_idempotency_key(
            merchant_id=body.merchant_id,
            product_keys=body.product_keys,
            subject_type=body.subject_type,
        )
        existing = await find_in_flight_by_idempotency_key(
            idempotency_key=idempotency_key,
        )
        if existing:
            logger.info(
                "audit_runs: idempotent replay merchant=%s run_id=%s",
                body.merchant_id, existing,
            )
            return AuditRunCreated(
                run_id=existing,
                stage=STAGE_QUEUED,  # safe default; GET returns truth
                idempotent_replay=True,
            )
        debit_idempotency_key = idempotency_key
    else:
        idempotency_key = None
        debit_idempotency_key = compute_audit_idempotency_key(
            merchant_id=body.merchant_id,
            product_keys=body.product_keys,
            subject_type=body.subject_type,
        )

    providers = _normalize_providers(body.providers)
    try:
        audit_required, audit_usd_cogs = _audit_metering(
            sku_count=len(_normalize_nonempty(body.product_keys)),
            prompts_per_sku=body.prompts_per_sku,
            providers=providers,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    requirements = {
        "audit": audit_required,
        "prompt": len(_normalize_nonempty(body.custom_prompts)),
        "execution": 0,
    }
    balance = await get_balance(body.merchant_id)
    gaps = _credit_gaps(requirements=requirements, balance=balance)
    if gaps:
        gap = gaps[0]
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_credits",
                "kind": gap["kind"],
                "required": gap["required"],
                "available": gap["available"],
                "preview_url": "/api/audits/preview",
            },
        )

    # Preview-only quota remains active for free-tier balances. Paid tiers
    # are credit-gated only.
    if str(balance.get("plan_tier") or "free") == "free":
        await _check_audit_rate_limit(body.merchant_id)

    debited: List[Tuple[str, int, bool, Decimal]] = []
    try:
        audit_required = int(requirements["audit"])
        if audit_required:
            audit_debit = await debit(
                body.merchant_id,
                "audit",
                audit_required,
                idempotency_key=debit_idempotency_key,
                usd_cogs=audit_usd_cogs,
            )
            debited.append((
                "audit",
                audit_required,
                bool(audit_debit.get("replay")),
                audit_usd_cogs,
            ))
        prompt_required = int(requirements["prompt"])
        if prompt_required:
            prompt_debit = await debit(
                body.merchant_id,
                "prompt",
                prompt_required,
                idempotency_key=debit_idempotency_key,
                usd_cogs=0,
            )
            debited.append((
                "prompt",
                prompt_required,
                bool(prompt_debit.get("replay")),
                Decimal("0"),
            ))

        # Use origin/main's race-safe enqueue (returns run_id +
        # was_existing for idempotent-replay signalling) inside Brief 3's
        # credit-gated try block, so a concurrent POST that wins the race
        # is reported as idempotent_replay without double-charging.
        run_id, was_existing = await enqueue_audit_run_with_replay(
            merchant_id=body.merchant_id,
            product_keys=body.product_keys,
            subject_type=body.subject_type,
            idempotency_key=idempotency_key,
            requested_by_user_id=auth_merchant_id,
        )
    except InsufficientCreditsError as exc:
        for kind, amount, replay, usd_cogs in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_credits",
                "kind": exc.kind,
                "required": exc.required,
                "available": exc.available,
                "preview_url": "/api/audits/preview",
            },
        ) from exc
    except Exception:
        for kind, amount, replay, usd_cogs in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                )
        raise
    if not run_id:
        for kind, amount, replay, usd_cogs in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                )
        # Persistence layer rejected the insert. Most likely cause is
        # the DB being unavailable; surface a 503 so callers retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Failed to enqueue audit run; persistence layer "
                "rejected the insert. Retry shortly."
            ),
        )

    logger.info(
        "audit_runs: enqueued merchant=%s run_id=%s products=%d "
        "force=%s race_replay=%s",
        body.merchant_id, run_id, len(body.product_keys), body.force,
        was_existing,
    )
    return AuditRunCreated(
        run_id=run_id, stage=STAGE_QUEUED,
        idempotent_replay=was_existing,
    )


@router.get("/{run_id}")
async def get_audit_run(
    run_id: str,
    audience: Optional[str] = None,
    auth_merchant_id: str = Depends(get_current_merchant),
):
    """Fetch the current state of an audit run.

    Default (no `?audience` param): returns the canonical shape with
    stage, partial_result, report (when terminal), cost summary,
    and error info — matches the P2.3 contract.

    With `?audience=X`: returns the cached P4.5 projection for that
    audience. Valid values: employee_bd, merchant, internal_ops,
    pivota_pdp_feed, frontend_agent_feed. If the projection hasn't
    been built yet (audit not at stage=completed), returns 409
    Conflict pointing the caller at the no-audience read.
    """
    row = await fetch_audit_run_by_id(run_id=run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run {run_id} not found",
        )
    if row.get("merchant_id") != auth_merchant_id:
        # Don't leak existence — same response as not found.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run {run_id} not found",
        )

    # P4.5: if the caller asked for a specific audience projection,
    # return the cached shape (or 409 if not yet built).
    if audience is not None:
        from db.audit_evidence import (
            MERCHANT_ALLOWED_AUDIENCES,
            VALID_AUDIENCES,
            fetch_projection,
        )
        if audience not in VALID_AUDIENCES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unknown audience {audience!r}. Valid values: "
                    f"{sorted(VALID_AUDIENCES)}"
                ),
            )
        # P0-4: merchant JWTs may only read the merchant projection.
        # employee_bd / internal_ops / pivota_pdp_feed /
        # frontend_agent_feed carry internal data (cost detail, full
        # raw evidence, ops dashboards, etc.) and require an
        # employee/admin auth path that this route does not yet
        # expose. Codex review surfaced this as P0-4 — before the
        # fix, a merchant could fetch their own audit's internal_ops
        # projection by adding `?audience=internal_ops`.
        if audience not in MERCHANT_ALLOWED_AUDIENCES:
            # 403, not 404. The merchant DOES own this run, so
            # hiding existence is misleading — they just don't
            # have role permission for this audience shape.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Audience {audience!r} requires employee or "
                    "admin authentication. Merchant JWTs may only "
                    "read 'merchant' (or omit ?audience for the "
                    "canonical shape)."
                ),
            )
        proj = await fetch_projection(
            audit_run_id=run_id, audience=audience,
        )
        if proj is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": (
                        f"Projection for audience {audience!r} not yet "
                        f"built. Audit run may not be at stage=completed."
                    ),
                    "current_stage": row.get("stage"),
                    "fallback": (
                        f"GET /api/audits/{run_id} (no audience) "
                        f"returns the canonical shape."
                    ),
                },
            )
        return _strip_brand_facing_internal_money(proj.get("payload_jsonb"))

    return AuditRunDetail(**_strip_brand_facing_internal_money(dict(row)))


@router.post(
    "/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_audit_run_endpoint(
    run_id: str,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Request cancellation. Sets cancelled_at; the worker checks
    this between stages and finalizes to STAGE_CANCELLED on the
    next transition. Returns 202 because cancellation is an ASK,
    not a guarantee — the worker may already be mid-stage."""
    row = await fetch_audit_run_by_id(run_id=run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run {run_id} not found",
        )
    if row.get("merchant_id") != auth_merchant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run {run_id} not found",
        )

    current_stage = row.get("stage")
    if current_stage not in ACTIVE_STAGES:
        # Already terminal — cancellation is a no-op. Return the
        # current stage so callers know there's nothing to wait for.
        return {
            "run_id": run_id,
            "cancellation_requested": False,
            "current_stage": current_stage,
            "reason": (
                f"Run already in terminal stage {current_stage!r}; "
                "no cancellation needed."
            ),
        }

    ok = await cancel_audit_run(run_id=run_id)
    return {
        "run_id": run_id,
        "cancellation_requested": ok,
        "current_stage": current_stage,
    }


@router.get("", response_model=List[Dict[str, Any]])
async def list_audit_runs(
    limit: int = 20,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> List[Dict[str, Any]]:
    """List the most recent audit runs for this merchant. Trend-
    friendly fields only — fetch a specific run via GET /api/audits/
    {run_id} for the full report payload."""
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100",
        )
    rows = await recent_runs_for_merchant(
        merchant_id=auth_merchant_id, limit=limit,
    )
    return _strip_brand_facing_internal_money(rows)
