"""
Platform Refund Webhook Handler
Processes refund webhooks from various e-commerce platforms
"""
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional
import hmac
import hashlib
import json

from db.database import database
from db.orders import get_order
from db.products import log_order_event
from services.platform_refund_adapter import platform_refund_adapter, PlatformRefundEvent
from services.refund_service import refund_service
from config.feature_flags import is_feature_enabled
from utils.logger import logger

router = APIRouter(prefix="/webhooks/refunds", tags=["refund-webhooks"])


async def process_platform_refund(
    refund_event: PlatformRefundEvent,
    merchant_id: str
) -> dict:
    """
    Process a normalized refund event from a platform
    
    Args:
        refund_event: Normalized refund event
        merchant_id: Merchant ID
        
    Returns:
        Processing result
    """
    try:
        # Find the order in our system
        query = """
        SELECT order_id, merchant_id, total, total_refunded, payment_status
        FROM orders
        WHERE merchant_id = :merchant_id
        AND (
            shopify_order_id = :platform_order_id
            OR order_id = :platform_order_id
        )
        LIMIT 1
        """
        
        order = await database.fetch_one(query, {
            "merchant_id": merchant_id,
            "platform_order_id": refund_event.platform_order_id
        })
        
        if not order:
            logger.warning(
                f"Order not found for platform refund: "
                f"{refund_event.platform_type} order {refund_event.platform_order_id}"
            )
            return {
                "status": "order_not_found",
                "message": f"Order {refund_event.platform_order_id} not found in system"
            }
        
        order_id = order["order_id"]
        
        # Check if this refund has already been processed
        check_existing = """
        SELECT refund_id FROM refund_records
        WHERE platform_type = :platform_type
        AND platform_refund_id = :platform_refund_id
        """
        
        existing_refund = await database.fetch_one(check_existing, {
            "platform_type": refund_event.platform_type,
            "platform_refund_id": refund_event.platform_refund_id
        })
        
        if existing_refund:
            logger.info(
                f"Refund already processed: {existing_refund['refund_id']}"
            )
            return {
                "status": "duplicate",
                "refund_id": existing_refund["refund_id"],
                "message": "Refund already processed"
            }
        
        # Create refund record directly (bypass PSP since it's already refunded on platform)
        import secrets
        refund_id = f"REF_{secrets.token_hex(8).upper()}"
        
        insert_refund = """
        INSERT INTO refund_records (
            refund_id, order_id, merchant_id, amount, currency,
            reason, source, status,
            platform_type, platform_refund_id,
            raw_payload, created_at, processed_at
        ) VALUES (
            :refund_id, :order_id, :merchant_id, :amount, :currency,
            :reason, 'platform_webhook', 'completed',
            :platform_type, :platform_refund_id,
            :raw_payload, NOW(), NOW()
        )
        """
        
        await database.execute(insert_refund, {
            "refund_id": refund_id,
            "order_id": order_id,
            "merchant_id": merchant_id,
            "amount": refund_event.amount,
            "currency": refund_event.currency,
            "reason": refund_event.reason,
            "platform_type": refund_event.platform_type,
            "platform_refund_id": refund_event.platform_refund_id,
            "raw_payload": json.dumps(refund_event.raw_event)
        })
        
        # Update order total_refunded
        update_order = """
        UPDATE orders
        SET 
            total_refunded = COALESCE(total_refunded, 0) + :amount,
            payment_status = CASE 
                WHEN COALESCE(total_refunded, 0) + :amount >= total THEN 'refunded'
                ELSE 'partially_refunded'
            END,
            updated_at = NOW()
        WHERE order_id = :order_id
        """
        
        await database.execute(update_order, {
            "amount": refund_event.amount,
            "order_id": order_id
        })
        
        # Log event
        await log_order_event(
            event_type="platform_refund_webhook",
            order_id=order_id,
            merchant_id=merchant_id,
            metadata={
                "refund_id": refund_id,
                "platform_type": refund_event.platform_type,
                "platform_refund_id": refund_event.platform_refund_id,
                "amount": refund_event.amount,
                "currency": refund_event.currency
            }
        )
        
        logger.info(
            f"Platform refund processed: {refund_id} for order {order_id} "
            f"({refund_event.platform_type})"
        )
        
        return {
            "status": "success",
            "refund_id": refund_id,
            "order_id": order_id,
            "amount": refund_event.amount
        }
        
    except Exception as e:
        logger.error(f"Failed to process platform refund: {e}")
        raise


@router.post("/shopify/{merchant_id}")
async def handle_shopify_refund_webhook(
    merchant_id: str,
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None)
):
    """
    Handle Shopify orders/refunded webhook
    
    Topic: orders/refunded
    """
    # Check feature flag
    if not is_feature_enabled("enable_platform_webhook_refund"):
        return {"status": "disabled", "message": "Platform webhook refunds are disabled"}
    
    try:
        payload = await request.body()
        event_data = json.loads(payload)
        
        # Verify webhook signature (TODO: get merchant's webhook secret)
        # For now, we'll skip verification in development
        
        logger.info(f"Received Shopify refund webhook for merchant {merchant_id}")
        
        # Normalize the refund event
        refund_event = platform_refund_adapter.normalize_refund_event(
            "shopify",
            event_data
        )
        
        # Process the refund
        result = await process_platform_refund(refund_event, merchant_id)
        
        return result
        
    except Exception as e:
        logger.error(f"Error handling Shopify refund webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/woocommerce/{merchant_id}")
async def handle_woocommerce_refund_webhook(
    merchant_id: str,
    request: Request
):
    """
    Handle WooCommerce order.refunded webhook
    """
    # Check feature flag
    if not is_feature_enabled("enable_platform_webhook_refund"):
        return {"status": "disabled", "message": "Platform webhook refunds are disabled"}
    
    try:
        event_data = await request.json()
        
        logger.info(f"Received WooCommerce refund webhook for merchant {merchant_id}")
        
        # Normalize the refund event
        refund_event = platform_refund_adapter.normalize_refund_event(
            "woocommerce",
            event_data
        )
        
        # Process the refund
        result = await process_platform_refund(refund_event, merchant_id)
        
        return result
        
    except Exception as e:
        logger.error(f"Error handling WooCommerce refund webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/amazon/{merchant_id}")
async def handle_amazon_refund_notification(
    merchant_id: str,
    request: Request
):
    """
    Handle Amazon SP-API REFUND_EVENT notification
    """
    # Check feature flag
    if not is_feature_enabled("enable_platform_webhook_refund"):
        return {"status": "disabled", "message": "Platform webhook refunds are disabled"}
    
    try:
        event_data = await request.json()
        
        logger.info(f"Received Amazon refund notification for merchant {merchant_id}")
        
        # Normalize the refund event
        refund_event = platform_refund_adapter.normalize_refund_event(
            "amazon",
            event_data
        )
        
        # Process the refund
        result = await process_platform_refund(refund_event, merchant_id)
        
        return result
        
    except Exception as e:
        logger.error(f"Error handling Amazon refund notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))

