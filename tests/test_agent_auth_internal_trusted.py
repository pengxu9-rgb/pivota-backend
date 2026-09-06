from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request


def _request(path: str = "/agent/v2/commerce/checkouts") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    return Request(scope)


def test_internal_trusted_api_key_env_names_include_photo_backend_agent_keys() -> None:
    import routes.agent_auth as module

    assert "PIVOTA_BACKEND_AGENT_API_KEY" in module._INTERNAL_TRUSTED_KEY_ENV_NAMES
    assert "PIVOTA_AGENT_API_KEY" in module._INTERNAL_TRUSTED_KEY_ENV_NAMES


@pytest.mark.asyncio
async def test_get_agent_context_accepts_internal_trusted_api_key_without_db_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_auth as module

    trusted_key = "internal-shared-key"
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", (trusted_key,))
    monkeypatch.setattr(module, "get_agent_by_key", lookup)

    request = _request()
    context = await module.get_agent_context(request, api_key=trusted_key, checkout_token=None)

    assert context.agent_name == "Internal Trusted Agent"
    assert context.agent_id.startswith("agent_internal_trusted_")
    assert context.can_access_merchant("merch_any") is True
    assert request.state.agent_id == context.agent_id
    assert request.state.agent_auth_source == "internal_trusted_key"
    assert request.state.agent_internal_trusted_key is True
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_agent_context_accepts_internal_trusted_bearer_without_db_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_auth as module

    trusted_key = "internal-bearer-key"
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", (trusted_key,))
    monkeypatch.setattr(module, "get_agent_by_key", lookup)

    request = _request()
    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=trusted_key)
    context = await module.get_agent_context(request, api_key=None, bearer=bearer, checkout_token=None)

    assert context.agent_name == "Internal Trusted Agent"
    assert context.agent_id.startswith("agent_internal_trusted_")
    assert request.state.agent_auth_source == "internal_trusted_key"
    assert request.state.agent_internal_trusted_key is True
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_agent_context_still_rejects_unknown_agent_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_auth as module

    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", ())
    monkeypatch.setattr(module, "get_agent_by_key", lookup)

    with pytest.raises(HTTPException) as excinfo:
        await module.get_agent_context(_request(), api_key="ak_" + ("a" * 64), checkout_token=None)

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "Invalid API Key"
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_agent_context_retries_transient_api_key_lookup_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_auth as module

    calls = {"lookup": 0}

    async def fake_lookup(api_key: str, metrics_out=None):
        calls["lookup"] += 1
        if calls["lookup"] == 1:
            raise module.AgentAuthLookupTransientError("pool is closing")
        if isinstance(metrics_out, dict):
            metrics_out["auth_lookup_ms"] = 1
            metrics_out["auth_cache_hit"] = False
            metrics_out["auth_source"] = "agent_api_keys_sha256"
        return {
            "agent_id": "agent_retry",
            "agent_name": "Retry Agent",
            "allowed_merchants": None,
            "is_active": True,
            "rate_limit": 100,
            "daily_quota": 1000,
        }

    async def fake_rate_limit(*args, **kwargs):
        return True, 0, 100

    async def fake_daily_quota(*args, **kwargs):
        return True, 0, 1000

    async def fake_update_stats(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "_INTERNAL_TRUSTED_API_KEYS", ())
    monkeypatch.setattr(module, "get_agent_by_key", fake_lookup)
    monkeypatch.setattr(module, "check_rate_limit", fake_rate_limit)
    monkeypatch.setattr(module, "check_daily_quota", fake_daily_quota)
    monkeypatch.setattr(module, "update_agent_stats", fake_update_stats)

    request = _request()
    context = await module.get_agent_context(
        request,
        api_key="ak_" + ("b" * 64),
        checkout_token=None,
    )

    assert context.agent_id == "agent_retry"
    assert request.state.agent_auth_source == "agent_api_keys_sha256"
    assert calls["lookup"] == 2


def test_agent_commerce_checkout_accepts_internal_trusted_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_auth as auth_module
    import routes.agent_commerce as commerce_module

    app = FastAPI()
    app.include_router(commerce_module.router)

    trusted_key = "internal-route-key"
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_module, "_INTERNAL_TRUSTED_API_KEYS", (trusted_key,))
    monkeypatch.setattr(auth_module, "get_agent_by_key", lookup)

    async def fake_readiness(_merchant_id: str):
        return {
            "execute_status": "ready",
            "discover_status": "ready",
            "signals_status": "ready",
            "primary_platform": "shopify",
        }

    async def fake_store(_merchant_id: str):
        return {"platform": "shopify"}

    async def fake_create_order(**kwargs):
        req = kwargs["order_request"]
        assert req.merchant_id == "merch_1"
        assert req.metadata["interaction_id"] == "int_checkout_1"
        return {
            "order_id": "ord_1",
            "merchant_id": "merch_1",
            "status": "pending",
            "payment_status": "awaiting_payment",
            "client_secret": "https://checkout.example.com/ord_1",
        }

    recorded = []

    async def fake_record_event(**kwargs):
        recorded.append(kwargs)
        return {"interaction_id": "int_checkout_1", "event_id": f"evt_{len(recorded)}"}

    monkeypatch.setattr(commerce_module, "upsert_merchant_commerce_readiness_state", fake_readiness)
    monkeypatch.setattr(commerce_module, "get_primary_store", fake_store)
    monkeypatch.setattr(commerce_module, "agent_create_order", fake_create_order)
    monkeypatch.setattr(commerce_module, "record_commerce_event", fake_record_event)

    client = TestClient(app)
    response = client.post(
        "/agent/v2/commerce/checkouts",
        headers={"X-API-Key": trusted_key},
        json={
            "merchant_id": "merch_1",
            "interaction_id": "int_checkout_1",
            "customer_email": "buyer@example.com",
            "shipping_address": {
                "name": "Buyer One",
                "address_line1": "1 Market St",
                "city": "San Francisco",
                "postal_code": "94105",
                "country": "US",
            },
            "items": [
                {
                    "product_id": "prod_1",
                    "variant_id": "var_1",
                    "quantity": 1,
                    "title": "Cleanser",
                    "unit_price": 24.0,
                }
            ],
        },
    )

    assert response.status_code == 200
    # Every agent-commerce ledger write is stamped as the first-party verified
    # issuer: get_agent_context authenticated the agent's own credential.
    assert recorded, "the checkout must write to the ledger"
    assert {call["write_path"] for call in recorded} == {"agent_commerce_api"}
    assert {call["authority"] for call in recorded} == {"pivota"}
    assert {call["agent_identity_confidence"] for call in recorded} == {"verified"}
    assert all(call["metadata"]["agent_identity_confidence"] == "verified" for call in recorded)
    assert all(call["metadata"]["agent_id"] == call["actor_id"] for call in recorded)
    assert response.json()["checkout_id"] == "ord_1"
    assert response.json()["payment_url"] == "https://checkout.example.com/ord_1"
    assert len(recorded) == 2
    assert all(str(event["actor_id"]).startswith("agent_internal_trusted_") for event in recorded)
    lookup.assert_not_awaited()
