"""Against real Postgres: a key admitted by db.agents.get_agent_by_key gets api_keys.last_used, and
the developer portal's key list (GET /agents/{agent_id}/api-keys) then reports it instead of None,
which the portal rendered as "Never" for every key.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_x_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_agent_key_last_used_postgres.py
"""

import hashlib
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
AGENT_ID = "agent_key_last_used_pg"
API_KEY = "ak_live_" + "8" * 64
STAFF = {"role": "admin", "email": "ops@example.com"}

# Same DDL routes/agent_keys.py ensures before it touches api_keys.
_API_KEYS_DDL = """
    CREATE TABLE IF NOT EXISTS api_keys (
        id SERIAL PRIMARY KEY,
        agent_id VARCHAR(255) NOT NULL,
        name VARCHAR(255) NOT NULL,
        key_hash VARCHAR(255) NOT NULL,
        key_prefix VARCHAR(20) NOT NULL,
        status VARCHAR(50) DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_used TIMESTAMP,
        usage_count INTEGER DEFAULT 0,
        UNIQUE(key_hash)
    )
"""


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")


async def _delete_own_rows(database):
    await database.execute("DELETE FROM api_keys WHERE agent_id = :a", {"a": AGENT_ID})
    await database.execute("DELETE FROM api_keys WHERE key_hash = :h", {"h": _sha(API_KEY)})
    await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT_ID})


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.database import database

    _assert_throwaway_database()
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_CACHE", OrderedDict())
    monkeypatch.setattr(agents_db, "_KEY_LAST_USED_MEMO", OrderedDict())
    monkeypatch.setattr(agents_db, "is_synthetic_call", lambda: False)

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Create-if-absent, never drop: the dialect gate shares one database across test files.
    await database.execute(str(CreateTable(agents_db.agents, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in agents_db.agents.indexes:
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(_API_KEYS_DDL)
    await _delete_own_rows(database)
    await database.execute(
        """
        INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active)
        VALUES (:agent_id, 'Key Last Used PG', 'custom', :api_key, :api_key_hash, TRUE)
        """,
        {"agent_id": AGENT_ID, "api_key": f"redacted:{AGENT_ID}", "api_key_hash": _sha(API_KEY)},
    )
    await database.execute(
        "INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status) VALUES (:a, 'Primary', :h, :p, 'active')",
        {"a": AGENT_ID, "h": _sha(API_KEY), "p": API_KEY[:10]},
    )
    try:
        yield database
    finally:
        try:
            await _delete_own_rows(database)
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _last_used(database):
    row = await database.fetch_one("SELECT last_used FROM api_keys WHERE key_hash = :h", {"h": _sha(API_KEY)})
    return row["last_used"]


@pytest.mark.asyncio
async def test_an_admitted_key_is_stamped_and_the_key_list_reports_it(db):
    import httpx
    from fastapi import FastAPI

    import db.agents as agents_db
    from routes.agent_keys import router
    from utils.auth import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: STAFF
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = (await client.get(f"/agents/{AGENT_ID}/api-keys")).json()
        assert [k["last_used"] for k in before["keys"]] == [None], before  # the control: unstamped

        assert await _last_used(db) is None
        agent = await agents_db.get_agent_by_key(API_KEY)
        assert agent and agent["agent_id"] == AGENT_ID
        stamped = await _last_used(db)
        assert stamped is not None and abs((datetime.utcnow() - stamped).total_seconds()) < 60

        after = (await client.get(f"/agents/{AGENT_ID}/api-keys")).json()
        assert [k["last_used"] for k in after["keys"]] == ["Just now"], after


@pytest.mark.asyncio
async def test_a_row_stamped_inside_the_window_by_another_instance_is_left_alone(db):
    import db.agents as agents_db

    recent = datetime.utcnow() - timedelta(seconds=30)
    await db.execute("UPDATE api_keys SET last_used = :t WHERE key_hash = :h", {"t": recent, "h": _sha(API_KEY)})

    assert await agents_db.get_agent_by_key(API_KEY)

    assert await _last_used(db) == recent  # this process's memo was empty: the SQL guard skipped it

    stale = datetime.utcnow() - timedelta(seconds=agents_db._KEY_LAST_USED_STAMP_SECONDS + 60)
    await db.execute("UPDATE api_keys SET last_used = :t WHERE key_hash = :h", {"t": stale, "h": _sha(API_KEY)})
    agents_db._AGENT_AUTH_CACHE.clear()
    agents_db._KEY_LAST_USED_MEMO.clear()

    assert await agents_db.get_agent_by_key(API_KEY)

    assert await _last_used(db) > stale
