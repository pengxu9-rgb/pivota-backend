"""Pivota commitments builder (PR-8d).

Generates the explicit "Pivota's commitment" + "what Pivota does
not promise" section that the polished Grüns report ends with —
the part that converts the audit from analysis to engagement-
ready proposal. Today's `_build_what_pivota_changes` produces a
verbose Layer-1/Layer-2 narrative; this complements it with a
tighter table of concrete commitments + explicit non-promises,
platform-aware so the disclosure is accurate per merchant.

**Why explicit non-promises matter.**

Telling a client "we don't promise Layer 1 lift on a guaranteed
timeline — Google's indexing arc is the rate-limiting step" up
front is more credible than letting them assume guarantees and
discover the caveat later. The polished Grüns report's "What
Pivota does not do" list is one of its most credibility-building
elements.

**Platform-aware behavior.**

Calling out "Order forwarding wired end-to-end into Shopify" for
a merchant on Wix would be over-promising. The builder branches
on platform_capabilities.supports_platform_order_writeback +
supports_platform_checkout to produce accurate disclosure per
merchant.

**Output shape:**

```python
{
  "merchant_platform": "shopify",
  "platform_capability_summary": "End-to-end order writeback shipped",
  "delivers_weeks_1_to_4": [
    "Standard Pivota merchant onboarding (KYB + PSP + Shopify OAuth) — 5-7 business days",
    "Canonical AI-channel PDPs created at agent.pivota.cc/products/* with embedded Schema.org structured data",
    ...
  ],
  "delivers_continuous": [
    "Weekly Search Console URL Inspection cadence for new SKUs",
    "Monthly diagnostic re-audit + quarterly trend report",
    ...
  ],
  "does_not_promise": [
    "Layer 1 grounded LLM citation lift on a guaranteed timeline",
    "We do not write your editorial pitches — your PR team owns the relationship",
    ...
  ],
}
```
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.platform_capabilities import (
    get_store_platform_capabilities,
    StorePlatformCapabilities,
)


# Platform capability summary strings used in the response header.
# Renderer surfaces this as a 1-line "what Pivota offers your
# platform" caption.
_PLATFORM_CAPABILITY_SUMMARY: Dict[str, str] = {
    "shopify": (
        "End-to-end automated order writeback into Shopify admin"
    ),
    "woocommerce": (
        "End-to-end automated order writeback into WooCommerce admin"
    ),
    "bigcommerce": (
        "End-to-end automated order writeback into BigCommerce admin"
    ),
    "wix": (
        "Audit + AI-channel discovery shipped today; automated order "
        "writeback on Q3 roadmap (manual order routing in the interim)"
    ),
    "custom": (
        "Audit + AI-channel discovery shipped today; order writeback "
        "via lightweight integration of your custom order API "
        "(typical scope: 1-2 weeks engineering)"
    ),
    "headless_generic": (
        "Audit + AI-channel discovery shipped today; order writeback "
        "via lightweight integration of your headless commerce API "
        "(typical scope: 1-2 weeks engineering)"
    ),
}


def _platform_capability_summary(
    capabilities: StorePlatformCapabilities,
) -> str:
    """Return the renderer-friendly capability caption for the
    merchant's platform. Falls back to a generic capability summary
    derived from the capability flags when platform isn't in the
    explicit map (handles future / custom platforms without a code
    change to add a label)."""
    explicit = _PLATFORM_CAPABILITY_SUMMARY.get(capabilities.platform.lower())
    if explicit:
        return explicit
    if capabilities.supports_platform_order_writeback:
        return (
            f"End-to-end automated order writeback into "
            f"{capabilities.platform.title()} admin"
        )
    return (
        "Audit + AI-channel discovery shipped today; order writeback "
        "via lightweight integration of your platform's order API "
        "(typical scope: 1-2 weeks engineering)"
    )


def _delivers_weeks_1_to_4(
    *,
    capabilities: StorePlatformCapabilities,
    cold_start: bool,
) -> List[str]:
    """The 4-week onboarding deliverables. Always include Pivota's
    canonical-PDP infrastructure + indexing acceleration. Add the
    platform-specific order-forwarding wiring only when it's
    actually shipped for this platform."""
    items: List[str] = []
    if not cold_start:
        # Onboarding-time items
        if capabilities.supports_platform_order_writeback:
            items.append(
                f"Standard merchant onboarding (KYB + PSP + "
                f"{capabilities.platform.title()} OAuth) — typically "
                f"5-7 business days end-to-end"
            )
            items.append(
                f"Order forwarding wired end-to-end into your "
                f"{capabilities.platform.title()} admin; first-party "
                f"customer data + source-attribution metadata "
                f"(`source = pivota_acp`, `agent = gemini` or other) "
                f"flow into your existing storefront within seconds "
                f"of in-chat checkout completion"
            )
        else:
            # Custom / Wix / non-writeback platform: lighter onboarding
            items.append(
                "Custom integration scoping (typically 1-2 weeks "
                "engineering depending on your platform's order API "
                "maturity); KYB + PSP onboarding in parallel"
            )
            items.append(
                "Manual order routing for in-chat checkout completions "
                "until automated writeback adapter ships"
            )
    items.extend([
        "Canonical AI-channel PDPs created for your full catalog at "
        "agent.pivota.cc/products/* with embedded Schema.org Product "
        "+ Offer + BreadcrumbList structured data optimized for "
        "grounded LLM retrieval",
        "ACP and UCP API endpoints made queryable against your "
        "catalog (immediate Layer 2 availability — agent-direct "
        "queries from the moment onboarding completes)",
        "Initial Search Console sitemap submission and top-SKU URL "
        "Inspection requests executed (Layer 1 indexing acceleration "
        "begins immediately)",
    ])
    return items


def _delivers_continuous(
    *,
    capabilities: StorePlatformCapabilities,
) -> List[str]:
    """Ongoing operations after week 4. Platform-agnostic for
    indexing + diagnostic work; platform-specific only for the
    order-forwarding maintenance line."""
    items = [
        "Weekly Search Console URL Inspection cadence for new SKUs "
        "and update cycles",
        "Monthly diagnostic re-audit + quarterly trend report "
        "delivered to your brand and growth teams",
        "Editorial relationship support: Pivota provides audit data "
        "and competitive intelligence to support your PR team's "
        "pitch cycles, and tracks publication impact in subsequent "
        "re-audits",
    ]
    if capabilities.supports_platform_order_writeback:
        items.append(
            f"Continuous order-forwarding from in-chat agent checkouts "
            f"into your {capabilities.platform.title()} admin"
        )
    else:
        items.append(
            "Continuous monitoring of in-chat agent checkout "
            "completions; manual order routing into your platform "
            "until automated writeback ships"
        )
    return items


def _does_not_promise() -> List[str]:
    """Explicit non-promises — same across platforms. These are the
    hard limits we want clients to know about up front, not
    discover later."""
    return [
        "Pivota does not promise Layer 1 (grounded LLM citation) "
        "lift on a guaranteed timeline. The 30-90 day window is "
        "bounded by Google's indexing cadence, not Pivota's effort. "
        "We co-invest by submitting Search Console URL Inspection "
        "requests weekly, but cannot accelerate Google's crawl "
        "beyond what that surface enables.",
        "Pivota does not write your editorial pitches. Your PR team "
        "owns the publisher relationships; we provide the audit "
        "data and competitive intelligence that strengthens the "
        "pitch.",
        "Pivota does not intermediate the customer relationship. "
        "All in-chat agent orders flow into your existing "
        "storefront admin with first-party customer data; you own "
        "the customer record.",
        "Pivota does not guarantee inclusion in any specific "
        "editorial roundup. Editorial outcomes depend on the "
        "publisher's own review process.",
    ]


def build_pivota_commitments(
    *,
    merchant_platform: Optional[str],
    cold_start: bool = False,
) -> Dict[str, Any]:
    """Build the Pivota commitments block keyed off merchant
    platform + onboarding state.

    Args:
      merchant_platform: e.g. 'shopify' / 'wix' / 'woocommerce' /
        'bigcommerce' / 'custom' / None (cold-start with unknown
        platform — emits generic multi-platform copy)
      cold_start: True when this is a pre-onboarding audit (BD pitch
        artifact). Skips OAuth-onboarding deliverables; emphasizes
        what Pivota would do once engaged.

    Returns the structured shape consumed by markdown + HTML
    renderers.
    """
    # Cold-start with no platform → emit generic multi-platform
    # copy; renderer surfaces "we support any platform" framing.
    if cold_start and not merchant_platform:
        return {
            "merchant_platform": None,
            "platform_capability_summary": (
                "Multi-platform: Shopify, WooCommerce, and BigCommerce "
                "supported end-to-end for automated order writeback. "
                "Wix supported for audit + manual order routing today. "
                "Custom and headless storefronts supported via "
                "lightweight engineering integration."
            ),
            "delivers_weeks_1_to_4": [
                "Canonical AI-channel PDPs created for your full "
                "catalog at agent.pivota.cc/products/* with embedded "
                "Schema.org structured data optimized for grounded "
                "LLM retrieval",
                "ACP and UCP API endpoints made queryable against "
                "your catalog (immediate Layer 2 availability)",
                "Initial Search Console sitemap submission and "
                "top-SKU URL Inspection requests executed (Layer 1 "
                "indexing acceleration begins immediately)",
            ],
            "delivers_continuous": [
                "Weekly Search Console URL Inspection cadence",
                "Monthly diagnostic re-audit + quarterly trend report",
                "Editorial relationship support with audit data",
            ],
            "does_not_promise": _does_not_promise(),
        }

    capabilities = get_store_platform_capabilities(merchant_platform)
    return {
        "merchant_platform": capabilities.platform,
        "platform_capability_summary": _platform_capability_summary(
            capabilities,
        ),
        "delivers_weeks_1_to_4": _delivers_weeks_1_to_4(
            capabilities=capabilities,
            cold_start=cold_start,
        ),
        "delivers_continuous": _delivers_continuous(
            capabilities=capabilities,
        ),
        "does_not_promise": _does_not_promise(),
    }
