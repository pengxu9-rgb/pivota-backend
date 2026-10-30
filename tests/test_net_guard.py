"""The loopback-only network guard (tests/net_guard_core.py) refuses, records, and FAILS.

Each case runs an inner pytest session in a subprocess with only the guard plugin loaded, so a
test that leaks can be shown to fail without this file failing. The inner tests swallow every
error the way production code does: a refusal that only raised would leave them green, which is
the whole failure mode the guard exists for. Nothing here leaves the machine -- the guard
refuses before any syscall, and the non-loopback targets are TEST-NET-3 (203.0.113.0/24) and
`.example` names.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

INNER = textwrap.dedent(
    '''
    import asyncio, concurrent.futures, http.server, os, socket, subprocess, sys, threading, time, urllib.request

    import httpx
    import pytest
    import requests


    def _swallow(fn):
        try:
            fn()
        except OSError:
            pass



    def test_swallowed_connect_fails():
        _swallow(lambda: socket.create_connection(("203.0.113.7", 443), timeout=1))


    def test_swallowed_dns_fails():
        _swallow(lambda: socket.getaddrinfo("leak-dns.example", 443))


    async def test_swallowed_async_httpx_fails():
        async with httpx.AsyncClient() as client:
            try:
                await client.get("https://leak-async.example/")
            except httpx.HTTPError:
                pass


    def test_swallowed_requests_fails():
        try:
            requests.get("http://203.0.113.8/", timeout=1)
        except requests.RequestException:
            pass


    def test_swallowed_udp_fails():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _swallow(lambda: s.sendto(b"x", ("203.0.113.11", 53)))
        finally:
            s.close()


    def test_swallowed_reverse_dns_fails():
        _swallow(lambda: socket.gethostbyaddr("203.0.113.12"))


    def test_local_proxy_port_fails():
        _swallow(lambda: socket.create_connection(("127.0.0.1", 7897), timeout=1))


    def test_swallowed_connect_ex_fails():
        with socket.socket() as s:
            s.settimeout(1)
            assert s.connect_ex(("203.0.113.17", 80)) != 0


    def test_localhost_resolving_outside_loopback_fails():
        # A hosts file (or resolver) that maps localhost elsewhere: the answer is checked.
        from tests import net_guard_core as core

        originals = core._state()["originals"]
        real = originals["getaddrinfo"]
        originals["getaddrinfo"] = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.16", 443))]
        try:
            _swallow(lambda: socket.getaddrinfo("localhost", 443))
        finally:
            originals["getaddrinfo"] = real


    def test_child_process_leak_fails():
        code = "import socket\\ntry:\\n socket.create_connection(('203.0.113.9', 80), 1)\\nexcept OSError:\\n pass\\n"
        subprocess.run([sys.executable, "-c", code], check=True)


    def test_thread_leak_inside_its_test_fails():
        t = threading.Thread(target=lambda: _swallow(lambda: socket.create_connection(("203.0.113.13", 80), 1)))
        t.start()
        t.join()


    def test_loopback_and_localhost_are_allowed():
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            assert httpx.get(f"http://127.0.0.1:{port}/").status_code in (200, 404)
            assert requests.get(f"http://localhost:{port}/").status_code in (200, 404)
            assert {a[4][0] for a in socket.getaddrinfo("api.localhost", port)} <= {"127.0.0.1", "::1"}
            with socket.socket(socket.AF_INET6) as six:
                six.bind(("::1", 0))
                six.listen(1)
                socket.create_connection(("::1", six.getsockname()[1]), timeout=1).close()
        finally:
            server.shutdown()


    def test_a_name_idna_cannot_encode_fails_as_the_resolver_would():
        # The real getaddrinfo raises UnicodeError before any query; the guard must not mask it.
        name = "a" * 64 + ".example.com"

        async def via_loop():
            await asyncio.get_running_loop().getaddrinfo(name, 443)

        for resolve in (lambda: socket.getaddrinfo(name, 443), lambda: asyncio.run(via_loop())):
            try:
                resolve()
            except UnicodeError:
                pass
            else:
                raise AssertionError("expected UnicodeError")


    def test_an_ip_literal_resolves_without_the_network():
        # Resolving a literal sends nothing; an SSRF check classifies it by address.
        assert socket.getaddrinfo("8.8.8.8", 53)[0][4][0] == "8.8.8.8"


    def test_proxies_are_off():
        assert not any(k.lower().endswith("_proxy") for k in os.environ), sorted(os.environ)
        assert urllib.request.getproxies() == {}
    '''
)

INNER_LATE = textwrap.dedent(
    '''
    import asyncio, concurrent.futures, socket, threading, time

    import pytest


    def _swallow(fn):
        try:
            fn()
        except OSError:
            pass


    # A shared pool whose worker thread starts here, at import, outside any test: only the
    # work item can say which test submitted it.
    _POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _POOL.submit(lambda: None).result()


    def test_a_starts_work_that_fires_after_it():
        def later():
            time.sleep(0.3)
            _swallow(lambda: socket.create_connection(("203.0.113.10", 80), 1))

        threading.Thread(target=later, daemon=True).start()


    def test_b_runs_while_the_late_attempt_fires():
        time.sleep(1.0)


    def test_c_submits_work_that_fires_after_it():
        def later():
            time.sleep(0.3)
            _swallow(lambda: socket.create_connection(("203.0.113.15", 80), 1))

        _POOL.submit(later)


    def test_d_runs_while_the_submitted_work_fires():
        time.sleep(1.0)


    _TASKS = []


    @pytest.mark.asyncio(loop_scope="module")
    async def test_e_leaves_a_task_on_a_shared_loop():
        async def later():
            await asyncio.sleep(0.3)
            try:
                await asyncio.get_running_loop().getaddrinfo("late-task.example", 443)
            except OSError:
                pass

        _TASKS.append(asyncio.get_running_loop().create_task(later()))


    @pytest.mark.asyncio(loop_scope="module")
    async def test_f_runs_on_that_loop_while_the_task_fires():
        await asyncio.sleep(1.0)
    '''
)


EXPECTED_FAIL = {
    "test_swallowed_connect_fails": "connect 203.0.113.7:443",
    "test_swallowed_dns_fails": "dns leak-dns.example",
    "test_swallowed_async_httpx_fails": "dns leak-async.example",
    "test_swallowed_requests_fails": "connect 203.0.113.8:80",
    "test_swallowed_udp_fails": "203.0.113.11:53 (udp)",
    "test_swallowed_reverse_dns_fails": "reverse-dns 203.0.113.12",
    "test_local_proxy_port_fails": "127.0.0.1:7897  (a local proxy port)",
    "test_child_process_leak_fails": "203.0.113.9:80  (thread MainThread)  [in child process",
    "test_thread_leak_inside_its_test_fails": "connect 203.0.113.13:80",
    "test_swallowed_connect_ex_fails": "connect 203.0.113.17:80",
    "test_localhost_resolving_outside_loopback_fails": "dns localhost (resolved to 203.0.113.16)",
}
LATE_PASS = {
    "test_a_starts_work_that_fires_after_it",
    "test_b_runs_while_the_late_attempt_fires",
    "test_c_submits_work_that_fires_after_it",
    "test_d_runs_while_the_submitted_work_fires",
    "test_e_leaves_a_task_on_a_shared_loop",
    "test_f_runs_on_that_loop_while_the_task_fires",
}
#: Where the attempt's stack has no frame of the test itself: a child process runs `-c` code.
NO_TEST_FRAME = {"test_child_process_leak_fails"}
EXPECTED_PASS = {
    "test_loopback_and_localhost_are_allowed",
    "test_an_ip_literal_resolves_without_the_network",
    "test_a_name_idna_cannot_encode_fails_as_the_resolver_would",
    "test_proxies_are_off",
}


def _run_inner(tmp_path: Path, source: str, **env_overrides: str):
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(source)
    junit = tmp_path / "inner.xml"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NETGUARD_") and k != "PYTEST_CURRENT_TEST"}
    # The inner run is guarded by its own plugin; drop the outer run's child hook.
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Proxy env the guard must strip (a laptop's usual settings).
    env.update({"HTTPS_PROXY": "http://127.0.0.1:7897", "http_proxy": "http://127.0.0.1:7897", "NO_PROXY": "localhost"})
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file), "-p", "tests.net_guard", "-p", "no:cacheprovider",
            "--rootdir", str(tmp_path), "-o", "asyncio_mode=auto", "-q", f"--junitxml={junit}",
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    outcomes = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            bad = case.find("error")
            if bad is None:
                bad = case.find("failure")
            outcomes[case.get("name")] = None if bad is None else (bad.get("message", "") + (bad.text or ""))
    return proc, outcomes


@pytest.fixture(scope="module")
def guarded_run(tmp_path_factory):
    return _run_inner(tmp_path_factory.mktemp("netguard"), INNER, CI="true", TEST_ALLOW_NETWORK=None)


@pytest.mark.parametrize("name", sorted(EXPECTED_FAIL))
def test_a_swallowed_attempt_fails_the_test_that_made_it(guarded_run, name):
    proc, outcomes = guarded_run
    assert name in outcomes, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert outcomes[name] is not None, f"{name} passed: the guard refused (or missed) the attempt but did not fail it"
    assert EXPECTED_FAIL[name] in outcomes[name], outcomes[name]
    if name not in NO_TEST_FRAME:
        # A frame of the test itself, not just its node id in the header: an attempt recorded
        # on an anonymous thread (a resolver executor) says WHAT, but not WHERE.
        assert 'test_inner.py", line' in outcomes[name], "the report must point at the test's own frame"


@pytest.mark.parametrize("name", sorted(EXPECTED_PASS))
def test_what_stays_on_loopback_passes(guarded_run, name):
    proc, outcomes = guarded_run
    assert name in outcomes, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert outcomes[name] is None, outcomes[name]


@pytest.fixture(scope="module")
def late_run(tmp_path_factory):
    return _run_inner(tmp_path_factory.mktemp("netguard_late"), INNER_LATE, CI="true", TEST_ALLOW_NETWORK=None)


def test_a_late_attempt_fails_the_session_naming_the_test_that_started_it(late_run):
    proc, outcomes = late_run
    # Every test passed -- none of them was running its own work when it fired -- and the
    # session failed anyway.
    assert outcomes == {name: None for name in LATE_PASS}, outcomes
    assert proc.returncode == 1, proc.stdout[-3000:]
    summary = proc.stdout.split("net_guard: attempts outside a running test", 1)
    assert len(summary) == 2, proc.stdout[-3000:]
    assert "started by test_inner.py::test_a_starts_work_that_fires_after_it" in summary[1]
    assert "203.0.113.10:80" in summary[1]
    assert "fired while test_inner.py::test_b_runs_while_the_late_attempt_fires was running" in summary[1]
    # Executor work carries the test that SUBMITTED it, not the one that started the pool thread.
    assert "started by test_inner.py::test_c_submits_work_that_fires_after_it" in summary[1]
    assert "203.0.113.15:80" in summary[1]
    # A task carries the context of the test that created it, even on a loop another test runs.
    assert "started by test_inner.py::test_e_leaves_a_task_on_a_shared_loop" in summary[1]
    assert "dns late-task.example" in summary[1]


def test_the_opt_out_disables_the_guard_locally(tmp_path):
    source = textwrap.dedent(
        '''
        from tests import net_guard_core as core

        def test_off():
            assert not core.installed()
        '''
    )
    proc, outcomes = _run_inner(tmp_path, source, TEST_ALLOW_NETWORK="1", CI=None)
    assert outcomes == {"test_off": None}, proc.stdout[-3000:]
    assert "net_guard: DISABLED by TEST_ALLOW_NETWORK=1" in proc.stdout


def test_the_opt_out_is_ignored_under_ci(tmp_path):
    source = textwrap.dedent(
        '''
        import socket

        def test_leak():
            try:
                socket.create_connection(("203.0.113.14", 80), timeout=1)
            except OSError:
                pass
        '''
    )
    proc, outcomes = _run_inner(tmp_path, source, TEST_ALLOW_NETWORK="1", CI="true")
    assert outcomes.get("test_leak") and "203.0.113.14:80" in outcomes["test_leak"], proc.stdout[-3000:]


def test_this_run_is_guarded():
    """The guard is live in the real suite, not only in the inner sessions above."""
    from tests import net_guard_core as core

    assert core.installed() or core.network_allowed()
    if core.installed():
        with pytest.raises(OSError):
            socket_module = __import__("socket")
            socket_module.getaddrinfo("this-run-is-guarded.example", 443)
        # That refusal was deliberate: take it back so this test does not fail itself.
        taken = core.take(os.environ.get(core.ORIGIN_ENV))
        assert [v["target"] for v in taken] == ["this-run-is-guarded.example"]
