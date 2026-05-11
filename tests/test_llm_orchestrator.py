"""Tests for the LLM orchestration layer (PR-7-orchestrator).

Coverage:
  - Provider registry registration + lookup + health updates
  - Suitability ranking + cost/latency tie-breaking
  - Each orchestration strategy (single_best / cost_optimized /
    cross_validate / fallback_chain)
  - Cost-cap fallback to cheapest provider
  - parse_provider_spec parses "auto:strategy" form
  - estimate_strategy_cost_usd computes correctly
  - Backwards compat: provider="gemini" / "deepseek" still routes
    directly without engaging orchestrator
  - Provider dispatch via agent_center_llm_client.probe(provider="auto")
    correctly resolves through orchestrator + recurses
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_provider_registry_per_test():
    """Re-seed the registry before each test so tests that mutate
    state (register_provider, update_provider_health) don't leak
    into subsequent tests."""
    from services.llm_providers import provider_registry
    provider_registry._reset_registry_for_test()
    provider_registry._seed_default_providers()
    yield
    # No teardown needed — next test will re-seed


# ---------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------


def test_default_providers_seeded_at_import():
    """gemini, deepseek, mock are pre-registered."""
    from services.llm_providers.provider_registry import list_providers
    ids = {p.id for p in list_providers()}
    assert "gemini" in ids
    assert "deepseek" in ids
    assert "mock" in ids


def test_register_provider_replaces_existing():
    from services.llm_providers.provider_registry import (
        LLMProvider, register_provider, get_provider,
        BACKEND_DEEPSEEK_LOCAL, HEALTH_HEALTHY,
    )
    custom = LLMProvider(
        id="gemini",  # same id as default
        display_name="Custom Gemini Override",
        backend=BACKEND_DEEPSEEK_LOCAL,
        cost_per_1k_input_tokens_usd=0.001,
        cost_per_1k_output_tokens_usd=0.002,
        avg_latency_ms_per_probe=500,
        rate_limit_per_minute=10,
        health_status=HEALTH_HEALTHY,
    )
    register_provider(custom)
    p = get_provider("gemini")
    assert p.display_name == "Custom Gemini Override"


def test_register_provider_rejects_non_provider():
    from services.llm_providers.provider_registry import register_provider
    with pytest.raises(TypeError):
        register_provider({"id": "broken"})


def test_list_providers_filters_unhealthy_by_default():
    from services.llm_providers.provider_registry import (
        list_providers, update_provider_health,
        HEALTH_UNAVAILABLE, HEALTH_HEALTHY,
    )
    update_provider_health("deepseek", HEALTH_UNAVAILABLE)
    healthy_ids = {p.id for p in list_providers()}
    assert "deepseek" not in healthy_ids
    # Restore for subsequent tests
    update_provider_health("deepseek", HEALTH_HEALTHY)


def test_list_providers_can_include_unhealthy():
    from services.llm_providers.provider_registry import (
        list_providers, update_provider_health,
        HEALTH_DEGRADED, HEALTH_HEALTHY,
    )
    update_provider_health("deepseek", HEALTH_DEGRADED)
    all_ids = {p.id for p in list_providers(healthy_only=False)}
    assert "deepseek" in all_ids
    update_provider_health("deepseek", HEALTH_HEALTHY)


def test_update_provider_health_with_invalid_status_noop():
    """Defensive: invalid status string doesn't corrupt the
    registry."""
    from services.llm_providers.provider_registry import (
        update_provider_health, get_provider,
    )
    update_provider_health("gemini", "bogus_status")
    # Should still be healthy
    p = get_provider("gemini")
    assert p.health_status in {"healthy", "degraded", "unavailable"}


def test_provider_suitability_weight():
    """Suitability ratings translate to numerical weights for
    ranking."""
    from services.llm_providers.provider_registry import (
        LLMProvider, BACKEND_DEEPSEEK_LOCAL, HEALTH_HEALTHY,
        SUITABILITY_EXCELLENT, SUITABILITY_GOOD, SUITABILITY_NONE,
    )
    p = LLMProvider(
        id="x", display_name="X", backend=BACKEND_DEEPSEEK_LOCAL,
        cost_per_1k_input_tokens_usd=0, cost_per_1k_output_tokens_usd=0,
        avg_latency_ms_per_probe=0, rate_limit_per_minute=10,
        health_status=HEALTH_HEALTHY,
        suitable_for_scan_modes={
            "open_product_visibility_test": SUITABILITY_EXCELLENT,
            "category_visibility_test": SUITABILITY_GOOD,
        },
    )
    assert p.suitability_weight("open_product_visibility_test") == 3
    assert p.suitability_weight("category_visibility_test") == 2
    # Unrated scan_mode falls through to NONE = 0
    assert p.suitability_weight("nonexistent_scan_mode") == 0


def test_provider_estimated_cost_per_probe():
    from services.llm_providers.provider_registry import get_provider
    deepseek = get_provider("deepseek")
    cost = deepseek.estimated_cost_per_probe_usd(
        expected_input_tokens=1000, expected_output_tokens=500,
    )
    expected = (1.0 * 0.00014) + (0.5 * 0.00028)
    assert abs(cost - expected) < 1e-9


# ---------------------------------------------------------------------
# Orchestration strategies
# ---------------------------------------------------------------------


def test_single_best_strategy_picks_excellent_over_good():
    """Gemini is rated 'excellent' for category_visibility_test;
    Deepseek 'good'. Single-best picks Gemini."""
    from services.llm_providers.orchestrator import (
        select_provider, STRATEGY_SINGLE_BEST,
    )
    chosen = select_provider(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_SINGLE_BEST,
    )
    assert chosen == "gemini"


def test_cost_optimized_picks_cheaper_when_both_at_least_good():
    """For open_product_visibility_test, Deepseek is 'good' and
    Gemini is 'excellent'. cost_optimized prefers cheaper Deepseek
    since both meet the 'good' threshold."""
    from services.llm_providers.orchestrator import (
        select_provider, STRATEGY_COST_OPTIMIZED,
    )
    chosen = select_provider(
        scan_mode="open_product_visibility_test",
        strategy=STRATEGY_COST_OPTIMIZED,
    )
    assert chosen == "deepseek"


def test_cost_optimized_wins_form_factor_classification():
    """Deepseek is rated 'excellent' AND cheaper for
    form_factor_classification — wins on both axes."""
    from services.llm_providers.orchestrator import (
        select_provider, STRATEGY_COST_OPTIMIZED,
    )
    chosen = select_provider(
        scan_mode="form_factor_classification",
        strategy=STRATEGY_COST_OPTIMIZED,
    )
    assert chosen == "deepseek"


def test_cross_validate_returns_top_two_providers():
    """Cross-validate strategy returns the top 2 ranked providers
    so caller invokes both in parallel."""
    from services.llm_providers.orchestrator import (
        select_providers, STRATEGY_CROSS_VALIDATE,
    )
    chosen = select_providers(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_CROSS_VALIDATE,
    )
    assert len(chosen) == 2
    assert "gemini" in chosen
    assert "deepseek" in chosen


def test_fallback_chain_returns_ordered_primary_secondary():
    """Fallback chain returns providers in suitability rank order
    so dispatcher can cascade primary → secondary on failure."""
    from services.llm_providers.orchestrator import (
        select_providers, STRATEGY_FALLBACK_CHAIN,
    )
    chosen = select_providers(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_FALLBACK_CHAIN,
    )
    # Gemini (excellent) ranks before Deepseek (good)
    assert chosen[0] == "gemini"


def test_unknown_strategy_raises():
    from services.llm_providers.orchestrator import select_provider
    with pytest.raises(ValueError):
        select_provider(scan_mode="x", strategy="bogus_strategy")


def test_select_raises_when_no_provider_suitable():
    """When no provider is rated above 'none' for the scan mode,
    raise so caller can decide to fall back manually."""
    from services.llm_providers.orchestrator import (
        OrchestratorSelectionError, select_provider,
        STRATEGY_SINGLE_BEST,
    )
    with pytest.raises(OrchestratorSelectionError):
        select_provider(
            scan_mode="bogus_unrated_scan_mode_does_not_exist",
            strategy=STRATEGY_SINGLE_BEST,
        )


def test_cost_optimized_raises_when_no_provider_at_least_good():
    """cost_optimized requires at least 'good' rating; if no provider
    meets that bar, raise rather than silently pick a 'limited'
    provider."""
    from services.llm_providers.orchestrator import (
        OrchestratorSelectionError, select_provider,
        STRATEGY_COST_OPTIMIZED,
    )
    with pytest.raises(OrchestratorSelectionError):
        select_provider(
            scan_mode="bogus_unrated_scan_mode",
            strategy=STRATEGY_COST_OPTIMIZED,
        )


def test_cost_capped_picks_cheapest_regardless_of_suitability():
    """When cost cap is engaged, fall through to cheapest healthy
    provider — even if 'mock' is the cheapest. We'd rather emit a
    degraded result than refuse the audit."""
    from services.llm_providers.orchestrator import select_provider
    chosen = select_provider(
        scan_mode="category_visibility_test",
        strategy="single_best",
        merchant_id="merch_test",
        cost_capped=True,
    )
    # Cheapest healthy provider is mock (0 cost)
    assert chosen == "mock"


# ---------------------------------------------------------------------
# parse_provider_spec
# ---------------------------------------------------------------------


def test_parse_provider_spec_simple():
    from services.llm_providers.orchestrator import parse_provider_spec
    pid, strategy = parse_provider_spec("gemini")
    assert pid == "gemini"
    assert strategy is None


def test_parse_provider_spec_with_strategy():
    from services.llm_providers.orchestrator import parse_provider_spec
    pid, strategy = parse_provider_spec("auto:cost_optimized")
    assert pid == "auto"
    assert strategy == "cost_optimized"


def test_parse_provider_spec_auto_no_strategy():
    from services.llm_providers.orchestrator import parse_provider_spec
    pid, strategy = parse_provider_spec("auto")
    assert pid == "auto"
    assert strategy is None


# ---------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------


def test_estimate_strategy_cost_single_best():
    from services.llm_providers.orchestrator import (
        estimate_strategy_cost_usd, STRATEGY_SINGLE_BEST,
    )
    # Single-best for category_visibility_test picks gemini
    cost = estimate_strategy_cost_usd(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_SINGLE_BEST,
    )
    # Gemini at default 500 in / 300 out tokens
    expected = (0.5 * 0.00035) + (0.3 * 0.00105)
    assert abs(cost - expected) < 1e-9


def test_estimate_strategy_cost_cross_validate_higher_than_single():
    """cross_validate runs N providers in parallel — total cost is
    the sum across all selected providers."""
    from services.llm_providers.orchestrator import (
        estimate_strategy_cost_usd,
        STRATEGY_SINGLE_BEST, STRATEGY_CROSS_VALIDATE,
    )
    single_cost = estimate_strategy_cost_usd(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_SINGLE_BEST,
    )
    multi_cost = estimate_strategy_cost_usd(
        scan_mode="category_visibility_test",
        strategy=STRATEGY_CROSS_VALIDATE,
    )
    assert multi_cost > single_cost


# ---------------------------------------------------------------------
# Backwards compat: direct provider routing still works
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_with_explicit_gemini_provider_does_not_engage_orchestrator(
    monkeypatch,
):
    """provider='gemini' bypasses the orchestrator — routes directly
    to upstream PIVOTA-Agent (existing behavior preserved)."""
    from services import agent_center_llm_client
    from services.llm_providers import orchestrator
    from config.settings import settings

    # Spy on orchestrator.select_provider to confirm it's NOT called
    orchestrator_called = {"yes": False}
    real_select = orchestrator.select_provider

    def spy_select(**kwargs):
        orchestrator_called["yes"] = True
        return real_select(**kwargs)
    monkeypatch.setattr(orchestrator, "select_provider", spy_select)

    # Need an API key so the upstream path engages (we'll mock httpx
    # to fail cleanly — the test only cares orchestrator wasn't
    # called, not whether the HTTP succeeds)
    monkeypatch.setattr(settings, "pivota_agent_internal_api_key", "test-key")

    # Mock httpx to raise so we don't actually hit the network
    async def fake_post(*args, **kwargs):
        from httpx import HTTPError
        raise HTTPError("simulated")

    import httpx

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *args, **kwargs):
            await fake_post(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    with pytest.raises(agent_center_llm_client.AgentCenterLlmClientError):
        await agent_center_llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="x", merchant_id="m", store_id="s",
            provider="gemini", max_runs=1,
        )
    # Orchestrator was NOT engaged — direct route preserved
    assert orchestrator_called["yes"] is False


@pytest.mark.asyncio
async def test_probe_with_auto_provider_engages_orchestrator(monkeypatch):
    """provider='auto' triggers orchestrator selection + recursive
    dispatch with the chosen provider."""
    from services import agent_center_llm_client
    from services.llm_providers import orchestrator, deepseek_probe
    from config.settings import settings

    # Set up so deepseek is routable
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")

    # Mock the actual probe call so we don't hit Deepseek
    async def fake_probe_one_scan_mode(**kwargs):
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

    # Orchestrator should pick deepseek for cost_optimized + open
    # product visibility (both rated, deepseek cheaper)
    result = await agent_center_llm_client.probe(
        scan_mode="open_product_visibility_test",
        scan_target_id="x",
        merchant_id="m",
        store_id="s",
        context={
            "product_title": "Test Product",
            "merchant_brand": "X",
            "merchant_pdp_url": "https://x.com/p",
        },
        provider="auto:cost_optimized",
        max_runs=1,
    )
    # Result came back from Deepseek (orchestrator picked it for cost)
    assert result["provider"] == "deepseek"


@pytest.mark.asyncio
async def test_probe_with_auto_default_strategy_picks_single_best(monkeypatch):
    """provider='auto' (no strategy override) defaults to single_best
    which picks the highest-rated provider."""
    from services import agent_center_llm_client
    from services.llm_providers import orchestrator
    from config.settings import settings

    # Spy on the orchestrator to capture which strategy was used
    captured_strategy = {}
    real_select = orchestrator.select_provider

    def spy_select(**kwargs):
        captured_strategy.update(kwargs)
        return "deepseek"  # short-circuit downstream
    monkeypatch.setattr(orchestrator, "select_provider", spy_select)

    # Set up deepseek so the recursive dispatch resolves
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    from services.llm_providers import deepseek_probe

    async def fake_probe_one_scan_mode(**kwargs):
        return {"scan_mode": kwargs.get("scan_mode"),
                "provider": "deepseek", "runs_count": 1,
                "scores": {"visibility_score": 0, "attribution_echo_rate": 0},
                "findings": [], "usage": {"input_tokens": 0, "output_tokens": 0},
                "raw_runs": []}
    monkeypatch.setattr(deepseek_probe, "probe_one_scan_mode", fake_probe_one_scan_mode)

    await agent_center_llm_client.probe(
        scan_mode="category_visibility_test",
        scan_target_id="x", merchant_id="m", store_id="s",
        context={"product_title": "X", "product_type": "y"},
        provider="auto",  # no explicit strategy
        max_runs=1,
    )
    assert captured_strategy.get("strategy") == "single_best"
