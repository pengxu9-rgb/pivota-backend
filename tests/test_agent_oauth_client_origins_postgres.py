"""OAuth redirect origin -> agent registrations on real Postgres (migration 240), resolved through the
authorization server's REAL client table (mcp_oauth_clients, as routes/mcp_oauth_as.PostgresStore
creates it): a client is credited only when EVERY redirect origin it registered maps to one agent.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_oauth_test \\
        .venv/bin/python -m pytest tests/test_agent_oauth_client_origins_postgres.py
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
SECRET = "oauth_origin_test_secret"
ISS = "https://as.pivota.test"
OP = "offers.resolve"
CLIENT_PREFIX = "mcpc_originsuite_"

# The authorization server's own DDL (routes/mcp_oauth_as.PostgresStore._ensure). Created if missing,
# never dropped: other suites share it. This suite only adds and removes its own prefixed rows.
_MCP_OAUTH_CLIENTS_DDL = """
CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_secret_hash TEXT,
    redirect_uris JSONB NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL,
    client_name TEXT,
    created_at BIGINT NOT NULL
)"""

# Agents are not under test; the real get_agent reads a table this suite must not rebuild.
_AGENTS = {
    "agent_claude": {"agent_id": "agent_claude", "is_active": True},
    "agent_chatgpt": {"agent_id": "agent_chatgpt", "is_active": True},
    "agent_retired": {"agent_id": "agent_retired", "is_active": False},
    "agent_gw": {"agent_id": "agent_gw", "is_active": True},
}


def _assert_throwaway_database():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop tables in {dbname!r}; throwaway only")


@pytest.fixture(autouse=True)
async def db(monkeypatch):
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()

    async def fake_get_agent(agent_id):
        return _AGENTS.get(agent_id)

    monkeypatch.setattr("db.agents.get_agent", fake_get_agent)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISS)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS agent_oauth_client_origins CASCADE")
    for stmt in split_statements((_MIG / "240_agent_oauth_client_origins.sql").read_text()):
        await database.execute(stmt)
    await database.execute(_MCP_OAUTH_CLIENTS_DDL)
    await database.execute("DELETE FROM mcp_oauth_clients WHERE client_id LIKE :p", {"p": CLIENT_PREFIX + "%"})
    try:
        yield database
    finally:
        try:
            await database.execute("DELETE FROM mcp_oauth_clients WHERE client_id LIKE :p",
                                   {"p": CLIENT_PREFIX + "%"})
            await database.execute("DROP TABLE IF EXISTS agent_oauth_client_origins CASCADE")
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _client(db, name, *redirect_uris):
    """A dynamically registered client, stored exactly as the authorization server stores it."""
    client_id = CLIENT_PREFIX + name
    await db.execute(
        "INSERT INTO mcp_oauth_clients (client_id, client_secret_hash, redirect_uris, "
        "token_endpoint_auth_method, client_name, created_at) "
        "VALUES (:c, NULL, :r, 'none', 'Claude', 0)",
        {"c": client_id, "r": json.dumps(list(redirect_uris))},
    )
    return client_id


async def _resolve(client_id, *, iss=ISS):
    from services.issuing_agent_assertion import resolve_asserted_agent_id, sign_issuing_agent_assertion

    now = int(time.time())
    token = sign_issuing_agent_assertion(
        {"v": 1, "kind": "oauth", "iss": iss, "cid": client_id, "op": OP, "ts": now}, SECRET)
    return await resolve_asserted_agent_id(token, op=OP, excluded_agent_ids={"agent_gw"}, now=now)


async def _register(origin, agent):
    from services.agent_oauth_client_origins import register_redirect_origin

    return await register_redirect_origin(redirect_origin=origin, agent_id=agent, registered_by="test")


async def test_every_install_of_a_registered_connector_is_credited_to_its_agent(db):
    await _register("https://claude.ai", "agent_claude")
    # Two installs, two random DCR ids, one connector.
    a = await _client(db, "a", "https://claude.ai/api/mcp/auth_callback")
    b = await _client(db, "b", "https://claude.ai/api/mcp/auth_callback", "https://CLAUDE.ai:443/other")
    assert await _resolve(a) == "agent_claude"
    assert await _resolve(b) == "agent_claude"


async def test_one_unregistered_or_conflicting_origin_credits_no_one(db):
    await _register("https://claude.ai", "agent_claude")
    await _register("https://chatgpt.com", "agent_chatgpt")
    # A squatter lists claude.ai's callback beside its own: it can complete grants at its own origin.
    squat = await _client(db, "squat", "https://claude.ai/api/mcp/auth_callback", "https://evil.example/cb")
    mixed = await _client(db, "mixed", "https://claude.ai/cb", "https://chatgpt.com/cb")
    assert await _resolve(squat) is None
    assert await _resolve(mixed) is None


async def test_loopback_and_non_https_redirects_credit_no_one(db):
    await _register("https://claude.ai", "agent_claude")
    for name, uri in [("loop", "http://localhost:8765/cb"), ("ip", "https://127.0.0.1/cb"),
                      ("custom", "claude://oauth/cb")]:
        client_id = await _client(db, name, "https://claude.ai/cb", uri)
        assert await _resolve(client_id) is None, uri


async def test_an_unknown_client_or_a_foreign_issuer_credits_no_one(db):
    await _register("https://claude.ai", "agent_claude")
    real = await _client(db, "real", "https://claude.ai/cb")
    assert await _resolve(CLIENT_PREFIX + "never_registered") is None
    assert await _resolve(real, iss="https://other-as.example") is None


async def test_re_pointing_an_origin_keeps_one_active_row_and_the_history(db):
    from services.agent_oauth_client_origins import list_redirect_origins

    await _register("https://claude.ai", "agent_claude")
    result = await _register("https://claude.ai", "agent_chatgpt")
    assert [r["agent_id"] for r in result["replaced"]] == ["agent_claude"]
    assert await _resolve(await _client(db, "c", "https://claude.ai/cb")) == "agent_chatgpt"
    rows = await list_redirect_origins()
    assert sorted((r["agent_id"], r["disabled_at"] is None) for r in rows) == [
        ("agent_chatgpt", True), ("agent_claude", False)]


async def test_a_disabled_origin_credits_no_one(db):
    from services.agent_oauth_client_origins import disable_redirect_origin

    await _register("https://claude.ai", "agent_claude")
    client_id = await _client(db, "d", "https://claude.ai/cb")
    assert [r["agent_id"] for r in await disable_redirect_origin(redirect_origin="https://claude.ai")] == [
        "agent_claude"]
    assert await _resolve(client_id) is None


async def test_the_database_refuses_two_active_rows_for_one_origin(db):
    sql = ("INSERT INTO agent_oauth_client_origins (issuer, redirect_origin, agent_id, registered_by) "
           "VALUES (:i, 'https://claude.ai', :a, 't')")
    await db.execute(sql, {"i": ISS, "a": "agent_claude"})
    with pytest.raises(Exception):
        await db.execute(sql, {"i": ISS, "a": "agent_chatgpt"})


@pytest.mark.parametrize(
    "origin, agent",
    [
        ("https://claude.ai", "agent_retired"),
        ("https://claude.ai", "agent_missing"),
        ("https://claude.ai", "agent_gw"),
        ("http://claude.ai", "agent_claude"),
        ("https://claude.ai/api/mcp/auth_callback", "agent_claude"),
        ("https://localhost", "agent_claude"),
        ("https://10.0.0.1", "agent_claude"),
    ],
)
async def test_registration_refuses_what_resolution_could_never_trust(db, origin, agent):
    from services.agent_oauth_client_origins import OAuthOriginRegistrationError, register_redirect_origin

    with pytest.raises(OAuthOriginRegistrationError):
        await register_redirect_origin(redirect_origin=origin, agent_id=agent, registered_by="t",
                                       excluded_agent_ids={"agent_gw"})
    assert (await db.fetch_val("SELECT COUNT(*) FROM agent_oauth_client_origins")) == 0


async def test_an_origin_registered_to_an_agent_later_retired_or_excluded_credits_no_one(db):
    await db.execute("INSERT INTO agent_oauth_client_origins (issuer, redirect_origin, agent_id, registered_by) "
                     "VALUES (:i, 'https://claude.ai', 'agent_retired', 't'), "
                     "(:i, 'https://chatgpt.com', 'agent_gw', 't')", {"i": ISS})
    assert await _resolve(await _client(db, "r", "https://claude.ai/cb")) is None
    assert await _resolve(await _client(db, "g", "https://chatgpt.com/cb")) is None
