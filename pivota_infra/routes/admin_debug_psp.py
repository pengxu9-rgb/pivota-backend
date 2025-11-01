"""
Admin Debug Endpoint for PSP Overview Issue
"""
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException
from db.database import database

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])
logger = logging.getLogger(__name__)

@router.get("/psp-overview-diagnosis")
async def diagnose_psp_overview():
    """
    Diagnose why PSP Overview shows 0 transactions
    Returns detailed breakdown of data and query results
    """
    try:
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {}
        }
        
        # 1. Check orders table
        orders = await database.fetch_all("""
            SELECT order_id, merchant_id, psp_used, psp_id, payment_status, total, created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT 5
        """)
        result["checks"]["orders"] = {
            "count": len(orders),
            "sample": [
                {
                    "order_id": o["order_id"],
                    "merchant_id": o["merchant_id"],
                    "psp_used": o["psp_used"],
                    "psp_id": o["psp_id"],
                    "payment_status": o["payment_status"],
                    "total": float(o["total"]) if o["total"] else 0,
                    "created_at": o["created_at"].isoformat() if o["created_at"] else None
                }
                for o in orders
            ]
        }
        
        # 2. Check merchant_psps table
        psps = await database.fetch_all("""
            SELECT psp_id, provider, merchant_id, status, connected_at
            FROM merchant_psps
            ORDER BY connected_at DESC
        """)
        result["checks"]["merchant_psps"] = {
            "count": len(psps),
            "configs": [
                {
                    "psp_id": p["psp_id"],
                    "provider": p["provider"],
                    "merchant_id": p["merchant_id"],
                    "status": p["status"],
                    "connected_at": p["connected_at"].isoformat() if p["connected_at"] else None
                }
                for p in psps
            ]
        }
        
        # 3. Test the actual JOIN query (last 7 days)
        start_time = datetime.utcnow() - timedelta(days=7)
        
        join_results = await database.fetch_all("""
            SELECT 
                mp.provider as psp_name,
                mp.psp_id as mp_psp_id,
                mp.status,
                COUNT(DISTINCT mp.merchant_id) as merchant_count,
                COUNT(o.order_id) as all_orders_joined,
                COUNT(CASE WHEN LOWER(o.psp_used) = LOWER(mp.provider) THEN o.order_id END) as matching_psp_used,
                COUNT(CASE WHEN o.psp_id = mp.psp_id THEN o.order_id END) as matching_psp_id,
                COUNT(CASE WHEN o.payment_status = 'paid' AND LOWER(o.psp_used) = LOWER(mp.provider) THEN 1 END) as success_count,
                COALESCE(SUM(CASE WHEN o.payment_status = 'paid' AND LOWER(o.psp_used) = LOWER(mp.provider) THEN o.total ELSE 0 END), 0) as total_volume
            FROM merchant_psps mp
            LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
                AND o.created_at >= :start_time
                AND ((o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
                     OR (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider)))
            WHERE mp.status = 'active'
            GROUP BY mp.provider, mp.psp_id, mp.status
        """, {"start_time": start_time})
        
        result["checks"]["join_query_7days"] = {
            "start_time": start_time.isoformat(),
            "results": [
                {
                    "psp_name": r["psp_name"],
                    "psp_id": r["mp_psp_id"],
                    "status": r["status"],
                    "merchant_count": r["merchant_count"],
                    "all_orders_joined": r["all_orders_joined"],
                    "matching_psp_used": r["matching_psp_used"],
                    "matching_psp_id": r["matching_psp_id"],
                    "success_count": r["success_count"],
                    "total_volume": float(r["total_volume"])
                }
                for r in join_results
            ]
        }
        
        # 4. Simplified JOIN - just merchant_id
        simple_join = await database.fetch_all("""
            SELECT 
                mp.provider,
                mp.merchant_id,
                mp.psp_id as mp_psp_id,
                COUNT(o.order_id) as order_count,
                STRING_AGG(DISTINCT o.psp_used, ', ') as psp_used_values,
                STRING_AGG(DISTINCT o.psp_id, ', ') as psp_id_values
            FROM merchant_psps mp
            LEFT JOIN orders o ON o.merchant_id = mp.merchant_id
                AND o.created_at >= :start_time
            WHERE mp.status = 'active'
            GROUP BY mp.provider, mp.merchant_id, mp.psp_id
        """, {"start_time": start_time})
        
        result["checks"]["simple_join_merchant_only"] = [
            {
                "provider": r["provider"],
                "merchant_id": r["merchant_id"],
                "mp_psp_id": r["mp_psp_id"],
                "order_count": r["order_count"],
                "psp_used_in_orders": r["psp_used_values"],
                "psp_id_in_orders": r["psp_id_values"]
            }
            for r in simple_join
        ]
        
        # 5. Case sensitivity check
        case_check = await database.fetch_all("""
            SELECT DISTINCT
                o.psp_used as order_psp_used,
                mp.provider as config_provider,
                CASE WHEN o.psp_used = mp.provider THEN true ELSE false END as exact_match,
                CASE WHEN LOWER(o.psp_used) = LOWER(mp.provider) THEN true ELSE false END as case_insensitive_match
            FROM orders o
            CROSS JOIN merchant_psps mp
            WHERE o.psp_used IS NOT NULL
            LIMIT 10
        """)
        
        result["checks"]["case_sensitivity"] = [
            {
                "order_psp_used": r["order_psp_used"],
                "config_provider": r["config_provider"],
                "exact_match": r["exact_match"],
                "case_insensitive_match": r["case_insensitive_match"]
            }
            for r in case_check
        ]
        
        # 6. Count active PSPs vs total PSPs
        psp_status = await database.fetch_one("""
            SELECT 
                COUNT(*) as total_psps,
                COUNT(CASE WHEN status = 'active' THEN 1 END) as active_psps,
                COUNT(CASE WHEN status != 'active' THEN 1 END) as inactive_psps
            FROM merchant_psps
        """)
        result["checks"]["psp_status_summary"] = {
            "total_psps": psp_status["total_psps"],
            "active_psps": psp_status["active_psps"],
            "inactive_psps": psp_status["inactive_psps"]
        }
        
        return result
        
    except Exception as e:
        logger.error(f"Error in PSP diagnosis: {e}")
        raise HTTPException(status_code=500, detail=f"Diagnosis failed: {str(e)}")


