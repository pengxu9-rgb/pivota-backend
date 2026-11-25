"""scripts/sandbox/register_gateway_service_agent.py against REAL Postgres (a throwaway schema).

    DATABASE_URL=postgresql://postgres@localhost:5432/pivota_sandbox_copy_test \\
        .venv/bin/python -m pytest tests/test_register_gateway_service_agent_postgres.py

Does not import `main`. The tables are built from the shapes db/agents.py and routes/agent_keys.py
write, and the lookup is the api_keys join db.agents._lookup_agent_from_key_table runs.
"""

from __future__ import annotations

import hashlib
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith(("postgresql://", "postgres://"))
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL (see docstring)")
if _IS_PG:
    _db = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in _db for m in ("dialect_check", "_test", "test_")):
        pytest.skip("throwaway database only", allow_module_level=True)

from scripts.sandbox import register_gateway_service_agent as R  # noqa: E402

S = "reg_agent_test"
KEY = "ak_test_" + "a1b2c3d4" * 8
OTHER_KEY = "ak_test_" + "0f" * 32
AGENT = "agent_982b1ea2df866206"


@pytest.fixture
async def conn():
    import asyncpg

    c = await asyncpg.connect(DATABASE_URL)
    await c.execute(f"""
        DROP SCHEMA IF EXISTS {S} CASCADE; CREATE SCHEMA {S};
        CREATE TABLE {S}.agents (
          id SERIAL PRIMARY KEY, agent_id VARCHAR(50) UNIQUE NOT NULL, agent_name VARCHAR(255) NOT NULL,
          agent_type VARCHAR(50) NOT NULL, description TEXT, api_key VARCHAR(100) UNIQUE NOT NULL,
          api_key_hash VARCHAR(255) NOT NULL, is_active BOOLEAN DEFAULT TRUE, allowed_merchants JSON,
          owner_email VARCHAR(255), metadata JSON, created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE {S}.api_keys (
          id SERIAL PRIMARY KEY, agent_id VARCHAR(255) NOT NULL, name VARCHAR(255) NOT NULL,
          key_hash VARCHAR(255) NOT NULL, key_prefix VARCHAR(20) NOT NULL, status VARCHAR(50) DEFAULT 'active',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_used TIMESTAMP, usage_count INTEGER DEFAULT 0,
          UNIQUE(key_hash));""")
    try:
        yield c
    finally:
        await c.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        await c.close()


async def _register(conn, **kw):
    args = dict(agent_id=AGENT, key=KEY, agent_name="Pivota Shopping (sandbox)", apply=True, schema=S)
    args.update(kw)
    return await R.register(conn, **args)


async def _lookup(conn, key):
    return await conn.fetchrow(
        f"SELECT a.* FROM {S}.agents a JOIN {S}.api_keys k ON k.agent_id = a.agent_id "
        f"WHERE k.key_hash = $1 AND k.status = 'active' LIMIT 1", hashlib.sha256(key.encode()).hexdigest())


async def _everything(conn) -> str:
    rows = [dict(r) for t in ("agents", "api_keys") for r in await conn.fetch(f"SELECT * FROM {S}.{t}")]
    return repr(rows)


async def test_a_dry_run_writes_nothing(conn):
    plan = await _register(conn, apply=False)
    assert plan == {"agent_id": AGENT, "apply": False, "agent_row": "create", "key_row": "create"}
    assert await conn.fetchval(f"SELECT count(*) FROM {S}.agents") == 0
    assert await conn.fetchval(f"SELECT count(*) FROM {S}.api_keys") == 0


async def test_apply_registers_the_hash_and_the_auth_join_finds_the_agent(conn):
    result = await _register(conn)
    assert result["done"] is True and result["agent_row"] == "create" and result["key_row"] == "create"
    agent = await _lookup(conn, KEY)
    assert agent["agent_id"] == AGENT and agent["agent_type"] == "service" and agent["is_active"] is True
    # The key itself is stored nowhere; the legacy plaintext column holds a non-key placeholder.
    assert KEY not in await _everything(conn)
    assert agent["api_key"] == "hashed-only:" + AGENT
    assert not agent["api_key"].startswith("ak_")
    assert await _lookup(conn, OTHER_KEY) is None


async def test_the_result_never_carries_the_key_or_its_hash(conn):
    result = repr(await _register(conn))
    assert KEY not in result and hashlib.sha256(KEY.encode()).hexdigest() not in result and KEY[:10] not in result


async def test_a_rerun_is_a_no_op(conn):
    await _register(conn)
    again = await _register(conn)
    assert again["agent_row"] == "exists" and again["key_row"] == "exists"
    assert await conn.fetchval(f"SELECT count(*) FROM {S}.api_keys") == 1


async def test_a_key_bound_to_another_agent_is_refused(conn):
    await _register(conn)
    with pytest.raises(R.Refused, match="another agent"):
        await _register(conn, agent_id="agent_someone_else")


async def test_an_existing_agent_gets_the_key_without_being_rewritten(conn):
    await conn.execute(f"INSERT INTO {S}.agents (agent_id, agent_name, agent_type, api_key, api_key_hash) "
                       f"VALUES ($1, 'Existing', 'custom', 'x', 'y')", AGENT)
    result = await _register(conn)
    assert result["agent_row"] == "exists" and result["key_row"] == "create"
    row = await conn.fetchrow(f"SELECT agent_name, agent_type FROM {S}.agents WHERE agent_id = $1", AGENT)
    assert dict(row) == {"agent_name": "Existing", "agent_type": "custom"}
    assert (await _lookup(conn, KEY))["agent_id"] == AGENT


async def test_an_inactive_agent_is_refused(conn):
    await conn.execute(f"INSERT INTO {S}.agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active) "
                       f"VALUES ($1, 'Off', 'custom', 'x', 'y', FALSE)", AGENT)
    with pytest.raises(R.Refused, match="inactive"):
        await _register(conn)


@pytest.mark.parametrize("key", ["ak_live_" + "ab" * 32, "ak_" + "ab" * 32, "ak_test_" + "AB" * 32, "", "ak_test_short"])
async def test_only_a_test_key_is_registered(conn, key):
    with pytest.raises(R.Refused, match="ak_test_") as excinfo:
        await _register(conn, key=key)
    if key:
        assert key not in str(excinfo.value)
    assert await conn.fetchval(f"SELECT count(*) FROM {S}.api_keys") == 0


@pytest.mark.parametrize("agent_id", ["", "agent_", "robot_1", "agent_x; drop table agents"])
async def test_a_malformed_agent_id_is_refused(conn, agent_id):
    with pytest.raises(R.Refused, match="agent-id"):
        await _register(conn, agent_id=agent_id)


async def test_run_reads_the_key_only_after_the_environment_passes(conn, monkeypatch):
    from tests.test_sandbox_guard import GOOD

    env = dict(GOOD, DATABASE_URL="postgresql://pivota:pw@10.61.0.5:5432/pivota",
               ISSUING_AGENT_EXCLUDED_AGENT_IDS=AGENT, ISSUING_ASSERTION_VOUCHER_AGENT_IDS=AGENT)
    reads = []

    def fetch():
        reads.append(1)
        return KEY

    class _Conn:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        async def close(self):
            pass

    async def connect(dsn):
        assert "10.61.0.5" in dsn
        return _Conn(conn)

    real_register = R.register
    monkeypatch.setattr(R, "register", lambda c, **kw: real_register(c, **{**kw, "schema": S}))
    result = await R._run(["--agent-id", AGENT], env, fetch_key=fetch, connect=connect)
    assert reads == [1] and result["apply"] is False and result["agent_row"] == "create"
