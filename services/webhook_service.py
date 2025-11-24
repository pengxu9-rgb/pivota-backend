"""
Webhook Service - Idempotency, Signature Verification, and Event Persistence

Provides centralized webhook handling with:
- HMAC signature verification
- Idempotency guards (duplicate detection)
- Event persistence and auditing
- Retry safety
"""

import hmac
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from db.database import database

logger = logging.getLogger(__name__)


class WebhookService:
    """Centralized webhook processing service"""
    
    @staticmethod
    async def verify_checkout_signature(
        payload: bytes,
        signature_header: str,
        secret: str
    ) -> bool:
        """
        Verify Checkout.com webhook signature
        
        Args:
            payload: Raw request body bytes
            signature_header: Signature from request header
            secret: Webhook secret from Checkout.com dashboard
            
        Returns:
            True if signature is valid, False otherwise
        """
        try:
            # Checkout.com uses HMAC-SHA256
            expected_signature = hmac.new(
                secret.encode('utf-8'),
                payload,
                hashlib.sha256
            ).hexdigest()
            
            # Compare signatures (constant-time comparison)
            return hmac.compare_digest(signature_header, expected_signature)
        except Exception as e:
            logger.error(f"Signature verification error: {e}")
            return False
    
    @staticmethod
    async def record_webhook_event(
        event_id: str,
        event_type: str,
        psp_type: str,
        order_id: Optional[str],
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        signature_verified: bool = False,
        signature_header: Optional[str] = None,
        status: str = "pending"
    ) -> int:
        """
        Record webhook event to database
        
        Args:
            event_id: External event ID from PSP
            event_type: Type of event (payment_captured, etc.)
            psp_type: PSP type (checkout, stripe, etc.)
            order_id: Associated order ID
            payload: Full webhook payload
            headers: Request headers
            signature_verified: Whether signature was verified
            signature_header: Signature header value
            status: Initial status (pending, processed, etc.)
            
        Returns:
            Database record ID
        """
        import json
        
        query = """
            INSERT INTO webhook_events (
                event_id, event_type, psp_type, order_id, reference,
                payload, headers, status, signature_verified, signature_header
            )
            VALUES (
                :event_id, :event_type, :psp_type, :order_id, :reference,
                :payload, :headers, :status, :signature_verified, :signature_header
            )
            RETURNING id
        """
        
        values = {
            "event_id": event_id,
            "event_type": event_type,
            "psp_type": psp_type,
            "order_id": order_id,
            "reference": payload.get("reference") or payload.get("data", {}).get("reference"),
            "payload": json.dumps(payload),
            "headers": json.dumps(headers or {}),
            "status": status,
            "signature_verified": signature_verified,
            "signature_header": signature_header
        }
        
        try:
            record_id = await database.execute(query, values)
            logger.info(f"Webhook event recorded: {event_id} (DB ID: {record_id})")
            return record_id
        except Exception as e:
            logger.error(f"Failed to record webhook event {event_id}: {e}")
            raise
    
    @staticmethod
    async def check_duplicate_event(
        event_id: str,
        order_id: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if event has already been processed (idempotency guard)
        
        Args:
            event_id: External event ID from PSP
            order_id: Optional order ID for additional verification
            
        Returns:
            Tuple of (is_duplicate, existing_record)
        """
        query = """
            SELECT id, event_id, order_id, status, processed_at, error_message
            FROM webhook_events
            WHERE event_id = :event_id
            ORDER BY received_at DESC
            LIMIT 1
        """
        
        existing = await database.fetch_one(query, {"event_id": event_id})
        
        if not existing:
            return False, None
        
        # Event exists - check if it was successfully processed
        is_duplicate = existing["status"] in ["processed", "ignored"]
        
        if is_duplicate:
            logger.info(
                f"Duplicate webhook event detected: {event_id} "
                f"(status: {existing['status']}, order: {existing['order_id']})"
            )
        
        return is_duplicate, dict(existing)
    
    @staticmethod
    async def update_event_status(
        event_id: str,
        status: str,
        error_message: Optional[str] = None
    ):
        """
        Update webhook event processing status
        
        Args:
            event_id: External event ID
            status: New status (processed, failed, etc.)
            error_message: Optional error message if failed
        """
        query = """
            UPDATE webhook_events
            SET 
                status = :status,
                processed_at = :processed_at,
                error_message = :error_message
            WHERE event_id = :event_id
        """
        
        values = {
            "event_id": event_id,
            "status": status,
            "processed_at": datetime.utcnow() if status == "processed" else None,
            "error_message": error_message
        }
        
        await database.execute(query, values)
        logger.info(f"Webhook event {event_id} updated to status: {status}")
    
    @staticmethod
    async def increment_retry_count(event_id: str):
        """
        Increment retry count for webhook event
        
        Args:
            event_id: External event ID
        """
        query = """
            UPDATE webhook_events
            SET 
                retry_count = retry_count + 1,
                last_retry_at = NOW()
            WHERE event_id = :event_id
        """
        
        await database.execute(query, {"event_id": event_id})
    
    @staticmethod
    async def get_event_stats(
        psp_type: Optional[str] = None,
        hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get webhook event statistics
        
        Args:
            psp_type: Optional filter by PSP type
            hours: Look back period in hours
            
        Returns:
            Statistics dictionary
        """
        where_clause = "WHERE received_at > NOW() - INTERVAL ':hours hours'"
        if psp_type:
            where_clause += " AND psp_type = :psp_type"
        
        query = f"""
            SELECT 
                COUNT(*) as total_events,
                COUNT(CASE WHEN status = 'processed' THEN 1 END) as processed,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
                COUNT(CASE WHEN status = 'duplicate' THEN 1 END) as duplicates,
                COUNT(CASE WHEN signature_verified THEN 1 END) as verified,
                AVG(EXTRACT(EPOCH FROM (processed_at - received_at))) as avg_processing_time
            FROM webhook_events
            {where_clause}
        """
        
        values = {"hours": hours}
        if psp_type:
            values["psp_type"] = psp_type
        
        result = await database.fetch_one(query, values)
        return dict(result) if result else {}


# Convenience functions
async def verify_webhook_signature(
    payload: bytes,
    signature_header: str,
    psp_type: str,
    secret: Optional[str] = None
) -> bool:
    """
    Verify webhook signature for any PSP
    
    Args:
        payload: Raw request body bytes
        signature_header: Signature from request header
        psp_type: PSP type (checkout, stripe, etc.)
        secret: Webhook secret (if None, will be fetched from config)
        
    Returns:
        True if signature is valid
    """
    if psp_type == "checkout":
        if not secret:
            # TODO: Fetch from environment or config
            logger.warning("Checkout webhook secret not configured")
            return False
        return await WebhookService.verify_checkout_signature(
            payload, signature_header, secret
        )
    elif psp_type == "stripe":
        # TODO: Implement Stripe signature verification
        logger.warning("Stripe signature verification not yet implemented")
        return False
    else:
        logger.warning(f"Signature verification not supported for PSP: {psp_type}")
        return False


async def process_webhook_with_idempotency(
    event_id: str,
    event_type: str,
    psp_type: str,
    order_id: Optional[str],
    payload: Dict[str, Any],
    processor_func,
    **kwargs
) -> Dict[str, Any]:
    """
    Process webhook with idempotency guard
    
    Args:
        event_id: External event ID
        event_type: Event type
        psp_type: PSP type
        order_id: Order ID
        payload: Webhook payload
        processor_func: Async function to process the event
        **kwargs: Additional arguments for processor_func
        
    Returns:
        Processing result dictionary
    """
    # Check for duplicates
    is_duplicate, existing = await WebhookService.check_duplicate_event(
        event_id, order_id
    )
    
    if is_duplicate:
        return {
            "status": "duplicate",
            "event_id": event_id,
            "order_id": order_id,
            "message": "Event already processed",
            "existing_record": existing
        }
    
    # Record new event
    try:
        await WebhookService.record_webhook_event(
            event_id=event_id,
            event_type=event_type,
            psp_type=psp_type,
            order_id=order_id,
            payload=payload,
            status="pending"
        )
        
        # Process event
        result = await processor_func(order_id, payload, **kwargs)
        
        # Mark as processed
        await WebhookService.update_event_status(event_id, "processed")
        
        return {
            "status": "success",
            "event_id": event_id,
            "order_id": order_id,
            "result": result
        }
    
    except Exception as e:
        logger.error(f"Webhook processing failed for {event_id}: {e}")
        
        # Mark as failed
        await WebhookService.update_event_status(
            event_id, "failed", str(e)
        )
        
        raise

