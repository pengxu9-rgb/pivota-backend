"""
Admin endpoint to fix orders with missing psp_id
"""
from fastapi import APIRouter, HTTPException
from db.database import database
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/fix", tags=["admin-fix"])

@router.post("/order-psp-associations/{merchant_id}")
async def fix_order_psp_associations(merchant_id: str):
    """
    Fix orders with NULL psp_id by associating them with merchant's PSPs
    """
    try:
        # Get merchant's PSPs
        psp_query = """
            SELECT psp_id, provider 
            FROM merchant_psps 
            WHERE merchant_id = :merchant_id AND status = 'active'
            ORDER BY 
                CASE WHEN provider = 'stripe' THEN 1 ELSE 2 END,
                connected_at DESC
            LIMIT 1
        """
        psp = await database.fetch_one(psp_query, {"merchant_id": merchant_id})
        
        if not psp:
            return {
                "success": False,
                "message": "No active PSP found for merchant"
            }
        
        # Update orders with NULL psp_id
        result = await database.execute(
            """
            UPDATE orders
            SET psp_id = :psp_id,
                psp_used = :provider
            WHERE merchant_id = :merchant_id
            AND (psp_id IS NULL OR psp_used IS NULL)
            """,
            {
                "psp_id": psp["psp_id"],
                "provider": psp["provider"],
                "merchant_id": merchant_id
            }
        )
        
        orders_fixed = result if result else 0
        
        return {
            "success": True,
            "merchant_id": merchant_id,
            "psp_used": psp["provider"],
            "psp_id": psp["psp_id"],
            "orders_fixed": orders_fixed
        }
        
    except Exception as e:
        logger.error(f"Failed to fix order PSP associations: {e}")
        raise HTTPException(status_code=500, detail=str(e))



