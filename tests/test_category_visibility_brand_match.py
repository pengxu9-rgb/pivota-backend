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


def _run_with_excerpt(
    brand_excerpt: str,
    *,
    sources: List[Dict[str, str]] | None = None,
    url_in_grounding: bool = False,
    llm_self_report: bool = False,
) -> Dict[str, Any]:
    return {
        "query": "best products",
        "raw": "{}",
        "url_match": {
            "in_grounding": url_in_grounding,
            "llm_self_report": llm_self_report,
        },
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
    # in_grounding AND no llm_self_report → should score 0, NOT 100.
    assert score == 0
    for d in details:
        assert d["excerpt_match"] is True
        assert d["title_match"] is False
        assert d["matched"] is False
        assert d["excerpt_only_signal"] is True
        assert d["excerpt_corroborated_match"] is False


# ---------------------------------------------------------------------
# 5. Excerpt-corroborated path (Grüns fix)
# ---------------------------------------------------------------------


def test_excerpt_corroborated_with_self_report_and_grounding_credits():
    """Reproduces the Grüns case: editorial source (Forbes) cites the
    brand by name, Gemini self-reports brand_appears=true, evidence
    excerpt contains the brand verbatim, but the brand string never
    appears in the grounding URL (vertex hash redirector) or title
    (bare hostname 'forbes.com'). All three signals agree → credit."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "Best Green Gummies: Grüns Superfoods Greens Gummies.",
            sources=[
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abcd",
                    "title": "forbes.com",
                },
            ],
            llm_self_report=True,
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    assert score == 100
    assert details[0]["excerpt_match"] is True
    assert details[0]["title_match"] is False
    assert details[0]["in_grounding"] is False
    assert details[0]["matched"] is True
    assert details[0]["excerpt_corroborated_match"] is True
    assert details[0]["excerpt_only_signal"] is False


def test_excerpt_match_with_self_report_but_no_grounding_does_not_credit():
    """Defense against pure LLM hallucination: excerpt + self-report
    but no grounding source means Gemini answered without web data —
    don't trust that answer."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "Grüns is a popular daily greens supplement.",
            sources=[],          # no grounding
            llm_self_report=True,
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    assert score == 0
    assert details[0]["matched"] is False
    assert details[0]["excerpt_corroborated_match"] is False


def test_self_report_alone_without_excerpt_match_does_not_credit():
    """Defense against generic LLM agreement: Gemini sometimes returns
    brand_appears=true as a default-yes even when the answer text
    doesn't actually mention the brand."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _run_with_excerpt(
            "There are many options in this category.",
            sources=[
                {"uri": "https://forbes.com/", "title": "forbes.com"},
            ],
            llm_self_report=True,
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    assert score == 0
    assert details[0]["matched"] is False


def test_gruns_real_audit_two_of_three_queries_credit():
    """End-to-end reproduction of the real Grüns audit. Of the 3
    category queries, query 1 had upstream failure (raw=""), queries
    2 and 3 had excerpt + self-report + grounding source. Expected
    score: 2 / 2 scoreable runs = 100/100 (NOT 0/3 = 0)."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        # Query 1: empty raw — upstream failure (the "network error").
        {
            "query": "best daily greens supplements 2026",
            "raw": "",
            "parsed": None,
            "url_match": {
                "in_grounding": False,
                "llm_self_report": None,
            },
            "grounding_sources": [],
        },
        # Query 2: Forbes citing Grüns by name.
        _run_with_excerpt(
            "Best Green Gummies: Grüns Superfoods Greens Gummies.",
            sources=[
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/q2",
                    "title": "forbes.com",
                },
            ],
            llm_self_report=True,
        ),
        # Query 3: trailandkale + Forbes + Women's Health.
        _run_with_excerpt(
            "When pitted against AG1, Grüns Daily offers a more "
            "budget-friendly alternative.",
            sources=[
                {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/q3a",
                 "title": "trailandkale.com"},
                {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/q3b",
                 "title": "forbes.com"},
                {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/q3c",
                 "title": "womenshealthmag.com"},
            ],
            llm_self_report=True,
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host="gruns.co", merchant_brand="Grüns",
    )
    # Pre-fix: this returned 0 (3 misses). Post-fix: 100 (2 of 2
    # scoreable runs matched; query 1 excluded as upstream-failed).
    assert score == 100
    assert details[0]["upstream_failed"] is True
    assert details[1]["matched"] is True
    assert details[1]["excerpt_corroborated_match"] is True
    assert details[2]["matched"] is True
    assert details[2]["excerpt_corroborated_match"] is True


# ---------------------------------------------------------------------
# 6. Upstream-failed runs excluded from denominator
# ---------------------------------------------------------------------


def test_upstream_failed_run_excluded_from_denominator():
    """When raw="" / parsed=None, the run is upstream-failed (likely
    a Gemini timeout / empty response). Don't count it as a miss."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        # 1 real match
        _run_with_excerpt(
            "",
            sources=[{"uri": "https://nymag.com/", "title": "Lunya is the top pick"}],
        ),
        # 1 upstream failure
        {
            "query": "what's new in pajamas this year",
            "raw": "",
            "parsed": None,
            "url_match": {"in_grounding": False, "llm_self_report": False},
            "grounding_sources": [],
        },
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Lunya",
    )
    # 1 match / 1 scoreable = 100 (NOT 1/2 = 50)
    assert score == 100
    assert details[1]["upstream_failed"] is True
    assert details[1]["matched"] is False


def test_all_runs_upstream_failed_returns_zero_with_details():
    """When every run failed upstream, score is 0 (rather than
    undefined NaN). Caller's UI uses `details[*].upstream_failed`
    to render 'couldn't probe' instead of '0/100 score'."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        {
            "query": "q1",
            "raw": "",
            "parsed": None,
            "url_match": {"in_grounding": False},
            "grounding_sources": [],
        },
        {
            "query": "q2",
            "raw": "   ",
            "parsed": None,
            "url_match": {"in_grounding": False},
            "grounding_sources": [],
        },
    ]
    score, details = score_category_visibility(
        runs, merchant_host=None, merchant_brand="Lunya",
    )
    assert score == 0
    assert all(d["upstream_failed"] for d in details)
