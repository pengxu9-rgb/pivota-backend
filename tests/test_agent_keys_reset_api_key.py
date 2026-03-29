import re
from datetime import datetime

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

    async def fetch_all(self, query, values=None):
        return []


class _FakeApiKeysListDatabase(_FakeDatabase):
    def __init__(self, rows=None, legacy=None):
        super().__init__()
        self.rows = rows or []
        self.legacy = legacy

    async def fetch_all(self, query, values=None):
        return self.rows

    async def fetch_one(self, query, values=None):
        if "FROM agents" in str(query):
            return self.legacy
        return await super().fetch_one(query, values)


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
async def test_create_api_key_uses_explicit_environment_not_free_text_name(monkeypatch):
    fake_db = _FakeDatabase()
    now = datetime.utcnow()

    async def _fake_fetch_one(query, values=None):
        fake_db.fetch_one_calls.append((str(query), values or {}))
        return {"id": 9, "created_at": now}

    monkeypatch.setattr(agent_keys_module, "database", fake_db)
    monkeypatch.setattr(fake_db, "fetch_one", _fake_fetch_one)

    response = await agent_keys_module.create_agent_api_key(
        "agent_demo_1",
        request=agent_keys_module.CreateApiKeyRequest(name="staging key", environment="live"),
        current_user={"agent_id": "agent_demo_1", "role": "agent"},
    )

    assert response["environment"] == "live"
    assert response["masked"] is False
    assert re.fullmatch(r"ak_live_[0-9a-f]{64}", response["key"])

    insert_calls = [values for sql, values in fake_db.fetch_one_calls if "INSERT INTO api_keys" in sql]
    assert insert_calls
    assert insert_calls[0]["name"] == "staging key"
    assert insert_calls[0]["key_prefix"].startswith("ak_live_")


@pytest.mark.asyncio
async def test_get_agent_api_keys_returns_masked_environment_metadata(monkeypatch):
    fake_db = _FakeApiKeysListDatabase(
        rows=[
            {
                "id": "3",
                "name": "Primary Key (Rotated)",
                "key": "ak_live_45****",
                "created_at": datetime.utcnow(),
                "last_used": None,
                "status": "active",
                "usage_count": 0,
            },
            {
                "id": "2",
                "name": "staging key",
                "key": "ak_test_67****",
                "created_at": datetime.utcnow(),
                "last_used": None,
                "status": "revoked",
                "usage_count": 0,
            },
        ]
    )
    monkeypatch.setattr(agent_keys_module, "database", fake_db)

    response = await agent_keys_module.get_agent_api_keys(
        "agent_demo_1",
        current_user={"agent_id": "agent_demo_1", "role": "agent"},
    )

    assert response["status"] == "success"
    assert response["keys"][0]["environment"] == "live"
    assert response["keys"][0]["masked"] is True
    assert response["keys"][1]["environment"] == "test"
    assert response["keys"][1]["masked"] is True


@pytest.mark.asyncio
async def test_get_agent_api_keys_legacy_fallback_marks_preview_as_masked(monkeypatch):
    fake_db = _FakeApiKeysListDatabase(
        rows=[],
        legacy={
            "agent_id": "agent_demo_1",
            "agent_name": "Primary Key",
            "owner_email": "demo@example.com",
            "api_key": "ak_live_" + ("f" * 64),
            "api_key_hash": None,
            "created_at": datetime.utcnow(),
        },
    )
    monkeypatch.setattr(agent_keys_module, "database", fake_db)

    response = await agent_keys_module.get_agent_api_keys(
        "agent_demo_1",
        current_user={"agent_id": "agent_demo_1", "role": "agent"},
    )

    assert response["status"] == "success"
    assert response["keys"][0]["key"].endswith("****")
    assert response["keys"][0]["environment"] == "live"
    assert response["keys"][0]["masked"] is True


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
