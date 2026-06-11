"""
PSP (Payment Service Provider) 适配器
支持多个支付提供商的统一接口
"""

import asyncio
import hashlib
import os
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from abc import ABC, abstractmethod
import stripe
import httpx
from config.settings import settings


_STRIPE_REQUEST_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("STRIPE_REQUEST_TIMEOUT_SECONDS", "20") or "20"),
)
_STRIPE_CONNECT_TIMEOUT_SECONDS = max(
    1.0,
    min(
        float(os.getenv("STRIPE_CONNECT_TIMEOUT_SECONDS", "5") or "5"),
        _STRIPE_REQUEST_TIMEOUT_SECONDS,
    ),
)
_STRIPE_MAX_NETWORK_RETRIES = max(
    0,
    int(os.getenv("STRIPE_MAX_NETWORK_RETRIES", "1") or "1"),
)


# Smallest-currency-unit conversion. Most currencies are 2-decimal (×100), but
# zero-decimal currencies (JPY, KRW, …) are ×1 and three-decimal currencies
# (BHD, KWD, …) are ×1000. Hardcoding ×100 overcharges JPY/KRW by 100× and
# undercharges BHD/KWD by 10× — see the matching table in webhook_routes.
_ZERO_DECIMAL_CURRENCIES = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}
_THREE_DECIMAL_CURRENCIES = {"bhd", "jod", "kwd", "omr", "tnd"}


def _minor_unit_factor(currency: Optional[str]) -> int:
    c = (currency or "").strip().lower()
    if c in _ZERO_DECIMAL_CURRENCIES:
        return 1
    if c in _THREE_DECIMAL_CURRENCIES:
        return 1000
    return 100


def _to_minor_units(amount: Any, currency: Optional[str]) -> int:
    """Convert a major-unit amount to the PSP's smallest currency unit."""
    factor = _minor_unit_factor(currency)
    return int((Decimal(str(amount)) * Decimal(factor)).to_integral_value())


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
        public_key: Optional[str] = None,
    ):
        self.id = id
        self.client_secret = client_secret
        self.amount = amount
        self.currency = currency
        self.status = status
        self.psp_type = psp_type
        self.raw_response = raw_response
        self.public_key = public_key
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
        currency: Optional[str] = None,
        full_refund: Optional[bool] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """退款"""
        pass

    async def capture_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        currency: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Capture a previously authorized payment when the PSP supports it."""
        return False, None, f"{self.__class__.__name__} does not support capture_payment"

    async def cancel_payment_authorization(
        self,
        payment_intent_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Void/cancel a previously authorized payment when the PSP supports it."""
        return False, None, f"{self.__class__.__name__} does not support cancel_payment_authorization"


class StripeAdapter(PSPAdapter):
    """Stripe PSP 适配器"""
    
    def __init__(
        self,
        api_key: str,
        account_id: Optional[str] = None,
        mode: str = "payment_intent",
        environment: Optional[str] = None,
        public_key: Optional[str] = None,
    ):
        self.api_key = api_key
        self.account_id = account_id
        self.mode = mode if mode in {"payment_intent", "checkout_session"} else "payment_intent"
        self.environment = (environment or "").strip().lower() or None
        self.public_key = str(public_key or "").strip() or None
        self._client = stripe.StripeClient(
            api_key,
            stripe_account=account_id,
            max_network_retries=_STRIPE_MAX_NETWORK_RETRIES,
            http_client=stripe.RequestsClient(
                timeout=(
                    _STRIPE_CONNECT_TIMEOUT_SECONDS,
                    _STRIPE_REQUEST_TIMEOUT_SECONDS,
                )
            ),
        )

    def _resolve_redirect_policy(self, metadata: Dict[str, Any]) -> str:
        explicit = str(
            metadata.get("stripe_allow_redirects")
            or metadata.get("allow_redirects")
            or ""
        ).strip().lower()
        if explicit in {"always", "never"}:
            return explicit

        return_url = str(metadata.get("return_url") or "").strip()
        if return_url:
            return "always"

        return "never"

    def _build_checkout_session_success_url(self, metadata: Dict[str, Any]) -> str:
        success_url = str(
            metadata.get("success_url")
            or metadata.get("return_url")
            or ""
        ).strip()
        if not success_url:
            return "https://merchant.pivota.cc/payment/success?session_id={CHECKOUT_SESSION_ID}"
        if "{CHECKOUT_SESSION_ID}" in success_url:
            return success_url
        separator = "&" if "?" in success_url else "?"
        if success_url.endswith("?") or success_url.endswith("&"):
            separator = ""
        return f"{success_url}{separator}session_id={{CHECKOUT_SESSION_ID}}"

    def _build_checkout_session_cancel_url(self, metadata: Dict[str, Any]) -> str:
        cancel_url = str(
            metadata.get("cancel_url")
            or metadata.get("checkout_cancel_url")
            or metadata.get("payment_cancel_url")
            or ""
        ).strip()
        return cancel_url or "https://merchant.pivota.cc/payment/cancel"

    def _resolve_capture_method(self, metadata: Dict[str, Any]) -> Optional[str]:
        capture_method = str(
            metadata.get("stripe_capture_method")
            or metadata.get("capture_method")
            or metadata.get("payment_capture_method")
            or ""
        ).strip().lower()
        if capture_method in {"manual", "automatic"}:
            return capture_method
        return None

    def _resolve_create_request_options(
        self,
        metadata: Dict[str, Any],
        *,
        stripe_mode: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        order_id = str(metadata.get("order_id") or "").strip()
        request_key = str(metadata.get("idempotency_key") or "").strip()
        mode = str(stripe_mode or self.mode or "payment_intent").strip().lower()
        if mode not in {"payment_intent", "checkout_session"}:
            mode = "payment_intent"
        raw_key = f"agent_payment:{mode}:{order_id}" if order_id else request_key
        if not raw_key:
            return None
        if len(raw_key) <= 255:
            return {"idempotency_key": raw_key}

        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        suffix = f":sha256:{digest}"
        return {"idempotency_key": f"{raw_key[:255 - len(suffix)]}{suffix}"}

    async def _resolve_payment_intent_ref(self, payment_reference: str) -> Tuple[Optional[str], Optional[str]]:
        payment_reference = str(payment_reference or "").strip()
        if not payment_reference:
            return None, "payment_intent_id is required"
        if not payment_reference.startswith("cs_"):
            return payment_reference, None

        session = await asyncio.to_thread(
            self._client.v1.checkout.sessions.retrieve,
            payment_reference,
            {"expand": ["payment_intent"]},
        )
        session_payment_intent = getattr(session, "payment_intent", None)
        if isinstance(session_payment_intent, dict):
            payment_intent_ref = str(session_payment_intent.get("id") or "").strip()
        elif hasattr(session_payment_intent, "id"):
            payment_intent_ref = str(session_payment_intent.id or "").strip()
        else:
            payment_intent_ref = str(session_payment_intent or "").strip()
        if not payment_intent_ref:
            return None, f"Checkout Session {payment_reference} has no payment_intent"
        return payment_intent_ref, None
    
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
            request_options = self._resolve_create_request_options(
                metadata,
                stripe_mode=stripe_mode,
            )

            # Agent / Checkout 场景：返回可跳转的支付链接
            if stripe_mode == "checkout_session":
                payment_intent_data: Dict[str, Any] = {"metadata": metadata}
                capture_method = self._resolve_capture_method(metadata)
                if capture_method:
                    payment_intent_data["capture_method"] = capture_method

                session = await asyncio.to_thread(
                    self._client.v1.checkout.sessions.create,
                    {
                        "mode": "payment",
                        "line_items": [
                            {
                                "quantity": 1,
                                "price_data": {
                                    "currency": currency.lower(),
                                    "unit_amount": _to_minor_units(amount, currency),
                                    "product_data": {
                                        # 尽量给一个可读名称，避免为空
                                        "name": metadata.get("description")
                                        or metadata.get("order_id")
                                        or "Order",
                                    },
                                },
                            },
                        ],
                        "success_url": self._build_checkout_session_success_url(metadata),
                        "cancel_url": self._build_checkout_session_cancel_url(metadata),
                        "metadata": metadata,
                        "payment_intent_data": payment_intent_data,
                    },
                    request_options,
                )

                return (
                    True,
                    PaymentIntent(
                        id=session.id,
                        client_secret=None,
                        amount=_to_minor_units(amount, currency),
                        currency=currency,
                        status="requires_action",
                        psp_type="stripe_checkout",
                    raw_response=session,
                    redirect_url=session.url,
                    public_key=self.public_key,
                ),
                None,
            )

            # 默认：PaymentIntent + client_secret（传统前端使用）
            redirect_policy = self._resolve_redirect_policy(metadata)
            payment_payload = {
                "amount": _to_minor_units(amount, currency),  # Stripe 使用分为单位
                "currency": currency.lower(),
                "metadata": metadata,
                "automatic_payment_methods": {
                    "enabled": True,
                    "allow_redirects": redirect_policy,
                },
            }
            capture_method = self._resolve_capture_method(metadata)
            if capture_method:
                payment_payload["capture_method"] = capture_method

            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.create,
                payment_payload,
                request_options,
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
                    raw_response={
                        "id": payment_intent.id,
                        "status": payment_intent.status,
                        "amount": payment_intent.amount,
                        "currency": payment_intent.currency,
                        **({"capture_method": capture_method} if capture_method else {}),
                        **({"public_key": self.public_key} if self.public_key else {}),
                        **({"stripe_account": self.account_id} if self.account_id else {}),
                        **({"account_id": self.account_id} if self.account_id else {}),
                        **({"environment": self.environment} if self.environment else {}),
                    },
                    redirect_url=None,
                    public_key=self.public_key,
                ),
                None,
            )
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, None, str(e)

    async def capture_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        currency: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Capture a Stripe PaymentIntent created with capture_method=manual."""
        try:
            payment_intent_ref, resolve_error = await self._resolve_payment_intent_ref(payment_intent_id)
            if resolve_error or not payment_intent_ref:
                return False, None, resolve_error or "Unable to resolve payment_intent"

            capture_data: Dict[str, Any] = {}
            if amount:
                capture_data["amount_to_capture"] = _to_minor_units(amount, currency)

            request_options = None
            if idempotency_key:
                request_options = {"idempotency_key": str(idempotency_key)}

            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.capture,
                payment_intent_ref,
                capture_data,
                request_options,
            )
            return True, str(getattr(payment_intent, "id", payment_intent_ref)), None
        except Exception as e:
            return False, None, str(e)

    async def cancel_payment_authorization(
        self,
        payment_intent_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Cancel an uncaptured Stripe PaymentIntent authorization."""
        try:
            payment_intent_ref, resolve_error = await self._resolve_payment_intent_ref(payment_intent_id)
            if resolve_error or not payment_intent_ref:
                return False, None, resolve_error or "Unable to resolve payment_intent"

            cancel_data: Dict[str, Any] = {}
            reason_norm = str(reason or "").strip().lower()
            if reason_norm in {"duplicate", "fraudulent", "requested_by_customer", "abandoned"}:
                cancel_data["cancellation_reason"] = reason_norm

            request_options = None
            if idempotency_key:
                request_options = {"idempotency_key": str(idempotency_key)}

            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.cancel,
                payment_intent_ref,
                cancel_data,
                request_options,
            )
            return True, str(getattr(payment_intent, "id", payment_intent_ref)), None
        except Exception as e:
            return False, None, str(e)
    
    async def confirm_payment(
        self,
        payment_intent_id: str,
        payment_method_id: str
    ) -> Tuple[bool, str, Optional[str]]:
        """确认 Stripe 支付"""
        try:
            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.confirm,
                payment_intent_id,
                {
                    "payment_method": payment_method_id,
                },
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
            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.retrieve,
                payment_intent_id,
            )
            return True, payment_intent.status, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, "unknown", str(e)

    async def get_payment_status_details(
        self,
        payment_intent_id: str,
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """Return status plus amount/currency evidence for paid-transition reconciliation."""

        def _get(obj: Any, key: str, default: Any = None) -> Any:
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            try:
                return obj.get(key, default)
            except Exception:
                return getattr(obj, key, default)

        def _minor_to_major(value: Any) -> Optional[str]:
            if value is None:
                return None
            try:
                return str((Decimal(str(value)) / Decimal("100")).quantize(Decimal("0.01")))
            except Exception:
                return None

        try:
            if str(payment_intent_id or "").startswith("cs_"):
                session = await asyncio.to_thread(
                    self._client.v1.checkout.sessions.retrieve,
                    payment_intent_id,
                    {"expand": ["payment_intent"]},
                )
                payment_intent = _get(session, "payment_intent")
                payment_status = str(_get(session, "payment_status", "") or "").lower()
                intent_status = str(_get(payment_intent, "status", "") or "").lower()
                status = intent_status or ("succeeded" if payment_status == "paid" else payment_status or "unknown")
                if intent_status == "requires_capture":
                    amount_minor = None
                    if payment_intent is not None:
                        amount_minor = _get(payment_intent, "amount") or _get(payment_intent, "amount_capturable")
                else:
                    amount_minor = (
                        _get(payment_intent, "amount_received")
                        if payment_intent is not None
                        else None
                    )
                if amount_minor is None:
                    amount_minor = _get(session, "amount_total")
                currency = _get(payment_intent, "currency") if payment_intent is not None else None
                currency = currency or _get(session, "currency")
                return (
                    True,
                    {
                        "status": status,
                        "amount": _minor_to_major(amount_minor),
                        "currency": str(currency or "").upper() or None,
                        "payment_reference_type": "stripe_checkout_session",
                    },
                    None,
                )

            payment_intent = await asyncio.to_thread(
                self._client.v1.payment_intents.retrieve,
                payment_intent_id,
            )
            status = str(_get(payment_intent, "status", "") or "").lower() or "unknown"
            amount_minor = _get(payment_intent, "amount_received")
            if status == "requires_capture":
                amount_minor = _get(payment_intent, "amount") or _get(payment_intent, "amount_capturable")
            if amount_minor is None:
                amount_minor = _get(payment_intent, "amount")
            return (
                True,
                {
                    "status": status,
                    "amount": _minor_to_major(amount_minor),
                    "currency": str(_get(payment_intent, "currency", "") or "").upper() or None,
                    "payment_reference_type": "stripe_payment_intent",
                    "capture_method": _get(payment_intent, "capture_method"),
                },
                None,
            )
        except Exception as e:
            return False, {"status": "unknown"}, str(e)
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        currency: Optional[str] = None,
        full_refund: Optional[bool] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Stripe 退款"""
        try:
            payment_reference = str(payment_intent_id or "").strip()
            payment_intent_ref = payment_reference
            if payment_reference.startswith("cs_"):
                session = await asyncio.to_thread(
                    self._client.v1.checkout.sessions.retrieve,
                    payment_reference,
                    {"expand": ["payment_intent"]},
                )
                session_payment_intent = getattr(session, "payment_intent", None)
                if isinstance(session_payment_intent, dict):
                    payment_intent_ref = str(session_payment_intent.get("id") or "").strip()
                elif hasattr(session_payment_intent, "id"):
                    payment_intent_ref = str(session_payment_intent.id or "").strip()
                else:
                    payment_intent_ref = str(session_payment_intent or "").strip()
                if not payment_intent_ref:
                    return False, None, f"Checkout Session {payment_reference} has no payment_intent"

            refund_data = {"payment_intent": payment_intent_ref}
            if amount:
                refund_data["amount"] = _to_minor_units(amount, currency)
            if reason:
                # Stripe only accepts a small enum for `reason`.
                # Keep caller-provided human text as metadata instead of failing the refund.
                allowed_reasons = {"duplicate", "fraudulent", "requested_by_customer"}
                reason_norm = str(reason).strip()
                if reason_norm in allowed_reasons:
                    refund_data["reason"] = reason_norm
                else:
                    refund_data["metadata"] = {"reason": reason_norm}
            if payment_reference.startswith("cs_"):
                metadata = dict(refund_data.get("metadata") or {})
                metadata["checkout_session_id"] = payment_reference
                refund_data["metadata"] = metadata

            request_options = None
            if idempotency_key:
                request_options = {"idempotency_key": str(idempotency_key)}

            refund = await asyncio.to_thread(
                self._client.v1.refunds.create,
                refund_data,
                request_options,
            )
            return True, refund.id, None
        except Exception as e:
            # Fall back to generic exception to avoid dependency on stripe.error namespace
            return False, None, str(e)

    async def get_refund_details(
        self,
        refund_id: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
        try:
            refund = await asyncio.to_thread(
                self._client.v1.refunds.retrieve,
                refund_id,
            )
            if hasattr(refund, "to_dict"):
                payload = refund.to_dict()
            elif isinstance(refund, dict):
                payload = refund
            else:
                payload = {}
            return True, payload if isinstance(payload, dict) else {}, None
        except Exception as e:
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

    @staticmethod
    def _resolve_adyen_capture_delay_hours(metadata: Dict[str, Any]) -> Optional[int]:
        capture_method = str(
            metadata.get("adyen_capture_method")
            or metadata.get("capture_method")
            or metadata.get("payment_capture_method")
            or ""
        ).strip().lower()
        payment_flow = str(metadata.get("payment_flow") or "").strip().lower()
        explicit_delay = metadata.get("adyen_capture_delay_hours")
        if explicit_delay is not None:
            try:
                delay = int(explicit_delay)
                return max(1, min(delay, 672))
            except Exception:
                return None
        if capture_method == "manual" or payment_flow == "authorization_first":
            # Adyen Checkout Sessions expose captureDelayHours in-request. True
            # manual capture may also require merchant-account configuration, so
            # order-flow auth-first stays disabled until merchant setup is proven.
            return 672
        return None
    
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
                    "value": _to_minor_units(amount, currency),
                    "currency": currency
                },
                "reference": metadata.get("order_id", "ORDER"),
                "merchantAccount": self.merchant_account,
                "returnUrl": "https://merchant.pivota.cc/payment/success",
                "countryCode": "US",
                "shopperLocale": "en_US",
                "channel": "Web"
            }
            capture_delay_hours = self._resolve_adyen_capture_delay_hours(metadata)
            if capture_delay_hours is not None:
                payload["captureDelayHours"] = capture_delay_hours

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
                            amount=_to_minor_units(amount, currency),
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

    async def capture_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        currency: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Capture an authorised Adyen payment by its pspReference."""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            }
            if idempotency_key:
                headers["Idempotency-Key"] = str(idempotency_key)[:64]

            payload: Dict[str, Any] = {
                "merchantAccount": self.merchant_account,
                "reference": str(idempotency_key or f"capture_{payment_intent_id[:16]}")[:80],
            }
            if amount is None or not currency:
                return False, None, "Adyen capture requires amount and currency"
            payload["amount"] = {
                "value": _to_minor_units(amount, currency),
                "currency": str(currency).upper(),
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/{payment_intent_id}/captures",
                    json=payload,
                    headers=headers,
                    timeout=10.0,
                )
                if response.status_code in (200, 201, 202):
                    data = response.json()
                    return True, data.get("pspReference") or data.get("paymentPspReference"), None
                return False, None, f"Adyen capture error: {response.status_code} - {response.text}"
        except Exception as e:
            return False, None, str(e)

    async def cancel_payment_authorization(
        self,
        payment_intent_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Cancel an authorised Adyen payment before capture by its pspReference."""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            }
            if idempotency_key:
                headers["Idempotency-Key"] = str(idempotency_key)[:64]

            payload = {
                "merchantAccount": self.merchant_account,
                "reference": str(idempotency_key or reason or f"cancel_{payment_intent_id[:16]}")[:80],
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/payments/{payment_intent_id}/cancels",
                    json=payload,
                    headers=headers,
                    timeout=10.0,
                )
                if response.status_code in (200, 201, 202):
                    data = response.json()
                    return True, data.get("pspReference") or data.get("paymentPspReference") or payment_intent_id, None
                return False, None, f"Adyen cancel error: {response.status_code} - {response.text}"
        except Exception as e:
            return False, None, str(e)
    
    async def refund_payment(
        self,
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        currency: Optional[str] = None,
        full_refund: Optional[bool] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Adyen 退款"""
        try:
            headers = {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json"
            }

            if idempotency_key:
                headers["Idempotency-Key"] = str(idempotency_key)[:64]

            reference = str(idempotency_key or reason or f"refund_{payment_intent_id[:16]}")[:80]
            payload = {
                "merchantAccount": self.merchant_account,
                "reference": reference,
            }

            # For a full refund right after AUTHORISATION, Adyen recommends using
            # reversals so the request succeeds whether the payment is only
            # authorised or has already been captured.
            if full_refund:
                endpoint = f"{self.base_url}/payments/{payment_intent_id}/reversals"
            else:
                endpoint = f"{self.base_url}/payments/{payment_intent_id}/refunds"
                payload["amount"] = {
                    "value": int((amount or Decimal("0")) * 100),
                    "currency": str(currency or "USD").upper(),
                }

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code in (200, 201):
                    data = response.json()
                    return True, data.get("pspReference"), None
                else:
                    return False, None, f"Adyen refund error: {response.status_code} - {response.text}"
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
            public_key=kwargs.get("public_key"),
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
