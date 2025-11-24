"""
PayPal Payment Adapter
Handles payment processing through PayPal REST API
"""

import httpx
import json
import base64
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import logging

from adapters.psp_adapter import PSPAdapter, PaymentIntent
from config.settings import settings

logger = logging.getLogger(__name__)


class PayPalAdapter(PSPAdapter):
    def __init__(self, client_id: str, client_secret: str, is_sandbox: bool = True):
        """
        Initialize PayPal adapter
        
        Args:
            client_id: PayPal Client ID
            client_secret: PayPal Client Secret
            is_sandbox: Whether to use sandbox environment
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.is_sandbox = is_sandbox
        
        # Set base URL based on environment
        if is_sandbox:
            self.base_url = "https://api-m.sandbox.paypal.com"
        else:
            self.base_url = "https://api-m.paypal.com"
        
        self.access_token = None
        self.token_expiry = None
    
    async def _get_access_token(self) -> str:
        """Get PayPal OAuth access token"""
        # Check if we have a valid token
        if self.access_token and self.token_expiry and datetime.now() < self.token_expiry:
            return self.access_token
        
        # Get new token
        auth_string = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data="grant_type=client_credentials"
            )
            
            if response.status_code != 200:
                logger.error(f"Failed to get PayPal access token: {response.text}")
                raise Exception(f"PayPal auth failed: {response.status_code}")
            
            data = response.json()
            self.access_token = data["access_token"]
            # Token expires in seconds, convert to datetime (subtract 60s for safety)
            expires_in = data.get("expires_in", 3600) - 60
            self.token_expiry = datetime.now().timestamp() + expires_in
            
            return self.access_token
    
    async def create_payment_intent(
        self, 
        amount: int,
        currency: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """
        Create a PayPal order
        
        Args:
            amount: Amount in cents
            currency: Currency code (e.g., 'USD')
            metadata: Additional metadata
        
        Returns:
            PaymentIntent object
        """
        try:
            # Get access token
            token = await self._get_access_token()
            
            # Convert amount from cents to dollars
            amount_in_dollars = amount / 100
            
            # Create order payload
            order_data = {
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": f"{amount_in_dollars:.2f}"
                    },
                    "description": metadata.get("description", "Payment") if metadata else "Payment"
                }],
                "application_context": {
                    "brand_name": "Pivota Commerce",
                    "return_url": metadata.get("return_url", "https://merchant.pivota.cc/payment/success") if metadata else "https://merchant.pivota.cc/payment/success",
                    "cancel_url": metadata.get("cancel_url", "https://merchant.pivota.cc/payment/cancel") if metadata else "https://merchant.pivota.cc/payment/cancel",
                    "user_action": "PAY_NOW"
                }
            }
            
            logger.info(f"🔍 PayPal: Creating order for {amount_in_dollars} {currency}")
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/v2/checkout/orders",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": metadata.get("order_id") if metadata else None
                    },
                    json=order_data
                )
                
                logger.info(f"   Response: {response.status_code}")
                
                if response.status_code not in [200, 201]:
                    logger.error(f"❌ PayPal order creation failed: {response.text}")
                    raise Exception(f"PayPal API error: {response.status_code}")
                
                order = response.json()
                
                # Find the approve link
                approve_url = None
                for link in order.get("links", []):
                    if link["rel"] == "approve":
                        approve_url = link["href"]
                        break
                
                logger.info(f"✅ PayPal order created: {order['id']}")
                
                payment_intent = PaymentIntent(
                    id=order["id"],
                    amount=amount,
                    currency=currency,
                    status="requires_action",  # User needs to approve
                    client_secret=approve_url,  # Return the approval URL
                    psp_type="paypal",
                    raw_response=order
                )
                
                return True, payment_intent, None
                
        except Exception as e:
            logger.error(f"PayPal payment intent creation failed: {str(e)}")
            return False, None, str(e)
    
    async def confirm_payment(self, payment_intent_id: str) -> Dict[str, Any]:
        """
        Capture a PayPal order after user approval
        
        Args:
            payment_intent_id: PayPal order ID
        
        Returns:
            Payment confirmation details
        """
        try:
            token = await self._get_access_token()
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/v2/checkout/orders/{payment_intent_id}/capture",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    }
                )
                
                if response.status_code != 201:
                    logger.error(f"PayPal capture failed: {response.text}")
                    raise Exception(f"PayPal capture error: {response.status_code}")
                
                capture_data = response.json()
                
                return {
                    "status": "succeeded",
                    "payment_intent_id": payment_intent_id,
                    "capture_id": capture_data["purchase_units"][0]["payments"]["captures"][0]["id"],
                    "amount": capture_data["purchase_units"][0]["amount"]["value"]
                }
                
        except Exception as e:
            logger.error(f"PayPal payment confirmation failed: {str(e)}")
            raise
    
    async def create_refund(
        self,
        payment_intent_id: str,
        amount: Optional[int] = None,
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Refund a PayPal payment
        
        Args:
            payment_intent_id: Original payment intent ID (order ID)
            amount: Amount to refund in cents (None for full refund)
            reason: Refund reason
        
        Returns:
            RefundResponse object
        """
        try:
            token = await self._get_access_token()
            
            # First, get the capture ID from the order
            async with httpx.AsyncClient() as client:
                # Get order details
                order_response = await client.get(
                    f"{self.base_url}/v2/checkout/orders/{payment_intent_id}",
                    headers={
                        "Authorization": f"Bearer {token}"
                    }
                )
                
                if order_response.status_code != 200:
                    raise Exception(f"Failed to get order details: {order_response.status_code}")
                
                order = order_response.json()
                
                # Find the capture ID
                capture_id = None
                for unit in order.get("purchase_units", []):
                    captures = unit.get("payments", {}).get("captures", [])
                    if captures:
                        capture_id = captures[0]["id"]
                        captured_amount = float(captures[0]["amount"]["value"])
                        break
                
                if not capture_id:
                    raise Exception("No capture found for this order")
                
                # Prepare refund data
                refund_data = {}
                if amount:
                    # Convert from cents to dollars
                    refund_amount = amount / 100
                    refund_data["amount"] = {
                        "value": f"{refund_amount:.2f}",
                        "currency_code": order["purchase_units"][0]["amount"]["currency_code"]
                    }
                
                if reason:
                    refund_data["note_to_payer"] = reason
                
                # Create refund
                refund_response = await client.post(
                    f"{self.base_url}/v2/payments/captures/{capture_id}/refund",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    },
                    json=refund_data if refund_data else None
                )
                
                if refund_response.status_code != 201:
                    logger.error(f"PayPal refund failed: {refund_response.text}")
                    raise Exception(f"PayPal refund error: {refund_response.status_code}")
                
                refund = refund_response.json()
                
                return (
                    True,
                    refund["id"],
                    None
                )
                
        except Exception as e:
            logger.error(f"PayPal refund failed: {str(e)}")
            return (False, None, str(e))
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[int] = None,
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Alias for create_refund to match PSPAdapter interface"""
        return await self.create_refund(payment_intent_id, amount, reason)
    
    async def get_payment_status(self, payment_intent_id: str) -> str:
        """
        Get the status of a PayPal order
        
        Args:
            payment_intent_id: PayPal order ID
        
        Returns:
            Payment status
        """
        try:
            token = await self._get_access_token()
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/v2/checkout/orders/{payment_intent_id}",
                    headers={
                        "Authorization": f"Bearer {token}"
                    }
                )
                
                if response.status_code != 200:
                    return "unknown"
                
                order = response.json()
                status = order.get("status", "UNKNOWN")
                
                # Map PayPal status to our status
                status_map = {
                    "CREATED": "requires_action",
                    "SAVED": "requires_action", 
                    "APPROVED": "requires_capture",
                    "COMPLETED": "succeeded",
                    "VOIDED": "canceled"
                }
                
                return status_map.get(status, "unknown")
                
        except Exception as e:
            logger.error(f"Failed to get PayPal payment status: {str(e)}")
            return "unknown"
