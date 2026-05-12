"""Tests for the polished markdown renderer v2 (PR-9a)."""

from __future__ import annotations


def _gruns_fixture_audit():
    """Returns a brand_report with all v2-relevant fields populated."""
    return {
        "merchant_name": "Grüns",
        "merchant_domain": "gruns.co",
        "timestamp": "2026-05-09T22:00:00Z",
        "per_product": [{
            "executive_summary": {
                "narrative_archetype": "editorial_strong_attribution_weak",
                "opening_paragraphs": [
                    "Grüns, a Unilever-owned brand (acquired 2024), is *visible* in AI-assisted shopping search...",
                    "Yet first-party attribution is the gap.",
                    "The strategic implication is specific and closeable.",
                ],
                "headline_finding": "Grüns is editorially visible but lacks first-party attribution.",
                "strategic_implication": "Convert mention-share into citation share.",
                "verdict_pill_text": "Visible via retailers + editorial",
                "evidence_quotes_used": 1,
            },
            "verdict": {
                "label": "VISIBLE VIA RETAILERS",
                "label_display": "Visible via retailers + editorial",
                "explanation": "...",
                "visibility_score": 0,
                "attribution_score": 0,
                "category_visibility_score": 100,
            },
            "industry_context": {
                "category": "wellness",
                "blurb": "AI shopping is ~11% of D2C wellness traffic.",
                "ai_search_share_pct": 11,
                "market_size_billions_usd": 6_500,
                "market_size_year": 2024,
                "growth_horizon_years": "2024-2028",
                "sub_category_trends": [
                    {"sub": "wellness gummies", "growth_pct": 14, "why": "Form factor preference shift"},
                    {"sub": "daily greens powders", "growth_pct": 11, "why": "AG1-driven category education"},
                ],
                "comparison_to_other_verticals": "Wellness AI-search is among the fastest-growing.",
            },
            "evidence_quotes": [{
                "query": "best daily greens supplements",
                "excerpt_text": "Best Green Gummies: Grüns Superfoods Greens Gummies.",
                "source_labels": ["forbes.com"],
                "attribution_path": "merchant_named_in_grounded_excerpt",
            }],
            "cohort_form_factor": {
                "merchant_form_factor": "gummy",
                "merchant_owns_unique_form_factor": True,
                "competitors_in_merchant_form_factor": [],
                "form_factor_summary": {
                    "gummy": ["Grüns"],
                    "powder": ["AG1", "Bloom", "Huel Daily Greens"],
                },
            },
            "category_visibility": {
                "score": 100,
                "competitor_brands": [
                    {"name": "AG1 (Athletic Greens)", "times_cited": 1},
                    {"name": "Bloom Greens", "times_cited": 2},
                ],
            },
            "merchant_view": {
                "receipts": {
                    "cited_hosts_detailed": [
                        {
                            "host": "forbes.com", "type": "editorial",
                            "times_cited": 2, "tier": 1,
                            "editorial_cadence": "quarterly",
                            "ai_grounding_weight": "high",
                            "expected_outreach_cycle_weeks": [4, 8],
                            "pitch_recipient": {"email": "vetted@forbes.com"},
                        },
                        {
                            "host": "trailandkale.com", "type": "editorial",
                            "times_cited": 1, "tier": 2,
                            "editorial_cadence": None,
                            "ai_grounding_weight": "medium",
                            "expected_outreach_cycle_weeks": None,
                            "pitch_recipient": None,
                        },
                    ],
                },
                "actions": [
                    {
                        "priority_order": 1, "severity": "critical",
                        "title": "Index your canonical PDPs with Google Search Console",
                        "body": "Submit sitemap.xml and request URL Inspection for top SKUs.",
                        "owner": "pivota_ops", "phase": "week_1_to_4",
                        "kpi_to_track": "Number of canonical PDPs indexed by Google",
                        "expected_outcome": "First grounded citations within 30-60 days.",
                        "concrete_next_step": "Submit sitemap to Search Console this week.",
                    },
                    {
                        "priority_order": 2, "severity": "high",
                        "title": "Pitch forbes.com editorial team",
                        "body": "Refresh-cycle pitch to vetted@forbes.com.",
                        "owner": "merchant_brand_team", "phase": "week_4_to_12",
                        "kpi_to_track": "Editorial inclusion + Google index date",
                        "expected_outcome": "New citation propagates within 4-8 weeks.",
                        "expected_timeline_weeks": [4, 8],
                        "pitch_draft": {
                            "subject": "Grüns for upcoming Forbes Vetted refresh",
                            "body": "Hi Forbes Vetted team,\n\nGrüns appeared as Best Green Gummies...",
                            "recipient_email": "vetted@forbes.com",
                        },
                    },
                ],
            },
            "implementation_roadmap": {
                "phases": [
                    {
                        "phase_id": "week_1_to_4", "label": "Phase 1: Foundation",
                        "weeks": "1-4", "weeks_low": 1, "weeks_high": 4,
                        "owners": ["pivota_ops"],
                        "activities": [{"title": "Index PDPs", "owner": "pivota_ops"}],
                        "activity_count": 1,
                        "expected_outcome": "Indexing accelerated.",
                    },
                    {
                        "phase_id": "week_4_to_12", "label": "Phase 2: Editorial Reinforcement",
                        "weeks": "4-12", "weeks_low": 4, "weeks_high": 12,
                        "owners": ["merchant_brand_team"],
                        "activities": [{"title": "Pitch Forbes", "owner": "merchant_brand_team"}],
                        "activity_count": 1,
                        "expected_outcome": "Editorial inclusion in Q3 refresh.",
                    },
                ],
                "total_weeks": 12, "total_activities": 2,
            },
            "pivota_commitments": {
                "merchant_platform": None,
                "platform_capability_summary": "Multi-platform: Shopify, WooCommerce, BigCommerce supported.",
                "delivers_weeks_1_to_4": [
                    "Canonical AI-channel PDPs created at agent.pivota.cc/products/*",
                    "ACP and UCP API endpoints queryable",
                ],
                "delivers_continuous": [
                    "Weekly Search Console URL Inspection cadence",
                    "Monthly diagnostic re-audit",
                ],
                "does_not_promise": [
                    "Layer 1 lift on a guaranteed timeline (Google indexing arc bounds it)",
                    "We do not write your editorial pitches",
                ],
            },
            "upstream_status": {
                "is_real": True, "requested_provider": "gemini",
                "visibility_provider": "gemini", "attribution_provider": "gemini",
            },
        }],
    }


def test_renders_executive_summary_section():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Executive Summary" in md
    assert "Unilever-owned brand" in md


def test_renders_headline_metrics_table():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Headline Metrics" in md
    assert "100/100" in md
    assert "Visible via retailers + editorial" in md


def test_renders_strategic_context_with_market_size():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Strategic Context" in md
    assert "$6500B" in md or "~$6500B" in md
    assert "wellness gummies" in md


def test_renders_evidence_quotes_section():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Evidence Quotes" in md
    assert "Best Green Gummies: Grüns Superfoods Greens Gummies" in md
    assert "forbes.com" in md


def test_renders_competitive_analysis_with_form_factor_moat():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Competitive Analysis" in md
    assert "only one in the cohort" in md
    assert "AG1" in md


def test_renders_publisher_analysis_with_tier_and_cadence():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Editorial Publisher Analysis" in md
    assert "Tier 1" in md
    assert "Quarterly refresh" in md
    assert "vetted@forbes.com" in md


def test_renders_recommendations_with_owner_kpi_outcome():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Recommendations" in md
    assert "Pivota Ops" in md
    assert "Merchant brand team" in md
    assert "KPI to track" in md
    assert "Expected outcome" in md
    assert "Suggested outreach" in md


def test_renders_implementation_roadmap():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Implementation Roadmap" in md
    assert "Phase 1: Foundation" in md
    assert "Phase 2: Editorial Reinforcement" in md


def test_renders_pivota_commitments_section():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Pivota's Commitment" in md
    assert "Multi-platform" in md
    assert "What Pivota does not promise" in md
    assert "Layer 1 lift on a guaranteed timeline" in md


def test_renders_methodology_section():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "## Methodology" in md
    assert "gemini" in md.lower()
    assert "hallucinated brand mentions" in md


def test_renders_appendix_with_citation_data():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert "Appendix: Citation Data" in md
    assert "forbes.com" in md
    assert "trailandkale.com" in md


def test_section_ordering_matches_polished_pdf():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    sections_in_order = [
        "## Executive Summary",
        "## Headline Metrics",
        "## Strategic Context",
        "## Evidence Quotes",
        "## Competitive Analysis",
        "## Editorial Publisher Analysis",
        "## Recommendations",
        "## Implementation Roadmap",
        "## Pivota's Commitment",
        "## Methodology",
        "## Appendix: Citation Data",
    ]
    last_index = -1
    for section in sections_in_order:
        idx = md.find(section)
        assert idx >= 0, f"missing section: {section}"
        assert idx > last_index
        last_index = idx


def test_renders_minimal_audit_without_v2_fields():
    """Older audits pre-Track A/B render cleanly with optional sections omitted."""
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    minimal = {
        "merchant_name": "X",
        "merchant_domain": "x.com",
        "per_product": [{
            "verdict": {"label": "INVISIBLE", "visibility_score": 0,
                        "attribution_score": 0,
                        "category_visibility_score": None},
            "industry_context": {"category": "default", "blurb": "..."},
            "merchant_view": {"receipts": {}, "actions": []},
            "upstream_status": {"is_real": True, "requested_provider": "gemini",
                                "visibility_provider": "gemini",
                                "attribution_provider": "gemini"},
        }],
    }
    md = render_brand_markdown_v2(minimal)
    assert "AI Commerce Readiness Audit" in md
    assert "## Headline Metrics" in md
    assert "## Methodology" in md
    assert "## Executive Summary" not in md
    assert "## Evidence Quotes" not in md
    assert "## Implementation Roadmap" not in md


def test_renders_empty_per_product_safely():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2({
        "merchant_name": "X", "merchant_domain": "x.com", "per_product": [],
    })
    assert "AI Commerce Readiness Audit" in md


def test_select_renderer_default_returns_v1(monkeypatch):
    from services.audit_markdown_renderer_v2 import select_renderer
    monkeypatch.delenv("AUDIT_RENDERER_VERSION", raising=False)
    renderer = select_renderer()
    assert renderer.__name__ == "render_brand_markdown"


def test_select_renderer_v2_when_env_set(monkeypatch):
    from services.audit_markdown_renderer_v2 import (
        render_brand_markdown_v2, select_renderer,
    )
    monkeypatch.setenv("AUDIT_RENDERER_VERSION", "v2")
    renderer = select_renderer()
    assert renderer is render_brand_markdown_v2


def test_select_renderer_unknown_value_falls_back_to_v1(monkeypatch):
    from services.audit_markdown_renderer_v2 import select_renderer
    monkeypatch.setenv("AUDIT_RENDERER_VERSION", "v3_does_not_exist")
    renderer = select_renderer()
    assert renderer.__name__ == "render_brand_markdown"


def test_full_render_substantive_output():
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2
    md = render_brand_markdown_v2(_gruns_fixture_audit())
    assert len(md) > 2000
    assert md.count("Grüns") >= 3
    assert "forbes.com" in md
    assert "Unilever" in md
    assert "gummy" in md
    assert "Search Console" in md
