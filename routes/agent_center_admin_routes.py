"""
Agent Center — admin-only ops routes.

Currently exposes the stuck-run lever:

    GET  /admin/agent-center/scan-targets/stuck?stale_minutes=30
         List scan_targets currently in `status='running'` whose
         updated_at is older than `stale_minutes`. Use to identify
         crashed runners blocking the /run lock.

    POST /admin/agent-center/scan-targets/{id}/force-reset
         body: {reason: string}
         Force a scan_target to `status='failed'`. Records the previous
         status + reason in payload.error so the action is auditable.

Why this exists: a runner that crashes mid-execution leaves the row in
`status='running'` forever, which the /run lock from #271 will refuse to
re-acquire. We deliberately do NOT auto-recover via a stale-running TTL
because that would steal the lock from a still-working long-runner and
re-introduce the race the lock prevents. Instead ops gets visibility +
an explicit lever.

Both routes are gated by `Depends(require_admin)` (matches the migration-
apply route's pattern in `routes/admin_apply_agent_center_migration.py`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from services import agent_center_service as ac
from utils.auth import require_admin

router = APIRouter(prefix="/admin/agent-center", tags=["agent-center", "admin"])
logger = logging.getLogger(__name__)


class ForceResetRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    logger.exception("Unhandled error in agent-center admin route")
    return HTTPException(status_code=500, detail="internal_error")


@router.get("/scan-targets/stuck")
async def list_stuck_runs(
    stale_minutes: int = Query(30, ge=1, le=10080),
    limit: int = Query(100, ge=1, le=500),
    current_user: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        items = await ac.list_stuck_running_targets(
            stale_minutes=stale_minutes, limit=limit,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"stale_minutes": stale_minutes, "items": items}


@router.post("/scan-targets/{scan_target_id}/force-reset")
async def force_reset_scan_target(
    scan_target_id: str,
    body: ForceResetRequest,
    current_user: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    reset_by = (
        current_user.get("email")
        or current_user.get("employee_id")
        or current_user.get("user_id")
        or "admin"
    )
    try:
        target = await ac.force_reset_scan_target(
            scan_target_id=scan_target_id,
            reason=body.reason,
            reset_by=str(reset_by),
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"status": "reset", "scan_target": target}
