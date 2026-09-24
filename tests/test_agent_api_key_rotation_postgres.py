"""Employee-portal API key reset against real Postgres, on the agents table the app's own model
builds (db.agents.agents, the shape create_all gives prod).

The fake-DB suite (test_agent_api_key_rotation.py) cannot see schema drift, which is exactly how
this broke: the handler wrote `last_key_rotation`, a column the real agents table never had, and
every reset 500'd with `column "last_key_rotation" of relation "agents" does not exist`. Here the
statements run for real, and the verdict is read through the real auth lookup
(db.agents.get_agent_by_key): the new key authenticates, the old one does not.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_key_rotation_test \\
        .venv/bin/python -m pytest tests/test_agent_api_key_rotation_postgres.py
"""

import hashlib
import os
import sys
from collections import OrderedDict

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
AGENT_ID = "agent_rotation_pg"
OTHER_AGENT_ID = "agent_rotation_pg_other"
OLD_KEY = "ak_live_" + "0" * 64
OTHER_KEY = "ak_live_" + "1" * 64
EMPLOYEE = {"role": "admin", "email": "ops@example.com"}

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
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _delete_own_rows(database):
    own_agents = {"a": AGENT_ID, "b": OTHER_AGENT_ID}
    for table in ("api_keys", "agent_api_keys"):
        exists = await database.fetch_one(f"SELECT to_regclass('public.{table}') AS t")
        if exists and dict(exists).get("t"):
            await database.execute(f"DELETE FROM {table} WHERE agent_id IN (:a, :b)", own_agents)
            await database.execute(
                f"DELETE FROM {table} WHERE key_hash IN (:x, :y)", {"x": _sha(OLD_KEY), "y": _sha(OTHER_KEY)}
            )
    await database.execute("DELETE FROM agents WHERE agent_id IN (:a, :b)", own_agents)


@pytest.fixture(autouse=True)
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

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Create-if-absent, never drop: the dialect gate shares ONE database across every
    # test_*_postgres.py file, and agents is the FK parent of tables sibling files build.
    await database.execute(str(CreateTable(agents_db.agents, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in agents_db.agents.indexes:  # unique=True/index=True columns: agent_id, api_key
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(_API_KEYS_DDL)
    await _delete_own_rows(database)

    for agent_id, key in ((AGENT_ID, OLD_KEY), (OTHER_AGENT_ID, OTHER_KEY)):
        await database.execute(
            """
            INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active)
            VALUES (:agent_id, :agent_name, 'custom', :api_key, :api_key_hash, TRUE)
            """,
            {
                "agent_id": agent_id,
                "agent_name": agent_id,
                "api_key": f"redacted:{agent_id}",
                "api_key_hash": _sha(key),
            },
        )
        await database.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
            VALUES (:agent_id, 'Primary Key', :key_hash, :key_prefix, 'active')
            """,
            {"agent_id": agent_id, "key_hash": _sha(key), "key_prefix": key[:10]},
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


@pytest.mark.asyncio
async def test_employee_reset_new_key_authenticates_and_old_key_does_not(db):
    from db.agents import get_agent_by_key
    from routes.employee_agent_mgmt import reset_agent_api_key

    # The old key works and is cached before the reset: eviction is part of what is under test.
    assert (await get_agent_by_key(OLD_KEY))["agent_id"] == AGENT_ID

    body = await reset_agent_api_key(AGENT_ID, current_user=EMPLOYEE)
    new_key = body["new_api_key"]

    assert (await get_agent_by_key(new_key))["agent_id"] == AGENT_ID
    assert await get_agent_by_key(OLD_KEY) is None

    row = await db.fetch_one(
        "SELECT api_key, api_key_hash FROM agents WHERE agent_id = :a", {"a": AGENT_ID}
    )
    assert row["api_key"] == f"redacted:{AGENT_ID}"
    assert row["api_key_hash"] == _sha(new_key)

    keys = await db.fetch_all(
        "SELECT key_hash, status FROM api_keys WHERE agent_id = :a ORDER BY id", {"a": AGENT_ID}
    )
    assert [(k["key_hash"], k["status"]) for k in keys] == [
        (_sha(OLD_KEY), "revoked"),
        (_sha(new_key), "active"),
    ]


@pytest.mark.asyncio
async def test_employee_reset_leaves_other_agents_keys_alone(db):
    from db.agents import get_agent_by_key
    from routes.employee_agent_mgmt import reset_agent_api_key

    await reset_agent_api_key(AGENT_ID, current_user=EMPLOYEE)

    assert (await get_agent_by_key(OTHER_KEY))["agent_id"] == OTHER_AGENT_ID


@pytest.mark.asyncio
async def test_a_failed_insert_rolls_back_the_revoke(db, monkeypatch):
    """The revoke and the insert commit together: a failure must not leave the agent keyless."""
    import routes.agent_account as agent_account
    from db.agents import get_agent_by_key
    from fastapi import HTTPException
    from routes.employee_agent_mgmt import reset_agent_api_key

    # Force the new key's hash to collide with the other agent's existing key: UNIQUE(key_hash)
    # rejects the INSERT, after the revoke UPDATE already ran inside the same transaction.
    monkeypatch.setattr(agent_account.secrets, "token_hex", lambda n: "1" * (2 * n))

    with pytest.raises(HTTPException) as exc:
        await reset_agent_api_key(AGENT_ID, current_user=EMPLOYEE)
    assert exc.value.status_code == 500

    assert (await get_agent_by_key(OLD_KEY))["agent_id"] == AGENT_ID
    row = await db.fetch_one("SELECT api_key_hash FROM agents WHERE agent_id = :a", {"a": AGENT_ID})
    assert row["api_key_hash"] == _sha(OLD_KEY)
