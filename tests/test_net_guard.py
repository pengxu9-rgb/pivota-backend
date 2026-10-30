"""The loopback-only network guard (tests/net_guard_core.py) refuses, records, and FAILS.

Each case runs an inner pytest session in a subprocess with only the guard plugin loaded, so a
test that leaks can be shown to fail without this file failing. The inner tests swallow every
error the way production code does: a refusal that only raised would leave them green, which is
the whole failure mode the guard exists for.

A broken guard must fail these tests, not send traffic. So an inner case that expects a refusal
runs with the guard's `originals` -- what it calls for traffic it lets through -- swapped for a
sink that raises and records: a target the guard wrongly allows lands there, and the case fails
by saying so; every case also checks the sink stayed silent, so a guard that records an attempt
and then makes it anyway fails too. A spawned child installs the same sink itself (or, if the
guard did not reach it, blocks the socket calls outright). Cases that need real loopback traffic swap in a stub that passes literals and
`localhost` only, so a name the guard should have answered itself fails instead of reaching the
resolver. The targets are TEST-NET-3 (203.0.113.0/24), 2001:db8::/32 and `.example` names, and
the inner sessions' proxy settings point at a listener this file owns. Any outer guard (a
sitecustomize on PYTHONPATH) stays loaded in the inner sessions and their children.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import net_guard_core as core

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Loaded with `-p` ahead of the guard plugin in every inner session.
SUPPORT = textwrap.dedent(
    '''
    """Inner-session support, loaded before the guard: a stand-in system proxy, and the sinks."""
    import ipaddress, os, socket, urllib.request

    import pytest

    # An OS proxy setting, on every platform: urllib.request.getproxies() falls back to the
    # system-proxy reader when no proxy env is set, as it does on a Mac. The guard must blank it.
    _PROXY = os.environ.get("SELFTEST_SYSTEM_PROXY_URL")
    if _PROXY:
        def _system_proxies():
            return {"http": _PROXY, "https": _PROXY}

        _environment = urllib.request.getproxies_environment
        urllib.request.getproxies_macosx_sysconf = _system_proxies
        urllib.request.getproxies_registry = _system_proxies
        urllib.request.getproxies = lambda: (
            _environment() or urllib.request.getproxies_macosx_sysconf() or urllib.request.getproxies_registry()
        )

    NETWORK_KEYS = (
        "connect", "connect_ex", "sendto", "sendmsg", "create_connection", "sock_connect", "getaddrinfo",
        "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "loop_getaddrinfo",
    )
    _ASYNC = {"sock_connect", "loop_getaddrinfo"}
    _RAW_SOCKET = {"connect", "connect_ex", "sendto", "sendmsg"}  # a name here is resolved in C


    def _host_of(key, args):
        try:
            if key in ("connect", "connect_ex"):
                sock, address = args[0], args[1]
            elif key == "sendto":
                sock, address = args[0], args[-1]
            elif key == "sendmsg":
                sock, address = args[0], (args[4] if len(args) > 4 else None)
            elif key == "sock_connect":
                sock, address = args[1], args[2]
            elif key == "create_connection":
                return args[0][0]
            elif key == "loop_getaddrinfo":
                return args[1]
            elif key == "getnameinfo":
                return args[0][0]
            else:
                return args[0]
            if address is None or sock.family not in (socket.AF_INET, socket.AF_INET6):
                return None
            return address[0]
        except Exception:
            return None


    def _literal(host):
        try:
            ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
            return True
        except (ValueError, AttributeError):
            return False


    def install(allow):
        """Swap the guard's originals for stubs that pass a call on only if allow(key, host)."""
        from tests import net_guard_core as core

        originals = core._state()["originals"]
        saved = dict(originals)
        reached = []

        def stub(key, real):
            def allowed(args):
                host = _host_of(key, args)
                if allow(key, host):
                    return True
                reached.append(f"{key} {host!r}")
                return False

            if key in _ASYNC:
                async def call(*args, **kwargs):
                    if allowed(args):
                        return await real(*args, **kwargs)
                    raise ConnectionRefusedError(f"[selftest sink] {key} reached the layer below the guard")
            else:
                def call(*args, **kwargs):
                    if allowed(args):
                        return real(*args, **kwargs)
                    raise ConnectionRefusedError(f"[selftest sink] {key} reached the layer below the guard")
            return call

        for key in NETWORK_KEYS:
            if saved.get(key) is not None:
                originals[key] = stub(key, saved[key])

        def restore():
            originals.clear()
            originals.update(saved)

        return reached, restore


    _RESOLVER = {"getaddrinfo", "loop_getaddrinfo", "gethostbyname", "gethostbyname_ex"}


    def nothing_that_can_leave(key, host):
        # Resolving a literal (or a passive bind lookup) sends nothing; everything else stops.
        return key in _RESOLVER and (host is None or _literal(host))


    def no_name_below_the_guard(key, host):
        if host is None or _literal(host):
            return True
        return host == "localhost" and key not in _RAW_SOCKET


    def install_in_this_child():
        """A spawned child's sink: below its guard when the guard is installed there, else in place
        of the socket calls themselves, so a child the guard failed to reach still sends nothing."""
        from tests import net_guard_core as core

        # Installed means patched: the flag alone is not enough, the calls must be the guard's.
        patched = all(
            getattr(f, "__module__", None) == "_pivota_net_guard_child"
            for f in (socket.create_connection, socket.socket.connect, socket.socket.connect_ex,
                      socket.socket.sendto, socket.getaddrinfo)
        )
        if core.installed() and patched:
            return install(nothing_that_can_leave)[0]
        reached = []

        def refuse(name):
            def call(*args, **kwargs):
                reached.append(name)
                raise ConnectionRefusedError(f"[selftest sink] {name} in a child without the guard")
            return call

        socket.socket.connect = refuse("connect")
        socket.socket.connect_ex = refuse("connect_ex")
        socket.socket.sendto = refuse("sendto")
        socket.create_connection = refuse("create_connection")
        real_getaddrinfo, refuse_getaddrinfo = socket.getaddrinfo, refuse("getaddrinfo")
        socket.getaddrinfo = lambda host, *a, **k: (
            real_getaddrinfo(host, *a, **k) if host is None or _literal(host) else refuse_getaddrinfo()
        )
        return reached


    @pytest.fixture
    def sink():
        """Nothing the guard lets through goes anywhere."""
        reached, restore = install(nothing_that_can_leave)
        yield reached
        restore()
        assert not reached, f"the guard let these through to the layer below it: {reached}"


    @pytest.fixture
    def no_resolver():
        """Real loopback traffic, but no name but `localhost` reaches the real resolver."""
        reached, restore = install(no_name_below_the_guard)
        yield reached
        restore()
        assert not reached, f"a name the guard should have answered reached the layer below it: {reached}"
    '''
)

INNER = textwrap.dedent(
    '''
    import asyncio, errno, http.server, os, socket, subprocess, sys, threading, urllib.request

    import httpx
    import pytest
    import requests

    from tests import net_guard_core as core


    def _swallow(fn):
        try:
            fn()
        except OSError:
            pass


    def _originals():
        return core._state()["originals"]


    def test_swallowed_connect_fails(sink):
        _swallow(lambda: socket.create_connection(("203.0.113.7", 443), timeout=1))


    def test_swallowed_dns_fails(sink):
        _swallow(lambda: socket.getaddrinfo("leak-dns.example", 443))


    def test_a_name_that_only_starts_like_localhost_fails(sink):
        _swallow(lambda: socket.getaddrinfo("localhost.leak.example", 443))


    async def test_swallowed_async_httpx_fails(sink):
        async with httpx.AsyncClient() as client:
            try:
                await client.get("https://leak-async.example/")
            except httpx.HTTPError:
                pass


    async def test_swallowed_sock_connect_fails(sink):
        with socket.socket() as s:
            s.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(s, ("203.0.113.26", 80))
            except OSError:
                pass


    def test_swallowed_requests_fails(sink):
        try:
            requests.get("http://203.0.113.8/", timeout=1)
        except requests.RequestException:
            pass


    def test_swallowed_udp_fails(sink):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            _swallow(lambda: s.sendto(b"x", ("203.0.113.11", 53)))


    def test_swallowed_udp_with_flags_fails(sink):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            _swallow(lambda: s.sendto(b"x", 0, ("203.0.113.28", 53)))


    def test_swallowed_sendmsg_fails(sink):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            _swallow(lambda: s.sendmsg([b"x"], [], 0, ("203.0.113.29", 53)))


    def test_swallowed_reverse_dns_fails(sink):
        _swallow(lambda: socket.gethostbyaddr("203.0.113.12"))


    def test_swallowed_getnameinfo_fails(sink):
        _swallow(lambda: socket.getnameinfo(("203.0.113.22", 80), 0))


    def test_local_proxy_port_fails(sink):
        _swallow(lambda: socket.create_connection(("127.0.0.1", 7897), timeout=1))


    def test_local_proxy_port_by_name_fails(sink):
        with socket.socket() as s:
            _swallow(lambda: s.connect(("localhost", 7897)))


    def test_ipv6_connect_fails(sink):
        with socket.socket(socket.AF_INET6) as s:
            _swallow(lambda: s.connect(("2001:db8::1", 80)))


    def test_ipv4_mapped_ipv6_connect_fails(sink):
        with socket.socket(socket.AF_INET6) as s:
            _swallow(lambda: s.connect(("::ffff:203.0.113.18", 80)))


    def test_link_local_metadata_address_fails(sink):
        _swallow(lambda: socket.create_connection(("169.254.169.254", 80), timeout=1))


    def test_names_that_only_look_like_literals_fail(sink):
        # The C resolver would look each of these up as a name.
        for host in ("[127.0.0.1]", "127.0.0.1%leak.example"):
            with socket.socket() as s:
                _swallow(lambda: s.connect((host, 80)))
        _swallow(lambda: socket.getaddrinfo("[203.0.113.30]", 80))


    def test_swallowed_connect_ex_fails(sink):
        with socket.socket() as s:
            s.connect_ex(("203.0.113.17", 80))


    def test_localhost_resolving_outside_loopback_fails(sink):
        # A hosts file (or resolver) that maps localhost elsewhere: the answer is checked.
        _originals()["getaddrinfo"] = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.16", 443))]
        _swallow(lambda: socket.getaddrinfo("localhost", 443))


    def test_gethostbyname_resolving_outside_loopback_fails(sink):
        _originals()["gethostbyname"] = lambda host: "203.0.113.19"
        _swallow(lambda: socket.gethostbyname("localhost"))


    def test_gethostbyname_ex_fails(sink):
        _swallow(lambda: socket.gethostbyname_ex("leak-ex.example"))


    def test_gethostbyname_ex_resolving_outside_loopback_fails(sink):
        _originals()["gethostbyname_ex"] = lambda host: ("localhost", [], ["127.0.0.1", "203.0.113.23"])
        _swallow(lambda: socket.gethostbyname_ex("localhost"))


    def test_child_process_leak_fails(sink):
        # Twice to the same target: each attempt is its own entry, not merged by target. The
        # child installs its own sink first: the one above does not cross into it.
        code = (
            "import socket, selftest_support\\n"
            "reached = selftest_support.install_in_this_child()\\n"
            "for _ in range(2):\\n"
            " try:\\n"
            "  socket.create_connection(('203.0.113.9', 80), 1)\\n"
            " except OSError:\\n"
            "  pass\\n"
            "assert not reached, f'the guard let these through to the layer below it: {reached}'\\n"
        )
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert child.returncode == 0, child.stderr


    def test_forked_child_leak_fails(sink):
        pid = os.fork()
        if pid == 0:
            try:
                socket.create_connection(("203.0.113.24", 80), timeout=1)
            except BaseException:
                pass
            # The child's copy of the sink is the only one that saw its calls: report by exit code.
            os._exit(3 if sink else 0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, "the guard let these through to the layer below it (in the forked child)"


    def test_thread_leak_inside_its_test_fails(sink):
        t = threading.Thread(target=lambda: _swallow(lambda: socket.create_connection(("203.0.113.13", 80), 1)))
        t.start()
        t.join()


    @pytest.fixture
    def teardown_that_fails():
        yield
        raise RuntimeError("this fixture's own teardown failed")


    def test_a_failing_teardown_still_reports_the_attempt(sink, teardown_that_fails):
        _swallow(lambda: socket.create_connection(("203.0.113.32", 80), timeout=1))


    def test_connect_ex_reports_the_refusal_and_records_it(sink):
        with socket.socket() as s:
            assert s.connect_ex(("203.0.113.17", 81)) == errno.ECONNREFUSED
        # Taken back, so this test passes only if the refusal was recorded against it.
        assert [v["target"] for v in core.take(os.environ[core.ORIGIN_ENV])] == ["203.0.113.17:81"]


    def test_loopback_and_localhost_are_allowed(no_resolver):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            assert httpx.get(f"http://127.0.0.1:{port}/").status_code in (200, 404)
            assert requests.get(f"http://localhost:{port}/").status_code in (200, 404)
            # *.localhost is answered without asking a resolver, by every entry point.
            assert {a[4][0] for a in socket.getaddrinfo("api.localhost", port)} <= {"127.0.0.1", "::1"}
            assert socket.gethostbyname("api.localhost") == "127.0.0.1"
            assert socket.gethostbyname_ex("api.localhost") == ("api.localhost", [], ["127.0.0.1"])
            # Every other spelling of localhost too: "localhost." is fully qualified, for DNS.
            for host in ("LOCALHOST", "localhost.", "Localhost"):
                assert {a[4][0] for a in socket.getaddrinfo(host, port)} <= {"127.0.0.1", "::1"}
                assert socket.gethostbyname(host) == "127.0.0.1"
                assert socket.gethostbyname_ex(host)[2] == ["127.0.0.1"]
                socket.create_connection((host, port), timeout=1).close()
            socket.create_connection(("api.localhost", port), timeout=1).close()
            for host in ("api.localhost", "LOCALHOST", "localhost."):
                with socket.socket() as s:
                    s.settimeout(1)
                    s.connect((host, port))
            with socket.socket(socket.AF_INET6) as six:
                six.bind(("::1", 0))
                six.listen(1)
                socket.create_connection(("::1", six.getsockname()[1]), timeout=1).close()
        finally:
            server.shutdown()


    def test_numeric_getnameinfo_needs_no_lookup(no_resolver):
        flags = socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
        assert socket.getnameinfo(("203.0.113.22", 80), flags) == ("203.0.113.22", "80")


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


    def test_an_ip_literal_resolves_without_the_network(no_resolver):
        # Resolving a literal sends nothing; an SSRF check classifies it by address.
        assert socket.getaddrinfo("8.8.8.8", 53)[0][4][0] == "8.8.8.8"


    def test_proxies_are_off(no_resolver):
        assert not any(k.lower().endswith("_proxy") for k in os.environ), sorted(os.environ)
        # selftest_support set a system proxy before the guard installed; the guard blanked it.
        assert urllib.request.getproxies() == {}
        assert httpx._utils.get_environment_proxies() == {}
        assert requests.utils.get_environ_proxies("http://203.0.113.20/") == {}
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # Through a proxy these would reach the outer test's listener instead of this server.
            url = f"http://proxy-check.localhost:{server.server_address[1]}/"
            assert httpx.get(url).status_code in (200, 404)
            assert requests.get(url).status_code in (200, 404)
        finally:
            server.shutdown()
    '''
)

INNER_LATE = textwrap.dedent(
    '''
    import _thread, asyncio, concurrent.futures, socket, threading, time

    import pytest

    from selftest_support import install, nothing_that_can_leave

    # Nothing below the guard for the whole session: these attempts fire between tests.
    _REACHED, _ = install(nothing_that_can_leave)


    def _swallow(fn):
        try:
            fn()
        except OSError:
            pass


    # A shared pool whose worker thread starts here, at import, outside any test: only the
    # work item can say which test submitted it.
    _POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _POOL.submit(lambda: None).result()

    # A thread started at import, outside any test, that fires during test_g: no test started
    # it, so none may be blamed for it.
    _FIRE = threading.Event()


    def _started_at_import():
        _FIRE.wait(30)
        _swallow(lambda: socket.create_connection(("203.0.113.21", 80), 1))


    threading.Thread(target=_started_at_import, daemon=True).start()


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


    def test_g_runs_while_import_time_work_fires():
        _FIRE.set()
        time.sleep(0.5)


    def test_h_starts_a_raw_thread():
        # _thread bypasses threading.Thread.start, so nothing records who started it.
        done = threading.Event()

        def raw():
            _swallow(lambda: socket.create_connection(("203.0.113.31", 80), 1))
            done.set()

        _thread.start_new_thread(raw, ())
        done.wait(5)


    def test_z_nothing_got_past_the_guard():
        # Runs last, after every late attempt above has fired.
        assert not _REACHED, f"the guard let these through to the layer below it: {_REACHED}"
    '''
)


EXPECTED_FAIL = {
    "test_swallowed_connect_fails": ("connect 203.0.113.7:443",),
    "test_swallowed_dns_fails": ("dns leak-dns.example",),
    "test_a_name_that_only_starts_like_localhost_fails": ("dns localhost.leak.example",),
    "test_swallowed_async_httpx_fails": ("dns leak-async.example",),
    "test_swallowed_sock_connect_fails": ("connect 203.0.113.26:80",),
    "test_swallowed_requests_fails": ("connect 203.0.113.8:80",),
    "test_swallowed_udp_fails": ("203.0.113.11:53 (udp)",),
    "test_swallowed_udp_with_flags_fails": ("203.0.113.28:53 (udp)",),
    "test_swallowed_sendmsg_fails": ("203.0.113.29:53 (sendmsg)",),
    "test_swallowed_reverse_dns_fails": ("reverse-dns 203.0.113.12",),
    "test_swallowed_getnameinfo_fails": ("reverse-dns 203.0.113.22",),
    "test_local_proxy_port_fails": ("127.0.0.1:7897  (a local proxy port)",),
    "test_local_proxy_port_by_name_fails": ("localhost:7897  (a local proxy port)",),
    "test_ipv6_connect_fails": ("connect 2001:db8::1:80",),
    "test_ipv4_mapped_ipv6_connect_fails": ("connect ::ffff:203.0.113.18:80",),
    "test_link_local_metadata_address_fails": ("connect 169.254.169.254:80",),
    "test_names_that_only_look_like_literals_fail": (
        "connect [127.0.0.1]:80", "connect 127.0.0.1%leak.example:80", "dns [203.0.113.30]",
    ),
    "test_swallowed_connect_ex_fails": ("connect 203.0.113.17:80",),
    "test_localhost_resolving_outside_loopback_fails": ("dns localhost (resolved to 203.0.113.16)",),
    "test_gethostbyname_resolving_outside_loopback_fails": ("dns localhost (resolved to 203.0.113.19)",),
    "test_gethostbyname_ex_fails": ("dns leak-ex.example",),
    "test_gethostbyname_ex_resolving_outside_loopback_fails": ("dns localhost (resolved to 203.0.113.23)",),
    "test_child_process_leak_fails": ("(2 attempt(s))", "203.0.113.9:80  (thread MainThread)  [in child process"),
    "test_forked_child_leak_fails": ("connect 203.0.113.24:80",),
    "test_thread_leak_inside_its_test_fails": ("connect 203.0.113.13:80",),
    "test_a_failing_teardown_still_reports_the_attempt": ("connect 203.0.113.32:80",),
}
LATE_PASS = {
    "test_a_starts_work_that_fires_after_it",
    "test_b_runs_while_the_late_attempt_fires",
    "test_c_submits_work_that_fires_after_it",
    "test_d_runs_while_the_submitted_work_fires",
    "test_e_leaves_a_task_on_a_shared_loop",
    "test_f_runs_on_that_loop_while_the_task_fires",
    "test_g_runs_while_import_time_work_fires",
    "test_h_starts_a_raw_thread",
    "test_z_nothing_got_past_the_guard",
}
#: Where the attempt's stack has no frame of the test itself: a child process runs `-c` code.
NO_TEST_FRAME = {"test_child_process_leak_fails"}
EXPECTED_PASS = {
    "test_connect_ex_reports_the_refusal_and_records_it",
    "test_loopback_and_localhost_are_allowed",
    "test_numeric_getnameinfo_needs_no_lookup",
    "test_an_ip_literal_resolves_without_the_network",
    "test_a_name_idna_cannot_encode_fails_as_the_resolver_would",
    "test_proxies_are_off",
}
NO_TEST = "(no test: import, collection, session, or an untracked thread)"
#: What a sink says when the guard let an attempt through to the layer below it. A guard that
#: records an attempt and then makes it anyway produces the expected report AND this.
SINK_REACHED = "let these through to the layer below"


@pytest.fixture(scope="module")
def proxy_listener():
    """Stands in for a laptop's local proxy: the inner sessions' proxy env and system proxy point
    here, so a guard that failed to switch proxies off shows up as a connection to this socket."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    accepted = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, peer = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            accepted.append(peer)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.getsockname()[1]}", accepted=accepted)
    stop.set()
    thread.join(2)
    server.close()
    # After every inner session of this module: none went through the proxy.
    assert accepted == [], f"an inner session connected to the stand-in proxy: {accepted}"


def _run_inner(tmp_path: Path, source: str, proxy, *, path_before=(), **env_overrides: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "selftest_support.py").write_text(SUPPORT)
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(source)
    junit = tmp_path / "inner.xml"
    env = {k: v for k, v in os.environ.items() if k not in (core.RUN_DIR_ENV, core.ORIGIN_ENV, "PYTEST_CURRENT_TEST")}
    # Prepend, never replace: the outer run's child hook stays on the path (and runs whatever
    # sitecustomize it shadows), and so does any outer guard after it.
    outer = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), *map(str, path_before), *outer])
    # Proxy settings the guard must switch off: env, both cases, and a system proxy (SUPPORT).
    env.update({"HTTPS_PROXY": proxy.url, "http_proxy": proxy.url, "ALL_PROXY": proxy.url, "NO_PROXY": "localhost",
                "SELFTEST_SYSTEM_PROXY_URL": proxy.url})
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file), "-p", "selftest_support", "-p", "tests.net_guard",
            "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "-o", "asyncio_mode=auto", "-q", f"--junitxml={junit}",
        ],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    outcomes = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            # pytest writes a call failure and a teardown error as two <testcase> elements with
            # the same name: keep the text of every one, so neither hides the other.
            bad = case.findall("failure") + case.findall("error")
            text = "\n".join(b.get("message", "") + (b.text or "") for b in bad) or None
            prev = outcomes.get(case.get("name"))
            outcomes[case.get("name")] = "\n".join(t for t in (prev, text) if t) or None
    return proc, outcomes


@pytest.fixture(scope="module")
def guarded_run(tmp_path_factory, proxy_listener):
    return _run_inner(tmp_path_factory.mktemp("netguard"), INNER, proxy_listener, CI="true", TEST_ALLOW_NETWORK=None)


@pytest.mark.parametrize("name", sorted(EXPECTED_FAIL))
def test_a_swallowed_attempt_fails_the_test_that_made_it(guarded_run, name):
    proc, outcomes = guarded_run
    assert name in outcomes, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert outcomes[name] is not None, f"{name} passed: the guard refused (or missed) the attempt but did not fail it"
    for expected in EXPECTED_FAIL[name]:
        assert expected in outcomes[name], outcomes[name]
    # Each attempt fired inside its own test (or a child it ran): none is labelled late.
    assert "fired while" not in outcomes[name], outcomes[name]
    # Refused, not recorded-and-made: nothing reached the layer below the guard.
    assert SINK_REACHED not in outcomes[name], outcomes[name]
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
def late_run(tmp_path_factory, proxy_listener):
    return _run_inner(tmp_path_factory.mktemp("netguard_late"), INNER_LATE, proxy_listener, CI="true", TEST_ALLOW_NETWORK=None)


def _late_groups(stdout: str):
    summary = stdout.split("net_guard: attempts outside a running test", 1)
    assert len(summary) == 2, stdout[-3000:]
    groups = {}
    for chunk in summary[1].split("started by ")[1:]:
        origin, _, body = chunk.partition(" -- after it finished, or outside any test:")
        groups[origin.strip()] = body
    return groups


def test_a_late_attempt_fails_the_session_naming_the_test_that_started_it(late_run):
    proc, outcomes = late_run
    # Every test passed -- none of them was running its own work when it fired -- and the
    # session failed anyway.
    assert outcomes == {name: None for name in LATE_PASS}, outcomes
    assert proc.returncode == 1, proc.stdout[-3000:]
    assert SINK_REACHED not in proc.stdout, proc.stdout[-3000:]
    groups = _late_groups(proc.stdout)
    a = groups["test_inner.py::test_a_starts_work_that_fires_after_it"]
    assert "203.0.113.10:80" in a
    assert "fired while test_inner.py::test_b_runs_while_the_late_attempt_fires was running" in a
    # Executor work carries the test that SUBMITTED it, not the one that started the pool thread.
    assert "203.0.113.15:80" in groups["test_inner.py::test_c_submits_work_that_fires_after_it"]
    # A task carries the context of the test that created it, even on a loop another test runs.
    assert "dns late-task.example" in groups["test_inner.py::test_e_leaves_a_task_on_a_shared_loop"]
    # Work no test started is blamed on none, whichever test happens to be running.
    nobody = groups[NO_TEST]
    assert "203.0.113.21:80" in nobody
    assert "fired while test_inner.py::test_g_runs_while_import_time_work_fires was running" in nobody
    assert "203.0.113.31:80" in nobody
    assert set(groups) == {
        "test_inner.py::test_a_starts_work_that_fires_after_it",
        "test_inner.py::test_c_submits_work_that_fires_after_it",
        "test_inner.py::test_e_leaves_a_task_on_a_shared_loop",
        NO_TEST,
    }, sorted(groups)


def test_the_opt_out_disables_the_guard_locally(tmp_path, proxy_listener):
    source = textwrap.dedent(
        '''
        from tests import net_guard_core as core

        def test_off():
            assert not core.installed()
        '''
    )
    proc, outcomes = _run_inner(tmp_path, source, proxy_listener, TEST_ALLOW_NETWORK="1", CI=None)
    assert outcomes == {"test_off": None}, proc.stdout[-3000:]
    assert "net_guard: DISABLED by TEST_ALLOW_NETWORK=1" in proc.stdout


def test_the_opt_out_is_ignored_under_ci(tmp_path, proxy_listener):
    source = textwrap.dedent(
        '''
        import socket

        from tests import net_guard_core as core

        def test_leak(sink):
            assert core.installed(), "the guard is off under CI"
            try:
                socket.create_connection(("203.0.113.14", 80), timeout=1)
            except OSError:
                pass
        '''
    )
    proc, outcomes = _run_inner(tmp_path, source, proxy_listener, TEST_ALLOW_NETWORK="1", CI="true")
    assert outcomes.get("test_leak") and "203.0.113.14:80" in outcomes["test_leak"], proc.stdout[-3000:]
    assert SINK_REACHED not in outcomes["test_leak"], outcomes["test_leak"]


def test_a_child_runs_the_sitecustomize_it_shadows_with_the_guard_on_top(tmp_path, proxy_listener):
    site = tmp_path / "outer_site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        textwrap.dedent(
            '''
            import socket

            _real = socket.socket.connect


            def outer_connect(self, address):
                return _real(self, address)


            socket.socket.connect = outer_connect
            socket._selftest_outer_site = True
            '''
        )
    )
    source = textwrap.dedent(
        '''
        import json, subprocess, sys

        def test_grandchild():
            code = (
                "import json, socket\\n"
                "s = getattr(socket, '_pivota_net_guard_state', {})\\n"
                "below = s.get('originals', {}).get('connect')\\n"
                "print(json.dumps({'outer': getattr(socket, '_selftest_outer_site', False), 'child': s.get('child'),"
                " 'installed': s.get('installed'), 'below': getattr(below, '__name__', None)}))\\n"
            )
            out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout
            assert json.loads(out) == {"outer": True, "child": True, "installed": True, "below": "outer_connect"}
        '''
    )
    proc, outcomes = _run_inner(tmp_path / "run", source, proxy_listener, path_before=[site], CI="true", TEST_ALLOW_NETWORK=None)
    assert outcomes == {"test_grandchild": None}, proc.stdout[-3000:] + proc.stderr[-3000:]


def test_a_child_that_cannot_install_the_guard_exits(tmp_path):
    shim = tmp_path / "_netguard_child"
    shim.mkdir()
    # No net_guard_core.py beside it: the shim cannot load the guard.
    shutil.copy(REPO_ROOT / "tests" / "_netguard_child" / "sitecustomize.py", shim)
    env = {**os.environ, "PYTHONPATH": str(shim), core.RUN_DIR_ENV: str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", "print('ran unguarded')"], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 70, (proc.returncode, proc.stdout, proc.stderr)
    assert "ran unguarded" not in proc.stdout
    assert "exiting rather than running unguarded" in proc.stderr


def test_two_copies_of_the_shim_on_the_path_install_the_guard_once(tmp_path):
    # The same directory under a second spelling (a symlink), and another checkout's copy: each
    # shim skips the other instead of chaining to it without end.
    real = REPO_ROOT / "tests" / "_netguard_child"
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    other = tmp_path / "other_checkout" / "tests"
    shutil.copytree(real, other / "_netguard_child")
    shutil.copy(REPO_ROOT / "tests" / "net_guard_core.py", other)
    # And a sitecustomize after both copies (a safety net): it must still run, under the guard.
    site = tmp_path / "outer_site"
    site.mkdir()
    (site / "sitecustomize.py").write_text("import socket\nsocket._selftest_outer_site = True\n")
    code = (
        "import json, socket; s = socket._pivota_net_guard_state; "
        "print(json.dumps([s['installed'], s['child'], getattr(socket, '_selftest_outer_site', False)]))"
    )
    for path in ([alias, real], [real, alias], [other / "_netguard_child", real], [real, other / "_netguard_child"]):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, [*path, site])), core.RUN_DIR_ENV: str(tmp_path)}
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        assert (proc.returncode, proc.stdout.strip()) == (0, "[true, true, true]"), (path, proc.returncode, proc.stderr[-2000:])


@pytest.mark.parametrize(
    "env, problems",
    [
        ({"DATABASE_URL": "sqlite+aiosqlite:///tmp/x.db"}, []),
        ({"DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/x"}, []),
        ({"DATABASE_URL": "postgresql://u:p%40ss@127.0.0.1/x", "PIVOTA_TEST_PG_URL": "postgres://[::1]:5432/x"}, []),
        ({"DATABASE_URL": "postgresql:///x?host=/var/run/postgresql"}, []),
        ({"DATABASE_URL": "postgresql://u@%2Ftmp/x", "PGHOST": "/tmp"}, []),
        ({"SENTRY_DSN": "https://key@sentry.example/1"}, []),
        ({"DATABASE_URL": "postgresql://u:p@10.25.0.2/x"}, ["DATABASE_URL -> 10.25.0.2"]),
        ({"MIGRATION_DB_URL": "postgresql+psycopg2://u@db.example:5432/x"}, ["MIGRATION_DB_URL -> db.example"]),
        ({"DATABASE_URL": "postgresql://u@localhost,db.example:5433/x"}, ["DATABASE_URL -> db.example"]),
        ({"DATABASE_URL": "postgresql://u@localhost/x?host=203.0.113.27"}, ["DATABASE_URL -> 203.0.113.27"]),
        ({"PG_DSN": "dbname=x host=db.example user=u"}, ["PG_DSN -> db.example"]),
        ({"PGHOST": "10.0.0.5"}, ["PGHOST -> 10.0.0.5"]),
        # libpq's key=value form: spaces around "=", quoted values, hostaddr.
        ({"PG_DSN": "dbname=x host = db.example"}, ["PG_DSN -> db.example"]),
        ({"PG_DSN": "host = db.example port=5432"}, ["PG_DSN -> db.example"]),
        ({"PG_DSN": "dbname=x hostaddr=203.0.113.41"}, ["PG_DSN -> 203.0.113.41"]),
        ({"PG_DSN": "host='/var/run/postgresql' dbname=x", "NOT_A_DSN": "a=b c=d"}, []),
        # A service file can name any host.
        ({"PG_DSN": "service=prod"}, ["PG_DSN -> service=prod (a service file can name any host)"]),
        ({"DATABASE_URL": "postgresql:///x?service=prod"}, ["DATABASE_URL -> service=prod (a service file can name any host)"]),
        ({"PGSERVICE": "prod"}, ["PGSERVICE -> service=prod (a service file can name any host)"]),
        # Only localhost itself: libpq sends any other name to the system resolver.
        ({"DATABASE_URL": "postgresql://u@db.localhost/x"}, ["DATABASE_URL -> db.localhost"]),
        ({"DATABASE_URL": "postgresql://u@localhost.db.example/x"}, ["DATABASE_URL -> localhost.db.example"]),
        # A URL inside a longer value, and one urlsplit cannot parse: reported, not an error.
        ({"APP_CONFIG": '{"db": "postgresql://u@db.example/x", "n": 1}'}, ["APP_CONFIG -> db.example"]),
        ({"DATABASE_URL": "postgresql://u@[db.example]/x"}, ["DATABASE_URL -> '[db.example]/x' (unparseable)"]),
        # Every URL in a value, and a JSON-escaped one.
        ({"APP_CONFIG": "postgresql://u@localhost/x postgresql://u@db.example/y"}, ["APP_CONFIG -> db.example"]),
        ({"APP_CONFIG": '{"db": "postgresql:\\/\\/u@db.example\\/x"}'}, ["APP_CONFIG -> db.example"]),
        # The key=value form only in a variable named like a database setting: elsewhere
        # "service=api" or "host=0.0.0.0 port=8000" is something else.
        ({"SERVICE_SPEC": "service=api", "APP_ARGS": "host=0.0.0.0 port=8000"}, []),
        ({"ANALYTICS_DATABASE_URL": "host=db.example dbname=x", "MY_POSTGRES_OPTS": "service=api"},
         ["ANALYTICS_DATABASE_URL -> db.example", "MY_POSTGRES_OPTS -> service=api (a service file can name any host)"]),
    ],
)
def test_a_postgres_url_is_judged_by_every_host_it_names(env, problems):
    assert core.postgres_env_problems(env) == problems


def test_a_postgres_url_off_this_machine_stops_the_session(tmp_path, proxy_listener):
    source = "def test_never_runs():\n    pass\n"
    url = "postgresql://u@203.0.113.27:5432/x"
    proc, outcomes = _run_inner(tmp_path / "ci", source, proxy_listener, DATABASE_URL=url, CI="true", TEST_ALLOW_NETWORK="1")
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "DATABASE_URL -> 203.0.113.27" in proc.stderr
    assert outcomes == {}
    # Set by the test module at import: caught after collection.
    late_source = f"import os\nos.environ['PIVOTA_TEST_PG_URL'] = {url!r}\n\n" + source
    proc, outcomes = _run_inner(tmp_path / "import", late_source, proxy_listener, CI="true", TEST_ALLOW_NETWORK=None)
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "PIVOTA_TEST_PG_URL -> 203.0.113.27" in proc.stderr
    # The local opt-out lets a debugging run reach it.
    proc, outcomes = _run_inner(tmp_path / "local", source, proxy_listener, DATABASE_URL=url, CI=None, TEST_ALLOW_NETWORK="1")
    assert outcomes == {"test_never_runs": None}, proc.stdout[-2000:] + proc.stderr[-2000:]


def test_this_run_is_guarded():
    """The guard is live in the real suite, not only in the inner sessions above."""
    assert core.installed() or core.network_allowed()
    if core.installed():
        originals = core._state()["originals"]
        real = originals["getaddrinfo"]

        def must_not_resolve(host, *args, **kwargs):
            raise AssertionError(f"{host!r} reached the real resolver")

        originals["getaddrinfo"] = must_not_resolve
        try:
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo("this-run-is-guarded.example", 443)
        finally:
            originals["getaddrinfo"] = real
        # That refusal was deliberate: take it back so this test does not fail itself.
        taken = core.take(os.environ.get(core.ORIGIN_ENV))
        assert [v["target"] for v in taken] == ["this-run-is-guarded.example"]
