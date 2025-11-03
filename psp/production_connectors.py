"""
Production PSP Connectors
Using your real Stripe and Adyen production credentials
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional
import requests

from psp.production_config import get_stripe_config, get_adyen_config
from psp.connectors import PaymentRequest, PaymentResponse

logger = logging.getLogger("production_psp_connectors")

class ProductionStripeConnector:
    """Production Stripe connector with your real credentials"""
    
    def __init__(self):
        config = get_stripe_config()
        self.api_key = config["api_key"]
        self.publishable_key = config["publishable_key"]
        self.webhook_secret = config["webhook_secret"]
        self.environment = config["environment"]
        self.fees = config["fees"]
        logger.info(f"Production Stripe connector initialized for {self.environment} environment")
    
    async def create_payment_intent(self, request: PaymentRequest) -> PaymentResponse:
        """Create a production Stripe payment intent"""
        try:
            import stripe
            stripe.api_key = self.api_key
            
            # Create payment intent with production settings
            intent = stripe.PaymentIntent.create(
                amount=int(request.amount * 100),  # Convert to cents
                currency=request.currency.lower(),
                payment_method_types=['card'],
                capture_method='automatic',
                metadata={
                    "order_id": request.order_id,
                    "customer_email": request.customer_email,
                    "merchant_id": request.metadata.get("merchant_id", "unknown"),
                    "agent_id": request.metadata.get("agent_id", "unknown"),
                    **(request.metadata or {})
                },
                description=f"Payment for order {request.order_id}",
                receipt_email=request.customer_email
            )
            
            # Calculate fees (Stripe: 2.9% + $0.30)
            fees = (request.amount * self.fees["percentage"] / 100) + self.fees["fixed"]
            
            logger.info(f"Production Stripe payment intent created: {intent.id}")
            return PaymentResponse(
                success=True,
                transaction_id=intent.id,
                status="requires_payment_method",
                fees=fees,
                metadata={
                    "client_secret": intent.client_secret,
                    "publishable_key": self.publishable_key,
                    "environment": self.environment
                }
            )
            
        except Exception as e:
            logger.error(f"Production Stripe payment intent creation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )
    
    async def confirm_payment(self, payment_intent_id: str) -> PaymentResponse:
        """Confirm a production Stripe payment"""
        try:
            import stripe
            stripe.api_key = self.api_key
            
            intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            
            if intent.status == "succeeded":
                fees = (intent.amount / 100) * (self.fees["percentage"] / 100) + self.fees["fixed"]
                return PaymentResponse(
                    success=True,
                    transaction_id=intent.id,
                    status="succeeded",
                    fees=fees,
                    metadata={"environment": self.environment}
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
            logger.error(f"Production Stripe payment confirmation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=payment_intent_id,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )

class ProductionAdyenConnector:
    """Production Adyen connector with your real credentials"""
    
    def __init__(self):
        config = get_adyen_config()
        self.api_key = config["api_key"]
        self.merchant_account = config["merchant_account"]
        self.webhook_secret = config["webhook_secret"]
        self.environment = config["environment"]
        self.fees = config["fees"]
        
        # Production Adyen URLs
        if self.environment == "live":
            self.base_url = "https://checkout-live.adyen.com/v71"
        else:
            self.base_url = "https://checkout-test.adyen.com/v71"
        
        logger.info(f"Production Adyen connector initialized for {self.environment} environment")
    
    async def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        """Create a production Adyen payment"""
        try:
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
                    "encryptedCardNumber": "test_4111111111111111",  # Test card for now
                    "encryptedExpiryMonth": "test_03",
                    "encryptedExpiryYear": "test_2030",
                    "encryptedSecurityCode": "test_737"
                },
                "returnUrl": "https://your-company.com/checkout?shopperOrder=12xy..",
                "metadata": {
                    "order_id": request.order_id,
                    "customer_email": request.customer_email,
                    "merchant_id": request.metadata.get("merchant_id", "unknown"),
                    "agent_id": request.metadata.get("agent_id", "unknown"),
                    **(request.metadata or {})
                }
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
                fees = (request.amount * self.fees["percentage"] / 100) + self.fees["fixed"]
                
                logger.info(f"Production Adyen payment created: {result.get('pspReference')}")
                return PaymentResponse(
                    success=True,
                    transaction_id=result.get("pspReference"),
                    status=result.get("resultCode", "unknown"),
                    fees=fees,
                    metadata={
                        **result,
                        "environment": self.environment,
                        "merchant_account": self.merchant_account
                    }
                )
            else:
                logger.error(f"Production Adyen payment creation failed: {response.status_code}")
                return PaymentResponse(
                    success=False,
                    transaction_id=None,
                    status="failed",
                    fees=0.0,
                    error_message=f"API error: {response.status_code} - {response.text}"
                )
                
        except Exception as e:
            logger.error(f"Production Adyen payment creation failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )

class ProductionPSPManager:
    """Production PSP Manager with real credentials"""
    
    def __init__(self):
        self.connectors = {}
        self.routing_rules = {}
        self._initialize_production_connectors()
        logger.info("Production PSP Manager initialized")
    
    def _initialize_production_connectors(self):
        """Initialize production connectors"""
        try:
            # Stripe production connector
            stripe_connector = ProductionStripeConnector()
            self.connectors["stripe"] = stripe_connector
            self.routing_rules["stripe"] = {
                "min_amount": 0.50,
                "max_amount": 100000.0,
                "priority": 1,
                "supported_currencies": ["USD", "EUR", "GBP", "CAD", "AUD"]
            }
            
            # Adyen production connector
            adyen_connector = ProductionAdyenConnector()
            self.connectors["adyen"] = adyen_connector
            self.routing_rules["adyen"] = {
                "min_amount": 0.50,
                "max_amount": 50000.0,
                "priority": 2,
                "supported_currencies": ["USD", "EUR", "GBP", "AUD", "CAD"]
            }
            
            logger.info("✅ Production PSP connectors initialized")
            logger.info(f"   🔌 Stripe: {stripe_connector.environment}")
            logger.info(f"   🔌 Adyen: {adyen_connector.environment}")
            
        except Exception as e:
            logger.error(f"Failed to initialize production connectors: {e}")
    
    async def select_psp(self, request: PaymentRequest) -> str:
        """Select the best PSP for production"""
        available_psps = list(self.connectors.keys())
        
        if not available_psps:
            raise Exception("No production PSP connectors available")
        
        # Production routing logic
        if request.amount < 10:
            return "stripe" if "stripe" in available_psps else available_psps[0]
        elif request.amount < 100:
            return "adyen" if "adyen" in available_psps else available_psps[0]
        else:
            return "stripe" if "stripe" in available_psps else available_psps[0]
    
    async def process_payment(self, request: PaymentRequest, psp_name: str = None) -> PaymentResponse:
        """Process payment through production PSP"""
        try:
            if psp_name and psp_name in self.connectors:
                selected_psp = psp_name
            else:
                selected_psp = await self.select_psp(request)
            
            connector = self.connectors[selected_psp]
            logger.info(f"Processing production payment via {selected_psp}")
            
            # Process payment based on connector type
            if hasattr(connector, 'create_payment_intent'):
                return await connector.create_payment_intent(request)
            elif hasattr(connector, 'create_payment'):
                return await connector.create_payment(request)
            else:
                raise Exception(f"Unknown connector type: {type(connector)}")
                
        except Exception as e:
            logger.error(f"Production payment processing failed: {e}")
            return PaymentResponse(
                success=False,
                transaction_id=None,
                status="failed",
                fees=0.0,
                error_message=str(e)
            )
    
    async def get_psp_status(self) -> Dict[str, Any]:
        """Get status of production PSPs"""
        status = {}
        for name, connector in self.connectors.items():
            try:
                status[name] = {
                    "active": True,
                    "environment": getattr(connector, 'environment', 'unknown'),
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

# Global production PSP manager
production_psp_manager = ProductionPSPManager()
