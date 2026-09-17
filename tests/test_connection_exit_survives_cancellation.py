"""A cancelled request must still return its pool connection.

WHAT THIS PREVENTS (2026-09-16). `databases` 0.7.0 `Connection.__aexit__`
decrements the checkout counter and releases the raw connection only inside
`async with self._connection_lock`. A cancellation delivered while the task
waits for that lock skipped both, so the pool slot leaked for the life of the
process. anyio (Starlette's BaseHTTPMiddleware) re-delivers cancellation at
every await, and sibling tasks in one request share one `Connection`, so prod
hit it constantly: every web instance's 12-slot pool drained within hours and
api.pivota.cc /health went 503.

No database needed: the defect is in `databases` core, so a fake backend
reproduces it exactly and deterministically. The Postgres-shaped evidence
(pg_stat_activity, anyio fuzz) is in the PR description.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest


class _FakeRawConnection:
    def __init__(self) -> None:
        self._connection = None
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> None:
        assert self._connection is None, "Connection is already acquired"
        self.acquired += 1
        self._connection = object()

    async def release(self) -> None:
        assert self._connection is not None, "Connection is not acquired"
        self.released += 1
        self._connection = None


class _FakeBackend:
    def __init__(self) -> None:
        self.raw = _FakeRawConnection()

    def connection(self) -> _FakeRawConnection:
        return self.raw


async def _spin(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _cancel_twice_across_a_contended_lock(conn) -> asyncio.Task:
    """Drive the exact prod interleaving and return the (finished) request task.

    1. The request holds a checkout (counter 1) and is cancelled mid-query.
    2. A sibling sharing the Connection holds `_connection_lock`, so the
       request's `__aexit__` has to wait for it.
    3. The cancellation is delivered AGAIN during that wait, as anyio does.
    """
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
    task.cancel()  # re-delivery while parked on the lock
    await _spin()
    conn._connection_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    return task


@pytest.mark.asyncio
async def test_a_re_delivered_cancellation_still_releases_the_connection() -> None:
    import db.database  # noqa: F401 — installs the patch; do NOT rely on test order
    from databases.core import Connection

    backend = _FakeBackend()
    conn = Connection(backend)

    await _cancel_twice_across_a_contended_lock(conn)

    assert conn._connection_counter == 0, (
        f"checkout counter stuck at {conn._connection_counter} — the raw "
        "connection is orphaned and its pool slot is lost for good"
    )
    assert backend.raw.released == 1, "connection was never returned to the pool"
    assert backend.raw._connection is None


@pytest.mark.asyncio
async def test_the_connection_is_reusable_after_the_cancelled_request() -> None:
    """A half-released handle ("Connection is already acquired") is the sibling failure."""
    import db.database  # noqa: F401
    from databases.core import Connection

    backend = _FakeBackend()
    conn = Connection(backend)

    await _cancel_twice_across_a_contended_lock(conn)

    async with conn:
        pass
    assert (backend.raw.acquired, backend.raw.released) == (2, 2)


@pytest.mark.asyncio
async def test_an_uncancelled_exit_still_releases_and_propagates_errors() -> None:
    """The wrapper must not change the normal path or swallow the body's exception."""
    import db.database  # noqa: F401
    from databases.core import Connection

    backend = _FakeBackend()
    conn = Connection(backend)

    with pytest.raises(ValueError, match="boom"):
        async with conn:
            raise ValueError("boom")
    assert conn._connection_counter == 0
    assert backend.raw.released == 1


def test_the_process_actually_installs_the_patch_on_import() -> None:
    """Importing `db.database` must leave `Connection.__aexit__` patched.

    A subprocess, because this interpreter has already imported the module and
    any in-process check is satisfied by whoever imported it first. Runs under
    whatever DATABASE_URL CI provides (SQLite in the main sweep) — the install
    is unconditional, and this pins that.
    """
    probe = (
        "import db.database;"
        "from databases.core import Connection as C;"
        "import sys;"
        "sys.exit(0 if getattr(C.__aexit__, '_pivota_cancel_safe', False) else 3)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=dict(os.environ),
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "importing db.database did not install the cancellation-safe exit "
        f"(rc={proc.returncode}): {proc.stderr.decode()[-400:]}"
    )
