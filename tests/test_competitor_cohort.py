"""PR-2: competitor cohort orchestrator + aggregation tests.

Pure-function coverage of:
  - _aggregate_competitor_brands (route helper)
  - _resolve_competitor_domain (orchestrator) — mocked httpx
  - enqueue_competitor_audits cohort capping

Full end-to-end (Gemini call → audit pipeline → DB write) is exercised
on staging — mocking the full chain adds brittleness without
confidence.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from routes.agent_center_bd_routes import _aggregate_competitor_brands
from services.competitor_audit_orchestrator import (
    _build_domain_prompt,
    _resolve_competitor_domain,
    enqueue_competitor_audits,
)


# ---------------------------------------------------------------------------
# _aggregate_competitor_brands
# ---------------------------------------------------------------------------


def _make_per_product(brands_per_product: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [
        {"category_visibility": {"competitor_brands": brands}}
        for brands in brands_per_product
    ]


def test_aggregate_unions_brands_across_products():
    """Same brand across 2 products → counts sum."""
    per_product = _make_per_product([
        [{"name": "Hiya", "times_cited": 3}, {"name": "Olly", "times_cited": 2}],
        [{"name": "Hiya", "times_cited": 1}, {"name": "First Day", "times_cited": 2}],
    ])
    out = _aggregate_competitor_brands(per_product, top_n=3)
    # Hiya: 3+1=4, Olly: 2, First Day: 2 → ordered by citation
    assert out[0] == "Hiya"
    assert "Olly" in out and "First Day" in out


def test_aggregate_respects_top_n():
    per_product = _make_per_product([
        [{"name": f"Brand{i}", "times_cited": 10 - i} for i in range(8)],
    ])
    out = _aggregate_competitor_brands(per_product, top_n=3)
    assert len(out) == 3
    assert out == ["Brand0", "Brand1", "Brand2"]


def test_aggregate_handles_missing_category_visibility():
    """Some per-product reports skip category_visibility (product_type
    missing). Don't crash; just contribute nothing."""
    per_product = [
        {"category_visibility": None},
        {"category_visibility": {"competitor_brands": [{"name": "Hiya", "times_cited": 1}]}},
    ]
    out = _aggregate_competitor_brands(per_product)
    assert out == ["Hiya"]


def test_aggregate_handles_garbage_entries():
    per_product = _make_per_product([
        [
            {"name": "Hiya", "times_cited": 2},
            {"name": "", "times_cited": 5},  # empty name → skip
            {"times_cited": 3},  # no name → skip
            "string instead of dict",  # garbage
            {"name": "Olly"},  # no times_cited → defaults to 1
        ],
    ])
    out = _aggregate_competitor_brands(per_product)
    assert out == ["Hiya", "Olly"]  # ordered by citation count


def test_aggregate_returns_empty_for_empty_input():
    assert _aggregate_competitor_brands([]) == []
    assert _aggregate_competitor_brands(_make_per_product([[]])) == []


# ---------------------------------------------------------------------------
# _build_domain_prompt
# ---------------------------------------------------------------------------


def test_domain_prompt_specifies_strict_format():
    p = _build_domain_prompt("Hiya")
    assert "Hiya" in p
    assert "bare hostname only" in p
    assert "JSON object" in p


# ---------------------------------------------------------------------------
# _resolve_competitor_domain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_competitor_domain_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
    out = await _resolve_competitor_domain("Hiya")
    assert out is None


def _make_fake_client(response_status: int, response_text: str):
    """Build a stand-in for httpx.AsyncClient that returns a canned
    response. Constructor must accept arbitrary args (httpx is called
    with `timeout=...`)."""
    class _FakeResponse:
        status_code = response_status
        text = response_text
        def json(self):
            return json.loads(response_text)

    class _FakeClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): return _FakeResponse()
    return _FakeClient


@pytest.mark.asyncio
async def test_resolve_competitor_domain_strips_scheme_and_www(monkeypatch):
    """Gemini sometimes returns full URL or www-prefixed; output must
    be bare hostname."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = _make_fake_client(200, json.dumps({
        "candidates": [{"content": {"parts": [
            {"text": '{"domain": "https://www.hiyahealth.com/", "confidence": "high"}'}
        ]}}]
    }))
    with patch("services.competitor_audit_orchestrator.httpx.AsyncClient", fake):
        domain = await _resolve_competitor_domain("Hiya")
    assert domain == "hiyahealth.com"


@pytest.mark.asyncio
async def test_resolve_competitor_domain_handles_null_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = _make_fake_client(200, json.dumps({
        "candidates": [{"content": {"parts": [
            {"text": '{"domain": null, "confidence": "low"}'}
        ]}}]
    }))
    with patch("services.competitor_audit_orchestrator.httpx.AsyncClient", fake):
        domain = await _resolve_competitor_domain("Brand With No D2C Site")
    assert domain is None


@pytest.mark.asyncio
async def test_resolve_competitor_domain_rejects_garbage_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake = _make_fake_client(200, json.dumps({
        "candidates": [{"content": {"parts": [
            {"text": "I cannot determine that brand's website."}
        ]}}]
    }))
    with patch("services.competitor_audit_orchestrator.httpx.AsyncClient", fake):
        domain = await _resolve_competitor_domain("Unknown")
    assert domain is None


@pytest.mark.asyncio
async def test_resolve_competitor_domain_handles_http_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class _FakeResponse:
        status_code = 500
        text = "internal error"

    class _FakeClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, *a, **kw): return _FakeResponse()

    with patch("services.competitor_audit_orchestrator.httpx.AsyncClient", _FakeClient):
        domain = await _resolve_competitor_domain("Hiya")
    assert domain is None


# ---------------------------------------------------------------------------
# enqueue_competitor_audits — cohort capping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_returns_early_for_no_competitors():
    out = await enqueue_competitor_audits(
        parent_audit_run_id="parent-1",
        competitor_brands=[],
    )
    assert out["audited"] == 0


@pytest.mark.asyncio
async def test_enqueue_returns_error_for_missing_parent_id():
    out = await enqueue_competitor_audits(
        parent_audit_run_id="",
        competitor_brands=["Hiya"],
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_enqueue_caps_cohort_at_hard_max():
    """User passes cohort_size=10; orchestrator caps at HARD_MAX=5."""
    captured_cohort_size: List[int] = []

    async def _fake_audit(brand, parent_audit_run_id, market, max_runs, category_override=None):
        return {"competitor_brand": brand, "status": "succeeded"}

    async def _fake_sem(*a, **kw):
        import asyncio
        return asyncio.Semaphore(10)

    with patch("services.competitor_audit_orchestrator._audit_one_competitor", new=AsyncMock(side_effect=_fake_audit)), \
         patch("services.agent_center_llm_client._get_per_merchant_semaphore", new=_fake_sem):
        out = await enqueue_competitor_audits(
            parent_audit_run_id="parent-1",
            competitor_brands=[f"Brand{i}" for i in range(10)],
            cohort_size=10,  # request 10
        )
    # Should have audited only 5 (HARD_MAX_COHORT_SIZE)
    assert out["cohort_size"] == 5
    assert out["succeeded"] == 5
