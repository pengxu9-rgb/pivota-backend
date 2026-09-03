"""
[Phase 5] Agent Routing API
Endpoints for agents to manage their own routing policies and view history
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List, Optional, Any
from datetime import datetime
import json

from db.database import database
from services.payment_routing_service import PaymentRoutingService
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user

# Initialize router
router = APIRouter(
    prefix="/agents/{agent_id}/routing",
    tags=["[Phase 5] Agent Routing Control"]
)

# Initialize services
routing_service = PaymentRoutingService(database)


# ========================================================================
# Request/Response Models
# ========================================================================

class RoutingTestRequest(BaseModel):
    """Request model for testing routing"""
    merchant_id: str = Field(..., description="Merchant ID to test routing with")
    amount: float = Field(..., gt=0, description="Transaction amount")
    currency: str = Field(default="USD", description="Currency code")
    scenarios: List[Dict[str, Any]] = Field(default=[], description="Multiple test scenarios")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "merchant_id": "merchant_123",
                "amount": 100.00,
                "currency": "USD"
            }
        }
    )


class RoutingPolicyResponse(BaseModel):
    """Response model for routing policy"""
    id: int
    owner_type: str
    owner_id: str
    policy: Dict[str, Any]
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime


# ========================================================================
# Agent Routing Endpoints
# ========================================================================

@router.post("/test")
async def test_agent_routing(
    agent_id: str = Path(..., description="Agent ID"),
    request: RoutingTestRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Test routing with agent's current policies
    
    Dry-run simulation without affecting actual data
    """
    # Verify agent access
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Use simulate endpoint from routing service
        selected_psp, routing_decision = await routing_service.resolve_dual_routing(
            merchant_id=request.merchant_id,
            agent_id=agent_id,
            amount=request.amount,
            currency=request.currency
        )
        
        return {
            "test_result": "success",
            "selected_psp": selected_psp,
            "conflict_detected": routing_decision.get('conflict_detected', False),
            "conflicts": routing_decision.get('conflicts', []),
            "resolution_method": routing_decision.get('resolution_method'),
            "decision_trace": routing_decision.get('decision_trace', []),
            "execution_time_ms": routing_decision.get('execution_time_ms'),
            "merchant_id": request.merchant_id,
            "agent_id": agent_id,
            "is_simulation": True
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Routing test failed: {str(e)}")


@router.get("/policies", response_model=RoutingPolicyResponse)
async def get_agent_routing_policy(
    agent_id: str = Path(..., description="Agent ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get this agent's routing policies
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        policy = await database.fetch_one(
            """
            SELECT * FROM routing_policies
            WHERE owner_type = 'agent' AND owner_id = :agent_id AND is_active = true
            """,
            {"agent_id": agent_id}
        )
        
        if not policy:
            raise HTTPException(status_code=404, detail="No active routing policy found for this agent")
        
        return RoutingPolicyResponse(
            id=policy["id"],
            owner_type=policy["owner_type"],
            owner_id=policy["owner_id"],
            policy=json.loads(policy["policy"]) if isinstance(policy["policy"], str) else policy["policy"],
            is_active=policy["is_active"],
            priority=policy["priority"],
            created_at=policy["created_at"],
            updated_at=policy["updated_at"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get routing policy: {str(e)}")


@router.post("/policies")
async def create_agent_routing_policy(
    agent_id: str = Path(..., description="Agent ID"),
    policy_data: Dict[str, Any] = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Create or update agent routing policy
    
    Note: Agent policies must respect merchant rules
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Validate policy structure
        required_fields = ["prefer", "weights"]
        if not all(field in policy_data for field in required_fields):
            raise HTTPException(
                status_code=400,
                detail=f"Policy must include: {required_fields}"
            )
        
        # Check if policy exists
        existing = await database.fetch_one(
            """
            SELECT id FROM routing_policies
            WHERE owner_type = 'agent' AND owner_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        if existing:
            # Update
            await database.execute(
                """
                UPDATE routing_policies
                SET policy = :policy,
                    priority = :priority,
                    is_active = true,
                    updated_at = NOW()
                WHERE owner_type = 'agent' AND owner_id = :agent_id
                """,
                {
                    "agent_id": agent_id,
                    "policy": json.dumps(policy_data),
                    "priority": policy_data.get('priority', 1)
                }
            )
        else:
            # Create
            await database.execute(
                """
                INSERT INTO routing_policies (
                    owner_type, owner_id, policy, priority,
                    is_active, created_at, updated_at
                ) VALUES (
                    'agent', :agent_id, :policy, :priority,
                    true, NOW(), NOW()
                )
                """,
                {
                    "agent_id": agent_id,
                    "policy": json.dumps(policy_data),
                    "priority": policy_data.get('priority', 1)
                }
            )
        
        return {
            "status": "success",
            "message": "Agent routing policy saved",
            "agent_id": agent_id,
            "policy": policy_data
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save routing policy: {str(e)}")


@router.delete("/policies/{policy_id}")
async def delete_agent_routing_policy(
    agent_id: str = Path(..., description="Agent ID"),
    policy_id: int = Path(..., description="Policy ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Delete specific routing policy
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        result = await database.execute(
            """
            UPDATE routing_policies
            SET is_active = false, updated_at = NOW()
            WHERE id = :policy_id AND owner_type = 'agent' AND owner_id = :agent_id
            """,
            {"policy_id": policy_id, "agent_id": agent_id}
        )
        
        if result == 0:
            raise HTTPException(status_code=404, detail="Policy not found")
        
        return {"status": "success", "message": "Routing policy deleted"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete policy: {str(e)}")


@router.get("/history")
async def get_agent_routing_history(
    agent_id: str = Path(..., description="Agent ID"),
    days: int = Query(30, ge=1, le=90, description="Days of history"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get routing decision history for this agent
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        results = await database.fetch_all(
            """
            SELECT 
                id, merchant_id, order_id, chosen_psp,
                conflict_detected, resolution_method, resolved_by,
                execution_time_ms, created_at
            FROM routing_logs
            WHERE agent_id = :agent_id
            AND created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"agent_id": agent_id, "days": days, "limit": limit}
        )
        
        history = []
        for row in results:
            row_dict = dict(row)  # Convert Record to dict
            history.append({
                "id": row_dict.get("id"),
                "merchant_id": row_dict.get("merchant_id"),
                "order_id": row_dict.get("order_id"),
                "chosen_psp": row_dict.get("chosen_psp"),
                "conflict_detected": row_dict.get("conflict_detected"),
                "resolution_method": row_dict.get("resolution_method"),
                "resolved_by": row_dict.get("resolved_by", "consensus"),
                "execution_time_ms": row_dict.get("execution_time_ms"),
                "created_at": row_dict.get("created_at").isoformat() if row_dict.get("created_at") else None
            })
        
        return {
            "agent_id": agent_id,
            "period_days": days,
            "total_routings": len(history),
            "history": history
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get routing history: {str(e)}")


# [Phase 5] Agent routing API initialized
print("[Phase 5] Agent routing API routes initialized")
