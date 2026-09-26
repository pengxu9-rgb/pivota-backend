from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
elif sys.path[0] != root_str:
    sys.path.remove(root_str)
    sys.path.insert(0, root_str)

# Canonical test DATABASE_URL, pinned BEFORE any test module imports.
#
# Test modules disagree on the fallback they setdefault ("sqlite:///:memory:"
# vs a postgres placeholder), and `db.database` binds its singleton to
# whichever module happens to be imported first during collection. That made
# full-suite runs nondeterministic and cross-polluted: sqlite-written tests
# (PRAGMA, AUTOINCREMENT) ran against a live local postgres, and asyncpg
# connections leaked across event loops. conftest imports before every test
# module, so pinning here turns all per-module setdefaults into no-ops.
#
# A file-backed sqlite DB (not :memory:) is required: the `databases` library
# opens a fresh connection per query outside an explicit connection block, so
# an in-memory DB loses its tables between consecutive execute() calls.
_TEST_DB_PATH = ROOT / "pivota_test.db"
if "DATABASE_URL" not in os.environ:
    # Fresh slate per pytest process; only when we own the file.
    _TEST_DB_PATH.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_PATH}"

# On sqlite, mvp.events falls back to its file sink, which appends to
# mvp_events.jsonl in the cwd — keep test runs from littering the repo root.
os.environ.setdefault(
    "MVP_EVENTS_FILE",
    os.path.join(tempfile.gettempdir(), f"mvp_events_test_{os.getpid()}.jsonl"),
)


import threading

import pytest

# Session-end hooks that stop aiosqlite workers stranded by a closed event loop
# and fail loudly on any other non-daemon thread, so the process exits after
# its summary instead of hanging until the CI job cap. See the module docstring.
from thread_exit_guard import (  # noqa: E402,F401
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_unconfigure,
)


@pytest.fixture(scope="session", autouse=True)
def _no_aiosqlite_worker_thread_dies_or_outlives_the_session():
    """Fail the session if an aiosqlite worker thread died or is still running.

    Every aiosqlite connection runs a NON-daemon worker thread. The sweep job
    hung after printing its summary (runs 35872378808 and 35871915582,
    2026-09-23) until the 25-minute timeout, with 10-17 of these threads per
    run logged dying on "Event loop is closed": a TestClient request's loop
    closed while a background task's sqlite checkout was still connecting.
    `db.database` now makes that checkout wait for its worker. This proves it
    holds for the whole suite: a dead worker is the leak itself, and a live one
    at the end is what keeps the interpreter from exiting.

    Chains `threading.excepthook` rather than replacing it, so pytest still
    reports each thread exception as a warning on the test it surfaced in.
    """
    died: list = []
    previous_hook = threading.excepthook

    def _record(args):
        if args.thread is not None and "_connection_worker_thread" in args.thread.name:
            died.append(args.thread.name)
        previous_hook(args)

    threading.excepthook = _record
    yield
    threading.excepthook = previous_hook

    alive = [t for t in threading.enumerate() if "_connection_worker_thread" in t.name]
    for thread in alive:
        thread.join(timeout=1.0)  # one finishing its last close is not a leak
    alive = [t.name for t in alive if t.is_alive()]
    assert not died and not alive, (
        f"{len(died)} aiosqlite worker thread(s) died with an unhandled exception and "
        f"{len(alive)} are still running after the session: {sorted(died + alive)[:10]}. "
        "Something closed an event loop while an aiosqlite connection on it was still "
        "open or connecting — see _install_sqlite_checkout_waits_for_its_worker in "
        "db/database.py."
    )


@pytest.fixture(autouse=True)
def _reset_anonymous_invoke_budget():
    """Give every test a full anonymous budget for /agent/shop/v1/invoke.

    That endpoint's per-IP limiter (`_INVOKE_ANON_IP_LIMIT_STORE`,
    SHOP_INVOKE_ANON_RPM=60/min) keys a MODULE-LEVEL dict by IP and calendar
    minute. Every anonymous test shares the key "testclient", so one budget is
    spread across the whole session — and a test that bursts requests fails with
    429 depending on what unrelated files spent in the same wall-clock minute.

    That has now been fixed twice, each time by relocating the obligation:
    first by zeroing the rpm in the files that SPEND the budget (which broke the
    moment a new caller forgot), then by isolating the one file that was VISIBLY
    starved. Both leave the class open for the next burst test. Resetting here
    ends it: no test can be starved by another test's spend.

    This RESETS rather than DISABLES, which is the important distinction. The
    limiter stays fully armed inside each test, so the tests that assert it
    actually returns 429 keep working — they saturate the budget within their own
    test function. Disabling it suite-wide would silently neuter exactly those.

    Proven by tests/test_anonymous_invoke_budget_isolation.py, which spends the
    budget in one test and asserts the next one starts clean.
    """
    import routes.agent_shop_gateway as gateway

    gateway._INVOKE_ANON_IP_LIMIT_STORE.clear()
    yield
