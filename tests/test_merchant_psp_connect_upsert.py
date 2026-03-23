from contextlib import asynccontextmanager

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
            },
            {
                "psp_id": "psp_stripe_old",
                "status": "active",
                "connected_at": None,
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
        },
    )

    assert response.status_code == 200
    assert executed
    insert_values = next(values for query, values in executed if query.startswith("INSERT INTO merchant_psps"))
    assert insert_values["provider_config"] == '{"mode": "payment_intent"}'
