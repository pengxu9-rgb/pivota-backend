"""
[Phase 4++] Routing policy management API
Employee endpoints for managing merchant and agent routing policies
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import json

from db.database import database
from services.payment_routing_service import PaymentRoutingService
from utils.auth import ADMIN_ROLES, get_current_employee

# Initialize router
router = APIRouter(
    prefix="/employee/routing",
    tags=["[Phase 4++] Routing Governance"],
    dependencies=[Depends(get_current_employee)]
)

# Initialize services
routing_service = PaymentRoutingService(database)


# ========================================================================
# Request/Response Models
# ========================================================================

class RoutingPolicyRequest(BaseModel):
    """Request model for setting routing policy"""
    exclude: List[str] = Field(default=[], description="PSPs to exclude")
    prefer: List[str] = Field(default=[], description="Preferred PSPs in order (auto-generated from weights)")
    required: List[str] = Field(default=[], description="Required PSPs (merchant only)")
    weights: Dict[str, float] = Field(default={}, description="PSP weights (0.0-1.0)")
    failover: List[str] = Field(default=[], description="Failover PSPs")
    priority: int = Field(default=1, ge=1, le=10, description="Policy priority (auto-set to 1)")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "exclude": ["paypal"],
                "prefer": ["stripe", "adyen"],
                "weights": {"stripe": 1.0, "adyen": 0.9},
                "failover": ["square"],
                "priority": 1
            }
        }
    )


class RoutingPolicyResponse(BaseModel):
    """Response model for routing policy"""
    owner_type: str
    owner_id: str
    policy: Dict[str, Any]
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None


class RoutingLogResponse(BaseModel):
    """Response model for routing log entry"""
    id: int
    merchant_id: Optional[str]
    agent_id: Optional[str]
    order_id: Optional[str]
    chosen_psp: Optional[str]
    conflict_detected: bool
    resolution_method: Optional[str]
    execution_time_ms: Optional[int]
    created_at: datetime
    
    # Additional fields populated in handler
    merchant_name: Optional[str] = None
    agent_name: Optional[str] = None
    conflicts: List[Dict[str, Any]] = []


class SimulationRequest(BaseModel):
    """Request model for routing simulation"""
    scenarios: List[Dict[str, Any]] = Field(
        ...,
        description="List of test scenarios with amount, currency, etc."
    )
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scenarios": [
                    {"amount": 100.00, "currency": "USD"},
                    {"amount": 50.00, "currency": "EUR"},
                    {"amount": 1000.00, "currency": "USD"}
                ]
            }
        }
    )


# ========================================================================
# Policy Management Endpoints
# ========================================================================

@router.post("/policies/{owner_type}/{owner_id}", response_model=RoutingPolicyResponse)
async def set_routing_policy(
    owner_type: str = Path(..., pattern="^(merchant|agent)$", description="Owner type: merchant or agent"),
    owner_id: str = Path(..., description="Merchant or Agent ID"),
    request: RoutingPolicyRequest = ...,
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Create or update routing policy for merchant or agent
    
    - Merchants can set exclusions and required PSPs
    - Agents can set preferences and weights
    - Only employees can manage these policies
    """
    try:
        # Build policy object
        policy = {
            "exclude": request.exclude,
            "prefer": request.prefer,
            "weights": request.weights,
            "failover": request.failover
        }
        
        # Add required field only for merchants
        if owner_type == "merchant":
            policy["required"] = request.required
        
        # Check if policy exists
        existing = await database.fetch_one(
            """
            SELECT id FROM routing_policies
            WHERE owner_type = :owner_type AND owner_id = :owner_id
            """,
            {"owner_type": owner_type, "owner_id": owner_id}
        )
        
        if existing:
            # Update existing policy
            await database.execute(
                """
                UPDATE routing_policies
                SET policy = :policy,
                    priority = :priority,
                    is_active = true,
                    updated_at = NOW()
                WHERE owner_type = :owner_type AND owner_id = :owner_id
                """,
                {
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "policy": json.dumps(policy),
                    "priority": request.priority
                }
            )
        else:
            # Create new policy
            await database.execute(
                """
                INSERT INTO routing_policies (
                    owner_type, owner_id, policy, priority,
                    is_active, created_by, created_at, updated_at
                ) VALUES (
                    :owner_type, :owner_id, :policy, :priority,
                    true, :created_by, NOW(), NOW()
                )
                """,
                {
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "policy": json.dumps(policy),
                    "priority": request.priority,
                    "created_by": current_user.get("email")
                }
            )
        
        # Fetch and return the policy
        result = await database.fetch_one(
            """
            SELECT * FROM routing_policies
            WHERE owner_type = :owner_type AND owner_id = :owner_id
            """,
            {"owner_type": owner_type, "owner_id": owner_id}
        )
        
        return RoutingPolicyResponse(
            owner_type=result["owner_type"],
            owner_id=result["owner_id"],
            policy=json.loads(result["policy"]) if isinstance(result["policy"], str) else result["policy"],
            is_active=result["is_active"],
            priority=result["priority"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            created_by=result["created_by"]
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set routing policy: {str(e)}")


@router.get("/policies/{owner_type}/{owner_id}", response_model=RoutingPolicyResponse)
async def get_routing_policy(
    owner_type: str = Path(..., pattern="^(merchant|agent)$", description="Owner type: merchant or agent"),
    owner_id: str = Path(..., description="Merchant or Agent ID"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Get routing policy for merchant or agent
    """
    try:
        result = await database.fetch_one(
            """
            SELECT * FROM routing_policies
            WHERE owner_type = :owner_type AND owner_id = :owner_id AND is_active = true
            """,
            {"owner_type": owner_type, "owner_id": owner_id}
        )
        
        if not result:
            raise HTTPException(status_code=404, detail=f"No active routing policy found for {owner_type} {owner_id}")
        
        return RoutingPolicyResponse(
            owner_type=result["owner_type"],
            owner_id=result["owner_id"],
            policy=json.loads(result["policy"]) if isinstance(result["policy"], str) else result["policy"],
            is_active=result["is_active"],
            priority=result["priority"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            created_by=result["created_by"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get routing policy: {str(e)}")


@router.delete("/policies/{owner_type}/{owner_id}")
async def delete_routing_policy(
    owner_type: str = Path(..., pattern="^(merchant|agent)$", description="Owner type: merchant or agent"),
    owner_id: str = Path(..., description="Merchant or Agent ID"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Delete (deactivate) routing policy
    """
    try:
        result = await database.execute(
            """
            UPDATE routing_policies
            SET is_active = false, updated_at = NOW()
            WHERE owner_type = :owner_type AND owner_id = :owner_id
            """,
            {"owner_type": owner_type, "owner_id": owner_id}
        )
        
        if result == 0:
            raise HTTPException(status_code=404, detail="Routing policy not found")
        
        return {"status": "success", "message": "Routing policy deactivated"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete routing policy: {str(e)}")


# ========================================================================
# Routing Logs & Monitoring Endpoints
# ========================================================================

@router.get("/logs", response_model=List[RoutingLogResponse])
async def get_routing_logs(
    merchant_id: Optional[str] = Query(None, description="Filter by merchant"),
    agent_id: Optional[str] = Query(None, description="Filter by agent"),
    conflict_only: bool = Query(False, description="Show only logs with conflicts"),
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Get routing decision logs with optional filters
    """
    try:
        # Build query with filters
        query = """
            SELECT 
                rl.*,
                rl.merchant_id as merchant_name,
                rl.agent_id as agent_name
            FROM routing_logs rl
            WHERE rl.created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
        """
        
        params = {"days": days, "limit": limit, "offset": offset}
        
        if merchant_id:
            query += " AND rl.merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        if agent_id:
            query += " AND rl.agent_id = :agent_id"
            params["agent_id"] = agent_id
        
        if conflict_only:
            query += " AND rl.conflict_detected = true"
        
        query += " ORDER BY rl.created_at DESC LIMIT :limit OFFSET :offset"
        
        results = await database.fetch_all(query, params)
        
        # Process results
        logs = []
        for row in results:
            # Extract conflicts from decision trace
            decision_trace = json.loads(row["decision_trace"]) if isinstance(row["decision_trace"], str) else row["decision_trace"]
            conflicts = []
            
            # Find conflicts in the engine's conflict detection
            for item in decision_trace:
                if isinstance(item, dict) and item.get("step") == "merchant_rules_applied":
                    # Check the decision trace for conflicts
                    break
            
            # Get conflicts from the routing engine result
            if row["conflict_detected"] and decision_trace:
                # Look for the conflicts in the decision
                for trace_item in decision_trace:
                    if isinstance(trace_item, dict) and "action" in trace_item and "conflict" in trace_item.get("action", ""):
                        conflicts.append(trace_item)
            
            logs.append(RoutingLogResponse(
                id=row["id"],
                merchant_id=row["merchant_id"],
                agent_id=row["agent_id"],
                order_id=row["order_id"],
                chosen_psp=row["chosen_psp"],
                conflict_detected=row["conflict_detected"],
                resolution_method=row["resolution_method"],
                execution_time_ms=row["execution_time_ms"],
                created_at=row["created_at"],
                merchant_name=row["merchant_name"],
                agent_name=row["agent_name"],
                conflicts=conflicts
            ))
        
        return logs
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get routing logs: {str(e)}")


@router.get("/conflicts", response_model=List[Dict[str, Any]])
async def get_routing_conflicts(
    days: int = Query(30, ge=1, le=90, description="Number of days to look back"),
    limit: int = Query(100, ge=1, le=500, description="Maximum results"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Get recent routing conflicts for monitoring
    """
    try:
        conflicts = await routing_service.get_routing_conflicts(days=days, limit=limit)
        return conflicts
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get routing conflicts: {str(e)}")


# ========================================================================
# Simulation & Testing Endpoints
# ========================================================================

@router.post("/simulate/{merchant_id}/{agent_id}")
async def simulate_routing(
    merchant_id: str = Path(..., description="Merchant ID"),
    agent_id: str = Path(..., description="Agent ID"),
    request: SimulationRequest = ...,
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Simulate routing decisions without executing payments
    
    Test different scenarios to see how merchant and agent rules interact
    """
    try:
        results = await routing_service.simulate_routing(
            merchant_id=merchant_id,
            agent_id=agent_id,
            test_scenarios=request.scenarios
        )
        
        return {
            "merchant_id": merchant_id,
            "agent_id": agent_id,
            "simulation_results": results,
            "summary": {
                "total_scenarios": len(results),
                "conflicts_detected": sum(1 for r in results if r.get("conflict_detected", False)),
                "errors": sum(1 for r in results if "error" in r)
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Simulation failed: {str(e)}")


# ========================================================================
# Analytics & Reports
# ========================================================================

@router.get("/analytics/conflict-summary")
async def get_conflict_summary(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Get summary analytics of routing conflicts
    """
    try:
        result = await database.fetch_one(
            """
            SELECT 
                COUNT(*) as total_routings,
                COUNT(*) FILTER (WHERE conflict_detected) as total_conflicts,
                ROUND((COUNT(*) FILTER (WHERE conflict_detected)::numeric / 
                       NULLIF(COUNT(*), 0) * 100), 2) as conflict_rate,
                COUNT(DISTINCT merchant_id) FILTER (WHERE conflict_detected) as merchants_with_conflicts,
                COUNT(DISTINCT agent_id) FILTER (WHERE conflict_detected) as agents_with_conflicts
            FROM routing_logs
            WHERE created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            """,
            {"days": days}
        )
        
        # Get resolution method breakdown
        resolution_breakdown = await database.fetch_all(
            """
            SELECT 
                resolution_method,
                COUNT(*) as count
            FROM routing_logs
            WHERE conflict_detected = true
            AND created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            GROUP BY resolution_method
            """,
            {"days": days}
        )
        
        return {
            "period_days": days,
            "total_routings": result["total_routings"],
            "total_conflicts": result["total_conflicts"],
            "conflict_rate_percent": float(result["conflict_rate"]) if result["conflict_rate"] else 0,
            "merchants_with_conflicts": result["merchants_with_conflicts"],
            "agents_with_conflicts": result["agents_with_conflicts"],
            "resolution_methods": {
                row["resolution_method"]: row["count"]
                for row in resolution_breakdown
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get conflict summary: {str(e)}")


@router.get("/analytics/psp-selection")
async def get_psp_selection_analytics(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Get analytics on PSP selection patterns
    """
    try:
        # PSP selection frequency
        psp_frequency = await database.fetch_all(
            """
            SELECT 
                chosen_psp,
                COUNT(*) as selection_count,
                ROUND((COUNT(*)::numeric / SUM(COUNT(*)) OVER () * 100), 2) as percentage
            FROM routing_logs
            WHERE created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            AND chosen_psp IS NOT NULL
            GROUP BY chosen_psp
            ORDER BY selection_count DESC
            """,
            {"days": days}
        )
        
        # Average execution time by PSP
        execution_times = await database.fetch_all(
            """
            SELECT 
                chosen_psp,
                AVG(execution_time_ms) as avg_execution_time,
                MIN(execution_time_ms) as min_execution_time,
                MAX(execution_time_ms) as max_execution_time
            FROM routing_logs
            WHERE created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            AND chosen_psp IS NOT NULL
            AND execution_time_ms IS NOT NULL
            GROUP BY chosen_psp
            """,
            {"days": days}
        )
        
        return {
            "period_days": days,
            "psp_frequency": [
                {
                    "psp": row["chosen_psp"],
                    "count": row["selection_count"],
                    "percentage": float(row["percentage"])
                }
                for row in psp_frequency
            ],
            "execution_times": [
                {
                    "psp": row["chosen_psp"],
                    "avg_ms": round(float(row["avg_execution_time"]), 2) if row["avg_execution_time"] else 0,
                    "min_ms": row["min_execution_time"],
                    "max_ms": row["max_execution_time"]
                }
                for row in execution_times
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get PSP selection analytics: {str(e)}")


# ========================================================================
# Agent Override Management
# ========================================================================

@router.put("/agents/{agent_id}/override-permission")
async def set_agent_override_permission(
    agent_id: str = Path(..., description="Agent ID"),
    enabled: bool = Query(..., description="Enable or disable routing override"),
    current_user: dict = Depends(get_current_employee)
):
    """
    [Phase 4++] Enable/disable agent's ability to override merchant routing rules
    
    Only admin employees should be able to grant this permission
    """
    try:
        # Check if employee is admin
        if current_user.get("role") not in ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="Only admin employees can manage override permissions")
        
        result = await database.execute(
            """
            UPDATE agents
            SET routing_override_enabled = :enabled
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id, "enabled": enabled}
        )
        
        if result == 0:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "routing_override_enabled": enabled,
            "message": f"Routing override {'enabled' if enabled else 'disabled'} for agent {agent_id}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update override permission: {str(e)}")


# [Phase 4++] Routing governance routes registered
print("[Phase 4++] Routing governance API routes initialized")
