import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_api_extensions as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test_connect",
            "email": "merchant@example.com",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_connect_psp_reuses_existing_provider_row(monkeypatch) -> None:
    client, module = _build_client()
    executed = []

    async def fake_get_merchant_id_from_user(current_user):
        assert current_user["role"] == "merchant"
        return "merch_test_connect"

    async def fake_fetch_all(query, values=None):
        assert values == {"merchant_id": "merch_test_connect", "provider": "stripe"}
        return [
            {
                "psp_id": "psp_stripe_existing",
                "status": "active",
                "connected_at": None,
                "provider_config": None,
                "account_id": None,
                "environment": "unknown",
            },
            {
                "psp_id": "psp_stripe_old",
                "status": "active",
                "connected_at": None,
                "provider_config": None,
                "account_id": None,
                "environment": "unknown",
            },
        ]

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    @asynccontextmanager
    async def fake_transaction():
        yield

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "transaction", lambda: fake_transaction())

    response = client.post(
        "/merchant/integrations/psp/connect",
        json={
            "provider": "stripe",
            "api_key": "sk_live_replacement_key",
            "account_id": "acct_live_123",
            "environment": "live",
            "mode": "payment_intent",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["id"] == "psp_stripe_existing"
    assert payload["data"]["reused_existing"] is True
    assert "updated successfully" in payload["message"]

    queries = [item[0] for item in executed]
    assert any("UPDATE merchant_psps SET status = 'inactive'" in query for query in queries)
    assert any("UPDATE merchant_psps SET merchant_id = :merchant_id" in query for query in queries)
    assert not any(query.startswith("INSERT INTO merchant_psps") for query in queries)


def test_connect_psp_forces_stripe_payment_intent_mode(monkeypatch) -> None:
    client, module = _build_client()
    executed = []

    async def fake_get_merchant_id_from_user(current_user):
        return "merch_test_connect"

    async def fake_fetch_all(query, values=None):
        return []

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    @asynccontextmanager
    async def fake_transaction():
        yield

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "transaction", lambda: fake_transaction())

    response = client.post(
        "/merchant/integrations/psp/connect",
        json={
            "provider": "stripe",
            "api_key": "sk_live_replacement_key",
            "environment": "live",
            "mode": "checkout_session",
            "public_key": "pk_live_connect_key",
        },
    )

    assert response.status_code == 200
    assert executed
    insert_values = next(values for query, values in executed if query.startswith("INSERT INTO merchant_psps"))
    assert insert_values["provider_config"] == '{"mode": "payment_intent", "public_key": "pk_live_connect_key"}'


def test_connect_psp_preserves_existing_stripe_webhook_fields(monkeypatch) -> None:
    client, module = _build_client()
    executed = []

    async def fake_get_merchant_id_from_user(current_user):
        return "merch_test_connect"

    async def fake_fetch_all(query, values=None):
        return [
            {
                "psp_id": "psp_stripe_existing",
                "status": "active",
                "connected_at": None,
                "provider_config": {
                    "mode": "payment_intent",
                    "webhook_endpoint_id": "we_existing",
                    "webhook_endpoint_secret": "whsec_existing",
                    "webhook_url": "https://api.pivota.cc/webhooks/stripe/psp_stripe_existing",
                },
                "account_id": None,
                "environment": "live",
            }
        ]

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    @asynccontextmanager
    async def fake_transaction():
        yield

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "transaction", lambda: fake_transaction())

    response = client.post(
        "/merchant/integrations/psp/connect",
        json={
            "provider": "stripe",
            "api_key": "sk_live_replacement_key",
            "environment": "live",
        },
    )

    assert response.status_code == 200
    update_values = next(
        values for query, values in executed if query.startswith("UPDATE merchant_psps SET merchant_id = :merchant_id")
    )
    provider_config = json.loads(update_values["provider_config"])
    assert provider_config["mode"] == "payment_intent"
    assert provider_config["webhook_endpoint_id"] == "we_existing"
    assert provider_config["webhook_endpoint_secret"] == "whsec_existing"


def test_connect_psp_preserves_existing_stripe_public_key(monkeypatch) -> None:
    client, module = _build_client()
    executed = []

    async def fake_get_merchant_id_from_user(current_user):
        return "merch_test_connect"

    async def fake_fetch_all(query, values=None):
        return [
            {
                "psp_id": "psp_stripe_existing",
                "status": "active",
                "connected_at": None,
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_existing",
                },
                "account_id": None,
                "environment": "live",
            }
        ]

    async def fake_execute(query, values=None):
        executed.append((" ".join(query.split()), dict(values or {})))

    @asynccontextmanager
    async def fake_transaction():
        yield

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "transaction", lambda: fake_transaction())

    response = client.post(
        "/merchant/integrations/psp/connect",
        json={
            "provider": "stripe",
            "api_key": "sk_live_replacement_key",
            "environment": "live",
        },
    )

    assert response.status_code == 200
    update_values = next(
        values for query, values in executed if query.startswith("UPDATE merchant_psps SET merchant_id = :merchant_id")
    )
    provider_config = json.loads(update_values["provider_config"])
    assert provider_config["mode"] == "payment_intent"
    assert provider_config["public_key"] == "pk_live_existing"


def test_get_order_detail_uses_order_total_refunded_when_record_lacks_get(monkeypatch) -> None:
    client, module = _build_client()

    class RecordWithoutGet:
        def __init__(self, payload):
            self._payload = dict(payload)

        def __iter__(self):
            return iter(self._payload.items())

        def __getitem__(self, key):
            return self._payload[key]

        def __bool__(self):
            return True

    async def fake_get_merchant_id_from_user(current_user):
        return "merch_test_connect"

    async def fake_fetch_one(query, values=None):
        query_norm = " ".join(query.split())
        if "FROM refund_records" in query_norm:
            return {"total_refunded": 0}
        return RecordWithoutGet(
            {
                "order_id": "ORD_REFUND_TRUTH",
                "merchant_id": "merch_test_connect",
                "store_id": "store_1",
                "psp_id": "psp_stripe_1",
                "total": "10.00",
                "currency": "USD",
                "status": "refunded",
                "payment_status": "refunded",
                "payment_method": "card",
                "customer_name": "Refunded Customer",
                "customer_email": "merchant@example.com",
                "shipping_address": {},
                "items": [],
                "subtotal": "9.00",
                "shipping_fee": "1.00",
                "tax": "0.00",
                "total_refunded": "1.00",
                "shopify_order_id": None,
                "created_at": datetime(2026, 3, 24, 0, 0, 0),
                "updated_at": datetime(2026, 3, 24, 0, 5, 0),
            }
        )

    async def fake_ensure_refund_tables_best_effort():
        return None

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module, "_ensure_refund_tables_best_effort", fake_ensure_refund_tables_best_effort)

    response = client.get("/merchant/orders/ORD_REFUND_TRUTH")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "refunded"
    assert body["data"]["payment_status"] == "refunded"
    assert body["data"]["total_refunded"] == 1.0
