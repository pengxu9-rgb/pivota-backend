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
    [{vertexaisearch.cloud.google.com: 2}] — useless. The fix reads the
    title ("Sephora", "Olive Young Global", …) and resolves it to the
    canonical DOMAIN so the rollup keys are consistent hosts (and OpenAI
    web_search page-headline titles never leak in as fake hosts)."""
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
    # Competitors keyed by resolved DOMAIN: Sephora(2) + Olive Young Global(1)
    # + YesStyle(1). No vertexaisearch entries — those are filtered as
    # redirectors; display-name titles resolve via the cited-host registry.
    assert competitors == Counter({
        "sephora.com": 2,
        "oliveyoungglobal.com": 1,
        "yesstyle.com": 1,
    })


def test_extract_cited_hosts_openai_titles_roll_up_by_domain_not_headline() -> None:
    """Regression (#7): OpenAI web_search returns sources with a REAL uri plus a
    page-headline title ("The 15 Best Hair Butters … | Marie Claire"). The
    rollup must key by the resolved domain, never the headline — otherwise the
    headline leaks into top_cited_hosts as a fake host."""
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [
        _run_with_sources([
            {"uri": "https://www.stylecraze.com/articles/best-hair-butters/",
             "title": "The 15 Best Hair Butters, Hairstylist’s Picks – 2026"},
            {"uri": "https://www.hwahae.com/en/products/2121145",
             "title": "Anuko NOURISHING HAIR BUTTER [shea butter & green tea]"},
            {"uri": "https://anukoofficial.com/product/hair-butter",
             "title": "Anuko Official"},  # merchant own-site
        ]),
    ]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs,
        merchant_host="anukoofficial.com",
        merchant_brand="Anuko",
    )
    assert runs_with_citations == 1
    # Keyed by domain; the headline is NOT a host. hwahae is a competitor host;
    # the Anuko-branded sources match the merchant (brand/own host).
    assert "stylecraze.com" in competitors
    assert all(" " not in h for h in competitors), competitors
    assert not any("best hair butter" in h.lower() for h in competitors)


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
    # Phase C-4 (PR-A): legacy fallback path (no evidence dict supplied)
    # emits a pitch-free generic sentence about indexing.
    assert "indexed" in explanation.lower() or "grounded" in explanation.lower()


def test_verdict_misattributed_when_visible_but_no_attribution() -> None:
    from agent_center_bd_external_merchant import _verdict_for
    label, explanation = _verdict_for(visibility_score=70, attribution_score=10)
    assert label == "VISIBLE BUT MISATTRIBUTED"
    # Phase C-4 (PR-A): pitch-free diagnostic. The BD value prop now
    # lives only in `what_pivota_changes`, not the verdict text.
    assert "third-party" in explanation.lower() or "competitors" in explanation.lower()


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
    # Verdict picked (rendered Title-Case in the heading now, so match case-insensitively)
    report_upper = report.upper()
    assert (
        "VISIBLE BUT MISATTRIBUTED" in report_upper
        or "INVISIBLE" in report_upper
        or "PARTIAL" in report_upper
    )
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
    # Phase C-4 (PR-A): STRONG action body now leads with cited-ratio
    # data; "monitoring" stays in the title, drift+schema watch-out
    # framing in the body.
    assert "monitoring" in items[0]["title"].lower()
    body_lower = items[0]["body"].lower()
    assert "drift" in body_lower or "schema regression" in body_lower


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
    competitor table because BD wants brand-wide narrative.

    Q-P1-3: hosts now carry confidence tier + per-probe breakdown.
    Buyer-intent-only hosts → grounded_competitor."""
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
    # Host normalization → lowercase. Buyer-intent only → grounded_competitor.
    top = out[0]
    assert top["host"] == "sephora"
    assert top["times_cited"] == 5
    assert top["confidence"] == "grounded_competitor"
    assert top["source"] == "buyer_intent"
    assert top["buyer_intent_cited"] == 5
    assert top["category_cited"] == 0
    # Top-15 ordering: Sephora (5), Ulta (1), YesStyle (1).
    assert len(out) == 3
    assert [e["host"] for e in out] == ["sephora", "ulta", "yesstyle"]


@pytest.mark.asyncio
async def test_run_brand_report_caps_at_50_products(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Cost guard: hard cap on per-call product count.

    Spec §I — cap raised from 5 → 50 SKUs since credit pre-flight
    (POST /api/audits/preview) is now the authoritative cost gate.
    The hard cap stays as a defense-in-depth ceiling.
    """
    from services.agent_center_bd_report_service import run_brand_report
    products = [
        {"title": f"P{i}", "pdp_url": f"https://x.com/p/{i}", "product_type": "thing"}
        for i in range(51)
    ]
    with pytest.raises(ValueError, match="capped at 50"):
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
        provider="gemini",
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
async def test_run_brand_report_us_shopper_fans_out_to_gemini_and_chatgpt(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    from services import agent_center_bd_report_service as bd

    calls = []

    async def _fake_probe(**kwargs):
        calls.append((kwargs.get("provider"), kwargs.get("scan_mode")))
        return {
            "scan_mode": kwargs.get("scan_mode"),
            "provider": kwargs.get("provider"),
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
        ],
        coverage_profile="us_shopper",
        include_category_visibility=False,
    )

    assert calls == [
        ("gemini", "open_product_visibility_test"),
        ("gemini", "merchant_store_attribution_test"),
        ("chatgpt", "open_product_visibility_test"),
        ("chatgpt", "merchant_store_attribution_test"),
    ]
    assert out["providers"] == ["gemini", "chatgpt"]
    assert out["verify_providers"] == ["deepseek"]
    assert out["pending_engine_support"] == []
    assert out["per_product"][0]["citation_by_provider"].keys() == {
        "gemini", "chatgpt",
    }


@pytest.mark.asyncio
async def test_run_brand_report_passes_default_and_override_chatgpt_models(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    from services import agent_center_bd_report_service as bd

    calls = []

    async def _fake_probe(**kwargs):
        calls.append({
            "provider": kwargs.get("provider"),
            "scan_mode": kwargs.get("scan_mode"),
            "model": kwargs.get("model"),
            "model_is_override": kwargs.get("model_is_override"),
        })
        return {
            "scan_mode": kwargs.get("scan_mode"),
            "provider": kwargs.get("provider"),
            "model": kwargs.get("model"),
            "model_is_override": kwargs.get("model_is_override", False),
            "scores": {"visibility_score": 50},
            "raw_runs": [],
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)

    default_out = await bd.run_brand_report(
        merchant_name="Brand X",
        merchant_domain="brandx.com",
        products=[
            {"title": "P1", "pdp_url": "https://x.com/p/1", "product_type": "thing"},
        ],
        coverage_profile="us_shopper",
        include_category_visibility=False,
    )

    default_chatgpt = [c for c in calls if c["provider"] == "chatgpt"]
    assert {c["model"] for c in default_chatgpt} == {"chat-latest"}
    assert {c["model_is_override"] for c in default_chatgpt} == {False}
    assert default_out["provider_models"]["chatgpt"]["model"] == "chat-latest"
    assert default_out["model_is_override"] is False
    assert default_out["per_product"][0]["model_is_override"] is False

    calls.clear()
    override_out = await bd.run_brand_report(
        merchant_name="Brand X",
        merchant_domain="brandx.com",
        products=[
            {"title": "P1", "pdp_url": "https://x.com/p/1", "product_type": "thing"},
        ],
        coverage_profile="us_shopper",
        model_overrides={"chatgpt": "gpt-5.5-mini"},
        include_category_visibility=False,
    )

    override_chatgpt = [c for c in calls if c["provider"] == "chatgpt"]
    assert {c["model"] for c in override_chatgpt} == {"gpt-5.5-mini"}
    assert {c["model_is_override"] for c in override_chatgpt} == {True}
    assert override_out["provider_models"]["chatgpt"] == {
        "model": "gpt-5.5-mini",
        "default_model": "chat-latest",
        "model_is_override": True,
    }
    assert override_out["model_is_override"] is True
    assert override_out["per_product"][0]["model_is_override"] is True


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
        provider="gemini",
        include_category_visibility=False,
    )
    # Sephora is cited 1× on each of 2 products = 2 total brand-wide.
    # Q-P1-3: rollup entry now carries confidence + source breakdown.
    # Buyer-intent only → grounded_competitor; the cited host resolves to its
    # canonical domain (sephora.com), not the bare display title.
    competitors = out["cross_product_competitors"]
    assert competitors[0]["host"] == "sephora.com"
    assert competitors[0]["times_cited"] == 2
    assert competitors[0]["confidence"] == "grounded_competitor"
    assert competitors[0]["source"] == "buyer_intent"


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
    # Distilled NBA appears before the longer action queue.
    assert "## What should you do next?" in md
    assert "**First move:**" in md
    assert "Why this is the leak" in md
    assert "Gap read" in md
    assert "Do yourself this week" in md
    assert "Use Pivota for" in md
    assert "How to track" in md
    assert md.find("## What should you do next?") < md.find("## Recommended actions")
    # Recommended actions section + at least one severity-tagged item
    assert "## Recommended actions" in md
    assert "_(severity:" in md


def test_render_markdown_includes_reaudit_delta_before_next_best_action() -> None:
    from services.agent_center_bd_report_service import render_markdown_from_structured
    report = _basic_report()
    report["merchant_view"]["reaudit_delta"] = {
        "is_first_audit": False,
        "headline": "No material change since your last audit 14 days ago — keep the current plan running.",
        "movements": [],
        "tracked_metric_results": [
            {
                "metric": "First-party citation rate on the same failed buyer questions.",
                "status": "unchanged",
                "note": "Mapped to First-party citation; no material movement.",
            }
        ],
    }

    md = render_markdown_from_structured(report)

    assert "## Since your last audit" in md
    assert "No material change since your last audit 14 days ago" in md
    assert "First-party citation rate on the same failed buyer questions" in md
    assert md.find("## Since your last audit") < md.find("## What should you do next?")


def test_render_markdown_includes_owned_buyer_path_play_checklist() -> None:
    from services.agent_center_bd_report_service import (
        build_structured_report,
        render_markdown_from_structured,
    )

    report = build_structured_report(
        merchant_name="Ownist",
        merchant_pdp_url="https://ownist.com/products/triple-shine-grape",
        product_title="Ownist Triple Shine Grape",
        product_vendor="Ownist",
        product_type="beauty supplement",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 100},
            "raw_runs": [_vis_run("q1", visible=True, grounding=["https://x.com"])],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 100},
            "raw_runs": [_attr_run("q1", found=True, grounding=["https://ownist.com/x"])],
        },
        provider="gemini",
    )
    report["merchant_view"]["next_best_action"] = {
        "canonical_page_play": {
            "lane": "vitamin c collagen jelly",
            "controllers": ["cogentsteps.net", "medsysgroup.com", "hellokoop.com"],
            "controller_strategy": "canonical_source_vacuum",
            "controller_strategy_label": "Canonical-source vacuum",
            "controller_profile": {
                "operator_focus": "AI grounding is filling a canonical-source gap with weak third-party hosts.",
                "exposure_read": (
                    "Read this as a weak citation trail and canonical-source vacuum, "
                    "not proof that material buyer traffic is going to those hosts."
                ),
            },
            "exposure_read": (
                "Read this as a weak citation trail and canonical-source vacuum, "
                "not proof that material buyer traffic is going to those hosts."
            ),
            "moves": [
                {
                    "type": "canonical_source_authority",
                    "operator_action": (
                        "Make the official page the source AI can cite for vitamin c collagen jelly."
                    ),
                    "why": "The cited route is weak third-party citation evidence.",
                },
                {
                    "type": "direct_buy_reason",
                    "operator_action": (
                        "After the official page is source-ready, add first-order offer, starter + replenishment bundle, "
                        "subscription incentive, and why-buy-direct proof."
                    ),
                },
            ],
            "checkout_readiness": "Make the page cited, buyable, and agent-checkout ready.",
            "economics_policy": "Mechanics only; do not recommend exact discount depths.",
        },
        "sideways_wedge": {
            "recommended_beachhead_lane": {
                "query": "vitamin c collagen jelly",
            },
            "why_this_lane_not_the_head_prompt": (
                "Start with \"vitamin c collagen jelly\" before "
                "\"healthy snacks collagen jelly\" because it is product-specific."
            ),
            "do_not_chase_yet": [
                {"query": "healthy snacks collagen jelly"},
            ],
        },
    }

    md = render_markdown_from_structured(report)

    assert "## Owned buyer path play" in md
    assert "Canonical-source vacuum" in md
    assert "`vitamin c collagen jelly`" in md
    assert "`cogentsteps.net`" in md
    assert "Sideways demand wedge" in md
    assert "Beachhead lane: `vitamin c collagen jelly`" in md
    assert "`healthy snacks collagen jelly`" in md
    assert "source AI can cite" in md
    assert "Exposure read" in md
    assert "Economics guard" in md
    assert "beat cogentsteps" not in md.lower()


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
    """The BoJ-class case: 2/3 runs surface the brand in a grounding
    source title (the editorial site published an article naming it).
    Re-scored: 67/100. Note: per the brand-match tightening fix, the
    brand must appear in the source TITLE (or the merchant URL must
    be in grounding chunks) — excerpt-only no longer credits because
    Gemini's evidence excerpt can hallucinate brand names that aren't
    actually in any grounded source."""
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
            grounding_sources=[{"uri": "https://r/", "title": "Sephora — Beauty of Joseon Revive Eye Patch"}],
        ),
        _category_run(
            "best eye patches under $50",
            excerpt="Beauty of Joseon Revive Under Eye Patches for Wrinkles...",
            grounding_sources=[{"uri": "https://r/", "title": "YouTube: Beauty of Joseon haul"}],
        ),
    ]
    score, details = score_category_visibility(
        runs,
        merchant_host="beautyofjoseon.com",
        merchant_brand="Beauty of Joseon",
    )
    assert score == 67  # 2/3 matched via title
    assert details[0]["matched"] is False
    assert details[1]["matched"] is True
    assert details[1]["title_match"] is True
    assert details[2]["matched"] is True
    assert details[2]["title_match"] is True


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
                        # Brand-named source credits title_match (post brand-match
                        # tightening fix); bare retailer host makes it into
                        # retailer_hosts for the funnel-pressure framing.
                        {"uri": "https://r2a/", "title": "Sephora — Beauty of Joseon Revive Eye Patch"},
                        {"uri": "https://r2b/", "title": "sephora.com"},
                        {"uri": "https://r3/", "title": "oliveyoung.com"},
                    ],
                    competitors_appearing=["Patchology", "Summer Fridays"],
                ),
                _category_run(
                    "best eye patches under $50",
                    excerpt="Beauty of Joseon Revive eye patches...",
                    grounding_sources=[
                        {"uri": "https://r4a/", "title": "Sephora: Beauty of Joseon under-$50 picks"},
                        {"uri": "https://r4b/", "title": "sephora.com"},
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
                    grounding_sources=[{"uri": "https://r/", "title": "Vogue Scandinavia: COSRX Glow Mask"}],
                ),
                _category_run(
                    "top face masks this year",
                    excerpt="COSRX Snail Mucin is highlighted...",
                    grounding_sources=[
                        {"uri": "https://r/", "title": "Vogue Scandinavia: COSRX Snail Mucin"},
                        {"uri": "https://r2/", "title": "Target — face masks"},
                    ],
                ),
                _category_run(
                    "best face masks under $50",
                    excerpt="COSRX offers various masks under $50...",
                    grounding_sources=[
                        {"uri": "https://r/", "title": "Ulta: COSRX face masks"},
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
    # Phase C-4 (PR-A): action body is now pitch-free — no agentic-
    # commerce / 25-30% / Pivota in diagnostic surfaces. The retailer
    # name(s) the brand is losing the funnel to should appear instead.
    assert "agentic-commerce" not in body.lower()
    assert "25-30%" not in body


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
                    grounding_sources=[{"uri": "https://r/", "title": "Sephora: Beauty of Joseon Revive"}],
                ),
            ],
        },
        provider="gemini",
    )
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    body = report["action_items"][0]["body"]
    assert "every grounded citation" in body.lower()
    # Phase C-4 (PR-A): pitch macros stripped from action bodies — they
    # live only in `what_pivota_changes` going forward.
    assert "agentic-commerce" not in body.lower()
    assert "25-30%" not in body


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
                    grounding_sources=[{"uri": "https://r/", "title": "Vogue Scandinavia: COSRX face mask"}],
                ),
            ],
        },
        provider="gemini",
    )
    assert report["verdict"]["label"] == VERDICT_VIA_RETAILERS
    titles = [a["title"] for a in report["action_items"]]
    assert not any("schema + sitemap" in t.lower() for t in titles)


def test_via_retailers_explanation_is_pitch_free_diagnostic() -> None:
    """Phase C-4 (PR-A): verdict explanation is pure diagnostic prose.
    The Pivota value prop (agentic-commerce / in-chat checkout / 12% →
    25-30% market sizing) lives only in `what_pivota_changes` and
    `merchant_view.pivota_value_prop`. The verdict text describes the
    diagnostic state — that the brand is findable in category queries
    but not capturing first-party attribution."""
    from services.agent_center_bd_report_service import verdict_for, VERDICT_VIA_RETAILERS
    label, explanation = verdict_for(
        visibility_score=0,
        attribution_score=33,
        category_visibility_score=100,
    )
    assert label == VERDICT_VIA_RETAILERS
    e = explanation.lower()
    # Pitch tokens explicitly absent from diagnostic.
    assert "agentic-commerce" not in e
    assert "25-30%" not in explanation
    assert "complementary to existing retail" not in e
    assert "not an seo fix" not in e
    # The diagnostic state IS described — brand findable, attribution gap.
    assert "category" in e or "category-level" in e
    assert "attribution" in e or "captur" in e


@pytest.mark.parametrize(
    ("category_match_details", "expected_signal"),
    [
        (
            [{"in_grounding": True}],
            "Your URL appears in some category-level grounded sources",
        ),
        (
            [{"title_match": True}],
            "Your brand was named in some category-level grounded source titles",
        ),
        (
            [{"excerpt_match": True, "matched": True}],
            "Your brand was mentioned in category-level answer prose",
        ),
        (
            [],
            (
                "Your brand's category presence was not tied to specific "
                "grounded sources in this analysis"
            ),
        ),
    ],
)
def test_via_retailers_evidence_explanation_is_grammatical(
    category_match_details,
    expected_signal,
) -> None:
    from services.agent_center_bd_report_service import (
        verdict_for,
        VERDICT_VIA_RETAILERS,
    )
    label, explanation = verdict_for(
        visibility_score=0,
        attribution_score=20,
        category_visibility_score=50,
        evidence={
            "attribution_runs_total": 5,
            "merchant_cited_runs": 1,
            "top_retailers": [],
            "category_score": 50,
            "gap_pct": 30,
            "category_match_details": category_match_details,
        },
    )
    assert label == VERDICT_VIA_RETAILERS
    assert expected_signal in explanation
    assert ", and your own URL appeared in few of the buyer-intent queries." in explanation
    assert ", but in few buyer-intent queries" not in explanation
    assert "category-visibility score is 50/100 (we don't have" not in explanation


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
    # Phase 2i: discovery_lift now expresses three layers (grounded LLM /
    # agent-direct API / editorial co-citation). Layer 1 carries the
    # SEO-flavored mechanics; Layer 2 carries the agent-direct API
    # mechanics; Layer 3 has none (outside Pivota's lever).
    layers = dl["layers"]
    # Phase 2j: Layer 3 (editorial co-citation) dropped — outside
    # Pivota's lever, dilutes the pitch. Just Layer 1 + Layer 2.
    assert len(layers) == 2
    layer_names = [l["name"].lower() for l in layers]
    assert any("layer 1" in n and "grounded" in n for n in layer_names)
    assert any("layer 2" in n and "agent-direct" in n for n in layer_names)
    assert not any("layer 3" in n for n in layer_names)
    # Pivota PDP baseline figures cited inside Layer 1's pivota_status.
    layer1 = next(l for l in layers if "layer 1" in l["name"].lower())
    status1 = layer1["pivota_status"]
    assert str(PIVOTA_PDP_BASELINE_REFERENCE["cited_count"]) in status1
    assert str(PIVOTA_PDP_BASELINE_REFERENCE["succeeded_count"]) in status1
    # Layer 1 carries the four canonical-PDP / SEO-style mechanics.
    layer1_mechs = layer1["mechanics"]
    assert len(layer1_mechs) == 4
    labels1 = [m.get("label", "").lower() for m in layer1_mechs]
    assert any("canonical" in l and "pdp" in l for l in labels1)
    assert any("schema.org" in l for l in labels1)
    assert any("sitemap" in l for l in labels1)
    # Layer 2 carries agent-direct API mechanics.
    layer2 = next(l for l in layers if "layer 2" in l["name"].lower())
    layer2_mechs = layer2["mechanics"]
    assert len(layer2_mechs) >= 3
    labels2 = [m.get("label", "").lower() for m in layer2_mechs]
    assert any("acp" in l and "/orders/create" in l for l in labels2)
    assert any("ucp" in l for l in labels2)
    assert any("agent_shop_gateway" in l or "agent/shop/v1" in l for l in labels2)
    assert all(m.get("shipped") is True for m in layer2_mechs)
    # Methodology disclosed.
    assert dl.get("methodology_note")
    assert "layer 1" in dl["methodology_note"].lower()


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
    assert "internal_shopify_test_merchant" in last_step["evidence"]


def test_checkout_loop_platform_coverage_multi_platform_messaging() -> None:
    """PR-10a (post-Grüns review): platform coverage now reflects
    actual code state — Shopify + WooCommerce + BigCommerce all
    shipped end-to-end (per routes/order_routes.py adapters); Wix
    audit-ready; custom storefronts via lightweight integration.

    Replaces the prior 'Shopify-only honest disclosure' framing,
    which was actively misleading since Woo + BC adapters were
    already shipped."""
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
    # All three real-adapter platforms now reported as shipped
    assert "Shopify" in pc["shipped"]
    assert "WooCommerce" in pc["shipped"]
    assert "BigCommerce" in pc["shipped"]
    # Wix is audit-only (writeback adapter on Q3 roadmap)
    assert "Wix" in (pc.get("audit_only") or [])
    # Custom integration disclosure exists
    assert pc.get("custom_integration"), "custom_integration tier expected"
    # Note copy reflects multi-platform stance, not Shopify-only
    note_lower = pc["note"].lower()
    assert "multi-platform" in note_lower
    assert "woocommerce" in note_lower
    # Should NOT claim Woo / BC are roadmap (they're shipped)
    roadmap = pc.get("roadmap") or []
    assert "WooCommerce" not in roadmap
    assert "BigCommerce" not in roadmap


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
    # Multi-layer discovery framing (Phase 2j: Layer 3 dropped — outside Pivota's lever).
    assert "Why your AI-channel discoverability will improve" in md
    assert "Layer 1" in md and "Grounded LLM citation" in md
    assert "Layer 2" in md and "Agent-direct API queries" in md
    assert "Layer 3" not in md
    # Layer 1 mechanics (SEO).
    assert "Schema.org" in md
    assert "sitemap" in md.lower()
    # Layer 2 mechanics (agent-direct API surfaces).
    assert "/orders/create" in md
    assert "ucp" in md.lower()
    assert "agent_shop_gateway" in md or "agent/shop/v1" in md
    # Indexing-up framing for Layer 1 — must be honest, not buried.
    assert "indexing-up" in md.lower()
    assert "30-90 day" in md
    # Checkout-loop sub-section with 6-step chain table.
    assert "How in-chat checkout closes the loop" in md
    assert "Shopify admin" in md
    assert "internal_shopify_test_merchant" in md
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


def test_verdict_explanations_are_pitch_free_for_all_labels() -> None:
    """Phase C-4 (PR-A) inverted contract: every verdict explanation
    is pitch-free across all labels. Pitch tokens (agentic-commerce,
    in-chat, 25-30%) live exclusively in `what_pivota_changes` and
    PR-B's `merchant_view.pivota_value_prop`. This is the analogue of
    `tests/test_diagnostic_no_pitch.py` for the no-evidence fallback
    path that this script-style helper still exercises."""
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
        for token in ("agentic-commerce", "in-chat", "25-30%", "pivota"):
            assert token not in e, (
                f"pitch token {token!r} leaked into ({vis},{attr},{cat}) "
                f"verdict explanation: {explanation[:120]}"
            )


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


@pytest.mark.asyncio
async def test_attach_reaudit_delta_fetches_full_prior_report(monkeypatch) -> None:
    from db import merchant_audit_runs as mar
    from services.agent_center_bd_report_service import _attach_reaudit_delta

    prior = _basic_report()
    current = _basic_report()
    prior["merchant_view"]["headline"]["scores"]["attribution"] = 35
    prior["verdict"]["attribution_score"] = 35
    current["merchant_view"]["headline"]["scores"]["attribution"] = 55
    current["verdict"]["attribution_score"] = 55

    async def fake_fetch(*, run_id: str) -> Dict[str, Any]:
        assert run_id == "prior-run"
        return {"report_jsonb": {"per_product": [prior]}}

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    await _attach_reaudit_delta(
        current,
        merchant_id="merchant_123",
        prior_runs=[
            {
                "run_id": "prior-run",
                "status": "succeeded",
                "requested_at": "2026-05-01T00:00:00+00:00",
            }
        ],
    )

    delta = current["merchant_view"]["reaudit_delta"]
    movement = next(m for m in delta["movements"] if m["signal"] == "attribution")
    assert movement["direction"] == "improved"
    assert movement["is_material"] is True

    # Audit→action→outcome loop rides the same attach: a legacy prior report
    # carries no win_plan/outreach targets, so the section degrades honestly
    # (present, not available) instead of fabricating host outcomes.
    outcomes = current["merchant_view"]["outreach_outcomes"]
    assert outcomes["is_first_audit"] is False
    assert outcomes["available"] is False
    assert outcomes["targets"] == []


@pytest.mark.asyncio
async def test_attach_reaudit_delta_attaches_outreach_outcomes_from_prior_targets(
    monkeypatch,
) -> None:
    """A per-SKU prior report with win-plan targets + a per-SKU-shaped current
    report produce classified outreach outcomes on the merchant view."""
    from db import merchant_audit_runs as mar
    from db import merchant_tasks as mt
    from services.agent_center_bd_report_service import _attach_reaudit_delta

    prior_report = {
        "win_plan": {
            "sku_plans": [
                {
                    "sku_key": "sku1",
                    "losing_queries": [
                        {
                            "query": "best hair care",
                            "axis": "category",
                            "grounds_in": [
                                {"host": "hwahae.com", "outreach": {"state": "draft_ready"}}
                            ],
                        }
                    ],
                }
            ]
        },
        "authority_map": {
            "hosts": [],
            "host_attribution_summary": {"endorsement_hosts": []},
        },
    }
    current = _basic_report()
    current["per_sku_reports"] = [
        {"sku_key": "sku1", "failing_prompts": []}
    ]
    current["authority_map"] = {
        "skus": [{"sku_key": "sku1", "authority_hosts": []}],
        "hosts": [{"host": "hwahae.com", "cites_exact_sku": True,
                   "citation_role": "independent_endorsement",
                   "prompts_cited_count": 2}],
        "host_attribution_summary": {
            "endorsement_hosts": ["hwahae.com"],
            "endorsement_category_hosts": ["hwahae.com"],
        },
    }

    async def fake_fetch(*, run_id: str) -> Dict[str, Any]:
        return {"report_jsonb": prior_report}

    async def fake_tasks(**kwargs) -> list:
        assert kwargs.get("status_filter") == ["done"]
        return [
            {
                "title": "Pitch hwahae.com",
                "status": "done",
                "completed_at": "2026-07-10T00:00:00+00:00",
                "evidence_jsonb": {"target_host": "hwahae.com"},
            }
        ]

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mt, "list_tasks_for_merchant", fake_tasks)

    await _attach_reaudit_delta(
        current,
        merchant_id="merchant_123",
        prior_runs=[
            {
                "run_id": "prior-run",
                "status": "succeeded",
                "requested_at": "2026-07-08T00:00:00+00:00",
            }
        ],
    )

    outcomes = current["merchant_view"]["outreach_outcomes"]
    assert outcomes["available"] is True
    row = next(t for t in outcomes["targets"] if t["host"] == "hwahae.com")
    # hwahae entered the endorsement set — reported as a win even though the
    # basis is unknown (endorsement transitions are basis-independent facts).
    assert row["outcome"] == "won"
    assert row["reason"] == "host_now_endorses"
    # The merchant's done-marked task is surfaced as fact, never causation.
    assert row["merchant_action"]["title"] == "Pitch hwahae.com"
    assert "caused" not in row["merchant_action"]["note"].lower()


def _per_sku_brand_report(*, basis_id: str = "sel_w2") -> Dict[str, Any]:
    """Minimal per-SKU brand-report shape: prompt_basis rides on the per-SKU
    rows (W2.1), win_plan/authority_map at the root — production mode."""
    return {
        "audit_mode": "per_sku",
        "timestamp": "2026-07-14T00:00:00+00:00",
        "per_sku_reports": [
            {
                "sku_key": "sku1",
                "prompt_basis": {"selected_set_id": basis_id},
                "failing_prompts": [],
                "opportunity": {"per_prompt": [{"query": "best hair care"}]},
            }
        ],
        "authority_map": {
            "skus": [{"sku_key": "sku1", "authority_hosts": []}],
            "hosts": [
                {
                    "host": "hwahae.com",
                    "citation_role": "independent_endorsement",
                    "cites_exact_sku": True,
                    "prompts_cited_count": 2,
                }
            ],
            "host_attribution_summary": {
                "endorsement_hosts": ["hwahae.com"],
                "endorsement_category_hosts": ["hwahae.com"],
            },
        },
        "win_plan": {"sku_plans": []},
        "merchant_narrative": {"where_youre_losing": {"outreach_moves": []}},
    }


@pytest.mark.asyncio
async def test_attach_outreach_outcomes_per_sku_classifies_prior_targets(
    monkeypatch,
) -> None:
    """Per-SKU production mode: prior per-SKU report's win-plan targets get
    classified on the current per-SKU brand report, attached TOP-LEVEL
    (no brand merchant_view exists), with same-basis query claims licensed."""
    from db import merchant_audit_runs as mar
    from db import merchant_tasks as mt
    from services.agent_center_bd_report_service import (
        _attach_outreach_outcomes_per_sku,
    )

    prior_report = _per_sku_brand_report()
    prior_report["win_plan"] = {
        "sku_plans": [
            {
                "sku_key": "sku1",
                "losing_queries": [
                    {
                        "query": "best hair care",
                        "axis": "category",
                        "grounds_in": [
                            {"host": "hwahae.com", "outreach": {"state": "draft_ready"}}
                        ],
                    }
                ],
            }
        ]
    }
    prior_report["authority_map"]["host_attribution_summary"] = {
        "endorsement_hosts": [],
        "endorsement_category_hosts": [],
    }
    current = _per_sku_brand_report()  # same basis id → same-basis comparison

    async def fake_fetch(*, run_id: str) -> Dict[str, Any]:
        assert run_id == "prior-per-sku-run"
        return {"report_jsonb": prior_report}

    async def fake_tasks(**kwargs) -> list:
        return []

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mt, "list_tasks_for_merchant", fake_tasks)

    await _attach_outreach_outcomes_per_sku(
        current,
        merchant_id="merchant_123",
        prior_runs=[
            {
                "run_id": "prior-per-sku-run",
                "status": "succeeded",
                "audit_mode": "per_sku",
                "requested_at": "2026-07-08T00:00:00+00:00",
            }
        ],
    )

    outcomes = current["outreach_outcomes"]
    assert "merchant_view" not in current  # top-level attach, per-SKU contract
    assert outcomes["available"] is True
    assert outcomes["comparable"] is True  # basis resolved from per_sku rows
    row = next(t for t in outcomes["targets"] if t["host"] == "hwahae.com")
    # The losing query left the failing set AND is in the probed-prompt list,
    # so the same-basis query-level win claim is licensed.
    assert row["outcome"] == "won"
    assert row["reason"] == "query_now_cited"


@pytest.mark.asyncio
async def test_attach_outreach_outcomes_per_sku_first_audit_and_no_context() -> None:
    from services.agent_center_bd_report_service import (
        _attach_outreach_outcomes_per_sku,
    )

    # prior_runs=None → no history context was provided → section omitted.
    no_context = _per_sku_brand_report()
    await _attach_outreach_outcomes_per_sku(
        no_context, merchant_id="merchant_123", prior_runs=None
    )
    assert "outreach_outcomes" not in no_context

    # prior_runs=[] → a real first audit → honest empty baseline attaches.
    first = _per_sku_brand_report()
    await _attach_outreach_outcomes_per_sku(
        first, merchant_id="merchant_123", prior_runs=[]
    )
    outcomes = first["outreach_outcomes"]
    assert outcomes["is_first_audit"] is True
    assert outcomes["available"] is False
    assert outcomes["targets"] == []


def test_discovery_lift_layer1_pivota_status_points_to_pdp_baseline_artifact() -> None:
    """Layer 1's pivota_status cites the canonical pivota-pdp-baseline.md
    artifact (the live operational health check)."""
    layers = _wpc(_basic_report())["discovery_lift"]["layers"]
    layer1 = next(l for l in layers if "layer 1" in l["name"].lower())
    # The methodology_note carries the artifact reference for Layer 1.
    note = _wpc(_basic_report())["discovery_lift"]["methodology_note"]
    status1 = layer1["pivota_status"]
    assert "pivota-pdp-baseline.md" in status1 or "pivota-pdp-baseline.md" in note


def test_discovery_lift_layer1_discloses_indexing_up_phase() -> None:
    """Layer 1's pivota_status honestly discloses the indexing-up phase
    + the 30-90 day arc."""
    layers = _wpc(_basic_report())["discovery_lift"]["layers"]
    layer1 = next(l for l in layers if "layer 1" in l["name"].lower())
    status1 = layer1["pivota_status"]
    assert "indexing-up" in status1.lower()
    assert "30-90 day" in status1


def test_discovery_lift_layer2_is_shipped_today_independent_of_indexing() -> None:
    """Layer 2 (agent-direct API) is the day-1 value: shipped today,
    independent of Google indexing arc. This is the core BD pitch
    differentiator — apps + personal agents can recommend onboarded
    merchants from day-1, not day-90."""
    layers = _wpc(_basic_report())["discovery_lift"]["layers"]
    layer2 = next(l for l in layers if "layer 2" in l["name"].lower())
    status2 = layer2["pivota_status"]
    # Must explicitly say shipped + independent of Google indexing.
    assert "shipped" in status2.lower()
    assert (
        "day-1" in status2.lower()
        or "independent of" in status2.lower()
    )


def test_discovery_lift_layer2_describes_apps_as_agents_and_personal_agents() -> None:
    """Layer 2's narrative names the agent ecosystem categories explicitly
    — apps-as-agents, personal agents — so BD can pitch the compounding
    play."""
    layers = _wpc(_basic_report())["discovery_lift"]["layers"]
    layer2 = next(l for l in layers if "layer 2" in l["name"].lower())
    blob = (layer2.get("subtitle", "") + " " + layer2.get("what_it_is", "")).lower()
    assert "apps" in blob and "agent" in blob
    assert "personal agent" in blob or "chatgpt" in blob or "claude" in blob


def test_discovery_lift_prediction_leads_with_layer2_day1_value() -> None:
    """Prediction must position Layer 2 as the day-1 value; Layer 1's
    indexing arc is a co-investment, not the headline."""
    pred = _wpc(_basic_report())["discovery_lift"]["prediction"]
    assert "layer 2" in pred.lower()
    assert "day-1" in pred.lower()
    assert "compound" in pred.lower() or "ecosystem" in pred.lower()


def test_pivota_pdp_baseline_reference_uses_live_zero_baseline_today() -> None:
    """As of 2026-05-06, the live baseline has 0/0 medians (indexing-up
    phase). Constants must reflect reality, not a hopeful 67/50."""
    from services.agent_center_bd_report_service import PIVOTA_PDP_BASELINE_REFERENCE
    assert PIVOTA_PDP_BASELINE_REFERENCE["median_visibility"] == 0
    assert PIVOTA_PDP_BASELINE_REFERENCE["median_attribution"] == 0
    assert PIVOTA_PDP_BASELINE_REFERENCE["cited_count"] == 0
    assert PIVOTA_PDP_BASELINE_REFERENCE["indexing_phase"] == "indexing-up"


def test_methodology_note_names_specific_probe_modes_used_in_baseline() -> None:
    """methodology_note must name `open_product_visibility_test` AND
    `merchant_store_attribution_test` — exactly the two scan modes
    `agent_center_pivota_pdp_baseline.py` runs."""
    note = _wpc(_basic_report())["discovery_lift"]["methodology_note"]
    assert "open_product_visibility_test" in note
    assert "merchant_store_attribution_test" in note


def test_methodology_note_clarifies_layer_specific_comparability() -> None:
    """methodology_note must clarify the Pivota baseline is comparable
    only to attribution_score (Layer 1) and that Layer 2 is binary by
    integration (no per-merchant probe). Phase 2j: Layer 3 dropped."""
    note = _wpc(_basic_report())["discovery_lift"]["methodology_note"]
    assert "attribution_score" in note
    assert "layer 2" in note.lower()
    assert "binary by" in note.lower() or "integration" in note.lower()


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
    assert "internal_shopify_test_merchant" in md
    assert "internal-shopify-test.example" in md
    # Manual-today steps render with the wrench badge.
    assert "🔧 manual today" in md
    # Roadmap_note explains RESERVED placeholders.
    assert "RESERVED" in md


# ---------------------------------------------------------------------------
# Phase 2j: Layer 2 reframe + visibility_booster + Layer 3 dropped.
# ---------------------------------------------------------------------------


def test_layer2_examples_are_apps_with_traffic_not_existing_shopping_apps() -> None:
    """Layer 2 narrative names apps with massive non-shopping traffic
    that are transforming into agents — Uber / Airbnb / Spotify / Reddit
    / X / Discord / Pinterest type — NOT Klarna (already a shopping app).
    These are the right examples for 'every app becomes an agent'."""
    layers = _wpc(_basic_report())["discovery_lift"]["layers"]
    layer2 = next(l for l in layers if "layer 2" in l["name"].lower())
    blob = (layer2.get("subtitle", "") + " " + layer2.get("what_it_is", "")).lower()
    # At least 3 of the named non-shopping apps should appear.
    candidates = ["uber", "airbnb", "spotify", "reddit", " x ", "discord", "pinterest"]
    matches = sum(1 for c in candidates if c in blob)
    assert matches >= 3, (
        f"Layer 2 should reference apps-with-traffic-but-no-shopping; "
        f"found only {matches} of {candidates} in blob"
    )
    # Klarna (existing shopping vertical) MUST NOT be the headline example.
    # It can appear in passing but shouldn't be the primary positioning.
    # (Looser check — just don't lead with Klarna.)
    assert "klarna" not in blob.split(".")[0].lower(), (
        "Layer 2 should not lead with Klarna (already a shopping app); "
        "lead with non-shopping apps becoming agents."
    )


def test_visibility_booster_block_present_with_three_required_pieces() -> None:
    """visibility_booster must explain the merchant-side workflow with
    three required parts: mechanisms_that_work, what_doesnt_work,
    honest_position."""
    vb = _wpc(_basic_report())["visibility_booster"]
    assert vb["title"]
    assert vb["intro"]
    assert vb["mechanisms_that_work"]
    assert vb["what_doesnt_work"]
    assert vb["honest_position"]


def test_visibility_booster_corrects_prompt_repetition_misconception() -> None:
    """Required honesty: explicitly call out that prompt repetition /
    spam does NOT lift grounded retrieval (folk remedy that BD has
    heard from merchants)."""
    vb = _wpc(_basic_report())["visibility_booster"]
    blob = " ".join(vb["what_doesnt_work"]).lower()
    assert "prompt repetition" in blob or "prompt history" in blob or "don't learn" in blob


def test_visibility_booster_mechanisms_include_index_reinforcement_and_demand_test() -> None:
    """The mechanisms_that_work list must include index reinforcement
    (Search Console URL Inspection) + Demand Test continuous diagnosis
    + structured-data depth + canonical URL authority. These are the
    actual levers Pivota operates."""
    vb = _wpc(_basic_report())["visibility_booster"]
    labels = " ".join(m.get("label", "").lower() for m in vb["mechanisms_that_work"])
    assert "search console" in labels or "url inspection" in labels or "index reinforcement" in labels
    assert "demand test" in labels or "continuous diagnosis" in labels
    assert "schema.org" in labels or "structured-data" in labels or "structured data" in labels
    assert "canonical url" in labels or "canonical pdp" in labels or "stable canonical" in labels


def test_visibility_booster_honest_position_does_not_overpromise_layer1() -> None:
    """The honest_position MUST explicitly acknowledge Layer 1 is a
    30-90 day arc and NOT promise immediate visibility lift."""
    vb = _wpc(_basic_report())["visibility_booster"]
    pos = vb["honest_position"].lower()
    assert "30-90 day" in pos
    assert "do not promise" in pos or "not promise" in pos
    # Should reposition value to Layer 2 + ongoing diagnosis.
    assert "layer 2" in pos
    assert "day-1" in pos


def test_render_markdown_includes_visibility_booster_section() -> None:
    from services.agent_center_bd_report_service import (
        render_markdown_from_structured,
    )
    md = render_markdown_from_structured(_basic_report())
    assert "Merchant-side agent workflow" in md
    assert "What does NOT work" in md or "what does not work" in md.lower()
    assert "Prompt repetition" in md or "prompt repetition" in md
    assert "Honest position" in md
    # Search Console + structured data evidence row visible.
    assert "Search Console" in md or "URL Inspection" in md
    assert "Schema.org" in md


# ---------------------------------------------------------------------------
# Phase 2k: competitive_pressure section — peers vs merchant first-party
# visibility comparison. The BD pressure point: "merchant might shrug off
# 0/3 attribution, but won't shrug off competitor X having their own .com
# cited."
# ---------------------------------------------------------------------------


def _category_run_with_competitors_and_hosts(
    query: str,
    *,
    competitors: List[str],
    hosts: List[str],
    excerpt: str = "",
) -> Dict[str, Any]:
    """Fixture builder. NOTE: each grounding source needs a distinct
    URI host or _identify_run_sources dedup will drop duplicates."""
    return {
        "query": query,
        "parsed": {
            "brand_appears": True,
            "competitors_appearing": competitors,
            "evidence_excerpt": excerpt,
        },
        "grounding_chunks": [
            f"https://r{i}.example/{i}" for i in range(len(hosts))
        ],
        "grounding_sources": [
            {"uri": f"https://r{i}.example/{i}", "title": h}
            for i, h in enumerate(hosts)
        ],
        "url_match": {
            "target_brand": "Test",
            "target_url": "https://test.com/p",
            "in_grounding": False,
            "in_text": False,
            "llm_self_report": True,
        },
    }


def _report_with_competitive_data(category_runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="TestBrand",
        merchant_pdp_url="https://testbrand.com/products/p",
        product_title="Test Product",
        product_vendor="TestBrand",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": category_runs,
        },
        provider="gemini",
    )


def test_competitive_pressure_present_with_peers_named() -> None:
    """The competitive_pressure block surfaces peer brands AI agents
    named in category queries — the comparative set."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Origins", "The Ordinary", "Beauty of Joseon"],
            hosts=["voguescandinavia.com", "skinsort.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    assert cp["title"]
    peer_names = [p["name"] for p in cp["peers_named"]]
    assert "Origins" in peer_names
    assert "The Ordinary" in peer_names


def test_competitive_pressure_detects_competitor_first_party_visibility() -> None:
    """Heuristic match: when a competitor's .com appears in retailer_hosts,
    flag them as first-party-visible. This is the BD pressure point —
    'X has their own .com cited; you don't.'"""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Origins", "The Ordinary"],
            # origins.com is a competitor's first-party domain
            hosts=["voguescandinavia.com", "origins.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    fp = cp["peers_with_first_party_visibility"]
    assert len(fp) >= 1
    assert any(p["brand"] == "Origins" for p in fp)
    origins_entry = next(p for p in fp if p["brand"] == "Origins")
    assert origins_entry["first_party_host"] == "origins.com"


def test_competitive_pressure_framing_when_peers_first_party_visible() -> None:
    """When some peers ARE first-party-visible, framing must be urgent —
    'every retailer-routed query is a customer they won and you didn't see'."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Origins"],
            hosts=["origins.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    framing = cp["framing"].lower()
    assert "competitive pressure" in framing
    assert "real and immediate" in framing or "won" in framing
    assert "origins" in framing


def test_competitive_pressure_framing_when_no_peers_first_party_visible() -> None:
    """When NO peers are first-party-visible (entire category retailer-
    mediated), framing pivots to 'first-mover opportunity' — wider win."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Origins", "The Ordinary"],
            hosts=["voguescandinavia.com", "skinsort.com", "target.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    assert len(cp["peers_with_first_party_visibility"]) == 0
    framing = cp["framing"].lower()
    assert "first-mover" in framing
    # Phase C-4 PR-B follow-up: framing no longer claims "retailer-mediated"
    # because cited hosts can include media (forbes.com), brand .coms
    # (serenaandlily.com), etc. — not always retailers. We say
    # "third-party hosts" instead.
    assert "third-party hosts" in framing


def test_competitive_pressure_peer_host_match_handles_multiword_brands() -> None:
    """The peer-host matcher (post-#525 N6 fix) must still match
    multi-word brands to their D2C domain: 'Beauty of Joseon' →
    'beautyofjoseon.com', 'PEACH & LILY' → 'peachandlily.com'. Every
    significant brand word must appear in the host segment AND cover
    >=60% of it — both these brands clear that bar."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Beauty of Joseon", "PEACH & LILY"],
            hosts=["beautyofjoseon.com", "peachandlily.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    fp = cp["peers_with_first_party_visibility"]
    fp_brands = {p["brand"] for p in fp}
    assert "Beauty of Joseon" in fp_brands
    assert "PEACH & LILY" in fp_brands


def test_competitive_pressure_rejects_fabricated_peer_host_pairing() -> None:
    """N6 regression (post-#525 codex review P1). Pre-fix the matcher
    took the single longest brand word and loose-substring-matched it:
    competitor 'YSE Beauty' → discriminator 'beauty' → matched the
    MERCHANT's own 'beautyofjoseon.com'. The report then claimed
    "YSE Beauty (beautyofjoseon.com)" as a peer-with-first-party-
    visibility — a fabricated pairing.

    Post-fix: 'yse' is not a substring of 'beautyofjoseon', so the
    all-words rule rejects it. And even if it did partially match,
    beautyofjoseon.com is the merchant's own domain (merchant brand
    'Beauty of Joseon') so it's excluded from peer matching outright."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["YSE Beauty", "Origins"],
            hosts=["beautyofjoseon.com", "origins.com"],
        ),
    ]
    # Merchant in _report_with_competitive_data is "TestBrand" — use a
    # dedicated builder so the merchant brand is "Beauty of Joseon",
    # making beautyofjoseon.com the merchant's OWN domain.
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="Beauty of Joseon",
        merchant_pdp_url="https://agent.pivota.cc/products/sig_x",
        product_title="Test Product",
        product_vendor="Beauty of Joseon",
        product_type="face mask",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": []},
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": runs,
        },
        provider="gemini",
    )
    cp = report["competitive_pressure"]
    fp_brands = {p["brand"] for p in cp["peers_with_first_party_visibility"]}
    fp_hosts = {p["first_party_host"] for p in cp["peers_with_first_party_visibility"]}
    # YSE Beauty must NOT be paired with beautyofjoseon.com.
    assert "YSE Beauty" not in fp_brands, (
        "YSE Beauty falsely matched a host — N6 fabricated pairing regressed"
    )
    # The merchant's own domain must never appear as a peer host.
    assert "beautyofjoseon.com" not in fp_hosts
    # Origins → origins.com is a legitimate match and should survive.
    assert "Origins" in fp_brands


def test_competitive_pressure_generic_word_brand_does_not_overmatch() -> None:
    """A brand whose only significant word is a generic category term
    must not claim a much longer unrelated host. 'Glow Co' → 'glow'
    is in 'glowrecipe' but covers only 4/9 chars (<60%) → rejected."""
    runs = [
        _category_run_with_competitors_and_hosts(
            "best serums 2026",
            competitors=["Glow Co"],
            hosts=["glowrecipe.com"],
        ),
    ]
    cp = _report_with_competitive_data(runs)["competitive_pressure"]
    fp_brands = {p["brand"] for p in cp["peers_with_first_party_visibility"]}
    assert "Glow Co" not in fp_brands


def test_render_markdown_includes_competitive_pressure_section() -> None:
    from services.agent_center_bd_report_service import render_markdown_from_structured
    runs = [
        _category_run_with_competitors_and_hosts(
            "best face masks 2026",
            competitors=["Origins", "The Ordinary"],
            hosts=["origins.com", "voguescandinavia.com"],
        ),
    ]
    md = render_markdown_from_structured(_report_with_competitive_data(runs))
    assert "Competitive pressure" in md
    assert "Direct competitor brands" in md
    assert "first-party in" in md.lower() or "their own .com" in md.lower()
    assert "Origins" in md
    assert "origins.com" in md


def _strong_brand_report_for_buyer_path() -> Dict[str, Any]:
    return {
        "merchant_name": "BB Lab",
        "merchant_domain": "bblab.shop",
        "aggregate": {
            "avg_visibility": 100.0,
            "avg_attribution": 100.0,
            "avg_category_visibility": 67.0,
            "brand_verdict_label": "STRONG",
            "brand_verdict_explanation": (
                "AI agents cite your URL in 3 of 3 buyer-intent queries. "
                "Both discovery and attribution are at goal state."
            ),
        },
        "per_product": [
            {
                "executive_summary": {
                    "verdict_pill_text": "Strong AI-channel attribution",
                },
                "verdict": {
                    "label": "STRONG",
                    "label_display": "Strong AI-channel attribution",
                    "explanation": (
                        "AI agents cite your URL in 3 of 3 buyer-intent queries. "
                        "Both discovery and attribution are at goal state."
                    ),
                    "visibility_score": 100,
                    "attribution_score": 100,
                    "category_visibility_score": 67,
                },
                "merchant_view": {
                    "headline": {
                        "verdict_label": "STRONG",
                        "verdict_label_display": "Strong AI-channel attribution",
                        "one_liner": "Both discovery and attribution are at goal state.",
                        "plain_summary": "Both discovery and attribution are at goal state.",
                    },
                },
            },
        ],
    }


def _sku_intelligence_with_prompt_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "prompt_matrix": rows,
        "next_best_action": {
            "primary_gap": "source_route_repair",
            "prescription_class": "operational_efficiency",
        },
    }


def test_buyer_path_postprocessor_preserves_raw_strong_but_changes_bblab_display() -> None:
    from services.agent_center_bd_report_service import (
        apply_buyer_path_verdict_to_brand_report,
    )

    brand_report = _strong_brand_report_for_buyer_path()
    sku_intelligence = _sku_intelligence_with_prompt_rows([
        {
            "query": "halal collagen sticks before bed",
            "ownership_state": "forum-owned",
            "sources": [
                {"host": "beautyandthebrows.co", "times_cited": 1},
                {"host": "kona.ng", "times_cited": 1},
                {"host": "moodarabia.com", "times_cited": 1},
            ],
            "buyer_path_action": {
                "controllers": [
                    "beautyandthebrows.co",
                    "kona.ng",
                    "moodarabia.com",
                ],
            },
            "opportunity_score": 20.5,
        },
        {
            "query": "fish collagen sticks",
            "ownership_state": "retailer-owned",
            "sources": [{"host": "yesstyle.com", "times_cited": 1}],
            "buyer_path_action": {"controllers": ["yesstyle.com"]},
            "opportunity_score": 12,
        },
    ])

    out = apply_buyer_path_verdict_to_brand_report(brand_report, sku_intelligence)
    verdict = out["per_product"][0]["verdict"]

    assert verdict["label"] == "STRONG"
    assert verdict["ai_attribution_label"] == "STRONG"
    assert verdict["label_display"] == "Strong AI visibility, weak owned buyer path"
    assert "0/2 evidenced prompt lanes are merchant-owned" in verdict["explanation"]
    assert '"halal collagen sticks before bed"' in verdict["explanation"]
    assert "beautyandthebrows.co" in verdict["explanation"]
    assert "goal state" not in verdict["explanation"].lower()
    assert verdict["buyer_path_verdict"]["merchant_owned_count"] == 0
    assert verdict["buyer_path_verdict"]["top_controllers"] == [
        "beautyandthebrows.co",
        "kona.ng",
        "moodarabia.com",
    ]
    assert out["aggregate"]["brand_verdict_label"] == "STRONG"
    assert (
        out["aggregate"]["brand_verdict_label_display"]
        == "Strong AI visibility, weak owned buyer path"
    )
    headline = out["per_product"][0]["merchant_view"]["headline"]
    assert headline["verdict_label"] == "STRONG"
    assert headline["verdict_label_display"] == "Strong AI visibility, weak owned buyer path"
    assert out["per_product"][0]["executive_summary"]["verdict_pill_text"] == (
        "Strong AI visibility, weak owned buyer path"
    )


def test_buyer_path_postprocessor_caps_ownist_controllers_and_avoids_grape_jelly_drift() -> None:
    from services.agent_center_bd_report_service import (
        apply_buyer_path_verdict_to_brand_report,
    )

    brand_report = _strong_brand_report_for_buyer_path()
    brand_report["merchant_name"] = "Ownist"
    sku_intelligence = _sku_intelligence_with_prompt_rows([
        {
            "query": "healthy snacks collagen jelly",
            "ownership_state": "retailer-owned",
            "sources": [
                {"host": "cogentsteps.net", "times_cited": 1},
                {"host": "medsysgroup.com", "times_cited": 1},
                {"host": "hellokoop.com", "times_cited": 1},
            ],
            "buyer_path_action": {
                "controllers": ["cogentsteps.net", "medsysgroup.com", "hellokoop.com"],
            },
            "opportunity_score": 18.0,
            "lane_priority_score": 0.21,
            "fit_penalties": ["lifestyle_drift:healthy snacks"],
        },
        {
            "query": "vitamin c collagen jelly",
            "ownership_state": "retailer-owned",
            "sources": [
                {"host": "oliveyoung.com", "times_cited": 1},
                {"host": "costco.com", "times_cited": 1},
                {"host": "flyairseoul.com", "times_cited": 1},
            ],
            "buyer_path_action": {
                "controllers": ["oliveyoung.com", "costco.com", "flyairseoul.com"],
            },
            "opportunity_score": 14.82,
            "lane_priority_score": 0.74,
        },
        {
            "query": "anti age collagen jelly",
            "ownership_state": "retailer-owned",
            "sources": [
                {"host": "en.healthyskin.ee", "times_cited": 1},
                {"host": "kaigoro.com", "times_cited": 1},
            ],
            "buyer_path_action": {
                "controllers": ["en.healthyskin.ee", "kaigoro.com"],
            },
            "opportunity_score": 10.1,
        },
    ])
    sku_intelligence["next_best_action"]["evidence_used"] = {
        "source_route_prompt": {
            "query": "vitamin c collagen jelly",
            "sources": [
                {"host": "oliveyoung.com", "times_cited": 1},
                {"host": "costco.com", "times_cited": 1},
                {"host": "flyairseoul.com", "times_cited": 1},
            ],
        }
    }

    assert all("grape jelly" not in row["query"].lower() for row in sku_intelligence["prompt_matrix"])
    out = apply_buyer_path_verdict_to_brand_report(brand_report, sku_intelligence)
    buyer_path = out["per_product"][0]["verdict"]["buyer_path_verdict"]

    assert buyer_path["state"] == "third_party_controlled"
    assert buyer_path["primary_lane"] == "vitamin c collagen jelly"
    assert buyer_path["top_controllers"] == [
        "oliveyoung.com",
        "costco.com",
        "flyairseoul.com",
    ]
    assert len(buyer_path["top_controllers"]) == 3
    assert "grape jelly" not in out["per_product"][0]["verdict"]["explanation"].lower()
    assert "healthy snacks collagen jelly" not in out["per_product"][0]["verdict"]["explanation"]


def test_buyer_path_postprocessor_keeps_strong_display_when_path_is_merchant_controlled() -> None:
    from services.agent_center_bd_report_service import (
        apply_buyer_path_verdict_to_brand_report,
    )

    brand_report = _strong_brand_report_for_buyer_path()
    sku_intelligence = _sku_intelligence_with_prompt_rows([
        {"query": "buy bb lab collagen", "ownership_state": "merchant-owned"},
        {"query": "bb lab collagen price", "ownership_state": "merchant-owned"},
        {
            "query": "fish collagen sticks",
            "ownership_state": "retailer-owned",
            "sources": [{"host": "yesstyle.com", "times_cited": 1}],
            "buyer_path_action": {"controllers": ["yesstyle.com"]},
        },
    ])

    out = apply_buyer_path_verdict_to_brand_report(brand_report, sku_intelligence)
    verdict = out["per_product"][0]["verdict"]
    assert verdict["buyer_path_verdict"]["state"] == "merchant_controlled"
    assert verdict["label_display"] == "Strong AI-channel attribution"
    assert "goal state" in verdict["explanation"].lower()


def test_buyer_path_postprocessor_shows_mixed_path_without_changing_raw_verdict() -> None:
    from services.agent_center_bd_report_service import (
        apply_buyer_path_verdict_to_brand_report,
    )

    brand_report = _strong_brand_report_for_buyer_path()
    sku_intelligence = _sku_intelligence_with_prompt_rows([
        {"query": "buy bb lab collagen", "ownership_state": "merchant-owned"},
        {
            "query": "best collagen sticks",
            "ownership_state": "publisher-owned",
            "sources": [{"host": "healthline.com", "times_cited": 1}],
            "buyer_path_action": {"controllers": ["healthline.com"]},
            "opportunity_score": 9,
        },
    ])

    out = apply_buyer_path_verdict_to_brand_report(brand_report, sku_intelligence)
    verdict = out["per_product"][0]["verdict"]
    assert verdict["label"] == "STRONG"
    assert verdict["buyer_path_verdict"]["state"] == "mixed"
    assert verdict["label_display"] == "Strong AI visibility, mixed owned buyer path"
    assert "only 1/2 evidenced prompt lanes are merchant-owned" in verdict["explanation"]


def test_buyer_path_postprocessor_leaves_display_when_sku_intelligence_missing() -> None:
    from services.agent_center_bd_report_service import (
        apply_buyer_path_verdict_to_brand_report,
    )

    brand_report = _strong_brand_report_for_buyer_path()
    out = apply_buyer_path_verdict_to_brand_report(brand_report, {})
    verdict = out["per_product"][0]["verdict"]

    assert verdict["buyer_path_verdict"]["state"] == "not_measured"
    assert verdict["label_display"] == "Strong AI-channel attribution"
    assert "goal state" in verdict["explanation"].lower()


def test_legacy_markdown_uses_combined_verdict_display() -> None:
    from services.agent_center_bd_report_service import render_markdown_from_structured

    report = _report_with_competitive_data([])
    report["verdict"]["label"] = "STRONG"
    report["verdict"]["label_display"] = "Strong AI visibility, weak owned buyer path"
    report["verdict"]["explanation"] = (
        "AI answer visibility is strong, but 0/2 evidenced prompt lanes are merchant-owned."
    )
    md = render_markdown_from_structured(report)

    assert "## Verdict: **Strong AI visibility, weak owned buyer path**" in md
    assert "## Verdict: **STRONG**" not in md
