"""
Unit tests for the pure analysis + rendering logic in
`scripts/agent_center_bd_external_merchant.py`. The HTTP path is
intentionally not tested — that's the production path the script is
meant to validate; mocking it would defeat the purpose. Real end-to-end
runs are the BD operator's responsibility.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from typing import Any, Dict, List

import pytest


_HERE = os.path.dirname(__file__)
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def test_normalize_host_strips_www_and_lowercases() -> None:
    from agent_center_bd_external_merchant import _normalize_host
    assert _normalize_host("https://www.Glossier.com/products/cloud-paint") == "glossier.com"
    assert _normalize_host("https://Sephora.com/buy/x") == "sephora.com"
    assert _normalize_host("ulta.com/p/123") == "ulta.com"


def test_normalize_host_handles_garbage() -> None:
    from agent_center_bd_external_merchant import _normalize_host
    assert _normalize_host("") is None
    assert _normalize_host(None) is None  # type: ignore[arg-type]
    # No host (just a path) → None
    assert _normalize_host("https:///just-a-path") is None


def _run(grounding_chunks: List[str]) -> Dict[str, Any]:
    """Legacy probe payload shape (URI strings only, pre-PR-30)."""
    return {"grounding_chunks": grounding_chunks}


def _run_with_sources(sources: List[Dict[str, str]]) -> Dict[str, Any]:
    """Modern probe payload shape (PR 30+) — `grounding_sources` carries
    both URI and title. This is what production now emits."""
    return {
        "grounding_sources": sources,
        "grounding_chunks": [s["uri"] for s in sources],  # legacy mirror
    }


def test_extract_cited_hosts_uses_title_when_uri_is_redirector() -> None:
    """The actual production case from the Beauty of Joseon BD run:
    Vertex AI grounding wraps every cited URL in a redirector. Without
    title-based extraction the competitor list ended up as
    [{vertexaisearch.cloud.google.com: 2}] — useless. The fix reads
    title ("Sephora", "Olive Young Global", etc.) instead."""
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [
        _run_with_sources([
            {"uri": "https://vertexaisearch.cloud.google.com/abc",
             "title": "Beauty of Joseon Official Store"},  # merchant
            {"uri": "https://vertexaisearch.cloud.google.com/def",
             "title": "Sephora"},
            {"uri": "https://vertexaisearch.cloud.google.com/ghi",
             "title": "Olive Young Global"},
        ]),
        _run_with_sources([
            {"uri": "https://vertexaisearch.cloud.google.com/jkl",
             "title": "YesStyle"},
            {"uri": "https://vertexaisearch.cloud.google.com/mno",
             "title": "Sephora"},
        ]),
    ]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    # Brand name match catches the merchant title even though the URI is
    # a redirector and the host doesn't equal beautyofjoseon.com.
    assert merchant_runs == 1
    assert runs_with_citations == 2
    # Competitors: Sephora(2 runs) + Olive Young Global(1) + YesStyle(1).
    # No vertexaisearch entries — those are filtered as redirectors.
    assert competitors == Counter({
        "Sephora": 2,
        "Olive Young Global": 1,
        "YesStyle": 1,
    })


def test_extract_cited_hosts_legacy_uri_only_payload() -> None:
    """Backward compat: pre-PR-30 payloads only have grounding_chunks
    (URI strings). Real-host URIs (not redirectors) still work — labels
    fall back to the host."""
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [
        _run([
            "https://glossier.com/products/cloud-paint",  # merchant
            "https://sephora.com/product/cloud-paint",
        ]),
        _run([
            "https://ulta.com/p/glossier-cloud-paint",
            "https://sephora.com/product/cloud-paint",
        ]),
        _run([]),
    ]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs, merchant_host="glossier.com",
    )
    assert competitors == Counter({"sephora.com": 2, "ulta.com": 1})
    assert merchant_runs == 1
    assert runs_with_citations == 2


def test_extract_cited_hosts_skips_redirector_only_runs() -> None:
    """A run whose ONLY chunks are redirectors with no titles is not a
    real citation — don't count it toward runs_with_any_citation."""
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [
        _run_with_sources([
            {"uri": "https://vertexaisearch.cloud.google.com/abc", "title": ""},
            {"uri": "https://vertexaisearch.cloud.google.com/def", "title": ""},
        ]),
    ]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs, merchant_host=None,
    )
    assert competitors == Counter()
    assert runs_with_citations == 0


def test_extract_cited_hosts_merchant_host_none_treats_all_as_competitors() -> None:
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [_run(["https://sephora.com/x", "https://ulta.com/y"])]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs, merchant_host=None,
    )
    assert merchant_runs == 0
    assert runs_with_citations == 1
    assert competitors == Counter({"sephora.com": 1, "ulta.com": 1})


def test_extract_cited_hosts_dedupes_within_run() -> None:
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [
        _run([
            "https://sephora.com/p/a",
            "https://sephora.com/p/b",
            "https://sephora.com/p/c",
        ]),
    ]
    competitors, _, _ = _extract_cited_hosts(raw_runs, merchant_host=None)
    assert competitors == Counter({"sephora.com": 1})


def test_verdict_invisible_when_both_scores_low() -> None:
    from agent_center_bd_external_merchant import _verdict_for
    label, explanation = _verdict_for(visibility_score=10, attribution_score=5)
    assert label == "INVISIBLE"
    assert "fast-growing" in explanation


def test_verdict_misattributed_when_visible_but_no_attribution() -> None:
    from agent_center_bd_external_merchant import _verdict_for
    label, explanation = _verdict_for(visibility_score=70, attribution_score=10)
    assert label == "VISIBLE BUT MISATTRIBUTED"
    assert "third-party" in explanation
    # BD pitch sweet spot: post-reframe, this verdict explicitly names
    # Pivota's two-part value prop (canonical AI-channel PDP + in-chat
    # checkout via agentic-commerce protocol).
    assert "agentic-commerce" in explanation.lower() or "in-chat" in explanation.lower()


def test_verdict_strong_when_both_high() -> None:
    from agent_center_bd_external_merchant import _verdict_for
    label, _ = _verdict_for(visibility_score=70, attribution_score=80)
    assert label == "STRONG"


def test_verdict_partial_when_mixed() -> None:
    from agent_center_bd_external_merchant import _verdict_for
    label, _ = _verdict_for(visibility_score=40, attribution_score=40)
    assert label == "PARTIAL"


def test_render_markdown_smoke() -> None:
    """Full report renders without throwing — the smoke test that
    catches template / formatting bugs the analysis tests miss."""
    from agent_center_bd_external_merchant import render_markdown_report
    report = render_markdown_report({
        "merchant_name": "Glossier",
        "merchant_pdp_url": "https://glossier.com/products/cloud-paint",
        "product_title": "Cloud Paint",
        "product_vendor": "Glossier",
        "product_type": "blush",
        "visibility_result": {
            "scores": {"visibility_score": 30},
            "raw_runs": [
                {
                    "query": "where can I buy Cloud Paint",
                    "parsed": {"product_visible": True},
                    "grounding_chunks": ["https://sephora.com/p/cloud-paint"],
                },
                {
                    "query": "Cloud Paint reviews",
                    "parsed": {"product_visible": False},
                    "grounding_chunks": [],
                },
            ],
        },
        "attribution_result": {
            "scores": {"visibility_score": 0},
            "raw_runs": [
                {
                    "query": "shop Cloud Paint online",
                    "parsed": {"merchant_url_found": False},
                    "grounding_chunks": [
                        "https://sephora.com/p/cloud-paint",
                        "https://ulta.com/cloud-paint",
                    ],
                },
            ],
        },
    })

    # Top-level structure
    assert "# AI Commerce Readiness Report — Glossier" in report
    # Verdict picked
    assert "VISIBLE BUT MISATTRIBUTED" in report or "INVISIBLE" in report or "PARTIAL" in report
    # Score values appear
    assert "30/100" in report
    assert "0/100" in report
    # Per-query table built
    assert "where can I buy Cloud Paint" in report
    # Competitor tables include the third-party hosts
    assert "sephora.com" in report
    assert "ulta.com" in report
    # Methodology section is present
    assert "Methodology" in report
    # Raw probe payload embedded for auditability
    assert "Raw probe data" in report


def test_structured_report_marks_real_when_upstream_is_gemini() -> None:
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    assert report["upstream_status"]["is_real"] is True
    assert report["upstream_status"]["reason"] is None
    assert report["upstream_status"]["visibility_provider"] == "gemini"


def test_structured_report_flags_local_mock_no_internal_key() -> None:
    """Backend's local mock path — call never reached PIVOTA-Agent.
    Most likely root cause: PIVOTA_AGENT_INTERNAL_API_KEY unset on
    Railway."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "local_mock_no_internal_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "local_mock_no_internal_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",  # requested gemini but got local mock
    )
    assert report["upstream_status"]["is_real"] is False
    # Reason must mention the canonical (production-preferred) env var
    # name AND the Railway service name so ops sees actionable info.
    assert "PROMOTIONS_ADMIN_KEY" in report["upstream_status"]["reason"]
    assert "web-production-fedb" in report["upstream_status"]["reason"]


def test_structured_report_flags_mock_fallback_no_gemini_key() -> None:
    """PIVOTA-Agent's GEMINI_API_KEY unset path."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "mock_fallback_no_gemini_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "mock_fallback_no_gemini_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    assert report["upstream_status"]["is_real"] is False
    assert "GEMINI_API_KEY" in report["upstream_status"]["reason"]


def test_structured_report_flags_when_only_one_probe_fell_back() -> None:
    """Asymmetric: visibility ran on Gemini, attribution returned mock.
    Reporting must treat the whole report as mock — any mock probe
    contaminates the verdict."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "mock_fallback_no_gemini_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    # The most-degraded provider drives the status.
    assert report["upstream_status"]["is_real"] is False
    # Both providers visible in the structure for diagnostic.
    assert report["upstream_status"]["visibility_provider"] == "mock_fallback_no_gemini_key"
    assert report["upstream_status"]["attribution_provider"] == "gemini"


def test_render_markdown_includes_mock_warning_when_not_real() -> None:
    """The markdown report must surface a "DO NOT SHARE" warning when
    upstream fell back to mock — silent fallback was the failure mode
    that motivated this PR."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "local_mock_no_internal_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "local_mock_no_internal_key", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert "MOCK DATA" in md
    assert "DO NOT SHARE" in md
    assert "PROMOTIONS_ADMIN_KEY" in md


def test_render_markdown_omits_warning_when_real() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="X", merchant_pdp_url="https://example.com/p/1",
        product_title="Y", product_vendor=None, product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert "MOCK DATA" not in md
    assert "DO NOT SHARE" not in md
    # Real upstream is mentioned in the header for transparency.
    assert "real Gemini" in md


def test_render_markdown_handles_empty_attribution_runs() -> None:
    """Edge case: if Gemini returned zero grounded sources for any
    attribution query, the report should explicitly say so rather than
    silently rendering an empty competitor table."""
    from agent_center_bd_external_merchant import render_markdown_report
    report = render_markdown_report({
        "merchant_name": "Test Merchant",
        "merchant_pdp_url": "https://example.com/p/x",
        "product_title": "Some Product",
        "visibility_result": {"scores": {"visibility_score": 5}, "raw_runs": []},
        "attribution_result": {
            "scores": {"visibility_score": 0},
            "raw_runs": [
                {
                    "query": "where can I buy Some Product",
                    "parsed": {"merchant_url_found": False},
                    "grounding_chunks": [],
                },
            ],
        },
    })
    assert "didn't return any cited URLs" in report


# ---------------------------------------------------------------------------
# Phase 1b: industry_context — category lookup
# ---------------------------------------------------------------------------


def test_industry_context_routes_beauty_keywords() -> None:
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for("eye patch", "Beauty of Joseon", "Revive Under Eye Patch")
    assert ctx["category"] == "beauty"
    assert ctx["ai_search_share_pct"] == 12
    assert "beauty" in ctx["blurb"].lower()


def test_industry_context_routes_fashion_keywords() -> None:
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for("sneaker", "Allbirds", "Tree Runners")
    assert ctx["category"] == "fashion"


def test_industry_context_routes_electronics_keywords() -> None:
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for("headphone", "Sony", "WH-1000XM5")
    assert ctx["category"] == "electronics"
    assert ctx["ai_search_share_pct"] == 14  # highest among consumer verticals


def test_industry_context_default_when_unknown() -> None:
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(None, None, None)
    assert ctx["category"] == "default"
    assert ctx["ai_search_share_pct"] is None  # don't fabricate numbers
    assert "fast-growing discovery channel" in ctx["blurb"]


def test_industry_context_inspects_title_when_product_type_missing() -> None:
    """Real BD scenario: operator forgets product_type. Fall back to
    title-keyword match so we still classify correctly."""
    from services.agent_center_bd_report_service import _industry_context_for
    ctx = _industry_context_for(None, "Glossier", "Cloud Paint blush")
    assert ctx["category"] == "beauty"


# ---------------------------------------------------------------------------
# Phase 1b: action_items — rule-based merchant-specific actions
# ---------------------------------------------------------------------------


def _attr_run(query: str, *, found: bool, grounding: List[str] | None = None) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"merchant_url_found": found},
        "grounding_chunks": grounding or [],
    }


def _vis_run(query: str, *, visible: bool, grounding: List[str] | None = None) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"product_visible": visible},
        "grounding_chunks": grounding or [],
    }


def test_action_items_invisible_leads_with_search_console() -> None:
    """INVISIBLE = nothing surfaces; root cause is most likely indexing,
    so the first action must point at Search Console submission."""
    from services.agent_center_bd_report_service import _generate_action_items
    items = _generate_action_items(
        verdict_label="INVISIBLE",
        visibility_runs=[_vis_run("q1", visible=False), _vis_run("q2", visible=False)],
        attribution_runs=[_attr_run("q1", found=False), _attr_run("q2", found=False)],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=0,
    )
    assert items[0]["severity"] == "critical"
    assert "Search Console" in items[0]["body"]


def test_action_items_misattributed_calls_out_zero_attribution_and_top_competitor() -> None:
    """The BD sweet spot: VISIBLE BUT MISATTRIBUTED with a clear top
    competitor. Rep should see (a) the verdict-level callout,
    (b) the named competitor, (c) zero-attribution flag, (d) failed
    query references."""
    from services.agent_center_bd_report_service import _generate_action_items
    items = _generate_action_items(
        verdict_label="VISIBLE BUT MISATTRIBUTED",
        visibility_runs=[
            _vis_run("where can I buy X", visible=True, grounding=["https://sephora.com/x"]),
            _vis_run("X reviews", visible=True, grounding=["https://allure.com/r"]),
        ],
        attribution_runs=[
            _attr_run("where can I buy X", found=False, grounding=["https://sephora.com/x"]),
            _attr_run("shop X online", found=False, grounding=["https://ulta.com/y"]),
        ],
        competitor_hosts=[
            {"host": "Sephora", "times_cited": 3},
            {"host": "Ulta", "times_cited": 1},
        ],
        merchant_cited_runs=0,
        runs_with_any_citation=2,
    )
    assert items[0]["severity"] == "critical"
    titles = [i["title"] for i in items]
    bodies = " ".join(i["body"] for i in items)
    # Top-competitor named with frequency
    assert any("Sephora" in t for t in titles)
    assert "3 of the queries" in bodies
    # Zero-attribution callout
    assert any("Zero direct AI-channel attribution" in t for t in titles)


def test_action_items_strong_yields_low_severity_maintain_action() -> None:
    from services.agent_center_bd_report_service import _generate_action_items
    items = _generate_action_items(
        verdict_label="STRONG",
        visibility_runs=[_vis_run("q", visible=True, grounding=["https://x.com"])],
        attribution_runs=[_attr_run("q", found=True, grounding=["https://x.com/p"])],
        competitor_hosts=[],
        merchant_cited_runs=1,
        runs_with_any_citation=1,
    )
    assert items[0]["severity"] == "low"
    assert "monitoring" in items[0]["body"].lower()


def test_action_items_capped_at_5() -> None:
    """Even when every action condition fires, list stays scannable."""
    from services.agent_center_bd_report_service import _generate_action_items
    items = _generate_action_items(
        verdict_label="VISIBLE BUT MISATTRIBUTED",
        visibility_runs=[
            _vis_run("q1", visible=False),
            _vis_run("q2", visible=False),
            _vis_run("q3", visible=False),
        ],
        attribution_runs=[
            _attr_run("q1", found=False),
            _attr_run("q2", found=False),
            _attr_run("q3", found=False),
        ],
        competitor_hosts=[
            {"host": "Sephora", "times_cited": 5},
            {"host": "Ulta", "times_cited": 3},
        ],
        merchant_cited_runs=0,
        runs_with_any_citation=3,
    )
    assert len(items) <= 5


def test_action_items_referenced_failed_queries_truncated() -> None:
    """Failed queries are surfaced inline; long ones must be truncated
    so the markdown stays scannable."""
    from services.agent_center_bd_report_service import _generate_action_items
    long_query = "where can I buy this very long product name with many additional descriptors blah blah"
    items = _generate_action_items(
        verdict_label="PARTIAL",
        visibility_runs=[_vis_run(long_query, visible=False)],
        attribution_runs=[_attr_run(long_query, found=False)],
        competitor_hosts=[],
        merchant_cited_runs=0,
        runs_with_any_citation=0,
    )
    failed_queries_action = next(
        (i for i in items if "queries where your URL was missing" in i["title"]),
        None,
    )
    assert failed_queries_action is not None
    assert "…" in failed_queries_action["body"]


# ---------------------------------------------------------------------------
# Phase 1b: structured report wires action_items + industry_context
# ---------------------------------------------------------------------------


def test_structured_report_includes_action_items_and_industry_context() -> None:
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/products/under-eye-patch",
        product_title="Revive Under Eye Patch",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 33},
            "raw_runs": [
                _vis_run("q1", visible=True, grounding=["https://x.com"]),
                _vis_run("q2", visible=False),
                _vis_run("q3", visible=False),
            ],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _attr_run("q1", found=False, grounding=["https://sephora.com/x"]),
                _attr_run("q2", found=False, grounding=["https://sephora.com/y"]),
                _attr_run("q3", found=False),
            ],
        },
        provider="gemini",
    )
    # Industry context picked from "eye patch" → beauty
    assert report["industry_context"]["category"] == "beauty"
    assert report["industry_context"]["ai_search_share_pct"] == 12
    # Action items generated
    assert isinstance(report["action_items"], list)
    assert 1 <= len(report["action_items"]) <= 5
    # Verdict is MISATTRIBUTED, so first action is the critical
    # reclaim-attribution callout
    assert report["action_items"][0]["severity"] == "critical"


def test_structured_report_with_category_visibility() -> None:
    """Phase 2a: when category_visibility_result is supplied, the
    structured report exposes a `category_visibility` block + an
    optional `verdict.category_visibility_score` field. Phase 2d:
    score is re-derived from raw_runs (brand text-match, not just
    URL match) — upstream's score is preserved at `upstream_score`
    for audit but doesn't drive the verdict."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/p/x",
        product_title="Under Eye Patch",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 33},
            "raw_runs": [_vis_run("q1", visible=True, grounding=["https://x.com"])],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("q1", found=False)],
        },
        category_visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 25},
            "raw_runs": [
                {
                    "query": "best Korean eye patches 2026",
                    "parsed": {"brand_appears": False},
                    "grounding_chunks": ["https://allure.com/x"],
                },
            ],
        },
        provider="gemini",
    )
    assert report["category_visibility"] is not None
    # Re-scored: 0 matches across 1 run (no URL/title/excerpt match
    # for the brand) → 0/100, NOT the upstream's 25.
    assert report["category_visibility"]["score"] == 0
    assert report["category_visibility"]["upstream_score"] == 25
    assert len(report["category_visibility"]["queries"]) == 1
    assert report["verdict"]["category_visibility_score"] == 0


def test_structured_report_omits_category_block_when_not_run() -> None:
    """Backward compat: when category_visibility_result is None, the
    block is null (not undefined / missing) so consumers can rely on
    its presence."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X",
        merchant_pdp_url="https://x.com/p/1",
        product_title="Y",
        product_vendor=None,
        product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": []},
        provider="gemini",
    )
    assert report["category_visibility"] is None
    assert report["verdict"]["category_visibility_score"] is None


def test_render_markdown_includes_category_section_when_present() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/p/x",
        product_title="Under Eye Patch",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        category_visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [
                {
                    "query": "best Korean eye patches 2026",
                    "parsed": {"brand_appears": False},
                    "grounding_chunks": [],
                },
            ],
        },
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert "## 1.5. Category-level discoverability" in md
    assert "best Korean eye patches 2026" in md
    # Score-0 callout fires for the BD-pitch sweet-spot phrasing
    assert "harshest BD signal" in md


def test_verdict_for_uses_default_thresholds_when_no_peer_thresholds() -> None:
    """Phase 2c: backward compat — verdict_for() with no peer_thresholds
    arg behaves exactly like the V1.5 version."""
    from services.agent_center_bd_report_service import verdict_for
    label, _ = verdict_for(visibility_score=20, attribution_score=10)
    assert label == "INVISIBLE"
    label, _ = verdict_for(visibility_score=70, attribution_score=20)
    assert label == "VISIBLE BUT MISATTRIBUTED"
    label, _ = verdict_for(visibility_score=70, attribution_score=70)
    assert label == "STRONG"
    label, _ = verdict_for(visibility_score=40, attribution_score=40)
    assert label == "PARTIAL"


def test_verdict_for_honors_peer_thresholds() -> None:
    """Phase 2c: verdict thresholds shift when calibrated to peer data.
    Same scores can yield different verdicts under different cohorts."""
    from services.agent_center_bd_report_service import verdict_for
    # Strict cohort: 50/100 thresholds. visibility=40 / attr=40 used to be
    # PARTIAL with default 30/60; under 50/100 it stays PARTIAL too
    # because attribution=40 ≥ 50 is false (misattr trigger), and both <
    # 100 (strong trigger). But visibility=40 < 50 AND attribution=40 < 50
    # → INVISIBLE under stricter thresholds.
    label, _ = verdict_for(
        visibility_score=40, attribution_score=40,
        peer_thresholds={"invisible_max": 50, "strong_min": 100, "misattributed_attr_max": 50},
    )
    assert label == "INVISIBLE"

    # Loose cohort: 10/30 thresholds. visibility=40 ≥ 30 AND attribution=40
    # ≥ 30 → STRONG.
    label, _ = verdict_for(
        visibility_score=40, attribution_score=40,
        peer_thresholds={"invisible_max": 10, "strong_min": 30, "misattributed_attr_max": 10},
    )
    assert label == "STRONG"


def test_verdict_for_partial_peer_thresholds_falls_back_per_key() -> None:
    """Calibrated thresholds support partial overrides — only specify
    the keys you have data for; the rest stay at defaults."""
    from services.agent_center_bd_report_service import verdict_for
    label, _ = verdict_for(
        visibility_score=70, attribution_score=70,
        # Only override strong_min upward — visibility/attr=70 should no
        # longer be STRONG. Defaults for invisible_max=30 still apply.
        peer_thresholds={"strong_min": 80},
    )
    # Both ≥ 30 (not invisible), attr ≥ 30 (not misattr), but neither ≥ 80 → PARTIAL
    assert label == "PARTIAL"


def test_verdict_explanation_includes_peer_calibration_prefix() -> None:
    """When peer thresholds are used, the BD rep needs to know the
    score is being interpreted against an empirical cohort, not raw
    intuition. Explanation text says so."""
    from services.agent_center_bd_report_service import verdict_for
    _, explanation = verdict_for(
        visibility_score=40, attribution_score=40,
        peer_thresholds={"invisible_max": 50, "strong_min": 100, "misattributed_attr_max": 50},
    )
    assert "Calibrated thresholds" in explanation
    assert "INVISIBLE < 50" in explanation
    assert "STRONG ≥ 100" in explanation
    assert "Your visibility 40/100" in explanation


def test_calibrate_thresholds_from_baseline_uses_p25_p75() -> None:
    """Phase 2c: empirical calibration. P25/P75 of visibility scores
    drive invisible_max / strong_min."""
    from services.agent_center_bd_report_service import calibrate_thresholds_from_baseline
    visibility_scores = [10, 20, 30, 40, 50, 60, 70, 80]
    attribution_scores = [0, 10, 20, 30, 40, 50, 60, 70]
    thresholds = calibrate_thresholds_from_baseline(visibility_scores, attribution_scores)
    # Nearest-rank P25 of [10,20,30,40,50,60,70,80] → ceil(0.25*8)=2nd → 20
    assert thresholds["invisible_max"] == 20
    # P75 → ceil(0.75*8)=6th → 60
    assert thresholds["strong_min"] == 60
    # P25 of attribution_scores → 2nd → 10
    assert thresholds["misattributed_attr_max"] == 10


def test_calibrate_thresholds_from_baseline_returns_defaults_on_empty() -> None:
    from services.agent_center_bd_report_service import (
        calibrate_thresholds_from_baseline,
        DEFAULT_VERDICT_THRESHOLDS,
    )
    assert calibrate_thresholds_from_baseline([], []) == DEFAULT_VERDICT_THRESHOLDS


def test_aggregate_brand_scores_averages_across_succeeded_products() -> None:
    """Phase 2b: brand-level aggregate is the simple-mean of per-product
    scores. No median / weighted-average — keep V1 honest."""
    from services.agent_center_bd_report_service import _aggregate_brand_scores
    per_product = [
        {"verdict": {"visibility_score": 60, "attribution_score": 30, "category_visibility_score": 0}},
        {"verdict": {"visibility_score": 100, "attribution_score": 60, "category_visibility_score": 25}},
        {"verdict": {"visibility_score": 40, "attribution_score": 0, "category_visibility_score": 0}},
    ]
    agg = _aggregate_brand_scores(per_product)
    # (60+100+40)/3 = 66.67, rounded to 1 decimal = 66.7
    assert agg["avg_visibility"] == 66.7
    # (30+60+0)/3 = 30.0
    assert agg["avg_attribution"] == 30.0
    # (0+25+0)/3 = 8.33 → 8.3
    assert agg["avg_category_visibility"] == 8.3
    # avg_visibility=66 (≥60) + avg_attribution=30 (<60) → not STRONG.
    # avg_visibility=66 ≥30 + avg_attribution=30 < 30 is false (== 30
    # is at the boundary — 30 < 30 is False). Falls into PARTIAL.
    assert agg["brand_verdict_label"] == "PARTIAL"


def test_aggregate_brand_scores_handles_empty() -> None:
    from services.agent_center_bd_report_service import _aggregate_brand_scores
    agg = _aggregate_brand_scores([])
    assert agg["avg_visibility"] is None
    assert agg["avg_attribution"] is None
    assert agg["brand_verdict_label"] is None
    assert "can't aggregate" in agg["brand_verdict_explanation"]


def test_aggregate_brand_scores_skips_missing_category_when_only_some_have_it() -> None:
    """Phase 2a interaction: some products have category_visibility,
    some don't (e.g. one product missing product_type). Average should
    use only the ones that ran."""
    from services.agent_center_bd_report_service import _aggregate_brand_scores
    per_product = [
        {"verdict": {"visibility_score": 50, "attribution_score": 50, "category_visibility_score": 25}},
        {"verdict": {"visibility_score": 50, "attribution_score": 50, "category_visibility_score": None}},
    ]
    agg = _aggregate_brand_scores(per_product)
    assert agg["avg_visibility"] == 50.0
    # Only the one with category=25 contributes to the average.
    assert agg["avg_category_visibility"] == 25.0


def test_aggregate_brand_competitors_sums_across_products() -> None:
    """Cross-product competitor frequency: Sephora cited 3× on product
    A + 2× on product B = 5 total. Better signal than per-product
    competitor table because BD wants brand-wide narrative."""
    from services.agent_center_bd_report_service import _aggregate_brand_competitors
    per_product = [
        {"attribution": {"competitor_hosts": [
            {"host": "Sephora", "times_cited": 3},
            {"host": "Ulta", "times_cited": 1},
        ]}},
        {"attribution": {"competitor_hosts": [
            {"host": "Sephora", "times_cited": 2},
            {"host": "YesStyle", "times_cited": 1},
        ]}},
    ]
    out = _aggregate_brand_competitors(per_product)
    assert out[0] == {"host": "Sephora", "times_cited": 5}
    # Top-15 ordering: Sephora (5), Ulta (1), YesStyle (1)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_run_brand_report_caps_at_5_products(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Cost guard: hard cap on per-call product count."""
    from services.agent_center_bd_report_service import run_brand_report
    products = [
        {"title": f"P{i}", "pdp_url": f"https://x.com/p/{i}", "product_type": "thing"}
        for i in range(6)
    ]
    with pytest.raises(ValueError, match="capped at 5"):
        await run_brand_report(
            merchant_name="X",
            merchant_domain=None,
            products=products,
        )


@pytest.mark.asyncio
async def test_run_brand_report_isolates_per_product_failures(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """One product crashing doesn't kill the brand run — per-product
    isolation is the core reliability property."""
    from services import agent_center_bd_report_service as bd

    call_count = {"n": 0}
    async def _fake_probe(**kwargs):
        call_count["n"] += 1
        # First product succeeds, second product raises, third succeeds.
        # run_bd_probes calls probe() twice (visibility + attribution)
        # OR 3 times (with category). For this test we'll have it
        # succeed on calls 1-3, fail on call 4 (visibility of product 2),
        # succeed on the rest.
        if call_count["n"] == 4:
            raise RuntimeError("upstream timeout for product 2")
        return {
            "scan_mode": kwargs.get("scan_mode"),
            "provider": "gemini",
            "scores": {"visibility_score": 50},
            "raw_runs": [],
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    out = await bd.run_brand_report(
        merchant_name="Brand X",
        merchant_domain="brandx.com",
        products=[
            {"title": "P1", "pdp_url": "https://x.com/p/1", "product_type": "thing"},
            {"title": "P2", "pdp_url": "https://x.com/p/2", "product_type": "thing"},
            {"title": "P3", "pdp_url": "https://x.com/p/3", "product_type": "thing"},
        ],
        include_category_visibility=False,  # 2 calls per product instead of 3
    )
    # Product 2 should be in failed[], products 1+3 in per_product
    assert out["aggregate"]["products_count"] == 3
    assert out["aggregate"]["products_succeeded"] == 2
    assert out["aggregate"]["products_failed"] == 1
    assert len(out["per_product"]) == 2
    assert out["failed"][0]["title"] == "P2"
    assert "upstream timeout" in out["failed"][0]["error"]


@pytest.mark.asyncio
async def test_run_brand_report_aggregate_competitor_view(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    """End-to-end: brand-level competitor frequency rolls up correctly."""
    from services import agent_center_bd_report_service as bd

    async def _fake_probe(**kwargs):
        # Both products' attribution probes return Sephora cite.
        # Visibility/category probes return no grounding.
        scan_mode = kwargs.get("scan_mode")
        if scan_mode == "merchant_store_attribution_test":
            return {
                "scan_mode": scan_mode,
                "provider": "gemini",
                "scores": {"visibility_score": 0},
                "raw_runs": [
                    {
                        "query": "buy",
                        "parsed": {"merchant_url_found": False},
                        "grounding_chunks": ["https://vertexaisearch.cloud.google.com/x"],
                        "grounding_sources": [
                            {"uri": "https://vertexaisearch.cloud.google.com/x",
                             "title": "Sephora"},
                        ],
                    },
                ],
                "findings": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        return {
            "scan_mode": scan_mode, "provider": "gemini",
            "scores": {"visibility_score": 50}, "raw_runs": [], "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    out = await bd.run_brand_report(
        merchant_name="Brand X",
        merchant_domain=None,
        products=[
            {"title": "P1", "pdp_url": "https://x.com/p/1", "product_type": "thing"},
            {"title": "P2", "pdp_url": "https://x.com/p/2", "product_type": "thing"},
        ],
        include_category_visibility=False,
    )
    # Sephora is cited 1× on each of 2 products = 2 total brand-wide.
    competitors = out["cross_product_competitors"]
    assert competitors[0] == {"host": "Sephora", "times_cited": 2}


def test_render_markdown_includes_industry_context_and_actions() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/products/under-eye-patch",
        product_title="Revive Under Eye Patch",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 33},
            "raw_runs": [_vis_run("q1", visible=True, grounding=["https://x.com"])],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("q1", found=False, grounding=["https://sephora.com/x"])],
        },
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    # Industry context section + concrete category figure
    assert "## Industry context" in md
    assert "12%" in md
    # Recommended actions section + at least one severity-tagged item
    assert "## Recommended actions" in md
    assert "_(severity:" in md


# ---------------------------------------------------------------------------
# Phase 2d: brand text-match scoring + VISIBLE_VIA_RETAILERS verdict.
#
# Regression case: the first real BoJ BD run scored category_visibility=0
# even though Gemini quoted "Beauty of Joseon" verbatim in 2/3 category
# query excerpts and cited Sephora / Olive Young / Lookfantastic as
# grounded sources. Upstream's URL-only matcher missed all of it.
# ---------------------------------------------------------------------------


def _category_run(
    query: str,
    *,
    excerpt: str = "",
    grounding_sources: List[Dict[str, str]] = None,
    competitors_appearing: List[str] = None,
    brand_appears: bool = False,
    in_grounding: bool = False,
) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {
            "brand_appears": brand_appears,
            "competitors_appearing": competitors_appearing or [],
            "evidence_excerpt": excerpt,
        },
        "grounding_chunks": [
            s["uri"] for s in (grounding_sources or [])
        ],
        "grounding_sources": grounding_sources or [],
        "url_match": {
            "in_grounding": in_grounding,
            "in_text": False,
            "llm_self_report": brand_appears,
        },
    }


def test_score_category_visibility_credits_brand_text_match() -> None:
    """The BoJ-class case: 2/3 runs quote the brand by name in the
    evidence_excerpt. Re-scored: 67/100, not the upstream's 0."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _category_run(
            "best eye patches 2026",
            excerpt="A wing-shaped hydrogel eye patch...",  # no brand name
            grounding_sources=[{"uri": "https://r/", "title": "sephora.com"}],
        ),
        _category_run(
            "top eye patches this year",
            excerpt="Beauty of Joseon Revive Under Eye Patch is described as...",
            grounding_sources=[{"uri": "https://r/", "title": "sephora.com"}],
        ),
        _category_run(
            "best eye patches under $50",
            excerpt="Beauty of Joseon Revive Under Eye Patches for Wrinkles...",
            grounding_sources=[{"uri": "https://r/", "title": "youtube.com"}],
        ),
    ]
    score, details = score_category_visibility(
        runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    assert score == 67  # 2/3 matched via excerpt
    assert details[0]["matched"] is False
    assert details[1]["matched"] is True
    assert details[1]["excerpt_match"] is True
    assert details[2]["matched"] is True


def test_score_category_visibility_credits_title_match() -> None:
    """A grounding source titled with the merchant brand counts as a
    full match even without text echo in the excerpt."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _category_run(
            "best eye patches 2026",
            excerpt="hydrogel patches...",
            grounding_sources=[
                {"uri": "https://r/", "title": "Beauty of Joseon Official Store"},
            ],
        ),
    ]
    score, details = score_category_visibility(
        runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    assert score == 100
    assert details[0]["title_match"] is True


def test_score_category_visibility_ignores_self_report_only() -> None:
    """Gemini's `brand_appears: true` self-report alone is NOT enough.
    Without textual confirmation in grounding we score 0 — the model
    frequently hallucinates self-attribution."""
    from services.agent_center_bd_report_service import score_category_visibility
    runs = [
        _category_run(
            "best eye patches 2026",
            excerpt="some other content",
            grounding_sources=[{"uri": "https://r/", "title": "sephora.com"}],
            brand_appears=True,  # LLM self-report — should be ignored
        ),
    ]
    score, _ = score_category_visibility(
        runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    assert score == 0


def test_score_category_visibility_empty() -> None:
    from services.agent_center_bd_report_service import score_category_visibility
    score, details = score_category_visibility(
        [], merchant_host="x.com", merchant_brand="X"
    )
    assert score == 0
    assert details == []


def test_extract_category_competitors_aggregates_brands_and_retailers() -> None:
    from services.agent_center_bd_report_service import extract_category_competitors
    runs = [
        _category_run(
            "q1",
            grounding_sources=[
                {"uri": "https://r1/", "title": "sephora.com"},
                {"uri": "https://r2/", "title": "youtube.com"},
            ],
            competitors_appearing=["Patchology", "Wander Beauty", "Beauty of Joseon"],
        ),
        _category_run(
            "q2",
            grounding_sources=[
                {"uri": "https://r3/", "title": "sephora.com"},  # repeat
                {"uri": "https://r4/", "title": "oliveyoung.com"},
            ],
            competitors_appearing=["Patchology", "Peace Out"],  # Patchology repeats
        ),
    ]
    brands, retailers = extract_category_competitors(
        runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    brand_map = {b["name"]: b["times_cited"] for b in brands}
    retailer_map = {r["host"]: r["times_cited"] for r in retailers}
    # Patchology in both runs → 2; Wander Beauty + Peace Out → 1 each.
    # Beauty of Joseon (the merchant) is filtered out.
    assert brand_map["Patchology"] == 2
    assert brand_map["Wander Beauty"] == 1
    assert brand_map["Peace Out"] == 1
    assert "Beauty of Joseon" not in brand_map
    # Sephora cited in both → 2; oliveyoung + youtube → 1 each.
    assert retailer_map["sephora.com"] == 2
    assert retailer_map["oliveyoung.com"] == 1


def test_verdict_visible_via_retailers_triggers_when_category_high_attr_zero() -> None:
    """The BoJ-class verdict: named-product visibility 0, attribution 0,
    BUT category_visibility 67 → VISIBLE VIA RETAILERS, not INVISIBLE."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS
    label, explanation = verdict_for(
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=67,
    )
    assert label == VERDICT_VIA_RETAILERS
    assert "retailers" in explanation.lower() or "retailer" in explanation.lower()


def test_verdict_invisible_when_category_also_zero() -> None:
    """Genuine INVISIBLE: nothing surfaces anywhere, including category."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_INVISIBLE
    label, _ = verdict_for(
        visibility_score=0,
        attribution_score=0,
        category_visibility_score=0,
    )
    assert label == VERDICT_INVISIBLE


def test_verdict_via_retailers_cosrx_class_partial_attribution() -> None:
    """COSRX-class case: category visibility 100, attribution 33 (1/3
    buyer-intent queries cite cosrx.com). The 67-point gap means
    retailers (Vogue Scandinavia, skinsort) capture 2/3 of the AI
    funnel — VIA_RETAILERS is the right pitch framing, not PARTIAL.

    Regression for the gap-based threshold: under the old "attr <
    misattr_max(30)" rule this fell into PARTIAL because attr=33 >= 30."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS
    label, explanation = verdict_for(
        visibility_score=0,
        attribution_score=33,
        category_visibility_score=100,
    )
    assert label == VERDICT_VIA_RETAILERS
    assert "retailer" in explanation.lower()


def test_verdict_via_retailers_does_not_trigger_when_gap_small() -> None:
    """STRONG-adjacent: cat=80, attr=70 -> 10-point gap. Brand IS
    captured first-party most of the time; should NOT be flagged
    VIA_RETAILERS. Falls through to STRONG (both >= 60)."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS, VERDICT_STRONG
    label, _ = verdict_for(
        visibility_score=80,
        attribution_score=70,
        category_visibility_score=80,
    )
    assert label != VERDICT_VIA_RETAILERS
    assert label == VERDICT_STRONG


def test_verdict_via_retailers_does_not_trigger_at_attribution_60() -> None:
    """Boundary: when attribution clears the strong_min(60) floor we
    consider attribution healthy enough that VIA_RETAILERS shouldn't
    fire even if the gap to category is large (cat=100, attr=60 ->
    gap 40, but attr already >= strong_min)."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS
    label, _ = verdict_for(
        visibility_score=20,
        attribution_score=60,
        category_visibility_score=100,
    )
    assert label != VERDICT_VIA_RETAILERS


def test_structured_report_boj_full_scenario_via_retailers() -> None:
    """End-to-end BoJ-class scenario: real-shape probe payloads
    → re-scored category visibility → VIA_RETAILERS verdict →
    competitor brands + retailer hosts surfaced in category block."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        VERDICT_VIA_RETAILERS,
    )
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/products/revive-under-eye-patch",
        product_title="Revive Under Eye Patch: Ginseng + Retinal",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("where can I buy X", visible=True, grounding=[])],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("where can I buy X", found=False)],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},  # upstream missed it
            "raw_runs": [
                _category_run(
                    "best eye patches 2026",
                    excerpt="hydrogel eye patch",
                    grounding_sources=[{"uri": "https://r1/", "title": "sephora.com"}],
                    competitors_appearing=["Patchology", "Wander Beauty"],
                ),
                _category_run(
                    "top eye patches this year",
                    excerpt="Beauty of Joseon Revive Under Eye Patch is described as...",
                    grounding_sources=[
                        {"uri": "https://r2/", "title": "sephora.com"},
                        {"uri": "https://r3/", "title": "oliveyoung.com"},
                    ],
                    competitors_appearing=["Patchology", "Summer Fridays"],
                ),
                _category_run(
                    "best eye patches under $50",
                    excerpt="Beauty of Joseon Revive eye patches...",
                    grounding_sources=[
                        {"uri": "https://r4/", "title": "sephora.com"},
                        {"uri": "https://r5/", "title": "youtube.com"},
                    ],
                    competitors_appearing=["Talika", "Peace Out"],
                ),
            ],
        },
        provider="gemini",
    )
    cat = report["category_visibility"]
    assert cat["score"] == 67  # 2/3 excerpt-matched
    assert cat["upstream_score"] == 0
    # Retailers + competitors surfaced.
    assert any(r["host"] == "sephora.com" for r in cat["retailer_hosts"])
    assert any(r["host"] == "oliveyoung.com" for r in cat["retailer_hosts"])
    brand_map = {b["name"]: b["times_cited"] for b in cat["competitor_brands"]}
    assert brand_map["Patchology"] == 2
    # VIA_RETAILERS verdict + retailer-aware action item present.
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    titles = [a["title"] for a in report["action_items"]]
    assert any("retailer" in t.lower() or "ai-channel funnel" in t.lower() for t in titles)


def test_render_markdown_includes_retailer_and_brand_tables_for_category() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/products/x",
        product_title="X",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best eye patches 2026",
                    excerpt="Beauty of Joseon Revive...",
                    grounding_sources=[{"uri": "https://r/", "title": "sephora.com"}],
                    competitors_appearing=["Patchology"],
                ),
            ],
        },
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert "Where category traffic is being routed" in md
    assert "sephora.com" in md
    assert "Top direct competitors named by Gemini" in md
    assert "Patchology" in md


# ---------------------------------------------------------------------------
# Phase 2e: VIA_RETAILERS reframed to AI-native commerce value prop.
#
# The COSRX BD run revealed the previous wording mis-pitches retail-strong
# brands: "every grounded citation routes through retailers" reads false
# when attribution is 33%, and "Schema.org + sitemap submission" reads as
# SEO hygiene rather than the actual Pivota value (in-Gemini transactions
# + AI-channel attribution growth).
# ---------------------------------------------------------------------------


def test_via_retailers_action_body_uses_gap_percentage_when_attribution_positive() -> None:
    """COSRX-class: attribution=33%, action body should lead with the
    explicit gap (67% routes through retailers), not "every grounded
    citation"."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        VERDICT_VIA_RETAILERS,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/products/p",
        product_title="Charcoal Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("q1", visible=True, grounding=[])],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 33},
            "raw_runs": [
                _attr_run("for sale", found=True,
                          grounding=["https://r/", "https://r2/"]),
                _attr_run("where can I buy", found=False),
                _attr_run("shop online", found=False),
            ],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best face masks 2026",
                    excerpt="COSRX Glow Mask is best for...",
                    grounding_sources=[{"uri": "https://r/", "title": "voguescandinavia.com"}],
                ),
                _category_run(
                    "top face masks this year",
                    excerpt="COSRX Snail Mucin is highlighted...",
                    grounding_sources=[
                        {"uri": "https://r/", "title": "voguescandinavia.com"},
                        {"uri": "https://r2/", "title": "target.com"},
                    ],
                ),
                _category_run(
                    "best face masks under $50",
                    excerpt="COSRX offers various masks under $50...",
                    grounding_sources=[
                        {"uri": "https://r/", "title": "ulta.com"},
                    ],
                ),
            ],
        },
        provider="gemini",
    )
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    first_action = report["action_items"][0]
    body = first_action["body"]
    # Manually compute attribution score the same way the helper does
    # to make the test resilient if the helper signature changes.
    # 1 of 3 runs has in_grounding=True via _attr_run helper → 33%.
    assert "33%" in body or "67%" in body
    assert "every grounded citation" not in body.lower()
    # Value-prop framing present.
    assert "agentic-commerce" in body.lower() or "in-chat checkout" in body.lower()
    assert "25-30%" in body  # forward-looking growth projection


def test_via_retailers_action_body_uses_every_grounded_citation_when_attribution_zero() -> None:
    """BoJ-class: attribution=0, retain the stronger 'every grounded
    citation' wording — it's accurate for that case."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        VERDICT_VIA_RETAILERS,
    )
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://beautyofjoseon.com/p/x",
        product_title="Eye Patch",
        product_vendor="Beauty of Joseon",
        product_type="eye patch",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("q1", visible=True, grounding=[])],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [
                _attr_run("q1", found=False),
                _attr_run("q2", found=False),
                _attr_run("q3", found=False),
            ],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best eye patches",
                    excerpt="Beauty of Joseon Revive...",
                    grounding_sources=[{"uri": "https://r/", "title": "sephora.com"}],
                ),
            ],
        },
        provider="gemini",
    )
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    body = report["action_items"][0]["body"]
    assert "every grounded citation" in body.lower()
    # Value-prop framing still present.
    assert "agentic-commerce" in body.lower() or "in-chat checkout" in body.lower()


def test_via_retailers_suppresses_schema_sitemap_action() -> None:
    """For VIA_RETAILERS verdict, action #5 ('Strengthen schema +
    sitemap') is suppressed — its 'your PDP isn't indexed' framing
    mis-pitches retail-strong brands whose PDPs ARE indexed."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        VERDICT_VIA_RETAILERS,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/products/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("q1", visible=True, grounding=[])],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 33},
            "raw_runs": [
                _attr_run("q1", found=True, grounding=["https://r/"]),
                _attr_run("q2", found=False),
                _attr_run("q3", found=False),
            ],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best face masks 2026",
                    excerpt="COSRX is highlighted...",
                    grounding_sources=[{"uri": "https://r/", "title": "voguescandinavia.com"}],
                ),
            ],
        },
        provider="gemini",
    )
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    titles = [a["title"] for a in report["action_items"]]
    assert not any("schema + sitemap" in t.lower() for t in titles)


def test_via_retailers_explanation_leads_with_value_prop_not_seo() -> None:
    """Verdict explanation should foreground Pivota's two-part value
    prop (canonical PDP for AI-channel attribution + agentic-commerce
    in-chat checkout), not generic SEO/sitemap framing."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS
    label, explanation = verdict_for(
        visibility_score=0,
        attribution_score=33,
        category_visibility_score=100,
    )
    assert label == VERDICT_VIA_RETAILERS
    e = explanation.lower()
    # Value-prop signals present.
    assert "agentic-commerce" in e or "in-chat" in e
    assert "25-30%" in e or "12%" in e
    # Explicitly NOT framed as SEO fix.
    assert "not an seo fix" in e or "complementary to existing retail" in e


# ---------------------------------------------------------------------------
# Phase 2f: Voice reframe — "AI Commerce Readiness Report" + value-prop
# verdicts + new "What Pivota changes" section + industry forward projection
# + methodology scope disclaimer.
# ---------------------------------------------------------------------------


def test_what_pivota_changes_has_discovery_lift_and_checkout_loop() -> None:
    """Phase 2g: what_pivota_changes is now a 2-part block —
    discovery_lift (mechanics + Pivota PDP baseline reference) and
    checkout_loop (6-step chain → merchant Shopify admin)."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/products/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        provider="gemini",
    )
    wpc = report["what_pivota_changes"]
    assert wpc is not None
    assert "today_summary" in wpc
    assert "discovery_lift" in wpc
    assert "checkout_loop" in wpc


def test_discovery_lift_includes_mechanics_and_pivota_baseline_reference() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        PIVOTA_PDP_BASELINE_REFERENCE,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        provider="gemini",
    )
    dl = report["what_pivota_changes"]["discovery_lift"]
    # Pivota PDP baseline figures cited verbatim.
    pr = dl["pivota_reference"]
    assert str(PIVOTA_PDP_BASELINE_REFERENCE["median_visibility"]) in pr
    assert str(PIVOTA_PDP_BASELINE_REFERENCE["median_attribution"]) in pr
    assert str(PIVOTA_PDP_BASELINE_REFERENCE["sample_size_pdps"]) in pr
    # Four mechanics, all shipped — these are the technical reasons
    # the merchant should believe the predicted lift.
    mechanics = dl["mechanics"]
    assert len(mechanics) == 4
    labels = [m.get("label", "").lower() for m in mechanics]
    assert any("canonical" in l and "pdp" in l for l in labels)
    assert any("schema.org" in l for l in labels)
    assert any("sitemap" in l for l in labels)
    assert any("categori" in l for l in labels)
    assert all(m.get("shipped") is True for m in mechanics)
    assert all(m.get("evidence") for m in mechanics)
    # Methodology disclosed honestly — not paired A/B.
    assert dl.get("methodology_note")
    assert "comparative" in dl["methodology_note"].lower()


def test_checkout_loop_chain_has_six_steps_each_with_evidence() -> None:
    """The 6-step chain from grounded answer → merchant Shopify admin.
    Each step carries an evidence reference (file path / test) so BD
    can stand behind the claim."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X",
        merchant_pdp_url="https://x.com/p/1",
        product_title="Y",
        product_vendor=None,
        product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        provider="gemini",
    )
    cl = report["what_pivota_changes"]["checkout_loop"]
    chain = cl["chain"]
    assert len(chain) == 6
    # Steps 1..6 in order, each with shipped=True + evidence.
    for i, s in enumerate(chain, start=1):
        assert s["step"] == i
        assert s.get("label")
        assert s.get("evidence")
        assert s.get("shipped") is True
    # End-to-end test merchant cited in the final-step evidence.
    last_step = chain[-1]
    assert "shopify" in last_step["label"].lower()
    assert "merch_38fa56d5118b9974" in last_step["evidence"]


def test_checkout_loop_platform_coverage_honest_about_shopify_only() -> None:
    """Platform coverage explicitly lists Shopify as shipped + Wix /
    Woo / PrestaShop on roadmap. Honest disclosure protects BD pitch
    from over-promising."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="X",
        merchant_pdp_url="https://x.com/p/1",
        product_title="Y",
        product_vendor=None,
        product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        provider="gemini",
    )
    pc = report["what_pivota_changes"]["checkout_loop"]["platform_coverage"]
    assert "Shopify" in pc["shipped"]
    assert any("woo" in p.lower() for p in pc["roadmap"])
    assert any("wix" in p.lower() for p in pc["roadmap"])
    assert "Q3" in pc["note"] or "roadmap" in pc["note"].lower()


def test_render_markdown_includes_discovery_lift_and_checkout_loop() -> None:
    """Markdown output renders both sub-sections + tables."""
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    # Top-level section + today.
    assert "## What Pivota changes after onboarding" in md
    assert "**Today:**" in md
    # Discovery-lift sub-section with mechanics table.
    assert "Why your AI-channel visibility will improve" in md
    assert "Mechanics that produce that surface" in md
    assert "Schema.org" in md
    assert "sitemap" in md.lower()
    # Pivota baseline reference inline.
    assert "67/100" in md  # median_visibility
    assert "50/100" in md  # median_attribution
    # Checkout-loop sub-section with 6-step chain table.
    assert "How in-chat checkout closes the loop" in md
    assert "Shopify admin" in md
    assert "merch_38fa56d5118b9974" in md
    # Platform coverage line + outcome.
    assert "Platform coverage" in md
    assert "Shopify" in md
    assert "Roadmap" in md


def test_render_markdown_uses_new_title_and_intro() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert md.startswith("# AI Commerce Readiness Report — COSRX")
    # Intro paragraph framing the two questions
    assert "25-30%" in md
    assert "the second question is what pivota addresses" in md.lower()


def test_render_markdown_methodology_scope_disclaimer() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="X",
        merchant_pdp_url="https://x.com/p",
        product_title="Y",
        product_vendor=None,
        product_type=None,
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    assert "**Scope.**" in md
    # Tells the merchant what this report does NOT measure.
    assert "does NOT measure" in md
    # Names the categories explicitly so retail-strong brands can
    # frame this as complementary, not redundant.
    assert "D2C web traffic" in md
    assert "retail sell-through" in md


def test_industry_context_beauty_has_forward_projection() -> None:
    from services.agent_center_bd_report_service import _INDUSTRY_CONTEXT_BY_CATEGORY
    beauty = _INDUSTRY_CONTEXT_BY_CATEGORY["beauty"]
    assert beauty["forward_projection"] is not None
    assert "25-30%" in beauty["forward_projection"]
    assert "2028" in beauty["forward_projection"]


def test_render_markdown_includes_forward_projection_for_beauty() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )
    report = build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        provider="gemini",
    )
    md = render_markdown_from_structured(report)
    # Beauty category triggers the "By 2028..." line in industry context.
    assert "_24-month projection:_" in md
    assert "2028" in md


def test_verdict_explanations_contain_value_prop_signals_for_all_labels() -> None:
    """Every verdict explanation (across the 4 reframed labels) carries
    a value-prop signal — agentic-commerce, in-chat, or projected
    growth — so the BD framing is consistent."""
    from services.agent_center_bd_report_service import verdict_for
    cases = [
        (0, 0, None),       # INVISIBLE
        (70, 10, None),     # MISATTRIBUTED
        (70, 80, None),     # STRONG
        (40, 50, None),     # PARTIAL
    ]
    for vis, attr, cat in cases:
        _, explanation = verdict_for(vis, attr, category_visibility_score=cat)
        e = explanation.lower()
        assert (
            "agentic-commerce" in e
            or "in-chat" in e
            or "25-30%" in e
        ), f"verdict for ({vis},{attr},{cat}) lacks value-prop signal: {explanation[:120]}"


# ---------------------------------------------------------------------------
# Phase 2h: Probe-mode honesty + onboarding_sequence with test-merchant
# validation. Each step in the onboarding sequence must cite a concrete
# test-merchant artifact so BD can show evidence, not just describe steps.
# ---------------------------------------------------------------------------


def _wpc(report: Dict[str, Any]) -> Dict[str, Any]:
    return report["what_pivota_changes"]


def _basic_report() -> Dict[str, Any]:
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="COSRX",
        merchant_pdp_url="https://www.cosrx.com/products/p",
        product_title="Mask",
        product_vendor="COSRX",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 33}, "raw_runs": []},
        provider="gemini",
    )


def test_discovery_lift_pivota_reference_specifies_named_product_probe_modes() -> None:
    """The pivota_reference paragraph must lead with 'named-product
    buyer-intent queries' so the merchant cannot misread the 67/50
    figures as a category-level promise."""
    pr = _wpc(_basic_report())["discovery_lift"]["pivota_reference"]
    assert "named-product" in pr.lower()
    assert "buyer-intent" in pr.lower()


def test_discovery_lift_pivota_reference_points_to_test_merchant_artifact() -> None:
    """The pivota_reference cites the test-merchant audit artifact path
    so BD can hand the merchant a paired-audit example."""
    pr = _wpc(_basic_report())["discovery_lift"]["pivota_reference"]
    assert "test-merchant-bd-audit.md" in pr


def test_methodology_note_names_specific_probe_modes_used_in_baseline() -> None:
    """methodology_note must name `open_product_visibility_test` AND
    `merchant_store_attribution_test` — exactly the two scan modes
    `agent_center_pivota_pdp_baseline.py` runs."""
    note = _wpc(_basic_report())["discovery_lift"]["methodology_note"]
    assert "open_product_visibility_test" in note
    assert "merchant_store_attribution_test" in note


def test_methodology_note_disclaims_category_level_comparison() -> None:
    """methodology_note must explicitly say the baseline is NOT
    comparable to category_visibility_score."""
    note = _wpc(_basic_report())["discovery_lift"]["methodology_note"]
    assert "category_visibility_score" in note
    assert "NOT" in note or "not" in note


def test_onboarding_sequence_present_with_five_steps() -> None:
    seq = _wpc(_basic_report())["onboarding_sequence"]
    assert seq
    assert seq.get("title")
    assert seq.get("intro")
    assert len(seq["steps"]) == 5
    # Steps numbered 1..5 in order.
    for i, s in enumerate(seq["steps"], start=1):
        assert s["step"] == i


def test_onboarding_sequence_each_step_has_test_merchant_validation() -> None:
    """Every step must carry a non-empty test_merchant_validation
    field — that's the whole point of the section."""
    seq = _wpc(_basic_report())["onboarding_sequence"]
    for s in seq["steps"]:
        assert s.get("test_merchant_validation"), (
            f"step {s.get('step')} ({s.get('name')}) missing "
            f"test_merchant_validation"
        )


def test_onboarding_sequence_test_merchant_block_present() -> None:
    """The merchant_id + shop_domain are surfaced as a structured
    field so the UI can render the playground identifier separately."""
    from services.agent_center_bd_report_service import TEST_MERCHANT_REFERENCE
    seq = _wpc(_basic_report())["onboarding_sequence"]
    tm = seq["test_merchant"]
    assert tm["merchant_id"] == TEST_MERCHANT_REFERENCE["merchant_id"]
    assert tm["shop_domain"] == TEST_MERCHANT_REFERENCE["shop_domain"]


def test_onboarding_sequence_does_not_promise_reserved_agents_as_shipped_agents() -> None:
    """The 3 RESERVED agents (Offer Execution / Checkout Verification
    / GMV Attribution) must NOT appear as shipped-agent step names.
    Steps 4 + 5 cover their function manually + are tagged
    `manual_today=True`."""
    seq = _wpc(_basic_report())["onboarding_sequence"]
    reserved = {"offer execution", "checkout verification", "gmv attribution"}
    for s in seq["steps"]:
        name_lower = s.get("name", "").lower()
        # If the name contains a reserved agent label, the step must be
        # `manual_today=True` (not promised as one-click).
        if any(r in name_lower for r in reserved):
            assert s.get("manual_today") is True, (
                f"step {s.get('step')} names a RESERVED agent but is "
                f"not flagged manual_today: {s}"
            )
    # roadmap_note must explicitly disclose the RESERVED status.
    assert "RESERVED" in seq["roadmap_note"]


def test_render_markdown_contains_onboarding_sequence_section() -> None:
    from services.agent_center_bd_report_service import (
        render_markdown_from_structured,
    )
    md = render_markdown_from_structured(_basic_report())
    # Section heading + the 2 actually-shipped agents named explicitly.
    assert "Onboarding sequence" in md
    assert "Demand Test" in md
    assert "SKU Match" in md
    assert "Shopify OAuth" in md
    # Test merchant playground line.
    assert "merch_38fa56d5118b9974" in md
    assert "shop.myshopify.com" in md
    # Manual-today steps render with the wrench badge.
    assert "🔧 manual today" in md
    # Roadmap_note explains RESERVED placeholders.
    assert "RESERVED" in md
