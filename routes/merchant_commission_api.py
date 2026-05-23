"""
Merchant Commission API.

DEPRECATED 2026-05-23. Phase 5.5 / Phase 6 introduced this API so merchants
could set per-agent-type commission rates and Pivota would dispatch
payouts. v1.3 monetization replaced it: merchants pay Pivota a take rate;
Pivota allocates partner shares via T8 Connect transfers. The
`merchant_commission_offers` table is no longer consulted by the runtime.

All endpoints return HTTP 410 Gone. The merchant-portal frontend at
merchant.pivota.cc/dashboard/commission should be hidden in
pivota-merchants-portal — that change is out of scope for this repo.

See docs/monetization/LEGACY_COMMISSION_SYSTEM_AUDIT.md for the
disposition rationale (codex industry research + Cowork decision).

Original implementation removed from this file; recoverable via git log
if needed.
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from typing import Any, Dict

from utils.auth import get_current_user

router = APIRouter(
    prefix="/merchants/{merchant_id}/commission",
    tags=["[Deprecated] Merchant Commission"],
)


_DEPRECATION_DETAIL = (
    "Merchant→agent direct commission was deprecated 2026-05-23. v1.3 "
    "monetization is the sole post-payment economic model: Pivota charges "
    "the merchant a take rate via T7 monthly Stripe Invoice and allocates "
    "partner shares via T8 Connect transfers. See "
    "docs/monetization/LEGACY_COMMISSION_SYSTEM_AUDIT.md for details."
)


def _check_merchant_self(merchant_id: str, current_user: Dict[str, Any]) -> None:
    # Preserve the original auth shape so any caller hitting the route in a
    # transition window gets the expected 403 before the 410.
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot manage other merchant's commission",
        )


@router.post("/offers")
async def create_commission_offer(
    merchant_id: str = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """DEPRECATED — see module docstring. Returns 410 Gone."""
    _check_merchant_self(merchant_id, current_user)
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)


@router.get("/offers")
async def get_commission_offers(
    merchant_id: str = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """DEPRECATED — see module docstring. Returns 410 Gone."""
    _check_merchant_self(merchant_id, current_user)
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)


@router.delete("/offers/{offer_id}")
async def delete_commission_offer(
    merchant_id: str = Path(...),
    offer_id: int = Path(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """DEPRECATED — see module docstring. Returns 410 Gone."""
    _check_merchant_self(merchant_id, current_user)
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)
