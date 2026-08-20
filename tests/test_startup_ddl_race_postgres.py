"""
First boot against an EMPTY Postgres must be deterministic.

Observed 2026-08-19 on Cloud Run (pivota-staging, fresh Cloud SQL PG17):
``asyncpg.exceptions.UniqueViolationError: duplicate key value violates unique
constraint "pg_type_typname_nsp_index"`` raised from
``ensure_agent_webhook_tables()`` — two sessions ran the same
``CREATE TABLE IF NOT EXISTS`` at once; IF NOT EXISTS is not atomic across
sessions and the loser dies on the pg_catalog unique index.

Two layers, each with its own mutant:

* ``db.startup_ddl.startup_ddl_lock`` — advisory lock around the startup DDL
  phase. Mutant: make the lock a no-op → ``test_lock_serializes_raw_ddl_racers``
  fails (4 racers × 10 rounds; the unlocked race reproduces 10/10 locally).
* ``db.startup_ddl.execute_ddl`` — classifies "already exists" so a lost race
  is success. Mutant: swap it for a plain ``database.execute`` in the ensure_*
  helpers → ``test_ensure_helpers_tolerate_lost_race`` fails.

The Postgres-backed tests run only when ``PIVOTA_PG_TEST_URL`` points at a
Postgres the test may create/drop databases on (locally: the brew postgresql@15
on 127.0.0.1:55433 — see db/startup_ddl.py docstring / memory notes). They
create a throwaway DATABASE per test so no shared schema is touched.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import uuid
from typing import Any, Awaitable, Callable, List, Optional

import pytest

import db.startup_ddl as startup_ddl
from db.startup_ddl import execute_ddl, is_already_exists_error, startup_ddl_lock

# DATABASE_URL is what .github/workflows/postgres-dialect-gate.yml sets (it
# discovers every tests/test_*_postgres.py, runs it against a real Postgres and
# FAILS the job on any skip). PIVOTA_PG_TEST_URL stays as a local override so a
# developer can point these at a throwaway cluster without touching the URL the
# rest of the suite imports.
PG_TEST_URL = (os.getenv("PIVOTA_PG_TEST_URL") or os.getenv("DATABASE_URL") or "").strip()
requires_pg = pytest.mark.skipif(
    not PG_TEST_URL.startswith("postgres"),
    reason="needs a Postgres DATABASE_URL (or PIVOTA_PG_TEST_URL) — these gates race real sessions",
)

RACERS = 4
ROUNDS = 10


# ---------------------------------------------------------------------------
# Pure-unit: error classification + execute_ddl + memoized ensure helpers
# ---------------------------------------------------------------------------


class _PgError(Exception):
    """Stand-in for an asyncpg/psycopg2 error carrying a SQLSTATE."""

    def __init__(self, msg: str, sqlstate: str, constraint_name: str = "") -> None:
        super().__init__(msg)
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


class _Wrapped(Exception):
    """Stand-in for sqlalchemy.exc.DBAPIError (driver error in .orig)."""

    def __init__(self, orig: BaseException) -> None:
        super().__init__(str(orig))
        self.orig = orig


def test_is_already_exists_error_classification() -> None:
    # The exact failure seen on Cloud Run: lost CREATE TABLE race on pg_type.
    assert is_already_exists_error(
        _PgError(
            'duplicate key value violates unique constraint "pg_type_typname_nsp_index"',
            "23505",
            "pg_type_typname_nsp_index",
        )
    )
    # Same thing surfaced via pg_class, via constraint name only (no text match).
    assert is_already_exists_error(_PgError("dup", "23505", "pg_class_relname_nsp_index"))
    # Wrapped by SQLAlchemy (metadata.create_all path).
    assert is_already_exists_error(
        _Wrapped(_PgError("dup", "23505", "pg_type_typname_nsp_index"))
    )
    # duplicate_table / duplicate_object / duplicate_column sqlstates.
    for state in ("42P07", "42710", "42701", "42P06"):
        assert is_already_exists_error(_PgError("exists", state)), state
    # A unique violation on a USER table is a real error — must NOT be swallowed.
    assert not is_already_exists_error(
        _PgError(
            'duplicate key value violates unique constraint "agent_webhook_configs_agent_id_key"',
            "23505",
            "agent_webhook_configs_agent_id_key",
        )
    )
    # Unrelated sqlstate with "already exists"-ish wording is NOT swallowed.
    assert not is_already_exists_error(_PgError("relation x already exists", "42P01"))
    assert not is_already_exists_error(RuntimeError("boom"))
    # No sqlstate at all (sqlite in hermetic tests): text fallback only.
    assert is_already_exists_error(Exception("table race_t already exists"))


class _FakeDb:
    def __init__(self, fail_with: Optional[BaseException] = None, fail_on: int = 0) -> None:
        self.calls: List[str] = []
        self._fail_with = fail_with
        self._fail_on = fail_on

    async def execute(self, query: str, values: Any = None) -> None:
        self.calls.append(" ".join(str(query).split()))
        if self._fail_with is not None and len(self.calls) - 1 == self._fail_on:
            raise self._fail_with


@pytest.mark.asyncio
async def test_execute_ddl_swallows_lost_race_and_reraises_real_errors() -> None:
    lost = _PgError("dup", "23505", "pg_type_typname_nsp_index")
    db = _FakeDb(fail_with=lost)
    assert await execute_ddl("CREATE TABLE IF NOT EXISTS t (id int)", db=db) is False
    assert db.calls == ["CREATE TABLE IF NOT EXISTS t (id int)"]

    real = _PgError("permission denied", "42501")
    with pytest.raises(_PgError):
        await execute_ddl("CREATE TABLE IF NOT EXISTS t (id int)", db=_FakeDb(fail_with=real))


@pytest.mark.asyncio
async def test_ensure_agent_webhook_tables_lost_race_is_success_and_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.agent_webhook_service as module

    monkeypatch.setattr(module, "_AGENT_WEBHOOK_DDL_READY", False)
    # Second statement (agent_webhook_deliveries) loses the race.
    fake = _FakeDb(fail_with=_PgError("dup", "23505", "pg_type_typname_nsp_index"), fail_on=1)
    monkeypatch.setattr(module, "database", fake)

    await module.ensure_agent_webhook_tables()  # must not raise (pre-fix: crashed boot)
    assert len(fake.calls) == len(module._AGENT_WEBHOOK_DDL_STATEMENTS)
    assert module._AGENT_WEBHOOK_DDL_READY is True

    # Memoized: lazy callers (routes / retry workers) issue no more DDL.
    await module.ensure_agent_webhook_tables()
    assert len(fake.calls) == len(module._AGENT_WEBHOOK_DDL_STATEMENTS)


@pytest.mark.asyncio
async def test_ensure_merchant_webhook_tables_lost_race_is_success_and_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.merchant_webhook_service as module

    monkeypatch.setattr(module, "_MERCHANT_WEBHOOK_DDL_READY", False)
    fake = _FakeDb(fail_with=_PgError("dup", "23505", "pg_class_relname_nsp_index"), fail_on=0)
    monkeypatch.setattr(module, "database", fake)

    await module.ensure_merchant_webhook_tables()
    assert len(fake.calls) == len(module._MERCHANT_WEBHOOK_DDL_STATEMENTS)
    assert module._MERCHANT_WEBHOOK_DDL_READY is True
    await module.ensure_merchant_webhook_tables()
    assert len(fake.calls) == len(module._MERCHANT_WEBHOOK_DDL_STATEMENTS)


@pytest.mark.asyncio
async def test_ensure_helpers_do_not_memoize_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.agent_webhook_service as module

    monkeypatch.setattr(module, "_AGENT_WEBHOOK_DDL_READY", False)
    fake = _FakeDb(fail_with=_PgError("permission denied", "42501"), fail_on=0)
    monkeypatch.setattr(module, "database", fake)
    with pytest.raises(_PgError):
        await module.ensure_agent_webhook_tables()
    assert module._AGENT_WEBHOOK_DDL_READY is False


@pytest.mark.asyncio
async def test_startup_ddl_lock_is_noop_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """held=False is the degraded default of every path, so assert the stronger
    thing: a disconnected/sqlite database must not even TRY to open a lock
    connection (dev and the hermetic sqlite suites boot through here)."""
    import asyncpg

    attempts = []

    async def _forbidden_connect(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("asyncpg.connect() attempted with no usable database")

    monkeypatch.setattr(asyncpg, "connect", _forbidden_connect)

    class _Disconnected:
        is_connected = False
        url = None

    monkeypatch.setattr(startup_ddl, "database", _Disconnected())
    async with startup_ddl_lock() as held:
        assert held is False

    class _Sqlite:
        is_connected = True

        class url:
            scheme = "sqlite+aiosqlite"

            def __str__(self) -> str:
                return "sqlite+aiosqlite:///./pivota.db"

    monkeypatch.setattr(startup_ddl, "database", _Sqlite())
    async with startup_ddl_lock() as held:
        assert held is False

    assert attempts == [], "a lock connection was attempted on a non-Postgres database"


# ---------------------------------------------------------------------------
# Real Postgres: the race itself
# ---------------------------------------------------------------------------


def _spawn_isolated(coro: Awaitable[Any]) -> "asyncio.Task[Any]":
    # Fresh contextvars.Context => databases==0.7.0 gives each task its own
    # Connection, i.e. a genuinely separate Postgres session (as a second
    # Cloud Run instance or a background worker would be).
    loop = asyncio.get_running_loop()
    return contextvars.Context().run(loop.create_task, coro)


@pytest.fixture
async def fresh_pg_database():
    """Yields a connected databases.Database on a brand-new, empty DATABASE."""
    import asyncpg
    from databases import Database
    from sqlalchemy.engine.url import make_url

    base = make_url(PG_TEST_URL)
    admin_url = str(base)
    name = f"ddlrace_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(admin_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    fresh_url = str(base.set(database=name))
    db = Database(fresh_url, min_size=1, max_size=2 * RACERS + 2)
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()
        admin = await asyncpg.connect(admin_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _race(
    racers: List[Callable[[], Awaitable[None]]],
) -> List[BaseException]:
    """Start all racers behind one gate so they hit the server together."""
    gate = asyncio.Event()

    async def _run(fn: Callable[[], Awaitable[None]]) -> None:
        await gate.wait()
        await fn()

    tasks = [_spawn_isolated(_run(fn)) for fn in racers]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, BaseException)]


RAW_DDL = "CREATE TABLE IF NOT EXISTS race_t (id SERIAL PRIMARY KEY, v TEXT)"


def _strict_ensure_quotes(quotes_module):
    """ensure_quotes_table() swallows every exception by design ("best-effort"),
    so a lost race would be invisible. Assert on its memo instead: it is only
    set when every statement got through."""

    async def _run() -> None:
        await quotes_module.ensure_quotes_table()
        assert quotes_module._QUOTES_DDL_READY is True, (
            "ensure_quotes_table() gave up (a lost CREATE race was not tolerated)"
        )

    return _run


@requires_pg
@pytest.mark.asyncio
async def test_lock_serializes_raw_ddl_racers(fresh_pg_database, monkeypatch) -> None:
    """RACERS sessions each run the SAME intolerant CREATE TABLE IF NOT EXISTS
    under startup_ddl_lock(). Without the lock this is a 10/10 UniqueViolation
    on pg_class/pg_type (measured); with it, zero."""
    db = fresh_pg_database
    monkeypatch.setattr(startup_ddl, "database", db)

    async def boot() -> None:
        async with startup_ddl_lock() as held:
            assert held is True, "advisory lock was not acquired"
            await db.execute(RAW_DDL)

    failures = 0
    first: Optional[BaseException] = None
    for _ in range(ROUNDS):
        await db.execute("DROP TABLE IF EXISTS race_t")
        errs = await _race([boot] * RACERS)
        if errs:
            failures += 1
            first = first or errs[0]
    assert failures == 0, f"{failures}/{ROUNDS} rounds raced; first error: {first!r}"


@requires_pg
@pytest.mark.asyncio
async def test_unlocked_raw_ddl_does_race(fresh_pg_database) -> None:
    """Control: proves the harness can observe the race at all (otherwise the
    test above would pass vacuously)."""
    db = fresh_pg_database

    async def boot() -> None:
        await db.execute(RAW_DDL)

    seen = 0
    for _ in range(ROUNDS):
        await db.execute("DROP TABLE IF EXISTS race_t")
        errs = await _race([boot] * RACERS)
        if errs:
            seen += 1
            assert is_already_exists_error(errs[0]), repr(errs[0])
    assert seen > 0, "harness never observed the CREATE TABLE IF NOT EXISTS race"


@requires_pg
@pytest.mark.asyncio
async def test_ensure_helpers_tolerate_lost_race(fresh_pg_database, monkeypatch) -> None:
    """No lock at all: RACERS concurrent ensure_agent_webhook_tables() and
    ensure_merchant_webhook_tables() on an empty DB must all succeed because a
    lost CREATE race is classified as 'already exists'."""
    import db.quotes as quotes_module
    import services.agent_webhook_service as aws
    import services.merchant_webhook_service as mws

    db = fresh_pg_database
    monkeypatch.setattr(aws, "database", db)
    monkeypatch.setattr(mws, "database", db)
    monkeypatch.setattr(quotes_module, "database", db)
    tables = (
        "agent_webhook_configs",
        "agent_webhook_deliveries",
        "agent_webhook_managed_inbox_events",
        "merchant_webhook_configs",
        "merchant_webhook_deliveries",
        "quotes",
    )

    failures = 0
    first: Optional[BaseException] = None
    for _ in range(ROUNDS):
        for t in tables:
            await db.execute(f"DROP TABLE IF EXISTS {t}")
        monkeypatch.setattr(aws, "_AGENT_WEBHOOK_DDL_READY", False)
        monkeypatch.setattr(mws, "_MERCHANT_WEBHOOK_DDL_READY", False)
        monkeypatch.setattr(quotes_module, "_QUOTES_DDL_READY", False)
        errs = await _race(
            [aws.ensure_agent_webhook_tables] * RACERS
            + [mws.ensure_merchant_webhook_tables] * RACERS
            + [_strict_ensure_quotes(quotes_module)] * RACERS
        )
        if errs:
            failures += 1
            first = first or errs[0]
    assert failures == 0, f"{failures}/{ROUNDS} rounds raised; first: {first!r}"
    assert aws._AGENT_WEBHOOK_DDL_READY and mws._MERCHANT_WEBHOOK_DDL_READY
    for t in tables:
        assert await db.fetch_val(f"SELECT to_regclass('{t}') IS NOT NULL") is True


@requires_pg
@pytest.mark.asyncio
async def test_lock_times_out_and_boots_unlocked(fresh_pg_database, monkeypatch) -> None:
    """A stuck sibling must not block boot forever: with lock_timeout elapsed
    the context yields held=False and the caller proceeds."""
    import asyncpg

    db = fresh_pg_database
    monkeypatch.setattr(startup_ddl, "database", db)

    # Hold the same advisory key from an outside session.
    holder = await asyncpg.connect(str(db.url).replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY)

        # Bounded: without this, a regression that drops the deadline check
        # turns this gate into a ~6-minute hang instead of a fast red.
        async def _boot_under_contention() -> bool:
            async with startup_ddl_lock(timeout_seconds=0.5) as held:
                # The contract is "boot continues", not just "the flag is
                # False" — so do the DDL the caller would have done.
                await db.execute("CREATE TABLE IF NOT EXISTS t_unlocked_boot (id int)")
                return held

        held = await asyncio.wait_for(_boot_under_contention(), timeout=30)
        assert held is False
        assert await db.fetch_val("SELECT to_regclass('t_unlocked_boot') IS NOT NULL") is True
    finally:
        await holder.close()

    # And once it is free again, we do acquire it.
    async with startup_ddl_lock(timeout_seconds=5) as held:
        assert held is True


# ---------------------------------------------------------------------------
# main.py wiring — AST check (importing main is too heavy for a unit test).
# Kills the mutant "DDL moved back outside the advisory lock".
# ---------------------------------------------------------------------------


def _main_ast():
    import ast

    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _find_async_def(tree, name):
    import ast

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"main.py has no async def {name}")


def _calls_named(node, name, *, receiver=None):
    """Calls to `name` inside `node`.

    `receiver` pins the object the method is called ON. Without it these gates
    match by method name alone, and

        _decoy = asyncio.Lock()
        await _decoy.acquire()

    satisfies "startup() acquires a lock" while reintroducing the exact
    instance-vs-instance race this PR exists to close — a per-process
    asyncio.Lock serializes nothing across Cloud Run instances.
    """
    import ast

    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            label = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if label != name:
                continue
            if receiver is not None:
                if not isinstance(fn, ast.Attribute):
                    continue
                if not (isinstance(fn.value, ast.Name) and fn.value.id == receiver):
                    continue
            out.append(sub)
    return out


def _has_kwarg(call, name):
    return any(kw.arg == name for kw in call.keywords)


# Every DDL call startup_event() makes. `ensure_promotions_table` is
# deliberately absent: the promotions lane was deleted (ADR-022).
_STARTUP_EVENT_DDL_CALLS = (
    "create_all",
    "ensure_quotes_table",
    "ensure_webhook_events_table",
    "ensure_agent_webhook_tables",
    "ensure_merchant_webhook_tables",
)


def _finally_release_lines(fn):
    import ast

    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            for fb in node.finalbody:
                out.extend(_calls_named(fb, "release", receiver="_startup_ddl_lock"))
    return out or [type("X", (), {"lineno": 10**9})()]


def test_startup_event_runs_its_ddl_inside_startup_ddl_lock() -> None:
    import ast

    fn = _find_async_def(_main_ast(), "startup_event")
    withs = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.AsyncWith)
        and any(_calls_named(item.context_expr, "startup_ddl_lock") for item in n.items)
    ]
    for w in withs:
        for item in w.items:
            for call in _calls_named(item.context_expr, "startup_ddl_lock"):
                assert not _has_kwarg(call, "timeout_seconds"), (
                    "startup_event() pins a timeout_seconds on the lock; a 0 there "
                    "makes every boot continue UNLOCKED after a single try"
                )
    assert withs, "startup_event() no longer wraps its DDL in `async with startup_ddl_lock()`"
    inside = set()
    for w in withs:
        for body_node in w.body:
            for name in _STARTUP_EVENT_DDL_CALLS:
                if _calls_named(body_node, name):
                    inside.add(name)
    missing = set(_STARTUP_EVENT_DDL_CALLS) - inside
    assert not missing, f"startup_event() DDL calls outside the advisory lock: {sorted(missing)}"
    # The retry workers (background tasks) must start AFTER the lock scope, not inside it.
    for w in withs:
        for body_node in w.body:
            assert not _calls_named(body_node, "start_agent_webhook_retry_worker")
            assert not _calls_named(body_node, "start_merchant_webhook_retry_worker")


def test_startup_acquires_lock_before_create_all_and_releases_in_finally() -> None:
    import ast

    fn = _find_async_def(_main_ast(), "startup")
    # Pinned to the module-level name main.startup() uses, so a decoy lock
    # object (e.g. asyncio.Lock()) cannot satisfy these gates.
    acquires = _calls_named(fn, "acquire", receiver="_startup_ddl_lock")
    releases = _calls_named(fn, "release", receiver="_startup_ddl_lock")
    create_alls = _calls_named(fn, "create_all")
    constructors = _calls_named(fn, "StartupDdlLock")
    assert constructors, "startup() no longer constructs a StartupDdlLock"
    for call in constructors:
        assert not _has_kwarg(call, "timeout_seconds"), (
            "startup() pins a timeout_seconds on the lock; a 0 there turns the "
            "cross-instance lock into a single try that always continues UNLOCKED"
        )
    assert acquires, "startup() no longer acquires StartupDdlLock"
    assert create_alls, "startup() no longer calls metadata.create_all"
    first_acquire = min(c.lineno for c in acquires)
    first_create_all = min(c.lineno for c in create_alls)
    assert first_acquire < first_create_all, "StartupDdlLock.acquire() must precede metadata.create_all()"
    # A release must live in a `finally:` so fast-mode `return` and error paths drop the lock.
    in_finally = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            for fb in node.finalbody:
                if _calls_named(fb, "release", receiver="_startup_ddl_lock"):
                    in_finally = True
    assert in_finally, "StartupDdlLock.release() must be called from a finally: block in startup()"
    # An explicit release at the end of the DDL phase, plus the finally safety
    # net. Counting nodes named "release" anywhere would be satisfied by any two
    # unrelated calls, so both of these are receiver-pinned above.
    assert len(releases) >= 2, (
        "expected an explicit release at the end of the DDL phase plus the finally safety net"
    )
    # The explicit release must come AFTER the post-init schema guard: that guard
    # issues 24 CREATE INDEX IF NOT EXISTS statements through bare
    # database.execute(), which race across sessions exactly like CREATE TABLE.
    guards = _calls_named(fn, "ensure_required_schema_light")
    assert len(guards) >= 2, "startup() no longer runs the post-init schema guard"
    assert max(r.lineno for r in releases if r.lineno < min(f.lineno for f in _finally_release_lines(fn))) > max(
        g.lineno for g in guards
    ), "the startup DDL lock is released before the post-init schema guard runs"


@requires_pg
@pytest.mark.asyncio
async def test_lock_is_actually_released_and_connection_closed(fresh_pg_database, monkeypatch) -> None:
    """release() must drop the advisory lock AND close its dedicated
    connection — otherwise the next instance's boot blocks on it and the
    holder lingers as an idle backend.

    Uses the explicit acquire/release form and keeps the lock object alive
    across the assertions on purpose: that is how main.startup() holds it, and
    it stops CPython from masking a no-op release() by garbage-collecting the
    connection (which would close the session and free the lock by accident).
    """
    import asyncpg

    db = fresh_pg_database
    monkeypatch.setattr(startup_ddl, "database", db)
    dsn = str(db.url).replace("postgresql+asyncpg://", "postgresql://")

    observer = await asyncpg.connect(dsn)
    lock = startup_ddl.StartupDdlLock()
    try:
        assert await lock.acquire() is True
        holder_pid = await observer.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND query LIKE '%advisory_lock(%' LIMIT 1"
        )
        assert holder_pid is not None, "no backend is holding the lock"
        # A competing session cannot take it while held.
        assert (
            await observer.fetchval(
                "SELECT pg_try_advisory_lock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY
            )
            is False
        ), "advisory lock was not actually held"

        # Keep a strong reference to the holder connection across release() so
        # CPython cannot close it by garbage collection and mask a release()
        # that forgot to.
        holder_conn = lock._conn
        assert holder_conn is not None

        await lock.release()

        assert lock.held is False
        assert holder_conn.is_closed(), "the holder connection was left open"
        # …and now it can.
        assert (
            await asyncio.wait_for(
                observer.fetchval(
                    "SELECT pg_try_advisory_lock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY
                ),
                timeout=5,
            )
            is True
        ), "advisory lock still held after release()"
        await observer.execute("SELECT pg_advisory_unlock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY)

        # The dedicated holder backend itself is gone, not merely unlocked —
        # a leaked idle connection per boot is its own problem.
        still_open = await observer.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE pid = $1", holder_pid
        )
        assert still_open == 0, "the advisory-lock holder connection was not closed"

        # release() is idempotent.
        await lock.release()
    finally:
        await lock.release()
        await observer.close()


@requires_pg
@pytest.mark.asyncio
async def test_lock_released_even_when_body_raises(fresh_pg_database, monkeypatch) -> None:
    """The context manager's finally must release. Asserting only that the key
    is free afterwards is NOT enough: with the try/finally deleted, CPython
    still collects the discarded lock object and closing its connection frees
    the session lock by accident. So capture the holder connection while the
    body runs and assert it was closed deliberately."""
    import asyncpg

    db = fresh_pg_database
    monkeypatch.setattr(startup_ddl, "database", db)
    dsn = str(db.url).replace("postgresql+asyncpg://", "postgresql://")

    captured = {}
    real_acquire = startup_ddl.StartupDdlLock.acquire

    async def _capturing_acquire(self):
        got = await real_acquire(self)
        captured["lock"] = self          # strong ref: defeats GC masking
        captured["conn"] = self._conn
        return got

    monkeypatch.setattr(startup_ddl.StartupDdlLock, "acquire", _capturing_acquire)

    with pytest.raises(RuntimeError):
        async with startup_ddl_lock() as held:
            assert held is True
            raise RuntimeError("boom")

    assert captured.get("conn") is not None, "no holder connection was opened"
    assert captured["conn"].is_closed(), (
        "the holder connection outlived the failed body — startup_ddl_lock() "
        "did not release in a finally"
    )
    assert captured["lock"].held is False

    observer = await asyncpg.connect(dsn)
    try:
        assert await observer.fetchval(
            "SELECT pg_try_advisory_lock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY
        ) is True
        await observer.execute("SELECT pg_advisory_unlock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY)
    finally:
        await observer.close()


@requires_pg
@pytest.mark.asyncio
async def test_release_drops_the_lock_even_when_cancelled(fresh_pg_database, monkeypatch) -> None:
    """SIGTERM during boot cancels release() mid-await. CancelledError is a
    BaseException, so an `except Exception` there would skip the close and
    strand the session lock on a connection the object no longer references."""
    import asyncpg

    db = fresh_pg_database
    monkeypatch.setattr(startup_ddl, "database", db)
    dsn = str(db.url).replace("postgresql+asyncpg://", "postgresql://")

    lock = startup_ddl.StartupDdlLock()
    assert await lock.acquire() is True
    holder_conn = lock._conn

    class _CancelOnExecute:
        """asyncpg's Connection.execute is read-only, so wrap instead of patch.
        Everything else delegates to the real connection."""

        def __init__(self, inner):
            self._inner = inner

        async def execute(self, *args, **kwargs):
            raise asyncio.CancelledError()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    lock._conn = _CancelOnExecute(holder_conn)

    with pytest.raises(asyncio.CancelledError):
        await lock.release()

    assert holder_conn.is_closed(), "cancelled release() stranded the holder connection"

    observer = await asyncpg.connect(dsn)
    try:
        assert await asyncio.wait_for(
            observer.fetchval("SELECT pg_try_advisory_lock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY),
            timeout=5,
        ) is True, "advisory lock still held after a cancelled release()"
        await observer.execute("SELECT pg_advisory_unlock($1)", startup_ddl.STARTUP_DDL_LOCK_KEY)
    finally:
        await observer.close()


def test_startup_rechecks_webhook_events_after_migrations() -> None:
    """startup_event() probes webhook_events BEFORE the migration that creates
    it; without this re-check, webhook audit persistence stays off for the
    whole process on a first boot."""
    fn = _find_async_def(_main_ast(), "startup")
    migrations = _calls_named(fn, "run_sql_migrations")
    rechecks = _calls_named(fn, "recheck_webhook_events_schema")
    assert migrations, "startup() no longer runs the SQL migrations"
    assert rechecks, "startup() no longer re-checks the webhook_events schema after migrations"
    assert min(r.lineno for r in rechecks) > min(m.lineno for m in migrations), (
        "the webhook_events re-check must run AFTER the migrations that create the table"
    )


def test_lock_timeout_default_survives_a_cold_migration_run(monkeypatch) -> None:
    """A waiter that gives up before the peer finishes its migrations runs the
    same DDL unlocked — measured: at 120s two boots deadlocked against each
    other. A full cold run is ~125s locally, so the default must be well above
    it, and still be overridable."""
    monkeypatch.delenv("STARTUP_DDL_LOCK_TIMEOUT_SECONDS", raising=False)
    assert startup_ddl._lock_timeout_seconds() >= 600.0

    monkeypatch.setenv("STARTUP_DDL_LOCK_TIMEOUT_SECONDS", "42")
    assert startup_ddl._lock_timeout_seconds() == 42.0

    monkeypatch.setenv("STARTUP_DDL_LOCK_TIMEOUT_SECONDS", "not-a-number")
    assert startup_ddl._lock_timeout_seconds() >= 600.0
