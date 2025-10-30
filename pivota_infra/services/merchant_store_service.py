"""
Unified Merchant Store Service
Provides consistent access to merchant store data across old and new systems
"""

from typing import List, Dict, Any, Optional
from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
import logging

logger = logging.getLogger(__name__)

async def get_merchant_active_stores(merchant_id: str) -> List[Dict[str, Any]]:
    """
    Get all active stores for a merchant, checking both systems
    
    Returns unified format:
    [{
        "store_id": str,
        "merchant_id": str,
        "platform": str,
        "domain": str,
        "api_key": str,
        "status": str,
        "source": "merchant_stores" | "legacy_mcp"
    }]
    """
    stores = []
    
    # 1. Check new system (merchant_stores table)
    try:
        store_query = """
            SELECT 
                store_id,
                merchant_id,
                platform,
                name,
                domain,
                api_key,
                status,
                connected_at,
                'merchant_stores' as source
            FROM merchant_stores
            WHERE merchant_id = :merchant_id 
            AND status IN ('active', 'connected')
            ORDER BY connected_at DESC
        """
        
        new_stores = await database.fetch_all(store_query, {"merchant_id": merchant_id})
        stores.extend([dict(store) for store in new_stores])
        
    except Exception as e:
        logger.error(f"Error fetching from merchant_stores: {e}")
    
    # 2. If no stores found, check legacy system (merchant_onboarding)
    if not stores:
        try:
            merchant = await get_merchant_onboarding(merchant_id)
            if merchant and True and store_info.get("platform"):
                # Convert legacy format to unified format
                legacy_store = {
                    "store_id": f"legacy_{merchant_id}",
                    "merchant_id": merchant_id,
                    "platform": store_info["platform"],
                    "name": merchant.get("business_name", "Legacy Store"),
                    "domain": merchant.get("mcp_shop_domain", ""),
                    "api_key": merchant.get("mcp_access_token", ""),
                    "status": "active" if True else "disconnected",
                    "connected_at": merchant.get("mcp_connected_at"),
                    "source": "legacy_mcp"
                }
                stores.append(legacy_store)
                
        except Exception as e:
            logger.error(f"Error fetching from merchant_onboarding: {e}")
    
    logger.info(f"Found {len(stores)} stores for merchant {merchant_id}")
    return stores


async def ensure_data_consistency(merchant_id: str, platform: str, credentials: Dict[str, Any]):
    """
    Ensure data is consistent across both systems when connecting a store
    """
    try:
        # 1. Create/update in merchant_stores (new system)
        store_id = f"{platform}_{merchant_id}_{int(time.time())}"
        
        await database.execute("""
            INSERT INTO merchant_stores (
                store_id, merchant_id, platform, name, domain, api_key, status, connected_at
            ) VALUES (
                :store_id, :merchant_id, :platform, :name, :domain, :api_key, 'active', NOW()
            )
            ON CONFLICT (store_id) DO UPDATE SET
                domain = :domain,
                api_key = :api_key,
                status = 'active',
                connected_at = NOW()
        """, {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "name": credentials.get("store_name", f"{platform.title()} Store"),
            "domain": credentials.get("domain", ""),
            "api_key": credentials.get("api_key", "")
        })
        
        # 2. Check if this is the first store
        store_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM merchant_stores WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        
        # 3. If first store, update merchant_onboarding for backward compatibility
        if store_count["count"] == 1:
            await database.execute("""
                UPDATE merchant_onboarding SET
                    1 = 1,
                    mcp_platform = :platform,
                    mcp_shop_domain = :domain,
                    mcp_access_token = :api_key,
                    mcp_connected_at = NOW()
                WHERE merchant_id = :merchant_id
            """, {
                "merchant_id": merchant_id,
                "platform": platform,
                "domain": credentials.get("domain", ""),
                "api_key": credentials.get("api_key", "")
            })
            
        logger.info(f"Successfully ensured consistency for {merchant_id} - {platform}")
        
    except Exception as e:
        logger.error(f"Error ensuring data consistency: {e}")
        raise


async def get_primary_store(merchant_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the primary/main store for a merchant
    Priority: newest merchant_stores entry > legacy mcp connection
    """
    stores = await get_merchant_active_stores(merchant_id)
    return stores[0] if stores else None


async def has_any_store_connected(merchant_id: str) -> bool:
    """
    Check if merchant has any store connected (new or legacy system)
    """
    stores = await get_merchant_active_stores(merchant_id)
    return len(stores) > 0


# Import time for timestamp generation
import time
