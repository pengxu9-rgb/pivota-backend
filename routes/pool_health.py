"""Pool-vs-database diagnostic. Mounted at GET /__pool_health.

Why this exists: the same wedge has now taken prod down three times — 2026-08-18
and 2026-08-28 measured, an earlier one recorded in #1754 — and the mechanism is
still unexplained. Every occurrence presents identically:

    /health 503 (db_ok:false)   every request 504 at a flat ~4.0s
    Postgres: ~23 connections, ALL plain `idle`, zero in transaction
    Cloud SQL at <10% of max_connections — the database is healthy throughout

The 2026-08-28 investigation cost ~3 hours and ended where the previous two did,
because the evidence that would separate the candidates only exists WHILE the
service is wedged, and the only remediation is a restart that destroys it. This
endpoint captures that evidence in one unauthenticated GET, so the next
occurrence is a 5-second question instead of a 3-hour excavation.

It answers the one question that actually partitions the hypotheses, which the
2026-08-18 notes name as the thing to check first and which nobody has yet checked
during a live wedge:

    can the app's OWN pool answer `SELECT 1`, while a FRESH direct connection can?

  pool ok, direct ok      -> healthy
  pool FAILS, direct ok   -> pool_starved. The database is fine; the application
                             is holding slots it never returns. Restart restores
                             service. THIS is the wedge.
  pool FAILS, direct FAILS-> database_unreachable. A real outage. Do NOT restart
                             the fleet — see the liveness note below.
  pool ok, direct FAILS   -> direct_probe_blocked. Almost certainly egress/IAM,
                             not a data-plane problem.

`tasks_by_frame` groups live asyncio tasks by the top frame of their stack. During
a wedge this names the code that is parked while holding pool slots — the missing
measurement in all three post-mortems. It deliberately reports FILE:LINE only, never
locals, arguments or SQL text, because this endpoint is unauthenticated like its
siblings /__build and /__scheduler_health.

## Note for whoever wires a liveness probe to this

Use `verdict == "pool_starved"`, never `/health` and never a bare 200/503 here.
`/health` is DB-gated, so pointing liveness at it means a real Postgres outage
fails every instance at once and crashloops the entire fleet while the database is
trying to recover. `pool_starved` is specifically the case where THIS process is
broken and its peers may not be — the only honest liveness signal of the four.
A shallow probe would not work either: a pool-starved instance has a responsive
event loop and an open port, which is exactly why Cloud Run has never once
recycled a wedged instance here.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import Counter
from typing import Any, Dict, Optional

from fastapi import APIRouter

router = APIRouter(tags=["pool-health"])

# Short by design. This endpoint is called while the service is already failing;
# a probe that waits as long as the request path it is diagnosing would hang the
# diagnosis too. 2s is comfortably above a healthy round trip (0.03s measured
# in-container on 2026-08-18) and far below the 4.0s route budget.
PROBE_TIMEOUT_SECONDS = 1.0

# ONE deadline for the whole handler, not per-leg. Three independent 2s legs
# (pool query, direct connect, direct query) summed to 6s worst case — past the
# ~4.0s route budget this module's docstring names as the incident signature, so
# the ingress would cut off the diagnostic in the `database_unreachable` case it
# exists to identify.
TOTAL_DEADLINE_SECONDS = 3.0

# The direct probe opens a real connection OUTSIDE the pool budget, and this
# route is unauthenticated with no rate limit (both global middlewares
# early-return for non-/agent paths). Unbounded, a few hundred rps would
# saturate the 300-connection ceiling — the diagnostic would become the
# incident. One at a time, and a short cache so a polling loop is nearly free.
_DIRECT_PROBE_LOCK = asyncio.Semaphore(1)
_CACHE_TTL_SECONDS = 2.0
_cached: Dict[str, Any] = {}

# Enough frames to be useful, few enough that a 200-task dump stays small.
_MAX_FRAME_GROUPS = 15


def _pool_counters() -> Dict[str, Any]:
    """Read asyncpg pool internals defensively.

    Every field here is a private attribute of a pinned dependency, so each is
    read through getattr with a fallback: a diagnostic that raises during an
    incident is worse than one that reports "unknown".
    """
    try:
        from db.database import database

        # `_pool` is an attribute of the BACKEND, not of the Database facade.
        # Reading `database._pool` returns None on a perfectly healthy pool, and
        # since getattr never raises it short-circuits silently to "no_pool" —
        # which is not a neutral unknown: `no_pool` is the state
        # utils/database_readiness._pool_is_provably_dead treats as "the pool
        # object is gone", a different incident with a different remediation. The
        # endpoint would have reported a fabricated state 100% of the time,
        # healthy or wedged. Both other places in this repo that touch the pool
        # go through the backend: db/database.py's bounded-checkout patch (where
        # `self._database` IS the PostgresBackend) and
        # utils/database_readiness.py:67.
        backend = getattr(database, "_backend", None)
        pool = getattr(backend, "_pool", None)
        if pool is None:
            return {"state": "no_pool"}
        # asyncpg's PUBLIC accessors, in preference to _queue/_maxsize: these
        # report CONNECTED counts rather than slot counts, so `free` cannot be
        # inflated by holders that were never dialled during min_size warm-up.
        size = pool.get_size() if hasattr(pool, "get_size") else None
        idle = pool.get_idle_size() if hasattr(pool, "get_idle_size") else None
        maxsize = pool.get_max_size() if hasattr(pool, "get_max_size") else None
        return {
            "state": "present",
            "max_size": maxsize,
            "size": size,
            "idle": idle,
            # The number that matters: connections checked out and not returned.
            "in_use": (size - idle) if isinstance(size, int) and isinstance(idle, int) else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"state": "unreadable", "error": type(exc).__name__}


async def _probe_pool() -> Dict[str, Any]:
    """`SELECT 1` through the application's own pool."""
    started = time.monotonic()
    try:
        from db.database import database

        await asyncio.wait_for(
            database.fetch_val("SELECT 1"), timeout=PROBE_TIMEOUT_SECONDS
        )
        return {"ok": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }


async def _probe_direct() -> Dict[str, Any]:
    """Serialized and briefly cached; see _DIRECT_PROBE_LOCK."""
    now = time.monotonic()
    cached = _cached.get("direct")
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return dict(cached[1], cached=True)
    async with _DIRECT_PROBE_LOCK:
        # Re-check: a caller that queued on the semaphore should reuse the result
        # the holder just produced rather than open a second connection.
        cached = _cached.get("direct")
        if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            return dict(cached[1], cached=True)
        result = await _probe_direct_uncached()
        _cached["direct"] = (time.monotonic(), result)
        return result


async def _probe_direct_uncached() -> Dict[str, Any]:
    """`SELECT 1` over a connection that bypasses the pool entirely.

    This is the control. Without it a failing pool probe is indistinguishable
    from a down database, which is precisely the ambiguity that made the previous
    two wedges unexplainable. Costs exactly one connection, against a ceiling of
    300 that has never been above 10% during any of these incidents.
    """
    started = time.monotonic()
    # The app's own normalized DSN, not a fresh os.getenv: db/database.py rewrites
    # `postgres://` -> `postgresql://` and validates the scheme at import, and
    # config.Settings can source the URL from a .env file the bare env var would
    # miss — in which case the probe would report "DATABASE_URL unset" and degrade
    # the verdict to `unknown` while the app is happily on Postgres.
    try:
        from db.database import DATABASE_URL as dsn
    except Exception:  # noqa: BLE001
        dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        return {"ok": None, "error": "DATABASE_URL unset"}
    conn = None
    try:
        import asyncpg

        conn = await asyncio.wait_for(
            asyncpg.connect(dsn), timeout=PROBE_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=PROBE_TIMEOUT_SECONDS)
        return {"ok": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }
    finally:
        # A diagnostic that leaks a connection every call would, run on a loop
        # during an incident, become the incident.
        if conn is not None:
            try:
                await conn.close(timeout=1)
            except BaseException:  # noqa: BLE001
                # `await` inside finally re-raises CancelledError at its first
                # suspension point and the socket survives. terminate() is
                # synchronous and cannot be interrupted, so it always closes.
                try:
                    conn.terminate()
                except Exception:  # noqa: BLE001
                    pass


# Frames below this are plumbing, never the answer. A parked task's DEEPEST
# frame is almost always `asyncio/tasks.py:sleep` or a driver's read; the useful
# frame is the deepest one belonging to this application.
_PLUMBING_MARKERS = (
    os.sep + "asyncio" + os.sep,
    os.sep + "site-packages" + os.sep,
    os.sep + "lib" + os.sep + "python",
)


def _is_app_frame(filename: str) -> bool:
    return not any(marker in filename for marker in _PLUMBING_MARKERS)


def _deepest_app_frame(task: "asyncio.Task") -> Optional[Any]:
    """Walk the coroutine chain for the deepest frame owned by this app.

    `Task.get_stack()` CANNOT do this. On a SUSPENDED task it returns only the
    outermost coroutine's frame — coroutine frames do not link `f_back` while
    suspended, so the walk terminates after one frame and the `limit` argument
    never binds. Under uvicorn every in-flight request is a task whose outermost
    coroutine is `RequestResponseCycle.run_asgi`, so `get_stack` collapses every
    parked request into a single `h11_impl.py` group — precisely the tasks whose
    identity matters during a pool wedge, reported as one anonymous blob.

    Following `cr_await` reaches the real chain. The last app-owned frame on the
    way down is the answer; going all the way to the bottom just lands in
    `asyncio.sleep` every time.
    """
    coro = task.get_coro()
    best = None
    for _ in range(60):  # cycles are impossible but a bound is cheap insurance
        if coro is None:
            break
        frame = (
            getattr(coro, "cr_frame", None)
            or getattr(coro, "gi_frame", None)
            or getattr(coro, "ag_frame", None)
        )
        if frame is not None and _is_app_frame(frame.f_code.co_filename):
            best = frame
        coro = (
            getattr(coro, "cr_await", None)
            or getattr(coro, "gi_yieldfrom", None)
            or getattr(coro, "ag_await", None)
        )
    return best


def _tasks_by_frame() -> Dict[str, int]:
    """Group running tasks by the deepest application frame they are parked in.

    FILE:LINE only — no locals, no arguments, no SQL. This endpoint is
    unauthenticated, and the value here is "which code is parked", which a code
    position answers completely.
    """
    counts: Counter = Counter()
    try:
        tasks = [t for t in asyncio.all_tasks() if not t.done()]
    except RuntimeError:
        return {}
    for task in tasks:
        try:
            frame = _deepest_app_frame(task)
        except Exception:  # noqa: BLE001
            continue
        if frame is None:
            counts["<no application frame>"] += 1
            continue
        counts[f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}"] += 1
    return dict(counts.most_common(_MAX_FRAME_GROUPS))


def _verdict(pool_ok: Optional[bool], direct_ok: Optional[bool]) -> str:
    if pool_ok:
        return "healthy" if direct_ok is not False else "direct_probe_blocked"
    if direct_ok:
        return "pool_starved"
    if direct_ok is False:
        return "database_unreachable"
    return "unknown"


async def _both_probes() -> Any:
    """Sequential, not gathered — see the note in the handler."""
    return await _probe_pool(), await _probe_direct()


@router.get("/__pool_health")
async def pool_health() -> Dict[str, Any]:
    """Partition a database-shaped failure into pool vs database. Always 200.

    200 in all cases, like /__scheduler_health: the response shape carries the
    verdict. A status code cannot express the four-way distinction this endpoint
    exists to make, and a probe that returns 503 for `database_unreachable` would
    invite exactly the fleet-wide crashloop the module docstring warns against.
    """
    # Sequential, not gathered. Under `databases==0.7.0` a Connection lives in a
    # ContextVar that child tasks inherit, so running these concurrently could
    # make the control probe share state with the thing it is meant to control
    # for. See reference-databases-070-shares-connection-across-child-tasks.
    try:
        pool_probe, direct_probe = await asyncio.wait_for(
            _both_probes(), timeout=TOTAL_DEADLINE_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        pool_probe = {"ok": False, "error": "handler deadline"}
        direct_probe = {"ok": None, "error": "handler deadline"}
    return {
        "verdict": _verdict(pool_probe.get("ok"), direct_probe.get("ok")),
        "pool": _pool_counters(),
        "pool_probe": pool_probe,
        "direct_probe": direct_probe,
        "tasks_by_frame": _tasks_by_frame(),
        "timestamp": time.time(),
    }
