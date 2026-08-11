from __future__ import annotations

import asyncio
import logging
import os
import weakref
from typing import Optional

from fastapi import HTTPException, status

from db.database import database
from utils.transient_errors import is_asyncpg_busy_error


logger = logging.getLogger(__name__)


class DatabaseUnavailableError(RuntimeError):
    def __init__(self, *, phase: str, error_type: str, message: str):
        super().__init__(message)
        self.phase = phase
        self.error_type = error_type
        self.message = message


def database_unavailable_http_exception(*, retry_after_seconds: int = 1) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(max(1, int(retry_after_seconds)))},
        detail={
            "error": "TEMPORARY_UNAVAILABLE",
            "message": "Temporary database unavailable. Please retry shortly.",
        },
    )


def _force_mark_database_disconnected() -> None:
    """
    Repair local `databases.Database` state after asyncpg reports a closed pool.

    `databases` leaves `is_connected=True` if backend disconnect times out or fails.
    In that state future `connect()` calls are skipped while requests keep raising
    `InterfaceError: pool is closed`. Force the wrapper back to disconnected so the
    next readiness pass creates a fresh pool.

    ⚠️ REACHED ON AN AMBIGUOUS SIGNAL. `ensure_database_ready` calls this from
    an unconditional `finally`, after a probe bounded at 3s — and a probe
    timeout does NOT prove the pool is dead (a slow database, or a merely
    saturated pool whose probe queues behind `acquire`, times out identically).
    A version of this function called `pool.terminate()` here; review round 19
    drove the consequence on a real pool: healthy pools destroyed and 30
    in-flight queries killed with `connection was closed in the middle of
    operation`. Abandoning the pool instead lets in-flight work finish.

    The cost of abandoning is a connection leak (round-17 R17-3: the orphaned
    pool's server connections stay open until the backend times them out).
    That leak is real but pre-existing and survivable; killing live queries on
    a healthy database is neither. Closing it properly means making the
    DECISION unambiguous before reaching here — separate work, deliberately
    not attempted from inside this function.
    """
    try:
        database.is_connected = False
    except Exception:
        pass

    backend = getattr(database, "_backend", None)
    if backend is not None and hasattr(backend, "_pool"):
        # DO NOT terminate() here. See the docstring: this runs on an
        # ambiguous signal, so terminating kills queries on a pool that may be
        # perfectly healthy. Dropping the reference leaks the pool's server
        # connections (round-17 R17-3) — the lesser harm, and the one that
        # does not take working requests down with it.
        try:
            backend._pool = None
        except Exception:
            pass


async def probe_database_health(*, probe_timeout_seconds: float = 3.0) -> None:
    """OBSERVE the database's state. Repair NOTHING. For health checks only.

    A HEALTH CHECK MUST NOT BE A REPAIR PATH. `/health` used to call
    `ensure_database_ready` (below), which reconnects and resets pool state
    before answering — so it reported the health of its own repair attempt
    rather than what request handlers see. Driven on the pre-fix commit: with
    the shared `database` disconnected, `/health` answered 200 five times out
    of five, `db_ok: true`, having silently reconnected on the first poll.

    An indicator that repairs what it measures cannot be trusted to reveal an
    outage, which matters because Railway's healthcheck points at `/health`.
    (The 12.5-hour outage of 2026-08-05/06 — `AssertionError: DatabaseBackend
    is not running` on every DB-backed request while `/health` stayed green —
    is what sent us looking here. Review round 16 could NOT reproduce that
    exact combination from this code path, so treat the connection as
    motivation, not as an established root cause: the mechanism that kept
    /health green for 12.5 hours is still unidentified.)

    This function connects nothing and resets nothing: if the shared
    `database` is not connected, that IS the answer. Repair is a separate
    concern with two homes, both deliberate: `ensure_database_ready` on the
    request paths, and `run_database_reconnect_supervisor` for the unattended
    case — because read-only traffic must not depend on someone logging in
    for the process to recover.
    """
    if not getattr(database, "is_connected", False):
        raise DatabaseUnavailableError(
            phase="disconnected",
            error_type="DatabaseBackendNotRunning",
            message="database backend is not running",
        )

    try:
        await asyncio.wait_for(database.execute("SELECT 1"), timeout=probe_timeout_seconds)
    except Exception as exc:
        raise DatabaseUnavailableError(
            phase="probe",
            error_type=type(exc).__name__,
            message="database readiness probe failed",
        ) from exc


# ---------------------------------------------------------------------------
# A timeout must not strand the server connections it already opened.
#
# WHY. `databases.PostgresBackend.connect()` is
#
#     assert self._pool is None
#     self._pool = await asyncpg.create_pool(**kwargs)
#
# and the assignment happens only AFTER the await. `asyncio.wait_for` cancels
# what it is waiting on when it times out, so on timeout the pool is never
# assigned: `backend._pool` stays None and `is_connected` stays False, while
# the server backends `create_pool` had ALREADY OPENED stay open. Nothing holds
# a reference to that pool, so nothing can ever close them — `disconnect()`
# only reaches the pool `backend._pool` currently points at. They stay open
# until Postgres times them out on its own.
#
# `create_pool` fills `min_size` connections EAGERLY, so one timed-out connect
# strands up to `min_size` backends, and every retry strands another batch.
#
# MEASURED on real Postgres, DB_POOL_MIN_SIZE=20, one caller, never-connected
# database, driving the real `ensure_database_ready`: 10 backends stranded by
# the first timed-out connect and one more by each of the next three, none of
# them reachable by a clean `disconnect()`. At `connect_timeout_seconds=0.015 /
# 0.020 / 0.025`: 3 / 10 / 9 stranded.
#
# WHAT REACHES IT IN PRODUCTION, on this base: `run_database_reconnect_supervisor`
# at 10.0s ON A TIMER — unattended, and pointed by design at a database that is
# not answering, so a slow one strands a batch per cycle for ever; POST
# /auth/login at 2.0s; the quote/order paths at the 3.0 default;
# `accounts_orders_api` at 3s; both startup connects at 15s. NOT `/health`: an
# earlier draft of this comment said it passed 5.0, which was true only against
# the PR #1684 base this was first written on. #1683 moved `/health` and `/` to
# `probe_database_health`, which connects nothing.
#
# THE FIX IS TO OWN THE POOL OBJECT. `asyncpg.create_pool()` is a plain
# function (not a coroutine): it returns the `Pool` synchronously and connects
# only when the Pool is awaited. Creating it and awaiting it as two statements
# is the same two steps in the same order that `databases` performs — but with
# a reference in hand, so `terminate()` can close what was opened.
#
# THIS COVERS THE FAILURE MODE A "DON'T CANCEL" DESIGN WOULD MISS. `Pool.
# _async__init__` sets `_initialized = True` in a `finally`, and never unwinds
# `_initialize` on error, so a create_pool that RAISES partway — the
# `TooManyConnectionsError` that a saturated server returns, which is exactly
# the state the leaked connections themselves produce — strands its already
# connected holders too, with no cancellation involved at all. Measured: 3
# backends stranded by a partial-init failure, 0 with `terminate()`. So the
# cleanup hangs off `BaseException`, not off the timeout.
#
# NOT A REIMPLEMENTATION OF `connect()`. The kwargs come from the backend's own
# `_get_connection_kwargs()` and its own parsed URL, and
# `test_connect_kwargs_match_the_library_backend` drives the real
# `PostgresBackend.connect()` and this function through the same recording
# `create_pool` and asserts the two argument sets are identical, so a
# `databases` upgrade that changes them fails the suite instead of silently
# diverging here.


def _postgres_backend(db):
    """The asyncpg-backed backend, or None for any other dialect.

    Local dev and most of the suite run `sqlite+aiosqlite`, which has no server
    to strand backends on and no `Pool` to own.
    """
    backend = getattr(db, "_backend", None)
    if backend is None:
        return None
    try:
        from databases.backends.postgres import PostgresBackend
    except Exception:
        # asyncpg not installed — db/database.py imports it lazily for exactly
        # this reason, so a Postgres backend cannot be in play either.
        return None
    return backend if isinstance(backend, PostgresBackend) else None


def _new_pool(backend):
    """Build the pool `PostgresBackend.connect()` would build, unconnected."""
    import asyncpg

    url = backend._database_url
    kwargs = dict(
        host=url.hostname,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    kwargs.update(backend._get_connection_kwargs())
    return asyncpg.create_pool(**kwargs)


# `terminate()` alone is not enough when the fill FAILS rather than times out.
# `Pool._initialize` fills the remaining holders with
# `await asyncio.gather(*connect_tasks)` — no `return_exceptions` — so the
# first holder to raise propagates WITHOUT cancelling its siblings. Those
# handshakes are still in flight when `terminate()` walks `_holders`, and a
# holder whose `_con` is not assigned yet is a no-op, so the connections land a
# moment later, attach to a discarded pool, and nothing ever closes them. No
# cancellation reaches them either, so asyncpg's own `except (Exception,
# CancelledError): tr.close()` never fires.
#
# MEASURED at DB_POOL_MIN_SIZE=5 on the `TooManyConnectionsError` path: 2 of 3
# opened backends stranded — 0 when counted immediately, which is why a probe
# that polls for "clean" without waiting reports a false pass. That is the
# shape this module cares most about: a saturated server ratcheting itself
# further into saturation on every poll.
#
# So the failure path re-sweeps: `terminate()` early-returns once `_closed`, so
# the sweep terminates surviving holder connections directly, several times,
# over a window long enough for a late handshake to land.
# KNOWN CONSEQUENCE, stated rather than hidden: the sweep is a superset of
# `terminate()` for the only thing these tests can observe — open server
# backends — so mutants that break `terminate()` alone (replacing it with
# `close()`, or terminating just one holder) now SURVIVE the suite where they
# previously died. That is the cost of a backstop, not a gap in the guarantee:
# the observable contract is "a discarded pool strands nothing", and it holds
# under either mechanism. `terminate()` stays because it is the documented API
# and it does the pool's own bookkeeping (`_closed`, holder release) that the
# sweep deliberately does not touch.
# Only the first pass is load-bearing for the suite — `(0.0,)` alone kills the
# no-sweep mutant, and truncating to `(0.0, 0.05, 0.25)` survives it. The tail
# is deliberate untested insurance for a handshake slower than a quarter
# second; its only measured cost is holding the discarded Pool reachable for
# the window (60 back-to-back failed connects peaked at 288 timer handles and
# 60 Pool objects, draining to zero afterwards).
_SWEEP_DELAYS = (0.0, 0.05, 0.25, 1.0, 3.0, 10.0)


def _sweep_quietly(pool) -> None:
    """Terminate any connection that landed after the pool was discarded."""
    try:
        for holder in getattr(pool, "_holders", ()) or ():
            con = getattr(holder, "_con", None)
            if con is not None and not con.is_closed():
                con.terminate()
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.debug(f"Sweeping a discarded database pool failed: {exc}")


def _terminate_quietly(pool) -> None:
    """Close what the pool opened, without ever replacing the caller's error.

    `terminate()` raises `InterfaceError` via `_check_init()` on a pool that
    never began initializing — which is also the case where it has nothing
    open to close.
    """
    try:
        pool.terminate()
    except Exception as exc:
        logger.debug(f"Discarding a failed database pool did not need termination: {exc}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop to schedule on
        return
    for delay in _SWEEP_DELAYS:
        loop.call_later(delay, _sweep_quietly, pool)


# ---------------------------------------------------------------------------
# ONE CONNECT IN FLIGHT PER DATABASE.
#
# `databases.Database.connect()` has no re-entrancy guard: it reads
# `is_connected`, awaits `create_pool()`, and only writes the flag afterwards,
# and `PostgresBackend.connect()`'s own `assert self._pool is None` is likewise
# checked before its await. So N concurrent callers all pass both guards, all
# build a pool, and each assigns `backend._pool` — last write wins and the
# losers are unreachable for ever, because `disconnect()` only ever closes the
# pool currently referenced.
#
# MEASURED on main, 6 concurrent `ensure_database_ready` callers at
# DB_POOL_MIN_SIZE=5, counted from an independent control connection:
# 50 -> 80 -> 96 backends over three waves, 85 still open after a clean
# `disconnect()`. `max_connections` (100) is gone in three waves — and this
# needs only concurrency on the ordinary recovery path, not a slow database.
#
# THE SLOT LIVES HERE because every one of the seven call sites already funnels
# through this function. Coalescing at any higher layer only covers the callers
# that reach it: `run_database_reconnect_supervisor` connects on a timer, and a
# slot in `ensure_database_ready` would not cover it — measured at 10 backends
# open where one pool is 5, 5 orphaned, when one supervisor tick lands inside
# one repair leader's connect window.
#
# THREE PROPERTIES, EACH PAID FOR BY A REVERTED ATTEMPT:
#
# 1. NO CALLER'S BUDGET TRUNCATES THE SHARED FILL. Call-site budgets span 2.0s
#    (/auth/login) to 15s (startup) to 10s on the supervisor's timer. An
#    attempt that ran under the first caller's `wait_for` starved every
#    longer-budget caller: 0 of 6 order requests served against 6 of 6, on a
#    database answering inside all of their budgets.
#
# 2. AN ATTEMPT STILL HAS TO END, or a wedged connect is never retried. The
#    deadline is `started + the largest budget any joiner asked for`, measured
#    from the ATTEMPT's start and not from each arrival — extending per arrival
#    never converges under sustained traffic, and measured 13 of 24 requests
#    served against 23 of 24, first success at 8.01s against 0.51s.
#
# 3. A FILL NOBODY IS WAITING FOR IS CANCELLED. Detaching the fill so property
#    1 holds also let it land after `disconnect()` and reinstall a pool into a
#    torn-down `Database` — 5 unreachable backends. Waiters are counted; when
#    the last one leaves, the fill is cancelled and its half-built pool
#    terminated.


class _ConnectAttempt:
    """One in-flight connect, and everyone waiting on it.

    The deadline lives HERE and not in a module global: an uncoalesced attempt
    (see `_join_connect`) must not be bounded by another database's budget.
    """

    __slots__ = ("loop", "db_ref", "future", "started", "deadline", "waiters", "runner")

    def __init__(self, loop, db, timeout_seconds: float):
        self.loop = loop
        self.db_ref = weakref.ref(db)
        self.future = loop.create_future()
        self.started = loop.time()
        self.deadline = self.started + max(0.0, timeout_seconds)
        self.waiters = 0
        self.runner = None

    def widen(self, timeout_seconds: float) -> None:
        """Cover a new joiner's budget, measured from the attempt's start."""
        self.deadline = max(self.deadline, self.started + max(0.0, timeout_seconds))


# At most one REGISTERED attempt. Uncoalesced ones are unregistered and simply
# not shared; they still get their own deadline and cleanup.
_connect_attempt: Optional["_ConnectAttempt"] = None


def _join_connect(db, timeout_seconds: float) -> "tuple[bool, _ConnectAttempt]":
    """Join the in-flight connect for `db`, or become the one that starts it.

    Contains no await, so the check-and-set cannot interleave with another
    caller — that atomicity is what makes this safe without a lock.
    """
    global _connect_attempt

    loop = asyncio.get_running_loop()
    current = _connect_attempt

    if current is not None and (current.loop is not loop or current.future.done()):
        # A registration from another (typically closed) event loop can never
        # be awaited here — it would hang rather than fail — and a finished one
        # is nobody's in-flight connect. Either way the slot is stale; drop it.
        # Without this, one closed loop wedged coalescing for the life of the
        # process: measured at 6 attempts for 6 callers where 1 was expected.
        _connect_attempt = current = None

    if current is not None:
        if current.db_ref() is db:
            current.widen(timeout_seconds)
            return False, current
        # Someone else's connect is live. NEVER evict it: that would leave its
        # database with no registration, so its next caller would start a
        # second fill behind its back — the very defect this exists to prevent.
        # Run unregistered instead; a missed coalesce is the old behaviour, a
        # second concurrent fill is a leak.
        return True, _ConnectAttempt(loop, db, timeout_seconds)

    _connect_attempt = _ConnectAttempt(loop, db, timeout_seconds)
    return True, _connect_attempt


def _retire_connect(attempt: "_ConnectAttempt") -> None:
    """Free the slot if this attempt still owns it.

    DELIBERATE SURVIVOR: making this a no-op does not fail the suite, because
    `_join_connect` also drops a registration whose future is `done()`. The two
    overlap by design — this frees the slot the instant the runner knows the
    attempt is over, the other is the backstop for a slot nobody retired. What
    is lost without this is only that a caller arriving in the window between
    the runner deciding and the runner publishing joins a dying attempt and
    pays one extra trip round the retry loop.
    """
    global _connect_attempt

    if _connect_attempt is attempt:
        _connect_attempt = None


async def _connect_now(db) -> None:
    """Build and publish one pool. Exactly one of these runs per attempt."""
    backend = _postgres_backend(db)
    if backend is None or getattr(db, "_force_rollback", False):
        # Not the leaking shape: no asyncpg pool to own, or `force_rollback`
        # means `connect()` also opens a global transaction that only the
        # library can manage. Behave exactly as before.
        await db.connect()
        return

    if backend._pool is not None:
        # The guard `PostgresBackend.connect()` opens with, kept because this
        # function publishes `_pool` by hand: assigning over a pool that is
        # already there would orphan it — the same leak, from the other end.
        # Raised rather than `assert`ed so `python -O` cannot strip it, but with
        # the library's own exception type and message so callers see no change.
        raise AssertionError("DatabaseBackend is already running")

    pool = _new_pool(backend)
    try:
        await pool
    except BaseException:
        _terminate_quietly(pool)
        raise

    if backend._pool is not None or getattr(db, "is_connected", False):
        # The world moved while we were filling — something else published a
        # pool. Assigning over it would orphan a LIVE pool, which is strictly
        # worse than failing this connect.
        #
        # DELIBERATE SURVIVOR: with the slot in place two fills for the same
        # database should not overlap, so no test reaches this. It is kept
        # because it is the last thing standing between a coordination bug and
        # an orphaned live pool, and because the pre-await guard above cannot
        # see a pool that appears DURING the fill.
        _terminate_quietly(pool)
        raise AssertionError("DatabaseBackend is already running")

    # Publish in the library's order: the pool first, then the flag that tells
    # every other caller the pool is there.
    backend._pool = pool
    db.is_connected = True
    # `Database.connect()` logs this and is never called on this path, so
    # without it incident logs show disconnects with no matching connects.
    logger.info("Connected to database %s", getattr(db, "url", "<unknown>"))


async def _run_connect_attempt(attempt: "_ConnectAttempt") -> None:
    """Own the fill: bound it by the deadline, publish once, never raise out."""
    db = attempt.db_ref()
    outcome: Optional[BaseException] = asyncio.TimeoutError()
    fill = asyncio.ensure_future(_connect_now(db)) if db is not None else None
    try:
        while fill is not None and not fill.done():
            if attempt.waiters <= 0:
                # Nobody is left to receive this pool, and a fill that lands
                # after everyone has gone reinstalls a pool into a Database the
                # process may already have torn down.
                break
            remaining = attempt.deadline - attempt.loop.time()
            if remaining <= 0:
                break
            await asyncio.wait({fill}, timeout=min(remaining, 0.05))
        if fill is not None and fill.done() and not fill.cancelled():
            outcome = fill.exception()
    except BaseException:  # pragma: no cover - loop teardown only
        pass
    finally:
        # Free the slot BEFORE releasing the waiters, so a waiter that still
        # has budget and re-claims gets a fresh attempt rather than this one.
        _retire_connect(attempt)
        if not attempt.future.done():
            # A result, never an exception: one nobody retrieves (because every
            # waiter had already gone) is logged by asyncio as
            # "Future exception was never retrieved".
            attempt.future.set_result(outcome)
        if fill is not None and not fill.done():
            fill.cancel()
            # `_connect_now` terminates its half-built pool on CancelledError;
            # let that run rather than dropping the task on the floor.
            await asyncio.gather(fill, return_exceptions=True)


async def connect_database_with_timeout(timeout_seconds: float, *, db=None) -> None:
    """`db.connect()`, bounded, single-flight, without stranding connections.

    Raises exactly what `asyncio.wait_for(db.connect(), ...)` raises —
    `asyncio.TimeoutError` on timeout, the underlying error otherwise — so
    every call site's error handling is unchanged.

    `db` defaults to the shared `database`, but EVERY CALL SITE PASSES ITS OWN
    module's reference. Call sites are monkeypatched by name in this suite
    (`bf.database`, `readiness.database`), and a helper that silently reached
    for its own global instead would connect the real database out from under
    a test that thought it had substituted a fake.
    """
    db = database if db is None else db

    # FAST PATH — no coordination at all for an already-connected database,
    # which is every call on every hot request path.
    if getattr(db, "is_connected", False):
        return

    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + max(0.0, timeout_seconds)

    while True:
        remaining = give_up_at - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()

        is_leader, attempt = _join_connect(db, remaining)
        attempt.waiters += 1
        if is_leader:
            attempt.runner = asyncio.ensure_future(_run_connect_attempt(attempt))

        try:
            # shield: `wait_for` cancels what it waits on, and this future is
            # SHARED — cancelling it would take down every co-waiter and the
            # runner's publish along with us.
            failure = await asyncio.wait_for(
                asyncio.shield(attempt.future), timeout=remaining
            )
        finally:
            attempt.waiters -= 1

        if failure is None:
            return
        if isinstance(failure, asyncio.TimeoutError) and loop.time() < give_up_at:
            # The ATTEMPT ran out of road, not us. Ours is the budget that
            # decides whether to try again, which is what keeps a caller from
            # doing worse by joining than by connecting alone.
            continue
        # A fresh instance per waiter: re-raising one shared object across
        # tasks accumulates tracebacks on it and lets concurrent raises race
        # each other's `__context__`. Call sites branch on the TYPE.
        raise _reraisable(failure)


def _reraisable(exc: BaseException) -> BaseException:
    """A private copy of the attempt's failure for one waiter to raise."""
    try:
        copy = type(exc)(*exc.args)
    except Exception:
        return exc
    copy.__cause__ = exc.__cause__
    return copy


async def ensure_database_ready(
    *,
    connect_timeout_seconds: float = 3.0,
    probe_timeout_seconds: float = 3.0,
    disconnect_timeout_seconds: float = 1.0,
) -> None:
    """
    Best-effort request-time database recovery.

    Production startup is intentionally allowed to continue in degraded mode when DB
    connection times out. That keeps the service deployable, but quote/order flows need
    a way to recover later when the DB becomes reachable again.
    """

    async def _connect_once() -> None:
        try:
            await connect_database_with_timeout(connect_timeout_seconds, db=database)
        except Exception as exc:  # pragma: no cover - covered via outer behavior tests
            raise DatabaseUnavailableError(
                phase="connect",
                error_type=type(exc).__name__,
                message="database connect failed",
            ) from exc

    async def _probe_once(phase: str) -> None:
        try:
            await asyncio.wait_for(database.execute("SELECT 1"), timeout=probe_timeout_seconds)
        except Exception as exc:
            raise DatabaseUnavailableError(
                phase=phase,
                error_type=type(exc).__name__,
                message="database readiness probe failed",
            ) from exc

    if not getattr(database, "is_connected", False):
        await _connect_once()

    try:
        await _probe_once("probe")
        return
    except DatabaseUnavailableError as first_err:
        logger.warning(
            "Database readiness probe failed; attempting one reconnect",
            extra={"phase": first_err.phase, "error_type": first_err.error_type},
        )

    try:
        if getattr(database, "is_connected", False):
            await asyncio.wait_for(database.disconnect(), timeout=disconnect_timeout_seconds)
    except Exception as exc:
        logger.warning(f"Database disconnect during recovery failed: {exc}")
    finally:
        _force_mark_database_disconnected()

    await _connect_once()

    try:
        await _probe_once("reconnect_probe")
    except DatabaseUnavailableError as exc:
        if is_asyncpg_busy_error(exc.__cause__ or exc):
            _force_mark_database_disconnected()
        raise


DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS = 30.0
DEFAULT_RECONNECT_CONNECT_TIMEOUT_SECONDS = 10.0
# Upper bound so a typo cannot silently retire the supervisor (review round 20).
MAX_RECONNECT_SUPERVISOR_INTERVAL_SECONDS = 300.0

_SUPERVISOR_ENABLED_ENV = "DB_RECONNECT_SUPERVISOR_ENABLED"
_SUPERVISOR_INTERVAL_ENV = "DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS"


def _pool_is_provably_dead() -> bool:
    """True only when the pool object ITSELF says it cannot serve — no probe,
    no timeout, no inference.

    THIS IS THE DISTINCTION FOUR REVIEW ROUNDS FAILED TO DRAW. Every earlier
    attempt decided liveness from a `SELECT 1` under a timeout, which cannot
    tell a dead pool from a slow or saturated one — and repairing on that
    guess destroyed healthy pools and killed in-flight queries. A pool that is
    `None`, or whose own `_closed` flag is set, is dead as a matter of local
    fact: there is nothing running on it to lose. Acting on that is safe in a
    way acting on a probe result never was.

    Deliberately NOT covered: a pool that exists and is open but whose
    connections are unusable. Distinguishing that from a slow database
    requires exactly the inference that kept going wrong, so it stays with
    request-path recovery.

    `_closed` is asyncpg PRIVATE API, so `test_asyncpg_still_exposes_the_
    attribute_this_depends_on` binds it to the real class: if asyncpg renames
    it, `getattr(..., False)` would silently mean "never dead" and this arm
    would vanish without a single test failing. Driven on real asyncpg:
    `_closed` is True only AFTER in-flight work drains (a closing pool reports
    `_closing=True, _closed=False`), which is what makes acting on it safe.
    Note the pool arm is inert on the SQLite backend — `SQLiteBackend._pool`
    is always a live object with no `_closed` — so it is exercised against
    real asyncpg only.
    """
    backend = getattr(database, "_backend", None)
    if backend is None or not hasattr(backend, "_pool"):
        return False
    pool = getattr(backend, "_pool", None)
    if pool is None:
        return True
    return bool(getattr(pool, "_closed", False))


def _supervisor_enabled() -> bool:
    """Kill switch, re-read every cycle so it works without a redeploy."""
    return os.getenv(_SUPERVISOR_ENABLED_ENV, "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _supervisor_interval() -> float:
    raw = os.getenv(_SUPERVISOR_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    # NaN/inf survive float() and would wedge the loop on sleep(inf) with no
    # log line, silently disabling the supervisor (round 19). So would a
    # finite-but-huge typo (86400 = once a day), the same defect by a more
    # plausible mistake (round 20) — so the clamp has BOTH ends, like
    # main.py's _health_timeout_seconds. Anything non-finite falls back,
    # loudly.
    if not (value == value) or value in (float("inf"), float("-inf")):
        logger.warning(
            "invalid %s=%r; using %.1fs",
            _SUPERVISOR_INTERVAL_ENV, raw,
            DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS,
        )
        return DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS
    clamped = min(max(1.0, value), MAX_RECONNECT_SUPERVISOR_INTERVAL_SECONDS)
    if clamped != value:
        logger.warning(
            "%s=%r out of range; clamped to %.1fs",
            _SUPERVISOR_INTERVAL_ENV, raw, clamped,
        )
    return clamped


async def run_database_reconnect_supervisor(
    *,
    interval_seconds: Optional[float] = None,
    connect_timeout_seconds: float = DEFAULT_RECONNECT_CONNECT_TIMEOUT_SECONDS,
    max_cycles: Optional[int] = None,
) -> None:
    """Reconnect a process whose backend is DISCONNECTED. Nothing else.

    WHY IT EXISTS. Startup deliberately continues when the initial DB connect
    fails (main.py: "keep the service up"), and nothing walked that back
    except `/health` reconnecting as a side effect of being polled. Making
    `/health` honest (see `probe_database_health`) removes that accidental
    heal, leaving only `ensure_database_ready` on POST login/quote/order — so
    a process serving read-only traffic would never recover. This closes that
    specific hole and no other.

    ⚠️ DELIBERATELY NON-DESTRUCTIVE, AND THAT IS THE WHOLE DESIGN. It acts
    only on LOCAL FACTS — the wrapper reports disconnected, or the pool object
    is `None`/`_closed` (`_pool_is_provably_dead`) — never on a probe result.
    And it calls `database.connect()`, never `ensure_database_ready`, whose
    unconditional `finally: _force_mark_database_disconnected()` resets pool
    state on a 3s-probe timeout that cannot tell a slow database from a dead
    one.

    WHY THAT IS SAFE, stated precisely: NOT "there is no pool in use" — after
    `_force_mark_database_disconnected` the flag is False while an abandoned
    pool may still be finishing in-flight queries, which is exactly why we
    stopped calling `terminate()`. The safety is that `database.connect()`
    builds a FRESH pool and never touches the old one, so live work on the
    abandoned pool completes undisturbed (driven: 25 queries completed, same
    pool object, zero killed).

    Four review rounds were spent trying to make a repairing supervisor safe
    (PR #1683, rounds 16-19). Each fix moved the destruction one layer further
    from the decision — a flag, then a probe, then a classifier, then a
    failure threshold — while the destruction itself stayed unconditional
    three layers down, and round 19 drove it destroying healthy pools and
    killing 30 in-flight queries. The lesson kept: a destructive actuator must
    not hang off an inference drawn from one `SELECT 1` under a timeout.

    COVERAGE, MEASURED (review round 20, real Postgres, zero request traffic):
      * degraded start (`is_connected` False)      — RECOVERED
      * `_pool` is None, flag True                 — RECOVERED (this is the
        other producer of the incident's own `AssertionError: DatabaseBackend
        is not running`)
      * pool closed client-side (`InterfaceError`) — RECOVERED
      * pool EXHAUSTED on a healthy database       — deliberately NOT touched
    The last one is the whole point: it is indistinguishable from a slow
    database without guessing, and guessing is what destroyed live pools in
    rounds 18 and 19. Request traffic through `ensure_database_ready` remains
    its only repair.

    NOT AN APSCHEDULER JOB: during the 2026-08-05/06 incident every scheduler
    job was wedged at max_instances, and a repair mechanism must not share
    fate with what it repairs. Never raises except CancelledError.
    """
    interval = _supervisor_interval() if interval_seconds is None else interval_seconds

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        await asyncio.sleep(interval)

        if not _supervisor_enabled():
            continue

        # Two LOCAL facts, never a probe: the wrapper says disconnected, or
        # the pool object itself is gone/closed. Both mean there is nothing
        # live to damage. Anything else — including a pool whose connections
        # are merely slow or exhausted — is left alone.
        connected = getattr(database, "is_connected", False)
        dead_pool = _pool_is_provably_dead()
        if connected and not dead_pool:
            continue

        # Clear stale wrapper state UNCONDITIONALLY before reconnecting.
        # There are TWO stale shapes and each breaks connect() differently:
        #   * flag True + pool gone  -> `databases.connect()` early-returns,
        #     so the "reconnect" is a silent no-op;
        #   * flag False + pool set  -> `PostgresBackend.connect()`'s
        #     `assert self._pool is None` raises "DatabaseBackend is already
        #     running", wedging EVERY later cycle (round-21 F1; the same
        #     wedge scripts/backfill_external_seed_quality_rescore.py:213
        #     already documents from a bounded disconnect).
        # An earlier version cleared only the first, which is this arc's
        # signature mistake: fixing one of two symmetric cases. The call is
        # non-destructive by construction — it drops a reference, it does not
        # terminate — so doing it always costs nothing.
        _force_mark_database_disconnected()

        try:
            await connect_database_with_timeout(
                connect_timeout_seconds, db=database
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — must outlive every failure
            logger.warning(
                "database reconnect supervisor: reconnect FAILED (%s: %s)",
                type(exc).__name__, exc,
            )
            continue

        logger.warning(
            "database reconnect supervisor: reconnected a dead backend"
        )
