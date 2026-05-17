"""Admin endpoint to cleanup test data and reset system"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.database import database
from routes.auth_routes import require_admin
from utils.runtime_safety import require_runtime_gate
import logging

router = APIRouter(prefix="/admin/cleanup", tags=["Admin Cleanup"])
logger = logging.getLogger(__name__)

class CleanupRequest(BaseModel):
    keep_merchant_id: str
    confirm: bool = False

@router.post("/remove-other-merchants")
async def remove_other_merchants(
    request: CleanupRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Remove all merchants except the specified one
    
    This cleans up test/mock data and keeps only the real merchant account
    
    Args:
        keep_merchant_id: The merchant to keep
        confirm: Must be true to execute
    """
    require_runtime_gate("ENABLE_DESTRUCTIVE_ADMIN_CLEANUP")
    if not request.confirm:
        return {
            "status": "warning",
            "message": "Set confirm=true to execute cleanup",
            "would_delete": "All merchants except " + request.keep_merchant_id
        }
    
    try:
        async with database.transaction():
            # Get list of merchants to delete
            merchants_to_delete = await database.fetch_all(
                """
                SELECT merchant_id, business_name, contact_email 
                FROM merchant_onboarding 
                WHERE merchant_id != :keep_id
                """,
                {"keep_id": request.keep_merchant_id}
            )
            
            if not merchants_to_delete:
                return {
                    "status": "success",
                    "message": "No other merchants to delete",
                    "kept_merchant": request.keep_merchant_id
                }
            
            merchant_ids_to_delete = [m["merchant_id"] for m in merchants_to_delete]
            
            # Delete in order (respecting foreign keys), using bound params only.
            for merchant_id in merchant_ids_to_delete:
                params = {"merchant_id": merchant_id}
                await database.execute("DELETE FROM orders WHERE merchant_id = :merchant_id", params)
                await database.execute("DELETE FROM merchant_psps WHERE merchant_id = :merchant_id", params)
                try:
                    await database.execute("DELETE FROM merchant_stores WHERE merchant_id = :merchant_id", params)
                except Exception:
                    pass  # Table may not exist
                await database.execute("DELETE FROM users WHERE merchant_id = :merchant_id", params)
                await database.execute("DELETE FROM merchant_onboarding WHERE merchant_id = :merchant_id", params)
            
            logger.info(f"✅ Cleanup complete. Kept {request.keep_merchant_id}, deleted {len(merchants_to_delete)} merchants")
            
            return {
                "status": "success",
                "message": f"Cleanup complete. Deleted {len(merchants_to_delete)} merchants",
                "kept_merchant": request.keep_merchant_id,
                "deleted_merchants": [
                    {
                        "merchant_id": m["merchant_id"],
                        "business_name": m["business_name"],
                        "email": m["contact_email"]
                    }
                    for m in merchants_to_delete
                ],
                "stats": {
                    "merchants_deleted": len(merchants_to_delete),
                    "orders_deleted": "deleted",
                    "psps_deleted": "deleted",
                    "users_deleted": "deleted"
                }
            }
        
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleanup failed: {str(e)}")

@router.get("/list-merchants")
async def list_all_merchants(current_user: dict = Depends(require_admin)):
    """List all merchants in the system"""
    try:
        merchants = await database.fetch_all(
            """
            SELECT 
                m.merchant_id,
                m.business_name,
                m.contact_email,
                m.mcp_shop_domain,
                COUNT(DISTINCT o.order_id) as order_count,
                COUNT(DISTINCT mp.psp_id) as psp_count,
                COALESCE(SUM(o.total), 0) as total_revenue
            FROM merchant_onboarding m
            LEFT JOIN orders o ON o.merchant_id = m.merchant_id
            LEFT JOIN merchant_psps mp ON mp.merchant_id = m.merchant_id AND mp.status = 'active'
            GROUP BY m.merchant_id, m.business_name, m.contact_email, m.mcp_shop_domain
            ORDER BY order_count DESC
            """
        )
        
        return {
            "status": "success",
            "total_merchants": len(merchants),
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "email": m["contact_email"],
                    "shop_domain": m["mcp_shop_domain"],
                    "order_count": m["order_count"],
                    "psp_count": m["psp_count"],
                    "total_revenue": float(m["total_revenue"]) if m["total_revenue"] else 0
                }
                for m in merchants
            ]
        }
        
    except Exception as e:
        logger.error(f"Failed to list merchants: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list merchants: {str(e)}")
