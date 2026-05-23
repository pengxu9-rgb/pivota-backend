"""
Admin helper to backfill commissions and payouts for existing orders.

DEPRECATED 2026-05-23. Legacy Phase 5.5/6 merchant→agent commission system
was retired in favor of v1.3 take-rate monetization. All endpoints in this
module now return HTTP 410 Gone. See
docs/monetization/LEGACY_COMMISSION_SYSTEM_AUDIT.md for the disposition +
decision rationale, and
docs/monetization/deploy/STAGE_2_HISTORICAL_BACKFILL.md for the v1.3
attribution-edge backfill that replaces this admin path.

Service modules (services/order_commission_service.py,
services/revenue_share_service.py) are kept for historical traceability
of the `commissions` and `revenue_matching_logs` tables; a future cleanup
PR will remove them once we're confident nothing else references them.

Original implementation removed from this file; recoverable via git log if
needed.
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any

from utils.auth import get_current_user

router = APIRouter(prefix="/admin/payouts", tags=["admin-payouts"])


_DEPRECATION_DETAIL = (
    "Legacy Phase 5.5/6 commission backfill is deprecated as of 2026-05-23. "
    "Use the v1.3 attribution-edge backfill instead "
    "(scripts/stage2_backfill_attribution_edges.py + docs/monetization/"
    "deploy/STAGE_2_HISTORICAL_BACKFILL.md). v1.3 charges merchants via T7 "
    "monthly Stripe invoices and allocates partner shares via T8 Connect "
    "transfers; merchant→agent direct commission is no longer used."
)


def _require_admin(current_user: Dict[str, Any]) -> None:
    if current_user.get("role") not in ["admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.post("/backfill")
async def backfill_commissions_and_payouts(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """DEPRECATED — see module docstring. Returns 410 Gone."""
    _require_admin(current_user)
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)


@router.post("/patch-schema")
async def patch_commissions_schema(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """DEPRECATED — see module docstring. Returns 410 Gone."""
    _require_admin(current_user)
    raise HTTPException(status_code=410, detail=_DEPRECATION_DETAIL)
