"""Tests for the retry-once logic in agent_center_llm_client.probe.

The motivating incident: a BD report run failed with an empty-message
RemoteProtocolError when PIVOTA-Agent rolled out a deploy mid-request.
The fix retries once on transport-layer errors so a single rolling
restart on either side doesn't fail the whole report.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture
def configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the upstream API key so probe() takes the real-HTTP path
    instead of returning the local mock."""
    from services import agent_center_llm_client as llm_client
    monkeypatch.setattr(
        llm_client.settings, "pivota_agent_internal_api_key", "test-secret"
    )


def _ok_response() -> httpx.Response:
    """A successful llm-probe upstream response shape."""
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "scan_mode": "open_product_visibility_test",
                "provider": "gemini",
                "runs_count": 1,
                "scores": {"visibility_score": 50, "attribution_echo_rate": 0},
                "findings": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "raw_runs": [],
            },
        },
    )


@pytest.mark.asyncio
async def test_probe_retries_once_on_remote_protocol_error(
    configured_api_key: None,
) -> None:
    """RemoteProtocolError on first attempt → retry → second attempt
    succeeds → caller gets the result. This is the deploy-overlap case."""
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _flaky_post(self, url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.RemoteProtocolError("")  # empty msg, like real incident
        return _ok_response()

    with patch.object(httpx.AsyncClient, "post", _flaky_post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="gemini",
            max_runs=1,
        )

    assert call_count["n"] == 2
    assert result["provider"] == "gemini"


@pytest.mark.asyncio
async def test_probe_retries_once_on_connect_error(
    configured_api_key: None,
) -> None:
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _flaky_post(self, url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return _ok_response()

    with patch.object(httpx.AsyncClient, "post", _flaky_post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="gemini",
            max_runs=1,
        )

    assert call_count["n"] == 2
    assert result["provider"] == "gemini"


@pytest.mark.asyncio
async def test_probe_retries_once_on_read_timeout(
    configured_api_key: None,
) -> None:
    """ReadTimeout on the first attempt → retry → second attempt
    succeeds → caller gets the result.

    Prod incident 2026-05-14 (audit f33b6069): many products failed
    mid-audit on `ReadTimeout('')`. A ReadTimeout means the call
    FAILED — the time window was spent for zero result — so a single
    retry has a real chance of producing an actual result. A lost
    product is worse than a slightly slower audit. Reverses the prior
    deliberate-but-wrong "timeouts are not retried" classification."""
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _flaky_post(self, url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ReadTimeout("")  # empty msg, like the real incident
        return _ok_response()

    with patch.object(httpx.AsyncClient, "post", _flaky_post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="gemini",
            max_runs=1,
        )

    assert call_count["n"] == 2
    assert result["provider"] == "gemini"


@pytest.mark.asyncio
async def test_probe_gives_up_after_two_read_timeouts(
    configured_api_key: None,
) -> None:
    """Both attempts time out → raise with the exception class named.
    The retry is bounded at one extra attempt — a genuinely slow /
    overloaded upstream still fails, just after 2 windows not 1, and
    the error message identifies the cause."""
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _always_timeout(self, url, **kwargs):
        call_count["n"] += 1
        raise httpx.ReadTimeout("")

    with patch.object(httpx.AsyncClient, "post", _always_timeout):
        with pytest.raises(llm_client.AgentCenterLlmClientError) as excinfo:
            await llm_client.probe(
                scan_mode="open_product_visibility_test",
                scan_target_id="t1",
                merchant_id="m1",
                store_id="s1",
                provider="gemini",
                max_runs=1,
            )

    assert call_count["n"] == 2
    assert "ReadTimeout" in str(excinfo.value)


@pytest.mark.asyncio
async def test_probe_gives_up_after_two_transport_errors(
    configured_api_key: None,
) -> None:
    """Both attempts fail with retryable transport errors → raise with
    the exception class named, so logs identify the root cause instead
    of showing 'transport failed: ' with empty repr."""
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _always_fail(self, url, **kwargs):
        call_count["n"] += 1
        raise httpx.RemoteProtocolError("")

    with patch.object(httpx.AsyncClient, "post", _always_fail):
        with pytest.raises(llm_client.AgentCenterLlmClientError) as excinfo:
            await llm_client.probe(
                scan_mode="open_product_visibility_test",
                scan_target_id="t1",
                merchant_id="m1",
                store_id="s1",
                provider="gemini",
                max_runs=1,
            )

    assert call_count["n"] == 2
    # Names the exception class so future on-call doesn't see empty msg.
    assert "RemoteProtocolError" in str(excinfo.value)
    assert "after retry" in str(excinfo.value)


@pytest.mark.asyncio
async def test_probe_succeeds_first_try_no_retry_overhead(
    configured_api_key: None,
) -> None:
    """First attempt succeeds → exactly one HTTP call, no retry latency."""
    from services import agent_center_llm_client as llm_client

    call_count = {"n": 0}

    async def _good_post(self, url, **kwargs):
        call_count["n"] += 1
        return _ok_response()

    with patch.object(httpx.AsyncClient, "post", _good_post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="gemini",
            max_runs=1,
        )

    assert call_count["n"] == 1
    assert result["provider"] == "gemini"


@pytest.mark.asyncio
async def test_probe_sends_request_level_model_and_stamps_result(
    configured_api_key: None,
) -> None:
    from services import agent_center_llm_client as llm_client

    captured: Dict[str, Any] = {}

    async def _post(self, url, **kwargs):
        captured.update(kwargs.get("json") or {})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "scan_mode": "open_product_visibility_test",
                    "provider": "chatgpt",
                    "runs_count": 1,
                    "scores": {"visibility_score": 50},
                    "findings": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "raw_runs": [],
                },
            },
        )

    with patch.object(httpx.AsyncClient, "post", _post):
        result = await llm_client.probe(
            scan_mode="open_product_visibility_test",
            scan_target_id="t1",
            merchant_id="m1",
            store_id="s1",
            provider="chatgpt",
            max_runs=1,
            model="gpt-5.5-mini",
            model_is_override=True,
        )

    assert captured["options"]["model"] == "gpt-5.5-mini"
    assert result["model"] == "gpt-5.5-mini"
    assert result["model_is_override"] is True
