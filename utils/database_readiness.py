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
    # NaN and inf both survive float(); inf would wedge the loop on
    # asyncio.sleep(inf) with no log line, silently disabling the supervisor
    # (review round 19). Anything not finite falls back, loudly.
    # NaN/inf survive float() and would wedge the loop on sleep(inf) with no
    # log line. So would a finite-but-huge typo (86400 = once a day), which is
    # the same silent-disable defect reachable by a plausible mistake — so the
    # clamp has BOTH ends, like main.py's _health_timeout_seconds.
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
            await asyncio.wait_for(
                database.connect(), timeout=connect_timeout_seconds
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
