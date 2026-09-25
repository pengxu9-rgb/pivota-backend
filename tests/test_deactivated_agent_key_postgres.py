"""A deactivated agent's API key is refused by every route that resolves a key to an agent_id
through db.agents.resolve_agent_id_by_api_key, against real Postgres on the agents table the app's
own model builds (db.agents.agents).

get_agent_context has always 403'd an inactive agent, but resolve_agent_id_by_api_key returned the
agent_id for any key get_agent_by_key found, active or not, so after an employee deactivated an
agent (routes/employee_agent_mgmt.py flips agents.is_active) its key still read its analytics and
metrics. The resolver now refuses an inactive agent itself (db.agents.agent_is_active, the one
is_active rule; NULL is active), so every caller, current and future, inherits the refusal.

The same routes also take an agent-portal JWT whose agent_id claim they trusted as-is; that token
lives 7 days and portal login checks users.active, not agents.is_active. The claim now goes
through db.agents.resolve_active_agent_id, which reads the row.

Each route is first called with the ACTIVE key and must answer with the agent's own data: a
refusal that any key would get proves nothing.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_key_rotation_test \\
        .venv/bin/python -m pytest tests/test_deactivated_agent_key_postgres.py
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
AGENT_ID = "agent_deactivated_key_pg"
API_KEY = "ak_live_" + "4" * 64
REQUEST_ID = "req_deactivated_key_pg"
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

# Every route that reads an X-API-Key through resolve_agent_id_by_api_key.
KEY_ROUTES = [
    "/agent/v1/analytics/funnel",
    "/agent/v1/analytics/queries",
    "/agent/metrics/summary",
    "/agent/metrics/timeline",
    "/agent/metrics/recent",
    "/agent/v1/metrics/recent",
]


# Every route that reads the agent_id claim of a Bearer JWT. /agent/debug is mounted only under
# DEBUG_MODE and answers a refusal as a 200 {"error": ...}; the rest answer 401.
JWT_ROUTES = [
    "/agent/v1/analytics/funnel",
    "/agent/v1/analytics/queries",
    "/agent/metrics/summary",
    "/agent/metrics/timeline",
    "/agent/metrics/recent",
    "/agent/v1/metrics/recent",
    "/agent/debug/usage-logs",
    "/agent/debug/orders",
]


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write test rows to {dbname!r}; throwaway only")


async def _build_shared_tables(database):
    """Create-if-absent, never drop: the dialect gate runs every test_*_postgres.py file against ONE
    database (see test_employee_agent_admin_postgres.py)."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    import db.agents as agents_db
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders as orders_table

    for table in (agents_db.agents, orders_table, agents_db.agent_usage_logs, merchant_onboarding):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in agents_db.agents.indexes:
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(_API_KEYS_DDL)


async def _delete_own_rows(database):
    await database.execute("DELETE FROM agent_usage_logs WHERE agent_id = :a", {"a": AGENT_ID})
    await database.execute("DELETE FROM agent_usage_logs WHERE request_id = :r", {"r": REQUEST_ID})
    await database.execute("DELETE FROM api_keys WHERE agent_id = :a", {"a": AGENT_ID})
    await database.execute("DELETE FROM api_keys WHERE key_hash = :h", {"h": _sha(API_KEY)})
    await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT_ID})


async def _seed(database, is_active):
    await database.execute(
        """
        INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active)
        VALUES (:agent_id, 'Deactivated Key PG', 'custom', :api_key, :api_key_hash, :is_active)
        """,
        {
            "agent_id": AGENT_ID,
            "api_key": f"redacted:{AGENT_ID}",
            "api_key_hash": _sha(API_KEY),
            "is_active": is_active,
        },
    )
    await database.execute(
        """
        INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
        VALUES (:a, 'Primary Key', :h, :p, 'active')
        """,
        {"a": AGENT_ID, "h": _sha(API_KEY), "p": API_KEY[:10]},
    )
    # One call of the agent's own, inside every route's window, so the active-key control has
    # agent-scoped data to show.
    await database.execute(
        """
        INSERT INTO agent_usage_logs (agent_id, endpoint, method, request_id, status_code, response_time_ms, timestamp)
        VALUES (:a, '/agent/v1/orders/create', 'POST', :r, 200, 42, NOW())
        """,
        {"a": AGENT_ID, "r": REQUEST_ID},
    )


@pytest.fixture
async def db(monkeypatch):
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
    await _build_shared_tables(database)
    await _delete_own_rows(database)
    try:
        yield database
    finally:
        try:
            await _delete_own_rows(database)
        except Exception:  # noqa: BLE001
            pass
        # `databases` shares one connection process-wide: disconnect only what this fixture opened.
        if not was_connected and database.is_connected:
            await database.disconnect()


@pytest.fixture
async def client():
    import httpx
    from fastapi import FastAPI

    from routes.agent_analytics import router as analytics_router
    from routes.agent_debug import router as debug_router
    from routes.agent_metrics import router as metrics_router
    from routes.agent_metrics_v1 import router as metrics_v1_router

    app = FastAPI()
    for router in (analytics_router, metrics_router, metrics_v1_router, debug_router):
        app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _assert_serves_the_agents_own_data(path, resp):
    assert resp.status_code == 200, (path, resp.status_code, resp.text)
    body = resp.json()
    if path.startswith("/agent/debug/"):
        assert "error" not in body and body["agent_id"] == AGENT_ID, (path, body)
        if path == "/agent/debug/usage-logs":
            assert body["total_logs"] == 1, (path, body)
    elif path == "/agent/v1/metrics/recent":
        assert [a["agent_id"] for a in body["activities"]] == [AGENT_ID], (path, body)
    elif path == "/agent/metrics/recent":
        # Only the 200 discriminates here: against the model's timestamptz column this route's
        # `datetime.now() - timestamp` raises and its catch-all answers an empty "success" for
        # every resolved caller. A refused key is a 401 before that.
        assert body["status"] == "success", (path, body)
    elif path == "/agent/metrics/summary":
        # "degraded" is the route's catch-all for a failed query: not a served answer.
        assert body.get("status") not in ("degraded", "error"), (path, body)
        assert body["overview"]["total_requests"] == 1, (path, body)  # scoped to this agent
    elif path == "/agent/metrics/timeline":
        assert sum(h["total_requests"] for h in body["timeline"]) == 1, (path, body)
    else:
        assert body.get("status") == "success", (path, body)
    if path == "/agent/v1/analytics/queries":
        assert body["product_searches"] == 1, body  # the seeded /orders/create call, and only it


@pytest.mark.asyncio
async def test_a_deactivated_agents_key_is_refused_by_every_resolver_route(db, client):
    from routes.employee_agent_mgmt import deactivate_agent

    await _seed(db, is_active=True)
    for path in KEY_ROUTES:  # also warms this process's auth cache with is_active=True
        _assert_serves_the_agents_own_data(path, await client.get(path, headers={"x-api-key": API_KEY}))

    body = await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)
    assert body["status"] == "success"

    answered = {}
    for path in KEY_ROUTES:
        resp = await client.get(path, headers={"x-api-key": API_KEY})
        answered[path] = (resp.status_code, resp.text)
    assert {p: code for p, (code, _) in answered.items()} == {p: 401 for p in KEY_ROUTES}, answered
    for path, (_, text) in answered.items():
        assert AGENT_ID not in text and REQUEST_ID not in text, (path, text)


def _portal_jwt():
    """What agent-portal login (routes/agent_account.py) mints: agent_id claim, 7 days."""
    from datetime import timedelta

    from utils.auth import create_access_token

    return create_access_token(
        {
            "sub": "ident_deactivated_key_pg",
            "email": "owner@example.com",
            "role": "agent",
            "agent_id": AGENT_ID,
            "membership_type": "agent",
            "aud": "agent-portal",
            "scope": "agent",
        },
        expires_delta=timedelta(days=7),
    )


@pytest.mark.asyncio
async def test_a_deactivated_agents_portal_jwt_is_refused_by_every_claim_route(db, client):
    from routes.employee_agent_mgmt import deactivate_agent

    await _seed(db, is_active=True)
    headers = {"authorization": f"Bearer {_portal_jwt()}"}  # minted while the agent was active
    for path in JWT_ROUTES:
        _assert_serves_the_agents_own_data(path, await client.get(path, headers=headers))

    await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)

    answered = {}
    for path in JWT_ROUTES:
        resp = await client.get(path, headers=headers)
        answered[path] = (resp.status_code, resp.text)
    expected = {p: (200 if p.startswith("/agent/debug/") else 401) for p in JWT_ROUTES}
    assert {p: code for p, (code, _) in answered.items()} == expected, answered
    for path, (_, text) in answered.items():
        assert AGENT_ID not in text and REQUEST_ID not in text, (path, text)
        if path.startswith("/agent/debug/"):
            assert "Missing or invalid token" in text, (path, text)


@pytest.mark.asyncio
async def test_a_null_is_active_agent_is_active_on_every_resolver_route(db, client):
    """NULL is_active is active, as get_agent_context and _normalize_agent_row treat it: a raw
    INSERT without the column (the model's default is Python-side) must not lock the agent out."""
    await _seed(db, is_active=None)
    assert await db.fetch_val("SELECT is_active FROM agents WHERE agent_id = :a", {"a": AGENT_ID}) is None
    for path in KEY_ROUTES:
        _assert_serves_the_agents_own_data(path, await client.get(path, headers={"x-api-key": API_KEY}))
    headers = {"authorization": f"Bearer {_portal_jwt()}"}
    for path in JWT_ROUTES:
        _assert_serves_the_agents_own_data(path, await client.get(path, headers=headers))


@pytest.mark.asyncio
async def test_a_deactivated_agents_key_does_not_select_its_user_jwt_issuers(db, monkeypatch):
    """routes/agent_user_auth: the calling agent's key picks which federated issuers may verify an
    agent-user JWT. Every current consumer also runs get_agent_context (which 403s first), but the
    dependency itself must not bind an issuer to a deactivated agent."""
    import routes.agent_user_auth as agent_user_auth
    from routes.employee_agent_mgmt import deactivate_agent

    seen = []

    async def _verify(token, agent_id):
        seen.append(agent_id)
        return None

    monkeypatch.setattr(agent_user_auth, "verify_agent_user_jwt_for_agent", _verify)
    await _seed(db, is_active=True)

    await agent_user_auth.get_agent_user_identity(x_agent_user_jwt="t.o.k", x_api_key=API_KEY)
    await deactivate_agent(AGENT_ID, current_user=EMPLOYEE)
    await agent_user_auth.get_agent_user_identity(x_agent_user_jwt="t.o.k", x_api_key=API_KEY)

    assert seen == [AGENT_ID, None]
