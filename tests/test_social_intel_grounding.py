"""Social-intel grounding stability — model tier, transport retry, and
ungrounded telemetry.

## The problem

The social sub-calls (own presence / KOL / competitive) run through
`_gemini_grounded_call` with the `google_search` tool enabled — but the
tool is model-discretionary. On `gemini-2.5-flash` the model routinely
answered social/KOL queries from internal knowledge WITHOUT searching:
the response carried zero grounding chunks, PR-9's honesty gate
(correctly) nulled every figure, and the merchant saw an empty section.

PR #530 tried to fix this with prompt escalation + an ungrounded retry.
Measured 2026-05-14: it recovered 0 of 13 ungrounded attempts. Prompt
escalation does not move a model that decided not to search. Two changes
replaced it (see ~/tmp/social-grounding-check/verdict.md):

1. **Model lever** — social probes use `_GEMINI_SOCIAL_MODEL`, a model
   chosen to ground social/KOL queries better than the stable 2.5-flash.
   (gemini-2.5-pro was tried first, PR #531, and reverted — it didn't
   move the rate; the current pick is a newer flash generation.)
   Non-social bd_ probes (retail / founder / press) stay on 2.5-flash.
2. **Transport retry** — `_gemini_grounded_call` retries a
   transport/timeout failure once. The bd_ social path uses its own
   httpx client and never got #528's transport retry; this is the same
   fix, here. `transport: ReadTimeout` was 43% of bd_ calls on 2026-05-14.

The ungrounded telemetry (`error_message="ungrounded"` on a
200-but-zero-grounding-chunks response) is kept — it's the feedback loop.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.bd_brand_signals import (
    _GEMINI_MODEL,
    _GEMINI_SOCIAL_MODEL,
    _GROUNDING_DIRECTIVE,
    _build_competitive_prompt,
    _build_kol_prompt,
    _build_own_presence_prompt,
    _gemini_grounded_call,
    _infer_competitive_social,
    _infer_kol_endorsements,
    _infer_own_presence,
)


def _payload(text: str, *, grounded: bool) -> Dict[str, Any]:
    """Gemini-shaped payload. `grounded` controls whether
    groundingMetadata.groundingChunks is populated."""
    candidate: Dict[str, Any] = {"content": {"parts": [{"text": text}]}}
    if grounded:
        candidate["groundingMetadata"] = {
            "groundingChunks": [
                {"web": {"uri": "https://socialblade.com/x", "title": "SB"}},
            ],
        }
    return {"candidates": [candidate]}


# =========================================================================
# Part 1 — prompt directives
# =========================================================================


def test_prompts_carry_the_search_directive():
    """Every social prompt carries the grounding directive that tells
    the model it MUST search first. (The escalated retry variant was
    removed with PR #530's retry — prompt escalation didn't ground.)"""
    own = _build_own_presence_prompt("Beauty of Joseon", "tiktok", "boj")
    kol = _build_kol_prompt("Beauty of Joseon", "instagram")
    comp = _build_competitive_prompt("Beauty of Joseon", ["Drunk Elephant"])
    for prompt in (own, kol, comp):
        assert _GROUNDING_DIRECTIVE in prompt
        assert "MUST use web search" in prompt


# =========================================================================
# Part 2a — model lever: social probes run on `_GEMINI_SOCIAL_MODEL`
# =========================================================================


@pytest.mark.asyncio
async def test_own_presence_uses_the_social_model():
    """`_infer_own_presence` calls `_gemini_grounded_call` with the
    social model — the stable flash tier grounds social queries poorly."""
    mock = AsyncMock(return_value=_payload(
        '{"follower_estimate": 5, "follower_band": "<10k"}', grounded=True,
    ))
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        await _infer_own_presence("Beauty of Joseon", "tiktok", "boj", "k")
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["model"] == _GEMINI_SOCIAL_MODEL
    assert kwargs["scan_mode"] == "bd_own_presence"


@pytest.mark.asyncio
async def test_kol_endorsements_uses_the_social_model():
    mock = AsyncMock(return_value=_payload(
        '[{"creator_handle": "@x", "follower_band": "10k-100k"}]',
        grounded=True,
    ))
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        await _infer_kol_endorsements("Beauty of Joseon", "instagram", "k")
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["model"] == _GEMINI_SOCIAL_MODEL
    assert kwargs["scan_mode"] == "bd_kol_endorsements"


@pytest.mark.asyncio
async def test_competitive_social_uses_the_social_model():
    mock = AsyncMock(return_value=_payload(
        '[{"brand": "Drunk Elephant", "gap_summary": "ahead"}]',
        grounded=True,
    ))
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        await _infer_competitive_social(
            "Beauty of Joseon", ["Drunk Elephant"], "k",
        )
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["model"] == _GEMINI_SOCIAL_MODEL
    assert kwargs["scan_mode"] == "bd_competitive_social"


@pytest.mark.asyncio
async def test_no_ungrounded_retry_fires():
    """An ungrounded response is NOT retried — the ungrounded-retry
    (PR #530) was removed: 0/13 recovery. Exactly one call; the honesty
    gate takes it from there."""
    mock = AsyncMock(return_value=_payload("{}", grounded=False))
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        _result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "boj", "k",
        )
    # exactly one call — no `_retry` follow-up
    assert mock.await_count == 1
    # ungrounded → honesty gate marks it (handle alone may still
    # surface; the point asserted here is no retry, not the gate token)
    assert reason in ("ungrounded", None)


# =========================================================================
# Part 2b — transport retry inside `_gemini_grounded_call`
# =========================================================================


def _http_response(payload: Dict[str, Any]):
    """A fake httpx.Response with status 200 + the given JSON payload."""
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    return _Resp()


class _RaisingClient:
    """httpx.AsyncClient stand-in. `.post()` raises `exc` for the first
    `fail_times` calls, then returns `ok_response`. Counts post calls so
    a test can assert how many attempts were made."""

    def __init__(self, exc, fail_times, ok_response=None):
        self._exc = exc
        self._fail_times = fail_times
        self._ok = ok_response
        self.post_calls = 0

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.post_calls += 1
        if self.post_calls <= self._fail_times:
            raise self._exc
        return self._ok


@pytest.mark.asyncio
async def test_transport_failure_retried_once_then_succeeds():
    """A lone ReadTimeout is retried — attempt 2 succeeds → payload
    returned, telemetry NOT marked failed."""
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _RaisingClient(
        httpx.ReadTimeout("slow"), fail_times=1,
        ok_response=_http_response(_payload("{}", grounded=True)),
    )
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture), \
         patch("services.bd_brand_signals.asyncio.sleep", AsyncMock()):
        result = await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_own_presence",
        )
    assert result is not None
    assert client.post_calls == 2  # retried once
    assert captured["status"] == "succeeded"


@pytest.mark.asyncio
async def test_transport_failure_both_attempts_gives_up():
    """ReadTimeout on both attempts → None, telemetry status=failed with
    a `transport:` error_message naming the exception class."""
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _RaisingClient(httpx.ReadTimeout("slow"), fail_times=2)
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture), \
         patch("services.bd_brand_signals.asyncio.sleep", AsyncMock()):
        result = await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_own_presence",
        )
    assert result is None
    assert client.post_calls == 2  # one retry, then gave up
    assert captured["status"] == "failed"
    assert captured["error_message"].startswith("transport:")
    assert "ReadTimeout" in captured["error_message"]


@pytest.mark.asyncio
async def test_non_retryable_request_error_not_retried():
    """A non-transient httpx.RequestError (e.g. UnsupportedProtocol) is
    NOT retried — only transport/timeout errors are."""
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _RaisingClient(
        httpx.UnsupportedProtocol("bad scheme"), fail_times=2,
    )
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture), \
         patch("services.bd_brand_signals.asyncio.sleep", AsyncMock()):
        result = await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_own_presence",
        )
    assert result is None
    assert client.post_calls == 1  # no retry
    assert captured["status"] == "failed"


@pytest.mark.asyncio
async def test_default_model_is_flash():
    """Callers that don't pass `model` (retail / founder / press) stay on
    flash — only the social probes opt into Pro."""
    captured_url: Dict[str, str] = {}

    class _UrlSpyClient(_RaisingClient):
        async def post(self, url, **kwargs):
            captured_url["url"] = url
            return await super().post(url, **kwargs)

    client = _UrlSpyClient(
        None, fail_times=0,
        ok_response=_http_response(_payload("{}", grounded=True)),
    )
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", AsyncMock()):
        await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_retail_presence",
        )
    assert _GEMINI_MODEL in captured_url["url"]
    assert _GEMINI_SOCIAL_MODEL not in captured_url["url"]


# =========================================================================
# Part 3 — ungrounded telemetry (kept — the feedback loop)
# =========================================================================


class _FakeClient:
    """httpx.AsyncClient stand-in returning a fixed response."""

    def __init__(self, response):
        self._response = response

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_telemetry_marks_ungrounded_response():
    """A 200 response with zero grounding chunks → telemetry records
    error_message='ungrounded' (status stays 'succeeded')."""
    captured: Dict[str, Any] = {}

    async def _capture_telemetry(**kwargs):
        captured.update(kwargs)

    fake_client = _FakeClient(_http_response(_payload("{}", grounded=False)))
    with patch("services.bd_brand_signals.httpx.AsyncClient", fake_client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture_telemetry):
        result = await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_own_presence",
        )
    assert result is not None  # the payload is still returned
    assert captured["status"] == "succeeded"
    assert captured["error_message"] == "ungrounded"


@pytest.mark.asyncio
async def test_telemetry_clean_on_grounded_response():
    """A 200 response WITH grounding chunks → telemetry records
    error_message=None (clean grounded success)."""
    captured: Dict[str, Any] = {}

    async def _capture_telemetry(**kwargs):
        captured.update(kwargs)

    fake_client = _FakeClient(_http_response(_payload("{}", grounded=True)))
    with patch("services.bd_brand_signals.httpx.AsyncClient", fake_client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture_telemetry):
        result = await _gemini_grounded_call(
            "prompt", api_key="k", scan_mode="bd_own_presence",
        )
    assert result is not None
    assert captured["status"] == "succeeded"
    assert captured["error_message"] is None
