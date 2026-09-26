"""Admin endpoint to debug agents table"""
from fastapi import APIRouter, Depends
from utils.auth import require_admin_or_key
from db.database import database

# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# GET /admin/agents/list returned every agent's agent_id, name, email and
# created_at to an anonymous caller, unpaginated.
router = APIRouter(prefix="/admin/agents", tags=["admin-debug"], dependencies=[Depends(require_admin_or_key)])

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
