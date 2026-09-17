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
            await database.execute(f"DROP FUNCTION IF EXISTS {TABLE}_slow()")
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


@pytest.mark.asyncio
async def test_a_commit_the_server_rejects_still_raises_through_the_deadline(db) -> None:
    """The deadline wrapper must pass a real COMMIT failure through, not swallow it.

    A DEFERRED constraint is checked at COMMIT, so COMMIT itself answers with an error.
    """
    import asyncpg

    await db.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {TABLE}_v_unique UNIQUE (v) DEFERRABLE INITIALLY DEFERRED"
    )

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(asyncpg.UniqueViolationError):
        await _within_deadline(db, write())

    assert _in_use(db) == 0
    assert db.terminated == [], "an answered COMMIT is not a silent socket"
    assert await _rows(db) == []


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


# --- the reply lost BEFORE the deadline: command timeout, dropped connection -----------------


def _fail_on(monkeypatch, statement: str, exc: BaseException, *, after_server_runs: bool) -> None:
    """Make `statement` fail without the server's answer — as a command timeout or a lost socket."""
    import asyncpg.connection

    real = asyncpg.connection.Connection.execute

    async def execute(self, query, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not query.strip().upper().startswith(statement):
            return await real(self, query, *args, **kwargs)
        if after_server_runs:
            await real(self, query, *args, **kwargs)
        raise exc

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", execute)


def _lost_reply_errors():
    import asyncpg

    return [
        pytest.param(asyncio.TimeoutError(), id="command-timeout"),
        pytest.param(asyncpg.ConnectionDoesNotExistError("connection was closed"), id="socket-lost"),
        pytest.param(ConnectionResetError("reset by peer"), id="socket-reset"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("server_committed", [True, False], ids=["committed", "not-sent"])
@pytest.mark.parametrize("exc", _lost_reply_errors())
async def test_a_commit_whose_reply_is_lost_early_is_outcome_unknown_too(
    db, monkeypatch, exc, server_committed
) -> None:
    """DB_COMMAND_TIMEOUT_SECONDS shorter than the deadline used to surface as a bare TimeoutError."""
    from db.database import CommitOutcomeUnknown

    _fail_on(monkeypatch, "COMMIT", exc, after_server_runs=server_committed)

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(CommitOutcomeUnknown, match="MAY OR MAY NOT have committed") as excinfo:
        await _within_deadline(db, write())
    assert excinfo.value.__cause__ is exc

    _assert_terminated_and_released(db)
    await _pool_still_works(db, monkeypatch, 2)
    assert await _rows(db) == ([1, 2] if server_committed else [2])


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _lost_reply_errors())
async def test_a_savepoint_whose_reply_is_lost_early_is_not_called_a_commit(db, monkeypatch, exc) -> None:
    """Only a ROOT COMMIT has an unknown outcome; a savepoint's enclosing transaction is still open."""
    from db.database import CommitOutcomeUnknown

    _fail_on(monkeypatch, "RELEASE SAVEPOINT", exc, after_server_runs=False)

    async def nested_write() -> None:
        async with db.transaction():
            async with db.transaction():
                await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(BaseException) as excinfo:
        await _within_deadline(db, nested_write())
    assert not isinstance(excinfo.value, CommitOutcomeUnknown), excinfo.value
    assert excinfo.value is exc, "a savepoint's own failure must reach the caller unchanged"


# --- a release that fails must not strand the databases Connection ---------------------------


def _fail_release_reset(monkeypatch) -> list:
    """asyncpg's release resets the connection; make that fail. asyncpg then terminates and re-raises."""
    import asyncpg.connection

    calls: list = []

    async def reset(self, *, timeout=None):  # type: ignore[no-untyped-def]
        calls.append(self)
        raise ConnectionError("simulated: the socket went silent after COMMIT answered")

    monkeypatch.setattr(asyncpg.connection.Connection, "reset", reset)
    return calls


@pytest.mark.asyncio
async def test_a_committed_write_whose_release_fails_reports_success_and_the_connection_stays_usable(
    db, monkeypatch, caplog
) -> None:
    """The COMMIT answered, so it HAS committed: a failure to release must not report otherwise.

    Before: the release error replaced the successful commit (a retry would write twice), and every
    later query on that databases Connection failed with "Connection is already acquired".
    """
    resets = _fail_release_reset(monkeypatch)

    async def task_body() -> list:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
        monkeypatch.undo()
        # Same task, same context: the same databases Connection is checked out again.
        return [await db.fetch_val("SELECT 42")]

    with caplog.at_level("WARNING", logger="db.database"):
        assert await _within_deadline(db, task_body()) == [42]

    assert len(resets) == 1, "the release reset was never reached — the test proves nothing"
    assert db.terminated and db.terminated[0].is_closed()
    assert _in_use(db) == 0
    assert await _rows(db) == [1]
    assert any("releasing a database connection failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_failed_release_that_did_not_give_the_slot_back_still_raises(db, monkeypatch) -> None:
    """Control: forgetting a checkout the pool still holds would leak its slot silently."""
    import asyncpg.pool

    real_release = asyncpg.pool.Pool.release

    async def release(self, connection, *, timeout=None):  # type: ignore[no-untyped-def]
        raise ConnectionError("simulated: release failed before touching the connection")

    monkeypatch.setattr(asyncpg.pool.Pool, "release", release)
    conn = db.connection()
    with pytest.raises(ConnectionError, match="before touching"):
        async with conn:
            await conn.fetch_val("SELECT 1")
    monkeypatch.setattr(asyncpg.pool.Pool, "release", real_release)
    # The checkout was not forgotten: releasing it for real now returns the slot.
    assert conn._connection._connection is not None
    await conn._connection.release()
    assert _in_use(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", _lost_reply_errors())
async def test_a_root_rollback_whose_reply_is_lost_early_is_not_called_a_commit(db, monkeypatch, exc) -> None:
    from db.database import CommitOutcomeUnknown

    _fail_on(monkeypatch, "ROLLBACK", exc, after_server_runs=False)

    async def failing_write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            raise ValueError("the caller's own error")

    with pytest.raises(BaseException) as excinfo:
        await _within_deadline(db, failing_write())
    assert not isinstance(excinfo.value, CommitOutcomeUnknown), excinfo.value
    assert excinfo.value is exc


@pytest.mark.asyncio
async def test_a_cancelled_release_after_the_proxy_was_detached_still_clears_and_still_cancels(
    db, monkeypatch
) -> None:
    """Cancellation is never swallowed — and the Connection is not stranded by it either."""
    import asyncpg.pool

    async def release(self, connection, *, timeout=None):  # type: ignore[no-untyped-def]
        connection._con.terminate()  # asyncpg detaches the proxy and returns the slot
        raise asyncio.CancelledError()

    from databases.core import Connection

    # A standalone Connection, not `db.connection()`: that one is shared through the context with
    # the fixture's teardown, which would inherit the half-exited state this test leaves.
    conn = Connection(db._backend)
    await conn.__aenter__()
    backend_conn = conn._connection
    monkeypatch.setattr(asyncpg.pool.Pool, "release", release)
    with pytest.raises(asyncio.CancelledError):
        await backend_conn.release()
    assert backend_conn._connection is None, "a cancelled release stranded the databases Connection"
    monkeypatch.undo()
    assert _in_use(db) == 0


# --- a COMMIT given up on must not keep running on the server --------------------------------
#
# Review of the first version: terminating right away also dropped the cancel asyncpg had
# queued, and a server busy inside COMMIT does not notice a closed socket. The COMMIT then
# landed AFTER CommitOutcomeUnknown told the caller to check the data (0 rows at the error, 1 row
# later): a caller that checks and retries writes twice. A deferred constraint trigger that sleeps
# makes COMMIT itself slow, on a real statement.


SLEEP = 2.0


async def _make_commit_slow(database) -> None:
    await database.execute(
        f"""CREATE OR REPLACE FUNCTION {TABLE}_slow() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_sleep({SLEEP});
            RETURN NULL;
        END $$"""
    )
    await database.execute(
        f"CREATE CONSTRAINT TRIGGER {TABLE}_slow AFTER INSERT ON {TABLE} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {TABLE}_slow()"
    )


async def _rows_once_the_commit_would_have_landed(database) -> list:
    await asyncio.sleep(SLEEP + 1.0)
    return await _rows(database)


@pytest.mark.asyncio
async def test_a_commit_past_the_deadline_is_cancelled_on_the_server_not_just_abandoned(db) -> None:
    from db.database import CommitOutcomeUnknown

    await _make_commit_slow(db)

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(CommitOutcomeUnknown, match="finished with it") as excinfo:
        await _within_deadline(db, write())
    assert excinfo.value.settled is True
    _assert_terminated_and_released(db)
    assert await _rows_once_the_commit_would_have_landed(db) == [], (
        "the COMMIT landed after the caller was told to check the data"
    )


@pytest.mark.asyncio
async def test_a_commit_past_the_command_timeout_is_cancelled_on_the_server_not_just_abandoned(
    db, monkeypatch
) -> None:
    from databases import Database

    import db.database as dbmod
    from db.database import CommitOutcomeUnknown

    await _make_commit_slow(db)
    monkeypatch.setattr(dbmod, "DB_POOL_CHECKOUT_TIMEOUT_SECONDS", 5.0)  # the command timeout fires first
    timed = Database(DATABASE_URL, min_size=1, max_size=1, command_timeout=0.3)
    await timed.connect()
    try:

        async def write() -> None:
            async with timed.transaction():
                await timed.execute(f"INSERT INTO {TABLE} VALUES (1)")

        with pytest.raises(CommitOutcomeUnknown, match="finished with it") as excinfo:
            await _within_deadline(db, write())
        assert excinfo.value.settled is True
        assert isinstance(excinfo.value.__cause__, asyncio.TimeoutError)
        assert _in_use(timed) == 0
    finally:
        await asyncio.wait_for(timed.disconnect(), timeout=10)
    assert await _rows_once_the_commit_would_have_landed(db) == [], (
        "the COMMIT landed after the caller was told to check the data"
    )


@pytest.mark.asyncio
async def test_when_the_cancel_cannot_land_the_error_says_the_commit_may_still_land(
    db, monkeypatch
) -> None:
    """And it is not a hypothetical: the server finishes that COMMIT after the error."""
    import asyncpg.connection

    from db.database import CommitOutcomeUnknown

    await _make_commit_slow(db)

    async def unreachable_cancel(self, waiter):  # type: ignore[no-untyped-def]
        await db.unhang.wait()  # the cancel connection never gets through; terminate() cancels this

    monkeypatch.setattr(asyncpg.connection.Connection, "_cancel", unreachable_cancel)

    async def write() -> None:
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    with pytest.raises(CommitOutcomeUnknown, match="MAY STILL BE RUNNING") as excinfo:
        await _within_deadline(db, write())
    assert excinfo.value.settled is False
    _assert_terminated_and_released(db)
    assert await _rows_once_the_commit_would_have_landed(db) == [1], (
        "the server did not finish the COMMIT — then this test no longer shows why the message warns"
    )
