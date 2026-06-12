"""Admin (internal) controlled-signal scorecard.

GET /agent/internal/signal-scorecard — per-signal coverage / freshness /
agent-exposure over the serving-eligible universe. Internal-only (admin key);
the merchant-facing "index health" view is a later, curated derivative.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, status

router = APIRouter(prefix="/agent/internal", tags=["internal-signal-scorecard"])


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = (os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY") or "").strip()
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.get("/signal-scorecard", response_model=Dict[str, Any])
async def get_signal_scorecard(
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """Coverage × freshness × exposure for every controlled decision signal."""
    from services.signal_scorecard_service import compute_scorecard

    return await compute_scorecard()
