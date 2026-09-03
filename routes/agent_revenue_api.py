"""
[Phase 5] Agent Revenue API
Endpoints for agents to manage revenue policies and view earnings
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from decimal import Decimal
import json

from db.database import database
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user

# Initialize router
router = APIRouter(
    prefix="/agents/{agent_id}/revenue",
    tags=["[Phase 5] Agent Revenue Management"]
)


# ========================================================================
# Request/Response Models
# ========================================================================

class RevenuePolicyRequest(BaseModel):
    """Request model for revenue policy"""
    merchant_id: Optional[str] = Field(None, description="Specific merchant (NULL = default for all)")
    split_ratio: float = Field(..., ge=0, le=1, description="Revenue split ratio (0.0 to 1.0)")
    currency: str = Field(default="USD", description="Currency code")
    min_transaction_amount: float = Field(default=0, description="Minimum transaction amount")
    max_transaction_amount: Optional[float] = Field(None, description="Maximum transaction amount")
    active_period_start: Optional[datetime] = None
    active_period_end: Optional[datetime] = None
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "merchant_id": None,
                "split_ratio": 0.02,
                "currency": "USD",
                "min_transaction_amount": 10.00
            }
        }
    )


class EarningsSummary(BaseModel):
    """Response model for earnings summary"""
    total_earned: float
    settled_amount: float
    pending_amount: float
    total_transactions: int
    avg_split_ratio: float
    currency: str
    period_days: int


# ========================================================================
# Revenue Policy Endpoints
# ========================================================================

@router.get("/policies")
async def get_revenue_policies(
    agent_id: str = Path(..., description="Agent ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get all revenue split policies for this agent
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        policies = await database.fetch_all(
            """
            SELECT * FROM agent_revenue_policies
            WHERE agent_id = :agent_id
            ORDER BY merchant_id NULLS FIRST, created_at DESC
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "agent_id": agent_id,
            "total_policies": len(policies),
            "policies": [
                {
                    "id": p["id"],
                    "merchant_id": p["merchant_id"],
                    "split_ratio": float(p["split_ratio"]),
                    "currency": p["currency"],
                    "min_amount": float(p["min_transaction_amount"]) if p["min_transaction_amount"] else 0,
                    "max_amount": float(p["max_transaction_amount"]) if p["max_transaction_amount"] else None,
                    "is_active": p["is_active"],
                    "active_period": {
                        "start": p["active_period_start"].isoformat() if p["active_period_start"] else None,
                        "end": p["active_period_end"].isoformat() if p["active_period_end"] else None
                    },
                    "created_at": p["created_at"].isoformat() if p["created_at"] else None
                }
                for p in policies
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get revenue policies: {str(e)}")


@router.post("/policies")
async def create_revenue_policy(
    agent_id: str = Path(..., description="Agent ID"),
    request: RevenuePolicyRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Create new revenue policy
    
    Note: Only employees/admins can create revenue policies for security
    """
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only employees can create revenue policies")
    
    try:
        # Check if policy already exists
        existing = await database.fetch_one(
            """
            SELECT id FROM agent_revenue_policies
            WHERE agent_id = :agent_id
            AND merchant_id IS NOT DISTINCT FROM :merchant_id
            AND currency = :currency
            """,
            {
                "agent_id": agent_id,
                "merchant_id": request.merchant_id,
                "currency": request.currency
            }
        )
        
        if existing:
            # Update existing
            await database.execute(
                """
                UPDATE agent_revenue_policies
                SET split_ratio = :split_ratio,
                    min_transaction_amount = :min_amount,
                    max_transaction_amount = :max_amount,
                    active_period_start = :period_start,
                    active_period_end = :period_end,
                    is_active = true,
                    updated_at = NOW()
                WHERE id = :policy_id
                """,
                {
                    "policy_id": existing["id"],
                    "split_ratio": request.split_ratio,
                    "min_amount": request.min_transaction_amount,
                    "max_amount": request.max_transaction_amount,
                    "period_start": request.active_period_start,
                    "period_end": request.active_period_end
                }
            )
            message = "Revenue policy updated"
        else:
            # Create new
            await database.execute(
                """
                INSERT INTO agent_revenue_policies (
                    agent_id, merchant_id, split_ratio, currency,
                    min_transaction_amount, max_transaction_amount,
                    active_period_start, active_period_end,
                    is_active, created_by, created_at, updated_at
                ) VALUES (
                    :agent_id, :merchant_id, :split_ratio, :currency,
                    :min_amount, :max_amount,
                    :period_start, :period_end,
                    true, :created_by, NOW(), NOW()
                )
                """,
                {
                    "agent_id": agent_id,
                    "merchant_id": request.merchant_id,
                    "split_ratio": request.split_ratio,
                    "currency": request.currency,
                    "min_amount": request.min_transaction_amount,
                    "max_amount": request.max_transaction_amount,
                    "period_start": request.active_period_start,
                    "period_end": request.active_period_end,
                    "created_by": current_user.get("email", "system")
                }
            )
            message = "Revenue policy created"
        
        return {
            "status": "success",
            "message": message,
            "agent_id": agent_id,
            "policy": request.dict()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save revenue policy: {str(e)}")


# ========================================================================
# Earnings and Analytics Endpoints
# ========================================================================

@router.get("/earnings", response_model=EarningsSummary)
async def get_agent_earnings(
    agent_id: str = Path(..., description="Agent ID"),
    days: int = Query(30, ge=1, le=365, description="Period to calculate"),
    currency: str = Query("USD", description="Currency filter"),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get earnings summary for agent
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Query from commissions table (Phase 6 commission automation)
        summary = await database.fetch_one(
            """
            SELECT 
                COALESCE(SUM(amount), 0) as total_earned,
                0 as settled,
                COALESCE(SUM(amount), 0) as pending,
                COUNT(*) as transactions,
                AVG(rate) as avg_ratio
            FROM commissions
            WHERE agent_id = :agent_id
            AND type = 'agent'
            AND created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            """,
            {"agent_id": agent_id, "days": days}
        )
        
        return EarningsSummary(
            total_earned=float(summary["total_earned"]),
            settled_amount=float(summary["settled"]),
            pending_amount=float(summary["pending"]),
            total_transactions=summary["transactions"],
            avg_split_ratio=float(summary["avg_ratio"]) if summary["avg_ratio"] else 0,
            currency=currency,
            period_days=days
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get earnings: {str(e)}")


@router.get("/logs")
async def get_revenue_logs(
    agent_id: str = Path(..., description="Agent ID"),
    days: int = Query(30, ge=1, le=365),
    settlement_status: Optional[str] = Query(None, description="Filter by settlement status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get revenue transaction logs
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        query = """
            SELECT * FROM agent_revenue_logs
            WHERE agent_id = :agent_id
            AND created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
        """
        
        params = {"agent_id": agent_id, "days": days, "limit": limit, "offset": offset}
        
        if settlement_status:
            query += " AND settlement_status = :status"
            params["status"] = settlement_status
        
        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        
        logs = await database.fetch_all(query, params)
        
        return {
            "agent_id": agent_id,
            "period_days": days,
            "total_logs": len(logs),
            "logs": [
                {
                    "id": log["id"],
                    "tx_id": log["tx_id"],
                    "merchant_id": log["merchant_id"],
                    "psp_used": log["psp_used"],
                    "transaction_amount": float(log["transaction_amount"]),
                    "agent_earned_amount": float(log["agent_earned_amount"]),
                    "split_ratio": float(log["split_ratio_applied"]),
                    "currency": log["currency"],
                    "settlement_status": log["settlement_status"],
                    "settlement_batch_id": log["settlement_batch_id"],
                    "settled_at": log["settled_at"].isoformat() if log["settled_at"] else None,
                    "created_at": log["created_at"].isoformat() if log["created_at"] else None
                }
                for log in logs
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get revenue logs: {str(e)}")


@router.get("/settlements")
async def get_settlement_history(
    agent_id: str = Path(..., description="Agent ID"),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    """
    [Phase 5] Get settlement batch history
    """
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Group by settlement batch
        batches = await database.fetch_all(
            """
            SELECT 
                settlement_batch_id,
                settlement_status,
                COUNT(*) as transaction_count,
                SUM(agent_earned_amount) as batch_amount,
                currency,
                MIN(settled_at) as settled_at,
                MIN(created_at) as first_transaction,
                MAX(created_at) as last_transaction
            FROM agent_revenue_logs
            WHERE agent_id = :agent_id
            AND settlement_batch_id IS NOT NULL
            GROUP BY settlement_batch_id, settlement_status, currency
            ORDER BY MIN(settled_at) DESC NULLS LAST, MIN(created_at) DESC
            LIMIT :limit
            """,
            {"agent_id": agent_id, "limit": limit}
        )
        
        return {
            "agent_id": agent_id,
            "total_batches": len(batches),
            "batches": [
                {
                    "batch_id": b["settlement_batch_id"],
                    "status": b["settlement_status"],
                    "transaction_count": b["transaction_count"],
                    "total_amount": float(b["batch_amount"]),
                    "currency": b["currency"],
                    "settled_at": b["settled_at"].isoformat() if b["settled_at"] else None,
                    "period": {
                        "first": b["first_transaction"].isoformat() if b["first_transaction"] else None,
                        "last": b["last_transaction"].isoformat() if b["last_transaction"] else None
                    }
                }
                for b in batches
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settlement history: {str(e)}")


# ========================================================================
# [Phase 5.5] Revenue Expectations Endpoints
# ========================================================================

@router.put("/expectations")
async def set_revenue_expectations(
    agent_id: str = Path(...),
    expected_rate: float = Query(..., ge=0, le=1),
    min_acceptable_rate: float = Query(..., ge=0, le=1),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Set agent revenue expectations"""
    
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        await database.execute(
            """
            UPDATE agent_revenue_expectations
            SET expected_commission_rate = :expected,
                min_acceptable_rate = :minimum,
                updated_at = NOW()
            WHERE agent_id = :agent_id AND merchant_id IS NULL
            """,
            {"agent_id": agent_id, "expected": expected_rate, "minimum": min_acceptable_rate}
        )
        
        return {"status": "success", "expected_rate": expected_rate, "min_rate": min_acceptable_rate}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/expectations")
async def get_revenue_expectations(
    agent_id: str = Path(...),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Get agent revenue expectations"""
    
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        exp = await database.fetch_one(
            "SELECT * FROM agent_revenue_expectations WHERE agent_id = :agent_id AND merchant_id IS NULL",
            {"agent_id": agent_id}
        )
        
        if not exp:
            return {"agent_id": agent_id, "has_expectations": False}
        
        exp_dict = dict(exp)  # Convert Record to dict
        return {
            "agent_id": agent_id,
            "has_expectations": True,
            "expected_commission_rate": float(exp_dict["expected_commission_rate"]) if exp_dict.get("expected_commission_rate") else None,
            "min_acceptable_rate": float(exp_dict["min_acceptable_rate"]) if exp_dict.get("min_acceptable_rate") else None,
            "agent_type": exp_dict.get("agent_type")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# [Phase 5] Agent revenue API initialized
print("[Phase 5.5] Agent revenue API routes initialized (with expectations)")
