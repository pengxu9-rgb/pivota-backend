"""Universal Product Sync - Works for all platforms"""
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, Dict, Any
from pydantic import BaseModel
from utils.auth import get_current_user
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from datetime import datetime
import httpx
import json
import logging
from adapters.product_adapters import fetch_merchant_products
from routes.product_routes import upsert_product_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["product-sync-v2"])

class UniversalSyncRequest(BaseModel):
    merchant_id: str
    force_refresh: bool = False
    limit: int = 50

class UniversalSyncResponse(BaseModel):
    status: str
    message: str
    merchant_id: str
    platform: str
    products_synced: int
    sync_time: str
    demo_mode: bool = False

@router.post("/sync-universal/", response_model=UniversalSyncResponse)
async def universal_product_sync(
    request: UniversalSyncRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Universal product sync endpoint that intelligently handles all platforms.
    
    Features:
    - Auto-detects platform from connected stores
    - Graceful fallbacks for missing credentials
    - Demo mode for testing
    - No 500 errors - always returns meaningful responses
    """
    start_time = datetime.now()
    
    try:
        logger.info(f"🔄 Universal sync request: merchant_id={request.merchant_id}")
        
        # 1. Get merchant info
        merchant = await get_merchant_onboarding(request.merchant_id)
        if not merchant:
            logger.warning(f"Merchant {request.merchant_id} not found")
            return UniversalSyncResponse(
                status="error",
                message="Merchant account not found",
                merchant_id=request.merchant_id,
                platform="unknown",
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        # 2. Find connected store (check all possible sources)
        store_info = await find_connected_store(request.merchant_id, merchant)
        
        if not store_info:
            logger.info(f"No store connected for merchant {request.merchant_id}")
            return UniversalSyncResponse(
                status="success",
                message="No store connected. Please connect your store in Integrations.",
                merchant_id=request.merchant_id,
                platform="none",
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        platform = store_info["platform"]
        logger.info(f"Found {platform} store for merchant {request.merchant_id}")
        
        # 3. Check credentials and prepare for sync
        credentials = prepare_platform_credentials(platform, store_info)
        
        if not credentials:
            logger.warning(f"Incomplete credentials for {platform}")
            # Return clear status without creating fake products
            return UniversalSyncResponse(
                status="warning",
                message=f"{platform.title()} API credentials are missing or incomplete. Please reconnect your {platform.title()} store in the Integrations page.",
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat(),
                demo_mode=False
            )
        
        # 4. Fetch products using the universal adapter
        products_obj, next_page_token, error = await fetch_merchant_products(
            merchant_id=request.merchant_id,
            platform=platform,
            credentials=credentials,
            limit=request.limit
        )
        
        if error:
            logger.error(f"Platform API error: {error}")
            # Still return success with helpful message
            return UniversalSyncResponse(
                status="warning",
                message=f"Could not fetch from {platform}: {error}. Please verify your store connection.",
                merchant_id=request.merchant_id,
                platform=platform,
                products_synced=0,
                sync_time=datetime.now().isoformat()
            )
        
        # 5. Cache products
        synced_count = 0
        if products_obj:
            for product in products_obj:
                try:
                    product_data = json.loads(product.json())
                    await upsert_product_cache(
                        merchant_id=request.merchant_id,
                        platform=platform,
                        platform_product_id=product.id,
                        product_data=product_data,
                        ttl_seconds=604800  # 7 days
                    )
                    synced_count += 1
                except Exception as e:
                    logger.error(f"Failed to cache product {product.id}: {e}")
        
        # 6. Update store sync status
        await update_sync_status(store_info.get("store_id"), synced_count)
        
        return UniversalSyncResponse(
            status="success",
            message=f"Successfully synced {synced_count} products from {platform.title()}",
            merchant_id=request.merchant_id,
            platform=platform,
            products_synced=synced_count,
            sync_time=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"Unexpected error in universal sync: {e}")
        # Never return 500 - always give meaningful response
        return UniversalSyncResponse(
            status="error",
            message=f"Sync service temporarily unavailable: {str(e)}",
            merchant_id=request.merchant_id,
            platform="unknown",
            products_synced=0,
            sync_time=datetime.now().isoformat()
        )


async def find_connected_store(merchant_id: str, merchant: Dict) -> Optional[Dict[str, Any]]:
    """Find connected store from any source"""
    
    # Check merchant_stores table first (preferred)
    store_query = """
        SELECT store_id, platform, name, domain, api_key, status
        FROM merchant_stores 
        WHERE merchant_id = :merchant_id 
        AND status IN ('active', 'connected')
        ORDER BY connected_at DESC
        LIMIT 1
    """
    store = await database.fetch_one(store_query, {"merchant_id": merchant_id})
    
    if store:
        return dict(store)
    
    # Fallback to merchant_onboarding for legacy MCP
    if merchant and merchant.get("mcp_platform"):
        return {
            "platform": merchant.get("mcp_platform"),
            "domain": merchant.get("mcp_shop_domain"),
            "api_key": merchant.get("mcp_access_token"),
            "store_id": f"legacy_{merchant_id}",
            "name": merchant.get("business_name")
        }
    
    return None


def prepare_platform_credentials(platform: str, store_info: Dict) -> Optional[Dict[str, str]]:
    """Prepare credentials based on platform requirements"""
    
    if platform == "shopify":
        # For Shopify, domain is the shop domain and api_key is the access token
        domain = store_info.get("domain")
        token = store_info.get("api_key")
        
        if domain and token:
            return {
                "shop_domain": domain,
                "access_token": token
            }
    
    elif platform == "wix":
        # For Wix, domain is the site_id and api_key is the API key
        site_id = store_info.get("domain")
        api_key = store_info.get("api_key")
        
        if site_id and api_key:
            return {
                "site_id": site_id,
                "api_key": api_key
            }
    
    elif platform == "woocommerce":
        # WooCommerce needs both consumer key and secret
        store_url = store_info.get("domain")
        api_key_data = store_info.get("api_key")
        
        if store_url and api_key_data:
            # Check if api_key contains JSON with both keys
            try:
                if api_key_data.startswith("{"):
                    credentials = json.loads(api_key_data)
                    return {
                        "store_url": store_url,
                        "consumer_key": credentials.get("consumer_key", ""),
                        "consumer_secret": credentials.get("consumer_secret", "")
                    }
                else:
                    # Legacy format - just consumer key
                    return {
                        "store_url": store_url,
                        "consumer_key": api_key_data,
                        "consumer_secret": ""
                    }
            except json.JSONDecodeError:
                # Fallback to treating it as consumer key only
                return {
                    "store_url": store_url,
                    "consumer_key": api_key_data,
                    "consumer_secret": ""
                }
    
    elif platform == "square":
        # Square uses location ID and access token
        location_id = store_info.get("domain")
        api_key_data = store_info.get("api_key")
        
        if location_id and api_key_data:
            try:
                if api_key_data.startswith("{"):
                    credentials = json.loads(api_key_data)
                    return {
                        "location_id": location_id,
                        "access_token": credentials.get("access_token", ""),
                        "application_id": credentials.get("application_id", "")
                    }
                else:
                    return {
                        "location_id": location_id,
                        "access_token": api_key_data,
                        "application_id": ""
                    }
            except json.JSONDecodeError:
                return {
                    "location_id": location_id,
                    "access_token": api_key_data,
                    "application_id": ""
                }
    
    elif platform == "bigcommerce":
        # BigCommerce uses store hash and access token
        store_hash = store_info.get("domain")
        api_key_data = store_info.get("api_key")
        
        if store_hash and api_key_data:
            try:
                if api_key_data.startswith("{"):
                    credentials = json.loads(api_key_data)
                    return {
                        "store_hash": store_hash,
                        "access_token": credentials.get("access_token", ""),
                        "client_id": credentials.get("client_id", "")
                    }
                else:
                    return {
                        "store_hash": store_hash,
                        "access_token": api_key_data,
                        "client_id": ""
                    }
            except json.JSONDecodeError:
                return {
                    "store_hash": store_hash,
                    "access_token": api_key_data,
                    "client_id": ""
                }
    
    # Add more platforms as needed
    logger.warning(f"Platform {platform} not yet implemented in credential preparation")
    return None




async def update_sync_status(store_id: Optional[str], product_count: int):
    """Update store sync status"""
    if store_id and not store_id.startswith("legacy_"):
        try:
            await database.execute(
                """UPDATE merchant_stores 
                   SET last_sync = :last_sync, product_count = :count
                   WHERE store_id = :store_id""",
                {
                    "last_sync": datetime.now(),
                    "count": product_count,
                    "store_id": store_id
                }
            )
        except Exception as e:
            logger.error(f"Failed to update sync status: {e}")
