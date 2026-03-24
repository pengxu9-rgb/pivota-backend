"""
PSP (Payment Service Provider) 适配器
支持多个支付提供商的统一接口
"""

from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from abc import ABC, abstractmethod
import stripe
import httpx
from config.settings import settings


class PaymentIntent:
    """统一的支付意图对象"""
    def __init__(
        self,
        id: str,
        client_secret: Optional[str],
        amount: int,
        currency: str,
        status: str,
        psp_type: str,
        raw_response: Dict[str, Any],
        redirect_url: Optional[str] = None,
    ):
        self.id = id
        self.client_secret = client_secret
        self.amount = amount
        self.currency = currency
        self.status = status
        self.psp_type = psp_type
        self.raw_response = raw_response
        # 对于基于重定向的支付方式（如 Stripe Checkout、PayPal 等），返回可直接跳转的支付 URL
        # 非重定向方式则为 None
        self.redirect_url = redirect_url


class PSPAdapter(ABC):
    """PSP 适配器基类"""
    
    @abstractmethod
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """创建支付意图"""
        pass
    
    @abstractmethod
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """确认支付"""
        pass
    
    @abstractmethod
    async def get_payment_status(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """查询支付状态"""
        pass
    
    @abstractmethod
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """退款"""
        pass


class StripeAdapter(PSPAdapter):
    """Stripe PSP 适配器"""
    
    def __init__(
        self,
        api_key: str,
        account_id: Optional[str] = None,
        mode: str = "payment_intent",
        environment: Optional[str] = None,
    ):
        self.api_key = api_key
        self.account_id = account_id
        self.mode = mode if mode in {"payment_intent", "checkout_session"} else "payment_intent"
        self.environment = (environment or "").strip().lower() or None
        stripe.api_key = api_key
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """
        创建 Stripe 支付意图。

        默认使用 PaymentIntent + client_secret 流程（兼容现有前端）。
        当 metadata.psp_mode == "stripe_checkout" 时，改走 Stripe Checkout Session，
        返回 redirect_url，方便 Agent / 外部前端直接跳转支付页。
        """
        try:
            psp_mode = (metadata.get("psp_mode") or "").lower()
            stripe_mode = "checkout_session" if psp_mode == "stripe_checkout" else self.mode
            request_kwargs: Dict[str, Any] = {}
            if self.account_id:
                request_kwargs["stripe_account"] = self.account_id

            # Agent / Checkout 场景：返回可跳转的支付链接
            if stripe_mode == "checkout_session":
                session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=[
                        {
                            "quantity": 1,
                            "price_data": {
                                "currency": currency.lower(),
                                "unit_amount": int(amount * 100),
                                "product_data": {
                                    # 尽量给一个可读名称，避免为空
                                    "name": metadata.get("description")
                                    or metadata.get("order_id")
                                    or "Order",
                                },
                            },
                        }
                    ],
                    success_url="https://merchant.pivota.cc/payment/success?session_id={CHECKOUT_SESSION_ID}",
                    cancel_url="https://merchant.pivota.cc/payment/cancel",
                    metadata=metadata,
                    payment_intent_data={"metadata": metadata},
                    **request_kwargs,
                )

                return (
                    True,
                    PaymentIntent(
                        id=session.id,
                        client_secret=None,
                        amount=int(amount * 100),
                        currency=currency,
                        status="requires_action",
                        psp_type="stripe_checkout",
                        raw_response=session,
                        redirect_url=session.url,
                    ),
                    None,
                )

            # 默认：PaymentIntent + client_secret（传统前端使用）
            payment_intent = stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Stripe 使用分为单位
                currency=currency.lower(),
                metadata=metadata,
                automatic_payment_methods={
                    "enabled": True,
                    "allow_redirects": "never"  # 避免测试环境强依赖 return_url
                },
                **request_kwargs,
            )

            return (
                True,
                PaymentIntent(
                    id=payment_intent.id,
                    client_secret=payment_intent.client_secret,
                    amount=payment_intent.amount,
                    currency=payment_intent.currency,
                    status=payment_intent.status,
                    psp_type="stripe",
                    raw_response=payment_intent,
                    redirect_url=None,
                ),
                None,
            )
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, None, str(e)
    
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """确认 Stripe 支付"""
        try:
            payment_intent = stripe.PaymentIntent.confirm(
                payment_intent_id,
                payment_method=payment_method_id
            )
            return True, payment_intent.status, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, "failed", str(e)
    
    async def get_payment_status(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """查询 Stripe 支付状态"""
        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            return True, payment_intent.status, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, "unknown", str(e)
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Stripe 退款"""
        try:
            refund_data = {"payment_intent": payment_intent_id}
            if amount:
                refund_data["amount"] = int(amount * 100)
            if reason:
                # Stripe only accepts a small enum for `reason`.
                # Keep caller-provided human text as metadata instead of failing the refund.
                allowed_reasons = {"duplicate", "fraudulent", "requested_by_customer"}
                reason_norm = str(reason).strip()
                if reason_norm in allowed_reasons:
                    refund_data["reason"] = reason_norm
                else:
                    refund_data["metadata"] = {"reason": reason_norm}

            if idempotency_key:
                refund = stripe.Refund.create(**refund_data, idempotency_key=str(idempotency_key))
            else:
                refund = stripe.Refund.create(**refund_data)
            return True, refund.id, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, None, str(e)


class AdyenAdapter(PSPAdapter):
    """Adyen PSP 适配器"""
    
    def __init__(
        self,
        api_key: str,
        merchant_account: str = "PivotaTestMerchant",
        environment: Optional[str] = None,
        client_key: Optional[str] = None,
    ):
        # Clean API key to avoid whitespace / newline pollution from DB or env
        key_clean = (api_key or "").replace("\n", "").replace("\r", "").replace(" ", "").strip()
        # If the key length looks wildly off, try to fall back to settings (env)
        if len(key_clean) < 50 or len(key_clean) > 120:
            fallback = getattr(settings, "adyen_api_key", key_clean)
            key_clean = (fallback or "").replace("\n", "").replace("\r", "").replace(" ", "").strip()
        self.api_key = key_clean

        # Some records might accidentally store Stripe account ids (acct_...). Guard against that.
        acct = (merchant_account or "").strip()
        if acct.startswith("acct_"):
            acct = getattr(settings, "adyen_merchant_account", "PivotaTestMerchant")
        self.merchant_account = acct or "PivotaTestMerchant"
        self.environment = (environment or "").strip().lower() or "test"
        self.client_key = (client_key or "").strip() or None
        self.base_url = (
            "https://checkout-live.adyen.com/v70"
            if self.environment == "live"
            else "https://checkout-test.adyen.com/v70"
        )

    @staticmethod
    def _should_force_card_only_for_canary(metadata: Dict[str, Any]) -> bool:
        source = str(metadata.get("source") or "").strip().lower()
        return bool(metadata.get("ops_canary")) and source in {
            "ops_order_backed_canary",
            "merchant_order_backed_canary",
        }
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """创建 Adyen Payment"""
        try:
            # Debug info to verify which credentials are actually used in production
            print(
                f"🔍 Adyen: Creating payment for {amount} {currency} | "
                f"merchant={self.merchant_account} | key_prefix={self.api_key[:12]} | len={len(self.api_key)}"
            )
            
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }
            
            # Use sessions API for redirect flow (like Stripe)
            payload = {
                "amount": {
                    "value": int(amount * 100),
                    "currency": currency
                },
                "reference": metadata.get("order_id", "ORDER"),
                "merchantAccount": self.merchant_account,
                "returnUrl": "https://merchant.pivota.cc/payment/success",
                "countryCode": "US",
                "shopperLocale": "en_US",
                "channel": "Web"
            }

            if self._should_force_card_only_for_canary(metadata):
                # Adyen test accounts can default to wallet-only methods for sessions.
                # Keep ops canaries deterministic by constraining the session to cards.
                payload["allowedPaymentMethods"] = ["scheme"]
            
            print(f"   Payload merchantAccount: {payload['merchantAccount']}")
            
            async with httpx.AsyncClient() as client:
                # Use /sessions endpoint for redirect flow
                response = await client.post(
                    f"{self.base_url}/sessions",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                print(f"   Response: {response.status_code}")
                
                if response.status_code in [200, 201]:
                    data = response.json()
                    session_id = data.get("id", "")
                    session_data = data.get("sessionData", "")
                    
                    print(f"   ✅ Adyen session created: {session_id}")
                    print(f"   🔗 Session data available: {'YES' if session_data else 'NO'}")
                    
                    return (
                        True,
                        PaymentIntent(
                            id=f"adyen_session_{session_id}",
                            client_secret=session_data,  # Adyen session data for frontend
                            amount=int(amount * 100),
                            currency=currency,
                            status="requires_action",
                            psp_type="adyen",
                            raw_response={
                                **data,
                                "clientKey": self.client_key,
                                "environment": self.environment,
                            }
                        ),
                        None
                    )
                else:
                    print(f"   ❌ Adyen API error: {response.status_code}")
                    return False, None, f"Adyen API error: {response.status_code} - {response.text}"
        except Exception as e:
            print(f"   ❌ Adyen exception: {e}")
            return False, None, str(e)
    
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """确认 Adyen 支付"""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }
            
            payload = {
                "merchantAccount": self.merchant_account,
                "paymentMethod": payment_method_id,
                "pspReference": payment_intent_id
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/details",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    status = data.get("resultCode", "pending").lower()
                    
                    # Adyen 状态映射
                    status_map = {
                        "authorised": "succeeded",
                        "refused": "failed",
                        "error": "failed",
                        "cancelled": "cancelled"
                    }
                    
                    return True, status_map.get(status, status), None
                else:
                    return False, "failed", f"Adyen API error: {response.status_code}"
        except Exception as e:
            return False, "failed", str(e)
    
    async def get_payment_status(
        self,
        payment_intent_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """查询 Adyen 支付状态"""
        # Adyen 需要通过 webhook 或 polling 查询状态
        # 简化实现：返回 pending
        return True, "pending", None
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Adyen 退款"""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }
            
            payload = {
                "merchantAccount": self.merchant_account,
                "originalReference": payment_intent_id
            }
            
            if amount:
                payload["modificationAmount"] = {
                    "value": int(amount * 100),
                    "currency": "USD"  # TODO: 从原始支付中获取
                }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/refunds",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return True, data.get("pspReference"), None
                else:
                    return False, None, f"Adyen refund error: {response.status_code}"
        except Exception as e:
            return False, None, str(e)


# ============================================================================
# PSP 工厂函数
# ============================================================================

def get_psp_adapter(psp_type: str, api_key: str, **kwargs) -> PSPAdapter:
    """
    获取 PSP 适配器
    
    Args:
        psp_type: PSP 类型 ("stripe", "adyen", "checkout", "paypal")
        api_key: API 密钥
        **kwargs: 其他 PSP 特定参数
    
    Returns:
        PSP 适配器实例
    
    Raises:
        ValueError: 不支持的 PSP 类型
    """
    psp_type = psp_type.lower()
    
    if psp_type == "stripe":
        return StripeAdapter(
            api_key,
            account_id=kwargs.get("account_id"),
            mode=kwargs.get("mode", "payment_intent"),
            environment=kwargs.get("environment"),
        )
    elif psp_type == "adyen":
        merchant_account = kwargs.get("merchant_account", "PivotaTestMerchant")
        return AdyenAdapter(
            api_key,
            merchant_account,
            environment=kwargs.get("environment"),
            client_key=kwargs.get("client_key"),
        )
    elif psp_type == "checkout":
        from adapters.checkout_adapter import CheckoutAdapter
        return CheckoutAdapter(
            api_key,
            public_key=kwargs.get("public_key"),
            processing_channel_id=kwargs.get("processing_channel_id"),
            environment=kwargs.get("environment"),
        )
    elif psp_type == "paypal":
        from adapters.paypal_adapter import PayPalAdapter
        client_secret = kwargs.get("client_secret", api_key)  # PayPal uses client_id as api_key
        is_sandbox = kwargs.get("is_sandbox", True)
        return PayPalAdapter(api_key, client_secret, is_sandbox)
    else:
        raise ValueError(f"Unsupported PSP type: {psp_type}")
