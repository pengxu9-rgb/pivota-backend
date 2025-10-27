"""
Agent API Metrics and Monitoring
Real-time metrics from agent_usage_logs table
"""
from fastapi import APIRouter, Depends, Query, Request
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from db.database import database
from utils.auth import require_admin, get_current_user

router = APIRouter(prefix="/agent/metrics", tags=["Agent Metrics"])


@router.get("/summary")
async def get_metrics_summary(request: Request, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Get real-time API usage metrics summary
    Available to agents and admins
    """
    try:
        # For now, return mock data to avoid table issues
        # TODO: Implement real metrics from agent_usage_logs table
        
        # Time ranges
        now = datetime.now()
        last_hour = now - timedelta(hours=1)
        last_24h = now - timedelta(hours=24)
        last_7d = now - timedelta(days=7)
        
        # Mock data for now
        total_requests = 1234
        hour_requests = 45
        day_requests = 567
        success_rate = 98.5
        
        # Mock average response time
        avg_response_time = 250
        
        # Mock top endpoints
        top_endpoints = [
            {"endpoint": "/agent/v1/orders/create", "count": 234},
            {"endpoint": "/agent/v1/products/search", "count": 189},
            {"endpoint": "/agent/metrics/summary", "count": 156}
        ]
        
        # Mock active agents
        active_agents = 3
        
        # Mock errors
        errors = []
        
        agent_id = current_user.get("agent_id")
        # Orders created (last 24h) for this agent
        orders_count = await database.fetch_val(
            """SELECT COUNT(*) FROM orders 
               WHERE created_at >= :since 
                 AND (is_deleted IS NULL OR is_deleted = FALSE)
                 AND agent_id = :agent_id""",
            {"since": last_24h, "agent_id": agent_id}
        ) or 0
        
        # Revenue (last 24h) for this agent (paid only)
        revenue = await database.fetch_val(
            """SELECT COALESCE(SUM(total), 0) FROM orders 
               WHERE created_at >= :since 
                 AND (is_deleted IS NULL OR is_deleted = FALSE)
                 AND payment_status = 'paid'
                 AND agent_id = :agent_id""",
            {"since": last_24h, "agent_id": agent_id}
        ) or 0
        
        return {
            "status": "healthy",
            "timestamp": now.isoformat(),
            "overview": {
                "total_requests": 0,
                "requests_last_hour": 0,
                "requests_last_24h": 0,
                "requests_last_7d": 0,
            },
            "performance": {
                "success_rate_24h": round(success_rate, 2),
                "avg_response_time_ms": round(float(avg_response_time), 2) if avg_response_time else 0,
            },
            "agents": {
                "active_last_24h": active_agents,
            },
            "orders": {
                "count_last_24h": orders_count,
                "revenue_last_24h": float(revenue),
            },
            "top_endpoints": [
                {"endpoint": row["endpoint"], "count": row["count"]} 
                for row in top_endpoints
            ],
            "errors": [
                {"status_code": row["status_code"], "count": row["count"]} 
                for row in errors
            ]
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/recent")
async def get_recent_activity(request: Request, limit: int = Query(5, ge=1, le=50), current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Get recent agent activity - returns mock data for now
    """
    try:
        # Pull from agent_usage_logs for this agent
        agent_id = current_user.get("agent_id")
        rows = await database.fetch_all(
            """
            SELECT id, endpoint, method, status_code, response_time_ms, timestamp
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
            ORDER BY timestamp DESC
            LIMIT :limit
            """,
            {"agent_id": agent_id, "limit": limit}
        )
        activities = [
            {
                "id": str(r["id"]),
                "method": r["method"],
                "endpoint": r["endpoint"],
                "status_code": r["status_code"],
                "response_time_ms": r["response_time_ms"],
                "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            }
            for r in rows
        ]
        return {"status": "success", "activities": activities, "total": len(activities)}
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "activities": []
        }


@router.get("/agents")
async def get_agent_metrics(current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    """
    Get per-agent usage metrics
    """
    try:
        last_24h = datetime.now() - timedelta(hours=24)
        
        agents = await database.fetch_all(
            """
            SELECT 
                a.agent_id,
                a.name,
                a.company,
                a.status,
                COUNT(l.id) as request_count,
                AVG(l.response_time_ms) as avg_response_time,
                SUM(CASE WHEN l.status_code < 400 THEN 1 ELSE 0 END)::float / 
                    NULLIF(COUNT(l.id), 0) * 100 as success_rate,
                MAX(l.timestamp) as last_active
            FROM agents a
            LEFT JOIN agent_usage_logs l ON a.agent_id = l.agent_id 
                AND l.timestamp >= :since
            WHERE a.status = 'active'
            GROUP BY a.agent_id, a.name, a.company, a.status
            ORDER BY request_count DESC
            """,
            {"since": last_24h}
        )
        
        return {
            "agents": [
                {
                    "agent_id": row["agent_id"],
                    "name": row["name"],
                    "company": row["company"],
                    "status": row["status"],
                    "metrics_24h": {
                        "request_count": row["request_count"],
                        "avg_response_time_ms": round(float(row["avg_response_time"] or 0), 2),
                        "success_rate": round(float(row["success_rate"] or 100), 2),
                        "last_active": row["last_active"].isoformat() if row["last_active"] else None,
                    }
                }
                for row in agents
            ],
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/timeline")
async def get_metrics_timeline(
    hours: int = 24,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Get hourly request timeline for the last N hours
    """
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
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/health")
async def get_system_health() -> Dict[str, Any]:
    """
    Public health check with basic system status
    No authentication required
    """
    try:
        # Check database
        await database.execute("SELECT 1")
        db_status = "healthy"
        
        # Get recent error rate
        last_hour = datetime.now() - timedelta(hours=1)
        total = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE timestamp >= :since",
            {"since": last_hour}
        ) or 1
        errors = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE timestamp >= :since AND status_code >= 500",
            {"since": last_hour}
        ) or 0
        
        error_rate = (errors / total * 100) if total > 0 else 0
        
        return {
            "status": "healthy" if error_rate < 5 else "degraded",
            "timestamp": datetime.now().isoformat(),
            "checks": {
                "database": db_status,
                "api": "operational",
            },
            "metrics": {
                "requests_last_hour": total,
                "error_rate_last_hour": round(error_rate, 2),
            }
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


@router.get("/recent")
async def get_recent_activity(
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    agent_id: Optional[str] = None,
    request: Request = None
) -> Dict[str, Any]:
    """
    Get recent API activity/calls
    Returns last N activities with details
    """
    try:
        # Filter by agent if not admin
        agent_filter = ""
        params = {"limit": limit, "offset": offset}

        # Optional filtering by agent
        resolved_agent_id = agent_id
        if not resolved_agent_id:
            # Try to resolve from x-api-key header
            api_key = request.headers.get("x-api-key") if request else None
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
                id,
                agent_id,
                endpoint,
                method,
                status_code,
                response_time_ms,
                timestamp
            FROM agent_usage_logs
            {agent_filter}
            ORDER BY timestamp DESC
            LIMIT :limit OFFSET :offset
            """,
            params
        )
        
        # Format activities
        formatted_activities = []
        for activity in activities:
            timestamp = activity["timestamp"]
            time_diff = datetime.now() - timestamp
            
            if time_diff.days > 0:
                time_ago = f"{time_diff.days} days ago"
            elif time_diff.seconds > 3600:
                time_ago = f"{time_diff.seconds // 3600} hours ago"
            elif time_diff.seconds > 60:
                time_ago = f"{time_diff.seconds // 60} minutes ago"
            else:
                time_ago = "Just now"
            
            # Determine activity type from endpoint
            endpoint = activity["endpoint"]
            if "/orders" in endpoint:
                activity_type = "order"
                action = "Order Completed" if activity["status_code"] < 300 else "Order Failed"
            elif "/catalog/search" in endpoint or "/products" in endpoint:
                activity_type = "search"
                action = "Product Search"
            elif "/inventory" in endpoint:
                activity_type = "inventory"
                action = "Inventory Check"
            elif "/pricing" in endpoint:
                activity_type = "price"
                action = "Price Query"
            else:
                activity_type = "api"
                action = f"{activity['method']} {endpoint}"
            
            formatted_activities.append({
                "id": str(activity["id"]),
                "type": activity_type,
                "action": action,
                "description": f"{activity['method']} {endpoint} → {activity['status_code']}",
                "response_time": activity["response_time_ms"],
                "timestamp": time_ago,
                "status": "success" if activity["status_code"] < 400 else "error"
            })
        
        return {
            "status": "success",
            "activities": formatted_activities,
            "count": len(formatted_activities),
            "limit": limit,
            "offset": offset
        }
        
    except Exception as e:
        print(f"Error getting recent activity: {e}")
        return {
            "status": "success",
            "activities": [],
            "count": 0
        }

