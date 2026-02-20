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
import os
import time
import json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from fastapi import BackgroundTasks

from db.database import database
from db.products import get_cached_products, upsert_product_cache
from models.standard_product import StandardProduct, ProductListResponse
from adapters.merchant_api_adapter import MerchantAPIAdapter

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = int(str(raw).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, parsed))


STALE_CACHE_FALLBACK_ENABLED = _env_bool(
    "AGENT_SHOP_ALLOW_STALE_CACHE_FALLBACK",
    True,
)
STALE_CACHE_MAX_AGE_HOURS = _env_int(
    "AGENT_SHOP_STALE_CACHE_MAX_AGE_HOURS",
    72,
    min_value=1,
    max_value=24 * 30,
)

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

        data = dict(result)
        return RealtimeConfig(
            realtime_enabled=bool(data.get("realtime_enabled") or False),
            api_endpoint=data.get("api_endpoint"),
            api_key=data.get("api_key"),
            ttl_seconds=int(data.get("query_ttl_seconds") or 600),
            platform=str(data.get("platform") or "unknown"),
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
    allow_stale_cache: bool = STALE_CACHE_FALLBACK_ENABLED,
) -> Tuple[List[StandardProduct], str, Optional[str]]:
    """
    Hybrid product query - decides between cache and realtime
    
    Args:
        merchant_id: Merchant to query
        limit: Max products to return
        agent_id: Agent making the request (for logging)
        background_tasks: FastAPI background tasks (for async cache refresh)
        allow_stale_cache: Whether expired cache rows may be used as fallback
    
    Returns:
        Tuple of (products list, query source, error message)
        query source: "cache", "realtime", "realtime_with_cache_fallback"
    """
    start_time = time.time()
    query_source = "cache"  # Default

    stale_cutoff = None
    if allow_stale_cache:
        stale_cutoff = datetime.utcnow() - timedelta(hours=STALE_CACHE_MAX_AGE_HOURS)
    
    try:
        # For special callers (e.g. creator surfaces) we may want to bypass
        # realtime entirely and rely on the cache catalog breadth. In this
        # mode we ignore platform scoping because production cache rows
        # can have platform mismatches between the column and product_data.
        if force_cache_only:
            products = await _get_from_cache_all_platforms(
                merchant_id,
                limit,
                include_expired=False,
            )
            if products:
                return products, "cache_all_platforms", None
            if allow_stale_cache:
                stale_products = await _get_from_cache_all_platforms(
                    merchant_id,
                    limit,
                    include_expired=True,
                    stale_cutoff=stale_cutoff,
                )
                if stale_products:
                    return stale_products, "cache_all_platforms_stale", None
            return products, "cache_all_platforms", None

        # Step 1: Get merchant configuration
        config = await get_merchant_realtime_config(merchant_id)

        if not config:
            logger.warning(f"No config found for merchant {merchant_id}, using cache for all platforms")
            products = await _get_from_cache_all_platforms(
                merchant_id,
                limit,
                include_expired=False,
            )
            if products:
                return products, "cache", None
            if allow_stale_cache:
                stale_products = await _get_from_cache_all_platforms(
                    merchant_id,
                    limit,
                    include_expired=True,
                    stale_cutoff=stale_cutoff,
                )
                if stale_products:
                    return stale_products, "cache_stale_no_config", None
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
                    if not products and allow_stale_cache:
                        stale_products = await _get_from_cache(
                            merchant_id,
                            config.platform,
                            limit,
                            include_expired=True,
                        )
                        if stale_products:
                            products = stale_products
                            query_source = "realtime_with_stale_cache_fallback"
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
                if not products and allow_stale_cache:
                    stale_products = await _get_from_cache(
                        merchant_id,
                        config.platform,
                        limit,
                        include_expired=True,
                    )
                    if stale_products:
                        products = stale_products
                        query_source = "realtime_with_stale_cache_fallback"
                return products, query_source, str(e)
        
        else:
            # Cache path (default)
            logger.info(f"[CACHE] Using cached products for {merchant_id}")
            products = await _get_from_cache(merchant_id, config.platform, limit)
            cache_source = "cache"
            if not products and allow_stale_cache:
                stale_products = await _get_from_cache(
                    merchant_id,
                    config.platform,
                    limit,
                    include_expired=True,
                )
                if stale_products:
                    products = stale_products
                    cache_source = "cache_stale"
            
            latency_ms = int((time.time() - start_time) * 1000)
            logger.info(f"[CACHE] Returned {len(products)} products in {latency_ms}ms")
            
            return products, cache_source, None
            
    except Exception as e:
        logger.error(f"Hybrid query failed: {e}")
        # Final fallback
        try:
            products = await _get_from_cache(merchant_id, "unknown", limit)
            source = "cache_fallback"
            if not products and allow_stale_cache:
                stale_products = await _get_from_cache(
                    merchant_id,
                    "unknown",
                    limit,
                    include_expired=True,
                )
                if stale_products:
                    products = stale_products
                    source = "cache_stale_fallback"
            return products, source, str(e)
        except:
            return [], "error", str(e)


async def _get_from_cache(
    merchant_id: str, 
    platform: str, 
    limit: int,
    include_expired: bool = False,
) -> List[StandardProduct]:
    """Get products from local cache for specific platform"""
    try:
        # Use existing cache function
        cached = await get_cached_products(
            merchant_id,
            platform,
            include_expired=include_expired,
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
    limit: int,
    include_expired: bool = False,
    stale_cutoff: Optional[datetime] = None,
) -> List[StandardProduct]:
    """Get products from cache across all platforms (when config not found)"""
    try:
        # Query cache without platform filter
        where_clauses = ["merchant_id = :merchant_id"]
        params: Dict[str, Any] = {"merchant_id": merchant_id, "cache_limit": limit}

        if not include_expired:
            where_clauses.append("(expires_at IS NULL OR expires_at > NOW())")
        elif stale_cutoff is not None:
            where_clauses.append("(cached_at IS NULL OR cached_at >= :stale_cutoff)")
            params["stale_cutoff"] = stale_cutoff

        query = f"""
            SELECT product_data
            FROM products_cache
            WHERE {" AND ".join(where_clauses)}
            ORDER BY cached_at DESC
            LIMIT :cache_limit
        """

        rows = await database.fetch_all(query, params)
        
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
