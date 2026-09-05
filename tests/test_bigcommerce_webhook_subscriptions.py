"""The BigCommerce hook installer.

BigCommerce does not sign deliveries, so the ONLY thing that makes the
receiver's header check meaningful is that this installer registers the secret
in the hook's `headers` map. A hook created without it would deliver events
that can never authenticate — which is why the header assertions here are the
point of the file, not decoration.
"""

from __future__ import annotations

import json

import pytest


CALLBACK = "https://api.example/webhooks/bigcommerce/store-bc"
SECRET = "bc-signing-secret"
HOOKS_ENDPOINT = "https://api.bigcommerce.com/stores/abcde/v3/hooks"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

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
        page = self.pages[len(self.gets) - 1]
        return page if isinstance(page, FakeResponse) else FakeResponse(200, page)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.create_status, {"data": {"id": 99}})

    async def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return FakeResponse(self.update_status, {"data": {"id": 10}})


def _install(monkeypatch, fake):
    from services import bigcommerce_webhook_subscriptions as service

    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: fake)
    return service


@pytest.mark.asyncio
async def test_installer_creates_missing_and_synchronizes_existing(monkeypatch):
    fake = FakeClient(
        [
            {
                "data": [
                    {
                        "id": 10,
                        "scope": "store/order/created",
                        "destination": f"{CALLBACK}/",
                        "is_active": True,
                    }
                ]
            }
        ]
    )
    service = _install(monkeypatch, fake)

    result = await service.ensure_bigcommerce_subscriptions(
        store_hash="abcde",
        access_token="bc-access-token",
        client_id="bc-client",
        callback_url=CALLBACK,
        secret=SECRET,
        scopes=("store/order/created", "store/order/refund/created"),
    )

    assert result["synchronized_scopes"] == ["store/order/created"]
    assert result["created_scopes"] == ["store/order/refund/created"]
    assert result["disabled_duplicates"] == []
    assert result["callback_url"] == CALLBACK

    assert fake.gets[0][0] == HOOKS_ENDPOINT
    assert fake.gets[0][1]["headers"]["X-Auth-Token"] == "bc-access-token"

    # The existing hook is re-pointed AND re-credentialed, so a rotation
    # cannot silently leave deliveries unverifiable.
    assert fake.puts[0][0] == f"{HOOKS_ENDPOINT}/10"
    assert fake.puts[0][1]["json"] == {
        "scope": "store/order/created",
        "destination": CALLBACK,
        "is_active": True,
        "headers": {"X-Pivota-Webhook-Secret": SECRET},
    }
    assert fake.posts[0][1]["json"] == {
        "scope": "store/order/refund/created",
        "destination": CALLBACK,
        "is_active": True,
        "headers": {"X-Pivota-Webhook-Secret": SECRET},
    }


@pytest.mark.asyncio
async def test_every_created_and_updated_hook_carries_the_secret_header(monkeypatch):
    """The one assertion the whole auth model rests on."""
    fake = FakeClient(
        [
            {
                "data": [
                    {"id": 10, "scope": "store/order/created", "destination": CALLBACK},
                ]
            }
        ]
    )
    service = _install(monkeypatch, fake)

    await service.ensure_bigcommerce_subscriptions(
        store_hash="abcde",
        access_token="t",
        callback_url=CALLBACK,
        secret=SECRET,
    )

    installs = [call[1]["json"] for call in fake.posts] + [
        call[1]["json"] for call in fake.puts
    ]
    assert installs, "the installer made no create/update calls"
    for body in installs:
        assert body["headers"] == {"X-Pivota-Webhook-Secret": SECRET}
    # Every default scope was installed exactly once.
    assert sorted(body["scope"] for body in installs) == sorted(
        service.SUPPORTED_BIGCOMMERCE_SCOPES
    )


@pytest.mark.asyncio
async def test_duplicates_for_one_scope_are_deactivated_not_deleted(monkeypatch):
    fake = FakeClient(
        [
            {
                "data": [
                    {"id": 21, "scope": "store/order/created", "destination": CALLBACK},
                    {"id": 7, "scope": "store/order/created", "destination": CALLBACK},
                    {"id": 44, "scope": "store/order/created", "destination": CALLBACK},
                    # Another app's hook for the same scope: not ours to touch.
                    {
                        "id": 90,
                        "scope": "store/order/created",
                        "destination": "https://elsewhere.example/hook",
                    },
                ]
            }
        ]
    )
    service = _install(monkeypatch, fake)

    result = await service.ensure_bigcommerce_subscriptions(
        store_hash="abcde",
        access_token="t",
        callback_url=CALLBACK,
        secret=SECRET,
        scopes=("store/order/created",),
    )

    # Lowest id wins and is synchronized; the rest are disabled.
    assert result["synchronized_scopes"] == ["store/order/created"]
    assert result["disabled_duplicates"] == [21, 44]
    assert [call[0] for call in fake.puts] == [
        f"{HOOKS_ENDPOINT}/7",
        f"{HOOKS_ENDPOINT}/21",
        f"{HOOKS_ENDPOINT}/44",
    ]
    assert fake.puts[1][1]["json"] == {"is_active": False}
    assert fake.puts[2][1]["json"] == {"is_active": False}
    assert 90 not in result["disabled_duplicates"]
    assert fake.posts == []


@pytest.mark.asyncio
async def test_the_secret_never_reaches_a_url_or_the_log(monkeypatch, caplog):
    fake = FakeClient([{"data": []}])
    service = _install(monkeypatch, fake)

    with caplog.at_level("DEBUG"):
        await service.ensure_bigcommerce_subscriptions(
            store_hash="abcde",
            access_token="bc-access-token",
            callback_url=CALLBACK,
            secret=SECRET,
        )

    for url, kwargs in fake.posts + fake.puts + fake.gets:
        assert SECRET not in url
        assert SECRET not in json.dumps(kwargs.get("params") or {})
    assert SECRET not in caplog.text
    assert "bc-access-token" not in caplog.text


@pytest.mark.asyncio
async def test_incomplete_credentials_and_a_non_https_callback_are_refused(monkeypatch):
    fake = FakeClient([{"data": []}])
    service = _install(monkeypatch, fake)

    for kwargs in (
        {"store_hash": "", "access_token": "t", "secret": SECRET},
        {"store_hash": "abcde", "access_token": "", "secret": SECRET},
        {"store_hash": "abcde", "access_token": "t", "secret": ""},
    ):
        with pytest.raises(service.BigCommerceWebhookSubscriptionError):
            await service.ensure_bigcommerce_subscriptions(
                callback_url=CALLBACK, **kwargs
            )

    with pytest.raises(service.BigCommerceWebhookSubscriptionError):
        await service.ensure_bigcommerce_subscriptions(
            store_hash="abcde",
            access_token="t",
            callback_url="http://api.example/webhooks/bigcommerce/store-bc",
            secret=SECRET,
        )
    # Nothing was installed on any refused call.
    assert fake.posts == []
    assert fake.puts == []


@pytest.mark.asyncio
async def test_a_failed_list_or_create_raises_instead_of_reporting_success(monkeypatch):
    service = _install(monkeypatch, FakeClient([FakeResponse(403, {})]))
    with pytest.raises(service.BigCommerceWebhookSubscriptionError):
        await service.ensure_bigcommerce_subscriptions(
            store_hash="abcde",
            access_token="t",
            callback_url=CALLBACK,
            secret=SECRET,
        )

    service = _install(monkeypatch, FakeClient([{"data": []}], create_status=422))
    with pytest.raises(service.BigCommerceWebhookSubscriptionError):
        await service.ensure_bigcommerce_subscriptions(
            store_hash="abcde",
            access_token="t",
            callback_url=CALLBACK,
            secret=SECRET,
        )


@pytest.mark.asyncio
async def test_a_bare_list_body_is_tolerated(monkeypatch):
    """The v3 hooks list wrapper could not be verified from the public docs."""
    fake = FakeClient([[{"id": 10, "scope": "store/order/created", "destination": CALLBACK}]])
    service = _install(monkeypatch, fake)

    result = await service.ensure_bigcommerce_subscriptions(
        store_hash="abcde",
        access_token="t",
        callback_url=CALLBACK,
        secret=SECRET,
        scopes=("store/order/created",),
    )
    assert result["synchronized_scopes"] == ["store/order/created"]
