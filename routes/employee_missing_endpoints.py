"""
Missing Employee Portal Endpoints
Only includes endpoints that don't exist elsewhere
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import get_current_user
from db.database import database

router = APIRouter()

# ============== Admin Endpoints ==============

# ============== Analytics ==============

@router.get("/analytics/payment-success")
async def get_payment_analytics(
    current_user: dict = Depends(get_current_user)
):
    """Get payment success analytics"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get payment success rate by status
        analytics_query = """
            SELECT 
                status,
                COUNT(*) as count,
                COALESCE(SUM(total), 0) as total_amount
            FROM orders
            WHERE (is_deleted IS NULL OR is_deleted = FALSE)
            GROUP BY status
        """
        
        results = await database.fetch_all(analytics_query)
        
        status_breakdown = []
        total_orders = 0
        successful_orders = 0
        
        for result in results:
            count = result["count"]
            total_orders += count
            
            if result["status"] in ["completed", "delivered"]:
                successful_orders += count
            
            status_breakdown.append({
                "status": result["status"],
                "count": count,
                "amount": float(result["total_amount"]),
                "percentage": 0  # Will calculate after
            })
        
        # Calculate percentages
        for item in status_breakdown:
            item["percentage"] = round((item["count"] / total_orders * 100), 1) if total_orders > 0 else 0
        
        success_rate = (successful_orders / total_orders * 100) if total_orders > 0 else 0
        
        return {
            "status": "success",
            "analytics": {
                "overall_success_rate": round(success_rate, 1),
                "total_transactions": total_orders,
                "successful_transactions": successful_orders,
                "failed_transactions": total_orders - successful_orders,
                "status_breakdown": status_breakdown
            }
        }
    
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
