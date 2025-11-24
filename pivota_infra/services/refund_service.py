"""
Refund Service - Core refund processing logic
Handles idempotency, validation, and coordination between DB and PSPs
"""
import secrets
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import asyncio
from databases import Database

from db.orders import get_order, update_order
from db.database import database as db
from adapters.psp_adapter import get_psp_adapter
from utils.logger import logger
from config.settings import settings


class RefundService:
    """Core refund processing service with idempotency and retry support"""
    
    def __init__(self, database: Database = None):
        self.db = database or db
    
    async def create_refund(
        self,
        order_id: str,
        amount: float,
        reason: str,
        source: str = "pivota_merchant",
        created_by: str = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a refund with idempotency protection
        
        Args:
            order_id: Order to refund
            amount: Amount to refund (required)
            reason: Refund reason
            source: Origin of refund (pivota_merchant/platform_webhook/admin)
            created_by: User/system that initiated refund
            idempotency_key: Optional custom idempotency key
            
        Returns:
            Dict with refund details and status
        """
        # Generate idempotency key if not provided
        if not idempotency_key:
            # Use order_id + amount + timestamp for natural deduplication
            timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            idempotency_key = f"{order_id}_{amount}_{timestamp_ms}"
        
        try:
            # Start transaction with row lock
            async with self.db.transaction():
                # 1. Check for duplicate using idempotency key
                existing = await self._check_existing_refund(idempotency_key)
                if existing:
                    logger.info(f"Duplicate refund request for key: {idempotency_key}")
                    return {
                        "status": "duplicate",
                        "refund_id": existing["refund_id"],
                        "message": "Refund already processed",
                        "refund": existing
                    }
                
                # 2. Lock and validate order
                order = await self._get_order_for_update(order_id)
                if not order:
                    raise ValueError(f"Order {order_id} not found")
                
                # 3. Validate refund eligibility
                validation_result = await self._validate_refund(order, amount)
                if not validation_result["valid"]:
                    raise ValueError(validation_result["error"])
                
                # 4. Create refund record (pending)
                refund_id = f"REF_{secrets.token_hex(8).upper()}"
                refund_record = await self._create_refund_record(
                    refund_id=refund_id,
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    amount=amount,
                    currency=order.get("currency", "USD"),
                    reason=reason,
                    source=source,
                    created_by=created_by,
                    idempotency_key=idempotency_key,
                    psp_type=order.get("psp_used", "stripe")
                )
                
                # 5. Process PSP refund (outside transaction to avoid long locks)
                psp_result = await self._process_psp_refund(order, refund_id, amount, reason)
                
                # 6. Update refund record with result
                if psp_result["success"]:
                    await self._update_refund_success(
                        refund_id=refund_id,
                        psp_refund_id=psp_result["refund_id"],
                        order_id=order_id,
                        amount=amount
                    )
                    
                    return {
                        "status": "success",
                        "refund_id": refund_id,
                        "psp_refund_id": psp_result["refund_id"],
                        "amount": amount,
                        "message": "Refund processed successfully"
                    }
                else:
                    await self._update_refund_failed(
                        refund_id=refund_id,
                        error=psp_result["error"]
                    )
                    
                    # Queue for retry
                    await self._queue_for_retry(refund_id)
                    
                    return {
                        "status": "failed",
                        "refund_id": refund_id,
                        "error": psp_result["error"],
                        "message": "Refund failed, queued for retry"
                    }
                    
        except Exception as e:
            logger.error(f"Refund creation failed: {e}")
            raise
    
    async def _check_existing_refund(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Check if refund with this idempotency key already exists"""
        query = """
        SELECT refund_id, order_id, amount, status, created_at
        FROM refund_records
        WHERE idempotency_key = :key
        """
        return await self.db.fetch_one(query, {"key": idempotency_key})
    
    async def _get_order_for_update(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order with row lock for update"""
        query = """
        SELECT * FROM orders 
        WHERE order_id = :order_id 
        FOR UPDATE
        """
        result = await self.db.fetch_one(query, {"order_id": order_id})
        return dict(result) if result else None
    
    async def _validate_refund(self, order: Dict[str, Any], amount: float) -> Dict[str, Any]:
        """Validate refund eligibility"""
        # Check payment status
        refundable_statuses = ["paid", "completed", "partially_refunded"]
        if order.get("payment_status") not in refundable_statuses:
            return {
                "valid": False,
                "error": f"Cannot refund order with payment status: {order.get('payment_status')}"
            }
        
        # Calculate already refunded amount
        total_refunded = await self._get_total_refunded(order["order_id"])
        
        # Check if amount exceeds refundable amount
        order_total = float(order.get("total", 0))
        remaining = order_total - total_refunded
        
        if amount > remaining:
            return {
                "valid": False,
                "error": f"Refund amount ${amount:.2f} exceeds refundable amount ${remaining:.2f}"
            }
        
        # Check time limits (90 days for most PSPs)
        created_at = order.get("created_at")
        if created_at:
            # Convert created_at to UTC if it has timezone info
            if created_at.tzinfo is not None:
                now = datetime.now(timezone.utc)
            else:
                now = datetime.now()
            days_since_order = (now - created_at).days
            if days_since_order > 90:
                return {
                    "valid": False,
                    "error": f"Order is {days_since_order} days old. Refund window is 90 days."
                }
        
        return {"valid": True}
    
    async def _get_total_refunded(self, order_id: str) -> float:
        """Get total amount already refunded for an order"""
        query = """
        SELECT COALESCE(SUM(amount), 0) as total_refunded
        FROM refund_records
        WHERE order_id = :order_id
        AND status = 'completed'
        """
        result = await self.db.fetch_one(query, {"order_id": order_id})
        return float(result["total_refunded"]) if result else 0
    
    async def _create_refund_record(self, **kwargs) -> Dict[str, Any]:
        """Create a pending refund record"""
        query = """
        INSERT INTO refund_records (
            refund_id, order_id, merchant_id, amount, currency,
            reason, source, created_by, idempotency_key, 
            psp_type, status, created_at
        ) VALUES (
            :refund_id, :order_id, :merchant_id, :amount, :currency,
            :reason, :source, :created_by, :idempotency_key,
            :psp_type, 'pending', NOW()
        )
        RETURNING *
        """
        
        result = await self.db.fetch_one(query, kwargs)
        return dict(result)
    
    async def _process_psp_refund(
        self, 
        order: Dict[str, Any], 
        refund_id: str,
        amount: float,
        reason: str
    ) -> Dict[str, Any]:
        """Process refund with payment service provider"""
        try:
            # Get PSP adapter
            psp_type = order.get("psp_used", "stripe")
            merchant_id = order["merchant_id"]
            
            # Get PSP credentials
            # TODO: Get from merchant settings
            if psp_type == "stripe":
                psp_key = settings.stripe_secret_key
            else:
                psp_key = None
            
            if not psp_key:
                raise ValueError(f"No PSP key configured for {psp_type}")
            
            # Get adapter and process refund
            adapter = get_psp_adapter(psp_type, psp_key)
            
            # For Stripe, we need the payment_intent_id
            payment_intent_id = order.get("payment_intent_id")
            if not payment_intent_id:
                raise ValueError("No payment_intent_id found for order")
            
            # Process refund
            success, psp_refund_id, error = await adapter.refund_payment(
                payment_intent_id=payment_intent_id,
                amount=Decimal(str(amount)),
                metadata={
                    "order_id": order["order_id"],
                    "refund_id": refund_id,
                    "reason": reason
                }
            )
            
            if success:
                logger.info(f"PSP refund successful: {psp_refund_id}")
                return {
                    "success": True,
                    "refund_id": psp_refund_id
                }
            else:
                logger.error(f"PSP refund failed: {error}")
                return {
                    "success": False,
                    "error": error or "Unknown PSP error"
                }
                
        except Exception as e:
            logger.error(f"PSP refund error: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def _update_refund_success(
        self, 
        refund_id: str, 
        psp_refund_id: str,
        order_id: str,
        amount: float
    ):
        """Update refund record after successful PSP refund"""
        # Update refund record
        update_refund = """
        UPDATE refund_records
        SET 
            status = 'completed',
            psp_refund_id = :psp_refund_id,
            processed_at = NOW()
        WHERE refund_id = :refund_id
        """
        await self.db.execute(update_refund, {
            "refund_id": refund_id,
            "psp_refund_id": psp_refund_id
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
        await self.db.execute(update_order, {
            "amount": amount,
            "order_id": order_id
        })
    
    async def _update_refund_failed(self, refund_id: str, error: str):
        """Update refund record after failed PSP refund"""
        query = """
        UPDATE refund_records
        SET 
            status = 'failed',
            error_message = :error,
            processed_at = NOW()
        WHERE refund_id = :refund_id
        """
        await self.db.execute(query, {
            "refund_id": refund_id,
            "error": error[:500]  # Limit error message length
        })
    
    async def _queue_for_retry(self, refund_id: str):
        """Queue failed refund for retry"""
        query = """
        INSERT INTO refund_retry_queue (
            refund_id,
            retry_count,
            next_retry_at,
            created_at
        ) VALUES (
            :refund_id,
            0,
            :next_retry,
            NOW()
        )
        """
        # Retry in 5 minutes
        next_retry = datetime.now(timezone.utc) + timedelta(minutes=5)
        await self.db.execute(query, {
            "refund_id": refund_id,
            "next_retry": next_retry
        })
    
    async def get_refund_history(self, order_id: str) -> list[Dict[str, Any]]:
        """Get refund history for an order with enhanced details"""
        query = """
        SELECT 
            refund_id,
            amount,
            currency,
            reason,
            source,
            status,
            created_by,
            created_at,
            processed_at,
            error_message,
            psp_refund_id,
            idempotency_key,
            metadata,
            CASE 
                WHEN status = 'completed' THEN 'success'
                WHEN status = 'failed' THEN 'error'
                WHEN status = 'pending' THEN 'warning'
                ELSE 'info'
            END as status_type,
            CASE
                WHEN processed_at IS NOT NULL 
                THEN EXTRACT(EPOCH FROM (processed_at - created_at))
                ELSE NULL
            END as processing_time_seconds
        FROM refund_records
        WHERE order_id = :order_id
        ORDER BY created_at DESC
        """
        
        results = await self.db.fetch_all(query, {"order_id": order_id})
        
        # Format the results with additional context
        refunds = []
        for r in results:
            refund = dict(r)
            
            # Handle JSONB metadata field
            if 'metadata' in refund and refund['metadata'] is not None:
                # metadata is already parsed from JSONB, keep as is
                pass
            else:
                refund['metadata'] = {}
            
            # Add human-readable status messages
            if refund['status'] == 'completed':
                refund['status_message'] = 'Refund successfully processed'
            elif refund['status'] == 'pending':
                refund['status_message'] = 'Refund is being processed'
            elif refund['status'] == 'failed':
                refund['status_message'] = refund.get('error_message', 'Refund failed')
            else:
                refund['status_message'] = f"Status: {refund['status']}"
            
            # Format timestamps for frontend
            if refund.get('created_at'):
                refund['created_at_formatted'] = refund['created_at'].isoformat() if hasattr(refund['created_at'], 'isoformat') else str(refund['created_at'])
            if refund.get('processed_at'):
                refund['processed_at_formatted'] = refund['processed_at'].isoformat() if hasattr(refund['processed_at'], 'isoformat') else str(refund['processed_at'])
            
            refunds.append(refund)
        
        return refunds
    
    async def retry_failed_refunds(self) -> int:
        """Process refunds in retry queue"""
        # Get refunds ready for retry
        query = """
        SELECT 
            rq.id as queue_id,
            rq.refund_id,
            rq.retry_count,
            r.order_id,
            r.amount,
            r.reason
        FROM refund_retry_queue rq
        JOIN refund_records r ON rq.refund_id = r.refund_id
        WHERE rq.next_retry_at <= NOW()
        AND rq.retry_count < rq.max_retries
        LIMIT 10
        """
        
        refunds_to_retry = await self.db.fetch_all(query)
        processed = 0
        
        for refund in refunds_to_retry:
            try:
                # Get order details
                order = await get_order(refund["order_id"])
                if not order:
                    logger.error(f"Order not found for retry: {refund['order_id']}")
                    continue
                
                # Retry PSP refund
                psp_result = await self._process_psp_refund(
                    order, 
                    refund["refund_id"],
                    refund["amount"],
                    refund["reason"]
                )
                
                if psp_result["success"]:
                    # Update as successful
                    await self._update_refund_success(
                        refund_id=refund["refund_id"],
                        psp_refund_id=psp_result["refund_id"],
                        order_id=refund["order_id"],
                        amount=refund["amount"]
                    )
                    
                    # Remove from retry queue
                    await self.db.execute(
                        "DELETE FROM refund_retry_queue WHERE id = :id",
                        {"id": refund["queue_id"]}
                    )
                    
                    processed += 1
                else:
                    # Update retry count and next retry time
                    next_retry = datetime.now(timezone.utc) + timedelta(
                        minutes=5 * (2 ** refund["retry_count"])  # Exponential backoff
                    )
                    
                    await self.db.execute("""
                        UPDATE refund_retry_queue
                        SET 
                            retry_count = retry_count + 1,
                            next_retry_at = :next_retry,
                            last_error = :error,
                            updated_at = NOW()
                        WHERE id = :id
                    """, {
                        "id": refund["queue_id"],
                        "next_retry": next_retry,
                        "error": psp_result["error"]
                    })
                    
            except Exception as e:
                logger.error(f"Error retrying refund {refund['refund_id']}: {e}")
                
        return processed


# Create singleton instance
refund_service = RefundService()

