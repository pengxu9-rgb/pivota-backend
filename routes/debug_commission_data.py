"""
Debug Commission Data
Temporary endpoint to diagnose why merchant sees no pending commissions
"""

from fastapi import APIRouter
from db.database import database
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug/commission", tags=["Debug"])

MERCHANT_ID = "merch_208139f7600dbf42"
AGENT_ID = "asdf@asdf.com"

@router.get("/check")
async def check_commission_data():
    """
    Diagnose why merchant portal shows no pending commissions
    This is a temporary debug endpoint - remove after fixing
    """
    
    result = {
        "merchant_id": MERCHANT_ID,
        "agent_id": AGENT_ID,
        "checks": []
    }
    
    try:
        # Check 1: Agent revenue logs
        logs = await database.fetch_all("""
            SELECT 
                id, tx_id, merchant_id, agent_earned_amount, settlement_status
            FROM agent_revenue_logs
            WHERE agent_id = :agent_id
              AND (settlement_status IS NULL OR settlement_status IN ('pending','processing'))
            ORDER BY created_at DESC
            LIMIT 10
        """, {"agent_id": AGENT_ID})
        
        total_pending = sum(float(log['agent_earned_amount'] or 0) for log in logs)
        merchant_ids = set(log['merchant_id'] if log['merchant_id'] else 'NULL' for log in logs)
        
        result["checks"].append({
            "name": "agent_revenue_logs_raw",
            "status": "success",
            "count": len(logs),
            "total_pending": total_pending,
            "merchant_ids_in_logs": list(merchant_ids),
            "sample_records": [
                {
                    "id": log['id'],
                    "tx_id": log['tx_id'],
                    "merchant_id": log['merchant_id'],
                    "amount": float(log['agent_earned_amount'] or 0)
                }
                for log in logs[:5]
            ]
        })
        
        # Check 2: Join with orders
        joined = await database.fetch_all("""
            SELECT 
                ar.id,
                ar.tx_id,
                ar.merchant_id as log_merchant_id,
                o.merchant_id as order_merchant_id,
                ar.agent_earned_amount,
                o.payment_status
            FROM agent_revenue_logs ar
            LEFT JOIN orders o ON o.order_id = ar.tx_id
            WHERE ar.agent_id = :agent_id
              AND (ar.settlement_status IS NULL OR ar.settlement_status IN ('pending','processing'))
            LIMIT 10
        """, {"agent_id": AGENT_ID})
        
        matching_merchant = []
        for row in joined:
            log_merch = row['log_merchant_id']
            order_merch = row['order_merchant_id']
            
            if log_merch == MERCHANT_ID or order_merch == MERCHANT_ID:
                matching_merchant.append({
                    "id": row['id'],
                    "tx_id": row['tx_id'],
                    "log_merchant_id": log_merch,
                    "order_merchant_id": order_merch,
                    "amount": float(row['agent_earned_amount'] or 0),
                    "payment_status": row['payment_status']
                })
        
        result["checks"].append({
            "name": "after_join_with_orders",
            "status": "success",
            "total_rows": len(joined),
            "matching_our_merchant": len(matching_merchant),
            "matches": matching_merchant
        })
        
        # Check 3: Run actual API query
        api_query_result = await database.fetch_all("""
            SELECT 
                ar.agent_id,
                COUNT(*) as transaction_count,
                SUM(ar.agent_earned_amount) as total_commission,
                COALESCE(ar.merchant_id, o.merchant_id) as resolved_merchant_id
            FROM agent_revenue_logs ar
            LEFT JOIN orders o ON o.order_id = ar.tx_id
            LEFT JOIN agent_payout_links apl ON ar.id = apl.revenue_id
            WHERE COALESCE(ar.merchant_id, o.merchant_id) = :merchant_id
              AND ar.agent_id IS NOT NULL
              AND ar.created_at >= NOW() - INTERVAL '180 days'
              AND (ar.settlement_status IS NULL OR ar.settlement_status IN ('pending','processing'))
              AND apl.revenue_id IS NULL
              AND (o.order_id IS NULL OR (o.payment_status IN ('paid','captured','succeeded','completed','fulfilled') AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)))
            GROUP BY ar.agent_id, COALESCE(ar.merchant_id, o.merchant_id)
        """, {"merchant_id": MERCHANT_ID})
        
        result["checks"].append({
            "name": "actual_api_query",
            "status": "success",
            "agents_returned": len(api_query_result),
            "agents": [
                {
                    "agent_id": row['agent_id'],
                    "transaction_count": row['transaction_count'],
                    "total_commission": float(row['total_commission'] or 0),
                    "merchant_id": row['resolved_merchant_id']
                }
                for row in api_query_result
            ]
        })
        
        # Check 4: Orders for this merchant
        orders_count = await database.fetch_one("""
            SELECT 
                COUNT(*) as total_orders,
                COUNT(CASE WHEN agent_id IS NOT NULL THEN 1 END) as orders_with_agent,
                SUM(total) as total_revenue
            FROM orders
            WHERE merchant_id = :merchant_id
              AND (is_deleted IS NULL OR is_deleted = FALSE)
        """, {"merchant_id": MERCHANT_ID})
        
        result["checks"].append({
            "name": "orders_for_merchant",
            "status": "success",
            "total_orders": orders_count['total_orders'],
            "orders_with_agent": orders_count['orders_with_agent'],
            "total_revenue": float(orders_count['total_revenue'] or 0)
        })
        
        # Diagnosis
        result["diagnosis"] = {
            "agent_has_pending_commissions": total_pending > 0,
            "merchant_ids_in_revenue_logs": list(merchant_ids),
            "our_merchant_id": MERCHANT_ID,
            "merchant_id_matches": MERCHANT_ID in merchant_ids or 'NULL' in merchant_ids,
            "api_returns_data": len(api_query_result) > 0,
            
            "conclusion": ""
        }
        
        if len(api_query_result) > 0:
            result["diagnosis"]["conclusion"] = "✅ API should return data - check frontend"
        elif matching_merchant:
            result["diagnosis"]["conclusion"] = "⚠️ Data exists but API filters it out - check query logic"
        elif 'NULL' in merchant_ids:
            result["diagnosis"]["conclusion"] = "❌ merchant_id is NULL in revenue_logs - need to join with orders"
        elif MERCHANT_ID not in merchant_ids:
            result["diagnosis"]["conclusion"] = f"❌ Wrong merchant_id in revenue_logs - expected {MERCHANT_ID}, found {merchant_ids}"
        else:
            result["diagnosis"]["conclusion"] = "❓ Unknown issue - need deeper investigation"
        
        await database.disconnect()
        
        return result
        
    except Exception as e:
        logger.error(f"Diagnosis failed: {e}")
        import traceback
        return {
            "error": str(e),
            "traceback": traceback.format_exc()
        }

@router.get("/fix-merchant-ids")
async def fix_null_merchant_ids():
    """
    Fix NULL merchant_ids in agent_revenue_logs by copying from orders
    """
    try:
        await database.connect()
        
        # Update NULL merchant_ids
        updated = await database.execute("""
            UPDATE agent_revenue_logs ar
            SET merchant_id = o.merchant_id
            FROM orders o
            WHERE ar.tx_id = o.order_id
              AND ar.merchant_id IS NULL
              AND o.merchant_id IS NOT NULL
        """)
        
        await database.disconnect()
        
        return {
            "status": "success",
            "message": f"Updated {updated} records",
            "updated_count": updated
        }
        
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
