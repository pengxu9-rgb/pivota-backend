"""
[Phase 5.6] Agent Settlement Routes
Endpoints for agents to view settlements and payouts
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from datetime import datetime, timedelta
from typing import Optional

from db.database import database
from core.settlement_engine import AgentSettlementEngine
from services.revenue_share_service import RevenueShareService
from utils.auth import get_current_user

router = APIRouter(
    prefix="/agents/{agent_id}/settlements",
    tags=["[Phase 5.6] Agent Settlements"]
)

# Initialize services (REUSE existing)
revenue_service = RevenueShareService(database)
settlement_engine = AgentSettlementEngine(revenue_service, database)


@router.get("")
async def get_agent_settlements(
    agent_id: str = Path(...),
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.6] List all settlements for agent"""
    
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        query = "SELECT * FROM agent_settlements WHERE agent_id = :agent_id"
        params = {"agent_id": agent_id, "limit": limit}
        
        if status:
            query += " AND status = :status"
            params["status"] = status
        
        query += " ORDER BY created_at DESC LIMIT :limit"
        
        settlements = await database.fetch_all(query, params)
        
        return {
            "agent_id": agent_id,
            "settlements": [
                {
                    "settlement_id": s["settlement_id"],
                    "amount": float(s["settlement_amount"]),
                    "status": s["status"],
                    "transactions": s["total_transactions"],
                    "period": {
                        "start": s["settlement_period_start"].isoformat(),
                        "end": s["settlement_period_end"].isoformat()
                    },
                    "payout_date": s["payout_date"].isoformat() if s["payout_date"] else None,
                    "created_at": s["created_at"].isoformat()
                }
                for s in settlements
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pending")
async def get_pending_settlements(
    agent_id: str = Path(...),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.6] Get pending settlements"""
    
    # Allow: agent accessing own data, or admin/employee
    if current_user.get("agent_id") != agent_id and current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        pending = await settlement_engine.get_pending_settlements(agent_id)
        return {"agent_id": agent_id, "pending_settlements": pending}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/calculate")
async def calculate_settlement(
    agent_id: str = Path(...),
    days: int = Query(30, ge=1, le=90),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.6] Calculate settlement for recent period"""
    
    if current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Only employees can calculate settlements")
    
    try:
        period_end = datetime.utcnow()
        period_start = period_end - timedelta(days=days)
        
        result = await settlement_engine.calculate_settlement(
            agent_id=agent_id,
            period_start=period_start,
            period_end=period_end
        )
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


print("[Phase 5.6] Agent settlement routes initialized")
