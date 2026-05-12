"""PR-10d — platform-aware copy in _build_platform_specific_chain_tail.

The audit report's checkout_loop chain has 6 steps. Steps 5-6 + the
closing outcome line are platform-specific:
  - For platforms where Pivota has shipped order writeback
    (Shopify / WooCommerce / BigCommerce): "Order forwarded to
    merchant's <platform> admin async; verified end-to-end"
  - For platforms with audit-only support (Wix / custom / headless):
    "Order routed to operations queue for manual fulfillment"
  - For cold-start audits (platform unknown): multi-platform copy
    that lists targets without claiming any specific integration

Before PR-10d, all merchants saw Shopify-specific language regardless
of their actual platform — actively misleading for Woo / BC / Wix /
custom merchants. PR-10c added the underlying capability entries;
PR-10d threads them through into the visible report.
"""

from __future__ import annotations

from typing import Dict


def _call(platform):
    from services.agent_center_bd_report_service import (
        _build_platform_specific_chain_tail,
    )
    return _build_platform_specific_chain_tail(platform)


def _assert_chain_shape(out: Dict):
    """Every variant must return the three expected keys with the
    chain-entry shape downstream callers depend on."""
    assert set(out.keys()) == {"step_5", "step_6", "outcome"}
    for key in ("step_5", "step_6"):
        entry = out[key]
        assert isinstance(entry, dict)
        assert "step" in entry
        assert "label" in entry
        assert "evidence" in entry
        assert "shipped" in entry
        assert isinstance(entry["label"], str)
    assert isinstance(out["outcome"], str)


def test_shopify_renders_shopify_specific_copy():
    out = _call("shopify")
    _assert_chain_shape(out)
    # Display label uses canonical capitalization in client-facing
    # text — BD operators say "Shopify", not "shopify".
    assert "Shopify" in out["step_5"]["label"]
    assert "Shopify" in out["step_6"]["label"]
    assert "Shopify" in out["outcome"]
    # Shopify is shipped — both steps must claim shipped=True.
    assert out["step_5"]["shipped"] is True
    assert out["step_6"]["shipped"] is True
    # Outcome must speak in second person to the merchant
    # ("your Shopify admin").
    assert "your Shopify admin" in out["outcome"]


def test_woocommerce_renders_woocommerce_specific_copy():
    out = _call("woocommerce")
    _assert_chain_shape(out)
    assert "WooCommerce" in out["step_5"]["label"]
    assert "WooCommerce" in out["step_6"]["label"]
    assert "WooCommerce" in out["outcome"]
    # Writeback is shipped for WooCommerce too.
    assert out["step_5"]["shipped"] is True
    # Regression guard: no leftover "Shopify" in the output for a
    # WooCommerce merchant.
    full = (
        out["step_5"]["label"]
        + out["step_6"]["label"]
        + out["outcome"]
    )
    assert "Shopify" not in full, (
        "WooCommerce merchant copy must NOT mention Shopify — "
        "that was the pre-PR-10d misleading language"
    )


def test_bigcommerce_renders_bigcommerce_specific_copy():
    out = _call("bigcommerce")
    _assert_chain_shape(out)
    assert "BigCommerce" in out["step_5"]["label"]
    assert "BigCommerce" in out["outcome"]
    assert out["step_5"]["shipped"] is True


def test_wix_renders_audit_only_copy_with_manual_fulfillment():
    """Wix has no order-writeback adapter shipped; the report must
    say so honestly — manual fulfillment via Pivota operations
    rather than claiming an automated path that doesn't exist."""
    out = _call("wix")
    _assert_chain_shape(out)
    assert "Wix" in out["step_5"]["label"]
    # The honest disclosure: manual routing, not auto-writeback.
    assert "manual" in out["step_5"]["label"].lower()
    # shipped=False is the truthful flag for the no-writeback path.
    assert out["step_5"]["shipped"] is False
    assert out["step_6"]["shipped"] is False
    # Outcome should mention "one-business-day SLA" or "operations"
    # to set the expectation correctly.
    assert (
        "SLA" in out["outcome"]
        or "operations" in out["outcome"].lower()
    )


def test_custom_platform_renders_custom_storefront_copy():
    """Custom-built storefronts (per the PR-10c capability tier)
    get explicit "custom storefront" language in the chain."""
    out = _call("custom")
    _assert_chain_shape(out)
    assert "custom storefront" in out["step_5"]["label"]
    # Custom has no writeback adapter shipped — manual fulfillment.
    assert out["step_5"]["shipped"] is False
    assert "manual" in out["step_5"]["label"].lower()


def test_headless_generic_renders_headless_backend_copy():
    out = _call("headless_generic")
    _assert_chain_shape(out)
    assert "headless commerce backend" in out["step_5"]["label"]
    assert out["step_5"]["shipped"] is False


def test_unknown_platform_falls_back_to_multi_platform_copy():
    """Cold-start audits (no merchant onboarded) MUST NOT claim
    any specific platform is integrated for THIS merchant — the
    fallback lists all supported targets neutrally."""
    out = _call(None)
    _assert_chain_shape(out)
    # The multi-platform fallback should mention the shipped trio.
    full = (
        out["step_5"]["label"]
        + out["step_5"]["evidence"]
        + out["step_6"]["label"]
        + out["step_6"]["evidence"]
        + out["outcome"]
    )
    assert "Shopify" in full
    assert "WooCommerce" in full
    assert "BigCommerce" in full


def test_empty_string_platform_falls_back_to_multi_platform_copy():
    """An empty merchant_platform string should be treated as
    unknown (same as None) — defensive against upstream callers
    that pass ``""`` instead of ``None``."""
    out = _call("")
    _assert_chain_shape(out)
    # Same shape as the None case — multi-platform listing.
    full = (
        out["step_5"]["label"]
        + out["step_6"]["label"]
        + out["outcome"]
    )
    assert "Shopify" in full
    assert "WooCommerce" in full


def test_unrecognized_platform_falls_back_to_multi_platform_copy():
    """A platform name we don't have a display mapping for should
    fall back gracefully rather than emit raw lowercase text in
    client-facing copy."""
    out = _call("totally-new-platform")
    _assert_chain_shape(out)
    full = (
        out["step_5"]["label"]
        + out["step_6"]["label"]
        + out["outcome"]
    )
    # Should NOT surface the raw platform name in a sentence —
    # render the neutral multi-platform fallback instead.
    assert "totally-new-platform" not in full.lower()


def test_platform_casing_normalized():
    """`integration_state.store_platform_name` may arrive with
    arbitrary casing depending on upstream callers — coerce to
    lowercase before lookup so 'SHOPIFY' / 'WooCommerce' resolve
    to the same shipped tier."""
    upper = _call("SHOPIFY")
    title = _call("Shopify")
    assert "Shopify" in upper["step_5"]["label"]
    assert "Shopify" in title["step_5"]["label"]
    # Both must claim shipped=True (the underlying capability is
    # the same regardless of input casing).
    assert upper["step_5"]["shipped"] is True
    assert title["step_5"]["shipped"] is True
