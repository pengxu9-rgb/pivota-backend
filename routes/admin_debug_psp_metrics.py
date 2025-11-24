"""
Debug endpoint to check PSP metrics data
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any, Optional
import logging
from db.database import database
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])


@router.get("/psp-metrics/{merchant_id}")
async def debug_psp_metrics(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Debug PSP metrics for a merchant"""
    if current_user["role"] not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check orders
        orders_query = """
            SELECT 
                order_id,
                psp_id,
                psp_used,
                payment_status,
                total,
                refund_total,
                is_deleted,
                created_at
            FROM orders
            WHERE merchant_id = :merchant_id
            ORDER BY created_at DESC
            LIMIT 20
        """
        orders = await database.fetch_all(orders_query, {"merchant_id": merchant_id})
        
        # Check merchant_psps
        psps_query = """
            SELECT psp_id, provider, name, status
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
        """
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        
        # Check the aggregation query
        metrics_query = """
            SELECT 
                psp_id,
                COUNT(*) as total_orders,
                SUM(CASE WHEN payment_status IN ('paid', 'captured', 'succeeded') THEN 1 ELSE 0 END) as successful_orders,
                COALESCE(SUM(CASE 
                    WHEN payment_status IN ('paid', 'captured', 'succeeded') 
                    THEN total ELSE 0 END), 0) as total_volume,
                COALESCE(SUM(CASE 
                    WHEN payment_status IN ('paid', 'captured', 'succeeded') 
                    THEN COALESCE(refund_total, 0) ELSE 0 END), 0) as total_refunds,
                array_agg(DISTINCT payment_status) as statuses
            FROM orders
            WHERE merchant_id = :merchant_id 
            AND psp_id IS NOT NULL
            AND (is_deleted IS NULL OR is_deleted = FALSE)
            GROUP BY psp_id
        """
        metrics = await database.fetch_all(metrics_query, {"merchant_id": merchant_id})
        
        # Check without psp_id filter to see all orders
        all_orders_query = """
            SELECT 
                payment_status,
                COUNT(*) as count,
                SUM(total) as volume
            FROM orders
            WHERE merchant_id = :merchant_id
            AND (is_deleted IS NULL OR is_deleted = FALSE)
            GROUP BY payment_status
        """
        all_orders_stats = await database.fetch_all(all_orders_query, {"merchant_id": merchant_id})
        
        return {
            "success": True,
            "merchant_id": merchant_id,
            "orders_sample": [
                {
                    "order_id": o["order_id"],
                    "psp_id": o["psp_id"],
                    "psp_used": o["psp_used"],
                    "payment_status": o["payment_status"],
                    "total": float(o["total"]) if o["total"] else 0,
                    "is_deleted": o["is_deleted"],
                    "created_at": str(o["created_at"])
                }
                for o in orders
            ],
            "merchant_psps": [
                {
                    "psp_id": p["psp_id"],
                    "provider": p["provider"],
                    "name": p["name"],
                    "status": p["status"]
                }
                for p in psps
            ],
            "aggregated_metrics": [
                {
                    "psp_id": m["psp_id"],
                    "total_orders": m["total_orders"],
                    "successful_orders": m["successful_orders"],
                    "total_volume": float(m["total_volume"]),
                    "total_refunds": float(m["total_refunds"]),
                    "statuses": m["statuses"]
                }
                for m in metrics
            ],
            "all_orders_by_status": [
                {
                    "payment_status": s["payment_status"],
                    "count": s["count"],
                    "volume": float(s["volume"]) if s["volume"] else 0
                }
                for s in all_orders_stats
            ]
        }
        
    except Exception as e:
        logger.error(f"Error debugging PSP metrics: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e)
        }

