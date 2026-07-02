"""Livemode gate on the Stripe *billing* webhook.

Regression guard: in production the billing webhook must refuse test-mode
Stripe events (livemode=false) so a test-mode endpoint secret, a Stripe test
clock, or a smoke-test harness can never mutate live billing state. This
mirrors the order/PSP webhook guard in webhook_routes.py.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import stripe

from routes import billing_routes


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


def _install_common(monkeypatch: pytest.MonkeyPatch, event: Dict[str, Any]) -> List[str]:
    """Wire the webhook so it reaches the dispatch switch; record handler calls."""

    called: List[str] = []

    monkeypatch.setattr(
        billing_routes.settings, "stripe_billing_webhook_secret", "whsec_test", raising=False
    )
    monkeypatch.setattr(
        stripe.Webhook,
        "construct_event",
        staticmethod(lambda payload, signature, secret: event),
    )
    # Treat every event as freshly inserted (not a duplicate/retry).
    async def _inserted(*_a: Any, **_k: Any) -> bool:
        return True

    async def _mark_ignored(event_id: str, _db: Any) -> None:
        called.append(f"ignored:{event_id}")

    async def _handle(event_arg: Dict[str, Any], _db: Any) -> None:
        called.append("handled:checkout.session.completed")

    monkeypatch.setattr(billing_routes, "_insert_stripe_event", _inserted)
    monkeypatch.setattr(billing_routes, "_mark_event_ignored", _mark_ignored)
    monkeypatch.setattr(billing_routes, "_handle_checkout_session_completed", _handle)
    return called


@pytest.mark.asyncio
async def test_prod_ignores_test_mode_event(monkeypatch: pytest.MonkeyPatch) -> None:
    event = {"id": "evt_test1", "type": "checkout.session.completed", "livemode": False}
    called = _install_common(monkeypatch, event)
    monkeypatch.setenv("ENVIRONMENT", "production")

    resp = await billing_routes.handle_stripe_billing_webhook(
        _FakeRequest(b"{}"), stripe_signature="sig"
    )

    assert resp.status_code == 200
    assert b"test_mode_event_in_production" in resp.body
    # Handler must NOT run; event recorded as ignored.
    assert "handled:checkout.session.completed" not in called
    assert "ignored:evt_test1" in called


@pytest.mark.asyncio
async def test_prod_processes_live_event(monkeypatch: pytest.MonkeyPatch) -> None:
    event = {"id": "evt_live1", "type": "checkout.session.completed", "livemode": True}
    called = _install_common(monkeypatch, event)
    monkeypatch.setenv("ENVIRONMENT", "production")

    resp = await billing_routes.handle_stripe_billing_webhook(
        _FakeRequest(b"{}"), stripe_signature="sig"
    )

    assert resp.status_code == 200
    assert "handled:checkout.session.completed" in called


@pytest.mark.asyncio
async def test_non_prod_processes_test_mode_event(monkeypatch: pytest.MonkeyPatch) -> None:
    event = {"id": "evt_test2", "type": "checkout.session.completed", "livemode": False}
    called = _install_common(monkeypatch, event)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    resp = await billing_routes.handle_stripe_billing_webhook(
        _FakeRequest(b"{}"), stripe_signature="sig"
    )

    assert resp.status_code == 200
    # Outside production the guard is inert — test-mode events still process.
    assert "handled:checkout.session.completed" in called
