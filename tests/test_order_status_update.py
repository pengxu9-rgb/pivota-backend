import os

import pytest


os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


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
