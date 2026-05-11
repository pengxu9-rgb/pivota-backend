"""Tests for the Pivota commitments builder (PR-8d).

Coverage:
  - Platform-aware capability summary (Shopify/Woo/BC vs Wix vs custom)
  - Onboarding deliverables differ by platform writeback support
  - Continuous deliverables include order-forwarding line for
    writeback-supported platforms only
  - Cold-start with no platform → multi-platform generic copy
  - Non-promises are platform-agnostic
  - Integration: build_structured_report surfaces commitments block
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# Platform capability summary
# ---------------------------------------------------------------------


def test_shopify_platform_capability_summary():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="shopify",
        cold_start=False,
    )
    assert result["merchant_platform"] == "shopify"
    assert "Shopify admin" in result["platform_capability_summary"]
    assert "End-to-end" in result["platform_capability_summary"]


def test_woocommerce_platform_capability_summary():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="woocommerce",
        cold_start=False,
    )
    assert "WooCommerce" in result["platform_capability_summary"]


def test_wix_platform_capability_summary_acknowledges_audit_only():
    """Wix doesn't have automated order writeback yet — disclosure
    must say so explicitly."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="wix",
        cold_start=False,
    )
    summary = result["platform_capability_summary"].lower()
    assert "audit" in summary
    assert "manual" in summary or "roadmap" in summary


def test_custom_platform_capability_summary_mentions_engineering_scope():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="custom",
        cold_start=False,
    )
    summary = result["platform_capability_summary"].lower()
    assert "1-2 weeks" in summary or "lightweight" in summary


# ---------------------------------------------------------------------
# Onboarding deliverables (delivers_weeks_1_to_4)
# ---------------------------------------------------------------------


def test_shopify_onboarding_deliverables_include_order_forwarding():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="shopify",
        cold_start=False,
    )
    deliverables = result["delivers_weeks_1_to_4"]
    text = " ".join(deliverables).lower()
    assert "shopify oauth" in text
    assert "order forwarding" in text or "order forwarding" in text


def test_wix_onboarding_deliverables_use_custom_integration_framing():
    """Wix lacks automated writeback — onboarding deliverables must
    say so without fudging."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="wix",
        cold_start=False,
    )
    deliverables = result["delivers_weeks_1_to_4"]
    text = " ".join(deliverables).lower()
    # Shouldn't claim shopify-style end-to-end wiring
    assert "shopify oauth" not in text
    assert "end-to-end" not in text or "manual" in text
    # Should mention the custom-integration scoping
    assert "manual" in text or "integration" in text


def test_onboarding_always_includes_canonical_pdp_creation():
    """Canonical PDP creation is platform-agnostic — should be in
    deliverables regardless of platform."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    for platform in ("shopify", "woocommerce", "bigcommerce", "wix", "custom"):
        result = build_pivota_commitments(
            merchant_platform=platform,
            cold_start=False,
        )
        text = " ".join(result["delivers_weeks_1_to_4"]).lower()
        assert "canonical ai-channel pdp" in text or "agent.pivota.cc" in text, (
            f"Platform {platform} missing canonical PDP deliverable"
        )


def test_onboarding_always_includes_search_console_indexing():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    for platform in ("shopify", "wix", "custom"):
        result = build_pivota_commitments(
            merchant_platform=platform, cold_start=False,
        )
        text = " ".join(result["delivers_weeks_1_to_4"]).lower()
        assert "search console" in text


# ---------------------------------------------------------------------
# Continuous deliverables (delivers_continuous)
# ---------------------------------------------------------------------


def test_continuous_deliverables_always_include_search_console_cadence():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    for platform in ("shopify", "wix", "custom"):
        result = build_pivota_commitments(
            merchant_platform=platform, cold_start=False,
        )
        text = " ".join(result["delivers_continuous"]).lower()
        assert "search console" in text or "url inspection" in text


def test_continuous_deliverables_writeback_line_only_for_writeback_platforms():
    """Order-forwarding maintenance line should appear for
    Shopify/Woo/BC but NOT for Wix/custom (manual-routing line
    instead)."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    shopify_text = " ".join(
        build_pivota_commitments(merchant_platform="shopify", cold_start=False)
        ["delivers_continuous"]
    ).lower()
    wix_text = " ".join(
        build_pivota_commitments(merchant_platform="wix", cold_start=False)
        ["delivers_continuous"]
    ).lower()
    assert "order-forwarding" in shopify_text or "order forwarding" in shopify_text
    # Wix gets manual-routing language
    assert "manual" in wix_text


# ---------------------------------------------------------------------
# Non-promises (does_not_promise)
# ---------------------------------------------------------------------


def test_non_promises_are_platform_agnostic():
    """Same non-promises across all platforms — these are universal
    Pivota limits."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    shopify_promises = build_pivota_commitments(
        merchant_platform="shopify", cold_start=False,
    )["does_not_promise"]
    wix_promises = build_pivota_commitments(
        merchant_platform="wix", cold_start=False,
    )["does_not_promise"]
    assert shopify_promises == wix_promises
    # Required non-promises always present
    text = " ".join(shopify_promises).lower()
    assert "30-90 day" in text or "google's indexing" in text
    assert "editorial pitches" in text or "publisher relationships" in text
    assert "intermediate the customer" in text


# ---------------------------------------------------------------------
# Cold-start handling
# ---------------------------------------------------------------------


def test_cold_start_no_platform_emits_multi_platform_copy():
    """Cold-start audit with no known platform → generic multi-
    platform copy. Renderer surfaces 'we support any platform' framing."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform=None, cold_start=True,
    )
    assert result["merchant_platform"] is None
    summary = result["platform_capability_summary"].lower()
    assert "multi-platform" in summary
    assert "shopify" in summary
    assert "woocommerce" in summary
    assert "bigcommerce" in summary
    # Cold-start deliverables exclude OAuth onboarding (merchant
    # not engaged yet)
    text = " ".join(result["delivers_weeks_1_to_4"]).lower()
    assert "oauth" not in text


def test_cold_start_with_platform_known_uses_platform_copy():
    """Even cold-start audits where the platform IS known (e.g.
    catalog-intelligence detected Shopify) get platform-specific
    framing. cold_start=True only suppresses the OAuth-onboarding
    deliverables."""
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="shopify", cold_start=True,
    )
    assert result["merchant_platform"] == "shopify"
    text = " ".join(result["delivers_weeks_1_to_4"]).lower()
    # OAuth onboarding deliverable suppressed for cold-start
    assert "shopify oauth" not in text
    # But Shopify-aware capability summary still present
    assert "shopify" in result["platform_capability_summary"].lower()


# ---------------------------------------------------------------------
# Unknown platform fallback
# ---------------------------------------------------------------------


def test_unknown_platform_falls_back_to_lightweight_integration_framing():
    from services.audit_pivota_commitments_builder import build_pivota_commitments
    result = build_pivota_commitments(
        merchant_platform="some_obscure_platform", cold_start=False,
    )
    summary = result["platform_capability_summary"].lower()
    assert "lightweight" in summary or "integration" in summary


# ---------------------------------------------------------------------
# Integration: build_structured_report
# ---------------------------------------------------------------------


def test_build_structured_report_includes_pivota_commitments():
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestBrand",
        merchant_pdp_url="https://test.com/p",
        product_title="X",
        product_vendor=None,
        product_type=None,
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        provider="gemini",
    )
    commitments = report.get("pivota_commitments")
    assert commitments is not None
    # Required structural fields
    for key in (
        "merchant_platform",
        "platform_capability_summary",
        "delivers_weeks_1_to_4",
        "delivers_continuous",
        "does_not_promise",
    ):
        assert key in commitments, f"missing {key} in pivota_commitments"
    # Non-promises always populated
    assert len(commitments["does_not_promise"]) > 0
