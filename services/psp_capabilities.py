from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from config.feature_flags import is_feature_enabled


@dataclass(frozen=True)
class PSPCapabilities:
    provider: str
    supports_authorize_capture: bool
    supports_manual_capture_create: bool
    supports_capture: bool
    supports_void_authorization: bool
    supports_auto_refund: bool
    order_flow_auth_first_enabled: bool
    recovery_requirement: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_CAPABILITIES: Dict[str, PSPCapabilities] = {
    "stripe": PSPCapabilities(
        provider="stripe",
        supports_authorize_capture=True,
        supports_manual_capture_create=True,
        supports_capture=True,
        supports_void_authorization=True,
        supports_auto_refund=True,
        order_flow_auth_first_enabled=False,
        recovery_requirement="payment_intent_or_checkout_session_manual_capture_available_order_flow_feature_flag_required",
    ),
    "adyen": PSPCapabilities(
        provider="adyen",
        supports_authorize_capture=True,
        supports_manual_capture_create=False,
        supports_capture=True,
        supports_void_authorization=True,
        supports_auto_refund=True,
        order_flow_auth_first_enabled=False,
        recovery_requirement="capture_cancel_primitives_available_order_flow_requires_manual_capture_setup_and_async_capture_webhook",
    ),
    "checkout": PSPCapabilities(
        provider="checkout",
        supports_authorize_capture=True,
        supports_manual_capture_create=False,
        supports_capture=True,
        supports_void_authorization=True,
        supports_auto_refund=True,
        order_flow_auth_first_enabled=False,
        recovery_requirement="capture_void_primitives_available_order_flow_requires_authorize_session_create_and_webhook_finalization",
    ),
    "paypal": PSPCapabilities(
        provider="paypal",
        supports_authorize_capture=True,
        supports_manual_capture_create=True,
        supports_capture=True,
        supports_void_authorization=True,
        supports_auto_refund=True,
        order_flow_auth_first_enabled=False,
        recovery_requirement="authorize_capture_primitives_available_order_flow_feature_flag_required",
    ),
}


def get_psp_capabilities(provider: str | None) -> PSPCapabilities:
    key = str(provider or "").strip().lower()
    capabilities = _CAPABILITIES.get(
        key,
        PSPCapabilities(
            provider=key or "unknown",
            supports_authorize_capture=False,
            supports_manual_capture_create=False,
            supports_capture=False,
            supports_void_authorization=False,
            supports_auto_refund=False,
            order_flow_auth_first_enabled=False,
            recovery_requirement="provider_specific_validation_required",
        ),
    )
    if (
        key == "stripe"
        and is_feature_enabled("enable_authorization_first_orders")
        and is_feature_enabled("enable_stripe_manual_capture")
    ) or (
        key == "paypal"
        and is_feature_enabled("enable_authorization_first_orders")
        and is_feature_enabled("enable_paypal_authorization_first")
    ):
        return PSPCapabilities(
            **{
                **capabilities.as_dict(),
                "order_flow_auth_first_enabled": True,
                "recovery_requirement": "authorization_first_enabled_for_supported_order_flow",
            }
        )
    return capabilities


def psp_capability_matrix() -> Dict[str, Dict[str, Any]]:
    return {provider: get_psp_capabilities(provider).as_dict() for provider in _CAPABILITIES}
