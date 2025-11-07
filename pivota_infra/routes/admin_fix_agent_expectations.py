"""
Admin endpoint to fix agent revenue expectations
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user
from utils.logger import logger

router = APIRouter(prefix="/admin/agent-expectations", tags=["Admin Agent Expectations"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.get("/check/{agent_id}")
async def check_agent_expectations(
    agent_id: str,
    current_user: dict = Depends(require_admin)
):
    """Check agent's revenue expectations"""
    try:
        expectations = await database.fetch_all(
            """
            SELECT * FROM agent_revenue_expectations
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "total": len(expectations),
            "expectations": [
                {
                    "id": dict(e).get('id'),
                    "merchant_id": dict(e).get('merchant_id'),
                    "expected_commission_rate": float(dict(e).get('expected_commission_rate', 0)),
                    "min_acceptable_rate": float(dict(e).get('min_acceptable_rate', 0)),
                    "is_active": dict(e).get('is_active'),
                    "created_at": str(dict(e).get('created_at'))
                }
                for e in expectations
            ]
        }
    except Exception as e:
        logger.error(f"Failed to check agent expectations: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/delete/{agent_id}")
async def delete_agent_expectations(
    agent_id: str,
    current_user: dict = Depends(require_admin)
):
    """Delete all revenue expectations for an agent"""
    try:
        deleted = await database.execute(
            """
            DELETE FROM agent_revenue_expectations
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        logger.info(f"Deleted {deleted or 0} revenue expectations for agent {agent_id}")
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "deleted_count": deleted or 0,
            "message": "Agent revenue expectations deleted"
        }
    except Exception as e:
        logger.error(f"Failed to delete agent expectations: {e}")
        raise HTTPException(status_code=500, detail=str(e))
