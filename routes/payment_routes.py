"""
Payment Routes
The Checkout.com webhook that finalizes successful payments.

This module also served POST /process, POST /retry, GET /status, GET /psps and
GET /orders/{order_id} until 2026-08-31. All five stood on
orchestrator/payment_orchestrator.py, which stood on the empty
`psp.connectors.psp_manager` registry, so none of them could ever reach a PSP --
they only wrote fabricated Order/Payment records into the in-memory
`dashboard_core` maps. See tests/test_dead_psp_connectors_removed.py.

The webhook below is unrelated to that belt: it is a live finalizer, covered by
tests/test_checkout_webhook_contract.py.
"""

import hashlib
import logging
import os
from db.merchant_order_sync_jobs import enqueue_merchant_order_create
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks

from config.platform import is_production
from services.psp_payment_finalizer import finalize_payment_success

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
        
        order = await get_order(order_id)
        if not order:
            await WebhookService.update_event_status(event_id, "failed", "Order not found")
            raise HTTPException(status_code=404, detail="Order not found")

        # Check if already paid (idempotency at order level)
        if order.get("payment_status") == "paid":
            shopify_sync = "already_linked" if order.get("shopify_order_id") else "initiated"
            if not order.get("shopify_order_id"):

                # Durable enqueue — replaces `background_tasks.add_task`, which ran
                # in this process with no retry and died with a revision swap.
                # `require_shopify_primary` preserves this webhook's own narrower
                # guard; the worker checks it rather than widening the path.
                await enqueue_merchant_order_create(
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    require_shopify_primary=True,
                )

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
        
        # Durable enqueue — see the already-paid branch above.
        await enqueue_merchant_order_create(
            order_id=order_id,
            merchant_id=order["merchant_id"],
            require_shopify_primary=True,
        )
        
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
