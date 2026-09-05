"""
Admin Governance Routes - Phase 3
Handle governance action proposals, approvals, and execution
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging

router = APIRouter(prefix="/admin/governance", tags=["Admin Governance"])
logger = logging.getLogger(__name__)


class ApproveActionRequest(BaseModel):
    """Request to approve a governance action"""
    pass


class RejectActionRequest(BaseModel):
    """Request to reject a governance action"""
    reason: str


@router.get("/pending-actions")
async def get_pending_governance_actions(current_user: dict = Depends(get_current_user)):
    """List all pending governance actions awaiting approval"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        from services.agent_governance_service import get_pending_actions
        
        actions = await get_pending_actions()
        
        return {
            "status": "success",
            "actions": [
                {
                    "action_id": a.get("action_id"),
                    "agent_id": a.get("agent_id"),
                    "agent_name": a.get("agent_name"),
                    "action_type": a.get("action_type"),
                    "triggered_by": a.get("triggered_by"),
                    "action_payload": a.get("action_payload"),
                    "reason": a.get("reason"),
                    "created_at": str(a.get("created_at")) if a.get("created_at") else None
                }
                for a in actions
            ],
            "total": len(actions)
        }
    
    except Exception as e:
        logger.error(f"Failed to get pending actions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/actions/{action_id}/approve")
async def approve_governance_action(
    action_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Approve and execute a governance action"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        from services.agent_governance_service import execute_governance_action
        
        success = await execute_governance_action(action_id, current_user.get("email"))
        
        if success:
            return {
                "status": "success",
                "message": "Governance action approved and executed",
                "action_id": action_id,
                "executed_by": current_user.get("email")
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to execute action")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to approve action {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/actions/{action_id}/reject")
async def reject_governance_action(
    action_id: str,
    request: RejectActionRequest,
    current_user: dict = Depends(get_current_user)
):
    """Reject a governance action"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        from services.agent_governance_service import reject_governance_action
        
        success = await reject_governance_action(
            action_id,
            current_user.get("email"),
            request.reason
        )
        
        if success:
            return {
                "status": "success",
                "message": "Governance action rejected",
                "action_id": action_id,
                "rejected_by": current_user.get("email")
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to reject action")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reject action {action_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{agent_id}/governance-history")
async def get_agent_governance_history(
    agent_id: str,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """Get governance action history for an agent"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        from services.agent_governance_service import get_governance_history
        
        history = await get_governance_history(agent_id, limit)
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "actions": [
                {
                    "action_id": h.get("action_id"),
                    "action_type": h.get("action_type"),
                    "triggered_by": h.get("triggered_by"),
                    "executed_by": h.get("executed_by"),
                    "action_payload": h.get("action_payload"),
                    "status": h.get("status"),
                    "reason": h.get("reason"),
                    "created_at": str(h.get("created_at")) if h.get("created_at") else None,
                    "executed_at": str(h.get("executed_at")) if h.get("executed_at") else None
                }
                for h in history
            ],
            "total": len(history)
        }
    
    except Exception as e:
        logger.error(f"Failed to get governance history for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/metrics/collect-now")
async def collect_metrics_now(current_user: dict = Depends(get_current_user)):
    """Manually trigger metrics collection for all agents"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        from services.agent_metrics_collector import collect_all_agents_metrics
        from services.agent_anomaly_detector import run_anomaly_detection
        
        # Collect metrics
        summary = await collect_all_agents_metrics(window_minutes=5)
        
        # Run anomaly detection on collected metrics
        total_alerts = 0
        for agent_id in []:  # TODO: get list of agents with new metrics
            pass  # Anomaly detection runs automatically in collector
        
        return {
            "status": "success",
            "message": "Metrics collection triggered",
            "summary": summary
        }
    
    except Exception as e:
        logger.error(f"Failed to collect metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

