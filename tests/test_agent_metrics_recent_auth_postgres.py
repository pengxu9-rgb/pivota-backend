"""GET /agent/metrics/recent and GET /agent/v1/metrics/recent read agent_usage_logs only for a
caller who may read it, against real Postgres on the tables the app's own model builds
(db.agents.agents / agent_usage_logs, whose timestamp is timestamptz, as in prod).

Both routes took `agent_id` from the query string as the filter with no credential, and with
neither it nor an x-api-key they ran with no WHERE clause: an anonymous caller read every agent's
calls (endpoint, method, status, timings, agent_id). Now (routes.agent_metrics.
resolve_recent_activity_scope, shared by both):
- an agent (x-api-key, or the agent portal's JWT agent_id claim) reads its own rows only, and
  naming another agent is a 403;
- an admin JWT (ADMIN_ROLES) reads every agent, or the one it names;
- anything else is a 401.

/agent/metrics/recent also computed `datetime.now() - timestamp`: naive minus aware raises on a
timestamptz row, and its catch-all answered {"status": "success", "activities": [], "count": 0}
to every caller. The own-rows assertions below pin that it now serves the row. Its rows now also
carry the fields the agent portal renders (method / endpoint / status_code / response_time_ms and an
ISO timestamp), as /agent/v1/metrics/recent's do.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_key_rotation_test \\
        .venv/bin/python -m pytest tests/test_agent_metrics_recent_auth_postgres.py
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
AGENT_A = "agent_recent_auth_pg_a"
AGENT_B = "agent_recent_auth_pg_b"
KEYS = {AGENT_A: "ak_live_" + "6" * 64, AGENT_B: "ak_live_" + "7" * 64}
# Distinct endpoints, so a leak check can match a row even where agent_id is not echoed (a refusal).
ENDPOINTS = {AGENT_A: "/agent/v1/recent-auth-pg/a", AGENT_B: "/agent/v1/recent-auth-pg/b"}
REQUEST_IDS = {AGENT_A: "req_recent_auth_pg_a", AGENT_B: "req_recent_auth_pg_b"}
ROUTES = ["/agent/metrics/recent", "/agent/v1/metrics/recent"]

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


def _token(claims):
    from utils.auth import create_access_token

    return "Bearer " + create_access_token({"sub": "u_recent_auth", "email": "recent@example.com", **claims})


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

    for table in (agents_db.agents, agents_db.agent_usage_logs):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect())))
    for index in agents_db.agents.indexes:
        await database.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=postgresql.dialect())))
    await database.execute(_API_KEYS_DDL)


async def _delete_own_rows(database):
    for agent_id in (AGENT_A, AGENT_B):
        await database.execute("DELETE FROM agent_usage_logs WHERE agent_id = :a", {"a": agent_id})
        await database.execute("DELETE FROM agent_usage_logs WHERE request_id = :r", {"r": REQUEST_IDS[agent_id]})
        await database.execute("DELETE FROM api_keys WHERE agent_id = :a", {"a": agent_id})
        await database.execute("DELETE FROM api_keys WHERE key_hash = :h", {"h": _sha(KEYS[agent_id])})
        await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": agent_id})


async def _seed(database):
    for agent_id in (AGENT_A, AGENT_B):
        await database.execute(
            """
            INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active)
            VALUES (:agent_id, 'Recent Auth PG', 'custom', :api_key, :api_key_hash, TRUE)
            """,
            {"agent_id": agent_id, "api_key": f"redacted:{agent_id}", "api_key_hash": _sha(KEYS[agent_id])},
        )
        await database.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
            VALUES (:a, 'Primary Key', :h, :p, 'active')
            """,
            {"a": agent_id, "h": _sha(KEYS[agent_id]), "p": KEYS[agent_id][:10]},
        )
        await database.execute(
            """
            INSERT INTO agent_usage_logs (agent_id, endpoint, method, request_id, status_code, response_time_ms, timestamp)
            VALUES (:a, :e, 'GET', :r, 200, 42, NOW())
            """,
            {"a": agent_id, "e": ENDPOINTS[agent_id], "r": REQUEST_IDS[agent_id]},
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
    await _seed(database)
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

    from routes.agent_metrics import router as metrics_router
    from routes.agent_metrics_v1 import router as metrics_v1_router

    app = FastAPI()
    for router in (metrics_router, metrics_v1_router):
        app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _seen(resp):
    """Which of this file's two agents' rows a 200 served."""
    assert resp.status_code == 200, (resp.status_code, resp.text)
    body = resp.json()
    assert body["status"] == "success", body
    return {a["agent_id"] for a in body["activities"] if a["agent_id"] in ENDPOINTS}


def _assert_leaks_nothing(resp):
    for agent_id in (AGENT_A, AGENT_B):
        assert agent_id not in resp.text and ENDPOINTS[agent_id] not in resp.text, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize(
    "query",
    [{}, {"agent_id": AGENT_A}],
    ids=["no-filter", "agent_id-in-query"],
)
async def test_an_anonymous_caller_is_refused(db, client, path, query):
    resp = await client.get(path, params={"limit": 100, **query})
    assert resp.status_code == 401, (path, query, resp.status_code, resp.text)
    _assert_leaks_nothing(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_an_agent_key_reads_its_own_rows_only(db, client, path):
    resp = await client.get(path, params={"limit": 100}, headers={"x-api-key": KEYS[AGENT_A]})
    assert _seen(resp) == {AGENT_A}, resp.text
    assert {a["agent_id"] for a in resp.json()["activities"]} == {AGENT_A}, resp.text
    if path == "/agent/metrics/recent":
        # The timestamptz row survived the "time ago" arithmetic (it used to raise into an empty 200).
        [row] = resp.json()["activities"]
        assert row["time_ago"] == "Just now", row


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_each_activity_carries_the_fields_the_agent_portal_renders(db, client, path):
    """pivota-agents-portal (app/dashboard/page.tsx, components/pages/LogsPage.tsx) renders
    event.id / method / endpoint / status_code / response_time_ms and parses event.timestamp as a
    date. /agent/metrics/recent served type / action / description / response_time and a relative
    "Just now" timestamp, so every row rendered blank with an Invalid Date."""
    from datetime import datetime, timedelta, timezone

    resp = await client.get(path, params={"limit": 100}, headers={"x-api-key": KEYS[AGENT_A]})
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["activities"]
    assert row["id"], row
    assert (row["method"], row["endpoint"], row["status_code"], row["response_time_ms"]) == (
        "GET", ENDPOINTS[AGENT_A], 200, 42
    ), row
    served_at = datetime.fromisoformat(row["timestamp"])
    assert served_at.tzinfo is not None, row
    assert abs(datetime.now(timezone.utc) - served_at) < timedelta(minutes=5), row


@pytest.mark.asyncio
async def test_a_row_with_no_status_or_timestamp_does_not_empty_the_list(db, client):
    """status_code and timestamp are nullable columns; one such row used to raise inside the
    formatting loop and the route's catch-all answered an empty "success" for the whole page."""
    await db.execute(
        "UPDATE agent_usage_logs SET status_code = NULL, timestamp = NULL WHERE request_id = :r",
        {"r": REQUEST_IDS[AGENT_A]},
    )
    resp = await client.get("/agent/metrics/recent", params={"limit": 100}, headers={"x-api-key": KEYS[AGENT_A]})
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["activities"]
    assert row["endpoint"] == ENDPOINTS[AGENT_A], row
    assert (row["status_code"], row["timestamp"], row["time_ago"], row["status"]) == (None, None, None, "error"), row


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_an_agent_key_cannot_name_another_agent(db, client, path):
    resp = await client.get(path, params={"limit": 100, "agent_id": AGENT_B}, headers={"x-api-key": KEYS[AGENT_A]})
    assert resp.status_code == 403, (path, resp.status_code, resp.text)
    _assert_leaks_nothing(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_an_agent_portal_jwt_reads_its_own_rows_and_cannot_name_another(db, client, path):
    """The agent portal (pivota-agents-portal lib/api-client.ts) calls /agent/metrics/recent with
    its session JWT only: role agent + an agent_id claim, no x-api-key."""
    auth = {"authorization": _token({"role": "agent", "agent_id": AGENT_A})}
    assert _seen(await client.get(path, params={"limit": 100}, headers=auth)) == {AGENT_A}

    resp = await client.get(path, params={"limit": 100, "agent_id": AGENT_B}, headers=auth)
    assert resp.status_code == 403, (path, resp.status_code, resp.text)
    _assert_leaks_nothing(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_an_admin_jwt_reads_across_agents_and_can_filter(db, client, path):
    auth = {"authorization": _token({"role": "admin"})}
    assert _seen(await client.get(path, params={"limit": 100}, headers=auth)) == {AGENT_A, AGENT_B}
    assert _seen(await client.get(path, params={"limit": 100, "agent_id": AGENT_B}, headers=auth)) == {AGENT_B}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(lambda: _token({"role": "employee"}), id="non-admin-staff-jwt"),
        pytest.param(lambda: _token({"role": "merchant", "merchant_id": "m_recent_auth"}), id="merchant-jwt"),
        pytest.param(lambda: "Bearer not-a-jwt", id="garbage-bearer"),
    ],
)
async def test_a_jwt_that_is_neither_admin_nor_an_agent_is_refused(db, client, path, authorization):
    resp = await client.get(path, params={"limit": 100}, headers={"authorization": authorization()})
    assert resp.status_code == 401, (path, resp.status_code, resp.text)
    _assert_leaks_nothing(resp)
