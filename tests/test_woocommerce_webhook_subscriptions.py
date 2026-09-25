import json

import httpx
import pytest
from fastapi import HTTPException


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, pages, *, create_status=201, update_status=200):
        self.pages = list(pages)
        self.create_status = create_status
        self.update_status = update_status
        self.gets = []
        self.posts = []
        self.puts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        response = self.pages[len(self.gets) - 1]
        return response if isinstance(response, FakeResponse) else FakeResponse(200, response)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.create_status, {"id": 99})

    async def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResponse(self.update_status, {"id": 10})


@pytest.mark.asyncio
async def test_installer_creates_missing_and_synchronizes_existing(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    callback = "https://api.example/webhooks/woocommerce/store-1"
    fake = FakeClient(
        [[{"id": 10, "topic": "order.created", "delivery_url": f"{callback}/"}]]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)

    result = await service.ensure_woocommerce_subscriptions(
        store_url="https://shop.example",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        webhook_secret="signing-secret",
        callback_url=callback,
        topics=("order.created", "order.updated"),
    )

    assert result["synchronized_topics"] == ["order.created"]
    assert result["created_topics"] == ["order.updated"]
    assert fake.puts[0][0] == "https://shop.example/wp-json/wc/v3/webhooks/10"
    assert fake.puts[0][1]["json"] == {
        "status": "active",
        "secret": "signing-secret",
    }
    assert fake.posts[0][1]["json"] == {
        "name": "Pivota order.updated",
        "status": "active",
        "topic": "order.updated",
        "delivery_url": callback,
        "secret": "signing-secret",
    }
    auth = fake.posts[0][1]["auth"]
    assert isinstance(auth, httpx.BasicAuth)
    assert "ck_test" not in fake.posts[0][0]
    assert "cs_test" not in fake.posts[0][0]


@pytest.mark.asyncio
async def test_installer_pages_and_rejects_plain_http(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    first_page = [
        {"id": index, "topic": "product.updated", "delivery_url": "https://elsewhere"}
        for index in range(100)
    ]
    fake = FakeClient(
        [
            FakeResponse(200, first_page, {"X-WP-TotalPages": "2"}),
            FakeResponse(200, [], {"X-WP-TotalPages": "2"}),
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)
    await service.ensure_woocommerce_subscriptions(
        store_url="https://shop.example",
        consumer_key="ck",
        consumer_secret="cs",
        webhook_secret="secret",
        callback_url="https://api.example/webhooks/woocommerce/store-1",
        topics=(),
    )
    assert [call[1]["params"]["page"] for call in fake.gets] == [1, 2]

    with pytest.raises(service.WooCommerceWebhookSubscriptionError) as caught:
        await service.ensure_woocommerce_subscriptions(
            store_url="http://shop.example",
            consumer_key="ck",
            consumer_secret="cs",
            webhook_secret="secret",
            callback_url="https://api.example/webhooks/woocommerce/store-1",
            topics=("order.created",),
        )
    assert "HTTPS" in str(caught.value)


@pytest.mark.asyncio
async def test_installer_does_not_request_page_after_exact_full_last_page(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    full_page = [
        {"id": index, "topic": "product.updated", "delivery_url": "https://elsewhere"}
        for index in range(100)
    ]
    fake = FakeClient(
        [
            FakeResponse(200, full_page, {"X-WP-TotalPages": "1"}),
            FakeResponse(400, {"code": "rest_post_invalid_page_number"}),
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)
    await service.ensure_woocommerce_subscriptions(
        store_url="https://shop.example",
        consumer_key="ck",
        consumer_secret="cs",
        webhook_secret="secret",
        callback_url="https://api.example/webhooks/woocommerce/store-1",
        topics=(),
    )
    assert len(fake.gets) == 1


@pytest.mark.asyncio
async def test_installer_accepts_empty_store_total_pages_zero(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    fake = FakeClient(
        [FakeResponse(200, [], {"X-WP-TotalPages": "0"})]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)
    result = await service.ensure_woocommerce_subscriptions(
        store_url="https://shop.example",
        consumer_key="ck",
        consumer_secret="cs",
        webhook_secret="secret",
        callback_url="https://api.example/webhooks/woocommerce/store-1",
        topics=("order.created", "order.updated"),
    )
    assert result["created_topics"] == ["order.created", "order.updated"]
    assert len(fake.posts) == 2


@pytest.mark.asyncio
async def test_installer_synchronizes_canonical_and_disables_duplicates(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    callback = "https://api.example/webhooks/woocommerce/store-1"
    fake = FakeClient(
        [
            FakeResponse(
                200,
                [
                    {"id": 20, "topic": "order.created", "delivery_url": callback},
                    {"id": 3, "topic": "order.created", "delivery_url": callback},
                    {"id": 10, "topic": "order.created", "delivery_url": callback},
                ],
                {"X-WP-TotalPages": "1"},
            )
        ]
    )
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)
    result = await service.ensure_woocommerce_subscriptions(
        store_url="https://shop.example",
        consumer_key="ck",
        consumer_secret="cs",
        webhook_secret="rotated-secret",
        callback_url=callback,
        topics=("order.created",),
    )
    assert fake.puts[0][0].endswith("/3")
    assert fake.puts[0][1]["json"] == {
        "status": "active",
        "secret": "rotated-secret",
    }
    assert [call[1]["json"] for call in fake.puts[1:]] == [
        {"status": "disabled"},
        {"status": "disabled"},
    ]
    assert result["disabled_duplicate_webhook_ids"] == [10, 20]


@pytest.mark.asyncio
async def test_installer_errors_do_not_leak_credentials(monkeypatch):
    from services import woocommerce_webhook_subscriptions as service

    fake = FakeClient([[]], create_status=403)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def public_target(_url):
        return "shop.example", "203.0.113.10"

    monkeypatch.setattr(service, "_validate_public_https_target", public_target)
    monkeypatch.setattr(service, "_pinned_https_url", lambda url, _address: url)
    with pytest.raises(service.WooCommerceWebhookSubscriptionError) as caught:
        await service.ensure_woocommerce_subscriptions(
            store_url="https://shop.example",
            consumer_key="do-not-leak-key",
            consumer_secret="do-not-leak-secret",
            webhook_secret="do-not-leak-hook",
            callback_url="https://api.example/webhooks/woocommerce/store-1",
            topics=("order.created",),
        )
    message = str(caught.value)
    assert message == "WooCommerce webhook create failed for order.created with HTTP 403"
    assert "do-not-leak" not in message


def test_callback_url_requires_https_origin(monkeypatch):
    from routes import merchant_store_connections as route

    for name in (
        "WOOCOMMERCE_WEBHOOK_BASE_URL",
        "PUBLIC_BASE_URL",
        "PIVOTA_BACKEND_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException) as missing:
        route._woocommerce_webhook_callback_url("store-1")
    assert missing.value.status_code == 503

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://api.example")
    with pytest.raises(HTTPException) as plaintext:
        route._woocommerce_webhook_callback_url("store-1")
    assert plaintext.value.status_code == 503

    monkeypatch.setenv("WOOCOMMERCE_WEBHOOK_BASE_URL", "https://api.example/base")
    assert route._woocommerce_webhook_callback_url("store 1") == (
        "https://api.example/base/webhooks/woocommerce/store%201"
    )


def test_route_credentials_support_legacy_colon_format():
    from routes import merchant_store_connections as route

    assert route._woocommerce_credentials("ck_legacy:cs_legacy") == {
        "consumer_key": "ck_legacy",
        "consumer_secret": "cs_legacy",
    }


@pytest.mark.asyncio
async def test_authenticated_route_supports_legacy_colon_credentials(monkeypatch):
    from routes import merchant_store_connections as route
    from services import woocommerce_webhook_subscriptions as service

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-legacy",
                "merchant_id": "merchant-1",
                "domain": "https://shop.example",
                "api_key": "ck_legacy:cs_legacy",
            }

    calls = []

    async def fake_ensure(**kwargs):
        calls.append(kwargs)
        return {
            "platform": "woocommerce",
            "callback_url": kwargs["callback_url"],
            "created_topics": [],
            "synchronized_topics": list(kwargs["topics"]),
        }

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example")
    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(service, "ensure_woocommerce_subscriptions", fake_ensure)
    await route.ensure_woocommerce_webhooks(
        "store-legacy",
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )
    assert calls[0]["consumer_key"] == "ck_legacy"
    assert calls[0]["consumer_secret"] == "cs_legacy"
    assert calls[0]["webhook_secret"] == "cs_legacy"


@pytest.mark.asyncio
async def test_authenticated_route_uses_connected_store_credentials(monkeypatch):
    from routes import merchant_store_connections as route
    from services import woocommerce_webhook_subscriptions as service

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-1",
                "merchant_id": "merchant-1",
                "domain": "https://shop.example",
                "api_key": json.dumps(
                    {
                        "consumer_key": "ck_test",
                        "consumer_secret": "cs_test",
                        "webhook_secret": "hook-secret",
                    }
                ),
            }

    calls = []

    async def fake_ensure(**kwargs):
        calls.append(kwargs)
        return {
            "platform": "woocommerce",
            "callback_url": kwargs["callback_url"],
            "created_topics": list(kwargs["topics"]),
            "synchronized_topics": [],
        }

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example")
    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(service, "ensure_woocommerce_subscriptions", fake_ensure)
    result = await route.ensure_woocommerce_webhooks(
        "store-1",
        current_user={"role": "merchant", "merchant_id": "merchant-1"},
    )

    assert result["status"] == "success"
    assert result["callback_url"] == (
        "https://api.example/webhooks/woocommerce/store-1"
    )
    assert calls[0]["consumer_key"] == "ck_test"
    assert calls[0]["webhook_secret"] == "hook-secret"


@pytest.mark.asyncio
async def test_route_rejects_cross_merchant_before_upstream(monkeypatch):
    from routes import merchant_store_connections as route

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-1",
                "merchant_id": "merchant-owner",
                "domain": "https://shop.example",
                "api_key": "{}",
            }

    monkeypatch.setattr(route, "database", FakeDB())
    with pytest.raises(HTTPException) as caught:
        await route.ensure_woocommerce_webhooks(
            "store-1",
            current_user={"role": "merchant", "merchant_id": "merchant-attacker"},
        )
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_route_sanitizes_upstream_network_errors(monkeypatch):
    from routes import merchant_store_connections as route
    from services import woocommerce_webhook_subscriptions as service

    class FakeDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": "store-1",
                "merchant_id": "merchant-1",
                "domain": "https://shop.example",
                "api_key": json.dumps(
                    {
                        "consumer_key": "ck_private",
                        "consumer_secret": "cs_private",
                    }
                ),
            }

    async def failed_request(**kwargs):
        request = httpx.Request("GET", "https://shop.example")
        raise httpx.ConnectError("connection failed for ck_private", request=request)

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example")
    monkeypatch.setattr(route, "database", FakeDB())
    monkeypatch.setattr(service, "ensure_woocommerce_subscriptions", failed_request)
    with pytest.raises(HTTPException) as caught:
        await route.ensure_woocommerce_webhooks(
            "store-1",
            current_user={"role": "merchant", "merchant_id": "merchant-1"},
        )
    assert caught.value.status_code == 502
    assert caught.value.detail == "WooCommerce webhook management request failed"
    assert "ck_private" not in caught.value.detail


@pytest.mark.asyncio
async def test_store_advisory_lock_rejects_concurrent_installer(monkeypatch):
    from routes import merchant_store_connections as route
    import asyncpg

    class FakeConnection:
        def __init__(self, state):
            self.state = state

        async def fetchval(self, *args, **kwargs):
            if self.state["locked"]:
                return False
            self.state["locked"] = True
            return True

        async def execute(self, *args, **kwargs):
            self.state["locked"] = False

        async def close(self):
            return None

        def terminate(self):
            return None

    state = {"locked": False}

    async def fake_connect(*args, **kwargs):
        return FakeConnection(state)

    monkeypatch.setattr(route, "IS_POSTGRES", True)
    monkeypatch.setattr(route, "_asyncpg_dsn", lambda: "postgresql://test")
    monkeypatch.setattr(route, "_connect_kwargs", lambda: {})
    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    async with route._woocommerce_webhook_install_lock("store-1"):
        with pytest.raises(HTTPException) as caught:
            async with route._woocommerce_webhook_install_lock("store-1"):
                pass
        assert caught.value.status_code == 409
    assert state["locked"] is False
