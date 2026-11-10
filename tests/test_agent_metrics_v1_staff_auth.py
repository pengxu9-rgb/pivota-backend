"""/agent/v1/metrics/summary and /agent/v1/metrics/timeline are staff-only (FIX_PLAN S-09).

Both answered anonymous callers with platform-wide numbers: every agent's request counts, the
active-agent count and 24 h order revenue. They now take the internal-usage gate
(utils.auth.require_admin_or_key): X-ADMIN-KEY or an admin JWT. Anonymous -> 401; a valid
non-admin token -> 403; an agent API key alone -> 401. A refused request never reaches the
handler body (zero database calls). Driven through main.app, so the mounted route is tested.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

ROUTES = ("/agent/v1/metrics/summary", "/agent/v1/metrics/timeline")
_FAKE_ADMIN_KEY = "test-admin-key-not-a-secret"


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str) -> str:
    from utils.auth import create_access_token

    return create_access_token({"sub": "u1", "email": "u1@example.com", "role": role})


class _DatabaseSpy:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def fetch_val(self, query: str, *a: Any, **kw: Any) -> int:
        self.calls.append("fetch_val")
        return 3

    async def fetch_all(self, query: str, *a: Any, **kw: Any) -> list:
        self.calls.append("fetch_all")
        if "DATE_TRUNC" in query:
            return [{"hour": datetime(2026, 11, 1, 3), "total_requests": 3,
                     "successful_requests": 3, "avg_response_time": 12.0}]
        return []


@pytest.fixture
def db(monkeypatch) -> _DatabaseSpy:
    import routes.agent_metrics_v1 as module

    spy = _DatabaseSpy()
    monkeypatch.setattr(module, "database", spy)
    monkeypatch.setenv("ADMIN_API_KEY", _FAKE_ADMIN_KEY)
    return spy


@pytest.mark.parametrize("path", ROUTES)
def test_anonymous_is_401_and_reads_nothing(client, db, path):
    resp = client.get(path)
    assert resp.status_code == 401, resp.text
    assert db.calls == []
    assert "revenue" not in resp.text


@pytest.mark.parametrize("path", ROUTES)
def test_agent_api_key_alone_is_401(client, db, path):
    resp = client.get(path, headers={"x-api-key": "ak_live_" + "0" * 64})
    assert resp.status_code == 401, resp.text
    assert db.calls == []


@pytest.mark.parametrize("path", ROUTES)
def test_wrong_admin_key_is_401(client, db, path):
    resp = client.get(path, headers={"X-ADMIN-KEY": "not-the-key"})
    assert resp.status_code == 401, resp.text
    assert db.calls == []


@pytest.mark.parametrize("role", ["merchant", "agent", "employee"])
@pytest.mark.parametrize("path", ROUTES)
def test_non_admin_token_is_403(client, db, path, role):
    resp = client.get(path, headers={"Authorization": f"Bearer {_token(role)}"})
    assert resp.status_code == 403, resp.text
    assert db.calls == []


@pytest.mark.parametrize("path", ROUTES)
def test_admin_key_is_served(client, db, path):
    resp = client.get(path, headers={"X-ADMIN-KEY": _FAKE_ADMIN_KEY})
    assert resp.status_code == 200, resp.text
    assert db.calls
    body = resp.json()
    assert "error" not in body, body


@pytest.mark.parametrize("path", ROUTES)
def test_admin_jwt_is_served(client, db, path):
    resp = client.get(path, headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 200, resp.text
    assert db.calls


def test_summary_served_shape_is_unchanged(client, db):
    body = client.get(ROUTES[0], headers={"X-ADMIN-KEY": _FAKE_ADMIN_KEY}).json()
    assert body["agents"]["active_last_24h"] == 3
    assert body["orders"]["revenue_last_24h"] == 3.0
    assert set(body) == {"status", "timestamp", "overview", "performance", "agents", "orders",
                         "top_endpoints"}


def test_recent_keeps_its_own_gate(client, db):
    """/recent was already scoped by resolve_recent_activity_scope; this change leaves it alone."""
    resp = client.get("/agent/v1/metrics/recent")
    assert resp.status_code == 401
