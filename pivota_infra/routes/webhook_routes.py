"""
Webhook 处理路由
处理来自 PSP（Stripe/Adyen）和 MCP（Shopify）的事件通知
"""

from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional, Dict, Any
import stripe
import hmac
import hashlib
import json
from datetime import datetime

from db.orders import get_order, update_order_status, mark_order_paid, mark_order_shipped
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from config.settings import settings
from utils.logger import logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ============================================================================
# Stripe Webhooks
# ============================================================================

@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None)
):
    """
    处理 Stripe 支付事件
    
    支持的事件：
    - payment_intent.succeeded: 支付成功
    - payment_intent.payment_failed: 支付失败
    - charge.refunded: 退款成功
    """
    try:
        payload = await request.body()
        event = None
        
        # 验证签名（如果配置了 webhook secret）
        if hasattr(settings, 'stripe_webhook_secret') and settings.stripe_webhook_secret:
            try:
                event = stripe.Webhook.construct_event(
                    payload, stripe_signature, settings.stripe_webhook_secret
                )
            except ValueError:
                logger.error("Invalid Stripe webhook payload")
                raise HTTPException(status_code=400, detail="Invalid payload")
            except stripe.error.SignatureVerificationError:
                logger.error("Invalid Stripe webhook signature")
                raise HTTPException(status_code=400, detail="Invalid signature")
        else:
            # 开发环境：不验证签名
            event = json.loads(payload)
        
        # 处理事件
        event_type = event.get("type")
        data = event.get("data", {}).get("object", {})
        
        logger.info(f"Received Stripe webhook: {event_type}")
        
        if event_type == "payment_intent.succeeded":
            # 支付成功
            payment_intent_id = data.get("id")
            
            # 查找对应的订单
            query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
            from db.database import database
            result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
            
            if result:
                order_id = result["order_id"]
                await mark_order_paid(order_id)
                await log_order_event(
                    event_type="payment_confirmed_webhook",
                    order_id=order_id,
                    merchant_id=result["merchant_id"],
                    metadata={
                        "payment_intent_id": payment_intent_id,
                        "amount": data.get("amount"),
                        "currency": data.get("currency")
                    }
                )
                logger.info(f"Order {order_id} marked as paid via webhook")
                
        elif event_type == "payment_intent.payment_failed":
            # 支付失败
            payment_intent_id = data.get("id")
            error_message = data.get("last_payment_error", {}).get("message", "Unknown error")
            
            query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
            from db.database import database
            result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
            
            if result:
                order_id = result["order_id"]
                await update_order_status(order_id, "payment_failed")
                await log_order_event(
                    event_type="payment_failed_webhook",
                    order_id=order_id,
                    merchant_id=result["merchant_id"],
                    metadata={
                        "payment_intent_id": payment_intent_id,
                        "error": error_message
                    }
                )
                logger.warning(f"Order {order_id} payment failed: {error_message}")
                
        elif event_type == "charge.refunded":
            # 退款成功
            charge_id = data.get("id")
            payment_intent_id = data.get("payment_intent")
            refund_amount = data.get("amount_refunded")
            
            query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
            from db.database import database
            result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
            
            if result:
                order_id = result["order_id"]
                await update_order_status(order_id, "refunded")
                await log_order_event(
                    event_type="refund_processed_webhook",
                    order_id=order_id,
                    merchant_id=result["merchant_id"],
                    metadata={
                        "charge_id": charge_id,
                        "refund_amount": refund_amount
                    }
                )
                logger.info(f"Order {order_id} refunded: {refund_amount}")
        
        return {"status": "success", "event": event_type}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error handling Stripe webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Shopify Webhooks
# ============================================================================

@router.post("/shopify/{merchant_id}")
async def handle_shopify_webhook(
    merchant_id: str,
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None)
):
    """
    处理 Shopify 事件
    
    支持的事件：
    - orders/fulfilled: 订单履约完成
    - orders/cancelled: 订单取消
    - orders/updated: 订单更新
    """
    try:
        payload = await request.body()
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # 验证 webhook（如果有 webhook secret）
        webhook_secret = merchant.get("shopify_webhook_secret")
        if webhook_secret and x_shopify_hmac_sha256:
            calculated_hmac = hmac.new(
                webhook_secret.encode('utf-8'),
                payload,
                hashlib.sha256
            ).digest()
            import base64
            calculated_hmac_base64 = base64.b64encode(calculated_hmac).decode()
            
            if calculated_hmac_base64 != x_shopify_hmac_sha256:
                logger.error(f"Invalid Shopify webhook signature for merchant {merchant_id}")
                raise HTTPException(status_code=401, detail="Invalid signature")
        
        # 解析事件
        data = json.loads(payload)
        topic = x_shopify_topic or "unknown"
        
        logger.info(f"Received Shopify webhook for {merchant_id}: {topic}")
        
        if topic == "orders/fulfilled":
            # 订单履约完成
            shopify_order_id = str(data.get("id"))
            tracking_numbers = []
            
            # 提取跟踪号
            for fulfillment in data.get("fulfillments", []):
                tracking_numbers.extend(fulfillment.get("tracking_numbers", []))
            
            # 更新 Pivota 订单
            query = "SELECT * FROM orders WHERE shopify_order_id = :shopify_order_id"
            from db.database import database
            result = await database.fetch_one(query, {"shopify_order_id": shopify_order_id})
            
            if result:
                order_id = result["order_id"]
                tracking_number = ", ".join(tracking_numbers) if tracking_numbers else None
                
                await mark_order_shipped(order_id, tracking_number)
                await log_order_event(
                    event_type="fulfillment_webhook",
                    order_id=order_id,
                    merchant_id=merchant_id,
                    metadata={
                        "shopify_order_id": shopify_order_id,
                        "tracking_numbers": tracking_numbers
                    }
                )
                logger.info(f"Order {order_id} marked as shipped via webhook")
                
        elif topic == "orders/cancelled":
            # 订单取消
            shopify_order_id = str(data.get("id"))
            cancel_reason = data.get("cancel_reason")
            
            query = "SELECT * FROM orders WHERE shopify_order_id = :shopify_order_id"
            from db.database import database
            result = await database.fetch_one(query, {"shopify_order_id": shopify_order_id})
            
            if result:
                order_id = result["order_id"]
                await update_order_status(order_id, "cancelled")
                await log_order_event(
                    event_type="order_cancelled_webhook",
                    order_id=order_id,
                    merchant_id=merchant_id,
                    metadata={
                        "shopify_order_id": shopify_order_id,
                        "cancel_reason": cancel_reason
                    }
                )
                logger.info(f"Order {order_id} cancelled via webhook: {cancel_reason}")
                
        elif topic == "orders/updated":
            # 订单更新
            shopify_order_id = str(data.get("id"))
            financial_status = data.get("financial_status")
            fulfillment_status = data.get("fulfillment_status")
            
            await log_order_event(
                event_type="order_updated_webhook",
                order_id=f"shopify_{shopify_order_id}",
                merchant_id=merchant_id,
                metadata={
                    "shopify_order_id": shopify_order_id,
                    "financial_status": financial_status,
                    "fulfillment_status": fulfillment_status
                }
            )
            logger.info(f"Shopify order {shopify_order_id} updated")
        
        return {"status": "success", "topic": topic}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error handling Shopify webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Webhook 注册（设置 Shopify webhooks）
# ============================================================================

@router.post("/register/shopify/{merchant_id}")
async def register_shopify_webhooks(
    merchant_id: str,
    callback_base_url: str
):
    """
    为商户注册 Shopify webhooks
    
    Args:
        merchant_id: 商户 ID
        callback_base_url: Webhook 回调的基础 URL（如 https://api.pivota.com）
    """
    try:
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant or not merchant.get("mcp_connected"):
            raise HTTPException(status_code=400, detail="Merchant not connected to Shopify")
        
        shop_domain = merchant.get("mcp_shop_domain")
        access_token = merchant.get("mcp_access_token")
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Missing Shopify credentials")
        
        # 要注册的 webhook topics
        topics = [
            "orders/fulfilled",
            "orders/cancelled",
            "orders/updated"
        ]
        
        registered = []
        import httpx
        
        async with httpx.AsyncClient() as client:
            for topic in topics:
                webhook_data = {
                    "webhook": {
                        "topic": topic,
                        "address": f"{callback_base_url}/webhooks/shopify/{merchant_id}",
                        "format": "json"
                    }
                }
                
                url = f"https://{shop_domain}/admin/api/2024-01/webhooks.json"
                headers = {
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json"
                }
                
                response = await client.post(url, json=webhook_data, headers=headers)
                
                if response.status_code == 201:
                    webhook = response.json()["webhook"]
                    registered.append({
                        "topic": topic,
                        "webhook_id": webhook["id"]
                    })
                    logger.info(f"Registered webhook for {topic} on {shop_domain}")
                else:
                    logger.warning(f"Failed to register webhook for {topic}: {response.text}")
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "registered_webhooks": registered
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error registering Shopify webhooks: {e}")
        raise HTTPException(status_code=500, detail="Failed to register webhooks")


# ============================================================================
# Adyen Webhooks (TODO)
# ============================================================================

@router.post("/adyen")
async def handle_adyen_webhook(request: Request):
    """
    处理 Adyen 支付事件
    TODO: 实现 Adyen webhook 处理
    """
    return {"status": "not_implemented", "message": "Adyen webhooks coming soon"}
