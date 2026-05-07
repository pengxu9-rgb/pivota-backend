"""
Phase C-4 follow-up: plain-language summary + competitive table.

Two new merchant-comprehension fields on `merchant_view`:

  1. `merchant_view.headline.plain_summary` — answers the merchant's
     direct question ("Am I visible?") in 1-2 sentences, in their
     language. Distinct from `verdict.explanation` (technical) and
     `verdict.label` (tier name).
  2. `merchant_view.receipts.competitive_table[]` — flat per-brand
     rows joining `peers_named` + `peers_with_first_party_visibility`.
     Frontend renders as a table (vs the prose-only competitive_pressure
     framing).
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------
# 1. Plain summary — per-tier behavior
# ---------------------------------------------------------------------


def test_plain_summary_for_invisible_says_no():
    from services.agent_center_bd_report_service import (
        _build_visibility_plain_summary, VERDICT_INVISIBLE,
    )
    s = _build_visibility_plain_summary(
        verdict_label=VERDICT_INVISIBLE,
        visibility_score=0, attribution_score=0,
        category_visibility_score=0,
        attribution_runs_total=9, merchant_cited_runs=0,
        top_retailers=[],
    )
    assert s.lower().startswith("no")
    assert "indexed" in s.lower() or "google" in s.lower()


def test_plain_summary_for_strong_says_yes():
    from services.agent_center_bd_report_service import (
        _build_visibility_plain_summary, VERDICT_STRONG,
    )
    s = _build_visibility_plain_summary(
        verdict_label=VERDICT_STRONG,
        visibility_score=85, attribution_score=80,
        category_visibility_score=85,
        attribution_runs_total=10, merchant_cited_runs=8,
        top_retailers=[],
    )
    assert s.lower().startswith("yes")
    assert "8 of 10" in s


def test_plain_summary_for_via_retailers_says_yes_and_no():
    """The most important case: merchants get the both/and answer
    that confused them in the original prompt — brand visible at
    category level, URL not winning citation."""
    from services.agent_center_bd_report_service import (
        _build_visibility_plain_summary, VERDICT_VIA_RETAILERS,
    )
    s = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=20, attribution_score=33,
        category_visibility_score=100,
        attribution_runs_total=9, merchant_cited_runs=3,
        top_retailers=["whowhatwear.com", "today.com", "forbes.com"],
    )
    assert "yes and no" in s.lower()
    assert "67%" in s
    assert "whowhatwear.com" in s
    assert "brand recognition" in s.lower()


def test_plain_summary_for_misattributed_says_partly():
    from services.agent_center_bd_report_service import (
        _build_visibility_plain_summary, VERDICT_MISATTRIBUTED,
    )
    s = _build_visibility_plain_summary(
        verdict_label=VERDICT_MISATTRIBUTED,
        visibility_score=70, attribution_score=10,
        category_visibility_score=None,
        attribution_runs_total=10, merchant_cited_runs=1,
        top_retailers=["amazon.com", "ebay.com"],
    )
    assert s.lower().startswith("partly")
    assert "1 of 10" in s
    assert "amazon.com" in s


def test_plain_summary_for_partial_says_mixed():
    from services.agent_center_bd_report_service import (
        _build_visibility_plain_summary, VERDICT_PARTIAL,
    )
    s = _build_visibility_plain_summary(
        verdict_label=VERDICT_PARTIAL,
        visibility_score=50, attribution_score=40,
        category_visibility_score=55,
        attribution_runs_total=10, merchant_cited_runs=4,
        top_retailers=[],
    )
    assert s.lower().startswith("mixed")
    assert "50/100" in s
    assert "40/100" in s


# ---------------------------------------------------------------------
# 2. Competitive table — flat per-brand rows
# ---------------------------------------------------------------------


def test_competitive_table_joins_peers_named_with_first_party():
    from services.agent_center_bd_report_service import _build_competitive_table
    cp = {
        "peers_named": [
            {"name": "Lunya", "times_cited": 5},
            {"name": "Eberjey", "times_cited": 3},
            {"name": "Hill House Home", "times_cited": 2},
        ],
        "peers_with_first_party_visibility": [
            {
                "brand": "Lunya",
                "first_party_host": "lunya.com",
                "category_query_mentions": 5,
                "host_citations": 2,
            },
        ],
    }
    rows = _build_competitive_table(cp)
    assert len(rows) == 3
    by_brand = {r["brand"]: r for r in rows}
    assert by_brand["Lunya"]["first_party_visible"] is True
    assert by_brand["Lunya"]["first_party_host"] == "lunya.com"
    assert by_brand["Lunya"]["host_citations"] == 2
    assert by_brand["Eberjey"]["first_party_visible"] is False
    assert by_brand["Eberjey"]["first_party_host"] is None
    assert by_brand["Hill House Home"]["first_party_visible"] is False


def test_competitive_table_empty_when_no_peers_named():
    from services.agent_center_bd_report_service import _build_competitive_table
    rows = _build_competitive_table({"peers_named": [], "peers_with_first_party_visibility": []})
    assert rows == []


def test_competitive_table_handles_missing_input():
    from services.agent_center_bd_report_service import _build_competitive_table
    assert _build_competitive_table({}) == []
    assert _build_competitive_table(None) == []


# ---------------------------------------------------------------------
# 3. End-to-end merchant_view plumbing
# ---------------------------------------------------------------------


def _vis_run(q): return {"query": q, "parsed": {"product_visible": False}, "grounding_chunks": []}
def _attr_run(q, **kw):
    parsed = {"merchant_url_found": kw.get("found", False)}
    if "competitors" in kw: parsed["competitors_appearing"] = kw["competitors"]
    return {"query": q, "parsed": parsed, "grounding_chunks": kw.get("grounding", [])}
def _category_run(q, *, sources, competitors=None):
    parsed = {"brand_appears": True, "evidence_text": ""}
    if competitors is not None:
        parsed["competitors_appearing"] = competitors
    return {
        "query": q, "parsed": parsed,
        "grounding_chunks": [s["uri"] for s in sources],
        "grounding_sources": sources,
    }


def _via_retailers_report():
    """Build a fixture matching the user's reported scenario:
    cat=100, attr~33, retailers in editorial. Note that the engine
    recomputes category_visibility from raw_runs by detecting the
    merchant_brand in grounding excerpts — fixture excerpts must
    mention 'TestSleepwear' for the recompute to land at 100."""
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Satin robe",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("v1")],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 33},
            "raw_runs": [
                _attr_run("buy q1", found=True, grounding=["https://testsleepwear.com/p/x"]),
                _attr_run("buy q2", found=False, grounding=["https://whowhatwear.com/x"]),
                _attr_run("buy q3", found=False, grounding=["https://today.com/x"]),
            ],
        },
        category_visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 100},
            "raw_runs": [
                {
                    "query": "best sleepwear brands",
                    "parsed": {
                        "brand_appears": True,
                        "evidence_text": "TestSleepwear is a top-rated brand for satin robes.",
                        "competitors_appearing": ["Lunya", "Eberjey"],
                    },
                    "grounding_chunks": [
                        "https://whowhatwear.com/",
                        "https://today.com/",
                    ],
                    "grounding_sources": [
                        {"uri": "https://whowhatwear.com/", "title": "TestSleepwear featured in best sleepwear roundup"},
                        {"uri": "https://today.com/", "title": "today.com"},
                    ],
                },
            ],
        },
        provider="gemini",
    )


def test_merchant_view_headline_carries_plain_summary():
    report = _via_retailers_report()
    h = report["merchant_view"]["headline"]
    assert "plain_summary" in h
    assert h["plain_summary"]
    # The reported scenario is VIA_RETAILERS — should say "yes and no".
    assert "yes and no" in h["plain_summary"].lower()


def test_merchant_view_receipts_carries_competitive_table():
    report = _via_retailers_report()
    r = report["merchant_view"]["receipts"]
    assert "competitive_table" in r
    table = r["competitive_table"]
    assert len(table) >= 1
    for row in table:
        for key in ("brand", "times_mentioned", "first_party_visible",
                    "first_party_host", "host_citations"):
            assert key in row, f"missing {key} in row {row}"
