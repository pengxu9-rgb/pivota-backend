"""
PayPal Payment Adapter
Handles payment processing through PayPal REST API
"""

import httpx
import json
import base64
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from decimal import Decimal
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
        if self.access_token and self.token_expiry:
            try:
                expiry_ts = (
                    self.token_expiry.timestamp()
                    if hasattr(self.token_expiry, "timestamp")
                    else float(self.token_expiry)
                )
                if datetime.now().timestamp() < expiry_ts:
                    return self.access_token
            except Exception:
                self.access_token = None
                self.token_expiry = None
        
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
    
    @staticmethod
    def _format_money(value: Any) -> str:
        return f"{Decimal(str(value or '0')).quantize(Decimal('0.01')):.2f}"

    @staticmethod
    def _paypal_request_id(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        request_id = str(value).strip()
        return request_id[:108] if request_id else None

    @staticmethod
    def _resolve_order_intent(metadata: Optional[Dict[str, Any]]) -> str:
        metadata = metadata or {}
        explicit_intent = str(metadata.get("paypal_intent") or "").strip().upper()
        if explicit_intent in {"CAPTURE", "AUTHORIZE"}:
            return explicit_intent
        capture_method = str(
            metadata.get("capture_method")
            or metadata.get("payment_capture_method")
            or ""
        ).strip().lower()
        payment_flow = str(metadata.get("payment_flow") or "").strip().lower()
        if capture_method == "manual" or payment_flow == "authorization_first":
            return "AUTHORIZE"
        return "CAPTURE"

    @staticmethod
    def _payment_entries(order: Dict[str, Any], payment_type: str) -> list[Dict[str, Any]]:
        entries: list[Dict[str, Any]] = []
        for unit in order.get("purchase_units", []) or []:
            payments = unit.get("payments", {}) if isinstance(unit, dict) else {}
            values = payments.get(payment_type, []) if isinstance(payments, dict) else []
            entries.extend(entry for entry in values if isinstance(entry, dict))
        return entries

    @classmethod
    def _first_authorization(cls, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        authorizations = cls._payment_entries(order, "authorizations")
        active_statuses = {"CREATED", "AUTHORIZED", "PENDING"}
        for authorization in authorizations:
            if str(authorization.get("status") or "").upper() in active_statuses:
                return authorization
        return authorizations[0] if authorizations else None

    @classmethod
    def _first_capture(cls, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        captures = cls._payment_entries(order, "captures")
        for capture in captures:
            if str(capture.get("status") or "").upper() == "COMPLETED":
                return capture
        return captures[0] if captures else None

    async def _get_order_details(self, client: httpx.AsyncClient, token: str, order_id: str) -> Dict[str, Any]:
        response = await client.get(
            f"{self.base_url}/v2/checkout/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get order details: {response.status_code}")
        return response.json()

    @classmethod
    def _status_details_from_order(cls, order: Dict[str, Any]) -> Dict[str, Any]:
        capture = cls._first_capture(order)
        if capture:
            capture_status = str(capture.get("status") or "").upper()
            amount = capture.get("amount") if isinstance(capture.get("amount"), dict) else {}
            return {
                "status": "succeeded" if capture_status == "COMPLETED" else "pending",
                "amount": amount.get("value"),
                "currency": amount.get("currency_code"),
                "raw_status": capture_status,
                "capture_id": capture.get("id"),
            }

        authorization = cls._first_authorization(order)
        if authorization:
            authorization_status = str(authorization.get("status") or "").upper()
            amount = authorization.get("amount") if isinstance(authorization.get("amount"), dict) else {}
            status = "requires_capture" if authorization_status in {"CREATED", "AUTHORIZED", "PENDING"} else "canceled"
            return {
                "status": status,
                "amount": amount.get("value"),
                "currency": amount.get("currency_code"),
                "raw_status": authorization_status,
                "authorization_id": authorization.get("id"),
            }

        order_status = str(order.get("status") or "UNKNOWN").upper()
        order_intent = str(order.get("intent") or "").upper()
        order_amount = {}
        try:
            order_amount = (order.get("purchase_units") or [{}])[0].get("amount") or {}
        except Exception:
            order_amount = {}
        status_map = {
            "CREATED": "requires_action",
            "SAVED": "requires_action",
            "APPROVED": "requires_action" if order_intent == "AUTHORIZE" else "requires_capture",
            "COMPLETED": "succeeded",
            "VOIDED": "canceled",
        }
        return {
            "status": status_map.get(order_status, "unknown"),
            "amount": order_amount.get("value"),
            "currency": order_amount.get("currency_code"),
            "raw_status": order_status,
            "intent": order_intent,
        }

    async def create_payment_intent(
        self,
        amount: Decimal,
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

            amount_value = self._format_money(amount)
            order_intent = self._resolve_order_intent(metadata)
            
            # Create order payload
            order_data = {
                "intent": order_intent,
                "purchase_units": [{
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": amount_value
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
            
            logger.info(f"🔍 PayPal: Creating order for {amount_value} {currency}")
            
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                request_id = self._paypal_request_id((metadata or {}).get("order_id"))
                if request_id:
                    headers["PayPal-Request-Id"] = request_id
                response = await client.post(
                    f"{self.base_url}/v2/checkout/orders",
                    headers=headers,
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
                    amount=int(Decimal(amount_value) * 100),
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
    
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Capture or authorize a PayPal order after user approval
        
        Args:
            payment_intent_id: PayPal order ID
        
        Returns:
            Payment confirmation details
        """
        try:
            token = await self._get_access_token()
            
            async with httpx.AsyncClient() as client:
                order = await self._get_order_details(client, token, payment_intent_id)
                details = self._status_details_from_order(order)
                if details.get("status") == "requires_capture" and details.get("authorization_id"):
                    return True, "requires_capture", None
                if details.get("status") == "succeeded":
                    return True, "succeeded", None

                order_intent = str(order.get("intent") or "").strip().upper()
                endpoint = "authorize" if order_intent == "AUTHORIZE" else "capture"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                request_id = self._paypal_request_id(f"paypal_{endpoint}:{payment_intent_id}")
                if request_id:
                    headers["PayPal-Request-Id"] = request_id
                response = await client.post(
                    f"{self.base_url}/v2/checkout/orders/{payment_intent_id}/{endpoint}",
                    headers=headers,
                    json={},
                )
                
                if response.status_code not in [200, 201]:
                    logger.error(f"PayPal {endpoint} failed: {response.text}")
                    raise Exception(f"PayPal {endpoint} error: {response.status_code}")
                
                return True, "requires_capture" if endpoint == "authorize" else "succeeded", None
                
        except Exception as e:
            logger.error(f"PayPal payment confirmation failed: {str(e)}")
            return False, "failed", str(e)
    
    async def create_refund(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        currency: Optional[str] = None,
        full_refund: Optional[bool] = None,
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
                order = await self._get_order_details(client, token, payment_intent_id)
                
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
                    refund_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
                    refund_data["amount"] = {
                        "value": f"{refund_amount:.2f}",
                        "currency_code": str(
                            currency
                            or order["purchase_units"][0]["amount"].get("currency_code")
                            or "USD"
                        ).upper()
                    }
                
                if reason:
                    refund_data["note_to_payer"] = reason
                
                # Create refund
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
                request_id = self._paypal_request_id(idempotency_key)
                if request_id:
                    headers["PayPal-Request-Id"] = request_id
                refund_response = await client.post(
                    f"{self.base_url}/v2/payments/captures/{capture_id}/refund",
                    headers=headers,
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
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        currency: Optional[str] = None,
        full_refund: Optional[bool] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Alias for create_refund to match PSPAdapter interface"""
        return await self.create_refund(
            payment_intent_id,
            amount,
            reason,
            idempotency_key=idempotency_key,
            currency=currency,
            full_refund=full_refund,
        )
    
    async def get_payment_status(self, payment_intent_id: str) -> Tuple[bool, str, Optional[str]]:
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
                order = await self._get_order_details(client, token, payment_intent_id)
                details = self._status_details_from_order(order)
                return True, str(details.get("status") or "unknown"), None

        except Exception as e:
            logger.error(f"Failed to get PayPal payment status: {str(e)}")
            return False, "unknown", str(e)

    async def get_payment_status_details(self, payment_intent_id: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """Return normalized PayPal payment status with amount/currency for fail-closed verification."""
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient() as client:
                order = await self._get_order_details(client, token, payment_intent_id)
                details = self._status_details_from_order(order)
                details["raw_response"] = order
                return True, details, None
        except Exception as e:
            logger.error(f"Failed to get PayPal payment status details: {str(e)}")
            return False, {"status": "unknown"}, str(e)

    async def capture_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        currency: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Capture a previously authorized PayPal order authorization."""
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient() as client:
                order = await self._get_order_details(client, token, payment_intent_id)
                authorization = self._first_authorization(order)
                if not authorization or not authorization.get("id"):
                    return False, None, "No PayPal authorization found for this order"

                payload: Dict[str, Any] = {"final_capture": True}
                if amount is not None:
                    auth_amount = authorization.get("amount") if isinstance(authorization.get("amount"), dict) else {}
                    payload["amount"] = {
                        "value": self._format_money(amount),
                        "currency_code": str(currency or auth_amount.get("currency_code") or "USD").upper(),
                    }

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                request_id = self._paypal_request_id(idempotency_key)
                if request_id:
                    headers["PayPal-Request-Id"] = request_id

                response = await client.post(
                    f"{self.base_url}/v2/payments/authorizations/{authorization['id']}/capture",
                    headers=headers,
                    json=payload,
                )
                if response.status_code not in [200, 201]:
                    logger.error(f"PayPal authorization capture failed: {response.text}")
                    return False, None, f"PayPal capture error: {response.status_code}"
                capture = response.json()
                return True, capture.get("id") or payment_intent_id, None
        except Exception as e:
            logger.error(f"PayPal authorization capture failed: {str(e)}")
            return False, None, str(e)

    async def cancel_payment_authorization(
        self,
        payment_intent_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Void a previously authorized PayPal order authorization."""
        try:
            token = await self._get_access_token()
            async with httpx.AsyncClient() as client:
                order = await self._get_order_details(client, token, payment_intent_id)
                authorization = self._first_authorization(order)
                if not authorization or not authorization.get("id"):
                    return False, None, "No PayPal authorization found for this order"

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                request_id = self._paypal_request_id(idempotency_key)
                if request_id:
                    headers["PayPal-Request-Id"] = request_id

                response = await client.post(
                    f"{self.base_url}/v2/payments/authorizations/{authorization['id']}/void",
                    headers=headers,
                    json={},
                )
                if response.status_code not in [200, 204]:
                    logger.error(f"PayPal authorization void failed: {response.text}")
                    return False, None, f"PayPal void error: {response.status_code}"
                return True, authorization.get("id"), None
        except Exception as e:
            logger.error(f"PayPal authorization void failed: {str(e)}")
            return False, None, str(e)
