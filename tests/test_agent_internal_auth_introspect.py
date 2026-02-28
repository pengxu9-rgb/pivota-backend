from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import agent_internal_auth as introspect_module


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(introspect_module.router)
    return TestClient(app)


def test_introspect_rejects_missing_internal_key(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")
    client = _build_client()

    response = client.post("/agent/internal/auth/introspect", json={"api_key": "ak_live_" + "a" * 64})

    assert response.status_code == 403
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "FORBIDDEN"


def test_introspect_returns_invalid_for_bad_key_format(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")
    client = _build_client()

    response = client.post(
        "/agent/internal/auth/introspect",
        headers={"X-Internal-Key": "internal_test_key"},
        json={"api_key": "not_a_valid_key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["auth_source"] == "format_invalid"


def test_introspect_returns_active_agent(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")

    async def _fake_get_agent_by_key(_api_key, metrics_out=None):
        if isinstance(metrics_out, dict):
            metrics_out["auth_source"] = "api_keys"
        return {"agent_id": "agent_123", "is_active": True}

    monkeypatch.setattr(introspect_module, "get_agent_by_key", _fake_get_agent_by_key)
    client = _build_client()

    response = client.post(
        "/agent/internal/auth/introspect",
        headers={"X-Internal-Key": "internal_test_key"},
        json={"api_key": "ak_live_" + "b" * 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["agent_id"] == "agent_123"
    assert body["is_active"] is True
    assert body["auth_source"] == "api_keys"


def test_introspect_returns_inactive_when_agent_status_inactive(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")

    async def _fake_get_agent_by_key(_api_key, metrics_out=None):
        if isinstance(metrics_out, dict):
            metrics_out["auth_source"] = "legacy_auto"
        return {"agent_id": "agent_456", "status": "inactive"}

    monkeypatch.setattr(introspect_module, "get_agent_by_key", _fake_get_agent_by_key)
    client = _build_client()

    response = client.post(
        "/agent/internal/auth/introspect",
        headers={"X-Internal-Key": "internal_test_key"},
        json={"api_key": "ak_live_" + "c" * 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["agent_id"] == "agent_456"
    assert body["is_active"] is False
    assert body["auth_source"] == "legacy_auto"


def test_introspect_returns_503_on_transient_auth_lookup_error(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")

    async def _fake_get_agent_by_key(_api_key, metrics_out=None):
        raise introspect_module.AgentAuthLookupTransientError("pool is closing")

    monkeypatch.setattr(introspect_module, "get_agent_by_key", _fake_get_agent_by_key)
    client = _build_client()

    response = client.post(
        "/agent/internal/auth/introspect",
        headers={"X-Internal-Key": "internal_test_key"},
        json={"api_key": "ak_live_" + "d" * 64},
    )

    assert response.status_code == 503
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "TEMPORARY_UNAVAILABLE"
