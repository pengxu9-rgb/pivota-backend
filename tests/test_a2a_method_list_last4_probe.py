"""scripts/ops/a2a_method_list_last4_probe.py: the read-only prod probe of the A2A rail's card list.

Against a mock transport only (no network): the host pin, the refusals before any call, the one read it sends, and
that its output carries counts and booleans and never a value.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.payment_orchestration import a2a_rail  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "ops", "a2a_method_list_last4_probe.py")
_spec = importlib.util.spec_from_file_location("a2a_method_list_last4_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
# As shipped, before any fixture patches them.
SHIPPED_DIGESTS = a2a_rail.ALLOWED_HOST_SHA256
PINNED_DIGEST = probe.PRODUCTION_HOST_SHA256

PROD = "rail-prod.test"
TEST_HOST = "rail-dev.test"
KEY = "sk_live_probe_key_0123456789"
MEMBER = "member_probe_0042"
CARDS = [
    {"id": "pm_secret_one", "status": "ACTIVE", "payment_brand": "visa", "last4": "4821", "created_at": "2026-11-01T00:00:00Z"},
    {"id": "pm_secret_two", "status": "DISABLED", "payment_brand": "visa", "last4": "77", "created_at": "2026-10-01T00:00:00Z"},
    {"id": "pm_secret_three", "status": "ACTIVE"},
]


def _digest(host: str) -> str:
    return hashlib.sha256(host.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _hosts(monkeypatch):
    # The rail client allows both hosts (as it ships: production and test); the probe pins production only.
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({_digest(PROD), _digest(TEST_HOST)}))
    monkeypatch.setattr(probe, "PRODUCTION_HOST_SHA256", _digest(PROD))
    monkeypatch.setenv(a2a_rail.A2A_RAIL_API_KEY_ENV, KEY)


def _task(methods):
    parts = [{"kind": "data", "data": {"component": "payment.method-list", "kind": "list",
                                       "data": {"methods": methods}, "actions": []}}]
    return {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", "id": "t", "contextId": "c",
            "status": {"state": "completed"}, "history": [{"role": "agent", "parts": parts}]}}


class Rail:
    def __init__(self, answer):
        self.answer = answer
        self.sent = []
        self.hosts = set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.add(request.url.host)
        if request.url.path == a2a_rail.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok_secret", "expires_in": 3600})
        body = json.loads(request.content)
        data = body["params"]["message"]["parts"][0]["data"]
        self.sent.append((f"{data['component']}#{data['action']}", data["payload"]))
        return self.answer if isinstance(self.answer, httpx.Response) else httpx.Response(200, json=self.answer)


def _run(rail, *argv, capsys):
    code = probe.main(list(argv), transport=httpx.MockTransport(rail.handler))
    return code, capsys.readouterr().out


def test_it_reads_the_card_list_once_and_prints_counts_and_booleans_only(capsys):
    rail = Rail(_task(CARDS))
    code, out = _run(rail, "--base", f"https://{PROD}", "--member", MEMBER, capsys=capsys)
    assert code == 0
    assert rail.sent == [("payment.method-view#submit", {"list_statuses": probe.LIST_ALL})]
    assert rail.hosts == {PROD}
    lines = out.strip().splitlines()
    assert lines[-1] == "A2A_LAST4_DONE"
    report = json.loads(lines[0][len("A2A_LAST4 "):])
    assert report == {
        "read": "ok", "methods": 3, "with_brand": 2, "with_last4_4_digits": 1, "active": 2,
        "active_with_last4_4_digits": 1,
        "per_method": [
            {"is_object": True, "brand": True, "last4_4_digits": True, "active": True},
            {"is_object": True, "brand": True, "last4_4_digits": False, "active": False},
            {"is_object": True, "brand": False, "last4_4_digits": False, "active": True},
        ],
    }
    # Never a value: no last4, brand, card id, member, token or key.
    for value in ("4821", "77\"", "visa", "pm_secret", MEMBER, "tok_secret", KEY):
        assert value not in out, value


@pytest.mark.parametrize("base", [
    f"https://{TEST_HOST}",          # the rail's test host: allowed by the client, refused here
    "https://elsewhere.test",
    f"http://{PROD}",
    f"https://{PROD}:8443",
    f"https://{PROD}/v1",
    f"https://user@{PROD}",
    f"https://{PROD}?x=1",
    "",
])
def test_anything_but_the_production_origin_is_refused_before_any_call(capsys, base):
    rail = Rail(_task(CARDS))
    code, out = _run(rail, "--base", base, "--member", MEMBER, capsys=capsys)
    assert code == 3 and out.startswith("A2A_LAST4_REFUSED")
    assert rail.sent == [] and rail.hosts == set()


def test_a_missing_key_or_a_bad_member_is_refused_before_any_call(monkeypatch, capsys):
    rail = Rail(_task(CARDS))
    code, out = _run(rail, "--base", f"https://{PROD}", "--member", "bad member!", capsys=capsys)
    assert code == 3 and "member" in out and rail.hosts == set()
    monkeypatch.delenv(a2a_rail.A2A_RAIL_API_KEY_ENV)
    code, out = _run(rail, "--base", f"https://{PROD}", "--member", MEMBER, capsys=capsys)
    assert code == 3 and "not mounted" in out and rail.hosts == set()


@pytest.mark.parametrize("answer, read", [
    (httpx.Response(500, text="boom 4821 visa"), "failed"),
    # A good answer that carries another card than the method list.
    ({"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", "id": "t", "contextId": "c",
      "status": {"state": "completed"}, "history": [{"role": "agent", "parts": [{"kind": "data", "data": {
          "component": "payment.method-add-required-card", "kind": "detail",
          "data": {"linkUrl": "https://rail-prod.test/bind?t=1", "status": "PENDING"}, "actions": []}}]}]}},
     "no_method_list"),
])
def test_a_failed_read_says_what_failed_and_nothing_else(capsys, answer, read):
    rail = Rail(answer)
    code, out = _run(rail, "--base", f"https://{PROD}", "--member", MEMBER, capsys=capsys)
    assert code == 2
    report = json.loads(out.strip().splitlines()[0][len("A2A_LAST4 "):])
    assert report["read"] == read
    for value in ("4821", "visa", MEMBER, KEY):
        assert value not in out


def test_the_pinned_digest_is_one_the_rail_client_ships_with():
    # The production host is one of the two the rail client allows; the probe pins exactly that one.
    assert PINNED_DIGEST in SHIPPED_DIGESTS
    assert len(SHIPPED_DIGESTS) == 2


def test_an_unexpected_error_prints_its_type_only(monkeypatch, capsys):
    async def boom(*args, **kwargs):
        raise RuntimeError(f"leaked {KEY} 4821 {MEMBER}")

    monkeypatch.setattr(probe, "probe", boom)
    code = probe.main(["--base", f"https://{PROD}", "--member", MEMBER])
    out = capsys.readouterr().out
    assert code == 2
    assert json.loads(out.strip().splitlines()[0][len("A2A_LAST4 "):]) == {"read": "error", "error_type": "RuntimeError"}
    for value in (KEY, "4821", MEMBER, "leaked"):
        assert value not in out
