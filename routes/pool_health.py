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
PROBE_TIMEOUT_SECONDS = 2.0

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

        pool = getattr(database, "_pool", None)
        if pool is None:
            return {"state": "no_pool"}
        queue = getattr(pool, "_queue", None)
        free = queue.qsize() if hasattr(queue, "qsize") else None
        maxsize = getattr(pool, "_maxsize", None)
        return {
            "state": "present",
            "max_size": maxsize,
            "free": free,
            # The number that matters: slots checked out and not returned.
            "in_use": (maxsize - free) if isinstance(maxsize, int) and isinstance(free, int) else None,
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
    """`SELECT 1` over a connection that bypasses the pool entirely.

    This is the control. Without it a failing pool probe is indistinguishable
    from a down database, which is precisely the ambiguity that made the previous
    two wedges unexplainable. Costs exactly one connection, against a ceiling of
    300 that has never been above 10% during any of these incidents.
    """
    started = time.monotonic()
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
            except Exception:  # noqa: BLE001
                pass


def _tasks_by_frame() -> Dict[str, int]:
    """Group running tasks by the top frame of their stack.

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
            stack = task.get_stack(limit=1)
        except Exception:  # noqa: BLE001
            continue
        if not stack:
            counts["<no stack: not started or awaiting>"] += 1
            continue
        frame = stack[0]
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
    pool_probe = await _probe_pool()
    direct_probe = await _probe_direct()
    return {
        "verdict": _verdict(pool_probe.get("ok"), direct_probe.get("ok")),
        "pool": _pool_counters(),
        "pool_probe": pool_probe,
        "direct_probe": direct_probe,
        "tasks_by_frame": _tasks_by_frame(),
        "timestamp": time.time(),
    }
