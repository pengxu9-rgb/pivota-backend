"""
Agent 专用 API 路由
为 AI Agent 提供优化的电商接口
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime
import json

from models.order import CreateOrderRequest, OrderResponse
from models.standard_product import StandardProduct
from db.merchant_onboarding import get_merchant_onboarding
from db.products import get_cached_products
from db.orders import get_order, get_orders_by_merchant
from routes.order_routes import create_new_order
from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from utils.logger import logger


router = APIRouter(prefix="/agent/v1", tags=["agent-api"])


# ============================================================================
# 产品搜索和浏览
# ============================================================================

@router.get("/products/search")
async def agent_search_products(
    merchant_id: str,
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock_only: bool = True,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    智能产品搜索
    
    特点：
    - 支持自然语言查询
    - 自动过滤库存
    - 价格区间筛选
    - 分页支持
    """
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 从缓存获取产品
        cached_products = await get_cached_products(merchant_id)
        
        products = []
        if cached_products and cached_products.get("products"):
            all_products = cached_products["products"]
            
            # 应用过滤器
            for product in all_products:
                # 库存过滤
                if in_stock_only and not product.get("in_stock", True):
                    continue
                
                # 价格过滤
                price = float(product.get("price", 0))
                if min_price and price < min_price:
                    continue
                if max_price and price > max_price:
                    continue
                
                # 类别过滤
                if category:
                    product_category = product.get("category", "").lower()
                    if category.lower() not in product_category:
                        continue
                
                # 搜索查询（简单的关键词匹配）
                if query:
                    query_lower = query.lower()
                    title = product.get("title", "").lower()
                    description = product.get("description", "").lower()
                    if query_lower not in title and query_lower not in description:
                        continue
                
                products.append(product)
        
        # 分页
        total = len(products)
        products = products[offset:offset + limit]
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "products": products,
            "filters_applied": {
                "query": query,
                "category": category,
                "price_range": f"{min_price or 0}-{max_price or 'unlimited'}",
                "in_stock_only": in_stock_only
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent product search error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Search failed")


@router.get("/products/{merchant_id}/{product_id}")
async def agent_get_product(
    merchant_id: str,
    product_id: str,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """获取单个产品详情"""
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 从缓存获取产品
        cached_products = await get_cached_products(merchant_id)
        
        if cached_products and cached_products.get("products"):
            for product in cached_products["products"]:
                if str(product.get("id")) == str(product_id):
                    # 记录请求
                    background_tasks.add_task(
                        log_agent_request,
                        context=context,
                        status_code=200,
                        merchant_id=merchant_id
                    )
                    return {
                        "status": "success",
                        "product": product
                    }
        
        raise HTTPException(status_code=404, detail="Product not found")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent get product error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to get product")


# ============================================================================
# 购物车验证和价格计算
# ============================================================================

@router.post("/cart/validate")
async def agent_validate_cart(
    merchant_id: str,
    items: List[Dict[str, Any]],
    shipping_country: str = "US",
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    验证购物车并计算价格
    
    功能：
    - 库存验证
    - 价格更新
    - 运费计算
    - 税费估算
    """
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 获取产品信息
        cached_products = await get_cached_products(merchant_id)
        product_map = {}
        
        if cached_products and cached_products.get("products"):
            for product in cached_products["products"]:
                product_map[str(product.get("id"))] = product
        
        # 验证每个商品
        validated_items = []
        validation_errors = []
        subtotal = Decimal("0")
        
        for item in items:
            product_id = str(item.get("product_id"))
            quantity = item.get("quantity", 1)
            
            if product_id not in product_map:
                validation_errors.append({
                    "product_id": product_id,
                    "error": "Product not found"
                })
                continue
            
            product = product_map[product_id]
            
            # 检查库存
            if not product.get("in_stock", True):
                validation_errors.append({
                    "product_id": product_id,
                    "error": "Out of stock"
                })
                continue
            
            # 计算价格
            unit_price = Decimal(str(product.get("price", 0)))
            item_subtotal = unit_price * quantity
            subtotal += item_subtotal
            
            validated_items.append({
                "product_id": product_id,
                "product_title": product.get("title"),
                "variant_id": product.get("variant_id"),
                "sku": product.get("sku"),
                "quantity": quantity,
                "unit_price": str(unit_price),
                "subtotal": str(item_subtotal),
                "in_stock": True
            })
        
        # 计算运费（简单示例）
        shipping_fee = Decimal("10.00") if shipping_country == "US" else Decimal("25.00")
        if subtotal > 100:
            shipping_fee = Decimal("0")  # 免运费
        
        # 计算税费（简单示例）
        tax_rate = Decimal("0.08") if shipping_country == "US" else Decimal("0.15")
        tax = subtotal * tax_rate
        
        # 总计
        total = subtotal + shipping_fee + tax
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "valid": len(validation_errors) == 0,
            "items": validated_items,
            "errors": validation_errors,
            "pricing": {
                "subtotal": str(subtotal),
                "shipping_fee": str(shipping_fee),
                "tax": str(tax),
                "total": str(total),
                "currency": "USD"
            },
            "shipping": {
                "country": shipping_country,
                "free_shipping_threshold": 100,
                "estimated_delivery": "3-5 business days" if shipping_country == "US" else "7-14 business days"
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cart validation error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Cart validation failed")


# ============================================================================
# 订单管理
# ============================================================================

@router.post("/orders/create")
async def agent_create_order(
    order_request: CreateOrderRequest,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    创建订单（代理标准订单创建流程）
    
    自动添加 Agent 追踪信息
    """
    try:
        # 验证商户访问权限
        if not context.can_access_merchant(order_request.merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 添加 Agent 元数据
        if not order_request.metadata:
            order_request.metadata = {}
        
        order_request.metadata.update({
            "agent_id": context.agent_id,
            "agent_name": context.agent_name,
            "created_via": "agent_api"
        })
        
        # 设置 agent session ID
        if not order_request.agent_session_id:
            order_request.agent_session_id = f"{context.agent_id}_{int(datetime.utcnow().timestamp())}"
        
        # 调用标准订单创建
        from routes.order_routes import create_new_order
        order_response = await create_new_order(order_request, background_tasks)
        
        # 计算订单总额
        order_amount = float(order_response.total)
        
        # 记录成功请求
        await log_agent_request(
            context=context,
            status_code=200,
            merchant_id=order_request.merchant_id,
            order_id=order_response.order_id,
            order_amount=order_amount
        )
        
        # 返回简化的响应给 Agent
        return {
            "status": "success",
            "order_id": order_response.order_id,
            "total": str(order_response.total),
            "currency": order_response.currency,
            "payment": {
                "client_secret": order_response.client_secret,
                "payment_intent_id": order_response.payment_intent_id,
                "instructions": "Use client_secret for Stripe payment confirmation"
            },
            "tracking": {
                "agent_session_id": order_request.agent_session_id,
                "created_at": order_response.created_at.isoformat()
            }
        }
        
    except HTTPException as e:
        await log_agent_request(
            context=context,
            status_code=e.status_code,
            merchant_id=order_request.merchant_id,
            error_message=e.detail
        )
        raise
    except Exception as e:
        logger.error(f"Agent order creation error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            merchant_id=order_request.merchant_id,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Order creation failed")


@router.get("/orders/{order_id}")
async def agent_get_order(
    order_id: str,
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """获取订单状态"""
    try:
        # 获取订单
        order = await get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # 验证商户访问权限
        if not context.can_access_merchant(order["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=order["merchant_id"],
            order_id=order_id
        )
        
        # 返回简化的订单信息
        return {
            "status": "success",
            "order": {
                "order_id": order["order_id"],
                "status": order["status"],
                "payment_status": order["payment_status"],
                "fulfillment_status": order.get("fulfillment_status"),
                "total": str(order["total"]),
                "currency": order["currency"],
                "tracking_number": order.get("tracking_number"),
                "created_at": order["created_at"],
                "updated_at": order.get("updated_at")
            }
        }
        
    except HTTPException as e:
        await log_agent_request(
            context=context,
            status_code=e.status_code,
            error_message=e.detail
        )
        raise
    except Exception as e:
        logger.error(f"Agent get order error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to get order")


@router.get("/orders")
async def agent_list_orders(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    """
    列出 Agent 创建的订单
    
    可以按商户或状态过滤
    """
    try:
        # 如果指定了商户，验证访问权限
        if merchant_id and not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")
        
        # 构建查询
        query = f"""
            SELECT * FROM orders 
            WHERE metadata->>'agent_id' = :agent_id
        """
        params = {"agent_id": context.agent_id}
        
        if merchant_id:
            query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        
        if status:
            query += " AND status = :status"
            params["status"] = status
        
        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset
        
        # 执行查询
        from db.database import database
        orders = await database.fetch_all(query, params)
        
        # 记录请求
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        return {
            "status": "success",
            "total": len(orders),
            "orders": [
                {
                    "order_id": order["order_id"],
                    "merchant_id": order["merchant_id"],
                    "status": order["status"],
                    "payment_status": order["payment_status"],
                    "total": str(order["total"]),
                    "created_at": order["created_at"]
                }
                for order in orders
            ]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent list orders error: {e}")
        await log_agent_request(
            context=context,
            status_code=500,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail="Failed to list orders")


# ============================================================================
# Agent 分析
# ============================================================================

@router.get("/analytics/summary")
async def agent_get_analytics(
    days: int = Query(default=30, le=365),
    context: AgentContext = Depends(get_agent_context)
):
    """
    获取 Agent 自己的分析数据
    
    包括：
    - 请求统计
    - 订单转化率
    - GMV
    - 热门商户
    """
    try:
        from datetime import timedelta
        from db.agents import get_agent_analytics
        
        start_date = datetime.utcnow() - timedelta(days=days)
        analytics = await get_agent_analytics(
            context.agent_id,
            start_date=start_date
        )
        
        return {
            "status": "success",
            "agent_id": context.agent_id,
            "agent_name": context.agent_name,
            "analytics": analytics
        }
        
    except Exception as e:
        logger.error(f"Agent analytics error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get analytics")
