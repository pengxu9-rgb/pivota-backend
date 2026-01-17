"""
Unified Merchant Store Service
Provides consistent access to merchant store data across old and new systems
"""

from typing import List, Dict, Any, Optional
import logging
import time
import json

from db.database import database
from db.merchant_onboarding import get_merchant_onboarding

logger = logging.getLogger(__name__)


def parse_api_key(api_key: str) -> str:
    """
    解析 api_key，支持两种格式：
    1. 纯字符串: "shpat_xxx"
    2. JSON 格式: {"access_token":"shpat_xxx"}
    
    返回实际的 token
    """
    if not api_key:
        return ""
    
    # 如果是 JSON 格式（以 { 开头）
    if api_key.strip().startswith("{"):
        try:
            parsed = json.loads(api_key)
            # 尝试提取 access_token
            if isinstance(parsed, dict) and "access_token" in parsed:
                token = parsed["access_token"]
                # Do not log secrets (tokens).
                logger.info("Parsed JSON format token from merchant_stores.api_key")
                return token
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON token, using as-is: {e}")
    
    # 否则直接返回（纯字符串格式）
    return api_key


def parse_api_credentials(api_key: str) -> Dict[str, Any]:
    """
    Best-effort parse of merchant_stores.api_key when it contains JSON credentials.

    Today we store Shopify tokens as JSON strings, e.g:
      {"access_token":"shpat_xxx"}

    We may also store additional tokens (e.g. Storefront) in the same JSON blob:
      {"access_token":"shpat_xxx","storefront_access_token":"xxx"}

    Returns {} when not JSON.
    """
    if not api_key:
        return {}
    raw = str(api_key).strip()
    if not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


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
                support_email,
                status,
                connected_at,
                'merchant_stores' as source
            FROM merchant_stores
            WHERE merchant_id = :merchant_id 
            AND status IN ('active', 'connected')
            ORDER BY connected_at DESC
        """
        
        try:
            new_stores = await database.fetch_all(store_query, {"merchant_id": merchant_id})
        except Exception as e:
            # Backward compatibility: support_email column might not exist yet.
            logger.error(f"Error fetching from merchant_stores (support_email): {e}")
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
        
        # 解析 api_key 格式（支持 JSON 和纯字符串）
        for store in new_stores:
            store_dict = dict(store)
            original_key = store_dict.get("api_key", "")
            store_dict["api_key_raw"] = original_key
            store_dict["api_credentials"] = parse_api_credentials(original_key)
            store_dict["api_key"] = parse_api_key(original_key)
            stores.append(store_dict)
        
    except Exception as e:
        logger.error(f"Error fetching from merchant_stores: {e}")
    
    # 2. If no stores found, check legacy system (merchant_onboarding)
    if not stores:
        try:
            merchant = await get_merchant_onboarding(merchant_id)
            if merchant and merchant.get("mcp_platform"):
                # Legacy 系统也可能有 JSON 格式的 token，同样解析
                raw_token = merchant.get("mcp_access_token", "")
                parsed_token = parse_api_key(raw_token)
                
                legacy_store = {
                    "store_id": f"legacy_{merchant_id}",
                    "merchant_id": merchant_id,
                    "platform": merchant.get("mcp_platform"),
                    "name": merchant.get("business_name", "Legacy Store"),
                    "domain": merchant.get("mcp_shop_domain", ""),
                    "api_key_raw": raw_token,
                    "api_credentials": parse_api_credentials(raw_token),
                    "api_key": parsed_token,
                    "status": "active" if merchant.get("mcp_connected", False) else "disconnected",
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
