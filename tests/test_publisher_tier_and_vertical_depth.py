"""Tests for the publisher registry tier/cadence enrichment (PR-7c)
and industry vertical depth extension (PR-7d).
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# PR-7c — publisher registry tier + cadence + grounding weight
# ---------------------------------------------------------------------


def test_classify_host_includes_tier_for_known_top_publishers():
    from services.cited_host_classifier import classify_host
    forbes = classify_host("forbes.com")
    assert forbes["tier"] == 1
    nymag = classify_host("nymag.com")
    assert nymag["tier"] == 1


def test_classify_host_includes_tier_for_niche_publishers():
    from services.cited_host_classifier import classify_host
    trail = classify_host("trailandkale.com")
    assert trail["tier"] == 2


def test_classify_host_default_tier_for_unknown_editorial():
    """Unknown editorial host gets conservative tier 3 default — only
    when the registry classifies it as editorial type."""
    from services.cited_host_classifier import classify_host
    # Unknown host that's not in registry → unclassified, no tier
    unknown = classify_host("nonexistent-blog-2026.example.com")
    assert unknown.get("tier") is None


def test_classify_host_includes_editorial_cadence():
    from services.cited_host_classifier import classify_host
    forbes = classify_host("forbes.com")
    assert forbes["editorial_cadence"] == "quarterly"
    womens = classify_host("womenshealthmag.com")
    assert womens["editorial_cadence"] == "annual"


def test_classify_host_includes_ai_grounding_weight():
    from services.cited_host_classifier import classify_host
    forbes = classify_host("forbes.com")
    assert forbes["ai_grounding_weight"] == "high"
    trail = classify_host("trailandkale.com")
    assert trail["ai_grounding_weight"] == "medium"


def test_classify_host_includes_outreach_cycle_weeks_from_cadence():
    """Quarterly cadence → 4-8 week outreach cycle. Annual → 12-24."""
    from services.cited_host_classifier import classify_host
    forbes = classify_host("forbes.com")
    assert forbes["expected_outreach_cycle_weeks"] == [4, 8]
    womens = classify_host("womenshealthmag.com")
    assert womens["expected_outreach_cycle_weeks"] == [12, 24]


def test_classify_host_explicit_registry_value_wins_over_default():
    """If the registry JSON explicitly sets a tier/cadence, it
    overrides the code default. Verifies the explicit-wins precedence
    in classify_host's enrichment block."""
    from services.cited_host_classifier import classify_host
    # nytimes.com is in default map as tier 1 + quarterly cadence;
    # confirm those land on the output (no explicit override in
    # registry JSON, so default should fire)
    nyt = classify_host("nytimes.com")
    assert nyt["tier"] == 1
    assert nyt["editorial_cadence"] == "quarterly"


def test_classify_host_unknown_host_has_null_enrichment_fields():
    """Hosts not in registry AND not in default map get null
    tier/cadence/weight/cycle. Renderer must handle nulls
    gracefully."""
    from services.cited_host_classifier import classify_host
    unknown = classify_host("nonexistent-2026.example.com")
    # Always present (defensive shape)
    assert "tier" in unknown
    assert "editorial_cadence" in unknown
    assert "ai_grounding_weight" in unknown
    assert "expected_outreach_cycle_weeks" in unknown
    # All null because host has no registry entry + no default
    assert unknown["tier"] is None
    assert unknown["editorial_cadence"] is None


# ---------------------------------------------------------------------
# PR-7d — industry vertical depth (market size + sub-category trends)
# ---------------------------------------------------------------------


def test_industry_context_for_wellness_includes_market_size():
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="daily greens supplements")
    # Wellness vertical (post-PR-434 split from fitness)
    assert ctx["category"] == "wellness"
    assert ctx["market_size_billions_usd"] == 6_500
    assert ctx["market_size_year"] == 2024
    assert ctx["growth_horizon_years"] == "2024-2028"


def test_industry_context_for_wellness_includes_gummy_subcategory():
    """Grüns case: wellness vertical includes a wellness-gummies
    sub-category trend specifically — feeds form-factor-aware
    narrative downstream."""
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="daily greens gummies")
    subs = ctx.get("sub_category_trends") or []
    sub_names = [s.get("sub", "").lower() for s in subs]
    assert any("gummies" in n for n in sub_names), (
        "wellness sub_category_trends should include gummy-specific entry"
    )
    # Find the gummies entry and confirm growth_pct is populated
    gummy_entry = next(
        (s for s in subs if "gummies" in s.get("sub", "").lower()), None
    )
    assert gummy_entry is not None
    assert gummy_entry["growth_pct"] == 14


def test_industry_context_for_beauty_includes_vertical_comparison():
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="serum")
    comparison = ctx.get("comparison_to_other_verticals") or ""
    # Beauty vs other verticals framing
    assert "beauty" in comparison.lower()
    assert "electronics" in comparison.lower()  # named comparison anchor


def test_industry_context_for_electronics_includes_market_size():
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="headphones")
    assert ctx["category"] == "electronics"
    assert ctx["market_size_billions_usd"] == 1_350


def test_industry_context_for_unknown_vertical_has_null_depth_fields():
    """Default vertical (no keyword match) gets null market_size +
    empty sub_category_trends. Renderer must handle gracefully."""
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type=None, product_title=None)
    assert ctx["category"] == "default"
    assert ctx["market_size_billions_usd"] is None
    assert ctx["sub_category_trends"] == []
    assert ctx["comparison_to_other_verticals"] is None


def test_industry_context_for_fashion_includes_loungewear_subcategory():
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="sleepwear")
    subs = ctx.get("sub_category_trends") or []
    sub_names = [s.get("sub", "").lower() for s in subs]
    assert any("loungewear" in n or "sleepwear" in n for n in sub_names)


def test_industry_context_default_has_safe_depth_fields():
    """Even the default fallback returns the required depth fields
    (nullable but present) so renderers don't have to defensively
    check field existence."""
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(product_type="completely_unknown_category")
    # All required depth fields exist
    assert "market_size_billions_usd" in ctx
    assert "sub_category_trends" in ctx
    assert "comparison_to_other_verticals" in ctx
    assert isinstance(ctx["sub_category_trends"], list)


# ---------------------------------------------------------------------
# Integration: classify_cited_hosts surfaces enriched fields in audit
# ---------------------------------------------------------------------


def test_classify_cited_hosts_propagates_tier_and_cadence():
    from services.cited_host_classifier import classify_cited_hosts
    cited = [
        {"host": "forbes.com", "times_cited": 3},
        {"host": "trailandkale.com", "times_cited": 1},
    ]
    classified = classify_cited_hosts(cited, merchant_category="wellness")
    assert classified[0]["tier"] == 1
    assert classified[0]["editorial_cadence"] == "quarterly"
    assert classified[0]["times_cited"] == 3
    assert classified[1]["tier"] == 2
    assert classified[1]["times_cited"] == 1


def test_build_structured_report_industry_context_has_depth():
    """End-to-end: the audit response's industry_context block
    includes the new depth fields."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestBrand",
        merchant_pdp_url="https://test.com/p",
        product_title="Greens Gummies",
        product_vendor=None,
        product_type="daily greens gummies",
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
    industry = report.get("industry_context") or {}
    # PR-7d depth fields surface
    assert industry.get("market_size_billions_usd") == 6_500
    assert industry.get("comparison_to_other_verticals")
    assert len(industry.get("sub_category_trends", [])) > 0
