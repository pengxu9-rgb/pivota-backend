"""
Temporary debug endpoints to test agent API without authentication
REMOVE BEFORE PRODUCTION
"""
from fastapi import APIRouter, HTTPException
from db.database import database

router = APIRouter(prefix="/agent/debug", tags=["debug"])

@router.get("/test-agent-lookup")
async def test_agent_lookup(api_key: str):
    """Test if we can find agent by API key"""
    try:
        result = await database.fetch_one(
            "SELECT * FROM agents WHERE api_key = :api_key LIMIT 1", 
            {"api_key": api_key}
        )
        if not result:
            return {"found": False, "api_key_prefix": api_key[:20]}
        
        agent = dict(result)
        return {
            "found": True,
            "agent_id": agent.get("agent_id"),
            "name": agent.get("name"),
            "agent_name": agent.get("agent_name"),
            "email": agent.get("email"),
            "status": agent.get("status"),
            "is_active": agent.get("is_active"),
            "columns": list(agent.keys())
        }
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}

@router.get("/test-merchants-noauth")
async def test_merchants_no_auth():
    """Test merchants query without authentication"""
    try:
        query = """
            SELECT merchant_id, business_name, status 
            FROM merchant_onboarding 
            WHERE status NOT IN ('deleted', 'rejected')
            LIMIT 5
        """
        rows = await database.fetch_all(query)
        return {
            "count": len(rows),
            "merchants": [dict(r) for r in rows]
        }
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}






