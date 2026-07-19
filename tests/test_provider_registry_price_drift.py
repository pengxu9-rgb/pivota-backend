"""Drift guard for #1508: the telemetry price table in provider_registry must
not disagree with the billing config.

provider_registry hardcoded per-1k token prices for llm_probe_runs.cost_usd that
had drifted from config/provider_credit_rates.json (gemini priced as "1.5 Flash"
$0.35/$1.05 vs config 2.5-flash $0.30/$2.50; chatgpt output $20 vs $30). The
registry now DERIVES its rates from the config loader, and this test asserts they
stay in lockstep: for every provider present in BOTH the registry and the config,
registry per-1k == config per-1M / 1000, at the provider level and for the
headline model_rates entry.
"""

from __future__ import annotations

import pytest

from services.llm_providers.provider_registry import get_provider, list_providers
from services.provider_credit_rates import provider_rate_entry


def _config_per_1k(provider_id):
    entry = provider_rate_entry(provider_id)
    return (
        float(entry["input_cost_usd_per_1m_tokens"]) / 1000.0,
        float(entry["output_cost_usd_per_1m_tokens"]) / 1000.0,
    )


def _providers_in_both():
    pairs = []
    for provider in list_providers(healthy_only=False):
        try:
            provider_rate_entry(provider.id)
        except ValueError:
            continue  # in the registry but not the billing config (e.g. "mock")
        pairs.append(provider.id)
    return pairs


def test_some_providers_are_in_both_places():
    # Guard against the parametrization silently collapsing to zero cases.
    both = _providers_in_both()
    assert {"gemini", "chatgpt", "claude", "deepseek"} <= set(both)


@pytest.mark.parametrize("provider_id", ["gemini", "chatgpt", "claude", "deepseek"])
def test_registry_provider_level_rate_matches_config(provider_id):
    provider = get_provider(provider_id)
    assert provider is not None
    exp_in, exp_out = _config_per_1k(provider_id)
    assert provider.cost_per_1k_input_tokens_usd == pytest.approx(exp_in), provider_id
    assert provider.cost_per_1k_output_tokens_usd == pytest.approx(exp_out), provider_id


@pytest.mark.parametrize(
    "provider_id,headline_model",
    [
        ("chatgpt", "chat-latest"),
        ("claude", "claude-sonnet-4"),
        ("deepseek", "deepseek-chat"),
    ],
)
def test_headline_model_rate_matches_config(provider_id, headline_model):
    """The headline model_rates entry (the SKU the config prices) tracks config
    too, so a telemetry caller passing the runtime model id can't undercount."""
    provider = get_provider(provider_id)
    assert provider is not None
    rates = provider.rate_for_model(headline_model)
    exp_in, exp_out = _config_per_1k(provider_id)
    assert rates["input_per_1k"] == pytest.approx(exp_in), (provider_id, headline_model)
    assert rates["output_per_1k"] == pytest.approx(exp_out), (provider_id, headline_model)


def test_drifted_gemini_15_flash_price_is_gone():
    """The specific stale values from the incident must not reappear."""
    gemini = get_provider("gemini")
    assert gemini.cost_per_1k_input_tokens_usd != pytest.approx(0.00035)
    assert gemini.cost_per_1k_output_tokens_usd != pytest.approx(0.00105)


def test_drifted_chatgpt_output_price_is_gone():
    chatgpt = get_provider("chatgpt")
    # Old (drifted) output was $20/1M => 0.02/1k; config is $30/1M => 0.03/1k.
    assert chatgpt.cost_per_1k_output_tokens_usd == pytest.approx(0.03)


def test_non_config_sku_keeps_code_owned_rate():
    """deepseek-reasoner is not in the billing config, so it keeps its
    code-owned constant rather than falling back to the flash rate."""
    provider = get_provider("deepseek")
    rates = provider.rate_for_model("deepseek-reasoner")
    assert rates["input_per_1k"] == pytest.approx(0.00055)
    assert rates["output_per_1k"] == pytest.approx(0.00219)
