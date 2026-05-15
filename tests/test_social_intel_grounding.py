"""Social-intel grounding — search-then-extract pipeline.

## The problem

The 3 social sub-calls (own presence / KOL / competitive) used to call
Gemini with the `google_search` grounding tool. A 2026-05-14 prod
diagnostic proved the model issues good search queries every call, but
Gemini's grounding *retrieval* returns 0 chunks for 12/15 social calls.
Three prior attempts (#530 prompt escalation, #531 gemini-2.5-pro, #533
gemini-3-flash-preview) all failed — the model was never the bottleneck.

## The fix this file covers

The social probes now run **search-then-extract**: deterministic SerpAPI
search (`services.social_search_client`) → Gemini *extraction* call
(`_gemini_extract_call`, no `google_search` tool). Grounding is now
deterministic: "did the search return results?" `_search_then_extract`
returns `(payload, search_status)` where status ∈ {ok, empty,
transport_error} — so the honesty gate distinguishes "search found
nothing" (`_FR_UNGROUNDED`) from "the call failed" (`_FR_TRANSPORT`).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.bd_brand_signals import (
    _EXTRACTION_DIRECTIVE,
    _FR_TRANSPORT,
    _FR_UNGROUNDED,
    _SEARCH_EMPTY,
    _SEARCH_OK,
    _SEARCH_TRANSPORT,
    _build_competitive_prompt,
    _build_kol_prompt,
    _build_own_presence_prompt,
    _competitive_queries,
    _gemini_extract_call,
    _infer_competitive_social,
    _infer_kol_endorsements,
    _infer_own_presence,
    _kol_queries,
    _own_presence_queries,
    _search_then_extract,
)


def _payload(text: str) -> Dict[str, Any]:
    """Gemini-shaped extraction-call response with `text` as the body."""
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


_RESULTS = [
    {"title": "Beauty of Joseon (@beautyofjoseon_official) TikTok",
     "url": "https://www.tiktok.com/@beautyofjoseon_official",
     "snippet": "1.2M followers · skincare brand"},
]


# =========================================================================
# Part 1 — extraction prompts + query builders
# =========================================================================


def test_extraction_prompts_embed_search_results():
    """Every social prompt embeds the real search-result snippets and
    carries the EXTRACTION directive."""
    own = _build_own_presence_prompt("Beauty of Joseon", "tiktok", "boj", _RESULTS)
    kol = _build_kol_prompt("Beauty of Joseon", "instagram", _RESULTS)
    comp = _build_competitive_prompt("Beauty of Joseon", ["Drunk Elephant"], _RESULTS)
    for prompt in (own, kol, comp):
        assert _EXTRACTION_DIRECTIVE in prompt
        assert "SEARCH RESULTS:" in prompt
        assert "beautyofjoseon_official" in prompt       # the snippet is embedded


def test_query_builders_are_bounded_and_templated():
    """The query builders are deterministic + bounded — no LLM call for
    query generation, and the per-probe fan-out is capped."""
    own = _own_presence_queries("Beauty of Joseon", "tiktok", "boj")
    kol = _kol_queries("Beauty of Joseon", "instagram")
    comp = _competitive_queries("Beauty of Joseon", ["A", "B", "C", "D", "E", "F"])
    assert 1 <= len(own) <= 3 and "Beauty of Joseon" in own[0]
    assert 1 <= len(kol) <= 3 and all("Beauty of Joseon" in q for q in kol)
    assert len(comp) == 4                                # capped at 4 competitors
    # own_presence drops the handle query when no handle is known
    assert len(_own_presence_queries("X", "tiktok", None)) == 2


# =========================================================================
# Part 2a — the 3 social probes route through `_search_then_extract`
# =========================================================================


@pytest.mark.asyncio
async def test_own_presence_routes_through_search_then_extract():
    """`_infer_own_presence` calls `_search_then_extract` once with its
    scan_mode + a non-empty query list + a callable extract_prompt_fn."""
    mock = AsyncMock(return_value=(
        _payload('{"follower_estimate": 5, "follower_band": "<10k"}'), _SEARCH_OK,
    ))
    with patch("services.bd_brand_signals._search_then_extract", mock):
        out, reason = await _infer_own_presence("Beauty of Joseon", "tiktok", "boj", "k")
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["scan_mode"] == "bd_own_presence"
    assert kwargs["queries"] and isinstance(kwargs["queries"], list)
    assert callable(kwargs["extract_prompt_fn"])
    assert reason is None and out is not None


@pytest.mark.asyncio
async def test_kol_endorsements_routes_through_search_then_extract():
    mock = AsyncMock(return_value=(
        _payload('[{"creator_handle": "@x", "follower_band": "10k-100k"}]'),
        _SEARCH_OK,
    ))
    with patch("services.bd_brand_signals._search_then_extract", mock):
        await _infer_kol_endorsements("Beauty of Joseon", "instagram", "k")
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["scan_mode"] == "bd_kol_endorsements"
    assert kwargs["queries"]


@pytest.mark.asyncio
async def test_competitive_social_routes_through_search_then_extract():
    mock = AsyncMock(return_value=(
        _payload('[{"brand": "Drunk Elephant", "gap_summary": "ahead"}]'),
        _SEARCH_OK,
    ))
    with patch("services.bd_brand_signals._search_then_extract", mock):
        await _infer_competitive_social("Beauty of Joseon", ["Drunk Elephant"], "k")
    assert mock.await_count == 1
    _, kwargs = mock.await_args
    assert kwargs["scan_mode"] == "bd_competitive_social"
    assert kwargs["queries"]


@pytest.mark.asyncio
async def test_infer_transport_vs_empty_are_distinct():
    """The 3-state search status maps correctly: transport_error →
    `_FR_TRANSPORT` (retry-worthy), empty → `_FR_UNGROUNDED` (kol/comp
    suppress wholesale). Guards against a search outage silently
    degrading to 'nothing found'."""
    # kol — transport vs empty
    with patch("services.bd_brand_signals._search_then_extract",
               AsyncMock(return_value=(None, _SEARCH_TRANSPORT))):
        assert (await _infer_kol_endorsements("B", "tiktok", "k")) == (None, _FR_TRANSPORT)
    with patch("services.bd_brand_signals._search_then_extract",
               AsyncMock(return_value=(None, _SEARCH_EMPTY))):
        assert (await _infer_kol_endorsements("B", "tiktok", "k")) == (None, _FR_UNGROUNDED)
    # competitive — same mapping
    with patch("services.bd_brand_signals._search_then_extract",
               AsyncMock(return_value=(None, _SEARCH_TRANSPORT))):
        assert (await _infer_competitive_social("B", ["C"], "k")) == (None, _FR_TRANSPORT)
    with patch("services.bd_brand_signals._search_then_extract",
               AsyncMock(return_value=(None, _SEARCH_EMPTY))):
        assert (await _infer_competitive_social("B", ["C"], "k")) == (None, _FR_UNGROUNDED)


@pytest.mark.asyncio
async def test_own_presence_empty_search_nulls_metrics_but_surfaces_handle():
    """own_presence on empty search: no extraction runs, every metric is
    nulled, but a caller-supplied handle still surfaces — identical
    outward behavior to the old '0 grounding chunks' path."""
    with patch("services.bd_brand_signals._search_then_extract",
               AsyncMock(return_value=(None, _SEARCH_EMPTY))):
        out, reason = await _infer_own_presence("Beauty of Joseon", "tiktok", "boj", "k")
    assert reason is None and out is not None            # surfaced, not suppressed
    assert out["handle"] == "boj"
    assert out["grounding"] == "ungrounded"
    assert out["follower_estimate"] is None and out["follower_band"] is None


# =========================================================================
# Part 2b — `_search_then_extract` helper
# =========================================================================


@pytest.mark.asyncio
async def test_search_then_extract_transport_short_circuits():
    """search transport failure → (None, transport_error), extraction NOT called."""
    extract = AsyncMock()
    with patch("services.social_search_client.search_web_many",
               AsyncMock(return_value=([], "transport_error"))), \
         patch("services.bd_brand_signals._gemini_extract_call", extract):
        result = await _search_then_extract(
            queries=["q"], extract_prompt_fn=lambda r: "p",
            scan_mode="bd_own_presence", api_key="k",
        )
    assert result == (None, _SEARCH_TRANSPORT)
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_then_extract_empty_and_no_key_both_map_to_empty():
    """search empty OR no_key → (None, empty); extraction NOT called."""
    for status in ("empty", "no_key"):
        extract = AsyncMock()
        with patch("services.social_search_client.search_web_many",
                   AsyncMock(return_value=([], status))), \
             patch("services.bd_brand_signals._gemini_extract_call", extract):
            result = await _search_then_extract(
                queries=["q"], extract_prompt_fn=lambda r: "p",
                scan_mode="bd_own_presence", api_key="k",
            )
        assert result == (None, _SEARCH_EMPTY)
        extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_then_extract_ok_runs_extraction():
    """search ok → extraction runs, prompt fn gets the results, payload returned."""
    seen: Dict[str, Any] = {}
    payload = _payload("{}")

    def _prompt_fn(results):
        seen["results"] = results
        return "extract this"

    with patch("services.social_search_client.search_web_many",
               AsyncMock(return_value=(_RESULTS, "ok"))), \
         patch("services.bd_brand_signals._gemini_extract_call",
               AsyncMock(return_value=payload)) as extract:
        result = await _search_then_extract(
            queries=["q"], extract_prompt_fn=_prompt_fn,
            scan_mode="bd_own_presence", api_key="k",
        )
    assert result == (payload, _SEARCH_OK)
    assert seen["results"] == _RESULTS
    _, kwargs = extract.await_args
    assert kwargs["scan_mode"] == "bd_own_presence"


@pytest.mark.asyncio
async def test_search_then_extract_extraction_failure_is_transport():
    """search ok but the extraction call returns None → (None, transport_error)."""
    with patch("services.social_search_client.search_web_many",
               AsyncMock(return_value=(_RESULTS, "ok"))), \
         patch("services.bd_brand_signals._gemini_extract_call",
               AsyncMock(return_value=None)):
        result = await _search_then_extract(
            queries=["q"], extract_prompt_fn=lambda r: "p",
            scan_mode="bd_own_presence", api_key="k",
        )
    assert result == (None, _SEARCH_TRANSPORT)


# =========================================================================
# Part 2c — `_gemini_extract_call` (the extraction step)
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
    `fail_times` calls, then returns `ok_response`."""

    def __init__(self, exc, fail_times, ok_response=None):
        self._exc = exc
        self._fail_times = fail_times
        self._ok = ok_response
        self.post_calls = 0
        self.last_json = None

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.post_calls += 1
        self.last_json = kwargs.get("json")
        if self.post_calls <= self._fail_times:
            raise self._exc
        return self._ok


@pytest.mark.asyncio
async def test_extract_call_omits_google_search_tool_and_lifts_token_budget():
    """`_gemini_extract_call` sends NO `tools` key (no google_search) and
    a 4096 output-token budget (the MAX_TOKENS-bug fix)."""
    client = _RaisingClient(
        None, fail_times=0, ok_response=_http_response(_payload("{}")),
    )
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", AsyncMock()):
        await _gemini_extract_call("prompt", api_key="k", scan_mode="bd_own_presence")
    assert "tools" not in client.last_json
    assert client.last_json["generationConfig"]["maxOutputTokens"] == 4096


@pytest.mark.asyncio
async def test_extract_call_retries_transport_then_succeeds():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _RaisingClient(
        httpx.ReadTimeout("slow"), fail_times=1,
        ok_response=_http_response(_payload("{}")),
    )
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture), \
         patch("services.bd_brand_signals.asyncio.sleep", AsyncMock()):
        result = await _gemini_extract_call("p", api_key="k", scan_mode="bd_own_presence")
    assert result is not None
    assert client.post_calls == 2
    assert captured["status"] == "succeeded"
    # the extraction call NEVER records "ungrounded" — groundedness is
    # decided upstream by the search step.
    assert captured["error_message"] is None


@pytest.mark.asyncio
async def test_extract_call_transport_both_attempts_gives_up():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _RaisingClient(httpx.ReadTimeout("slow"), fail_times=2)
    with patch("services.bd_brand_signals.httpx.AsyncClient", client), \
         patch("services.bd_brand_signals._record_bd_telemetry", _capture), \
         patch("services.bd_brand_signals.asyncio.sleep", AsyncMock()):
        result = await _gemini_extract_call("p", api_key="k", scan_mode="bd_own_presence")
    assert result is None
    assert client.post_calls == 2
    assert captured["status"] == "failed"
    assert captured["error_message"].startswith("transport:")


