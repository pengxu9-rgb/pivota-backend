"""
Debug endpoints to verify agent_usage_logs ingestion and summarize activity
"""
from fastapi import APIRouter
from db.database import database
from typing import Any, Dict

router = APIRouter(prefix="/admin/debug/usage-logs", tags=["debug", "usage-logs"]) 

@router.get("/summary")
async def usage_logs_summary() -> Dict[str, Any]:
    """Return totals and breakdown by endpoint/method for agent_usage_logs."""
    result: Dict[str, Any] = {"status": "success"}
    try:
        total = await database.fetch_val("SELECT COUNT(*) FROM agent_usage_logs")
        result["total"] = int(total or 0)
    except Exception as e:
        result["total_error"] = str(e)
        result["total"] = 0
    try:
        by_endpoint = await database.fetch_all(
            """
            SELECT endpoint, COUNT(*) AS count
            FROM agent_usage_logs
            GROUP BY endpoint
            ORDER BY count DESC
            LIMIT 20
            """
        )
        result["by_endpoint"] = [dict(r) for r in by_endpoint]
    except Exception as e:
        result["by_endpoint_error"] = str(e)
        result["by_endpoint"] = []
    try:
        by_status = await database.fetch_all(
            """
            SELECT status_code, COUNT(*) AS count
            FROM agent_usage_logs
            GROUP BY status_code
            ORDER BY count DESC
            LIMIT 20
            """
        )
        result["by_status"] = [dict(r) for r in by_status]
    except Exception as e:
        result["by_status_error"] = str(e)
        result["by_status"] = []
    return result

@router.get("/recent")
async def usage_logs_recent(limit: int = 25) -> Dict[str, Any]:
    """Return the most recent usage logs."""
    try:
        rows = await database.fetch_all(
            """
            SELECT agent_id, endpoint, method, status_code, response_time_ms, timestamp
            FROM agent_usage_logs
            ORDER BY timestamp DESC
            LIMIT :limit
            """,
            {"limit": limit},
        )
        return {"status": "success", "count": len(rows), "rows": [dict(r) for r in rows]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


