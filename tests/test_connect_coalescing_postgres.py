"""One connect in flight per database — measured in server backends.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob). The coordination
contract is pinned against fakes in `test_connect_coalescing.py`; the cost of
getting it wrong is only visible as real backend processes on a real server,
which is what this file counts.

THE DEFECT. `databases.Database.connect()` reads `is_connected`, awaits
`create_pool()`, and only then writes the flag, while `PostgresBackend.connect()`'s
own `assert self._pool is None` is likewise checked before its await. N
concurrent callers therefore all pass both guards, all build a pool, and each
assigns `backend._pool` — last write wins and the losers are unreachable,
because `disconnect()` only ever closes the pool currently referenced.

MEASURED before this change, 6 concurrent `ensure_database_ready` callers at
DB_POOL_MIN_SIZE=5: 50 -> 80 -> 96 backends over three waves, 85 still open
after a clean `disconnect()`. `max_connections` (100) is gone in three waves,
and it takes only concurrency on the ordinary recovery path — not a slow
database, which is the separate defect the sibling leak suite covers.

Backends are counted from an INDEPENDENT asyncpg control connection, never
through the pool under test, against a per-test baseline.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

CALLERS = 6
# In-flight handshakes land after the call that abandoned them returns, so a
# count read immediately can certify a clean run a moment before the leak
# exists. See the sibling leak suite, where that was a false pass.
SETTLE_SECONDS = 2.0


def _database_url() -> str:
    return os.environ["DATABASE_URL"]


def _database_name() -> str:
    return _database_url().rsplit("/", 1)[-1].split("?")[0]


def _pool_min_size() -> int:
    from db.database import database_kwargs

    return database_kwargs["min_size"]


class _Probe:
    def __init__(self, control, baseline: int):
        self._control = control
        self.baseline = baseline

    async def extra(self) -> int:
        total = await self._control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        )
        return total - self.baseline

    async def settled_extra(self) -> int:
        await asyncio.sleep(SETTLE_SECONDS)
        for _ in range(30):
            extra = await self.extra()
            if extra <= 0:
                return extra
            await asyncio.sleep(0.1)
        return await self.extra()


@pytest.fixture
async def probe():
    import asyncpg

    from db.database import database
    from utils import database_readiness as readiness

    readiness._connect_attempt = None
    if database.is_connected:
        readiness._force_mark_database_disconnected()

    control = await asyncpg.connect(_database_url())
    try:
        yield _Probe(control, await control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        ))
    finally:
        readiness._connect_attempt = None
        if database.is_connected:
            try:
                await asyncio.wait_for(database.disconnect(), timeout=10)
            except Exception:
                readiness._force_mark_database_disconnected()
        await control.close()


def _stall(seconds: float):
    from asyncpg import connection as asyncpg_connection

    async def _connect(*args, **kwargs):
        await asyncio.sleep(seconds)
        return await asyncpg_connection.connect(*args, **kwargs)

    return _connect


@pytest.mark.asyncio
async def test_a_wave_of_callers_builds_one_pool(probe) -> None:
    """Six concurrent callers, one pool per blip — not six.

    ONE orphaned pool per blip is expected and is NOT this fix failing:
    `_force_mark_database_disconnected` drops the pool reference without
    closing it, deliberately, because #1683 established that an abandoned pool
    may still be finishing in-flight queries. What must not happen is a pool
    per CALLER.
    """
    from db.database import database
    from utils.database_readiness import _force_mark_database_disconnected, ensure_database_ready

    min_size = _pool_min_size()
    waves = 3

    for _ in range(waves):
        _force_mark_database_disconnected()
        results = await asyncio.gather(
            *(ensure_database_ready() for _ in range(CALLERS)), return_exceptions=True
        )
        assert not [r for r in results if isinstance(r, BaseException)], results[:2]

    live = await probe.extra()
    # The pool may grow past min_size to serve CALLERS concurrent probes, which
    # is ordinary pool behaviour, so allow one pool's worth of slack per wave.
    ceiling = (min_size + CALLERS) * waves
    assert live <= ceiling, (
        f"{live} backends after {waves} waves of {CALLERS} callers; one pool is "
        f"{min_size} and a pool per caller would be {CALLERS * min_size} per wave"
    )

    await asyncio.wait_for(database.disconnect(), timeout=10)
    orphaned = await probe.settled_extra()
    assert orphaned <= ceiling, (
        f"{orphaned} backends survived a clean disconnect — more than the one "
        f"abandoned pool per blip that #1683's non-destructive design accepts"
    )


@pytest.mark.asyncio
async def test_the_supervisor_does_not_collide_with_a_repair_leader(probe) -> None:
    """THE #1683 SEAM, and why the slot belongs at the connect layer.

    A slot only coalesces callers that reach it. `run_database_reconnect_supervisor`
    connects on a timer, and `Database.connect()` sets `is_connected` True only
    AFTER `create_pool()` returns while a repair sets it False before awaiting —
    so for the whole of a repair's connect the supervisor's guard reads True and
    fires. Both build a pool and the loser is unreachable for ever.

    Measured with the slot one layer up, in `ensure_database_ready`, one tick
    inside one leader's connect window: 10 backends open where one pool is 5,
    and 5 orphaned after a clean disconnect. Putting the slot under the function
    BOTH callers already share is what closes it.
    """
    from db.database import database
    from utils.database_readiness import (
        _force_mark_database_disconnected,
        ensure_database_ready,
        run_database_reconnect_supervisor,
    )

    min_size = _pool_min_size()
    backend = database._backend
    saved = backend._options
    backend._options = {**saved, "connect": _stall(1.5)}
    try:
        _force_mark_database_disconnected()

        leader = asyncio.create_task(
            ensure_database_ready(connect_timeout_seconds=30.0, probe_timeout_seconds=10.0)
        )
        await asyncio.sleep(0.75)          # now inside the leader's connect window
        await run_database_reconnect_supervisor(
            interval_seconds=0.01, connect_timeout_seconds=30.0, max_cycles=1
        )
        await asyncio.gather(leader, return_exceptions=True)

        during = await probe.extra()
        assert during <= min_size + CALLERS, (
            f"{during} backends open after the collision; one pool is {min_size}"
        )
    finally:
        backend._options = saved

    await asyncio.wait_for(database.disconnect(), timeout=10)
    orphaned = await probe.settled_extra()
    assert orphaned <= 0, (
        f"{orphaned} backends survived a clean disconnect — the supervisor and "
        "the repair each built a pool"
    )


@pytest.mark.asyncio
async def test_an_abandoned_fill_does_not_resurrect_a_disconnected_database(probe) -> None:
    """A fill nobody is waiting for must not reinstall a pool after teardown.

    The fill is detached from its callers so that no one caller's budget can
    truncate it — which is exactly what let an earlier design land a pool into a
    `Database` the process had already disconnected: 5 unreachable backends,
    with no caller left to close them. Waiters are counted; when the last one
    leaves, the fill is cancelled.
    """
    from db.database import database
    from utils.database_readiness import (
        _force_mark_database_disconnected,
        connect_database_with_timeout,
    )

    backend = database._backend
    saved = backend._options
    backend._options = {**saved, "connect": _stall(2.0)}
    try:
        _force_mark_database_disconnected()

        caller = asyncio.create_task(connect_database_with_timeout(15, db=database))
        await asyncio.sleep(0.3)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        await database.disconnect()       # a no-op: is_connected is already False
        await asyncio.sleep(4.0)          # long enough for the abandoned fill to land

        assert not database.is_connected, "a detached fill resurrected the database"
        assert database._backend._pool is None, "a pool was published with nobody waiting"
    finally:
        backend._options = saved

    orphaned = await probe.settled_extra()
    assert orphaned <= 0, f"{orphaned} backends survived an abandoned fill"
