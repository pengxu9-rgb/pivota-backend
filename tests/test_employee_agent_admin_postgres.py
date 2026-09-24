"""Employee-portal agent admin endpoints against real Postgres, on the agents table the app's own
model builds (db.agents.agents, the shape create_all gives prod), with the verdict read through the
real agent auth door (db.agents.get_agent_by_key / routes.agent_auth.get_agent_context).

Pinned:
- deactivate / activate flip agents.is_active (the model has no status or deactivated_at column,
  which is what these handlers used to write) and a deactivated agent's key is refused at once;
- a reset retires active keys in the key table auth is not reading, too.

Parametrised over the two key tables auth can read. Both exist, as in prod; "api_keys" is the
default resolution (auth prefers it), "agent_api_keys" is AGENT_AUTH_KEY_TABLE pinned to it.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_key_rotation_test \\
        .venv/bin/python -m pytest tests/test_employee_agent_admin_postgres.py
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
AGENT_ID = "agent_admin_pg"
OLD_KEY = "ak_live_" + "2" * 64
DORMANT_KEY = "ak_live_" + "3" * 64
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

# db/migrations/008_agents_advanced_schema.sql, Part 1.
_AGENT_API_KEYS_DDL = """
    CREATE TABLE IF NOT EXISTS agent_api_keys (
        id SERIAL PRIMARY KEY,
        agent_id VARCHAR(50) NOT NULL,
        key_id VARCHAR(50) UNIQUE NOT NULL,
        key_hash VARCHAR(255) NOT NULL,
        key_prefix VARCHAR(20) NOT NULL,
        scopes JSON DEFAULT '["orders:read", "products:read"]'::json,
        ip_whitelist JSON DEFAULT '[]'::json,
        is_active BOOLEAN DEFAULT true,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        expires_at TIMESTAMP WITH TIME ZONE,
        last_used_at TIMESTAMP WITH TIME ZONE,
        last_rotated_at TIMESTAMP WITH TIME ZONE,
        created_by VARCHAR(100),
        FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
    )
"""


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


async def _build_shared_tables(database):
    """Create-if-absent, never drop. The dialect gate runs every test_*_postgres.py file against ONE
    database: agents is the FK parent of agent_wallets / agent_beneficiaries built by sibling files,
    and a DROP ... CASCADE here would strip their foreign keys. Same convention as
    test_wallet_status_vocabulary_postgres.py."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.orders import orders as orders_table

    for table in (agents_db.agents, orders_table, agents_db.agent_usage_logs):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in agents_db.agents.indexes:  # agent_id / api_key are unique=True: a separate CREATE UNIQUE INDEX
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(_API_KEYS_DDL)
    await database.execute(_AGENT_API_KEYS_DDL)


async def _delete_own_rows(database, key_hashes):
    for table in ("api_keys", "agent_api_keys"):
        await database.execute(f"DELETE FROM {table} WHERE agent_id = :a", {"a": AGENT_ID})
        for key_hash in key_hashes:
            await database.execute(f"DELETE FROM {table} WHERE key_hash = :h", {"h": key_hash})
    await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT_ID})


@pytest.fixture(params=["api_keys", "agent_api_keys"])
async def db(request, monkeypatch):
    """Both key tables exist, as in prod. The layout under test is chosen the way ops would choose
    it: AGENT_AUTH_KEY_TABLE, which both the auth lookup and the key writers honour."""
    import db.agents as agents_db
    from db.database import database

    _assert_throwaway_database()
    layout = request.param
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto" if layout == "api_keys" else layout)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_CACHE", OrderedDict())

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _build_shared_tables(database)
    own_hashes = [_sha(OLD_KEY), _sha(DORMANT_KEY)]
    await _delete_own_rows(database, own_hashes)

    await database.execute(
        """
        INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active)
        VALUES (:agent_id, 'Admin PG', 'custom', :api_key, :api_key_hash, TRUE)
        """,
        {"agent_id": AGENT_ID, "api_key": f"redacted:{AGENT_ID}", "api_key_hash": _sha(OLD_KEY)},
    )
    if layout == "api_keys":
        await database.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
            VALUES (:a, 'Primary Key', :h, :p, 'active')
            """,
            {"a": AGENT_ID, "h": _sha(OLD_KEY), "p": OLD_KEY[:10]},
        )
    else:
        await database.execute(
            """
            INSERT INTO agent_api_keys (agent_id, key_id, key_hash, key_prefix, is_active, created_by)
            VALUES (:a, 'key_old', :h, :p, TRUE, 'seed')
            """,
            {"a": AGENT_ID, "h": _sha(OLD_KEY), "p": OLD_KEY[:12] + "..."},
        )
    try:
        yield layout
    finally:
        try:
            await _delete_own_rows(database, own_hashes)
        except Exception:  # noqa: BLE001
            pass
        # `databases` shares one connection process-wide: disconnect only what this fixture opened.
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _agent_context(api_key):
    from starlette.requests import Request

    from routes.agent_auth import get_agent_context

    scope = {"type": "http", "method": "GET", "path": "/agent/v1/probe", "headers": [], "query_string": b""}
    return await get_agent_context(Request(scope), api_key=api_key, bearer=None, checkout_token=None)


async def _old_key_id(layout):
    from db.database import database

    if layout == "api_keys":
        row = await database.fetch_one("SELECT id FROM api_keys WHERE key_hash = :h", {"h": _sha(OLD_KEY)})
        return str(row["id"])
    return "key_old"


# ── deactivate / activate ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deactivate_refuses_the_agents_key_at_once_and_activate_restores_it(db):
    from fastapi import HTTPException

    from db.agents import get_agent_by_key
    from routes.employee_agent_mgmt import activate_agent, deactivate_agent

    assert (await get_agent_by_key(OLD_KEY))["is_active"] is True  # now cached

    body = await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)
    assert body["status"] == "success"
    with pytest.raises(HTTPException) as exc:
        await _agent_context(OLD_KEY)
    assert exc.value.status_code == 403

    body = await activate_agent(AGENT_ID, current_user=EMPLOYEE)
    assert body["status"] == "success"
    assert (await get_agent_by_key(OLD_KEY))["is_active"] is True


@pytest.mark.asyncio
async def test_deactivate_twice_is_a_400_not_a_second_write(db):
    from fastapi import HTTPException

    from routes.employee_agent_mgmt import activate_agent, deactivate_agent

    await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)
    with pytest.raises(HTTPException) as exc:
        await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)
    assert exc.value.status_code == 400

    await activate_agent(AGENT_ID, current_user=EMPLOYEE)
    with pytest.raises(HTTPException) as exc:
        await activate_agent(AGENT_ID, current_user=EMPLOYEE)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_agent_list_reports_is_active_from_the_column_auth_reads(db):
    from routes.employee_agent_mgmt import deactivate_agent, get_all_agents

    await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)

    listed = await get_all_agents(status=None, date_range="7d", current_user=EMPLOYEE)
    [row] = [a for a in listed["agents"] if a["agent_id"] == AGENT_ID]
    assert row["is_active"] is False and row["status"] == "inactive"

    inactive = await get_all_agents(status="inactive", date_range="7d", current_user=EMPLOYEE)
    assert AGENT_ID in [a["agent_id"] for a in inactive["agents"]]
    assert all(a["is_active"] is False for a in inactive["agents"])
    active = await get_all_agents(status="active", date_range="7d", current_user=EMPLOYEE)
    assert AGENT_ID not in [a["agent_id"] for a in active["agents"]]


# ── reset vs the key table auth is not reading ────────────────────────────────

@pytest.mark.asyncio
async def test_reset_kills_a_dormant_key_in_the_table_auth_is_not_reading(db, monkeypatch):
    """Prod 2026-09-24: both tables exist, auth reads api_keys, and agent_api_keys still holds
    ACTIVE rows (agent_659a77ae254b8f4c has one from 02-27). Pinning AGENT_AUTH_KEY_TABLE to
    agent_api_keys -- the documented rollback lever -- would revive them, so a reset retires them."""
    if db != "api_keys":
        pytest.skip("needs both key tables")
    import db.agents as agents_db
    from db.database import database
    from routes.employee_agent_mgmt import reset_agent_api_key

    dormant = DORMANT_KEY
    await database.execute(
        """
        INSERT INTO agent_api_keys (agent_id, key_id, key_hash, key_prefix, is_active, created_by)
        VALUES (:a, 'key_dormant', :h, :p, TRUE, 'self_reset')
        """,
        {"a": AGENT_ID, "h": _sha(dormant), "p": dormant[:12] + "..."},
    )

    def pin(mode):
        monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", mode)
        monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
        monkeypatch.setattr(agents_db, "_AGENT_AUTH_CACHE", OrderedDict())

    pin("agent_api_keys")
    assert (await agents_db.get_agent_by_key(dormant))["agent_id"] == AGENT_ID  # the lever revives it

    pin("auto")
    await reset_agent_api_key(AGENT_ID, current_user=EMPLOYEE)

    pin("agent_api_keys")
    assert await agents_db.get_agent_by_key(dormant) is None
