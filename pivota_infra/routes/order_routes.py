"""
订单处理 API 路由
Pivota 核心业务流程：Agent 下单 → 支付 → 履约
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime
import httpx
import os

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
from routes.auth_routes import require_admin
from config.settings import settings
from adapters.psp_adapter import get_psp_adapter

router = APIRouter(prefix="/orders", tags=["orders"])


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
    
    # 2. 计算订单金额
    subtotal = sum(item.subtotal for item in order_request.items)
    shipping_fee = Decimal("0")  # TODO: 动态计算运费
    tax = Decimal("0")  # TODO: 动态计算税费
    total = subtotal + shipping_fee + tax
    
    # 3. 创建订单
    order_data = {
        "merchant_id": order_request.merchant_id,
        "customer_email": order_request.customer_email,
        "items": [item.dict() for item in order_request.items],
        "shipping_address": order_request.shipping_address.dict(),
        "subtotal": float(subtotal),
        "shipping_fee": float(shipping_fee),
        "tax": float(tax),
        "total": float(total),
        "currency": order_request.currency,
        "agent_session_id": order_request.agent_session_id,
        "metadata": order_request.metadata or {}
    }
    
    order_id = await create_order(order_data)
    
    # 4. 创建 Payment Intent（后台任务，不阻塞）
    async def create_payment_intent_task():
        """后台创建支付意图（支持多 PSP）"""
        try:
            # 获取商户的 PSP 类型和密钥
            psp_type = merchant.get("psp_type", "stripe")
            psp_key = merchant.get("psp_key") or (
                settings.stripe_secret_key if psp_type == "stripe" else settings.adyen_api_key
            )
            
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
    
    # 5. 返回订单信息
    order = await get_order(order_id)
    
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
        client_secret=order.get("client_secret"),
        created_at=order["created_at"],
        updated_at=order["updated_at"],
        agent_session_id=order.get("agent_session_id"),
        metadata=order.get("metadata")
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
        psp_key = merchant.get("psp_key") or (
            settings.stripe_secret_key if psp_type == "stripe" else settings.adyen_api_key
        )
        
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
                if merchant.get("mcp_connected") and merchant.get("mcp_platform") == "shopify":
                    await create_shopify_order(payment_request.order_id)
            
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
    current_user: dict = Depends(require_admin)
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
        order = await get_order(order_id)
        if not order:
            return False
        
        merchant = await get_merchant_onboarding(order["merchant_id"])
        if not merchant or not merchant.get("mcp_connected"):
            return False
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
        if not shop_domain or not access_token:
            return False
        
        # 构造 Shopify 订单数据
        shopify_order_data = {
            "order": {
                "email": order["customer_email"],
                "financial_status": "paid",
                "send_receipt": True,
                "send_fulfillment_receipt": True,
                "line_items": [
                    {
                        "variant_id": int(item["variant_id"]) if item.get("variant_id") else None,
                        "quantity": item["quantity"],
                        "price": str(item["unit_price"])
                    } for item in order["items"] if item.get("variant_id")
                ],
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
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=shopify_order_data, headers=headers, timeout=10.0)
            
            if response.status_code == 201:
                shopify_order = response.json()["order"]
                
                # 更新 Pivota 订单的 Shopify 订单 ID
                await update_fulfillment_info(
                    order_id=order_id,
                    shopify_order_id=str(shopify_order["id"]),
                    fulfillment_status="processing"
                )
                
                # 记录事件
                await log_order_event(
                    event_type="shopify_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={"shopify_order_id": shopify_order["id"]}
                )
                
                return True
            else:
                # 记录失败事件
                await log_order_event(
                    event_type="shopify_order_failed",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "status_code": response.status_code,
                        "error": response.text[:500]
                    }
                )
                return False
                
    except Exception as e:
        # 记录异常
        await log_order_event(
            event_type="shopify_order_error",
            order_id=order_id,
            merchant_id=order.get("merchant_id", "unknown"),
            metadata={"error": str(e)}
        )
        return False


# ============================================================================
# 订单状态更新（Admin/Webhook 调用）
# ============================================================================

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

