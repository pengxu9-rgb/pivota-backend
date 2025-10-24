"""
Debug query analytics - check what's being counted
"""
from fastapi import APIRouter
from db.database import database
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin/debug", tags=["debug"])

@router.get("/query-analytics-raw/{agent_id}")
async def debug_query_analytics(agent_id: str):
    """Debug query analytics counts"""
    try:
        last_24h = datetime.now() - timedelta(hours=24)
        
        # Get all product-related logs
        product_logs = await database.fetch_all(
            """
            SELECT endpoint, method, status_code, timestamp
            FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since
            AND (endpoint LIKE '%/products/search%' OR endpoint LIKE '%/catalog/search%' OR endpoint LIKE '%/products%')
            ORDER BY timestamp DESC
            """,
            {"agent_id": agent_id, "since": last_24h}
        )
        
        # Count
        count = await database.fetch_val(
            """
            SELECT COUNT(*) FROM agent_usage_logs
            WHERE agent_id = :agent_id 
            AND timestamp >= :since
            AND (endpoint LIKE '%/products/search%' OR endpoint LIKE '%/catalog/search%' OR endpoint LIKE '%/products%')
            """,
            {"agent_id": agent_id, "since": last_24h}
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "last_24h_cutoff": last_24h.isoformat(),
            "count": count,
            "matching_logs": [
                {
                    "endpoint": log["endpoint"],
                    "method": log["method"],
                    "status_code": log["status_code"],
                    "timestamp": log["timestamp"].isoformat()
                }
                for log in product_logs
            ]
        }
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }
