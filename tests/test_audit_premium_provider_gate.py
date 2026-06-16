"""Premium-provider labeling helpers.

ADR-005: premium providers (ChatGPT/Claude) are gated by credit BALANCE, not
plan tier — the `_maybe_premium_block` plan-gate was removed. `premium_providers`
/ `premium_providers_requested` now only *label* the higher-cost providers (for
the cost preview + portal); they no longer gate access. These tests cover that
labeling. The balance-gated launch behavior is covered in
test_phase2_audit_runs_endpoints.py.
"""

from __future__ import annotations

from services.coverage_profiles import (
    premium_providers,
    premium_providers_requested,
)


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
