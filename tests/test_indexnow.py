from __future__ import annotations

from unittest.mock import patch

import pytest

from config import settings as settings_module


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(settings_module.settings, "indexnow_enabled", True)
    monkeypatch.setattr(settings_module.settings, "indexnow_host", "agent.pivota.cc")
    monkeypatch.setattr(settings_module.settings, "indexnow_key", "testkey")
    monkeypatch.setattr(
        settings_module.settings,
        "indexnow_endpoint",
        "https://api.indexnow.org/indexnow",
    )


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stand-in for httpx.AsyncClient: records the last POST and returns a
    configurable status (or raises if `boom` is set)."""

    status = 200
    boom = False
    last = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.last = {"url": url, "json": json, "headers": headers}
        if _FakeClient.boom:
            raise RuntimeError("network down")
        return _FakeResp(_FakeClient.status)


@pytest.mark.asyncio
async def test_submit_noop_when_disabled(monkeypatch) -> None:
    from services import indexnow

    monkeypatch.setattr(settings_module.settings, "indexnow_enabled", False)
    _FakeClient.last = None
    with patch("httpx.AsyncClient", _FakeClient):
        assert await indexnow.submit_url("https://agent.pivota.cc/products/sig_a") is False
    assert _FakeClient.last is None  # never hit the network


@pytest.mark.asyncio
async def test_submit_filters_foreign_hosts_and_builds_payload(monkeypatch) -> None:
    from services import indexnow

    _enable(monkeypatch)
    _FakeClient.status = 200
    _FakeClient.boom = False
    _FakeClient.last = None
    with patch("httpx.AsyncClient", _FakeClient):
        ok = await indexnow.submit_urls(
            [
                "https://evil.com/x",
                "https://agent.pivota.cc/products/sig_1",
                "https://agent.pivota.cc/products/sig_1",  # dupe
                "https://agent.pivota.cc/products/sig_2",
            ]
        )
    assert ok is True
    body = _FakeClient.last["json"]
    assert body["host"] == "agent.pivota.cc"
    assert body["key"] == "testkey"
    assert body["keyLocation"] == "https://agent.pivota.cc/testkey.txt"
    assert body["urlList"] == [
        "https://agent.pivota.cc/products/sig_1",
        "https://agent.pivota.cc/products/sig_2",
    ]


@pytest.mark.asyncio
async def test_submit_returns_false_when_nothing_valid(monkeypatch) -> None:
    from services import indexnow

    _enable(monkeypatch)
    _FakeClient.last = None
    with patch("httpx.AsyncClient", _FakeClient):
        assert await indexnow.submit_urls(["https://evil.com/x"]) is False
    assert _FakeClient.last is None


@pytest.mark.asyncio
async def test_submit_non_2xx_returns_false(monkeypatch) -> None:
    from services import indexnow

    _enable(monkeypatch)
    _FakeClient.status = 422
    _FakeClient.boom = False
    with patch("httpx.AsyncClient", _FakeClient):
        assert await indexnow.submit_url("https://agent.pivota.cc/products/sig_1") is False


@pytest.mark.asyncio
async def test_submit_never_raises_on_network_error(monkeypatch) -> None:
    from services import indexnow

    _enable(monkeypatch)
    _FakeClient.boom = True
    with patch("httpx.AsyncClient", _FakeClient):
        assert await indexnow.submit_url("https://agent.pivota.cc/products/sig_1") is False
    _FakeClient.boom = False


@pytest.mark.asyncio
async def test_schedule_submit_url_fires_task(monkeypatch) -> None:
    import asyncio

    from services import indexnow

    _enable(monkeypatch)
    _FakeClient.status = 200
    _FakeClient.boom = False
    _FakeClient.last = None
    with patch("httpx.AsyncClient", _FakeClient):
        indexnow.schedule_submit_url("https://agent.pivota.cc/products/sig_9")
        # let the scheduled task run
        await asyncio.sleep(0.05)
    assert _FakeClient.last is not None
    assert _FakeClient.last["json"]["urlList"] == [
        "https://agent.pivota.cc/products/sig_9"
    ]
