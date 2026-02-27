import re

import pytest
from fastapi import FastAPI, HTTPException

from routes import agent_management as agent_management_module
from routes import agent_keys as agent_keys_module


class _FakeDatabase:
    def __init__(self):
        self.execute_calls = []
        self.fetch_one_calls = []

    async def execute(self, query, values=None):
        self.execute_calls.append((str(query), values or {}))
        return "UPDATE 1"

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls.append((str(query), values or {}))
        # Simulate api_keys table present.
        return {
            "api_keys_table": "api_keys",
            "agent_api_keys_table": None,
        }


@pytest.mark.asyncio
async def test_reset_api_key_generates_ak_live_64hex_and_syncs_api_keys_table(monkeypatch):
    fake_db = _FakeDatabase()
    monkeypatch.setattr(agent_keys_module, "database", fake_db)

    response = await agent_keys_module.reset_agent_api_key(
        "agent_demo_1",
        current_user={"agent_id": "agent_demo_1", "role": "agent"},
    )

    new_key = str(response.get("new_api_key") or "")
    assert re.fullmatch(r"ak_live_[0-9a-f]{64}", new_key)
    assert response["api_key"] == new_key
    assert response["key_sync_source"] == "api_keys"

    executed_sql = "\n".join(sql for sql, _ in fake_db.execute_calls)
    assert "UPDATE agents" in executed_sql
    assert "UPDATE api_keys" in executed_sql
    assert "INSERT INTO api_keys" in executed_sql


@pytest.mark.asyncio
async def test_reset_api_key_rejects_other_agent(monkeypatch):
    fake_db = _FakeDatabase()
    monkeypatch.setattr(agent_keys_module, "database", fake_db)

    with pytest.raises(HTTPException) as exc:
        await agent_keys_module.reset_agent_api_key(
            "agent_owner",
            current_user={"agent_id": "another_agent", "role": "agent"},
        )

    assert exc.value.status_code == 403


def test_reset_api_key_route_is_unique_and_from_agent_keys_router():
    app = FastAPI()
    app.include_router(agent_management_module.router)
    app.include_router(agent_keys_module.router)

    routes = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/agents/{agent_id}/reset-api-key"
        and "POST" in getattr(route, "methods", set())
    ]

    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == agent_keys_module.__name__
