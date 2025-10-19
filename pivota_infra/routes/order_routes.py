"""
订单处理 API 路由
Pivota 核心业务流程：Agent 下单 → 支付 → 履约
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal
from datetime import datetime
import httpx
import os
import json

from models.order import (
    CreateOrderRequest, OrderResponse, PaymentConfirmRequest, 
    OrderListResponse, OrderItem, OrderStatus
)
from db.orders import (
    create_order, get_order, get_orders_by_merchant, get_orders_by_customer,
    update_order_status, update_payment_info, mark_order_paid, 
    update_fulfillment_info, mark_order_shipped, get_order_stats
)
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from utils.auth import require_admin, verify_jwt_token
from config.settings import settings
from adapters.psp_adapter import get_psp_adapter
from utils.logger import logger

router = APIRouter(prefix="/orders", tags=["orders"])


# ============================================================================
# 库存检查
# ============================================================================

async def check_inventory_availability(
    merchant_id: str,
    items: List[OrderItem]
) -> Tuple[bool, Dict[str, Any]]:
    """
    检查 Shopify 库存是否充足
    
    返回: (是否有库存, 库存详情)
    """
    try:
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant or not merchant.get("mcp_connected"):
            # 如果未连接 MCP，默认允许订单
            return True, {"message": "MCP not connected, skipping inventory check"}
        
        if merchant.get("mcp_platform") != "shopify":
            # 非 Shopify 平台，暂不检查库存
            return True, {"message": f"Platform {merchant.get('mcp_platform')} inventory check not implemented"}
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
        if not shop_domain or not access_token:
            return True, {"message": "Shop credentials missing, skipping inventory check"}
        
        # 获取所有产品和变体
        url = f"https://{shop_domain}/admin/api/2024-01/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                return True, {"message": "Failed to fetch products, allowing order"}
            
            products = response.json().get("products", [])
            
            # 建立 variant_id -> inventory 的映射
            inventory_map = {}
            for product in products:
                for variant in product.get("variants", []):
                    variant_id = str(variant["id"])
                    inventory_map[variant_id] = {
                        "available": variant.get("inventory_quantity", 0),
                        "tracked": variant.get("inventory_management") == "shopify",
                        "sku": variant.get("sku"),
                        "title": f"{product['title']} - {variant.get('title', '')}"
                    }
            
            # 检查每个订单项的库存
            insufficient_items = []
            inventory_details = {}
            
            for item in items:
                if not item.variant_id:
                    # 如果没有 variant_id，跳过检查
                    continue
                
                variant_id = str(item.variant_id)
                if variant_id in inventory_map:
                    inv = inventory_map[variant_id]
                    inventory_details[variant_id] = inv
                    
                    if inv["tracked"] and inv["available"] < item.quantity:
                        insufficient_items.append({
                            "product": item.product_title,
                            "requested": item.quantity,
                            "available": inv["available"]
                        })
            
            if insufficient_items:
                return False, {
                    "message": "Insufficient inventory",
                    "items": insufficient_items
                }
            
            return True, {
                "message": "Inventory check passed",
                "details": inventory_details
            }
            
    except Exception as e:
        # 库存检查失败时，默认允许订单（fail-open）
        logger.error(f"Inventory check failed: {e}")
        return True, {"message": f"Inventory check error: {str(e)}, allowing order"}


# ============================================================================
# 订单创建（Agent 调用）
# ============================================================================

@router.post("/create", response_model=OrderResponse)
async def create_new_order(
    order_request: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)  # Agent 需要管理员权限
):
    """
    **创建新订单（Agent → Pivota）**
    
    流程：
    1. 验证商户存在且已连接 PSP
    2. 计算订单总价
    3. 创建订单记录
    4. 创建 Stripe Payment Intent
    5. 返回订单详情和支付密钥
    
    防御性设计：
    - 订单创建后立即记录事件日志
    - 金额使用 Decimal 精确计算
    - 支付信息与订单解耦，失败不影响订单创建
    """
    
    # 1. 验证商户
    merchant = await get_merchant_onboarding(order_request.merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    if not merchant.get("psp_connected"):
        raise HTTPException(
            status_code=400, 
            detail="Merchant has not connected PSP. Cannot process payments."
        )
    
    # 2. 检查库存（如果商户连接了 Shopify）
    has_inventory, inventory_info = await check_inventory_availability(
        order_request.merchant_id,
        order_request.items
    )
    
    if not has_inventory:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Insufficient inventory",
                "items": inventory_info.get("items", [])
            }
        )
    
    # 3. 计算订单金额
    subtotal = sum(item.subtotal for item in order_request.items)
    shipping_fee = Decimal("0")  # TODO: 动态计算运费
    tax = Decimal("0")  # TODO: 动态计算税费
    total = subtotal + shipping_fee + tax
    
    # 4. 创建订单
    # 使用 json.loads(x.json()) 确保 Decimal 被序列化为字符串
    order_data = {
        "merchant_id": order_request.merchant_id,
        "customer_email": order_request.customer_email,
        "items": [json.loads(item.json()) for item in order_request.items],
        "shipping_address": json.loads(order_request.shipping_address.json()),
        "subtotal": float(subtotal),
        "shipping_fee": float(shipping_fee),
        "tax": float(tax),
        "total": float(total),
        "currency": order_request.currency,
        "agent_session_id": order_request.agent_session_id,
        "metadata": order_request.metadata or {}
    }
    
    order_id = await create_order(order_data)
    
    # 5. 创建 Payment Intent（后台任务，不阻塞）
    async def create_payment_intent_task():
        """后台创建支付意图（支持多 PSP）"""
        try:
            # 获取商户的 PSP 类型和密钥
            psp_type = merchant.get("psp_type", "stripe")
            # 尝试获取 psp_sandbox_key 或 psp_key
            psp_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")
            
            # 如果商户没有配置密钥，使用系统默认（开发环境）
            if not psp_key:
                if psp_type == "stripe":
                    psp_key = getattr(settings, "stripe_secret_key", None)
                else:
                    psp_key = getattr(settings, "adyen_api_key", None)
            
            if not psp_key:
                raise ValueError(f"No PSP key found for merchant {merchant['merchant_id']}")
            
            # 创建 PSP 适配器
            psp_adapter = get_psp_adapter(psp_type, psp_key)
            
            # 创建支付意图
            success, payment_intent, error = await psp_adapter.create_payment_intent(
                amount=total,
                currency=order_request.currency,
                metadata={
                    "order_id": order_id,
                    "merchant_id": order_request.merchant_id,
                    "customer_email": order_request.customer_email
                }
            )
            
            if success and payment_intent:
                # 更新订单支付信息
                await update_payment_info(
                    order_id=order_id,
                    payment_intent_id=payment_intent.id,
                    client_secret=payment_intent.client_secret,
                    payment_status="awaiting_payment"
                )
                
                # 记录订单创建事件
                await log_order_event(
                    event_type="order_created",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={
                        "total": float(total),
                        "currency": order_request.currency,
                        "items_count": len(order_request.items),
                        "payment_intent_id": payment_intent.id,
                        "psp_type": psp_type
                    }
                )
            else:
                # 支付创建失败，记录错误但不影响订单
                await log_order_event(
                    event_type="payment_intent_failed",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    metadata={"error": error, "psp_type": psp_type}
                )
            
        except Exception as e:
            # 捕获所有异常
            await log_order_event(
                event_type="payment_intent_error",
                order_id=order_id,
                merchant_id=order_request.merchant_id,
                metadata={"error": str(e)}
            )
    
    background_tasks.add_task(create_payment_intent_task)
    
    # 6. 返回订单信息
    # Note: Order was just created, construct response from known data
    # This avoids potential race conditions with database transactions
    return OrderResponse(
        order_id=order_id,
        merchant_id=order_request.merchant_id,
        customer_email=order_request.customer_email,
        items=order_request.items,
        shipping_address=order_request.shipping_address,
        subtotal=float(subtotal),
        shipping_fee=float(shipping_fee),
        tax=float(tax),
        total=float(total),
        currency=order_request.currency,
        status="pending",
        payment_status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )


# ============================================================================
# 支付处理
# ============================================================================

@router.post("/payment/confirm")
async def confirm_payment(
    payment_request: PaymentConfirmRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    """
    **确认支付（Agent 调用）**
    
    流程：
    1. 验证订单存在
    2. 确认 Stripe Payment Intent
    3. 更新订单状态为已支付
    4. 触发履约流程（创建 Shopify 订单）
    """
    
    order = await get_order(payment_request.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        return {"status": "success", "message": "Order already paid"}
    
    # 获取商户信息
    merchant = await get_merchant_onboarding(order["merchant_id"])
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    try:
        # 获取商户的 PSP 类型和密钥
        psp_type = merchant.get("psp_type", "stripe")
        # 尝试获取 psp_sandbox_key 或 psp_key (same logic as create_new_order)
        psp_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")
        
        # 如果商户没有配置密钥，使用系统默认（开发环境）
        if not psp_key:
            if psp_type == "stripe":
                psp_key = getattr(settings, "stripe_secret_key", None)
            else:
                psp_key = getattr(settings, "adyen_api_key", None)
        
        if not psp_key:
            raise ValueError(f"No PSP key found for merchant {merchant['merchant_id']}")
        
        # 创建 PSP 适配器
        psp_adapter = get_psp_adapter(psp_type, psp_key)
        
        # 确认支付
        success, status, error = await psp_adapter.confirm_payment(
            payment_intent_id=order["payment_intent_id"],
            payment_method_id=payment_request.payment_method_id
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Payment confirmation failed: {error}")
        
        if status == "succeeded":
            # 标记订单已支付
            await mark_order_paid(payment_request.order_id)
            
            # 记录支付成功事件
            await log_order_event(
                event_type="payment_succeeded",
                order_id=payment_request.order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "payment_intent_id": order["payment_intent_id"],
                    "amount": float(order["total"]),
                    "currency": order["currency"],
                    "psp_type": psp_type
                }
            )
            
            # 后台任务：创建 Shopify 订单
            async def create_shopify_order_task():
                """创建 Shopify 订单通知商户发货"""
                try:
                    if merchant.get("mcp_connected") and merchant.get("mcp_platform") == "shopify":
                        logger.info(f"Creating Shopify order for {payment_request.order_id}")
                        success = await create_shopify_order(payment_request.order_id)
                        if success:
                            logger.info(f"Shopify order created successfully for {payment_request.order_id}")
                        else:
                            logger.error(f"Failed to create Shopify order for {payment_request.order_id}")
                except Exception as e:
                    logger.error(f"Error in Shopify order creation task: {e}")
            
            background_tasks.add_task(create_shopify_order_task)
            
            return {
                "status": "success",
                "message": "Payment confirmed successfully",
                "order_id": payment_request.order_id,
                "payment_intent_id": order["payment_intent_id"],
                "psp_type": psp_type
            }
        else:
            return {
                "status": "pending",
                "message": f"Payment status: {status}",
                "payment_intent_id": order["payment_intent_id"]
            }
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment failed: {str(e)}")


# ============================================================================
# 订单查询
# ============================================================================

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_details(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取订单详情"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return OrderResponse(
        order_id=order["order_id"],
        merchant_id=order["merchant_id"],
        customer_email=order["customer_email"],
        items=[OrderItem(**item) for item in order["items"]],
        shipping_address=order["shipping_address"],
        subtotal=order["subtotal"],
        shipping_fee=order["shipping_fee"],
        tax=order["tax"],
        total=order["total"],
        currency=order["currency"],
        status=order["status"],
        payment_status=order["payment_status"],
        fulfillment_status=order.get("fulfillment_status"),
        payment_intent_id=order.get("payment_intent_id"),
        shopify_order_id=order.get("shopify_order_id"),
        tracking_number=order.get("tracking_number"),
        created_at=order["created_at"],
        updated_at=order["updated_at"],
        paid_at=order.get("paid_at"),
        shipped_at=order.get("shipped_at"),
        agent_session_id=order.get("agent_session_id"),
        metadata=order.get("metadata")
    )


@router.get("/merchant/{merchant_id}", response_model=OrderListResponse)
async def get_merchant_orders(
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(verify_jwt_token)  # Allow authenticated users
):
    """获取商户的订单列表"""
    orders_list = await get_orders_by_merchant(merchant_id, status, limit, offset)
    
    return OrderListResponse(
        status="success",
        total=len(orders_list),
        orders=[
            OrderResponse(
                order_id=o["order_id"],
                merchant_id=o["merchant_id"],
                customer_email=o["customer_email"],
                items=[OrderItem(**item) for item in o["items"]],
                shipping_address=o["shipping_address"],
                subtotal=o["subtotal"],
                shipping_fee=o["shipping_fee"],
                tax=o["tax"],
                total=o["total"],
                currency=o["currency"],
                status=o["status"],
                payment_status=o["payment_status"],
                fulfillment_status=o.get("fulfillment_status"),
                payment_intent_id=o.get("payment_intent_id"),
                shopify_order_id=o.get("shopify_order_id"),
                tracking_number=o.get("tracking_number"),
                created_at=o["created_at"],
                updated_at=o["updated_at"],
                paid_at=o.get("paid_at"),
                shipped_at=o.get("shipped_at"),
                agent_session_id=o.get("agent_session_id"),
                metadata=o.get("metadata")
            ) for o in orders_list
        ]
    )


@router.get("/merchant/{merchant_id}/stats")
async def get_merchant_order_stats(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取商户订单统计"""
    stats = await get_order_stats(merchant_id)
    return stats


# ============================================================================
# Shopify 订单创建（履约集成）
# ============================================================================

async def create_shopify_order(order_id: str) -> bool:
    """
    在 Shopify 中创建订单（通知商户发货）
    
    防御性设计：
    - 失败不影响 Pivota 订单状态
    - 记录事件日志用于后续重试
    """
    try:
        logger.info(f"Starting Shopify order creation for {order_id}")
        
        order = await get_order(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return False
        
        merchant = await get_merchant_onboarding(order["merchant_id"])
        if not merchant or not merchant.get("mcp_connected"):
            logger.error(f"Merchant not connected to Shopify: {order['merchant_id']}")
            return False
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
        if not shop_domain or not access_token:
            logger.error(f"Missing Shopify credentials for merchant {order['merchant_id']}")
            return False
        
        logger.info(f"Using Shopify store: {shop_domain}")
        
        # 构造 Shopify 订单数据 - use title-based line items instead of variant_id
        line_items = []
        for item in order["items"]:
            line_item = {
                "title": item.get("product_title", "Product"),
                "quantity": item["quantity"],
                "price": str(item["unit_price"])
            }
            # Only add variant_id if it's valid
            if item.get("variant_id"):
                try:
                    variant_id = int(item["variant_id"])
                    # Only use variant_id if it's reasonable (not our test ID)
                    if variant_id < 1000000000000:
                        line_item["variant_id"] = variant_id
                except (ValueError, TypeError):
                    pass
            line_items.append(line_item)
        
        shopify_order_data = {
            "order": {
                "email": order["customer_email"],
                "financial_status": "paid",
                "send_receipt": True,
                "send_fulfillment_receipt": True,
                "line_items": line_items,
                "shipping_address": order["shipping_address"],
                "note": f"Pivota Order ID: {order_id}",
                "tags": "pivota,agent-order"
            }
        }
        
        # 调用 Shopify API
        url = f"https://{shop_domain}/admin/api/2024-01/orders.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        logger.info(f"Calling Shopify API: {url}")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=shopify_order_data, headers=headers, timeout=10.0)
            
            logger.info(f"Shopify API response: {response.status_code}")
            
            if response.status_code == 201:
                shopify_order = response.json()["order"]
                shopify_order_id = str(shopify_order["id"])
                
                logger.info(f"Shopify order created: {shopify_order_id}")
                
                # 更新 Pivota 订单的 Shopify 订单 ID
                await update_fulfillment_info(
                    order_id=order_id,
                    shopify_order_id=shopify_order_id,
                    fulfillment_status="processing"
                )
                
                # 记录事件
                await log_order_event(
                    event_type="shopify_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={"shopify_order_id": shopify_order_id}
                )
                
                logger.info(f"Successfully created Shopify order {shopify_order_id} for Pivota order {order_id}")
                return True
            else:
                error_msg = response.text[:500]
                logger.error(f"Shopify API error: {response.status_code} - {error_msg}")
                
                # 记录失败事件
                await log_order_event(
                    event_type="shopify_order_failed",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "status_code": response.status_code,
                        "error": error_msg
                    }
                )
                return False
                
    except Exception as e:
        logger.error(f"Exception in create_shopify_order: {str(e)}")
        # 记录异常
        await log_order_event(
            event_type="shopify_order_error",
            order_id=order_id,
            merchant_id=order.get("merchant_id", "unknown") if order else "unknown",
            metadata={"error": str(e)}
        )
        return False


# ============================================================================
# 订单状态更新（Admin/Webhook 调用）
# ============================================================================

@router.post("/{order_id}/create-shopify")
async def trigger_shopify_order(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """Manually trigger Shopify order creation for debugging"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.get("shopify_order_id"):
        return {"status": "already_exists", "shopify_order_id": order["shopify_order_id"]}
    
    if order.get("payment_status") != "paid":
        return {"status": "not_paid", "payment_status": order.get("payment_status")}
    
    try:
        success = await create_shopify_order(order_id)
        if success:
            updated_order = await get_order(order_id)
            return {
                "status": "success",
                "shopify_order_id": updated_order.get("shopify_order_id"),
                "message": "Shopify order created"
            }
        else:
            return {"status": "failed", "message": "Check logs for details"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@router.post("/{order_id}/ship")
async def mark_order_as_shipped(
    order_id: str,
    tracking_number: str,
    carrier: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """标记订单已发货"""
    success = await mark_order_shipped(order_id, tracking_number, carrier)
    
    if not success:
        raise HTTPException(status_code=404, detail="Order not found")
    
    order = await get_order(order_id)
    
    # 记录发货事件
    await log_order_event(
        event_type="order_shipped",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={
            "tracking_number": tracking_number,
            "carrier": carrier
        }
    )
    
    return {
        "status": "success",
        "message": "Order marked as shipped",
        "order_id": order_id,
        "tracking_number": tracking_number
    }


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """取消订单"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        raise HTTPException(
            status_code=400, 
            detail="Cannot cancel paid order. Please process refund first."
        )
    
    success = await update_order_status(
        order_id=order_id,
        status="cancelled",
        cancelled_at=datetime.now(),
        metadata={**(order.get("metadata") or {}), "cancellation_reason": reason}
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to cancel order")
    
    # 记录取消事件
    await log_order_event(
        event_type="order_cancelled",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={"reason": reason}
    )
    
    return {
        "status": "success",
        "message": "Order cancelled",
        "order_id": order_id
    }

