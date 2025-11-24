"""
Agent Payout View Routes
Allows agents to view their payouts
Phase 6 - Payouts & Banking
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from typing import Optional
from datetime import date, datetime
from pydantic import BaseModel
import logging

from db.database import database
from db.payout_repo import PayoutRepo
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/payouts", tags=["agent-payouts"])

# Response Models
class AgentPayoutResponse(BaseModel):
    id: int
    merchant_id: str
    amount: float
    currency: str
    status: str
    payout_reference: Optional[str]
    method: Optional[str]
    provider: Optional[str]
    period_start: date
    period_end: date
    confirmed_at: Optional[datetime]
    created_at: datetime

class PayoutSummary(BaseModel):
    total_paid: float
    total_pending: float
    total_uploaded: float
    count_paid: int
    count_pending: int
    count_uploaded: int
    last_payment_date: Optional[datetime]

@router.get("", response_model=dict)
async def list_agent_payouts(
    agent_id: str,
    status: Optional[str] = Query('paid', description="Filter by status: pending, uploaded, paid"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user)
):
    """
    List payouts for an agent
    Agents can only see their own payouts
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    # Check if agent is accessing their own data
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized to view these payouts")
    
    try:
        repo = PayoutRepo()
        
        # Get paginated results
        items = await repo.list(
            merchant_id=None,
            agent_id=agent_id,
            status=status,
            limit=size,
            offset=(page-1)*size
        )
        
        # Get total count
        count_query = "SELECT COUNT(*) as total FROM agent_payouts WHERE agent_id = :aid"
        params = {"aid": agent_id}
        if status:
            count_query += " AND status = :st"
            params["st"] = status
        
        total_result = await database.fetch_one(query=count_query, values=params)
        total = total_result["total"] if total_result else 0
        
        # Get summary statistics
        summary = await repo.get_summary_by_agent(agent_id)
        
        # Format items for agent view (hide some merchant details if needed)
        formatted_items = []
        for item in items:
            formatted_items.append({
                "id": item["id"],
                "merchant_id": item["merchant_id"],
                "amount": item["amount"],
                "currency": item["currency"],
                "status": item["status"],
                "payout_reference": item.get("payout_reference"),
                "method": item.get("method"),
                "provider": item.get("provider"),
                "period_start": item["period_start"],
                "period_end": item["period_end"],
                "confirmed_at": item.get("confirmed_at"),
                "created_at": item["created_at"]
            })
        
        return {
            "items": formatted_items,
            "total": total,
            "page": page,
            "pages": (total + size - 1) // size if size > 0 else 0,
            "summary": summary
        }
        
    except Exception as e:
        logger.error(f"Error listing payouts for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to list payouts")

@router.get("/summary", response_model=PayoutSummary)
async def get_payout_summary(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get payout summary statistics for an agent
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get summary by status
        query = """
        SELECT 
            status,
            COUNT(*) as count,
            COALESCE(SUM(amount), 0) as total
        FROM agent_payouts
        WHERE agent_id = :aid
        GROUP BY status
        """
        
        results = await database.fetch_all(query=query, values={"aid": agent_id})
        
        # Initialize summary
        summary = {
            "total_paid": 0.0,
            "total_pending": 0.0,
            "total_uploaded": 0.0,
            "count_paid": 0,
            "count_pending": 0,
            "count_uploaded": 0,
            "last_payment_date": None
        }
        
        # Process results
        for row in results:
            status = row["status"]
            if status == "paid":
                summary["total_paid"] = float(row["total"])
                summary["count_paid"] = row["count"]
            elif status == "pending":
                summary["total_pending"] = float(row["total"])
                summary["count_pending"] = row["count"]
            elif status == "uploaded":
                summary["total_uploaded"] = float(row["total"])
                summary["count_uploaded"] = row["count"]
        
        # Get last payment date
        last_payment = await database.fetch_one(
            query="""
            SELECT MAX(confirmed_at) as last_payment 
            FROM agent_payouts 
            WHERE agent_id = :aid AND status = 'paid'
            """,
            values={"aid": agent_id}
        )
        
        if last_payment and last_payment["last_payment"]:
            summary["last_payment_date"] = last_payment["last_payment"]
        
        return PayoutSummary(**summary)
        
    except Exception as e:
        logger.error(f"Error getting payout summary for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payout summary")

@router.get("/export/csv", response_model=dict)
async def export_agent_payouts_csv(
    agent_id: str,
    status: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """
    Export agent's payouts as CSV data
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with filters
        conditions = ["agent_id = :aid"]
        params = {"aid": agent_id}
        
        if status:
            conditions.append("status = :st")
            params["st"] = status
        
        if start_date:
            conditions.append("confirmed_at >= :start")
            params["start"] = start_date
        
        if end_date:
            conditions.append("confirmed_at <= :end")
            params["end"] = end_date
        
        where_clause = " AND ".join(conditions)
        
        query = f"""
        SELECT 
            id,
            merchant_id,
            amount,
            currency,
            status,
            payout_reference,
            method,
            period_start,
            period_end,
            confirmed_at,
            created_at
        FROM agent_payouts
        WHERE {where_clause}
        ORDER BY created_at DESC
        """
        
        results = await database.fetch_all(query=query, values=params)
        
        # Convert to list of dicts for CSV export
        data = []
        for row in results:
            data.append({
                "Payout ID": row["id"],
                "Merchant": row["merchant_id"],
                "Amount": row["amount"],
                "Currency": row["currency"],
                "Status": row["status"],
                "Reference": row["payout_reference"] or "",
                "Method": row["method"] or "",
                "Period Start": row["period_start"],
                "Period End": row["period_end"],
                "Payment Date": row["confirmed_at"] or "",
                "Created": row["created_at"]
            })
        
        return {
            "status": "success",
            "count": len(data),
            "data": data,
            "filename": f"my_payouts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        }
        
    except Exception as e:
        logger.error(f"Error exporting agent payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to export payouts")

@router.get("/{payout_id}", response_model=AgentPayoutResponse)
async def get_payout_details(
    agent_id: str,
    payout_id: int,
    current_user: dict = Depends(get_current_user)
):
    """
    Get details of a specific payout
    """
    # Verify agent access
    if current_user.get("role") != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
    
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        repo = PayoutRepo()
        payout = await repo.get_by_id(payout_id)
        
        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")
        
        if payout["agent_id"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized to view this payout")
        
        return AgentPayoutResponse(
            id=payout["id"],
            merchant_id=payout["merchant_id"],
            amount=payout["amount"],
            currency=payout["currency"],
            status=payout["status"],
            payout_reference=payout.get("payout_reference"),
            method=payout.get("method"),
            provider=payout.get("provider"),
            period_start=payout["period_start"],
            period_end=payout["period_end"],
            confirmed_at=payout.get("confirmed_at"),
            created_at=payout["created_at"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payout details: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payout details")
