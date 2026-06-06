"""Tests for the HTML renderer + PDF conversion pipeline (PR-9b)."""

from __future__ import annotations


def _gruns_fixture_audit():
    """Same fixture as the markdown renderer tests — sister implementation."""
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
                "headline_finding": "Grüns is editorially visible.",
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
                    "powder": ["AG1", "Bloom"],
                },
            },
            "category_visibility": {
                "score": 100,
                "competitor_brands": [
                    {"name": "AG1", "times_cited": 1},
                    {"name": "Bloom", "times_cited": 2},
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
                    ],
                },
                "actions": [
                    {
                        "priority_order": 1, "severity": "critical",
                        "title": "Index your canonical PDPs with Google Search Console",
                        "body": "Submit sitemap.xml.",
                        "owner": "pivota_ops", "phase": "week_1_to_4",
                        "kpi_to_track": "Number of canonical PDPs indexed",
                        "expected_outcome": "First grounded citations within 30-60 days.",
                        "concrete_next_step": "Submit sitemap this week.",
                    },
                ],
                "next_best_action": {
                    "canonical_page_play": {
                        "lane": "vitamin c collagen jelly",
                        "controllers": [
                            "cogentsteps.net",
                            "medsysgroup.com",
                            "hellokoop.com",
                        ],
                        "controller_strategy": "canonical_source_vacuum",
                        "controller_strategy_label": "Canonical-source vacuum",
                        "controller_profile": {
                            "operator_focus": (
                                "AI is filling a canonical-source gap with weak "
                                "third-party hosts; claim the official source before "
                                "optimizing against those hosts."
                            ),
                        },
                        "economics_policy": (
                            "Mechanics only: first-order offer, starter + "
                            "replenishment bundle, subscription incentive, and "
                            "why-buy-direct proof. Do not recommend exact discount depths."
                        ),
                        "moves": [
                            {
                                "type": "canonical_source_authority",
                                "operator_action": (
                                    "Make the official brand PDP the official source "
                                    "of truth for vitamin c collagen jelly."
                                ),
                                "why": "AI is filling the lane with weak third-party hosts.",
                            },
                            {
                                "type": "direct_buy_reason",
                                "operator_action": (
                                    "Add first-order offer, starter + replenishment "
                                    "bundle, subscription incentive, and why-buy-direct proof."
                                ),
                            },
                        ],
                        "checkout_readiness": (
                            "Make the official brand PDP cited, buyable, and agent-checkout ready."
                        ),
                    },
                    "sideways_wedge": {
                        "recommended_beachhead_lane": {
                            "query": "vitamin c collagen jelly",
                        },
                        "why_this_lane_not_the_head_prompt": (
                            "Start with \"vitamin c collagen jelly\" before "
                            "\"healthy snacks collagen jelly\" because it is "
                            "product-specific, commercially useful, and easier "
                            "to make the official page the best cited + buyable route."
                        ),
                        "do_not_chase_yet": [
                            {"query": "healthy snacks collagen jelly"},
                            {"query": "best collagen supplements"},
                        ],
                    },
                },
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
                ],
                "total_weeks": 4, "total_activities": 1,
            },
            "pivota_commitments": {
                "merchant_platform": None,
                "platform_capability_summary": "Multi-platform support.",
                "delivers_weeks_1_to_4": ["Canonical PDPs created"],
                "delivers_continuous": ["Weekly Search Console cadence"],
                "does_not_promise": ["Layer 1 lift on guaranteed timeline"],
            },
            "upstream_status": {
                "is_real": True, "requested_provider": "gemini",
                "visibility_provider": "gemini", "attribution_provider": "gemini",
            },
        }],
    }


# ---------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------


def test_html_renderer_returns_valid_html_doctype():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert html.startswith("<!DOCTYPE html>")
    assert "<html" in html
    assert "</html>" in html
    assert "<head>" in html
    assert "<body>" in html


def test_html_renderer_includes_print_styles():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "@page" in html
    assert "letter" in html  # paper size
    assert "page-break-after" in html


def test_html_renderer_emits_executive_summary():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Executive Summary" in html
    assert "Unilever-owned brand" in html
    assert "verdict-pill" in html


def test_html_renderer_emits_headline_metrics_table():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Headline Metrics" in html
    assert "100/100" in html
    assert "<table>" in html


def test_html_renderer_emits_strategic_context():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Strategic Context" in html
    assert "$6500B" in html
    assert "wellness gummies" in html


def test_html_renderer_emits_evidence_quotes_as_blockquotes():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Evidence Quotes" in html
    assert "Best Green Gummies: Grüns Superfoods Greens Gummies" in html
    assert "<blockquote>" in html
    assert "forbes.com" in html


def test_html_renderer_emits_competitive_analysis_with_moat_callout():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Competitive Analysis" in html
    assert "summary-box" in html  # moat callout uses summary-box
    assert "only one in the cohort" in html


def test_html_renderer_emits_publisher_table():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Editorial Publisher Analysis" in html
    assert "Tier 1" in html
    assert "Quarterly" in html
    assert "vetted@forbes.com" in html


def test_html_renderer_emits_recommendations_with_pitch_drafts():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Recommendations" in html
    assert "Pivota Ops" in html
    assert "Search Console" in html


def test_html_renderer_emits_owned_buyer_path_play_with_controller_strategy():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Owned Buyer Path Play" in html
    assert "Canonical-source vacuum" in html
    assert "vitamin c collagen jelly" in html
    assert "cogentsteps.net" in html
    assert "Sideways demand wedge" in html
    assert "Beachhead lane" in html
    assert "healthy snacks collagen jelly" in html
    assert "Do not chase yet" in html
    assert "official source of truth" in html
    assert "Economics guard" in html
    assert "beat cogentsteps" not in html.lower()


def test_html_renderer_emits_implementation_roadmap():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Implementation Roadmap" in html
    assert "Phase 1: Foundation" in html


def test_html_renderer_emits_commitments_section():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "Pivota's Commitment" in html
    assert "What Pivota does not promise" in html
    assert "Layer 1 lift on guaranteed timeline" in html


def test_html_renderer_escapes_user_content_against_xss():
    """Brand names / publisher names go through html.escape — XSS-safe."""
    from services.audit_html_renderer import render_brand_html_v2
    audit = _gruns_fixture_audit()
    audit["merchant_name"] = "<script>alert('xss')</script>"
    html = render_brand_html_v2(audit)
    # Raw script tag must NOT appear in rendered output
    assert "<script>alert" not in html
    # Should be escaped
    assert "&lt;script&gt;" in html


def test_html_renderer_handles_minimal_audit():
    """Older audits without v2 fields render cleanly."""
    from services.audit_html_renderer import render_brand_html_v2
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
    html = render_brand_html_v2(minimal)
    assert "<!DOCTYPE html>" in html
    assert "Headline Metrics" in html
    # Optional sections cleanly omitted
    assert "Executive Summary" not in html
    assert "Evidence Quotes" not in html


def test_html_renderer_handles_empty_per_product():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2({
        "merchant_name": "X",
        "merchant_domain": "x.com",
        "per_product": [],
    })
    assert "<!DOCTYPE html>" in html


# ---------------------------------------------------------------------
# PDF conversion
# ---------------------------------------------------------------------


def test_html_to_pdf_returns_none_when_weasyprint_not_installed(monkeypatch):
    """When weasyprint isn't available, returns None so caller can
    fall back to HTML download. Doesn't crash."""
    from services import audit_html_renderer

    # Force the import to fail
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "weasyprint":
            raise ImportError("simulated missing weasyprint")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = audit_html_renderer.html_to_pdf_bytes("<html>x</html>")
    assert result is None


# ---------------------------------------------------------------------
# Sister-renderer parity (HTML & markdown surface same content fields)
# ---------------------------------------------------------------------


def test_html_and_markdown_renderers_surface_same_content():
    """Both renderers should produce output containing the same key
    facts (brand name, scores, evidence quotes, recommendations).
    Different formatting, same payload coverage."""
    from services.audit_html_renderer import render_brand_html_v2
    from services.audit_markdown_renderer_v2 import render_brand_markdown_v2

    audit = _gruns_fixture_audit()
    html = render_brand_html_v2(audit)
    md = render_brand_markdown_v2(audit)

    # Same key facts in both
    for fact in (
        "Grüns",
        "Unilever",
        "Best Green Gummies",
        "100/100",
        "Search Console",
        "Pivota Ops",
        "vetted@forbes.com",
    ):
        assert fact in html, f"missing fact in HTML: {fact}"
        assert fact in md, f"missing fact in markdown: {fact}"


def test_html_renderer_substantive_output():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert len(html) > 4000  # HTML overhead vs markdown
    assert html.count("Grüns") >= 3


def test_html_renderer_surfaces_combined_buyer_path_verdict():
    from services.audit_html_renderer import render_brand_html_v2

    audit = _gruns_fixture_audit()
    primary = audit["per_product"][0]
    primary["executive_summary"]["verdict_pill_text"] = (
        "Strong AI visibility, weak owned buyer path"
    )
    primary["verdict"]["label"] = "STRONG"
    primary["verdict"]["label_display"] = "Strong AI visibility, weak owned buyer path"
    primary["verdict"]["explanation"] = (
        "AI answer visibility is strong, but 0/2 evidenced prompt lanes are merchant-owned."
    )

    html = render_brand_html_v2(audit)

    assert "Strong AI visibility, weak owned buyer path" in html
    assert "0/2 evidenced prompt lanes are merchant-owned" in html
