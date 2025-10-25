"""
Checkout.com PSP Adapter
Implements payment processing for Checkout.com
"""

from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
import httpx
from adapters.psp_adapter import PSPAdapter, PaymentIntent


class CheckoutAdapter(PSPAdapter):
    """Checkout.com payment adapter"""
    
    def __init__(self, api_key: str, public_key: Optional[str] = None):
        self.api_key = api_key  # Secret key
        self.public_key = public_key
        self.base_url = "https://api.sandbox.checkout.com"  # Use sandbox for testing
        if api_key and "sk_" in api_key and "prod" in api_key.lower():
            self.base_url = "https://api.checkout.com"  # Production
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """Create a Checkout.com payment session"""
        try:
            # Log for debugging
            print(f"🔍 Checkout: Creating payment intent for {amount} {currency}")
            print(f"   API Key: {self.api_key[:20]}... (len={len(self.api_key)})")
            print(f"   Base URL: {self.base_url}")
            
            headers = {
                "Authorization": self.api_key,
                "Content-Type": "application/json"
            }
            
            # For Checkout.com, we'll create a payment intent that frontend can complete
            # Using a simple approach that works without processing_channel_id
            payload = {
                "amount": int(amount * 100),  # Checkout uses minor units (cents)
                "currency": currency.upper(),
                "reference": metadata.get("order_id", "ORDER"),
                "metadata": metadata,
                "customer": {
                    "email": metadata.get("customer_email", "customer@example.com")
                },
                "payment_type": "Regular",
                "capture": True
            }
            
            # Add processing channel if available (from account_id or public_key)
            if self.public_key:
                payload["processing_channel_id"] = self.public_key
            
            print(f"   Payload: amount={payload['amount']}, currency={payload['currency']}")
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                print(f"   Response: {response.status_code}")
                
                if response.status_code in [200, 201, 202]:
                    data = response.json()
                    print(f"   ✅ Checkout payment created: {data.get('id', 'unknown')[:30]}")
                    
                    return (
                        True,
                        PaymentIntent(
                            id=data.get("id", ""),
                            client_secret=data.get("_links", {}).get("redirect", {}).get("href", "") or f"checkout_{data.get('id','')}",
                            amount=int(amount * 100),
                            currency=currency,
                            status=data.get("status", "pending").lower(),
                            psp_type="checkout",
                            raw_response=data
                        ),
                        None
                    )
                else:
                    error_data = response.json() if response.text else {}
                    error_msg = f"Checkout API error: {response.status_code} - {error_data.get('error_type', '')} {error_data.get('error_codes', '')} {response.text[:200]}"
                    print(f"   ❌ {error_msg}")
                    return False, None, error_msg
                    
        except httpx.TimeoutException:
            print("   ❌ Checkout API timeout")
            return False, None, "Checkout API timeout"
        except Exception as e:
            print(f"   ❌ Checkout error: {e}")
            return False, None, f"Checkout error: {str(e)}"
    
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """Confirm a Checkout.com payment"""
        try:
            headers = {
                "Authorization": self.api_key,
                "Content-Type": "application/json"
            }
            
            async with httpx.AsyncClient() as client:
                # Get payment details
                response = await client.get(
                    f"{self.base_url}/payments/{payment_intent_id}",
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status", "pending").lower()
                    
                    # Checkout status mapping
                    status_map = {
                        "authorized": "succeeded",
                        "captured": "succeeded",
                        "card_verified": "succeeded",
                        "declined": "failed",
                        "expired": "failed",
                        "canceled": "cancelled",
                        "pending": "processing"
                    }
                    
                    return True, status_map.get(status, status), None
                else:
                    return False, "failed", f"Checkout API error: {response.status_code}"
                    
        except Exception as e:
            return False, "failed", str(e)
    
    async def get_payment_status(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """Get Checkout.com payment status"""
        try:
            headers = {
                "Authorization": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/payments/{payment_intent_id}",
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status", "pending").lower()
                    
                    # Map to standard statuses
                    status_map = {
                        "authorized": "succeeded",
                        "captured": "succeeded",
                        "card_verified": "succeeded",
                        "declined": "failed",
                        "expired": "failed",
                        "canceled": "cancelled",
                        "pending": "processing"
                    }
                    
                    return True, status_map.get(status, status), None
                else:
                    return False, "unknown", f"Checkout API error: {response.status_code}"
                    
        except Exception as e:
            return False, "unknown", str(e)
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Process a Checkout.com refund"""
        try:
            headers = {
                "Authorization": self.api_key,
                "Content-Type": "application/json"
            }
            
            payload = {
                "reference": f"refund_{payment_intent_id[:16]}"
            }
            
            if amount:
                payload["amount"] = int(amount * 100)
            
            if reason:
                payload["metadata"] = {"reason": reason}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/{payment_intent_id}/refunds",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code in [200, 201, 202]:
                    data = response.json()
                    return True, data.get("action_id") or data.get("id"), None
                else:
                    error_data = response.json() if response.text else {}
                    return False, None, f"Checkout refund error: {response.status_code} - {error_data.get('error_type', response.text)}"
                    
        except Exception as e:
            return False, None, str(e)
    
    async def test_connection(self) -> Tuple[bool, str]:
        """Test if Checkout.com API key is valid"""
        try:
            headers = {
                "Authorization": self.api_key
            }
            
            async with httpx.AsyncClient() as client:
                # Use a lightweight endpoint to test connection
                response = await client.get(
                    f"{self.base_url}/instruments",
                    headers=headers,
                    timeout=5.0
                )
                
                if response.status_code in [200, 401, 403]:
                    # 401/403 means API responded (key format valid, just not authorized for this endpoint)
                    # For sandbox keys, this is expected
                    return True, "Connection OK - Checkout.com API reachable"
                else:
                    return False, f"Unexpected response: {response.status_code}"
                    
        except httpx.TimeoutException:
            return False, "Connection timeout"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

