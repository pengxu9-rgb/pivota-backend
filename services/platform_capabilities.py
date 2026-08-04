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
    Wix ACP: in-chat capture is live in-process (`services/acp_checkout_session_service`
    drives the shared gate chain → off-session capture; ADR-021), so a Wix merchant
    CAN be checked out in-chat — but only once THIS store's order-writeback readiness is `enabled`
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


# --- Settlement rails (mid-man alignment, 2026-07-23) --------------------------
#
# Founder-set frame: Pivota is a MID-MAN — it passes transactions and rich data
# through protocols between agents and merchant offers; it is never buyer, seller,
# merchant-of-record, or a custodian of funds. Under that frame the agent-side
# protocol (which door the agent came through: MCP / ACP REST / UCP) is ORTHOGONAL
# to the merchant-side settlement rail (how the money actually settles). The
# legacy `protocols[]` matrix above conflates the two: it defines "protocol
# capable" as "handed Pivota a chargeable PSP key" (`requires_active_psp`), which
# is the LEAST mid-man rail and structurally excludes merchants who can settle on
# their own infrastructure.
#
# The rails, most- to least-aligned with the mid-man thesis:
#   platform_native  — the transaction completes on the merchant's OWN platform
#                      checkout (e.g. Shopify Payments). Merchant gives Pivota
#                      nothing. Signal: pcs_merchant_capabilities.has_shopify_
#                      payments (collected by the Shopify verify, previously read
#                      by nothing).
#   delegated_token  — the transaction completes on the merchant's OWN native
#                      agentic endpoint (e.g. their Shopify UCP server) under a
#                      delegated payment authorization. Merchant gives Pivota
#                      nothing. No stored reachability signal exists yet, so this
#                      rail is modeled but never `available` until a probe feeds
#                      it — honest, like every other gate in this module.
#   pivota_psp       — Pivota executes the charge on PSP keys the merchant handed
#                      over (today's ONLY wired mode; the Tier-2 ACP lane).
#                      Correct as a fallback for merchants with no other rail,
#                      wrong as the definition of protocol readiness.
#
# `get_platform_protocols*` above is UNCHANGED and still answers the narrower
# question the in-chat charge router asks ("can Pivota itself settle this?").
# This function answers the mid-man question: on which rails can a transaction
# PASS THROUGH to this merchant, and is each rail usable with current facts?
SETTLEMENT_PIVOTA_PSP = "pivota_psp"
SETTLEMENT_PLATFORM_NATIVE = "platform_native"
SETTLEMENT_DELEGATED_TOKEN = "delegated_token"

# Platforms with a wired Pivota-side charge path (the in-process ACP lane).
_PIVOTA_PSP_CONNECTOR_PLATFORMS = frozenset({"shopify", "wix"})
# Platforms whose merchants can expose a native agentic endpoint today.
_DELEGATED_ENDPOINT_PLATFORMS = frozenset({"shopify"})


def get_platform_settlement_rails(
    platform: str | None,
    store: Dict[str, Any] | None = None,
    *,
    has_live_psp: bool,
    has_native_payments: bool | None = None,
    native_payments_as_of: Any = None,
    native_endpoint_reachable: bool = False,
) -> list[Dict[str, Any]]:
    """Truthful settlement rails for a merchant on `platform`.

    Each rail: ``{"rail", "available", "requirement", "protocol_scope"}`` plus,
    where meaningful, fact provenance (``source``/``as_of``) and the ``store_id``
    the rail was resolved against. A rail is listed when the platform supports it
    AT ALL, and ``available`` only when current facts satisfy its requirement —
    `None` facts (unknown) are never treated as yes.

    Contract notes (set now, while there are zero consumers):
      * ``protocol_scope`` — which agent-side protocol lane can DRIVE this rail
        for autonomous completion. Empty = door-agnostic (a handoff rail any
        door can route to; the buyer completes on the merchant's checkout).
      * ``as_of`` — when the underlying fact was last verified. A verified fact
        can go stale (merchant later disables Shopify Payments); consumers apply
        their own freshness floor rather than trusting `available` blindly.
      * ``store_id`` — the Wix pivota_psp rail is per-store; ambiguous for
        multi-store merchants without it.

    `has_native_payments` is the pcs_merchant_capabilities.has_shopify_payments
    fact (None = merchant never went through the Shopify verify). `store` matters
    only for the Wix per-store pivota_psp gate, mirroring
    `get_platform_protocols_for_store`.
    """
    key = str(platform or "").strip().lower()
    rails: list[Dict[str, Any]] = []
    if not key or key == "unknown":
        return rails

    caps = get_store_platform_capabilities(key)

    if key in _PIVOTA_PSP_CONNECTOR_PLATFORMS:
        chargeable = bool(
            get_platform_protocols_for_store(key, store, has_live_psp=has_live_psp)
        )
        rail: Dict[str, Any] = {
            "rail": SETTLEMENT_PIVOTA_PSP,
            "available": chargeable,
            "requirement": (
                "live_charge_ready_psp_and_store_order_writeback_enabled"
                if key == "wix"
                else "live_charge_ready_psp"
            ),
            "protocol_scope": [PROTOCOL_ACP],
        }
        if key == "wix":
            store_id = str((store or {}).get("store_id") or "").strip()
            rail["store_id"] = store_id or None
        rails.append(rail)

    if caps.supports_platform_checkout:
        if key == "shopify":
            rails.append({
                "rail": SETTLEMENT_PLATFORM_NATIVE,
                # Strict identity check on purpose: only a verified TRUE fact
                # lights the rail — unknown (None) and any truthy-non-True
                # value are treated as not-verified.
                "available": has_native_payments is True,
                "requirement": "shopify_payments_verified_on_merchant_store",
                # Door-agnostic handoff: any agent-side door can route to the
                # merchant's own checkout; the buyer completes there.
                "protocol_scope": [],
                "source": "pcs_shopify_verify",
                "as_of": native_payments_as_of,
            })
        else:
            # WooCommerce / BigCommerce declare platform checkout support but
            # per the matrix above still require external validation — no stored
            # per-merchant fact exists, so the rail is listed and honestly dark.
            rails.append({
                "rail": SETTLEMENT_PLATFORM_NATIVE,
                "available": False,
                "requirement": caps.purchase_requirement,
                "protocol_scope": [],
            })

    if key in _DELEGATED_ENDPOINT_PLATFORMS:
        rails.append({
            "rail": SETTLEMENT_DELEGATED_TOKEN,
            "available": bool(native_endpoint_reachable),
            "requirement": "merchant_native_agentic_endpoint_reachable_no_stored_signal_yet",
            "protocol_scope": [PROTOCOL_UCP],
        })

    return rails


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
