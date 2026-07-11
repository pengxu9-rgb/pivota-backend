"""P2-5: Vertex grounding redirect URIs unwrap to real publisher URLs.

The redirector answers an unauthenticated 302 whose Location is the real
article (verified live 2026-07-11 on run 37237ccf's URIs). These tests mock
httpx at the client boundary; no network.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import services.grounding_redirect_resolver as R  # noqa: E402

VERTEX = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQFhq8"
)
REAL = "https://believeintherun.com/gear-reviews/mojawa-run-plus-headphones-review/"


class _FakeResponse:
    def __init__(self, status_code, location=None):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


class _FakeClient:
    """Stands in for httpx.AsyncClient; records requested URIs."""

    calls: list = []
    responder = staticmethod(lambda uri: _FakeResponse(302, REAL))

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, uri, follow_redirects=False):
        assert follow_redirects is False
        _FakeClient.calls.append(uri)
        return _FakeClient.responder(uri)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(R, "_ENABLED", True)
    R._RESOLVE_CACHE.clear()
    _FakeClient.calls = []
    _FakeClient.responder = staticmethod(lambda uri: _FakeResponse(302, REAL))
    yield


def test_is_vertex_redirect():
    assert R.is_vertex_redirect(VERTEX)
    assert not R.is_vertex_redirect("https://believeintherun.com/review/")
    assert not R.is_vertex_redirect(None)
    assert not R.is_vertex_redirect("not a url")


def test_rewrites_sources_and_chunks_in_place():
    runs = [{
        "grounding_sources": [
            {"uri": VERTEX, "title": "believeintherun.com"},
            {"uri": "https://mojawa.com/products/purra-run", "title": "mojawa.com"},
        ],
        "grounding_chunks": [VERTEX, "https://mojawa.com/products/purra-run"],
    }]
    n = asyncio.run(R.resolve_grounding_redirects_in_runs(runs))
    assert n == 2  # one source + one chunk
    assert runs[0]["grounding_sources"][0]["uri"] == REAL
    # non-vertex URLs untouched and never requested
    assert runs[0]["grounding_sources"][1]["uri"] == "https://mojawa.com/products/purra-run"
    assert runs[0]["grounding_chunks"] == [REAL, "https://mojawa.com/products/purra-run"]
    assert _FakeClient.calls == [VERTEX]


def test_resolution_is_cached_across_calls():
    runs = [{"grounding_sources": [{"uri": VERTEX, "title": "x"}]}]
    asyncio.run(R.resolve_grounding_redirects_in_runs(runs))
    runs2 = [{"grounding_sources": [{"uri": VERTEX, "title": "x"}]}]
    asyncio.run(R.resolve_grounding_redirects_in_runs(runs2))
    assert runs2[0]["grounding_sources"][0]["uri"] == REAL
    assert _FakeClient.calls == [VERTEX]  # one request total


def test_failure_leaves_uri_and_does_not_retry():
    _FakeClient.responder = staticmethod(lambda uri: _FakeResponse(200))
    runs = [{"grounding_sources": [{"uri": VERTEX, "title": "x"}]}]
    n = asyncio.run(R.resolve_grounding_redirects_in_runs(runs))
    assert n == 0
    assert runs[0]["grounding_sources"][0]["uri"] == VERTEX
    # cached as unresolvable — a second pass makes no new request
    asyncio.run(R.resolve_grounding_redirects_in_runs(runs))
    assert _FakeClient.calls == [VERTEX]


def test_non_http_location_rejected():
    _FakeClient.responder = staticmethod(
        lambda uri: _FakeResponse(302, "javascript:alert(1)")
    )
    runs = [{"grounding_sources": [{"uri": VERTEX, "title": "x"}]}]
    assert asyncio.run(R.resolve_grounding_redirects_in_runs(runs)) == 0
    assert runs[0]["grounding_sources"][0]["uri"] == VERTEX


def test_kill_switch(monkeypatch):
    monkeypatch.setattr(R, "_ENABLED", False)
    runs = [{"grounding_sources": [{"uri": VERTEX, "title": "x"}]}]
    assert asyncio.run(R.resolve_grounding_redirects_in_runs(runs)) == 0
    assert _FakeClient.calls == []
