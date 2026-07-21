import pytest
from starlette.requests import Request


from routes import readiness_internal


@pytest.mark.asyncio
async def test_shopify_reconciliation_debug_returns_latest_reconciliation(monkeypatch):
    monkeypatch.setattr(readiness_internal, "_feature_enabled", lambda: True)
    monkeypatch.setenv("UCP_INTERNAL_API_KEY", "internal_test_key")

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_DEBUG_1"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "status": "paid",
            "payment_status": "paid",
            "fulfillment_status": None,
            "shopify_order_id": None,
            "payment_intent_id": "pi_123",
            "total": "9.09",
            "currency": "USD",
            "updated_at": "2026-04-22T04:34:22Z",
        }

    class DummyDatabase:
        async def fetch_all(self, _query, values):
            assert values["order_id"] == "ORD_DEBUG_1"
            return [
                {
                    "id": 1,
                    "event_type": "shopify_sync_retry_requested",
                    "created_at": "2026-04-22T04:34:21Z",
                    "metadata": {"requested_by": "agent_confirm_payment"},
                },
                {
                    "id": 2,
                    "event_type": "shopify_discount_reconciliation",
                    "created_at": "2026-04-22T04:34:22Z",
                    "metadata": {"status": "failed", "mismatches": ["shopify_discount_total"]},
                },
            ]

    monkeypatch.setattr(readiness_internal, "get_order", fake_get_order)
    monkeypatch.setattr(readiness_internal, "database", DummyDatabase())

    request = Request({"type": "http", "path": "/internal/readiness/orders/ORD_DEBUG_1/shopify-reconciliation-debug", "headers": [(b"host", b"testserver")]})
    payload = await readiness_internal.get_order_shopify_reconciliation_debug(
        order_id="ORD_DEBUG_1",
        request=request,
        x_pivota_internal_key="internal_test_key",
    )

    assert payload["order"]["order_id"] == "ORD_DEBUG_1"
    assert payload["latest_reconciliation"]["event_type"] == "shopify_discount_reconciliation"
    assert payload["latest_reconciliation"]["metadata"]["status"] == "failed"
    assert payload["events"][0]["event_type"] == "shopify_sync_retry_requested"
