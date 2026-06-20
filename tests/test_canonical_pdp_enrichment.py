"""E1 — CanonicalPdpEnrichmentAgent: should_run gating + execute result shapes.

Mocks the Gemini call, the candidate fetch, and the persistence/publish calls
(upsert_enrichment + refresh_agent_pdp_view_for_content_key) so the test is
pure-logic — no DB, no real LLM. The compliance guard runs for real (it's a
pure keyword check) so the block path is exercised end-to-end.

Firing tests also patch `_merchant_enabled=True` — the executor ships behind a
safe-by-default rollout allowlist (dormant when the env var is unset).
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from services.executor_agents.base import ExecutorContext
from services.executor_agents.canonical_pdp_enrichment import (
    CanonicalPdpEnrichmentAgent,
    _ALLOWLIST_ENV,
    _merchant_enabled,
)

_MOD = "services.executor_agents.canonical_pdp_enrichment"


def _candidate(**overrides) -> Dict[str, Any]:
    base = {
        "merchant_id": "m1",
        "platform": "shopify",
        "source_product_id": "sp-1",
        "content_key": "ck-1",
        "title": "Good Night Collagen, 30 sticks",
        "description": "thin",
        "brand": "BB Lab",
        "product_type": "supplement",
        "category": "health",
    }
    base.update(overrides)
    return base


def _enrichment(**overrides) -> Dict[str, Any]:
    base = {
        "description_markdown": (
            "Low-molecular-weight collagen in single-serve sticks. Each box has "
            "30 sticks designed for daily use. Easy to take on the go; mixes into "
            "water or your morning drink. Made in Korea; halal-certified per the "
            "manufacturer. Suitable for adults looking to add collagen to a daily "
            "routine."
        ),
        "summary_short": "Low-molecular collagen, 30 single-serve sticks.",
        "bullet_points": ["30 sticks per box", "Single-serve", "Halal-certified"],
        "usage_scenarios": ["Daily morning routine", "On-the-go travel"],
        "audience_tags": ["adults", "collagen users"],
        "title_override": "Good Night Collagen (Low-Molecular), 30 Sticks",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# rollout allowlist (safe-by-default)
# ---------------------------------------------------------------------------


def test_merchant_enabled_semantics(monkeypatch):
    monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)
    assert _merchant_enabled("m1") is False  # dormant by default
    monkeypatch.setenv(_ALLOWLIST_ENV, "")
    assert _merchant_enabled("m1") is False
    monkeypatch.setenv(_ALLOWLIST_ENV, "*")
    assert _merchant_enabled("m1") is True  # widen step
    assert _merchant_enabled(None) is False
    monkeypatch.setenv(_ALLOWLIST_ENV, "m1, m2")
    assert _merchant_enabled("m1") is True  # staged rollout
    assert _merchant_enabled("m3") is False


# ---------------------------------------------------------------------------
# should_run gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_run_false_without_merchant_id():
    agent = CanonicalPdpEnrichmentAgent()
    assert await agent.should_run(ExecutorContext(merchant_id=None)) is False


@pytest.mark.asyncio
async def test_should_run_false_when_merchant_not_allowlisted():
    """Even with a key + candidates, a merchant outside the allowlist is gated."""
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=False), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_should_run_false_without_gemini_key():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value=None):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_should_run_false_when_no_candidates():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is False


@pytest.mark.asyncio
async def test_should_run_true_when_candidates_exist():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])):
        assert await agent.should_run(ExecutorContext(merchant_id="m1")) is True


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_skipped_when_not_allowlisted():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=False):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"
    assert "allowlist" in (result.error_message or "")


@pytest.mark.asyncio
async def test_execute_skipped_without_key():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value=None):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"


@pytest.mark.asyncio
async def test_execute_skipped_no_candidates():
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[])):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "skipped"
    assert result.evidence["reason"] == "no thin canonical PDPs to enrich"


@pytest.mark.asyncio
async def test_execute_enriches_and_publishes():
    """Happy path: generate → compliance OK → upsert → refresh (publish)."""
    agent = CanonicalPdpEnrichmentAgent()
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=_enrichment())), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))

    assert result.status == "succeeded"
    assert result.evidence["enriched_count"] == 1
    assert result.evidence["blocked_count"] == 0
    assert result.evidence["enriched"][0]["published"] is True
    upsert.assert_awaited_once()
    upsert_data = upsert.await_args.args[4]
    assert "Low-molecular-weight collagen" in upsert_data["description_markdown"]
    refresh.assert_awaited_once()
    assert refresh.await_args.args[0] == "ck-1"


@pytest.mark.asyncio
async def test_execute_blocks_noncompliant_copy():
    """A risky-keyword description is blocked by the compliance guard — never
    persisted or published."""
    agent = CanonicalPdpEnrichmentAgent()
    bad = _enrichment(
        description_markdown=(
            "This supplement offers a guaranteed return on your health and will "
            "cure fatigue for everyone who takes it daily over the long term, no "
            "exceptions, with results that compound month over month for years."
        )
    )
    upsert = AsyncMock()
    refresh = AsyncMock(return_value=True)
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=bad)), \
         patch("db.product_enrichment.upsert_enrichment", new=upsert), \
         patch("services.agent_pdp_view_assembler.refresh_agent_pdp_view_for_content_key", new=refresh):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))

    assert result.status == "failed"  # nothing enriched
    assert result.evidence["blocked_count"] == 1
    assert result.evidence["enriched_count"] == 0
    upsert.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_failed_generation_counts_but_does_not_crash():
    """A None generation (LLM failed/unparseable) lands in `failed`, not a crash."""
    agent = CanonicalPdpEnrichmentAgent()
    with patch(f"{_MOD}._merchant_enabled", return_value=True), \
         patch(f"{_MOD}._resolve_gemini_api_key", return_value="k"), \
         patch(f"{_MOD}._fetch_thin_canonical_pdps", new=AsyncMock(return_value=[_candidate()])), \
         patch(f"{_MOD}._generate_enrichment", new=AsyncMock(return_value=None)):
        result = await agent.execute(ExecutorContext(merchant_id="m1"))
    assert result.status == "failed"
    assert result.evidence["failed_count"] == 1
    assert result.evidence["enriched_count"] == 0


def test_agent_registered_in_dispatcher_and_worker():
    """Guard the two-place registration hazard: the agent must be in BOTH the
    dispatcher registry (which enqueues) and the worker registry (which
    executes) — or enqueued rows hit `unknown_agent` and never run."""
    from services.executor_agents.dispatcher import _registry
    from services.executor_run_worker import _agent_registry_by_name

    name = CanonicalPdpEnrichmentAgent().name
    assert name in {a.name for a in _registry()}
    assert name in _agent_registry_by_name()
