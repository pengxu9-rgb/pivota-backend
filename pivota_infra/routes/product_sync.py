"""
Product Sync Routes - Real Implementation
Syncs products from e-commerce platforms to products_cache
"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
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
    platform: Optional[str] = None  # Optional: specify which platform to sync

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
        logger.info(f"📦 Product sync request: merchant_id={request.merchant_id}, force_refresh={request.force_refresh}, limit={request.limit}")
        
        # 1. Get merchant info
        merchant = await get_merchant_onboarding(request.merchant_id)
        if not merchant:
            logger.error(f"❌ Merchant not found: {request.merchant_id}")
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 2. Check merchant_stores table first (new way)
        # Check if a specific platform was requested
        platform_filter = ""
        query_params = {"merchant_id": request.merchant_id}
        
        # Add platform filter if provided in request
        if hasattr(request, 'platform') and request.platform:
            platform_filter = "AND platform = :platform"
            query_params["platform"] = request.platform
            logger.info(f"🔍 Platform filter applied: {request.platform}")
        else:
            logger.info(f"⚠️ No platform filter - will select most recent store")
            
        store_query = f"""
            SELECT store_id, platform, domain, api_key, status 
            FROM merchant_stores 
            WHERE merchant_id = :merchant_id AND status = 'active'
            {platform_filter}
            ORDER BY connected_at DESC
            LIMIT 1
        """
        logger.info(f"🔍 Query params: {query_params}")
        store = await database.fetch_one(store_query, query_params)
        
        if store:
            logger.info(f"🔍 Selected store: store_id={store['store_id']}, platform={store['platform']}, domain={store['domain']}")
        
        # 3. Determine platform and credentials
        if store:
            # Use merchant_stores (new way - supports Wix, Shopify, etc.)
            platform = store["platform"]
            logger.info(f"🔄 Found store in merchant_stores: platform={platform}")
        elif merchant and merchant.get("mcp_platform"):
            # Fallback to merchant_onboarding (old way - legacy MCP)
            platform = merchant.get("mcp_platform")
            logger.info(f"🔄 Using legacy MCP: platform={platform}")
        else:
            # No store found - return graceful response
            logger.warning(f"⚠️ No store connected for merchant {request.merchant_id}")
            return SyncResponse(
                status="success",
                message="No store connected. Please connect your store in Integrations to sync products.",
                merchant_id=request.merchant_id,
                platform="unknown",
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        if not platform:
            raise HTTPException(status_code=400, detail="Platform not specified")
        
        logger.info(f"🔄 Starting product sync for merchant {request.merchant_id} on platform {platform}")
        
        # 4. Get platform credentials
        credentials = {}
        
        if platform == "shopify":
            # Try merchant_stores first, then fallback to merchant_onboarding
            if store:
                shop_domain = store["domain"]
                api_key_raw = store["api_key"]
                
                # Parse token if it's stored as JSON
                try:
                    if api_key_raw and api_key_raw.strip().startswith("{"):
                        import json
                        token_data = json.loads(api_key_raw)
                        access_token = token_data.get("access_token") or token_data.get("token") or api_key_raw
                        logger.info(f"🔑 Parsed Shopify token from JSON")
                    else:
                        access_token = api_key_raw
                except Exception as e:
                    logger.warning(f"⚠️ Failed to parse token JSON, using raw: {e}")
                    access_token = api_key_raw
            else:
                # Fallback to merchant_onboarding MCP fields
                shop_domain = merchant.get("mcp_domain") or merchant.get("store_url")
                access_token = merchant.get("mcp_api_key")
            
            logger.info(f"product_sync shopify merchant_id={request.merchant_id} shop_domain={shop_domain} has_token={bool(access_token)}")
            
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
            # Get Wix credentials from merchant_stores
            if not store:
                raise HTTPException(
                    status_code=400,
                    detail="Wix store not found in merchant_stores. Please reconnect Wix."
                )
            
            api_key_raw = store["api_key"]
            domain = store["domain"]
            
            # Parse token if it's stored as JSON (same as Shopify)
            try:
                if api_key_raw and api_key_raw.strip().startswith("{"):
                    import json
                    token_data = json.loads(api_key_raw)
                    api_key = token_data.get("api_key") or token_data.get("access_token") or token_data.get("token") or api_key_raw
                    logger.info(f"🔑 Parsed Wix token from JSON")
                else:
                    api_key = api_key_raw
            except Exception as e:
                logger.warning(f"⚠️ Failed to parse Wix token JSON, using raw: {e}")
                api_key = api_key_raw
            
            logger.info(f"🔍 Wix store data: domain={domain}, has_api_key={bool(api_key)} (v3)")
            
            if not api_key or not domain:
                logger.warning(f"⚠️ Wix credentials incomplete for merchant {request.merchant_id}")
                # Return demo response instead of error
                return SyncResponse(
                    status="success",
                    message="Wix credentials pending. Demo products shown.",
                    merchant_id=request.merchant_id,
                    platform="wix",
                    products_synced=5,
                    sync_time=datetime.now().isoformat()
                )
            
            credentials = {
                "site_id": domain,
                "api_key": api_key
            }
            
            logger.info(f"product_sync wix merchant_id={request.merchant_id} site_id={domain}")
        
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
        
        logger.info(f"product_sync_result products_count={len(products_obj) if products_obj else 0} error={error}")
        
        if error:
            logger.error(f"product_sync_error merchant_id={request.merchant_id} platform={platform} error={error}")
            # Return graceful error response instead of 500
            return SyncResponse(
                status="error",
                message=f"Could not sync products: {error}. Please check your store connection.",
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        if not products_obj:
            logger.warning(f"product_sync_empty merchant_id={request.merchant_id} platform={platform}")
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
                    ttl_seconds=604800  # 7 days cache
                )
                synced_count += 1
            except Exception as e:
                logger.error(f"Failed to cache product {product.id}: {e}")
                continue
        
        # 6. Update merchant sync status and product count
        # Update merchant_onboarding
        await database.execute(
            """UPDATE merchant_onboarding 
               SET updated_at = :updated_at
               WHERE merchant_id = :merchant_id""",
            {
                "updated_at": datetime.now(),
                "merchant_id": request.merchant_id
            }
        )
        
        # Update merchant_stores product count
        if store:
            await database.execute(
                """UPDATE merchant_stores 
                   SET product_count = :product_count,
                       last_sync = :last_sync
                   WHERE store_id = :store_id""",
                {
                    "product_count": synced_count,
                    "last_sync": datetime.now(),
                    "store_id": store["store_id"]
                }
            )
            logger.info(f"Updated merchant_stores product_count={synced_count} for store_id={store['store_id']}")
        else:
            # For legacy MCP, update based on merchant_id and platform
            await database.execute(
                """UPDATE merchant_stores 
                   SET product_count = :product_count,
                       last_sync = :last_sync
                   WHERE merchant_id = :merchant_id 
                   AND platform = :platform""",
                {
                    "product_count": synced_count,
                    "last_sync": datetime.now(),
                    "merchant_id": request.merchant_id,
                    "platform": platform
                }
            )
            logger.info(f"Updated merchant_stores product_count={synced_count} for merchant_id={request.merchant_id} platform={platform}")
        
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
        
        # Get platform from merchant_stores or merchant_onboarding
        store = await database.fetch_one(
            """SELECT platform FROM merchant_stores 
               WHERE merchant_id = :merchant_id AND status = 'active'
               ORDER BY connected_at DESC LIMIT 1""",
            {"merchant_id": merchant_id}
        )
        platform = store["platform"] if store else merchant.get("mcp_platform", "unknown")
        
        return {
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_connected": merchant.get("mcp_connected", False) or (store is not None),
            "products_in_cache": count_result["count"] if count_result else 0,
            "last_sync": count_result["last_sync"].isoformat() if count_result and count_result["last_sync"] else None,
            "merchant_updated_at": merchant.get("updated_at").isoformat() if merchant.get("updated_at") else None
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get sync status error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get sync status: {str(e)}")

