"""Guardrails on the two uncontrolled public surfaces (2026-08-08 audit):
POST /agent/account/register (mints ak_live_* keys with no gate/throttle) and
POST /agent/shop/v1/invoke (unmetered for credential-less callers — the
RateLimitMiddleware only counts requests that carry an API key)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── registration ──────────────────────────────────────────────────────────────

def _account_client(monkeypatch):
    import routes.agent_account as module

    monkeypatch.setattr(module, "_REGISTRATION_IP_LIMIT_STORE", {})
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), module


def _register_body():
    return {
        "email": "new-agent@example.com",
        "password": "longenough1",
        "agent_name": "Test Agent",
    }


def test_registration_kill_switch_answers_403(monkeypatch):
    client, _ = _account_client(monkeypatch)
    monkeypatch.setenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED", "false")

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 403


def test_registration_per_ip_throttle_answers_429(monkeypatch):
    client, module = _account_client(monkeypatch)
    monkeypatch.delenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_REGISTRATION_PER_IP_HOURLY", "1")

    async def fake_fetch_one(query, values=None):
        # First registration short-circuits on "already registered" so the
        # test never needs a real database past the limiter.
        return {"id": 1}

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    first = client.post("/agent/account/register", json=_register_body())
    second = client.post("/agent/account/register", json=_register_body())

    assert first.status_code == 400  # already-registered stub
    assert second.status_code == 429
    assert second.headers.get("retry-after") == "3600"


def test_registration_default_preserves_current_behavior(monkeypatch):
    # Flag unset ⇒ enabled; limiter default (5/h) admits a first attempt.
    client, module = _account_client(monkeypatch)
    monkeypatch.delenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_REGISTRATION_PER_IP_HOURLY", raising=False)

    async def fake_fetch_one(query, values=None):
        return {"id": 1}

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    resp = client.post("/agent/account/register", json=_register_body())
    assert resp.status_code == 400  # reached the handler body, not a guard


# ── anonymous invoke throttle ────────────────────────────────────────────────

def _gateway_module(monkeypatch):
    import routes.agent_shop_gateway as module

    monkeypatch.setattr(module, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    return module


def test_invoke_anon_limiter_counts_per_ip_window(monkeypatch):
    module = _gateway_module(monkeypatch)
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "2")

    assert module._check_invoke_anon_rate_limit("1.2.3.4") is True
    assert module._check_invoke_anon_rate_limit("1.2.3.4") is True
    assert module._check_invoke_anon_rate_limit("1.2.3.4") is False
    # A different IP has its own budget.
    assert module._check_invoke_anon_rate_limit("5.6.7.8") is True


def test_invoke_anon_limiter_zero_disables(monkeypatch):
    module = _gateway_module(monkeypatch)
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    for _ in range(10):
        assert module._check_invoke_anon_rate_limit("1.2.3.4") is True


def test_credential_detection_matches_the_three_header_forms(monkeypatch):
    module = _gateway_module(monkeypatch)

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers

    assert module._request_carries_credential(FakeRequest({"x-api-key": "ak_live_x"})) is True
    assert module._request_carries_credential(FakeRequest({"authorization": "Bearer t"})) is True
    assert module._request_carries_credential(FakeRequest({"x-checkout-token": "ct"})) is True
    assert module._request_carries_credential(FakeRequest({})) is False


def test_invoke_route_429s_anonymous_over_limit_but_not_keyed(monkeypatch):
    module = _gateway_module(monkeypatch)
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "1")

    app = FastAPI()
    app.include_router(module.router)  # router already carries /agent/shop/v1
    client = TestClient(app)
    body = {"operation": "definitely_not_an_operation", "payload": {}}

    first = client.post("/agent/shop/v1/invoke", json=body)
    assert first.status_code != 429  # unknown op, but admitted past the limiter

    second = client.post("/agent/shop/v1/invoke", json=body)
    assert second.status_code == 429
    assert second.headers.get("retry-after") == "60"

    # A keyed caller is untouched by the anonymous limiter.
    keyed = client.post("/agent/shop/v1/invoke", json=body, headers={"X-API-Key": "ak_live_k"})
    assert keyed.status_code != 429
