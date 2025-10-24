"""
Agent Metrics aliases under /agent/v1/metrics for external agents and portals
These mirror /agent/metrics endpoints and support x-api-key-based filtering.
"""
from fastapi import APIRouter, Query, Request
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from db.database import database

router = APIRouter(prefix="/agent/v1/metrics", tags=["Agent Metrics V1"])


@router.get("/summary")
async def get_metrics_summary_v1(request: Request) -> Dict[str, Any]:
    try:
        now = datetime.now()
        last_hour = now - timedelta(hours=1)
        last_24h = now - timedelta(hours=24)
        last_7d = now - timedelta(days=7)

        total_requests = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs"
        ) or 0

        hour_requests = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE timestamp >= :since",
            {"since": last_hour}
        ) or 0

        day_requests = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE timestamp >= :since",
            {"since": last_24h}
        ) or 0

        success_count = await database.fetch_val(
            """SELECT COUNT(*) FROM agent_usage_logs 
               WHERE timestamp >= :since AND status_code < 400""",
            {"since": last_24h}
        ) or 0
        success_rate = (success_count / day_requests * 100) if day_requests > 0 else 100

        avg_response_time = await database.fetch_val(
            """SELECT AVG(response_time_ms) FROM agent_usage_logs 
               WHERE timestamp >= :since AND response_time_ms IS NOT NULL""",
            {"since": last_24h}
        ) or 0

        top_endpoints = await database.fetch_all(
            """SELECT endpoint, COUNT(*) as count 
               FROM agent_usage_logs 
               WHERE timestamp >= :since 
               GROUP BY endpoint 
               ORDER BY count DESC 
               LIMIT 10""",
            {"since": last_24h}
        )

        active_agents = await database.fetch_val(
            """SELECT COUNT(DISTINCT agent_id) FROM agent_usage_logs 
               WHERE timestamp >= :since""",
            {"since": last_24h}
        ) or 0

        # Revenue from orders table (last 24h)
        revenue = await database.fetch_val(
            """SELECT COALESCE(SUM(total), 0) FROM orders 
               WHERE created_at >= :since AND (is_deleted IS NULL OR is_deleted = FALSE)""",
            {"since": last_24h}
        ) or 0

        return {
            "status": "healthy",
            "timestamp": now.isoformat(),
            "overview": {
                "total_requests": total_requests,
                "requests_last_hour": hour_requests,
                "requests_last_24h": day_requests,
                "requests_last_7d": await database.fetch_val(
                    "SELECT COUNT(*) FROM agent_usage_logs WHERE timestamp >= :since",
                    {"since": last_7d}
                ) or 0,
            },
            "performance": {
                "success_rate_24h": round(success_rate, 2),
                "avg_response_time_ms": round(float(avg_response_time), 2) if avg_response_time else 0,
            },
            "agents": {
                "active_last_24h": active_agents,
            },
            "orders": {
                "count_last_24h": await database.fetch_val(
                    """SELECT COUNT(*) FROM agent_usage_logs 
                       WHERE timestamp >= :since AND endpoint LIKE '%/orders%' AND status_code < 300""",
                    {"since": last_24h}
                ) or 0,
                "revenue_last_24h": float(revenue),
            },
            "top_endpoints": [
                {"endpoint": row["endpoint"], "count": row["count"]}
                for row in top_endpoints
            ],
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "timestamp": datetime.now().isoformat()}


@router.get("/recent")
async def get_recent_activity_v1(
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    agent_id: Optional[str] = None,
    request: Request = None
) -> Dict[str, Any]:
    try:
        agent_filter = ""
        params = {"limit": limit, "offset": offset}

        resolved_agent_id = agent_id
        if not resolved_agent_id and request:
            api_key = request.headers.get("x-api-key")
            if api_key:
                try:
                    row = await database.fetch_one(
                        "SELECT agent_id FROM agents WHERE api_key = :k LIMIT 1",
                        {"k": api_key}
                    )
                    if row:
                        resolved_agent_id = row["agent_id"]
                except Exception:
                    pass

        if resolved_agent_id:
            agent_filter = "WHERE agent_id = :agent_id"
            params["agent_id"] = resolved_agent_id

        activities = await database.fetch_all(
            f"""
            SELECT 
                id, agent_id, endpoint, method, status_code, response_time_ms, timestamp
            FROM agent_usage_logs
            {agent_filter}
            ORDER BY timestamp DESC
            LIMIT :limit OFFSET :offset
            """,
            params
        )

        formatted = [
            {
                "id": str(a["id"]),
                "agent_id": a["agent_id"],
                "endpoint": a["endpoint"],
                "method": a["method"],
                "status_code": a["status_code"],
                "response_time_ms": a["response_time_ms"],
                "timestamp": a["timestamp"].isoformat() if a["timestamp"] else None,
            }
            for a in activities
        ]

        return {"status": "success", "activities": formatted, "count": len(formatted), "limit": limit, "offset": offset}
    except Exception as e:
        return {"status": "success", "activities": [], "count": 0, "note": str(e)}

@router.get("/timeline")
async def get_metrics_timeline_v1(hours: int = 24) -> Dict[str, Any]:
    try:
        since = datetime.now() - timedelta(hours=hours)
        timeline = await database.fetch_all(
            """
            SELECT 
                DATE_TRUNC('hour', timestamp) as hour,
                COUNT(*) as total_requests,
                SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) as successful_requests,
                AVG(response_time_ms) as avg_response_time
            FROM agent_usage_logs
            WHERE timestamp >= :since
            GROUP BY DATE_TRUNC('hour', timestamp)
            ORDER BY hour DESC
            """,
            {"since": since}
        )
        return {
            "timeline": [
                {
                    "hour": row["hour"].isoformat(),
                    "total_requests": row["total_requests"],
                    "successful_requests": row["successful_requests"],
                    "avg_response_time_ms": round(float(row["avg_response_time"] or 0), 2),
                }
                for row in timeline
            ],
            "period_hours": hours,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


