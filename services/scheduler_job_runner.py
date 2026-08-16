"""Per-run isolation, watchdog and observability for background jobs.

WHY THIS EXISTS (issue #1754 — every APScheduler job on prod `web` skipping with
"maximum number of running instances reached (1)" for hours; the payment
reconciler included).

`databases==0.7.0` (the pinned version) keeps its `Connection` object in a
`ContextVar` and never removes it. `contextvars` are inherited by every task
spawned from a context, and `copy_context()` is shallow — the child sees the
SAME object. `main.startup_event()` runs DB work (ensure_*_table) BEFORE it
calls `start_scheduler()`, so the startup context carries a `Connection`, and
APScheduler's timer chain (`call_later` -> `wakeup` -> `create_task`) copies
that context into every job task. Result, measured on the prod pin: **all
scheduler jobs (and the startup-spawned retry workers) share ONE
`databases.Connection`** — one asyncpg connection, one `_query_lock`, one
`_connection_lock`, one `_transaction_stack`.

Two consequences follow, both reproduced on real Postgres:

* a job's `database.transaction()` issues BEGIN outside `_query_lock`, so it
  collides with another job's in-flight query (`another operation is in
  progress`); asyncpg had already set `_top_xact`, so every later transaction
  on that connection issues SAVEPOINT on an idle session — the exact
  `SAVEPOINT can only be used in transaction blocks` burst in the Postgres log
  — and `databases` leaks the connection counter, so the raw connection is
  never returned to the pool;
* any ONE job blocked in a DB await (dead socket with no command_timeout, a
  lock wait, a hung pool release) holds the shared lock forever, so EVERY job
  blocks behind it forever, `max_instances=1` skips every tick, and Postgres
  sees nothing — the wedge in #1754.

The fix, at the root: every job run executes in a **fresh, empty
`contextvars.Context`**, so `database.connection()` creates a new
`Connection` for that run and nothing is inherited from startup (this is
also how the library's own per-task fix in `databases>=0.8` behaves, without
the SQLAlchemy 2.0 upgrade that would require). A wedged run can then only
wedge itself.

In addition — because a run that never completes still starves ITS OWN
future ticks under `max_instances=1` — every run gets a per-job **deadline**:
on expiry the run is cancelled, given a short grace to unwind, and abandoned
as a "zombie" if it will not (a task hung in asyncpg's cancel path must not
take the watchdog down with it). The next tick then proceeds. Every run is
recorded in a registry that `/__scheduler_health` exposes, and
`run_job_now()` lets an operator force a run out-of-band (money path:
`payment_reconcile_tick`).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# How long a cancelled run gets to unwind before it is abandoned. Long enough
# for a normal `finally:` chain (pool release, lock release), short enough that
# a hung unwind cannot hold the job's slot for another interval.
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


class JobRunCancelled(RuntimeError):
    """The run's own task was cancelled (operator `cancel-running`), as
    opposed to the wrapper being cancelled — which propagates CancelledError."""


class JobDeadlineExceeded(RuntimeError):
    """A run exceeded its deadline and was cancelled (or abandoned)."""

    def __init__(self, job_id: str, deadline_seconds: float, *, zombie: bool):
        self.job_id = job_id
        self.deadline_seconds = deadline_seconds
        self.zombie = zombie
        super().__init__(
            f"job {job_id!r} exceeded its {deadline_seconds:g}s run deadline and was "
            + ("ABANDONED (did not unwind within grace)" if zombie else "cancelled")
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRunState:
    job_id: str
    deadline_seconds: float
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS
    runs_started: int = 0
    runs_ok: int = 0
    runs_failed: int = 0
    runs_deadline_exceeded: int = 0
    runs_cancelled: int = 0
    zombie_count: int = 0
    last_started_at: Optional[str] = None
    last_finished_at: Optional[str] = None
    last_duration_ms: Optional[int] = None
    max_duration_ms: Optional[int] = None  # to tune _JOB_RUN_DEADLINES from data
    last_outcome: Optional[str] = None
    last_error_type: Optional[str] = None
    last_error: Optional[str] = None
    # task -> monotonic start (a scheduled run and a run_job_now can overlap)
    active: Dict[asyncio.Task, float] = field(default_factory=dict)
    zombies: Set[asyncio.Task] = field(default_factory=set)

    def _reap_zombies(self) -> None:
        self.zombies = {t for t in self.zombies if not t.done()}

    def snapshot(self, *, include_error_text: bool = False) -> Dict[str, Any]:
        self._reap_zombies()
        now = time.monotonic()
        running_for = [now - t0 for t0 in self.active.values()]
        oldest_running_s = max(running_for) if running_for else None
        out: Dict[str, Any] = {
            "deadline_seconds": self.deadline_seconds,
            "running": len(self.active),
            "running_for_seconds": round(oldest_running_s, 1) if oldest_running_s is not None else None,
            "runs_started": self.runs_started,
            "runs_ok": self.runs_ok,
            "runs_failed": self.runs_failed,
            "runs_deadline_exceeded": self.runs_deadline_exceeded,
            "runs_cancelled": self.runs_cancelled,
            "zombie_count": self.zombie_count,
            "zombies_alive": len(self.zombies),
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_duration_ms": self.last_duration_ms,
            "max_duration_ms": self.max_duration_ms,
            "last_outcome": self.last_outcome,
            "last_error_type": self.last_error_type,
        }
        if include_error_text:
            out["last_error"] = self.last_error
        return out


_REGISTRY: Dict[str, JobRunState] = {}
# job_id -> the wrapped (isolated + deadline-bounded) callable, for run_job_now.
_WRAPPED: Dict[str, Callable[..., Awaitable[Any]]] = {}


def _state(
    job_id: str, deadline_seconds: float,
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
) -> JobRunState:
    st = _REGISTRY.get(job_id)
    if st is None:
        st = JobRunState(job_id=job_id, deadline_seconds=float(deadline_seconds),
                         cancel_grace_seconds=float(cancel_grace_seconds))
        _REGISTRY[job_id] = st
    else:
        st.deadline_seconds = float(deadline_seconds)
        st.cancel_grace_seconds = float(cancel_grace_seconds)
    return st


def _spawn_in_new_context(coro: Awaitable[Any], name: Optional[str]):
    """(task, context) — the context is kept so a zombie's own DB connection
    can be found and terminated (see `_terminate_run_connection`)."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.Context()
    task = ctx.run(loop.create_task, coro)
    if name:
        try:
            task.set_name(name)
        except Exception:  # pragma: no cover - cosmetic
            pass
    return task, ctx


def _terminate_run_connection(ctx: contextvars.Context, task: asyncio.Task) -> bool:
    """Best-effort: close the raw asyncpg connection a ZOMBIE run holds.

    A run cancelled mid-command puts asyncpg into its cancelling state; the
    pool release then waits for the original socket to answer, which on a dead
    socket is forever — so an abandoned run would hold one of `max_size` pool
    slots permanently. Terminating the raw connection makes asyncpg raise
    inside the zombie (unwinding it) and lets the pool discard the holder.
    Reaches into `databases` internals (0.7.0 ContextVar / 0.9.0 per-task map)
    and asyncpg's `Connection.terminate()`; every step is guarded — a miss
    just leaves the pre-existing leak.
    """
    try:
        from db.database import database
    except Exception:  # noqa: BLE001
        return False
    conn = None
    try:
        cv = getattr(database, "_connection_context", None)  # databases 0.7.0
        if cv is not None:
            try:
                conn = ctx.run(cv.get)
            except LookupError:
                conn = None
        if conn is None:
            cmap = getattr(database, "_connection_map", None)  # databases >= 0.8
            if cmap is not None:
                conn = cmap.get(task)
    except Exception:  # noqa: BLE001
        conn = None
    if conn is None:
        return False
    try:
        raw = getattr(getattr(conn, "_connection", None), "_connection", None)
        terminate = getattr(raw, "terminate", None)
        if raw is not None and callable(terminate) and not raw.is_closed():
            terminate()
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def spawn_isolated(coro: Awaitable[Any], *, name: Optional[str] = None) -> asyncio.Task:
    """Create a task in a FRESH, EMPTY `contextvars.Context`.

    Nothing from the caller's context is inherited — in particular not the
    `databases` `Connection` that 0.7.0 parks in a ContextVar — so the task's
    first `database.*` call creates its own Connection and its own pool
    checkout. Use this for every long-lived worker spawned at startup and for
    every scheduler run.
    """
    task, _ctx = _spawn_in_new_context(coro, name)
    return task


async def run_isolated(
    job_id: str,
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    deadline_seconds: float,
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    **kwargs: Any,
) -> Any:
    """Run `func(*args, **kwargs)` isolated (fresh context) and bounded.

    * Completes normally -> returns the result, records `ok`.
    * Raises -> records `error` and re-raises (APScheduler logs it as before).
    * Exceeds `deadline_seconds` -> the run is cancelled; if it does not finish
      within `cancel_grace_seconds` it is ABANDONED (kept in the registry as a
      zombie), and `JobDeadlineExceeded` is raised either way so the outcome is
      visible in the scheduler's job log. The job's slot is free again, so its
      next tick runs.
    * The wrapper itself being cancelled (scheduler shutdown) cancels the run.
    """
    st = _state(job_id, deadline_seconds, cancel_grace_seconds)
    task, ctx = _spawn_in_new_context(func(*args, **kwargs), f"job:{job_id}")
    t0 = time.monotonic()
    st.runs_started += 1
    st.last_started_at = _now_iso()
    st.active[task] = t0

    def _finish(outcome: str, exc: Optional[BaseException] = None) -> None:
        st.active.pop(task, None)
        st.last_finished_at = _now_iso()
        st.last_duration_ms = int((time.monotonic() - t0) * 1000)
        st.max_duration_ms = max(st.max_duration_ms or 0, st.last_duration_ms)
        st.last_outcome = outcome
        if exc is not None:
            st.last_error_type = type(exc).__name__
            st.last_error = str(exc)[:500]
        else:
            st.last_error_type = None
            st.last_error = None

    def _adopt_as_zombie(reason: str) -> None:
        # The run is being abandoned while (possibly) still alive: keep it
        # visible and cancellable, never let its exception go unretrieved,
        # and cut its DB connection so it cannot hold a pool slot forever.
        task.add_done_callback(_swallow_task_result)
        if not task.done():
            st.zombie_count += 1
            st.zombies.add(task)
            terminated = _terminate_run_connection(ctx, task)
            logger.error(
                "scheduler job %r: run ABANDONED as a zombie (%s)%s — it stays visible "
                "on /__scheduler_health and reachable via cancel-running. "
                "(issue #1754 wedge class)",
                job_id, reason,
                "; its DB connection was terminated" if terminated else "",
            )

    try:
        done, _pending = await asyncio.wait({task}, timeout=deadline_seconds)
    except asyncio.CancelledError:
        # The WRAPPER was cancelled (scheduler shutdown/restart). Cancel the
        # run too, but do not lose track of it if it will not unwind (#1756
        # review finding 1).
        task.cancel()
        st.runs_cancelled += 1
        _finish("cancelled")
        _adopt_as_zombie("wrapper cancelled while the run was in flight")
        raise

    if task in done:
        try:
            result = task.result()
        except asyncio.CancelledError:
            # The RUN's task was cancelled (operator cancel-running), not us.
            st.runs_cancelled += 1
            err = JobRunCancelled(f"job {job_id!r} run was cancelled")
            _finish("cancelled", err)
            raise err
        except BaseException as exc:
            st.runs_failed += 1
            _finish("error", exc)
            raise
        st.runs_ok += 1
        _finish("ok")
        return result

    # ---- deadline exceeded ----
    task.cancel()
    try:
        done2, _ = await asyncio.wait({task}, timeout=cancel_grace_seconds)
    except asyncio.CancelledError:
        st.runs_cancelled += 1
        _finish("cancelled")
        _adopt_as_zombie("wrapper cancelled during the grace window")
        raise
    zombie = task not in done2
    st.runs_deadline_exceeded += 1
    err = JobDeadlineExceeded(job_id, deadline_seconds, zombie=zombie)
    _finish("deadline_exceeded", err)
    if zombie:
        _adopt_as_zombie("did not unwind within %gs grace after the deadline" % cancel_grace_seconds)
    else:
        _swallow_task_result(task)
        logger.error(
            "scheduler job %r exceeded its %gs run deadline; run cancelled cleanly. "
            "The job's slot is free; its next tick will run. (issue #1754 wedge class)",
            job_id, deadline_seconds,
        )
    raise err


def _swallow_task_result(task: asyncio.Task) -> None:
    try:
        if not task.cancelled():
            task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        pass


def wrap_job(
    job_id: str,
    func: Callable[..., Awaitable[Any]],
    *,
    deadline_seconds: float,
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
) -> Callable[..., Awaitable[Any]]:
    """Return the scheduler-facing callable for `func`: same name (so
    APScheduler log lines are unchanged), but every invocation goes through
    `run_isolated`. Registered under `job_id` for `run_job_now`."""
    if not asyncio.iscoroutinefunction(func):
        # A sync job would run inline on the loop and then hand
        # `create_task` a non-awaitable. Every registered job is async today;
        # fail loudly at registration rather than on every tick.
        raise TypeError(f"wrap_job({job_id!r}): {func!r} must be an `async def`")

    async def _isolated_job(*args: Any, **kwargs: Any) -> Any:
        return await run_isolated(
            job_id, func, *args,
            deadline_seconds=deadline_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
            **kwargs,
        )

    functools.update_wrapper(_isolated_job, func)
    _isolated_job.__wrapped_job_id__ = job_id  # type: ignore[attr-defined]
    _state(job_id, deadline_seconds, cancel_grace_seconds)
    _WRAPPED[job_id] = _isolated_job
    return _isolated_job


def registered_job_ids() -> list:
    return sorted(_WRAPPED)


async def run_job_now(job_id: str) -> Dict[str, Any]:
    """Force one out-of-band run of a registered job, through the same isolated,
    deadline-bounded path as the scheduler. Returns the run's outcome; never
    raises for job errors (they are in the returned dict)."""
    fn = _WRAPPED.get(job_id)
    if fn is None:
        raise KeyError(job_id)
    started = _now_iso()
    outcome: Dict[str, Any] = {"job_id": job_id, "started_at": started}
    try:
        result = await fn()
        outcome["outcome"] = "ok"
        if result is not None:
            outcome["result"] = result if isinstance(result, (dict, list, str, int, float, bool)) else repr(result)
    except JobDeadlineExceeded as exc:
        outcome["outcome"] = "deadline_exceeded"
        outcome["error"] = str(exc)
    except JobRunCancelled as exc:
        outcome["outcome"] = "cancelled"
        outcome["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        outcome["outcome"] = "error"
        outcome["error_type"] = type(exc).__name__
        outcome["error"] = str(exc)[:500]
    outcome["state"] = _REGISTRY[job_id].snapshot(include_error_text=True)
    return outcome


def cancel_running(job_id: str) -> int:
    """Operator lever: cancel every in-flight run (and alive zombie) of a job.
    Returns how many tasks were signalled."""
    st = _REGISTRY.get(job_id)
    if st is None:
        return 0
    n = 0
    for t in list(st.active) + list(st.zombies):
        if not t.done():
            t.cancel()
            n += 1
    return n


def registry_snapshot(*, include_error_text: bool = False) -> Dict[str, Dict[str, Any]]:
    return {jid: st.snapshot(include_error_text=include_error_text) for jid, st in sorted(_REGISTRY.items())}


def stalled_jobs(*, now: Optional[float] = None) -> Dict[str, float]:
    """Jobs with a run older than their deadline (+grace). Should be empty
    post-#1754; non-empty means the watchdog itself is not firing."""
    now = time.monotonic() if now is None else now
    out: Dict[str, float] = {}
    for jid, st in _REGISTRY.items():
        for t0 in st.active.values():
            age = now - t0
            if age > st.deadline_seconds + st.cancel_grace_seconds:
                out[jid] = max(out.get(jid, 0.0), round(age, 1))
    return out


def _reset_for_tests() -> None:
    _REGISTRY.clear()
    _WRAPPED.clear()
