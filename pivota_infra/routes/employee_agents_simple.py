"""
Minimal working version of employee agents endpoint
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import get_current_user
from db.database import database

router = APIRouter(prefix="/employee/agents-simple", tags=["employee-agents-simple"])

@router.get("/")
async def get_all_agents_simple(current_user: dict = Depends(get_current_user)):
    """Minimal working agent list - for testing"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Simplest possible query
        agents = await database.fetch_all("SELECT * FROM agents ORDER BY created_at DESC")
        
        # Minimal formatting
        result = []
        for row in agents:
            # Convert to dict
            a = dict(row)
            result.append({
                "agent_id": a.get("agent_id"),
                "agent_name": a.get("name"),
                "owner_email": a.get("email"),
                "status": a.get("status"),
                "request_count": a.get("request_count", 0),
                "total_orders": a.get("total_orders", 0),
                "total_gmv": float(a.get("total_gmv", 0)),
                "rate_limit": a.get("rate_limit", 1000)
            })
        
        return {
            "status": "success",
            "agents": result,
            "total": len(result)
        }
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

