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


# ---------------------------------------------------------------------------------------------
# checkout_token introspection. The gateway sends X-Checkout-Token here because it does not hold
# CHECKOUT_TOKEN_SECRET; before this existed it accepted ANY non-empty token as authentication.
# Tokens are minted by the real producer (mint_checkout_token), never hand-built, so a change to
# the mint shape breaks these tests instead of passing them.
# ---------------------------------------------------------------------------------------------

import time as _time

from routes import agent_checkout_intents as _intents

_CHECKOUT_SECRET = "introspect-test-checkout-secret"


def _introspect_token(client, token, **extra):
    return client.post(
        "/agent/internal/auth/introspect",
        headers={"X-Internal-Key": "internal_test_key"},
        json={"checkout_token": token, **extra},
    )


def _checkout_env(monkeypatch, *, agent=None):
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal_test_key")
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", _CHECKOUT_SECRET)
    monkeypatch.delenv("AGENT_CHECKOUT_TOKEN_SECRET", raising=False)
    looked_up = []

    async def _fake_get_agent(agent_id):
        looked_up.append(agent_id)
        if agent is None:
            return None
        return {"agent_id": agent_id, **agent}

    monkeypatch.setattr(introspect_module, "get_agent", _fake_get_agent)
    return looked_up


def _mint(**overrides):
    payload = {
        "agent_id": "agent_ct_1",
        "merchant_ids": ["merch_a"],
        "scopes": ["checkout"],
        "intent_id": "ci_test",
        **overrides,
    }
    return _intents.mint_checkout_token(payload, ttl_seconds=600)


def test_introspect_checkout_token_accepts_a_minted_token(monkeypatch):
    looked_up = _checkout_env(monkeypatch, agent={"is_active": True})
    token = _mint()

    body = _introspect_token(_build_client(), token).json()

    assert body["valid"] is True
    assert body["agent_id"] == "agent_ct_1"
    assert body["is_active"] is True
    assert body["auth_source"] == "checkout_token"
    assert body["scopes"] == ["checkout"]
    assert body["merchant_ids"] == ["merch_a"]
    assert body["expires_at"] > int(_time.time())
    assert looked_up == ["agent_ct_1"]


def test_introspect_checkout_token_refuses_an_arbitrary_string(monkeypatch):
    looked_up = _checkout_env(monkeypatch, agent={"is_active": True})

    for bogus in ("x", "bogus-token", "v1.abc.def", "abc.def", "v1.a.b.c"):
        response = _introspect_token(_build_client(), bogus)
        assert response.status_code == 200, bogus
        assert response.json()["valid"] is False, bogus
        assert response.json()["auth_source"] == "checkout_token_invalid", bogus
    assert looked_up == []


def test_introspect_checkout_token_refuses_a_token_signed_with_another_secret(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", "some-other-secret")
    forged = _mint()
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", _CHECKOUT_SECRET)

    body = _introspect_token(_build_client(), forged).json()

    assert body["valid"] is False
    assert body["auth_source"] == "checkout_token_invalid"


def test_introspect_checkout_token_refuses_a_tampered_payload(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})
    version, _payload_b64, sig = _mint().split(".")
    other_payload_b64 = _mint(agent_id="agent_someone_else").split(".")[1]

    body = _introspect_token(_build_client(), f"{version}.{other_payload_b64}.{sig}").json()

    assert body["valid"] is False


def test_introspect_checkout_token_refuses_an_expired_token(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})
    real_time = _time.time
    monkeypatch.setattr(_intents.time, "time", lambda: real_time() - 3600)
    token = _intents.mint_checkout_token({"agent_id": "agent_ct_1", "scopes": ["checkout"]}, ttl_seconds=60)
    monkeypatch.setattr(_intents.time, "time", real_time)

    body = _introspect_token(_build_client(), token).json()

    assert body["valid"] is False
    assert body["auth_source"] == "checkout_token_invalid"


def test_introspect_checkout_token_refuses_an_unknown_agent(monkeypatch):
    _checkout_env(monkeypatch, agent=None)

    body = _introspect_token(_build_client(), _mint()).json()

    assert body["valid"] is False
    assert body["auth_source"] == "checkout_token_agent_not_found"


def test_introspect_checkout_token_reports_a_deactivated_agent(monkeypatch):
    _checkout_env(monkeypatch, agent={"status": "inactive"})

    body = _introspect_token(_build_client(), _mint()).json()

    assert body["valid"] is True
    assert body["is_active"] is False


def test_introspect_checkout_token_is_a_500_without_the_secret(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})
    token = _mint()
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET", raising=False)

    response = _introspect_token(_build_client(), token)

    assert response.status_code == 500


def test_introspect_refuses_both_credentials_at_once(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})

    body = _introspect_token(_build_client(), _mint(), api_key="ak_live_" + "e" * 64).json()

    assert body["valid"] is False
    assert body["auth_source"] == "ambiguous"


def test_introspect_checkout_token_still_needs_the_internal_key(monkeypatch):
    _checkout_env(monkeypatch, agent={"is_active": True})

    response = _build_client().post(
        "/agent/internal/auth/introspect", json={"checkout_token": _mint()}
    )

    assert response.status_code == 403


def test_introspect_checkout_token_refuses_a_signed_token_without_an_agent(monkeypatch):
    looked_up = _checkout_env(monkeypatch, agent={"is_active": True})
    token = _intents.mint_checkout_token({"scopes": ["checkout"]}, ttl_seconds=600)

    body = _introspect_token(_build_client(), token).json()

    assert body["valid"] is False
    assert looked_up == []
