"""
Admin endpoint to fix missing psp_id in orders
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
import logging
from db.database import database
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin Fixes"])


@router.post("/fix-orders-psp-id")
async def fix_orders_psp_id(
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Backfill missing psp_id in orders table
    Only accessible by admin/employee
    """
    if current_user["role"] not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Check how many orders are missing psp_id
        check_query = """
            SELECT COUNT(*) as missing_count
            FROM orders
            WHERE psp_id IS NULL AND psp_used IS NOT NULL
        """
        result = await database.fetch_one(check_query)
        missing_count = result['missing_count'] if result else 0
        
        logger.info(f"Found {missing_count} orders with missing psp_id")
        
        if missing_count == 0:
            return {
                "success": True,
                "message": "No orders need fixing",
                "fixed_count": 0,
                "still_missing": 0
            }
        
        # Update orders: match with merchant_psps based on merchant_id + provider
        update_query = """
            UPDATE orders o
            SET psp_id = mp.psp_id
            FROM merchant_psps mp
            WHERE o.merchant_id = mp.merchant_id
            AND LOWER(o.psp_used) = LOWER(mp.provider)
            AND o.psp_id IS NULL
            AND o.psp_used IS NOT NULL
            AND mp.status = 'active'
        """
        
        await database.execute(update_query)
        logger.info(f"✅ Updated orders with psp_id")
        
        # Verify the fix
        verify_result = await database.fetch_one(check_query)
        still_missing = verify_result['missing_count'] if verify_result else 0
        fixed_count = missing_count - still_missing
        
        # Get examples of unfixed orders
        examples = []
        if still_missing > 0:
            examples_query = """
                SELECT order_id, merchant_id, psp_used, payment_status
                FROM orders
                WHERE psp_id IS NULL AND psp_used IS NOT NULL
                LIMIT 5
            """
            example_rows = await database.fetch_all(examples_query)
            examples = [
                {
                    "order_id": row["order_id"],
                    "merchant_id": row["merchant_id"],
                    "psp_used": row["psp_used"],
                    "payment_status": row["payment_status"]
                }
                for row in example_rows
            ]
        
        return {
            "success": True,
            "message": f"Fixed {fixed_count} orders",
            "fixed_count": fixed_count,
            "still_missing": still_missing,
            "unfixed_examples": examples
        }
        
    except Exception as e:
        logger.error(f"Error fixing psp_ids: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e)
        }

