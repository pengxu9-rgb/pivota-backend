import pytest


@pytest.mark.asyncio
async def test_check_duplicate_event_missing_table_is_non_fatal(monkeypatch):
    from services import webhook_service as ws

    async def noop_ensure():
        return None

    class DummyDB:
        async def fetch_one(self, query, values):
            raise Exception('relation "webhook_events" does not exist')

    monkeypatch.setattr(ws.WebhookService, "ensure_webhook_events_table", staticmethod(noop_ensure))
    monkeypatch.setattr(ws, "database", DummyDB())

    is_dup, existing = await ws.WebhookService.check_duplicate_event("evt_123")
    assert is_dup is False
    assert existing is None


@pytest.mark.asyncio
async def test_record_webhook_event_unique_violation_returns_existing_id(monkeypatch):
    from services import webhook_service as ws

    async def noop_ensure():
        return None

    async def noop_increment(_event_id: str):
        return None

    class DummyDB:
        async def execute(self, query, values=None):
            raise Exception("duplicate key value violates unique constraint")

        async def fetch_val(self, query, values=None):
            return 123

    monkeypatch.setattr(ws.WebhookService, "ensure_webhook_events_table", staticmethod(noop_ensure))
    monkeypatch.setattr(ws.WebhookService, "increment_retry_count", staticmethod(noop_increment))
    monkeypatch.setattr(ws, "database", DummyDB())

    record_id = await ws.WebhookService.record_webhook_event(
        event_id="evt_123",
        event_type="payment_captured",
        psp_type="checkout",
        order_id="ORD_1",
        payload={"id": "evt_123"},
        headers={"h": "v"},
        status="pending",
    )
    assert record_id == 123


@pytest.mark.asyncio
async def test_check_duplicate_event_failed_status_is_reprocessable(monkeypatch):
    from services import webhook_service as ws

    async def noop_ensure():
        return None

    class DummyDB:
        async def fetch_one(self, query, values):
            return {
                "id": 77,
                "event_id": values["event_id"],
                "order_id": "ORD_RETRY",
                "status": "failed",
                "processed_at": None,
                "error_message": "transient upstream timeout",
            }

    monkeypatch.setattr(ws.WebhookService, "ensure_webhook_events_table", staticmethod(noop_ensure))
    monkeypatch.setattr(ws, "database", DummyDB())

    is_dup, existing = await ws.WebhookService.check_duplicate_event("evt_retryable")
    assert is_dup is False
    assert existing is not None
    assert existing["status"] == "failed"
    assert existing["order_id"] == "ORD_RETRY"
