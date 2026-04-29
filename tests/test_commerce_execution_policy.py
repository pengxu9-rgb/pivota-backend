from services.commerce_execution_policy import (
    COMMERCE_PATH_EXTERNAL_PLATFORM_CHECKOUT,
    COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST,
    COMMERCE_PATH_UNSUPPORTED,
    SURFACE_EXTERNAL_PLATFORM_CHECKOUT,
    SURFACE_PUBLIC_AGENT_PURCHASE,
    VALIDATION_AUTHORITY_CACHE_ESTIMATE,
    VALIDATION_AUTHORITY_MERCHANT_PLATFORM_CHECKOUT,
    VALIDATION_AUTHORITY_PIVOTA_LIVE_QUOTE,
    cache_estimate_execution_policy,
    resolve_commerce_execution_policy,
)


def test_shopify_public_agent_purchase_allows_direct_quote_first() -> None:
    policy = resolve_commerce_execution_policy(
        platform="shopify",
        surface=SURFACE_PUBLIC_AGENT_PURCHASE,
    )

    assert policy.commerce_path == COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST
    assert policy.allows_pivota_order is True
    assert policy.allows_psp_creation is True
    assert policy.requires_live_quote is True
    assert policy.allows_external_redirect is False
    assert policy.legacy_or_fallback is False
    assert policy.validation_authority == VALIDATION_AUTHORITY_PIVOTA_LIVE_QUOTE


def test_non_shopify_public_agent_purchase_fails_closed() -> None:
    for platform in ("woocommerce", "bigcommerce", "wix", "unknown"):
        policy = resolve_commerce_execution_policy(
            platform=platform,
            surface=SURFACE_PUBLIC_AGENT_PURCHASE,
        )

        assert policy.commerce_path == COMMERCE_PATH_UNSUPPORTED
        assert policy.allows_pivota_order is False
        assert policy.allows_psp_creation is False
        assert policy.requires_live_quote is True
        assert policy.validation_authority == "unsupported"


def test_woocommerce_bigcommerce_external_checkout_allows_redirect_only() -> None:
    for platform in ("woocommerce", "bigcommerce"):
        policy = resolve_commerce_execution_policy(
            platform=platform,
            surface=SURFACE_EXTERNAL_PLATFORM_CHECKOUT,
        )

        assert policy.commerce_path == COMMERCE_PATH_EXTERNAL_PLATFORM_CHECKOUT
        assert policy.allows_pivota_order is False
        assert policy.allows_psp_creation is False
        assert policy.allows_external_redirect is True
        assert policy.legacy_or_fallback is True
        assert policy.validation_authority == VALIDATION_AUTHORITY_MERCHANT_PLATFORM_CHECKOUT


def test_unsupported_external_checkout_platforms_fail_closed() -> None:
    for platform in ("shopify", "wix", "unknown"):
        policy = resolve_commerce_execution_policy(
            platform=platform,
            surface=SURFACE_EXTERNAL_PLATFORM_CHECKOUT,
        )

        assert policy.commerce_path == COMMERCE_PATH_UNSUPPORTED
        assert policy.allows_pivota_order is False
        assert policy.allows_psp_creation is False
        assert policy.allows_external_redirect is False


def test_cache_estimate_policy_is_not_purchase_authoritative() -> None:
    policy = cache_estimate_execution_policy(platform="shopify", surface="agent_cart_validate")

    assert policy.commerce_path == COMMERCE_PATH_UNSUPPORTED
    assert policy.allows_pivota_order is False
    assert policy.allows_psp_creation is False
    assert policy.requires_live_quote is True
    assert policy.legacy_or_fallback is True
    assert policy.validation_authority == VALIDATION_AUTHORITY_CACHE_ESTIMATE
