"""Admin endpoint to debug agent_usage_logs"""
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
# Anyone could read any agent's usage logs and stats by id.
router = APIRouter(prefix="/admin/usage", tags=["admin-debug"], dependencies=[Depends(require_admin_or_key)])

@router.get("/logs/{agent_id}")
async def get_agent_usage_logs(agent_id: str, limit: int = 20):
    """Get agent usage logs for debugging"""
    try:
        logs = await database.fetch_all(
            """SELECT agent_id, endpoint, method, status_code, response_time_ms, timestamp 
               FROM agent_usage_logs 
               WHERE agent_id = :agent_id 
               ORDER BY timestamp DESC 
               LIMIT :limit""",
            {"agent_id": agent_id, "limit": limit}
        )
        
        return {
            "success": True,
            "agent_id": agent_id,
            "total": len(logs),
            "logs": [
                {
                    "agent_id": log["agent_id"],
                    "endpoint": log["endpoint"],
                    "method": log["method"],
                    "status_code": log["status_code"],
                    "response_time_ms": log.get("response_time_ms"),
                    "timestamp": str(log["timestamp"])
                }
                for log in logs
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.get("/stats/{agent_id}")
async def get_agent_stats(agent_id: str):
    """Get aggregated stats for an agent"""
    try:
        from datetime import datetime, timedelta
        last_24h = datetime.now() - timedelta(hours=24)
        
        total = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE agent_id = :aid",
            {"aid": agent_id}
        ) or 0
        
        last_24h_count = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE agent_id = :aid AND timestamp >= :since",
            {"aid": agent_id, "since": last_24h}
        ) or 0
        
        orders = await database.fetch_val(
            "SELECT COUNT(*) FROM orders WHERE agent_id = :aid",
            {"aid": agent_id}
        ) or 0
        
        return {
            "success": True,
            "agent_id": agent_id,
            "total_api_calls": total,
            "api_calls_24h": last_24h_count,
            "total_orders": orders
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


