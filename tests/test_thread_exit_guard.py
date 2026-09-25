"""The pytest process exits after its summary even when a test strands a thread.

WHAT THIS PREVENTS (2026-09-23/24). The Backend Test Sweep hung after "N passed"
until the 25-minute job cap cancelled it (runs 35851774012, 35940218804): an
aiosqlite worker whose last result post was discarded by a closing loop keeps
its own Connection alive from its stack and never exits. See
tests/thread_exit_guard.py for the chain.

Each case runs pytest in a subprocess on a one-test file, because the thing
under test is whether a whole pytest PROCESS exits. The first case runs without
the guard and must hang, which proves the stranded state is real and not just
something the guard happens to pass on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Deterministic version of what a TestClient request loop does to a
# fire-and-forget query: the worker posts the result, and the loop closes before
# running that post.
STRAND_A_WORKER = textwrap.dedent(
    """
    import asyncio
    import threading
    import time

    import aiosqlite


    def test_strands_an_aiosqlite_worker():
        loop = asyncio.new_event_loop()
        conn = loop.run_until_complete(aiosqlite.connect(":memory:"))
        gate = threading.Event()

        def held():
            gate.wait(5)
            return 1

        loop.create_task(conn._execute(held))
        loop.run_until_complete(asyncio.sleep(0))  # the task queues `held` and awaits it
        gate.set()
        deadline = time.monotonic() + 5
        while not loop._ready and time.monotonic() < deadline:  # the worker's result post
            time.sleep(0.01)
        assert loop._ready, "worker never posted its result"
        loop.close()  # discards the post: that future never resolves
        worker = conn._thread
        del conn
        assert worker.is_alive()
    """
)

BUSY_EXECUTOR_WORKER = textwrap.dedent(
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    EXECUTOR = ThreadPoolExecutor(1, thread_name_prefix="busy-executor")


    def test_leaves_an_executor_worker_inside_a_work_item():
        started = threading.Event()

        def forever():
            started.set()
            threading.Event().wait()

        EXECUTOR.submit(forever)
        assert started.wait(5)
    """
)

IDLE_EXECUTOR_WORKER = textwrap.dedent(
    """
    from concurrent.futures import ThreadPoolExecutor

    EXECUTOR = ThreadPoolExecutor(1, thread_name_prefix="idle-executor")


    def test_leaves_an_idle_executor_worker():
        assert EXECUTOR.submit(lambda: 1).result(5) == 1
    """
)

LEAK_A_PLAIN_THREAD = textwrap.dedent(
    """
    import threading


    def test_leaks_a_non_daemon_thread():
        threading.Thread(target=threading.Event().wait, name="leaked-by-test").start()
    """
)

SHORT_GRACE_CONFTEST = "import thread_exit_guard\nthread_exit_guard._GRACE_SECONDS = 0.5\n"


def _pytest(tmp_path: Path, body: str, *, guard: bool) -> subprocess.Popen:
    """Start pytest on `body`; its output goes to tmp_path/out.log so no read can block."""
    (tmp_path / "test_case.py").write_text(body)
    if guard:
        (tmp_path / "conftest.py").write_text(SHORT_GRACE_CONFTEST)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly"]
    if guard:
        args += ["-p", "thread_exit_guard"]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(TESTS_DIR), os.environ.get("PYTHONPATH", "")]))
    env.pop("PYTEST_ADDOPTS", None)
    with open(tmp_path / "out.log", "w") as log:
        return subprocess.Popen(
            args + [str(tmp_path / "test_case.py")],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )


def _output(tmp_path: Path) -> str:
    return (tmp_path / "out.log").read_text()


def _finish(proc: subprocess.Popen, tmp_path: Path, timeout: float) -> tuple:
    """(returncode, output); returncode is None if it had not exited within `timeout` (then killed)."""
    try:
        proc.wait(timeout=timeout)
        return proc.returncode, _output(tmp_path)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None, _output(tmp_path)


def test_without_the_guard_a_stranded_worker_hangs_the_process_after_its_summary(tmp_path):
    proc = _pytest(tmp_path, STRAND_A_WORKER, guard=False)
    deadline = time.monotonic() + 60
    # Poll a file, never a pipe: a child that hangs without printing a summary
    # must not hang this test too.
    while " passed" not in _output(tmp_path) and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if " passed" not in _output(tmp_path):
        proc.kill()
        proc.wait()
    assert "1 passed" in _output(tmp_path), _output(tmp_path)

    returncode, _ = _finish(proc, tmp_path, timeout=5)

    assert returncode is None, "the process exited, so this no longer reproduces the sweep hang"


def test_the_guard_stops_a_stranded_worker_and_the_process_exits(tmp_path):
    returncode, out = _finish(_pytest(tmp_path, STRAND_A_WORKER, guard=True), tmp_path, timeout=60)

    assert returncode == 0, out
    assert "1 passed" in out
    assert "stopped orphaned aiosqlite workers" in out
    assert "_connection_worker_thread" in out


def test_any_other_surviving_non_daemon_thread_fails_the_session_loudly_and_exits(tmp_path):
    started = time.monotonic()
    returncode, out = _finish(_pytest(tmp_path, LEAK_A_PLAIN_THREAD, guard=True), tmp_path, timeout=60)

    assert returncode == 1, out
    assert "1 passed" in out
    assert "NON-DAEMON THREADS OUTLIVED THE SESSION" in out
    assert "leaked-by-test" in out
    assert "in wait" in out  # the thread's stack, not just its name
    assert time.monotonic() - started < 30


def test_an_executor_worker_stuck_inside_a_work_item_is_not_exempt(tmp_path):
    # Idle executor workers are released by interpreter shutdown; a busy one is
    # joined forever, so it must be reported like any other leaked thread.
    returncode, out = _finish(_pytest(tmp_path, BUSY_EXECUTOR_WORKER, guard=True), tmp_path, timeout=60)

    assert returncode == 1, out
    assert "NON-DAEMON THREADS OUTLIVED THE SESSION" in out
    assert "busy-executor" in out


def test_an_idle_executor_worker_is_left_to_interpreter_shutdown(tmp_path):
    returncode, out = _finish(_pytest(tmp_path, IDLE_EXECUTOR_WORKER, guard=True), tmp_path, timeout=60)

    assert returncode == 0, out
    assert "OUTLIVED THE SESSION" not in out


def test_the_suite_conftest_installs_the_guard(request):
    # The subprocess cases load the guard with `-p`; this proves the real suite
    # loads it too, through tests/conftest.py.
    import thread_exit_guard

    hooks = {getattr(plugin, "pytest_sessionfinish", None) for plugin in request.config.pluginmanager.get_plugins()}
    assert thread_exit_guard.pytest_sessionfinish in hooks
