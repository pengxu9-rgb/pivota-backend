from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest


class FakeDatabase:
    def __init__(self, order: Dict[str, Any], existing_refund: Optional[Dict[str, Any]] = None) -> None:
        self.order = order
        self.existing_refund = existing_refund
        self.fetches: List[tuple[str, Dict[str, Any]]] = []
        self.executes: List[tuple[str, Dict[str, Any]]] = []

    async def fetch_one(self, query: str, values=None):
        self.fetches.append((str(query), dict(values or {})))
        if "FROM orders" in str(query):
            return self.order
        if "FROM refund_records" in str(query):
            return self.existing_refund
        return None

    async def execute(self, query: str, values=None):
        self.executes.append((str(query), dict(values or {})))
        return 1


@pytest.mark.asyncio
async def test_shopify_refund_webhook_for_stripe_order_is_observation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_webhook_routes as refund_webhooks
    from services.platform_refund_adapter import PlatformRefundEvent

    fake_db = FakeDatabase(
        {
            "order_id": "ORD_STRIPE_1",
            "merchant_id": "merch_1",
            "total": Decimal("2.22"),
            "total_refunded": Decimal("0.00"),
            "payment_status": "paid",
            "status": "paid",
            "payment_method": "stripe",
            "payment_intent_id": "cs_live_fake",
            "psp_used": "stripe",
            "metadata": {},
        }
    )
    logged_events = []

    async def fake_log_order_event(**kwargs):
        logged_events.append(kwargs)

    monkeypatch.setattr(refund_webhooks, "database", fake_db)
    monkeypatch.setattr(refund_webhooks, "log_order_event", fake_log_order_event)

    result = await refund_webhooks.process_platform_refund(
        PlatformRefundEvent(
            platform_type="shopify",
            platform_order_id="7531537269064",
            platform_refund_id="shopify_refund_1",
            amount=2.22,
            currency="EUR",
            raw_event={"id": "shopify_refund_1", "order_id": "7531537269064"},
        ),
        merchant_id="merch_1",
    )

    assert result["status"] == "ignored"
    assert result["reason"] == "shopify_refund_webhook_observation_only_external_psp"
    assert any("INSERT INTO refund_records" in sql for sql, _ in fake_db.executes)
    assert not any("UPDATE orders" in sql for sql, _ in fake_db.executes)
    insert_values = fake_db.executes[0][1]
    assert insert_values["amount"] == 2.22
    assert insert_values["error_message"] == "shopify_refund_webhook_observation_only_external_psp"
    assert logged_events[0]["event_type"] == "platform_refund_webhook_ignored"


@pytest.mark.asyncio
async def test_shopify_refund_webhook_overrun_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_webhook_routes as refund_webhooks
    from services.platform_refund_adapter import PlatformRefundEvent

    fake_db = FakeDatabase(
        {
            "order_id": "ORD_SHOPIFY_DONE",
            "merchant_id": "merch_1",
            "total": Decimal("4.07"),
            "total_refunded": Decimal("4.07"),
            "payment_status": "refunded",
            "status": "refunded",
            "payment_method": "shopify",
            "payment_intent_id": None,
            "psp_used": "shopify",
            "metadata": {},
        }
    )

    async def fake_log_order_event(**_kwargs):
        return None

    monkeypatch.setattr(refund_webhooks, "database", fake_db)
    monkeypatch.setattr(refund_webhooks, "log_order_event", fake_log_order_event)

    result = await refund_webhooks.process_platform_refund(
        PlatformRefundEvent(
            platform_type="shopify",
            platform_order_id="7531638980936",
            platform_refund_id="shopify_refund_2",
            amount=4.07,
            currency="EUR",
            raw_event={"id": "shopify_refund_2", "order_id": "7531638980936"},
        ),
        merchant_id="merch_1",
    )

    assert result["status"] == "ignored"
    assert result["reason"] == "shopify_refund_webhook_order_already_refunded"
    assert not any("UPDATE orders" in sql for sql, _ in fake_db.executes)


@pytest.mark.asyncio
async def test_shopify_refund_webhook_for_shopify_source_order_updates_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.refund_webhook_routes as refund_webhooks
    from services.platform_refund_adapter import PlatformRefundEvent

    fake_db = FakeDatabase(
        {
            "order_id": "ORD_SHOPIFY_1",
            "merchant_id": "merch_1",
            "total": Decimal("4.07"),
            "total_refunded": Decimal("0.00"),
            "payment_status": "paid",
            "status": "paid",
            "payment_method": "shopify",
            "payment_intent_id": None,
            "psp_used": "shopify",
            "metadata": {},
        }
    )

    async def fake_log_order_event(**_kwargs):
        return None

    monkeypatch.setattr(refund_webhooks, "database", fake_db)
    monkeypatch.setattr(refund_webhooks, "log_order_event", fake_log_order_event)

    result = await refund_webhooks.process_platform_refund(
        PlatformRefundEvent(
            platform_type="shopify",
            platform_order_id="7531638980936",
            platform_refund_id="shopify_refund_3",
            amount=4.07,
            currency="EUR",
            raw_event={"id": "shopify_refund_3", "order_id": "7531638980936"},
        ),
        merchant_id="merch_1",
    )

    assert result["status"] == "success"
    assert any("UPDATE orders" in sql for sql, _ in fake_db.executes)
