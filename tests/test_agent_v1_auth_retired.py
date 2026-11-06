"""POST /agent/v1/auth is retired: 501 for any payload, a fixed body, and no database access.

Drives the real router on a minimal app (never `main`, never TestClient).
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _NoDatabase:
    """Stands in for db.database.database: any call is a test failure, not a 501."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"POST /agent/v1/auth reached database.{name}")

        return _call


@pytest.fixture
def no_db(monkeypatch):
    import routes.agent_sdk_fixed as mod

    spy = _NoDatabase()
    monkeypatch.setattr(mod, "database", spy)
    return spy


def _app():
    from fastapi import FastAPI

    from routes.agent_sdk_fixed import router

    app = FastAPI()
    app.include_router(router)
    return app


async def _post(**kwargs):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/agent/v1/auth", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        # The old request shape: a known or unknown agent email.
        {"json": {"agent_name": "x", "agent_email": "victim@example.com"}},
        {"json": {"agent_name": "x", "agent_email": "new@example.com", "company": "c", "description": "d"}},
        # Anything else is 501 too, never 422.
        {"json": {}},
        {"content": b"not json", "headers": {"content-type": "application/json"}},
        {},
    ],
)
async def test_auth_answers_501_and_never_reads_or_writes(no_db, kwargs):
    resp = await _post(**kwargs)
    assert resp.status_code == 501, resp.text
    assert resp.json() == {
        "detail": "Agent key issuance here is retired; register through POST /agent/account/register"
    }
    assert no_db.calls == []


def test_the_retired_route_is_not_in_the_openapi_schema():
    assert "/agent/v1/auth" not in _app().openapi()["paths"]
