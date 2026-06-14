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


@router.post("/outcomes/refresh", response_model=Dict[str, Any])
async def refresh_outcomes_admin(
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    from services.outcome_aggregation_service import refresh_all_outcomes

    counts = await refresh_all_outcomes()
    return {"refreshed": True, "counts": counts}
