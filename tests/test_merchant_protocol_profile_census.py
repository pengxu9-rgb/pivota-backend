"""jobs/merchant_protocol_profile_census: a read-only census of /.well-known/ucp and /.well-known/acp.json.

Every test drives the job's own entry point (`main`) or `run` with an injected httpx transport, an
injected resolver, and an injected clock and sleep. No socket is opened and no name is resolved.
The example documents are the specifications' own (tests/fixtures/merchant_protocol_profile).
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from jobs import merchant_protocol_profile_census as census
from services import agent_card_issuance, crawl_identity

FIXTURES = Path(__file__).parent / "fixtures" / "merchant_protocol_profile"
MARKER = "LEAKMARKER"


def _fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


UCP_0825 = _fixture("ucp_business_profile_2026_08_25.json")
UCP_EMPTY = _fixture("ucp_supported_versions_profile.json")
HANDLER_NT = _fixture("ucp_payment_handler_network_token_2026_08_25.json")
ACP_FULL = _fixture("acp_discovery_full.json")
ACP_MIN = _fixture("acp_discovery_minimal.json")


class _Body(httpx.AsyncByteStream):
    """An unread body, as a real server's is: httpx reads a Response built from `content=` at once."""

    def __init__(self, data: bytes, chunk: int = 8192) -> None:
        self.data, self.chunk, self.chunks = data, chunk, 0

    async def __aiter__(self):
        for start in range(0, len(self.data), self.chunk):
            self.chunks += 1
            yield self.data[start:start + self.chunk]


def _raw(status: int, body: bytes = b"", **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    """A route: builds a fresh, unread response for each request."""
    fixed = {k.replace("_", "-"): v for k, v in headers.items()}
    return lambda request: httpx.Response(status, stream=_Body(body), headers=fixed)


def _json(doc: Any, status: int = 200, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    return _raw(status, json.dumps(doc).encode(), content_type="application/json", **headers)


def _redirect(location: str, status: int = 301, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    return _raw(status, b"", location=location, **headers)


class Net:
    """The fake network: a route table keyed by (host, path), a request log and a resolver log."""

    def __init__(self, routes: Optional[Dict[Any, Any]] = None, private: Optional[set] = None) -> None:
        self.routes = routes or {}
        self.private = private or set()
        self.requests: List[httpx.Request] = []
        self.resolved: List[str] = []
        self.now = 1000.0
        self.sleeps: List[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.url.host, request.url.path), _raw(404))
        if isinstance(answer, BaseException):
            raise answer
        return answer(request)

    def resolver(self, host: str) -> bool:
        self.resolved.append(host)
        return host not in self.private

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def seams(self, **extra: Any) -> Dict[str, Any]:
        return {
            "transport": httpx.MockTransport(self.handler), "resolver": self.resolver,
            "clock": self.clock, "sleep": self.sleep, **extra,
        }  # fmt: skip

    def urls(self) -> List[str]:
        return [str(r.url) for r in self.requests]

    def paths(self, host: str) -> List[str]:
        return [r.url.path for r in self.requests if r.url.host == host]


class Run:
    def __init__(self, code: int, lines: List[str]) -> None:
        self.code = code
        self.lines = lines
        self.rows = [json.loads(x[len(census.ROW_PREFIX):]) for x in lines if x.startswith(census.ROW_PREFIX)]
        sums = [json.loads(x[len(census.SUM_PREFIX):]) for x in lines if x.startswith(census.SUM_PREFIX)]
        self.summary = sums[0] if sums else None

    def row(self, domain: Optional[str]) -> Dict[str, Any]:
        (found,) = [r for r in self.rows if r["d"] == domain]
        return found


def _run(monkeypatch, net: Net, argv: List[str], flag: Optional[str] = "on", **extra: Any) -> Run:
    if flag is None:
        monkeypatch.delenv(census.FLAG, raising=False)
    else:
        monkeypatch.setenv(census.FLAG, flag)
    lines: List[str] = []
    code = census.main(argv, out=lines.append, **net.seams(**extra))
    return Run(code, lines)


def _census(monkeypatch, routes: Dict[Any, Any], domains: str = "shop-a.example", **kw: Any):
    net = Net(routes, private=kw.pop("private", None))
    return net, _run(monkeypatch, net, ["--domains", domains], **kw)


U, A, R = census.UCP_PATH, census.ACP_PATH, census.ROBOTS_PATH
SUMMARY_ZERO = {
    "domains": 0, "ucp_ok": 0, "with_checkout": 0, "with_order": 0, "with_discount": 0,
    "handlers_accepting_network_token": 0, "acp_ok": 0, "errors": 0,
    "robots_ok": 0, "robots_absent": 0, "robots_disallowed": 0, "robots_unavailable": 0, "deadline": 0,
}  # fmt: skip


def _robots(text: str, **headers: str):
    return _raw(200, text.encode(), content_type="text/plain", **headers)


class _FakeDirectoryDb:
    """Stands in for the process database's three calls the directory read makes (the repo's
    pattern: patch the methods of the real `db.database.database` object)."""

    def __init__(self, monkeypatch, tierb: List[Any], purchasability: List[Any], fail: Optional[str] = None) -> None:
        from db.database import database

        self.calls: List[str] = []
        self.rows = {"tierb_cart_link_eligibility": [{"shop_domain": v} for v in tierb],
                     "merchant_purchasability": [{"merchant_domain": v} for v in purchasability]}
        self.fail = fail
        # Another test file in the same process may have left the shared object connected; the
        # directory read connects only when nothing has, so pin the state it starts from.
        monkeypatch.setattr(database, "is_connected", False)
        monkeypatch.setattr(database, "connect", self.connect)
        monkeypatch.setattr(database, "disconnect", self.disconnect)
        monkeypatch.setattr(database, "fetch_all", self.fetch_all)
        for write in ("execute", "execute_many", "fetch_one", "fetch_val", "iterate", "transaction"):
            monkeypatch.setattr(database, write, self._never)

    def _never(self, *a: Any, **k: Any) -> None:
        raise AssertionError("the census made a database call other than its two reads")

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.fail == "connect":
            raise OSError("no database")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")

    async def fetch_all(self, query: Any, values: Any = None) -> List[Dict[str, Any]]:
        assert values is None
        self.calls.append(" ".join(str(query).split()))
        if self.fail == "read":
            raise RuntimeError(f"relation missing {MARKER}")
        (table,) = [t for t in self.rows if t in str(query)]
        return self.rows[table]


# ── the dial ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "0", "off", "false", "no", "maybe"],
                         ids=["unset", "empty", "zero", "off", "false", "no", "unknown-word"])
def test_flag_off_prints_one_line_and_touches_nothing(monkeypatch, tmp_path, value):
    def _no_dns(*a, **k):
        raise AssertionError("a name was resolved with the flag off")

    def _no_open(*a, **k):
        raise AssertionError("a file was opened with the flag off")

    db = _FakeDirectoryDb(monkeypatch, [], [])
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    monkeypatch.setattr("builtins.open", _no_open)
    net = Net({("shop-a.example", U): _json(UCP_0825)})
    assert _run(monkeypatch, net, ["--from-directory"], flag=value).lines == [census.DISABLED_LINE]
    assert db.calls == []
    # Arguments that would be refused, or read a file, with the flag on: none of it is looked at.
    got = _run(monkeypatch, net, ["--domains", "shop-a.example", "--domains-file", str(tmp_path / "x"), "--bogus"],
               flag=value)
    assert got.code == 0
    assert got.lines == [census.DISABLED_LINE]
    assert net.requests == [] and net.resolved == [] and net.sleeps == []


def test_flag_default_is_off(monkeypatch):
    monkeypatch.delenv(census.FLAG, raising=False)
    assert census.FLAG == "MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED"
    assert census.is_enabled() is False
    for on in ("1", "true", "on", "yes", " ON "):
        monkeypatch.setenv(census.FLAG, on)
        assert census.is_enabled() is True


# ── the two example documents ────────────────────────────────────────────────────────────────


def test_the_specs_example_documents_become_the_expected_row(monkeypatch):
    net, got = _census(monkeypatch, {("shop-a.example", U): _json(UCP_0825), ("shop-a.example", A): _json(ACP_FULL)})
    assert got.code == 0
    assert got.rows == [{
        "d": "shop-a.example",
        "robots": "absent",
        "ucp": 200,
        "ver": "2026-08-25",
        "supported": [],
        "svc": [
            {"name": "dev.ucp.shopping", "transport": "rest"},
            {"name": "dev.ucp.shopping", "transport": "mcp"},
            {"name": "dev.ucp.shopping", "transport": "a2a"},
            {"name": "dev.ucp.shopping", "transport": "embedded"},
        ],
        "cap": [
            "dev.ucp.shopping.checkout",
            "dev.ucp.shopping.fulfillment",
            "dev.ucp.shopping.discount",
            "dev.ucp.common.identity_linking",
        ],
        "ph": {"processor_tokenizer": ["card"]},
        "keys": 2,
        "acp": 200,
        "acp_ver": "2026-01-30",
        "acp_cap": ["checkout", "orders", "delegate_payment", "carts", "discount", "fulfillment"],
        "err": None,
    }]
    assert got.summary == {**SUMMARY_ZERO, "domains": 1, "ucp_ok": 1, "with_checkout": 1, "with_discount": 1,
                           "acp_ok": 1, "robots_absent": 1}
    assert got.lines[-1] == census.DONE_LINE
    assert net.urls() == ["https://shop-a.example/robots.txt", "https://shop-a.example/.well-known/ucp",
                          "https://shop-a.example/.well-known/acp.json"]


def test_row_keys_and_their_order_are_the_documented_ones(monkeypatch):
    _, got = _census(monkeypatch, {})
    (line,) = [x for x in got.lines if x.startswith(census.ROW_PREFIX)]
    assert list(json.loads(line[len(census.ROW_PREFIX):])) == [
        "d", "robots", "ucp", "ver", "supported", "svc", "cap", "ph", "keys", "acp", "acp_ver", "acp_cap", "err"]
    assert list(got.summary) == ["domains", "ucp_ok", "with_checkout", "with_order", "with_discount",
                                 "handlers_accepting_network_token", "acp_ok", "errors",
                                 "robots_ok", "robots_absent", "robots_disallowed", "robots_unavailable", "deadline"]
    assert " " not in line[len(census.ROW_PREFIX):]  # compact JSON


def test_minimal_acp_document_and_no_ucp_profile(monkeypatch):
    _, got = _census(monkeypatch, {("shop-a.example", A): _json(ACP_MIN)})
    row = got.row("shop-a.example")
    assert (row["ucp"], row["ver"], row["acp"], row["acp_ver"], row["acp_cap"]) == (
        404, None, 200, "2025-09-29", ["checkout"])
    assert row["err"] is None  # a merchant that publishes nothing is not an error
    assert (got.summary["ucp_ok"], got.summary["acp_ok"], got.summary["errors"]) == (0, 1, 0)


def test_network_token_handler_is_read_and_counted(monkeypatch):
    profile = copy.deepcopy(UCP_0825)
    profile["ucp"]["payment_handlers"]["com.example.second"] = [HANDLER_NT]
    profile["ucp"]["capabilities"]["dev.ucp.shopping.order"] = [{"version": "2026-08-25"}]
    _, got = _census(monkeypatch, {("shop-a.example", U): _json(profile), ("shop-b.example", U): _json(UCP_0825)},
                     domains="shop-a.example,shop-b.example")
    assert got.row("shop-a.example")["ph"] == {
        "processor_tokenizer": ["card"],
        "processor_tokenizer_1234": ["card", "pan", "network_token"],
    }
    assert got.summary["handlers_accepting_network_token"] == 1
    assert (got.summary["with_order"], got.summary["with_checkout"], got.summary["domains"]) == (1, 2, 2)


def test_handler_without_declared_instruments_maps_to_an_empty_list():
    facts = census.parse_ucp_profile({"ucp": {"version": "2026-08-25", "payment_handlers": {
        "com.example.pay": [{"id": "h1"}], "com.example.nameless": [{"config": {"k": MARKER}}]}}})
    assert facts["ph"] == {"h1": [], "com.example.nameless": []}


def test_empty_registries_and_a_version_this_job_has_never_seen(monkeypatch):
    _, got = _census(monkeypatch, {("shop-a.example", U): _json(UCP_EMPTY)})
    row = got.row("shop-a.example")
    assert row == {**census.empty_row("shop-a.example"), "robots": "absent", "ucp": 200, "ver": "2026-01-23",
                   "supported": ["2026-01-11"], "acp": 404}
    assert got.summary["ucp_ok"] == 1 and got.summary["with_checkout"] == 0


def test_the_2026_04_08_shape_is_read_too():
    # The differences the gateway's parser documents for 2026-04-08: signing keys in `signing_keys`
    # beside `ucp`, and `supported_versions` as a plain list.
    profile = copy.deepcopy(UCP_0825)
    profile["signing_keys"] = profile.pop("keys")
    profile["ucp"]["version"] = "2026-04-08"
    profile["ucp"]["supported_versions"] = ["2026-01-23", "2026-04-08", "nonsense", 7]
    facts = census.parse_ucp_profile(json.dumps(profile))
    assert (facts["ok"], facts["ver"], facts["supported"], facts["keys"]) == (True, "2026-04-08", ["2026-01-23"], 2)
    assert "dev.ucp.shopping.checkout" in facts["cap"]
    flat = {"ucp_version": "2026-04-08", "capabilities": ["dev.ucp.shopping.checkout", {"name": "dev.ucp.shopping.order"}],
            "services": [{"name": "dev.ucp.shopping", "transport": "MCP"}],
            "payment_handlers": [{"name": "com.example.pay", "id": "h1", "available_instruments": [{"type": "card"}]}]}
    facts = census.parse_ucp_profile(flat)
    assert facts["cap"] == ["dev.ucp.shopping.checkout", "dev.ucp.shopping.order"]
    assert facts["svc"] == [{"name": "dev.ucp.shopping", "transport": "mcp"}] and facts["ph"] == {"h1": ["card"]}


def test_a_capability_mapped_to_null_or_false_is_not_advertised():
    facts = census.parse_ucp_profile({"ucp": {"version": "2026-08-25", "capabilities": {
        "dev.ucp.shopping.checkout": None, "dev.ucp.shopping.order": False, "dev.ucp.shopping.discount": [],
        "dev.ucp.shopping.fulfillment": [{}], "dev.ucp.common.identity_linking": {}}}})
    assert facts["cap"] == ["dev.ucp.shopping.fulfillment", "dev.ucp.common.identity_linking"]


@pytest.mark.parametrize("doc, reason", [
    ({"ucp": {"version": "v9"}}, "version_invalid"),
    ({"ucp": {"version": "2026-08-25T00:00"}}, "version_invalid"),
    ({"ucp": {}}, "version_missing"),
    ({"ucp": {"version": 20260825}}, "version_missing"),
    ([1, 2], "not_an_object"),
    ("{not json", "not_json"),
    (b"\xff\xfe\x00", "not_json"),
    ("[" * 100_000, "not_json"),
    ("9" * 10_000, "not_json"),
    (" " * (census.MAX_BODY_BYTES + 1), "too_large"),
    (None, "not_an_object"),
], ids=["version-word", "version-timestamp", "version-absent", "version-number", "array", "broken-json",
        "bad-bytes", "deep-nesting", "huge-integer", "oversize-text", "none"])
def test_unreadable_ucp_documents_yield_a_reason(doc, reason):
    assert census.parse_ucp_profile(doc) == {"ok": False, "reason": reason}


NASTY = [
    None, 0, 1.5, True, "", "x", b"", [], {}, {"ucp": []}, {"ucp": {"version": []}}, {"ucp": {"version": {}}},
    {"ucp": {"version": "2026-08-25", "services": "x", "capabilities": 7, "payment_handlers": [1, None, {"id": {}}],
             "supported_versions": 3}, "keys": {}},
    {"ucp": {"version": "2026-08-25", "services": {"a": [1, None, {"transport": 5}, {"transport": "x" * 99}], "b": 2},
             "capabilities": [None, 1, {"name": []}, ["x"]],
             "payment_handlers": {"h": [{"id": "ok", "available_instruments": [1, {"type": []}, {"type": "card", "ucp": {
                 "request_constraints": [3, {"properties": {"credential": {"anyOf": [4, {"properties": {"type": {
                     "const": {}, "enum": [None, "network_token", {}]}}}]}}}]}}]}], "i": "str"}},
     "keys": [1, {"kid": 1, "kty": "EC"}, {"kid": "a", "kty": "EC"}]},
    {"ucp": {"version": "2026-08-25", "capabilities": {str(i): 1 for i in range(5000)},
             "services": {"s": [{"transport": "mcp"}] * 5000}}},
    {"protocol": []}, {"protocol": {"version": "2026-04-17"}, "capabilities": {"services": "x", "extensions": [1, {}, []]}},
    {1: 2},
]


@pytest.mark.parametrize("index", range(len(NASTY)), ids=[f"nasty-{i}" for i in range(len(NASTY))])
def test_neither_parser_raises_on_wrong_types(index):
    for parse in (census.parse_ucp_profile, census.parse_acp_discovery):
        facts = parse(NASTY[index])
        assert isinstance(facts, dict) and isinstance(facts["ok"], bool)
        assert facts["ok"] or isinstance(facts["reason"], str)
        json.dumps(facts)


def test_wrong_types_inside_a_readable_profile_are_skipped_not_fatal():
    facts = census.parse_ucp_profile(NASTY[13])
    assert facts["ok"] and facts["svc"] == [] and facts["cap"] == []
    assert facts["ph"] == {"ok": ["card", "network_token"]} and facts["keys"] == 1
    big = census.parse_ucp_profile(NASTY[14])
    assert len(big["cap"]) == 256 and len(big["svc"]) == 256


def test_a_parser_that_breaks_inside_still_returns_a_reason(monkeypatch):
    def _boom(doc):
        raise RuntimeError("boom")

    monkeypatch.setattr(census, "_read_ucp_profile", _boom)
    monkeypatch.setattr(census, "_read_acp_discovery", _boom)
    assert census.parse_ucp_profile({}) == {"ok": False, "reason": "unreadable"}
    assert census.parse_acp_discovery({}) == {"ok": False, "reason": "unreadable"}


@pytest.mark.parametrize("doc, reason", [
    ({"protocol": {"name": "acp"}}, "version_missing"),
    ({"protocol": {"version": "latest"}}, "version_invalid"),
    ("<html>", "not_json"),
    ([], "not_an_object"),
], ids=["no-version", "version-word", "html", "array"])
def test_unreadable_acp_documents_yield_a_reason(doc, reason):
    assert census.parse_acp_discovery(doc) == {"ok": False, "reason": reason}


# ── bodies ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("response, ucp, err", [
    (_raw(200, b"<html>shop</html>", content_type="text/html"), "not_json", "ucp_not_json"),
    (_raw(200, b"{broken", content_type="application/json"), 200, "ucp_not_json"),
    (_raw(200, b"[1,2]", content_type="application/json"), 200, "ucp_not_an_object"),
    (_raw(200, b"{}", content_type="application/json"), 200, "ucp_version_missing"),
    (_raw(200, b"x" * 2000, content_type="application/json", content_encoding="gzip"), "encoded_body", "ucp_encoded_body"),
    (_raw(500, b"{}", content_type="application/json"), 500, None),
    (_raw(304), 304, None),
], ids=["html", "broken-json", "json-array", "empty-object", "compressed-anyway", "server-error", "not-modified"])
def test_garbage_bodies_become_codes(monkeypatch, response, ucp, err):
    _, got = _census(monkeypatch, {("shop-a.example", U): response})
    row = got.row("shop-a.example")
    assert (row["ucp"], row["err"], row["ver"]) == (ucp, err, None)
    assert got.summary["ucp_ok"] == 0 and got.summary["errors"] == (1 if err else 0)


def test_json_served_without_a_json_content_type_is_still_read(monkeypatch):
    body = json.dumps(UCP_EMPTY).encode()
    _, got = _census(monkeypatch, {
        ("shop-a.example", U): _raw(200, body, content_type="application/octet-stream")})
    assert got.row("shop-a.example")["ver"] == "2026-01-23"


class _Endless(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks = 0

    async def __aiter__(self):
        while self.chunks < 10_000:
            self.chunks += 1
            yield b"a" * 8192


def test_reading_stops_at_the_size_cap(monkeypatch):
    stream = _Endless()
    _, got = _census(monkeypatch, {
        ("shop-a.example", U): lambda r: httpx.Response(200, stream=stream, headers={"content-type": "application/json"})})
    assert (got.row("shop-a.example")["ucp"], got.row("shop-a.example")["err"]) == ("too_large", "ucp_too_large")
    # 256 KB is 32 chunks of 8 KB: the read stops right after the cap, not at the end of the body.
    assert census.MAX_BODY_BYTES == 256 * 1024
    assert 32 < stream.chunks <= 36


def test_a_body_of_exactly_the_cap_is_read_and_one_byte_more_is_not(monkeypatch):
    pad = census.MAX_BODY_BYTES - len(json.dumps(UCP_EMPTY))
    at_cap = json.dumps(UCP_EMPTY).encode() + b" " * pad
    for body, expect in ((at_cap, 200), (at_cap + b" ", "too_large")):
        _, got = _census(monkeypatch, {("shop-a.example", U): _raw(200, body, content_type="application/json")})
        assert got.row("shop-a.example")["ucp"] == expect
        assert (got.row("shop-a.example")["ver"] == "2026-01-23") is (expect == 200)


def test_a_declared_length_over_the_cap_is_refused_without_reading(monkeypatch):
    stream = _Endless()
    _, got = _census(monkeypatch, {("shop-a.example", U): lambda r: httpx.Response(
        200, stream=stream, headers={"content-type": "application/json", "content-length": str(census.MAX_BODY_BYTES + 1)})})
    assert got.row("shop-a.example")["ucp"] == "too_large" and stream.chunks == 0


# ── the address guard ────────────────────────────────────────────────────────────────────────


def test_the_guards_are_the_card_issuance_ones_not_copies():
    assert census.resolves_only_public is agent_card_issuance.resolves_only_public
    assert census.validate_merchant_domain is agent_card_issuance.validate_merchant_domain


def test_a_host_that_resolves_privately_is_never_requested(monkeypatch):
    net, got = _census(monkeypatch, {("shop-b.example", U): _json(UCP_EMPTY)},
                       domains="shop-a.example,shop-b.example", private={"shop-a.example"})
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == (
        "unavailable", "robots_unavailable", "robots_unavailable", "robots_not_public")
    assert all(r.url.host == "shop-b.example" for r in net.requests) and len(net.requests) == 3
    assert got.row("shop-b.example")["ver"] == "2026-01-23"
    # asked before EVERY request: robots, then once per path
    assert net.resolved == ["shop-a.example", "shop-b.example", "shop-b.example", "shop-b.example"]


def test_the_address_is_checked_again_before_each_well_known_request(monkeypatch):
    net = Net({("shop-a.example", U): _json(UCP_0825), ("shop-a.example", A): _json(ACP_FULL)})
    answers = iter([True, False, False])  # public for robots.txt, then it starts resolving inside

    def _rebinding(host):
        net.resolved.append(host)
        return next(answers)

    got = _run(monkeypatch, net, ["--domains", "shop-a.example"], resolver=_rebinding)
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == ("absent", "not_public", "not_public", "ucp_not_public")
    assert net.urls() == ["https://shop-a.example/robots.txt"] and len(net.resolved) == 3


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "192.168.1.9"],
                         ids=["rfc1918-10", "loopback", "link-local-metadata", "v6-loopback", "rfc1918-192"])
def test_without_an_injected_resolver_the_real_guard_decides(monkeypatch, address):
    asked: List[str] = []

    def _fake_getaddrinfo(host, port, *a, **k):
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setenv(census.FLAG, "on")
    net = Net({("shop-a.example", U): _json(UCP_0825)})
    seams = net.seams()
    del seams["resolver"]
    lines: List[str] = []
    assert census.main(["--domains", "shop-a.example"], out=lines.append, **seams) == 0
    got = Run(0, lines)
    row = got.row("shop-a.example")
    assert (row["ucp"], row["err"]) == ("robots_unavailable", "robots_not_public") and net.requests == []
    assert asked == ["shop-a.example"] * 2  # the guard's lookup, then one more to name the refusal


def test_a_resolver_that_raises_or_hangs_costs_one_row(monkeypatch):
    def _raises(host):
        raise OSError("resolver down")

    net = Net({})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example"], resolver=_raises)
    assert got.row("shop-a.example")["err"] == "robots_fetch_failed" and net.requests == []
    assert got.lines[-1] == census.DONE_LINE


@pytest.mark.parametrize("host", ["203.0.113.7", "shop-a.internal", "localhost", "shop-a.example:8443", "Shop-A.example"],
                         ids=["ip-literal", "internal-suffix", "localhost", "port", "not-normalized"])
def test_the_fetcher_itself_refuses_a_host_that_is_not_a_bare_public_name(host):
    net = Net({})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(net.handler)) as client:
            pacer = census.Pacer(clock=net.clock, sleep=net.sleep)
            return await census.fetch_well_known(client, host, census.UCP_PATH, resolver=net.resolver, pacer=pacer)

    assert asyncio.run(go()) == census.Fetched("invalid_domain", None, None)
    assert net.requests == [] and net.resolved == []


# ── redirects ────────────────────────────────────────────────────────────────────────────────


def test_one_hop_to_the_www_sibling_is_followed(monkeypatch):
    net, got = _census(monkeypatch, {
        ("shop-a.example", U): _redirect("https://www.shop-a.example/.well-known/ucp?utm=1#frag",
                                         set_cookie=f"sid={MARKER}; Path=/"),
        ("www.shop-a.example", U): _json(UCP_EMPTY),
    })
    assert got.row("shop-a.example")["ver"] == "2026-01-23" and got.row("shop-a.example")["err"] is None
    assert net.urls() == ["https://shop-a.example/robots.txt", "https://shop-a.example/.well-known/ucp",
                          "https://www.shop-a.example/.well-known/ucp", "https://shop-a.example/.well-known/acp.json"]
    # the target is guarded too
    assert net.resolved == ["shop-a.example", "shop-a.example", "www.shop-a.example", "shop-a.example"]
    assert all("cookie" not in r.headers for r in net.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308], ids=lambda s: f"status-{s}")
def test_every_redirect_status_is_handled_the_same_way(monkeypatch, status):
    net, got = _census(monkeypatch, {
        ("shop-a.example", U): _redirect("https://www.shop-a.example/.well-known/ucp", status),
        ("www.shop-a.example", U): _json(UCP_EMPTY)})
    assert got.row("shop-a.example")["ucp"] == 200
    net, got = _census(monkeypatch, {("shop-a.example", U): _redirect("https://other.example/.well-known/ucp", status)})
    assert got.row("shop-a.example")["ucp"] == "redirect_refused" and len(net.requests) == 3


def test_a_second_redirect_is_refused(monkeypatch):
    net, got = _census(monkeypatch, {
        ("shop-a.example", U): _redirect("https://www.shop-a.example/.well-known/ucp"),
        ("www.shop-a.example", U): _redirect("https://shop-a.example/.well-known/ucp"),
    })
    row = got.row("shop-a.example")
    assert (row["ucp"], row["err"]) == ("redirect_refused", "ucp_redirect_refused")
    assert [u for u in net.urls() if u.endswith("/ucp")] == [
        "https://shop-a.example/.well-known/ucp", "https://www.shop-a.example/.well-known/ucp"]


@pytest.mark.parametrize("location", [
    "https://shop-b.example/.well-known/ucp",
    "https://www.shop-b.example/.well-known/ucp",
    "https://shop-a.example.attacker.example/.well-known/ucp",
    "http://www.shop-a.example/.well-known/ucp",
    "https://www.shop-a.example:8443/.well-known/ucp",
    "https://www.shop-a.example/api/ucp/mcp",
    "https://www.shop-a.example/",
    "https://cdn.shop-a.example/.well-known/ucp",
    "https://203.0.113.7/.well-known/ucp",
    "//www.shop-a.example/.well-known/ucp",
    "/.well-known/acp.json",
    "",
], ids=["other-domain", "other-domain-www", "lookalike-suffix", "http-downgrade", "own-port", "other-path", "root",
        "other-subdomain", "ip-literal", "scheme-relative", "relative-other-path", "no-location"])
def test_any_other_redirect_is_refused_and_not_requested(monkeypatch, location):
    net, got = _census(monkeypatch, {("shop-a.example", U): _redirect(location)})
    row = got.row("shop-a.example")
    assert (row["ucp"], row["err"]) == ("redirect_refused", "ucp_redirect_refused")
    assert net.urls() == ["https://shop-a.example/robots.txt", "https://shop-a.example/.well-known/ucp",
                          "https://shop-a.example/.well-known/acp.json"]
    assert got.lines[-1] == census.DONE_LINE and MARKER not in "".join(got.lines)


def test_a_sibling_that_resolves_privately_is_not_requested(monkeypatch):
    net, got = _census(monkeypatch, {("shop-a.example", U): _redirect("https://www.shop-a.example/.well-known/ucp")},
                       private={"www.shop-a.example"})
    assert got.row("shop-a.example")["ucp"] == "not_public"
    assert all(r.url.host == "shop-a.example" for r in net.requests)


# ── input ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("shop-a.example", "shop-a.example"),
    ("  Shop-A.Example ", "shop-a.example"),
    ("www.shop-a.example", "shop-a.example"),
    ("WWW.shop-a.co.example", "shop-a.co.example"),
    ("https://shop-a.example", None),
    ("http://shop-a.example", None),
    ("//shop-a.example", None),
    ("shop-a.example/", None),
    ("shop-a.example/.well-known/ucp", None),
    ("shop-a.example:443", None),
    ("user@shop-a.example", None),
    ("user:pw@shop-a.example", None),
    ("shop-a.example?x=1", None),
    ("shop-a.example#x", None),
    ("203.0.113.7", None),
    ("127.1", None),
    ("0x7f.0.0.1", None),
    ("[2001:db8::1]", None),
    ("2001:db8::1", None),
    ("localhost", None),
    ("shop-a", None),
    ("shop-a.internal", None),
    ("shop-a.local", None),
    ("shop a.example", None),
    ("shop-a.example\nshop-b.example", None),
    ("shop%2da.example", None),
    ("-shop.example", None),
    ("www.", None),
    ("", None),
    (None, None),
    (7, None),
], ids=["bare", "case-and-space", "www", "www-upper", "https", "http", "scheme-relative", "trailing-slash", "path",
        "port", "userinfo", "userinfo-password", "query", "fragment", "ipv4", "short-ipv4", "hex-ipv4",
        "ipv6-bracket", "ipv6", "localhost", "single-label", "internal", "local", "inner-space", "newline",
        "percent", "leading-dash", "only-www", "empty", "none", "number"])
def test_domain_normalization_and_rejects(raw, expected):
    assert census.normalize_domain(raw) == expected


def test_a_refused_entry_gets_a_row_without_echoing_it_and_the_run_goes_on(monkeypatch):
    net, got = _census(monkeypatch, {("shop-b.example", U): _json(UCP_EMPTY)},
                       domains=f"https://user:{MARKER}@shop-a.example/x,shop-b.example")
    assert got.rows[0] == {**census.empty_row(None), "err": "invalid_domain"}
    assert got.row("shop-b.example")["ver"] == "2026-01-23"
    assert MARKER not in "".join(got.lines) and all(r.url.host == "shop-b.example" for r in net.requests)
    assert (got.summary["domains"], got.summary["errors"]) == (2, 1)


def test_domains_come_from_the_argument_and_the_file_without_duplicates(monkeypatch, tmp_path):
    path = tmp_path / "domains.txt"
    path.write_text("# a comment\n\nshop-b.example\nwww.shop-a.example\r\n  shop-c.example  \n", encoding="utf-8")
    net = Net({})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example, shop-b.example,", "--domains-file", str(path)])
    assert [r["d"] for r in got.rows] == ["shop-a.example", "shop-b.example", "shop-c.example"]
    assert len(net.requests) == 9


@pytest.mark.parametrize("argv, line", [
    ([], "UCPPROF_REFUSED no_domains"),
    (["--domains", " , "], "UCPPROF_REFUSED no_domains"),
    (["--domains-file", "/nonexistent/census/domains.txt"], "UCPPROF_REFUSED domains_file_unreadable"),
], ids=["no-arguments", "blank-list", "missing-file"])
def test_a_run_with_nothing_to_read_is_refused(monkeypatch, argv, line):
    net = Net({})
    got = _run(monkeypatch, net, argv)
    assert (got.code, got.lines) == (census.EXIT_REFUSED, [line])
    assert net.requests == [] and net.resolved == []


def test_more_than_300_domains_is_refused_before_anything_is_read(monkeypatch, tmp_path):
    assert census.MAX_DOMAINS == 300
    path = tmp_path / "domains.txt"
    path.write_text("\n".join(f"shop-{i}.example" for i in range(301)), encoding="utf-8")
    net = Net({})
    got = _run(monkeypatch, net, ["--domains-file", str(path)])
    assert got.code == census.EXIT_REFUSED != 0
    assert got.lines == ["UCPPROF_REFUSED too_many_domains n=301 max=300; nothing was read"]
    assert net.requests == [] and net.resolved == []


def test_exactly_300_domains_run(monkeypatch):
    net = Net({})
    got = _run(monkeypatch, net, ["--domains", ",".join(f"shop-{i}.example" for i in range(300))])
    assert got.code == 0 and len(got.rows) == 300 and got.summary["domains"] == 300
    assert len(net.requests) == 900  # three per domain, never more
    assert len(net.sleeps) == 899 and got.lines[-1] == census.DONE_LINE


# ── requests ─────────────────────────────────────────────────────────────────────────────────


def test_pacing_sleeps_one_second_between_requests(monkeypatch):
    net, got = _census(monkeypatch, {("shop-a.example", U): _redirect("https://www.shop-a.example/.well-known/ucp")},
                       domains="shop-a.example,shop-b.example")
    assert len(net.requests) == 7  # the redirect hop is a request and is paced like any other
    assert net.sleeps == [1.0] * 6
    assert census.MIN_SECONDS_BETWEEN_REQUESTS == 1.0


def test_pacing_counts_time_already_spent(monkeypatch):
    net = Net({})

    def handler(request):
        net.now += 0.4  # the request itself took 0.4 s
        return net.handler(request)

    monkeypatch.setenv(census.FLAG, "on")
    seams = net.seams(transport=httpx.MockTransport(handler))
    census.main(["--domains", "shop-a.example"], out=lambda line: None, **seams)
    assert net.sleeps == [pytest.approx(0.6), pytest.approx(0.6)]
    slow = Net({})

    def slower(request):
        slow.now += 3.0
        return slow.handler(request)

    census.main(["--domains", "shop-a.example"], out=lambda line: None,
                **slow.seams(transport=httpx.MockTransport(slower)))
    assert slow.sleeps == []


def test_every_request_is_a_get_to_one_of_the_two_paths_over_https(monkeypatch):
    net, got = _census(monkeypatch, {
        ("shop-a.example", U): _json(UCP_0825), ("shop-a.example", A): _json(ACP_FULL),
        ("shop-b.example", U): _redirect("https://www.shop-b.example/.well-known/ucp"),
        ("shop-c.example", A): _redirect("https://www.shop-c.example/.well-known/acp.json"),
        ("www.shop-c.example", A): _json(ACP_MIN),
    }, domains="shop-a.example,shop-b.example,shop-c.example")
    assert len(net.requests) == 11
    for request in net.requests:
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.port is None
        assert request.url.path in {"/robots.txt", "/.well-known/ucp", "/.well-known/acp.json"}
        assert request.url.query == b"" and request.url.userinfo == b"" and request.content == b""
        assert request.headers["user-agent"] == crawl_identity.DECLARED_USER_AGENT
        assert request.headers["accept"] == ("text/plain" if request.url.path == "/robots.txt" else "application/json")
        assert request.headers["accept-encoding"] == "identity"
        assert not {"cookie", "authorization", "proxy-authorization"} & set(request.headers)
    assert got.row("shop-c.example")["acp_ver"] == "2025-09-29"
    per_domain: Dict[str, int] = {}
    for request in net.requests:
        key = request.url.host.removeprefix("www.")
        per_domain[key] = per_domain.get(key, 0) + 1
    assert per_domain == {"shop-a.example": 3, "shop-b.example": 4, "shop-c.example": 4}


def test_the_only_url_builder_refuses_everything_else():
    assert census.well_known_url("shop-a.example", census.UCP_PATH) == "https://shop-a.example/.well-known/ucp"
    assert census.well_known_url("shop-a.example", census.ACP_PATH) == "https://shop-a.example/.well-known/acp.json"
    assert census.well_known_url("shop-a.example", census.ROBOTS_PATH) == "https://shop-a.example/robots.txt"
    assert (census.UCP_PATH, census.ACP_PATH, census.ROBOTS_PATH) == (
        "/.well-known/ucp", "/.well-known/acp.json", "/robots.txt")
    for path in ("/api/ucp/mcp", "/", "", "/.well-known/ucp?x=1", "/.well-known/ucp/", "/cart.js"):
        with pytest.raises(ValueError):
            census.well_known_url("shop-a.example", path)
    for host in ("shop-a.example/x", "shop-a.example:8443", "u@shop-a.example", "203.0.113.7", "Shop-A.example",
                 "localhost", "", None):
        with pytest.raises(ValueError):
            census.well_known_url(host, census.UCP_PATH)


def test_a_failing_domain_does_not_stop_the_run(monkeypatch):
    def _explodes(request):
        raise RuntimeError(f"boom {MARKER}")

    net, got = _census(monkeypatch, {
        ("shop-a.example", U): httpx.ConnectError(f"refused {MARKER}"),
        ("shop-a.example", A): httpx.ReadTimeout("slow"),
        ("shop-b.example", U): _explodes,
        ("shop-c.example", U): _raw(200, b"\x00\x01", content_type="application/json"),
        ("shop-d.example", U): _json(UCP_0825),
    }, domains="shop-a.example,shop-b.example,shop-c.example,shop-d.example")
    assert got.code == 0
    a, b, c, d = (got.row(f"shop-{x}.example") for x in "abcd")
    assert (a["ucp"], a["acp"], a["err"]) == ("http_error", "timeout", "ucp_http_error")
    assert (b["ucp"], b["acp"], b["err"]) == ("fetch_failed", 404, "ucp_fetch_failed")
    assert (c["ucp"], c["err"]) == (200, "ucp_not_json")
    assert d["ver"] == "2026-08-25" and d["err"] is None
    assert got.summary == {**SUMMARY_ZERO, "domains": 4, "ucp_ok": 1, "with_checkout": 1, "with_discount": 1,
                           "errors": 3, "robots_absent": 4}
    assert got.lines[-1] == census.DONE_LINE and MARKER not in "".join(got.lines)
    assert len(net.requests) == 12  # no retry


def test_a_row_that_cannot_be_built_does_not_stop_the_run(monkeypatch):
    real = census.build_row

    def _build(domain, *rest):
        if domain == "shop-a.example":
            raise RuntimeError("boom")
        return real(domain, *rest)

    monkeypatch.setattr(census, "build_row", _build)
    _, got = _census(monkeypatch, {("shop-b.example", U): _json(UCP_EMPTY)}, domains="shop-a.example,shop-b.example")
    assert got.row("shop-a.example") == {**census.empty_row("shop-a.example"), "err": "row_failed"}
    assert got.row("shop-b.example")["ver"] == "2026-01-23" and got.summary["errors"] == 1


def test_the_five_second_limit_covers_the_whole_request(monkeypatch):
    assert census.REQUEST_TIMEOUT_SECONDS == 5.0

    async def _slow(request):
        if request.url.path != R or request.url.host == "shop-b.example":
            await asyncio.sleep(0.5)
        return _json(UCP_0825)(request) if request.url.path != R else _raw(404)(request)

    net = Net({})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example,shop-b.example"],
               transport=httpx.MockTransport(_slow), timeout=0.02)
    row = got.row("shop-a.example")
    assert (row["ucp"], row["acp"], row["err"]) == ("timeout", "timeout", "ucp_timeout")
    row = got.row("shop-b.example")  # a robots.txt that does not answer in time allows nothing
    assert (row["robots"], row["ucp"], row["err"]) == ("unavailable", "robots_unavailable", "robots_timeout")


# ── output ───────────────────────────────────────────────────────────────────────────────────


def test_output_lines_are_prefixed_and_carry_no_body_text(monkeypatch):
    profile = copy.deepcopy(UCP_0825)
    ucp = profile["ucp"]
    ucp["services"]["dev.ucp.shopping"][0]["endpoint"] = f"https://shop-a.example/ucp?token={MARKER}"
    ucp["services"][f"bad name {MARKER}"] = [{"transport": "mcp"}]
    ucp["services"]["dev.ucp.shopping"].append({"transport": f"mcp {MARKER}"})
    ucp["capabilities"][f"cap/{MARKER}?x=1"] = [{}]
    ucp["payment_handlers"]["com.example.processor_tokenizer"][0]["config"]["merchant_id"] = MARKER
    ucp["payment_handlers"]["com.example.processor_tokenizer"][0]["available_instruments"].append(
        {"type": f"https://x.example/?{MARKER}"})
    ucp["payment_handlers"][f"h {MARKER}"] = [{"id": f"id {MARKER}"}]
    ucp["supported_versions"] = {f"2026-01-23?{MARKER}": f"https://shop-a.example/{MARKER}"}
    profile["keys"][0]["kid"] = MARKER
    profile[MARKER] = MARKER
    acp = copy.deepcopy(ACP_FULL)
    acp["api_base_url"] = f"https://shop-a.example/api?key={MARKER}"
    acp["capabilities"]["services"].append(f"svc {MARKER}")
    acp["capabilities"]["extensions"].append({"name": f"ext?{MARKER}", "spec": f"https://x.example/{MARKER}"})
    _, got = _census(monkeypatch, {("shop-a.example", U): _json(profile, x_secret=MARKER),
                                   ("shop-a.example", A): _json(acp)})
    row = got.row("shop-a.example")
    assert row["err"] is None and row["keys"] == 2 and row["ph"] == {"processor_tokenizer": ["card"]}
    assert [s["name"] for s in row["svc"]] == ["dev.ucp.shopping"] * 4 + [None]
    assert len(got.lines) == 4 and got.lines[0].startswith("UCPPROF_PLAN {")
    assert got.lines[1].startswith("UCPPROF_ROW {") and got.lines[2].startswith("UCPPROF_SUM {")
    assert got.lines[3] == "UCPPROF_DONE"
    text = "\n".join(got.lines)
    for forbidden in (MARKER, "http", "?", "token=", "token_retrieval", "/ucp/v1", "poqkLGiymh", "psp.example", "accounts.example"):
        assert forbidden not in text, forbidden


def test_main_prints_to_stdout_by_default(monkeypatch, capsys):
    monkeypatch.delenv(census.FLAG, raising=False)
    assert census.main([]) == 0
    assert capsys.readouterr().out == census.DISABLED_LINE + "\n"


# ── robots.txt ───────────────────────────────────────────────────────────────────────────────

BOTH = {("shop-a.example", U): _json(UCP_0825), ("shop-a.example", A): _json(ACP_FULL)}


def test_the_robots_token_is_the_product_name_inside_the_declared_user_agent():
    assert census.ROBOTS_TOKEN == "PivotaBot"
    assert f"{census.ROBOTS_TOKEN}/" in census.USER_AGENT and census.USER_AGENT == crawl_identity.DECLARED_USER_AGENT


@pytest.mark.parametrize("text, ucp, acp, asked", [
    ("User-agent: PivotaBot\nDisallow: /.well-known/ucp\n", "robots_disallowed", 200, [R, A]),
    ("User-agent: pivotabot\nDisallow: /.well-known/acp.json\n", 200, "robots_disallowed", [R, U]),
    ("User-agent: PivotaBot\nDisallow: /.well-known/\n", "robots_disallowed", "robots_disallowed", [R]),
    ("User-agent: PivotaBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "robots_disallowed", "robots_disallowed", [R]),
    ("User-agent: *\nDisallow: /\n", "robots_disallowed", "robots_disallowed", [R]),
    ("User-agent: *\nDisallow: /.well-known/ucp\n", "robots_disallowed", 200, [R, A]),
    ("\ufeffUser-agent: *\r\nDisallow: /.well-known\r\n", "robots_disallowed", "robots_disallowed", [R]),
    ("User-agent: *\nDisallow: /*.json$\n", 200, "robots_disallowed", [R, U]),
    ("User-agent: *\nDisallow: /.well-known/*\n", "robots_disallowed", "robots_disallowed", [R]),
    ("User-agent: otherbot\nUser-agent: PivotaBot/1.0\nDisallow: /.well-known/ucp$\n", "robots_disallowed", 200, [R, A]),
], ids=["our-token-ucp", "our-token-lowercase-acp", "our-token-directory", "our-token-beats-star-allow", "star-everything",
        "star-ucp", "bom-and-crlf", "wildcard-suffix", "wildcard-directory", "shared-group-anchored"])
def test_a_path_robots_disallows_is_never_requested(monkeypatch, text, ucp, acp, asked):
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): _robots(text)})
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"]) == ("disallowed", ucp, acp)
    assert net.paths("shop-a.example") == asked
    assert row["err"] == ("ucp_robots_disallowed" if ucp != 200 else "acp_robots_disallowed")
    assert (got.summary["robots_disallowed"], got.summary["ucp_ok"]) == (1, 1 if ucp == 200 else 0)
    assert got.lines[-1] == census.DONE_LINE


@pytest.mark.parametrize("text", [
    "User-agent: *\nDisallow: /cart\nDisallow: /checkout\nAllow: /\n",
    "User-agent: otherbot\nDisallow: /\n",
    "User-agent: *\nDisallow: /\n\nUser-agent: PivotaBot\nAllow: /\n",
    "User-agent: *\nDisallow:\n",
    "User-agent: *\nDisallow: /*?preview=\nSitemap: https://shop-a.example/sitemap.xml\n",
    "<html><body>not a robots file</body></html>",
    "User-agent: otherbot\nDisallow: /.well-known/*\n",
    "Disallow: /\n",
    "User-agent: *\nDisallow: /.well-known/ucp/\nDisallow: /.well-known/acp.jsonl$\nDisallow: /.WELL-KNOWN/\n",
], ids=["other-paths", "other-bot-only", "our-token-allowed-by-name", "empty-disallow", "unrelated-wildcard", "html",
        "other-bot-wildcard", "rule-before-any-group", "near-misses"])
def test_a_robots_file_that_allows_both_paths(monkeypatch, text):
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): _robots(text)})
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == ("ok", 200, 200, None)
    assert net.paths("shop-a.example") == [R, U, A] and got.summary["robots_ok"] == 1


@pytest.mark.parametrize("route", [_raw(404), _raw(410), _robots(""), _robots("  \n\n")],
                         ids=["404", "410", "empty", "blank-lines"])
def test_no_robots_file_means_nothing_to_obey(monkeypatch, route):
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): route})
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == ("absent", 200, 200, None)
    assert net.paths("shop-a.example") == [R, U, A] and got.summary["robots_absent"] == 1


@pytest.mark.parametrize("route, why", [
    (_raw(500), "robots_500"),
    (_raw(503), "robots_503"),
    (_raw(403), "robots_403"),
    (_raw(401), "robots_401"),
    (_raw(429), "robots_429"),
    (_raw(204), "robots_204"),
    (_robots("User-agent: *\nAllow: /\n" + "# pad\n" * 60_000), "robots_too_large"),
    (_raw(200, b"User-agent: *\nAllow: /\n\xff\xfe\x00", content_type="text/plain"), "robots_undecodable"),
    (_robots("User-agent: *\nAllow: /\n", content_encoding="gzip"), "robots_encoded_body"),
    (_redirect("https://other.example/robots.txt"), "robots_redirect_refused"),
    (_redirect("https://www.other.example/robots.txt"), "robots_redirect_refused"),
    (_redirect("http://www.shop-a.example/robots.txt"), "robots_redirect_refused"),
    (_redirect("https://www.shop-a.example/"), "robots_redirect_refused"),
    (httpx.ConnectError("refused"), "robots_http_error"),
    (httpx.ReadTimeout("slow"), "robots_timeout"),
], ids=["500", "503", "403", "401", "429", "204", "oversize", "undecodable", "compressed-anyway", "redirect-off-domain",
        "redirect-off-domain-www", "redirect-http", "redirect-other-path", "connect-error", "timeout"])
def test_an_unreadable_robots_file_fails_closed(monkeypatch, route, why):
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): route, ("shop-b.example", U): _json(UCP_EMPTY)},
                       domains="shop-a.example,shop-b.example")
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == (
        "unavailable", "robots_unavailable", "robots_unavailable", why)
    assert row["ver"] is None and net.paths("shop-a.example") == [R]  # neither well-known path was asked
    assert all(r.url.host in {"shop-a.example", "shop-b.example"} for r in net.requests)
    assert got.row("shop-b.example")["ver"] == "2026-01-23"  # and the run went on
    assert (got.summary["robots_unavailable"], got.summary["robots_absent"], got.summary["errors"]) == (1, 1, 1)


def test_a_robots_redirect_to_the_www_sibling_is_followed_once_and_obeyed(monkeypatch):
    net, got = _census(monkeypatch, {
        **BOTH,
        ("shop-a.example", R): _redirect("https://www.shop-a.example/robots.txt"),
        ("www.shop-a.example", R): _robots("User-agent: *\nDisallow: /.well-known/acp.json\n"),
        ("www.shop-a.example", U): _json(UCP_EMPTY),
    })
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["ver"], row["acp"]) == ("disallowed", 200, "2026-01-23", "robots_disallowed")
    # the host that answered robots.txt is asked directly: no second redirect is paid for
    assert net.urls() == ["https://shop-a.example/robots.txt", "https://www.shop-a.example/robots.txt",
                          "https://www.shop-a.example/.well-known/ucp"]
    net, got = _census(monkeypatch, {
        **BOTH,
        ("shop-a.example", R): _redirect("https://www.shop-a.example/robots.txt"),
        ("www.shop-a.example", R): _redirect("https://shop-a.example/robots.txt"),
    })
    assert got.row("shop-a.example")["err"] == "robots_redirect_refused" and len(net.requests) == 2


def test_the_robots_parser_is_given_text_and_fetches_nothing(monkeypatch):
    import urllib.request

    def _no_fetch(*a, **k):
        raise AssertionError("the robots parser tried to fetch")

    monkeypatch.setattr(urllib.request, "urlopen", _no_fetch)
    state, allowed, why = census.read_robots("shop-a.example", (200, b"User-agent: *\nDisallow: /.well-known/ucp\n"))
    assert (state, allowed, why) == ("disallowed", {U: False, A: True}, None)


@pytest.mark.parametrize("fetched", [(200, None), (None, None), ("timeout", None), (200, 7), (301, b""), None, (200,)],
                         ids=["no-body", "no-status", "code", "body-not-bytes", "redirect-status", "none", "short-tuple"])
def test_read_robots_never_raises_and_allows_nothing_it_could_not_read(fetched):
    state, allowed, why = census.read_robots("shop-a.example", fetched)
    assert state == "unavailable" and allowed == {U: False, A: False} and isinstance(why, str)


def test_a_robots_reader_that_breaks_allows_nothing(monkeypatch):
    def _boom(text):
        raise RuntimeError("boom")

    monkeypatch.setattr(census, "_robots_rules", _boom)
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): _robots("User-agent: *\nAllow: /\n")})
    assert got.row("shop-a.example")["err"] == "robots_unparseable" and net.paths("shop-a.example") == [R]


def test_a_run_leaves_the_http_library_log_levels_as_it_found_them(monkeypatch):
    """The job quiets httpx/httpcore for its own run. Left quiet, every later test in the same
    process that asserts on those loggers' records sees none."""
    import logging

    before = {}
    for name, level in (("httpx", logging.DEBUG), ("httpcore", logging.NOTSET)):
        before[name] = logging.getLogger(name).level
        logging.getLogger(name).setLevel(level)
    try:
        _, got = _census(monkeypatch, {})
        assert got.code == 0
        assert logging.getLogger("httpx").level == logging.DEBUG
        assert logging.getLogger("httpcore").level == logging.NOTSET
    finally:
        for name, level in before.items():
            logging.getLogger(name).setLevel(level)


# ── the directory population ─────────────────────────────────────────────────────────────────


def test_the_two_directory_statements_are_selects_on_the_directory_tables():
    assert " ".join(census._DIRECTORY_TIERB_DOMAINS_SQL.split()) == (
        "SELECT DISTINCT shop_domain FROM tierb_cart_link_eligibility")
    assert " ".join(census._DIRECTORY_PURCHASABILITY_DOMAINS_SQL.split()) == (
        "SELECT DISTINCT merchant_domain FROM merchant_purchasability")


def test_from_directory_reads_both_tables_normalizes_sorts_and_writes_nothing(monkeypatch):
    db = _FakeDirectoryDb(monkeypatch, ["WWW.Shop-C.example", "shop-a.example", None, ""],
                          ["shop-b.example", "shop-a.example", f"https://{MARKER}@shop-d.example/x", "www.shop-c.example"])
    net = Net({("shop-b.example", U): _json(UCP_EMPTY)})
    got = _run(monkeypatch, net, ["--from-directory"])
    assert got.code == 0
    assert [r["d"] for r in got.rows] == ["shop-a.example", "shop-b.example", "shop-c.example", None]
    assert got.rows[-1]["err"] == "invalid_domain" and MARKER not in "".join(got.lines)
    assert got.row("shop-b.example")["ver"] == "2026-01-23"
    assert db.calls == ["connect", "SELECT DISTINCT shop_domain FROM tierb_cart_link_eligibility",
                        "SELECT DISTINCT merchant_domain FROM merchant_purchasability", "disconnect"]
    assert len(net.requests) == 9 and got.summary["domains"] == 4


@pytest.mark.parametrize("extra", [["--domains", "shop-a.example"], ["--domains-file", "/nonexistent/census/x.txt"]],
                         ids=["with-domains", "with-domains-file"])
def test_from_directory_cannot_be_combined_with_a_typed_population(monkeypatch, extra):
    db = _FakeDirectoryDb(monkeypatch, ["shop-a.example"], [])
    net = Net({})
    got = _run(monkeypatch, net, ["--from-directory", *extra])
    assert got.code == census.EXIT_REFUSED and len(got.lines) == 1
    assert got.lines[0].startswith("UCPPROF_REFUSED conflicting_sources")
    assert db.calls == [] and net.requests == [] and net.resolved == []


@pytest.mark.parametrize("fail, calls", [("connect", ["connect"]), ("read", 3)], ids=["cannot-connect", "cannot-read"])
def test_an_unreadable_directory_refuses_the_run(monkeypatch, fail, calls):
    db = _FakeDirectoryDb(monkeypatch, ["shop-a.example"], [], fail=fail)
    net = Net({})
    got = _run(monkeypatch, net, ["--from-directory"])
    assert got.code == census.EXIT_REFUSED
    assert got.lines == [f"UCPPROF_REFUSED directory_unreadable error_type={'OSError' if fail == 'connect' else 'RuntimeError'}"]
    assert net.requests == [] and net.resolved == []
    if fail == "read":
        assert db.calls[0] == "connect" and db.calls[-1] == "disconnect" and len(db.calls) == calls
    else:
        assert db.calls == calls


def test_an_empty_directory_and_an_oversize_one_are_refused(monkeypatch):
    _FakeDirectoryDb(monkeypatch, [], [None, ""])
    net = Net({})
    assert _run(monkeypatch, net, ["--from-directory"]).lines == ["UCPPROF_REFUSED no_domains"]
    _FakeDirectoryDb(monkeypatch, [f"shop-{i}.example" for i in range(200)], [f"www.shop-{i}.example" for i in range(100, 301)])
    got = _run(monkeypatch, net, ["--from-directory"])
    assert got.code == census.EXIT_REFUSED
    assert got.lines == ["UCPPROF_REFUSED too_many_domains n=301 max=300; nothing was read"]
    _FakeDirectoryDb(monkeypatch, [f"shop-{i}.example" for i in range(200)], [f"www.shop-{i}.example" for i in range(100, 300)])
    got = _run(monkeypatch, net, ["--from-directory"])
    assert got.code == 0 and len(got.rows) == 300
    assert [r["d"] for r in got.rows] == sorted(f"shop-{i}.example" for i in range(300))


def test_the_typed_population_never_touches_the_database(monkeypatch):
    db = _FakeDirectoryDb(monkeypatch, ["shop-z.example"], [])
    net = Net({})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example"])
    assert got.code == 0 and db.calls == [] and [r["d"] for r in got.rows] == ["shop-a.example"]


# ── robots patterns: linear, bounded, longest match ──────────────────────────────────────────


@pytest.mark.parametrize("pattern, path, expected", [
    ("/", "/.well-known/ucp", True),
    ("/.well-known", "/.well-known/ucp", True),
    ("/.well-known/ucp", "/.well-known/ucp", True),
    ("/.well-known/ucp", "/.well-known/ucpx", True),
    ("/.well-known/ucp/", "/.well-known/ucp", False),
    ("/.well-known/acp", "/.well-known/ucp", False),
    ("/*/ucp", "/.well-known/ucp", True),
    ("/.well*ucp", "/.well-known/ucp", True),
    ("/*.json", "/.well-known/acp.json", True),
    ("/*.json", "/.well-known/acp.json5", True),
    ("/*.json$", "/.well-known/acp.json", True),
    ("/*.json$", "/.well-known/acp.json5", False),
    ("/*.json$", "/.well-known/ucp", False),
    ("/.well-known/ucp$", "/.well-known/ucp", True),
    ("/.well-known/ucp$", "/.well-known/ucp/x", False),
    ("/.well-known$", "/.well-known/ucp", False),
    ("*", "/.well-known/ucp", True),
    ("*$", "/.well-known/ucp", True),
    ("$", "/.well-known/ucp", False),
    ("", "/.well-known/ucp", True),
    ("/a*b*c*d", "/.well-known/ucp", False),
    ("/*k*n*w*n/*", "/.well-known/ucp", True),
    ("/.well-known/u$cp", "/.well-known/u$cp", True),
    ("/.well-known/u$cp", "/.well-known/ucp", False),
    ("/.well%2Dknown/ucp", "/.well-known/ucp", False),
    ("/.well%2Dknown/ucp", "/.well%2Dknown/ucp", True),
    ("/.Well-Known/ucp", "/.well-known/ucp", False),
    (".well-known", "/.well-known/ucp", False),
], ids=["root", "prefix", "exact", "prefix-longer-path", "longer-than-path", "other-prefix", "star-middle",
        "star-inside-segment", "star-suffix", "star-suffix-is-prefix", "anchored-suffix", "anchored-suffix-miss",
        "anchored-other", "anchored-exact", "anchored-exact-miss", "anchored-prefix-miss", "lone-star",
        "lone-star-anchored", "lone-anchor", "empty", "many-stars-miss", "many-stars-hit", "inner-dollar-literal",
        "inner-dollar-miss", "percent-encoding-as-written", "percent-encoding-literal", "case-sensitive",
        "no-leading-slash"])
def test_robots_pattern_semantics(pattern, path, expected):
    assert census.glob_matches(pattern, path) is expected


def _timed(text: str):
    import time as _time

    started = _time.perf_counter()
    result = census.read_robots("shop-a.example", (200, text.encode()))
    return result, _time.perf_counter() - started


@pytest.mark.parametrize("stars", [12, 20, 5000], ids=["stars-12", "stars-20", "stars-5000"])
def test_a_run_of_stars_costs_no_more_than_one(stars):
    (state, allowed, _), elapsed = _timed("User-agent: *\nDisallow: /" + "*" * stars + "Z$\n")
    assert (state, allowed) == ("ok", {U: True, A: True})  # the run collapses to one `*`; `/*Z$` matches neither
    assert elapsed < 0.5, elapsed
    (state, allowed, _), second = _timed("User-agent: *\nDisallow: /" + "*" * stars + "n$\n")
    assert (state, allowed) == ("disallowed", {U: True, A: False}) and second < 0.5


@pytest.mark.parametrize("line, expected", [
    ("Disallow: /" + "a" * 200_000, {U: False, A: False}),
    ("Disallow: /" + "*a" * 100_000, {U: False, A: False}),
    ("Allow: /" + "*a" * 100_000, {U: True, A: True}),
    ("Disallow: " + "*" * 200_000, {U: False, A: False}),
], ids=["literal", "alternating-stars", "allow-alternating-stars", "only-stars"])
def test_a_200_kb_single_line_pattern_returns_at_once(line, expected):
    (state, allowed, why), elapsed = _timed("User-agent: *\n" + line + "\n")
    assert allowed == expected and why is None
    assert elapsed < 0.5, elapsed


@pytest.mark.parametrize("pattern, disallowed", [
    ("/x" + "*y" * 16, False),
    ("/x" + "*y" * 17, True),
    ("/x" + "**y" * 16, False),
    ("/" + "x" * 511, False),
    ("/" + "x" * 512, True),
], ids=["16-stars", "17-stars", "16-runs-of-stars", "512-chars", "513-chars"])
def test_a_pattern_too_large_to_evaluate_disallows(pattern, disallowed):
    assert (census.MAX_ROBOTS_PATTERN_STARS, census.MAX_ROBOTS_PATTERN_CHARS) == (16, 512)
    state, allowed, _ = census.read_robots("shop-a.example", (200, f"User-agent: *\nDisallow: {pattern}\n".encode()))
    assert allowed == {U: not disallowed, A: not disallowed}
    # the same pattern as an Allow is ignored, so it cannot lift a Disallow
    text = f"User-agent: *\nDisallow: /.well-known/\nAllow: {pattern}\n"
    assert census.read_robots("shop-a.example", (200, text.encode()))[1] == {U: False, A: False}


def test_more_rules_than_the_reader_takes_fails_closed(monkeypatch):
    assert census.MAX_ROBOTS_RULES == 5000
    lines = "User-agent: *\n" + "Disallow: /p\n" * 5000
    assert census.read_robots("shop-a.example", (200, lines.encode()))[0] == "ok"
    state, allowed, why = census.read_robots("shop-a.example", (200, (lines + "Disallow: /q\n").encode()))
    assert (state, allowed, why) == ("unavailable", {U: False, A: False}, "too_many_rules")
    # rules of groups that are not ours are not counted (and not read)
    other = "User-agent: otherbot\n" + "Disallow: /\n" * 6000 + "User-agent: *\nDisallow: /cart\n"
    assert census.read_robots("shop-a.example", (200, other.encode()))[0] == "ok"
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): _robots(lines + "Disallow: /q\n")})
    assert got.row("shop-a.example")["err"] == "robots_too_many_rules" and net.paths("shop-a.example") == [R]


@pytest.mark.parametrize("group", ["*", "PivotaBot"], ids=["star-group", "our-group"])
@pytest.mark.parametrize("rules, ucp, acp", [
    ("Allow: /\nDisallow: /.well-known/ucp", False, True),
    ("Disallow: /.well-known/ucp\nAllow: /", False, True),
    ("Disallow: /\nAllow: /.well-known/", True, True),
    ("Allow: /.well-known/\nDisallow: /", True, True),
    ("Disallow: /.well-known/\nAllow: /.well-known/acp.json", False, True),
    ("Disallow: /.well-known/ucp\nAllow: /.well-known/ucp", True, True),
    ("Allow: /.well-known/ucp\nDisallow: /.well-known/ucp", True, True),
    ("Allow: /.well-known/*\nDisallow: /.well-known/acp.json$", True, False),
    ("Disallow: /*.json\nAllow: /.well-known/acp", True, True),
], ids=["allow-root-then-disallow-path", "disallow-path-then-allow-root", "disallow-root-allow-directory",
        "allow-directory-disallow-root", "longer-allow", "tie-disallow-first", "tie-allow-first",
        "longer-anchored-disallow", "longer-allow-over-wildcard"])
def test_the_longest_matching_rule_decides(monkeypatch, group, rules, ucp, acp):
    text = f"User-agent: {group}\n{rules}\n"
    net, got = _census(monkeypatch, {**BOTH, ("shop-a.example", R): _robots(text)})
    row = got.row("shop-a.example")
    assert (row["ucp"] == 200, row["acp"] == 200) == (ucp, acp)
    assert net.paths("shop-a.example") == [R] + ([U] if ucp else []) + ([A] if acp else [])


def test_our_group_is_used_alone_when_it_exists_and_groups_of_one_name_add_up():
    def read(text):
        return census.read_robots("shop-a.example", (200, text.encode()))[1]

    assert read("User-agent: *\nDisallow: /\n\nUser-agent: PivotaBot\nDisallow: /cart\n") == {U: True, A: True}
    assert read("User-agent: PivotaBot\nDisallow: /cart\n\nUser-agent: *\nDisallow: /\n") == {U: True, A: True}
    assert read("User-agent: PivotaBot\nAllow: /\n\nUser-agent: pivotabot\nDisallow: /.well-known/ucp\n") == {U: False, A: True}
    assert read("User-agent: *\nAllow: /\n\nUser-agent: *\nDisallow: /.well-known/acp.json\n") == {U: True, A: False}
    assert read("User-agent: PivotaBot\n\nUser-agent: *\nDisallow: /\n") == {U: False, A: False}  # one group, two names
    assert read("User-agent: PivotaBotX\nDisallow: /\n") == {U: True, A: True}  # another crawler's name
    # a group that names us and disallows nothing is still OUR group: the `*` group is not consulted
    assert read("User-agent: PivotaBot\nDisallow:\n\nUser-agent: *\nDisallow: /\n") == {U: True, A: True}
    assert read("User-agent: *\nCrawl-delay: 5\nSitemap: https://shop-a.example/s.xml\nDisallow: /.well-known/ucp # no\n") == {
        U: False, A: True}


# ── strict tokens, capped rows ───────────────────────────────────────────────────────────────


def test_a_token_with_a_trailing_newline_is_not_a_token():
    facts = census.parse_ucp_profile({"ucp": {
        "version": "2026-08-25",
        "supported_versions": ["2026-01-11\n", "2026-01-23"],
        "services": {"dev.ucp.shopping\n": [{"transport": "mcp"}], "dev.ucp.shopping": [{"transport": "rest"}]},
        "capabilities": {"dev.ucp.shopping.checkout\n": [{}], "dev.ucp.shopping.order": [{}]},
        "payment_handlers": {"com.example.pay": [{"id": "h1\n", "available_instruments": [{"type": "card\n"}, {"type": "card"}]}]},
    }})
    assert facts["supported"] == ["2026-01-23"] and facts["cap"] == ["dev.ucp.shopping.order"]
    assert facts["svc"] == [{"name": None, "transport": "mcp"}, {"name": "dev.ucp.shopping", "transport": "rest"}]
    assert facts["ph"] == {"com.example.pay": ["card"]}
    assert census.parse_ucp_profile({"ucp": {"version": "2026-08-25\nx"}}) == {"ok": False, "reason": "version_invalid"}
    assert census.parse_acp_discovery({"protocol": {"version": "2026-04-17"}, "capabilities": {
        "services": ["checkout\n", "orders"], "extensions": [{"name": "discount\n"}]}})["cap"] == ["orders"]
    for pattern in (census._VERSION_RE, census._NAME_RE, census._TRANSPORT_RE, census._TLD_RE):
        assert pattern.fullmatch("a1\n") is None and pattern.fullmatch("2026-01-11\n") is None
    assert census._name("card\n") is None and census._version("2026-01-11\n") is None


def test_the_client_ignores_the_environment_and_never_follows_redirects_itself(monkeypatch):
    seen: List[Dict[str, Any]] = []
    real = httpx.AsyncClient

    class _Recording(real):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(census.httpx, "AsyncClient", _Recording)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    net, got = _census(monkeypatch, BOTH)
    assert got.row("shop-a.example")["ucp"] == 200
    (kwargs,) = seen
    assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
    assert "proxy" not in kwargs and "proxies" not in kwargs and "auth" not in kwargs and "cookies" not in kwargs
    assert kwargs.get("verify", True) is True


def test_every_list_in_a_printed_row_is_capped_and_says_so(monkeypatch):
    assert census.MAX_ROW_LIST == 32
    old = {f"2025-01-{d:02d}": "x" for d in range(1, 29)}
    old.update({f"2025-03-{d:02d}": "x" for d in range(1, 29)})
    handlers = {f"com.example.h{i}": [{"id": f"h{i}"}] for i in range(40)}
    handlers["com.example.nt"] = [{"id": "zz", "available_instruments": [{"type": "network_token"}]}]
    handlers["com.example.wide"] = [{"id": "h0", "available_instruments": [{"type": f"type{i}"} for i in range(40)]}]
    capabilities = {f"com.example.cap{i}": [{}] for i in range(40)}
    capabilities["dev.ucp.shopping.checkout"] = [{}]
    profile = {"ucp": {
        "version": "2026-08-25", "supported_versions": old, "capabilities": capabilities, "payment_handlers": handlers,
        "services": {"dev.ucp.shopping": [{"transport": f"t{i}"} for i in range(40)]},
    }}
    acp = {"protocol": {"version": "2026-04-17"}, "capabilities": {"services": [f"svc{i}" for i in range(40)]}}
    _, got = _census(monkeypatch, {("shop-a.example", U): _json(profile), ("shop-a.example", A): _json(acp),
                                   ("shop-b.example", U): _json(UCP_0825)}, domains="shop-a.example,shop-b.example")
    row = got.row("shop-a.example")
    assert row["truncated"] is True and list(row)[-1] == "truncated"
    assert [len(row[k]) for k in ("supported", "svc", "cap", "ph", "acp_cap")] == [32] * 5
    assert len(row["ph"]["h0"]) == 32 and row["cap"][0] == "com.example.cap0"
    assert "truncated" not in got.row("shop-b.example")
    # the totals are counted before the cut: the 41st capability and the 41st handler still count
    assert (got.summary["with_checkout"], got.summary["handlers_accepting_network_token"]) == (2, 1)
    assert max(len(line) for line in got.lines) < 8000


@pytest.mark.parametrize("key", ["supported", "svc", "cap", "acp_cap", "ph", "ph-types"],
                         ids=["supported", "svc", "cap", "acp-cap", "handlers", "handler-types"])
def test_each_list_is_capped_on_its_own(key):
    row = census.empty_row("shop-a.example")
    if key == "ph":
        row["ph"] = {f"h{i}": [] for i in range(33)}
    elif key == "ph-types":
        row["ph"] = {"h": [f"t{i}" for i in range(33)]}
    else:
        row[key] = [f"x{i}" for i in range(33)]
    printed = census.printable_row(row)
    assert printed["truncated"] is True
    assert max([len(printed[k]) for k in ("supported", "svc", "cap", "acp_cap", "ph")]
               + [len(v) for v in printed["ph"].values()]) == 32
    at_cap = census.empty_row("shop-a.example")
    at_cap.update(supported=["x"] * 32, svc=["x"] * 32, cap=["x"] * 32, acp_cap=["x"] * 32,
                  ph={f"h{i}": ["t"] * 32 for i in range(32)})
    assert census.printable_row(at_cap) == at_cap


# ── unresolvable is not "not public" ─────────────────────────────────────────────────────────


def test_a_name_that_does_not_resolve_has_its_own_code_and_is_not_retried_elsewhere(monkeypatch):
    net = Net(BOTH)

    def _resolver(host):
        net.resolved.append(host)
        return "unresolvable"

    got = _run(monkeypatch, net, ["--domains", "shop-a.example"], resolver=_resolver)
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["err"]) == ("unavailable", "robots_unavailable", "robots_unresolvable")
    assert net.requests == [] and net.resolved == ["shop-a.example"]  # no www retry


@pytest.mark.parametrize("answer, code", [
    (socket.gaierror(8, "nodename nor servname provided"), "robots_unresolvable"),
    ([], "robots_unresolvable"),
    ([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))], "robots_not_public"),
    ([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 443)),
      (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443))], "robots_not_public"),
], ids=["lookup-fails", "no-answer", "private-answer", "mixed-answer"])
def test_the_default_resolver_names_why_the_guard_refused(monkeypatch, answer, code):
    def _fake_getaddrinfo(host, port, *a, **k):
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setenv(census.FLAG, "on")
    net = Net(BOTH)
    seams = net.seams()
    del seams["resolver"]
    lines: List[str] = []
    assert census.main(["--domains", "shop-a.example"], out=lines.append, **seams) == 0
    assert Run(0, lines).row("shop-a.example")["err"] == code and net.requests == []


@pytest.mark.parametrize("answer", [None, 1, "yes", "public", [True], 0, ""],
                         ids=["none", "one", "yes", "word", "list", "zero", "empty"])
def test_only_a_plain_true_from_the_resolver_lets_a_request_out(monkeypatch, answer):
    net = Net(BOTH)
    got = _run(monkeypatch, net, ["--domains", "shop-a.example"], resolver=lambda host: answer)
    assert got.row("shop-a.example")["err"] == "robots_not_public" and net.requests == []


# ── the host robots.txt answered on is reused ────────────────────────────────────────────────

WWW = "www.shop-a.example"


def test_a_www_canonical_shop_costs_four_requests_not_six(monkeypatch):
    net, got = _census(monkeypatch, {
        ("shop-a.example", R): _redirect(f"https://{WWW}/robots.txt"),
        ("shop-a.example", U): _redirect(f"https://{WWW}/.well-known/ucp"),
        ("shop-a.example", A): _redirect(f"https://{WWW}/.well-known/acp.json"),
        (WWW, R): _robots("User-agent: *\nDisallow: /cart\n"),
        (WWW, U): _json(UCP_0825), (WWW, A): _json(ACP_MIN),
    })
    row = got.row("shop-a.example")
    assert (row["d"], row["robots"], row["ucp"], row["ver"], row["acp"], row["err"]) == (
        "shop-a.example", "ok", 200, "2026-08-25", 200, None)
    assert net.urls() == ["https://shop-a.example/robots.txt", f"https://{WWW}/robots.txt",
                          f"https://{WWW}/.well-known/ucp", f"https://{WWW}/.well-known/acp.json"]
    assert net.resolved == ["shop-a.example", WWW, WWW, WWW]  # the reused host is guarded before each request
    assert net.sleeps == [1.0] * 3


def test_the_reused_host_is_checked_again_and_refused_if_it_now_resolves_inside(monkeypatch):
    net = Net({("shop-a.example", R): _redirect(f"https://{WWW}/robots.txt"), (WWW, R): _raw(404),
               (WWW, U): _json(UCP_0825), (WWW, A): _json(ACP_MIN)})
    answers = iter([True, True, False, "unresolvable"])  # apex, www for robots, then www turns private, then gone

    def _resolver(host):
        net.resolved.append(host)
        return next(answers)

    got = _run(monkeypatch, net, ["--domains", "shop-a.example"], resolver=_resolver)
    row = got.row("shop-a.example")
    assert (row["robots"], row["ucp"], row["acp"], row["err"]) == ("absent", "not_public", "unresolvable", "ucp_not_public")
    assert net.urls() == ["https://shop-a.example/robots.txt", f"https://{WWW}/robots.txt"]
    assert net.resolved == ["shop-a.example", WWW, WWW, WWW]


def test_from_the_reused_host_the_one_hop_rule_still_holds(monkeypatch):
    net, got = _census(monkeypatch, {
        ("shop-a.example", R): _redirect(f"https://{WWW}/robots.txt"), (WWW, R): _raw(404),
        (WWW, U): _redirect("https://shop-a.example/.well-known/ucp"),  # back to the apex: the sibling, one hop
        ("shop-a.example", U): _json(UCP_EMPTY),
        (WWW, A): _redirect("https://cdn.shop-a.example/.well-known/acp.json"),
    })
    row = got.row("shop-a.example")
    assert (row["ucp"], row["ver"], row["acp"], row["err"]) == (200, "2026-01-23", "redirect_refused", "acp_redirect_refused")
    assert len(net.requests) == 5 <= census.MAX_REQUESTS_PER_DOMAIN
    assert {r.url.host for r in net.requests} == {"shop-a.example", WWW}


def test_an_unavailable_robots_after_a_redirect_reuses_nothing(monkeypatch):
    net, got = _census(monkeypatch, {("shop-a.example", R): _redirect(f"https://{WWW}/robots.txt"), (WWW, R): _raw(503),
                                     (WWW, U): _json(UCP_0825)})
    assert got.row("shop-a.example")["err"] == "robots_503" and len(net.requests) == 2


# ── the plan line and the run deadline ───────────────────────────────────────────────────────


def test_the_plan_is_printed_before_the_first_request(monkeypatch):
    net = Net({})
    lines: List[str] = []
    resolved_after: List[int] = []

    def _resolver(host):
        resolved_after.append(len(lines))
        return True

    monkeypatch.setenv(census.FLAG, "on")
    code = census.main(["--domains", "shop-a.example,https://x,shop-b.example"], out=lines.append,
                       **net.seams(resolver=_resolver))
    assert code == 0 and resolved_after[0] == 1  # one line was out before the first name was even resolved
    assert lines[0] == 'UCPPROF_PLAN {"domains":3,"max_requests":12,"pace_seconds":1,"deadline_seconds":1500}'
    assert census.DEFAULT_DEADLINE_SECONDS == 1500 and census.MAX_REQUESTS_PER_DOMAIN == 6


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "86401"],
                         ids=["zero", "negative", "nan", "infinite", "over-a-day"])
def test_a_deadline_that_is_not_a_usable_number_refuses_the_run(monkeypatch, value):
    net = Net({})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example", f"--deadline-seconds={value}"])
    assert got.code == census.EXIT_REFUSED and len(got.lines) == 1
    assert got.lines[0].startswith("UCPPROF_REFUSED bad_deadline") and net.requests == [] and net.resolved == []


FOUR = "shop-a.example,shop-b.example,shop-c.example,shop-d.example"


def _deadline_run(monkeypatch, seconds: str, **kw: Any):
    routes = {(f"shop-{x}.example", path): (_json(UCP_EMPTY) if path == U else _json(ACP_MIN))
              for x in "abcd" for path in (U, A)}
    net = Net(routes)
    got = _run(monkeypatch, net, ["--domains", FOUR, "--deadline-seconds", seconds], **kw)
    return net, got


def test_past_the_deadline_every_remaining_domain_gets_a_deadline_row(monkeypatch):
    # The fake clock moves only when the pacer sleeps: request k (from 0) goes out at second k.
    net, got = _deadline_run(monkeypatch, "5.5")  # a: 0,1,2  b: 3,4,5  c: its robots.txt would be second 6
    assert got.code == 0 and len(net.requests) == 6
    assert [r["err"] for r in got.rows] == [None, None, "deadline", "deadline"]
    for late in got.rows[2:]:
        assert late == {**census.empty_row(late["d"]), "err": "deadline"}
    assert got.summary == {**SUMMARY_ZERO, "domains": 4, "ucp_ok": 2, "acp_ok": 2, "robots_absent": 2,
                           "errors": 2, "deadline": 2}
    assert got.lines[0].endswith('"deadline_seconds":5.5}') and got.lines[-1] == census.DONE_LINE
    assert len(got.lines) == 1 + 4 + 2
    assert {r.url.host for r in net.requests} == {"shop-a.example", "shop-b.example"}


@pytest.mark.parametrize("seconds, sent, row_b", [
    ("3.5", 4, {"robots": "absent"}),
    ("4.5", 5, {"robots": "absent", "ucp": 200, "ver": "2026-01-23", "supported": ["2026-01-11"]}),
], ids=["after-robots-before-ucp", "between-ucp-and-acp"])
def test_a_deadline_in_the_middle_of_a_domain_keeps_what_was_read(monkeypatch, seconds, sent, row_b):
    net, got = _deadline_run(monkeypatch, seconds)  # b: robots.txt at second 3, ucp at 4, acp at 5
    assert got.code == 0
    assert len(net.requests) == sent  # nothing is sent once the deadline has passed
    assert net.paths("shop-b.example") == [R, U, A][:sent - 3]
    b = got.row("shop-b.example")
    assert b == {**census.empty_row("shop-b.example"), **row_b, "err": "deadline"}
    assert b["acp"] is None and b["acp_ver"] is None
    assert got.row("shop-a.example")["err"] is None
    for later in ("shop-c.example", "shop-d.example"):
        assert got.row(later) == {**census.empty_row(later), "err": "deadline"}
    assert not net.paths("shop-c.example") and not net.paths("shop-d.example")
    assert (got.summary["deadline"], got.summary["errors"], got.summary["domains"]) == (3, 3, 4)
    assert got.summary["ucp_ok"] == (2 if "ucp" in row_b else 1) and got.summary["robots_absent"] == 2
    assert got.lines[-2].startswith(census.SUM_PREFIX) and got.lines[-1] == census.DONE_LINE


def test_a_deadline_passed_while_a_request_was_in_flight_stops_before_the_next_lookup(monkeypatch):
    net = Net({})

    def handler(request):
        if request.url.host == "shop-b.example" and request.url.path == R:
            net.now += 100.0  # this answer took a very long time
        return net.handler(request)

    got = _run(monkeypatch, net, ["--domains", FOUR, "--deadline-seconds", "50"], transport=httpx.MockTransport(handler))
    assert len(net.requests) == 4 and got.code == 0
    # not one more name is resolved and not one more pace is waited for
    assert net.resolved == ["shop-a.example"] * 3 + ["shop-b.example"] and net.sleeps == [1.0] * 3
    assert got.row("shop-b.example") == {**census.empty_row("shop-b.example"), "robots": "absent", "err": "deadline"}
    assert [r["err"] for r in got.rows] == [None, "deadline", "deadline", "deadline"]
    assert got.summary["deadline"] == 3 and got.lines[-1] == census.DONE_LINE


def test_a_deadline_overrides_an_earlier_error_in_the_same_row_and_spares_refused_entries(monkeypatch):
    net = Net({("shop-a.example", U): _raw(200, b"{broken", content_type="application/json")})
    got = _run(monkeypatch, net, ["--domains", "shop-a.example,https://x,shop-b.example", "--deadline-seconds", "1.5"])
    assert got.rows[0]["ucp"] == 200 and got.rows[0]["err"] == "deadline"
    assert [r["err"] for r in got.rows] == ["deadline", "invalid_domain", "deadline"]
    assert (got.summary["deadline"], got.summary["errors"]) == (2, 3)
