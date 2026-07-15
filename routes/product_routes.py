"""
Product Proxy API Routes
Pivota 核心价值：实时代理 + 智能缓存 + 数据标准化
防御性架构：Agent 只读，事件追踪，自动清理
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from typing import Optional, Dict, Any
from datetime import datetime
import os
import time
import json

from models.standard_product import ProductListResponse
from adapters.bigcommerce_adapter import normalize_bigcommerce_store_hash
from adapters.product_adapters import (
    BigCommerceProductAdapter,
    WooCommerceProductAdapter,
    fetch_merchant_products,
)
from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from db.merchant_onboarding import get_merchant_onboarding
from db.products import (
    get_cached_products, upsert_product_cache, mark_cache_accessed,
    log_api_call, cleanup_expired_cache
)
from utils.auth import require_admin, get_current_user
from config.settings import settings
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/products", tags=["products"])


def _parse_woocommerce_credentials(raw_value: Optional[str]) -> Dict[str, str]:
    raw = str(raw_value or "").strip()
    if not raw:
        return {"consumer_key": "", "consumer_secret": ""}
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {
                    "consumer_key": str(parsed.get("consumer_key") or "").strip(),
                    "consumer_secret": str(parsed.get("consumer_secret") or "").strip(),
                }
    except Exception:
        pass
    if ":" in raw:
        consumer_key, consumer_secret = raw.split(":", 1)
        return {
            "consumer_key": consumer_key.strip(),
            "consumer_secret": consumer_secret.strip(),
        }
    return {"consumer_key": raw, "consumer_secret": ""}


def _parse_bigcommerce_credentials(raw_value: Optional[str], domain: Optional[str]) -> Dict[str, str]:
    raw = str(raw_value or "").strip()
    credentials = {
        "store_hash": normalize_bigcommerce_store_hash(domain),
        "access_token": raw,
        "client_id": "",
    }
    if not raw:
        return credentials
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                credentials["store_hash"] = normalize_bigcommerce_store_hash(
                    parsed.get("store_hash") or domain
                )
                credentials["access_token"] = str(parsed.get("access_token") or "").strip()
                credentials["client_id"] = str(parsed.get("client_id") or "").strip()
    except Exception:
        pass
    return credentials


@router.get("/{merchant_id}", response_model=ProductListResponse)
async def get_merchant_products_realtime(
    merchant_id: str,
    background_tasks: BackgroundTasks,
    limit: int = Query(50, ge=1, le=250, description="返回产品数量"),
    force_refresh: bool = Query(False, description="强制刷新缓存"),
    current_user: dict = Depends(get_current_user)  # Allow all authenticated users
):
    """
    **实时获取商户产品（标准格式）+ 智能缓存**
    
    架构特点：
    - ✅ Read-Through Cache：优先读缓存，miss 时实时拉取
    - ✅ 防御性设计：Agent 只读，不影响核心数据
    - ✅ 事件追踪：记录所有 API 调用，用于分析
    - ✅ 自动过期：缓存 TTL 1小时，自动清理
    
    **Pivota 核心价值：数据标准化 + 性能优化 + 业务洞察**
    """
    start_time = time.time()
    cache_hit = False
    products = []
    
    
    # 直接从缓存获取，不再检查 mcp_connected
    try:
        # 获取商家的所有活跃商店
        stores = await get_merchant_active_stores(merchant_id)
        if not stores:
            raise HTTPException(
                status_code=404,
                detail=f"No active stores found for merchant {merchant_id}"
            )
        
        # 获取主商店
        primary_store = stores[0]
        platform = primary_store["platform"]
        store_info = primary_store
        
        # 从缓存获取所有平台的产品（不只是主商店）
        all_cached = []
        for store in stores:
            try:
                store_platform = store["platform"]
                platform_cached = await get_cached_products(merchant_id, store_platform, include_expired=False)
                all_cached.extend(platform_cached)
                logger.info(f"📦 Loaded {len(platform_cached)} products from {store_platform}")
            except Exception as store_error:
                logger.error(f"Error loading products from {store.get('platform', 'unknown')}: {store_error}")
                continue
        
        products = [c["product_data"] for c in all_cached[:limit]]
        logger.info(f"📦 Total loaded: {len(products)} products from {len(stores)} stores")
        
        # 如果有产品，直接返回（已经从多平台聚合）
        if products:
            cache_hit = True
            response_time_ms = int((time.time() - start_time) * 1000)
            
            # 从缓存返回的是字典，需要转换为 StandardProduct 对象
            from models.standard_product import StandardProduct
            product_objects = [StandardProduct(**p) for p in products]
            
            # 使用 "multi" 表示多平台
            platform_label = "multi" if len(stores) > 1 else platform
            
            return ProductListResponse(
                status="success",
                merchant_id=merchant_id,
                platform=platform_label,
                total=len(products),
                products=product_objects,
                next_page_token=None,
                fetched_at=datetime.now()
            )
        else:
            # 没有产品，返回空列表
            return ProductListResponse(
                status="success",
                merchant_id=merchant_id,
                platform="multi" if len(stores) > 1 else platform,
                total=0,
                products=[],
                next_page_token=None,
                fetched_at=datetime.now()
            )
    
    except Exception as e:
        # Handle errors
        logger.error(f"Error in product query: {str(e)}")
        return ProductListResponse(
            status="success",
            merchant_id=merchant_id,
            platform="unknown",
            total=0,
            products=[],
            next_page_token=None,
            fetched_at=datetime.now()
        )
    
    # 4. Cache miss → 实时拉取（这段代码不应该被执行到了）
    credentials = {}
    
    if platform == "shopify":
        # 安全地访问 store_info，避免未定义错误
        shop_domain = store_info.get("domain") if 'store_info' in locals() and store_info else None
        access_token = None
        if shop_domain and 'store_info' in locals() and store_info:
            access_token, _ = await resolve_shopify_admin_access_token(
                shop_domain=shop_domain,
                api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
                store_id=str(store_info.get("store_id") or "").strip() or None,
            )
        
        # 如果从 store_info 获取失败，尝试环境变量或设置
        shop_domain = shop_domain or os.getenv("SHOPIFY_SHOP_DOMAIN") or getattr(settings, "shopify_shop_domain", None)
        access_token = access_token or os.getenv("SHOPIFY_ACCESS_TOKEN") or getattr(settings, "shopify_access_token", None)
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Shopify credentials not found.")
        
        credentials = {"shop_domain": shop_domain, "access_token": access_token}
    
    elif platform == "wix":
        # Wix credentials - 安全地访问 store_info
        site_id = store_info.get("domain") if 'store_info' in locals() and store_info else None
        api_key = store_info.get("api_key") if 'store_info' in locals() and store_info else None
        
        # 如果从 store_info 获取失败，尝试环境变量
        site_id = site_id or os.getenv("WIX_SITE_ID")
        api_key = api_key or os.getenv("WIX_API_KEY")
        
        if not site_id or not api_key:
            raise HTTPException(status_code=400, detail="Wix credentials not found.")
        
        credentials = {"site_id": site_id, "api_key": api_key}
    
    elif platform == "woocommerce":
        raw_credentials = _parse_woocommerce_credentials(
            (store_info or {}).get("api_key_raw") or (store_info or {}).get("api_key")
        )
        store_url = normalize_woocommerce_store_url((store_info or {}).get("domain"))
        if not store_url or not raw_credentials["consumer_key"] or not raw_credentials["consumer_secret"]:
            raise HTTPException(status_code=400, detail="WooCommerce credentials not found.")
        credentials = {
            "store_url": store_url,
            "consumer_key": raw_credentials["consumer_key"],
            "consumer_secret": raw_credentials["consumer_secret"],
        }

    elif platform == "bigcommerce":
        raw_credentials = _parse_bigcommerce_credentials(
            (store_info or {}).get("api_key_raw") or (store_info or {}).get("api_key"),
            (store_info or {}).get("domain"),
        )
        if not raw_credentials["store_hash"] or not raw_credentials["access_token"]:
            raise HTTPException(status_code=400, detail="BigCommerce credentials not found.")
        credentials = raw_credentials
    
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")
    
    # 5. 实时拉取产品
    products_obj, next_page_token, error = await fetch_merchant_products(
        merchant_id=merchant_id,
        platform=platform,
        credentials=credentials,
        limit=limit
    )
    
    if error:
        # 记录失败事件
        response_time_ms = int((time.time() - start_time) * 1000)
        await log_api_call(
            event_type="product_query",
            merchant_id=merchant_id,
            endpoint=f"/products/v2/{merchant_id}",
            request_params={"limit": limit, "force_refresh": force_refresh},
            response_status=500,
            cache_hit=False,
            response_time_ms=response_time_ms
        )
        raise HTTPException(status_code=500, detail=f"Failed to fetch products: {error}")
    
    # 6. 更新缓存（后台任务，不阻塞响应）
    for p in products_obj:
        # 使用 p.json() + json.loads() 确保 datetime 被序列化为 ISO 字符串
        await upsert_product_cache(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=p.id,
            product_data=json.loads(p.json()),
            ttl_seconds=3600  # 1小时
        )
    
    # 7. 记录 API 调用事件（缓存未命中）
    response_time_ms = int((time.time() - start_time) * 1000)
    await log_api_call(
        event_type="product_query",
        merchant_id=merchant_id,
        endpoint=f"/products/v2/{merchant_id}",
        request_params={"limit": limit, "force_refresh": force_refresh},
        response_status=200,
        cache_hit=False,
        response_time_ms=response_time_ms,
        product_ids=[p.id for p in products_obj]
    )
    
    # 8. 返回标准化格式
    return ProductListResponse(
        status="success",
        merchant_id=merchant_id,
        platform=platform,
        total=len(products_obj),
        products=products_obj,
        next_page_token=next_page_token,
        fetched_at=datetime.now()
    )


@router.get("/{merchant_id}/{product_id}")
async def get_single_product_realtime(
    merchant_id: str,
    product_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    实时获取单个产品详情（标准格式）
    
    TODO: 实现单个产品的精确查询
    目前通过列表过滤实现
    """
    # 简化实现：拉取列表并过滤
    # 生产环境应该直接调用平台的单个产品 API
    
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    store_info = await get_primary_store(merchant_id)
    if not store_info:
        raise HTTPException(
            status_code=404, 
            detail="No connected stores found for merchant",
            headers={"X-Error-Code": "STORE_NOT_FOUND"}
        )

    platform = store_info.get("platform")
    
    if platform == "shopify":
        shop_domain = store_info.get("domain") or store_info.get("shop_domain")
        if not shop_domain:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing domain",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"}
            )
        
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key") or store_info.get("access_token"),
            store_id=str(store_info.get("store_id") or "").strip() or None,
        )
        access_token = access_token or os.getenv("SHOPIFY_ACCESS_TOKEN")
        if not access_token:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing credentials",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"}
            )
        
        # Shopify 单个产品 API
        import httpx
        url = f"https://{shop_domain}/admin/api/2025-10/products/{product_id}.json"
        headers = {"X-Shopify-Access-Token": access_token}
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail="Product not found")
            
            data = response.json()
            shopify_product = data.get("product")
            
            from adapters.product_adapters import ShopifyProductAdapter
            standard_product = ShopifyProductAdapter.convert_to_standard(shopify_product, merchant_id)
            
            return {
                "status": "success",
                "product": standard_product.dict()
            }
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch product: {str(e)}")
    
    elif platform == "woocommerce":
        store_url = normalize_woocommerce_store_url(store_info.get("domain"))
        credentials = _parse_woocommerce_credentials(
            store_info.get("api_key_raw") or store_info.get("api_key")
        )
        if not store_url:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing domain",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )
        if not credentials["consumer_key"] or not credentials["consumer_secret"]:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing credentials",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        standard_product, error = await WooCommerceProductAdapter.fetch_product_by_id(
            store_url=store_url,
            consumer_key=credentials["consumer_key"],
            consumer_secret=credentials["consumer_secret"],
            merchant_id=merchant_id,
            product_id=product_id,
        )
        if error == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Product not found")
        if error:
            raise HTTPException(status_code=500, detail=f"Failed to fetch product: {error}")
        return {"status": "success", "product": standard_product.dict()}

    elif platform == "bigcommerce":
        credentials = _parse_bigcommerce_credentials(
            store_info.get("api_key_raw") or store_info.get("api_key"),
            store_info.get("domain"),
        )
        if not credentials["store_hash"]:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing domain",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )
        if not credentials["access_token"]:
            raise HTTPException(
                status_code=400,
                detail="Store configuration incomplete - missing credentials",
                headers={"X-Error-Code": "INVALID_STORE_CONFIG"},
            )

        standard_product, error = await BigCommerceProductAdapter.fetch_product_by_id(
            store_hash=credentials["store_hash"],
            access_token=credentials["access_token"],
            client_id=credentials["client_id"],
            merchant_id=merchant_id,
            product_id=product_id,
        )
        if error == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Product not found")
        if error:
            raise HTTPException(status_code=500, detail=f"Failed to fetch product: {error}")
        return {"status": "success", "product": standard_product.dict()}

    else:
        raise HTTPException(status_code=501, detail=f"Platform {platform} not yet implemented for single product fetch")


# ============================================================================
# ANALYTICS & MAINTENANCE APIs
# ============================================================================

@router.get("/analytics/{merchant_id}")
async def get_merchant_analytics(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    获取商户业务分析指标
    
    - API 调用量和缓存命中率
    - 订单创建率和转化率
    - 支付成功率和总收入
    """
    from db.products import merchant_analytics
    from db.database import database
    
    analytics = await database.fetch_one(
        merchant_analytics.select().where(merchant_analytics.c.merchant_id == merchant_id)
    )
    
    if not analytics:
        # 如果没有数据，触发计算
        from db.products import calculate_merchant_analytics
        await calculate_merchant_analytics(merchant_id, days=30)
        analytics = await database.fetch_one(
            merchant_analytics.select().where(merchant_analytics.c.merchant_id == merchant_id)
        )
    
    if not analytics:
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "message": "No analytics data available yet. Data will be generated after API calls."
        }
    
    return {
        "status": "success",
        "merchant_id": merchant_id,
        "analytics": dict(analytics)
    }


@router.post("/maintenance/cleanup-cache")
async def cleanup_expired_cache_api(
    current_user: dict = Depends(require_admin)
):
    """
    手动清理过期缓存（也可由定时任务调用）
    """
    deleted = await cleanup_expired_cache()
    return {
        "status": "success",
        "message": f"Cleaned up {deleted} expired cache entries"
    }


@router.post("/maintenance/recalculate-analytics/{merchant_id}")
async def recalculate_analytics(
    merchant_id: str,
    days: int = Query(30, ge=1, le=365, description="统计窗口（天）"),
    current_user: dict = Depends(require_admin)
):
    """
    手动重新计算商户分析指标
    """
    from db.products import calculate_merchant_analytics
    await calculate_merchant_analytics(merchant_id, days=days)
    
    return {
        "status": "success",
        "message": f"Analytics recalculated for merchant {merchant_id} (last {days} days)"
    }

    return {
        "status": "success",
        "message": f"Analytics recalculated for merchant {merchant_id} (last {days} days)"
    }
