"""
Agent Center — SKU Match Agent V1 routes.

Mirror the demand-test routes (employee-portal-internal pilot, V1).
Endpoints:

    POST   /api/agent-center/sku-match
           body: {merchant_id, store_id, payload?}
           returns: scan_target row

    GET    /api/agent-center/sku-match
           list scan targets (filter by merchant_id)

    GET    /api/agent-center/sku-match/{scan_target_id}
           returns: scan_target row + summary of issues

    GET    /api/agent-center/sku-match/{scan_target_id}/issues
           returns: paginated issue list

    POST   /api/agent-center/sku-match/{scan_target_id}/run
           triggers BackgroundTasks → run_sku_match
           returns: {status: "running", ...}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from services import agent_center_service as ac
from services.agent_center_sku_match_service import (
    SKU_MATCH_SCAN_MODE,
    is_sku_match_scan_mode,
    run_sku_match,
)
from utils.auth import get_current_employee

router = APIRouter(prefix="/api/agent-center/sku-match", tags=["agent-center"])
logger = logging.getLogger(__name__)


class CreateSkuMatchRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    store_id: str = Field(..., min_length=1)
    payload: Optional[Dict[str, Any]] = None


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    logger.exception("Unhandled error in agent-center sku-match route")
    return HTTPException(status_code=500, detail="internal_error")


@router.post("")
async def create_sku_match(
    body: CreateSkuMatchRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        target = await ac.create_scan_target(
            merchant_id=body.merchant_id,
            store_id=body.store_id,
            scan_mode=SKU_MATCH_SCAN_MODE,
            payload=body.payload,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"status": "queued", "scan_target": target}


@router.get("")
async def list_sku_match_runs(
    merchant_id: str = Query(..., min_length=1),
    status: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        return await ac.list_scan_targets(
            merchant_id=merchant_id,
            scan_mode=SKU_MATCH_SCAN_MODE,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/{scan_target_id}")
async def get_sku_match(
    scan_target_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_sku_match_scan_mode(target["scan_mode"]):
            # Belongs to a different agent — hide via 404 for surface focus.
            raise LookupError(
                f"scan_target {scan_target_id} is not a sku-match run"
            )
        issues = await ac.list_issues(scan_target_id=scan_target_id, limit=200)
        return {"scan_target": target, "issues": issues["items"]}
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/{scan_target_id}/issues")
async def list_sku_match_issues(
    scan_target_id: str,
    status: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_sku_match_scan_mode(target["scan_mode"]):
            raise LookupError(
                f"scan_target {scan_target_id} is not a sku-match run"
            )
        return await ac.list_issues(
            scan_target_id=scan_target_id,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/{scan_target_id}/run")
async def run_sku_match_route(
    scan_target_id: str,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_sku_match_scan_mode(target["scan_mode"]):
            raise LookupError(
                f"scan_target {scan_target_id} is not a sku-match run"
            )
        # Match the runner's accepted statuses; ValueError otherwise (mapped 400).
        if target["status"] not in {"queued", "stub_complete", "succeeded", "failed"}:
            raise ValueError(
                f"scan_target is in status={target['status']}, "
                "expected `queued` / `stub_complete` / `succeeded` / `failed` to (re)run"
            )
    except Exception as exc:
        raise _map_error(exc) from exc

    background_tasks.add_task(run_sku_match, scan_target_id)
    return {
        "status": "running",
        "scan_target_id": scan_target_id,
        "scan_mode": target["scan_mode"],
    }
