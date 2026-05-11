"""Tests for the executive summary narrative builder (PR-8a).

Coverage:
  - Archetype detection across the 4 score profiles
  - Each archetype's narrative composition (paragraphs reference real
    numbers + brand name + cited publishers + evidence quotes)
  - Defensive handling of missing/empty fields
  - Builder error never blocks downstream — surfaces minimal-shape
    fallback
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# Archetype detection
# ---------------------------------------------------------------------


def test_detects_editorial_strong_attribution_weak_archetype():
    """The Grüns case: low attribution + high category visibility +
    at least one corroborated evidence quote."""
    from services.audit_narrative_builder import (
        detect_narrative_archetype,
        ARCH_EDITORIAL_STRONG_ATTR_WEAK,
    )
    archetype = detect_narrative_archetype(
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[
            {"excerpt_text": "Best Green Gummies: Grüns.", "source_labels": ["forbes.com"]},
        ],
    )
    assert archetype == ARCH_EDITORIAL_STRONG_ATTR_WEAK


def test_does_not_detect_editorial_when_no_evidence_quotes():
    """Without corroborating evidence quotes, low attribution + high
    category falls into mixed_or_partial, NOT the editorial archetype.
    Hallucination defense from PR-433 carried through to narrative."""
    from services.audit_narrative_builder import (
        detect_narrative_archetype,
        ARCH_EDITORIAL_STRONG_ATTR_WEAK,
        ARCH_MIXED_OR_PARTIAL,
    )
    archetype = detect_narrative_archetype(
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[],  # no quotes
    )
    assert archetype != ARCH_EDITORIAL_STRONG_ATTR_WEAK
    assert archetype == ARCH_MIXED_OR_PARTIAL


def test_detects_strong_everywhere_archetype():
    from services.audit_narrative_builder import (
        detect_narrative_archetype, ARCH_STRONG_EVERYWHERE,
    )
    archetype = detect_narrative_archetype(
        visibility_score=80, attribution_score=70,
        category_visibility_score=85, evidence_quotes=[],
    )
    assert archetype == ARCH_STRONG_EVERYWHERE


def test_detects_strong_everywhere_when_category_unmeasured():
    """category_visibility_score=None (test wasn't run) is OK if
    visibility + attribution are both high."""
    from services.audit_narrative_builder import (
        detect_narrative_archetype, ARCH_STRONG_EVERYWHERE,
    )
    archetype = detect_narrative_archetype(
        visibility_score=70, attribution_score=65,
        category_visibility_score=None, evidence_quotes=[],
    )
    assert archetype == ARCH_STRONG_EVERYWHERE


def test_detects_fully_invisible_archetype():
    from services.audit_narrative_builder import (
        detect_narrative_archetype, ARCH_FULLY_INVISIBLE,
    )
    archetype = detect_narrative_archetype(
        visibility_score=0, attribution_score=0,
        category_visibility_score=10, evidence_quotes=[],
    )
    assert archetype == ARCH_FULLY_INVISIBLE


def test_does_not_detect_fully_invisible_when_evidence_quote_present():
    """Even with low scores, if there's a corroborated evidence quote,
    the brand isn't fully invisible — falls into editorial archetype."""
    from services.audit_narrative_builder import (
        detect_narrative_archetype,
        ARCH_FULLY_INVISIBLE,
        ARCH_EDITORIAL_STRONG_ATTR_WEAK,
    )
    archetype = detect_narrative_archetype(
        visibility_score=0, attribution_score=0,
        category_visibility_score=50,  # at editorial threshold
        evidence_quotes=[
            {"excerpt_text": "X", "source_labels": ["forbes.com"]},
        ],
    )
    assert archetype != ARCH_FULLY_INVISIBLE
    assert archetype == ARCH_EDITORIAL_STRONG_ATTR_WEAK


def test_falls_back_to_mixed_or_partial():
    """Mid-range scores not matching any archetype boundary → mixed."""
    from services.audit_narrative_builder import (
        detect_narrative_archetype, ARCH_MIXED_OR_PARTIAL,
    )
    archetype = detect_narrative_archetype(
        visibility_score=40, attribution_score=35,
        category_visibility_score=45, evidence_quotes=[],
    )
    assert archetype == ARCH_MIXED_OR_PARTIAL


# ---------------------------------------------------------------------
# Narrative composition: editorial-strong-attribution-weak
# ---------------------------------------------------------------------


def test_editorial_archetype_paragraphs_reference_real_data():
    """The paradox-framing narrative weaves in actual brand name,
    publishers, and at least one evidence quote — not boilerplate."""
    from services.audit_narrative_builder import (
        build_executive_summary, ARCH_EDITORIAL_STRONG_ATTR_WEAK,
    )
    result = build_executive_summary(
        merchant_name="Grüns",
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[
            {
                "excerpt_text": "Best Green Gummies: Grüns Superfoods Greens Gummies.",
                "source_labels": ["forbes.com"],
            },
        ],
        cited_publishers=["forbes.com", "trailandkale.com", "womenshealthmag.com"],
        competitor_brands=[
            {"name": "AG1", "times_cited": 2},
            {"name": "Bloom", "times_cited": 1},
        ],
        industry_blurb="AI shopping is ~11% of D2C wellness traffic.",
        industry_share_pct=11,
        verdict_pill_text="Visible via retailers + editorial",
    )
    assert result["narrative_archetype"] == ARCH_EDITORIAL_STRONG_ATTR_WEAK
    paragraphs = result["opening_paragraphs"]
    full_text = "\n\n".join(paragraphs).lower()
    # All real data points should appear in the narrative
    assert "grüns" in full_text
    assert "forbes.com" in full_text
    assert "best green gummies: grüns superfoods greens gummies" in full_text.lower()
    # Paradox framing: brand IS visible BUT not first-party attributed
    assert "visible" in full_text or "visibility" in full_text
    assert "first-party" in full_text or "attribution" in full_text
    # Strategic implication is the closeable-gap pitch
    assert "30-90" in result["strategic_implication"]


def test_editorial_archetype_uses_only_first_evidence_quote_in_paragraph():
    """When multiple quotes exist, only ONE goes into the opening
    paragraph (rest still surface in the dedicated evidence_quotes
    section). Prevents the executive summary from becoming a quote
    dump."""
    from services.audit_narrative_builder import build_executive_summary
    result = build_executive_summary(
        merchant_name="X",
        visibility_score=0, attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[
            {"excerpt_text": "First quote about X.", "source_labels": ["a.com"]},
            {"excerpt_text": "Second quote about X.", "source_labels": ["b.com"]},
        ],
        cited_publishers=["a.com"],
        competitor_brands=[],
        industry_blurb="",
        industry_share_pct=None,
        verdict_pill_text="",
    )
    full_text = "\n\n".join(result["opening_paragraphs"])
    assert "First quote" in full_text
    assert "Second quote" not in full_text  # only first surfaced
    assert result["evidence_quotes_used"] == 1


def test_editorial_archetype_truncates_long_evidence_quotes_inline():
    """Excerpts >180 chars get truncated in the inline paragraph. Full
    quote still available in the dedicated evidence_quotes payload
    section, but the executive summary stays readable."""
    from services.audit_narrative_builder import build_executive_summary
    long_excerpt = "Grüns " + ("blah " * 80)
    result = build_executive_summary(
        merchant_name="X",
        visibility_score=0, attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[
            {"excerpt_text": long_excerpt, "source_labels": ["a.com"]},
        ],
        cited_publishers=["a.com"],
        competitor_brands=[],
        industry_blurb="",
        industry_share_pct=None,
        verdict_pill_text="",
    )
    full_text = "\n\n".join(result["opening_paragraphs"])
    # Should be truncated with ellipsis
    assert "..." in full_text


# ---------------------------------------------------------------------
# Narrative composition: fully invisible
# ---------------------------------------------------------------------


def test_fully_invisible_narrative_acknowledges_absence():
    from services.audit_narrative_builder import (
        build_executive_summary, ARCH_FULLY_INVISIBLE,
    )
    result = build_executive_summary(
        merchant_name="UnknownBrand",
        visibility_score=0, attribution_score=0,
        category_visibility_score=10,
        evidence_quotes=[],
        cited_publishers=["forbes.com"],
        competitor_brands=[
            {"name": "AG1", "times_cited": 2},
        ],
        industry_blurb="", industry_share_pct=11,
        verdict_pill_text="Invisible in grounded LLM citations",
    )
    assert result["narrative_archetype"] == ARCH_FULLY_INVISIBLE
    full_text = "\n\n".join(result["opening_paragraphs"])
    assert "UnknownBrand" in full_text
    assert ("does not currently surface" in full_text
            or "absent" in full_text)
    assert result["evidence_quotes_used"] == 0
    # Strategic implication frames as ground-up build
    assert "ground-up" in result["strategic_implication"].lower() \
        or "infrastructure" in result["strategic_implication"].lower()


# ---------------------------------------------------------------------
# Narrative composition: strong everywhere
# ---------------------------------------------------------------------


def test_strong_everywhere_narrative_frames_as_goal_state():
    from services.audit_narrative_builder import (
        build_executive_summary, ARCH_STRONG_EVERYWHERE,
    )
    result = build_executive_summary(
        merchant_name="WellPositionedBrand",
        visibility_score=80, attribution_score=75,
        category_visibility_score=85,
        evidence_quotes=[],
        cited_publishers=["wellpositioned.com"],
        competitor_brands=[],
        industry_blurb="", industry_share_pct=11,
        verdict_pill_text="Strong AI-channel attribution",
    )
    assert result["narrative_archetype"] == ARCH_STRONG_EVERYWHERE
    full_text = "\n\n".join(result["opening_paragraphs"])
    assert "WellPositionedBrand" in full_text
    assert ("goal state" in full_text.lower()
            or "strong" in full_text.lower())
    # Strategic implication: defend + extend
    assert ("defend" in result["strategic_implication"].lower()
            or "extend" in result["strategic_implication"].lower())


# ---------------------------------------------------------------------
# Narrative composition: mixed or partial fallback
# ---------------------------------------------------------------------


def test_mixed_or_partial_narrative_acknowledges_mixed_picture():
    from services.audit_narrative_builder import (
        build_executive_summary, ARCH_MIXED_OR_PARTIAL,
    )
    result = build_executive_summary(
        merchant_name="MixedBrand",
        visibility_score=40, attribution_score=35,
        category_visibility_score=45,
        evidence_quotes=[],
        cited_publishers=[],
        competitor_brands=[],
        industry_blurb="", industry_share_pct=11,
        verdict_pill_text="Partial AI-channel attribution",
    )
    assert result["narrative_archetype"] == ARCH_MIXED_OR_PARTIAL
    full_text = "\n\n".join(result["opening_paragraphs"])
    assert "MixedBrand" in full_text
    assert "mixed" in full_text.lower()


# ---------------------------------------------------------------------
# Defensive: missing/empty fields don't crash
# ---------------------------------------------------------------------


def test_missing_brand_name_falls_back_to_generic():
    from services.audit_narrative_builder import build_executive_summary
    result = build_executive_summary(
        merchant_name="",
        visibility_score=0, attribution_score=0,
        category_visibility_score=None,
        evidence_quotes=[], cited_publishers=[], competitor_brands=[],
        industry_blurb="", industry_share_pct=None,
        verdict_pill_text="",
    )
    full_text = "\n\n".join(result["opening_paragraphs"])
    assert "This brand" in full_text  # fallback name


def test_no_industry_share_pct_omits_growth_phrase():
    from services.audit_narrative_builder import build_executive_summary
    result = build_executive_summary(
        merchant_name="X",
        visibility_score=0, attribution_score=0,
        category_visibility_score=100,
        evidence_quotes=[
            {"excerpt_text": "X is great.", "source_labels": ["a.com"]},
        ],
        cited_publishers=["a.com"], competitor_brands=[],
        industry_blurb="", industry_share_pct=None,  # missing
        verdict_pill_text="",
    )
    full_text = "\n\n".join(result["opening_paragraphs"])
    # Should NOT include the "% of category traffic" growth blurb
    assert "% of category traffic" not in full_text
    # Other sections should still render
    assert len(result["opening_paragraphs"]) >= 2


def test_publisher_oxford_comma_join():
    """Publisher list rendering uses Oxford-and join for 3+ items."""
    from services.audit_narrative_builder import _format_publisher_list
    assert _format_publisher_list(["a.com"]) == "a.com"
    assert _format_publisher_list(["a.com", "b.com"]) == "a.com and b.com"
    assert _format_publisher_list(["a.com", "b.com", "c.com"]) == "a.com, b.com, and c.com"
    assert _format_publisher_list([]) == ""
    # Limit caps at 3 by default
    assert _format_publisher_list(
        ["a.com", "b.com", "c.com", "d.com"]
    ) == "a.com, b.com, and c.com"


def test_competitor_count_phrase():
    from services.audit_narrative_builder import _format_competitor_count
    assert _format_competitor_count([]) == "no direct competitor brands surfaced"
    assert _format_competitor_count([{"name": "AG1"}]) == "1 direct competitor brand surfaced"
    assert _format_competitor_count([
        {"name": "AG1"}, {"name": "Bloom"}
    ]) == "2 direct competitor brands surfaced"


def test_executive_summary_always_returns_required_fields():
    """No matter the inputs, the result has all required fields so
    renderers don't have to defensively check field presence."""
    from services.audit_narrative_builder import build_executive_summary
    result = build_executive_summary(
        merchant_name="X",
        visibility_score=0, attribution_score=0,
        category_visibility_score=None,
        evidence_quotes=[], cited_publishers=[], competitor_brands=[],
        industry_blurb="", industry_share_pct=None, verdict_pill_text="",
    )
    required_fields = {
        "narrative_archetype",
        "opening_paragraphs",
        "headline_finding",
        "strategic_implication",
        "verdict_pill_text",
        "evidence_quotes_used",
    }
    assert required_fields.issubset(result.keys())
    assert isinstance(result["opening_paragraphs"], list)


# ---------------------------------------------------------------------
# Integration: surfaces in build_structured_report response
# ---------------------------------------------------------------------


def test_build_structured_report_includes_executive_summary():
    """End-to-end: build_structured_report emits the
    executive_summary block in its response payload, with the
    archetype field populated."""
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
    assert "executive_summary" in report
    es = report["executive_summary"]
    assert "narrative_archetype" in es
    assert "opening_paragraphs" in es
    assert "headline_finding" in es
    assert "TestBrand" in es["headline_finding"]
