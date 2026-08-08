"""A connect that times out must not strand the server backends it opened.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob). Nothing else in the
suite drives a connect that FAILS to complete: `tests/test_database_readiness.py`
runs a synchronous `FakeDatabase` with no pool and no server connections, so it
cannot see this class of defect at all. Real backend processes on a real
Postgres are the only place it is observable.

WHAT IS BEING TESTED. `databases.PostgresBackend.connect()` is

    assert self._pool is None
    self._pool = await asyncpg.create_pool(**kwargs)

with the assignment only AFTER the await. `asyncio.wait_for` cancels what it
waits on, so a timed-out connect leaves `backend._pool` None and `is_connected`
False while the backends `create_pool` had already opened stay open, referenced
by nothing and therefore closable by nothing. `create_pool` fills `min_size`
eagerly, so each timed-out attempt strands another batch. What reaches it in
production: `run_database_reconnect_supervisor` at 10.0s ON A TIMER, POST
/auth/login at 2.0s, the quote/order paths at the 3.0 default,
`accounts_orders_api` at 3s, and both startup connects at 15s. NOT `/health` —
#1683 moved it to `probe_database_health`, which connects nothing.

WHY THE ASSERTION IS SIMPLY "ZERO EXTRA". Elsewhere in this suite the sharp
question is what survives a clean `disconnect()`. Here it is sharper still:
these tests assert the wrapper is NOT connected, which means `backend._pool` is
None and there is no pool for `disconnect()` to reach even in principle. Every
extra backend at that point is unreachable by construction.

FAULT INJECTION, NOT SIMULATION. The failure is injected at asyncpg's own
`connect=` seam — the per-connection callable `create_pool` uses to open each
backend — reached through `backend._options`, which `_get_connection_kwargs()`
merges into the real kwargs. Everything above it is the production path:
`ensure_database_ready` -> `_connect_once` -> `connect_database_with_timeout`
-> the real `asyncpg.create_pool`, opening real backends on a real server. A
plain "use a very short timeout" test would be timing-dependent (measured: the
same 25ms budget both timed out and succeeded on this machine); stalling a
known number of holders makes the partial fill exact.

Connections are counted from an INDEPENDENT asyncpg control connection, never
through the pool under test, and against a baseline captured before each test,
so unrelated connections cannot mask a leak or invent one.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

# The pool the tests build. Small enough to stay well inside `max_connections`,
# big enough that a partial fill is unambiguous.
POOL_SIZE = 6
# How many holders get a real backend before the rest stall. `_initialize`
# connects the first holder on its own and then gathers the rest, so this must
# be >= 2 for the concurrent leg to be exercised as well as the sequential one.
CONNECTED_BEFORE_STALL = 3
# How long a stranded connection is given to appear before a count is believed.
# See `_Probe.settled_extra` — measured at ~1s on this machine; 2.0 is margin.
SETTLE_SECONDS = 2.0


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
        total = await self._control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        )
        return total - self.baseline

    async def settled_extra(self) -> int:
        """`extra()` once the server has reaped closed sockets AND any in-flight
        handshake has had time to land.

        THE MINIMUM WAIT IS LOAD-BEARING, not politeness. `Pool._initialize`
        gathers the remaining holder connects WITHOUT `return_exceptions`, so
        the first holder to raise propagates without cancelling its siblings.
        Those handshakes reach the server AFTER the failure returns, so polling
        for `extra <= 0` straight away certifies a clean run a moment before
        the leak exists. Measured on the `TooManyConnectionsError` path: 0
        immediately, 2 one second later, and 2 for ever after — a green test
        for a guarantee the code did not provide.
        """
        await asyncio.sleep(SETTLE_SECONDS)
        for _ in range(30):
            extra = await self.extra()
            if extra <= 0:
                return extra
            await asyncio.sleep(0.1)
        return await self.extra()


@pytest.fixture
async def probe():
    """Measure against a per-test baseline, on a DISCONNECTED wrapper.

    Unlike the sibling suite, these tests must start with the database NOT
    connected — a timed-out connect is only reachable when there is no pool
    yet. Per-test connect/disconnect is the house convention here because each
    test runs on a fresh event loop and an asyncpg pool cannot outlive its loop.
    """
    import asyncpg

    from db.database import database
    from utils.database_readiness import _force_mark_database_disconnected

    if database.is_connected:
        # A pool left over from another test's loop, which cannot be closed
        # from here. Drop the reference and let the baseline absorb it — every
        # assertion below is a delta.
        _force_mark_database_disconnected()

    control = await asyncpg.connect(_database_url())
    try:
        yield _Probe(control, await control.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _database_name(),
        ))
    finally:
        if database.is_connected:
            try:
                await asyncio.wait_for(database.disconnect(), timeout=10)
            except Exception:
                _force_mark_database_disconnected()
        await control.close()


@pytest.fixture
def partial_fill(monkeypatch: pytest.MonkeyPatch):
    """Make `create_pool` open exactly CONNECTED_BEFORE_STALL real backends.

    Returns a factory: pass what the remaining holders should do instead of
    connecting — stall forever (a slow database) or raise (a saturated one).
    `connect=` is asyncpg's documented per-connection hook, and
    `backend._options` is what `databases` merges into the pool kwargs, so the
    injection rides the production path rather than replacing it.
    """
    from asyncpg import connection as asyncpg_connection

    from db.database import database

    class _Fill:
        """What the injected hook has actually done, for tests to wait on.

        Counting real handshakes here rather than watching `probe.extra()` is
        deliberate: `extra()` is a DELTA against a baseline that deliberately
        absorbs connections from another test's closed loop, and the server
        reaps those at an arbitrary later moment — so the delta can sit
        permanently below the true count and a wait on it never finishes.
        """

        def __init__(self) -> None:
            self.attempts = 0
            self.opened = 0
            self.filled = asyncio.Event()

    def _install(rest) -> _Fill:
        state = _Fill()

        async def _connect(*args, **kwargs):
            state.attempts += 1
            if state.attempts > CONNECTED_BEFORE_STALL:
                await rest()
            con = await asyncpg_connection.connect(*args, **kwargs)
            state.opened += 1
            if state.opened >= CONNECTED_BEFORE_STALL:
                state.filled.set()
            return con

        backend = database._backend
        monkeypatch.setattr(
            backend,
            "_options",
            {**backend._options, "min_size": POOL_SIZE, "max_size": POOL_SIZE, "connect": _connect},
        )
        return state

    return _install


# A broken connect bound must produce a RED test, not a stuck one. Every call
# below is already bounded by the parameter under test, so this watchdog can
# only fire when that bound itself is broken — which is precisely the case that
# would otherwise burn a CI job's whole timeout with no output. Measured: a
# mutant removing the `wait_for` hung this file indefinitely.
# Strictly greater than every budget this file passes (max 30), so the
# watchdog and the bound it is watching can never race and misattribute.
WATCHDOG_SECONDS = 90


async def _bounded(coro):
    """Await `coro`, failing loudly if it outlives every timeout it should honour."""
    try:
        return await asyncio.wait_for(coro, timeout=WATCHDOG_SECONDS)
    except asyncio.TimeoutError:
        pytest.fail(
            f"the call outlived its own timeout by {WATCHDOG_SECONDS}s — the "
            "connect bound is broken, so this would have hung the suite"
        )


async def _never() -> None:
    """A handshake that never completes — a database that is merely slow."""
    await asyncio.sleep(3600)


async def _refused() -> None:
    """What a saturated server returns once its `max_connections` is gone.

    Not a hypothetical: it is what the stranded connections themselves cause.
    `Pool._async__init__` sets `_initialized = True` in a `finally` and never
    unwinds `_initialize`, so a create_pool that RAISES partway strands its
    already-connected holders with no cancellation involved at all — which is
    why the cleanup hangs off `BaseException` and not off the timeout alone.
    """
    from asyncpg import exceptions

    raise exceptions.TooManyConnectionsError("sorry, too many clients already")


@pytest.mark.asyncio
async def test_timed_out_connect_strands_no_backends(probe, partial_fill) -> None:
    from db.database import database
    from utils.database_readiness import DatabaseUnavailableError, ensure_database_ready

    fill = partial_fill(_never)

    budget = 1.0
    started = time.monotonic()
    with pytest.raises(DatabaseUnavailableError) as exc_info:
        await _bounded(ensure_database_ready(connect_timeout_seconds=budget))
    elapsed = time.monotonic() - started

    # The failure must still look exactly like it did before the fix; every
    # call site branches on nothing else.
    assert exc_info.value.phase == "connect"
    assert exc_info.value.error_type == "TimeoutError"

    # THE BUDGET IS THE POINT OF THE FUNCTION, so assert it, not just that it
    # eventually gave up. Nothing else in either suite pins the value passed to
    # `wait_for` on the Postgres path: scaling it (`timeout_seconds * 5`) left
    # all 262 dialect-gate tests green and only showed up as wall clock. In
    # production that silently turns /health's 5.0s into 25s — a Railway
    # healthcheck kill during exactly the outage this code exists for.
    assert elapsed < budget * 3, (
        f"connect took {elapsed:.2f}s against a {budget}s budget; the caller's "
        "timeout is not reaching asyncio.wait_for"
    )

    # Nothing is connected, so `backend._pool` is None and there is no pool for
    # `disconnect()` to reach. Anything still open is unreachable forever.
    assert not database.is_connected
    assert database._backend._pool is None

    leaked = await probe.settled_extra()
    assert fill.opened >= CONNECTED_BEFORE_STALL, (
        f"the fill opened only {fill.opened} backends, so there was nothing "
        "to strand and this assertion proves nothing"
    )

    assert leaked <= 0, (
        f"{leaked} server backends survived a timed-out connect with no pool "
        f"to close them; the fill had opened {CONNECTED_BEFORE_STALL}"
    )


@pytest.mark.asyncio
async def test_failed_connect_strands_no_backends(probe, partial_fill) -> None:
    """The same guarantee when create_pool RAISES instead of being cancelled."""
    from db.database import database
    from utils.database_readiness import DatabaseUnavailableError, ensure_database_ready

    fill = partial_fill(_refused)

    with pytest.raises(DatabaseUnavailableError) as exc_info:
        await _bounded(ensure_database_ready(connect_timeout_seconds=10.0))

    assert exc_info.value.phase == "connect"
    assert exc_info.value.error_type == "TooManyConnectionsError"
    assert not database.is_connected
    assert database._backend._pool is None

    leaked = await probe.settled_extra()
    assert fill.opened >= CONNECTED_BEFORE_STALL, (
        f"the fill opened only {fill.opened} backends, so there was nothing "
        "to strand and this assertion proves nothing"
    )

    assert leaked <= 0, (
        f"{leaked} server backends survived a failed connect with no pool to "
        f"close them; the fill had opened {CONNECTED_BEFORE_STALL}"
    )


@pytest.mark.asyncio
async def test_repeated_timeouts_do_not_accumulate(probe, partial_fill) -> None:
    """The leak's shape is unbounded growth — one attempt can look clean by luck.

    `run_database_reconnect_supervisor` runs on a timer, so a slow database
    means a timed-out connect every interval, each stranding another batch with
    nobody watching. Measured at DB_POOL_MIN_SIZE=20: 10 stranded by the first
    attempt and one more by each of the next three, none of them ever closable.
    """
    from utils.database_readiness import DatabaseUnavailableError, ensure_database_ready

    fill = partial_fill(_never)

    counts = []
    for _ in range(4):
        with pytest.raises(DatabaseUnavailableError):
            await _bounded(ensure_database_ready(connect_timeout_seconds=1.0))
        assert fill.opened >= CONNECTED_BEFORE_STALL, (
            f"wave opened only {fill.opened} backends — nothing to strand"
        )
        counts.append(await probe.settled_extra())

    assert max(counts) <= 0, (
        f"server backends accumulated across repeated timed-out connects: {counts}"
    )


@pytest.mark.asyncio
async def test_cancelled_connect_strands_no_backends(probe, partial_fill) -> None:
    """A cancelled caller must clean up too — that is why the guard is BaseException.

    `asyncio.CancelledError` derives from BaseException, not Exception, and a
    connect in flight is cancellable from outside for ordinary reasons: uvicorn
    shutting the worker down, or a request task torn down while it waits on
    `ensure_database_ready`. Cleaning up only on the timeout would strand the
    fill in exactly those cases.
    """
    from db.database import database
    from utils.database_readiness import connect_database_with_timeout

    fill = partial_fill(_never)

    connecting = asyncio.create_task(connect_database_with_timeout(3600))
    # Wait on the hook's own count of completed handshakes, never on a server
    # count: `probe.extra()` is a DELTA against a baseline that deliberately
    # absorbs another test's reaped connections, so it can sit permanently
    # below the true count. BOUNDED either way — a hung suite is a worse
    # failure than a red one.
    try:
        await asyncio.wait_for(fill.filled.wait(), timeout=10)
    except asyncio.TimeoutError:
        connecting.cancel()
        pytest.fail(
            f"the fill opened only {fill.opened} of {CONNECTED_BEFORE_STALL} "
            "backends; nothing to strand, so this test would prove nothing"
        )

    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting

    assert not database.is_connected
    assert database._backend._pool is None

    leaked = await probe.settled_extra()
    assert leaked <= 0, (
        f"{leaked} server backends survived a cancelled connect with no pool "
        "to close them"
    )


@pytest.mark.asyncio
async def test_successful_connect_publishes_a_closable_pool(probe) -> None:
    """The fix builds the pool itself, so prove the library can still close it.

    `connect_database_with_timeout` assigns `backend._pool` and `is_connected`
    by hand instead of letting `databases.connect()` do it. If either were
    wrong, `disconnect()` would not reach the pool — the same orphaning, just
    on the success path.
    """
    from db.database import database
    from utils.database_readiness import connect_database_with_timeout

    await _bounded(connect_database_with_timeout(30))

    assert database.is_connected
    assert database._backend._pool is not None
    assert await probe.extra() > 0, "expected an eagerly filled pool"

    # A real query, through the wrapper, on the pool this function published.
    assert await database.fetch_val("SELECT 1") == 1

    # And a transaction — `databases` acquires from `backend._pool` through its
    # own connection machinery, which is the part a hand-published pool could
    # plausibly leave in a state the library did not expect.
    async with database.transaction():
        assert await database.fetch_val("SELECT 2") == 2

    await asyncio.wait_for(database.disconnect(), timeout=10)
    leaked = await probe.settled_extra()
    assert leaked <= 0, f"{leaked} backends survived a clean disconnect"

    # A full cycle: the library must be able to reconnect over what this
    # function published and then tear that down too. `disconnect()` restores
    # `_pool = None`, so a second pass exercises the already-running guard's
    # happy side as well.
    await _bounded(connect_database_with_timeout(30))
    assert await database.fetch_val("SELECT 3") == 3
    await asyncio.wait_for(database.disconnect(), timeout=10)
    assert await probe.settled_extra() <= 0, "a second cycle leaked"


@pytest.mark.asyncio
async def test_supervisor_reconnect_strands_no_backends(probe, partial_fill) -> None:
    """The UNATTENDED call site, and the one that repeats on a timer.

    `run_database_reconnect_supervisor` exists to reconnect a process that
    started degraded, so by construction it runs against exactly the database
    this leak needs: one that is not answering. It is also the only caller on a
    LOOP — every cycle against a slow database stranded another `min_size`
    batch, with no request and no operator to notice. Nothing else in this file
    reaches it: the other tests drive `ensure_database_ready`, and reverting
    only the supervisor to `asyncio.wait_for(database.connect(), ...)` left
    every one of them green.
    """
    from db.database import database
    from utils.database_readiness import run_database_reconnect_supervisor

    fill = partial_fill(_never)

    # One cycle, with a budget far below the stalled fill so it must time out.
    await _bounded(
        run_database_reconnect_supervisor(
            interval_seconds=0.01, connect_timeout_seconds=1.0, max_cycles=1
        )
    )

    assert fill.opened >= 1, "the supervisor never attempted a connect"
    # The supervisor is deliberately non-destructive and swallows its own
    # failures, so the only evidence is the connection count.
    assert not database.is_connected
    assert database._backend._pool is None

    leaked = await probe.settled_extra()
    assert leaked <= 0, (
        f"{leaked} server backends survived a supervisor reconnect that timed "
        f"out; it opened {fill.opened}, and it runs on a timer for ever"
    )


@pytest.mark.asyncio
async def test_request_path_connect_strands_no_backends(probe, partial_fill) -> None:
    """The highest-frequency call site: `accounts_orders_api`, 3s, 9 endpoints.

    It reaches `database.connect()` on its own — not through
    `ensure_database_ready` — so nothing else in this file covers it. Reverting
    just this site to `asyncio.wait_for(database.connect(), timeout=3)` left the
    whole dialect gate AND the whole sweep green before this test existed, which
    is how a production leak walks back in unnoticed.
    """
    from fastapi import HTTPException

    from db.database import database
    from routes.accounts_orders_api import _ensure_database_connected

    fill = partial_fill(_never)

    # It converts any connect failure into a 503; the leak is the only other
    # evidence there is.
    with pytest.raises(HTTPException) as exc_info:
        await _bounded(_ensure_database_connected())
    assert exc_info.value.status_code == 503

    assert not database.is_connected
    assert database._backend._pool is None

    leaked = await probe.settled_extra()
    assert fill.opened >= CONNECTED_BEFORE_STALL, (
        f"the fill opened only {fill.opened} backends, so there was nothing to strand"
    )
    assert leaked <= 0, (
        f"{leaked} server backends survived a timed-out request-path connect"
    )
