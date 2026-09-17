"""A COMMIT or ROLLBACK that never answers must give up, terminate, and never report success.

`databases` Transaction.commit/rollback run uncancellably (db/database.py makes them, so a
cancelled request still returns its slot) while holding the shared Connection's
`_transaction_lock`. On a socket gone silent nothing else bounds them: the statement timeout is
server-side and asyncpg waits for a cancel acknowledgement before any command_timeout. Reproduced
through a traffic-dropping TCP proxy: still running after 15s with the pool's free slots at 0.

A timed-out root COMMIT has an UNKNOWN outcome — the server may have committed and the answer been
lost — so it must raise `CommitOutcomeUnknown`, never return. The two COMMIT tests below pin both
halves of "unknown": the same error whether or not the server committed.

POSTGRES GATE: the hang is on asyncpg's connection (patched on the asyncpg class, because the pool
hands out proxies). 🚨 THESE GATE FILES SHARE ONE DATABASE — one per-process table, dropped after.
"""

from __future__ import annotations

import asyncio
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — the hang lives on the asyncpg connection",
)

TABLE = f"pivota_test_bounded_txn_end_{os.getpid()}"
DEADLINE = 0.5


@pytest.fixture
async def db(monkeypatch):
    import asyncpg.connection

    import db.database as dbmod
    from databases import Database

    monkeypatch.setattr(dbmod, "DB_POOL_CHECKOUT_TIMEOUT_SECONDS", DEADLINE)
    terminated: list = []
    real_terminate = asyncpg.connection.Connection.terminate

    def terminate(self):  # type: ignore[no-untyped-def]
        terminated.append(self)
        real_terminate(self)

    monkeypatch.setattr(asyncpg.connection.Connection, "terminate", terminate)

    database = Database(DATABASE_URL, min_size=1, max_size=2)
    await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await database.execute(f"CREATE TABLE {TABLE} (v int)")
    database.terminated = terminated  # type: ignore[attr-defined]
    database.unhang = asyncio.Event()  # type: ignore[attr-defined]
    try:
        yield database
    finally:
        database.unhang.set()
        try:
            await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
        finally:
            await asyncio.wait_for(database.disconnect(), timeout=10)


def _in_use(database) -> int:
    pool = database._backend._pool
    return pool.get_size() - pool.get_idle_size()


async def _rows(database) -> list:
    return sorted(r[0] for r in await database.fetch_all(f"SELECT v FROM {TABLE}"))


def _hang_on(db, monkeypatch, statement: str, *, after_server_runs: bool) -> asyncio.Event:
    """Make `statement` never answer — before it reaches the server, or after the server ran it.

    Cancellable, like asyncpg's real wait on a silent socket. `db.unhang` lets a test end a hang
    the code under test failed to bound, so a regression fails instead of hanging the suite.
    """
    import asyncpg.connection

    real = asyncpg.connection.Connection.execute
    reached = asyncio.Event()

    async def execute(self, query, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not query.strip().upper().startswith(statement):
            return await real(self, query, *args, **kwargs)
        if after_server_runs:
            await real(self, query, *args, **kwargs)
        reached.set()
        await db.unhang.wait()
        raise ConnectionError("simulated: the silent socket was given up on by the test")

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", execute)
    return reached


async def _within_deadline(db, awaitable, seconds: float = 10):
    """Await `awaitable`, failing (not hanging) if it outlives `seconds`.

    Not `asyncio.wait_for`: the end runs uncancellably, so wait_for's cancel would wait on it forever.
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait((task,), timeout=seconds)
    if not done:
        db.unhang.set()
        await asyncio.wait((task,), timeout=5)
        pytest.fail(f"still running after {seconds}s: the transaction end has no deadline")
    return task.result()


def _assert_terminated_and_released(db) -> None:
    assert _in_use(db) == 0, "the slot is still held by an end that never answered"
    assert len(db.terminated) == 1 and db.terminated[0].is_closed()


async def _pool_still_works(db, monkeypatch, value: int) -> None:
    monkeypatch.undo()  # also restores the deadline; the fixture's teardown needs none of it
    async with db.transaction():  # the pool replaced the terminated connection
        await db.execute(f"INSERT INTO {TABLE} VALUES ({value})")


# --- root COMMIT: unknown outcome -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("server_committed", [True, False], ids=["answer-lost", "never-sent"])
async def test_a_commit_that_never_answers_raises_outcome_unknown(
    db, monkeypatch, server_committed
) -> None:
    from db.database import CommitOutcomeUnknown

    _hang_on(db, monkeypatch, "COMMIT", after_server_runs=server_committed)

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(CommitOutcomeUnknown, match="MAY OR MAY NOT have committed"):
        await _within_deadline(db, write())

    _assert_terminated_and_released(db)
    await _pool_still_works(db, monkeypatch, 2)
    # Both outcomes happen behind the same error — which is why it cannot claim either.
    assert await _rows(db) == ([1, 2] if server_committed else [2])


@pytest.mark.asyncio
async def test_a_hung_commit_does_not_block_siblings_on_the_shared_connection(db, monkeypatch) -> None:
    """The end holds `_transaction_lock`; a sibling waiting on it must get an answer, not hang."""
    import asyncpg

    from db.database import CommitOutcomeUnknown

    reached = _hang_on(db, monkeypatch, "COMMIT", after_server_runs=False)

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    async def sibling() -> None:
        await reached.wait()  # the COMMIT is in flight, the lock is held
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (2)")

    async with db.connection():  # child tasks inherit this one Connection
        results = await _within_deadline(
            db, asyncio.gather(write(), sibling(), return_exceptions=True)
        )

    assert isinstance(results[0], CommitOutcomeUnknown), results
    # The session is gone, so the sibling fails loudly rather than writing into nothing.
    assert isinstance(results[1], asyncpg.InterfaceError), results
    _assert_terminated_and_released(db)
    await _pool_still_works(db, monkeypatch, 3)
    assert await _rows(db) == [3]


# --- ROLLBACK and savepoints: nothing committed, but still an error --------------------------


@pytest.mark.asyncio
async def test_a_rollback_that_never_answers_terminates_and_raises(db, monkeypatch) -> None:
    from db.database import CommitOutcomeUnknown, TransactionEndTimedOut

    _hang_on(db, monkeypatch, "ROLLBACK", after_server_runs=False)

    async def failing_write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            raise ValueError("the caller's own error")

    with pytest.raises(TransactionEndTimedOut, match="Nothing was committed") as excinfo:
        await _within_deadline(db, failing_write())
    assert not isinstance(excinfo.value, CommitOutcomeUnknown)
    assert isinstance(excinfo.value.__context__, ValueError), "the caller's error was lost"

    _assert_terminated_and_released(db)
    await _pool_still_works(db, monkeypatch, 2)
    assert await _rows(db) == [2]


@pytest.mark.asyncio
async def test_a_savepoint_release_that_never_answers_loses_the_enclosing_transaction_loudly(
    db, monkeypatch
) -> None:
    from db.database import CommitOutcomeUnknown, TransactionEndTimedOut

    _hang_on(db, monkeypatch, "RELEASE SAVEPOINT", after_server_runs=False)
    outer_error: list = []

    async def nested_write() -> None:
        outer = await db.transaction()
        try:
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            async with db.transaction():
                await db.execute(f"INSERT INTO {TABLE} VALUES (2)")
        finally:
            try:
                await outer.commit()
            except Exception as exc:  # the session is gone: this must not succeed either
                outer_error.append(exc)

    with pytest.raises(TransactionEndTimedOut, match="enclosing this savepoint") as excinfo:
        await _within_deadline(db, nested_write())
    assert not isinstance(excinfo.value, CommitOutcomeUnknown)
    assert len(outer_error) == 1, "the outer COMMIT reported success on a terminated session"

    _assert_terminated_and_released(db)
    await _pool_still_works(db, monkeypatch, 3)
    assert await _rows(db) == [3]
