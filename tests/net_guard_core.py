"""Loopback-only network guard for the backend pytest suites (tests/ and readiness/tests/).

WHY. On 2026-10-30 a full sweep under a hand-rolled version of this guard refused outbound
connects and DNS queries from eleven test files: real merchant storefronts, platform APIs, an
LLM API, and `.example` fixture hosts that only "fail" because DNS says no. Every one of those
tests was green, because the code under test swallows a failed fetch and falls back. So "the
suite passes" said nothing about whether it reached the internet, and a test that reaches the
internet depends on a third party's uptime and content, can trip a merchant's rate limits, and
for our own hosts can write to production. The PIVOTA-Agent gateway has had the same guard for
jest since gw#2519 (tests/_support/netGuard.cjs); this is its pytest twin.

WHAT IT ALLOWS. A socket may go to 127.0.0.0/8, ::1 (or ::ffff:127.x), or a unix socket. A
name is allowed only if it is loopback: `localhost` through the real resolver (and every address
it returns must be loopback), `*.localhost` answered as 127.0.0.1 / ::1 without asking any
resolver (RFC 6761 reserves the zone). An IP LITERAL passes the resolver untouched -- resolving
"8.8.8.8" sends nothing -- and is judged by address when something connects to it. Refused,
and recorded: every other connect, UDP send, and name lookup; and a loopback connect to a local
proxy port (7897, 7890, 1080, 8888), which is how a laptop's system proxy carries traffic out.

WHERE IT HOOKS. socket.socket.connect / connect_ex / sendto (every Python client ends there:
httpx, requests, urllib, aiohttp, asyncpg, redis, dnspython), socket.getaddrinfo and the
gethostby* family, and asyncio's loop.getaddrinfo, so a refused async lookup is recorded on
the task that asked rather than on an anonymous executor thread. Not covered: C extensions
that open their own sockets (none in the suite), and a child process that is not Python or
whose env drops PYTHONPATH.

PROXIES. HTTP(S)_PROXY / ALL_PROXY / NO_PROXY (both cases) are removed for the run, and the
macOS / Windows system-proxy readers in urllib.request return nothing. Unsetting the env is
not enough on a Mac: with no proxy env, urllib.request.getproxies() -- which httpx and requests
both call -- falls back to the system proxy settings.

HOW IT FAILS. Refusing is not failing: the caller catches the error and falls back, which is
exactly how these leaks hid. So every refusal is RECORDED with the test that STARTED the work,
and:
  * the test that started it fails at teardown, listing each target and where it was made;
  * an attempt made after its test finished (a thread or task that outlived it) does NOT fail
    the innocent test running at the time; it fails the session at the end, naming the test
    that started it. So does an attempt made outside any test (import, collection).
Attribution: a ContextVar carries the test's node id into everything it starts (asyncio tasks
copy the context they are created in); threads and executor work items are tagged with the
origin when they are started / submitted; a Python child process gets it through the env and
appends its own refusals to a run directory the parent reads back.

OPT-OUT. TEST_ALLOW_NETWORK=1 disables the guard (and leaves the proxy settings alone) for a
local debugging run. It is IGNORED when CI is set: CI must never reach the internet.
"""

from __future__ import annotations

import contextvars
import errno
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

ALLOW_ENV = "TEST_ALLOW_NETWORK"
RUN_DIR_ENV = "NETGUARD_RUN_DIR"
ORIGIN_ENV = "NETGUARD_ORIGIN"
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
#: Local proxy ports (Clash/mihomo, Surge, SOCKS, mitmproxy-style). A connect to one of these on
#: loopback is a connect to the internet.
PROXY_PORTS = frozenset({7897, 7890, 1080, 8888})

_CHILD_SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_netguard_child")

#: State lives on the socket module, not on this module, so a second copy of this file (imported
#: under another name) shares it and installing twice is a no-op.
_STATE_ATTR = "_pivota_net_guard_state"

_ORIGIN: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("pivota_net_guard_origin", default=None)
_TLS = threading.local()


class NetworkBlocked(ConnectionRefusedError):
    """What a refused connect raises: a ConnectionRefusedError, so callers take the same path
    they would for a host that is down, and the refusal is recorded either way."""


def _state() -> Dict[str, Any]:
    s = getattr(socket, _STATE_ATTR, None)
    if s is None:
        s = {
            "installed": False,
            "child": False,
            "violations": [],
            "current": None,  # node id of the test running right now, if any
            "run_dir": None,
            "run_dir_owned": False,
            "run_log_seen": set(),
            "originals": {},
            "seq": 0,
            "lock": threading.Lock(),
        }
        setattr(socket, _STATE_ATTR, s)
    return s


def _ci() -> bool:
    return str(os.environ.get("CI") or "").strip().lower() in ("1", "true", "yes")


def network_allowed() -> bool:
    return os.environ.get(ALLOW_ENV) == "1" and not _ci()


# ---------------------------------------------------------------------------------------------
# What counts as loopback.
# ---------------------------------------------------------------------------------------------


def _strip(host: Any) -> str:
    h = host.decode("ascii", "replace") if isinstance(host, (bytes, bytearray)) else str(host)
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h.split("%", 1)[0]


def _ip(host: Any) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(_strip(host))
    except ValueError:
        return None


def is_loopback_ip(host: Any) -> bool:
    ip = _ip(host)
    if ip is None:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4:
        return ip in ipaddress.ip_network("127.0.0.0/8")
    return ip == ipaddress.ip_address("::1")


def _name(host: Any) -> str:
    return _strip(host).lower().rstrip(".")


def is_localhost_name(host: Any) -> bool:
    h = _name(host)
    return h == "localhost" or h.endswith(".localhost")


# ---------------------------------------------------------------------------------------------
# Recording.
# ---------------------------------------------------------------------------------------------


def current_origin() -> Optional[str]:
    """The test whose work this is: the task's context, else the executor work item or thread
    that test started, else (a child process) the env, else the test running right now."""
    origin = _ORIGIN.get()
    if origin:
        return origin
    origin = getattr(_TLS, "origin", None)
    if origin:
        return origin
    origin = getattr(threading.current_thread(), "_netguard_origin", None)
    if origin:
        return origin
    s = _state()
    if s["child"]:
        return os.environ.get(ORIGIN_ENV) or None
    return s["current"]


_STDLIB = os.path.dirname(os.__file__)


def _is_library(filename: str) -> bool:
    return "site-packages" in filename or filename.startswith((_STDLIB, "<frozen"))


def _stack() -> str:
    here = os.path.abspath(__file__)
    frames = [f for f in traceback.extract_stack()[:-3] if os.path.abspath(f.filename) != here]
    own = [f for f in frames if not _is_library(f.filename)]
    # The caller's own frames (the test, the app) say who; the last library frame says how.
    # An attempt on an anonymous thread has none of the first kind, and its report shows that.
    picked = (own[-10:] + [f for f in frames[-1:] if f not in own]) if own else frames[-8:]
    return "".join(traceback.format_list(picked)).rstrip()


def _record(kind: str, target: str) -> Dict[str, Any]:
    s = _state()
    with s["lock"]:
        s["seq"] += 1
        seq = s["seq"]
    entry = {
        "id": f"{os.getpid()}:{seq}",
        "kind": kind,
        "target": target,
        "origin": current_origin(),
        "running": s["current"],
        "thread": threading.current_thread().name,
        "at": time.time(),
        "stack": _stack(),
        "child": " ".join(sys.argv[:3]) if s["child"] else None,
    }
    if s["child"]:
        _append_run_log(entry)
    else:
        with s["lock"]:
            s["violations"].append(entry)
    return entry


def _append_run_log(entry: Dict[str, Any]) -> None:
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_dir:
        return
    try:
        with open(os.path.join(run_dir, f"{os.getpid()}.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # the run is over and its directory is gone; nothing is left to fail


def _read_run_log() -> List[Dict[str, Any]]:
    s = _state()
    run_dir = s["run_dir"]
    out: List[Dict[str, Any]] = []
    if not run_dir or not os.path.isdir(run_dir):
        return out
    for name in sorted(os.listdir(run_dir)):
        try:
            with open(os.path.join(run_dir, name), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("id") in s["run_log_seen"]:
                continue
            s["run_log_seen"].add(entry.get("id"))
            out.append(entry)
    return out


def _collect() -> None:
    """Fold child processes' refusals into this process's record."""
    entries = _read_run_log()
    if entries:
        s = _state()
        with s["lock"]:
            s["violations"].extend(entries)


def take(origin: Optional[str]) -> List[Dict[str, Any]]:
    """Remove and return the recorded attempts that `origin` started."""
    _collect()
    s = _state()
    with s["lock"]:
        mine = [v for v in s["violations"] if v["origin"] == origin]
        s["violations"] = [v for v in s["violations"] if v["origin"] != origin]
    return mine


def take_all() -> List[Dict[str, Any]]:
    _collect()
    s = _state()
    with s["lock"]:
        out, s["violations"] = s["violations"], []
    return out


def format_violations(entries: List[Dict[str, Any]], *, header: str) -> str:
    lines = [
        header,
        "Tests may only reach 127.0.0.0/8, ::1 and unix sockets. Script the call instead: monkeypatch "
        "the client or helper, use an httpx.MockTransport, or point it at a loopback server (see "
        "tests/net_guard.py).",
    ]
    for v in entries:
        where = f"  [in child process: {v['child']}]" if v.get("child") else ""
        late = ""
        if v.get("origin") and v.get("running") != v.get("origin"):
            late = f"  [fired while {v.get('running') or 'no test'} was running]"
        lines.append(f"- {v['kind']} {v['target']}  (thread {v.get('thread')}){where}{late}")
        if v.get("stack"):
            lines.extend("    " + l for l in v["stack"].splitlines())
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# The patches.
# ---------------------------------------------------------------------------------------------


def _refuse_connect(target: str) -> NetworkBlocked:
    _record("connect", target)
    return NetworkBlocked(errno.ECONNREFUSED, f"[net_guard] blocked connect to {target}: tests may only reach loopback")


def _refuse_dns(name: str, kind: str = "dns") -> socket.gaierror:
    _record(kind, name)
    return socket.gaierror(socket.EAI_NONAME, f"[net_guard] blocked {kind} lookup of {name!r}: tests may only reach loopback")


def _connect_target(sock: socket.socket, address: Any) -> Optional[str]:
    """None if the connect may proceed, else the target to report."""
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None  # AF_UNIX and friends never leave the machine
    try:
        host, port = address[0], int(address[1])
    except (TypeError, ValueError, IndexError):
        return None  # let the real connect raise its own TypeError
    target = f"{_strip(host)}:{port}"
    if _ip(host) is None:
        # socket.connect(("name", port)) resolves in C, past socket.getaddrinfo.
        return None if is_localhost_name(host) and port not in PROXY_PORTS else target
    if is_loopback_ip(host) and port not in PROXY_PORTS:
        return None
    return target + ("  (a local proxy port)" if is_loopback_ip(host) else "")


def _resolve_target(host: Any) -> Optional[str]:
    """None if resolving `host` is allowed through the real resolver, else the name to report.
    `*.localhost` is handled by the caller."""
    if host is None:
        return None  # a passive (bind) lookup
    if isinstance(host, str):
        # What the real resolver does first, before any query: a name IDNA cannot encode (a
        # 64-character label) raises UnicodeError here and never reaches the network. Refusing
        # it instead would turn that error into a gaierror and hide it from the code under test.
        host.encode("idna")
    if _ip(host) is not None:
        return None  # a literal never leaves the process
    if _name(host) == "localhost":
        return None
    return _strip(host)


def _check_loopback_results(name: Any, results: List[Any]) -> None:
    outside = [r[4][0] for r in results if not is_loopback_ip(r[4][0])]
    if outside:
        raise _refuse_dns(f"{_strip(name)} (resolved to {outside[0]})")


def _install_patches() -> None:
    s = _state()
    o = s["originals"]

    o["connect"] = socket.socket.connect
    o["connect_ex"] = socket.socket.connect_ex
    o["sendto"] = socket.socket.sendto
    o["getaddrinfo"] = socket.getaddrinfo
    o["gethostbyname"] = socket.gethostbyname
    o["gethostbyname_ex"] = socket.gethostbyname_ex
    o["gethostbyaddr"] = socket.gethostbyaddr

    def connect(self, address):
        target = _connect_target(self, address)
        if target is None:
            return o["connect"](self, address)
        raise _refuse_connect(target)

    def connect_ex(self, address):
        target = _connect_target(self, address)
        if target is None:
            return o["connect_ex"](self, address)
        _refuse_connect(target)
        return errno.ECONNREFUSED

    def sendto(self, data, *args):
        address = args[-1] if args else None
        target = _connect_target(self, address) if address is not None else None
        if target is None:
            return o["sendto"](self, data, *args)
        raise _refuse_connect(target + " (udp)")

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host is not None and _ip(host) is None and is_localhost_name(host) and _name(host) != "localhost":
            v6 = family == socket.AF_INET6
            return o["getaddrinfo"]("::1" if v6 else "127.0.0.1", port, family, type, proto, flags)
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        results = o["getaddrinfo"](host, port, family, type, proto, flags)
        if host is not None and _ip(host) is None:
            _check_loopback_results(host, results)
        return results

    def gethostbyname(host):
        if is_localhost_name(host) and _name(host) != "localhost":
            return "127.0.0.1"
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        out = o["gethostbyname"](host)
        if _ip(host) is None and not is_loopback_ip(out):
            raise _refuse_dns(f"{_strip(host)} (resolved to {out})")
        return out

    def gethostbyname_ex(host):
        target = _resolve_target(host)
        if target is not None and not is_localhost_name(host):
            raise _refuse_dns(target)
        return o["gethostbyname_ex"](host)

    def gethostbyaddr(host):
        # A reverse lookup of anything but loopback is a DNS query.
        if is_loopback_ip(host) or _name(host) == "localhost":
            return o["gethostbyaddr"](host)
        raise _refuse_dns(_strip(host), kind="reverse-dns")

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr

    # asyncio resolves on an executor thread, which has neither the task's context nor its
    # test. Refuse here, on the task, so the attempt is recorded against the test that awaited it.
    import asyncio.base_events as base_events

    o["loop_getaddrinfo"] = base_events.BaseEventLoop.getaddrinfo

    async def loop_getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        if not (host is not None and _ip(host) is None and is_localhost_name(host)):
            target = _resolve_target(host)
            if target is not None:
                raise _refuse_dns(target)
        return await o["loop_getaddrinfo"](self, host, port, family=family, type=type, proto=proto, flags=flags)

    base_events.BaseEventLoop.getaddrinfo = loop_getaddrinfo

    # Carry the starting test into threads and executor work items (contextvars do not cross
    # into either on Python 3.11). Tagging only: the code under test sees no difference.
    o["thread_start"] = threading.Thread.start

    def thread_start(self):
        if getattr(self, "_netguard_origin", None) is None:
            self._netguard_origin = current_origin()
        return o["thread_start"](self)

    threading.Thread.start = thread_start

    import concurrent.futures.thread as cf_thread

    o["executor_submit"] = cf_thread.ThreadPoolExecutor.submit

    def executor_submit(self, fn, /, *args, **kwargs):
        origin = current_origin()

        def tagged(*a, **k):
            prev = getattr(_TLS, "origin", None)
            _TLS.origin = origin
            try:
                return fn(*a, **k)
            finally:
                _TLS.origin = prev

        return o["executor_submit"](self, tagged, *args, **kwargs)

    cf_thread.ThreadPoolExecutor.submit = executor_submit


def _disable_proxies() -> None:
    for name in PROXY_ENV:
        os.environ.pop(name, None)
        os.environ.pop(name.lower(), None)
    import urllib.request

    # urllib.request.getproxies() = env proxies OR these. Both look them up as module globals at
    # call time, so every client that calls getproxies() (httpx, requests) gets nothing.
    for reader in ("getproxies_macosx_sysconf", "getproxies_registry"):
        if hasattr(urllib.request, reader):
            setattr(urllib.request, reader, lambda: {})
    for bypass in ("proxy_bypass_macosx_sysconf", "proxy_bypass_registry"):
        if hasattr(urllib.request, bypass):
            setattr(urllib.request, bypass, lambda host: False)


def install() -> bool:
    """Install the guard in the pytest process. Idempotent; False under the opt-out."""
    s = _state()
    if s["installed"]:
        return True
    if network_allowed():
        return False
    _disable_proxies()
    _install_patches()
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_dir or not os.path.isdir(run_dir):
        run_dir = tempfile.mkdtemp(prefix="netguard-")
        s["run_dir_owned"] = True
        os.environ[RUN_DIR_ENV] = run_dir
    s["run_dir"] = run_dir
    # Python children load tests/_netguard_child/sitecustomize.py, which installs the child half.
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != _CHILD_SITE_DIR]
    os.environ["PYTHONPATH"] = os.pathsep.join([_CHILD_SITE_DIR, *parts])
    s["installed"] = True
    return True


def install_in_child() -> bool:
    """Installed by tests/_netguard_child/sitecustomize.py in a Python child of a guarded run."""
    s = _state()
    if s["installed"] or not os.environ.get(RUN_DIR_ENV) or network_allowed():
        return False
    s["child"] = True
    _disable_proxies()
    _install_patches()
    s["installed"] = True
    return True


def installed() -> bool:
    return bool(_state()["installed"])
