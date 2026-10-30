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


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    if not core._state()["installed"]:
        return result
    mine = core.take(item.nodeid)
    if mine:
        raise pytest.fail.Exception(
            core.format_violations(mine, header=f"[net_guard] {item.nodeid} reached outside loopback ({len(mine)} attempt(s))."),
            pytrace=False,
        )
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
            by_origin.setdefault(v.get("origin") or "(no test: import, collection or session)", []).append(v)
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
