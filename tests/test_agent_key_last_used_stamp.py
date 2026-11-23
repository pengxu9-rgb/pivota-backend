"""db.agents.get_agent_by_key records the admitted key's "last used" time, OFF the auth path.

The developer portal's API keys page (and the employee portal's agent page) show
api_keys.last_used / agent_api_keys.last_used_at, but nothing wrote either column, so every key read
"Never". A key-table lookup that admits an ACTIVE agent's key now queues a stamp (put_nowait, O(1),
no DB), and one drainer task started by the app lifespan writes it under lock / statement timeouts
and an overall deadline (review of d54c5c551: the first version awaited the UPDATE inline on auth).
"""

import asyncio
import contextvars
import hashlib
from collections import OrderedDict
from datetime import datetime, timezone

import pytest

API_KEY = "ak_live_" + "7" * 64
SHA = hashlib.sha256(API_KEY.encode()).hexdigest()
MD5 = hashlib.md5(API_KEY.encode()).hexdigest()
AGENT_ROW = {"agent_id": "agent_last_used_unit", "agent_name": "Last Used Unit", "is_active": True}


class _Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.statements.append("BEGIN")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.db.statements.append("ROLLBACK" if exc_type else "COMMIT")
        return False


class _FakeDb:
    def __init__(self, *, key_table, hash_alg="sha256", agent=None, update_error=None, update_hangs=False):
        self.key_table = key_table
        self.hash_alg = hash_alg
        self.agent = dict(agent or AGENT_ROW)
        self.update_error = update_error
        self.update_hangs = update_hangs
        self.statements = []
        self.updates = []
        self.seen_context = []

    def transaction(self):
        return _Tx(self)

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "to_regclass('public.api_keys')" in q:
            return {
                "api_keys_table": "api_keys" if self.key_table == "api_keys" else None,
                "agent_api_keys_table": "agent_api_keys" if self.key_table == "agent_api_keys" else None,
            }
        if "JOIN api_keys k" in q:
            return dict(self.agent) if values.get("key_hash") == SHA else None
        if "JOIN agent_api_keys k" in q:
            return {**self.agent, "_auth_hash_alg": self.hash_alg}
        if q.startswith("SELECT * FROM agents WHERE api_key ="):
            return dict(self.agent) if values.get("api_key") == API_KEY else None
        return None

    async def execute(self, query, values=None):
        q = " ".join(str(query).split())
        self.statements.append(q)
        if q.startswith("UPDATE"):
            self.seen_context.append(_PROBE.get(None))
            if self.update_hangs:
                await asyncio.sleep(3600)
            if self.update_error:
                raise self.update_error
            self.updates.append((q, dict(values or {})))
        return None


_PROBE: "contextvars.ContextVar[str]" = contextvars.ContextVar("last_used_test_probe")


@pytest.fixture
def agents_db(monkeypatch):
    import db.agents as module

    monkeypatch.setattr(module, "IS_POSTGRES", True)
    monkeypatch.setattr(module, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    monkeypatch.setattr(module, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})
    monkeypatch.setattr(module, "_AGENT_AUTH_CACHE", OrderedDict())
    monkeypatch.setattr(module, "_KEY_LAST_USED_MEMO", OrderedDict())
    monkeypatch.setattr(module, "_key_last_used_queue", None)
    monkeypatch.setattr(module, "_key_last_used_dropped", 0)
    monkeypatch.setattr(module, "is_synthetic_call", lambda: False)
    return module


def _install(monkeypatch, module, fake):
    monkeypatch.setattr(module.database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(module.database, "execute", fake.execute)
    monkeypatch.setattr(module.database, "transaction", fake.transaction)


def _queue(monkeypatch, module, maxsize=10):
    queue = asyncio.Queue(maxsize=maxsize)
    monkeypatch.setattr(module, "_key_last_used_queue", queue)
    return queue


def _drain_now(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# ── the auth path only queues ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_auth_path_queues_a_stamp_and_writes_nothing(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)
    queue = _queue(monkeypatch, agents_db)

    agent = await agents_db.get_agent_by_key(API_KEY)

    assert agent and agent["agent_id"] == AGENT_ROW["agent_id"]
    assert fake.statements == [], "no DB write on the auth path"
    [(table, key_hash, now)] = _drain_now(queue)
    assert (table, key_hash) == ("api_keys", SHA)
    # api_keys.last_used is TIMESTAMP and routes/agent_keys.py subtracts it from utcnow().
    assert now.tzinfo is None and abs((datetime.utcnow() - now).total_seconds()) < 5


@pytest.mark.asyncio
async def test_without_a_drainer_nothing_is_queued(monkeypatch, agents_db):
    # Scripts (the redaction script calls get_agent_by_key) and tests never start one.
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)

    assert await agents_db.get_agent_by_key(API_KEY)

    assert fake.statements == [] and agents_db._KEY_LAST_USED_MEMO == OrderedDict()


@pytest.mark.asyncio
async def test_a_second_lookup_inside_the_window_queues_nothing(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)
    queue = _queue(monkeypatch, agents_db)

    await agents_db.get_agent_by_key(API_KEY)
    agents_db._AGENT_AUTH_CACHE.clear()  # force a second key-table lookup, as an expired cache would
    await agents_db.get_agent_by_key(API_KEY)

    assert len(_drain_now(queue)) == 1


@pytest.mark.asyncio
async def test_agent_api_keys_queues_the_hash_that_matched(monkeypatch, agents_db):
    for alg, expected in (("sha256", SHA), ("md5", MD5)):
        agents_db._AGENT_AUTH_CACHE.clear()
        agents_db._KEY_LAST_USED_MEMO.clear()
        agents_db._AGENT_AUTH_KEY_TABLE_CACHE.update({"table": None, "expires_at": 0.0})
        fake = _FakeDb(key_table="agent_api_keys", hash_alg=alg)
        _install(monkeypatch, agents_db, fake)
        queue = _queue(monkeypatch, agents_db)

        assert await agents_db.get_agent_by_key(API_KEY)

        [(table, key_hash, now)] = _drain_now(queue)
        assert (table, key_hash) == ("agent_api_keys", expected), alg
        assert now.tzinfo == timezone.utc, alg  # last_used_at is timestamptz


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["synthetic", "inactive_agent", "null_is_active_inactive_status", "legacy_match", "unknown_key"],
)
async def test_calls_that_are_not_a_use_queue_nothing(monkeypatch, agents_db, case):
    agent = dict(AGENT_ROW)
    key_table = "api_keys"
    key = API_KEY
    if case == "synthetic":
        monkeypatch.setattr(agents_db, "is_synthetic_call", lambda: True)
    elif case == "inactive_agent":
        agent["is_active"] = False  # get_agent_context 403s next; a refused call is not a use
    elif case == "null_is_active_inactive_status":
        agent["is_active"] = None
        agent["status"] = "suspended"
    elif case == "legacy_match":
        key_table = None  # no key table: falls back to agents.api_key, which has no per-key row
    elif case == "unknown_key":
        key = "ak_live_" + "0" * 64
    fake = _FakeDb(key_table=key_table, agent=agent)
    _install(monkeypatch, agents_db, fake)
    queue = _queue(monkeypatch, agents_db)

    await agents_db.get_agent_by_key(key)

    assert _drain_now(queue) == [], case


@pytest.mark.asyncio
async def test_a_full_queue_drops_without_memoizing_so_the_next_lookup_retries(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)
    queue = _queue(monkeypatch, agents_db, maxsize=1)
    queue.put_nowait(("api_keys", "someone-else", datetime.utcnow()))

    assert await agents_db.get_agent_by_key(API_KEY)  # auth still answers

    assert agents_db._key_last_used_dropped == 1
    assert f"api_keys:{SHA}" not in agents_db._KEY_LAST_USED_MEMO
    queue.get_nowait()
    agents_db._AGENT_AUTH_CACHE.clear()
    await agents_db.get_agent_by_key(API_KEY)
    assert [item[1] for item in _drain_now(queue)] == [SHA]


@pytest.mark.asyncio
async def test_the_memo_is_capped_and_evicts_the_oldest(monkeypatch, agents_db):
    monkeypatch.setattr(agents_db, "_KEY_LAST_USED_MEMO_MAX_ENTRIES", 3)
    _queue(monkeypatch, agents_db, maxsize=100)
    hashes = [f"hash-{i}" for i in range(5)]
    for h in hashes:
        agents_db._enqueue_key_last_used(source="api_keys", api_key="unused", key_hash_sha256=h, agent=AGENT_ROW)

    assert list(agents_db._KEY_LAST_USED_MEMO) == [f"api_keys:{h}" for h in hashes[-3:]]


# ── the drainer writes, bounded ───────────────────────────────────────────────────────────────────


async def _run_drainer(monkeypatch, agents_db, fake, items, *, deadline=None):
    _install(monkeypatch, agents_db, fake)
    if deadline is not None:
        monkeypatch.setattr(agents_db, "_KEY_LAST_USED_WRITE_DEADLINE_SECONDS", deadline)
    _PROBE.set("request-context")  # a value the drainer must NOT see (it runs in a fresh context)
    task = agents_db.start_key_last_used_drainer()
    try:
        queue = agents_db._key_last_used_queue
        for item in items:
            queue.put_nowait(item)
        await asyncio.wait_for(queue.join(), timeout=5)
    finally:
        agents_db.stop_key_last_used_drainer()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return queue


@pytest.mark.asyncio
async def test_the_drainer_writes_under_lock_and_statement_timeouts_in_a_fresh_context(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    now = datetime.utcnow()

    await _run_drainer(monkeypatch, agents_db, fake, [("api_keys", SHA, now), ("agent_api_keys", MD5, datetime.now(timezone.utc))])

    assert fake.statements[:4] == [
        "BEGIN",
        "SET LOCAL lock_timeout = '1s'",
        "SET LOCAL statement_timeout = '2s'",
        fake.updates[0][0],
    ]
    (q1, v1), (q2, v2) = fake.updates
    assert q1.startswith("UPDATE api_keys SET last_used = :now WHERE key_hash = :key_hash AND status = 'active'")
    assert "COALESCE(is_active, TRUE) = TRUE" in q2 and v2["key_hash"] == MD5
    assert v1["key_hash"] == SHA and (v1["now"] - v1["cutoff"]).total_seconds() == agents_db._KEY_LAST_USED_STAMP_SECONDS
    assert fake.seen_context == [None, None], "the drainer runs in a fresh context (its own DB connection)"


@pytest.mark.asyncio
async def test_a_failing_or_hanging_write_is_skipped_and_the_drainer_goes_on(monkeypatch, agents_db):
    failing = _FakeDb(key_table="api_keys", update_error=RuntimeError("lock timeout"))
    await _run_drainer(monkeypatch, agents_db, failing, [("api_keys", "a", datetime.utcnow()), ("api_keys", "b", datetime.utcnow())])
    assert failing.statements.count("ROLLBACK") == 2, "both items were attempted and rolled back"

    hanging = _FakeDb(key_table="api_keys", update_hangs=True)
    started = asyncio.get_running_loop().time()
    await _run_drainer(monkeypatch, agents_db, hanging, [("api_keys", "c", datetime.utcnow()), ("api_keys", "d", datetime.utcnow())], deadline=0.05)
    assert asyncio.get_running_loop().time() - started < 2, "the overall deadline cut both hung writes"
    assert len(hanging.seen_context) == 2


@pytest.mark.asyncio
async def test_stopping_the_drainer_stops_queueing(monkeypatch, agents_db):
    fake = _FakeDb(key_table="api_keys")
    _install(monkeypatch, agents_db, fake)
    task = agents_db.start_key_last_used_drainer()
    agents_db.stop_key_last_used_drainer()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await agents_db.get_agent_by_key(API_KEY)
    assert agents_db._key_last_used_queue is None and agents_db._KEY_LAST_USED_MEMO == OrderedDict()
