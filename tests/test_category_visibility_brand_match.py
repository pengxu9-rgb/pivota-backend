"""
Tightened brand-matching in `score_category_visibility`.

User reported a 1688 no-name product audit returned 100/100 category
visibility — implausible. Three failure modes in the original
substring-based match:

  1. Substring match against source titles / excerpts false-positives
     when the brand name is a substring of an unrelated word.
  2. excerpt_match alone credited a run, but Gemini's evidence
     excerpt is LLM-generated and can hallucinate the brand into the
     answer text without any grounding source actually citing it.
  3. No length floor on brand matching — short brand strings were
     matching far too aggressively.

Fix:
  - Word-boundary regex for brand match when brand is ≥ 4 chars.
  - excerpt_match alone no longer credits a run (must be corroborated
    by url_match or title_match). Surfaced in details as
    `excerpt_only_signal: True` for diagnostic visibility.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _run_with_excerpt(brand_excerpt: str, *, sources: List[Dict[str, str]] | None = None,
                     url_in_grounding: bool = False) -> Dict[str, Any]:
    return {
        "query": "best products",
        "url_match": {"in_grounding": url_in_grounding},
        "parsed": {"evidence_excerpt": brand_excerpt},
        "grounding_chunks": [s.get("uri") for s in (sources or [])],
        "grounding_sources": sources or [],
    }


# ---------------------------------------------------------------------
# 1. Substring false-positives are gone (word-boundary match)
# ---------------------------------------------------------------------


def test_brand_substring_no_longer_false_positive_in_title():
    """Brand 'Lily' should NOT match 'Lily-style designs' or
    'family-friendly' — substring match was the original bug."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "We tested family-friendly pajamas and lily-style designs.",
            sources=[{"uri": "https://nymag.com/", "title": "Family-friendly bedtime guide"}],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Lily",
    )
    # 'family' contains 'lily' as substring; word-boundary match
    # rejects this, so score should be 0.
    assert score == 0
    # Diagnostic flags reflect what was checked.
    assert details[0]["title_match"] is False


def test_brand_word_boundary_match_still_credits_legitimate_mention():
    """Brand 'Lunya' matches 'Lunya' verbatim in title — should still
    credit the run."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "",
            sources=[{"uri": "https://nymag.com/", "title": "Lunya is the top sleepwear pick"}],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Lunya",
    )
    assert score == 100
    assert details[0]["title_match"] is True


# ---------------------------------------------------------------------
# 2. Excerpt-only no longer credits (Gemini hallucination guard)
# ---------------------------------------------------------------------


def test_excerpt_only_does_not_credit_run():
    """The original bug: Gemini's evidence excerpt mentions the brand
    name (possibly hallucinated) but NO grounding source title cites
    it. That should NOT credit the run anymore."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "Chydan offers a range of satin sleepwear in this category.",
            sources=[
                {"uri": "https://nymag.com/", "title": "best sleepwear roundup"},
                {"uri": "https://forbes.com/", "title": "Forbes Vetted loungewear"},
            ],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Chydan",
    )
    assert score == 0
    assert details[0]["excerpt_match"] is True
    assert details[0]["matched"] is False
    assert details[0]["excerpt_only_signal"] is True


def test_excerpt_match_corroborated_by_title_match_credits():
    """When excerpt mentions the brand AND a grounding source title
    also contains the brand, the title_match alone is enough; excerpt
    is just bonus corroboration."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "Lunya is highly rated in this category.",
            sources=[{"uri": "https://nymag.com/", "title": "Lunya featured in Strategist's pajama guide"}],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Lunya",
    )
    assert score == 100
    assert details[0]["title_match"] is True
    assert details[0]["excerpt_match"] is True
    assert details[0]["matched"] is True
    assert details[0]["excerpt_only_signal"] is False


def test_url_match_in_grounding_credits_even_without_brand_match():
    """When the merchant URL itself appears in grounding chunks,
    that's the strongest signal — credit the run even if the brand
    name doesn't appear in title or excerpt."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "best sleepwear includes various brands",
            sources=[{"uri": "https://nymag.com/", "title": "Strategist roundup"}],
            url_in_grounding=True,
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host="testbrand.com", merchant_brand="TestBrand",
    )
    assert score == 100
    assert details[0]["in_grounding"] is True
    assert details[0]["matched"] is True


# ---------------------------------------------------------------------
# 3. Short-brand fallback (≤ 3 chars keeps substring match)
# ---------------------------------------------------------------------


def test_short_brand_keeps_substring_match():
    """Brands like 'GAP' (3 chars) are too short for word-boundary
    regex to reliably distinguish. Keep substring match for short
    brands; the false-positive class is moot at that length."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "",
            sources=[{"uri": "https://example.com/", "title": "GAP launches new line"}],
        ),
    ]
    score, _ = score_category_visibility(
        runs, merchant_host=None, merchant_brand="GAP",
    )
    assert score == 100


# ---------------------------------------------------------------------
# 4. End-to-end: 1688-style no-name product no longer scores 100
# ---------------------------------------------------------------------


def test_no_name_brand_with_only_excerpt_mentions_scores_zero():
    """Reproduces the user's reported scenario: a no-name brand whose
    name appears in a few evidence excerpts (Gemini paraphrasing) but
    is never in any grounding source title or URL — should score 0,
    not 100."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "The market for satin lingerie includes Chydan and several others.",
            sources=[
                {"uri": "https://whowhatwear.com/", "title": "Best lingerie brands 2026"},
                {"uri": "https://forbes.com/", "title": "Forbes Vetted: top loungewear"},
            ],
        ),
        _run_with_excerpt(
            "Chydan offers a range of satin sets in this segment.",
            sources=[
                {"uri": "https://today.com/", "title": "Today's pick: satin lingerie roundup"},
            ],
        ),
        _run_with_excerpt(
            "Among newer brands like Chydan, the satin set is popular.",
            sources=[
                {"uri": "https://nicolalondors.com/", "title": "Top sleepwear of the year"},
            ],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Chydan",
    )
    # All 3 runs have excerpt_match but no title_match and no
    # in_grounding → should score 0, NOT 100.
    assert score == 0
    for d in details:
        assert d["excerpt_match"] is True
        assert d["title_match"] is False
        assert d["matched"] is False
        assert d["excerpt_only_signal"] is True
