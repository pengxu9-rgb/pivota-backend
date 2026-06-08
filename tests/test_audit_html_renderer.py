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
                    "headline": (
                        "Win non-branded collagen discovery before optimizing "
                        "more named-product visibility."
                    ),
                    "why_this_first": (
                        "AI can find Grüns when named, but category shoppers are "
                        "still learning from Forbes and competitor-heavy roundups."
                    ),
                    "first_move": (
                        "Add category-intent comparison modules, then pitch the "
                        "cited sources that already shape AI answers."
                    ),
                    "evidence_summary": (
                        "Named visibility is 88 vs category visibility 52: "
                        "a 36-point category gap."
                    ),
                    "evidence_chips": [
                        "Visibility 88",
                        "Attribution 70",
                        "Category visibility 52",
                    ],
                    "self_serve_actions": [
                        "Answer the failed category questions on the official PDP.",
                        "Pitch Forbes with the specific competitor comparison angle.",
                    ],
                    "pivota_path": (
                        "Pivota can turn the official PDP into a canonical, "
                        "buyable AI-channel path and monitor whether attribution moves."
                    ),
                    "evidence_used": {
                        "failed_query_examples": [
                            {"query": "best daily greens supplements"},
                        ],
                        "source_hosts": [
                            {"host": "forbes.com", "times_cited": 2},
                        ],
                        "competitors_named": ["AG1", "Bloom"],
                    },
                    "secondary_moves": [
                        {
                            "title": "Pitch forbes.com editorial team",
                            "reason": "forbes.com was cited 2 times.",
                        }
                    ],
                    "tracking_metrics": [
                        "Category visibility on the failed non-branded questions.",
                        "Competitor-only answers where the merchant is still absent.",
                    ],
                    "cta": {
                        "label": "Create the owned AI buying path",
                        "trust_note": (
                            "You can do the content and PR work yourself; Pivota "
                            "is for canonical serving, checkout, and monitoring."
                        ),
                    },
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
                                "AI grounding is filling a canonical-source gap with weak "
                                "third-party hosts; claim the official source before "
                                "optimizing against those hosts."
                            ),
                            "exposure_read": (
                                "Read this as a weak citation trail and canonical-source "
                                "vacuum, not proof that material buyer traffic is going to those hosts."
                            ),
                        },
                        "exposure_read": (
                            "Read this as a weak citation trail and canonical-source "
                            "vacuum, not proof that material buyer traffic is going to those hosts."
                        ),
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
                                    "AI can cite for vitamin c collagen jelly."
                                ),
                                "why": "AI grounding is leaning on weak third-party hosts.",
                            },
                            {
                                "type": "direct_buy_reason",
                                "operator_action": (
                                    "After the official page is source-ready, add first-order offer, starter + replenishment "
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


def test_html_renderer_emits_next_best_action_before_recommendations():
    from services.audit_html_renderer import render_brand_html_v2
    html = render_brand_html_v2(_gruns_fixture_audit())
    assert "What Should You Do Next?" in html
    assert "Win non-branded collagen discovery" in html
    assert "First move:" in html
    assert "Why this is the leak" in html
    assert "36-point category gap" in html
    assert "Gap read" in html
    assert "Category visibility 52" in html
    assert "Do yourself this week" in html
    assert "Use Pivota for" in html
    assert "best daily greens supplements" in html
    assert "forbes.com" in html
    assert "AG1" in html
    assert "How to track" in html
    assert "Category visibility on the failed non-branded questions" in html
    assert "Create the owned AI buying path" in html
    assert html.find("What Should You Do Next?") < html.find("Recommendations")


def test_html_renderer_emits_deliverability_before_next_best_action_and_escapes():
    from services.audit_html_renderer import render_brand_html_v2

    audit = _gruns_fixture_audit()
    audit["brand_rollup"] = {
        "deliverability": {
            "status_counts": {
                "transactable": 1,
                "servable_not_transactable": 1,
            }
        }
    }
    audit["per_sku_reports"] = [
        {
            "sku_key": "sku-ready",
            "sku_title": "Ready <Serum>",
            "checkout_handoff": {
                "status": "eligible",
                "label": "Open <buyable> Pivota product page",
                "handoff_url": 'https://agent.pivota.cc/checkout/handoff?token="t"',
            },
            "deliverability": {
                "status": "transactable",
                "summary": "This SKU is serving eligible and has a ready merchant-checkout path.",
                "serving": {"status": "ready"},
                "checkout": {"status": "ready"},
            },
        },
        {
            "sku_key": "sku-stock",
            "sku_title": "Unknown Stock Serum",
            "deliverability": {
                "status": "servable_not_transactable",
                "summary": "Needs explicit availability <script>alert(1)</script>",
                "serving": {"status": "ready"},
                "checkout": {"status": "blocked"},
            },
        },
    ]

    html = render_brand_html_v2(audit)

    assert "Servability and Checkout" in html
    assert "1 of 2 audited SKUs is confirmed transactable." in html
    assert "explicit available-stock signal" in html
    assert "Ready &lt;Serum&gt;" in html
    assert 'href="https://agent.pivota.cc/checkout/handoff?token=&quot;t&quot;"' in html
    assert "Open &lt;buyable&gt; Pivota product page" in html
    assert "Needs explicit availability &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.find("Servability and Checkout") < html.find("What Should You Do Next?")


def test_html_renderer_emits_reaudit_delta_before_next_best_action_and_escapes():
    from services.audit_html_renderer import render_brand_html_v2
    audit = _gruns_fixture_audit()
    audit["per_product"][0]["merchant_view"]["reaudit_delta"] = {
        "is_first_audit": False,
        "headline": "No material change <script>alert(1)</script>",
        "movements": [
            {
                "signal": "attribution",
                "label": "First-party <citation>",
                "from": 40,
                "to": 58,
                "direction": "improved",
                "is_material": True,
            }
        ],
        "tracked_metric_results": [
            {
                "metric": "First-party citation <rate>",
                "status": "moved",
                "note": "Mapped to attribution <ok>.",
            }
        ],
    }

    html = render_brand_html_v2(audit)

    assert "Since your last audit" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "First-party &lt;citation&gt;" in html
    assert "First-party citation &lt;rate&gt;" in html
    assert "summary-box no-break" in html
    assert html.find("Since your last audit") < html.find("What Should You Do Next?")


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
    assert "official source AI can cite" in html
    assert "Exposure read" in html
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
