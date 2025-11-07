"""
Employee Finance Management
Real financial data from orders and transactions
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from datetime import datetime, timedelta
from db.database import database
from utils.auth import get_current_user

router = APIRouter(prefix="/employee/finance", tags=["Employee Finance"])


@router.get("/summary")
async def get_finance_summary(
    time_range: str = "today",
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Get financial summary for employee dashboard
    Real data from orders table
    """
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Determine time range
        now = datetime.now()
        if time_range == "today":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif time_range == "week":
            since = now - timedelta(days=7)
        elif time_range == "month":
            since = now - timedelta(days=30)
        else:  # all
            since = datetime(2020, 1, 1)  # Far past date
        
        successful_statuses = (
            'paid', 'captured', 'succeeded', 'completed', 'fulfilled'
        )
        status_params = {
            "since": since,
            "statuses": list(successful_statuses)
        }
        
        # Total Revenue (all paid orders)
        total_revenue_raw = await database.fetch_val(
            """SELECT COALESCE(SUM(total), 0) FROM orders 
               WHERE payment_status = ANY(:statuses)
               AND created_at >= :since
               AND (is_deleted IS NULL OR is_deleted = FALSE)""",
            status_params
        ) or 0
        
        # Convert to float to avoid Decimal * float errors
        total_revenue = float(total_revenue_raw)
        
        # Total orders count
        total_orders = await database.fetch_val(
            """SELECT COUNT(*) FROM orders 
               WHERE payment_status = ANY(:statuses)
               AND created_at >= :since
               AND (is_deleted IS NULL OR is_deleted = FALSE)""",
            dict(status_params)
        ) or 0
        
        # Query actual agent commissions from commissions table
        agent_commissions_raw = await database.fetch_val(
            """SELECT COALESCE(SUM(amount), 0) FROM commissions
               WHERE type = 'agent'
               AND created_at >= :since""",
            {"since": since}
        ) or 0
        agent_commissions = float(agent_commissions_raw)
        
        # Platform fee (assume 2.9% + $0.30 per transaction for now)
        # TODO: Get actual PSP fees from payment records
        estimated_fees = float(total_orders) * 0.30 + (total_revenue * 0.029)
        
        # Platform revenue - currently $0 (not charging yet)
        platform_revenue = 0.0
        
        # Merchant payouts = revenue - PSP fees - agent commissions
        merchant_payouts = total_revenue - estimated_fees - agent_commissions
        
        return {
            "status": "success",
            "summary": {
                "total_revenue": float(total_revenue),
                "merchant_payouts": float(merchant_payouts),
                "agent_commissions": float(agent_commissions),
                "platform_revenue": float(platform_revenue),
                "pending_amount": 0,  # TODO: Query pending payouts table
                "total_orders": total_orders,
                "period": time_range
            },
            "pending_payouts": [],  # TODO: Implement payouts table and query
            "time_range": time_range,
            "calculated_at": now.isoformat()
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "summary": {
                "total_revenue": 0,
                "merchant_payouts": 0,
                "agent_commissions": 0,
                "platform_revenue": 0,
                "pending_amount": 0,
                "total_orders": 0
            },
            "pending_payouts": []
        }


