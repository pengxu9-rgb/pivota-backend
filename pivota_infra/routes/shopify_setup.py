"""
Temporary endpoint to setup Shopify for the merchant
"""
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
from typing import Dict, Any
from config.settings import settings
from db.merchant_onboarding import merchant_onboarding, get_merchant_onboarding
from db.orders import orders
from db.database import database
from routes.order_routes import create_shopify_order
from routes.auth_routes import require_admin
from utils.logger import logger

router = APIRouter(prefix="/shopify-setup", tags=["Shopify Setup"])

@router.post("/configure/{merchant_id}")
async def configure_shopify(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """Configure Shopify for a merchant using environment variables"""
    
    # Get Shopify credentials from environment
    shopify_store = settings.shopify_store_url or "chydantest.myshopify.com"
    shopify_token = settings.shopify_access_token
    
    if not shopify_token:
        raise HTTPException(status_code=400, detail="SHOPIFY_ACCESS_TOKEN not set in environment")
    
    # Update merchant with Shopify fields
    update_data = {
        "mcp_connected": True,
        "mcp_platform": "shopify",
        "mcp_shop_domain": shopify_store.replace("https://", "").replace("http://", ""),
        "mcp_access_token": shopify_token,
        "updated_at": datetime.now()
    }
    
    # Use database operation directly
    query = merchant_onboarding.update().where(
        merchant_onboarding.c.merchant_id == merchant_id
    ).values(**update_data)
    
    result = await database.execute(query)
    
    if not result:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    logger.info(f"Configured Shopify for merchant {merchant_id}")
    
    # Try to create any pending Shopify orders
    pending_orders = await database.fetch_all(
        orders.select().where(
            (orders.c.merchant_id == merchant_id) &
            (orders.c.payment_status == 'paid') &
            (orders.c.shopify_order_id.is_(None))
        ).limit(5)
    )
    
    shopify_orders_created = []
    for order in pending_orders:
        success = await create_shopify_order(order['order_id'])
        if success:
            shopify_orders_created.append(order['order_id'])
            logger.info(f"Created Shopify order for {order['order_id']}")
    
    return {
        "status": "success",
        "merchant_id": merchant_id,
        "shopify_store": shopify_store,
        "shopify_configured": True,
        "pending_orders_processed": len(shopify_orders_created),
        "shopify_orders_created": shopify_orders_created
    }

@router.get("/test/{merchant_id}")
async def test_shopify_connection(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """Test Shopify connection for a merchant"""
    
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    return {
        "merchant_id": merchant_id,
        "mcp_connected": merchant.get("mcp_connected"),
        "mcp_platform": merchant.get("mcp_platform"),
        "mcp_shop_domain": merchant.get("mcp_shop_domain"),
        "has_access_token": bool(merchant.get("mcp_access_token")),
        "ready_for_shopify": merchant.get("mcp_connected") and merchant.get("mcp_access_token")
    }
