"""Both reset-api-key endpoints rotate through routes.agent_account.reset_agent_primary_api_key.

The employee portal's POST /employee/agents/{id}/reset-api-key used to run its own
`UPDATE agents SET api_key = <plaintext>, last_key_rotation = ...`. Prod's agents table has no
last_key_rotation column, so it 500'd; and had it run, the new key would not have been on the
hash auth path (api_keys) and the old key would have stayed active. Pinned here:

1. the new key's sha256 lands in the key table auth reads, and every older key is revoked;
2. agents.api_key gets the redacted marker, never the plaintext (legacy: plaintext, since that
   column IS the auth lookup there);
3. all writes run inside one transaction;
4. the rotated agent's auth-cache entries are evicted, other agents' are not;
5. unknown agent -> 404 and a failed key-table probe -> 503, both with nothing written.
"""

import hashlib
import re

import pytest
from fastapi import HTTPException

import db.agents as agents_db
import routes.agent_account as agent_account
import routes.agent_keys as agent_keys
import routes.employee_agent_mgmt as employee_agent_mgmt

AGENT_ID = "agent_659a77ae254b8f4c"
EMPLOYEE = {"role": "admin", "email": "ops@example.com"}


class _Txn:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.in_txn = True
        self.db.txn_events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.db.in_txn = False
        self.db.txn_events.append("rollback" if exc_type else "commit")
        return False


class _FakeDb:
    def __init__(self, *, key_table, agents=(AGENT_ID,), probe_error=False, other_table_present=False):
        self.key_table = key_table
        self.other_table_present = other_table_present
        self.agents = set(agents)
        self.probe_error = probe_error
        self.in_txn = False
        self.txn_events: list[str] = []
        self.executed: list[tuple[str, dict, bool]] = []

    def transaction(self):
        return _Txn(self)

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "to_regclass('public.api_keys')" in q:
            if self.probe_error:
                raise RuntimeError("connection reset during probe")
            # other_table_present: the key table auth is NOT resolving to exists too (prod's shape).
            present = {self.key_table} | ({"api_keys", "agent_api_keys"} if self.other_table_present else set())
            return {
                "api_keys_table": "api_keys" if "api_keys" in present else None,
                "agent_api_keys_table": "agent_api_keys" if "agent_api_keys" in present else None,
            }
        if q.startswith("SELECT agent_id FROM agents"):
            agent_id = (values or {}).get("agent_id")
            return {"agent_id": agent_id} if agent_id in self.agents else None
        if q.startswith("INSERT INTO api_keys") and q.endswith("RETURNING id"):
            await self.execute(query, values)
            return {"id": 41}
        raise AssertionError(f"unexpected fetch_one: {q}")

    async def fetch_all(self, query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("UPDATE") and "RETURNING" in q:  # key-row revokes report what they retired
            await self.execute(query, values)
            return [{"key_hash": "retired-hash"}]
        raise AssertionError(f"unexpected fetch_all: {q}")

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {}), self.in_txn))
        return None

    def statements(self, prefix):
        return [(v, in_txn) for q, v, in_txn in self.executed if q.startswith(prefix)]


@pytest.fixture
def install(monkeypatch):
    def _install(fake):
        monkeypatch.setattr(agent_account, "database", fake)
        monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
        monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
        monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
        return fake

    return _install


async def _employee_reset(agent_id=AGENT_ID):
    return await employee_agent_mgmt.reset_agent_api_key(agent_id, current_user=EMPLOYEE)


@pytest.mark.asyncio
async def test_employee_reset_puts_new_key_on_auth_path_and_revokes_the_old(install):
    fake = install(_FakeDb(key_table="api_keys"))

    body = await _employee_reset()

    new_key = body["new_api_key"]
    assert re.fullmatch(r"ak_live_[0-9a-f]{64}", new_key)
    new_hash = hashlib.sha256(new_key.encode()).hexdigest()

    [(agents_update, _)] = fake.statements("UPDATE agents")
    assert agents_update == {"api_key": f"redacted:{AGENT_ID}", "api_key_hash": new_hash, "agent_id": AGENT_ID}

    [(revoke, _)] = fake.statements("UPDATE api_keys SET status = 'revoked'")
    assert revoke == {"agent_id": AGENT_ID}  # every active key of this agent, not one id
    [(insert, _)] = fake.statements("INSERT INTO api_keys")
    assert insert["key_hash"] == new_hash and insert["agent_id"] == AGENT_ID
    assert not fake.statements("UPDATE agent_api_keys")  # no other table in this layout

    # Revoke runs BEFORE insert, or the new key would revoke itself.
    order = [q.split(" SET")[0].split(" (")[0] for q, _, _ in fake.executed]
    assert order.index("UPDATE api_keys") < order.index("INSERT INTO api_keys")

    for query, values, _ in fake.executed:
        assert "last_key_rotation" not in query
        assert new_key not in values.values()  # the plaintext reaches no statement


@pytest.mark.asyncio
async def test_every_write_runs_inside_one_transaction(install):
    fake = install(_FakeDb(key_table="api_keys"))

    await _employee_reset()

    assert fake.txn_events == ["begin", "commit"]
    assert fake.executed and all(in_txn for _, _, in_txn in fake.executed)


@pytest.mark.asyncio
async def test_agent_api_keys_table_branch(install):
    fake = install(_FakeDb(key_table="agent_api_keys"))

    body = await _employee_reset()

    new_hash = hashlib.sha256(body["new_api_key"].encode()).hexdigest()
    [(revoke, _)] = fake.statements("UPDATE agent_api_keys SET is_active = FALSE")
    assert revoke == {"agent_id": AGENT_ID}
    [(insert, _)] = fake.statements("INSERT INTO agent_api_keys")
    assert insert["key_hash"] == new_hash and insert["created_by"] == "employee_reset"
    [(agents_update, _)] = fake.statements("UPDATE agents")
    assert agents_update["api_key"] == f"redacted:{AGENT_ID}"


@pytest.mark.asyncio
async def test_reset_also_retires_active_keys_in_the_table_auth_is_not_reading(install):
    """Prod has both tables; auth reads api_keys, and agent_api_keys still holds active rows. They
    are dormant, not dead: pinning AGENT_AUTH_KEY_TABLE=agent_api_keys would revive them."""
    fake = install(_FakeDb(key_table="api_keys", other_table_present=True))

    await _employee_reset()

    [(retire, in_txn)] = fake.statements("UPDATE agent_api_keys SET is_active = FALSE")
    assert retire == {"agent_id": AGENT_ID} and in_txn
    assert not fake.statements("INSERT INTO agent_api_keys")  # the new key goes to auth's table only


@pytest.mark.asyncio
async def test_legacy_deployment_writes_plaintext_because_that_column_is_the_auth_lookup(install):
    fake = install(_FakeDb(key_table=None))

    body = await _employee_reset()

    [(agents_update, _)] = fake.statements("UPDATE agents")
    assert agents_update["api_key"] == body["new_api_key"]
    assert not fake.statements("UPDATE api_keys") and not fake.statements("INSERT INTO api_keys")


@pytest.mark.asyncio
async def test_unknown_agent_is_404_and_writes_nothing(install):
    fake = install(_FakeDb(key_table="api_keys", agents=()))

    with pytest.raises(HTTPException) as exc:
        await _employee_reset("agent_does_not_exist")

    assert exc.value.status_code == 404
    assert fake.executed == []


@pytest.mark.asyncio
async def test_key_table_probe_failure_is_503_and_writes_nothing(install):
    fake = install(_FakeDb(key_table="api_keys", probe_error=True))

    with pytest.raises(HTTPException) as exc:
        await _employee_reset()

    assert exc.value.status_code == 503
    assert fake.executed == []


@pytest.mark.asyncio
async def test_a_failed_write_rolls_back_and_surfaces_as_500(install):
    fake = install(_FakeDb(key_table="api_keys"))
    real_fetch_one = fake.fetch_one

    async def failing_insert(query, values=None):
        row = await real_fetch_one(query, values)
        if str(query).strip().startswith("INSERT INTO api_keys"):
            raise RuntimeError("duplicate key value violates unique constraint")
        return row

    fake.fetch_one = failing_insert

    with pytest.raises(HTTPException) as exc:
        await _employee_reset()

    assert exc.value.status_code == 500
    assert fake.txn_events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_rotation_evicts_only_the_rotated_agents_auth_cache(install, monkeypatch):
    install(_FakeDb(key_table="api_keys"))
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_CACHE", agents_db.OrderedDict())
    agents_db._put_cached_agent_auth("old-key-hash", {"agent_id": AGENT_ID})
    agents_db._put_cached_agent_auth("other-key-hash", {"agent_id": "agent_other"})

    await _employee_reset()

    assert agents_db._get_cached_agent_auth("old-key-hash") == (False, None)
    assert agents_db._get_cached_agent_auth("other-key-hash") == (True, {"agent_id": "agent_other"})


@pytest.mark.asyncio
async def test_employee_reset_refuses_non_staff(install):
    fake = install(_FakeDb(key_table="api_keys"))

    with pytest.raises(HTTPException) as exc:
        await employee_agent_mgmt.reset_agent_api_key(AGENT_ID, current_user={"role": "agent", "agent_id": AGENT_ID})

    assert exc.value.status_code == 403
    assert fake.executed == []


@pytest.mark.asyncio
async def test_agent_portal_reset_shares_the_path_and_no_longer_stores_plaintext(install):
    fake = install(_FakeDb(key_table="api_keys"))

    body = await agent_keys.reset_agent_api_key(AGENT_ID, current_user={"agent_id": AGENT_ID, "role": "agent"})

    assert body["api_key"] == body["new_api_key"]
    assert body["key_sync_source"] == "api_keys"
    [(agents_update, _)] = fake.statements("UPDATE agents")
    assert agents_update["api_key"] == f"redacted:{AGENT_ID}"
    [(insert, _)] = fake.statements("INSERT INTO api_keys")
    assert insert["key_hash"] == hashlib.sha256(body["api_key"].encode()).hexdigest()


@pytest.mark.asyncio
async def test_a_failed_key_table_inventory_is_503_and_writes_nothing(install):
    """_existing_key_tables probes after the resolver: its failure is 'retry', not a bare 500."""
    fake = install(_FakeDb(key_table="api_keys"))
    probes = {"n": 0}
    real_fetch_one = fake.fetch_one

    async def second_probe_fails(query, values=None):
        if "to_regclass('public.api_keys')" in str(query):
            probes["n"] += 1
            if probes["n"] == 2:
                raise RuntimeError("connection reset during probe")
        return await real_fetch_one(query, values)

    fake.fetch_one = second_probe_fails

    with pytest.raises(HTTPException) as exc:
        await _employee_reset()

    assert exc.value.status_code == 503
    assert fake.executed == []


@pytest.mark.asyncio
async def test_off_postgres_reset_skips_the_key_table_inventory(install, monkeypatch):
    """No to_regclass off Postgres: the resolver already answers 'no key table' there."""
    fake = install(_FakeDb(key_table="api_keys"))
    monkeypatch.setattr(agents_db, "IS_POSTGRES", False)

    body = await _employee_reset()

    [(agents_update, _)] = fake.statements("UPDATE agents")
    assert agents_update["api_key"] == body["new_api_key"]  # legacy: the column is the auth lookup
    assert not fake.statements("UPDATE api_keys") and not fake.statements("UPDATE agent_api_keys")
