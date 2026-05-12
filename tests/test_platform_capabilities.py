from __future__ import annotations


def test_non_shopify_platforms_are_not_live_quote_purchase_ready() -> None:
    from services.platform_capabilities import get_store_platform_capabilities

    for platform in ("wix", "woocommerce", "bigcommerce"):
        capabilities = get_store_platform_capabilities(platform)
        assert capabilities.supports_live_quote is False
        assert capabilities.supports_live_inventory_check is False
        assert capabilities.supports_inventory_hold is False
        assert capabilities.supports_authorize_capture is False

    assert get_store_platform_capabilities("wix").supports_platform_checkout is False
    assert get_store_platform_capabilities("wix").purchase_status == "requires_merchant_checkout_validation"
    assert get_store_platform_capabilities("woocommerce").supports_platform_checkout is True
    assert get_store_platform_capabilities("woocommerce").purchase_status == "requires_external_platform_checkout_validation"
    assert get_store_platform_capabilities("bigcommerce").supports_platform_checkout is True
    assert get_store_platform_capabilities("bigcommerce").purchase_status == "requires_external_platform_checkout_validation"
    assert get_store_platform_capabilities("woocommerce").supports_platform_order_writeback is True
    assert get_store_platform_capabilities("bigcommerce").supports_platform_order_writeback is True
    assert get_store_platform_capabilities("wix").supports_platform_order_writeback is False


def test_custom_and_headless_tiers_audit_only_no_writeback() -> None:
    """PR-10c: custom and headless_generic merchants get audit + AI-channel
    discovery today; order writeback gated on a custom integration. The
    capability flags must reflect that — not the generic "unknown" stub —
    so downstream copy can speak specifically to that tier."""
    from services.platform_capabilities import get_store_platform_capabilities

    for platform in ("custom", "headless_generic"):
        capabilities = get_store_platform_capabilities(platform)
        assert capabilities.platform == platform, (
            f"capabilities for {platform!r} must surface the exact platform "
            f"key, not the unknown fallback"
        )
        assert capabilities.supports_live_quote is False
        assert capabilities.supports_live_inventory_check is False
        assert capabilities.supports_platform_checkout is False
        assert capabilities.supports_platform_order_writeback is False
        assert capabilities.supports_inventory_reservation is False
        assert capabilities.supports_inventory_hold is False
        assert capabilities.supports_authorize_capture is False
        assert (
            capabilities.purchase_status
            == "requires_custom_integration_for_order_writeback"
        ), (
            f"{platform!r} must surface the custom-integration purchase_status "
            f"so renderers don't conflate it with truly unknown platforms"
        )


def test_custom_tier_distinct_from_unknown_fallback() -> None:
    """Regression guard: an explicit 'custom' platform must NOT fall through
    to the generic 'unknown_requires_validation' stub (which would make
    downstream copy evasive). PR-10c entries break that conflation."""
    from services.platform_capabilities import get_store_platform_capabilities

    explicit_custom = get_store_platform_capabilities("custom")
    explicit_headless = get_store_platform_capabilities("headless_generic")
    truly_unknown = get_store_platform_capabilities("totally-made-up-platform")

    assert explicit_custom.purchase_status != truly_unknown.purchase_status
    assert explicit_headless.purchase_status != truly_unknown.purchase_status
    assert truly_unknown.purchase_status == "unknown_requires_validation"


def test_capability_matrix_exposes_custom_and_headless_entries() -> None:
    """platform_capability_matrix() is consumed by agent_v2 routes; both
    new tiers must surface so capability-matrix consumers can disclose
    them without needing to enumerate dataclass keys themselves."""
    from services.platform_capabilities import platform_capability_matrix

    matrix = platform_capability_matrix()
    assert "custom" in matrix
    assert "headless_generic" in matrix
    assert matrix["custom"]["supports_platform_order_writeback"] is False
    assert matrix["headless_generic"]["supports_platform_order_writeback"] is False


def test_shopify_platform_supports_live_quote_with_no_reservation_claim() -> None:
    from services.platform_capabilities import get_store_platform_capabilities

    capabilities = get_store_platform_capabilities("shopify")
    assert capabilities.supports_live_quote is True
    assert capabilities.supports_live_inventory_check is True
    assert capabilities.supports_platform_order_writeback is True
    assert capabilities.supports_inventory_reservation is False
    assert capabilities.supports_inventory_hold is False
    assert capabilities.supports_authorize_capture is False
    assert capabilities.supports_auto_void is False
    assert capabilities.supports_auto_refund is False


def test_psp_capabilities_are_provider_specific() -> None:
    from services.psp_capabilities import get_psp_capabilities

    stripe = get_psp_capabilities("stripe")
    assert stripe.supports_authorize_capture is True
    assert stripe.supports_capture is True
    assert stripe.supports_void_authorization is True
    assert stripe.supports_auto_refund is True
    assert stripe.order_flow_auth_first_enabled is False

    adyen = get_psp_capabilities("adyen")
    assert adyen.supports_authorize_capture is True
    assert adyen.supports_manual_capture_create is False
    assert adyen.supports_capture is True
    assert adyen.supports_void_authorization is True
    assert adyen.supports_auto_refund is True
    assert adyen.order_flow_auth_first_enabled is False

    checkout = get_psp_capabilities("checkout")
    assert checkout.supports_authorize_capture is True
    assert checkout.supports_manual_capture_create is False
    assert checkout.supports_capture is True
    assert checkout.supports_void_authorization is True
    assert checkout.supports_auto_refund is True
    assert checkout.order_flow_auth_first_enabled is False

    paypal = get_psp_capabilities("paypal")
    assert paypal.supports_authorize_capture is True
    assert paypal.supports_capture is True
    assert paypal.supports_void_authorization is True
    assert paypal.supports_auto_refund is True
    assert paypal.order_flow_auth_first_enabled is False

    unknown = get_psp_capabilities("unknown_psp")
    assert unknown.supports_auto_refund is False


def test_stripe_auth_first_capability_is_feature_flagged(monkeypatch) -> None:
    from config import feature_flags
    import services.psp_capabilities as module

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_authorization_first_orders", True)
    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_stripe_manual_capture", True)

    stripe = module.get_psp_capabilities("stripe")

    assert stripe.supports_authorize_capture is True
    assert stripe.order_flow_auth_first_enabled is True


def test_paypal_auth_first_capability_is_feature_flagged(monkeypatch) -> None:
    from config import feature_flags
    import services.psp_capabilities as module

    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_authorization_first_orders", True)
    monkeypatch.setitem(feature_flags.FEATURE_FLAGS, "enable_paypal_authorization_first", True)

    paypal = module.get_psp_capabilities("paypal")

    assert paypal.supports_authorize_capture is True
    assert paypal.order_flow_auth_first_enabled is True
