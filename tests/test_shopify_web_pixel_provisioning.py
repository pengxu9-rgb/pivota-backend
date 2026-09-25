import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _route_app(current_user):
    from routes.merchant_events import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)

    async def fake_current_user():
        return current_user

    app.dependency_overrides[get_current_user] = fake_current_user
    return app


class _ShopifyStoreDatabase:
    async def fetch_one(self, _query, values):
        return {
            "store_id": values["store_id"],
            "merchant_id": "merchant_1",
            "platform": "shopify",
            "domain": "shop.example.myshopify.com",
            "api_key": "stored-admin-secret",
            "status": "active",
        }


@pytest.mark.asyncio
async def test_ensure_creates_missing_pixel_and_redacts_settings(monkeypatch):
    import services.shopify_web_pixel_provisioning as service

    calls = []

    async def fake_graphql(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"webPixel": None}
        return {
            "webPixelCreate": {
                "webPixel": {
                    "id": "gid://shopify/WebPixel/1",
                    "settings": '{"endpoint":"https://api.example","collectorToken":"secret"}',
                },
                "userErrors": [],
            }
        }

    monkeypatch.setattr(service, "shopify_admin_graphql", fake_graphql)
    result = await service.ensure_shopify_web_pixel(
        shop_domain="shop.example",
        access_token="admin-secret",
        settings={"endpoint": "https://api.example", "collectorToken": "secret"},
    )
    assert result == {
        "status": "created",
        "configured": True,
        "web_pixel_id": "gid://shopify/WebPixel/1",
        "settings_keys": ["collectorToken", "endpoint"],
    }
    assert "admin-secret" not in str(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_ensure_updates_existing_pixel(monkeypatch):
    import services.shopify_web_pixel_provisioning as service

    calls = []

    async def fake_graphql(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {
                "webPixel": {
                    "id": "gid://shopify/WebPixel/1",
                    "settings": {"endpoint": "old"},
                }
            }
        return {
            "webPixelUpdate": {
                "webPixel": {
                    "id": "gid://shopify/WebPixel/1",
                    "settings": {"endpoint": "new", "collectorToken": "secret"},
                },
                "userErrors": [],
            }
        }

    monkeypatch.setattr(service, "shopify_admin_graphql", fake_graphql)
    result = await service.ensure_shopify_web_pixel(
        shop_domain="shop.example",
        access_token="admin-secret",
        settings={"endpoint": "new", "collectorToken": "secret"},
    )
    assert result["status"] == "updated"
    assert calls[1]["variables"]["id"] == "gid://shopify/WebPixel/1"
    assert calls[1]["redact_errors"] is True


@pytest.mark.asyncio
async def test_concurrent_create_taken_requeries_and_updates(monkeypatch):
    import services.shopify_web_pixel_provisioning as service

    responses = [
        {"webPixel": None},
        {
            "webPixelCreate": {
                "webPixel": None,
                "userErrors": [{"code": "TAKEN", "message": "already exists"}],
            }
        },
        {"webPixel": {"id": "gid://shopify/WebPixel/race", "settings": {}}},
        {
            "webPixelUpdate": {
                "webPixel": {
                    "id": "gid://shopify/WebPixel/race",
                    "settings": {"collectorToken": "secret", "endpoint": "https://api"},
                },
                "userErrors": [],
            }
        },
    ]
    calls = []

    async def fake_graphql(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(service, "shopify_admin_graphql", fake_graphql)
    result = await service.ensure_shopify_web_pixel(
        shop_domain="shop.example",
        access_token="admin-secret",
        settings={"collectorToken": "secret", "endpoint": "https://api"},
    )

    assert result["status"] == "updated"
    assert result["web_pixel_id"] == "gid://shopify/WebPixel/race"
    assert calls[-1]["variables"]["id"] == "gid://shopify/WebPixel/race"
    assert responses == []


@pytest.mark.asyncio
async def test_graphql_redacted_errors_never_log_or_raise_echoed_token(
    monkeypatch, caplog
):
    import services.shopify_graphql_client as client

    secret = "collector-token-that-must-not-escape"

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {"x-request-id": "request-1"}

        def json(self):
            return {"errors": [{"message": f"invalid settings {secret}"}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(client.httpx, "AsyncClient", FakeClient)
    with pytest.raises(client.ShopifyGraphQLError) as caught:
        await client.shopify_admin_graphql(
            shop_domain="shop.example",
            access_token="admin-secret",
            query="mutation Pixel($settings: JSON!) { pixel(settings: $settings) }",
            variables={"settings": {"collectorToken": secret}},
            redact_errors=True,
        )

    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert caught.value.errors == [{"message": "redacted"}]


@pytest.mark.asyncio
async def test_graphql_redacted_http_body_never_reaches_logs(monkeypatch, caplog):
    import services.shopify_graphql_client as client

    secret = "collector-token-echoed-in-http-body"

    class FakeResponse:
        status_code = 422
        text = f'invalid settings: {{"collectorToken":"{secret}"}}'
        headers = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(client.httpx, "AsyncClient", FakeClient)
    with pytest.raises(RuntimeError) as caught:
        await client.shopify_admin_graphql(
            shop_domain="shop.example",
            access_token="admin-secret",
            query="mutation Pixel { pixel }",
            variables={"settings": {"collectorToken": secret}},
            redact_errors=True,
        )

    assert str(caught.value) == "Shopify GraphQL HTTP 422"
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_user_error_does_not_echo_sensitive_settings(monkeypatch):
    import services.shopify_web_pixel_provisioning as service

    async def fake_graphql(**kwargs):
        if "query PivotaWebPixel" in kwargs["query"]:
            return {"webPixel": None}
        return {
            "webPixelCreate": {
                "webPixel": None,
                "userErrors": [
                    {
                        "field": ["webPixel", "settings"],
                        "message": "invalid collectorToken secret-value",
                        "code": "INVALID",
                    }
                ],
            }
        }

    monkeypatch.setattr(service, "shopify_admin_graphql", fake_graphql)
    with pytest.raises(service.ShopifyWebPixelProvisioningError) as caught:
        await service.ensure_shopify_web_pixel(
            shop_domain="shop.example",
            access_token="admin-secret",
            settings={"collectorToken": "secret-value", "endpoint": "https://api"},
        )
    assert str(caught.value) == "web_pixel_rejected:INVALID"
    assert "secret-value" not in str(caught.value)


@pytest.mark.asyncio
async def test_status_returns_only_setting_keys(monkeypatch):
    import services.shopify_web_pixel_provisioning as service

    async def fake_graphql(**kwargs):
        return {
            "webPixel": {
                "id": "gid://shopify/WebPixel/2",
                "settings": {"collectorToken": "secret", "endpoint": "https://api"},
            }
        }

    monkeypatch.setattr(service, "shopify_admin_graphql", fake_graphql)
    result = await service.get_shopify_web_pixel_status(
        shop_domain="shop.example", access_token="admin-secret"
    )
    assert result["settings_keys"] == ["collectorToken", "endpoint"]
    assert "secret" not in str(result)


def test_ensure_route_activates_pixel_without_returning_tokens(monkeypatch):
    from routes import merchant_events as route

    async def fake_resolve(**_kwargs):
        return "admin-secret", {"refreshed": False}

    async def fake_ensure(**kwargs):
        assert kwargs["settings"]["collectorToken"] == "collector-secret"
        return {
            "status": "created",
            "configured": True,
            "web_pixel_id": "gid://shopify/WebPixel/1",
            "settings_keys": ["collectorToken", "endpoint"],
        }

    monkeypatch.setattr(route, "database", _ShopifyStoreDatabase())
    monkeypatch.setattr(route, "resolve_shopify_admin_access_token", fake_resolve)
    monkeypatch.setattr(
        route,
        "issue_shopify_pixel_token",
        lambda **_kwargs: {
            "token": "collector-secret",
            "jti": "ct_fake",
            "expires_at": "2026-12-01T00:00:00Z",
            "renewal_due_at": "2026-11-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(route, "ensure_shopify_web_pixel", fake_ensure)

    async def fake_version(_store_id):
        return 1

    async def fake_register(**kwargs):
        return kwargs

    monkeypatch.setattr(route, "current_store_token_version", fake_version)
    monkeypatch.setattr(route, "register_issued_token", fake_register)
    response = TestClient(
        _route_app({"role": "merchant", "merchant_id": "merchant_1"})
    ).post(
        "/merchant-events/v1/shopify-pixel/ensure",
        json={"store_id": "store_1"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "created"
    assert "collector-secret" not in response.text
    assert "admin-secret" not in response.text


def test_status_route_redacts_settings_and_enforces_tenant(monkeypatch):
    from routes import merchant_events as route

    async def fake_resolve(**_kwargs):
        return "admin-secret", {"refreshed": False}

    async def fake_status(**_kwargs):
        return {
            "configured": True,
            "web_pixel_id": "gid://shopify/WebPixel/1",
            "settings_keys": ["collectorToken", "endpoint"],
        }

    monkeypatch.setattr(route, "database", _ShopifyStoreDatabase())
    monkeypatch.setattr(route, "resolve_shopify_admin_access_token", fake_resolve)
    monkeypatch.setattr(route, "get_shopify_web_pixel_status", fake_status)
    allowed = TestClient(
        _route_app({"role": "merchant", "merchant_id": "merchant_1"})
    ).get("/merchant-events/v1/shopify-pixel/store_1/status")
    denied = TestClient(_route_app({"role": "merchant", "merchant_id": "other"})).get(
        "/merchant-events/v1/shopify-pixel/store_1/status"
    )

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "configured"
    assert "secret" not in allowed.text
    assert denied.status_code == 403
