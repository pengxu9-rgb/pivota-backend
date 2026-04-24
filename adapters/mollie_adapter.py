"""
Mollie Payment Adapter
Handles Mollie API integration for payments
"""
import logging
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
import httpx
from datetime import datetime

from .base_psp_adapter import BasePSPAdapter, PaymentStatus
from config.settings import resolve_public_api_base_url

logger = logging.getLogger(__name__)


class MollieAdapter(BasePSPAdapter):
    """Mollie payment adapter implementation"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize Mollie adapter with configuration"""
        super().__init__(config)
        self.api_key = config.get('api_key')
        self.profile_id = config.get('profile_id')
        self.test_mode = config.get('test_mode', True)
        
        # Mollie API base URL
        self.base_url = 'https://api.mollie.com/v2'
        self.webhook_base_url = str(
            config.get("webhook_base_url")
            or config.get("public_api_base_url")
            or resolve_public_api_base_url()
        ).rstrip("/")
        
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
    
    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """Validate Mollie configuration"""
        if not self.api_key:
            return False, "Mollie api_key is required"
        
        # Check if it's a test key when in test mode
        if self.test_mode and not self.api_key.startswith('test_'):
            return False, "Test mode requires a test API key (starting with 'test_')"
        elif not self.test_mode and self.api_key.startswith('test_'):
            return False, "Production mode requires a live API key"
            
        return True, None
    
    async def create_payment(
        self,
        amount: Decimal,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Create a payment with Mollie"""
        try:
            # Format amount with 2 decimal places
            amount_str = f"{amount:.2f}"
            
            payment_data = {
                "amount": {
                    "currency": currency.upper(),
                    "value": amount_str
                },
                "description": f"Order {order_id}",
                "redirectUrl": f"https://pivota.cc/order/{order_id}/complete",
                "webhookUrl": f"{self.webhook_base_url}/webhooks/mollie/{order_id}",
                "metadata": metadata or {}
            }
            
            # Add profile ID if configured
            if self.profile_id:
                payment_data["profileId"] = self.profile_id
            
            # Add customer email
            if customer_email:
                payment_data["metadata"]["email"] = customer_email
            
            # Add order ID to metadata
            payment_data["metadata"]["order_id"] = order_id
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    json=payment_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code not in [200, 201]:
                    error_msg = response_data.get('detail', 'Unknown error')
                    logger.error(f"Mollie payment creation failed: {error_msg}")
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                return {
                    'success': True,
                    'psp_payment_id': response_data.get('id'),
                    'status': self._map_mollie_status(response_data.get('status')),
                    'amount': amount,
                    'currency': currency,
                    'psp_response': response_data,
                    'payment_url': response_data.get('_links', {}).get('checkout', {}).get('href'),
                    'metadata': {
                        'profile_id': response_data.get('profileId'),
                        'mode': response_data.get('mode'),
                        'expires_at': response_data.get('expiresAt')
                    }
                }
                
        except Exception as e:
            logger.error(f"Mollie payment creation error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_payment_status(self, psp_payment_id: str) -> Dict[str, Any]:
        """Get payment status from Mollie"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/payments/{psp_payment_id}",
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('detail', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg
                    }
                
                amount_data = response_data.get('amount', {})
                
                return {
                    'success': True,
                    'status': self._map_mollie_status(response_data.get('status')),
                    'psp_status': response_data.get('status'),
                    'amount': Decimal(amount_data.get('value', '0')),
                    'currency': amount_data.get('currency'),
                    'psp_response': response_data,
                    'metadata': {
                        'method': response_data.get('method'),
                        'paid_at': response_data.get('paidAt'),
                        'failed_at': response_data.get('failedAt'),
                        'canceled_at': response_data.get('canceledAt'),
                        'settlement_amount': response_data.get('settlementAmount')
                    }
                }
                
        except Exception as e:
            logger.error(f"Mollie payment status error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def capture_payment(self, psp_payment_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Capture a payment (Mollie auto-captures most payments)"""
        # Most Mollie payment methods auto-capture
        # For methods that support manual capture (like Klarna), implement capture endpoint
        return await self.get_payment_status(psp_payment_id)
    
    async def refund_payment(
        self,
        psp_payment_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refund a payment through Mollie"""
        try:
            # Get payment details first
            payment_status = await self.get_payment_status(psp_payment_id)
            if not payment_status.get('success'):
                return payment_status
            
            # Check if payment is refundable
            if payment_status.get('status') != PaymentStatus.PAID:
                return {
                    'success': False,
                    'error': 'Payment must be paid before it can be refunded'
                }
            
            # Use full amount if not specified
            if amount is None:
                amount = payment_status.get('amount')
            
            amount_str = f"{amount:.2f}"
            currency = payment_status.get('currency')
            
            refund_data = {
                "amount": {
                    "currency": currency,
                    "value": amount_str
                }
            }
            
            if reason:
                refund_data["description"] = reason
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/{psp_payment_id}/refunds",
                    json=refund_data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code not in [200, 201]:
                    error_msg = response_data.get('detail', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                return {
                    'success': True,
                    'refund_id': response_data.get('id'),
                    'status': 'refunded' if response_data.get('status') == 'refunded' else 'refund_pending',
                    'amount': amount,
                    'currency': currency,
                    'psp_response': response_data
                }
                
        except Exception as e:
            logger.error(f"Mollie refund error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_webhook(self, url: str, events: list) -> Dict[str, Any]:
        """Create a webhook (Mollie uses per-payment webhooks)"""
        # Mollie doesn't have global webhook subscriptions
        # Webhooks are set per payment during creation
        return {
            'success': True,
            'webhook_url': url,
            'events': events,
            'note': 'Mollie webhooks are configured per payment'
        }
    
    def _map_mollie_status(self, mollie_status: str) -> PaymentStatus:
        """Map Mollie payment status to our internal status"""
        status_mapping = {
            'open': PaymentStatus.PENDING,
            'canceled': PaymentStatus.CANCELLED,
            'pending': PaymentStatus.PROCESSING,
            'authorized': PaymentStatus.PROCESSING,
            'expired': PaymentStatus.FAILED,
            'failed': PaymentStatus.FAILED,
            'paid': PaymentStatus.PAID
        }
        return status_mapping.get(mollie_status, PaymentStatus.PENDING)
    
    async def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate Mollie webhook"""
        # Mollie doesn't sign webhooks, but we can verify by fetching the payment
        return True
    
    async def parse_webhook(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Mollie webhook data"""
        # Mollie sends minimal webhook data, need to fetch full payment details
        payment_id = data.get('id')
        
        if payment_id:
            # Fetch full payment details
            payment_details = await self.get_payment_status(payment_id)
            
            if payment_details.get('success'):
                return {
                    'event_type': 'payment.updated',
                    'payment_id': payment_id,
                    'status': payment_details.get('status'),
                    'amount': payment_details.get('amount'),
                    'currency': payment_details.get('currency'),
                    'metadata': payment_details.get('metadata')
                }
        
        return {
            'event_type': 'unknown',
            'data': data
        }
    
    async def list_payment_methods(self) -> Dict[str, Any]:
        """List available payment methods"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/methods",
                    headers=self.headers,
                    params={
                        'include': 'issuers',
                        'includeWallets': 'applepay'
                    },
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    return {
                        'success': False,
                        'error': response_data.get('detail', 'Unknown error')
                    }
                
                methods = response_data.get('_embedded', {}).get('methods', [])
                
                return {
                    'success': True,
                    'methods': [
                        {
                            'id': method.get('id'),
                            'name': method.get('description'),
                            'image': method.get('image', {}).get('size2x'),
                            'min_amount': method.get('minimumAmount'),
                            'max_amount': method.get('maximumAmount')
                        }
                        for method in methods
                    ]
                }
                
        except Exception as e:
            logger.error(f"Mollie list methods error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }


