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
    return {"grounding_chunks": grounding_chunks}


def test_extract_cited_hosts_separates_merchant_from_competitors() -> None:
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
        _run([]),  # no grounding sources
    ]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs, merchant_host="glossier.com",
    )
    assert competitors == Counter({"sephora.com": 2, "ulta.com": 1})
    assert merchant_runs == 1
    assert runs_with_citations == 2  # third run had no chunks, doesn't count


def test_extract_cited_hosts_merchant_host_none_treats_all_as_competitors() -> None:
    """When the merchant URL is missing/unparseable, every cited host is
    a competitor — caller can still see WHO Gemini is sending traffic to."""
    from agent_center_bd_external_merchant import _extract_cited_hosts
    raw_runs = [_run(["https://sephora.com/x", "https://ulta.com/y"])]
    competitors, merchant_runs, runs_with_citations = _extract_cited_hosts(
        raw_runs, merchant_host=None,
    )
    assert merchant_runs == 0
    assert runs_with_citations == 1
    assert competitors == Counter({"sephora.com": 1, "ulta.com": 1})


def test_extract_cited_hosts_dedupes_within_run() -> None:
    """If Gemini cites sephora.com twice in one answer, that counts as
    1 run-occurrence for sephora.com, not 2 — we want host frequency
    across runs, not raw chunk counts."""
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
    assert "PIVOTA_AGENT_INTERNAL_API_KEY" in report["upstream_status"]["reason"]
    assert "Railway" in report["upstream_status"]["reason"]


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
    assert "PIVOTA_AGENT_INTERNAL_API_KEY" in md


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
