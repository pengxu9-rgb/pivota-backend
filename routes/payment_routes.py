"""
Payment Routes
API endpoints for payment processing and orchestration
"""

import hashlib
import logging
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, status, Request, BackgroundTasks
from pydantic import BaseModel

from config.platform import is_production
from orchestrator.payment_orchestrator import payment_orchestrator, OrchestrationResult
from dashboard.core import dashboard_core, User
from services.psp_payment_finalizer import finalize_payment_success
from utils.auth import get_current_user

logger = logging.getLogger("payment_routes")
router = APIRouter(prefix="/api/payments", tags=["payments"])


def _unsigned_webhook_is_fatal() -> bool:
    """With no CHECKOUT_WEBHOOK_SECRET configured, production refuses the event.

    Hoisted out of the route body so it is testable without driving a whole
    webhook request — that inaccessibility is why this gate had no parity
    coverage. Off production the caller logs and falls through, which is the
    pre-existing behaviour.
    """
    return (
        os.getenv("ENVIRONMENT", "").lower() == "production"
        or is_production()
    )

# Request/Response Models
class PaymentRequest(BaseModel):
    merchant_id: str
    agent_id: Optional[str] = None
    customer_email: str
    total_amount: float
    currency: str = "USD"
    payment_method: str = "card"
    items: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}
    preferred_psp: Optional[str] = None

class PaymentResponse(BaseModel):
    success: bool
    order_id: str
    payment_id: Optional[str]
    psp_used: Optional[str]
    transaction_id: Optional[str]
    amount: float
    currency: str
    fees: float
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = {}

class RetryRequest(BaseModel):
    order_id: str
    retry_count: int = 1

# Payment processing endpoints
@router.post("/process", response_model=PaymentResponse)
async def process_payment(
    request: PaymentRequest,
    current_user: User = Depends(get_current_user)
):
    """Process a payment for an order"""
    try:
        # Validate user permissions
        if current_user.role.value not in ["pivota_admin", "pivota_operator", "merchant", "agent"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="Insufficient permissions for payment processing"
            )
        
        # Convert request to order data
        order_data = {
            "id": f"order_{datetime.now().timestamp()}",
            "merchant_id": request.merchant_id,
            "agent_id": request.agent_id,
            "customer_email": request.customer_email,
            "total_amount": request.total_amount,
            "currency": request.currency,
            "payment_method": request.payment_method,
            "items": request.items,
            "metadata": request.metadata
        }
        
        logger.info(f"Processing payment for order {order_data['id']} by user {current_user.username}")
        
        # Process payment through orchestrator
        result = await payment_orchestrator.process_order_payment(
            order_data, 
            request.preferred_psp
        )
        
        return PaymentResponse(
            success=result.success,
            order_id=result.order_id,
            payment_id=result.payment_id,
            psp_used=result.psp_used,
            transaction_id=result.transaction_id,
            amount=result.amount,
            currency=result.currency,
            fees=result.fees,
            error_message=result.error_message,
            metadata=result.metadata or {}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment processing failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {str(e)}"
        )

@router.post("/retry", response_model=PaymentResponse)
async def retry_payment(
    request: RetryRequest,
    current_user: User = Depends(get_current_user)
):
    """Retry a failed payment"""
    try:
        # Validate user permissions
        if current_user.role.value not in ["pivota_admin", "pivota_operator"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin/operator can retry payments"
            )
        
        logger.info(f"Retrying payment for order {request.order_id} by user {current_user.username}")
        
        # Retry payment
        result = await payment_orchestrator.retry_failed_payment(
            request.order_id,
            request.retry_count
        )
        
        return PaymentResponse(
            success=result.success,
            order_id=result.order_id,
            payment_id=result.payment_id,
            psp_used=result.psp_used,
            transaction_id=result.transaction_id,
            amount=result.amount,
            currency=result.currency,
            fees=result.fees,
            error_message=result.error_message,
            metadata=result.metadata or {}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Payment retry failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment retry failed: {str(e)}"
        )

@router.get("/status")
async def get_payment_status(
    current_user: User = Depends(get_current_user)
):
    """Get payment system status"""
    try:
        status = await payment_orchestrator.get_orchestration_status()
        return status
    except Exception as e:
        logger.error(f"Failed to get payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get payment status: {str(e)}"
        )

@router.get("/psps")
async def get_available_psps(
    current_user: User = Depends(get_current_user)
):
    """Get available payment service providers"""
    try:
        from psp.connectors import psp_manager
        psp_status = await psp_manager.get_psp_status()
        
        return {
            "available_psps": list(psp_status.keys()),
            "psp_status": psp_status,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to get PSP status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get PSP status: {str(e)}"
        )

@router.get("/orders/{order_id}")
async def get_order_payment_status(
    order_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get payment status for a specific order"""
    try:
        # Check if order exists
        order = dashboard_core.orders.get(order_id)
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Order not found"
            )
        
        # Check user permissions
        if current_user.role.value == "merchant" and order.merchant_id != current_user.entity_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this order"
            )
        
        if current_user.role.value == "agent" and order.agent_id != current_user.entity_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this order"
            )
        
        # Get payment information
        payments = [p for p in dashboard_core.payments.values() if p.order_id == order_id]
        
        return {
            "order_id": order_id,
            "order_status": order.status.value,
            "total_amount": order.total_amount,
            "currency": order.currency,
            "payments": [
                {
                    "payment_id": p.id,
                    "amount": p.amount,
                    "psp": p.psp.value,
                    "status": p.status,
                    "transaction_id": p.transaction_id,
                    "fees": p.fees,
                    "created_at": p.created_at.isoformat()
                }
                for p in payments
            ],
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get order payment status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get order payment status: {str(e)}"
        )


@router.post("/webhooks/checkout")
async def checkout_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    """
    Checkout.com webhook to finalize successful payments.
    
    Features:
    - HMAC signature verification (if CHECKOUT_WEBHOOK_SECRET is set)
    - Idempotency guard (prevents duplicate processing)
    - Event persistence and auditing
    - Retry-safe (safe to re-receive same event)
    
    Headers:
    - Cko-Signature: Checkout.com webhook signature
    
    Notes:
    - We rely on the 'reference' or 'metadata.order_id' to identify the order.
    - Event ID is used for idempotency (from data.id or id field)
    """
    import os
    from services.webhook_service import WebhookService, process_webhook_with_idempotency
    
    try:
        # Get raw body for signature verification
        body = await request.body()
        # Parse JSON from raw body
        import json as json_module
        payload = json_module.loads(body.decode('utf-8'))
        
        # Extract event metadata
        event_type = payload.get("type") or payload.get("event_type") or ""
        data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else payload
        
        # Extract event ID for idempotency.
        # Fallback uses sha256 of the raw body — Python's built-in hash() is
        # randomised per process (PEP 456), so two gunicorn workers would
        # compute different "ids" for the same payload and the dedup against
        # webhook_events.event_id would be useless.
        event_id = (
            data.get("id")
            or data.get("event_id")
            or payload.get("id")
            or payload.get("event_id")
            or f"checkout_{hashlib.sha256(body).hexdigest()[:32]}"
        )
        
        # Try to resolve order_id from multiple potential fields
        order_id = (
            data.get("reference")
            or payload.get("reference")
            or (data.get("metadata", {}) or {}).get("order_id")
            or (payload.get("metadata", {}) or {}).get("order_id")
        )
        
        # Signature verification (optional but recommended)
        signature_header = request.headers.get("cko-signature") or request.headers.get("Cko-Signature")
        webhook_secret = os.getenv("CHECKOUT_WEBHOOK_SECRET")
        signature_verified = False
        
        if webhook_secret and signature_header:
            signature_verified = await WebhookService.verify_checkout_signature(
                body, signature_header, webhook_secret
            )
            if not signature_verified:
                logger.warning(f"Checkout webhook signature verification failed for event {event_id}")
                await WebhookService.record_webhook_event(
                    event_id=event_id,
                    event_type=event_type,
                    psp_type="checkout",
                    order_id=order_id,
                    payload=payload,
                    headers=dict(request.headers),
                    signature_verified=False,
                    signature_header=signature_header,
                    status="failed"
                )
                raise HTTPException(status_code=401, detail="Invalid signature")
        elif webhook_secret:
            logger.warning(f"Checkout webhook signature not provided for event {event_id}")
            await WebhookService.record_webhook_event(
                event_id=event_id,
                event_type=event_type,
                psp_type="checkout",
                order_id=order_id,
                payload=payload,
                headers=dict(request.headers),
                signature_verified=False,
                signature_header=None,
                status="failed"
            )
            raise HTTPException(status_code=401, detail="Missing signature")
        else:
            # No webhook secret configured. In production this is a hard
            # failure — we refuse to silently accept unsigned webhooks. In
            # dev/staging, log a warning and fall through (existing behaviour).
            is_prod = _unsigned_webhook_is_fatal()
            if is_prod:
                logger.error(
                    f"CHECKOUT_WEBHOOK_SECRET not configured in production — "
                    f"rejecting unsigned event {event_id}"
                )
                await WebhookService.record_webhook_event(
                    event_id=event_id,
                    event_type=event_type,
                    psp_type="checkout",
                    order_id=order_id,
                    payload=payload,
                    headers=dict(request.headers),
                    signature_verified=False,
                    signature_header=signature_header,
                    status="failed"
                )
                raise HTTPException(status_code=503, detail="webhook_secret_not_configured")
            logger.warning(
                f"Checkout webhook signature verification skipped for event {event_id} "
                "(CHECKOUT_WEBHOOK_SECRET not set; permitted only in non-production)"
            )

        # Accept success-like events
        normalized_type = (event_type or "").lower()
        is_success = any(t in normalized_type for t in [
            "payment_captured", "payment_approved", "payment_paid", "charge_succeeded"
        ]) or bool(data.get("approved"))
        
        if not order_id:
            logger.warning(f"Checkout webhook missing order reference. event_id={event_id}")
            await WebhookService.record_webhook_event(
                event_id=event_id,
                event_type=event_type,
                psp_type="checkout",
                order_id=None,
                payload=payload,
                status="ignored"
            )
            return {"status": "ignored", "reason": "missing_order_reference", "event_id": event_id}
        
        if not is_success:
            logger.info(f"Checkout webhook non-success event '{event_type}' for order {order_id}")
            await WebhookService.record_webhook_event(
                event_id=event_id,
                event_type=event_type,
                psp_type="checkout",
                order_id=order_id,
                payload=payload,
                status="ignored"
            )
            return {"status": "ignored", "reason": f"event {event_type}", "event_id": event_id}
        
        # Check for duplicates (idempotency guard)
        is_duplicate, existing = await WebhookService.check_duplicate_event(event_id, order_id)
        if is_duplicate:
            logger.info(f"Duplicate Checkout webhook: {event_id} (order: {order_id})")
            return {
                "status": "duplicate",
                "event_id": event_id,
                "order_id": order_id,
                "message": "Event already processed"
            }
        
        # Record event as pending
        await WebhookService.record_webhook_event(
            event_id=event_id,
            event_type=event_type,
            psp_type="checkout",
            order_id=order_id,
            payload=payload,
            headers=dict(request.headers),
            signature_verified=signature_verified,
            signature_header=signature_header,
            status="pending"
        )
        
        # Lazy imports to avoid circular dependencies
        from db.orders import get_order, mark_order_paid, update_payment_info
        from routes.order_routes import log_order_event
        from services.merchant_store_service import get_primary_store
        from routes.order_routes import create_shopify_order
        
        order = await get_order(order_id)
        if not order:
            await WebhookService.update_event_status(event_id, "failed", "Order not found")
            raise HTTPException(status_code=404, detail="Order not found")

        # Check if already paid (idempotency at order level)
        if order.get("payment_status") == "paid":
            shopify_sync = "already_linked" if order.get("shopify_order_id") else "initiated"
            if not order.get("shopify_order_id"):

                async def create_shopify_order_task_already_paid():
                    try:
                        store_info = await get_primary_store(order["merchant_id"])
                        if store_info and store_info.get("platform") == "shopify":
                            await create_shopify_order(order_id)
                    except Exception as e:
                        logger.error(f"[Checkout webhook] Shopify sync failed for {order_id}: {e}")

                background_tasks.add_task(create_shopify_order_task_already_paid)

            logger.info(
                f"Order {order_id} already paid, marking webhook as processed (shopify_sync={shopify_sync})"
            )
            await WebhookService.update_event_status(event_id, "processed")
            return {
                "status": "already_paid",
                "event_id": event_id,
                "order_id": order_id,
                "shopify_sync": shopify_sync,
                "shopify_order_id": order.get("shopify_order_id"),
                "message": "Order already marked as paid",
            }
        
        transaction_id = data.get("id") or data.get("payment_id") or payload.get("id")
        await finalize_payment_success(
            order,
            psp="checkout",
            payment_reference=transaction_id or order.get("payment_intent_id"),
            transaction_id=transaction_id or order.get("payment_intent_id"),
            amount_minor=None,
            currency=str(order.get("currency") or "USD"),
            source_event="payment_succeeded",
            metadata_extra={
                "psp_type": "checkout",
                "webhook_type": event_type,
                "webhook_event_id": event_id,
                "signature_verified": signature_verified,
            },
            update_payment_info_fn=update_payment_info,
            mark_order_paid_fn=mark_order_paid,
            log_order_event_fn=log_order_event,
        )
        
        # Background: create Shopify order if applicable
        async def create_shopify_order_task():
            try:
                store_info = await get_primary_store(order["merchant_id"])
                if store_info and store_info.get("platform") == "shopify":
                    await create_shopify_order(order_id)
            except Exception as e:
                logger.error(f"[Checkout webhook] Shopify sync failed for {order_id}: {e}")
        
        background_tasks.add_task(create_shopify_order_task)
        
        # Mark webhook as processed
        await WebhookService.update_event_status(event_id, "processed")
        
        logger.info(f"✅ Checkout webhook processed: {event_id} (order: {order_id})")
        
        return {
            "status": "success",
            "event_id": event_id,
            "order_id": order_id,
            "signature_verified": signature_verified
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Checkout webhook error: {e}", exc_info=True)
        # Try to update event status if we have event_id
        try:
            if 'event_id' in locals():
                await WebhookService.update_event_status(event_id, "failed", str(e))
        except:
            pass
        raise HTTPException(status_code=500, detail="Webhook handling failed")
