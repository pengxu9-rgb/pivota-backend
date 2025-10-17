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
        client_secret: str,
        amount: int,
        currency: str,
        status: str,
        psp_type: str,
        raw_response: Dict[str, Any]
    ):
        self.id = id
        self.client_secret = client_secret
        self.amount = amount
        self.currency = currency
        self.status = status
        self.psp_type = psp_type
        self.raw_response = raw_response


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
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """退款"""
        pass


class StripeAdapter(PSPAdapter):
    """Stripe PSP 适配器"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        stripe.api_key = api_key
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """创建 Stripe Payment Intent"""
        try:
            payment_intent = stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Stripe 使用分为单位
                currency=currency.lower(),
                metadata=metadata,
                automatic_payment_methods={
                    "enabled": True,
                    "allow_redirects": "never"  # Avoid requiring return_url for test payments
                }
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
                    raw_response=payment_intent
                ),
                None
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
        reason: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Stripe 退款"""
        try:
            refund_data = {"payment_intent": payment_intent_id}
            if amount:
                refund_data["amount"] = int(amount * 100)
            if reason:
                refund_data["reason"] = reason
            
            refund = stripe.Refund.create(**refund_data)
            return True, refund.id, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, None, str(e)


class AdyenAdapter(PSPAdapter):
    """Adyen PSP 适配器"""
    
    def __init__(self, api_key: str, merchant_account: str = "PivotaTestMerchant"):
        self.api_key = api_key
        self.merchant_account = merchant_account
        self.base_url = "https://checkout-test.adyen.com/v70"  # Test environment
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any]
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str]]:
        """创建 Adyen Payment"""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }
            
            payload = {
                "amount": {
                    "value": int(amount * 100),  # Adyen 也使用分为单位
                    "currency": currency
                },
                "reference": metadata.get("order_id", "ORDER"),
                "merchantAccount": self.merchant_account,
                "returnUrl": "https://your-company.com/checkout/complete",
                "metadata": metadata
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments",
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return (
                        True,
                        PaymentIntent(
                            id=data.get("pspReference", ""),
                            client_secret=data.get("sessionData", ""),  # Adyen 的客户端密钥
                            amount=int(amount * 100),
                            currency=currency,
                            status=data.get("resultCode", "pending").lower(),
                            psp_type="adyen",
                            raw_response=data
                        ),
                        None
                    )
                else:
                    return False, None, f"Adyen API error: {response.status_code} - {response.text}"
        except Exception as e:
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
        reason: Optional[str] = None
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
        psp_type: PSP 类型 ("stripe" 或 "adyen")
        api_key: API 密钥
        **kwargs: 其他 PSP 特定参数
    
    Returns:
        PSP 适配器实例
    
    Raises:
        ValueError: 不支持的 PSP 类型
    """
    psp_type = psp_type.lower()
    
    if psp_type == "stripe":
        return StripeAdapter(api_key)
    elif psp_type == "adyen":
        merchant_account = kwargs.get("merchant_account", "PivotaTestMerchant")
        return AdyenAdapter(api_key, merchant_account)
    else:
        raise ValueError(f"Unsupported PSP type: {psp_type}")

