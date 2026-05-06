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
    assert "highest-impact" in explanation  # this is the BD pitch sweet spot


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
    assert "# AI Visibility Report — Glossier" in report
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
