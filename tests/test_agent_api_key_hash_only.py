"""Agent API keys are persisted hash-only and shown once.

Before this change `agents.api_key` held the live key in plaintext and
POST /agent/account/login returned it on every login, so the registration
promise "it won't be shown again" was false and a DB read yielded usable
credentials. Three contracts pinned here:

1. register: a key table exists ⇒ agents.api_key gets the redacted marker, the
   sha256 goes to the key table, the plaintext appears ONLY in the response.
   No key table (legacy) ⇒ plaintext is still written, because that column IS
   the auth lookup there.
2. login: never returns the key; legacy plaintext rows still get their hash
   backfilled onto the auth path; redacted rows do not trigger a backfill.
3. the analytics/metrics resolvers go through the hash lookup
   (db.agents.get_agent_by_key), not `WHERE api_key = :key`.
"""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _account_client(monkeypatch):
    import routes.agent_account as module

    monkeypatch.setattr(module, "_REGISTRATION_IP_LIMIT_STORE", {})
    monkeypatch.delenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL", raising=False)
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app), module


def _register_body():
    return {"email": "hash-only@example.com", "password": "longenough1", "agent_name": "Hash Only"}


class _FakeDb:
    """Records every execute(); answers fetch_one by query shape."""

    def __init__(self, *, key_table: str | None, probe_error: bool = False):
        self.key_table = key_table
        self.probe_error = probe_error
        self.executed: list[tuple[str, dict]] = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "to_regclass('public.api_keys')" in q:
            if self.probe_error:
                raise RuntimeError("connection reset during probe")
            return {
                "api_keys_table": "api_keys" if self.key_table == "api_keys" else None,
                "agent_api_keys_table": "agent_api_keys" if self.key_table == "agent_api_keys" else None,
            }
        if q.startswith("SELECT id FROM users"):
            return None  # not registered yet
        if q.startswith("INSERT INTO users"):
            self.executed.append((q, dict(values or {})))
            return {"id": 77}
        return None

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))
        return None


def _install_fake_db(monkeypatch, module, fake):
    import db.agents as agents_db

    monkeypatch.setattr(module.database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(module.database, "execute", fake.execute)
    # The key-table resolver is Postgres-gated and env-gated; pin both so the probe runs.
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})

    async def _no_membership(**_kwargs):
        return None

    monkeypatch.setattr(module, "_sync_agent_auth_membership", _no_membership)


# ── 1. register ───────────────────────────────────────────────────────────────

def test_register_with_key_table_stores_redacted_marker_and_hash_only(monkeypatch):
    client, module = _account_client(monkeypatch)
    fake = _FakeDb(key_table="api_keys")
    _install_fake_db(monkeypatch, module, fake)

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    api_key = body["api_key"]
    assert api_key.startswith("ak_live_") and len(api_key) == len("ak_live_") + 64
    agent_id = body["agent_id"]

    agents_insert = [v for q, v in fake.executed if q.startswith("INSERT INTO agents")]
    assert len(agents_insert) == 1
    assert agents_insert[0]["api_key"] == f"redacted:{agent_id}"
    assert agents_insert[0]["api_key_hash"] == hashlib.sha256(api_key.encode()).hexdigest()

    key_inserts = [v for q, v in fake.executed if q.startswith("INSERT INTO api_keys")]
    assert len(key_inserts) == 1
    assert key_inserts[0]["key_hash"] == hashlib.sha256(api_key.encode()).hexdigest()

    # The plaintext reaches the database in NO statement at all.
    for _q, values in fake.executed:
        assert api_key not in values.values()


def test_register_without_key_table_keeps_plaintext_for_legacy_auth(monkeypatch):
    client, module = _account_client(monkeypatch)
    fake = _FakeDb(key_table=None)
    _install_fake_db(monkeypatch, module, fake)

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 200, resp.text
    api_key = resp.json()["api_key"]
    agents_insert = [v for q, v in fake.executed if q.startswith("INSERT INTO agents")]
    assert agents_insert[0]["api_key"] == api_key
    assert not any(q.startswith("INSERT INTO api_keys") for q, _ in fake.executed)


def test_register_fails_closed_if_key_table_disappears_mid_flight(monkeypatch):
    """Redacted column + no hash row = an agent that can never authenticate. Refuse instead."""
    client, module = _account_client(monkeypatch)
    fake = _FakeDb(key_table="api_keys")
    _install_fake_db(monkeypatch, module, fake)

    async def _sync_returns_legacy(**_kwargs):
        return "legacy"

    monkeypatch.setattr(module, "_sync_new_agent_api_key", _sync_returns_legacy)

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 500
    # and the partial rows were cleaned up
    assert any(q.startswith("DELETE FROM agents") for q, _ in fake.executed)


def test_register_probe_error_is_503_and_writes_nothing(monkeypatch):
    """A transient probe failure must not GUESS where the key lives (plaintext vs hash) — and must
    leave no orphan users row that would block the email from ever registering."""
    client, module = _account_client(monkeypatch)
    fake = _FakeDb(key_table="api_keys", probe_error=True)
    _install_fake_db(monkeypatch, module, fake)

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 503, resp.text
    assert fake.executed == []


def test_register_honours_AGENT_AUTH_KEY_TABLE_rollback_lever(monkeypatch):
    """legacy_only ⇒ the auth path reads agents.api_key ⇒ the plaintext must be written there, even
    though the api_keys table physically exists."""
    client, module = _account_client(monkeypatch)
    import db.agents as agents_db

    fake = _FakeDb(key_table="api_keys")
    _install_fake_db(monkeypatch, module, fake)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "legacy_only")

    resp = client.post("/agent/account/register", json=_register_body())

    assert resp.status_code == 200, resp.text
    api_key = resp.json()["api_key"]
    agents_insert = [v for q, v in fake.executed if q.startswith("INSERT INTO agents")]
    assert agents_insert[0]["api_key"] == api_key
    assert not any(q.startswith("INSERT INTO api_keys") for q, _ in fake.executed)


def test_register_cleanup_deletes_only_the_user_row_it_created(monkeypatch):
    client, module = _account_client(monkeypatch)
    fake = _FakeDb(key_table="api_keys")
    _install_fake_db(monkeypatch, module, fake)

    async def boom(**_kwargs):
        raise RuntimeError("key table write failed")

    monkeypatch.setattr(module, "_sync_new_agent_api_key", boom)
    resp = client.post("/agent/account/register", json=_register_body())
    assert resp.status_code == 500
    deletes = [(q, v) for q, v in fake.executed if q.startswith("DELETE FROM users")]
    assert len(deletes) == 1
    assert "WHERE id = :id" in deletes[0][0] and deletes[0][1]["id"] == 77
    assert "email" not in deletes[0][1]


# ── 1b. the redaction marker is never a credential ───────────────────────────

@pytest.mark.asyncio
async def test_redacted_marker_is_refused_as_a_presented_key_without_touching_the_db(monkeypatch):
    import db.agents as agents_db

    async def forbidden(*_a, **_k):
        raise AssertionError("no DB lookup may run for a redacted marker")

    monkeypatch.setattr(agents_db.database, "fetch_one", forbidden)
    metrics = {}
    assert await agents_db.get_agent_by_key("redacted:agent_abc", metrics) is None
    assert metrics["auth_source"] == "redacted_marker_refused"
    assert await agents_db.resolve_agent_id_by_api_key("redacted:agent_abc") is None


def test_legacy_plaintext_lookups_exclude_the_marker_at_the_sql_level():
    import db.agents as agents_db

    assert "api_key NOT LIKE 'redacted:%'" in agents_db._LEGACY_API_KEY_LOOKUP_SQL
    import inspect

    src = inspect.getsource(agents_db.get_agent_by_key)
    assert "SELECT * FROM agents WHERE api_key = :api_key LIMIT 1" not in src
    assert src.count("_LEGACY_API_KEY_LOOKUP_SQL") == 2


def test_api_keys_legacy_branch_never_hashes_the_marker(monkeypatch):
    """routes/agent_keys.py surfaces agents.api_key when no api_keys rows exist — and used to hash it
    into api_keys. For a redacted row that would mint `redacted:<agent_id>` as a live credential."""
    import routes.agent_keys as keys_module
    from utils.auth import get_current_user

    executed = []

    async def fetch_all(query, values=None):
        return []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if "FROM agents" in q:
            return {"agent_id": "agent_r", "agent_name": "R", "owner_email": "r@x.io", "api_key": "redacted:agent_r", "api_key_hash": None, "created_at": None}
        return None

    async def execute(query, values=None):
        executed.append((" ".join(str(query).split()), dict(values or {})))

    monkeypatch.setattr(keys_module.database, "fetch_all", fetch_all)
    monkeypatch.setattr(keys_module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(keys_module.database, "execute", execute)
    app = FastAPI()
    app.include_router(keys_module.router)
    app.dependency_overrides[get_current_user] = lambda: {"agent_id": "agent_r", "role": "agent"}
    resp = TestClient(app).get("/agents/agent_r/api-keys")
    assert resp.status_code == 200, resp.text
    assert resp.json()["keys"] == []
    assert not any(q.startswith("INSERT INTO api_keys") for q, _ in executed)
    assert "redacted" not in resp.text


# ── 1c. metrics endpoints fail CLOSED on an unresolvable key ─────────────────

def test_recent_activity_endpoints_401_on_an_unresolvable_key_instead_of_dumping_everyone(monkeypatch):
    import db.agents as agents_db
    import routes.agent_metrics as metrics
    import routes.agent_metrics_v1 as metrics_v1

    async def no_agent(_key):
        return None

    async def fetch_all(query, values=None):
        raise AssertionError(f"the activity query must not run: {' '.join(str(query).split())[:80]}")

    monkeypatch.setattr(agents_db, "get_agent_by_key", lambda k, metrics_out=None: _none())
    monkeypatch.setattr(metrics.database, "fetch_all", fetch_all)
    monkeypatch.setattr(metrics_v1.database, "fetch_all", fetch_all)
    app = FastAPI()
    app.include_router(metrics.router)
    app.include_router(metrics_v1.router)
    client = TestClient(app)
    r1 = client.get("/agent/metrics/recent", headers={"x-api-key": "ak_live_" + "9" * 64})
    r2 = client.get("/agent/v1/metrics/recent", headers={"x-api-key": "ak_live_" + "9" * 64})
    assert r1.status_code == 401, r1.text
    assert r2.status_code == 401, r2.text


async def _none():
    return None


# ── 2. login ──────────────────────────────────────────────────────────────────

def _login_setup(monkeypatch, module, *, stored_api_key: str):
    from utils.auth import hash_password

    executed: list[tuple[str, dict]] = []

    async def fetch_one(query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("SELECT id, email, password_hash"):
            return {
                "id": 7,
                "email": "hash-only@example.com",
                "password_hash": hash_password("longenough1"),
                "full_name": "Hash Only",
                "role": "agent",
                "active": True,
            }
        if "FROM agents" in q and "owner_email" in q:
            return {
                "agent_id": "agent_abc",
                "agent_name": "Hash Only",
                "owner_email": "hash-only@example.com",
                "api_key": stored_api_key,
                "agent_type": "basic",
            }
        if "to_regclass('public.api_keys')" in q:
            return {"api_keys_table": "api_keys", "agent_api_keys_table": None}
        if q.startswith("SELECT id, status FROM api_keys"):
            return None
        return None

    async def execute(query, values=None):
        executed.append((" ".join(str(query).split()), dict(values or {})))

    monkeypatch.setattr(module.database, "fetch_one", fetch_one)
    monkeypatch.setattr(module.database, "execute", execute)
    import db.agents as agents_db

    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")

    async def _no_membership(**_kwargs):
        return None

    monkeypatch.setattr(module, "_sync_agent_auth_membership", _no_membership)
    return executed


def test_login_never_returns_api_key_for_redacted_row(monkeypatch):
    client, module = _account_client(monkeypatch)
    executed = _login_setup(monkeypatch, module, stored_api_key="redacted:agent_abc")

    resp = client.post("/agent/account/login", json={"email": "hash-only@example.com", "password": "longenough1"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["api_key"] is None
    assert "redacted:" not in resp.text
    # a redacted row is not a key: no backfill into api_keys
    assert not any(q.startswith("INSERT INTO api_keys") for q, _ in executed)


def test_login_backfills_legacy_plaintext_row_but_still_does_not_return_it(monkeypatch):
    client, module = _account_client(monkeypatch)
    legacy_key = "ak_live_" + "c" * 64
    executed = _login_setup(monkeypatch, module, stored_api_key=legacy_key)

    resp = client.post("/agent/account/login", json={"email": "hash-only@example.com", "password": "longenough1"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["api_key"] is None
    assert legacy_key not in resp.text
    inserts = [v for q, v in executed if q.startswith("INSERT INTO api_keys")]
    assert len(inserts) == 1
    assert inserts[0]["key_hash"] == hashlib.sha256(legacy_key.encode()).hexdigest()


def test_login_does_not_require_a_plaintext_key_to_exist(monkeypatch):
    """Before: 403 'Agent API key is unavailable' when the column was empty."""
    client, module = _account_client(monkeypatch)
    _login_setup(monkeypatch, module, stored_api_key="")

    resp = client.post("/agent/account/login", json={"email": "hash-only@example.com", "password": "longenough1"})

    assert resp.status_code == 200, resp.text


# ── 3. resolvers ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_agent_id_by_api_key_uses_hash_lookup(monkeypatch):
    import db.agents as agents_db

    seen = {}

    async def fake_get_agent_by_key(api_key, metrics_out=None):
        seen["key"] = api_key
        return {"agent_id": "agent_from_hash"}

    monkeypatch.setattr(agents_db, "get_agent_by_key", fake_get_agent_by_key)

    assert await agents_db.resolve_agent_id_by_api_key("ak_live_x") == "agent_from_hash"
    assert seen["key"] == "ak_live_x"
    assert await agents_db.resolve_agent_id_by_api_key("") is None


@pytest.mark.asyncio
async def test_analytics_and_metrics_resolvers_no_longer_match_plaintext(monkeypatch):
    """A redacted agents.api_key must not break X-API-Key callers of analytics/metrics.

    Mutant killed: restoring `SELECT agent_id FROM agents WHERE api_key = :key` in either
    module would make this fetch_one fire and the resolver answer None → 401.
    """
    import routes.agent_analytics as analytics
    import routes.agent_metrics as metrics
    import db.agents as agents_db

    async def forbidden_fetch_one(query, values=None):
        raise AssertionError(f"plaintext lookup ran: {' '.join(str(query).split())[:60]}")

    async def fake_get_agent_by_key(api_key, metrics_out=None):
        return {"agent_id": "agent_hashed"} if api_key == "ak_live_good" else None

    monkeypatch.setattr(agents_db, "get_agent_by_key", fake_get_agent_by_key)
    monkeypatch.setattr(analytics.database, "fetch_one", forbidden_fetch_one)
    monkeypatch.setattr(metrics.database, "fetch_one", forbidden_fetch_one)

    assert await analytics.resolve_agent_id(authorization=None, x_api_key="ak_live_good") == "agent_hashed"
    assert await agents_db.resolve_agent_id_by_api_key("ak_live_bad") is None
    # metrics module imports the same symbol by name
    assert metrics.resolve_agent_id_by_api_key is agents_db.resolve_agent_id_by_api_key
    import inspect

    assert "WHERE api_key = :key" not in inspect.getsource(analytics)
    assert "WHERE api_key = :k" not in inspect.getsource(metrics)
    assert "WHERE api_key = :key" not in inspect.getsource(metrics)
