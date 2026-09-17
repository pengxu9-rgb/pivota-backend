"""A cancelled request must still return its pool connection.

WHAT THIS PREVENTS (2026-09-16). `databases` 0.7.0 hands a pool connection back
only at the end of a run of awaits with no try/finally around them:
`Connection.__aexit__` (inside `_connection_lock`), `Transaction.start` (after
BEGIN), `Transaction.commit` / `rollback` (inside `_transaction_lock`, after
COMMIT/ROLLBACK). A cancellation or error at any of those awaits orphaned the
connection for the life of the process. anyio (Starlette's BaseHTTPMiddleware)
re-delivers cancellation at every await, and sibling tasks in one request share
one `Connection`, so prod hit it constantly: every web instance's 12-slot pool
drained within hours and api.pivota.cc /health went 503.

No database needed: the defect is in `databases` core, so a fake backend
reproduces each interleaving exactly and deterministically. The Postgres-shaped
evidence (pg_stat_activity, anyio fuzz) is in the PR description.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest


class _FakeTransaction:
    def __init__(self, raw: "_FakeRawConnection") -> None:
        self._raw = raw

    async def start(self, is_root: bool, extra_options: dict) -> None:
        await self._raw.gate("begin")

    async def commit(self) -> None:
        await self._raw.gate("commit")

    async def rollback(self) -> None:
        await self._raw.gate("rollback")


class _FakeRawConnection:
    """Stands in for PostgresConnection. `block`/`fail` script each step."""

    def __init__(self) -> None:
        self._connection = None
        self.acquired = 0
        self.released = 0
        self.block: dict = {}  # step -> asyncio.Event the step waits for
        self.fail: dict = {}  # step -> exception the step raises
        self.entered: dict = {}  # step -> asyncio.Event set when the step starts

    async def gate(self, step: str) -> None:
        self.entered.setdefault(step, asyncio.Event()).set()
        if step in self.block:
            await self.block[step].wait()
        if step in self.fail:
            raise self.fail[step]

    async def acquire(self) -> None:
        assert self._connection is None, "Connection is already acquired"
        self.acquired += 1
        self._connection = object()

    async def release(self) -> None:
        assert self._connection is not None, "Connection is not acquired"
        await self.gate("release")
        self.released += 1
        self._connection = None

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


class _FakeBackend:
    def __init__(self) -> None:
        self.raw = _FakeRawConnection()

    def connection(self) -> _FakeRawConnection:
        return self.raw


def _connection():
    import db.database  # noqa: F401 — installs the patch; do NOT rely on test order
    from databases.core import Connection

    backend = _FakeBackend()
    return Connection(backend), backend.raw


def _transaction(conn):
    from databases.core import Transaction

    return Transaction(lambda: conn, force_rollback=False)


async def _spin(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _assert_returned(conn, raw, *, times: int = 1) -> None:
    assert conn._connection_counter == 0, (
        f"checkout counter stuck at {conn._connection_counter} — the raw "
        "connection is orphaned and its pool slot is lost for good"
    )
    assert raw.released == times, f"released {raw.released}x, expected {times}x"
    assert raw._connection is None


# --- Connection.__aexit__ ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_re_delivered_cancellation_still_releases_the_connection() -> None:
    """The prod interleaving: cancelled mid-query, then AGAIN while waiting for the lock."""
    conn, raw = _connection()
    inside = asyncio.Event()

    async def request() -> None:
        async with conn:
            inside.set()
            await asyncio.sleep(3600)  # the in-flight query

    task = asyncio.create_task(request())
    await asyncio.wait_for(inside.wait(), timeout=5)
    await conn._connection_lock.acquire()  # a sibling is mid-acquire/release
    task.cancel()  # first delivery: unwind into __aexit__
    await _spin()
    task.cancel()  # re-delivery while parked on the lock (anyio does this)
    await _spin()
    conn._connection_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    _assert_returned(conn, raw)

    async with conn:  # reusable: no half-released handle left behind
        pass
    assert (raw.acquired, raw.released) == (2, 2)


@pytest.mark.asyncio
async def test_a_single_cancellation_landing_in_exit_releases_and_is_not_swallowed() -> None:
    """One plain `task.cancel()` is enough; the wrapper must not eat it either.

    The body finishes normally, so the ONLY cancellation is the one delivered
    while `__aexit__` waits for the lock. Kills a wrapper that absorbs the
    cancellation without re-raising (the task would then return normally).
    """
    conn, raw = _connection()
    body_waiting = asyncio.Event()
    finish_body = asyncio.Event()

    async def request() -> str:
        async with conn:
            body_waiting.set()
            await finish_body.wait()
        return "returned normally"

    task = asyncio.create_task(request())
    await asyncio.wait_for(body_waiting.wait(), timeout=5)
    await conn._connection_lock.acquire()  # a sibling takes the lock
    finish_body.set()  # the body completes WITHOUT any cancellation
    await _spin()  # ...and the request parks in __aexit__ on the lock
    task.cancel()  # the one and only delivery
    await _spin()
    conn._connection_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    _assert_returned(conn, raw)


@pytest.mark.asyncio
async def test_an_uncancelled_exit_releases_and_propagates_the_body_error() -> None:
    conn, raw = _connection()
    with pytest.raises(ValueError, match="boom"):
        async with conn:
            raise ValueError("boom")
    _assert_returned(conn, raw)


@pytest.mark.asyncio
async def test_a_failing_release_propagates_when_nothing_was_cancelled() -> None:
    """Kills a wrapper that never reads the cleanup task's outcome."""
    conn, raw = _connection()
    raw.fail["release"] = RuntimeError("connection lost during release")
    with pytest.raises(RuntimeError, match="connection lost during release"):
        async with conn:
            pass
    assert conn._connection_counter == 0


@pytest.mark.asyncio
async def test_a_cancellation_during_a_failing_release_is_not_dropped() -> None:
    """Cancellation wins and the release error rides along as its cause.

    Before the fix, a release error raised after the cancellation arrived
    replaced it: a caller's `except Exception` then swallowed the error and the
    cancelled task carried on running.
    """
    conn, raw = _connection()
    raw.block["release"] = asyncio.Event()
    raw.fail["release"] = RuntimeError("connection lost during release")

    async def request() -> str:
        try:
            async with conn:
                pass
        except Exception:
            return "cancellation swallowed"
        return "returned normally"

    task = asyncio.create_task(request())
    await asyncio.wait_for(raw.entered.setdefault("release", asyncio.Event()).wait(), timeout=5)
    task.cancel()
    await _spin()
    raw.block["release"].set()

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await asyncio.wait_for(task, timeout=5)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# --- Transaction --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_begin_returns_the_connection() -> None:
    conn, raw = _connection()
    raw.block["begin"] = asyncio.Event()  # never set: BEGIN is in flight

    async def request() -> None:
        async with _transaction(conn):
            pass

    task = asyncio.create_task(request())
    await asyncio.wait_for(raw.entered.setdefault("begin", asyncio.Event()).wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    _assert_returned(conn, raw)
    assert conn._transaction_stack == []


@pytest.mark.asyncio
async def test_a_failed_begin_returns_the_connection() -> None:
    conn, raw = _connection()
    raw.fail["begin"] = RuntimeError("could not begin")
    with pytest.raises(RuntimeError, match="could not begin"):
        async with _transaction(conn):
            pass
    _assert_returned(conn, raw)


@pytest.mark.asyncio
async def test_a_rollback_re_cancelled_while_waiting_for_the_lock_returns_the_connection() -> None:
    conn, raw = _connection()
    inside = asyncio.Event()

    async def request() -> None:
        async with _transaction(conn):
            inside.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(request())
    await asyncio.wait_for(inside.wait(), timeout=5)
    await conn._transaction_lock.acquire()  # a sibling's transaction step
    task.cancel()  # unwind into Transaction.__aexit__ -> rollback()
    await _spin()
    task.cancel()  # re-delivered while rollback waits for the lock
    await _spin()
    conn._transaction_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert raw.entered.get("rollback") is not None, "ROLLBACK was never sent"
    _assert_returned(conn, raw)
    assert conn._transaction_stack == []


@pytest.mark.asyncio
async def test_a_failed_commit_returns_the_connection() -> None:
    conn, raw = _connection()
    raw.fail["commit"] = RuntimeError("could not serialize access")
    with pytest.raises(RuntimeError, match="could not serialize"):
        async with _transaction(conn):
            pass
    _assert_returned(conn, raw)


@pytest.mark.asyncio
async def test_a_committed_transaction_still_works() -> None:
    conn, raw = _connection()
    async with _transaction(conn):
        async with _transaction(conn):  # nested: savepoint path
            pass
    assert raw.entered.get("commit") is not None
    _assert_returned(conn, raw)


# --- anyio -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anyio_cancellation_does_not_spin_while_the_release_finishes(monkeypatch) -> None:
    """anyio re-delivers cancellation on every await; the wait must not busy-loop."""
    import anyio

    conn, raw = _connection()
    raw.block["release"] = asyncio.Event()
    calls = {"n": 0}
    real_wait = asyncio.wait

    def counting_wait(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Count only the patch's own waits; anyio's task group uses asyncio.wait too.
        if sys._getframe(1).f_globals.get("__name__") == "db.database":
            calls["n"] += 1
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", counting_wait)

    async def request() -> None:
        async with conn:
            pass

    async def finish_release_later() -> None:
        await asyncio.sleep(0.2)
        raw.block["release"].set()

    helper = asyncio.create_task(finish_release_later())
    async with anyio.create_task_group() as tg:
        tg.start_soon(request)
        await asyncio.wait_for(raw.entered.setdefault("release", asyncio.Event()).wait(), timeout=5)
        tg.cancel_scope.cancel()
    await helper

    _assert_returned(conn, raw)
    assert 1 <= calls["n"] < 20, f"cleanup wait looped {calls['n']}x — it is spinning (or never ran)"


# --- installation ---------------------------------------------------------------


def test_the_process_actually_installs_the_patch_on_import() -> None:
    """Importing `db.database` must leave every patched method in place.

    A subprocess, because this interpreter has already imported the module and
    any in-process check is satisfied by whoever imported it first. Runs under
    whatever DATABASE_URL CI provides (SQLite in the main sweep) — the install
    is unconditional, and this pins that.
    """
    probe = (
        "import db.database as d;"
        "from databases.core import Connection as C, Transaction as T;"
        "import sys;"
        "ok = getattr(C.__aexit__, '_pivota_cancel_safe', False)"
        " and all(getattr(T, m).__module__ == 'db.database' for m in ('start','commit','rollback'));"
        "sys.exit(0 if ok else 3)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=dict(os.environ),
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "importing db.database did not install the cancellation-safe connection "
        f"handling (rc={proc.returncode}): {proc.stderr.decode()[-400:]}"
    )
