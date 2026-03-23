from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_dashboard_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test_integrations",
            "email": "merchant@example.com",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_get_api_credentials_returns_real_key(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_fetch_one(query, values=None):
        assert values["merchant_id"] == "merch_test_integrations"
        return {
            "merchant_id": "merch_test_integrations",
            "api_key": "pk_live_realmerchantkey",
            "api_key_hash": "hash",
            "updated_at": None,
            "created_at": None,
        }

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    response = client.get("/merchant/api-credentials")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["issued"] is True
    assert body["data"]["api_key"] == "pk_live_realmerchantkey"
    assert body["data"]["api_key_last4"] == "tkey"
    assert body["data"]["header_name"] == "X-Merchant-API-Key"
    assert body["data"]["sample_endpoint"] == "/payment/execute"


def test_rotate_api_credentials_persists_new_key(monkeypatch) -> None:
    client, module = _build_client()
    executed = {}

    async def fake_fetch_one(query, values=None):
        return {"merchant_id": "merch_test_integrations"}

    async def fake_execute(query, values=None):
        executed.update(values or {})

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    response = client.post("/merchant/api-credentials/rotate")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["api_key"].startswith("pk_live_")
    assert executed["merchant_id"] == "merch_test_integrations"
    assert executed["api_key"] == body["data"]["api_key"]
    assert len(executed["api_key_hash"]) == 64


def test_webhook_routes_use_real_service_contract(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_get_config(merchant_id: str):
        assert merchant_id == "merch_test_integrations"
        return {
            "url": "https://merchant.example/webhooks/pivota",
            "events": ["order.created", "payment.completed"],
            "enabled": True,
            "signing_secret_last4": "abcd",
            "last_test_at": None,
            "last_test_status": "delivered",
            "delivery_summary_24h": {"total": 1, "succeeded": 1, "failed": 0, "retrying": 0},
        }

    async def fake_update(merchant_id: str, *, enabled: bool, destination_url: str, subscribed_events):
        assert merchant_id == "merch_test_integrations"
        assert enabled is True
        assert destination_url == "https://merchant.example/webhooks/pivota"
        assert subscribed_events == ["order.created", "payment.completed"]
        return await fake_get_config(merchant_id)

    async def fake_get_secret(merchant_id: str):
        return {
            "status": "success",
            "signing_secret": "whsec_test_secret",
            "signing_secret_last4": "cret",
        }

    async def fake_rotate(merchant_id: str):
        return {
            "status": "success",
            "new_signing_secret": "whsec_rotated_secret",
            "signing_secret_last4": "cret",
        }

    async def fake_test(merchant_id: str, *, event_type: str, request_id=None):
        assert event_type == "payment.completed"
        return {
            "delivery_id": "mwh_123",
            "event_type": event_type,
            "status": "delivered",
        }

    async def fake_deliveries(merchant_id: str, *, limit: int = 25, status=None):
        assert limit == 20
        return {
            "status": "success",
            "deliveries": [
                {
                    "delivery_id": "mwh_123",
                    "event_type": "payment.completed",
                    "status": "delivered",
                }
            ],
            "summary_24h": {"total": 1, "succeeded": 1, "failed": 0, "retrying": 0},
        }

    monkeypatch.setattr(module, "get_merchant_webhook_config", fake_get_config)
    monkeypatch.setattr(module, "update_merchant_webhook_config", fake_update)
    monkeypatch.setattr(module, "get_merchant_webhook_signing_secret", fake_get_secret)
    monkeypatch.setattr(module, "rotate_merchant_webhook_signing_secret", fake_rotate)
    monkeypatch.setattr(module, "send_merchant_test_webhook", fake_test)
    monkeypatch.setattr(module, "list_merchant_webhook_deliveries", fake_deliveries)

    get_response = client.get("/merchant/webhooks/config")
    put_response = client.put(
        "/merchant/webhooks/config",
        json={
            "url": "https://merchant.example/webhooks/pivota",
            "events": ["order.created", "payment.completed"],
            "enabled": True,
        },
    )
    secret_response = client.get("/merchant/webhooks/secret")
    rotate_response = client.post("/merchant/webhooks/secret/rotate")
    test_response = client.post("/merchant/webhooks/test", json={"event_type": "payment.completed"})
    deliveries_response = client.get("/merchant/webhooks/deliveries?limit=20")

    assert get_response.status_code == 200
    assert get_response.json()["data"]["url"] == "https://merchant.example/webhooks/pivota"
    assert put_response.status_code == 200
    assert put_response.json()["data"]["enabled"] is True
    assert secret_response.status_code == 200
    assert secret_response.json()["signing_secret"] == "whsec_test_secret"
    assert rotate_response.status_code == 200
    assert rotate_response.json()["new_signing_secret"] == "whsec_rotated_secret"
    assert test_response.status_code == 200
    assert test_response.json()["data"]["delivery_id"] == "mwh_123"
    assert deliveries_response.status_code == 200
    assert deliveries_response.json()["data"]["deliveries"][0]["event_type"] == "payment.completed"


def test_get_merchant_psps_returns_environment_and_provider_summary(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_fetch_all(query, values=None):
        query_norm = " ".join(query.split())
        if "FROM merchant_psps" in query_norm:
            return [
                {
                    "psp_id": "psp_adyen_1",
                    "provider": "adyen",
                    "name": "Adyen Account",
                    "account_id": "WoopayECOM",
                    "status": "active",
                    "connected_at": None,
                    "capabilities": "card,bank_transfer",
                    "api_key": "test_adyen_key",
                    "environment": "live",
                    "provider_config": {"merchant_account": "WoopayECOM", "client_key": "pub_123"},
                    "validation_status": "valid",
                    "validation_error": None,
                    "last_validated_at": None,
                }
            ]
        if "FROM orders" in query_norm:
            return []
        raise AssertionError(f"Unexpected query: {query_norm}")

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    response = client.get("/merchant/merch_test_integrations/psps")

    assert response.status_code == 200
    payload = response.json()["data"]["psps"][0]
    assert payload["environment"] == "live"
    assert payload["validation_status"] == "valid"
    assert payload["provider_summary"]["merchant_account"] == "WoopayECOM"
    assert payload["provider_summary"]["client_key_present"] is True
    assert payload["live_charge_ready"] is True
    assert payload["readiness_blockers"] == []


def test_test_psp_connection_persists_environment_and_validation_truth(monkeypatch) -> None:
    client, module = _build_client()
    executed = []
    requested_urls = []

    async def fake_fetch_one(query, values=None):
        assert values["psp_id"] == "psp_checkout_1"
        return {
            "provider": "checkout",
            "api_key": "sk_live_checkout_secret",
            "secret_key": None,
            "account_id": "pc_live_123",
            "merchant_id": "merch_test_integrations",
            "status": "active",
            "environment": "unknown",
            "provider_config": {
                "processing_channel_id": "pc_live_123",
                "public_key": "pk_live_123",
            },
            "validation_status": "unknown",
            "validation_error": None,
        }

    async def fake_execute(query, values=None):
        executed.append({"query": " ".join(query.split()), "values": dict(values or {})})

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, timeout=None):
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)

    response = client.post("/merchant/psp/psp_checkout_1/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["environment"] == "live"
    assert body["data"]["validation_status"] == "valid"
    assert body["data"]["live_charge_ready"] is True
    assert requested_urls == ["https://api.checkout.com/instruments"]
    assert executed
    assert executed[0]["values"]["environment"] == "live"
    assert executed[0]["values"]["validation_status"] == "valid"
