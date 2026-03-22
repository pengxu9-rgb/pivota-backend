from __future__ import annotations

from typing import Any, Dict, Optional

COMMERCE_SURFACE_AGENT_API = "agent_api"
COMMERCE_SURFACE_UCP = "ucp"
COMMERCE_SURFACE_ACP = "acp"

SUPPORTED_COMMERCE_SURFACES = (
    COMMERCE_SURFACE_AGENT_API,
    COMMERCE_SURFACE_UCP,
    COMMERCE_SURFACE_ACP,
)


def normalize_commerce_surface(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in SUPPORTED_COMMERCE_SURFACES:
        return token
    return COMMERCE_SURFACE_AGENT_API


def commerce_surface_to_channel(surface: Any) -> Optional[str]:
    normalized = normalize_commerce_surface(surface)
    if normalized in {COMMERCE_SURFACE_UCP, COMMERCE_SURFACE_ACP}:
        return normalized
    return None


def payment_capabilities_support_surface(
    payment_capabilities: Optional[Dict[str, Any]],
    surface: Any,
) -> bool:
    capabilities = payment_capabilities or {}
    normalized = normalize_commerce_surface(surface)

    if normalized == COMMERCE_SURFACE_UCP:
        return bool(
            capabilities.get("ucp_checkout_supported")
            or capabilities.get("merchant_native_checkout_supported")
        )
    if normalized == COMMERCE_SURFACE_ACP:
        return bool(
            capabilities.get("acp_checkout_supported")
            or capabilities.get("merchant_native_checkout_supported")
        )
    return bool(
        capabilities.get("merchant_native_checkout_supported")
        or capabilities.get("ucp_checkout_supported")
        or capabilities.get("acp_checkout_supported")
    )
