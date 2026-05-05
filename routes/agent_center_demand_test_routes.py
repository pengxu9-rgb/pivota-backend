"""
Agent Center — Demand Test Agent V1 routes.

Internal-pilot routes (employee auth only). The merchant-facing equivalents
land when the merchant-portal UI does, in a follow-up PR.

Endpoints:

    POST   /api/agent-center/demand-tests
           body: {merchant_id, store_id, scan_mode, payload?}
           returns: scan_target row

    GET    /api/agent-center/demand-tests/{scan_target_id}
           returns: scan_target row + summary of issues

    GET    /api/agent-center/demand-tests/{scan_target_id}/issues
           returns: paginated issue list

    POST   /api/agent-center/demand-tests/{scan_target_id}/run
           triggers BackgroundTasks → run_demand_test_stub
           returns: {status: "running", ...}

    GET    /api/agent-center/demand-tests
           list scan targets (filter by merchant_id)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from services import agent_center_service as ac
from services.agent_center_demand_test_service import (
    is_demand_test_scan_mode,
    run_demand_test,
)
from utils.auth import get_current_employee

router = APIRouter(prefix="/api/agent-center/demand-tests", tags=["agent-center"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateDemandTestRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    store_id: str = Field(..., min_length=1)
    scan_mode: str = Field(..., min_length=1)
    payload: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_error(exc: Exception) -> HTTPException:
    """Translate service-layer exceptions to HTTP responses (matches the
    convention in routes/employee_pdp_governance.py)."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    logger.exception("Unhandled error in agent-center demand-test route")
    return HTTPException(status_code=500, detail="internal_error")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("")
async def create_demand_test(
    body: CreateDemandTestRequest,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Create a new Demand Test scan target. Returns the queued row."""
    if not is_demand_test_scan_mode(body.scan_mode):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported demand-test scan_mode: {body.scan_mode}. "
                f"Allowed: {list(ac.DEMAND_TEST_SCAN_MODES)}"
            ),
        )
    try:
        target = await ac.create_scan_target(
            merchant_id=body.merchant_id,
            store_id=body.store_id,
            scan_mode=body.scan_mode,
            payload=body.payload,
        )
    except Exception as exc:
        raise _map_error(exc) from exc
    return {"status": "queued", "scan_target": target}


@router.get("")
async def list_demand_tests(
    merchant_id: str = Query(..., min_length=1),
    scan_mode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """List recent demand-test scan targets for a merchant."""
    if scan_mode is not None and not is_demand_test_scan_mode(scan_mode):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported demand-test scan_mode filter: {scan_mode}. "
                f"Allowed: {list(ac.DEMAND_TEST_SCAN_MODES)}"
            ),
        )
    try:
        return await ac.list_scan_targets(
            merchant_id=merchant_id,
            scan_mode=scan_mode,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/{scan_target_id}")
async def get_demand_test(
    scan_target_id: str,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Fetch a single demand-test scan target with its issue summary."""
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_demand_test_scan_mode(target["scan_mode"]):
            # The scan target exists but belongs to a different agent. Hide
            # via 404 rather than 403 so the demand-test surface stays
            # focused.
            raise LookupError(
                f"scan_target {scan_target_id} is not a demand-test run"
            )
        issues = await ac.list_issues(scan_target_id=scan_target_id, limit=50)
        return {
            "scan_target": target,
            "issues": issues["items"],
        }
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/{scan_target_id}/issues")
async def list_demand_test_issues(
    scan_target_id: str,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """List issues attached to a demand-test scan target."""
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_demand_test_scan_mode(target["scan_mode"]):
            raise LookupError(
                f"scan_target {scan_target_id} is not a demand-test run"
            )
        return await ac.list_issues(
            scan_target_id=scan_target_id,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/{scan_target_id}/run")
async def run_demand_test(
    scan_target_id: str,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Kick off the (V1 stub) Demand Test run for a scan target.

    Real Gemini integration lives in a follow-up PR; until then this route
    schedules `run_demand_test_stub` which transitions the target through
    queued → running → stub_complete and creates a synthetic issue.
    """
    try:
        target = await ac.get_scan_target(scan_target_id=scan_target_id)
        if target is None:
            raise LookupError(f"scan_target not found: {scan_target_id}")
        if not is_demand_test_scan_mode(target["scan_mode"]):
            raise LookupError(
                f"scan_target {scan_target_id} is not a demand-test run"
            )
        if target["status"] not in {"queued", "stub_complete"}:
            raise ValueError(
                f"scan_target is in status={target['status']}, "
                "expected `queued` or `stub_complete` to start a (re)run"
            )
    except Exception as exc:
        raise _map_error(exc) from exc

    background_tasks.add_task(run_demand_test, scan_target_id)
    return {
        "status": "running",
        "scan_target_id": scan_target_id,
        "scan_mode": target["scan_mode"],
    }
