from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Dict

from services.platform_order_writeback_readiness import (
    ORDER_WRITEBACK_STATUS_CANARY,
    ORDER_WRITEBACK_STATUS_ENABLED,
    is_store_order_writeback_allowed,
    store_order_writeback_context,
)


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
        purchase_status="order_writeback_store_readiness_pending",
        purchase_requirement="validated_active_wix_store_with_order_writeback_status_enabled_or_canary",
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


# --- P-T2.1: protocol dimension (Tier-2 in-chat checkout capability gate) ------
#
# Which agentic-commerce protocols a merchant's rails can be driven by. This is
# the routing gate: the decision layer sends traffic to an in-chat protocol
# checkout only for protocols listed here, else it falls back to the redirect
# floor (never a dead end). Kept deliberately CONSERVATIVE + honest — a protocol
# is listed only where we can actually honor it today.
PROTOCOL_ACP = "acp"
PROTOCOL_UCP = "ucp"
PROTOCOL_AP2 = "ap2"

# Per-platform protocol support. Each entry: (protocol, requires_active_psp).
# `requires_active_psp=True` protocols only light up when the merchant has a
# live-charge-ready PSP — this mirrors the existing ephemeral ACP signal in
# readiness/sources/shopify_live.acp_checkout_supported (shopify_connected AND
# psp_config). Any platform absent here honestly resolves to no protocols.
#
# Today only Shopify + a live PSP can be driven via the (external) ACP provider.
# UCP is absent (readiness export only) and AP2 is dormant (router unmounted),
# so neither is ever listed until its lane actually ships (P-T2.3/P-T2.4).
_PLATFORM_PROTOCOLS: Dict[str, tuple[tuple[str, bool], ...]] = {
    "shopify": ((PROTOCOL_ACP, True),),
}


def get_platform_protocols(platform: str | None, *, has_live_psp: bool) -> list[str]:
    """Truthful protocols[] a merchant on `platform` can be checked out through.

    `has_live_psp` gates charge-capable protocols (ACP) so an order can only be
    routed to a protocol we can actually settle. Returns a stable, de-duplicated,
    conservatively-ordered list — empty (honest "redirect only") for any platform
    we cannot drive in-chat today.
    """
    key = str(platform or "").strip().lower()
    out: list[str] = []
    for protocol, requires_psp in _PLATFORM_PROTOCOLS.get(key, ()):  # noqa: E501
        if requires_psp and not has_live_psp:
            continue
        if protocol not in out:
            out.append(protocol)
    return out


def get_platform_protocols_for_store(
    platform: str | None,
    store: Dict[str, Any] | None,
    *,
    has_live_psp: bool,
) -> list[str]:
    """Store-aware protocols[] (P-T2.3.6 — Wix production ACP gate).

    Same truthful list as `get_platform_protocols`, but layers on the protocols
    whose capability is gated *per store* rather than per platform. Today that is
    Wix ACP: the in-chat capture connector is live (pivota-acp WixConnector →
    shared backend quote→order(acp)→pay), so a Wix merchant CAN be checked out
    in-chat — but only once THIS store's order-writeback readiness is `enabled`
    (an active, verified Wix store) and it has a live PSP to settle on.

    Deliberately kept OUT of the platform-global `_PLATFORM_PROTOCOLS` /
    `get_platform_protocols`: those stay honest that Wix *alone* (platform only,
    no store context) grants no protocol. This function is the only place the
    per-store grant is added, so it can never light up from a bare platform slug
    (e.g. a domain-fingerprinted crawl merchant with no connected store).

    Dark until a Wix store row is marked `order_writeback_status='enabled'`;
    `canary` status is intentionally NOT routable here (it is a per-order
    writeback gate keyed on a specific order id, not a pre-checkout signal).
    """
    protocols = get_platform_protocols(platform, has_live_psp=has_live_psp)
    key = str(platform or "").strip().lower()
    if (
        key == "wix"
        and has_live_psp
        and PROTOCOL_ACP not in protocols
        and is_store_order_writeback_allowed(store, platform="wix")
    ):
        protocols = [*protocols, PROTOCOL_ACP]
    return protocols


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


def get_store_runtime_platform_capabilities(
    platform: str | None,
    store: Dict[str, Any] | None,
    *,
    order_id: str | None = None,
) -> StorePlatformCapabilities:
    base = get_store_platform_capabilities(platform)
    key = str(platform or "").strip().lower()
    if key != "wix":
        return base

    context = store_order_writeback_context(store, order_id=order_id, platform="wix")
    if not context.get("allowed"):
        return base

    readiness_status = str(context.get("order_writeback_status") or "").strip().lower()
    if readiness_status == ORDER_WRITEBACK_STATUS_ENABLED:
        purchase_status = "order_writeback_ready"
        purchase_requirement = "active_wix_store_with_order_writeback_status_enabled"
    elif readiness_status == ORDER_WRITEBACK_STATUS_CANARY:
        purchase_status = "order_writeback_canary_ready"
        purchase_requirement = "matching_canary_order_id_on_active_wix_store"
    else:
        purchase_status = base.purchase_status
        purchase_requirement = base.purchase_requirement

    return replace(
        base,
        supports_platform_order_writeback=True,
        purchase_status=purchase_status,
        purchase_requirement=purchase_requirement,
    )


def platform_capability_matrix() -> Dict[str, Dict[str, Any]]:
    return {
        platform: get_store_platform_capabilities(platform).as_dict()
        for platform in _CAPABILITIES
    }
