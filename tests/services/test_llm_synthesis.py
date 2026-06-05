from __future__ import annotations

import json
from typing import Any, Dict

import httpx
import pytest

from services import llm_synthesis


def _client_factory(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_async_client = httpx.AsyncClient

    class _ClientFactory:
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            self._client = real_async_client(*args, **kwargs)

        async def __aenter__(self):
            return await self._client.__aenter__()

        async def __aexit__(self, *exc):
            return await self._client.__aexit__(*exc)

    monkeypatch.setattr(llm_synthesis.httpx, "AsyncClient", _ClientFactory)


def _transport(payload: Dict[str, Any], captured: Dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_synthesize_builds_deepseek_request(monkeypatch):
    captured: Dict[str, Any] = {}
    _client_factory(
        monkeypatch,
        _transport(
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
            captured,
        ),
    )
    monkeypatch.setattr(llm_synthesis.settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(
        llm_synthesis.settings,
        "deepseek_api_base_url",
        "https://api.deepseek.com",
    )

    result = await llm_synthesis.synthesize(
        system="SYS",
        user="USR",
        provider="deepseek",
        model="deepseek-chat",
        max_tokens=321,
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer deepseek-key"
    assert captured["body"]["model"] == "deepseek-chat"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert "tools" not in captured["body"]
    assert result == {
        "text": '{"ok": true}',
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "provider": "deepseek",
        "model": "deepseek-chat",
    }


@pytest.mark.asyncio
async def test_synthesize_builds_openai_request_for_chatgpt_alias(monkeypatch):
    captured: Dict[str, Any] = {}
    _client_factory(
        monkeypatch,
        _transport(
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            captured,
        ),
    )
    monkeypatch.setattr(llm_synthesis.settings, "openai_api_key", "openai-key")

    result = await llm_synthesis.synthesize(
        system="SYS",
        user="USR",
        provider="chatgpt",
        model="gpt-4.1",
    )

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer openai-key"
    assert captured["body"]["model"] == "gpt-4.1"
    assert result["provider"] == "openai"
    assert result["usage"] == {"input_tokens": 5, "output_tokens": 3}


@pytest.mark.asyncio
async def test_synthesize_builds_anthropic_request_for_claude_alias(monkeypatch):
    captured: Dict[str, Any] = {}
    _client_factory(
        monkeypatch,
        _transport(
            {
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "usage": {"input_tokens": 13, "output_tokens": 8},
            },
            captured,
        ),
    )
    monkeypatch.setattr(llm_synthesis.settings, "anthropic_api_key", "anthropic-key")

    result = await llm_synthesis.synthesize(
        system="SYS",
        user="USR",
        provider="claude",
        model="claude-3-5-sonnet-latest",
    )

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "anthropic-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "SYS"
    assert captured["body"]["messages"] == [{"role": "user", "content": "USR"}]
    assert captured["body"]["model"] == "claude-3-5-sonnet-latest"
    assert result == {
        "text": '{"ok": true}',
        "usage": {"input_tokens": 13, "output_tokens": 8},
        "provider": "anthropic",
        "model": "claude-3-5-sonnet-latest",
    }


@pytest.mark.asyncio
async def test_synthesize_raises_typed_missing_key(monkeypatch):
    monkeypatch.setattr(llm_synthesis.settings, "openai_api_key", None)

    with pytest.raises(llm_synthesis.MissingLLMKeyError):
        await llm_synthesis.synthesize(
            system="SYS",
            user="USR",
            provider="openai",
            model="gpt-4.1",
        )
