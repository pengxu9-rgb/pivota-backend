from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_client():
    import routes.merchant_dashboard_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {
            "role": "merchant",
            "merchant_id": "merch_test_settings",
            "email": "merchant@example.com",
        }

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


def test_get_preferences_returns_defaults(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_get_preferences(merchant_id: str):
        assert merchant_id == "merch_test_settings"
        return {"merchant_id": merchant_id}

    monkeypatch.setattr(module, "get_merchant_portal_preferences", fake_get_preferences)

    response = client.get("/merchant/settings/preferences")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["email_orders"] is True
    assert body["data"]["email_payments"] is True
    assert body["data"]["email_inventory"] is False
    assert body["data"]["email_weekly"] is False


def test_update_preferences_persists_payload(monkeypatch) -> None:
    client, module = _build_client()

    async def fake_upsert_preferences(merchant_id: str, payload: dict):
        assert merchant_id == "merch_test_settings"
        assert payload["email_orders"] is False
        assert payload["email_payments"] is True
        assert payload["email_inventory"] is True
        assert payload["email_weekly"] is False
        return {
            "merchant_id": merchant_id,
            **payload,
            "updated_at": "2026-03-22T00:00:00Z",
        }

    monkeypatch.setattr(module, "upsert_merchant_portal_preferences", fake_upsert_preferences)

    response = client.put(
        "/merchant/settings/preferences",
        json={
            "email_orders": False,
            "email_payments": True,
            "email_inventory": True,
            "email_weekly": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["email_orders"] is False
    assert body["data"]["email_inventory"] is True
    assert body["data"]["updated_at"] == "2026-03-22T00:00:00Z"
