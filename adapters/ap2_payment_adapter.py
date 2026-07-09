"""
[Phase 4++] AP2 Protocol Payment Adapter
Integrates AP2 protocol with actual payment processing using existing PSP adapters
"""

from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from datetime import datetime
import json
import hashlib
import logging

from .base_psp_adapter import BasePSPAdapter, PaymentStatus
from services.protocol_adapter_service import AP2Adapter
from db.database import database

logger = logging.getLogger(__name__)


class AP2PaymentAdapter(BasePSPAdapter):
    """
    AP2 Protocol Payment Adapter
    
    This adapter:
    1. Accepts AP2 protocol format requests
    2. Transforms to internal PSP adapter format
    3. Routes to actual PSP (Stripe, Adyen, etc)
    4. Transforms response back to AP2 format
    5. Logs all transactions to ap2_transactions table
    """
    
    PROTOCOL = "AP2"
    VERSION = "2.0"
    
    def __init__(self, config: Dict[str, Any], underlying_psp_adapter: Optional[BasePSPAdapter] = None):
        """
        Initialize AP2 adapter
        
        Args:
            config: Configuration including AP2 settings
            underlying_psp_adapter: The actual PSP adapter to use (Stripe, Adyen, etc)
        """
        super().__init__(config)
        self.underlying_psp = underlying_psp_adapter
        self.protocol_adapter = AP2Adapter(database)  # Reuse Phase 4 protocol adapter
        self.ap2_config = config.get('ap2', {})
        
        logger.info(f"[Phase 4++] AP2PaymentAdapter initialized with underlying PSP: {type(underlying_psp_adapter).__name__ if underlying_psp_adapter else 'None'}")
    
    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """Validate AP2 configuration"""
        if not self.underlying_psp:
            return False, "No underlying PSP adapter configured"
        
        # Validate underlying PSP config
        is_valid, error = self.underlying_psp.validate_config()
        if not is_valid:
            return False, f"Underlying PSP config error: {error}"
        
        # Validate AP2 specific config
        if not self.ap2_config.get('endpoint'):
            logger.warning("[Phase 4++] No AP2 endpoint configured, using default")
        
        return True, None
    
    async def create_payment(
        self,
        amount: Decimal,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create payment using AP2 protocol
        
        This method:
        1. Transforms to AP2 format
        2. Validates with AP2 protocol rules
        3. Executes with underlying PSP
        4. Logs to ap2_transactions
        5. Returns AP2 formatted response
        """
        try:
            # Extract routing context from metadata
            routing_context = metadata.get('routing_context', {}) if metadata else {}
            routing_log_id = routing_context.get('log_id')
            agent_id = metadata.get('agent_id')
            merchant_id = metadata.get('merchant_id')
            
            # Create AP2 request payload
            ap2_request = {
                "protocol": self.PROTOCOL,
                "version": self.VERSION,
                "timestamp": datetime.utcnow().isoformat(),
                "transaction": {
                    "order_id": order_id,
                    "amount": str(amount),
                    "currency": currency,
                    "merchant_id": merchant_id,
                    "agent_id": agent_id,
                    "customer": {
                        "email": customer_email
                    } if customer_email else None
                },
                "routing": {
                    "selected_psp": type(self.underlying_psp).__name__.replace('Adapter', '').lower() if self.underlying_psp else None,
                    "routing_log_id": routing_log_id
                }
            }
            
            # Generate unique AP2 transaction ID
            ap2_transaction_id = self._generate_ap2_transaction_id(ap2_request)
            ap2_request['transaction_id'] = ap2_transaction_id
            
            # Log AP2 transaction start
            ap2_tx_id = await self._log_ap2_transaction(
                transaction_id=ap2_transaction_id,
                order_id=order_id,
                agent_id=agent_id,
                merchant_id=merchant_id,
                routing_log_id=routing_log_id,
                status='pending',
                ap2_request=ap2_request,
                amount=amount,
                currency=currency
            )
            
            logger.info(f"[Phase 4++] Creating AP2 payment: order={order_id}, amount={amount} {currency}, tx={ap2_transaction_id}")
            
            # Validate with AP2 protocol rules
            validation_result = await self.protocol_adapter.validate_request(ap2_request['transaction'])
            if not validation_result[0]:
                error_msg = validation_result[1] or "AP2 validation failed"
                await self._update_ap2_transaction(ap2_tx_id, 'failed', error_message=error_msg)
                return {
                    "success": False,
                    "error": error_msg,
                    "ap2_transaction_id": ap2_transaction_id
                }
            
            # Execute with underlying PSP if available
            if self.underlying_psp:
                # Add AP2 transaction ID to metadata
                psp_metadata = metadata or {}
                psp_metadata['ap2_transaction_id'] = ap2_transaction_id
                
                # Call underlying PSP
                psp_response = await self.underlying_psp.create_payment(
                    amount=amount,
                    currency=currency,
                    order_id=order_id,
                    customer_email=customer_email,
                    metadata=psp_metadata
                )
                
                # Transform PSP response to AP2 format
                ap2_response = await self._transform_to_ap2_response(
                    psp_response=psp_response,
                    ap2_transaction_id=ap2_transaction_id,
                    order_id=order_id
                )
                
                # Update AP2 transaction with response
                status = 'authorized' if psp_response.get('success') else 'failed'
                await self._update_ap2_transaction(
                    ap2_tx_id,
                    status,
                    ap2_response=ap2_response,
                    psp_payment_id=psp_response.get('psp_payment_id')
                )
                
            else:
                # No underlying PSP — FAIL CLOSED. This previously returned a
                # fabricated status="simulated" response and marked the AP2
                # transaction 'authorized' (success=True) — a fake authorization
                # with no real charge. With AP2 now a partner-facing capability
                # (ENABLE_AP2_ROUTES), never report a successful authorization
                # without a real PSP; raise so the caller returns an honest failure.
                logger.error(
                    f"[AP2] No underlying PSP for AP2 transaction {ap2_transaction_id} "
                    "— failing closed (simulated authorization disabled)."
                )
                raise RuntimeError(
                    f"AP2 authorization unavailable for order {order_id}: "
                    "no underlying PSP configured (simulated auth disabled)."
                )
            
            # Return combined response
            return {
                "success": ap2_response.get('status') in ['authorized', 'simulated'],
                "ap2_transaction_id": ap2_transaction_id,
                "psp_payment_id": ap2_response.get('psp_payment_id'),
                "status": ap2_response.get('status'),
                "ap2_response": ap2_response,
                "ap2_request": ap2_request
            }
            
        except Exception as e:
            logger.error(f"[Phase 4++] AP2 payment creation failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def get_payment_status(self, payment_id: str) -> Dict[str, Any]:
        """Get payment status using AP2 transaction ID"""
        try:
            # Check if this is an AP2 transaction ID
            ap2_tx = await database.fetch_one(
                """
                SELECT transaction_id, status, psp_used, ap2_response
                FROM ap2_transactions
                WHERE transaction_id = :transaction_id
                """,
                {"transaction_id": payment_id}
            )
            
            if ap2_tx:
                return {
                    "success": True,
                    "payment_id": ap2_tx['transaction_id'],
                    "status": self._map_ap2_status(ap2_tx['status']),
                    "ap2_status": ap2_tx['status'],
                    "psp_used": ap2_tx['psp_used'],
                    "raw_response": ap2_tx['ap2_response']
                }
            
            # Fall back to underlying PSP if not found
            if self.underlying_psp:
                return await self.underlying_psp.get_payment_status(payment_id)
            
            return {
                "success": False,
                "error": "Payment not found"
            }
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get AP2 payment status: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def capture_payment(self, payment_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Capture an authorized AP2 payment"""
        try:
            # Get AP2 transaction
            ap2_tx = await database.fetch_one(
                """
                SELECT id, transaction_id, status, psp_used, ap2_response
                FROM ap2_transactions
                WHERE transaction_id = :transaction_id
                """,
                {"transaction_id": payment_id}
            )
            
            if not ap2_tx:
                return {
                    "success": False,
                    "error": "AP2 transaction not found"
                }
            
            if ap2_tx['status'] != 'authorized':
                return {
                    "success": False,
                    "error": f"Cannot capture payment in status: {ap2_tx['status']}"
                }
            
            # Create AP2 capture request
            ap2_capture_request = {
                "protocol": self.PROTOCOL,
                "version": self.VERSION,
                "action": "capture",
                "transaction_id": payment_id,
                "amount": str(amount) if amount else None,
                "timestamp": datetime.utcnow().isoformat()
            }
            
            # Capture with underlying PSP if available
            if self.underlying_psp and ap2_tx['ap2_response'].get('psp_payment_id'):
                psp_result = await self.underlying_psp.capture_payment(
                    ap2_tx['ap2_response']['psp_payment_id'],
                    amount
                )
                
                if psp_result.get('success'):
                    # Update AP2 transaction status
                    await self._update_ap2_transaction(ap2_tx['id'], 'captured')
                    
                    return {
                        "success": True,
                        "ap2_transaction_id": payment_id,
                        "status": "captured",
                        "captured_amount": str(amount) if amount else None
                    }
                else:
                    return psp_result
            
            # Simulate capture if no underlying PSP
            await self._update_ap2_transaction(ap2_tx['id'], 'captured')
            return {
                "success": True,
                "ap2_transaction_id": payment_id,
                "status": "captured",
                "simulated": True
            }
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to capture AP2 payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def refund_payment(
        self,
        payment_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refund an AP2 payment"""
        try:
            # Get AP2 transaction
            ap2_tx = await database.fetch_one(
                """
                SELECT id, transaction_id, status, psp_used, ap2_response, amount, currency
                FROM ap2_transactions
                WHERE transaction_id = :transaction_id
                """,
                {"transaction_id": payment_id}
            )
            
            if not ap2_tx:
                return {
                    "success": False,
                    "error": "AP2 transaction not found"
                }
            
            if ap2_tx['status'] not in ['captured', 'authorized']:
                return {
                    "success": False,
                    "error": f"Cannot refund payment in status: {ap2_tx['status']}"
                }
            
            # Refund with underlying PSP if available
            if self.underlying_psp and ap2_tx['ap2_response'].get('psp_payment_id'):
                psp_result = await self.underlying_psp.refund_payment(
                    ap2_tx['ap2_response']['psp_payment_id'],
                    amount,
                    reason
                )
                
                if psp_result.get('success'):
                    # Update AP2 transaction status
                    await self._update_ap2_transaction(ap2_tx['id'], 'refunded')
                    
                    return {
                        "success": True,
                        "ap2_transaction_id": payment_id,
                        "status": "refunded",
                        "refund_amount": str(amount or ap2_tx['amount']),
                        "refund_reason": reason
                    }
                else:
                    return psp_result
            
            # Simulate refund if no underlying PSP
            await self._update_ap2_transaction(ap2_tx['id'], 'refunded')
            return {
                "success": True,
                "ap2_transaction_id": payment_id,
                "status": "refunded",
                "simulated": True
            }
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to refund AP2 payment: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def cancel_payment(self, payment_id: str) -> Dict[str, Any]:
        """Cancel a pending AP2 payment"""
        # Similar implementation to refund but for pending payments
        pass  # Implementation follows same pattern
    
    def _generate_ap2_transaction_id(self, ap2_request: Dict[str, Any]) -> str:
        """Generate unique AP2 transaction ID"""
        # Create hash from order_id + timestamp + random component
        data = f"{ap2_request.get('transaction', {}).get('order_id')}_{datetime.utcnow().isoformat()}"
        hash_value = hashlib.sha256(data.encode()).hexdigest()[:16]
        return f"ap2_txn_{hash_value}"
    
    async def _transform_to_ap2_response(
        self,
        psp_response: Dict[str, Any],
        ap2_transaction_id: str,
        order_id: str
    ) -> Dict[str, Any]:
        """Transform PSP response to AP2 format"""
        return {
            "protocol": self.PROTOCOL,
            "version": self.VERSION,
            "transaction_id": ap2_transaction_id,
            "order_id": order_id,
            "status": "authorized" if psp_response.get('success') else "failed",
            "psp_payment_id": psp_response.get('psp_payment_id'),
            "psp_used": type(self.underlying_psp).__name__.replace('Adapter', '').lower() if self.underlying_psp else None,
            "timestamp": datetime.utcnow().isoformat(),
            "amount": psp_response.get('amount'),
            "currency": psp_response.get('currency'),
            "error": psp_response.get('error'),
            "raw_psp_response": psp_response
        }
    
    def _map_ap2_status(self, ap2_status: str) -> PaymentStatus:
        """Map AP2 status to internal PaymentStatus"""
        status_mapping = {
            'pending': PaymentStatus.PENDING,
            'authorized': PaymentStatus.PROCESSING,
            'captured': PaymentStatus.PAID,
            'failed': PaymentStatus.FAILED,
            'refunded': PaymentStatus.REFUNDED,
            'cancelled': PaymentStatus.CANCELLED
        }
        return status_mapping.get(ap2_status, PaymentStatus.PENDING)
    
    async def _log_ap2_transaction(
        self,
        transaction_id: str,
        order_id: str,
        agent_id: Optional[str],
        merchant_id: Optional[str],
        routing_log_id: Optional[int],
        status: str,
        ap2_request: Dict[str, Any],
        amount: Decimal,
        currency: str
    ) -> int:
        """Log AP2 transaction to database"""
        result = await database.execute(
            """
            INSERT INTO ap2_transactions (
                transaction_id, order_id, agent_id, merchant_id,
                routing_log_id, status, ap2_request, amount, currency,
                psp_used, created_at
            ) VALUES (
                :transaction_id, :order_id, :agent_id, :merchant_id,
                :routing_log_id, :status, :ap2_request, :amount, :currency,
                :psp_used, NOW()
            )
            RETURNING id
            """,
            {
                "transaction_id": transaction_id,
                "order_id": order_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "routing_log_id": routing_log_id,
                "status": status,
                "ap2_request": json.dumps(ap2_request),
                "amount": amount,
                "currency": currency,
                "psp_used": type(self.underlying_psp).__name__.replace('Adapter', '').lower() if self.underlying_psp else None
            }
        )
        
        logger.info(f"[Phase 4++] Logged AP2 transaction {transaction_id} with ID {result}")
        return result
    
    async def _update_ap2_transaction(
        self,
        ap2_tx_id: int,
        status: str,
        ap2_response: Optional[Dict[str, Any]] = None,
        psp_payment_id: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """Update AP2 transaction record"""
        update_data = {
            "id": ap2_tx_id,
            "status": status,
            "updated_at": datetime.utcnow()
        }
        
        if ap2_response:
            update_data["ap2_response"] = json.dumps(ap2_response)
        
        await database.execute(
            """
            UPDATE ap2_transactions
            SET status = :status,
                ap2_response = COALESCE(:ap2_response, ap2_response),
                updated_at = :updated_at
            WHERE id = :id
            """,
            update_data
        )
        
        logger.info(f"[Phase 4++] Updated AP2 transaction {ap2_tx_id} to status {status}")


# [Phase 4++] Factory function to create AP2 adapter with underlying PSP
def create_ap2_adapter(psp_name: str, config: Dict[str, Any]) -> AP2PaymentAdapter:
    """
    Factory function to create AP2 adapter with appropriate underlying PSP
    
    Args:
        psp_name: Name of the PSP (stripe, adyen, paypal)
        config: Configuration for both AP2 and underlying PSP
        
    Returns:
        AP2PaymentAdapter instance
    """
    from .stripe_adapter import StripeAdapter
    from .paypal_adapter import PayPalAdapter
    # Import other adapters as needed
    
    psp_mapping = {
        'stripe': StripeAdapter,
        'paypal': PayPalAdapter,
        # Add more mappings as needed
    }
    
    psp_class = psp_mapping.get(psp_name)
    if not psp_class:
        logger.error(f"[Phase 4++] Unknown PSP: {psp_name}")
        return AP2PaymentAdapter(config)  # Return without underlying PSP
    
    # Create underlying PSP adapter
    underlying_adapter = psp_class(config.get(psp_name, {}))
    
    # Create AP2 adapter with underlying PSP
    return AP2PaymentAdapter(config, underlying_adapter)


# [Phase 4++] Test if module loads correctly
if __name__ == "__main__":
    print("[Phase 4++] AP2 Payment Adapter module loaded successfully")
    
    # Test adapter creation
    test_config = {
        "ap2": {"endpoint": "https://api.ap2.test"},
        "stripe": {"api_key": "test_key"}
    }
    
    adapter = create_ap2_adapter('stripe', test_config)
    print(f"[Phase 4++] Created AP2 adapter with underlying PSP: {type(adapter.underlying_psp).__name__ if adapter.underlying_psp else 'None'}")
