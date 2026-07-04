"""Admin (internal) audit-payload backfill.

W1 site-5 repair: stored per-SKU audit reports computed before the 2026-07-04
channels fix can display "Your site 0/N" while the merchant's own page was
cited on most prompts (see services/audit_backfill.py). This route recomputes
the contradicted block from the run's persisted probe runs.

Safety:
- Admin-key gated (same X-ADMIN-KEY as the other /agent/internal routes).
- dry_run defaults to TRUE — a plain call reports would-be changes only.
- Idempotent; per-run provenance marker (`own_site_backfill`) on every write.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status

from services.audit_backfill import backfill_channel_own_site

router = APIRouter(prefix="/agent/internal/audits", tags=["internal-audit-backfill"])


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = (os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY") or "").strip()
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.post(
    "/merchant/{merchant_id}/backfill-own-site",
    response_model=Dict[str, Any],
    dependencies=[Depends(require_admin_key)],
)
async def backfill_own_site(
    merchant_id: str = Path(..., description="Internal merchant id"),
    dry_run: bool = Query(True, description="Report changes without writing (default)"),
    limit: int = Query(20, ge=1, le=100, description="Most-recent succeeded runs to scan"),
    run_ids: Optional[str] = Query(
        None, description="Comma-separated run_ids to scope the sweep (optional)"
    ),
) -> Dict[str, Any]:
    ids: Optional[List[str]] = None
    if run_ids:
        ids = [r.strip() for r in run_ids.split(",") if r.strip()]
    return await backfill_channel_own_site(
        merchant_id=merchant_id,
        run_ids=ids,
        limit=limit,
        dry_run=dry_run,
    )
