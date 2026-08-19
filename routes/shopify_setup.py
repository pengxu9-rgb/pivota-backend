"""
Temporary endpoint to setup Shopify for the merchant
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
from typing import Dict, Any
import os
import httpx
from config.platform import is_deployed
from config.settings import settings
from db.merchant_onboarding import merchant_onboarding, get_merchant_onboarding
from db.orders import orders
from db.database import database
from routes.order_routes import create_shopify_order
from utils.auth import require_admin
from utils.logger import logger

router = APIRouter(prefix="/shopify-setup", tags=["Shopify Setup"])

def _is_production() -> bool:
    return (
        os.getenv("APP_ENV", "").lower() == "production"
        or os.getenv("ENVIRONMENT", "").lower() == "production"
        # `bool(RAILWAY_GIT_COMMIT_SHA)` meant "deployed"; False on Cloud Run.
        or is_deployed()
    )


def _shopify_setup_enabled() -> bool:
    return (os.getenv("ENABLE_SHOPIFY_SETUP_ENDPOINTS") or "").strip().lower() in {"1", "true", "yes", "on"}


@router.post("/configure/{merchant_id}")
async def configure_shopify(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """Configure Shopify for a merchant using environment variables"""
    if _is_production() and not _shopify_setup_enabled():
        # Avoid accidental credential overwrites in production.
        raise HTTPException(status_code=404, detail="Not found")
    
    # Get Shopify credentials from environment
    shopify_store = settings.shopify_store_url
    shopify_token = settings.shopify_access_token
    
    if not shopify_store:
        raise HTTPException(status_code=400, detail="SHOPIFY_STORE_URL not set in environment")
    if not shopify_token:
        raise HTTPException(status_code=400, detail="SHOPIFY_ACCESS_TOKEN not set in environment")

    # Validate env credentials before writing them to DB.
    shop_domain = shopify_store.replace("https://", "").replace("http://", "").strip().strip("/").lower()
    try:
        url = f"https://{shop_domain}/admin/api/2025-10/shop.json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"X-Shopify-Access-Token": shopify_token})
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Invalid Shopify env credentials (status={resp.status_code})")
        shop = (resp.json() or {}).get("shop") or {}
        canonical = str(shop.get("myshopify_domain") or "").strip().lower()
        if canonical and canonical != shop_domain:
            raise HTTPException(
                status_code=400,
                detail=f"Env token does not match shop domain (expected {shop_domain}, got {canonical})",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to validate Shopify env credentials")
    
    # Update merchant with Shopify fields
    update_data = {
        "mcp_connected": True,
        "mcp_platform": "shopify",
        "mcp_shop_domain": shop_domain,
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
        "mcp_connected": True,
        "mcp_platform": store_info.get("platform"),
        "mcp_shop_domain": store_info.get("domain"),
        "has_access_token": bool(store_info.get("api_key")),
        "ready_for_shopify": True and store_info.get("api_key")
    }
