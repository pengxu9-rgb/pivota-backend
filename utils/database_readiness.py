from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Optional

from fastapi import HTTPException, status

from db.database import database
from utils.transient_errors import is_asyncpg_busy_error


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# One repair at a time.
#
# WHY. `databases.Database.connect()` HAS NO RE-ENTRANCY GUARD:
#
#     if self.is_connected: return        # read
#     await self._backend.connect()       # asyncpg.create_pool(), assigns _pool
#     self.is_connected = True            # write
#
# and the Postgres backend's own `assert self._pool is None` is checked before
# its await. `_connect_once` below wraps the call in `asyncio.wait_for`, which
# schedules a Task, so N concurrent callers arrive as N tasks, ALL see
# `is_connected` False, ALL pass both guards, ALL build a pool, and each
# assigns `backend._pool` — last write wins. The other N-1 pools are
# unreachable, so nothing ever closes them: `disconnect()` only ever closes the
# pool currently referenced. Their server connections stay open until Postgres
# times them out.
#
# `_force_mark_database_disconnected` is what puts every caller in that state
# at once, so the whole request wave collides on exactly the recovery path.
#
# MEASURED on real Postgres, DB_POOL_MIN_SIZE=5 (asyncpg opens min_size
# eagerly), 5 concurrent callers per blip: 30 -> 60 -> 90 -> 99 server
# connections over four blips, at which point `max_connections` (100 — the
# production default) was exhausted and EVERY caller began failing with
# "database connect failed". 99 connections were still open after a clean
# `disconnect()`. That is the recovery path taking the database down for every
# other client, during the outage it exists to recover from.
#
# SINGLE-FLIGHT, NOT A LOCK. Callers do not queue for a turn at repairing;
# exactly one becomes the leader and everyone else waits for ITS OUTCOME —
# success or failure — and adopts it.
#
# A plain lock fixes the leak and introduces a worse bug. Followers would wake
# up one at a time and each run the whole repair (re-probe + disconnect +
# connect + reconnect_probe), which is fine when the repair SUCCEEDS (the next
# caller's probe passes and it returns) and catastrophic when it FAILS — which
# is the condition this function exists for. Measured against a live-but-wedged
# Postgres at production-default timeouts, with the lock version:
#
#     callers   locked      parent
#        5      33.0s        6.0s
#       20     123.1s        6.0s
#
# and under sustained arrivals the queue never drains at all: 9s, 44s, 79s,
# climbing ~5s per second of traffic. `/health` (main.py) is one of these
# callers and Railway's healthcheck points at it, so that is a container kill
# in the middle of an outage. Sharing the outcome makes the worst case ONE
# repair for everybody, no matter how many callers arrive.
#
# The leader skips the extra probe, so its latency matches the parent commit
# exactly and this fix costs nothing on any path. The justification is narrow
# and worth stating precisely, because a wider one is tempting and false:
# `_claim_repair` is a plain `def` and no await separates the fast path's
# verdict from claiming the slot, so no OTHER caller can interleave — but the
# verdict itself can be up to `probe_timeout_seconds` STALE, because the probe
# that produced it may have spent that long timing out. A leader can therefore
# tear down a pool that someone else rebuilt while its probe was hanging.
#
# That staleness is pre-existing: the parent probes exactly once here too and
# was measured behaving identically, so this change neither introduces nor
# worsens it. It is also NOT mitigated anywhere yet, and this comment used to
# claim otherwise — it cited a `pool.terminate()` plus `is_dead_pool_error`
# guard in PR #1683. Checked against that branch at 74ba785d: `terminate()` was
# REMOVED by its own round 19 ("DO NOT terminate() here"), and
# `is_dead_pool_error` does not exist in the branch at all. The cited guard is
# gone, so the window is simply open, on this branch and on main alike.
#
# The teardown it can reach is `_force_mark_database_disconnected`, which today
# only drops the reference — live queries on the abandoned pool finish
# undisturbed, and it is #1683's deliberate design that they keep doing so. The
# cost is therefore a wasted teardown and rebuild, not killed work. Closing the
# window means re-probing before the teardown, which doubles the probe cost on
# the degraded path and changes the parent's probe semantics; that is a
# separate decision, not a silent rider on a leak fix. Do not read the skipped
# probe as evidence that the window is closed.
#
# WHY NOT A MODULE-LEVEL PRIMITIVE (this repo's idiom for the ~25 `_DDL_LOCK`s
# in db/*). Both `asyncio.Lock` and `asyncio.Future` are loop-bound: a
# module-level Lock binds to the first event loop that CONTENDS it and raises
# `RuntimeError: ... is bound to a different event loop` on every loop after
# (measured on CPython 3.11.14; uncontended acquires never bind, which is why
# the DDL locks get away with it). Awaiting a Future from the wrong loop hangs.
# Every call site catches `DatabaseUnavailableError` and nothing else, so a
# stray RuntimeError would be a 500 on the order path where a 503 belongs.
#
# A single slot rather than a dict keyed by loop: a Future holds `_loop`, a
# strong reference back to the key, so a WeakKeyDictionary would retain every
# event loop the process ever ran (measured). The web process has exactly one
# loop — `app_lifespan` awaits both startup connects sequentially and uvicorn
# serves nothing until it returns, APScheduler is `AsyncIOScheduler` on that
# same loop, and `asyncio.to_thread` runs sync callables only. KNOWN LIMIT: if
# a second event loop were ever run CONCURRENTLY (not merely afterwards), it
# would take the slot and the two loops would silently stop coalescing — a
# leak, not a crash. Verified absent across main.py, routes/, utils/,
# services/ and db/; re-check before introducing a worker-thread loop.
#
# 🚨 THE SLOT ONLY COALESCES CALLERS THAT CLAIM IT. Any other code that calls
# `database.connect()` in this process bypasses it completely and reinstates
# the orphan leak above, because `Database.connect()` sets `is_connected` only
# AFTER `create_pool()` returns — so for the whole of a leader's connect, an
# `if not is_connected: connect()` guard elsewhere reads True and fires.
#
# This is not hypothetical. PR #1683 (`claude/health-check-truthful`, at
# 74ba785d, UNMERGED) adds `run_database_reconnect_supervisor`, which calls
# `database.connect()` directly on a timer and deliberately NOT
# `ensure_database_ready` — a sound choice on its own terms, since it refuses
# to inherit this function's unconditional
# `finally: _force_mark_database_disconnected()`. Review round 22 drove the
# collision on real Postgres and measured a 5-connection orphan when a
# supervisor tick lands inside a leader's connect window. The window is up to
# `connect_timeout_seconds` per repair, and it is permanent while the database
# is down, because a repair whose connect fails leaves `is_connected` False.
#
# WHICHEVER OF THE TWO MERGES SECOND MUST CLOSE THIS. The fix is for the
# supervisor's connect to claim the slot as well; it cannot be written here
# because that code does not exist on this branch. Startup
# (`main.py` app_lifespan) is exempt: it connects before traffic is served.
_repair_loop: Optional["weakref.ref[asyncio.AbstractEventLoop]"] = None
_repair_inflight: Optional["asyncio.Future"] = None
# The LEADER's absolute deadline, on its loop's clock. Followers wait until
# this rather than computing a bound from their own timeouts — the call sites
# disagree wildly (`/health` passes 5/3/1, POST /auth/login passes 2/2/1), and
# a follower that applies its own budget to someone else's repair gives up
# before a repair that was going to succeed.
_repair_deadline: float = 0.0

# What the leader publishes: None for success, or the (phase, error_type) of
# the DatabaseUnavailableError it raised.
RepairOutcome = Optional["tuple[str, str]"]

# NOT a failure — the leader vanished before reaching a verdict (its request
# was cancelled). Reporting that to followers as a database fault would turn
# one client hanging up into a 503 for everyone else, and would surface
# `CancelledError` as `/health`'s `db_error`. Followers re-claim instead.
_REPAIR_ABANDONED: "tuple[str, str]" = ("__abandoned__", "__abandoned__")


def _leader_budget(
    *,
    connect_timeout_seconds: float,
    probe_timeout_seconds: float,
    disconnect_timeout_seconds: float,
) -> float:
    """The leader's worst case, from the moment it claims the slot.

    COUNT EVERY AWAIT THE LEADER CAN MAKE. It calls `_connect_once` TWICE — once
    when it enters disconnected, and again after the teardown — and probes
    twice. An earlier version counted connect once; at defaults that made the
    budget 11.0s against a 13.0s worst case, so followers 503'd a second before
    a recovery that was about to succeed landed. Measured on the parent, which
    served all three callers where this turned two away.
    """
    return (
        connect_timeout_seconds * 2
        + probe_timeout_seconds * 2
        + disconnect_timeout_seconds
        # Scheduling slack: these bounds are per-await, and the leader has to
        # be resumed between them.
        + 1.0
    )


def _claim_repair(leader_budget: float) -> "tuple[bool, asyncio.Future, float]":
    """Become the repair leader, or return the in-flight repair to wait on.

    Returns `(is_leader, future, deadline)`. Contains no await, so the
    check-and-set cannot interleave with another caller — that atomicity is
    what makes this safe without a lock.
    """
    global _repair_loop, _repair_inflight, _repair_deadline

    loop = asyncio.get_running_loop()
    bound = _repair_loop() if _repair_loop is not None else None
    if bound is not loop:
        # A Future from another loop must never be awaited here; it would hang
        # rather than fail. Start this loop's slot empty.
        _repair_loop = weakref.ref(loop)
        _repair_inflight = None
        # No need to clear `_repair_deadline` here: with the slot emptied,
        # control always reaches the claim below, which rewrites both together.
        # They are only ever written or cleared as a pair, which is what makes
        # it impossible to read a deadline belonging to a different future.

    inflight = _repair_inflight
    if inflight is not None and not inflight.done():
        return False, inflight, _repair_deadline

    _repair_inflight = loop.create_future()
    _repair_deadline = loop.time() + leader_budget
    return True, _repair_inflight, _repair_deadline


def _release_repair_slot(future: "asyncio.Future") -> None:
    """Free the slot, without publishing anything, if it is still ours."""
    global _repair_inflight

    if _repair_inflight is future:
        _repair_inflight = None


def _publish_repair_outcome(future: "asyncio.Future", outcome: RepairOutcome) -> None:
    """Release the followers. MUST run on every exit path the leader can take,
    cancellation included, or the followers wait forever."""
    _release_repair_slot(future)
    if not future.done():
        # A result, never an exception: an exception nobody retrieves (because
        # there happened to be no followers) is logged by asyncio as
        # "Future exception was never retrieved".
        future.set_result(outcome)


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
    """
    try:
        database.is_connected = False
    except Exception:
        pass

    backend = getattr(database, "_backend", None)
    if backend is not None and hasattr(backend, "_pool"):
        try:
            backend._pool = None
        except Exception:
            pass


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
            await asyncio.wait_for(database.connect(), timeout=connect_timeout_seconds)
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

    probed = False

    async def _ready() -> bool:
        """Does the shared database answer a query right now?"""
        nonlocal probed
        if not getattr(database, "is_connected", False):
            return False
        probed = True
        try:
            await _probe_once("probe")
            return True
        except DatabaseUnavailableError as err:
            logger.warning(
                "Database readiness probe failed; attempting one reconnect",
                extra={"phase": err.phase, "error_type": err.error_type},
            )
            return False

    # FAST PATH — no coordination whatsoever. This is what the hot request
    # paths (`/health`, POST /auth/login, /quotes/preview, /orders/create,
    # /orders/payment/confirm) pay on every call: exactly one `SELECT 1`, the
    # same as the parent commit. Only a caller that has already established the
    # database is not answering goes any further.
    if await _ready():
        return

    async def _lead(inflight: "asyncio.Future") -> None:
        """The repair itself. Body unchanged from the parent commit."""
        nonlocal probed
        outcome: RepairOutcome = ("repair", "Unknown")
        try:
            if not getattr(database, "is_connected", False):
                await _connect_once()
                # A brand-new pool; any earlier probe said nothing about it.
                probed = False

            if not probed:
                try:
                    await _probe_once("probe")
                    outcome = None
                    return
                except DatabaseUnavailableError as first_err:
                    logger.warning(
                        "Database readiness probe failed; attempting one reconnect",
                        extra={
                            "phase": first_err.phase,
                            "error_type": first_err.error_type,
                        },
                    )

            try:
                if getattr(database, "is_connected", False):
                    await asyncio.wait_for(
                        database.disconnect(), timeout=disconnect_timeout_seconds
                    )
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
            outcome = None
        except DatabaseUnavailableError as exc:
            outcome = (exc.phase, exc.error_type)
            raise
        except asyncio.CancelledError:
            # NOT a verdict. This client hung up; it says nothing about the
            # database, so followers must retry rather than inherit a fault
            # that was never observed.
            outcome = _REPAIR_ABANDONED
            raise
        except BaseException as exc:
            outcome = ("repair", type(exc).__name__)
            raise
        finally:
            # Unconditional: a leader that dies without publishing would
            # strand every follower until its own caller-side timeout.
            _publish_repair_outcome(inflight, outcome)

    budget = _leader_budget(
        connect_timeout_seconds=connect_timeout_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        disconnect_timeout_seconds=disconnect_timeout_seconds,
    )

    # At most two passes: adopt an in-flight repair, and if its leader turned
    # out to have vanished rather than decided, lead one ourselves. Bounded so
    # a pathological churn of cancelled leaders cannot spin here.
    for _attempt in (0, 1):
        is_leader, inflight, deadline = _claim_repair(budget)

        if is_leader:
            await _lead(inflight)
            return

        # A repair is already running. Adopt its outcome rather than start a
        # second one — including when it FAILS, which is the whole point: N
        # callers must cost one repair, not N serialized repairs.
        #
        # Wait against the LEADER's deadline, not a budget of our own: the call
        # sites pass very different timeouts, and applying ours to someone
        # else's repair means giving up on one that was going to succeed.
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            # shield: `wait_for` cancels what it is waiting on when it times
            # out, and this future is SHARED — cancelling it would take down
            # every other follower and the leader's publish along with us.
            outcome = await asyncio.wait_for(asyncio.shield(inflight), timeout=remaining)
        except asyncio.TimeoutError:
            # The leader outlived its own worst case, so it is presumed gone
            # (`asyncio.wait_for` does not hard-bound a cancel that itself
            # hangs). Free the slot: leaving it occupied wedges the repair path
            # for the LIFE OF THE PROCESS — every later caller 503s and nothing
            # ever attempts recovery again, masked by the fast path until the
            # database actually needs repairing.
            #
            # HONEST COST, measured: if that leader is in fact still alive and
            # stuck, this admits one new leader — and therefore one unreachable
            # pool — PER BUDGET WINDOW, for as long as the condition lasts. Not
            # one in total. At production defaults (budget 14.0s, min_size 5)
            # that is ~4 pools/minute, roughly a thirtieth of the rate this
            # function was changed to fix, and it requires a leader whose own
            # cancellation hangs. A bounded-rate leak beats a dead recovery
            # path, but it is a leak, not an absence of one.
            _release_repair_slot(inflight)
            raise DatabaseUnavailableError(
                phase="repair_wait",
                # Distinct from a real connect timeout so `/health`'s db_error
                # does not conflate "the database timed out" with "we gave up
                # waiting on someone else's repair".
                error_type="RepairWaitTimeout",
                message="database readiness repair did not complete in time",
            ) from None

        if outcome is _REPAIR_ABANDONED:
            # DISCARD OUR OWN PROBE RESULT. It was taken before the abandoned
            # leader ran, so it says nothing about the pool that leader left
            # behind — and `_lead` only re-probes when `probed` is False.
            # Without this, a caller that takes over goes straight to the
            # teardown and destroys a pool it never probed — measured as
            # connect=2/disconnect=2 where one of each was correct, triggered
            # by an unrelated client hanging up.
            probed = False
            continue
        if outcome is None:
            return
        phase, error_type = outcome
        raise DatabaseUnavailableError(
            phase=phase,
            error_type=error_type,
            message="database readiness repair failed",
        )

    raise DatabaseUnavailableError(
        phase="repair_wait",
        error_type="RepairAbandoned",
        message="database readiness repair was abandoned",
    )
