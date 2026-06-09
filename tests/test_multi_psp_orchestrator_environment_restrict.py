"""Safety tests for restrict_environment on the PSP orchestrator (test-PSP probe).

The guarantee: with restrict_environment="test", a LIVE-keyed PSP row can NEVER be charged — it is
filtered out by the runtime secret-key prefix (the strong signal), even if its environment column lies.
If no test row exists, the orchestrator FAILS CLOSED rather than falling back to a live row.
"""
from __future__ import annotations

from decimal import Decimal

import pytest


class _FakeIntent:
    def __init__(self, provider: str):
        self.id = f"pi_{provider}"
        self.psp_type = provider
        self.client_secret = f"cs_{provider}"
        self.redirect_url = None
        self.status = "requires_action"
        self.raw_response = {}


def _install(monkeypatch, configs, attempted):
    from adapters import multi_psp_orchestrator as module

    class _FakeAdapter:
        def __init__(self, provider: str, api_key: str):
            self.provider = provider
            self.api_key = api_key

        async def create_payment_intent(self, *, amount, currency, metadata):
            attempted.append((self.provider, self.api_key))
            return True, _FakeIntent(self.provider), None

    async def fake_load(self, *, canonical_only: bool = False):
        self.psp_configs = list(configs)

    async def fake_db_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(module.MultiPSPOrchestrator, "load_psp_configs", fake_load)
    monkeypatch.setattr(module, "get_psp_adapter", lambda provider, api_key, **kwargs: _FakeAdapter(provider, api_key))
    monkeypatch.setattr(module.database, "execute", fake_db_execute)
    return module


@pytest.mark.asyncio
async def test_restrict_test_excludes_live_row_even_with_higher_priority(monkeypatch):
    """A live stripe row at priority 1 must be excluded; the test row is charged instead."""
    from adapters import multi_psp_orchestrator as module
    attempted: list = []
    configs = [
        module.PSPConfig(psp_type="stripe", api_key="sk_live_LIVE", priority=1, is_active=True, environment="live"),
        module.PSPConfig(psp_type="stripe", api_key="sk_test_TEST", priority=1, is_active=True, environment="test"),
    ]
    _install(monkeypatch, configs, attempted)
    orch = module.MultiPSPOrchestrator("merch_1")
    success, intent, error, psp_used = await orch.create_payment_intent(
        Decimal("1.69"), "USD", {"order_id": "ORD_1"},
        enforce_live_readiness=False, restrict_environment="test",
    )
    assert success is True and error is None
    # Only the TEST key was ever attempted; the live key was structurally excluded.
    assert attempted == [("stripe", "sk_test_TEST")]


@pytest.mark.asyncio
async def test_restrict_test_excludes_row_whose_column_lies_live_key(monkeypatch):
    """A row whose environment column says 'test' but holds an sk_live_ key is treated as LIVE → excluded."""
    from adapters import multi_psp_orchestrator as module
    attempted: list = []
    configs = [
        # Mislabeled: column 'test' but a real live key. Must be classified live and excluded → fail closed.
        module.PSPConfig(psp_type="stripe", api_key="sk_live_SNEAKY", priority=1, is_active=True, environment="test"),
    ]
    _install(monkeypatch, configs, attempted)
    orch = module.MultiPSPOrchestrator("merch_1")
    success, intent, error, psp_used = await orch.create_payment_intent(
        Decimal("1.69"), "USD", {"order_id": "ORD_2"},
        enforce_live_readiness=False, restrict_environment="test",
    )
    assert success is False
    assert attempted == []
    assert "test-environment PSP" in (error or "")


@pytest.mark.asyncio
async def test_restrict_test_fails_closed_when_no_test_row(monkeypatch):
    """No test row → empty candidate list → fail closed, never a live fallback."""
    from adapters import multi_psp_orchestrator as module
    attempted: list = []
    configs = [
        module.PSPConfig(psp_type="stripe", api_key="sk_live_ONLY", priority=1, is_active=True, environment="live"),
        module.PSPConfig(psp_type="adyen", api_key="live_ONLY", priority=2, is_active=True, environment="live"),
    ]
    _install(monkeypatch, configs, attempted)
    orch = module.MultiPSPOrchestrator("merch_1")
    success, intent, error, psp_used = await orch.create_payment_intent(
        Decimal("1.69"), "USD", {"order_id": "ORD_3"},
        enforce_live_readiness=False, restrict_environment="test",
    )
    assert success is False and attempted == []
    assert psp_used == "none"


@pytest.mark.asyncio
async def test_no_restriction_preserves_default_behavior(monkeypatch):
    """Without restrict_environment, behavior is unchanged (live row usable)."""
    from adapters import multi_psp_orchestrator as module
    attempted: list = []
    configs = [
        module.PSPConfig(psp_type="stripe", api_key="sk_live_OK", priority=1, is_active=True, environment="live"),
    ]
    _install(monkeypatch, configs, attempted)
    orch = module.MultiPSPOrchestrator("merch_1")
    success, intent, error, psp_used = await orch.create_payment_intent(
        Decimal("1.69"), "USD", {"order_id": "ORD_4"},
        enforce_live_readiness=False,  # no restrict_environment
    )
    assert success is True
    assert attempted == [("stripe", "sk_live_OK")]
