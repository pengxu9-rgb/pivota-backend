from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.apm_config import validate_apm_config
from utils.auth import get_current_merchant


@pytest.fixture
def apm_client(monkeypatch: pytest.MonkeyPatch):
    from routes import merchant_audit_routes as mar

    store: Dict[str, Dict[str, Any]] = {}
    configured_at = datetime(2026, 5, 12, 19, 0, tzinfo=timezone.utc)

    async def _fake_upsert_apm_config(
        *,
        merchant_id: str,
        enabled: bool,
        cadence_days: int,
        scope: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized_scope = validate_apm_config(
            cadence_days=cadence_days,
            scope=scope,
        )
        row = {
            "merchant_id": merchant_id,
            "enabled": enabled,
            "cadence_days": cadence_days,
            "scope": normalized_scope,
            "apm_configured_at": configured_at,
            "apm_last_run_at": None,
        }
        store[merchant_id] = row
        return row

    async def _fake_get_apm_config(merchant_id: str) -> Optional[Dict[str, Any]]:
        return store.get(merchant_id)

    async def _override_merchant() -> str:
        return "merch_apm"

    monkeypatch.setattr(mar, "upsert_apm_config", _fake_upsert_apm_config)
    monkeypatch.setattr(mar, "get_apm_config", _fake_get_apm_config)

    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_merchant] = _override_merchant
    return TestClient(app), store


def _valid_scope() -> Dict[str, Any]:
    return {
        "scan_modes": [
            "open_product_visibility_test",
            "merchant_store_attribution_test",
            "category_visibility_test",
        ],
        "providers": ["gemini"],
        "max_products_per_audit": 5,
    }


def _detail_messages(response) -> str:
    return " ".join(str(item.get("msg", "")) for item in response.json()["detail"])


def test_configure_apm_happy_path_persists_expected_values(apm_client):
    client, store = apm_client

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={
            "enabled": True,
            "cadence_days": 14,
            "scope": _valid_scope(),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merchant_id"] == "merch_apm"
    assert body["enabled"] is True
    assert body["cadence_days"] == 14
    assert body["scope"] == _valid_scope()
    assert body["apm_configured_at"].startswith("2026-05-12T19:00:00")
    assert store["merch_apm"]["scope"] == _valid_scope()


def test_configure_apm_rejects_invalid_cadence_days(apm_client):
    client, _ = apm_client

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={"enabled": True, "cadence_days": 99, "scope": _valid_scope()},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "cadence_days"]
    assert "7, 14, or 30" in _detail_messages(response)


def test_configure_apm_rejects_perplexity_provider(apm_client):
    client, _ = apm_client
    scope = _valid_scope()
    scope["providers"] = ["perplexity"]

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={"enabled": True, "cadence_days": 7, "scope": scope},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "scope", "providers"]
    assert "perplexity" in _detail_messages(response)


def test_configure_apm_rejects_zero_max_products(apm_client):
    client, _ = apm_client
    scope = _valid_scope()
    scope["max_products_per_audit"] = 0

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={"enabled": True, "cadence_days": 7, "scope": scope},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "scope", "max_products_per_audit"]
    assert "between 1 and 10" in _detail_messages(response)


def test_configure_apm_enabled_false_still_persists(apm_client):
    client, store = apm_client

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={"enabled": False, "cadence_days": 30, "scope": _valid_scope()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert store["merch_apm"]["enabled"] is False


def test_get_apm_config_returns_404_when_never_configured(apm_client):
    client, _ = apm_client

    response = client.get("/api/merchant-center/audit/apm-config")

    assert response.status_code == 404
    assert "APM config" in response.json()["detail"]


def test_get_apm_config_returns_persisted_shape(apm_client):
    client, _ = apm_client
    post = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={"enabled": True, "cadence_days": 7, "scope": _valid_scope()},
    )
    assert post.status_code == 200

    response = client.get("/api/merchant-center/audit/apm-config")

    assert response.status_code == 200, response.text
    assert response.json() == post.json()


def test_configure_apm_defaults_provider_to_gemini_only(apm_client):
    client, _ = apm_client

    response = client.post(
        "/api/merchant-center/audit/configure-apm",
        json={
            "enabled": True,
            "cadence_days": 7,
            "scope": {"max_products_per_audit": 5},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["scope"]["providers"] == ["gemini"]
