"""
Admin cleanup and rebuild endpoints for testing
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import logging
from datetime import datetime

from db.database import database
from routes.auth_routes import require_admin

router = APIRouter(prefix="/admin/cleanup", tags=["admin-cleanup"])
logger = logging.getLogger(__name__)


class DeleteOrdersRequest(BaseModel):
    merchant_id: str
    confirm: bool = False


class DeletePSPsRequest(BaseModel):
    merchant_id: str
    confirm: bool = False


@router.post("/delete-all-orders")
async def delete_all_orders(
    request: DeleteOrdersRequest,
    current_user: dict = Depends(require_admin)
):
    """Delete all orders for a merchant"""
    if not request.confirm:
        return {"error": "Must confirm deletion"}
    
    try:
        # Count orders first
        count_query = """
            SELECT COUNT(*) as count 
            FROM orders 
            WHERE merchant_id = :merchant_id
        """
        count_result = await database.fetch_one(
            count_query,
            {"merchant_id": request.merchant_id}
        )
        order_count = count_result["count"] if count_result else 0
        
        # Delete all orders
        delete_query = """
            DELETE FROM orders 
            WHERE merchant_id = :merchant_id
        """
        await database.execute(
            delete_query,
            {"merchant_id": request.merchant_id}
        )
        
        logger.info(f"Deleted {order_count} orders for merchant {request.merchant_id}")
        
        return {
            "success": True,
            "message": f"Deleted {order_count} orders",
            "merchant_id": request.merchant_id
        }
        
    except Exception as e:
        logger.error(f"Error deleting orders: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete-all-psps")
async def delete_all_psps(
    request: DeletePSPsRequest,
    current_user: dict = Depends(require_admin)
):
    """Delete all PSP configurations for a merchant"""
    if not request.confirm:
        return {"error": "Must confirm deletion"}
    
    try:
        # Count PSPs first
        count_query = """
            SELECT COUNT(*) as count 
            FROM merchant_psps 
            WHERE merchant_id = :merchant_id
        """
        count_result = await database.fetch_one(
            count_query,
            {"merchant_id": request.merchant_id}
        )
        psp_count = count_result["count"] if count_result else 0
        
        # Delete all PSPs
        delete_query = """
            DELETE FROM merchant_psps 
            WHERE merchant_id = :merchant_id
        """
        await database.execute(
            delete_query,
            {"merchant_id": request.merchant_id}
        )
        
        logger.info(f"Deleted {psp_count} PSPs for merchant {request.merchant_id}")
        
        return {
            "success": True,
            "message": f"Deleted {psp_count} PSP configurations",
            "merchant_id": request.merchant_id
        }
        
    except Exception as e:
        logger.error(f"Error deleting PSPs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/verify-clean-state/{merchant_id}")
async def verify_clean_state(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """Verify that merchant has clean state"""
    try:
        # Check orders
        order_query = """
            SELECT COUNT(*) as count 
            FROM orders 
            WHERE merchant_id = :merchant_id
        """
        order_result = await database.fetch_one(
            order_query,
            {"merchant_id": merchant_id}
        )
        order_count = order_result["count"] if order_result else 0
        
        # Check PSPs
        psp_query = """
            SELECT COUNT(*) as count 
            FROM merchant_psps 
            WHERE merchant_id = :merchant_id
        """
        psp_result = await database.fetch_one(
            psp_query,
            {"merchant_id": merchant_id}
        )
        psp_count = psp_result["count"] if psp_result else 0
        
        # Check merchant exists
        merchant_query = """
            SELECT merchant_id, contact_email 
            FROM merchant_onboarding 
            WHERE merchant_id = :merchant_id
        """
        merchant = await database.fetch_one(
            merchant_query,
            {"merchant_id": merchant_id}
        )
        
        return {
            "merchant_exists": merchant is not None,
            "merchant_email": merchant["contact_email"] if merchant else None,
            "order_count": order_count,
            "psp_count": psp_count,
            "is_clean": order_count == 0 and psp_count == 0
        }
        
    except Exception as e:
        logger.error(f"Error verifying state: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
