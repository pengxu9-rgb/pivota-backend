from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class StorePlatformCapabilities:
    platform: str
    supports_live_quote: bool
    supports_live_inventory_check: bool
    supports_platform_checkout: bool
    supports_platform_order_writeback: bool
    supports_inventory_reservation: bool
    supports_inventory_hold: bool
    supports_authorize_capture: bool
    supports_auto_void: bool
    supports_auto_refund: bool
    purchase_status: str
    purchase_requirement: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_CAPABILITIES: Dict[str, StorePlatformCapabilities] = {
    "shopify": StorePlatformCapabilities(
        platform="shopify",
        supports_live_quote=True,
        supports_live_inventory_check=True,
        supports_platform_checkout=True,
        supports_platform_order_writeback=True,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="purchase_ready_with_live_quote",
        purchase_requirement="live_quote_and_final_revalidation",
    ),
    "wix": StorePlatformCapabilities(
        platform="wix",
        supports_live_quote=False,
        supports_live_inventory_check=False,
        supports_platform_checkout=False,
        supports_platform_order_writeback=False,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="requires_merchant_checkout_validation",
        purchase_requirement="external_checkout_or_platform_live_quote_adapter",
    ),
    "woocommerce": StorePlatformCapabilities(
        platform="woocommerce",
        supports_live_quote=False,
        supports_live_inventory_check=False,
        supports_platform_checkout=True,
        supports_platform_order_writeback=True,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="requires_external_platform_checkout_validation",
        purchase_requirement="external_platform_checkout_or_platform_live_quote_adapter",
    ),
    "bigcommerce": StorePlatformCapabilities(
        platform="bigcommerce",
        supports_live_quote=False,
        supports_live_inventory_check=False,
        supports_platform_checkout=True,
        supports_platform_order_writeback=True,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="requires_external_platform_checkout_validation",
        purchase_requirement="external_platform_checkout_or_platform_live_quote_adapter",
    ),
    # PR-10c: Custom-built and headless storefronts. Audit + AI-channel
    # discovery work without any integration. Order writeback requires a
    # lightweight engineering integration against the merchant's own
    # order API (typical scope: 1-2 weeks).
    #
    # Distinct from "unknown" — these merchants have explicitly told us
    # they're on a non-major platform, so downstream copy can be
    # specific ("your custom order API") rather than evasive.
    "custom": StorePlatformCapabilities(
        platform="custom",
        supports_live_quote=False,
        supports_live_inventory_check=False,
        supports_platform_checkout=False,
        supports_platform_order_writeback=False,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="requires_custom_integration_for_order_writeback",
        purchase_requirement="merchant_custom_order_api_credentials_or_manual_fulfillment",
    ),
    "headless_generic": StorePlatformCapabilities(
        platform="headless_generic",
        supports_live_quote=False,
        supports_live_inventory_check=False,
        supports_platform_checkout=False,
        supports_platform_order_writeback=False,
        supports_inventory_reservation=False,
        supports_inventory_hold=False,
        supports_authorize_capture=False,
        supports_auto_void=False,
        supports_auto_refund=False,
        purchase_status="requires_custom_integration_for_order_writeback",
        purchase_requirement="merchant_headless_commerce_api_credentials_or_manual_fulfillment",
    ),
}


def get_store_platform_capabilities(platform: str | None) -> StorePlatformCapabilities:
    key = str(platform or "").strip().lower()
    return _CAPABILITIES.get(
        key,
        StorePlatformCapabilities(
            platform=key or "unknown",
            supports_live_quote=False,
            supports_live_inventory_check=False,
            supports_platform_checkout=False,
            supports_platform_order_writeback=False,
            supports_inventory_reservation=False,
            supports_inventory_hold=False,
            supports_authorize_capture=False,
            supports_auto_void=False,
            supports_auto_refund=False,
            purchase_status="unknown_requires_validation",
            purchase_requirement="merchant_specific_validation_required",
        ),
    )


def platform_capability_matrix() -> Dict[str, Dict[str, Any]]:
    return {platform: capabilities.as_dict() for platform, capabilities in _CAPABILITIES.items()}
