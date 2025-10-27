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
        
        agents_list = []
        for a in agents:
            try:
                agents_list.append({
                    "agent_id": a["agent_id"],
                    "name": a.get("name") if hasattr(a, 'get') else a["name"],
                    "email": a.get("email") if hasattr(a, 'get') else a["email"],
                    "created_at": str(a.get("created_at") if hasattr(a, 'get') else a["created_at"])
                })
            except Exception as e:
                # Skip problematic records
                continue
        
        return {
            "success": True,
            "total": len(agents_list),
            "agents": agents_list
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
