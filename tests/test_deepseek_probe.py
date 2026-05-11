"""Tests for the Deepseek V4 multi-LLM probe (PR-3a).

Coverage areas:
  - Query template generation per scan_mode
  - System prompt + user-message structure per scan_mode
  - Response parsing (bare JSON, fenced, mid-text JSON)
  - Grounding-source extraction from multiple Deepseek response shapes
  - url_match shape matching the Gemini probe parity
  - Score arithmetic per scan_mode
  - End-to-end probe_one_scan_mode flow with mocked HTTP

Tests do NOT make real Deepseek API calls — every external HTTP is
mocked via httpx.MockTransport so the suite is hermetic.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch, AsyncMock

import httpx
import pytest


# ---------------------------------------------------------------------
# Query string generation per scan_mode
# ---------------------------------------------------------------------


def test_open_product_visibility_queries():
    from services.llm_providers.deepseek_probe import _build_query_strings
    qs = _build_query_strings(
        scan_mode="open_product_visibility_test",
        product_title="Greens Gummies",
        max_runs=3,
    )
    assert len(qs) == 3
    assert all("Greens Gummies" in q for q in qs)
    assert "where can I buy" in qs[0]
    assert "shop" in qs[1]
    assert "for sale" in qs[2]


def test_merchant_attribution_queries_match_visibility_queries():
    """Both scan modes use the same buyer-intent queries — paired
    test design lets the two scores be compared apples-to-apples."""
    from services.llm_providers.deepseek_probe import _build_query_strings
    vis_qs = _build_query_strings(
        scan_mode="open_product_visibility_test",
        product_title="Greens Gummies",
        max_runs=3,
    )
    attr_qs = _build_query_strings(
        scan_mode="merchant_store_attribution_test",
        product_title="Greens Gummies",
        max_runs=3,
    )
    assert vis_qs == attr_qs


def test_category_visibility_queries_use_product_type_not_brand():
    """Category-open queries DO NOT name the brand — tests organic
    discoverability."""
    from services.llm_providers.deepseek_probe import _build_query_strings
    qs = _build_query_strings(
        scan_mode="category_visibility_test",
        product_title="Greens Gummies",
        product_type="daily greens supplements",
        merchant_brand="Grüns",
        max_runs=3,
    )
    assert len(qs) == 3
    assert all("daily greens supplements" in q for q in qs)
    # Must not mention the brand name
    assert all("Grüns" not in q for q in qs)
    assert all("Greens Gummies" not in q for q in qs)


def test_category_visibility_returns_empty_when_no_product_type():
    """Without product_type, category queries can't be generated."""
    from services.llm_providers.deepseek_probe import _build_query_strings
    qs = _build_query_strings(
        scan_mode="category_visibility_test",
        product_title="Greens Gummies",
        product_type=None,
        max_runs=3,
    )
    assert qs == []


def test_max_runs_caps_query_count():
    from services.llm_providers.deepseek_probe import _build_query_strings
    qs = _build_query_strings(
        scan_mode="open_product_visibility_test",
        product_title="X",
        max_runs=2,
    )
    assert len(qs) == 2


# ---------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------


def test_parse_deepseek_response_bare_json():
    from services.llm_providers.deepseek_probe import _parse_deepseek_response
    parsed = _parse_deepseek_response(
        '{"product_visible": true, "competitors_listed": ["AG1"], '
        '"evidence_excerpt": "Grüns is great"}'
    )
    assert parsed is not None
    assert parsed["product_visible"] is True
    assert parsed["competitors_listed"] == ["AG1"]


def test_parse_deepseek_response_fenced_json():
    from services.llm_providers.deepseek_probe import _parse_deepseek_response
    text = (
        "Sure, here's the answer:\n"
        "```json\n"
        '{"brand_appears": true, "competitors_appearing": ["AG1", "Bloom"]}\n'
        "```"
    )
    parsed = _parse_deepseek_response(text)
    assert parsed is not None
    assert parsed["brand_appears"] is True


def test_parse_deepseek_response_mid_text_json():
    """LLM occasionally wraps JSON in prose. Last-resort regex finds
    the {...} substring."""
    from services.llm_providers.deepseek_probe import _parse_deepseek_response
    text = 'Here you go: {"merchant_url_found": false, "evidence_excerpt": ""} - end.'
    parsed = _parse_deepseek_response(text)
    assert parsed is not None
    assert parsed["merchant_url_found"] is False


def test_parse_deepseek_response_returns_none_for_empty():
    from services.llm_providers.deepseek_probe import _parse_deepseek_response
    assert _parse_deepseek_response("") is None
    assert _parse_deepseek_response("   ") is None


def test_parse_deepseek_response_returns_none_for_non_json():
    from services.llm_providers.deepseek_probe import _parse_deepseek_response
    assert _parse_deepseek_response("This is not JSON at all.") is None


# ---------------------------------------------------------------------
# Grounding source extraction
# ---------------------------------------------------------------------


def test_extract_grounding_sources_from_top_level_citations_strings():
    """Older Deepseek search API returns plain URL strings."""
    from services.llm_providers.deepseek_probe import _extract_grounding_sources
    sources = _extract_grounding_sources({
        "citations": ["https://forbes.com/best-greens", "https://trailandkale.com/review"],
    })
    assert len(sources) == 2
    assert sources[0]["uri"] == "https://forbes.com/best-greens"
    assert sources[0]["title"] == "forbes.com"


def test_extract_grounding_sources_from_top_level_citations_objects():
    """Newer shape returns {url, title} objects."""
    from services.llm_providers.deepseek_probe import _extract_grounding_sources
    sources = _extract_grounding_sources({
        "citations": [
            {"url": "https://forbes.com/best-greens", "title": "Forbes Vetted Best Greens 2026"},
        ],
    })
    assert len(sources) == 1
    assert sources[0]["title"] == "Forbes Vetted Best Greens 2026"


def test_extract_grounding_sources_from_message_annotations():
    """OpenAI-compatible url_citation annotations format."""
    from services.llm_providers.deepseek_probe import _extract_grounding_sources
    payload = {
        "choices": [{
            "message": {
                "content": "...",
                "annotations": [
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://womenshealthmag.com/2026-greens",
                            "title": "Women's Health 2026 Greens Roundup",
                        },
                    },
                ],
            },
        }],
    }
    sources = _extract_grounding_sources(payload)
    assert len(sources) == 1
    assert sources[0]["uri"] == "https://womenshealthmag.com/2026-greens"


def test_extract_grounding_sources_dedupes():
    """Same URI appearing in multiple shapes should only count once."""
    from services.llm_providers.deepseek_probe import _extract_grounding_sources
    payload = {
        "citations": ["https://forbes.com/x"],
        "choices": [{
            "message": {
                "annotations": [
                    {"type": "url_citation",
                     "url_citation": {"url": "https://forbes.com/x", "title": "Forbes"}},
                ],
            },
        }],
    }
    sources = _extract_grounding_sources(payload)
    assert len(sources) == 1


def test_extract_grounding_sources_empty_when_no_grounding():
    from services.llm_providers.deepseek_probe import _extract_grounding_sources
    sources = _extract_grounding_sources({"choices": [{"message": {"content": "..."}}]})
    assert sources == []


# ---------------------------------------------------------------------
# url_match shape (Gemini probe parity)
# ---------------------------------------------------------------------


def test_url_match_for_attribution_test_detects_in_grounding():
    from services.llm_providers.deepseek_probe import _build_url_match
    um = _build_url_match(
        scan_mode="merchant_store_attribution_test",
        parsed={"merchant_url_found": True},
        merchant_brand="Grüns",
        merchant_pdp_url="https://gruns.co/products/greens-gummies",
        grounding_sources=[
            {"uri": "https://gruns.co/products/greens-gummies", "title": "Grüns"},
            {"uri": "https://forbes.com/x", "title": "Forbes"},
        ],
        raw_text="Yes, gruns.co/products/greens-gummies is the merchant URL.",
    )
    assert um["target_url"] == "https://gruns.co/products/greens-gummies"
    assert um["in_grounding"] is True
    assert um["in_text"] is True
    assert um["llm_self_report"] is True


def test_url_match_for_attribution_test_when_url_not_in_grounding():
    from services.llm_providers.deepseek_probe import _build_url_match
    um = _build_url_match(
        scan_mode="merchant_store_attribution_test",
        parsed={"merchant_url_found": False},
        merchant_brand="Grüns",
        merchant_pdp_url="https://gruns.co/products/greens-gummies",
        grounding_sources=[{"uri": "https://forbes.com/x", "title": "Forbes"}],
        raw_text="Forbes recommends Grüns greens gummies.",
    )
    assert um["in_grounding"] is False
    assert um["in_text"] is False  # exact URL string not present
    assert um["llm_self_report"] is False


def test_url_match_for_category_visibility_uses_brand_self_report():
    from services.llm_providers.deepseek_probe import _build_url_match
    um = _build_url_match(
        scan_mode="category_visibility_test",
        parsed={"brand_appears": True},
        merchant_brand="Grüns",
        merchant_pdp_url="https://gruns.co",
        grounding_sources=[{"uri": "https://forbes.com/x", "title": "Forbes"}],
        raw_text="Best Green Gummies: Grüns Superfoods.",
    )
    assert um["target_brand"] == "Grüns"
    assert um["in_grounding"] is False  # category test doesn't probe URL
    assert um["llm_self_report"] is True


def test_url_match_returns_none_for_open_product_visibility():
    """The open_product_visibility scan mode has no url_match in
    upstream Gemini output — we keep parity by returning None."""
    from services.llm_providers.deepseek_probe import _build_url_match
    um = _build_url_match(
        scan_mode="open_product_visibility_test",
        parsed={"product_visible": True},
        merchant_brand="Grüns",
        merchant_pdp_url="https://gruns.co",
        grounding_sources=[],
        raw_text="...",
    )
    assert um is None


# ---------------------------------------------------------------------
# Score arithmetic
# ---------------------------------------------------------------------


def test_compute_scores_open_product_visibility():
    from services.llm_providers.deepseek_probe import _compute_scores_from_runs
    runs = [
        {"parsed": {"product_visible": True}},
        {"parsed": {"product_visible": True}},
        {"parsed": {"product_visible": False}},
    ]
    scores = _compute_scores_from_runs(
        scan_mode="open_product_visibility_test", runs=runs,
    )
    assert scores["visibility_score"] == 67


def test_compute_scores_category_visibility():
    from services.llm_providers.deepseek_probe import _compute_scores_from_runs
    runs = [
        {"parsed": {"brand_appears": True}},
        {"parsed": {"brand_appears": True}},
        {"parsed": None},  # upstream-failed run
    ]
    scores = _compute_scores_from_runs(
        scan_mode="category_visibility_test", runs=runs,
    )
    # 2 positive / 3 total = 67%
    assert scores["visibility_score"] == 67


def test_compute_scores_handles_empty_runs():
    from services.llm_providers.deepseek_probe import _compute_scores_from_runs
    scores = _compute_scores_from_runs(
        scan_mode="open_product_visibility_test", runs=[],
    )
    assert scores["visibility_score"] == 0


# ---------------------------------------------------------------------
# Findings shape
# ---------------------------------------------------------------------


def test_build_findings_shape_matches_v1():
    """Match the V1 finding shape PIVOTA-Agent emits — needed for the
    BD report builder to consume Deepseek findings identically."""
    from services.llm_providers.deepseek_probe import _build_findings
    findings = _build_findings(
        scan_mode="category_visibility_test",
        runs=[{"query": "best greens"}],
        visibility_score=33,
    )
    assert len(findings) == 1
    assert findings[0]["issue_type"] == "category_discoverability_gap"
    assert findings[0]["severity"] == "medium"
    assert "evidence" in findings[0]


def test_build_findings_high_severity_for_low_score():
    from services.llm_providers.deepseek_probe import _build_findings
    findings = _build_findings(
        scan_mode="open_product_visibility_test",
        runs=[],
        visibility_score=0,
    )
    assert findings[0]["severity"] == "high"


# ---------------------------------------------------------------------
# End-to-end probe_one_scan_mode (mocked HTTP)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_one_scan_mode_end_to_end_with_mocked_http(monkeypatch):
    """Full probe path with mocked Deepseek HTTP — verifies queries
    fan out, responses are parsed, grounding is extracted, scores
    compute correctly, V1 result shape is emitted."""
    from services.llm_providers import deepseek_probe

    # Mock the chat call to return a successful structured response.
    async def fake_call(**kwargs):
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "product_visible": True,
                        "competitors_listed": ["AG1"],
                        "evidence_excerpt": "Grüns is excellent.",
                    }),
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": "https://forbes.com/x",
                                "title": "Forbes Vetted",
                            },
                        },
                    ],
                },
            }],
        }
    monkeypatch.setattr(deepseek_probe, "_call_deepseek_chat", fake_call)

    result = await deepseek_probe.probe_one_scan_mode(
        scan_mode="open_product_visibility_test",
        product_title="Greens Gummies",
        merchant_brand="Grüns",
        merchant_pdp_url="https://gruns.co/products/greens-gummies",
        max_runs=3,
        api_key="test-key",
    )
    assert result["scan_mode"] == "open_product_visibility_test"
    assert result["provider"] == "deepseek"
    assert result["runs_count"] == 3
    assert result["scores"]["visibility_score"] == 100  # all 3 positive
    assert len(result["raw_runs"]) == 3
    first = result["raw_runs"][0]
    assert first["parsed"]["product_visible"] is True
    assert len(first["grounding_sources"]) == 1
    assert first["grounding_sources"][0]["title"] == "Forbes Vetted"
    # url_match is None for visibility scan mode (matches Gemini probe)
    assert first["url_match"] is None
    # Findings shape parity
    assert len(result["findings"]) == 1
    assert result["findings"][0]["issue_type"] == "ai_visibility_loss"


@pytest.mark.asyncio
async def test_probe_one_scan_mode_with_unparseable_response(monkeypatch):
    """When Deepseek returns garbage, the run is recorded as
    upstream-failed (parsed=None) so the scorer excludes it from
    the denominator — same semantics as PR-433 fix for Gemini."""
    from services.llm_providers import deepseek_probe

    async def fake_call(**kwargs):
        return {
            "choices": [{
                "message": {
                    "content": "I don't have information about this product.",
                },
            }],
        }
    monkeypatch.setattr(deepseek_probe, "_call_deepseek_chat", fake_call)

    result = await deepseek_probe.probe_one_scan_mode(
        scan_mode="category_visibility_test",
        product_title="Greens Gummies",
        product_type="daily greens supplements",
        merchant_brand="Grüns",
        max_runs=2,
        api_key="test-key",
    )
    # Both runs unparseable → parsed=None → score=0 (no positive runs)
    assert result["scores"]["visibility_score"] == 0
    # But raw_runs are STILL recorded with raw text so debug context
    # exists; parsed is None, downstream scorer treats appropriately
    assert all(r["parsed"] is None for r in result["raw_runs"])


@pytest.mark.asyncio
async def test_probe_one_scan_mode_handles_transport_failure(monkeypatch):
    """Single-query transport failure marks that run as upstream-
    failed (raw='', parsed=None) but doesn't fail the whole scan."""
    from services.llm_providers import deepseek_probe

    call_count = [0]

    async def flaky_call(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise deepseek_probe.DeepseekProbeError("transport timeout")
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "brand_appears": True,
                        "competitors_appearing": [],
                        "evidence_excerpt": "Grüns appears.",
                    }),
                },
            }],
        }
    monkeypatch.setattr(deepseek_probe, "_call_deepseek_chat", flaky_call)

    result = await deepseek_probe.probe_one_scan_mode(
        scan_mode="category_visibility_test",
        product_title="Greens Gummies",
        product_type="daily greens",
        merchant_brand="Grüns",
        max_runs=2,
        api_key="test-key",
    )
    # First run failed → raw="", parsed=None
    assert result["raw_runs"][0]["raw"] == ""
    assert result["raw_runs"][0]["parsed"] is None
    # Second run succeeded
    assert result["raw_runs"][1]["parsed"]["brand_appears"] is True


@pytest.mark.asyncio
async def test_probe_one_scan_mode_raises_when_api_key_missing(monkeypatch):
    """No DEEPSEEK_API_KEY in environment → raise loudly. Caller
    should never silently fall back to mock data for Deepseek."""
    from services.llm_providers import deepseek_probe
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", None)
    with pytest.raises(deepseek_probe.DeepseekProbeError):
        await deepseek_probe.probe_one_scan_mode(
            scan_mode="open_product_visibility_test",
            product_title="X",
            api_key=None,
        )


@pytest.mark.asyncio
async def test_probe_one_scan_mode_returns_empty_for_unknown_scan_mode(monkeypatch):
    """An unknown scan_mode produces no queries; result is empty-but-
    shaped (caller's pipeline doesn't blow up)."""
    from services.llm_providers import deepseek_probe

    result = await deepseek_probe.probe_one_scan_mode(
        scan_mode="bogus_test_mode",
        product_title="X",
        api_key="test-key",
    )
    assert result["runs_count"] == 0
    assert result["raw_runs"] == []


# ---------------------------------------------------------------------
# Provider dispatch in agent_center_llm_client.probe()
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_center_llm_client_routes_deepseek_to_local_path(
    monkeypatch,
):
    """provider='deepseek' bypasses the upstream PIVOTA-Agent HTTP
    call and routes directly to the local Deepseek probe."""
    from services import agent_center_llm_client
    from services.llm_providers import deepseek_probe
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", "test-key")

    # Spy on the local probe — it should be called.
    called_with: Dict[str, Any] = {}

    async def fake_probe_one_scan_mode(**kwargs):
        called_with.update(kwargs)
        return {
            "scan_mode": kwargs.get("scan_mode"),
            "provider": "deepseek",
            "runs_count": 1,
            "scores": {"visibility_score": 50, "attribution_echo_rate": 0},
            "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "raw_runs": [],
        }
    monkeypatch.setattr(deepseek_probe, "probe_one_scan_mode", fake_probe_one_scan_mode)

    # If this routes to upstream HTTP, httpx would error since we
    # didn't configure it. Successful return = local dispatch worked.
    result = await agent_center_llm_client.probe(
        scan_mode="open_product_visibility_test",
        scan_target_id="prod_123",
        merchant_id="merch_test",
        store_id="store_test",
        context={
            "product_title": "Greens Gummies",
            "product_type": "daily greens",
            "merchant_brand": "Grüns",
            "merchant_pdp_url": "https://gruns.co/p",
        },
        provider="deepseek",
        max_runs=1,
    )
    assert result["provider"] == "deepseek"
    assert called_with["scan_mode"] == "open_product_visibility_test"
    assert called_with["product_title"] == "Greens Gummies"
    assert called_with["merchant_brand"] == "Grüns"


@pytest.mark.asyncio
async def test_agent_center_llm_client_deepseek_requires_product_title(
    monkeypatch,
):
    """Caller-side error: provider='deepseek' without product_title
    in context raises ValueError so route layer maps to 422."""
    from services import agent_center_llm_client
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", "test-key")
    with pytest.raises(ValueError, match="product_title"):
        await agent_center_llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="prod_123",
            merchant_id="merch_test",
            store_id="store_test",
            context={},  # no product_title
            provider="deepseek",
            max_runs=1,
        )


@pytest.mark.asyncio
async def test_agent_center_llm_client_deepseek_raises_when_key_missing(
    monkeypatch,
):
    """Without DEEPSEEK_API_KEY → AgentCenterLlmClientError, same
    failure shape as the upstream Gemini path missing
    PIVOTA_AGENT_INTERNAL_API_KEY."""
    from services import agent_center_llm_client
    from config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", None)
    with pytest.raises(agent_center_llm_client.AgentCenterLlmClientError):
        await agent_center_llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="prod_123",
            merchant_id="merch_test",
            store_id="store_test",
            context={"product_title": "X"},
            provider="deepseek",
            max_runs=1,
        )
