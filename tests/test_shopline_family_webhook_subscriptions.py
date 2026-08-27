import json

import pytest
from fastapi import HTTPException


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, list_payload, create_status=200):
        self.list_payload = list_payload
        self.create_status = create_status
        self.gets = []
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if isinstance(self.list_payload, list):
            payload = self.list_payload[min(len(self.gets) - 1, len(self.list_payload) - 1)]
        else:
            payload = self.list_payload
        return FakeResponse(200, payload)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.create_status, {"webhook": {"id": "new"}})


@pytest.mark.asyncio
async def test_shopline_subscription_installer_creates_only_missing_topics(monkeypatch):
    from services import shopline_family_webhook_subscriptions as service

    callback = "https://api.example/webhooks/shopline/store-1"
    fake = FakeClient(
        {
            "webhooks": [
                {"id": 1, "topic": "orders/create", "address": callback},
                {"id": 2, "topic": "products/create", "address": callback},
            ]
        }
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)
    result = await service.ensure_shopline_subscriptions(
        handle="demo",
        access_token="token",
        api_version="v20260601",
        callback_url=callback,
        topics=["orders/create", "orders/paid"],
    )

    assert result["existing_topics"] == ["orders/create"]
    assert result["created_topics"] == ["orders/paid"]
    assert fake.gets[0][0].endswith("/admin/openapi/v20260601/webhooks.json")
    assert fake.posts[0][1]["json"] == {
        "webhook": {
            "api_version": "v20260601",
            "topic": "orders/paid",
            "address": callback,
        }
    }
    assert fake.posts[0][1]["headers"]["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_shoplazza_subscription_installer_understands_data_wrapper(monkeypatch):
    from services import shopline_family_webhook_subscriptions as service

    callback = "https://api.example/webhooks/shoplazza/store-2"
    fake = FakeClient(
        {
            "data": {
                "webhooks": [
                    {"id": "1", "topic": "orders/paid", "address": f"{callback}/"}
                ]
            }
        }
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)
    result = await service.ensure_shoplazza_subscriptions(
        store_url="https://demo.myshoplaza.com",
        access_token="token",
        api_version="2026-01",
        callback_url=callback,
        topics=["orders/paid", "orders/refunded"],
    )

    assert result["existing_topics"] == ["orders/paid"]
    assert result["created_topics"] == ["orders/refunded"]
    assert fake.gets[0][1]["params"] == {"page_size": 250}
    assert fake.posts[0][1]["json"] == {
        "webhook": {"topic": "orders/refunded", "address": callback}
    }
    assert fake.posts[0][1]["headers"]["Access-Token"] == "token"


@pytest.mark.asyncio
async def test_shoplazza_subscription_installer_follows_cursor_pages(monkeypatch):
    from services import shopline_family_webhook_subscriptions as service

    callback = "https://api.example/webhooks/shoplazza/store-2"
    fake = FakeClient(
        [
            {"data": {"webhooks": [], "has_more": True, "cursor": "next-page"}},
            {
                "data": {
                    "webhooks": [
                        {"id": "2", "topic": "orders/refunded", "address": callback}
                    ],
                    "has_more": False,
                }
            },
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)
    result = await service.ensure_shoplazza_subscriptions(
        store_url="https://demo.myshoplaza.com",
        access_token="token",
        api_version="2026-01",
        callback_url=callback,
        topics=["orders/refunded"],
    )
    assert result["existing_topics"] == ["orders/refunded"]
    assert not fake.posts
    assert fake.gets[1][1]["params"]["cursor"] == "next-page"


@pytest.mark.asyncio
async def test_subscription_installer_reports_topic_and_status_without_body(monkeypatch):
    from services import shopline_family_webhook_subscriptions as service

    fake = FakeClient({"webhooks": []}, create_status=403)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)
    with pytest.raises(service.WebhookSubscriptionError) as caught:
        await service.ensure_shopline_subscriptions(
            handle="demo",
            access_token="do-not-leak",
            api_version="v20260601",
            callback_url="https://api.example/webhooks/shopline/store-1",
            topics=["orders/paid"],
        )
    assert str(caught.value) == "SHOPLINE webhook create failed for orders/paid with HTTP 403"
    assert "do-not-leak" not in str(caught.value)


def test_callback_url_requires_configured_https_origin(monkeypatch):
    from routes import shopline_integrations as route

    for name in (
        "SHOPLINE_WEBHOOK_BASE_URL",
        "SHOPLAZZA_WEBHOOK_BASE_URL",
        "PUBLIC_BASE_URL",
        "PIVOTA_BACKEND_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException) as missing:
        route._callback_url("shopline", "store-1")
    assert missing.value.status_code == 503

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://api.example")
    with pytest.raises(HTTPException) as plaintext:
        route._callback_url("shoplazza", "store-2")
    assert plaintext.value.status_code == 503

    monkeypatch.setenv("SHOPLAZZA_WEBHOOK_BASE_URL", "https://hooks.example/base")
    assert route._callback_url("shoplazza", "store 2") == (
        "https://hooks.example/base/webhooks/shoplazza/store%202"
    )


def test_webhook_secret_prefers_store_override_then_platform_environment(monkeypatch):
    from services.shopline_family_webhook_auth import resolve_webhook_secret

    monkeypatch.setenv("SHOPLINE_APP_SECRET", "central-secret")
    assert resolve_webhook_secret("shopline", {}) == "central-secret"
    assert resolve_webhook_secret("shopline", {"app_secret": "store-secret"}) == "store-secret"


@pytest.mark.asyncio
async def test_authenticated_ensure_route_uses_connected_store_credentials(monkeypatch):
    from routes import shopline_integrations as route

    async def fake_store(store_id, platform):
        return {
            "store_id": store_id,
            "merchant_id": "merchant-1",
            "domain": "demo.myshopline.com",
            "api_key": json.dumps(
                {
                    "handle": "demo",
                    "access_token": "token",
                    "api_version": "v20260601",
                    "app_secret": "signing-secret",
                }
            ),
        }

    calls = []

    async def fake_ensure(**kwargs):
        calls.append(kwargs)
        return {
            "platform": "shopline",
            "callback_url": kwargs["callback_url"],
            "created_topics": list(kwargs["topics"]),
            "existing_topics": [],
        }

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example")
    monkeypatch.setattr(route, "_connected_store", fake_store)
    monkeypatch.setattr(route, "ensure_shopline_subscriptions", fake_ensure)
    result = await route.ensure_shopline_webhooks(
        "store-1",
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert result["status"] == "success"
    assert result["callback_url"] == "https://api.example/webhooks/shopline/store-1"
    assert calls[0]["access_token"] == "token"
    assert set(calls[0]["topics"]) == {
        "orders/create",
        "orders/paid",
        "orders/cancelled",
        "refunds/create",
    }


@pytest.mark.asyncio
async def test_ensure_route_rejects_cross_merchant_access_before_upstream(monkeypatch):
    from routes import shopline_integrations as route

    async def fake_store(store_id, platform):
        return {
            "store_id": store_id,
            "merchant_id": "merchant-owner",
            "domain": "demo.myshopline.com",
            "api_key": "{}",
        }

    monkeypatch.setattr(route, "_connected_store", fake_store)
    with pytest.raises(HTTPException) as caught:
        await route.ensure_shopline_webhooks(
            "store-1",
            current_user={"role": "merchant", "merchant_id": "merchant-attacker"},
        )
    assert caught.value.status_code == 403
