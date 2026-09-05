"""`since`/`until` on GET /merchant/analytics/commerce-funnel.

The window is resolved in the service; the route's job is to parse ISO-8601,
normalise to aware UTC, and refuse an unusable pair with 422 rather than
letting the service raise inside a request handler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch: pytest.MonkeyPatch, captured: Dict[str, Any]) -> TestClient:
    import routes.merchant_analytics_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_principal():
        return {"merchant_id": "merch_1", "role": "merchant"}

    async def fake_funnel(**kwargs):
        captured.update(kwargs)
        return {"merchant_id": "merch_1", "summary": {}, "slices": []}

    app.dependency_overrides[module._get_principal] = fake_principal
    monkeypatch.setattr(module, "get_merchant_commerce_funnel", fake_funnel)
    return TestClient(app)


def test_the_route_passes_aware_utc_bounds_to_the_wrapper(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel",
        params={"since": "2026-06-01T00:00:00Z", "until": "2026-06-30T23:59:59+00:00"},
    )

    assert response.status_code == 200
    assert captured["since"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert captured["until"] == datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)


def test_an_offset_bound_is_normalised_to_utc(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel",
        params={"since": "2026-06-01T09:00:00+09:00"},
    )

    assert response.status_code == 200
    assert captured["since"] == datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def test_a_date_only_bound_is_read_as_utc_midnight(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel", params={"since": "2026-06-01"}
    )

    assert response.status_code == 200
    assert captured["since"] == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_omitting_the_bounds_sends_none(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get("/merchant/analytics/commerce-funnel")

    assert response.status_code == 200
    assert captured["since"] is None
    assert captured["until"] is None


@pytest.mark.parametrize("param", ["since", "until"])
def test_an_unparseable_bound_is_422(monkeypatch, param):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel", params={param: "last-tuesday"}
    )

    assert response.status_code == 422
    assert param in response.json()["detail"]
    assert captured == {}, "the funnel must not be read for an unparseable bound"


def test_since_after_until_is_422(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel",
        params={"since": "2026-06-30T00:00:00Z", "until": "2026-06-01T00:00:00Z"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "since must not be after until"
    assert captured == {}, "the funnel must not be read for an inverted window"


def test_since_equal_to_until_is_accepted(monkeypatch):
    captured: Dict[str, Any] = {}
    response = _client(monkeypatch, captured).get(
        "/merchant/analytics/commerce-funnel",
        params={"since": "2026-06-01T00:00:00Z", "until": "2026-06-01T00:00:00Z"},
    )

    assert response.status_code == 200
    assert captured["since"] == captured["until"]
