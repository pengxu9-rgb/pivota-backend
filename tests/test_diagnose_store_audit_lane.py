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
    """SELECTs only. A diagnostic that mutates production is not a diagnostic.

    Checked against the AST, not the text: an earlier version of this test
    scanned lines and tripped on its own docstring saying "no INSERT, UPDATE,
    DELETE" — a test that fails on prose while passing real writes is worse
    than none.
    """
    import ast

    tree = ast.parse(open("scripts/diagnose_store_audit_lane.py").read())

    # No call to .execute(...) anywhere — the only write door in this codebase's
    # `databases` usage.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name != "execute", f"execute() call at line {node.lineno}"

    # Every SQL-shaped string literal starts with SELECT or WITH.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip().lower()
            if any(v in text for v in ("insert into", "update ", "delete from",
                                       "drop ", "alter ", "create table")):
                assert text.startswith(("select", "with")), (
                    f"write-shaped SQL at line {node.lineno}"
                )


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
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): raise socket.timeout("timed out")
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
    class _Res:
        status = 301
        def read(self, n): return b""
        def getheader(self, k): return "https://elsewhere.example/.well-known/ucp"

    class _Conn:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return _Res()
        def close(self): pass

    monkeypatch.setattr(mod.http.client, "HTTPSConnection", _Conn)
    out = mod._request("shop.example", "GET", "/.well-known/ucp")
    assert out["status"] == 301
    # The Location is kept as diagnosis but scrubbed of the URL itself.
    assert "elsewhere.example" not in str(out["location"])


def test_dns_reports_whether_every_address_is_public(mod, monkeypatch):
    """A non-public resolution is refused by the client, and that refusal throws
    — arriving as the same `profile_unreachable` a timeout does. Only the
    addresses separate them."""
    monkeypatch.setattr(mod.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("23.227.38.65", 443))])
    assert mod._dns("shop.example")["addresses"] == [
        {"addr": "23.227.38.65", "public": True}
    ]

    monkeypatch.setattr(mod.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("10.25.0.2", 443))])
    assert mod._dns("shop.example")["addresses"] == [
        {"addr": "10.25.0.2", "public": False}
    ]


def test_a_dns_failure_is_reported_not_swallowed(mod, monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(mod.socket, "getaddrinfo", boom)
    assert "gaierror" in mod._dns("nope.example")["error"]
