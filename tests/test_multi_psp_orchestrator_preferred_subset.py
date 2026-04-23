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


@pytest.mark.asyncio
async def test_orchestrator_restricts_to_preferred_psps_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import multi_psp_orchestrator as module

    attempted: list[str] = []

    class _FakeAdapter:
        def __init__(self, provider: str):
            self.provider = provider

        async def create_payment_intent(self, *, amount, currency, metadata):
            attempted.append(self.provider)
            if self.provider == "adyen":
                return True, _FakeIntent(self.provider), None
            raise AssertionError("restrict_to_preferred_psps should not fall through to non-preferred providers")

    async def fake_load(self, *, canonical_only: bool = False):
        self.psp_configs = [
            module.PSPConfig(psp_type="stripe", api_key="sk_live_123", priority=1, is_active=True),
            module.PSPConfig(psp_type="adyen", api_key="AQE_test_123", priority=2, is_active=True),
            module.PSPConfig(psp_type="checkout", api_key="sk_sbox_123", priority=3, is_active=True),
        ]

    async def fake_db_execute(*args, **kwargs):
        return None

    monkeypatch.setattr(module.MultiPSPOrchestrator, "load_psp_configs", fake_load)
    monkeypatch.setattr(module, "get_psp_adapter", lambda provider, api_key, **kwargs: _FakeAdapter(provider))
    monkeypatch.setattr(module.database, "execute", fake_db_execute)

    orchestrator = module.MultiPSPOrchestrator("merch_1")
    success, payment_intent, error, psp_used = await orchestrator.create_payment_intent(
        Decimal("1.09"),
        "USD",
        {"order_id": "ORD_1"},
        preferred_psps=["adyen"],
        restrict_to_preferred_psps=True,
    )

    assert success is True
    assert error is None
    assert psp_used == "adyen"
    assert payment_intent.id == "pi_adyen"
    assert attempted == ["adyen"]
