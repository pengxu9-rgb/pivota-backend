from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from services.platform_capabilities import get_store_platform_capabilities


EXECUTION_POLICY_VERSION = "2026-04-29.v1"

COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST = "pivota_direct_quote_first"
COMMERCE_PATH_EXTERNAL_PLATFORM_CHECKOUT = "external_platform_checkout"
COMMERCE_PATH_LEGACY_ADMIN = "legacy_admin"
COMMERCE_PATH_UNSUPPORTED = "unsupported"

SURFACE_PUBLIC_AGENT_PURCHASE = "public_agent_purchase"
SURFACE_EXTERNAL_PLATFORM_CHECKOUT = "external_platform_checkout"
SURFACE_LEGACY_ADMIN = "legacy_admin"
SURFACE_CACHE_ESTIMATE = "cache_estimate"

VALIDATION_AUTHORITY_PIVOTA_LIVE_QUOTE = "pivota_live_quote"
VALIDATION_AUTHORITY_MERCHANT_PLATFORM_CHECKOUT = "merchant_platform_checkout"
VALIDATION_AUTHORITY_CACHE_ESTIMATE = "cache_estimate"
VALIDATION_AUTHORITY_UNSUPPORTED = "unsupported"

_EXTERNAL_CHECKOUT_PLATFORMS = {"woocommerce", "bigcommerce"}


@dataclass(frozen=True)
class CommerceExecutionPolicy:
    commerce_path: str
    platform: str
    surface: str
    allows_pivota_order: bool
    allows_psp_creation: bool
    requires_live_quote: bool
    allows_external_redirect: bool
    legacy_or_fallback: bool
    validation_authority: str
    execution_policy_version: str = EXECUTION_POLICY_VERSION
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def response_fields(self) -> Dict[str, Any]:
        return {
            "commerce_path": self.commerce_path,
            "execution_policy": self.as_dict(),
            "legacy_or_fallback": self.legacy_or_fallback,
            "validation_authority": self.validation_authority,
        }


def resolve_commerce_execution_policy(
    *,
    platform: Optional[str],
    surface: str,
) -> CommerceExecutionPolicy:
    normalized_platform = str(platform or "").strip().lower() or "unknown"
    normalized_surface = str(surface or "").strip().lower()
    capabilities = get_store_platform_capabilities(normalized_platform)

    if normalized_surface == SURFACE_PUBLIC_AGENT_PURCHASE:
        if capabilities.supports_live_quote and normalized_platform == "shopify":
            return CommerceExecutionPolicy(
                commerce_path=COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST,
                platform=normalized_platform,
                surface=normalized_surface,
                allows_pivota_order=True,
                allows_psp_creation=True,
                requires_live_quote=True,
                allows_external_redirect=False,
                legacy_or_fallback=False,
                validation_authority=VALIDATION_AUTHORITY_PIVOTA_LIVE_QUOTE,
                reason="shopify_live_quote_and_final_revalidation_required",
            )
        return CommerceExecutionPolicy(
            commerce_path=COMMERCE_PATH_UNSUPPORTED,
            platform=normalized_platform,
            surface=normalized_surface,
            allows_pivota_order=False,
            allows_psp_creation=False,
            requires_live_quote=True,
            allows_external_redirect=False,
            legacy_or_fallback=False,
            validation_authority=VALIDATION_AUTHORITY_UNSUPPORTED,
            reason="platform_live_quote_adapter_required_before_pivota_direct_purchase",
        )

    if normalized_surface == SURFACE_EXTERNAL_PLATFORM_CHECKOUT:
        if normalized_platform in _EXTERNAL_CHECKOUT_PLATFORMS and capabilities.supports_platform_checkout:
            return CommerceExecutionPolicy(
                commerce_path=COMMERCE_PATH_EXTERNAL_PLATFORM_CHECKOUT,
                platform=normalized_platform,
                surface=normalized_surface,
                allows_pivota_order=False,
                allows_psp_creation=False,
                requires_live_quote=False,
                allows_external_redirect=True,
                legacy_or_fallback=True,
                validation_authority=VALIDATION_AUTHORITY_MERCHANT_PLATFORM_CHECKOUT,
                reason="merchant_platform_checkout_is_final_validation_authority",
            )
        return CommerceExecutionPolicy(
            commerce_path=COMMERCE_PATH_UNSUPPORTED,
            platform=normalized_platform,
            surface=normalized_surface,
            allows_pivota_order=False,
            allows_psp_creation=False,
            requires_live_quote=False,
            allows_external_redirect=False,
            legacy_or_fallback=True,
            validation_authority=VALIDATION_AUTHORITY_UNSUPPORTED,
            reason="verified_external_platform_checkout_adapter_required",
        )

    if normalized_surface == SURFACE_LEGACY_ADMIN:
        return CommerceExecutionPolicy(
            commerce_path=COMMERCE_PATH_LEGACY_ADMIN,
            platform=normalized_platform,
            surface=normalized_surface,
            allows_pivota_order=True,
            allows_psp_creation=True,
            requires_live_quote=False,
            allows_external_redirect=False,
            legacy_or_fallback=True,
            validation_authority=VALIDATION_AUTHORITY_CACHE_ESTIMATE,
            reason="legacy_or_admin_path_must_not_be_treated_as_agent_direct_purchase",
        )

    return CommerceExecutionPolicy(
        commerce_path=COMMERCE_PATH_UNSUPPORTED,
        platform=normalized_platform,
        surface=normalized_surface or "unknown",
        allows_pivota_order=False,
        allows_psp_creation=False,
        requires_live_quote=True,
        allows_external_redirect=False,
        legacy_or_fallback=True,
        validation_authority=VALIDATION_AUTHORITY_UNSUPPORTED,
        reason="unknown_commerce_execution_surface",
    )


def cache_estimate_execution_policy(
    *,
    platform: Optional[str] = None,
    surface: str = SURFACE_CACHE_ESTIMATE,
) -> CommerceExecutionPolicy:
    normalized_platform = str(platform or "").strip().lower() or "unknown"
    normalized_surface = str(surface or "").strip().lower() or SURFACE_CACHE_ESTIMATE
    return CommerceExecutionPolicy(
        commerce_path=COMMERCE_PATH_UNSUPPORTED,
        platform=normalized_platform,
        surface=normalized_surface,
        allows_pivota_order=False,
        allows_psp_creation=False,
        requires_live_quote=True,
        allows_external_redirect=False,
        legacy_or_fallback=True,
        validation_authority=VALIDATION_AUTHORITY_CACHE_ESTIMATE,
        reason="cache_estimate_is_discovery_only_and_not_purchase_authoritative",
    )
