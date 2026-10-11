"""services/payment_orchestration/a2a_rail.py: the A2A rail transport, offline.

Every response fixture below has the shape measured against the provider's sandbox on 2026-10-11
(`result.kind == "task"`, the agent's message in `history`, a text part plus a data card). Card
values are fake. The host allowlist is swapped for two test hosts (`_test_hosts`); the built-in
digests are checked on their own below. An httpx MockTransport stands in for the provider and records every request, so the
tests can assert both what comes back and what was (or was not) sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.payment_orchestration.a2a_rail as a2a  # noqa: E402

#: The allowlist as shipped, read before the autouse fixture swaps it.
SHIPPED_DIGESTS = a2a.ALLOWED_HOST_SHA256

BASE = "https://rail-dev.test"
TEST_HOSTS = ("rail.test", "rail-dev.test")
API_KEY = "sk_test_fixture_key_0123456789"
MEMBER = "pivota-buyer-ref-1"
CTX = "pp-ctx-0001"
DPAN = "5204731620064587"
CRYPTOGRAM = "AOHHzRvcAAAAAAAAAAAAAAAAAAAAAA=="


@pytest.fixture(autouse=True)
def _test_hosts(monkeypatch):
    digests = frozenset(hashlib.sha256(h.encode()).hexdigest() for h in TEST_HOSTS)
    monkeypatch.setattr(a2a, "ALLOWED_HOST_SHA256", digests)


def _task(*parts, state="completed"):
    return {
        "jsonrpc": "2.0", "id": "rpc-1",
        "result": {
            "kind": "task", "id": "task-1", "contextId": CTX, "status": {"state": state},
            "artifacts": [],
            "history": [{"role": "agent", "kind": "message", "messageId": "m1", "contextId": CTX,
                         "taskId": "task-1", "parts": list(parts)}],
        },
    }


def _text(text):
    return {"kind": "text", "text": text}


def _card(component, data, actions=(), kind="detail"):
    return {"kind": "data", "data": {"component": component, "kind": kind, "data": data,
                                     "actions": [{"id": a, "dispatch": "agent"} for a in actions]}}


METHOD_LIST = _task(
    _text("[Payment Method List] — here are the available options:\n  No options available."),
    _card("payment.method-list", {"methods": []}, ("select-method", "add-method"), kind="list"),
)
TOKEN_RESULT = _task(
    _text(f"[Payment Token Result] — details:\n  - Payment Token Id: ptk_x\n  - Network Token: {DPAN}"),
    _card("payment.token-result", {
        "paymentTokenId": "ptk_8eb3e0486e5f4c689296be5d8bcff7c2", "status": "ACTIVE", "paymentBrand": "evo",
        "networkToken": {"brand": "Mastercard", "eci": "06", "cryptogram": CRYPTOGRAM,
                         "expiryDate": "1129", "value": DPAN},
    }, ("charge",)),
)
EMAIL_QUESTION = _task(_text("I'd be happy to help you with your booking! **What's your email address?**"))
ERROR_CARD = _task(_card("error", {"code": "PRICE_CHANGED", "message": "x", "recoverable": True}))


class _Rail:
    """A fake rail provider: the token endpoint plus a queue of message:send answers."""

    def __init__(self, *answers, token_status=200, expires_in=3600):
        self.answers = list(answers)
        self.requests = []
        self.token_calls = 0
        self.token_status = token_status
        self.expires_in = expires_in

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if request.url.path == a2a.TOKEN_PATH:
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "INVALID_API_KEY"})
            return httpx.Response(200, json={"access_token": f"tok-{self.token_calls}", "token_type": "bearer",
                                              "expires_in": self.expires_in, "scope": "agents:run"})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def sends(self):
        return [r for r in self.requests if r["url"].endswith(a2a.MESSAGE_PATH)]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _client(rail, clock=None, **kw):
    return a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(rail.handler),
                               clock=clock or _Clock(), **kw)


async def _send(client, component="payment.method-view", action="submit", payload=None, member=MEMBER):
    return await client.send_action(member_id=member, context_id=CTX, component=component,
                                    action=action, payload=payload or {})


# ── configuration ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://rail-dev.test", "https://rail-dev.test.evil.example", "https://evil.example",
    "https://user:pw@rail.test", "https://rail.test:8443", "https://rail.test/a2a", "",
])
def test_only_a_bare_https_allowed_origin_is_accepted(url):
    with pytest.raises(a2a.A2ARailConfigError):
        a2a.validate_base_url(url)


def test_the_allowed_hosts_are_accepted_case_insensitively():
    assert a2a.validate_base_url("https://rail.test/") == "https://rail.test"
    assert a2a.validate_base_url("https://RAIL-DEV.test") == "https://rail-dev.test"


def test_the_shipped_allowlist_is_two_sha256_digests_and_rejects_other_hosts(monkeypatch):
    assert len(SHIPPED_DIGESTS) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in SHIPPED_DIGESTS)
    monkeypatch.setattr(a2a, "ALLOWED_HOST_SHA256", SHIPPED_DIGESTS)
    for host in TEST_HOSTS + ("evil.example",):
        with pytest.raises(a2a.A2ARailConfigError):
            a2a.validate_base_url(f"https://{host}")


def test_from_env_is_dark_by_default_and_loud_when_misconfigured(monkeypatch):
    monkeypatch.delenv(a2a.A2A_RAIL_ENABLED_ENV, raising=False)
    assert a2a.from_env() is None
    monkeypatch.setenv(a2a.A2A_RAIL_ENABLED_ENV, "1")
    monkeypatch.setenv(a2a.A2A_RAIL_BASE_URL_ENV, "https://evil.example")
    monkeypatch.setenv(a2a.A2A_RAIL_API_KEY_ENV, API_KEY)
    with pytest.raises(a2a.A2ARailConfigError):
        a2a.from_env()
    monkeypatch.setenv(a2a.A2A_RAIL_BASE_URL_ENV, BASE)
    monkeypatch.setenv(a2a.A2A_RAIL_API_KEY_ENV, " ")
    with pytest.raises(a2a.A2ARailConfigError):
        a2a.from_env()


# ── what may be sent ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("component,action", [
    ("payment.pay-setup", "submit"), ("payment.pay-confirm", "confirm"),
    ("payment.charge-token-setup", "submit"), ("payment.charge-token-confirm", "confirm"),
    ("payment.token-result", "charge"), ("payment.refund-setup", "submit"),
    ("payment.refund-confirm", "confirm"), ("payment.card-action-required", "paid"),
    ("trip.checkout-confirm", "confirm"), ("hotel.booking-confirm", "confirm"),
    ("payment.method-view", "charge"),
])
async def test_money_moving_and_foreign_actions_raise_before_any_request(component, action):
    rail = _Rail(METHOD_LIST)
    with pytest.raises(a2a.ActionNotAllowed):
        await _send(_client(rail), component, action)
    assert rail.requests == []


def test_no_allowed_action_charges_or_refunds():
    for component, action in a2a.ALLOWED_ACTIONS:
        assert not any(word in f"{component}#{action}" for word in ("charge", "pay-", "refund", "trip.", "capture"))


async def test_only_a_structured_data_part_is_ever_sent():
    rail = _Rail(METHOD_LIST)
    await _send(_client(rail), payload={"list_statuses": "ACTIVE"})
    [sent] = rail.sends()
    message = sent["body"]["params"]["message"]
    assert sent["body"]["method"] == "message/send"
    assert message["role"] == "user" and message["contextId"] == CTX
    assert message["parts"] == [{"kind": "data", "data": {
        "component": "payment.method-view", "action": "submit", "payload": {"list_statuses": "ACTIVE"}}}]
    assert sent["headers"]["authorization"] == "Bearer tok-1"


@pytest.mark.parametrize("member,ctx", [("", CTX), ("a b", CTX), ("x" * 129, CTX), (MEMBER, ""), (MEMBER, "c\nd")])
async def test_unsafe_ids_are_refused_before_any_request(member, ctx):
    rail = _Rail(METHOD_LIST)
    with pytest.raises(ValueError):
        await _client(rail).send_action(member_id=member, context_id=ctx, component="payment.method-view",
                                          action="submit", payload={})
    assert rail.requests == []


# ── credentials never come back ──────────────────────────────────────────────────────────────


async def test_the_network_token_is_removed_before_the_card_is_returned():
    rail = _Rail(TOKEN_RESULT)
    result = await _send(_client(rail), "payment.token-setup", "submit", {"amount_cents": 100})
    assert result.ok
    card = result.card("payment.token-result")
    assert card.data == {"paymentTokenId": "ptk_8eb3e0486e5f4c689296be5d8bcff7c2", "status": "ACTIVE",
                         "paymentBrand": "evo"}
    assert card.action_ids() == ("charge",)
    assert result.credential_fields_stripped >= 2
    everything = json.dumps([c.data for c in result.cards]) + json.dumps(result.texts)
    assert DPAN not in everything and CRYPTOGRAM not in everything


@pytest.mark.parametrize("data", [
    {"network_token": {"value": DPAN}},
    {"deep": [{"NetworkToken": {"value": DPAN}}]},
    {"card": {"cardNumber": DPAN, "cvv": "123"}},
    {"x": {"token_cryptogram": CRYPTOGRAM}},
    {"payment": {"cryptogram": CRYPTOGRAM, "eci": "06"}},
    {"tavv": CRYPTOGRAM},
    {"note": f"pan {DPAN} here"},
])
def test_credential_shapes_are_removed_at_any_depth(data):
    cleaned, removed = a2a._strip_credentials(data)
    assert removed >= 1
    assert DPAN not in json.dumps(cleaned) and CRYPTOGRAM not in json.dumps(cleaned)


def test_ordinary_ids_and_amounts_survive():
    data = {"paymentMethodId": "abd1b96c3f0348ed8918176e4edf9b99", "amount_cents": 4650, "last4": "6905",
            "first6": "512035", "linkUrl": "https://rail-dev.test/x?t=abc"}
    cleaned, removed = a2a._strip_credentials(data)
    assert cleaned == data and removed == 0


async def test_logs_never_carry_the_credential_member_or_key(caplog):
    caplog.set_level(logging.INFO, logger=a2a.__name__)
    rail = _Rail(TOKEN_RESULT)
    await _send(_client(rail), "payment.token-setup", "submit", {"amount_cents": 100})
    text = caplog.text
    for secret in (DPAN, CRYPTOGRAM, MEMBER, API_KEY, "tok-1"):
        assert secret not in text
    assert "credential field" in text


# ── classification ───────────────────────────────────────────────────────────────────────────


async def test_free_text_where_a_card_was_expected_fails_closed():
    result = await _send(_client(_Rail(EMAIL_QUESTION)), "payment.method-add", "submit")
    assert result.kind == "unexpected_text" and not result.ok
    assert "email" in result.texts[0]


async def test_an_error_card_is_named():
    result = await _send(_client(_Rail(ERROR_CARD)))
    assert result.kind == "error_card" and result.error_code == "PRICE_CHANGED" and result.recoverable is True


@pytest.mark.parametrize("body", [{"jsonrpc": "2.0", "error": {"code": -32601, "message": "x"}},
                                  {"result": {"kind": "message"}}, [1, 2], {"result": "x"}])
async def test_protocol_shapes_that_are_not_a_task_are_protocol_errors(body):
    result = await _send(_client(_Rail(body)))
    assert result.kind == "protocol_error"


async def test_a_timeout_on_a_maybe_write_is_outcome_unknown_never_a_retryable_timeout():
    rail = _Rail(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    client = _client(rail)
    write = await _send(client, "payment.token-setup", "submit", {"amount_cents": 100})
    read = await _send(client, "payment.method-view", "submit")
    assert write.kind == "outcome_unknown"
    assert read.kind == "timeout"


async def test_a_transport_error_on_a_maybe_write_is_outcome_unknown():
    rail = _Rail(httpx.ConnectError("reset"), httpx.ConnectError("reset"))
    client = _client(rail)
    assert (await _send(client, "payment.method-add", "submit")).kind == "outcome_unknown"
    assert (await _send(client, "payment.await", "poll")).kind == "transport"


def test_every_read_only_action_is_allowed_and_nothing_else_is_read_only():
    assert a2a.READ_ONLY_ACTIONS <= a2a.ALLOWED_ACTIONS
    for component, action in a2a.READ_ONLY_ACTIONS:
        assert action in ("submit", "poll") and ("view" in component or "await" in component)


async def test_a_redirect_is_not_followed():
    rail = _Rail(httpx.Response(302, headers={"Location": "https://evil.example/steal"}))
    result = await _send(_client(rail))
    assert result.kind == "http_error" and result.http_status == 302
    assert all("evil.example" not in r["url"] for r in rail.requests)


async def test_an_oversized_body_is_refused():
    rail = _Rail(httpx.Response(200, content=b"x" * (a2a.MAX_BODY_BYTES + 10)))
    assert (await _send(_client(rail))).kind == "too_large"


# ── tokens ───────────────────────────────────────────────────────────────────────────────────


async def test_one_token_per_member_reused_until_near_expiry():
    clock = _Clock()
    rail = _Rail(METHOD_LIST, METHOD_LIST, METHOD_LIST, METHOD_LIST)
    client = _client(rail, clock=clock)
    await _send(client)
    await _send(client)
    assert rail.token_calls == 1
    await _send(client, member="pivota-buyer-ref-2")
    assert rail.token_calls == 2
    clock.now += 3600 - a2a.TOKEN_REFRESH_MARGIN_S + 1
    await _send(client)
    assert rail.token_calls == 3


async def test_a_refused_token_exchange_sends_nothing_and_caches_nothing():
    rail = _Rail(METHOD_LIST, token_status=401)
    client = _client(rail)
    assert (await _send(client)).kind == "auth_failed"
    assert rail.sends() == []
    rail.token_status = 200
    assert (await _send(client)).ok


async def test_a_401_on_send_drops_the_cached_token():
    rail = _Rail(httpx.Response(401, json={"error": "expired"}), METHOD_LIST)
    client = _client(rail)
    assert (await _send(client)).kind == "http_error"
    assert (await _send(client)).ok
    assert rail.token_calls == 2
