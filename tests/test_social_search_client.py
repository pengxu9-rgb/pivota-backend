"""Tests for the SerpAPI social search client.

`services/social_search_client.py` is the deterministic retrieval layer
for the BD social-intel probes — it replaced Gemini's `google_search`
grounding (which a 2026-05-14 diagnostic proved returns 0 chunks 12/15
calls). Honesty contract: no key / any failure → NO results, never
fabricated ones; the caller's PR-9 honesty gate then nulls/suppresses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services import social_search_client as ssc


@pytest.fixture(autouse=True)
def _reset_search_state():
    """Drop the lazily-created semaphore so each async test gets a fresh
    one bound to its own event loop."""
    ssc._reset_search_state_for_test()
    yield
    ssc._reset_search_state_for_test()


def _resp(status_code: int = 200, body: Optional[Dict[str, Any]] = None,
          *, bad_json: bool = False):
    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            if bad_json:
                raise ValueError("not json")
            return body if body is not None else {}

    return _Resp()


class _FakeGetClient:
    """httpx.AsyncClient stand-in. `.get()` raises `exc` for the first
    `fail_times` calls, then returns `response`. Counts calls."""

    def __init__(self, response=None, exc=None, fail_times: int = 0):
        self._response = response
        self._exc = exc
        self._fail_times = fail_times
        self.get_calls = 0

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        self.get_calls += 1
        if self._exc is not None and self.get_calls <= self._fail_times:
            raise self._exc
        return self._response


def _organic(n: int) -> Dict[str, Any]:
    return {"organic_results": [
        {"title": f"Result {i}", "link": f"https://ex.com/{i}",
         "snippet": f"snippet {i}"}
        for i in range(n)
    ]}


# =========================================================================
# no-key / honesty
# =========================================================================


@pytest.mark.asyncio
async def test_no_api_key_search_web_returns_no_key():
    with patch.object(ssc, "_resolve_search_api_key", return_value=None):
        results, status = await ssc.search_web("beauty of joseon followers")
    assert results == []
    assert status == "no_key"


@pytest.mark.asyncio
async def test_no_api_key_search_web_many_emits_single_mock_fallback_row():
    captured: List[Dict[str, Any]] = []

    async def _capture(**kwargs):
        captured.append(kwargs)

    with patch.object(ssc, "_resolve_search_api_key", return_value=None), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web_many(["q1", "q2", "q3"])
    assert results == [] and status == "no_key"
    # ONE mock_fallback row, not one per query — honest no-op.
    assert len(captured) == 1
    assert captured[0]["status"] == "mock_fallback"


# =========================================================================
# search_web — parsing
# =========================================================================


@pytest.mark.asyncio
async def test_search_web_parses_and_caps_organic_results():
    client = _FakeGetClient(response=_resp(200, _organic(20)))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", AsyncMock()):
        results, status = await ssc.search_web("q")
    assert status == "ok"
    assert len(results) == ssc._SEARCH_RESULT_CAP            # capped
    assert results[0] == {"title": "Result 0", "url": "https://ex.com/0",
                          "snippet": "snippet 0"}


@pytest.mark.asyncio
async def test_answer_box_and_knowledge_graph_folded_in_first():
    body = {
        "answer_box": {"answer": "1.2M followers", "link": "https://x.com"},
        "knowledge_graph": {"title": "Beauty of Joseon", "type": "Brand"},
        "organic_results": [
            {"title": "Organic", "link": "https://ex.com/o", "snippet": "..."},
        ],
    }
    client = _FakeGetClient(response=_resp(200, body))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", AsyncMock()):
        results, status = await ssc.search_web("q")
    assert status == "ok"
    # answer_box + knowledge_graph come FIRST (where exact counts live).
    assert results[0]["title"] == "Google answer box"
    assert "1.2M followers" in results[0]["snippet"]
    assert results[1]["title"] == "Google knowledge graph"
    assert results[2]["title"] == "Organic"


@pytest.mark.asyncio
async def test_search_web_empty_results_recorded_as_empty_search():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _FakeGetClient(response=_resp(200, {"organic_results": []}))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web("q")
    assert results == [] and status == "empty"
    # 200 + 0 results → the replacement signal for the retired `ungrounded`.
    assert captured["status"] == "succeeded"
    assert captured["error_message"] == "empty_search"


@pytest.mark.asyncio
async def test_search_web_serpapi_error_body_is_transport():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _FakeGetClient(response=_resp(200, {"error": "Invalid API key"}))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web("q")
    assert results == [] and status == "transport_error"
    assert captured["status"] == "failed"
    assert "serpapi_error" in captured["error_message"]


# =========================================================================
# search_web — transport / HTTP errors
# =========================================================================


@pytest.mark.asyncio
async def test_search_web_transport_retried_once_then_succeeds():
    client = _FakeGetClient(
        response=_resp(200, _organic(2)),
        exc=httpx.ReadTimeout("slow"), fail_times=1,
    )
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch("services.social_search_client.asyncio.sleep", AsyncMock()), \
         patch.object(ssc, "_record_search_telemetry", AsyncMock()):
        results, status = await ssc.search_web("q")
    assert status == "ok" and len(results) == 2
    assert client.get_calls == 2                            # retried once


@pytest.mark.asyncio
async def test_search_web_transport_both_attempts_gives_up():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _FakeGetClient(exc=httpx.ReadTimeout("slow"), fail_times=2)
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch("services.social_search_client.asyncio.sleep", AsyncMock()), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web("q")
    assert results == [] and status == "transport_error"
    assert client.get_calls == 2
    assert captured["status"] == "failed"
    assert captured["error_message"].startswith("transport:")


@pytest.mark.asyncio
async def test_search_web_429_is_rate_limited():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _FakeGetClient(response=_resp(429, {}))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web("q")
    assert results == [] and status == "transport_error"
    assert captured["status"] == "rate_limited"


@pytest.mark.asyncio
async def test_search_web_500_is_failed():
    captured: Dict[str, Any] = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    client = _FakeGetClient(response=_resp(500, {}))
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch("services.social_search_client.httpx.AsyncClient", client), \
         patch.object(ssc, "_record_search_telemetry", _capture):
        results, status = await ssc.search_web("q")
    assert results == [] and status == "transport_error"
    assert captured["status"] == "failed"
    assert captured["error_message"] == "http_500"


# =========================================================================
# search_web_many — dedupe + status aggregation
# =========================================================================


@pytest.mark.asyncio
async def test_search_web_many_dedupes_by_url():
    async def _fake_search(query):
        return ([
            {"title": "A", "url": "https://dup.com", "snippet": "1"},
            {"title": "B", "url": f"https://uniq/{query}", "snippet": "2"},
        ], "ok")

    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch.object(ssc, "search_web", _fake_search):
        results, status = await ssc.search_web_many(["q1", "q2"])
    assert status == "ok"
    urls = [r["url"] for r in results]
    assert urls.count("https://dup.com") == 1               # deduped
    assert "https://uniq/q1" in urls and "https://uniq/q2" in urls


@pytest.mark.asyncio
async def test_search_web_many_all_empty_is_empty():
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch.object(ssc, "search_web", AsyncMock(return_value=([], "empty"))):
        results, status = await ssc.search_web_many(["q1", "q2"])
    assert results == [] and status == "empty"


@pytest.mark.asyncio
async def test_search_web_many_all_transport_is_transport():
    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch.object(ssc, "search_web",
                      AsyncMock(return_value=([], "transport_error"))):
        results, status = await ssc.search_web_many(["q1", "q2"])
    assert results == [] and status == "transport_error"


@pytest.mark.asyncio
async def test_search_web_many_zero_results_with_any_transport_is_transport():
    """0 combined results + ANY query transport-failed → transport_error
    (retry-worthy), not 'empty' — guards a partial-outage from silently
    looking like 'data not found'."""
    statuses = iter([([], "empty"), ([], "transport_error")])

    async def _fake_search(query):
        return next(statuses)

    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch.object(ssc, "search_web", _fake_search):
        results, status = await ssc.search_web_many(["q1", "q2"])
    assert results == [] and status == "transport_error"


@pytest.mark.asyncio
async def test_search_web_many_any_results_is_ok():
    statuses = iter([
        ([{"title": "T", "url": "https://x", "snippet": "s"}], "ok"),
        ([], "transport_error"),
    ])

    async def _fake_search(query):
        return next(statuses)

    with patch.object(ssc, "_resolve_search_api_key", return_value="key"), \
         patch.object(ssc, "search_web", _fake_search):
        results, status = await ssc.search_web_many(["q1", "q2"])
    assert status == "ok" and len(results) == 1
