"""Staff-only click-out reporting: issued vs clicked (FIX_PLAN O-08). See services/click_reporting.py.

Router-level require_admin_or_key (X-ADMIN-KEY or an admin JWT; anonymous -> 401), so a route added
here later inherits it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.click_reporting import issued_vs_clicked
from utils.auth import require_admin_or_key

router = APIRouter(
    prefix="/admin/clicks", tags=["admin-clicks"], dependencies=[Depends(require_admin_or_key)]
)

_MAX_WINDOW_DAYS = 366


def _parse(value: Optional[str], name: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be ISO-8601")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/issued-vs-clicked")
async def get_issued_vs_clicked(
    start_at: Optional[str] = Query(None, description="ISO-8601; default end_at - 7 days"),
    end_at: Optional[str] = Query(None, description="ISO-8601; default now"),
    merchant_id: Optional[str] = Query(None, max_length=50),
) -> Dict[str, Any]:
    end = _parse(end_at, "end_at") or datetime.now(timezone.utc)
    start = _parse(start_at, "start_at") or (end - timedelta(days=7))
    if start >= end:
        raise HTTPException(status_code=400, detail="start_at must be before end_at")
    if end - start > timedelta(days=_MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"window is at most {_MAX_WINDOW_DAYS} days")
    return await issued_vs_clicked(start_at=start, end_at=end, merchant_id=merchant_id)
