"""
Manual Shopify order creation endpoint for debugging
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from routes.auth_routes import require_admin
from routes.order_routes import create_shopify_order
from db.orders import get_order
from db.merchant_onboarding import get_merchant_onboarding
from utils.logger import logger

router = APIRouter(prefix="/shopify-manual", tags=["Shopify Manual"])

@router.post("/create-order/{order_id}")
async def manually_create_shopify_order(
    order_id: str,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """Manually trigger Shopify order creation for a paid order"""
    
    logger.info(f"Manual Shopify order creation requested for {order_id}")
    
    # Get order
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Check if already has Shopify order
    if order.get("shopify_order_id"):
        return {
            "status": "already_exists",
            "shopify_order_id": order["shopify_order_id"],
            "message": "Order already has a Shopify order ID"
        }
    
    # Check payment status
    if order.get("payment_status") != "paid":
        return {
            "status": "not_paid",
            "payment_status": order.get("payment_status"),
            "message": "Order must be paid before creating Shopify order"
        }
    
    # Get merchant details
    merchant = await get_merchant_onboarding(order["merchant_id"])
    
    merchant_info = {
        "merchant_id": order["merchant_id"],
        "mcp_connected": merchant.get("mcp_connected") if merchant else False,
        "mcp_platform": merchant.get("mcp_platform") if merchant else None,
        "mcp_shop_domain": merchant.get("mcp_shop_domain") if merchant else None,
        "has_access_token": bool(merchant.get("mcp_access_token")) if merchant else False
    }
    
    # Try to create Shopify order
    try:
        logger.info(f"Calling create_shopify_order for {order_id}")
        success = await create_shopify_order(order_id)
        
        if success:
            # Get updated order with Shopify ID
            updated_order = await get_order(order_id)
            
            return {
                "status": "success",
                "order_id": order_id,
                "shopify_order_id": updated_order.get("shopify_order_id"),
                "merchant_info": merchant_info,
                "message": "Shopify order created successfully"
            }
        else:
            return {
                "status": "failed",
                "order_id": order_id,
                "merchant_info": merchant_info,
                "message": "Failed to create Shopify order - check logs"
            }
            
    except Exception as e:
        logger.error(f"Exception in manual Shopify order creation: {str(e)}")
        return {
            "status": "error",
            "order_id": order_id,
            "merchant_info": merchant_info,
            "error": str(e),
            "message": "Exception occurred - check logs"
        }

