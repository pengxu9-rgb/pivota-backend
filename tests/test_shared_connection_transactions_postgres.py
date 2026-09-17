"""Transactions on a Connection shared by sibling tasks must not lose writes silently or leak a slot.

Two defects in `databases` 0.7.0 + asyncpg that have nothing to do with cancellation-safe exits:

1. A cancelled root BEGIN that the server already ran leaves asyncpg's `_top_xact` set and the
   session inside a transaction nobody owns. While a sibling still holds the shared Connection
   nothing is released, so the request's NEXT `transaction()` becomes a SAVEPOINT inside that
   orphan, reports success, and is rolled back by the release reset: 0 rows, no error.
2. A transaction ended while a sibling's is still open above it failed 0.7.0's stack assertion
   before exiting the connection and held the pool slot forever.

POSTGRES GATE because both live in asyncpg's transaction state and pool; the fake backend in
test_connection_exit_survives_cancellation.py covers the `databases` side deterministically.

🚨 THESE GATE FILES SHARE ONE DATABASE. This one creates ONE table with a per-process name and
drops it again; the other pool tests open their own `Database` objects against the same URL.
"""

from __future__ import annotations

import asyncio
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — the state lives in asyncpg and the server session",
)

TABLE = f"pivota_test_shared_conn_txn_{os.getpid()}"


@pytest.fixture
async def db():
    import db.database  # noqa: F401 — installs the patches; do NOT rely on test order
    from databases import Database

    database = Database(DATABASE_URL, min_size=1, max_size=2)
    await database.connect()
    await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
    await database.execute(f"CREATE TABLE {TABLE} (v int)")
    try:
        yield database
    finally:
        # A leaked slot would make disconnect() wait forever; bound it so the failure is visible.
        try:
            await database.execute(f"DROP TABLE IF EXISTS {TABLE}")
        finally:
            await asyncio.wait_for(database.disconnect(), timeout=10)


def _in_use(database) -> int:
    pool = database._backend._pool
    return pool.get_size() - pool.get_idle_size()


async def _rows(database) -> list:
    return sorted(r[0] for r in await database.fetch_all(f"SELECT v FROM {TABLE}"))


def _cancel_after_server_runs(monkeypatch, statement: str, fail_rollback: bool = False):
    """Make `statement` succeed on the server, then park so the caller can cancel it.

    That is the losing interleaving: the server is inside the transaction, the client
    never saw BEGIN complete. Patched on the asyncpg class because the pool hands out
    read-only proxies.
    """
    import asyncpg.connection

    real = asyncpg.connection.Connection.execute
    parked = asyncio.Event()

    async def execute(self, query, *args, **kwargs):  # type: ignore[no-untyped-def]
        if fail_rollback and query.strip().upper().startswith("ROLLBACK"):
            raise ConnectionError("simulated: ROLLBACK could not be sent")
        result = await real(self, query, *args, **kwargs)
        if query.strip().upper().startswith(statement):
            parked.set()
            await asyncio.sleep(3600)
        return result

    monkeypatch.setattr(asyncpg.connection.Connection, "execute", execute)
    return parked, lambda: monkeypatch.setattr(asyncpg.connection.Connection, "execute", real)


async def _cancel_a_root_begin(database, monkeypatch, *, fail_rollback: bool = False) -> None:
    parked, restore = _cancel_after_server_runs(monkeypatch, "BEGIN", fail_rollback)
    begin = asyncio.ensure_future(database.connection().transaction().start())
    await asyncio.wait_for(parked.wait(), timeout=5)
    begin.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(begin, timeout=5)
    restore()


# --- 1. a cancelled root BEGIN ------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_after_a_cancelled_begin_persist_while_a_sibling_holds_the_connection(
    db, monkeypatch
) -> None:
    async with db.connection() as conn:  # the sibling: counter stays > 0, nothing is released
        await _cancel_a_root_begin(db, monkeypatch)

        raw = conn.raw_connection._con
        assert raw._top_xact is None, "asyncpg still believes the cancelled BEGIN owns the session"
        assert not raw._protocol.is_in_transaction(), "the server is still inside the orphan BEGIN"

        async with db.transaction():  # the request's next write
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    assert _in_use(db) == 0
    assert await _rows(db) == [1], "the write reported success and was then rolled back"


@pytest.mark.asyncio
async def test_if_the_cleanup_rollback_fails_the_next_transaction_fails_loudly(db, monkeypatch) -> None:
    """With ROLLBACK unsendable, success must not be reported for a write that will vanish."""
    import asyncpg

    async with db.connection() as conn:
        await _cancel_a_root_begin(db, monkeypatch, fail_rollback=True)
        assert conn.raw_connection._con._protocol.is_in_transaction()  # the orphan really is there

        with pytest.raises(asyncpg.InterfaceError, match="manually started transaction"):
            async with db.transaction():
                await db.execute(f"INSERT INTO {TABLE} VALUES (1)")

    assert _in_use(db) == 0
    assert await _rows(db) == []


# --- 2. transactions ended out of order ------------------------------------------------------


@pytest.mark.asyncio
async def test_sibling_transactions_ending_out_of_order_fail_loudly_and_return_the_slot(db) -> None:
    import asyncpg

    from db.database import TransactionEndedOutOfOrder

    a_open, b_open, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def task_a() -> None:  # root: BEGIN
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (1)")
            a_open.set()
            await b_open.wait()
        # not reached: the commit above raises

    async def task_b() -> None:  # nested: SAVEPOINT inside A's transaction
        await a_open.wait()
        async with db.transaction():
            await db.execute(f"INSERT INTO {TABLE} VALUES (2)")
            b_open.set()
            await a_done.wait()

    async def a_then_signal() -> None:
        try:
            await task_a()
        finally:
            a_done.set()

    async with db.connection():  # the request: child tasks inherit this Connection
        results = await asyncio.wait_for(
            asyncio.gather(a_then_signal(), task_b(), return_exceptions=True), timeout=10
        )

    assert isinstance(results[0], TransactionEndedOutOfOrder), results
    # A rolled back, which discarded B's savepoint: B's own end fails on the server, not silently.
    assert isinstance(results[1], asyncpg.PostgresError), results
    assert _in_use(db) == 0, "an out-of-order end kept its pool slot"
    assert await _rows(db) == [], "an out-of-order COMMIT committed its sibling's open work"

    async with db.transaction():  # the pool and the session are usable afterwards
        await db.execute(f"INSERT INTO {TABLE} VALUES (3)")
    assert await _rows(db) == [3]
