from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest


def _order(**overrides: Any) -> Dict[str, Any]:
    return {
        "order_id": "ORD_VERIFY_PAYMENT",
        "merchant_id": "m_verify",
        "payment_intent_id": "pi_verify",
        "total": "55.10",
        "currency": "EUR",
        **overrides,
    }


class _DetailsAdapter:
    def __init__(self, *, ok: bool = True, status: str = "succeeded", amount: str | None = "55.10", currency: str | None = "EUR", error: str | None = None):
        self.ok = ok
        self.status = status
        self.amount = amount
        self.currency = currency
        self.error = error

    async def get_payment_status_details(self, payment_reference: str):
        return (
            self.ok,
            {
                "status": self.status,
                "amount": self.amount,
                "currency": self.currency,
            },
            self.error,
        )


class _LegacyTupleAdapter:
    async def get_payment_status(self, payment_reference: str):
        return True, "succeeded", None


@pytest.mark.asyncio
async def test_verify_order_payment_succeeded_checks_amount_and_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as order_routes

    async def fake_resolve(order: Dict[str, Any]):
        return "stripe", _DetailsAdapter()

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", fake_resolve)

    assert await order_routes.verify_order_payment_succeeded(_order()) == (True, "succeeded", None)


@pytest.mark.asyncio
async def test_verify_order_payment_succeeded_rejects_amount_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as order_routes

    async def fake_resolve(order: Dict[str, Any]):
        return "stripe", _DetailsAdapter(amount="54.10")

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", fake_resolve)

    ok, status, error = await order_routes.verify_order_payment_succeeded(_order())

    assert ok is False
    assert status == "succeeded"
    assert "amount mismatch" in str(error)


@pytest.mark.asyncio
async def test_verify_order_payment_succeeded_rejects_currency_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as order_routes

    async def fake_resolve(order: Dict[str, Any]):
        return "stripe", _DetailsAdapter(currency="USD")

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", fake_resolve)

    ok, status, error = await order_routes.verify_order_payment_succeeded(_order())

    assert ok is False
    assert status == "succeeded"
    assert "currency mismatch" in str(error)


@pytest.mark.asyncio
async def test_verify_order_payment_succeeded_keeps_legacy_status_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.order_routes as order_routes

    async def fake_resolve(order: Dict[str, Any]):
        return "legacy", _LegacyTupleAdapter()

    monkeypatch.setattr(order_routes, "_resolve_order_psp_adapter", fake_resolve)

    assert await order_routes.verify_order_payment_succeeded(_order()) == (True, "succeeded", None)


@pytest.mark.asyncio
async def test_stripe_payment_status_details_from_payment_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    import adapters.psp_adapter as psp_adapter

    adapter = psp_adapter.StripeAdapter("sk_test_fake")

    async def fake_to_thread(fn, *args, **kwargs):
        return SimpleNamespace(
            id="pi_verify",
            status="succeeded",
            amount=5510,
            amount_received=5510,
            currency="eur",
        )

    monkeypatch.setattr(psp_adapter.asyncio, "to_thread", fake_to_thread)

    ok, details, error = await adapter.get_payment_status_details("pi_verify")

    assert ok is True
    assert error is None
    assert details["status"] == "succeeded"
    assert details["amount"] == "55.10"
    assert details["currency"] == "EUR"


@pytest.mark.asyncio
async def test_stripe_payment_status_details_from_checkout_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import adapters.psp_adapter as psp_adapter

    adapter = psp_adapter.StripeAdapter("sk_test_fake")

    async def fake_to_thread(fn, *args, **kwargs):
        return SimpleNamespace(
            id="cs_verify",
            payment_status="paid",
            amount_total=5510,
            currency="eur",
            payment_intent=SimpleNamespace(
                id="pi_verify",
                status="succeeded",
                amount_received=5510,
                currency="eur",
            ),
        )

    monkeypatch.setattr(psp_adapter.asyncio, "to_thread", fake_to_thread)

    ok, details, error = await adapter.get_payment_status_details("cs_verify")

    assert ok is True
    assert error is None
    assert details["status"] == "succeeded"
    assert details["amount"] == "55.10"
    assert details["currency"] == "EUR"
    assert details["payment_reference_type"] == "stripe_checkout_session"
