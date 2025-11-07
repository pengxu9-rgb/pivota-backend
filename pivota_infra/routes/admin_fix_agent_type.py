"""
Admin endpoint to fix agent_type NULL issues
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user
from utils.logger import logger

router = APIRouter(prefix="/admin/agents", tags=["Admin Agent Fixes"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.post("/fix-null-agent-types")
async def fix_null_agent_types(
    payload: Dict[str, Any],
    current_user: dict = Depends(require_admin)
):
    """
    Fix agents with NULL agent_type
    Sets them to 'basic' to avoid defaulting to 'standard' (2% commission)
    """
    default_type = payload.get("default_type", "basic")
    
    if default_type not in ["basic", "premium"]:
        raise HTTPException(status_code=400, detail="default_type must be 'basic' or 'premium'")
    
    try:
        # Count affected agents
        count_result = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agents WHERE agent_type IS NULL"
        )
        affected_count = count_result['count'] if count_result else 0
        
        # Fix NULL agent_types
        updated = await database.execute(
            """
            UPDATE agents
            SET agent_type = :default_type
            WHERE agent_type IS NULL
            """,
            {"default_type": default_type}
        )
        
        # Also fix any 'standard' types (legacy)
        standard_updated = await database.execute(
            """
            UPDATE agents
            SET agent_type = 'basic'
            WHERE agent_type = 'standard'
            """
        )
        
        logger.info(
            f"[Agent Type Fix] Fixed {updated or 0} NULL + {standard_updated or 0} standard agents "
            f"by {current_user.get('email')}"
        )
        
        # Get sample of fixed agents
        fixed_agents = await database.fetch_all(
            """
            SELECT agent_id, email, agent_type
            FROM agents
            WHERE agent_type = :default_type
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT 10
            """,
            {"default_type": default_type}
        )
        
        return {
            "status": "success",
            "fixed": {
                "null_to_type": updated or 0,
                "standard_to_basic": standard_updated or 0,
                "total": (updated or 0) + (standard_updated or 0)
            },
            "sample_agents": [dict(a) for a in fixed_agents[:5]],
            "message": f"Fixed agent types to prevent 2% default commission"
        }
        
    except Exception as e:
        logger.error(f"Failed to fix agent types: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/{agent_id}/type")
async def fix_specific_agent_type(
    agent_id: str,
    payload: Dict[str, Any],
    current_user: dict = Depends(require_admin)
):
    """Fix agent_type for a specific agent"""
    new_type = payload.get("agent_type")
    
    if new_type not in ["basic", "premium"]:
        raise HTTPException(status_code=400, detail="agent_type must be 'basic' or 'premium'")
    
    try:
        # Get current agent
        agent = await database.fetch_one(
            "SELECT agent_id, email, agent_type FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        old_type = dict(agent).get("agent_type")
        
        # Update agent_type
        await database.execute(
            "UPDATE agents SET agent_type = :new_type WHERE agent_id = :agent_id",
            {"new_type": new_type, "agent_id": agent_id}
        )
        
        logger.info(
            f"[Agent Type Fix] {agent_id}: {old_type} → {new_type} "
            f"by {current_user.get('email')}"
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "old_type": old_type,
            "new_type": new_type,
            "message": f"Agent type updated to {new_type}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent type: {e}")
        raise HTTPException(status_code=500, detail=str(e))
