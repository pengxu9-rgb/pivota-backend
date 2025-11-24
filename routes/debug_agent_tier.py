"""
Temporary debug endpoint to check agent tier without auth
"""
from fastapi import APIRouter, HTTPException
from db.database import database

router = APIRouter(prefix="/debug/agent-tier", tags=["debug"])

@router.get("/{agent_id}")
async def get_agent_tier_debug(agent_id: str):
    """Get agent tier without authentication (temporary debug)"""
    try:
        agent = await database.fetch_one(
            "SELECT agent_id, email, agent_type FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            return {
                "found": False,
                "agent_id": agent_id,
                "message": "Agent not found in database"
            }
        
        return {
            "found": True,
            "agent_id": dict(agent).get("agent_id"),
            "email": dict(agent).get("email"),
            "agent_type": dict(agent).get("agent_type"),
            "message": "Success - no auth required for debug"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

