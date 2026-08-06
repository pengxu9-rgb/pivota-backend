from __future__ import annotations

import asyncio
import logging
import os
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
DEFAULT_RECONNECT_CONNECT_TIMEOUT_SECONDS = 10.0

_SUPERVISOR_ENABLED_ENV = "DB_RECONNECT_SUPERVISOR_ENABLED"
_SUPERVISOR_INTERVAL_ENV = "DB_RECONNECT_SUPERVISOR_INTERVAL_SECONDS"


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
    # NaN and inf both survive float(); inf would wedge the loop on
    # asyncio.sleep(inf) with no log line, silently disabling the supervisor
    # (review round 19). Anything not finite falls back, loudly.
    if not (value == value) or value in (float("inf"), float("-inf")):
        logger.warning(
            "invalid %s=%r; using %.1fs",
            _SUPERVISOR_INTERVAL_ENV, raw,
            DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS,
        )
        return DEFAULT_RECONNECT_SUPERVISOR_INTERVAL_SECONDS
    return max(1.0, value)


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
    only when `is_connected` is False — i.e. when there is no pool in use to
    damage — and it calls `database.connect()`, never `ensure_database_ready`,
    whose unconditional `finally: _force_mark_database_disconnected()` resets
    pool state on a 3s-probe timeout that cannot distinguish a slow database
    from a dead one.

    Four review rounds were spent trying to make a repairing supervisor safe
    (PR #1683, rounds 16-19). Each fix moved the destruction one layer further
    from the decision — a flag, then a probe, then a classifier, then a
    failure threshold — while the destruction itself stayed unconditional
    three layers down, and round 19 drove it destroying healthy pools and
    killing 30 in-flight queries. The lesson kept: a destructive actuator must
    not hang off an inference drawn from one `SELECT 1` under a timeout.

    KNOWN GAP, ACCEPTED. A backend that is `is_connected=True` with a dead
    pool is NOT repaired here — only request traffic through
    `ensure_database_ready` repairs that shape. Recovering it unattended needs
    a decision procedure that can prove death without guessing, which is
    separate work. This supervisor covers the degraded-START case, which is
    the one the incident actually described.

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
        if getattr(database, "is_connected", False):
            continue

        try:
            await asyncio.wait_for(
                database.connect(), timeout=connect_timeout_seconds
            )
            logger.warning(
                "database reconnect supervisor: reconnected a disconnected backend"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — must outlive every failure
            logger.warning(
                "database reconnect supervisor: still unreachable (%s: %s)",
                type(exc).__name__, exc,
            )
