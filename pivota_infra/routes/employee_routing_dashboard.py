"""
Employee Routing Dashboard API Routes - Phase 4
Monitoring and management endpoints for payment routing
"""
from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json

from db.database import database
from services.payment_metrics_collector import PaymentMetricsCollector
from utils.auth import get_current_employee


router = APIRouter(prefix="/employee/psp", tags=["Employee PSP Dashboard"])


# Response Models
class PSPPerformanceResponse(BaseModel):
    psp_name: str
    current_status: str  # healthy, degraded, down
    success_rate_5min: float
    success_rate_1h: float
    avg_response_time_ms: int
    total_attempts_1h: int
    failover_triggered_count: int
    alerts: List[Dict[str, Any]]


class RoutingOverviewResponse(BaseModel):
    total_routes: int
    active_routes: int
    total_agents: int
    routes_by_strategy: Dict[str, int]
    top_performing_routes: List[Dict[str, Any]]
    recent_changes: List[Dict[str, Any]]


class FailoverEventResponse(BaseModel):
    event_id: str
    order_id: str
    agent_id: str
    primary_psp: str
    failover_psp: str
    primary_error: str
    success: bool
    total_attempts: int
    timestamp: str


# Initialize metrics collector
metrics_collector = PaymentMetricsCollector(database)


@router.get("/performance", response_model=List[PSPPerformanceResponse])
async def get_psp_performance(
    include_inactive: bool = Query(False, description="Include inactive PSPs"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Get real-time PSP performance metrics
    """
    # Collect current metrics
    metrics_data = await metrics_collector.collect_psp_metrics()
    psp_metrics = metrics_data.get("psps", {})
    
    performance_list = []
    
    for psp_name, metrics in psp_metrics.items():
        # Determine PSP status
        success_rate_5min = metrics["current_5min"]["success_rate"]
        
        if success_rate_5min >= 95:
            status = "healthy"
        elif success_rate_5min >= 80:
            status = "degraded"
        else:
            status = "down"
        
        # Count failovers where this PSP was primary
        failover_count = await database.fetch_one(
            """
            SELECT COUNT(DISTINCT order_id) as count
            FROM payment_attempts
            WHERE psp_name = :psp_name
            AND attempt_number = 1
            AND status = 'failed'
            AND created_at >= :cutoff
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(hours=1)
            }
        )
        
        performance_list.append(
            PSPPerformanceResponse(
                psp_name=psp_name,
                current_status=status,
                success_rate_5min=success_rate_5min,
                success_rate_1h=metrics["last_hour"]["success_rate"],
                avg_response_time_ms=metrics["last_hour"]["avg_response_ms"],
                total_attempts_1h=metrics["last_hour"]["total_attempts"],
                failover_triggered_count=dict(failover_count)["count"] if failover_count else 0,
                alerts=metrics.get("alerts", [])
            )
        )
    
    # Sort by status (down first, then degraded, then healthy)
    status_order = {"down": 0, "degraded": 1, "healthy": 2}
    performance_list.sort(key=lambda x: status_order.get(x.current_status, 3))
    
    return performance_list


@router.get("/routes/overview", response_model=RoutingOverviewResponse)
async def get_routing_overview(
    current_user: dict = Depends(get_current_employee)
):
    """
    Get overview of all routing configurations
    """
    # Count routes
    route_stats = await database.fetch_one(
        """
        SELECT 
            COUNT(*) as total_routes,
            COUNT(CASE WHEN is_active THEN 1 END) as active_routes,
            COUNT(DISTINCT agent_id) as total_agents
        FROM payment_routes
        """
    )
    
    # Routes by strategy
    strategy_breakdown = await database.fetch_all(
        """
        SELECT routing_strategy, COUNT(*) as count
        FROM payment_routes
        WHERE is_active = true
        GROUP BY routing_strategy
        """
    )
    
    # Top performing routes (last 24h)
    top_routes = await database.fetch_all(
        """
        SELECT 
            pr.route_id,
            pr.agent_id,
            a.name as agent_name,
            COUNT(pa.attempt_id) as total_attempts,
            COUNT(CASE WHEN pa.status = 'success' THEN 1 END) as successful,
            AVG(pa.response_time_ms) as avg_response_time
        FROM payment_routes pr
        LEFT JOIN payment_attempts pa ON pr.route_id = pa.route_id
        LEFT JOIN agents a ON pr.agent_id = a.agent_id
        WHERE pa.created_at >= :cutoff
        GROUP BY pr.route_id, pr.agent_id, a.name
        HAVING COUNT(pa.attempt_id) > 0
        ORDER BY (COUNT(CASE WHEN pa.status = 'success' THEN 1 END)::FLOAT / COUNT(pa.attempt_id)) DESC
        LIMIT 10
        """,
        {"cutoff": datetime.utcnow() - timedelta(hours=24)}
    )
    
    # Recent route changes
    recent_changes = await database.fetch_all(
        """
        SELECT 
            pr.route_id,
            pr.agent_id,
            a.name as agent_name,
            pr.updated_at,
            pr.routing_strategy
        FROM payment_routes pr
        LEFT JOIN agents a ON pr.agent_id = a.agent_id
        WHERE pr.updated_at >= :cutoff
        ORDER BY pr.updated_at DESC
        LIMIT 10
        """,
        {"cutoff": datetime.utcnow() - timedelta(days=7)}
    )
    
    stats = dict(route_stats) if route_stats else {}
    
    return RoutingOverviewResponse(
        total_routes=stats.get("total_routes", 0),
        active_routes=stats.get("active_routes", 0),
        total_agents=stats.get("total_agents", 0),
        routes_by_strategy={
            s["routing_strategy"]: s["count"]
            for s in strategy_breakdown
        },
        top_performing_routes=[
            {
                "route_id": r["route_id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "success_rate": (r["successful"] / r["total_attempts"] * 100) if r["total_attempts"] > 0 else 0,
                "total_attempts": r["total_attempts"],
                "avg_response_time_ms": r["avg_response_time"]
            }
            for r in top_routes
        ],
        recent_changes=[
            {
                "route_id": r["route_id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "updated_at": r["updated_at"].isoformat(),
                "strategy": r["routing_strategy"]
            }
            for r in recent_changes
        ]
    )


@router.get("/failovers", response_model=List[FailoverEventResponse])
async def get_failover_events(
    hours: int = Query(24, description="Hours to look back"),
    limit: int = Query(100, description="Maximum events to return"),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Get recent failover events
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    query = """
        SELECT DISTINCT ON (pa1.order_id)
            CONCAT('fail_', pa1.order_id, '_', pa1.created_at) as event_id,
            pa1.order_id,
            pa1.agent_id,
            pa1.psp_name as primary_psp,
            pa1.error_message as primary_error,
            pa2.psp_name as failover_psp,
            pa2.status as failover_status,
            MAX(pa2.attempt_number) as total_attempts,
            pa1.created_at
        FROM payment_attempts pa1
        JOIN payment_attempts pa2 ON pa1.order_id = pa2.order_id
        WHERE pa1.attempt_number = 1
        AND pa1.status = 'failed'
        AND pa2.attempt_number > 1
        AND pa1.created_at >= :cutoff
    """
    
    params = {"cutoff": cutoff}
    
    if agent_id:
        query += " AND pa1.agent_id = :agent_id"
        params["agent_id"] = agent_id
    
    query += """
        GROUP BY pa1.order_id, pa1.agent_id, pa1.psp_name, 
                 pa1.error_message, pa2.psp_name, pa2.status, pa1.created_at
        ORDER BY pa1.order_id, pa1.created_at DESC
        LIMIT :limit
    """
    
    params["limit"] = limit
    
    failovers = await database.fetch_all(query, params)
    
    return [
        FailoverEventResponse(
            event_id=f["event_id"],
            order_id=f["order_id"],
            agent_id=f["agent_id"],
            primary_psp=f["primary_psp"],
            failover_psp=f["failover_psp"],
            primary_error=f["primary_error"] or "Unknown error",
            success=f["failover_status"] == "success",
            total_attempts=f["total_attempts"],
            timestamp=f["created_at"].isoformat()
        )
        for f in failovers
    ]


@router.post("/routes/{route_id}/test")
async def test_route_configuration(
    route_id: str = Path(..., description="Route ID to test"),
    test_amount: float = Query(100.0, description="Test amount"),
    test_currency: str = Query("USD", description="Test currency"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Test a routing configuration with simulated payment
    """
    # Get route configuration
    route = await database.fetch_one(
        """
        SELECT route_id, agent_id, merchant_id, psp_priority, routing_strategy
        FROM payment_routes
        WHERE route_id = :route_id
        """,
        {"route_id": route_id}
    )
    
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    
    route_dict = dict(route)
    psp_priority = json.loads(route_dict["psp_priority"]) if isinstance(route_dict["psp_priority"], str) else route_dict["psp_priority"]
    
    # Simulate testing each PSP in order
    test_results = []
    
    for psp_config in psp_priority:
        psp_name = psp_config.get("psp")
        priority = psp_config.get("priority")
        
        # Check recent PSP performance
        recent_perf = await database.fetch_one(
            """
            SELECT 
                AVG(CASE WHEN status = 'success' THEN 100.0 ELSE 0.0 END) as success_rate,
                AVG(response_time_ms) as avg_response_time
            FROM payment_attempts
            WHERE psp_name = :psp_name
            AND created_at >= :cutoff
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(hours=1)
            }
        )
        
        perf = dict(recent_perf) if recent_perf else {"success_rate": 100.0, "avg_response_time": 250}
        
        test_results.append({
            "psp": psp_name,
            "priority": priority,
            "test_status": "available",
            "recent_success_rate": perf.get("success_rate", 0),
            "avg_response_time_ms": perf.get("avg_response_time", 0),
            "recommendation": "healthy" if perf.get("success_rate", 0) >= 95 else "monitor"
        })
    
    return {
        "route_id": route_id,
        "agent_id": route_dict["agent_id"],
        "merchant_id": route_dict["merchant_id"],
        "routing_strategy": route_dict["routing_strategy"],
        "test_amount": test_amount,
        "test_currency": test_currency,
        "psp_test_results": test_results,
        "overall_health": "healthy" if all(r["recommendation"] == "healthy" for r in test_results) else "needs_attention",
        "tested_at": datetime.utcnow().isoformat()
    }


@router.get("/metrics/realtime")
async def get_realtime_metrics(
    current_user: dict = Depends(get_current_employee)
):
    """
    Get real-time payment routing metrics for monitoring
    """
    # Current 5-minute window metrics
    current_window = await database.fetch_one(
        """
        SELECT 
            COUNT(*) as total_attempts,
            COUNT(CASE WHEN status = 'success' THEN 1 END) as successful,
            COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
            COUNT(CASE WHEN attempt_number > 1 THEN 1 END) as failovers,
            COUNT(DISTINCT order_id) as unique_payments,
            COUNT(DISTINCT agent_id) as active_agents,
            AVG(response_time_ms) as avg_response_time
        FROM payment_attempts
        WHERE created_at >= :cutoff
        """,
        {"cutoff": datetime.utcnow() - timedelta(minutes=5)}
    )
    
    # PSP distribution
    psp_distribution = await database.fetch_all(
        """
        SELECT 
            psp_name,
            COUNT(*) as attempts,
            COUNT(CASE WHEN status = 'success' THEN 1 END) as successful
        FROM payment_attempts
        WHERE created_at >= :cutoff
        GROUP BY psp_name
        """,
        {"cutoff": datetime.utcnow() - timedelta(minutes=5)}
    )
    
    # Critical alerts
    failures = await metrics_collector.detect_psp_failures()
    critical_alerts = [f for f in failures if f.get("severity") == "critical"]
    
    current = dict(current_window) if current_window else {}
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "current_5min": {
            "total_attempts": current.get("total_attempts", 0),
            "success_rate": (current.get("successful", 0) / current.get("total_attempts", 1) * 100) if current.get("total_attempts", 0) > 0 else 0,
            "failover_rate": (current.get("failovers", 0) / current.get("total_attempts", 1) * 100) if current.get("total_attempts", 0) > 0 else 0,
            "unique_payments": current.get("unique_payments", 0),
            "active_agents": current.get("active_agents", 0),
            "avg_response_time_ms": current.get("avg_response_time", 0)
        },
        "psp_distribution": [
            {
                "psp": p["psp_name"],
                "percentage": (p["attempts"] / current.get("total_attempts", 1) * 100) if current.get("total_attempts", 0) > 0 else 0,
                "success_rate": (p["successful"] / p["attempts"] * 100) if p["attempts"] > 0 else 0
            }
            for p in psp_distribution
        ],
        "critical_alerts": critical_alerts,
        "health_status": "critical" if critical_alerts else ("warning" if current.get("failovers", 0) > 5 else "healthy")
    }


@router.post("/metrics/collect")
async def trigger_metrics_collection(
    current_user: dict = Depends(get_current_employee)
):
    """
    Manually trigger metrics collection cycle
    """
    result = await metrics_collector.run_collection_cycle()
    
    if result.get("success"):
        return {
            "status": "success",
            "message": "Metrics collection completed",
            "timestamp": result.get("timestamp"),
            "summary": {
                "psps_monitored": len(result.get("psp_metrics", {}).get("psps", {})),
                "failures_detected": len(result.get("failures_detected", [])),
                "routes_analyzed": result.get("route_efficiency", {}).get("total_routes", 0)
            }
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Metrics collection failed: {result.get('error')}"
        )


@router.get("/alerts/active")
async def get_active_psp_alerts(
    current_user: dict = Depends(get_current_employee)
):
    """
    Get currently active PSP alerts
    """
    # Detect current issues
    failures = await metrics_collector.detect_psp_failures()
    
    # Group by severity
    alerts_by_severity = {
        "critical": [],
        "warning": [],
        "info": []
    }
    
    for failure in failures:
        severity = failure.get("severity", "info")
        alerts_by_severity[severity].append(failure)
    
    # Get recent incident history
    recent_incidents = await database.fetch_all(
        """
        SELECT 
            psp_name,
            COUNT(CASE WHEN status = 'failed' THEN 1 END) as failure_count,
            MIN(created_at) as first_failure,
            MAX(created_at) as last_failure
        FROM payment_attempts
        WHERE status = 'failed'
        AND created_at >= :cutoff
        GROUP BY psp_name
        HAVING COUNT(CASE WHEN status = 'failed' THEN 1 END) > 5
        ORDER BY failure_count DESC
        """,
        {"cutoff": datetime.utcnow() - timedelta(hours=1)}
    )
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_alerts": len(failures),
        "alerts": alerts_by_severity,
        "recent_incidents": [
            {
                "psp": i["psp_name"],
                "failure_count": i["failure_count"],
                "duration_minutes": int((i["last_failure"] - i["first_failure"]).total_seconds() / 60),
                "first_failure": i["first_failure"].isoformat(),
                "last_failure": i["last_failure"].isoformat()
            }
            for i in recent_incidents
        ]
    }

