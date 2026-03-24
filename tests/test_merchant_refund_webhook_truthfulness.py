from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_api_extensions as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test_refunds",
            "email": "merchant@example.com",
            "user_id": "merchant_user_1",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_merchant_refund_order_emits_refund_processed_webhook(monkeypatch) -> None:
    client, module = _build_client()
    merchant_webhook_calls = []
    order_events = []

    async def fake_get_merchant_id_from_user(current_user):
        return "merch_test_refunds"

    async def fake_fetch_one(query, values=None):
        normalized = " ".join(query.split())
        if "SELECT 1 FROM orders" in normalized:
            return {"?column?": 1}
        if "SELECT shopify_order_id FROM orders" in normalized:
            return {"shopify_order_id": None}
        raise AssertionError(f"Unexpected query: {normalized}")

    async def fake_create_refund(**kwargs):
        assert kwargs["order_id"] == "ORD_REFUND_1"
        assert kwargs["amount"] == 1.0
        return {
            "status": "success",
            "refund_id": "REF_TEST_1",
            "psp_refund_id": "re_test_1",
        }

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)

    async def fake_get_order(order_id: str):
        assert order_id == "ORD_REFUND_1"
        return {
            "order_id": order_id,
            "merchant_id": "merch_test_refunds",
            "currency": "USD",
            "payment_status": "refunded",
            "status": "refunded",
        }

    async def fake_emit_merchant_webhook_event(
        merchant_id: str,
        *,
        event_type: str,
        payload,
        request_id=None,
        force_delivery: bool = False,
    ):
        merchant_webhook_calls.append(
            {
                "merchant_id": merchant_id,
                "event_type": event_type,
                "payload": dict(payload),
                "request_id": request_id,
                "force_delivery": force_delivery,
            }
        )
        return {"status": "delivered"}

    async def fake_ensure_refund_tables_best_effort():
        return None

    monkeypatch.setattr(module, "is_feature_enabled", lambda flag: True)
    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module, "_ensure_refund_tables_best_effort", fake_ensure_refund_tables_best_effort)
    monkeypatch.setattr(module.refund_service, "create_refund", fake_create_refund)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event)

    response = client.post(
        "/merchant/orders/ORD_REFUND_1/refund",
        json={"amount": 1.0, "reason": "ops_canary", "source": "pivota_merchant"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert order_events[0]["event_type"] == "merchant_refund"
    assert merchant_webhook_calls == [
        {
            "merchant_id": "merch_test_refunds",
            "event_type": "refund.processed",
            "payload": {
                "order_id": "ORD_REFUND_1",
                "merchant_id": "merch_test_refunds",
                "refund_id": "REF_TEST_1",
                "amount": 1.0,
                "currency": "USD",
                "is_partial": False,
                "status": "refunded",
            },
            "request_id": None,
            "force_delivery": False,
        }
    ]
