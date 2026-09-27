"""A try-once, session-scoped Postgres advisory lock held on its OWN connection.

Why not ``await database.fetch_one("SELECT pg_try_advisory_lock(...)")``: outside an
``async with database.connection():`` block, databases 0.7.0 acquires a pool connection for
that one statement and hands it straight back, and asyncpg's reset on release runs
``pg_advisory_unlock_all()``. The lock is gone before the caller's next line (measured
2026-09-27: a second session takes it immediately), and the matching unlock runs on whatever
pool connection comes next and no-ops.

Why not pin the pool connection instead (``async with database.connection():``): that pins the
per-CONTEXT Connection, which databases 0.7.0 shares with every child task created after it
was set. A job whose body fans out (``asyncio.gather`` over merchants or probes) would push all
of those tasks' queries through the one raw connection -- and their transactions would nest
into each other's. A dedicated connection changes nothing about how the work's own queries run.

Closing the connection ends the session, which drops every lock it held, so ``release()`` only
has to close. Same DSN and per-connection settings as the startup DDL lock and the retailer
catalog write lock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 10.0
_STATEMENT_TIMEOUT_S = 10.0
_CLOSE_TIMEOUT_S = 10.0


class AdvisoryLockUnavailable(Exception):
    """The lock connection could not be opened or the try-lock failed: whether another session
    holds the lock is UNKNOWN. The caller decides whether to fail open or closed."""

    def __init__(self, cause: BaseException):
        self.error_type = type(cause).__name__
        super().__init__(f"advisory lock unavailable: {self.error_type}: {str(cause)[:200]}")


async def _open_connection() -> Any:
    import asyncpg

    from db.startup_ddl import _asyncpg_dsn, _connect_kwargs

    dsn = _asyncpg_dsn()
    if not dsn:
        raise RuntimeError("a session advisory lock needs a Postgres DATABASE_URL")
    return await asyncio.wait_for(asyncpg.connect(dsn, **_connect_kwargs()), timeout=_CONNECT_TIMEOUT_S)


async def _close(conn: Any) -> None:
    """Close, or terminate: either ends the session and with it the lock. Never raises except
    ``CancelledError``, and then only after the session is already gone."""
    try:
        await asyncio.wait_for(conn.close(), timeout=_CLOSE_TIMEOUT_S)
    except BaseException as exc:  # noqa: BLE001
        conn.terminate()  # synchronous: works on the cancellation path
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.warning("session advisory lock: close failed, connection terminated: %s", str(exc)[:200])


class DedicatedSessionAdvisoryLock:
    """``try_acquire()`` once, then ``release()`` in a ``finally`` (idempotent).

    try_acquire returns True when this object holds the lock, False when another session does
    (the connection is closed at once), and raises ``AdvisoryLockUnavailable`` when it cannot
    tell."""

    def __init__(self, lock_id: int) -> None:
        self.lock_id = int(lock_id)
        self._conn: Optional[Any] = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def try_acquire(self) -> bool:
        if self._conn is not None:
            return True
        conn = None
        try:
            conn = await _open_connection()
            got = bool(
                await asyncio.wait_for(
                    conn.fetchval("SELECT pg_try_advisory_lock($1)", self.lock_id),
                    timeout=_STATEMENT_TIMEOUT_S,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            if conn is not None:
                conn.terminate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AdvisoryLockUnavailable(exc) from exc
        if not got:
            await _close(conn)
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await _close(conn)
