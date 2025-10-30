"""
Square Payment Adapter
Handles Square API integration for payments
"""
import logging
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
import httpx
from datetime import datetime
import uuid

from .base_psp_adapter import BasePSPAdapter, PaymentStatus

logger = logging.getLogger(__name__)


class SquareAdapter(BasePSPAdapter):
    """Square payment adapter implementation"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize Square adapter with configuration"""
        super().__init__(config)
        self.access_token = config.get('access_token')
        self.location_id = config.get('location_id')
        self.application_id = config.get('application_id')
        self.environment = config.get('environment', 'sandbox')
        
        # Set base URL based on environment
        if self.environment == 'production':
            self.base_url = 'https://connect.squareup.com/v2'
        else:
            self.base_url = 'https://connect.squareupsandbox.com/v2'
            
        self.headers = {
            'Square-Version': '2024-01-18',
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
    
    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """Validate Square configuration"""
        if not self.access_token:
            return False, "Square access_token is required"
        if not self.location_id:
            return False, "Square location_id is required"
        return True, None
    
    async def create_payment(
        self,
        amount: Decimal,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Create a payment with Square"""
        try:
            # Square expects amount in smallest currency unit (cents)
            amount_cents = int(amount * 100)
            
            # Generate idempotency key
            idempotency_key = str(uuid.uuid4())
            
            payment_data = {
                "idempotency_key": idempotency_key,
                "amount_money": {
                    "amount": amount_cents,
                    "currency": currency.upper()
                },
                "source_id": "EXTERNAL",  # For manual payment entry
                "location_id": self.location_id,
                "reference_id": order_id,
                "note": f"Order {order_id}"
            }
            
            # Add customer email if provided
            if customer_email:
                payment_data["buyer_email_address"] = customer_email
            
            # Add metadata
            if metadata:
                payment_data["custom_attributes"] = {
                    k: str(v) for k, v in metadata.items()
                }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    json=payment_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('errors', [{}])[0].get('detail', 'Unknown error')
                    logger.error(f"Square payment creation failed: {error_msg}")
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                payment = response_data.get('payment', {})
                
                return {
                    'success': True,
                    'psp_payment_id': payment.get('id'),
                    'status': self._map_square_status(payment.get('status')),
                    'amount': amount,
                    'currency': currency,
                    'psp_response': response_data,
                    'payment_url': None,  # Square doesn't provide payment URLs for API payments
                    'metadata': {
                        'location_id': payment.get('location_id'),
                        'order_id': payment.get('reference_id'),
                        'receipt_url': payment.get('receipt_url')
                    }
                }
                
        except Exception as e:
            logger.error(f"Square payment creation error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_payment_status(self, psp_payment_id: str) -> Dict[str, Any]:
        """Get payment status from Square"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/payments/{psp_payment_id}",
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('errors', [{}])[0].get('detail', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg
                    }
                
                payment = response_data.get('payment', {})
                
                return {
                    'success': True,
                    'status': self._map_square_status(payment.get('status')),
                    'psp_status': payment.get('status'),
                    'amount': Decimal(payment.get('amount_money', {}).get('amount', 0)) / 100,
                    'currency': payment.get('amount_money', {}).get('currency'),
                    'psp_response': response_data,
                    'metadata': {
                        'card_details': payment.get('card_details'),
                        'receipt_url': payment.get('receipt_url'),
                        'updated_at': payment.get('updated_at')
                    }
                }
                
        except Exception as e:
            logger.error(f"Square payment status error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def capture_payment(self, psp_payment_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Capture a payment (Square auto-captures, so this just verifies status)"""
        # Square automatically captures payments, so we just check the status
        return await self.get_payment_status(psp_payment_id)
    
    async def refund_payment(
        self,
        psp_payment_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refund a payment through Square"""
        try:
            # Get payment details first
            payment_status = await self.get_payment_status(psp_payment_id)
            if not payment_status.get('success'):
                return payment_status
            
            # Use full amount if not specified
            if amount is None:
                amount = payment_status.get('amount')
            
            amount_cents = int(amount * 100)
            currency = payment_status.get('currency')
            
            refund_data = {
                "idempotency_key": str(uuid.uuid4()),
                "payment_id": psp_payment_id,
                "amount_money": {
                    "amount": amount_cents,
                    "currency": currency
                }
            }
            
            if reason:
                refund_data["reason"] = reason
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/refunds",
                    json=refund_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('errors', [{}])[0].get('detail', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                refund = response_data.get('refund', {})
                
                return {
                    'success': True,
                    'refund_id': refund.get('id'),
                    'status': 'refunded' if refund.get('status') == 'COMPLETED' else 'refund_pending',
                    'amount': amount,
                    'currency': currency,
                    'psp_response': response_data
                }
                
        except Exception as e:
            logger.error(f"Square refund error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_webhook(self, url: str, events: list) -> Dict[str, Any]:
        """Create a webhook subscription in Square"""
        try:
            # Map our events to Square events
            square_events = []
            event_mapping = {
                'payment.completed': 'payment.updated',
                'payment.failed': 'payment.updated',
                'refund.completed': 'refund.updated'
            }
            
            for event in events:
                if event in event_mapping:
                    square_events.append(event_mapping[event])
            
            webhook_data = {
                "subscription": {
                    "name": f"Pivota Webhook - {datetime.now().isoformat()}",
                    "event_types": square_events,
                    "notification_url": url,
                    "api_version": "2024-01-18"
                },
                "idempotency_key": str(uuid.uuid4())
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/webhooks/subscriptions",
                    json=webhook_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('errors', [{}])[0].get('detail', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg
                    }
                
                subscription = response_data.get('subscription', {})
                
                return {
                    'success': True,
                    'webhook_id': subscription.get('id'),
                    'webhook_url': url,
                    'events': square_events,
                    'psp_response': response_data
                }
                
        except Exception as e:
            logger.error(f"Square webhook creation error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _map_square_status(self, square_status: str) -> PaymentStatus:
        """Map Square payment status to our internal status"""
        status_mapping = {
            'APPROVED': PaymentStatus.PROCESSING,
            'COMPLETED': PaymentStatus.PAID,
            'CANCELED': PaymentStatus.CANCELLED,
            'FAILED': PaymentStatus.FAILED,
            'PENDING': PaymentStatus.PENDING
        }
        return status_mapping.get(square_status, PaymentStatus.PENDING)
    
    async def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate Square webhook signature"""
        # Square webhook validation
        signature = headers.get('x-square-hmacsha256-signature')
        if not signature:
            return False
        
        # Implement HMAC validation here
        # For now, return True for development
        return True
    
    async def parse_webhook(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Square webhook data"""
        event_type = data.get('type')
        event_data = data.get('data', {})
        
        if event_type == 'payment.updated':
            payment = event_data.get('object', {}).get('payment', {})
            return {
                'event_type': 'payment.updated',
                'payment_id': payment.get('id'),
                'status': self._map_square_status(payment.get('status')),
                'amount': Decimal(payment.get('amount_money', {}).get('amount', 0)) / 100,
                'currency': payment.get('amount_money', {}).get('currency'),
                'metadata': payment
            }
        
        elif event_type == 'refund.updated':
            refund = event_data.get('object', {}).get('refund', {})
            return {
                'event_type': 'refund.updated',
                'refund_id': refund.get('id'),
                'payment_id': refund.get('payment_id'),
                'status': 'refunded' if refund.get('status') == 'COMPLETED' else 'refund_pending',
                'amount': Decimal(refund.get('amount_money', {}).get('amount', 0)) / 100,
                'currency': refund.get('amount_money', {}).get('currency'),
                'metadata': refund
            }
        
        return {
            'event_type': event_type,
            'data': data
        }


