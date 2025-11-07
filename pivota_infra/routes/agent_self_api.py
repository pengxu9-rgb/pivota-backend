"""
Agent Self-Service API
Endpoints for agents to manage their own resources
"""

from fastapi import APIRouter, Depends, HTTPException
from utils.auth import get_current_user
from db.database import database
from utils.logger import logger

router = APIRouter(prefix="/agent/self", tags=["agent-self-service"])

@router.get("/api-key")
async def get_own_api_key(
    current_user: dict = Depends(get_current_user)
):
    """
    Get agent's own API key (full, unmasked)
    
    Only agents can access this endpoint for their own key.
    """
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    agent_id = current_user.get("agent_id")
    email = current_user.get("email")
    
    if not agent_id and not email:
        raise HTTPException(status_code=400, detail="No agent identifier found")
    
    try:
        # Get agent's full API key
        query = """
            SELECT agent_id, api_key, name, created_at, last_active, 
                   agent_type, email, status
            FROM agents 
            WHERE (agent_id = :agent_id OR email = :email)
            AND status = 'active'
            LIMIT 1
        """
        
        agent = await database.fetch_one(
            query,
            {"agent_id": agent_id or "", "email": email or ""}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        agent_dict = dict(agent)
        
        # Return full API key for agent's own use
        return {
            "status": "success",
            "agent_id": agent_dict["agent_id"],
            "api_key": agent_dict["api_key"],  # Full key
            "name": agent_dict.get("name"),
            "created_at": agent_dict.get("created_at"),
            "last_active": agent_dict.get("last_active"),
            "usage_count": 0,  # TODO: Get from usage logs
            "agent_type": agent_dict.get("agent_type", "basic")
        }
        
    except Exception as e:
        logger.error(f"Failed to get own API key for agent_id={agent_id}, email={email}: {str(e)}")
        logger.error(f"Query executed: {query}")
        logger.error(f"Parameters: agent_id={agent_id or 'None'}, email={email or 'None'}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/profile")
async def get_own_profile(
    current_user: dict = Depends(get_current_user)
):
    """Get agent's own profile information"""
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    agent_id = current_user.get("agent_id")
    email = current_user.get("email")
    
    try:
        agent = await database.fetch_one(
            """
            SELECT 
                agent_id,
                name,
                email,
                agent_type,
                status,
                created_at,
                last_active
            FROM agents
            WHERE (agent_id = :agent_id OR email = :email)
            AND status = 'active'
            LIMIT 1
            """,
            {"agent_id": agent_id or "", "email": email or ""}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        return {
            "status": "success",
            "profile": dict(agent)
        }
        
    except Exception as e:
        logger.error(f"Failed to get profile: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve profile")
