"""Admin (internal) outcome-aggregation endpoints.

GET  /agent/internal/outcomes?subject_type=&subject_key=&window=  — read one subject.
POST /agent/internal/outcomes/refresh                            — recompute the store.

Internal-only (admin key). The merchant-facing view lives on the merchant-center
router; the agent-facing aggregated_outcomes signal is a separate, flag-gated read.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status

router = APIRouter(prefix="/agent/internal", tags=["internal-outcomes"])


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = (os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY") or "").strip()
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.get("/outcomes", response_model=Dict[str, Any])
async def get_outcomes_admin(
    subject_type: str,
    subject_key: str,
    window: str = "all_time",
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    from services.outcome_aggregation_service import get_outcomes

    row = await get_outcomes(subject_type, subject_key, window_key=window)
    return {"found": bool(row), "outcomes": row}


@router.get("/seller-trust", response_model=Dict[str, Any])
async def get_seller_trust_admin(
    merchant_id: str,
    window: str = "all_time",
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """The outcome-derived seller-trust envelope for one merchant (W8) — the same
    signal attached to agent_pdp_view offers. None when the merchant has no
    transacted outcomes yet (honest empty state, not a fabricated score)."""
    from services.outcome_aggregation_service import get_seller_trust

    trust = await get_seller_trust(merchant_id, window_key=window)
    return {"found": bool(trust), "seller_trust": trust}


@router.get("/audit-health", response_model=Dict[str, Any])
async def get_audit_health_admin(
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """W7 audit-health snapshot: run-failure + honest-failure (brief unavailable_*)
    rates over the rolling window, plus any threshold breaches. The same payload the
    hourly audit_health_tick evaluates for alerting."""
    from services.audit_health_metrics import compute_audit_health

    return await compute_audit_health()


@router.post("/outcomes/refresh", response_model=Dict[str, Any])
async def refresh_outcomes_admin(
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    from services.outcome_aggregation_service import refresh_all_outcomes

    counts = await refresh_all_outcomes()
    return {"refreshed": True, "counts": counts}
