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
from pydantic import BaseModel, Field, model_validator

from config.settings import settings
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
from services.agent_center_bd_report_service import (
    _resolve_audit_verify_providers,
    sanitize_report_for_merchant,
)
from services.idempotency import compute_audit_idempotency_key
from services.merchant_audit_readiness import assess_merchant_audit_readiness
from services.merchant_credit_balance_service import (
    InsufficientCreditsError,
    MissingVerifiedPaymentMethodError,
    OveragePaymentBlockedError,
    OveragePaymentFailedError,
    credit,
    debit,
    get_balance,
    require_verified_payment_method,
)
from services.coverage_profiles import (
    default_coverage_profile,
    resolve_coverage_profile,
    resolve_provider_models,
)
from services.provider_credit_rates import (
    provider_default_grounded,
    provider_prompt_fraction,
)
from services.credit_consumption_service import estimate_probe_credits
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
        default_factory=list, max_length=50,
        description=(
            "1–50 product_key values from catalog_products. Optional when "
            "`sku_keys` is supplied — the two are alternatives."
        ),
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
    prompts_per_sku: Optional[int] = Field(
        None,
        ge=1,
        le=200,
        description=(
            "Explicit prompt fan-out override (internal/testing knob). Omit "
            "to use the tier's budget — standard 40, deep 80. When omitted "
            "the worker resolves the count from audit_tier, so a deep run "
            "never probes a standard budget against a deep-pinned basis."
        ),
    )
    audit_tier: str = Field(
        "standard",
        max_length=16,
        description=(
            "Audit depth tier: 'standard' (40 prompts/SKU/provider) or "
            "'deep' (Deep Landscape Scan, 80). Deep requires "
            "AUDIT_DEEP_TIER_ENABLED and is rejected (422) when the flag is "
            "off — never silently downgraded at launch, so the merchant is "
            "never billed for a tier the worker won't run."
        ),
    )
    custom_prompts: Optional[List[str]] = Field(
        default=None,
        max_length=10,
        description="Merchant-input prompt slots; each consumes prompt credits.",
    )
    declared_competitors: Optional[List[str]] = Field(
        default=None,
        max_length=5,
        description=(
            "Deep tier only: up to 5 competitor brand names to anchor the "
            "competitive lane on, ahead of the automatic cited-competitor "
            "harvest. Lets the audit target rivals AI answers don't cite yet "
            "(e.g. brands investing in AI visibility). Ignored on standard "
            "runs; sanitized server-side (own brand, junk, and prose-shaped "
            "names dropped). NOTE: a re-audit replays its pinned question "
            "set for comparability, so newly declared competitors take "
            "effect only with refresh_prompt_basis=true (or on a first "
            "deep audit)."
        ),
    )
    refresh_prompt_basis: bool = Field(
        False,
        description=(
            "Regenerate the pinned measurement basis for this run instead of "
            "replaying the prior run's question set. Resets re-audit "
            "comparability (a new baseline starts); required for newly "
            "declared competitors to take effect on a re-audit."
        ),
    )
    coverage_profile: str = Field(
        default_factory=default_coverage_profile,
        max_length=64,
        description="Configured provider coverage profile for this audit.",
    )
    providers: Optional[List[str]] = Field(
        default=None,
        max_length=4,
        description=(
            "Legacy explicit provider override. Prefer coverage_profile."
        ),
    )
    model_overrides: Optional[Dict[str, str]] = Field(
        default=None,
        max_length=4,
        description=(
            "Optional per-provider model override, e.g. "
            "{\"chatgpt\": \"gpt-5.5-mini\"}. Absent uses profile defaults."
        ),
    )
    sku_keys: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description=(
            "Alternative to product_keys: `platform:source_product_id` "
            "composites (what the merchant AI-Readiness portal selects). "
            "Resolved to product_keys server-side, scoped to the merchant."
        ),
    )

    @model_validator(mode="after")
    def _require_products_or_skus(self) -> "CreateAuditRequest":
        if not self.product_keys and not self.sku_keys:
            raise ValueError(
                "provide product_keys or sku_keys (1–50 products to audit)"
            )
        return self


class AuditPreviewScope(BaseModel):
    sku_keys: Optional[List[str]] = Field(default=None, max_length=50)
    select_top_n_by_revenue: Optional[int] = Field(default=None, ge=1, le=50)


class AuditPreviewRequest(BaseModel):
    merchant_id: str
    scope: AuditPreviewScope
    # None = the tier's budget (standard 40, deep 80) — mirrors
    # CreateAuditRequest so preview and launch always price the same count.
    prompts_per_sku: Optional[int] = Field(default=None, ge=1, le=200)
    audit_tier: str = Field(default="standard", max_length=16)
    custom_prompts: Optional[List[str]] = Field(default=None, max_length=10)
    coverage_profile: str = Field(
        default_factory=default_coverage_profile,
        max_length=64,
    )
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


async def _resolve_refs_to_product_keys(
    *, merchant_id: str, refs: List[str],
) -> List[str]:
    """Resolve audit product references to catalog `product_key`s for a merchant.

    The merchant AI-Readiness portal selects products and sends
    `platform:source_product_id` composites (single ':'), documenting that the
    backend resolves them server-side. A ref containing '::' (a minted
    `<product_key>::v::<variant_id>` sku_key) or no ':' is treated as an
    already-resolved `product_key` and validated for ownership. Raises 404
    listing any ref that does not resolve to a product the merchant owns —
    mirroring the (platform, source_product_id) ownership check the legacy
    /ai-commerce-readiness route performs.
    """
    cleaned = _normalize_nonempty(refs)
    if not cleaned:
        return []
    from db.catalog import catalog_products
    from sqlalchemy.sql import select as _select

    def _is_composite(ref: str) -> bool:
        return ":" in ref and "::" not in ref

    composite_refs = [r for r in cleaned if _is_composite(r)]
    bare_refs = [r for r in cleaned if not _is_composite(r)]

    resolved: List[str] = []
    missing: List[str] = []

    if composite_refs:
        pairs = [
            tuple(part.strip() for part in r.split(":", 1)) for r in composite_refs
        ]
        rows = await database.fetch_all(
            _select(
                catalog_products.c.product_key,
                catalog_products.c.platform,
                catalog_products.c.source_product_id,
            ).where(
                catalog_products.c.merchant_id == merchant_id,
                catalog_products.c.platform.in_([p for p, _ in pairs]),
                catalog_products.c.source_product_id.in_([s for _, s in pairs]),
            )
        )
        pair_to_key = {
            (
                str(dict(row).get("platform") or ""),
                str(dict(row).get("source_product_id") or ""),
            ): str(dict(row).get("product_key") or "")
            for row in rows or []
        }
        for ref, pair in zip(composite_refs, pairs):
            key = pair_to_key.get(pair)
            if key:
                resolved.append(key)
            else:
                missing.append(ref)

    if bare_refs:
        not_owned = set(
            await _missing_product_keys_for_merchant(
                merchant_id=merchant_id, product_keys=bare_refs,
            )
        )
        for ref in bare_refs:
            (missing if ref in not_owned else resolved).append(ref)

    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": (
                    f"{len(missing)} product reference(s) not found for "
                    "this merchant."
                ),
                "missing_refs": sorted(missing),
            },
        )

    seen: set = set()
    out: List[str] = []
    for key in resolved:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def _audit_readiness_platform(
    merchant_id: str, product_keys: Optional[List[str]] = None,
) -> str:
    """Platform to assess audit-readiness against: the dominant platform of the
    audited products (or the merchant's catalog when no keys are given).
    Defaults to 'shopify' when the catalog is empty.
    """
    from db.catalog import catalog_products
    from sqlalchemy.sql import func as _func, select as _select

    query = _select(
        catalog_products.c.platform,
        _func.count().label("n"),
    ).where(catalog_products.c.merchant_id == merchant_id)
    keys = _normalize_nonempty(product_keys or [])
    if keys:
        query = query.where(catalog_products.c.product_key.in_(keys))
    query = query.group_by(catalog_products.c.platform).order_by(
        _func.count().desc()
    )
    rows = await database.fetch_all(query)
    for row in rows or []:
        platform = str(dict(row).get("platform") or "").strip()
        if platform:
            return platform
    return "shopify"


async def _enforce_audit_readiness(
    *, merchant_id: str, product_keys: List[str], force: bool,
) -> None:
    """Raise 409 when the merchant's audit data isn't ready.

    A fresh merchant can launch before the async content-quality backfill
    finishes, which makes every SKU score `band=blocked` — a false "you're
    invisible" first impression. This blocks the run with a clear "still
    preparing, retry shortly" response instead. `force=true` bypasses (BD /
    test / fixture re-audits and callers that accept a partial run). No-op when
    ready. Read-only COUNT probe.
    """
    if force:
        return
    platform = await _audit_readiness_platform(merchant_id, product_keys)
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
                "1–2 minutes of a product sync — please try again shortly."
            ),
            "blocking_gaps": readiness.get("blocking_gaps") or [],
            "counts": readiness.get("counts") or {},
            "platform": platform,
            "retry_after_seconds": 60,
        },
    )


# ADR-005: the premium-provider *plan*-gate was removed. Premium providers
# (ChatGPT/Claude) are gated by credit BALANCE, not plan tier — they cost more
# credits per run (config/provider_credit_rates.json), which the metering +
# insufficient-credits gate already enforce. A free-plan merchant with enough
# credits can run them; `premium_providers()` is retained only to label the
# higher-cost providers for the cost preview / portal, not to block.


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


def _runnable_verify_providers(
    verify_providers: Optional[List[str]],
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Return verify providers that can actually run in this process.

    Billing rule: we only debit for work that will run. DeepSeek verify is
    backend-direct, so a missing key is knowable at pre-flight time; when it
    is absent, remove it from the billable/run context and stamp the skip.
    """
    runnable: List[str] = []
    skipped: List[Dict[str, str]] = []
    for provider in _normalize_nonempty(verify_providers):
        provider_id = provider.strip().lower()
        if provider_id == "deepseek" and not (
            settings.deepseek_api_key or ""
        ).strip():
            skipped.append({
                "provider": provider_id,
                "reason": "missing_deepseek_api_key",
            })
            continue
        runnable.append(provider_id)
    return runnable, skipped


def _resolve_audit_coverage(
    *,
    coverage_profile: Optional[str],
    providers: Optional[List[str]],
) -> Dict[str, Any]:
    return resolve_coverage_profile(
        coverage_profile=coverage_profile,
        providers=providers,
    )


def _resolve_audit_provider_models(
    *,
    providers: List[str],
    model_overrides: Optional[Dict[str, str]],
) -> Dict[str, Dict[str, Any]]:
    return resolve_provider_models(
        providers,
        model_overrides=model_overrides,
    )


def _resolve_request_audit_tier(value: Any) -> str:
    """Normalize the requested tier and enforce the deep-tier flag at the
    LAUNCH boundary: with AUDIT_DEEP_TIER_ENABLED off, a deep request is a
    422 — never a silent downgrade, so preview/billing/worker can't disagree
    about which tier actually ran. (The worker keeps its own degrade-to-
    standard fallback for hand-enqueued runs that bypass this route.)"""
    from config.settings import settings
    from services.prompt_basis import AUDIT_TIER_DEEP, normalize_audit_tier

    raw = str(value or "").strip().lower()
    tier = normalize_audit_tier(raw)
    if raw and raw != tier:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown audit_tier {raw!r}. Valid values: standard, deep.",
        )
    if tier == AUDIT_TIER_DEEP and not getattr(
        settings, "audit_deep_tier_enabled", False
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "audit_tier='deep' (Deep Landscape Scan) is not enabled on "
                "this environment."
            ),
        )
    return tier


def _effective_prompts_per_sku(
    explicit: Optional[int], audit_tier: str
) -> int:
    """The count everything downstream prices and runs: the explicit
    internal/testing override when provided, else the tier's budget. This is
    the route-side half of the worker's _launch_prompts_per_sku contract —
    the route no longer persists a defaulted 40 into launch options, so the
    tier budget is actually reachable (see _launch_prompts_per_sku CAUTION)."""
    from services.prompt_basis import prompts_per_sku_for_tier

    if explicit is not None:
        return max(1, int(explicit))
    return prompts_per_sku_for_tier(audit_tier)


def _credit_requirements(
    *,
    sku_count: int,
    prompts_per_sku: int,
    providers: List[str],
    verify_providers: Optional[List[str]] = None,
    verify_sample: Optional[Dict[str, Any]] = None,
    custom_prompts: Optional[List[str]] = None,
) -> Dict[str, int]:
    audit_required, _usd_cogs = _audit_metering(
        sku_count=sku_count,
        prompts_per_sku=prompts_per_sku,
        providers=providers,
        verify_providers=verify_providers,
        verify_sample=verify_sample,
    )
    return {
        "audit": int(audit_required),
        "prompt": len(_normalize_nonempty(custom_prompts)),
        "execution": 0,
    }


def _verify_probe_count_per_sku(
    *,
    prompts_per_sku: int,
    verify_sample: Optional[Dict[str, Any]],
) -> int:
    sample = verify_sample or {}
    try:
        fraction = float(sample.get("positive_fraction", 0.25))
    except (TypeError, ValueError):
        fraction = 0.25
    fraction = max(0.0, min(1.0, fraction))
    cap = int(math.ceil(max(0, int(prompts_per_sku)) * fraction))
    if fraction > 0 and int(prompts_per_sku) > 0:
        cap = max(1, cap)
    max_per_sku_raw = sample.get("max_per_sku")
    if max_per_sku_raw is not None:
        try:
            cap = min(cap, max(0, int(max_per_sku_raw)))
        except (TypeError, ValueError):
            pass
    return max(0, cap)


def _audit_metering(
    *,
    sku_count: int,
    prompts_per_sku: int,
    providers: List[str],
    verify_providers: Optional[List[str]] = None,
    verify_sample: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Decimal]:
    # Build the provider-probe set, then price it through the shared per-probe
    # estimator (credit_consumption_service) so audit and any future metered
    # workflow bill identically per probe. Model overrides intentionally do not
    # alter audit credits yet — each provider bills at its single config rate
    # (config/provider_credit_rates.json; grounded ChatGPT ~11.7 credits/probe
    # on its measured ~15k-token representative probe, #1506) until per-model
    # rate mapping becomes a separate productized follow-up.
    total_prompts = int(sku_count) * int(prompts_per_sku)
    probes: List[Tuple[str, int, bool]] = []
    for provider in providers:
        fraction = provider_prompt_fraction(provider)
        probe_count = int(math.ceil(float(Decimal(total_prompts) * fraction)))
        if probe_count <= 0:
            continue
        probes.append((provider, probe_count, provider_default_grounded(provider)))

    verify_count_per_sku = _verify_probe_count_per_sku(
        prompts_per_sku=prompts_per_sku,
        verify_sample=verify_sample,
    )
    verify_probe_count = int(sku_count) * verify_count_per_sku
    if verify_probe_count > 0:
        for provider in _normalize_nonempty(verify_providers):
            probes.append(
                (provider, verify_probe_count, provider_default_grounded(provider))
            )

    return estimate_probe_credits(probes)


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
    coverage_profile: str,
    providers: List[str],
    verify_providers: Optional[List[str]],
    verify_sample: Optional[Dict[str, Any]],
    custom_prompts: Optional[List[str]],
) -> str:
    raw = {
        "merchant_id": merchant_id,
        "sku_keys": sorted(sku_keys),
        "prompts_per_sku": int(prompts_per_sku),
        "coverage_profile": coverage_profile,
        "providers": sorted(providers),
        "verify_providers": sorted(_normalize_nonempty(verify_providers)),
        "verify_sample": verify_sample or {},
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
        # The portal sends `platform:source_product_id` composites; resolve to
        # product_keys (same resolver the run endpoint uses) so the preview cost
        # and the launched run agree on the product set. Returns product_keys;
        # the preview only uses the count + cache key, so granularity matches.
        return await _resolve_refs_to_product_keys(
            merchant_id=merchant_id, refs=requested,
        )

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
    coverage: Dict[str, Any],
) -> Dict[str, Any]:
    providers = list(coverage.get("providers") or [])
    # Default answer-quality verify to the engine's supported verifier(s) for the
    # explicit/single coverage paths (the "Models to run" UI's only path), which
    # carry verify_providers=[]. Mirrors run_brand_report's audit-time resolution
    # (_resolve_audit_verify_providers) so the cost preview AND the stored
    # launch.verify_providers match what actually runs; _runnable_verify_providers
    # still preflight-skips a missing DeepSeek key (no bill for work that won't run).
    verify_providers, verify_skipped = _runnable_verify_providers(
        _resolve_audit_verify_providers(coverage, None),
    )
    verify_sample = coverage.get("verify_sample") or {}
    cache_key = _preview_cache_key(
        merchant_id=merchant_id,
        sku_keys=sku_keys,
        prompts_per_sku=prompts_per_sku,
        coverage_profile=str(coverage.get("profile") or ""),
        providers=providers,
        verify_providers=verify_providers,
        verify_sample=verify_sample,
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
            verify_providers=verify_providers,
            verify_sample=verify_sample,
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
            "coverage_profile": coverage.get("profile"),
            "coverage_profile_label": coverage.get("label"),
            "providers": providers,
            "verify_providers": verify_providers,
            "verify_skipped": verify_skipped,
            "verify_sample": verify_sample,
            "requested_providers": coverage.get("requested_providers") or providers,
            "requested_verify_providers": (
                coverage.get("requested_verify_providers") or verify_providers
            ),
            "pending_engine_support": coverage.get("pending_engine_support") or [],
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
        verify_providers=list(cost_part.get("verify_providers") or []),
        verify_sample=cost_part.get("verify_sample") or {},
        custom_prompts=custom_prompts,
    )
    gaps = _credit_gaps(requirements=requirements, balance=balance)
    # A PAID tier can launch on overage — the launch gate only hard-blocks the
    # FREE tier (`if gaps and not paid_tier`, below). So the preview must report
    # "sufficient to run" for a paid merchant even when the selected audit costs
    # more than the current balance; otherwise the portal disables the Run
    # button and tells a funded, paying merchant to "top up" when they're
    # actually entitled to run on overage. `will_overage` lets the UI explain
    # that the excess bills as overage instead of silently hiding it.
    paid_tier = str(balance.get("plan_tier") or "free").lower() != "free"
    will_overage = bool(gaps) and paid_tier
    return {
        **cost_part,
        "merchant_id": merchant_id,
        "current_balance": _balance_public_shape(balance),
        "sufficient": (not gaps) or paid_tier,
        "will_overage": will_overage,
        "gaps": gaps,
    }


def _resolve_audit_merchant_id(body_merchant_id: str, auth_merchant_id: str) -> str:
    """Resolve the body's merchant_id against the authenticated merchant.

    The merchant AI-Readiness portal sends the sentinel ``'self'`` (it
    documents that the backend attaches the real merchant_id server-side);
    resolve it to the authenticated merchant. Any other value must match the
    authenticated merchant exactly — cross-tenant submission stays forbidden.
    """
    if body_merchant_id == "self":
        return auth_merchant_id
    if body_merchant_id != auth_merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "merchant_id in body must match the authenticated "
                "merchant — cross-tenant audit submission is not permitted."
            ),
        )
    return body_merchant_id


@router.post("/preview")
async def preview_audit_run(
    body: AuditPreviewRequest,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Pure pre-flight cost/credit preview for SKU audit v3."""
    body.merchant_id = _resolve_audit_merchant_id(body.merchant_id, auth_merchant_id)
    sku_keys = await _resolve_preview_sku_keys(
        merchant_id=body.merchant_id,
        scope=body.scope,
    )
    audit_tier = _resolve_request_audit_tier(body.audit_tier)
    try:
        coverage = _resolve_audit_coverage(
            coverage_profile=body.coverage_profile,
            providers=body.providers,
        )
        preview = await _build_preview(
            merchant_id=body.merchant_id,
            sku_keys=sku_keys,
            prompts_per_sku=_effective_prompts_per_sku(
                body.prompts_per_sku, audit_tier
            ),
            custom_prompts=body.custom_prompts,
            coverage=coverage,
        )
        preview["audit_tier"] = audit_tier
        return preview
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
    body.merchant_id = _resolve_audit_merchant_id(body.merchant_id, auth_merchant_id)

    # Portal-ref resolution: the merchant AI-Readiness page selects products
    # and sends `platform:source_product_id` composites as `sku_keys` (it
    # documents that the backend resolves them). Resolve to product_keys here
    # so the rest of the path stays product_key-native. Direct product_keys
    # callers (BD/tests) are unaffected.
    if not body.product_keys and body.sku_keys:
        body.product_keys = await _resolve_refs_to_product_keys(
            merchant_id=auth_merchant_id, refs=body.sku_keys,
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

    # Readiness gate: block a fresh merchant from launching before the async
    # quality backfill finishes (which would yield a false all-blocked audit
    # that reads as "Pivota doesn't work for me"). force=true bypasses.
    await _enforce_audit_readiness(
        merchant_id=auth_merchant_id,
        product_keys=body.product_keys,
        force=body.force,
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

    # Tier resolution BEFORE metering: billing, launch options, and the worker
    # must all agree on the count (deep w/ flag off is a 422 up in the model
    # helper, so a merchant is never billed for a tier that won't run).
    audit_tier = _resolve_request_audit_tier(body.audit_tier)
    effective_prompts_per_sku = _effective_prompts_per_sku(
        body.prompts_per_sku, audit_tier
    )
    try:
        coverage = _resolve_audit_coverage(
            coverage_profile=body.coverage_profile,
            providers=body.providers,
        )
        providers = list(coverage.get("providers") or [])
        # Default verify to the supported verifier(s) for the explicit/single
        # coverage paths (the UI's only path), matching the audit-time resolution
        # in run_brand_report; preflight still skips a missing DeepSeek key.
        verify_providers, verify_skipped = _runnable_verify_providers(
            _resolve_audit_verify_providers(coverage, None),
        )
        verify_sample = coverage.get("verify_sample") or {}
        provider_models = _resolve_audit_provider_models(
            providers=providers,
            model_overrides=body.model_overrides,
        )
        audit_required, audit_usd_cogs = _audit_metering(
            sku_count=len(_normalize_nonempty(body.product_keys)),
            prompts_per_sku=effective_prompts_per_sku,
            providers=providers,
            verify_providers=verify_providers,
            verify_sample=verify_sample,
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
    plan_tier = str(balance.get("plan_tier") or "free").lower()
    paid_tier = plan_tier != "free"
    # ADR-005: providers are gated by credit BALANCE, not plan tier. ChatGPT/
    # Claude simply cost more credits per run (per-provider rates in
    # config/provider_credit_rates.json) and are reflected in `requirements`
    # below — so a free-plan merchant with enough credits (e.g. a top-up) can
    # run them, and an underfunded run is refused by the insufficient-credits
    # gate, not a subscription paywall. (No premium plan-gate here by design.)
    if balance.get("overage_blocked_until_payment"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "overage_payment_failed",
                "message": (
                    "A prior overage charge failed. Update payment or "
                    "complete a successful top-up before launching more audits."
                ),
            },
        )
    if paid_tier:
        try:
            await require_verified_payment_method(body.merchant_id)
        except MissingVerifiedPaymentMethodError as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": exc.code,
                    "message": (
                        "Paid-tier audits require a verified Stripe card "
                        "before launch."
                    ),
                    "reason": exc.reason,
                },
            ) from exc
    gaps = _credit_gaps(requirements=requirements, balance=balance)
    if gaps and not paid_tier:
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
    if not paid_tier:
        await _check_audit_rate_limit(body.merchant_id)

    debited: List[Tuple[str, int, bool, Decimal, int]] = []
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
                int(audit_debit.get("purchased_credits_debited") or 0),
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
                int(prompt_debit.get("purchased_credits_debited") or 0),
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
            request_options_jsonb={
                "launch": {
                    "audit_mode": "per_sku",
                    "coverage_profile": coverage.get("profile"),
                    "coverage_profile_label": coverage.get("label"),
                    "providers": providers,
                    "verify_providers": verify_providers,
                    "verify_skipped": verify_skipped,
                    "verify_sample": verify_sample,
                    "requested_providers": (
                        coverage.get("requested_providers") or providers
                    ),
                    "requested_verify_providers": (
                        coverage.get("requested_verify_providers")
                        or verify_providers
                    ),
                    "pending_engine_support": (
                        coverage.get("pending_engine_support") or []
                    ),
                    "provider_models": provider_models,
                    "model_overrides": {
                        provider: payload.get("model")
                        for provider, payload in provider_models.items()
                        if payload.get("model_is_override")
                    },
                    # The depth tier drives the worker's probe budget AND
                    # scopes basis pinning. prompts_per_sku is persisted ONLY
                    # when explicitly overridden — a defaulted count here used
                    # to shadow the tier budget (see _launch_prompts_per_sku
                    # CAUTION), probing 40 against a deep-pinned basis.
                    "audit_tier": audit_tier,
                    **(
                        {"prompts_per_sku": int(body.prompts_per_sku)}
                        if body.prompts_per_sku is not None
                        else {}
                    ),
                    # Merchant-input prompt slots are debited as prompt credits
                    # above; persist them here so the worker actually PROBES them
                    # (they were billed-but-never-probed before this).
                    "custom_prompts": _normalize_nonempty(body.custom_prompts),
                    # Deep-tier competitive anchors (sanitized again worker-side;
                    # inert on standard runs).
                    "declared_competitors": _normalize_nonempty(
                        body.declared_competitors
                    ),
                    # Explicit basis regeneration (baseline reset) — required
                    # for newly declared competitors on a re-audit.
                    "refresh_prompt_basis": bool(body.refresh_prompt_basis),
                    "estimated_audit_credits": int(audit_required),
                    "estimated_prompt_credits": int(prompt_required),
                    "debited": [
                        {
                            "kind": kind,
                            "amount": int(amount),
                            "replay": bool(replay),
                            "purchased_credits": int(purchased_credits),
                        }
                        for (
                            kind,
                            amount,
                            replay,
                            _usd_cogs,
                            purchased_credits,
                        ) in debited
                    ],
                    "debit_idempotency_key": debit_idempotency_key,
                }
            },
        )
    except InsufficientCreditsError as exc:
        for kind, amount, replay, usd_cogs, purchased_credits in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                    purchased_credits=purchased_credits,
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
    except (OveragePaymentBlockedError, OveragePaymentFailedError) as exc:
        for kind, amount, replay, usd_cogs, purchased_credits in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                    purchased_credits=purchased_credits,
                )
        detail: Dict[str, Any] = {
            "error": "overage_payment_failed",
            "message": (
                "Overage payment failed. Update payment or complete a "
                "successful top-up before launching more audits."
            ),
        }
        reason = getattr(exc, "reason", None)
        if reason:
            detail["reason"] = reason
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=detail,
        ) from exc
    except Exception:
        for kind, amount, replay, usd_cogs, purchased_credits in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                    purchased_credits=purchased_credits,
                )
        raise
    if not run_id:
        for kind, amount, replay, usd_cogs, purchased_credits in reversed(debited):
            if not replay:
                await credit(
                    body.merchant_id,
                    kind,  # type: ignore[arg-type]
                    amount,
                    source_event_id=f"refund:{debit_idempotency_key}",
                    usd_cogs=usd_cogs,
                    purchased_credits=purchased_credits,
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


@router.get("/readiness")
async def get_audit_readiness(
    auth_merchant_id: str = Depends(get_current_merchant),
) -> Dict[str, Any]:
    """Pre-launch readiness for the authenticated merchant's v3 audit.

    The portal calls this before/at launch to show "ready" vs "still syncing"
    instead of running an audit that would come back all-blocked. Mirrors the
    gate POST /api/audits enforces (unless force=true). Read-only.

    Declared BEFORE GET /{run_id} so "readiness" is matched as a static path,
    not captured as a run_id.
    """
    platform = await _audit_readiness_platform(auth_merchant_id)
    return await assess_merchant_audit_readiness(auth_merchant_id, platform)


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
        return sanitize_report_for_merchant(
            _strip_brand_facing_internal_money(proj.get("payload_jsonb"))
        )

    return AuditRunDetail(
        **sanitize_report_for_merchant(_strip_brand_facing_internal_money(dict(row)))
    )


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
    subject_type: Optional[str] = None,
    auth_merchant_id: str = Depends(get_current_merchant),
) -> List[Dict[str, Any]]:
    """List the most recent audit runs for this merchant. Trend-
    friendly fields only — fetch a specific run via GET /api/audits/
    {run_id} for the full report payload.

    `subject_type` (optional) scopes to one run kind so each history
    surface lists only the runs it can open: "merchant" = per-SKU
    catalog audits (this page), "merchant_url" = the URL-visibility
    wedge. Omitted → all kinds."""
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100",
        )
    rows = await recent_runs_for_merchant(
        merchant_id=auth_merchant_id, limit=limit, subject_type=subject_type,
    )
    return _strip_brand_facing_internal_money(rows)
