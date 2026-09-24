"""OAuth client -> agent registrations on real Postgres (migration 240), and the assertion path that
reads them: one ACTIVE registration per (issuer, client_id), history kept on re-pointing, and a
disabled or unregistered client naming no agent.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_oauth_test \\
        .venv/bin/python -m pytest tests/test_agent_oauth_clients_postgres.py
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG = Path(__file__).resolve().parent.parent / "db/migrations"
SECRET = "oauth_test_secret"
ISS = "https://auth.example.com/"
OP = "offers.resolve"

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
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS agent_oauth_clients CASCADE")
    for stmt in split_statements((_MIG / "240_agent_oauth_clients.sql").read_text()):
        await database.execute(stmt)
    try:
        yield database
    finally:
        try:
            await database.execute("DROP TABLE IF EXISTS agent_oauth_clients CASCADE")
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


def _token(client_id, *, iss=ISS, now):
    from services.issuing_agent_assertion import sign_issuing_agent_assertion

    return sign_issuing_agent_assertion(
        {"v": 1, "kind": "oauth", "iss": iss, "cid": client_id, "op": OP, "ts": int(now)}, SECRET
    )


async def _resolve(client_id, *, iss=ISS, excluded=("agent_gw",)):
    import time

    from services.issuing_agent_assertion import resolve_asserted_agent_id

    now = time.time()
    return await resolve_asserted_agent_id(_token(client_id, iss=iss, now=now), op=OP,
                                           excluded_agent_ids=set(excluded), now=now)


async def test_a_registered_client_is_credited_to_its_agent_and_an_unregistered_one_to_no_one(db):
    from services.agent_oauth_clients import register_oauth_client

    await register_oauth_client(issuer=ISS, client_id="claude_connector", agent_id="agent_claude",
                                registered_by="test")
    assert await _resolve("claude_connector") == "agent_claude"
    assert await _resolve("unknown_connector") is None
    # The issuer is part of the key: the same client id under another issuer is another client.
    assert await _resolve("claude_connector", iss="https://other.example.com/") is None


async def test_re_pointing_a_client_keeps_one_active_row_and_the_history(db):
    from services.agent_oauth_clients import list_oauth_clients, register_oauth_client

    await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_claude", registered_by="test")
    result = await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_chatgpt",
                                         registered_by="test")
    assert [r["agent_id"] for r in result["replaced"]] == ["agent_claude"]
    assert await _resolve("c1") == "agent_chatgpt"
    rows = await list_oauth_clients()
    assert sorted((r["agent_id"], r["disabled_at"] is None) for r in rows) == [
        ("agent_chatgpt", True), ("agent_claude", False)]


async def test_the_database_refuses_two_active_rows_for_one_client(db):
    await db.execute("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
                     "VALUES (:i, 'c1', 'agent_claude', 't')", {"i": ISS})
    with pytest.raises(Exception):
        await db.execute("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
                         "VALUES (:i, 'c1', 'agent_chatgpt', 't')", {"i": ISS})


async def test_a_disabled_client_names_no_one(db):
    from services.agent_oauth_clients import disable_oauth_client, register_oauth_client

    await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_claude", registered_by="test")
    disabled = await disable_oauth_client(issuer=ISS, client_id="c1")
    assert [r["agent_id"] for r in disabled] == ["agent_claude"]
    assert await _resolve("c1") is None


async def test_registration_refuses_an_inactive_agent_or_a_service_identity(db):
    from services.agent_oauth_clients import OAuthClientRegistrationError, register_oauth_client

    with pytest.raises(OAuthClientRegistrationError):
        await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_retired", registered_by="t")
    with pytest.raises(OAuthClientRegistrationError):
        await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_missing", registered_by="t")
    with pytest.raises(OAuthClientRegistrationError):
        await register_oauth_client(issuer=ISS, client_id="c1", agent_id="agent_gw", registered_by="t",
                                    excluded_agent_ids={"agent_gw"})
    assert (await db.fetch_val("SELECT COUNT(*) FROM agent_oauth_clients")) == 0


async def test_a_client_registered_to_an_agent_later_retired_or_excluded_names_no_one(db):
    await db.execute("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
                     "VALUES (:i, 'c_ret', 'agent_retired', 't'), (:i, 'c_gw', 'agent_gw', 't')", {"i": ISS})
    assert await _resolve("c_ret") is None
    assert await _resolve("c_gw") is None
