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
resolver (RFC 6761 reserves the zone). A name handed straight to connect() -- which resolves it
in C, past every hook here -- goes on only if it is a localhost name, and then as the loopback
literal. An IP LITERAL passes the resolver untouched -- resolving "8.8.8.8" sends nothing --
and is judged by address when something connects to it; a string that only looks like one
("[127.0.0.1]", "127.0.0.1%x") is a name. Refused, and recorded: every other connect, UDP
send, and forward or reverse lookup; and a loopback connect to a local proxy port (7897, 7890,
1080, 8888), which is how a laptop's system proxy carries traffic out.

WHERE IT HOOKS. socket.socket.connect / connect_ex / sendto / sendmsg (every Python client ends
there: httpx, requests, urllib, aiohttp, asyncpg, redis, dnspython), socket.getaddrinfo, the
gethostby* family and getnameinfo, and asyncio's loop.getaddrinfo, so a refused async lookup is
recorded on the task that asked rather than on an anonymous executor thread. It also checks
socket.create_connection and asyncio's loop.sock_connect, which end in the hooks above anyway,
so that a guard installed before this one (a sitecustomize safety net) cannot refuse first and
leave the attempt unrecorded. Python children are covered too (see below).

NOT COVERED. Sockets opened in C: libpq, which psycopg2 (the sync engine, the *_postgres
tests) connects through. tests/net_guard.py stops the session before any test runs unless
every Postgres URL in the env (in any variable), every libpq key=value string (in a variable
named like a database setting), and PGHOST / PGHOSTADDR is loopback or a unix socket. Also not covered: the
`_socket` module used directly, a child process that is not Python, or one whose env or flags
drop PYTHONPATH (`-I`, `-E`, an env that replaces it).

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
    that started it. So does an attempt from work started outside any test (import,
    collection), and from a thread this guard did not see start (a raw `_thread`).
Attribution: a ContextVar carries the test's node id into everything it starts (asyncio tasks
copy the context they are created in); threads and executor work items are tagged with the
origin when they are started / submitted; a Python child process (spawned, or forked) gets it
through the env and appends its own refusals to a run directory the parent reads back.

OPT-OUT. TEST_ALLOW_NETWORK=1 disables the guard (and leaves the proxy settings alone) for a
local debugging run. It is IGNORED when CI is set: CI must never reach the internet.
"""

from __future__ import annotations

import contextvars
import errno
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

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
            "process": uuid.uuid4().hex[:12],  # with the pid, names this process's run log
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


def _text(host: Any) -> str:
    return host.decode("ascii", "replace") if isinstance(host, (bytes, bytearray)) else str(host)


def _ip(host: Any) -> Optional[ipaddress._BaseAddress]:
    # Exactly what the C resolver takes as a literal: "fe80::1%lo0" is one, "[::1]" and
    # "127.0.0.1%x" are names it would look up.
    try:
        return ipaddress.ip_address(_text(host))
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
    return ip.is_loopback  # ::1, with or without a scope


def _name(host: Any) -> str:
    return _text(host).lower().rstrip(".")


def is_localhost_name(host: Any) -> bool:
    h = _name(host)
    return h == "localhost" or h.endswith(".localhost")


def _answered_here(host: Any) -> bool:
    """A localhost name the guard answers itself, as the loopback literal, without asking any
    resolver: `*.localhost`, and every spelling of localhost but the plain one ("LOCALHOST",
    "localhost.") -- a trailing dot makes it a fully qualified name a resolver may send to DNS.
    Plain `localhost` goes to the real resolver, which answers it from the hosts file, and its
    every answer is checked."""
    return host is not None and _ip(host) is None and is_localhost_name(host) and _text(host) != "localhost"


# ---------------------------------------------------------------------------------------------
# Recording.
# ---------------------------------------------------------------------------------------------


def current_origin() -> Optional[str]:
    """The test whose work this is: the task's context, else the executor work item or thread
    that test started, else (a child process) the env, else -- on the main thread only -- the
    test running right now. A thread started outside any test, or one this guard never saw
    start, belongs to no test: blaming whichever test happens to be running would be a guess."""
    origin = _ORIGIN.get()
    if origin:
        return origin
    origin = getattr(_TLS, "origin", None)
    if origin:
        return origin
    thread = threading.current_thread()
    origin = getattr(thread, "_netguard_origin", None)
    if origin:
        return origin
    s = _state()
    if s["child"]:
        return os.environ.get(ORIGIN_ENV) or None
    if thread is threading.main_thread():
        return s["current"]
    return None


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
        "id": f"{os.getpid()}:{s['process']}:{seq}",
        "kind": kind,
        "target": target,
        "origin": current_origin(),
        # A child cannot know which test the parent is running; its entries say so (None).
        "running": None if s["child"] else s["current"],
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
    name = f"{os.getpid()}-{_state()['process']}.jsonl"  # a reused pid gets a file of its own
    try:
        with open(os.path.join(run_dir, name), "a", encoding="utf-8") as fh:
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
        if not v.get("child") and v.get("running") and v.get("running") != v.get("origin"):
            late = f"  [fired while {v['running']} was running]"
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


def _vet(family: int, address: Any) -> Tuple[Optional[str], Any]:
    """(None, the address to use) if a connect may proceed, else (the target to report, None)."""
    if family not in (socket.AF_INET, socket.AF_INET6):
        return None, address  # AF_UNIX and friends never leave the machine
    try:
        host, port = address[0], int(address[1])
    except (TypeError, ValueError, IndexError):
        return None, address  # let the real call raise its own error
    target = f"{_text(host)}:{port}"
    if _ip(host) is None:
        if not is_localhost_name(host):
            return target, None
        # connect(("name", port)) resolves in C, past socket.getaddrinfo: send the literal.
        host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        address = (host, *tuple(address)[1:])
    if not is_loopback_ip(host):
        return target, None
    if port in PROXY_PORTS:
        return target + "  (a local proxy port)", None
    return None, address


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
    return _text(host)


def _check_loopback_results(name: Any, addresses: List[Any]) -> None:
    outside = [a for a in addresses if not is_loopback_ip(a)]
    if outside:
        raise _refuse_dns(f"{_text(name)} (resolved to {outside[0]})")


def _precheck(family: int, address: Any) -> Any:
    """For an entry point that resolves and connects by itself (socket.create_connection,
    loop.sock_connect): refuse, here, what the guarded getaddrinfo / connect underneath would,
    so no hook installed before this guard gets the attempt first. Returns the address to use."""
    try:
        host, port = address[0], int(address[1])
    except (TypeError, ValueError, IndexError):
        return address
    if _ip(host) is None and not is_localhost_name(host):
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        return address
    target, vetted = _vet(family, address)
    if target is not None:
        raise _refuse_connect(target)
    if _text(host) == "localhost":
        return address  # resolved -- and every answer checked -- by the guarded getaddrinfo
    return vetted


def _install_patches() -> None:
    s = _state()
    o = s["originals"]

    o["connect"] = socket.socket.connect
    o["connect_ex"] = socket.socket.connect_ex
    o["sendto"] = socket.socket.sendto
    o["sendmsg"] = getattr(socket.socket, "sendmsg", None)
    o["create_connection"] = socket.create_connection
    o["getaddrinfo"] = socket.getaddrinfo
    o["gethostbyname"] = socket.gethostbyname
    o["gethostbyname_ex"] = socket.gethostbyname_ex
    o["gethostbyaddr"] = socket.gethostbyaddr
    o["getnameinfo"] = socket.getnameinfo

    def connect(self, address):
        target, address = _vet(self.family, address)
        if target is None:
            return o["connect"](self, address)
        raise _refuse_connect(target)

    def connect_ex(self, address):
        target, address = _vet(self.family, address)
        if target is None:
            return o["connect_ex"](self, address)
        _refuse_connect(target)
        return errno.ECONNREFUSED

    def sendto(self, data, *args):
        # sendto(data, address) or sendto(data, flags, address)
        if args:
            target, address = _vet(self.family, args[-1])
            if target is not None:
                raise _refuse_connect(target + " (udp)")
            args = (*args[:-1], address)
        return o["sendto"](self, data, *args)

    def sendmsg(self, buffers, *args):
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        if len(args) >= 3 and args[2] is not None:
            target, address = _vet(self.family, args[2])
            if target is not None:
                raise _refuse_connect(target + " (sendmsg)")
            args = (*args[:2], address, *args[3:])
        return o["sendmsg"](self, buffers, *args)

    def create_connection(address, *args, **kwargs):
        ip = _ip(address[0]) if isinstance(address, (tuple, list)) and address else None
        family = socket.AF_INET6 if ip is not None and ip.version == 6 else socket.AF_INET
        return o["create_connection"](_precheck(family, address), *args, **kwargs)

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if _answered_here(host):
            v6 = family == socket.AF_INET6
            return o["getaddrinfo"]("::1" if v6 else "127.0.0.1", port, family, type, proto, flags)
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        results = o["getaddrinfo"](host, port, family, type, proto, flags)
        if host is not None and _ip(host) is None:
            _check_loopback_results(host, [r[4][0] for r in results])
        return results

    def gethostbyname(host):
        if _answered_here(host):
            return "127.0.0.1"
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        out = o["gethostbyname"](host)
        if _ip(host) is None:
            _check_loopback_results(host, [out])
        return out

    def gethostbyname_ex(host):
        if _answered_here(host):
            return (_text(host), [], ["127.0.0.1"])
        target = _resolve_target(host)
        if target is not None:
            raise _refuse_dns(target)
        out = o["gethostbyname_ex"](host)
        if _ip(host) is None:
            _check_loopback_results(host, out[2])
        return out

    def gethostbyaddr(host):
        # A reverse lookup of anything but loopback is a DNS query.
        if is_loopback_ip(host) or _name(host) == "localhost":
            return o["gethostbyaddr"](host)
        raise _refuse_dns(_text(host), kind="reverse-dns")

    def getnameinfo(sockaddr, flags):
        # Without NI_NUMERICHOST this is a reverse lookup of the address.
        if flags & socket.NI_NUMERICHOST or is_loopback_ip(sockaddr[0]):
            return o["getnameinfo"](sockaddr, flags)
        raise _refuse_dns(_text(sockaddr[0]), kind="reverse-dns")

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    if o["sendmsg"] is not None:
        socket.socket.sendmsg = sendmsg
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr
    socket.getnameinfo = getnameinfo

    # asyncio resolves on an executor thread, which has neither the task's context nor its
    # test. Refuse here, on the task, so the attempt is recorded against the test that awaited it.
    import asyncio.base_events as base_events
    import asyncio.selector_events as selector_events

    o["loop_getaddrinfo"] = base_events.BaseEventLoop.getaddrinfo
    o["sock_connect"] = selector_events.BaseSelectorEventLoop.sock_connect

    async def loop_getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        if not (host is not None and _ip(host) is None and is_localhost_name(host)):
            target = _resolve_target(host)
            if target is not None:
                raise _refuse_dns(target)
        return await o["loop_getaddrinfo"](self, host, port, family=family, type=type, proto=proto, flags=flags)

    async def sock_connect(self, sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            address = _precheck(sock.family, address)
        return await o["sock_connect"](self, sock, address)

    base_events.BaseEventLoop.getaddrinfo = loop_getaddrinfo
    selector_events.BaseSelectorEventLoop.sock_connect = sock_connect

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

    # A forked child (os.fork, multiprocessing's fork start method) keeps these patches but not
    # the parent's memory: it reports through the run directory, like a spawned child.
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork)


def _after_fork() -> None:
    s = _state()
    if not s["installed"]:
        return
    s["lock"] = threading.Lock()  # the parent's may have been held by another thread
    s["process"] = uuid.uuid4().hex[:12]
    s["seq"] = 0
    if not s["child"]:
        s["child"] = True
        s["violations"] = []


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


# ---------------------------------------------------------------------------------------------
# Postgres URLs: libpq connects in C, past every hook above.
# ---------------------------------------------------------------------------------------------

#: A Postgres URL anywhere in a value (on its own, or inside JSON or a longer string, where
#: the slashes may be escaped).
_PG_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?:(?://|\\/\\/)[^\s\"'<>;]+", re.IGNORECASE)
#: libpq's key=value form: `key = value`, spaces optional, a value optionally single-quoted.
_CONNINFO_PAIR = re.compile(r"\s*([A-Za-z_]+)\s*=\s*('(?:[^'\\]|\\.)*'|[^\s']*)")
_CONNINFO_KEYS = frozenset(
    """host hostaddr port dbname user password passfile channel_binding connect_timeout
    client_encoding options application_name fallback_application_name keepalives
    keepalives_idle keepalives_interval keepalives_count tcp_user_timeout replication gssencmode
    sslmode requiressl sslcompression sslcert sslkey sslpassword sslcertmode sslrootcert sslcrl
    sslcrldir sslsni requirepeer require_auth ssl_min_protocol_version ssl_max_protocol_version
    krbsrvname gsslib gssdelegation service target_session_attrs load_balance_hosts""".split()
)


def _service(name: str) -> str:
    # A pg_service.conf entry can name any host; this guard does not read service files.
    return f"service={name} (a service file can name any host)"


def _url_hosts(url: str) -> List[str]:
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
    except ValueError:
        return [f"{url.split('@')[-1][:40]!r} (unparseable)"]
    hosts = []
    for item in parts.netloc.rsplit("@", 1)[-1].split(","):
        item = unquote(item)
        if item.startswith("["):
            hosts.append(item[1:].split("]", 1)[0])
        else:
            hosts.append(item.rsplit(":", 1)[0] if item.count(":") == 1 else item)
    for key in ("host", "hostaddr"):
        for v in query.get(key, []):
            hosts.extend(v.split(","))
    hosts.extend(_service(v) for v in query.get("service", []))
    return hosts


def _conninfo_hosts(value: str) -> Optional[List[str]]:
    """The hosts of a libpq key=value string, or None if `value` is not one."""
    pairs, pos = [], 0
    while value[pos:].strip():
        m = _CONNINFO_PAIR.match(value, pos)
        if m is None or m.end() == pos:
            return None
        key, v = m.group(1).lower(), m.group(2)
        if key not in _CONNINFO_KEYS:
            return None
        if v.startswith("'"):
            v = re.sub(r"\\(.)", r"\1", v[1:-1])
        pairs.append((key, v))
        pos = m.end()
    if not pairs:
        return None
    hosts = []
    for key, v in pairs:
        if key in ("host", "hostaddr"):
            hosts.extend(v.split(","))
        elif key == "service":
            hosts.append(_service(v))
    return hosts


def _names_a_database(key: str) -> bool:
    """A variable whose value may be a libpq key=value string. Elsewhere `service=api` or
    `host=0.0.0.0 port=8000` means something else; a URL counts in any variable."""
    k = key.upper()
    return k.startswith("PG") or "POSTGRES" in k or "DATABASE" in k or k.endswith("DSN") or "_PG_" in k or k.endswith("_PG")


def _pg_host_ok(host: str) -> bool:
    # Only `localhost` itself: libpq resolves any other name, `*.localhost` included, through
    # the system resolver, which may ask DNS.
    host = host.strip()
    return host == "" or host.startswith("/") or host.lower() == "localhost" or is_loopback_ip(host)


def postgres_env_problems(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """Each env var that would point libpq (or asyncpg) off this machine."""
    env = os.environ if environ is None else environ
    problems = []
    for key in sorted(env):
        value = env[key]
        if key in ("PGHOST", "PGHOSTADDR"):
            hosts: List[str] = value.split(",")
        elif key == "PGSERVICE":
            hosts = [_service(value)]
        else:
            urls = [url.replace("\\/", "/") for url in _PG_URL.findall(value)]
            if urls:
                hosts = [h for url in urls for h in _url_hosts(url)]
            elif _names_a_database(key):
                hosts = _conninfo_hosts(value) or []
            else:
                continue
        bad = [h for h in hosts if not _pg_host_ok(h)]
        if bad:
            problems.append(f"{key} -> {', '.join(bad)}")
    return problems


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
    # Python children load tests/_netguard_child/sitecustomize.py, which installs the child half
    # and then runs the sitecustomize it shadows.
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and os.path.realpath(p) != os.path.realpath(_CHILD_SITE_DIR)]
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
