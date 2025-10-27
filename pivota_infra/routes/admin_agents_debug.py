"""Admin endpoint to debug agents table"""
from fastapi import APIRouter
from db.database import database

router = APIRouter(prefix="/admin/agents", tags=["admin-debug"])

@router.get("/list")
async def list_all_agents():
    """List all agents in database for debugging"""
    try:
        agents = await database.fetch_all(
            "SELECT agent_id, name, email, created_at FROM agents ORDER BY created_at DESC"
        )
        
        return {
            "success": True,
            "total": len(agents),
            "agents": [
                {
                    "agent_id": a["agent_id"],
                    "name": a.get("name", "Unknown"),
                    "email": a.get("email", "Unknown"),
                    "created_at": str(a.get("created_at", ""))
                }
                for a in agents
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
