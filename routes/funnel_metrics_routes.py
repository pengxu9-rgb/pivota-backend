"""Admin funnel metrics — the audit-growth-funnel conversion dashboard (WS-4).

GET /api/admin/funnel-metrics?since=YYYY-MM-DD&until=YYYY-MM-DD

Admin-only: platform-wide counts across registrations, audit runs, and
subscriptions. Defaults to the last 30 days; `since` also floors at the
2026-07-22 funnel launch when omitted, so the default view is the
registration-first era rather than legacy history.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from services.funnel_metrics_service import compute_funnel_metrics
from utils.auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin-funnel-metrics"])

FUNNEL_LAUNCH = datetime(2026, 7, 22, tzinfo=timezone.utc)


def _parse_bound(raw: Optional[str], field: str) -> Optional[datetime]:
    if not raw or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be an ISO date/datetime, got {raw!r}",
        )
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@router.get("/funnel-metrics")
async def get_funnel_metrics(
    since: Optional[str] = None,
    until: Optional[str] = None,
    _admin: Any = Depends(require_admin),
) -> Dict[str, Any]:
    until_dt = _parse_bound(until, "until") or datetime.now(timezone.utc)
    since_dt = _parse_bound(since, "since") or max(
        FUNNEL_LAUNCH, until_dt - timedelta(days=30)
    )
    if since_dt >= until_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="since must be before until",
        )
    # Pass the LIVE allowance-gate parameters so quota.exhausted tracks the
    # gate that actually 402s merchants.
    from routes.merchant_audit_routes import (
        _FREE_AUDIT_COUNT_SINCE,
        _FREE_URL_AUDITS_PER_MERCHANT,
    )

    return await compute_funnel_metrics(
        since=since_dt,
        until=until_dt,
        free_audit_cap=_FREE_URL_AUDITS_PER_MERCHANT,
        free_count_since=_FREE_AUDIT_COUNT_SINCE,
    )
