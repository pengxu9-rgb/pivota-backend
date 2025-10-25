"""
Debug agent API key
"""

from fastapi import APIRouter
from db.database import database

router = APIRouter(prefix="/admin/debug", tags=["admin-debug"])

@router.get("/agent-key")
async def debug_agent_key():
    """Check agent@test.com's API key in database"""
    try:
        # Get agent record
        agent = await database.fetch_one(
            "SELECT agent_id, email, api_key, status FROM agents WHERE email = 'agent@test.com'"
        )
        
        if agent:
            api_key = agent["api_key"]
            return {
                "status": "found",
                "agent_id": agent["agent_id"],
                "email": agent["email"],
                "api_key_prefix": api_key[:15] if api_key else "NULL",
                "api_key_length": len(api_key) if api_key else 0,
                "api_key_format": "VALID" if api_key and api_key.startswith("ak_") and len(api_key) > 60 else "INVALID",
                "status": agent["status"],
                "full_key_for_test": api_key  # Only for testing!
            }
        else:
            return {
                "status": "not_found",
                "message": "agent@test.com not found in agents table"
            }
            
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }



