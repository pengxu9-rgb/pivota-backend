"""
Employee Payout Management Routes
Allows employees to review and confirm payouts across all merchants
Phase 6 - Payouts & Banking
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import date, datetime
import logging

from db.database import database
from db.payout_repo import PayoutRepo
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/employee/payouts", tags=["employee-payouts"])

# Request/Response Models
class ConfirmBulkRequest(BaseModel):
    payout_ids: List[int] = Field(..., min_length=1, max_length=1000)

class ConfirmResponse(BaseModel):
    status: str
    updated: int
    payout_ids: List[int]

class PayoutDetailResponse(BaseModel):
    id: int
    merchant_id: str
    agent_id: str
    amount: float
    currency: str
    status: str
    payout_reference: Optional[str]
    file_url: Optional[str]
    method: Optional[str]
    provider: Optional[str]
    external_id: Optional[str]
    period_start: date
    period_end: date
    uploaded_at: Optional[datetime]
    confirmed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    
    # Additional info for employee view
    merchant_name: Optional[str] = None
    agent_name: Optional[str] = None
    agent_email: Optional[str] = None

@router.get("", response_model=dict)
async def list_all_payouts(
    status: Optional[str] = Query('uploaded', description="Filter by status: pending, uploaded, paid"),
    merchant_id: Optional[str] = Query(None, description="Filter by merchant"),
    agent_id: Optional[str] = Query(None, description="Filter by agent"),
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=1000),
    sort_by: str = Query("created_at", description="Sort by: created_at, amount, merchant_id"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    current_user: dict = Depends(get_current_user)
):
    """
    List all payouts with filters
    Employees can see payouts across all merchants
    """
    # Verify employee access
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
    
    try:
        # Build query with joins for additional info
        conditions = []
        filter_params: Dict[str, Any] = {}
        query_params: Dict[str, Any] = {"limit": size, "offset": (page-1)*size}
        
        if status:
            conditions.append("p.status = :st")
            filter_params["st"] = status
        
        if merchant_id:
            conditions.append("p.merchant_id = :mid")
            filter_params["mid"] = merchant_id
        
        if agent_id:
            conditions.append("p.agent_id = :aid")
            filter_params["aid"] = agent_id

        # Merge filter params for main query (with pagination)
        query_params.update(filter_params)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        # Validate sort column
        valid_sort_columns = ["created_at", "amount", "merchant_id", "agent_id", "status"]
        if sort_by not in valid_sort_columns:
            sort_by = "created_at"
        
        # Get payouts with merchant and agent info
        query = f"""
        SELECT 
            p.*,
            m.business_name as merchant_name,
            a.name as agent_name,
            a.email as agent_email
        FROM agent_payouts p
        LEFT JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
        LEFT JOIN agents a ON p.agent_id = a.agent_id
        WHERE {where_clause}
        ORDER BY p.{sort_by} {sort_order}
        LIMIT :limit OFFSET :offset
        """
        
        results = await database.fetch_all(query=query, values=query_params)
        
        # Get total count
        count_query = f"SELECT COUNT(*) as total FROM agent_payouts p WHERE {where_clause}"
        total_result = await database.fetch_one(query=count_query, values=filter_params)
        total = total_result["total"] if total_result else 0
        
        # Get summary statistics
        summary_query = f"""
        SELECT 
            COUNT(*) as count,
            SUM(amount) as total_amount,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_count,
            COUNT(CASE WHEN status = 'uploaded' THEN 1 END) as uploaded_count,
            COUNT(CASE WHEN status = 'paid' THEN 1 END) as paid_count,
            SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) as pending_amount,
            SUM(CASE WHEN status = 'uploaded' THEN amount ELSE 0 END) as uploaded_amount,
            SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) as paid_amount
        FROM agent_payouts p
        WHERE {where_clause}
        """
        
        summary_row = await database.fetch_one(query=summary_query, values=filter_params)
        summary = dict(summary_row) if summary_row else None
        
        # Format results
        items = []
        for row in results:
            record = dict(row)
            items.append({
                "id": record["id"],
                "merchant_id": record["merchant_id"],
                "merchant_name": record.get("merchant_name"),
                "agent_id": record["agent_id"],
                "agent_name": record.get("agent_name"),
                "agent_email": record.get("agent_email"),
                "amount": record["amount"],
                "currency": record["currency"],
                "status": record["status"],
                "payout_reference": record.get("payout_reference"),
                "file_url": record.get("file_url"),
                "method": record.get("method"),
                "provider": record.get("provider"),
                "external_id": record.get("external_id"),
                "period_start": record["period_start"],
                "period_end": record["period_end"],
                "uploaded_at": record.get("uploaded_at"),
                "confirmed_at": record.get("confirmed_at"),
                "created_at": record["created_at"],
                "updated_at": record["updated_at"]
            })
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": (total + size - 1) // size if size > 0 else 0,
            "summary": {
                "total_count": summary["count"] if summary else 0,
                "total_amount": float(summary["total_amount"] or 0) if summary else 0,
                "pending": {
                    "count": summary["pending_count"] if summary else 0,
                    "amount": float(summary["pending_amount"] or 0) if summary else 0
                },
                "uploaded": {
                    "count": summary["uploaded_count"] if summary else 0,
                    "amount": float(summary["uploaded_amount"] or 0) if summary else 0
                },
                "paid": {
                    "count": summary["paid_count"] if summary else 0,
                    "amount": float(summary["paid_amount"] or 0) if summary else 0
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Error listing payouts for employee: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list payouts: {e}")

@router.patch("/{payout_id}/confirm", response_model=ConfirmResponse)
async def confirm_single_payout(
    payout_id: int,
    current_user: dict = Depends(get_current_user)
):
    """
    Confirm a single payout (uploaded -> paid).
    """
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only employees can confirm payouts")
    
    try:
        repo = PayoutRepo()
        updated = await repo.confirm(payout_id)
        if updated is False:
            raise HTTPException(status_code=404, detail="Payout not found or not in uploaded status")
        return ConfirmResponse(status="success", updated=1, payout_ids=[payout_id])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error confirming payout {payout_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to confirm payout")

@router.post("/confirm-bulk", response_model=dict)
async def confirm_bulk_payouts(
    request: ConfirmBulkRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Confirm multiple payouts as paid
    Only payouts with status='uploaded' can be confirmed
    """
    # Verify employee access
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
    
    try:
        if not request.payout_ids:
            raise HTTPException(status_code=400, detail="No payout IDs provided")
        
        # Verify all payouts exist and are in 'uploaded' status
        verify_query = """
        SELECT id, status 
        FROM agent_payouts 
        WHERE id = ANY(:ids)
        """
        
        existing_payouts = await database.fetch_all(
            query=verify_query,
            values={"ids": request.payout_ids}
        )
        
        existing_ids = {row["id"] for row in existing_payouts}
        uploaded_ids = {row["id"] for row in existing_payouts if row["status"] == "uploaded"}
        
        # Check for missing payouts
        missing_ids = set(request.payout_ids) - existing_ids
        if missing_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Payouts not found: {list(missing_ids)}"
            )
        
        # Check for invalid status
        not_uploaded_ids = existing_ids - uploaded_ids
        if not_uploaded_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Payouts not in 'uploaded' status: {list(not_uploaded_ids)}"
            )
        
        # Confirm payouts
        repo = PayoutRepo()
        updated = await repo.confirm_bulk(list(uploaded_ids))
        
        # Log the action
        logger.info(
            f"Employee {current_user.get('email')} confirmed {updated} payouts. "
            f"IDs: {list(uploaded_ids)}"
        )
        
        return {
            "status": "success",
            "confirmed": updated,
            "payout_ids": list(uploaded_ids),
            "message": f"Successfully confirmed {updated} payouts"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error confirming bulk payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to confirm payouts")

@router.get("/dashboard", response_model=dict)
async def get_payout_dashboard(
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get payout dashboard statistics for employee overview
    """
    # Verify employee access
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
    
    try:
        # Get overall statistics
        stats_query = f"""
        SELECT
            COUNT(DISTINCT merchant_id) as active_merchants,
            COUNT(DISTINCT agent_id) as active_agents,
            COUNT(*) as total_payouts,
            SUM(amount) as total_amount,
            AVG(amount) as avg_payout_amount,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_count,
            COUNT(CASE WHEN status = 'uploaded' THEN 1 END) as uploaded_count,
            COUNT(CASE WHEN status = 'paid' THEN 1 END) as paid_count
        FROM agent_payouts
        WHERE created_at >= NOW() - INTERVAL '{days} days'
        """
        
        stats_row = await database.fetch_one(query=stats_query)
        stats = dict(stats_row) if stats_row else None
        
        # Get top merchants by payout volume
        top_merchants_query = f"""
        SELECT 
            p.merchant_id,
            m.business_name as merchant_name,
            COUNT(*) as payout_count,
            SUM(p.amount) as total_amount
        FROM agent_payouts p
        LEFT JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
        WHERE p.created_at >= NOW() - INTERVAL '{days} days'
        GROUP BY p.merchant_id, m.business_name
        ORDER BY total_amount DESC
        LIMIT 10
        """
        
        top_merchants_rows = await database.fetch_all(query=top_merchants_query)
        top_merchants = [dict(row) for row in top_merchants_rows]
        
        # Get pending actions summary
        pending_actions_query = """
        SELECT
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as awaiting_upload,
            COUNT(CASE WHEN status = 'uploaded' THEN 1 END) as awaiting_confirmation,
            SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) as pending_amount,
            SUM(CASE WHEN status = 'uploaded' THEN amount ELSE 0 END) as uploaded_amount
        FROM agent_payouts
        """
        
        pending_row = await database.fetch_one(query=pending_actions_query)
        pending = dict(pending_row) if pending_row else None
        
        return {
            "period_days": days,
            "overview": {
                "active_merchants": stats["active_merchants"] if stats else 0,
                "active_agents": stats["active_agents"] if stats else 0,
                "total_payouts": stats["total_payouts"] if stats else 0,
                "total_amount": float(stats["total_amount"] or 0) if stats else 0,
                "avg_payout": float(stats["avg_payout_amount"] or 0) if stats else 0,
                "by_status": {
                    "pending": stats["pending_count"] if stats else 0,
                    "uploaded": stats["uploaded_count"] if stats else 0,
                    "paid": stats["paid_count"] if stats else 0
                }
            },
            "top_merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "merchant_name": m.get("merchant_name"),
                    "payout_count": m["payout_count"],
                    "total_amount": float(m["total_amount"])
                }
                for m in top_merchants
            ],
            "pending_actions": {
                "awaiting_upload": pending["awaiting_upload"] if pending else 0,
                "awaiting_confirmation": pending["awaiting_confirmation"] if pending else 0,
                "pending_amount": float(pending["pending_amount"] or 0) if pending else 0,
                "uploaded_amount": float(pending["uploaded_amount"] or 0) if pending else 0
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting payout dashboard: {e}")
        raise HTTPException(status_code=500, detail="Failed to get dashboard data")
