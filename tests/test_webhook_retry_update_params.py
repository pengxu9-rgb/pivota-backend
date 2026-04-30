from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest


class _ExistingDeliveryDatabase:
    def __init__(self) -> None:
        self.executed_query: Optional[str] = None
        self.executed_values: Optional[Dict[str, Any]] = None

    async def fetch_val(self, query: str, values: Dict[str, Any]) -> int:
        assert "delivery_id" in values
        return 1

    async def execute(self, query: str, values: Dict[str, Any]) -> None:
        self.executed_query = query
        self.executed_values = dict(values)


@pytest.mark.asyncio
async def test_agent_webhook_retry_update_filters_insert_only_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.agent_webhook_service as module

    fake_db = _ExistingDeliveryDatabase()
    monkeypatch.setattr(module, "database", fake_db)

    await module._persist_delivery_attempt(
        delivery_id="whd_agent_retry",
        agent_id="agent_1",
        event_id="evt_agent_retry",
        event_type="order.completed",
        status="failed",
        http_status=500,
        attempt_count=2,
        latency_ms=42,
        created_at=datetime.now(timezone.utc),
        delivered_at=None,
        next_retry_at=None,
        request_id="req_1",
        destination_url="https://agent.example/webhook",
        payload={"id": "evt_agent_retry"},
        request_headers={"X-Test": "1"},
        response_body="failed",
        last_error="HTTP 500",
    )

    assert fake_db.executed_query and "UPDATE agent_webhook_deliveries" in fake_db.executed_query
    assert fake_db.executed_values is not None
    assert "agent_id" not in fake_db.executed_values
    assert "event_id" not in fake_db.executed_values
    assert "event_type" not in fake_db.executed_values
    assert "created_at" not in fake_db.executed_values
    assert fake_db.executed_values["delivery_id"] == "whd_agent_retry"
    assert fake_db.executed_values["attempt_count"] == 2


@pytest.mark.asyncio
async def test_merchant_webhook_retry_update_filters_insert_only_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_webhook_service as module

    fake_db = _ExistingDeliveryDatabase()
    monkeypatch.setattr(module, "database", fake_db)

    await module._persist_delivery_attempt(
        delivery_id="whd_merchant_retry",
        merchant_id="merch_1",
        event_id="evt_merchant_retry",
        event_type="order.completed",
        status="failed",
        http_status=500,
        attempt_count=2,
        latency_ms=42,
        created_at=datetime.now(timezone.utc),
        delivered_at=None,
        next_retry_at=None,
        request_id="req_1",
        destination_url="https://merchant.example/webhook",
        payload={"id": "evt_merchant_retry"},
        request_headers={"X-Test": "1"},
        response_body="failed",
        last_error="HTTP 500",
    )

    assert fake_db.executed_query and "UPDATE merchant_webhook_deliveries" in fake_db.executed_query
    assert fake_db.executed_values is not None
    assert "merchant_id" not in fake_db.executed_values
    assert "event_id" not in fake_db.executed_values
    assert "event_type" not in fake_db.executed_values
    assert "created_at" not in fake_db.executed_values
    assert fake_db.executed_values["delivery_id"] == "whd_merchant_retry"
    assert fake_db.executed_values["attempt_count"] == 2
