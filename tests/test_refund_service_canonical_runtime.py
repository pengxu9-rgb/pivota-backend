from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.refund_service import RefundService


class _FakeRefundDB:
    async def fetch_one(self, query: str, values=None):
        return None


@pytest.mark.asyncio
async def test_wave1_refunds_fail_closed_without_canonical_psp(monkeypatch: pytest.MonkeyPatch) -> None:
    service = RefundService(database=_FakeRefundDB())

    result = await service._process_psp_refund(
        {
            "merchant_id": "merch_1",
            "psp_used": "stripe",
            "payment_intent_id": "pi_live_123",
        },
        refund_id="ref_1",
        amount=10.0,
        reason="requested_by_customer",
    )

    assert result["success"] is False
    assert "Canonical merchant_psps configuration is missing for stripe refunds" in result["error"]


@pytest.mark.asyncio
async def test_paypal_refunds_keep_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.refund_service as module

    service = RefundService(database=_FakeRefundDB())
    captured = {}

    class _FakePayPalAdapter:
        async def refund_payment(self, *, payment_intent_id, amount, reason, idempotency_key=None):
            captured["payment_intent_id"] = payment_intent_id
            captured["amount"] = str(amount)
            captured["reason"] = reason
            return True, "rfnd_paypal_123", None

    def fake_get_psp_adapter(provider: str, api_key: str, **kwargs):
        captured["provider"] = provider
        captured["api_key"] = api_key
        captured["kwargs"] = kwargs
        return _FakePayPalAdapter()

    monkeypatch.setattr(module, "get_psp_adapter", fake_get_psp_adapter)
    monkeypatch.setattr(
        module,
        "settings",
        SimpleNamespace(
            paypal_client_id="paypal_client_live",
            paypal_api_key=None,
            paypal_client_secret="paypal_secret_live",
            paypal_sandbox=False,
        ),
    )

    result = await service._process_psp_refund(
        {
            "merchant_id": "merch_1",
            "psp_used": "paypal",
            "payment_intent_id": "pay_paypal_123",
        },
        refund_id="ref_paypal_1",
        amount=12.5,
        reason="requested_by_customer",
    )

    assert result == {"success": True, "refund_id": "rfnd_paypal_123"}
    assert captured["provider"] == "paypal"
    assert captured["api_key"] == "paypal_client_live"
    assert captured["kwargs"]["client_secret"] == "paypal_secret_live"
    assert captured["kwargs"]["environment"] == "live"
    assert captured["kwargs"]["is_sandbox"] is False
