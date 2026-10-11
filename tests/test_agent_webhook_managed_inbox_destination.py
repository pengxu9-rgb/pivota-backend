"""A managed-inbox destination is delivered to the inbox's CURRENT URL, whatever host is stored.

In prod, the one enabled agent webhook config stored its managed-inbox URL under the retired API
host. `get_webhook_config` showed the rewritten URL, but emit, test send and retry POSTed the
stored one, and every delivery for weeks 404'd at the old edge. These cases run the REAL send
paths (DB and network stubbed) against a config and delivery row that store the stale host, and
pin that the POST and the persisted row both name `get_managed_receiver_url(agent_id)`.

A destination the agent registered itself is a control: it must go out byte-for-byte as stored.
"""

from __future__ import annotations

import pytest

from services import agent_webhook_service as svc

AGENT = "agent_abc123"
STALE = f"https://retired-host.example/agents/{AGENT}/webhooks/managed-inbox"
CUSTOM = "https://customer.example/hook"
EVENT = svc.DEFAULT_WEBHOOK_EVENTS[0]


class _FakeHttp:
    """Replaces httpx.AsyncClient: records each POST URL and the client kwargs, answers 200."""

    posts: list = []
    client_kwargs: list = []

    def __init__(self, *a, **k):
        _FakeHttp.client_kwargs.append(k)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeHttp.posts.append(url)

        class _Resp:
            status_code = 200
            text = "ok"

        return _Resp()


def _arm(monkeypatch, *, config_url, row_url=None, due_rows=0):
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.delenv("WEBHOOK_DELIVERY_ENABLED", raising=False)
    _FakeHttp.posts = []
    _FakeHttp.client_kwargs = []
    persisted: list = []
    monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeHttp)

    config = {
        "destination_url": config_url,
        "enabled": True,
        "subscribed_events": [EVENT],
        "signing_secret": "whsec_test",
    }
    row = {
        "agent_id": AGENT, "delivery_id": "whd_1", "event_id": "evt_1", "event_type": EVENT,
        "destination_url": row_url, "payload": {"data": {"a": 1}}, "created_at": None,
        "request_id": None, "attempt_count": 1,
    }

    async def raw_config(*a, **k):
        return dict(config)

    async def persist(**kwargs):
        persisted.append(kwargs["destination_url"])

    async def noop(*a, **k):
        return None

    async def fetch_one(query, params=None):
        return dict(row)

    async def fetch_all(query, params=None):
        return [dict(row) for _ in range(due_rows)]

    monkeypatch.setattr(svc, "_get_or_create_raw_config", raw_config)
    monkeypatch.setattr(svc, "_persist_delivery_attempt", persist)
    monkeypatch.setattr(svc, "ensure_agent_webhook_tables", noop)
    monkeypatch.setattr(svc.database, "fetch_one", fetch_one)
    monkeypatch.setattr(svc.database, "fetch_all", fetch_all)
    monkeypatch.setattr(svc.database, "execute", noop)
    return persisted


PATHS = {
    "emit": lambda: svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"a": 1}),
    "send_test_webhook": lambda: svc.send_test_webhook(AGENT),
    "retry_delivery": lambda: svc.retry_delivery(AGENT, "whd_1"),
}


def test_the_stale_url_is_not_already_the_current_one():
    """Guards the premise: if the current URL ever equalled STALE, every case below would pass
    without the fix."""
    assert svc.get_managed_receiver_url(AGENT) != STALE
    assert svc.get_managed_receiver_url(AGENT).endswith(f"/agents/{AGENT}/webhooks/managed-inbox")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PATHS))
async def test_a_stale_managed_inbox_url_is_delivered_to_the_current_one(monkeypatch, path):
    persisted = _arm(monkeypatch, config_url=STALE, row_url=STALE)
    result = await PATHS[path]()
    current = svc.get_managed_receiver_url(AGENT)
    assert _FakeHttp.posts == [current], path
    assert persisted == [current], f"{path} recorded the stale host on the delivery row"
    assert result["status"] == "delivered", result


@pytest.mark.asyncio
async def test_a_retry_whose_row_has_no_url_falls_back_to_the_normalized_config(monkeypatch):
    _arm(monkeypatch, config_url=STALE, row_url=None)
    await svc.retry_delivery(AGENT, "whd_1")
    assert _FakeHttp.posts == [svc.get_managed_receiver_url(AGENT)]


@pytest.mark.asyncio
async def test_the_retry_loop_sends_a_stale_row_to_the_current_inbox(monkeypatch):
    """process_due_retries is how a `retrying` row actually goes out in prod."""
    _arm(monkeypatch, config_url=STALE, row_url=STALE, due_rows=2)
    assert await svc.process_due_retries(limit=5) == 2
    assert _FakeHttp.posts == [svc.get_managed_receiver_url(AGENT)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(PATHS))
async def test_an_agent_registered_url_is_sent_as_stored(monkeypatch, path):
    persisted = _arm(monkeypatch, config_url=CUSTOM, row_url=CUSTOM)
    await PATHS[path]()
    assert _FakeHttp.posts == [CUSTOM]
    assert persisted == [CUSTOM]


@pytest.mark.asyncio
async def test_another_agents_managed_inbox_is_not_rewritten_to_this_one(monkeypatch):
    """The rewrite keys on THIS agent's inbox path, so a URL naming a different agent's inbox
    is left alone rather than silently redirected."""
    other = "https://retired-host.example/agents/agent_other/webhooks/managed-inbox"
    _arm(monkeypatch, config_url=other, row_url=other)
    await svc.emit_agent_webhook_event(AGENT, event_type=EVENT, payload={"a": 1})
    assert _FakeHttp.posts == [other]
