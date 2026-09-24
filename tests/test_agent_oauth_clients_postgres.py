"""Provisioned OAuth client -> agent registrations on real Postgres (migration 240), resolved through
the authorization server's REAL client table and flow (mcp_oauth_clients via
routes/mcp_oauth_as.PostgresStore, services/mcp_oauth_flow): only a confidential client this
deployment provisioned is ever credited; a public client is not, even one registered with a
partner's callback URL (the squatter path, driven through the real flow below).

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_oauth_test \\
        .venv/bin/python -m pytest tests/test_agent_oauth_clients_postgres.py
"""

import base64
import hashlib
import os
import secrets
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
SECRET = "oauth_client_test_secret"
ISS = "https://as.pivota.test"
OP = "offers.resolve"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
OPERATOR_SECRET = "a1" * 32  # what `openssl rand -hex 32` produces

# Agents are not under test; the real get_agent reads a table this suite must not rebuild.
_AGENTS = {
    "agent_acme": {"agent_id": "agent_acme", "is_active": True},
    "agent_beta": {"agent_id": "agent_beta", "is_active": True},
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
    from routes.mcp_oauth_as import PostgresStore

    _assert_throwaway_database()

    async def fake_get_agent(agent_id):
        return _AGENTS.get(agent_id)

    monkeypatch.setattr("db.agents.get_agent", fake_get_agent)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISS)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS agent_oauth_clients CASCADE")
    for stmt in split_statements((_MIG / "240_agent_oauth_clients.sql").read_text()):
        await database.execute(stmt)
    store = PostgresStore()
    await store._ensure()  # the authorization server's own tables, as it creates them
    created_before = {r["client_id"] for r in await database.fetch_all("SELECT client_id FROM mcp_oauth_clients")}
    try:
        yield store
    finally:
        try:
            # Remove ONLY what this test created from the shared authorization-server tables.
            rows = await database.fetch_all("SELECT client_id FROM mcp_oauth_clients")
            for client_id in {r["client_id"] for r in rows} - created_before:
                for table in ("mcp_oauth_refresh", "mcp_oauth_codes", "mcp_oauth_consents", "mcp_oauth_clients"):
                    await database.execute(f"DELETE FROM {table} WHERE client_id = :c", {"c": client_id})
            await database.execute("DROP TABLE IF EXISTS agent_oauth_clients CASCADE")
        except Exception:  # noqa: BLE001
            pass
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _resolve(client_id, *, iss=ISS):
    from services.issuing_agent_assertion import resolve_asserted_agent_id, sign_issuing_agent_assertion

    now = int(time.time())
    token = sign_issuing_agent_assertion(
        {"v": 1, "kind": "oauth", "iss": iss, "cid": client_id, "op": OP, "ts": now}, SECRET)
    return await resolve_asserted_agent_id(token, op=OP, excluded_agent_ids={"agent_gw"}, now=now)


async def _provision(store, agent="agent_acme", uris=(CLAUDE_CALLBACK,), secret=OPERATOR_SECRET):
    from services.agent_oauth_clients import provision_oauth_client

    return await provision_oauth_client(agent_id=agent, redirect_uris=list(uris), client_name="Acme on Claude",
                                        registered_by="test", client_secret=secret,
                                        excluded_agent_ids={"agent_gw"}, store=store)


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _grant(store, client_id, *, client_secret=None, subject="buyer-1"):
    """The real authorization-code flow, as a browser + connector would drive it."""
    import jwt

    from services import mcp_oauth_flow as flow

    verifier, challenge = _pkce()
    resource = os.environ["MCP_OAUTH_AS_ALLOWED_RESOURCES"].split(",")[0]
    validated = await flow.validate_authorization_request(store, {
        "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource})
    code = await flow.issue_authorization_code(store, validated=validated, subject=subject)
    tok = await flow.exchange_authorization_code(store, params={
        "client_id": client_id, "code": code, "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier},
        client_secret=client_secret)
    return jwt.decode(tok["access_token"], options={"verify_signature": False})


@pytest.fixture
def auth_server(monkeypatch):
    """Enough authorization-server config to run the real flow (as tests/test_mcp_oauth_flow.py does)."""
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", "agent-oauth-clients-kid")
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOWED_RESOURCES", "https://agent.pivota.test/mcp")


async def test_a_provisioned_client_is_credited_and_the_secret_is_never_returned_or_stored(db):
    out = await _provision(db)
    assert out["token_endpoint_auth_method"] == "client_secret_basic"
    assert OPERATOR_SECRET not in str(out), "the secret must never reach a job log"
    stored = await db.get_client(out["client_id"])
    assert stored["client_secret_hash"] and OPERATOR_SECRET not in str(stored)
    assert await _resolve(out["client_id"]) == "agent_acme"


@pytest.mark.parametrize("secret", ["", "short", "x" * 31, "has spaces " * 4, "slash/plus+equals=" * 3])
async def test_a_weak_or_unsafe_operator_secret_is_refused_before_anything_is_created(db, secret):
    from services.agent_oauth_clients import OAuthClientRegistrationError

    before = await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients")
    with pytest.raises(OAuthClientRegistrationError):
        await _provision(db, secret=secret)
    assert await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients") == before


async def test_a_secret_with_a_trailing_newline_authenticates_as_the_partner_will_send_it(db, auth_server):
    out = await _provision(db, secret=OPERATOR_SECRET + "\n")
    claims = await _grant(db, out["client_id"], client_secret=OPERATOR_SECRET)
    assert await _resolve(claims["client_id"]) == "agent_acme"


async def test_provisioning_without_an_issuer_creates_nothing(db, monkeypatch):
    from services.agent_oauth_clients import OAuthClientRegistrationError

    monkeypatch.delenv("MCP_OAUTH_AS_ISSUER")
    before = await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients")
    with pytest.raises(OAuthClientRegistrationError):
        await _provision(db)
    assert await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients") == before


async def test_a_failed_registration_removes_the_client_it_just_created(db, monkeypatch):
    import services.agent_oauth_clients as svc

    async def boom(*args, **kwargs):
        raise RuntimeError("registry insert failed")

    monkeypatch.setattr(svc, "_write_registration", boom)
    before = await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients")
    with pytest.raises(RuntimeError):
        await _provision(db)
    assert await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients") == before


async def test_the_squatter_path_credits_no_one(db, auth_server):
    """The review's attack, end to end: open registration of a PUBLIC client with Claude's callback,
    authorize in one's own browser, exchange with PKCE and no secret. The token is real and names
    the client; it must credit nobody, even with a registration row forced in beside it."""
    from services import mcp_oauth_flow as flow

    await _provision(db)  # the legitimate partner, registered to agent_acme
    squat = await flow.register_client(db, {"redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": "none"})
    claims = await _grant(db, squat["client_id"])
    assert claims["iss"] == ISS and claims["client_id"] == squat["client_id"]
    assert await _resolve(claims["client_id"]) is None
    # Even an operator mistake that maps the public client cannot make it creditable.
    await db_execute("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
                     "VALUES (:i, :c, 'agent_acme', 't')", {"i": ISS, "c": squat["client_id"]})
    assert await _resolve(squat["client_id"]) is None


async def test_a_provisioned_clients_real_grant_needs_its_secret_and_then_credits_its_agent(db, auth_server):
    from services.mcp_oauth_flow import OAuthFlowError

    out = await _provision(db)
    with pytest.raises(OAuthFlowError):
        await _grant(db, out["client_id"], client_secret=None)
    with pytest.raises(OAuthFlowError):
        await _grant(db, out["client_id"], client_secret="b2" * 32)
    claims = await _grant(db, out["client_id"], client_secret=OPERATOR_SECRET)
    assert await _resolve(claims["client_id"]) == "agent_acme"


async def test_registration_refuses_a_public_unknown_or_stranger_provisioned_client(db):
    from services import mcp_oauth_flow as flow
    from services.agent_oauth_clients import OAuthClientRegistrationError, register_oauth_client

    public = await flow.register_client(db, {"redirect_uris": [CLAUDE_CALLBACK]})
    # Open registration also mints CONFIDENTIAL clients, for whoever asks: a client_id a stranger sends
    # us must never be credited, since the stranger holds its secret.
    stranger = await flow.register_client(db, {"redirect_uris": [CLAUDE_CALLBACK],
                                               "token_endpoint_auth_method": "client_secret_basic"})
    for client_id in (public["client_id"], stranger["client_id"], "mcpc_never_registered"):
        with pytest.raises(OAuthClientRegistrationError):
            await register_oauth_client(client_id=client_id, agent_id="agent_acme", registered_by="t")
    assert (await db_fetch_val("SELECT COUNT(*) FROM agent_oauth_clients")) == 0


async def test_a_foreign_issuer_credits_no_one(db):
    out = await _provision(db)
    assert await _resolve(out["client_id"], iss="https://other-as.example") is None
    # Even a row forced in under the foreign issuer: its client records are not ours to verify, and a
    # colliding client_id there says nothing about the confidential client of the same id here.
    await db_execute("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
                     "VALUES ('https://other-as.example', :c, 'agent_beta', 't')", {"c": out["client_id"]})
    assert await _resolve(out["client_id"], iss="https://other-as.example") is None


async def test_re_pointing_a_client_keeps_one_active_row_and_the_history(db):
    from services.agent_oauth_clients import list_oauth_clients, register_oauth_client

    out = await _provision(db)
    result = await register_oauth_client(client_id=out["client_id"], agent_id="agent_beta", registered_by="t")
    assert [r["agent_id"] for r in result["replaced"]] == ["agent_acme"]
    assert await _resolve(out["client_id"]) == "agent_beta"
    rows = [r for r in await list_oauth_clients() if r["client_id"] == out["client_id"]]
    assert sorted((r["agent_id"], r["disabled_at"] is None) for r in rows) == [
        ("agent_acme", False), ("agent_beta", True)]


async def test_a_disabled_client_credits_no_one_but_keeps_working_for_oauth(db):
    from services.agent_oauth_clients import disable_oauth_client

    out = await _provision(db)
    assert [r["agent_id"] for r in await disable_oauth_client(client_id=out["client_id"])] == ["agent_acme"]
    assert await _resolve(out["client_id"]) is None
    assert await db.get_client(out["client_id"]) is not None


async def test_the_database_refuses_two_active_rows_for_one_client(db):
    sql = ("INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by) "
           "VALUES (:i, 'mcpc_x', :a, 't')")
    await db_execute(sql, {"i": ISS, "a": "agent_acme"})
    with pytest.raises(Exception):
        await db_execute(sql, {"i": ISS, "a": "agent_beta"})


@pytest.mark.parametrize("agent", ["agent_retired", "agent_missing", "agent_gw"])
async def test_provisioning_refuses_an_inactive_or_service_agent_before_minting(db, agent):
    from services.agent_oauth_clients import OAuthClientRegistrationError

    before = await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients")
    with pytest.raises(OAuthClientRegistrationError):
        await _provision(db, agent=agent)
    assert await db_fetch_val("SELECT COUNT(*) FROM mcp_oauth_clients") == before
    assert await db_fetch_val("SELECT COUNT(*) FROM agent_oauth_clients") == 0


async def test_a_client_registered_to_an_agent_later_retired_or_excluded_credits_no_one(db):
    retired = await _provision(db)
    excluded = await _provision(db)
    await db_execute("UPDATE agent_oauth_clients SET agent_id = 'agent_retired' WHERE client_id = :c",
                     {"c": retired["client_id"]})
    await db_execute("UPDATE agent_oauth_clients SET agent_id = 'agent_gw' WHERE client_id = :c",
                     {"c": excluded["client_id"]})
    assert await _resolve(retired["client_id"]) is None
    assert await _resolve(excluded["client_id"]) is None


async def db_execute(sql, values=None):
    from db.database import database

    return await database.execute(sql, values or {})


async def db_fetch_val(sql, values=None):
    from db.database import database

    return await database.fetch_val(sql, values or {})
