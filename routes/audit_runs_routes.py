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

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db.merchant_audit_runs import (
    ACTIVE_STAGES,
    STAGE_QUEUED,
    cancel_audit_run,
    enqueue_audit_run_with_replay,
    fetch_audit_run_by_id,
    find_in_flight_by_idempotency_key,
    recent_runs_for_merchant,
)
from services.idempotency import compute_audit_idempotency_key
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
# Endpoints
# =====================================================================


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

    # Idempotency dedupe (unless force=true).
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
    else:
        idempotency_key = None

    # enqueue_audit_run_with_replay returns (run_id, was_existing).
    # was_existing=True means INSERT ... ON CONFLICT DO NOTHING fired
    # against the partial unique idempotency index — a concurrent
    # POST won the race; we hand back the winning run_id and signal
    # idempotent_replay so the client knows it wasn't a fresh enqueue.
    run_id, was_existing = await enqueue_audit_run_with_replay(
        merchant_id=body.merchant_id,
        product_keys=body.product_keys,
        subject_type=body.subject_type,
        idempotency_key=idempotency_key,
        requested_by_user_id=auth_merchant_id,
    )
    if not run_id:
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
        return proj.get("payload_jsonb")

    return AuditRunDetail(**row)


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
    return await recent_runs_for_merchant(
        merchant_id=auth_merchant_id, limit=limit,
    )
