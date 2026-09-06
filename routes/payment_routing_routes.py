"""
Payment Routing API Routes - Phase 4
Endpoints for payment routing with multi-PSP failover
"""
from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json

from db.database import database
from services.payment_routing_service import PaymentRoutingService
from utils.auth import ADMIN_ROLES, get_current_user, get_current_employee

# The guards here are `caller is the agent OR caller is an admin`. Only the
# second conjunct changes: the ownership test is untouched, so a caller who is
# neither the owning agent nor an admin is refused exactly as before. Not
# widened to staff -- an agent's payment-routing configuration is not a
# general employee-portal read today, and making it one is a separate call.


router = APIRouter(prefix="/agents", tags=["Payment Routing"])


# Request/Response Models
class RoutePaymentRequest(BaseModel):
    order_id: str = Field(..., description="Order ID for the payment")
    amount: float = Field(..., gt=0, description="Payment amount")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 currency code")
    merchant_id: Optional[str] = Field(None, description="Merchant ID (optional)")
    payment_method: Optional[Dict[str, Any]] = Field(None, description="Payment method details")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")


class RoutePaymentResponse(BaseModel):
    success: bool
    transaction_id: Optional[str] = None
    psp_used: Optional[str] = None
    attempt_number: int = 1
    response_time_ms: Optional[int] = None
    order_id: str
    status: str
    error: Optional[str] = None


class UpdateRouteRequest(BaseModel):
    psp_priority: List[Dict[str, Any]] = Field(..., description="PSP priority list")
    routing_strategy: str = Field("priority", description="Routing strategy: priority, cost, performance")
    max_retries: int = Field(2, ge=0, le=5, description="Maximum retry attempts")
    timeout_ms: int = Field(30000, gt=0, description="Timeout in milliseconds")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)


class RouteConfigResponse(BaseModel):
    route_id: str
    agent_id: str
    merchant_id: Optional[str]
    psp_priority: List[Dict[str, Any]]
    routing_strategy: str
    is_active: bool
    max_retries: int
    timeout_ms: int
    metadata: Dict[str, Any]
    created_at: str
    updated_at: str


class PaymentAttemptResponse(BaseModel):
    attempt_id: str
    order_id: str
    psp_name: str
    attempt_number: int
    status: str
    response_time_ms: Optional[int]
    error_code: Optional[str]
    error_message: Optional[str]
    amount: float
    currency: str
    created_at: str


# Initialize service
routing_service = PaymentRoutingService(database)


@router.post("/{agent_id}/payments/route", response_model=RoutePaymentResponse)
async def execute_payment_with_routing(
    agent_id: str = Path(..., description="Agent ID"),
    request: RoutePaymentRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    Execute payment with intelligent routing and automatic failover
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Execute payment with failover
        result = await routing_service.execute_with_failover(
            payment_request=request.dict(),
            agent_id=agent_id,
            merchant_id=request.merchant_id
        )
        
        return RoutePaymentResponse(
            success=result.get("success", False),
            transaction_id=result.get("transaction_id"),
            psp_used=result.get("psp_used"),
            attempt_number=result.get("attempt_number", 1),
            response_time_ms=result.get("response_time_ms"),
            order_id=request.order_id,
            status="success" if result.get("success") else "failed",
            error=result.get("error")
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payment routing failed: {str(e)}")


@router.get("/{agent_id}/routes", response_model=List[RouteConfigResponse])
async def get_agent_routes(
    agent_id: str = Path(..., description="Agent ID"),
    include_inactive: bool = Query(False, description="Include inactive routes"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get routing configurations for an agent
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    query = """
        SELECT 
            route_id, agent_id, merchant_id, psp_priority,
            routing_strategy, is_active, max_retries, timeout_ms,
            metadata, created_at, updated_at
        FROM payment_routes
        WHERE agent_id = :agent_id
    """
    
    params = {"agent_id": agent_id}
    
    if not include_inactive:
        query += " AND is_active = true"
    
    query += " ORDER BY created_at DESC"
    
    routes = await database.fetch_all(query, params)
    
    return [
        RouteConfigResponse(
            route_id=r["route_id"],
            agent_id=r["agent_id"],
            merchant_id=r["merchant_id"],
            psp_priority=json.loads(r["psp_priority"]) if isinstance(r["psp_priority"], str) else r["psp_priority"],
            routing_strategy=r["routing_strategy"],
            is_active=r["is_active"],
            max_retries=r["max_retries"],
            timeout_ms=r["timeout_ms"],
            metadata=json.loads(r["metadata"]) if isinstance(r["metadata"], str) else r["metadata"],
            created_at=r["created_at"].isoformat(),
            updated_at=r["updated_at"].isoformat()
        )
        for r in routes
    ]


@router.put("/{agent_id}/routes/{route_id}", response_model=RouteConfigResponse)
async def update_route_config(
    agent_id: str = Path(..., description="Agent ID"),
    route_id: str = Path(..., description="Route ID"),
    request: UpdateRouteRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    Update routing configuration
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Check route exists and belongs to agent
    existing = await database.fetch_one(
        "SELECT route_id FROM payment_routes WHERE route_id = :route_id AND agent_id = :agent_id",
        {"route_id": route_id, "agent_id": agent_id}
    )
    
    if not existing:
        raise HTTPException(status_code=404, detail="Route not found")
    
    # Update route
    await database.execute(
        """
        UPDATE payment_routes
        SET psp_priority = :psp_priority,
            routing_strategy = :routing_strategy,
            max_retries = :max_retries,
            timeout_ms = :timeout_ms,
            metadata = :metadata,
            updated_at = NOW()
        WHERE route_id = :route_id
        """,
        {
            "route_id": route_id,
            "psp_priority": json.dumps(request.psp_priority),
            "routing_strategy": request.routing_strategy,
            "max_retries": request.max_retries,
            "timeout_ms": request.timeout_ms,
            "metadata": json.dumps(request.metadata)
        }
    )
    
    # Fetch and return updated route
    route = await database.fetch_one(
        """
        SELECT 
            route_id, agent_id, merchant_id, psp_priority,
            routing_strategy, is_active, max_retries, timeout_ms,
            metadata, created_at, updated_at
        FROM payment_routes
        WHERE route_id = :route_id
        """,
        {"route_id": route_id}
    )
    
    return RouteConfigResponse(
        route_id=route["route_id"],
        agent_id=route["agent_id"],
        merchant_id=route["merchant_id"],
        psp_priority=json.loads(route["psp_priority"]) if isinstance(route["psp_priority"], str) else route["psp_priority"],
        routing_strategy=route["routing_strategy"],
        is_active=route["is_active"],
        max_retries=route["max_retries"],
        timeout_ms=route["timeout_ms"],
        metadata=json.loads(route["metadata"]) if isinstance(route["metadata"], str) else route["metadata"],
        created_at=route["created_at"].isoformat(),
        updated_at=route["updated_at"].isoformat()
    )


@router.get("/payments/{payment_id}/attempts", response_model=List[PaymentAttemptResponse])
async def get_payment_attempts(
    payment_id: str = Path(..., description="Payment/Order ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all attempts for a specific payment
    """
    attempts = await database.fetch_all(
        """
        SELECT 
            attempt_id, order_id, psp_name, attempt_number,
            status, response_time_ms, error_code, error_message,
            amount, currency, created_at
        FROM payment_attempts
        WHERE order_id = :order_id
        ORDER BY attempt_number ASC
        """,
        {"order_id": payment_id}
    )
    
    if not attempts:
        raise HTTPException(status_code=404, detail="No payment attempts found")
    
    return [
        PaymentAttemptResponse(
            attempt_id=a["attempt_id"],
            order_id=a["order_id"],
            psp_name=a["psp_name"],
            attempt_number=a["attempt_number"],
            status=a["status"],
            response_time_ms=a["response_time_ms"],
            error_code=a["error_code"],
            error_message=a["error_message"],
            amount=float(a["amount"]) if a["amount"] else 0,
            currency=a["currency"],
            created_at=a["created_at"].isoformat()
        )
        for a in attempts
    ]


@router.post("/{agent_id}/routes", response_model=RouteConfigResponse)
async def create_route_config(
    agent_id: str = Path(..., description="Agent ID"),
    request: UpdateRouteRequest = ...,
    merchant_id: Optional[str] = Query(None, description="Merchant ID (optional)"),
    current_user: dict = Depends(get_current_user)
):
    """
    Create a new routing configuration
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Generate route ID
    import hashlib
    route_id = f"route_{hashlib.md5(f'{agent_id}{merchant_id}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
    
    # Create route
    await database.execute(
        """
        INSERT INTO payment_routes (
            route_id, agent_id, merchant_id, psp_priority,
            routing_strategy, max_retries, timeout_ms, metadata
        ) VALUES (
            :route_id, :agent_id, :merchant_id, :psp_priority,
            :routing_strategy, :max_retries, :timeout_ms, :metadata
        )
        """,
        {
            "route_id": route_id,
            "agent_id": agent_id,
            "merchant_id": merchant_id,
            "psp_priority": json.dumps(request.psp_priority),
            "routing_strategy": request.routing_strategy,
            "max_retries": request.max_retries,
            "timeout_ms": request.timeout_ms,
            "metadata": json.dumps(request.metadata)
        }
    )
    
    # Fetch and return created route
    route = await database.fetch_one(
        """
        SELECT 
            route_id, agent_id, merchant_id, psp_priority,
            routing_strategy, is_active, max_retries, timeout_ms,
            metadata, created_at, updated_at
        FROM payment_routes
        WHERE route_id = :route_id
        """,
        {"route_id": route_id}
    )
    
    return RouteConfigResponse(
        route_id=route["route_id"],
        agent_id=route["agent_id"],
        merchant_id=route["merchant_id"],
        psp_priority=json.loads(route["psp_priority"]) if isinstance(route["psp_priority"], str) else route["psp_priority"],
        routing_strategy=route["routing_strategy"],
        is_active=route["is_active"],
        max_retries=route["max_retries"],
        timeout_ms=route["timeout_ms"],
        metadata=json.loads(route["metadata"]) if isinstance(route["metadata"], str) else route["metadata"],
        created_at=route["created_at"].isoformat(),
        updated_at=route["updated_at"].isoformat()
    )


@router.delete("/{agent_id}/routes/{route_id}")
async def delete_route_config(
    agent_id: str = Path(..., description="Agent ID"),
    route_id: str = Path(..., description="Route ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    Delete (deactivate) a routing configuration
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Deactivate route instead of hard delete
    result = await database.execute(
        """
        UPDATE payment_routes
        SET is_active = false, updated_at = NOW()
        WHERE route_id = :route_id AND agent_id = :agent_id
        """,
        {"route_id": route_id, "agent_id": agent_id}
    )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Route not found")
    
    return {"message": "Route deactivated successfully"}


# Employee-only endpoints for monitoring
employee_router = APIRouter(prefix="/employee/routing", tags=["Employee Routing"])


@employee_router.get("/performance")
async def get_routing_performance(
    hours: int = Query(24, description="Hours to look back"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Get overall routing performance metrics (Employee only)
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    # Get performance by PSP
    psp_performance = await database.fetch_all(
        """
        SELECT 
            psp_name,
            COUNT(*) as total_attempts,
            COUNT(CASE WHEN status = 'success' THEN 1 END) as successful,
            COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
            COUNT(CASE WHEN status = 'timeout' THEN 1 END) as timeouts,
            AVG(response_time_ms) as avg_response_time,
            MIN(response_time_ms) as min_response_time,
            MAX(response_time_ms) as max_response_time
        FROM payment_attempts
        WHERE created_at >= :cutoff
        GROUP BY psp_name
        ORDER BY total_attempts DESC
        """,
        {"cutoff": cutoff}
    )
    
    # Get failover statistics
    failover_stats = await database.fetch_one(
        """
        SELECT 
            COUNT(DISTINCT order_id) as total_payments,
            COUNT(CASE WHEN attempt_number > 1 THEN 1 END) as failover_count,
            AVG(attempt_number) as avg_attempts_per_payment
        FROM payment_attempts
        WHERE created_at >= :cutoff
        """,
        {"cutoff": cutoff}
    )
    
    return {
        "period_hours": hours,
        "psp_performance": [
            {
                "psp": p["psp_name"],
                "total_attempts": p["total_attempts"],
                "success_rate": (p["successful"] / p["total_attempts"] * 100) if p["total_attempts"] > 0 else 0,
                "failure_rate": (p["failed"] / p["total_attempts"] * 100) if p["total_attempts"] > 0 else 0,
                "timeout_rate": (p["timeouts"] / p["total_attempts"] * 100) if p["total_attempts"] > 0 else 0,
                "avg_response_time_ms": p["avg_response_time"],
                "min_response_time_ms": p["min_response_time"],
                "max_response_time_ms": p["max_response_time"]
            }
            for p in psp_performance
        ],
        "failover_statistics": dict(failover_stats) if failover_stats else {},
        "timestamp": datetime.utcnow().isoformat()
    }


@employee_router.get("/recent-failovers")
async def get_recent_failovers(
    limit: int = Query(50, description="Number of recent failovers"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Get recent payment failover events (Employee only)
    """
    failovers = await database.fetch_all(
        """
        SELECT 
            pa1.order_id,
            pa1.agent_id,
            pa1.psp_name as primary_psp,
            pa1.status as primary_status,
            pa1.error_message as primary_error,
            pa2.psp_name as failover_psp,
            pa2.status as failover_status,
            pa2.attempt_number as total_attempts,
            pa1.created_at
        FROM payment_attempts pa1
        JOIN payment_attempts pa2 ON pa1.order_id = pa2.order_id
        WHERE pa1.attempt_number = 1
        AND pa2.attempt_number > 1
        AND pa1.status = 'failed'
        ORDER BY pa1.created_at DESC
        LIMIT :limit
        """,
        {"limit": limit}
    )
    
    return {
        "total": len(failovers),
        "failovers": [
            {
                "order_id": f["order_id"],
                "agent_id": f["agent_id"],
                "primary_psp": f["primary_psp"],
                "primary_error": f["primary_error"],
                "failover_psp": f["failover_psp"],
                "failover_success": f["failover_status"] == "success",
                "total_attempts": f["total_attempts"],
                "timestamp": f["created_at"].isoformat()
            }
            for f in failovers
        ]
    }

