"""
Webhook 处理路由
处理来自 PSP（Stripe/Adyen）和 MCP（Shopify）的事件通知
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional, Dict, Any
import stripe
import os
import hmac
import hashlib
import json
from datetime import datetime

from db.orders import get_order, update_order_status, mark_order_paid, mark_order_shipped
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from config.settings import settings
from utils.logger import logger
from services.shopify_webhook_ingest import verify_shopify_hmac, ingest_shopify_webhook
from services.pcs_evidence_pack_service import create_order_snapshot_evidence_pack

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
            except Exception:
                # Avoid referencing stripe.error.SignatureVerificationError directly
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
                merchant_id = result["merchant_id"]
                
                await mark_order_paid(order_id)
                await log_order_event(
                    event_type="payment_confirmed_webhook",
                    order_id=order_id,
                    merchant_id=merchant_id,
                    metadata={
                        "payment_intent_id": payment_intent_id,
                        "amount": data.get("amount"),
                        "currency": data.get("currency")
                    }
                )
                logger.info(f"Order {order_id} marked as paid via webhook")

                # PCS: freeze order snapshot evidence (best-effort; does not block payment success)
                try:
                    await create_order_snapshot_evidence_pack(order_id, triggered_by="stripe_webhook")
                except Exception as e:
                    logger.warning(f"PCS evidence snapshot failed for {order_id}: {e}")
                
                # 触发 Shopify 订单创建
                from routes.merchant_onboarding_routes import get_merchant_onboarding
                from routes.order_routes import create_shopify_order
                
                merchant = await get_merchant_onboarding(merchant_id)
                store_info = await get_primary_store(merchant_id)
                if merchant and store_info and store_info.get("platform") == "shopify":
                    logger.info(f"🔄 Creating Shopify order for {order_id} after webhook payment confirmation")
                    try:
                        success = await create_shopify_order(order_id)
                        if success:
                            logger.info(f"✅ Shopify order created via webhook for {order_id}")
                        else:
                            logger.error(f"❌ Shopify order creation failed for {order_id}")
                    except Exception as shop_err:
                        logger.error(f"❌ Shopify order creation error: {shop_err}")
                
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
    x_shopify_topic: Optional[str] = Header(None),
    x_shopify_shop_domain: Optional[str] = Header(None),
    x_shopify_webhook_id: Optional[str] = Header(None),
    x_shopify_triggered_at: Optional[str] = Header(None),
):
    """
    处理 Shopify 事件
    
    支持的事件：
    - orders/create, orders/updated, orders/paid, orders/cancelled
    - fulfillments/create, fulfillments/update, orders/fulfilled (legacy)
    - refunds/create (preferred) / orders/refunded (legacy)
    """
    try:
        payload = await request.body()
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # Verify signature (strict in production; must use raw request body).
        is_production = (
            os.getenv("APP_ENV", "").lower() == "production"
            or os.getenv("ENVIRONMENT", "").lower() == "production"
            or bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))
        )
        shopify_secret = getattr(settings, "shopify_client_secret", None) or ""
        if is_production:
            if not shopify_secret:
                logger.error("SHOPIFY_CLIENT_SECRET is not configured; cannot verify Shopify webhooks in production")
                raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
            if not x_shopify_hmac_sha256:
                raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")

        signature_verified = verify_shopify_hmac(
            secret=shopify_secret,
            payload=payload,
            header_hmac_base64=x_shopify_hmac_sha256,
        )
        if is_production and not signature_verified:
            raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

        # Parse event
        data = json.loads(payload)
        topic = x_shopify_topic or "unknown"
        shop_domain = x_shopify_shop_domain or merchant.get("mcp_shop_domain") or "unknown"

        # Anti-cross-tenant poisoning: validate shop_domain matches primary store domain (when available).
        try:
            store_info = await get_primary_store(merchant_id)
            expected_domain = (store_info or {}).get("domain")
            if is_production and expected_domain and x_shopify_shop_domain:
                if expected_domain.strip().lower() != x_shopify_shop_domain.strip().lower():
                    logger.error(
                        "Shopify webhook shop_domain mismatch merchant=%s expected=%s got=%s topic=%s",
                        merchant_id,
                        expected_domain,
                        x_shopify_shop_domain,
                        topic,
                    )
                    raise HTTPException(status_code=403, detail="Shop domain mismatch")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Shop domain validation skipped merchant={merchant_id}: {e}")

        # Occurred_at is best-effort; Shopify may provide X-Shopify-Triggered-At.
        occurred_at: Optional[datetime] = None
        if x_shopify_triggered_at:
            try:
                occurred_at = datetime.fromisoformat(x_shopify_triggered_at.replace("Z", "+00:00"))
            except Exception:
                occurred_at = None

        # Persist event (append-only) with idempotency guard
        try:
            is_dup, _row = await ingest_shopify_webhook(
                merchant_id=merchant_id,
                topic=topic,
                payload=payload,
                shop_domain=shop_domain,
                webhook_id=x_shopify_webhook_id,
                occurred_at=occurred_at,
                signature_verified=signature_verified,
            )
            if is_dup:
                return {"status": "success", "topic": topic, "duplicate": True}
        except Exception as e:
            # Do not skip verification even when persistence fails; we already verified above.
            logger.warning(f"PCS webhook event persistence failed merchant={merchant_id} topic={topic}: {e}")

        logger.info(f"Received Shopify webhook for {merchant_id}: {topic}")

        if topic in ("orders/fulfilled", "fulfillments/create", "fulfillments/update"):
            # 履约更新（订单级 or fulfillment 级）
            tracking_numbers = []

            # fulfillments/* 通常是 fulfillment object，包含 order_id + tracking_numbers
            if topic.startswith("fulfillments/") and data.get("order_id"):
                shopify_order_id = str(data.get("order_id"))
                if isinstance(data.get("tracking_numbers"), list):
                    tracking_numbers.extend([str(x) for x in data.get("tracking_numbers") if x])
                if data.get("tracking_number"):
                    tracking_numbers.append(str(data.get("tracking_number")))
            else:
                # orders/fulfilled 通常是 order object，包含 fulfillments[]
                shopify_order_id = str(data.get("id"))
                for fulfillment in data.get("fulfillments", []) or []:
                    tracking_numbers.extend(fulfillment.get("tracking_numbers", []) or [])

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

        elif topic in ("orders/create", "orders/paid"):
            shopify_order_id = str(data.get("id"))
            from db.database import database
            result = await database.fetch_one(
                "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                {"shopify_order_id": shopify_order_id},
            )
            pivota_order_id = result["order_id"] if result else f"shopify_{shopify_order_id}"
            await log_order_event(
                event_type="shopify_order_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"shopify_order_id": shopify_order_id, "topic": topic},
            )

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

            from db.database import database
            result = await database.fetch_one(
                "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                {"shopify_order_id": shopify_order_id},
            )
            pivota_order_id = result["order_id"] if result else f"shopify_{shopify_order_id}"

            await log_order_event(
                event_type="order_updated_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "shopify_order_id": shopify_order_id,
                    "financial_status": financial_status,
                    "fulfillment_status": fulfillment_status
                }
            )
            logger.info(f"Shopify order {shopify_order_id} updated")

        elif topic in ("refunds/create", "orders/refunded"):
            shopify_order_id = str((data.get("order_id") or {}).get("id") or data.get("order_id") or data.get("id") or "unknown")
            await log_order_event(
                event_type="refund_webhook",
                order_id=f"shopify_{shopify_order_id}",
                merchant_id=merchant_id,
                metadata={"topic": topic, "shopify_order_id": shopify_order_id},
            )

        elif topic == "tender_transactions/create":
            await log_order_event(
                event_type="tender_transaction_webhook",
                order_id=f"shopify_{data.get('order_id') or 'unknown'}",
                merchant_id=merchant_id,
                metadata={"topic": topic, "payload_keys": list((data or {}).keys())},
            )

        elif topic in ("disputes/create", "disputes/update"):
            platform_order_id = data.get("order_id") or data.get("orderId") or "unknown"
            await log_order_event(
                event_type="dispute_webhook",
                order_id=f"shopify_{platform_order_id}",
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "shopify_order_id": platform_order_id,
                    "dispute_id": data.get("id"),
                    "status": data.get("status"),
                    "reason": data.get("reason"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                },
            )

        elif topic in ("customers/data_request", "customers/redact", "shop/redact"):
            await log_order_event(
                event_type="gdpr_webhook",
                order_id=f"gdpr_{merchant_id}",
                merchant_id=merchant_id,
                metadata={"topic": topic, "payload_keys": list((data or {}).keys())},
            )
        
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
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        store_info = await get_primary_store(merchant_id)
        if not store_info or store_info.get("platform") != "shopify":
            raise HTTPException(status_code=400, detail="Primary store is not Shopify")

        shop_domain = store_info.get("domain")
        access_token = store_info.get("api_key")
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Missing Shopify credentials")
        
        # 要注册的 webhook topics
        topics = [
            # Orders
            "orders/create",
            "orders/updated",
            "orders/paid",
            "orders/cancelled",
            # Fulfillments
            "fulfillments/create",
            "fulfillments/update",
            # Legacy support
            "orders/fulfilled",
            # Refunds (preferred)
            "refunds/create",
            # Money movement
            "tender_transactions/create",
            # Disputes (Shopify Payments)
            "disputes/create",
            "disputes/update",
            # GDPR compliance
            "customers/data_request",
            "customers/redact",
            "shop/redact",
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
                
                url = f"https://{shop_domain}/admin/api/2024-07/webhooks.json"
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
