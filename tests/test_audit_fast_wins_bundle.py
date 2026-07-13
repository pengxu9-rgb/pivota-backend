"""Tests for the audit fast-wins bundle:

  PR-7e — evidence_quotes surfaced as first-class field
  PR-11 — pivota_baseline_reference populated for cold-start with
          pitch_framing context
  PR-10a — platform_coverage block reflects shipped Woo + BC adapters
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# PR-7e — evidence_quotes
# ---------------------------------------------------------------------


def test_evidence_quotes_extracts_only_corroborated_runs():
    """Only runs where the brand was named in the excerpt AND
    corroborated by LLM self-report + grounding source surface as
    quotes. Excerpt-only paraphrases (no_name 1688 hallucination
    case) are filtered out."""
    from services.agent_center_bd_report_service import _build_evidence_quotes

    details = [
        # Real editorial citation — should surface
        {
            "query": "best daily greens supplements",
            "matched": True,
            "excerpt_corroborated_match": True,
            "evidence_excerpt_text": "Best Green Gummies: Grüns Superfoods Greens Gummies.",
            "source_labels": ["forbes.com"],
        },
        # Excerpt-only paraphrase — should NOT surface
        {
            "query": "best green powders 2026",
            "matched": False,
            "excerpt_corroborated_match": False,
            "evidence_excerpt_text": "Chydan offers a range of satin sets...",
            "source_labels": ["nymag.com"],
        },
        # Upstream-failed run — no excerpt at all
        {
            "query": "best greens 2026",
            "matched": False,
            "excerpt_corroborated_match": False,
            "evidence_excerpt_text": None,
            "source_labels": [],
            "upstream_failed": True,
        },
    ]
    quotes = _build_evidence_quotes(details)
    assert len(quotes) == 1
    assert quotes[0]["query"] == "best daily greens supplements"
    assert "Grüns Superfoods Greens Gummies" in quotes[0]["excerpt_text"]
    assert quotes[0]["source_labels"] == ["forbes.com"]
    assert quotes[0]["attribution_path"] == "merchant_named_in_grounded_excerpt"


def test_evidence_quotes_truncates_long_excerpts():
    """Excerpts over 500 chars get truncated with ellipsis."""
    from services.agent_center_bd_report_service import _build_evidence_quotes

    long_excerpt = "Grüns is excellent. " + ("blah " * 200)
    details = [{
        "query": "best greens",
        "matched": True,
        "excerpt_corroborated_match": True,
        "evidence_excerpt_text": long_excerpt,
        "source_labels": ["forbes.com"],
    }]
    quotes = _build_evidence_quotes(details)
    assert len(quotes) == 1
    assert len(quotes[0]["excerpt_text"]) <= 500
    assert quotes[0]["excerpt_text"].endswith("...")


def test_evidence_quotes_returns_empty_list_when_no_details():
    from services.agent_center_bd_report_service import _build_evidence_quotes
    assert _build_evidence_quotes(None) == []
    assert _build_evidence_quotes([]) == []


def test_score_category_visibility_now_returns_excerpt_text_in_details():
    """The scorer now preserves the verbatim excerpt + source labels
    in match_details so _build_evidence_quotes has data to filter."""
    from services.agent_center_bd_report_service import score_category_visibility

    runs = [
        {
            "query": "best daily greens supplements",
            "raw": "{}",
            "url_match": {
                "in_grounding": False,
                "llm_self_report": True,
            },
            "parsed": {
                "evidence_excerpt": "Best Green Gummies: Grüns Superfoods Greens Gummies.",
            },
            "grounding_chunks": ["https://vertexaisearch.cloud.google.com/grounding-api-redirect/q1"],
            "grounding_sources": [
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/q1",
                    "title": "forbes.com",
                },
            ],
        },
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    assert score == 100
    assert details[0]["matched"] is True
    assert details[0]["excerpt_corroborated_match"] is True
    # PR-7e additions
    assert details[0]["evidence_excerpt_text"] == (
        "Best Green Gummies: Grüns Superfoods Greens Gummies."
    )
    assert "forbes.com" in details[0]["source_labels"]


def test_score_category_visibility_drops_excerpt_text_when_brand_not_in_excerpt():
    """When excerpt_match is False (brand not in excerpt at all),
    we don't surface the excerpt text — keeps payload size bounded
    and avoids leaking unrelated quotes."""
    from services.agent_center_bd_report_service import score_category_visibility

    runs = [
        {
            "query": "best greens",
            "raw": "{}",
            "url_match": {"in_grounding": False, "llm_self_report": False},
            "parsed": {
                "evidence_excerpt": "There are many options in this category.",
            },
            "grounding_chunks": [],
            "grounding_sources": [],
        },
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    assert score == 0
    assert details[0]["evidence_excerpt_text"] is None


# ---------------------------------------------------------------------
# PR-11 — pivota_baseline_reference for cold-start with pitch framing
# ---------------------------------------------------------------------


def test_baseline_reference_populated_for_cold_start_with_pitch_framing():
    """Cold-start audits used to null pivota_baseline_reference; now
    it's populated with a pitch_framing block that the renderer can
    use to present the indexing-up baseline as forward-looking."""
    from services.agent_center_bd_report_service import _build_tracking_block

    cold_start_integration = {
        "fully_integrated": False,
        "missing_pieces": ["store_platform", "psp"],
    }
    block = _build_tracking_block(
        prior_runs=None,
        integration_state=cold_start_integration,
        pivota_baseline={
            "median_visibility": 0,
            "median_attribution": 0,
            "as_of_date": "2026-05-06",
            "indexing_phase": "indexing-up",
            "sample_size_pdps": 5,
        },
        your_gap_to_baseline={"visibility": 0, "attribution": 0},
    )
    # Cold-start: gap is null (math has no meaning pre-onboarding)
    assert block["your_gap_to_baseline"] is None
    # Cold-start: baseline IS populated with pitch_framing
    baseline = block["pivota_baseline_reference"]
    assert baseline is not None
    assert baseline["visibility"] == 0
    assert baseline["attribution"] == 0
    assert baseline["indexing_phase"] == "indexing-up"
    assert "pitch_framing" in baseline
    pf = baseline["pitch_framing"]
    assert "30-90 day" in pf["headline"]
    assert "indexing-up phase" in pf["honest_caveat"]
    # Forward-looking framing for cold-start reads as pitch material
    expected_pf = pf["what_to_expect_post_onboarding"]
    assert "30-90 days" in expected_pf
    assert "indexing arc" in expected_pf


def test_baseline_reference_for_onboarded_merchant_steady_state():
    """Onboarded merchant in steady-state phase: gap to baseline is
    populated, pitch_framing uses comparable-benchmark framing."""
    from services.agent_center_bd_report_service import _build_tracking_block

    onboarded_integration = {
        "fully_integrated": True,
        "store_platform_name": "shopify",
        "psp_provider": "stripe",
    }
    block = _build_tracking_block(
        prior_runs=None,
        integration_state=onboarded_integration,
        pivota_baseline={
            "median_visibility": 45,
            "median_attribution": 30,
            "as_of_date": "2026-05-06",
            "indexing_phase": "steady-state",
            "sample_size_pdps": 12,
        },
        your_gap_to_baseline={"visibility": -10, "attribution": -5},
    )
    # Onboarded: gap populated
    assert block["your_gap_to_baseline"] == {"visibility": -10, "attribution": -5}
    # Onboarded steady-state: baseline shows comparable benchmark framing
    baseline = block["pivota_baseline_reference"]
    assert baseline["pitch_framing"]["headline"].startswith(
        "Pivota canonical PDPs"
    )
    assert "steady state" in baseline["pitch_framing"]["headline"]


def test_baseline_pitch_framing_function_directly():
    """The _baseline_pitch_framing helper handles both phases +
    cold-start vs onboarded variants."""
    from services.agent_center_bd_report_service import _baseline_pitch_framing

    cold = _baseline_pitch_framing(
        indexing_phase="indexing-up", cold_start=True,
        baseline_visibility=0, baseline_attribution=0,
    )
    assert "30-90 day" in cold["headline"]
    assert "0/0" in cold["honest_caveat"]

    onboarded_indexing = _baseline_pitch_framing(
        indexing_phase="indexing-up", cold_start=False,
        baseline_visibility=12, baseline_attribution=8,
    )
    assert "30-90 day" in onboarded_indexing["headline"]
    assert "12/8" in onboarded_indexing["honest_caveat"]

    steady = _baseline_pitch_framing(
        indexing_phase="steady-state", cold_start=False,
        baseline_visibility=45, baseline_attribution=30,
    )
    assert "45/30" in steady["headline"]
    assert "steady state" in steady["headline"]


# ---------------------------------------------------------------------
# PR-10a — platform_coverage reflects shipped Woo + BC adapters
# ---------------------------------------------------------------------


def test_platform_coverage_lists_shopify_woo_bc_as_shipped():
    """checkout_loop.platform_coverage now reflects actual code state:
    Shopify, WooCommerce, BigCommerce are all shipped end-to-end
    (per routes/order_routes.py:create_woocommerce_order +
    create_bigcommerce_order + _create_shopify_order_impl, dispatched
    by sync_order_to_connected_store). Wix is audit-only. Custom
    storefronts via lightweight integration."""
    from services.agent_center_bd_report_service import _build_what_pivota_changes

    wpc = _build_what_pivota_changes(
        merchant_name="Grüns",
        merchant_pdp_url="https://gruns.co/products/greens-gummies",
        attribution_score=0,
        attribution_runs=3,
        merchant_cited_runs=0,
        category_retailer_hosts=[{"host": "forbes.com", "times_cited": 2}],
        category_visibility_score=100,
    )
    pc = wpc["checkout_loop"]["platform_coverage"]
    assert "Shopify" in pc["shipped"]
    assert "WooCommerce" in pc["shipped"]
    assert "BigCommerce" in pc["shipped"]
    # Wix is audit-only
    assert "Wix" in pc.get("audit_only", [])
    # Custom integration tier is described
    assert pc.get("custom_integration"), "custom_integration field expected"
    # Note copy reflects multi-platform stance
    assert "Multi-platform" in pc["note"]
    assert "WooCommerce" in pc["note"]
    # No longer claims roadmap for Woo + BC (they're shipped)
    assert "WooCommerce" not in (pc.get("roadmap") or [])
    assert "BigCommerce" not in (pc.get("roadmap") or [])


# ---------------------------------------------------------------------
# Honest model naming in discovery_lift (Layer-1 methodology copy names
# the providers that actually ran, not a hardcoded "Gemini" — parity
# with the URL-audit surface's _shape_url_audit_response).
# ---------------------------------------------------------------------


def _discovery_lift_for_providers(providers):
    from services.agent_center_bd_report_service import _build_what_pivota_changes

    wpc = _build_what_pivota_changes(
        merchant_name="Grüns",
        merchant_pdp_url="https://gruns.co/products/greens-gummies",
        attribution_score=0,
        attribution_runs=3,
        merchant_cited_runs=0,
        category_retailer_hosts=[{"host": "forbes.com", "times_cited": 2}],
        category_visibility_score=100,
        providers=providers,
    )
    return wpc["discovery_lift"]


def test_methodology_note_names_multiple_providers_when_they_ran():
    """Gemini + ChatGPT run → methodology_note + Layer-1 subtitle name BOTH,
    and ChatGPT is not also parked in the 'as those engines mature' roadmap."""
    dl = _discovery_lift_for_providers(["gemini", "chatgpt"])

    note = dl["methodology_note"]
    assert "grounded LLM citation via Gemini and ChatGPT" in note

    layer1 = dl["layers"][0]
    assert layer1["name"].startswith("Layer 1")
    subtitle = layer1["subtitle"]
    assert subtitle.startswith("Gemini and ChatGPT today")
    # ChatGPT already ran — must not be re-listed as a maturing engine.
    assert "ChatGPT search" not in subtitle
    # Non-run roadmap engines still surface.
    assert "Perplexity" in subtitle and "Claude" in subtitle


def test_methodology_note_gemini_only_run():
    """Single-provider (Gemini-only) run still reads 'via Gemini' and keeps
    ChatGPT/Perplexity/Claude on the maturing roadmap."""
    dl = _discovery_lift_for_providers(["gemini"])

    assert "grounded LLM citation via Gemini)" in dl["methodology_note"]
    subtitle = dl["layers"][0]["subtitle"]
    assert subtitle.startswith("Gemini today")
    assert "ChatGPT search" in subtitle


def test_methodology_note_falls_back_to_gemini_when_no_providers():
    """Legacy callers that don't thread providers get the prior 'Gemini'
    wording rather than an empty label."""
    dl = _discovery_lift_for_providers(None)

    assert "grounded LLM citation via Gemini)" in dl["methodology_note"]
    assert dl["layers"][0]["subtitle"].startswith("Gemini today")
