"""Premium-provider paywall for agent-center audits.

Tiered model: free accounts run Gemini only; ChatGPT/Claude require a paid plan.
A free account that requests a premium provider must get a 402 (subscribe), not
a silent downgrade. Tests the pure helpers + the route's block decision.
"""

from __future__ import annotations

from fastapi import HTTPException

from services.coverage_profiles import (
    premium_providers,
    premium_providers_requested,
)
from routes.audit_runs_routes import _maybe_premium_block


# --- premium_providers / premium_providers_requested (pure) ----------------

def test_default_premium_set_is_chatgpt_and_claude():
    premium = set(premium_providers())
    assert "chatgpt" in premium
    assert "claude" in premium
    # Free / cheap providers are NOT premium.
    assert "gemini" not in premium
    assert "deepseek" not in premium


def test_requested_premium_empty_for_free_providers_only():
    assert premium_providers_requested(["gemini"]) == []
    assert premium_providers_requested(["gemini", "deepseek"]) == []
    assert premium_providers_requested([]) == []


def test_requested_premium_detects_chatgpt_and_claude():
    assert premium_providers_requested(["gemini", "chatgpt"]) == ["chatgpt"]
    assert premium_providers_requested(["gemini", "claude"]) == ["claude"]


def test_requested_premium_normalizes_case_and_dedupes():
    assert premium_providers_requested(["ChatGPT", "chatgpt", " GEMINI "]) == ["chatgpt"]


# --- _maybe_premium_block (route decision) ---------------------------------

def test_no_block_for_gemini_only_free_account():
    assert _maybe_premium_block(
        paid_tier=False, providers=["gemini"], verify_providers=["deepseek"],
    ) is None


def test_no_block_for_paid_account_even_with_premium():
    assert _maybe_premium_block(
        paid_tier=True, providers=["gemini", "chatgpt"], verify_providers=[],
    ) is None


def test_block_free_account_requesting_chatgpt():
    block = _maybe_premium_block(
        paid_tier=False, providers=["gemini", "chatgpt"], verify_providers=[],
    )
    assert isinstance(block, HTTPException)
    assert block.status_code == 402
    assert block.detail["code"] == "premium_provider_subscription_required"
    assert block.detail["premium_providers_requested"] == ["chatgpt"]
    assert block.detail["free_alternative_provider"] == "gemini"


def test_block_free_account_when_premium_only_in_verify_providers():
    block = _maybe_premium_block(
        paid_tier=False, providers=["gemini"], verify_providers=["claude"],
    )
    assert isinstance(block, HTTPException)
    assert block.status_code == 402
    assert block.detail["premium_providers_requested"] == ["claude"]
