from __future__ import annotations

import asyncio
import logging
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
    """
    try:
        database.is_connected = False
    except Exception:
        pass

    backend = getattr(database, "_backend", None)
    if backend is not None and hasattr(backend, "_pool"):
        # TERMINATE BEFORE DROPPING THE REFERENCE. Nulling `_pool` alone
        # abandons the asyncpg pool with its server connections still open —
        # they stay in pg_stat_activity until the backend times them out.
        # Measured on real Postgres (review round 17): one leak of
        # DB_POOL_MIN_SIZE connections per call, growing linearly. That was
        # survivable while this ran only on request-path failures; the
        # reconnect supervisor calls it on a timer, so at the production
        # default (min_size 5, 30s interval) an unattended degraded process
        # would burn ~600 server connections an hour and exhaust
        # max_connections — taking the database down for every other client
        # during the very outage this code exists to recover from.
        # terminate() is synchronous and immediate; close() is a coroutine
        # that waits on in-flight work we have already given up on.
        pool = getattr(backend, "_pool", None)
        terminate = getattr(pool, "terminate", None)
        if callable(terminate):
            try:
                terminate()
            except Exception:
                pass
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


async def run_database_reconnect_supervisor(
    *,
    interval_seconds: Optional[float] = None,
    max_cycles: Optional[int] = None,
) -> None:
    """Reconnect a degraded process without waiting for the right request.

    WHY THIS EXISTS. Startup deliberately continues when the initial DB
    connect fails (main.py: "keep the service up"), and nothing used to walk
    that back except `/health` reconnecting as a side effect of being polled.
    Removing that side effect (see `probe_database_health`) would otherwise
    leave exactly one class of repair: `ensure_database_ready`, called only
    from POST /auth/login, /quotes/preview, /orders/create and
    /orders/payment/confirm. A process serving only READ traffic — agent
    product search, promotions — would then stay broken indefinitely, which
    is strictly worse than the accidental heal it replaced. Review round 16
    on PR #1683 drove exactly that regression; this closes it.

    DELIBERATELY NOT AN APSCHEDULER JOB. During the 2026-08-05/06 incident
    every scheduler job sat at "maximum number of running instances reached"
    because jobs call `database.connect()` with no timeout and hang forever.
    A repair mechanism must not share fate with the thing it repairs, so this
    is a plain asyncio task owned by the app lifespan, and every DB call it
    makes is timeout-bounded via `ensure_database_ready`.

    Never raises: a supervisor that dies on an unexpected error is a
    supervisor that silently stops supervising. `max_cycles` exists so tests
    can drive a bounded number of iterations.
    """
    # Resolved at CALL time, not as a default argument: a default binds at
    # definition and cannot be patched, which made the lifespan-started
    # supervisor untestable end-to-end (round-17 R17-4).
    interval = (
        DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS
        if interval_seconds is None
        else interval_seconds
    )

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

        # LIVENESS IS DECIDED BY A QUERY, NOT BY THE FLAG. An earlier version
        # gated on `database.is_connected`, which the repair path sets itself
        # — two failures followed, both driven in review round 17:
        #   * the connected-but-broken-pool shape (`is_connected` True while
        #     every query raises `InterfaceError: pool is closed` — the shape
        #     `_force_mark_database_disconnected` above exists for) was
        #     skipped entirely, so nothing ever repaired it;
        #   * after one failed cycle, `ensure_database_ready` had already set
        #     the flag True, so every later cycle short-circuited and
        #     supervision ended permanently — including for "the database
        #     system is starting up", i.e. exactly the restart case this
        #     supervisor exists to ride out.
        # A `SELECT 1` every 30s costs nothing and cannot lie to us.
        try:
            await probe_database_health()
            continue
        except DatabaseUnavailableError:
            pass
        except Exception:  # noqa: BLE001 — probing must never end supervision
            logger.exception("database reconnect supervisor: probe error")

        try:
            await ensure_database_ready()
            logger.warning(
                "database reconnect supervisor: restored an unhealthy backend"
            )
        except DatabaseUnavailableError as exc:
            logger.warning(
                "database reconnect supervisor: still unavailable",
                extra={"phase": exc.phase, "error_type": exc.error_type},
            )
        except Exception:  # noqa: BLE001 — must outlive every failure mode
            logger.exception("database reconnect supervisor: unexpected error")
