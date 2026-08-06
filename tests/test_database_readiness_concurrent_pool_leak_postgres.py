"""`ensure_database_ready` must build ONE pool per repair, not one per caller.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob), and so is the fact
that this file exists at all. `tests/test_database_readiness.py` drives a
synchronous `FakeDatabase` and STRUCTURALLY CANNOT catch this class of defect:
a fake has no pool, no server connections, and its `connect()` never yields, so
callers can never overlap inside it. The defect is only observable as real
backend processes on a real Postgres.

WHAT IS BEING TESTED. `databases.Database.connect()` has no re-entrancy guard —
it reads `is_connected`, awaits `create_pool()`, and only then writes the flag,
while the Postgres backend's `assert self._pool is None` is likewise checked
before its own await. N concurrent callers therefore all build a pool and each
assigns `backend._pool`; the last write wins and the other N-1 pools are
unreachable. Nothing can ever close them, because `disconnect()` only closes
the pool currently referenced.

So the sharp assertion is not "how many connections are open" but "how many
survive a clean shutdown". An orphaned pool is precisely one that a clean
`disconnect()` cannot reach. Measured on the parent commit at
DB_POOL_MIN_SIZE=5 with 5 concurrent callers: 30 -> 60 -> 90 -> 99 server
connections over four blips, exhausting `max_connections` (100, the production
default) until every caller failed with "database connect failed" — the
recovery path taking the database down for every other client.

Connections are counted from an INDEPENDENT asyncpg control connection, never
through the pool under test, so the measurement cannot be an artifact of the
thing being measured. Counts are compared against a baseline captured before
each test connects, so unrelated connections on the same database (other
suites' engines) cannot make a leak look clean or a clean run look leaky.
"""

from __future__ import annotations

import asyncio
import os
import weakref

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

CONCURRENT_CALLERS = 5


def _database_url() -> str:
    return os.environ["DATABASE_URL"]


def _database_name() -> str:
    return _database_url().rsplit("/", 1)[-1].split("?")[0]


class _Probe:
    """An independent view of the server, plus the count we started from."""

    def __init__(self, control, baseline: int):
        self._control = control
        self.baseline = baseline

    async def extra(self) -> int:
        """Backend processes beyond the baseline, counted from OUTSIDE the pool."""
        total = await self._control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        )
        return total - self.baseline

    async def settled_extra(self) -> int:
        """`extra()` once the server has finished reaping closed sockets."""
        for _ in range(30):
            extra = await self.extra()
            if extra <= 0:
                return extra
            await asyncio.sleep(0.1)
        return await self.extra()


@pytest.fixture
async def probe():
    """Connect/disconnect per test, and measure against a per-test baseline.

    Per-test connect/disconnect is the house convention for this suite
    (`test_acp_checkout_sessions_postgres.py`): each test runs on a fresh event
    loop and an asyncpg pool that outlives its loop cannot be closed.
    """
    import asyncpg

    from db.database import database
    from utils.database_readiness import _force_mark_database_disconnected

    if database.is_connected:
        # A pool left over from another test's loop. It cannot be closed from
        # here, so drop the reference and let the baseline absorb whatever it
        # still holds — the assertions below are all deltas.
        _force_mark_database_disconnected()

    control = await asyncpg.connect(_database_url())
    try:
        probe = _Probe(control, await control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        ))
        await database.connect()
        yield probe
    finally:
        if database.is_connected:
            try:
                await asyncio.wait_for(database.disconnect(), timeout=10)
            except Exception:
                _force_mark_database_disconnected()
        await control.close()


async def _break_the_pool() -> None:
    """Produce the exact shape the recovery path exists for.

    Closing the asyncpg pool underneath the wrapper leaves `is_connected` True
    while every query raises `InterfaceError: pool is closed` — the state
    `_force_mark_database_disconnected`'s docstring describes. `close()`
    releases its own server connections, so the blip itself leaks nothing and
    anything left over afterwards is attributable to the repair.
    """
    from db.database import database

    await database._backend._pool.close()


def _pool_min_size() -> int:
    from db.database import database_kwargs

    return database_kwargs["min_size"]


@pytest.mark.asyncio
async def test_concurrent_repair_builds_one_pool_and_orphans_none(probe) -> None:
    from db.database import database
    from utils.database_readiness import ensure_database_ready

    min_size = _pool_min_size()
    assert await probe.extra() >= min_size, "expected an eagerly filled pool"

    await _break_the_pool()

    results = await asyncio.gather(
        *(ensure_database_ready() for _ in range(CONCURRENT_CALLERS)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"repair failed: {failures[:2]}"

    # One repair, one pool. The parent commit built CONCURRENT_CALLERS of them
    # here. A little slack above min_size for connections asyncpg may have
    # grown on demand; the point is that it is nowhere near
    # CONCURRENT_CALLERS * min_size.
    during = await probe.extra()
    assert during <= min_size + 2, (
        f"{during} server connections after one repair wave; one pool is "
        f"{min_size}, {CONCURRENT_CALLERS} pools would be "
        f"{CONCURRENT_CALLERS * min_size}"
    )

    # THE ORPHAN ASSERTION. `disconnect()` can only reach the pool that
    # `backend._pool` currently points at, so whatever is still open after a
    # clean shutdown is unreachable and stays open until Postgres times it out.
    await asyncio.wait_for(database.disconnect(), timeout=10)
    leaked = await probe.settled_extra()
    assert leaked <= 0, (
        f"{leaked} server connections survived a clean disconnect — "
        "orphaned pools that nothing can ever close"
    )


@pytest.mark.asyncio
async def test_repeated_blips_do_not_grow_the_connection_count(probe) -> None:
    """The leak is unbounded growth, not a one-off — drive several waves.

    A single wave can be made to look fine by luck (one caller winning the
    race outright). Four waves is what exhausted `max_connections` on the
    parent commit.
    """
    from utils.database_readiness import ensure_database_ready

    min_size = _pool_min_size()

    counts = []
    for _ in range(4):
        await _break_the_pool()
        await asyncio.gather(
            *(ensure_database_ready() for _ in range(CONCURRENT_CALLERS)),
            return_exceptions=True,
        )
        counts.append(await probe.extra())

    assert max(counts) <= min_size + 2, (
        f"connection count grew across blips: {counts} (one pool is {min_size})"
    )


@pytest.mark.asyncio
async def test_healthy_calls_do_not_wait_on_an_in_flight_repair(probe) -> None:
    """The fast path must not coordinate at all — these are hot request paths.

    `ensure_database_ready` sits on `/health`, POST /auth/login,
    /quotes/preview, /orders/create and /orders/payment/confirm. If a healthy
    call joined the single-flight slot, one slow repair would stall every order
    in flight. A repair is parked in the slot for real here, so a regression
    that claims the slot before probing fails this outright rather than merely
    slowing it down.
    """
    from utils import database_readiness as readiness

    # Park a repair in the slot without running one: exactly what a follower
    # would find, and what a healthy caller must ignore.
    loop = asyncio.get_running_loop()
    parked = loop.create_future()
    readiness._repair_loop = weakref.ref(loop)
    readiness._repair_inflight = parked
    try:
        await asyncio.wait_for(readiness.ensure_database_ready(), timeout=2.0)
    except asyncio.TimeoutError:  # pragma: no cover - the regression signal
        pytest.fail(
            "a healthy ensure_database_ready() waited on an in-flight repair; "
            "the fast path must probe before claiming the slot"
        )
    finally:
        assert not parked.done(), "the fast path must not touch the slot"
        readiness._publish_repair_outcome(parked, None)
