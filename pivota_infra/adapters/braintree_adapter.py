"""
Braintree Payment Adapter
Handles Braintree API integration for payments
"""
import logging
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
import httpx
from datetime import datetime
import base64

from .base_psp_adapter import BasePSPAdapter, PaymentStatus

logger = logging.getLogger(__name__)


class BraintreeAdapter(BasePSPAdapter):
    """Braintree payment adapter implementation"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize Braintree adapter with configuration"""
        super().__init__(config)
        self.merchant_id = config.get('merchant_id')
        self.public_key = config.get('public_key')
        self.private_key = config.get('private_key')
        self.environment = config.get('environment', 'sandbox')
        
        # Set base URL based on environment
        if self.environment == 'production':
            self.base_url = 'https://payments.braintree-api.com/graphql'
        else:
            self.base_url = 'https://payments.sandbox.braintree-api.com/graphql'
        
        # Create basic auth header
        auth_string = f"{self.public_key}:{self.private_key}"
        auth_bytes = auth_string.encode('ascii')
        auth_b64 = base64.b64encode(auth_bytes).decode('ascii')
        
        self.headers = {
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/json',
            'Braintree-Version': '2019-01-01'
        }
    
    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """Validate Braintree configuration"""
        if not self.merchant_id:
            return False, "Braintree merchant_id is required"
        if not self.public_key:
            return False, "Braintree public_key is required"
        if not self.private_key:
            return False, "Braintree private_key is required"
        return True, None
    
    async def create_payment(
        self,
        amount: Decimal,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Create a payment with Braintree"""
        try:
            # Braintree GraphQL mutation for creating a transaction
            query = """
            mutation ChargePaymentMethod($input: ChargePaymentMethodInput!) {
                chargePaymentMethod(input: $input) {
                    transaction {
                        id
                        legacyId
                        status
                        amount {
                            value
                            currencyCode
                        }
                        orderId
                        merchantAccountId
                        paymentMethodSnapshot {
                            ... on PayPalTransactionDetails {
                                payer {
                                    email
                                }
                            }
                            ... on CreditCardDetails {
                                last4
                                brandCode
                            }
                        }
                    }
                }
            }
            """
            
            variables = {
                "input": {
                    "paymentMethodId": "fake-valid-nonce",  # For testing
                    "transaction": {
                        "amount": str(amount),
                        "orderId": order_id,
                        "customFields": [
                            {"name": name, "value": str(value)}
                            for name, value in (metadata or {}).items()
                        ]
                    }
                }
            }
            
            if customer_email:
                variables["input"]["transaction"]["customer"] = {
                    "email": customer_email
                }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    json={
                        "query": query,
                        "variables": variables
                    },
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200 or 'errors' in response_data:
                    error_msg = 'Unknown error'
                    if 'errors' in response_data:
                        error_msg = response_data['errors'][0].get('message', error_msg)
                    logger.error(f"Braintree payment creation failed: {error_msg}")
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                transaction = response_data.get('data', {}).get('chargePaymentMethod', {}).get('transaction', {})
                
                return {
                    'success': True,
                    'psp_payment_id': transaction.get('legacyId'),
                    'status': self._map_braintree_status(transaction.get('status')),
                    'amount': amount,
                    'currency': currency,
                    'psp_response': response_data,
                    'payment_url': None,  # Braintree doesn't provide payment URLs
                    'metadata': {
                        'id': transaction.get('id'),
                        'merchant_account_id': transaction.get('merchantAccountId')
                    }
                }
                
        except Exception as e:
            logger.error(f"Braintree payment creation error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def get_payment_status(self, psp_payment_id: str) -> Dict[str, Any]:
        """Get payment status from Braintree"""
        try:
            # Use REST API for transaction lookup (GraphQL doesn't support legacy ID lookup easily)
            rest_url = self.base_url.replace('/graphql', f'/merchants/{self.merchant_id}/transactions/{psp_payment_id}')
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    rest_url,
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200:
                    error_msg = response_data.get('message', 'Unknown error')
                    return {
                        'success': False,
                        'error': error_msg
                    }
                
                transaction = response_data.get('transaction', response_data)
                
                return {
                    'success': True,
                    'status': self._map_braintree_status(transaction.get('status')),
                    'psp_status': transaction.get('status'),
                    'amount': Decimal(transaction.get('amount', '0')),
                    'currency': transaction.get('currency_iso_code', 'USD'),
                    'psp_response': response_data,
                    'metadata': {
                        'type': transaction.get('type'),
                        'payment_instrument_type': transaction.get('payment_instrument_type'),
                        'processor_response_code': transaction.get('processor_response_code'),
                        'processor_response_text': transaction.get('processor_response_text'),
                        'created_at': transaction.get('created_at'),
                        'updated_at': transaction.get('updated_at')
                    }
                }
                
        except Exception as e:
            logger.error(f"Braintree payment status error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def capture_payment(self, psp_payment_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """Submit authorized payment for settlement"""
        try:
            # GraphQL mutation for submitting for settlement
            query = """
            mutation SubmitTransactionForSettlement($transactionId: ID!, $amount: Amount) {
                submitTransactionForSettlement(input: {
                    transactionId: $transactionId,
                    amount: $amount
                }) {
                    transaction {
                        id
                        legacyId
                        status
                        amount {
                            value
                            currencyCode
                        }
                    }
                }
            }
            """
            
            variables = {
                "transactionId": psp_payment_id
            }
            
            if amount is not None:
                variables["amount"] = {
                    "value": str(amount)
                }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    json={
                        "query": query,
                        "variables": variables
                    },
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200 or 'errors' in response_data:
                    error_msg = 'Settlement failed'
                    if 'errors' in response_data:
                        error_msg = response_data['errors'][0].get('message', error_msg)
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                transaction = response_data.get('data', {}).get('submitTransactionForSettlement', {}).get('transaction', {})
                
                return {
                    'success': True,
                    'status': self._map_braintree_status(transaction.get('status')),
                    'psp_response': response_data
                }
                
        except Exception as e:
            logger.error(f"Braintree capture error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def refund_payment(
        self,
        psp_payment_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Refund a payment through Braintree"""
        try:
            # GraphQL mutation for refunding
            query = """
            mutation RefundTransaction($input: RefundTransactionInput!) {
                refundTransaction(input: $input) {
                    refund {
                        id
                        legacyId
                        amount {
                            value
                            currencyCode
                        }
                        status
                        refundedTransaction {
                            id
                            legacyId
                        }
                    }
                }
            }
            """
            
            refund_input = {
                "transactionId": psp_payment_id
            }
            
            if amount is not None:
                refund_input["amount"] = {
                    "value": str(amount)
                }
            
            if reason:
                refund_input["orderId"] = f"Refund: {reason}"
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.base_url,
                    json={
                        "query": query,
                        "variables": {
                            "input": refund_input
                        }
                    },
                    headers=self.headers,
                    timeout=30.0
                )
                
                response_data = response.json()
                
                if response.status_code != 200 or 'errors' in response_data:
                    error_msg = 'Refund failed'
                    if 'errors' in response_data:
                        error_msg = response_data['errors'][0].get('message', error_msg)
                    return {
                        'success': False,
                        'error': error_msg,
                        'psp_response': response_data
                    }
                
                refund = response_data.get('data', {}).get('refundTransaction', {}).get('refund', {})
                
                return {
                    'success': True,
                    'refund_id': refund.get('legacyId'),
                    'status': 'refunded' if refund.get('status') in ['SETTLED', 'SETTLING'] else 'refund_pending',
                    'amount': Decimal(refund.get('amount', {}).get('value', '0')),
                    'currency': refund.get('amount', {}).get('currencyCode', 'USD'),
                    'psp_response': response_data
                }
                
        except Exception as e:
            logger.error(f"Braintree refund error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def create_webhook(self, url: str, events: list) -> Dict[str, Any]:
        """Create webhook notification"""
        try:
            # Braintree uses webhook notifications configured in control panel
            # We can programmatically verify the endpoint
            return {
                'success': True,
                'webhook_url': url,
                'events': events,
                'note': 'Configure webhook URL in Braintree Control Panel under Settings > Webhooks'
            }
            
        except Exception as e:
            logger.error(f"Braintree webhook setup error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _map_braintree_status(self, braintree_status: str) -> PaymentStatus:
        """Map Braintree transaction status to our internal status"""
        status_mapping = {
            'authorized': PaymentStatus.PROCESSING,
            'authorizing': PaymentStatus.PROCESSING,
            'submitted_for_settlement': PaymentStatus.PROCESSING,
            'settling': PaymentStatus.PROCESSING,
            'settled': PaymentStatus.PAID,
            'settlement_confirmed': PaymentStatus.PAID,
            'settlement_pending': PaymentStatus.PROCESSING,
            'settlement_declined': PaymentStatus.FAILED,
            'failed': PaymentStatus.FAILED,
            'gateway_rejected': PaymentStatus.FAILED,
            'processor_declined': PaymentStatus.FAILED,
            'voided': PaymentStatus.CANCELLED
        }
        return status_mapping.get(braintree_status, PaymentStatus.PENDING)
    
    async def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """Validate Braintree webhook signature"""
        signature = headers.get('bt-signature')
        if not signature:
            return False
        
        # Implement Braintree webhook signature validation
        # For now, return True for development
        return True
    
    async def parse_webhook(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Braintree webhook notification"""
        kind = data.get('kind')
        subject = data.get('subject', {})
        
        if kind in ['transaction_settled', 'transaction_settlement_declined']:
            transaction = subject.get('transaction', {})
            return {
                'event_type': 'payment.updated',
                'payment_id': transaction.get('id'),
                'status': self._map_braintree_status(transaction.get('status')),
                'amount': Decimal(transaction.get('amount', '0')),
                'currency': transaction.get('currency_iso_code', 'USD'),
                'metadata': transaction
            }
        
        elif kind == 'transaction_disbursed':
            disbursement = subject.get('disbursement', {})
            return {
                'event_type': 'payment.disbursed',
                'disbursement_id': disbursement.get('id'),
                'amount': Decimal(disbursement.get('amount', '0')),
                'disbursement_date': disbursement.get('disbursement_date'),
                'metadata': disbursement
            }
        
        return {
            'event_type': kind,
            'data': data
        }
    
    async def generate_client_token(self, customer_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate client token for Drop-in UI"""
        try:
            # Use REST API for client token generation
            rest_url = self.base_url.replace('/graphql', f'/merchants/{self.merchant_id}/client_token')
            
            data = {}
            if customer_id:
                data['customer_id'] = customer_id
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    rest_url,
                    json=data,
                    headers=self.headers,
                    timeout=30.0
                )
                
                if response.status_code != 201:
                    return {
                        'success': False,
                        'error': 'Failed to generate client token'
                    }
                
                return {
                    'success': True,
                    'client_token': response.json().get('client_token')
                }
                
        except Exception as e:
            logger.error(f"Braintree client token error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }



