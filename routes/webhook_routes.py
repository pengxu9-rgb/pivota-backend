"""
Webhook 处理路由
处理来自 PSP（Stripe/Adyen）和 MCP（Shopify）的事件通知
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Header, Response
from typing import Optional, Dict, Any
from urllib.parse import urlparse
import stripe
import os
import hmac
import hashlib
import json
import socket
from datetime import datetime
from decimal import Decimal

from db.orders import get_order, update_order_status, mark_order_paid, mark_order_shipped
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from config.settings import settings
from utils.logger import logger
from services.shopify_webhook_ingest import verify_shopify_hmac, ingest_shopify_webhook
from services.pcs_evidence_pack_service import create_order_snapshot_evidence_pack
from routes.reviews_invitation_issuer import (
    SendInvitationEmailFromOrderRequest,
    _internal_key as _reviews_invitation_internal_key,
    send_invitation_email_from_order,
    _invitation_send_delay_seconds as _reviews_invitation_send_delay_seconds,
    enqueue_invitation_email_send_job_from_order,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

def _reviews_invitation_auto_send_on_shopify_fulfillment_enabled() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_AUTO_SEND_ON_SHOPIFY_FULFILLMENT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _stripe_minor_unit_factor(currency: Optional[str]) -> Decimal:
    """
    Stripe reports amounts in the smallest currency unit.
    Default exponent=2 (factor=100), with common exceptions handled.
    """
    c = (currency or "").strip().lower()
    if not c:
        return Decimal("100")

    zero_decimal = {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
    three_decimal = {"bhd", "jod", "kwd", "omr", "tnd"}

    if c in zero_decimal:
        return Decimal("1")
    if c in three_decimal:
        return Decimal("1000")
    return Decimal("100")


def _canonicalize_shop_domain(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower()
        return host or None
    except Exception:
        return raw.lower()


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
    - charge.dispute.*: 争议/拒付（chargeback）信号（best-effort 记录，不自动变更订单状态）
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
            currency = (data.get("currency") or "").strip().lower() or None
            
            query = "SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id"
            from db.database import database
            result = await database.fetch_one(query, {"payment_intent_id": payment_intent_id})
            
            if result:
                order_id = result["order_id"]
                # Stripe's charge.amount_refunded is cumulative (not delta). Use it to converge
                # order state without double-counting if we also processed the refund internally.
                try:
                    order_total = Decimal(str(result.get("total") or "0"))
                except Exception:
                    order_total = Decimal("0")
                try:
                    existing_total_refunded = Decimal(str(result.get("total_refunded") or "0"))
                except Exception:
                    existing_total_refunded = Decimal("0")
                try:
                    refunded_minor = Decimal(str(refund_amount)) if refund_amount is not None else Decimal("0")
                except Exception:
                    refunded_minor = Decimal("0")

                factor = _stripe_minor_unit_factor(currency or str(result.get("currency") or ""))
                try:
                    refunded_total = refunded_minor / factor
                except Exception:
                    refunded_total = Decimal("0")

                next_total_refunded = max(existing_total_refunded, refunded_total)
                if order_total > Decimal("0") and next_total_refunded < order_total:
                    next_status = "partially_refunded"
                else:
                    next_status = "refunded"

                existing_meta = result.get("metadata") or {}
                if not isinstance(existing_meta, dict):
                    existing_meta = {}
                await update_order_status(
                    order_id,
                    next_status,
                    payment_status=next_status,
                    total_refunded=next_total_refunded,
                    metadata={
                        **existing_meta,
                        "stripe_charge_refunded": {
                            "charge_id": charge_id,
                            "amount_refunded_minor": refund_amount,
                            "currency": currency or str(result.get("currency") or ""),
                            "received_at": datetime.now().isoformat(),
                        },
                    },
                )
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

        elif event_type and str(event_type).startswith("charge.dispute."):
            # Stripe dispute/chargeback signals.
            # Do not mutate order state here; treat as risk/ops signal and persist best-effort.
            try:
                from services.dispute_records_service import upsert_stripe_dispute_record_best_effort
                dispute_payload = {}
                if isinstance(data, dict):
                    dispute_payload = data
                elif hasattr(data, "to_dict"):
                    try:
                        dispute_payload = data.to_dict()
                    except Exception:
                        dispute_payload = {}

                await upsert_stripe_dispute_record_best_effort(
                    dispute_payload,
                    event_type=str(event_type),
                )
            except Exception:
                pass
        
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
    background_tasks: BackgroundTasks,
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
	    - returns/* (Shopify Returns; topic availability varies by shop/app)
	    """
    try:
        payload = await request.body()
        
        # 获取商户信息
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # Verify signature (strict in production; must use raw request body).
        instance_id = socket.gethostname()
        is_production = (
            os.getenv("APP_ENV", "").lower() == "production"
            or os.getenv("ENVIRONMENT", "").lower() == "production"
            or bool(os.getenv("RAILWAY_GIT_COMMIT_SHA"))
        )
        merchant_secret = merchant.get("shopify_webhook_secret")
        app_secret = getattr(settings, "shopify_client_secret", None)
        shopify_secret = merchant_secret or app_secret or ""
        secret_source = "merchant" if merchant_secret else ("app_env" if app_secret else "none")
        secret_len = len(shopify_secret) if shopify_secret else 0
        secret_sha256_prefix = (
            hashlib.sha256(shopify_secret.encode("utf-8")).hexdigest()[:10]
            if shopify_secret
            else None
        )

        debug_meta = {
            "merchant_id": merchant_id,
            "instance": instance_id,
            "topic": x_shopify_topic or "unknown",
            "webhook_id": x_shopify_webhook_id,
            "shop_domain": _canonicalize_shop_domain(x_shopify_shop_domain) if x_shopify_shop_domain else None,
            "has_shop_domain_header": bool(x_shopify_shop_domain),
            "has_hmac_header": bool(x_shopify_hmac_sha256),
            "has_webhook_id_header": bool(x_shopify_webhook_id),
            "content_length": request.headers.get("content-length"),
            "user_agent": request.headers.get("user-agent"),
            "secret_source": secret_source,
            "secret_len": secret_len,
            "secret_sha256_prefix": secret_sha256_prefix,
        }
        if is_production:
            if not x_shopify_shop_domain:
                logger.warning("Shopify webhook rejected: missing shop domain header %s", debug_meta)
                raise HTTPException(status_code=401, detail="Missing Shopify shop domain")
            if not shopify_secret:
                logger.error("SHOPIFY_CLIENT_SECRET is not configured; cannot verify Shopify webhooks in production")
                raise HTTPException(status_code=500, detail="Shopify webhook verification not configured")
            if not x_shopify_hmac_sha256:
                logger.warning("Shopify webhook rejected: missing HMAC header %s", debug_meta)
                raise HTTPException(status_code=401, detail="Missing Shopify webhook signature")

        signature_verified = verify_shopify_hmac(
            secret=shopify_secret,
            payload=payload,
            header_hmac_base64=x_shopify_hmac_sha256,
        )
        if is_production and not signature_verified:
            # This commonly indicates env drift across instances (different SHOPIFY_CLIENT_SECRET).
            meta = dict(debug_meta)
            if x_shopify_hmac_sha256:
                meta["hmac_prefix"] = x_shopify_hmac_sha256[:10]
            logger.warning("Shopify webhook rejected: invalid signature %s", meta)
            raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

        # Parse event
        data = json.loads(payload)
        topic = x_shopify_topic or "unknown"
        shop_domain = x_shopify_shop_domain or merchant.get("mcp_shop_domain") or "unknown"

        # Anti-cross-tenant poisoning: validate shop_domain matches a connected Shopify store domain.
        #
        # NOTE: Do NOT rely on "primary store" being Shopify. Many merchants can have multiple
        # stores/platforms; primary is merely the newest connected store in our DB.
        try:
            stores = await get_merchant_active_stores(merchant_id)
            allowed_domains = set()
            for store in stores or []:
                if (store.get("platform") or "").lower() != "shopify":
                    continue
                dom = _canonicalize_shop_domain(store.get("domain"))
                if dom:
                    allowed_domains.add(dom)

            if is_production:
                got_canon = _canonicalize_shop_domain(x_shopify_shop_domain)
                if not allowed_domains:
                    logger.error(
                        "Shopify webhook rejected: no Shopify store configured merchant=%s topic=%s got=%s",
                        merchant_id,
                        topic,
                        got_canon,
                    )
                    raise HTTPException(status_code=400, detail="No Shopify store connected")
                if got_canon and got_canon not in allowed_domains:
                    logger.error(
                        "Shopify webhook shop_domain mismatch merchant=%s allowed=%s got=%s topic=%s",
                        merchant_id,
                        sorted(list(allowed_domains)),
                        got_canon,
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
            # In production, fail so Shopify will retry and we don't lose the audit trail.
            logger.warning(f"PCS webhook event persistence failed merchant={merchant_id} topic={topic}: {e}")
            if is_production:
                raise HTTPException(status_code=500, detail="Webhook event persistence unavailable")

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

                if _reviews_invitation_auto_send_on_shopify_fulfillment_enabled():
                    async def send_review_invitation_task() -> None:
                        try:
                            internal_key = (_reviews_invitation_internal_key() or "").strip()
                            if not internal_key:
                                logger.info("Reviews invitation issuer disabled; skip send.")
                                return
                            if _reviews_invitation_send_delay_seconds() > 0:
                                ok = await enqueue_invitation_email_send_job_from_order(
                                    merchant_id=merchant_id,
                                    order_id=order_id,
                                )
                                logger.info(f"Reviews invitation job enqueued for order {order_id} ok={ok}")
                                return
                            req = SendInvitationEmailFromOrderRequest(
                                merchant_id=merchant_id,
                                order_id=order_id,
                                ttl_seconds=7 * 24 * 3600,
                            )
                            await send_invitation_email_from_order(
                                body=req,
                                response=Response(),
                                x_internal_key=internal_key,
                            )
                            logger.info(f"Reviews invitation email dispatched for order {order_id} (shopify webhook)")
                        except HTTPException as e:
                            logger.warning(
                                f"Reviews invitation skipped for order {order_id} (shopify webhook): {e.detail}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Reviews invitation error for order {order_id} (shopify webhook): {e}"
                            )

                    background_tasks.add_task(send_review_invitation_task)

        elif topic in ("orders/create", "orders/paid"):
            # Best-effort linkage: map Shopify order id -> Pivota order by orders.shopify_order_id
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

            pivota = await database.fetch_one(
                "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                {"shopify_order_id": shopify_order_id},
            )
            pivota_order_id = pivota["order_id"] if pivota else f"shopify_{shopify_order_id}"

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

            # Compatibility: some shops/apps only reliably emit orders/updated when fulfillment changes.
            # If Shopify indicates fulfilled here, converge Pivota order state to shipped.
            try:
                raw_fulfillment_status = str(fulfillment_status or "").strip().lower()
                if raw_fulfillment_status == "fulfilled" and pivota:
                    current = await database.fetch_one(
                        "SELECT order_id, fulfillment_status, tracking_number FROM orders WHERE shopify_order_id = :shopify_order_id",
                        {"shopify_order_id": shopify_order_id},
                    )
                    if current:
                        current_status = str(current.get("fulfillment_status") or "").strip().lower()
                        if current_status not in {"shipped", "delivered"}:
                            tracking_numbers: list[str] = []
                            carrier: Optional[str] = None
                            for fulfillment in (data.get("fulfillments") or []) or []:
                                if isinstance(fulfillment, dict):
                                    if not carrier:
                                        carrier = fulfillment.get("tracking_company") or fulfillment.get("tracking_company_name")
                                    if isinstance(fulfillment.get("tracking_numbers"), list):
                                        tracking_numbers.extend(
                                            [str(x) for x in (fulfillment.get("tracking_numbers") or []) if x]
                                        )
                                    if fulfillment.get("tracking_number"):
                                        tracking_numbers.append(str(fulfillment.get("tracking_number")))
                                    if fulfillment.get("tracking_info") and isinstance(fulfillment.get("tracking_info"), dict):
                                        ti = fulfillment.get("tracking_info") or {}
                                        if not carrier:
                                            carrier = ti.get("company") or ti.get("tracking_company") or ti.get("carrier")
                                        if ti.get("number"):
                                            tracking_numbers.append(str(ti.get("number")))
                            tracking_number = ", ".join(dict.fromkeys(tracking_numbers)) if tracking_numbers else None
                            await mark_order_shipped(
                                str(current.get("order_id")),
                                tracking_number,
                                carrier=carrier,
                            )
                            await log_order_event(
                                event_type="fulfillment_via_order_updated_webhook",
                                order_id=str(current.get("order_id")),
                                merchant_id=merchant_id,
                                metadata={
                                    "shopify_order_id": shopify_order_id,
                                    "tracking_numbers": tracking_numbers,
                                    "carrier": carrier,
                                },
                            )
                            logger.info(f"Order {current.get('order_id')} marked as shipped via orders/updated webhook")
            except Exception as e:
                logger.warning(f"orders/updated fulfillment convergence skipped for shopify_order_id={shopify_order_id}: {e}")

        elif topic in ("refunds/create", "orders/refunded"):
            platform_order_id = str(data.get("order_id") or data.get("id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = (
                pivota["order_id"]
                if pivota
                else (f"shopify_{platform_order_id}" if platform_order_id else f"shopify_refund_{datetime.utcnow().timestamp()}")
            )

            await log_order_event(
                event_type="refund_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"topic": topic, "shopify_order_id": platform_order_id or None},
            )

            # Best-effort normalize using existing adapter.
            try:
                from routes.refund_webhook_routes import process_platform_refund
                from services.platform_refund_adapter import platform_refund_adapter

                refund_event = platform_refund_adapter.normalize_refund_event("shopify", data)
                result = await process_platform_refund(refund_event, merchant_id)
                logger.info(f"Processed Shopify refund webhook for merchant {merchant_id}: {result.get('status')}")
            except Exception as e:
                logger.warning(f"Failed to process Shopify refund webhook merchant={merchant_id}: {e}")

        elif topic == "tender_transactions/create":
            # Money movement signal (payment/refund). Best-effort: record as immutable event; do not assume state transitions.
            platform_order_id = str(data.get("order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = pivota["order_id"] if pivota else (f"shopify_{platform_order_id}" if platform_order_id else f"shopify_tender_{datetime.utcnow().timestamp()}")
            await log_order_event(
                event_type="tender_transaction_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "shopify_order_id": platform_order_id or None,
                    "kind": data.get("kind"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                    "status": data.get("status"),
                    "tender_transaction_id": data.get("id"),
                },
            )

        elif topic in ("disputes/create", "disputes/update"):
            # Dispute signals are critical for tiering/risk; store event and best-effort link to order_id.
            platform_order_id = str(data.get("order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = pivota["order_id"] if pivota else (f"shopify_{platform_order_id}" if platform_order_id else "shopify_dispute_unknown")
            await log_order_event(
                event_type="dispute_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={
                    "topic": topic,
                    "shopify_order_id": platform_order_id or None,
                    "dispute_id": data.get("id"),
                    "status": data.get("status"),
                    "reason": data.get("reason"),
                    "amount": data.get("amount"),
                    "currency": data.get("currency"),
                },
            )

            # MVP measurement scaffolding: dispute opened/resolved (metadata-only).
            try:
                from mvp.constants import EVENT_DISPUTE_OPENED, EVENT_DISPUTE_RESOLVED, SURFACE_BACKEND
                from mvp.events import emit_best_effort

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                evt = EVENT_DISPUTE_RESOLVED if is_resolved else EVENT_DISPUTE_OPENED

                emit_best_effort(
                    event_type=evt,
                    payload={
                        "merchant_id": merchant_id,
                        "order_id": pivota_order_id,
                        "shopify_order_id": platform_order_id or None,
                        "dispute_id": data.get("id"),
                        "status": data.get("status"),
                        "reason": data.get("reason"),
                        "amount": data.get("amount"),
                        "currency": data.get("currency"),
                    },
                    merchant_id=merchant_id,
                    geo=None,
                    surface=SURFACE_BACKEND,
                    adapter="shopify_webhook",
                    risk_tier="unknown",
                    idempotency_key=str(data.get("id") or "") or None,
                )
            except Exception:
                pass

            # PCS: best-effort dispute evidence pack builder (draft on open, frozen on resolution).
            try:
                from services.pcs_evidence_pack_service import create_dispute_evidence_pack

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                await create_dispute_evidence_pack(
                    merchant_id=str(merchant_id),
                    dispute_ref=str(data.get("id") or ""),
                    order_id=str(pivota_order_id) if pivota_order_id else None,
                    dispute_payload=dict(data or {}),
                    status="frozen" if is_resolved else "draft",
                    triggered_by=f"shopify_webhook:{topic}",
                )
            except Exception:
                pass

            # MVP ledger event (best-effort): dispute timeline entry.
            try:
                from mvp.ledger_events import emit_ledger_event_best_effort

                raw_status = (data.get("status") or "").lower()
                is_resolved = raw_status in {"won", "lost", "resolved", "closed"}
                emit_ledger_event_best_effort(
                    merchant_id=str(merchant_id),
                    event_type="dispute_resolved" if is_resolved else "dispute_opened",
                    order_id=str(pivota_order_id) if pivota_order_id else None,
                    source={"type": "shopify_webhook", "external_event_id": str(data.get("id") or "")},
                    amount={
                        "value": float(data.get("amount") or 0.0),
                        "currency": str(data.get("currency") or "USD"),
                    }
                    if (data.get("amount") is not None)
                    else None,
                    refs={"shopify_order_id": platform_order_id or None},
                    geo=None,
                    surface="backend",
                    adapter="shopify_webhook",
                    risk_tier="unknown",
                    idempotency_key=str(data.get("id") or "") or None,
                    signature_verified=True,
                )
            except Exception:
                pass

            # Upsert normalized dispute record for ops visibility (best-effort).
            try:
                from services.dispute_records_service import upsert_shopify_dispute_record_best_effort

                await upsert_shopify_dispute_record_best_effort(
                    merchant_id=str(merchant_id),
                    payload=dict(data or {}),
                    topic=str(topic),
                )
            except Exception:
                pass

        elif topic and topic.startswith("returns/"):
            # Returns/RMA signals (if enabled). Best-effort: record and upsert minimal return record.
            platform_order_id = str(data.get("order_id") or data.get("shopify_order_id") or "")
            from db.database import database

            pivota = None
            if platform_order_id:
                pivota = await database.fetch_one(
                    "SELECT order_id FROM orders WHERE shopify_order_id = :shopify_order_id",
                    {"shopify_order_id": platform_order_id},
                )
            pivota_order_id = (
                pivota["order_id"]
                if pivota
                else (f"shopify_{platform_order_id}" if platform_order_id else "shopify_return_unknown")
            )

            await log_order_event(
                event_type="return_webhook",
                order_id=pivota_order_id,
                merchant_id=merchant_id,
                metadata={"topic": topic, "shopify_order_id": platform_order_id or None},
            )

            try:
                from services.return_records_service import upsert_shopify_return_record_best_effort

                await upsert_shopify_return_record_best_effort(
                    merchant_id=str(merchant_id),
                    payload=dict(data or {}),
                    topic=str(topic),
                )
            except Exception:
                pass

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

        stores = await get_merchant_active_stores(merchant_id)
        shopify_store = None
        for store in stores or []:
            if (store.get("platform") or "").lower() != "shopify":
                continue
            if store.get("domain") and store.get("api_key"):
                shopify_store = store
                break
        if not shopify_store:
            raise HTTPException(status_code=400, detail="No Shopify store connected")

        shop_domain = shopify_store.get("domain")
        access_token = shopify_store.get("api_key")
        
        if not shop_domain or not access_token:
            raise HTTPException(status_code=400, detail="Missing Shopify credentials")

        shop_domain_canon = _canonicalize_shop_domain(shop_domain)
        if not shop_domain_canon:
            raise HTTPException(status_code=400, detail="Invalid Shopify store domain")
        
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
            # Money movement (refund funds settled / payment settled signals)
            "tender_transactions/create",
            # Disputes (Shopify Payments)
            "disputes/create",
            "disputes/update",
            # Returns (if enabled by shop/app)
            "returns/create",
            "returns/update",
            # GDPR compliance (required when accessing customer/order data)
            "customers/data_request",
            "customers/redact",
            "shop/redact",
        ]
        
        registered = []
        already_exists = []
        failed = []
        import httpx
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            for topic in topics:
                webhook_data = {
                    "webhook": {
                        "topic": topic,
                        "address": f"{callback_base_url.rstrip('/')}/webhooks/shopify/{merchant_id}",
                        "format": "json"
                    }
                }
                
                url = f"https://{shop_domain_canon}/admin/api/2024-07/webhooks.json"
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
                    # Common idempotency response: address already taken
                    if response.status_code == 422:
                        try:
                            body = response.json() or {}
                            errors = body.get("errors") or {}
                            addr_errs = errors.get("address") or []
                            if isinstance(addr_errs, list) and any("already" in str(x).lower() for x in addr_errs):
                                already_exists.append(topic)
                                continue
                        except Exception:
                            pass

                    failed.append(
                        {
                            "topic": topic,
                            "status_code": response.status_code,
                            "body": (response.text or "")[:800],
                        }
                    )
                    logger.warning(f"Failed to register webhook for {topic}: {response.status_code} {response.text}")
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "registered_webhooks": registered,
            "already_exists": already_exists,
            "failed_webhooks": failed,
            "summary": {
                "requested": len(topics),
                "created": len(registered),
                "already_exists": len(already_exists),
                "failed": len(failed),
            },
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
