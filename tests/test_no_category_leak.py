"""
Phase C-4 (PR-A) — category-leak enforcement.

The audit engine had two places where beauty-specific retailer names
("Sephora", "Ulta", "Vogue", "beauty marketplaces") leaked into output
for non-beauty products:

  1. Several `verdict_for` explanation paragraphs hardcoded those
     retailers (stripped by the main PR-A refactor).
  2. `_build_competitive_pressure` framing's "first-mover opportunity"
     branch hardcoded "(Vogue, Sephora, Ulta, Target, beauty
     marketplaces)" regardless of the merchant's actual category.
  3. `_industry_context_for` didn't recognize sleepwear / intimates /
     loungewear / swimwear keywords, so the test merchant's sleepwear
     products fell through to default — fine on its own, but combined
     with (2) they saw beauty-flavored output anyway.

These tests assert the diagnostic for a sleepwear merchant contains
no beauty-only retailer names, and that sleepwear keywords route to
the fashion industry context.
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.agent_center_bd_report_service import (
    _build_competitive_pressure,
    _industry_context_for,
)


# Beauty-only retailers that must not appear when the audit's actual
# retailer_hosts don't include them. (Sephora / Ulta CAN legitimately
# appear if a sleepwear brand IS surfaced via Sephora — our concern is
# the hardcoded fallback list, not real grounded results.)
HARDCODED_BEAUTY_RETAILERS = [
    "Sephora",
    "Ulta",
    "Vogue",
    "Target, beauty marketplaces",
    "beauty marketplaces",
]


def test_competitive_pressure_first_mover_uses_real_retailers_when_available():
    """When the audit surfaced real retailer hosts (e.g. Nordstrom,
    Macy's for a sleepwear merchant), framing names THOSE — not a
    hardcoded beauty fallback."""
    framing = _build_competitive_pressure(
        category_competitor_brands=[
            {"name": "Lunya", "times_cited": 3},
            {"name": "Eberjey", "times_cited": 2},
            {"name": "Cuup", "times_cited": 2},
        ],
        category_retailer_hosts=[
            {"host": "nordstrom.com", "times_cited": 5},
            {"host": "macys.com", "times_cited": 3},
            {"host": "shopbop.com", "times_cited": 2},
        ],
        merchant_brand="TestSleepwearBrand",
        merchant_host="testsleepwearbrand.com",
        merchant_attribution_score=0,
    )["framing"]
    # No hardcoded beauty retailer leak.
    for token in HARDCODED_BEAUTY_RETAILERS:
        assert token not in framing, (
            f"beauty retailer {token!r} hardcoded into sleepwear framing: {framing!r}"
        )
    # Real category retailers DO appear.
    assert "nordstrom.com" in framing
    assert "macys.com" in framing


def test_competitive_pressure_first_mover_no_hosts_uses_generic_language():
    """When the audit surfaced no cited hosts at all, framing falls
    back to a generic 'cited surface is split across third-party hosts'
    line — never beauty-specific names, never asserting 'retailers'
    when we don't know what those hosts are."""
    framing = _build_competitive_pressure(
        category_competitor_brands=[
            {"name": "Lunya", "times_cited": 1},
        ],
        category_retailer_hosts=[],
        merchant_brand="TestSleepwearBrand",
        merchant_host="testsleepwearbrand.com",
        merchant_attribution_score=0,
    )["framing"]
    for token in HARDCODED_BEAUTY_RETAILERS:
        assert token not in framing, (
            f"beauty retailer {token!r} leaked in zero-host fallback: {framing!r}"
        )
    assert "third-party hosts" in framing


def test_industry_context_recognizes_sleepwear_as_fashion():
    """Sleepwear / pajama / loungewear / lingerie / swimwear all route
    to fashion industry context, not default."""
    for product_type in [
        "Sleepwear",
        "Women's Pajama Set",
        "Robe",
        "Loungewear Top",
        "Lingerie",
        "Swimwear",
        "Bralette",
    ]:
        ctx = _industry_context_for(product_type=product_type)
        assert ctx["category"] == "fashion", (
            f"{product_type!r} should route to fashion, got {ctx['category']!r}"
        )


def test_industry_context_does_not_misclassify_sleepwear_as_beauty():
    """Defensive: sleepwear keywords must NOT trigger the beauty
    keyword list (some overlap risk e.g. 'mask' ≠ 'sleep mask')."""
    ctx = _industry_context_for(product_type="Women's Sleepwear Set")
    assert ctx["category"] != "beauty"
    assert "beauty" not in (ctx.get("blurb") or "").lower()
