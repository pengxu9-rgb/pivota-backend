import pytest


@pytest.mark.asyncio
async def test_update_order_status_treats_verified_write_as_success(monkeypatch):
    from db import orders as orders_module

    class DummyDB:
        def __init__(self):
            self.fetch_calls = 0

        async def fetch_one(self, *_args, **_kwargs):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return {
                    "merchant_id": "merch_1",
                    "status": "pending",
                    "fulfillment_status": None,
                    "payment_status": "awaiting_payment",
                    "metadata": {"existing": True},
                }
            return {
                "merchant_id": "merch_1",
                "status": "cancelled",
                "fulfillment_status": None,
                "payment_status": "cancelled",
                "shopify_order_id": None,
            }

        async def execute(self, *_args, **_kwargs):
            return 0

    monkeypatch.setattr(orders_module, "database", DummyDB())

    ok = await orders_module.update_order_status(
        "ORD_ROWCOUNT",
        "cancelled",
        payment_status="cancelled",
        metadata={"cleanup_reason": "ops_canary_cleanup"},
    )

    assert ok is True


@pytest.mark.asyncio
async def test_update_order_status_still_fails_when_read_after_write_does_not_match(monkeypatch):
    from db import orders as orders_module

    class DummyDB:
        def __init__(self):
            self.fetch_calls = 0

        async def fetch_one(self, *_args, **_kwargs):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return {
                    "merchant_id": "merch_1",
                    "status": "pending",
                    "fulfillment_status": None,
                    "payment_status": "awaiting_payment",
                    "metadata": {},
                }
            return {
                "merchant_id": "merch_1",
                "status": "pending",
                "fulfillment_status": None,
                "payment_status": "awaiting_payment",
                "shopify_order_id": None,
            }

        async def execute(self, *_args, **_kwargs):
            return 0

    monkeypatch.setattr(orders_module, "database", DummyDB())

    ok = await orders_module.update_order_status(
        "ORD_ROWCOUNT",
        "cancelled",
        payment_status="cancelled",
    )

    assert ok is False


@pytest.mark.asyncio
async def test_admin_cancel_unpaid_order_marks_payment_cancelled(monkeypatch):
    from routes import order_routes

    captured = {}

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_CANCEL"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "awaiting_payment",
            "metadata": {"canary": True},
        }

    async def fake_update_order_status(order_id: str, status: str, **kwargs):
        captured["order_id"] = order_id
        captured["status"] = status
        captured["kwargs"] = kwargs
        return True

    async def fake_log_order_event(**_kwargs):
        return None

    monkeypatch.setattr(order_routes, "get_order", fake_get_order)
    monkeypatch.setattr(order_routes, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(order_routes, "log_order_event", fake_log_order_event)

    result = await order_routes.cancel_order(
        "ORD_CANCEL",
        reason="cleanup",
        current_user={"role": "admin"},
    )

    assert result["status"] == "success"
    assert captured["status"] == "cancelled"
    assert captured["kwargs"]["payment_status"] == "cancelled"
    assert captured["kwargs"]["metadata"]["cancellation_reason"] == "cleanup"
