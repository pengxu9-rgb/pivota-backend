import json

import pytest
from fastapi import HTTPException


def _request(**overrides):
    from routes.merchant_store_connections import ConnectCustomStoreRequest

    values = {
        "merchant_id": "merchant-1",
        "store_url": "https://shop.example.com",
        "store_name": "Headless Shop",
        "allowed_origins": ["https://checkout.example.com"],
        "collector_token_ttl_days": 30,
    }
    values.update(overrides)
    return ConnectCustomStoreRequest(**values)


@pytest.mark.asyncio
async def test_custom_store_connect_creates_store_and_returns_consent_pending_install(monkeypatch):
    from routes import merchant_store_connections as route

    writes = []

    class FakeDB:
        async def fetch_one(self, query, values):
            return None

        async def execute(self, query, values):
            writes.append((query, dict(values)))

    def issue(**kwargs):
        assert kwargs["platform"] == "custom"
        assert kwargs["allowed_origins"] == [
            "https://shop.example.com",
            "https://checkout.example.com",
        ]
        return {
            "token": "public-collector-token",
            "expires_at": "2026-09-30T00:00:00+00:00",
            "allowed_origins": kwargs["allowed_origins"],
        }

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "issue_web_collector_token", issue)
    monkeypatch.setattr(route, "resolve_public_api_base_url", lambda: "https://api.example.com")

    result = await route.merchant_connect_custom_store(
        request=_request(),
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert result["status"] == "success"
    assert result["platform"] == "custom"
    assert result["store_id"].startswith("store_custom_")
    assert result["collector_token"] == "public-collector-token"
    assert result["reused_existing"] is False
    assert 'data-pivota-consent="pending"' in result["install_snippet"]
    insert = next(values for query, values in writes if "INSERT INTO merchant_stores" in query)
    assert insert["domain"] == "https://shop.example.com"
    assert json.loads(insert["api_key"]) == {
        "collector_only": True,
        "credential_version": 1,
    }


@pytest.mark.asyncio
async def test_custom_store_connect_reuses_existing_scope_and_reissues_for_persisted_id(monkeypatch):
    from routes import merchant_store_connections as route

    issued_store_ids = []
    writes = []

    class FakeDB:
        async def fetch_one(self, query, values):
            return {"store_id": "legacy-custom-store"}

        async def execute(self, query, values):
            writes.append((query, dict(values)))

    def issue(**kwargs):
        issued_store_ids.append(kwargs["store_id"])
        return {
            "token": f'token-for-{kwargs["store_id"]}',
            "expires_at": "2026-09-30T00:00:00+00:00",
            "allowed_origins": kwargs["allowed_origins"],
        }

    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(route, "issue_web_collector_token", issue)
    monkeypatch.setattr(route, "resolve_public_api_base_url", lambda: "https://api.example.com")

    result = await route.merchant_connect_custom_store(
        request=_request(allowed_origins=[]),
        current_user={"role": "admin"},
    )

    assert result["store_id"] == "legacy-custom-store"
    assert result["collector_token"] == "token-for-legacy-custom-store"
    assert result["reused_existing"] is True
    assert len(issued_store_ids) == 2
    assert any("UPDATE merchant_stores" in query for query, _values in writes)
    assert not any("INSERT INTO merchant_stores" in query for query, _values in writes)


@pytest.mark.asyncio
async def test_custom_store_connect_enforces_tenant_and_https_origin():
    from routes import merchant_store_connections as route

    with pytest.raises(HTTPException) as tenant_error:
        await route.merchant_connect_custom_store(
            request=_request(),
            current_user={"role": "merchant", "merchant_id": "merchant-other"},
        )
    assert tenant_error.value.status_code == 403

    for invalid_url in (
        "http://shop.example.com",
        "https://user:password@shop.example.com",
        "https://shop.example.com/catalog",
    ):
        with pytest.raises(HTTPException) as origin_error:
            await route.merchant_connect_custom_store(
                request=_request(store_url=invalid_url),
                current_user={"role": "merchant", "merchant_id": "merchant-1"},
            )
        assert origin_error.value.status_code == 422


@pytest.mark.asyncio
async def test_custom_store_connect_fails_before_write_when_collector_signing_unavailable(monkeypatch):
    from routes import merchant_store_connections as route
    from services.merchant_web_collector_service import WebCollectorError

    class NoWriteDB:
        async def fetch_one(self, *args, **kwargs):
            raise AssertionError("store lookup must not happen before token provisioning")

        async def execute(self, *args, **kwargs):
            raise AssertionError("store write must not happen")

    def unavailable(**kwargs):
        raise WebCollectorError(503, "Web collector token signing is not configured")

    monkeypatch.setattr(route, "database", NoWriteDB())
    monkeypatch.setattr(route, "issue_web_collector_token", unavailable)

    with pytest.raises(HTTPException) as error:
        await route.merchant_connect_custom_store(
            request=_request(),
            current_user={"role": "merchant", "merchant_id": "merchant-1"},
        )
    assert error.value.status_code == 503
