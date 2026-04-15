from datetime import datetime, timezone
from typing import Any, Dict

import pytest


@pytest.mark.asyncio
async def test_log_order_event_uses_metadata_currency_when_column_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    import db.products as products_module

    captured: Dict[str, Any] = {}

    async def fake_execute(query: Any) -> None:
        captured.update(query.compile().params)

    monkeypatch.setattr(products_module.database, "execute", fake_execute)

    await products_module.log_order_event(
        event_type="payment_succeeded",
        merchant_id="m_eur",
        order_id="ORD_EUR",
        metadata={"amount": 2.22, "currency": "eur"},
    )

    assert captured["currency"] == "EUR"


def test_agent_order_events_feed_normalizes_legacy_default_usd_from_metadata() -> None:
    from routes.agent_api import _normalize_order_event_feed_row

    row = {
        "id": 1,
        "event_type": "payment_succeeded",
        "merchant_id": "m_eur",
        "order_id": "ORD_EUR",
        "status": None,
        "total_amount": None,
        "currency": "USD",
        "payment_method": None,
        "error_message": None,
        "created_at": datetime.now(timezone.utc),
        "event_metadata": {"amount": 2.22, "currency": "EUR"},
        "order_currency": "EUR",
        "order_total": "2.22",
    }

    event = _normalize_order_event_feed_row(row)

    assert event["currency"] == "EUR"
    assert event["total_amount"] == 2.22
    assert "event_metadata" not in event
    assert "order_currency" not in event
    assert "order_total" not in event


def test_agent_order_events_feed_normalizes_legacy_default_usd_from_order() -> None:
    from routes.agent_api import _normalize_order_event_feed_row

    row = {
        "id": 2,
        "event_type": "shopify_order_created",
        "merchant_id": "m_eur",
        "order_id": "ORD_EUR",
        "status": None,
        "total_amount": None,
        "currency": "USD",
        "payment_method": None,
        "error_message": None,
        "created_at": datetime.now(timezone.utc),
        "event_metadata": {"shopify_order_id": "7531537269064"},
        "order_currency": "EUR",
        "order_total": "2.22",
    }

    event = _normalize_order_event_feed_row(row)

    assert event["currency"] == "EUR"
    assert event["total_amount"] == 2.22
