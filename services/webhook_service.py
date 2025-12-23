"""
Webhook Service - Idempotency, Signature Verification, and Event Persistence

Provides centralized webhook handling with:
- HMAC signature verification
- Idempotency guards (duplicate detection)
- Event persistence and auditing
- Retry safety
"""

import asyncio
import hmac
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from db.database import database

logger = logging.getLogger(__name__)

_WEBHOOK_EVENTS_SCHEMA_READY = False
_WEBHOOK_EVENTS_SCHEMA_LOCK = asyncio.Lock()


def _is_unique_violation(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "duplicate key value violates unique constraint" in msg
        or "unique violation" in msg
        or "uniqueconstraint" in msg
    )


def _is_schema_or_permission_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "permission denied" in msg or "insufficientprivilege" in msg:
        return True
    if "webhook_events" in msg and ("does not exist" in msg or "undefined" in msg):
        return True
    if "column" in msg and "does not exist" in msg:
        return True
    return False


class WebhookService:
    """Centralized webhook processing service"""

    @staticmethod
    async def ensure_webhook_events_table() -> None:
        """
        Best-effort: ensure `webhook_events` exists so PSP webhooks don't 500 due to missing migrations.

        This intentionally does not raise. If the runtime DB role cannot run DDL,
        we continue without persistence/idempotency (order-level idempotency still applies).
        """
        global _WEBHOOK_EVENTS_SCHEMA_READY
        if _WEBHOOK_EVENTS_SCHEMA_READY:
            return

        async with _WEBHOOK_EVENTS_SCHEMA_LOCK:
            if _WEBHOOK_EVENTS_SCHEMA_READY:
                return
            try:
                create_sql = """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id SERIAL PRIMARY KEY,
                    event_id VARCHAR(255) UNIQUE,
                    event_type VARCHAR(100),
                    psp_type VARCHAR(50),
                    order_id VARCHAR(50),
                    reference VARCHAR(255),
                    payload JSONB,
                    headers JSONB,
                    status VARCHAR(50) DEFAULT 'pending',
                    processed_at TIMESTAMP WITH TIME ZONE,
                    error_message TEXT,
                    signature_verified BOOLEAN DEFAULT FALSE,
                    signature_header VARCHAR(500),
                    retry_count INTEGER DEFAULT 0,
                    last_retry_at TIMESTAMP WITH TIME ZONE,
                    received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
                """
                await database.execute(create_sql)

                # Add missing columns to older schemas (safe no-ops when present).
                alter_statements = [
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS event_id VARCHAR(255);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS event_type VARCHAR(100);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS psp_type VARCHAR(50);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS order_id VARCHAR(50);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS reference VARCHAR(255);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS payload JSONB;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS headers JSONB;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'pending';",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP WITH TIME ZONE;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS error_message TEXT;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS signature_verified BOOLEAN DEFAULT FALSE;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS signature_header VARCHAR(500);",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS last_retry_at TIMESTAMP WITH TIME ZONE;",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();",
                    "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();",
                ]
                for stmt in alter_statements:
                    try:
                        await database.execute(stmt)
                    except Exception:
                        # Ignore per-column failures (type conflicts on legacy tables, etc.)
                        pass

                # Indexes help idempotency checks; all are optional.
                index_statements = [
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_event_id ON webhook_events(event_id);",
                    "CREATE INDEX IF NOT EXISTS idx_webhook_events_order_id ON webhook_events(order_id);",
                    "CREATE INDEX IF NOT EXISTS idx_webhook_events_status ON webhook_events(status);",
                    "CREATE INDEX IF NOT EXISTS idx_webhook_events_psp_type ON webhook_events(psp_type);",
                    "CREATE INDEX IF NOT EXISTS idx_webhook_events_event_type ON webhook_events(event_type);",
                    "CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at ON webhook_events(received_at DESC);",
                ]
                for stmt in index_statements:
                    try:
                        await database.execute(stmt)
                    except Exception:
                        pass

                _WEBHOOK_EVENTS_SCHEMA_READY = True
            except Exception as exc:
                logger.warning(
                    "Webhook events table not ready (continuing without persistence): %s",
                    exc,
                    exc_info=True,
                )
                _WEBHOOK_EVENTS_SCHEMA_READY = False

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

        # Best-effort: never let webhook processing fail due to missing audit tables.
        await WebhookService.ensure_webhook_events_table()

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
            if _is_unique_violation(e):
                # Retry-safe: treat as idempotent insert and return existing id.
                try:
                    existing_id = await database.fetch_val(
                        "SELECT id FROM webhook_events WHERE event_id = :event_id",
                        {"event_id": event_id},
                    )
                    if existing_id is not None:
                        try:
                            await WebhookService.increment_retry_count(event_id)
                        except Exception:
                            pass
                        return int(existing_id)
                except Exception:
                    pass
                logger.warning("Duplicate webhook event_id=%s (could not fetch existing row)", event_id)
                return 0

            if _is_schema_or_permission_error(e):
                logger.warning(
                    "Failed to persist webhook event (continuing): event_id=%s err=%s",
                    event_id,
                    e,
                    exc_info=True,
                )
                return 0

            logger.error(f"Failed to record webhook event {event_id}: {e}", exc_info=True)
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
        await WebhookService.ensure_webhook_events_table()

        query = """
            SELECT id, event_id, order_id, status, processed_at, error_message
            FROM webhook_events
            WHERE event_id = :event_id
            ORDER BY received_at DESC
            LIMIT 1
        """

        try:
            existing = await database.fetch_one(query, {"event_id": event_id})
        except Exception as exc:
            if _is_schema_or_permission_error(exc):
                logger.warning(
                    "Webhook idempotency unavailable (continuing): %s",
                    exc,
                    exc_info=True,
                )
                return False, None
            raise
        
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
        await WebhookService.ensure_webhook_events_table()

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
        
        try:
            await database.execute(query, values)
            logger.info(f"Webhook event {event_id} updated to status: {status}")
        except Exception as exc:
            if _is_schema_or_permission_error(exc):
                logger.warning(
                    "Failed to update webhook event status (continuing): event_id=%s err=%s",
                    event_id,
                    exc,
                    exc_info=True,
                )
                return
            raise
    
    @staticmethod
    async def increment_retry_count(event_id: str):
        """
        Increment retry count for webhook event
        
        Args:
            event_id: External event ID
        """
        await WebhookService.ensure_webhook_events_table()

        query = """
            UPDATE webhook_events
            SET 
                retry_count = retry_count + 1,
                last_retry_at = NOW()
            WHERE event_id = :event_id
        """

        try:
            await database.execute(query, {"event_id": event_id})
        except Exception as exc:
            if _is_schema_or_permission_error(exc):
                return
            raise
    
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
