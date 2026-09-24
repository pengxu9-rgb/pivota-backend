"""Make the pytest process exit after its summary, even when a test leaked a thread.

WHAT THIS PREVENTS. The Backend Test Sweep job intermittently hung AFTER pytest
printed "N passed ... in 9xxs", silent until the 25-minute job cap cancelled it
(runs 35851774012 and 35940218804, 2026-09-23/24; the re-runs passed). Python
joins every non-daemon thread in `threading._shutdown` before it exits, and
one of them never finished: an aiosqlite connection worker.

WHY THAT WORKER NEVER FINISHES (reproduced 2026-09-24, aiosqlite 0.22.1).
Every aiosqlite connection runs a NON-daemon worker that loops on
`future, function = tx.get()` and exits only when its connection queues a stop,
from `close()` or from `Connection.__del__`. The worker posts each result with
`loop.call_soon_threadsafe`. If that post lands just before the loop closes, the
loop discards the handle, the future stays pending forever, and the task
awaiting it never resumes. This happens when a bare `TestClient` request's loop
closes while fire-and-forget DB work is still in flight, for example work
spawned from a cancellation handler during `asyncio.run` teardown. The worker
then blocks in `tx.get()` with that future still bound to its frame local, which
gives this chain:

    worker's stack -> future -> Task.task_wakeup -> task -> coroutine
                   -> aiosqlite.Connection   (its __del__ is what would stop the worker)

A running thread's stack is a GC root, so `gc.collect()` cannot free it. The
worker keeps alive the one object that would stop it, and `threading._shutdown`
waits on it forever. Whether this happens depends on where the loop stops, so a
standalone repro hangs in about a third of runs.

WHAT THIS DOES, at the end of the session and after every report is written:
  1. Stops each aiosqlite worker that is still running by queueing the same
     sentinel `Connection.stop()` would. A warning names each worker it stops.
     The run's result is unchanged: the leak comes from the library, not from
     a test assertion.
  2. Gives any other non-daemon thread a grace period to finish. A thread still
     running after that is a regression, so the guard prints its name and
     stack, fails the session, and exits the process so CI never sits silent
     until the job cap. `concurrent.futures` workers are exempt, because
     `threading._shutdown` itself signals and joins them.

Proven by tests/test_thread_exit_guard.py, which recreates the deadlocked worker
state and runs pytest as a subprocess with and without this plugin.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from typing import List

import pytest

# Long enough for a thread that is genuinely finishing, e.g. a final close.
_GRACE_SECONDS = 10.0

_STOPPED_WORKERS: List[str] = []
_SURVIVORS: List[threading.Thread] = []
_EXIT_STATUS: List[int] = [int(pytest.ExitCode.TESTS_FAILED)]


def _is_aiosqlite_worker(thread: threading.Thread) -> bool:
    target = getattr(thread, "_target", None)
    return (
        getattr(target, "__module__", None) == "aiosqlite.core"
        and getattr(target, "__name__", None) == "_connection_worker_thread"
    )


def stop_orphaned_aiosqlite_workers(join_timeout: float = 5.0) -> List[str]:
    """Queue a stop to every running aiosqlite worker; return the names of those that exited."""
    workers = [t for t in threading.enumerate() if t.is_alive() and _is_aiosqlite_worker(t)]
    if not workers:
        return []
    import aiosqlite.core as core

    sentinel = core._STOP_RUNNING_SENTINEL
    for worker in workers:
        # `_args` is `(tx,)`, the queue the worker blocks on. This is exactly
        # what `Connection.stop()` queues, minus closing the sqlite3 handle,
        # which must happen on the worker thread; the process is about to exit.
        worker._args[0].put_nowait((None, lambda: sentinel))
    for worker in workers:
        worker.join(join_timeout)
    return [w.name for w in workers if not w.is_alive()]


def surviving_non_daemon_threads(grace: float) -> List[threading.Thread]:
    """Non-daemon threads that would block interpreter exit, after `grace` seconds to finish."""
    import concurrent.futures.thread as cf_thread

    def blocking() -> List[threading.Thread]:
        return [
            t
            for t in threading.enumerate()
            if t.is_alive()
            and not t.daemon
            and t is not threading.main_thread()
            and getattr(t, "_target", None) is not cf_thread._worker
        ]

    threads = blocking()
    for thread in threads:
        thread.join(grace / max(len(threads), 1))
    return blocking()


def _describe(threads: List[threading.Thread]) -> str:
    frames = sys._current_frames()
    lines = []
    for thread in threads:
        lines.append(f"--- {thread.name} (ident={thread.ident}, target={getattr(thread, '_target', None)!r})")
        frame = frames.get(thread.ident)
        if frame is not None:
            lines.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
    return "\n".join(lines)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # trylast: runs after the runner has torn down session fixtures, so any
    # thread still alive here would outlive the process's last test code.
    _STOPPED_WORKERS[:] = stop_orphaned_aiosqlite_workers()
    _SURVIVORS[:] = surviving_non_daemon_threads(_GRACE_SECONDS)
    if _SURVIVORS and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    _EXIT_STATUS[0] = int(session.exitstatus)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # type: ignore[no-untyped-def]
    if _STOPPED_WORKERS:
        terminalreporter.write_sep("-", "thread exit guard: stopped orphaned aiosqlite workers", yellow=True)
        terminalreporter.write_line(
            f"{len(_STOPPED_WORKERS)} aiosqlite worker thread(s) were still running at session end "
            "(an event loop closed with a query result still undelivered). Without this stop "
            "the process would never exit: " + ", ".join(_STOPPED_WORKERS)
        )
    if _SURVIVORS:
        terminalreporter.write_sep("=", "thread exit guard: NON-DAEMON THREADS OUTLIVED THE SESSION", red=True)
        terminalreporter.write_line(
            f"{len(_SURVIVORS)} non-daemon thread(s) were still running {_GRACE_SECONDS:g}s after the "
            "last test. Python waits for these before exiting, so the process would hang here "
            "until the CI job cap. Stop the thread in a fixture finaliser or make it a daemon. "
            "The session is marked failed, and the process exits once reporting finishes."
        )
        terminalreporter.write_line(_describe(_SURVIVORS))


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    # Every report, including junitxml and the terminal summary, has been
    # written by now. `threading._shutdown` runs before atexit handlers, so no
    # later hook could release these threads. Exit instead of hanging.
    if any(t.is_alive() for t in _SURVIVORS):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_EXIT_STATUS[0])
