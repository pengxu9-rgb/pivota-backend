"""
Phase C-4 (PR-F): per-failed-query winner attribution.

Today's report says "your URL was missing on N queries" — abstract.
After PR-F, each failed query carries:
  - the verbatim query Gemini was asked
  - the URL that won (top_cited_url)
  - the host extracted from that URL
  - the host's classification (PR-E)
  - any competitor brands Gemini named in the response

Surfaced as `merchant_view.receipts.failed_queries_detailed[]`.

These tests cover:
  - The `_build_failed_queries_detailed` helper directly (filtering,
    competitor extraction, classification integration)
  - The end-to-end merchant_view block plumbing
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------
# Helpers — same shape the service module expects
# ---------------------------------------------------------------------


def _attr_run(
    query: str,
    *,
    found: bool = False,
    grounding: List[str] | None = None,
    competitors: List[str] | None = None,
) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {"merchant_url_found": found}
    if competitors is not None:
        parsed["competitors_appearing"] = competitors
    return {
        "query": query,
        "parsed": parsed,
        "grounding_chunks": list(grounding or []),
    }


def _vis_run(query: str, *, visible: bool = False) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"product_visible": visible},
        "grounding_chunks": [],
    }


def _category_run(query: str, *, grounding_sources=None):
    return {
        "query": query,
        "parsed": {"brand_appears": True, "evidence_text": ""},
        "grounding_chunks": [s.get("uri") for s in (grounding_sources or [])],
        "grounding_sources": grounding_sources or [],
    }


# ---------------------------------------------------------------------
# 1. Helper: _build_failed_queries_detailed
# ---------------------------------------------------------------------


def test_helper_skips_queries_where_merchant_was_cited():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run("query A", found=True, grounding=["https://merchant.com/p/x"]),
        _attr_run("query B", found=False, grounding=["https://nymag.com/strategist/best-pajamas"]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert len(out) == 1
    assert out[0]["query"] == "query B"


def test_helper_extracts_top_cited_host_from_url():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run("q1", found=False, grounding=["https://nymag.com/strategist/best-pajamas-2026"]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert out[0]["top_cited_host"] == "nymag.com"
    assert out[0]["top_cited_url"] == "https://nymag.com/strategist/best-pajamas-2026"


def test_helper_classifies_top_cited_host():
    """When the winning URL is on a known editorial host, the entry
    carries the full classification metadata (type / coverage_note /
    outreach_hint / applies_to_merchant_category)."""
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run("q1", found=False, grounding=["https://nymag.com/strategist/best-pajamas"]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    cls = out[0]["host_classification"]
    assert cls["type"] == "editorial"
    assert cls["subtype"] == "review_site"
    assert cls["coverage_note"]
    assert cls["outreach_hint"]
    assert cls["applies_to_merchant_category"] is True
    # `host` field should NOT be in classification (redundant with top_cited_host)
    assert "host" not in cls


def test_helper_classifies_unknown_host_as_unclassified():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run("q1", found=False, grounding=["https://made-up.example/x"]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    cls = out[0]["host_classification"]
    assert cls["type"] == "unclassified"


def test_helper_handles_query_with_no_grounded_sources():
    """A failed query may simply have no grounding at all (Gemini
    returned nothing). top_cited_url + top_cited_host should be null;
    classification falls to unclassified."""
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run("q1", found=False, grounding=[]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert len(out) == 1
    assert out[0]["top_cited_url"] is None
    assert out[0]["top_cited_host"] is None
    assert out[0]["host_classification"]["type"] == "unclassified"


def test_helper_extracts_competitors_named_filtering_merchant_brand():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run(
            "q1",
            found=False,
            grounding=["https://nymag.com/x"],
            competitors=["Lunya", "Eberjey", "TestMerchant", "Hill House Home"],
        ),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert "TestMerchant" not in out[0]["competitors_named"]
    assert out[0]["competitors_named"] == ["Lunya", "Eberjey", "Hill House Home"]


def test_helper_filters_own_brand_aliases_not_just_substring():
    """#1382 follow-up (nit #3): the own-brand skip here used to be a bare
    bidirectional substring, so a de-spaced echo ("bblab") or a multi-word
    brand's alias slipped through into this merchant-facing competitors_named.
    Now alias-aware (derive_brand_aliases + word boundary), matching the
    authority-map / win-plan paths. Only the genuine rival survives."""
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run(
            "where to buy BB Lab collagen",
            found=False,
            grounding=["https://nymag.com/x"],
            # LLM echoed the merchant back in exact, spaced, and de-spaced forms
            # ("bblab" is NOT a substring of "bb lab global" → the old test missed it).
            competitors=["BB Lab Global", "BB Lab", "bblab", "GlowCo"],
        ),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="BB Lab Global",
        merchant_host="bblab.com",
        merchant_category="beauty",
    )
    assert out[0]["competitors_named"] == ["GlowCo"], out[0]["competitors_named"]


def test_helper_caps_competitors_at_5():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    competitors = [f"Brand{i}" for i in range(10)]
    runs = [
        _attr_run("q1", found=False, grounding=["https://nymag.com/x"], competitors=competitors),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert len(out[0]["competitors_named"]) == 5


def test_helper_caps_total_entries_at_default_10():
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run(f"q{i}", found=False, grounding=["https://nymag.com/x"])
        for i in range(15)
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    assert len(out) == 10


def test_helper_skips_entry_when_merchant_host_is_top_cited():
    """Defensive: if the parsed.merchant_url_found flag was wrong but
    the merchant's host is actually the top cited URL, skip the entry
    rather than show a misleading 'failed' for a query they actually
    won."""
    from services.agent_center_bd_report_service import _build_failed_queries_detailed
    runs = [
        _attr_run(
            "q1",
            found=False,  # parser said no, but the URL is the merchant's
            grounding=["https://merchant.com/p/x"],
        ),
        _attr_run("q2", found=False, grounding=["https://nymag.com/x"]),
    ]
    out = _build_failed_queries_detailed(
        runs,
        merchant_brand="TestMerchant",
        merchant_host="merchant.com",
        merchant_category="sleepwear",
    )
    # Only q2 makes it through.
    assert [e["query"] for e in out] == ["q2"]


# ---------------------------------------------------------------------
# 2. End-to-end merchant_view plumbing
# ---------------------------------------------------------------------


def _build_test_report():
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("v1")],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _attr_run(
                    "best women's pajamas under 100",
                    found=False,
                    grounding=["https://nymag.com/strategist/best-pajamas"],
                    competitors=["Lunya", "Eberjey", "Hill House Home"],
                ),
                _attr_run(
                    "soft sleepwear sets",
                    found=False,
                    grounding=["https://forbes.com/vetted/loungewear"],
                    competitors=["Eberjey", "Cuyana"],
                ),
                _attr_run(
                    "satin pajamas brand",
                    found=False,
                    grounding=[],  # Gemini returned nothing for this query
                ),
                _attr_run(
                    "where to buy testsleepwear pajamas",
                    found=True,  # success — should NOT appear in failed_queries_detailed
                    grounding=["https://testsleepwear.com/p/x"],
                ),
            ],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best pajamas",
                    grounding_sources=[{"uri": "https://nymag.com/", "title": "nymag.com"}],
                ),
            ],
        },
        provider="gemini",
    )


def test_merchant_view_failed_queries_detailed_block_present():
    report = _build_test_report()
    receipts = report["merchant_view"]["receipts"]
    assert "failed_queries_detailed" in receipts
    fqd = receipts["failed_queries_detailed"]
    # 4 attribution runs, 1 succeeded → 3 failed entries
    assert len(fqd) == 3


def test_merchant_view_failed_queries_excludes_succeeded():
    report = _build_test_report()
    fqd = report["merchant_view"]["receipts"]["failed_queries_detailed"]
    queries = [e["query"] for e in fqd]
    assert "where to buy testsleepwear pajamas" not in queries


def test_merchant_view_failed_queries_carry_classification_and_competitors():
    report = _build_test_report()
    fqd = report["merchant_view"]["receipts"]["failed_queries_detailed"]
    by_query = {e["query"]: e for e in fqd}

    nymag_q = by_query["best women's pajamas under 100"]
    assert nymag_q["top_cited_host"] == "nymag.com"
    assert nymag_q["host_classification"]["type"] == "editorial"
    assert "Lunya" in nymag_q["competitors_named"]
    assert "Eberjey" in nymag_q["competitors_named"]

    forbes_q = by_query["soft sleepwear sets"]
    assert forbes_q["top_cited_host"] == "forbes.com"
    assert forbes_q["host_classification"]["type"] == "editorial"

    no_grounding_q = by_query["satin pajamas brand"]
    assert no_grounding_q["top_cited_url"] is None
    assert no_grounding_q["top_cited_host"] is None
    assert no_grounding_q["host_classification"]["type"] == "unclassified"
