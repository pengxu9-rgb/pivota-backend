"""pytest plugin half of the loopback-only network guard; the guard is tests/net_guard_core.py,
whose docstring says what it refuses, why, and how a refusal becomes a failure.

Registered from the repo-root conftest.py so it covers tests/ and readiness/tests/ alike, and
installed when this module is imported -- before collection, so before any test module or the
app imports an HTTP client.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List

import pytest

from tests import net_guard_core as core

core.install()


def _check_postgres_env() -> None:
    """libpq (psycopg2) connects in C, where the guard cannot see it: refuse to run at all
    while a Postgres URL in the env points off this machine."""
    if core.network_allowed():
        return
    problems = core.postgres_env_problems()
    if problems:
        raise pytest.UsageError(
            "[net_guard] a Postgres URL in the env is not loopback or a unix socket; psycopg2 "
            "(libpq) would connect to it past the guard. Point it at a local database or unset "
            "it" + ("" if core._ci() else f" ({core.ALLOW_ENV}=1 skips this locally)") + ":\n  "
            + "\n  ".join(problems)
        )


def pytest_configure(config):
    _check_postgres_env()


def pytest_collection_finish(session):
    # Again after collection: a conftest or test module may have set one at import.
    _check_postgres_env()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    s = core._state()
    if not s["installed"]:
        return (yield)
    s["current"] = item.nodeid
    os.environ[core.ORIGIN_ENV] = item.nodeid
    token = core._ORIGIN.set(item.nodeid)
    try:
        return (yield)
    finally:
        core._ORIGIN.reset(token)
        s["current"] = None
        os.environ.pop(core.ORIGIN_ENV, None)


def _fail_for(item) -> None:
    mine = core.take(item.nodeid)
    if mine:
        raise pytest.fail.Exception(
            core.format_violations(mine, header=f"[net_guard] {item.nodeid} reached outside loopback ({len(mine)} attempt(s))."),
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    if not core._state()["installed"]:
        return (yield)
    try:
        result = yield
    except Exception as exc:
        # A teardown that failed on its own still owes the guard its report, or the attempts
        # would surface at the session end as if no test had made them.
        _fail_for(item)
        raise exc
    _fail_for(item)
    return result


_LATE: List[Dict[str, Any]] = []


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    s = core._state()
    if not s["installed"]:
        return
    _LATE.extend(core.take_all())
    if _LATE and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _LATE:
        terminalreporter.section("net_guard: attempts outside a running test", sep="=", red=True)
        by_origin: Dict[str, List[Dict[str, Any]]] = {}
        for v in _LATE:
            by_origin.setdefault(v.get("origin") or "(no test: import, collection, session, or an untracked thread)", []).append(v)
        for origin, entries in by_origin.items():
            terminalreporter.write_line(
                core.format_violations(entries, header=f"started by {origin} -- after it finished, or outside any test:")
            )
    if not core._state()["installed"] and core.network_allowed():
        terminalreporter.write_line(f"net_guard: DISABLED by {core.ALLOW_ENV}=1 (local only; ignored under CI)", yellow=True)


def pytest_unconfigure(config):
    s = core._state()
    if s.get("run_dir_owned") and s.get("run_dir"):
        shutil.rmtree(s["run_dir"], ignore_errors=True)
