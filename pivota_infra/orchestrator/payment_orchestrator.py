"""
Payment Orchestrator
Centralized payment processing and orchestration
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
import json

from psp.connectors import psp_manager, PaymentRequest, PaymentResponse
from dashboard.core import dashboard_core, Order, Payment, OrderStatus, PSPType
from utils.event_publisher import event_publisher

logger = logging.getLogger("payment_orchestrator")

@dataclass
class OrchestrationResult:
    """Result of payment orchestration"""
    success: bool
    order_id: str
    payment_id: Optional[str]
    psp_used: Optional[str]
    transaction_id: Optional[str]
    amount: float
    currency: str
    fees: float
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = None

class PaymentOrchestrator:
    """Main payment orchestration service"""
    
    def __init__(self):
        self.retry_queue: List[Dict[str, Any]] = []
        self.max_retries = 3
        self.retry_delay = 5  # seconds
        logger.info("Payment Orchestrator initialized")
    
    async def process_order_payment(
        self, 
        order_data: Dict[str, Any],
        preferred_psp: Optional[str] = None
    ) -> OrchestrationResult:
        """Process payment for an order"""
        try:
            order_id = order_data.get("id", f"order_{datetime.now().timestamp()}")
            
            # Create payment request
            payment_request = PaymentRequest(
                amount=order_data.get("total_amount", 0.0),
                currency=order_data.get("currency", "USD"),
                customer_email=order_data.get("customer_email", ""),
                payment_method=order_data.get("payment_method", "card"),
                order_id=order_id,
                metadata=order_data.get("metadata", {})
            )
            
            # Select PSP
            if preferred_psp:
                selected_psp = preferred_psp
            else:
                selected_psp = await psp_manager.select_psp(payment_request)
            
            logger.info(f"Processing payment for order {order_id} via {selected_psp}")
            
            # Process payment
            payment_response = await psp_manager.process_payment(payment_request, selected_psp)
            
            # Create order record
            order = Order(
                id=order_id,
                merchant_id=order_data.get("merchant_id", "MERCH_001"),
                agent_id=order_data.get("agent_id"),
                customer_email=payment_request.customer_email,
                total_amount=payment_request.amount,
                currency=payment_request.currency,
                status=OrderStatus.PAID if payment_response.success else OrderStatus.FAILED,
                items=order_data.get("items", []),
                payment_method=payment_request.payment_method,
                psp_used=selected_psp,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                metadata=order_data.get("metadata", {})
            )
            
            # Create payment record
            payment = Payment(
                id=f"payment_{datetime.now().timestamp()}",
                order_id=order_id,
                amount=payment_request.amount,
                currency=payment_request.currency,
                psp=PSPType(selected_psp) if selected_psp in [e.value for e in PSPType] else PSPType.STRIPE,
                status="succeeded" if payment_response.success else "failed",
                transaction_id=payment_response.transaction_id,
                fees=payment_response.fees,
                created_at=datetime.now(),
                metadata=payment_response.metadata or {}
            )
            
            # Store in dashboard core
            dashboard_core.orders[order_id] = order
            dashboard_core.payments[payment.id] = payment
            
            # Publish events
            await self._publish_payment_events(order, payment, payment_response)
            
            result = OrchestrationResult(
                success=payment_response.success,
                order_id=order_id,
                payment_id=payment.id,
                psp_used=selected_psp,
                transaction_id=payment_response.transaction_id,
                amount=payment_request.amount,
                currency=payment_request.currency,
                fees=payment_response.fees,
                error_message=payment_response.error_message,
                metadata=payment_response.metadata
            )
            
            logger.info(f"Payment orchestration completed for order {order_id}: {result.success}")
            return result
            
        except Exception as e:
            logger.error(f"Payment orchestration failed: {e}")
            return OrchestrationResult(
                success=False,
                order_id=order_data.get("id", "unknown"),
                payment_id=None,
                psp_used=None,
                transaction_id=None,
                amount=order_data.get("total_amount", 0.0),
                currency=order_data.get("currency", "USD"),
                fees=0.0,
                error_message=str(e)
            )
    
    async def _publish_payment_events(
        self, 
        order: Order, 
        payment: Payment, 
        payment_response: PaymentResponse
    ):
        """Publish payment events to dashboard and external systems"""
        try:
            # Publish payment result event
            await event_publisher.publish_payment_result(
                order_id=order.id,
                payment_id=payment.id,
                success=payment_response.success,
                psp=payment.psp.value,
                amount=payment.amount,
                currency=payment.currency,
                fees=payment.fees,
                transaction_id=payment.transaction_id,
                error_message=payment_response.error_message,
                agent=order.agent_id or "system",
                agent_name=f"Agent {order.agent_id}" if order.agent_id else "System",
                merchant=order.merchant_id,
                merchant_name=f"Merchant {order.merchant_id}",
                latency_ms=100,  # Simulated latency
                attempt=1,
                additional_data={
                    "order_status": order.status.value,
                    "payment_method": order.payment_method,
                    "customer_email": order.customer_email
                }
            )
            
            # Publish order event
            await event_publisher.publish_order_event(
                order_id=order.id,
                merchant_id=order.merchant_id,
                agent_id=order.agent_id,
                status=order.status.value,
                total_amount=order.total_amount,
                currency=order.currency,
                items_count=len(order.items),
                customer_email=order.customer_email,
                additional_data={
                    "payment_id": payment.id,
                    "psp_used": payment.psp.value,
                    "transaction_id": payment.transaction_id
                }
            )
            
        except Exception as e:
            logger.error(f"Failed to publish payment events: {e}")
    
    async def retry_failed_payment(
        self, 
        order_id: str, 
        retry_count: int = 1
    ) -> OrchestrationResult:
        """Retry a failed payment"""
        try:
            if retry_count > self.max_retries:
                logger.warning(f"Max retries exceeded for order {order_id}")
                return OrchestrationResult(
                    success=False,
                    order_id=order_id,
                    payment_id=None,
                    psp_used=None,
                    transaction_id=None,
                    amount=0.0,
                    currency="USD",
                    fees=0.0,
                    error_message="Max retries exceeded"
                )
            
            # Get original order
            order = dashboard_core.orders.get(order_id)
            if not order:
                raise Exception(f"Order {order_id} not found")
            
            # Convert order back to order_data format
            order_data = {
                "id": order.id,
                "merchant_id": order.merchant_id,
                "agent_id": order.agent_id,
                "total_amount": order.total_amount,
                "currency": order.currency,
                "customer_email": order.customer_email,
                "payment_method": order.payment_method,
                "items": order.items,
                "metadata": order.metadata
            }
            
            logger.info(f"Retrying payment for order {order_id} (attempt {retry_count})")
            
            # Wait before retry
            await asyncio.sleep(self.retry_delay * retry_count)
            
            # Process payment again
            return await self.process_order_payment(order_data)
            
        except Exception as e:
            logger.error(f"Payment retry failed for order {order_id}: {e}")
            return OrchestrationResult(
                success=False,
                order_id=order_id,
                payment_id=None,
                psp_used=None,
                transaction_id=None,
                amount=0.0,
                currency="USD",
                fees=0.0,
                error_message=str(e)
            )
    
    async def get_orchestration_status(self) -> Dict[str, Any]:
        """Get orchestration system status"""
        try:
            psp_status = await psp_manager.get_psp_status()
            
            return {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "psp_status": psp_status,
                "retry_queue_size": len(self.retry_queue),
                "max_retries": self.max_retries,
                "total_orders": len(dashboard_core.orders),
                "total_payments": len(dashboard_core.payments)
            }
        except Exception as e:
            logger.error(f"Failed to get orchestration status: {e}")
            return {
                "status": "error",
                "timestamp": datetime.now().isoformat(),
                "error": str(e)
            }

# Global orchestrator instance
payment_orchestrator = PaymentOrchestrator()
