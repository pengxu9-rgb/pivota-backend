"""The pure parts of the lane diagnostic script.

The script's value is that it distinguishes WHY a fetch failed — a thrown
exception (which is what `profile_unreachable` means) from an HTTP status. If
its error handling collapsed those, it would reproduce the ambiguity it exists
to resolve.
"""
from __future__ import annotations

import importlib.util
import socket

import pytest


def _load():
    """Load the script's definitions without running main() or touching the DB."""
    src = open("scripts/diagnose_store_audit_lane.py").read()
    src = src.replace("asyncio.run(main())", "")
    src = src.replace("from db.database import database", "database = None")
    spec = importlib.util.spec_from_loader("lane_diag", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src, "lane_diag", "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_it_issues_no_tools_call(mod):
    """tools/call is a GRANT to the merchant. A read-only diagnostic must never
    reach for it, and the only mention in the file is the comment saying so."""
    src = open("scripts/diagnose_store_audit_lane.py").read()
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "tools/call" not in code
    assert "tools/list" in code


def test_it_writes_nothing(mod):
    """SELECTs only — and the ratchet has to see what `databases` actually allows.

    An earlier version exempted any literal starting with SELECT or WITH and
    matched only the attribute name `execute`. Four real write shapes walked
    straight through it: a data-modifying CTE (`WITH x AS (DELETE ... RETURNING
    ...)`, the same shape this script's own COVERAGE query uses), TRUNCATE,
    `execute_many`, and a verb split across a newline. fetch_all/fetch_one run
    arbitrary SQL on asyncpg, so the runner name is not the boundary — the SQL
    is.
    """
    import ast
    import re

    src = open("scripts/diagnose_store_audit_lane.py").read()
    tree = ast.parse(src)

    # No call whose name can run a statement we did not read.
    RUNNERS = {"execute", "execute_many", "executemany"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in RUNNERS, f"{name}() at line {node.lineno}"

    # Every SQL-shaped literal is scanned for write verbs ANYWHERE in it, not
    # just at the front — a CTE hides them in the middle.
    WRITE = re.compile(
        r"\b(insert\s+into|update|delete\s+from|truncate|drop|alter|"
        r"create\s+(table|index|view)|grant|revoke|copy|merge\s+into)\b",
        re.IGNORECASE | re.DOTALL,
    )
    # Identify docstrings by NODE, not by text: ast.get_docstring() returns the
    # cleaned/dedented string while the Constant holds the raw one, so comparing
    # the two never matches and the module docstring — which names the verbs it
    # forbids — trips the ratchet.
    doc_nodes = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and n.body:
            first = n.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_nodes.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if id(node) in doc_nodes:   # prose may NAME the verbs it forbids
                continue
            hit = WRITE.search(text)
            assert not hit, f"{hit.group(0)!r} in SQL literal at line {node.lineno}"


def test_urls_are_scrubbed_from_free_text(mod):
    out = mod.scrub("redirected to https://shop.example/x?token=abc")
    assert "https://shop.example" not in out and "token=abc" not in out
    assert "redirected to" in out
    assert mod.scrub(None) is None
    assert mod.scrub(7) == 7


def test_a_thrown_request_reports_the_exception_not_a_status(mod, monkeypatch):
    """`profile_unreachable` IS the throw branch, so the exception type is the
    entire diagnosis. Reporting a status here would erase it."""
    class _Boom:
        def __init__(self, *a, **kw): self.sock = None
        def connect(self): raise socket.timeout("timed out")
        def request(self, *a, **kw): raise AssertionError("unreachable")
        def getresponse(self): raise AssertionError("unreachable")
        def close(self): pass

    monkeypatch.setattr(mod.http.client, "HTTPSConnection", _Boom)
    out = mod._request("shop.example", "GET", "/.well-known/ucp")
    assert "status" not in out
    assert out["threw"].startswith("timeout") or "timed out" in out["threw"]
    assert isinstance(out["ms"], int)


def test_a_redirect_is_reported_as_a_status_not_followed(mod, monkeypatch):
    """The client refuses to follow a profile redirect; a diagnostic that
    followed one would report the wrong origin's answer as this domain's."""
    class _Sock:
        def settimeout(self, _t): pass

    class _Res:
        status = 301
        def read(self, *a): return b""
        def getheader(self, k): return "https://elsewhere.example/.well-known/ucp"

    class _Conn:
        def __init__(self, *a, **kw): self.sock = _Sock()
        def connect(self): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return _Res()
        def close(self): pass

    calls = []
    monkeypatch.setattr(
        mod.http.client, "HTTPSConnection",
        lambda *a, **kw: (calls.append(a[0]), _Conn())[1],
    )
    out = mod._request("shop.example", "GET", "/.well-known/ucp")
    assert out["status"] == 301
    # ONE connection. The earlier assertion passed a _request that followed up
    # to five hops, because every hop answered 301 and only the last was read —
    # so it pinned the status and not the "not followed" property it named.
    assert calls == ["shop.example"]
    # Location is kept as diagnosis but scrubbed of the URL itself.
    assert "elsewhere.example" not in str(out["location"])


def test_dns_reports_the_clients_refusal_not_is_global(mod, monkeypatch):
    """The client throws on a forbidden address, and that throw is stored as the
    same `profile_unreachable` a timeout produces — so this must mirror the
    client's list exactly, not ipaddress.is_global."""
    monkeypatch.setattr(mod.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("23.227.38.65", 443))])
    out = mod._dns("shop.example")
    assert out["addresses"] == [{"addr": "23.227.38.65", "refused": False}]
    assert out["client_refuses"] is False

    monkeypatch.setattr(mod.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("10.25.0.2", 443))])
    assert mod._dns("shop.example")["client_refuses"] is True


@pytest.mark.parametrize("addr", [
    "224.0.0.1",            # multicast
    "192.88.99.1",          # 6to4 relay anycast
    "::ffff:1.2.3.4",       # IPv4-mapped
    "64:ff9b::1.2.3.4",     # NAT64
    "not-an-ip",            # unknown family fails CLOSED
])
def test_addresses_is_global_calls_public_are_still_refused(mod, addr):
    """Every one of these returns is_global=True (or is not an IP at all), and
    every one is on the client's refusal list. Using is_global here would print
    a false all-clear for an address the client throws on."""
    assert mod._client_would_refuse(addr) is True


def test_an_ordinary_public_address_is_not_refused(mod):
    """The positive counterpart — a refusal list that refuses everything would
    pass the test above while making the diagnostic useless."""
    assert mod._client_would_refuse("23.227.38.65") is False


def test_a_dns_failure_is_reported_not_swallowed(mod, monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(mod.socket, "getaddrinfo", boom)
    assert "gaierror" in mod._dns("nope.example")["error"]
