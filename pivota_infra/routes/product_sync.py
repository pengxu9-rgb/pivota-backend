"""
Product Sync Routes - Real Implementation
Syncs products from e-commerce platforms to products_cache
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json

from utils.auth import get_current_user
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from db.products import upsert_product_cache
from adapters.product_adapters import fetch_merchant_products
from utils.logger import logger

router = APIRouter(prefix="/products/sync", tags=["product-sync"])

class SyncRequest(BaseModel):
    merchant_id: str
    force_refresh: bool = False
    limit: int = 250

class SyncResponse(BaseModel):
    status: str
    message: str
    merchant_id: str
    platform: str
    products_synced: int
    sync_time: str

@router.post("/", response_model=SyncResponse)
async def sync_products(
    request: SyncRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Sync products from merchant's e-commerce platform to products_cache
    
    This endpoint:
    1. Fetches merchant's platform credentials
    2. Calls the platform API (Shopify/Wix/WooCommerce)
    3. Converts products to StandardProduct format
    4. Stores them in products_cache for agent access
    
    Returns:
        Number of products synced and sync status
    """
    start_time = datetime.now()
    
    try:
        # 1. Get merchant info
        merchant = await get_merchant_onboarding(request.merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 2. Check if merchant has MCP connected
        if not merchant.get("mcp_connected"):
            raise HTTPException(
                status_code=400,
                detail="Merchant has not connected to any e-commerce platform. Please connect first."
            )
        
        platform = merchant.get("mcp_platform")
        if not platform:
            raise HTTPException(status_code=400, detail="MCP platform not specified")
        
        logger.info(f"🔄 Starting product sync for merchant {request.merchant_id} on platform {platform}")
        
        # 3. Get platform credentials
        credentials = {}
        
        if platform == "shopify":
            shop_domain = merchant.get("mcp_shop_domain")
            access_token = merchant.get("mcp_access_token")
            
            if not shop_domain or not access_token:
                raise HTTPException(
                    status_code=400,
                    detail="Shopify credentials not found. Please reconnect Shopify."
                )
            
            credentials = {
                "shop_domain": shop_domain,
                "access_token": access_token
            }
        
        elif platform == "wix":
            raise HTTPException(status_code=501, detail="Wix sync not yet implemented")
        
        elif platform == "woocommerce":
            raise HTTPException(status_code=501, detail="WooCommerce sync not yet implemented")
        
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
        
        # 4. Fetch products from platform
        products_obj, next_page_token, error = await fetch_merchant_products(
            merchant_id=request.merchant_id,
            platform=platform,
            credentials=credentials,
            limit=request.limit
        )
        
        if error:
            raise HTTPException(status_code=500, detail=f"Failed to fetch products: {error}")
        
        if not products_obj:
            return SyncResponse(
                status="success",
                message="No products found on platform",
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        # 5. Upsert products into cache
        synced_count = 0
        for product in products_obj:
            try:
                # Convert StandardProduct to dict using json serialization
                product_data = json.loads(product.json())
                
                await upsert_product_cache(
                    merchant_id=request.merchant_id,
                    platform=platform,
                    platform_product_id=product.id,
                    product_data=product_data,
                    ttl_seconds=86400  # 24 hours cache
                )
                synced_count += 1
            except Exception as e:
                logger.error(f"Failed to cache product {product.id}: {e}")
                continue
        
        # 6. Update merchant sync status
        await database.execute(
            """UPDATE merchant_onboarding 
               SET updated_at = :updated_at
               WHERE merchant_id = :merchant_id""",
            {
                "updated_at": datetime.now(),
                "merchant_id": request.merchant_id
            }
        )
        
        sync_duration = (datetime.now() - start_time).total_seconds()
        
        logger.info(
            f"✅ Product sync completed for merchant {request.merchant_id}: "
            f"{synced_count} products synced in {sync_duration:.2f}s"
        )
        
        return SyncResponse(
            status="success",
            message=f"Successfully synced {synced_count} products from {platform}",
            merchant_id=request.merchant_id,
            platform=platform,
            products_synced=synced_count,
            sync_time=datetime.now().isoformat()
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Product sync error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Product sync failed: {str(e)}"
        )

@router.get("/status/{merchant_id}")
async def get_sync_status(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get product sync status for a merchant
    
    Returns:
        Last sync time, product count, platform info
    """
    try:
        # Get merchant info
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Get product count from cache
        count_result = await database.fetch_one(
            """SELECT COUNT(*) as count, MAX(cached_at) as last_sync
               FROM products_cache
               WHERE merchant_id = :merchant_id
               AND cache_status != 'expired'""",
            {"merchant_id": merchant_id}
        )
        
        return {
            "merchant_id": merchant_id,
            "platform": merchant.get("mcp_platform"),
            "platform_connected": merchant.get("mcp_connected", False),
            "products_in_cache": count_result["count"] if count_result else 0,
            "last_sync": count_result["last_sync"].isoformat() if count_result and count_result["last_sync"] else None,
            "merchant_updated_at": merchant.get("updated_at").isoformat() if merchant.get("updated_at") else None
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get sync status error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get sync status: {str(e)}")

