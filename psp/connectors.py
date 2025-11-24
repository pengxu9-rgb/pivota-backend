"""
PSP Connectors
Payment Service Provider integrations for Stripe, Adyen, PayPal
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass
import json

logger = logging.getLogger("psp_connectors")

@dataclass
class PaymentRequest:
    """Payment request model"""
    amount: float
    currency: str
    customer_email: str
    payment_method: str
    order_id: str
    metadata: Dict[str, Any] = None

@dataclass
class PaymentResponse:
    """Payment response model"""
    success: bool
    transaction_id: Optional[str]
    status: str
    fees: float
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = None

class StripeConnector:
    """Stripe payment connector"""
    
    def __init__(self, api_key: str, webhook_secret: str = None):
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.base_url = "https://api.stripe.com/v1"
        logger.info("Stripe connector initialized")
    
    async def create_payment_intent(self, request: PaymentRequest) -> PaymentResponse:
        """Create a Stripe payment intent"""
        try:
            import stripe
            stripe.api_key = self.api_key
            
            # Create payment intent
            intent = stripe.PaymentIntent.create(
                amount=int(request.amount * 100),  # Convert to cents
                currency=request.currency.lower(),
                payment_method_types=['card'],
                metadata={
                    "order_id": request.order_id,
                    "customer_email": request.customer_email,
                    **(request.metadata or {})
                }
            )
            
            # Calculate fees (Stripe: 2.9% + $0.30)
            fees = (request.amount * 0.029) + 0.30
            
            logger.info(f"Stripe payment intent created: {intent.id}")
            return PaymentResponse(
                success=True,
                transaction_id=intent.id,
                status="requires_payment_method",
                fees=fees,
                metadata={"client_secret": intent.client_secret}
            )
            
        except Exception as e:
            logger.error(f"Stripe payment intent creation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )
    
    async def confirm_payment(self, payment_intent_id: str) -> PaymentResponse:
        """Confirm a Stripe payment"""
        try:
            import stripe
            stripe.api_key = self.api_key
            
            intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            
            if intent.status == "succeeded":
                fees = (intent.amount / 100) * 0.029 + 0.30  # Convert back from cents
                return PaymentResponse(
                    success=True,
                    transaction_id=intent.id,
                    status="succeeded",
                    fees=fees
                )
            else:
                return PaymentResponse(
                    success=False,
                    transaction_id=intent.id,
                    status=intent.status,
                    fees=0.0,
                    error_message=f"Payment not completed: {intent.status}"
                )
                
        except Exception as e:
            logger.error(f"Stripe payment confirmation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=payment_intent_id,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )

class AdyenConnector:
    """Adyen payment connector"""
    
    def __init__(self, api_key: str, merchant_account: str, environment: str = "test"):
        self.api_key = api_key
        self.merchant_account = merchant_account
        self.environment = environment
        self.base_url = f"https://checkout-test.adyen.com/v71" if environment == "test" else "https://checkout-live.adyen.com/v71"
        logger.info(f"Adyen connector initialized for {environment} environment")
    
    async def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        """Create an Adyen payment"""
        try:
            import requests
            
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }
            
            payload = {
                "amount": {
                    "value": int(request.amount * 100),  # Convert to minor units
                    "currency": request.currency
                },
                "reference": request.order_id,
                "merchantAccount": self.merchant_account,
                "paymentMethod": {
                    "type": "scheme",
                    "encryptedCardNumber": "test_4111111111111111",  # Test card
                    "encryptedExpiryMonth": "test_03",
                    "encryptedExpiryYear": "test_2030",
                    "encryptedSecurityCode": "test_737"
                },
                "returnUrl": "https://your-company.com/checkout?shopperOrder=12xy..",
                "metadata": request.metadata or {}
            }
            
            response = requests.post(
                f"{self.base_url}/payments",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                
                # Calculate fees (Adyen: 1.4% + $0.25)
                fees = (request.amount * 0.014) + 0.25
                
                logger.info(f"Adyen payment created: {result.get('pspReference')}")
                return PaymentResponse(
                    success=True,
                    transaction_id=result.get("pspReference"),
                    status=result.get("resultCode", "unknown"),
                    fees=fees,
                    metadata=result
                )
            else:
                logger.error(f"Adyen payment creation failed: {response.status_code}")
                return PaymentResponse(
                    success=False,
                    transaction_id=None,
                    status="failed",
                    fees=0.0,
                    error_message=f"API error: {response.status_code}"
                )
                
        except Exception as e:
            logger.error(f"Adyen payment creation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )

class PayPalConnector:
    """PayPal payment connector"""
    
    def __init__(self, client_id: str, client_secret: str, environment: str = "sandbox"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment
        self.base_url = "https://api.sandbox.paypal.com" if environment == "sandbox" else "https://api.paypal.com"
        self.access_token = None
        logger.info(f"PayPal connector initialized for {environment} environment")
    
    async def _get_access_token(self) -> Optional[str]:
        """Get PayPal access token"""
        try:
            import requests
            
            auth = (self.client_id, self.client_secret)
            data = {"grant_type": "client_credentials"}
            
            response = requests.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=auth,
                data=data,
                headers={"Accept": "application/json", "Accept-Language": "en_US"}
            )
            
            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data["access_token"]
                return self.access_token
            else:
                logger.error(f"PayPal token request failed: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"PayPal token request failed: {e}")
            return None
    
    async def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        """Create a PayPal payment"""
        try:
            import requests
            
            if not self.access_token:
                await self._get_access_token()
            
            if not self.access_token:
                return PaymentResponse(
                    success=False,
                    transaction_id=None,
                    status="failed",
                    fees=0.0,
                    error_message="Failed to get access token"
                )
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "intent": "sale",
                "payer": {
                    "payment_method": "paypal"
                },
                "transactions": [{
                    "amount": {
                        "total": str(request.amount),
                        "currency": request.currency
                    },
                    "description": f"Payment for order {request.order_id}",
                    "custom": request.order_id
                }],
                "redirect_urls": {
                    "return_url": "https://your-company.com/success",
                    "cancel_url": "https://your-company.com/cancel"
                }
            }
            
            response = requests.post(
                f"{self.base_url}/v1/payments/payment",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 201:
                result = response.json()
                
                # Calculate fees (PayPal: 2.9% + $0.30)
                fees = (request.amount * 0.029) + 0.30
                
                logger.info(f"PayPal payment created: {result.get('id')}")
                return PaymentResponse(
                    success=True,
                    transaction_id=result.get("id"),
                    status="created",
                    fees=fees,
                    metadata=result
                )
            else:
                logger.error(f"PayPal payment creation failed: {response.status_code}")
                return PaymentResponse(
                    success=False,
                    transaction_id=None,
                    status="failed",
                    fees=0.0,
                    error_message=f"API error: {response.status_code}"
                )
                
        except Exception as e:
            logger.error(f"PayPal payment creation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )

class PSPManager:
    """PSP Manager for handling multiple payment providers"""
    
    def __init__(self):
        self.connectors: Dict[str, Any] = {}
        self.routing_rules: Dict[str, Any] = {}
        logger.info("PSP Manager initialized")
    
    def register_connector(self, name: str, connector: Any, routing_rules: Dict[str, Any] = None):
        """Register a PSP connector"""
        self.connectors[name] = connector
        self.routing_rules[name] = routing_rules or {}
        logger.info(f"Registered PSP connector: {name}")
    
    async def select_psp(self, request: PaymentRequest) -> str:
        """Select the best PSP based on routing rules"""
        # Simple routing logic - can be enhanced with ML/AI
        available_psps = list(self.connectors.keys())
        
        if not available_psps:
            raise Exception("No PSP connectors available")
        
        # For demo, use round-robin or amount-based selection
        if request.amount < 10:
            return "stripe" if "stripe" in available_psps else available_psps[0]
        elif request.amount < 100:
            return "adyen" if "adyen" in available_psps else available_psps[0]
        else:
            return "paypal" if "paypal" in available_psps else available_psps[0]
    
    async def process_payment(self, request: PaymentRequest, psp_name: str = None) -> PaymentResponse:
        """Process payment through specified or best PSP"""
        try:
            if psp_name and psp_name in self.connectors:
                selected_psp = psp_name
            else:
                selected_psp = await self.select_psp(request)
            
            connector = self.connectors[selected_psp]
            logger.info(f"Processing payment via {selected_psp}")
            
            # Process payment based on connector type
            if hasattr(connector, 'create_payment_intent'):
                return await connector.create_payment_intent(request)
            elif hasattr(connector, 'create_payment'):
                return await connector.create_payment(request)
            else:
                raise Exception(f"Unknown connector type: {type(connector)}")
                
        except Exception as e:
            logger.error(f"Payment processing failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )
    
    async def get_psp_status(self) -> Dict[str, Any]:
        """Get status of all registered PSPs"""
        status = {}
        for name, connector in self.connectors.items():
            try:
                # Simple health check - can be enhanced
                status[name] = {
                    "active": True,
                    "last_check": datetime.now().isoformat(),
                    "type": type(connector).__name__
                }
            except Exception as e:
                status[name] = {
                    "active": False,
                    "error": str(e),
                    "last_check": datetime.now().isoformat()
                }
        return status

# Global PSP Manager instance
psp_manager = PSPManager()
