"""Settlement rails — the mid-man decoupling of agent-side protocol from
merchant-side settlement (2026-07-23).

The legacy `protocols[]` matrix defines protocol capability as "merchant handed
Pivota a chargeable PSP key" (`requires_active_psp`) — the least mid-man rail.
`get_platform_settlement_rails` answers the wider truthful question: on which
rails can a transaction PASS THROUGH to this merchant, with the merchant's own
checkout (`has_shopify_payments`) and their own native agentic endpoint modeled
alongside the legacy pivota_psp mode. `protocols[]` behavior stays byte-identical
(covered by the existing test_merchant_capability_resolver suite).
"""

from __future__ import annotations

from services.platform_capabilities import (
    SETTLEMENT_DELEGATED_TOKEN,
    SETTLEMENT_PIVOTA_PSP,
    SETTLEMENT_PLATFORM_NATIVE,
    get_platform_settlement_rails,
)


def _by_rail(rails):
    return {r["rail"]: r for r in rails}


# --- shopify: all three rails modeled ----------------------------------------

def test_shopify_live_psp_only_pivota_psp_available():
    rails = _by_rail(get_platform_settlement_rails("shopify", has_live_psp=True))
    assert rails[SETTLEMENT_PIVOTA_PSP]["available"] is True
    # No Shopify-Payments fact -> the merchant's own checkout is NOT assumed.
    assert rails[SETTLEMENT_PLATFORM_NATIVE]["available"] is False
    assert rails[SETTLEMENT_DELEGATED_TOKEN]["available"] is False


def test_shopify_native_payments_without_psp_handover():
    # THE mid-man case: merchant verified with Shopify Payments, gave Pivota no
    # PSP keys. Their own checkout is a usable rail; pivota_psp honestly is not.
    rails = _by_rail(get_platform_settlement_rails(
        "shopify", has_live_psp=False, has_native_payments=True,
    ))
    assert rails[SETTLEMENT_PLATFORM_NATIVE]["available"] is True
    assert rails[SETTLEMENT_PIVOTA_PSP]["available"] is False


def test_shopify_unknown_native_payments_is_not_yes():
    # None = merchant never went through the Shopify verify. Unknown must never
    # light a rail (the module's standing honesty rule).
    rails = _by_rail(get_platform_settlement_rails(
        "shopify", has_live_psp=False, has_native_payments=None,
    ))
    assert rails[SETTLEMENT_PLATFORM_NATIVE]["available"] is False


def test_shopify_delegated_token_needs_reachability_signal():
    dark = _by_rail(get_platform_settlement_rails("shopify", has_live_psp=False))
    assert dark[SETTLEMENT_DELEGATED_TOKEN]["available"] is False
    lit = _by_rail(get_platform_settlement_rails(
        "shopify", has_live_psp=False, native_endpoint_reachable=True,
    ))
    assert lit[SETTLEMENT_DELEGATED_TOKEN]["available"] is True


# --- wix: pivota_psp is per-store, no native rails ---------------------------

def _wix_store(*, status="active", order_writeback_status="enabled"):
    return {
        "store_id": "wix_store_1",
        "platform": "wix",
        "status": status,
        "order_writeback_status": order_writeback_status,
    }


def test_wix_pivota_psp_mirrors_the_store_writeback_gate():
    # Same per-store gate as get_platform_protocols_for_store: PSP + enabled
    # order-writeback store -> chargeable; either missing -> not.
    ready = _by_rail(get_platform_settlement_rails(
        "wix", _wix_store(), has_live_psp=True,
    ))
    assert ready[SETTLEMENT_PIVOTA_PSP]["available"] is True

    no_store = _by_rail(get_platform_settlement_rails("wix", None, has_live_psp=True))
    assert no_store[SETTLEMENT_PIVOTA_PSP]["available"] is False

    no_psp = _by_rail(get_platform_settlement_rails(
        "wix", _wix_store(), has_live_psp=False,
    ))
    assert no_psp[SETTLEMENT_PIVOTA_PSP]["available"] is False


def test_wix_has_no_native_rails():
    rails = _by_rail(get_platform_settlement_rails("wix", _wix_store(), has_live_psp=True))
    assert SETTLEMENT_PLATFORM_NATIVE not in rails
    assert SETTLEMENT_DELEGATED_TOKEN not in rails


# --- woocommerce / bigcommerce: platform checkout modeled, honestly dark -----

def test_woocommerce_platform_native_listed_but_dark():
    rails = _by_rail(get_platform_settlement_rails("woocommerce", has_live_psp=True))
    assert SETTLEMENT_PIVOTA_PSP not in rails  # no wired charge connector
    assert rails[SETTLEMENT_PLATFORM_NATIVE]["available"] is False
    assert "external" in rails[SETTLEMENT_PLATFORM_NATIVE]["requirement"]


# --- unknown / empty: no rails, never a guess --------------------------------

def test_unknown_platforms_have_no_rails():
    for platform in (None, "", "unknown", "custom", "headless_generic"):
        rails = get_platform_settlement_rails(platform, has_live_psp=True)
        for r in rails:
            assert r["available"] is False, f"{platform}: no rail may be available"
    assert get_platform_settlement_rails("unknown", has_live_psp=True) == []
    assert get_platform_settlement_rails(None, has_live_psp=True) == []
