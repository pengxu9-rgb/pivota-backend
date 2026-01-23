"""
Product Query Service - Hybrid Architecture

Dataflow:
  Agent API Call
    ↓
  ProductQueryService (decision layer)
    ↓
  ├─ Cache Path: products_cache table (fast, <50ms)
  └─ Realtime Path: MerchantAPIAdapter → Merchant API (1s timeout)
       ↓ (async background)
       └─ Cache Refresh Worker

Performance targets:
- Cache hit: <100ms
- Realtime query: <1000ms (with 1s timeout)
- Background refresh: non-blocking

TODO (Future enhancements):
- Smart prefetch based on agent query patterns (AI-driven demand prediction)
  function predictProductFetchPatterns(agent_id, merchant_id): Promise<string[]>
- Per-product TTL based on inventory volatility
- Circuit breaker for failing merchant APIs
"""

import logging
import time
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from fastapi import BackgroundTasks

from db.database import database
from db.products import get_cached_products, upsert_product_cache
from models.standard_product import StandardProduct, ProductListResponse
from adapters.merchant_api_adapter import MerchantAPIAdapter

logger = logging.getLogger(__name__)

class RealtimeConfig:
    """Merchant realtime query configuration"""
    def __init__(
        self,
        realtime_enabled: bool,
        api_endpoint: Optional[str],
        api_key: Optional[str],
        ttl_seconds: int,
        platform: str
    ):
        self.realtime_enabled = realtime_enabled
        self.api_endpoint = api_endpoint
        self.api_key = api_key
        self.ttl_seconds = ttl_seconds
        self.platform = platform


async def get_merchant_realtime_config(merchant_id: str) -> Optional[RealtimeConfig]:
    """
    Get merchant's realtime query configuration
    
    Returns None if merchant not found or has no stores
    """
    try:
        query = """
            SELECT 
                realtime_enabled, 
                api_endpoint, 
                api_key, 
                query_ttl_seconds,
                platform
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
            AND status IN ('active', 'connected')
            ORDER BY connected_at DESC
            LIMIT 1
        """
        
        result = await database.fetch_one(query, {"merchant_id": merchant_id})
        
        if not result:
            return None
        
        return RealtimeConfig(
            realtime_enabled=result.get("realtime_enabled", False),
            api_endpoint=result.get("api_endpoint"),
            api_key=result.get("api_key"),
            ttl_seconds=result.get("query_ttl_seconds", 600),
            platform=result.get("platform", "unknown")
        )
        
    except Exception as e:
        logger.error(f"Failed to get realtime config for {merchant_id}: {e}")
        return None


async def get_products_hybrid(
    merchant_id: str,
    limit: int,
    agent_id: str,
    background_tasks: Optional[BackgroundTasks] = None,
    force_cache_only: bool = False,
) -> Tuple[List[StandardProduct], str, Optional[str]]:
    """
    Hybrid product query - decides between cache and realtime
    
    Args:
        merchant_id: Merchant to query
        limit: Max products to return
        agent_id: Agent making the request (for logging)
        background_tasks: FastAPI background tasks (for async cache refresh)
    
    Returns:
        Tuple of (products list, query source, error message)
        query source: "cache", "realtime", "realtime_with_cache_fallback"
    """
    start_time = time.time()
    query_source = "cache"  # Default
    
    try:
        # For special callers (e.g. creator surfaces) we may want to bypass
        # realtime entirely and rely on the cache catalog breadth. In this
        # mode we ignore platform scoping because production cache rows
        # can have platform mismatches between the column and product_data.
        if force_cache_only:
            products = await _get_from_cache_all_platforms(merchant_id, limit)
            return products, "cache_all_platforms", None

        # Step 1: Get merchant configuration
        config = await get_merchant_realtime_config(merchant_id)

        if not config:
            logger.warning(f"No config found for merchant {merchant_id}, using cache for all platforms")
            products = await _get_from_cache_all_platforms(merchant_id, limit)
            return products, "cache", None
        
        # Step 2: Decide query path
        if config.realtime_enabled and config.api_endpoint and not force_cache_only:
            # Realtime path
            logger.info(f"[REALTIME] Querying merchant API for {merchant_id}")
            query_source = "realtime"
            
            try:
                adapter = MerchantAPIAdapter(config.api_endpoint, {"api_key": config.api_key})
                products, error = await adapter.query_products(limit=limit)
                
                if error:
                    # Fallback to cache on error
                    logger.warning(f"Merchant API failed, falling back to cache: {error}")
                    query_source = "realtime_with_cache_fallback"
                    products = await _get_from_cache(merchant_id, config.platform, limit)
                else:
                    # Success - optionally refresh cache in background
                    if background_tasks and products:
                        background_tasks.add_task(
                            _refresh_cache_async,
                            merchant_id,
                            config.platform,
                            products,
                            config.ttl_seconds
                        )
                
                # Set merchant_id on products
                for p in products:
                    p.merchant_id = merchant_id
                
                latency_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[REALTIME] Returned {len(products)} products in {latency_ms}ms")
                
                return products, query_source, None
                
            except Exception as e:
                logger.error(f"Realtime query failed: {e}")
                query_source = "realtime_with_cache_fallback"
                products = await _get_from_cache(merchant_id, config.platform, limit)
                return products, query_source, str(e)
        
        else:
            # Cache path (default)
            logger.info(f"[CACHE] Using cached products for {merchant_id}")
            products = await _get_from_cache(merchant_id, config.platform, limit)
            
            latency_ms = int((time.time() - start_time) * 1000)
            logger.info(f"[CACHE] Returned {len(products)} products in {latency_ms}ms")
            
            return products, "cache", None
            
    except Exception as e:
        logger.error(f"Hybrid query failed: {e}")
        # Final fallback
        try:
            products = await _get_from_cache(merchant_id, "unknown", limit)
            return products, "cache_fallback", str(e)
        except:
            return [], "error", str(e)


async def _get_from_cache(
    merchant_id: str, 
    platform: str, 
    limit: int
) -> List[StandardProduct]:
    """Get products from local cache for specific platform"""
    try:
        # Use existing cache function
        cached = await get_cached_products(
            merchant_id,
            platform,
            include_expired=False,
            limit=limit,
            offset=0,
        )
        
        # Convert to StandardProduct objects
        products = []
        for c in cached:
            product_data = c["product_data"]
            if isinstance(product_data, str):
                product_data = json.loads(product_data)
            
            try:
                product = StandardProduct(**product_data)
                products.append(product)
            except Exception as e:
                logger.warning(f"Failed to parse cached product: {e}")
                continue
        
        return products
        
    except Exception as e:
        logger.error(f"Failed to get from cache: {e}")
        return []


async def _get_from_cache_all_platforms(
    merchant_id: str,
    limit: int
) -> List[StandardProduct]:
    """Get products from cache across all platforms (when config not found)"""
    try:
        # Query cache without platform filter
        query = f"""
            SELECT product_data
            FROM products_cache
            WHERE merchant_id = :merchant_id
            AND expires_at > NOW()
            ORDER BY cached_at DESC
            LIMIT {limit}
        """
        
        rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        
        products = []
        for row in rows:
            product_data = row["product_data"]
            if isinstance(product_data, str):
                product_data = json.loads(product_data)
            
            try:
                product = StandardProduct(**product_data)
                products.append(product)
            except Exception as e:
                logger.warning(f"Failed to parse cached product: {e}")
                continue
        
        logger.info(f"Loaded {len(products)} products from cache (all platforms)")
        return products
        
    except Exception as e:
        logger.error(f"Failed to get from cache (all platforms): {e}")
        return []


async def _refresh_cache_async(
    merchant_id: str,
    platform: str,
    fresh_products: List[StandardProduct],
    ttl_seconds: int
):
    """
    Background task: refresh cache with fresh data from realtime query
    Non-blocking, runs after response is sent to agent
    """
    try:
        logger.info(f"[CACHE_REFRESH] Refreshing cache for {merchant_id} with {len(fresh_products)} products")
        
        for product in fresh_products:
            try:
                await upsert_product_cache(
                    merchant_id=merchant_id,
                    platform=platform,
                    platform_product_id=product.id,
                    product_data=json.loads(product.json()),
                    ttl_seconds=ttl_seconds
                )
            except Exception as e:
                logger.error(f"Failed to cache product {product.id}: {e}")
        
        logger.info(f"[CACHE_REFRESH] Completed for {merchant_id}")
        
    except Exception as e:
        logger.error(f"Cache refresh failed: {e}")


def log_query_source(
    agent_id: str,
    merchant_id: str,
    endpoint: str,
    query_source: str,
    response_time_ms: int,
    product_count: int
):
    """
    Log product query with source tracking
    
    Args:
        agent_id: Agent making the query
        merchant_id: Target merchant
        endpoint: API endpoint called
        query_source: "cache" | "realtime" | "realtime_with_cache_fallback"
        response_time_ms: Total response time
        product_count: Number of products returned
    """
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "agent_id": agent_id,
        "merchant_id": merchant_id,
        "endpoint": endpoint,
        "query_source": query_source,  # NEW FIELD
        "response_time_ms": response_time_ms,
        "product_count": product_count,
        "cache_hit": query_source == "cache"
    }
    
    logger.info(f"[PRODUCT_QUERY] {json.dumps(log_entry)}")
    
    # TODO: Also write to structured logging table or metrics system
    # await log_to_database(log_entry)
