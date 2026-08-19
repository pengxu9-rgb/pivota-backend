"""Registration notification: unset → silent; set → one POST, never the key, never raises."""

import pytest


@pytest.mark.asyncio
async def test_notify_is_silent_when_unset(monkeypatch):
    import services.agent_registration_notify as mod

    monkeypatch.delenv("AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL", raising=False)
    assert await mod.notify_agent_registered(
        agent_id="agent_1", agent_name="A", email="a@x.io", company=None, client_ip="1.1.1.1", key_sync_source="api_keys"
    ) is False


def test_notice_carries_identity_fields_and_no_secret():
    import services.agent_registration_notify as mod

    notice = mod.build_registration_notice(
        agent_id="agent_1", agent_name="Minds", email="m@minds.io", company="Minds Inc", client_ip="9.9.9.9", key_sync_source="api_keys"
    )
    assert notice["event"] == "agent.registered"
    assert notice["agent_id"] == "agent_1" and notice["email"] == "m@minds.io" and notice["company"] == "Minds Inc"
    assert "Minds" in notice["text"] and "agent_1" in notice["text"]
    assert "ak_live" not in str(notice)
    assert "api_key" not in notice


@pytest.mark.asyncio
async def test_notify_posts_once_and_swallows_failures(monkeypatch):
    import services.agent_registration_notify as mod

    monkeypatch.setenv("AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL", "https://hooks.example/abc")
    calls = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok = await mod.notify_agent_registered(
        agent_id="agent_2", agent_name="B", email="b@x.io", company=None, client_ip="2.2.2.2", key_sync_source="api_keys"
    )
    assert ok is True
    assert len(calls) == 1 and calls[0][0] == "https://hooks.example/abc"
    assert calls[0][1]["agent_id"] == "agent_2"

    class _Boom(_Client):
        async def post(self, url, json=None):
            raise RuntimeError("webhook down")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert await mod.notify_agent_registered(
        agent_id="agent_3", agent_name="C", email="c@x.io", company=None, client_ip="3.3.3.3", key_sync_source="api_keys"
    ) is False
