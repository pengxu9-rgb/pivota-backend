"""Social-intel grounding stability — prompt directives, ungrounded
retry, and ungrounded telemetry.

## The problem

The social sub-calls run through `_gemini_grounded_call` with the
`google_search` tool enabled — but the tool is model-discretionary.
When a prompt says "estimate X's reach", Gemini often answers from
internal knowledge WITHOUT searching; the response carries zero
grounding chunks; PR-9's honesty gate (correctly) nulls every
figure; the merchant sees an empty social section. Lots of empty
sections → churn risk.

## The three-part fix this file covers

1. Prompt directives — every social prompt now demands a web search;
   a harder `escalated` variant exists for the retry.
2. `_grounded_call_with_retry` — an ungrounded attempt-1 response
   triggers one retry with the escalated prompt.
3. Telemetry — `_gemini_grounded_call` records `error_message="ungrounded"`
   on a 200-but-zero-grounding-chunks response, so the ungrounded
   rate is measurable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.bd_brand_signals import (
    _GROUNDING_DIRECTIVE,
    _GROUNDING_DIRECTIVE_ESCALATED,
    _build_competitive_prompt,
    _build_kol_prompt,
    _build_own_presence_prompt,
    _gemini_grounded_call,
    _grounded_call_with_retry,
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


def test_base_prompts_carry_the_search_directive():
    """Every social prompt — base variant — carries the grounding
    directive that tells Gemini it MUST search first."""
    own = _build_own_presence_prompt("Beauty of Joseon", "tiktok", "boj")
    kol = _build_kol_prompt("Beauty of Joseon", "instagram")
    comp = _build_competitive_prompt("Beauty of Joseon", ["Drunk Elephant"])
    for prompt in (own, kol, comp):
        assert _GROUNDING_DIRECTIVE in prompt
        assert "MUST use web search" in prompt


def test_escalated_prompts_carry_the_escalated_directive():
    """The escalated variant swaps in the harder directive — used by
    the retry after attempt 1 came back un-grounded."""
    own = _build_own_presence_prompt(
        "Beauty of Joseon", "tiktok", "boj", escalated=True,
    )
    kol = _build_kol_prompt("Beauty of Joseon", "instagram", escalated=True)
    comp = _build_competitive_prompt(
        "Beauty of Joseon", ["Drunk Elephant"], escalated=True,
    )
    for prompt in (own, kol, comp):
        assert _GROUNDING_DIRECTIVE_ESCALATED in prompt
        assert _GROUNDING_DIRECTIVE not in prompt  # swapped, not appended
        assert "previous attempt answered without searching" in prompt


def test_escalated_directive_is_strictly_more_forceful():
    """Sanity: the escalated directive isn't just a rename — it
    explicitly references the rejected prior attempt."""
    assert "rejected" in _GROUNDING_DIRECTIVE_ESCALATED
    assert "rejected" not in _GROUNDING_DIRECTIVE


# =========================================================================
# Part 2 — _grounded_call_with_retry
# =========================================================================


@pytest.mark.asyncio
async def test_grounded_on_attempt_1_no_retry():
    """A grounded attempt-1 response → return it, NO retry call."""
    mock = AsyncMock(return_value=_payload("{}", grounded=True))
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        result = await _grounded_call_with_retry(
            base_prompt="base", escalated_prompt="escalated",
            api_key="k", scan_mode="bd_own_presence",
        )
    assert result is not None
    assert mock.await_count == 1
    # attempt 1 used the base prompt + the base scan_mode
    args, kwargs = mock.await_args_list[0]
    assert args[0] == "base"
    assert kwargs["scan_mode"] == "bd_own_presence"


@pytest.mark.asyncio
async def test_ungrounded_attempt_1_retries_and_uses_grounded_retry():
    """Ungrounded attempt 1 → retry with the escalated prompt → retry
    grounds → the retry payload is returned."""
    grounded_retry = _payload('{"follower_estimate": 5}', grounded=True)
    mock = AsyncMock(side_effect=[
        _payload("{}", grounded=False),  # attempt 1: ungrounded
        grounded_retry,                   # attempt 2: grounded
    ])
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        result = await _grounded_call_with_retry(
            base_prompt="base", escalated_prompt="escalated",
            api_key="k", scan_mode="bd_own_presence",
        )
    assert result is grounded_retry
    assert mock.await_count == 2
    # retry used the escalated prompt + the _retry scan_mode suffix
    retry_args, retry_kwargs = mock.await_args_list[1]
    assert retry_args[0] == "escalated"
    assert retry_kwargs["scan_mode"] == "bd_own_presence_retry"


@pytest.mark.asyncio
async def test_ungrounded_both_attempts_returns_last_payload():
    """Ungrounded on both attempts → return the retry payload anyway
    (non-None) so the caller's PR-9 honesty gate nulls it
    consistently — the gate, not this wrapper, decides suppression."""
    retry_payload = _payload("{}", grounded=False)
    mock = AsyncMock(side_effect=[
        _payload("{}", grounded=False),  # attempt 1
        retry_payload,                    # attempt 2 — still ungrounded
    ])
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        result = await _grounded_call_with_retry(
            base_prompt="base", escalated_prompt="escalated",
            api_key="k", scan_mode="bd_kol_endorsements",
        )
    assert result is retry_payload
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_transport_failure_on_attempt_1_does_not_retry():
    """Attempt 1 returns None (transport/HTTP failure already logged
    by _gemini_grounded_call) → return None, NO retry. No point
    retrying a dead endpoint."""
    mock = AsyncMock(return_value=None)
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        result = await _grounded_call_with_retry(
            base_prompt="base", escalated_prompt="escalated",
            api_key="k", scan_mode="bd_competitive_social",
        )
    assert result is None
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_retry_transport_failure_falls_back_to_attempt_1_payload():
    """Attempt 1 ungrounded, retry transport-fails (None) → return the
    attempt-1 payload (better an ungrounded payload the honesty gate
    can null than nothing)."""
    attempt_1 = _payload("{}", grounded=False)
    mock = AsyncMock(side_effect=[attempt_1, None])
    with patch("services.bd_brand_signals._gemini_grounded_call", mock):
        result = await _grounded_call_with_retry(
            base_prompt="base", escalated_prompt="escalated",
            api_key="k", scan_mode="bd_own_presence",
        )
    assert result is attempt_1
    assert mock.await_count == 2


# =========================================================================
# Part 3 — ungrounded telemetry
# =========================================================================


def _http_response(payload: Dict[str, Any]):
    """A fake httpx.Response with status 200 + the given JSON payload."""
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    return _Resp()


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
