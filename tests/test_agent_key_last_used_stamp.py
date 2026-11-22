"""db.agents.get_agent_by_key stamps the admitted key's "last used" time.

The developer portal's API keys page (and the employee portal's agent page) show
api_keys.last_used / agent_api_keys.last_used_at, but nothing wrote either column,
so every key read "Never", including keys in daily production use. A key-table
lookup that admits a key now stamps it: best effort, at most once per window per
key per process, never for a synthetic (CI probe) call, never for a legacy
agents.api_key match, and a failing write never fails the call.
"""

import hashlib
from collections import OrderedDict
from datetime import datetime, timezone

import pytest

API_KEY = "ak_live_" + "7" * 64
SHA = hashlib.sha256(API_KEY.encode()).hexdigest()
MD5 = hashlib.md5(API_KEY.encode()).hexdigest()
AGENT_ROW = {"agent_id": "agent_last_used_unit", "agent_name": "Last Used Unit", "is_active": True}


class _FakeDb:
    def __init__(self, *, key_table, hash_alg="sha256", execute_error=None):
        self.key_table = key_table
        self.hash_alg = hash_alg
        self.execute_error = execute_error
        self.updates = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "to_regclass('public.api_keys')" in q:
            return {
                "api_keys_table": "api_keys" if self.key_table == "api_keys" else None,
                "agent_api_keys_table": "agent_api_keys" if self.key_table == "agent_api_keys" else None,
            }
        if "JOIN api_keys k" in q:
            return dict(AGENT_ROW) if values.get("key_hash") == SHA else None
        if "JOIN agent_api_keys k" in q:
            return {**AGENT_ROW, "_auth_hash_alg": self.hash_alg}
        if q.startswith("SELECT * FROM agents WHERE api_key ="):
            return dict(AGENT_ROW) if values.get("api_key") == API_KEY else None
        return None

    async def execute(self, query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("UPDATE"):
            if self.execute_error:
                raise self.execute_error
            self.updates.append((q, dict(values or {})))
        return None


@pytest.fixture
def agents_db(monkeypatch):
    import db.agents as module

    monkeypatch.setattr(module, "IS_POSTGRES", True)
    monkeypatch.setattr(module, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    monkeypatch.setattr(module, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
    monkeypatch.setattr(module, "_AGENT_AUTH_CACHE", OrderedDict())
    monkeypatch.setattr(module, "_KEY_LAST_USED_MEMO", OrderedDict())
    monkeypatch.setattr(module, "is_synthetic_call", lambda: False)
    return module


def _install(monkeypatch, module, fake):
    monkeypatch.setattr(module.database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(module.database, "execute", fake.execute)


@pytest.mark.asyncio
async def test_an_admitted_api_keys_key_is_stamped_in_naive_utc(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)

    agent = await agents_db.get_agent_by_key(API_KEY)

    assert agent and agent["agent_id"] == AGENT_ROW["agent_id"]
    assert len(fake.updates) == 1
    query, values = fake.updates[0]
    assert query.startswith("UPDATE api_keys SET last_used = :now WHERE key_hash = :key_hash")
    assert "last_used < :cutoff" in query
    assert values["key_hash"] == SHA
    # api_keys.last_used is TIMESTAMP and routes/agent_keys.py subtracts it from utcnow().
    assert values["now"].tzinfo is None
    assert abs((datetime.utcnow() - values["now"]).total_seconds()) < 5
    assert (values["now"] - values["cutoff"]).total_seconds() == agents_db._KEY_LAST_USED_STAMP_SECONDS


@pytest.mark.asyncio
async def test_a_second_lookup_inside_the_window_writes_nothing(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)

    await agents_db.get_agent_by_key(API_KEY)
    agents_db._AGENT_AUTH_CACHE.clear()  # force a second key-table lookup, as an expired cache would
    await agents_db.get_agent_by_key(API_KEY)

    assert len(fake.updates) == 1


@pytest.mark.asyncio
async def test_a_cache_hit_writes_nothing(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)

    await agents_db.get_agent_by_key(API_KEY)
    agents_db._KEY_LAST_USED_MEMO.clear()
    await agents_db.get_agent_by_key(API_KEY)  # served from the auth cache

    assert len(fake.updates) == 1


@pytest.mark.asyncio
async def test_agent_api_keys_is_stamped_with_the_hash_that_matched(monkeypatch, agents_db):
    for alg, expected in (("sha256", SHA), ("md5", MD5)):
        agents_db._AGENT_AUTH_CACHE.clear()
        agents_db._KEY_LAST_USED_MEMO.clear()
        agents_db._AGENT_AUTH_KEY_TABLE_CACHE.update({"table": None, "expires_at": 0.0})
        fake = _FakeDb(key_table="agent_api_keys", hash_alg=alg)
        _install(monkeypatch, agents_db, fake)

        assert await agents_db.get_agent_by_key(API_KEY)

        assert len(fake.updates) == 1, alg
        query, values = fake.updates[0]
        assert query.startswith("UPDATE agent_api_keys SET last_used_at = :now WHERE key_hash = :key_hash"), alg
        assert values["key_hash"] == expected, alg
        assert values["now"].tzinfo == timezone.utc, alg  # last_used_at is timestamptz


@pytest.mark.asyncio
async def test_a_failing_stamp_never_fails_the_call(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys", execute_error=RuntimeError("pool busy"))
    _install(monkeypatch, agents_db, fake)

    agent = await agents_db.get_agent_by_key(API_KEY)

    assert agent and agent["agent_id"] == AGENT_ROW["agent_id"]
    # Memoized before the write: the next lookup in the window does not retry it.
    assert f"api_keys:{SHA}" in agents_db._KEY_LAST_USED_MEMO


@pytest.mark.asyncio
async def test_a_synthetic_call_is_not_stamped(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)
    monkeypatch.setattr(agents_db, "is_synthetic_call", lambda: True)

    assert await agents_db.get_agent_by_key(API_KEY)

    assert fake.updates == []


@pytest.mark.asyncio
async def test_a_legacy_agents_api_key_match_is_not_stamped(monkeypatch, agents_db):
    fake = _FakeDb(key_table=None)  # no key table: get_agent_by_key falls back to agents.api_key
    _install(monkeypatch, agents_db, fake)

    agent = await agents_db.get_agent_by_key(API_KEY)

    assert agent and agent["agent_id"] == AGENT_ROW["agent_id"]
    assert fake.updates == []


@pytest.mark.asyncio
async def test_an_unknown_key_is_not_stamped(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)

    assert await agents_db.get_agent_by_key("ak_live_" + "0" * 64) is None

    assert fake.updates == []
