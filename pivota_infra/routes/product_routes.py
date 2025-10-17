"""
Product Proxy API Routes
Pivota 核心价值：实时代理 + 智能缓存 + 数据标准化
防御性架构：Agent 只读，事件追踪，自动清理
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Dict, Any
from datetime import datetime
import os
import time

from models.standard_product import ProductListResponse
from adapters.product_adapters import fetch_merchant_products
from db.merchant_onboarding import get_merchant_onboarding
from db.products import (
    get_cached_products, upsert_product_cache, mark_cache_accessed,
    log_api_call, cleanup_expired_cache
)
from routes.auth_routes import require_admin
from config.settings import settings

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/{merchant_id}", response_model=ProductListResponse)
async def get_merchant_products_realtime(
    merchant_id: str,
    limit: int = Query(50, ge=1, le=250, description="返回产品数量"),
    force_refresh: bool = Query(False, description="强制刷新缓存"),
    current_user: dict = Depends(require_admin)
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
    
    # 1. 获取商户信息
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # 2. 检查 MCP 连接状态
    if not merchant.get("mcp_connected"):
        raise HTTPException(
            status_code=400,
            detail=f"Merchant {merchant_id} has not connected to any e-commerce platform (MCP)."
        )
    
    platform = merchant.get("mcp_platform")
    if not platform:
        raise HTTPException(status_code=400, detail="MCP platform not specified")
    
    # 3. 尝试从缓存读取（除非强制刷新）
    if not force_refresh:
        cached = await get_cached_products(merchant_id, platform, include_expired=False)
        if cached:
            cache_hit = True
            products = [c["product_data"] for c in cached[:limit]]
            
            # 更新访问统计
            for c in cached[:limit]:
                await mark_cache_accessed(c["id"])
            
            # 记录 API 调用事件（缓存命中）
            response_time_ms = int((time.time() - start_time) * 1000)
            await log_api_call(
                event_type="product_query",
                merchant_id=merchant_id,
                endpoint=f"/products/{merchant_id}",
                request_params={"limit": limit, "force_refresh": force_refresh},
                response_status=200,
                cache_hit=True,
                response_time_ms=response_time_ms,
                product_ids=[p["id"] for p in products]
            )
            
            # 从缓存返回的是字典，需要转换为 StandardProduct 对象
            from models.standard_product import StandardProduct
            product_objects = [StandardProduct(**p) for p in products]
            
            return ProductListResponse(
                status="success",
                merchant_id=merchant_id,
                platform=platform,
                total=len(products),
                products=product_objects,
                next_page_token=None,
                fetched_at=datetime.now()
            )
    
    # 4. Cache miss → 实时拉取
    credentials = {}
    
    if platform == "shopify":
        shop_domain = merchant.get("mcp_shop_domain") or os.getenv("SHOPIFY_SHOP_DOMAIN") or getattr(settings, "shopify_shop_domain", None)
        access_token = merchant.get("mcp_access_token") or os.getenv("SHOPIFY_ACCESS_TOKEN") or getattr(settings, "shopify_access_token", None)
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Shopify credentials not found.")
        
        credentials = {"shop_domain": shop_domain, "access_token": access_token}
    
    elif platform == "wix":
        raise HTTPException(status_code=501, detail="Wix platform not yet implemented")
    
    elif platform == "woocommerce":
        raise HTTPException(status_code=501, detail="WooCommerce platform not yet implemented")
    
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
            endpoint=f"/products/{merchant_id}",
            request_params={"limit": limit, "force_refresh": force_refresh},
            response_status=500,
            cache_hit=False,
            response_time_ms=response_time_ms
        )
        raise HTTPException(status_code=500, detail=f"Failed to fetch products: {error}")
    
    # 6. 更新缓存（后台任务，不阻塞响应）
    for p in products_obj:
        await upsert_product_cache(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=p.id,
            product_data=p.dict(),
            ttl_seconds=3600  # 1小时
        )
    
    # 7. 记录 API 调用事件（缓存未命中）
    response_time_ms = int((time.time() - start_time) * 1000)
    await log_api_call(
        event_type="product_query",
        merchant_id=merchant_id,
        endpoint=f"/products/{merchant_id}",
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
    
    platform = merchant.get("mcp_platform")
    
    if platform == "shopify":
        shop_domain = merchant.get("mcp_shop_domain") or os.getenv("SHOPIFY_SHOP_DOMAIN")
        access_token = merchant.get("mcp_access_token") or os.getenv("SHOPIFY_ACCESS_TOKEN")
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Shopify credentials not found")
        
        # Shopify 单个产品 API
        import httpx
        url = f"https://{shop_domain}/admin/api/2024-07/products/{product_id}.json"
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
