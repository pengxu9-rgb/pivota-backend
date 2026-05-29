from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from utils.email_sender import EmailSendResult
from utils.order_track_token import verify_order_track_token


def _order(**overrides):
    base = {
        "order_id": "ORD_EMAIL_1",
        "merchant_id": "merch_email",
        "customer_name": "Buyer Example",
        "customer_email": "buyer@example.com",
        "items": [{"title": "Test Item", "quantity": 2}],
        "total": "45.20",
        "currency": "USD",
        "metadata": {},
        "created_at": datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_order_confirmation_email_once_skips_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.order_confirmation_email_service as service

    send_calls: list[dict] = []

    async def fake_get_order(_order_id: str):
        raise AssertionError("flag-off send should not load the order")

    def fake_send_email(**kwargs):
        send_calls.append(kwargs)
        return EmailSendResult(ok=True, provider="test", message_id="msg_1")

    monkeypatch.delenv("ORDER_CONFIRMATION_EMAIL_ENABLED", raising=False)
    monkeypatch.setattr(service, "get_order", fake_get_order)
    monkeypatch.setattr(service, "send_email", fake_send_email)

    result = await service.send_order_confirmation_email_once("ORD_EMAIL_1")

    assert result.ok is False
    assert result.error == "DISABLED"
    assert send_calls == []


@pytest.mark.asyncio
async def test_order_confirmation_email_once_skips_when_already_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.order_confirmation_email_service as service

    send_calls: list[dict] = []

    async def fake_get_order(_order_id: str):
        return _order(metadata={"order_confirmation_email_sent_at": "2026-05-29T12:00:00+00:00"})

    def fake_send_email(**kwargs):
        send_calls.append(kwargs)
        return EmailSendResult(ok=True, provider="test", message_id="msg_1")

    monkeypatch.setenv("ORDER_CONFIRMATION_EMAIL_ENABLED", "true")
    monkeypatch.setattr(service, "get_order", fake_get_order)
    monkeypatch.setattr(service, "send_email", fake_send_email)

    result = await service.send_order_confirmation_email_once("ORD_EMAIL_1")

    assert result.ok is False
    assert result.error == "ALREADY_SENT"
    assert send_calls == []


@pytest.mark.asyncio
async def test_order_confirmation_email_once_sends_and_marks_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.order_confirmation_email_service as service

    send_calls: list[dict] = []
    updates: list[tuple[str, dict]] = []

    async def fake_get_order(_order_id: str):
        return _order()

    async def fake_get_primary_store(_merchant_id: str):
        return {
            "name": "Demo Store",
            "support_email": "support@example.com",
            "domain": "demo.example.com",
        }

    async def fake_update_order(order_id: str, update_data: dict) -> bool:
        updates.append((order_id, update_data))
        return True

    def fake_send_email(**kwargs):
        send_calls.append(kwargs)
        return EmailSendResult(ok=True, provider="test", message_id="msg_1")

    monkeypatch.setenv("ORDER_CONFIRMATION_EMAIL_ENABLED", "true")
    monkeypatch.setenv("ORDER_TRACK_TOKEN_SECRET", "test-order-track-secret")
    monkeypatch.setenv("CHECKOUT_UI_BASE_URL", "https://agent.pivota.cc")
    monkeypatch.setenv("FROM_EMAIL", "noreply@example.com")
    monkeypatch.setattr(service, "get_order", fake_get_order)
    monkeypatch.setattr(service, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(service, "update_order", fake_update_order)
    monkeypatch.setattr(service, "send_email", fake_send_email)

    result = await service.send_order_confirmation_email_once("ORD_EMAIL_1")

    assert result.ok is True
    assert send_calls[0]["to_email"] == "buyer@example.com"
    assert send_calls[0]["subject"] == "Your Pivota order is confirmed"
    assert send_calls[0]["from_email"] == "noreply@example.com"
    assert send_calls[0]["reply_to"] == "support@example.com"
    assert "https://agent.pivota.cc/order/track?token=" in send_calls[0]["text_body"]

    parsed = urlparse(send_calls[0]["text_body"].split("Track your order: ", 1)[1].splitlines()[0])
    token = parse_qs(parsed.query)["token"][0]
    assert verify_order_track_token(token) == "ORD_EMAIL_1"

    assert updates[0][0] == "ORD_EMAIL_1"
    metadata = updates[0][1]["metadata"]
    assert metadata["order_confirmation_email_provider"] == "test"
    assert metadata["order_confirmation_email_message_id"] == "msg_1"
    assert metadata["order_confirmation_email_sent_at"]
