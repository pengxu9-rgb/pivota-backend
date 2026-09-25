"""
Agent API Metrics and Monitoring
Real-time metrics from agent_usage_logs table
"""
from fastapi import APIRouter, Depends, Query, Request, Header, HTTPException
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
from db.database import database
from db.agents import resolve_active_agent_id, resolve_agent_id_by_api_key
from utils.auth import ADMIN_ROLES, require_admin, decode_token

router = APIRouter(prefix="/agent/metrics", tags=["Agent Metrics"])


@router.get("/summary")
async def get_metrics_summary(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
) -> Dict[str, Any]:
    """
    Get real-time API usage metrics summary
    Available to agents and admins
    """
    try:
        # Time ranges
        now = datetime.now()
        last_hour = now - timedelta(hours=1)
        last_24h = now - timedelta(hours=24)
        last_30d = now - timedelta(days=30)
        
        # Resolve agent_id from JWT or X-API-Key
        agent_id = None
        employee_context = False
        if authorization and authorization.startswith("Bearer "):
            try:
                payload = decode_token(authorization.split(" ")[1])
                role = payload.get("role")
                if role in ["super_admin", "admin", "employee", "outsourced"]:
                    employee_context = True
                agent_id = await resolve_active_agent_id(payload.get("agent_id"))
            except:
                pass
        if not agent_id and x_api_key:
            agent_id = await resolve_agent_id_by_api_key(x_api_key)
        if not agent_id and not employee_context:
            raise HTTPException(status_code=401, detail="Missing or invalid agent credentials")

        status_label = "healthy"
        internal_errors: List[str] = []

        # Query real data from agent_usage_logs (agent-scoped or global)
        agent_filter = " AND agent_id = :agent_id" if agent_id else ""
        last_7d = now - timedelta(days=7)
        agent_params = {"agent_id": agent_id} if agent_id else {}
        try:
            usage_row = await database.fetch_one(
                f"""
                SELECT
                    COUNT(*)::bigint as total_requests,
                    SUM(CASE WHEN timestamp >= :last_hour THEN 1 ELSE 0 END)::bigint as requests_last_hour,
                    SUM(CASE WHEN timestamp >= :last_24h THEN 1 ELSE 0 END)::bigint as requests_last_24h,
                    SUM(CASE WHEN timestamp >= :last_7d THEN 1 ELSE 0 END)::bigint as requests_last_7d,
                    SUM(CASE WHEN timestamp >= :last_24h AND status_code < 400 THEN 1 ELSE 0 END)::bigint as success_last_24h,
                    AVG(CASE WHEN timestamp >= :last_24h THEN response_time_ms END) as avg_response_time_ms,
                    COUNT(DISTINCT CASE WHEN timestamp >= :last_24h THEN agent_id END)::bigint as active_agents_last_24h
                FROM agent_usage_logs
                WHERE 1=1 {agent_filter}
                """,
                {
                    "last_hour": last_hour,
                    "last_24h": last_24h,
                    "last_7d": last_7d,
                    **agent_params,
                }
            )
            usage = dict(usage_row) if usage_row else {}
        except Exception as e:
            usage = {}
            status_label = "degraded"
            internal_errors.append(f"agent_usage_logs query failed: {str(e)}")
        total_requests = int(usage.get("total_requests") or 0)
        hour_requests = int(usage.get("requests_last_hour") or 0)
        day_requests = int(usage.get("requests_last_24h") or 0)
        week_requests = int(usage.get("requests_last_7d") or 0)
        success_count = int(usage.get("success_last_24h") or 0)
        success_rate = (success_count / day_requests * 100) if day_requests > 0 else 100.0
        avg_response_time = usage.get("avg_response_time_ms") or 0
        active_agents = int(usage.get("active_agents_last_24h") or 0)
        
        # Top endpoints (last 24h)
        try:
            top_endpoint_rows = await database.fetch_all(
                f"""SELECT endpoint, COUNT(*) as count 
                   FROM agent_usage_logs 
                   WHERE timestamp >= :since {agent_filter}
                   GROUP BY endpoint 
                   ORDER BY count DESC 
                   LIMIT 5""",
                {"since": last_24h, **agent_params}
            )
            top_endpoints = [{"endpoint": row["endpoint"], "count": row["count"]} for row in top_endpoint_rows]
        except Exception as e:
            top_endpoints = []
            status_label = "degraded"
            internal_errors.append(f"top_endpoints query failed: {str(e)}")

        try:
            error_rows = await database.fetch_all(
                f"""SELECT status_code, COUNT(*) as count
                   FROM agent_usage_logs
                   WHERE status_code >= 400 AND timestamp >= :since {agent_filter}
                   GROUP BY status_code
                   ORDER BY count DESC
                   LIMIT 5""",
                {"since": last_24h, **agent_params}
            )
            errors = [{"status_code": row["status_code"], "count": row["count"]} for row in error_rows]
        except Exception as e:
            errors = []
            status_label = "degraded"
            internal_errors.append(f"error breakdown query failed: {str(e)}")

        try:
            orders_row = await database.fetch_one(
                f"""
                SELECT
                    SUM(CASE WHEN created_at >= :last_24h THEN 1 ELSE 0 END)::bigint as count_last_24h,
                    COALESCE(SUM(CASE WHEN created_at >= :last_24h AND payment_status = 'paid' THEN total ELSE 0 END), 0) as revenue_last_24h,
                    COALESCE(SUM(CASE WHEN created_at >= :last_30d AND payment_status = 'paid' THEN total ELSE 0 END), 0) as revenue_last_30d,
                    COUNT(*)::bigint as total_orders,
                    SUM(CASE WHEN payment_status = 'paid' THEN 1 ELSE 0 END)::bigint as total_paid_orders,
                    COALESCE(SUM(CASE WHEN payment_status = 'paid' THEN total ELSE 0 END), 0) as total_revenue,
                    COUNT(DISTINCT merchant_id)::bigint as merchant_count
                FROM orders
                WHERE (is_deleted IS NULL OR is_deleted = FALSE)
                  {agent_filter}
                """,
                {
                    "last_24h": last_24h,
                    "last_30d": last_30d,
                    **agent_params,
                }
            )
            orders = dict(orders_row) if orders_row else {}
        except Exception as e:
            orders = {}
            status_label = "degraded"
            internal_errors.append(f"orders query failed: {str(e)}")
        orders_count_24h = int(orders.get("count_last_24h") or 0)
        revenue_24h = float(orders.get("revenue_last_24h") or 0)
        revenue_30d = float(orders.get("revenue_last_30d") or 0)
        total_orders = int(orders.get("total_orders") or 0)
        total_paid_orders = int(orders.get("total_paid_orders") or 0)
        total_revenue = float(orders.get("total_revenue") or 0)
        merchant_count = int(orders.get("merchant_count") or 0)
        
        return {
            "status": status_label,
            "timestamp": now.isoformat(),
            "overview": {
                "total_requests": total_requests,
                "requests_last_hour": hour_requests,
                "requests_last_24h": day_requests,
                "requests_last_7d": week_requests,
            },
            "performance": {
                "success_rate_24h": round(success_rate, 2),
                "avg_response_time_ms": round(float(avg_response_time), 2) if avg_response_time else 0,
            },
            "agents": {
                "active_last_24h": active_agents if employee_context else (active_agents or 1),
            },
            "orders": {
                # Last 24h metrics
                "count_last_24h": orders_count_24h,
                "revenue_last_24h": float(revenue_24h),
                # Last 30d metrics
                "revenue_last_30d": float(revenue_30d),
                # All-time metrics (for dashboard calculations)
                "total_orders": total_orders,
                "total_paid_orders": total_paid_orders,
                "total_revenue": float(total_revenue),
            },
            "merchants": {
                "total_count": merchant_count,
            },
            "top_endpoints": [
                {"endpoint": row["endpoint"], "count": row["count"]} 
                for row in top_endpoints
            ],
            "errors": [
                {"status_code": row["status_code"], "count": row["count"]} 
                for row in errors
            ],
            "debug_errors": internal_errors,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "degraded",
            "timestamp": datetime.now().isoformat(),
            "overview": {
                "total_requests": 0,
                "requests_last_hour": 0,
                "requests_last_24h": 0,
                "requests_last_7d": 0,
            },
            "performance": {
                "success_rate_24h": 0,
                "avg_response_time_ms": 0,
            },
            "agents": {
                "active_last_24h": 0,
            },
            "orders": {
                "count_last_24h": 0,
                "revenue_last_24h": 0.0,
                "revenue_last_30d": 0.0,
                "total_orders": 0,
                "total_paid_orders": 0,
                "total_revenue": 0.0,
            },
            "merchants": {
                "total_count": 0,
            },
            "top_endpoints": [],
            "errors": [],
            "debug_errors": [str(e)],
        }


async def resolve_recent_activity_scope(
    authorization: Optional[str],
    x_api_key: Optional[str],
    requested_agent_id: Optional[str],
) -> Optional[str]:
    """The agent_id whose agent_usage_logs rows the caller may read, or None for every agent.

    Shared by /agent/metrics/recent and /agent/v1/metrics/recent. Both used to take `agent_id`
    from the query string as the filter with no credential, and with neither it nor an x-api-key
    they ran unfiltered, so an anonymous caller read every agent's calls.

    - admin / super_admin JWT (ADMIN_ROLES, what require_admin admits): every agent, or the one
      named by `agent_id`;
    - agent JWT (its agent_id claim, the agent portal's session, through resolve_active_agent_id)
      or x-api-key (through resolve_agent_id_by_api_key), of an ACTIVE agent: that agent only;
      naming another agent is a 403;
    - anything else: 401.
    """
    payload: Dict[str, Any] = {}
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization.split(" ", 1)[1]) or {}
        except Exception:
            payload = {}
    if payload.get("role") in ADMIN_ROLES:
        return requested_agent_id or None

    # The claim proves who the caller was at login, not that the agent is still active.
    caller_agent_id = await resolve_active_agent_id(payload.get("agent_id"))
    if not caller_agent_id and x_api_key:
        try:
            caller_agent_id = await resolve_agent_id_by_api_key(x_api_key)
        except Exception:
            caller_agent_id = None
    if not caller_agent_id:
        raise HTTPException(status_code=401, detail="Missing or invalid agent credentials")
    if requested_agent_id and requested_agent_id != caller_agent_id:
        raise HTTPException(status_code=403, detail="Cannot read another agent's activity")
    return str(caller_agent_id)


@router.get("/agents")
async def get_agent_metrics(current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    """
    Get per-agent usage metrics
    """
    # agents has agent_name / owner_email / is_active (db/agents.py); this read name, company and
    # status, which it does not have, so every call answered {"status": "error"}. A NULL is_active
    # is active, the same as the auth door (routes/agent_auth.py).
    try:
        last_24h = datetime.now() - timedelta(hours=24)
        
        agents = await database.fetch_all(
            """
            SELECT 
                a.agent_id,
                a.agent_name,
                a.owner_email,
                COUNT(l.id) as request_count,
                AVG(l.response_time_ms) as avg_response_time,
                SUM(CASE WHEN l.status_code < 400 THEN 1 ELSE 0 END)::float / 
                    NULLIF(COUNT(l.id), 0) * 100 as success_rate,
                MAX(l.timestamp) as last_active
            FROM agents a
            LEFT JOIN agent_usage_logs l ON a.agent_id = l.agent_id 
                AND l.timestamp >= :since
            WHERE a.is_active IS NOT FALSE
            GROUP BY a.agent_id, a.agent_name, a.owner_email
            ORDER BY request_count DESC
            """,
            {"since": last_24h}
        )
        
        return {
            "agents": [
                {
                    "agent_id": row["agent_id"],
                    "name": row["agent_name"],
                    "owner_email": row["owner_email"],
                    "status": "active",
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
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
) -> Dict[str, Any]:
    """
    Get hourly request timeline for the last N hours (agent-scoped)
    """
    try:
        # Resolve agent_id from JWT or X-API-Key
        agent_id = None
        if authorization and authorization.startswith("Bearer "):
            try:
                payload = decode_token(authorization.split(" ")[1])
                agent_id = await resolve_active_agent_id(payload.get("agent_id"))
            except:
                pass
        if not agent_id and x_api_key:
            agent_id = await resolve_agent_id_by_api_key(x_api_key)
        
        if not agent_id:
            raise HTTPException(status_code=401, detail="Missing or invalid agent credentials")
        
        since = datetime.now() - timedelta(hours=hours)
        
        timeline = await database.fetch_all(
            """
            SELECT 
                DATE_TRUNC('hour', timestamp) as hour,
                COUNT(*) as total_requests,
                SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) as successful_requests,
                AVG(response_time_ms) as avg_response_time
            FROM agent_usage_logs
            WHERE timestamp >= :since AND agent_id = :agent_id
            GROUP BY DATE_TRUNC('hour', timestamp)
            ORDER BY hour DESC
            """,
            {"since": since, "agent_id": agent_id}
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
        
    except HTTPException:
        raise
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
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key")
) -> Dict[str, Any]:
    """
    Get recent API activity/calls
    Returns last N activities with details
    """
    try:
        agent_filter = ""
        params = {"limit": limit, "offset": offset}

        # None only for an admin reading every agent; see resolve_recent_activity_scope.
        resolved_agent_id = await resolve_recent_activity_scope(authorization, x_api_key, agent_id)
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
        
        # Format activities. agent_usage_logs.timestamp is timestamptz: a naive now() raised on the
        # first row and the catch-all below answered an empty "success".
        now = datetime.now(timezone.utc)
        formatted_activities = []
        for activity in activities:
            timestamp = activity["timestamp"]
            time_diff = now - timestamp
            
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
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting recent activity: {e}")
        return {
            "status": "success",
            "activities": [],
            "count": 0
        }
