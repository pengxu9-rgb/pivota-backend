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
REF = "pp_0001"
CTX = "ctx-in-answers"  # the provider echoes some contextId; never read
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
        "paymentTokenId": "ptk_8eb3e0486e5f4c689296be5d8bcff7c2", "status": "ACTIVE", "paymentBrand": "scheme_a",
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
                                              "expires_in": self.expires_in, "scope": "run"})
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


async def _send(client, component="payment.method-view", action="submit", payload=None, member=MEMBER,
                context=None):
    return await client.send_action(member_id=member, context_id=context or client.context_for(member, REF),
                                    component=component, action=action, payload=payload or {})


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
    ("payment.method-view", "charge"), ("payment.method-view", "pay"),
])
async def test_money_moving_and_foreign_actions_raise_before_any_request(component, action):
    rail = _Rail(METHOD_LIST)
    with pytest.raises(a2a.ActionNotAllowed):
        await _send(_client(rail), component, action)
    assert rail.requests == []


def test_no_allowed_action_charges_or_refunds():
    for component, action in a2a.ALLOWED_ACTIONS:
        assert not any(word in f"{component}#{action}" for word in ("charge", "pay-", "refund", "capture"))


async def test_only_a_structured_data_part_is_ever_sent():
    rail = _Rail(METHOD_LIST)
    await _send(_client(rail), payload={"list_statuses": "ACTIVE"})
    [sent] = rail.sends()
    message = sent["body"]["params"]["message"]
    assert sent["body"]["method"] == "message/send"
    assert message["role"] == "user" and message["contextId"] == _client(rail).context_for(MEMBER, REF)
    assert message["parts"] == [{"kind": "data", "data": {
        "component": "payment.method-view", "action": "submit", "payload": {"list_statuses": "ACTIVE"}}}]
    assert sent["headers"]["authorization"] == "Bearer tok-1"


@pytest.mark.parametrize("member,ctx", [("", None), ("a b", None), ("x" * 129, None), (MEMBER, ""),
                                        (MEMBER, "c\nd")])
async def test_unsafe_ids_are_refused_before_any_request(member, ctx):
    rail = _Rail(METHOD_LIST)
    client = _client(rail)
    with pytest.raises(ValueError):
        await client.send_action(member_id=member, context_id=client.context_for(MEMBER, REF) if ctx is None else ctx,
                                 component="payment.method-view", action="submit", payload={})
    assert rail.requests == []


# ── credentials never come back ──────────────────────────────────────────────────────────────


async def test_the_network_token_is_removed_before_the_card_is_returned():
    rail = _Rail(TOKEN_RESULT)
    result = await _send(_client(rail), "payment.token-setup", "submit", {"amount_cents": 100})
    assert result.ok
    card = result.card("payment.token-result")
    assert card.data == {"paymentTokenId": "ptk_8eb3e0486e5f4c689296be5d8bcff7c2", "status": "ACTIVE",
                         "paymentBrand": "scheme_a"}
    assert card.action_ids() == ("charge",)
    assert result.credential_fields_stripped >= 1
    # the text part carried the DPAN too: free text is counted, never returned
    assert result.text_parts == 1
    assert DPAN not in repr(result) and CRYPTOGRAM not in repr(result)


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
    assert result.text_parts == 1 and "email" not in repr(result)


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


def test_the_read_only_set_is_exactly_the_four_reads():
    # Pinned, not inferred: classing a write as read-only would turn its timeout into a resend.
    assert a2a.READ_ONLY_ACTIONS == {
        ("payment.method-view", "submit"), ("payment.method-add-await-card", "poll"),
        ("payment.method-add-await", "poll"), ("payment.await", "poll"),
    }
    assert a2a.READ_ONLY_ACTIONS <= a2a.ALLOWED_ACTIONS


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
    bearers = [(r["body"]["params"]["message"]["messageId"], r["headers"]["authorization"]) for r in rail.sends()]
    assert [b for _, b in bearers] == ["Bearer tok-1", "Bearer tok-1", "Bearer tok-2"]
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


# ── review hardening: shapes a denylist could miss ───────────────────────────────────────────


SPACED = "5204 7316 2006 4587"
DASHED = "5204-7316-2006-4587"


@pytest.mark.parametrize("data", [
    {"note": f"cryptogram {CRYPTOGRAM}"},
    {"note": f"card {SPACED}"},
    {"note": f"card {DASHED}"},
    {"token": {"value": int(DPAN)}},
    {"token": {"value": float(DPAN)}},
    {"cryptogramValue": "x"},
    {"primaryAccountNumber": "x"},
    {"dpan": "x"},
    {"security_code": "123"},
    {"networkTokenData": {"v": "x"}},
    {"network.token": {"v": "x"}},
    {DPAN: "keyed by the PAN"},
    {"embedded": json.dumps({"cryptogram": CRYPTOGRAM, "value": DPAN})},
])
def test_formatted_numeric_keyed_and_embedded_credentials_are_removed(data):
    cleaned, removed = a2a._strip_credentials(data)
    text = json.dumps(cleaned)
    assert removed >= 1
    for secret in (DPAN, CRYPTOGRAM, SPACED, DASHED, str(int(DPAN)), "5.204731620064587e+15"):
        assert secret not in text


def test_a_url_and_ordinary_ids_survive_the_wider_rules():
    data = {"linkUrl": "https://rail-dev.test/bind?t=QUJDREVGR0hJSktMTU5PUFFSU1Q=", "orderNo": "20261011123456",
            "createdAt": "2026-10-11 12:00:00", "amount_cents": 4650, "ts": 1760000000,
            # a 32-hex id whose 14-digit run fails Luhn, a ms epoch, a digits-only time, a 19-digit order id
            "paymentTokenId": "ptk_a12345678901234bcdef0123456789ab", "createdAtMs": 1760000000123,
            "updatedAt": "20261011123456", "orderId": 1234567890123456789,
            "bindUrl": "https://rail-dev.test/bind?t=1760000000123&n=12345678901234"}
    cleaned, removed = a2a._strip_credentials(data)
    assert cleaned == data and removed == 0


def test_a_reference_key_keeps_its_digits_and_nothing_else_does():
    cleaned, removed = a2a._strip_credentials({"orderNo": "20261011123456", "note": "order 20261011123456",
                                               "merchant_order_ref": "20261011123456",
                                               "cardNumber": "20261011123456", "orderId": CRYPTOGRAM})
    assert cleaned == {"orderNo": "20261011123456", "note": None,
                       "merchant_order_ref": "20261011123456", "orderId": None}
    assert removed == 3


@pytest.mark.parametrize("key", ["cardRef", "tokenReference", "panRef", "card_number_ref",
                                 "accountNumberReference", "createdAt_card", "cardIssuedAt", "tokenTs"])
def test_a_card_word_in_a_reference_or_time_key_does_not_spare_a_pan(key):
    cleaned, removed = a2a._strip_credentials({key: DPAN})
    assert removed == 1 and DPAN not in json.dumps(cleaned)


def test_a_cryptogram_used_as_a_key_is_removed():
    cleaned, removed = a2a._strip_credentials({CRYPTOGRAM: "x", "ok": 1})
    assert cleaned == {"ok": 1} and removed == 1


def test_a_value_with_a_credential_is_dropped_whole_not_patched():
    cleaned, _ = a2a._strip_credentials({"id": f"ptk_{DPAN}", "linkUrl": f"https://rail-dev.test/b?pan={DPAN}"})
    assert cleaned == {"id": None, "linkUrl": None}


def test_nesting_past_the_depth_cap_is_dropped_not_recursed():
    deep = current = {}
    for _ in range(3000):
        current["n"] = {}
        current = current["n"]
    current["value"] = DPAN
    cleaned, removed = a2a._strip_credentials(deep)
    assert removed >= 1 and DPAN not in json.dumps(cleaned)


async def test_identifiers_carrying_a_pan_are_dropped():
    body = _task({"kind": "data", "data": {"component": f"x.{DPAN}", "kind": DPAN, "data": {},
                                           "actions": [{"id": DPAN}, {"id": "ok-action", "role": DPAN}]}})
    result = await _send(_client(_Rail(body)))
    assert DPAN not in repr(result)
    assert result.cards[0].action_ids() == ("ok-action",)


async def test_a_body_nested_too_deep_to_parse_is_classified_not_raised():
    bomb = httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000)
    rail = _Rail(bomb, bomb)
    client = _client(rail)
    assert (await _send(client)).kind == "protocol_error"
    assert (await _send(client, "payment.token-setup", "submit")).kind == "outcome_unknown"


# ── review hardening: money flows ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("component", ["payment.pay-result", "payment.card-result", "payment.charge-token-result",
                                       "payment.refund-result"])
async def test_a_money_result_card_is_never_ok_and_is_logged_as_an_error(caplog, component):
    caplog.set_level(logging.INFO, logger=a2a.__name__)
    body = _task(_card(component, {"chargeNo": "c1", "status": "SUCCEEDED"}))
    result = await _send(_client(_Rail(body)), "payment.await", "poll")
    assert result.kind == "money_result" and not result.ok
    assert any(r.levelno == logging.ERROR and "money-result" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("context", [
    "pp-ctx-0001",                        # a plain string
    "pvt-pp_0001-0000000000000000",       # right shape, wrong signature
])
async def test_a_conversation_this_client_did_not_sign_is_refused_before_any_request(context):
    rail = _Rail(METHOD_LIST)
    with pytest.raises(ValueError):
        await _send(_client(rail), "payment.action-required", "paid", context=context)
    assert rail.requests == []


async def test_a_context_signed_under_another_key_is_refused():
    rail = _Rail(METHOD_LIST)
    other = a2a.A2ARailClient(BASE, "sk_test_some_other_key_99", transport=httpx.MockTransport(rail.handler))
    with pytest.raises(ValueError):
        await _send(_client(rail), context=other.context_for(MEMBER, REF))
    assert rail.requests == []


async def test_a_conversation_is_bound_to_its_member():
    rail = _Rail(METHOD_LIST)
    client = _client(rail)
    with pytest.raises(ValueError):
        await _send(client, member="pivota-buyer-ref-2", context=client.context_for(MEMBER, REF))
    assert rail.requests == []


async def test_a_dedicated_context_secret_survives_an_api_key_rotation():
    rail = _Rail(METHOD_LIST)
    before = a2a.A2ARailClient(BASE, API_KEY, context_secret="ctx-secret-1")
    after = a2a.A2ARailClient(BASE, "sk_test_rotated_key_000000", context_secret="ctx-secret-1",
                              transport=httpx.MockTransport(rail.handler))
    assert (await _send(after, context=before.context_for(MEMBER, REF))).ok


def test_from_env_reads_the_context_secret(monkeypatch):
    monkeypatch.setenv(a2a.A2A_RAIL_ENABLED_ENV, "1")
    monkeypatch.setenv(a2a.A2A_RAIL_BASE_URL_ENV, BASE)
    monkeypatch.setenv(a2a.A2A_RAIL_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(a2a.A2A_RAIL_CONTEXT_SECRET_ENV, "ctx-secret-1")
    explicit = a2a.A2ARailClient(BASE, "another-key", context_secret="ctx-secret-1")
    assert a2a.from_env().context_for(MEMBER, REF) == explicit.context_for(MEMBER, REF)


async def test_the_echoed_context_and_task_state_are_validated():
    body = _task(_card("payment.method-list", {"methods": []}), state=DPAN)
    body["result"]["contextId"] = "pvt-pp_0001-0123456789abcdef"
    result = await _send(_client(_Rail(body)))
    assert result.task_state is None and result.context_id == "pvt-pp_0001-0123456789abcdef"
    body = _task(_card("payment.method-list", {"methods": []}))
    body["result"]["contextId"] = "bad id with spaces"
    assert (await _send(_client(_Rail(body)))).context_id is None


async def test_the_token_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(a2a, "_TOKEN_CACHE_MAX", 2)
    rail = _Rail(METHOD_LIST, METHOD_LIST, METHOD_LIST)
    client = _client(rail)
    for member in ("m1", "m2", "m3"):
        await _send(client, member=member)
    assert len(client._tokens) == 2 and "m3" in client._tokens


async def test_an_exchange_outlives_a_caller_that_gives_up_and_leaves_nothing_behind():
    import asyncio

    release = asyncio.Event()

    async def handler(request):
        if request.url.path == a2a.TOKEN_PATH:
            await release.wait()
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json=METHOD_LIST)

    client = a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(handler))
    impatient = asyncio.ensure_future(client._access_token("m1"))
    await asyncio.sleep(0)
    impatient.cancel()
    patient = asyncio.ensure_future(client._access_token("m1"))
    await asyncio.sleep(0)
    release.set()
    assert await patient == "tok-1"
    assert client._minting == {} and client._tokens["m1"][0] == "tok-1"


@pytest.mark.parametrize("ref", ["", "a-b", "x" * 81, "a\nb"])
def test_context_refs_are_strict(ref):
    with pytest.raises(ValueError):
        _client(_Rail()).context_for(MEMBER, ref)


# ── review hardening: a maybe-write without a definite answer ────────────────────────────────


@pytest.mark.parametrize("answer", [
    httpx.Response(500), httpx.Response(502), httpx.Response(503), httpx.Response(504), httpx.Response(408),
    httpx.Response(200, content=b"<html>gateway</html>"),
    httpx.Response(200, content=b"x" * (a2a.MAX_BODY_BYTES + 10)),
    RuntimeError("not an httpx error"),
])
async def test_a_write_without_a_definite_answer_is_outcome_unknown(answer):
    assert (await _send(_client(_Rail(answer)), "payment.token-setup", "submit")).kind == "outcome_unknown"


@pytest.mark.parametrize("status", [400, 403, 404, 409, 422, 429])
async def test_a_4xx_on_a_write_is_a_definite_refusal(status):
    result = await _send(_client(_Rail(httpx.Response(status))), "payment.token-setup", "submit")
    assert result.kind == "http_error" and result.http_status == status and result.resend_safe


@pytest.mark.parametrize("status", [201, 202, 204, 301, 302, 303, 307])
async def test_a_non_200_success_or_redirect_on_a_write_is_outcome_unknown(status):
    result = await _send(_client(_Rail(httpx.Response(status))), "payment.token-setup", "submit")
    assert result.kind == "outcome_unknown" and not result.resend_safe


async def test_resend_safety_is_reported_per_result():
    read = await _send(_client(_Rail(httpx.ReadTimeout("slow"))))
    text_on_write = await _send(_client(_Rail(EMAIL_QUESTION)), "payment.method-add", "submit")
    assert read.resend_safe and not read.may_write
    assert text_on_write.kind == "unexpected_text" and not text_on_write.resend_safe


async def test_a_5xx_on_a_read_stays_an_http_error():
    assert (await _send(_client(_Rail(httpx.Response(503))))).kind == "http_error"


async def test_a_401_on_a_write_is_not_resent():
    rail = _Rail(httpx.Response(401, json={"error": "expired"}))
    result = await _send(_client(rail), "payment.token-setup", "submit", {"amount_cents": 100})
    assert result.kind == "http_error" and result.http_status == 401
    assert len(rail.sends()) == 1


async def test_a_failed_task_with_a_card_is_not_ok():
    result = await _send(_client(_Rail(_task(_card("payment.method-list", {"methods": []}), state="failed"))))
    assert result.kind == "cards" and result.task_state == "failed" and not result.ok


async def test_the_answer_is_read_from_status_message_when_history_ends_with_the_user():
    task = _task()
    task["result"]["history"] = [{"role": "user", "kind": "message", "messageId": "u1", "parts": []}]
    task["result"]["status"]["message"] = {"role": "agent", "kind": "message", "messageId": "m2",
                                           "parts": [_card("payment.method-list", {"methods": []})]}
    result = await _send(_client(_Rail(task)))
    assert result.ok and result.card("payment.method-list") is not None


# ── review hardening: tokens ─────────────────────────────────────────────────────────────────


async def test_a_redirect_on_the_token_exchange_is_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    client = a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(handler))
    assert (await _send(client)).kind == "auth_failed"
    assert calls == [BASE + a2a.TOKEN_PATH]


async def test_a_token_without_expiry_is_never_reused():
    rail = _Rail(METHOD_LIST, METHOD_LIST, expires_in=None)
    client = _client(rail)
    await _send(client)
    await _send(client)
    assert rail.token_calls == 2


async def test_one_slow_member_mint_does_not_serialise_other_members():
    import asyncio

    in_flight, peak, mints = [0], [0], []

    async def handler(request):
        if request.url.path == a2a.TOKEN_PATH:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await asyncio.sleep(0.05)
            in_flight[0] -= 1
            mints.append(json.loads(request.content)["member_id"])
            return httpx.Response(200, json={"access_token": f"tok-{len(mints)}", "expires_in": 3600})
        return httpx.Response(200, json=METHOD_LIST)

    client = a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(handler))
    await asyncio.gather(_send(client, member="m1"), _send(client, member="m2"), _send(client, member="m1"))
    assert peak[0] == 2                      # m1 and m2 minted at the same time
    assert sorted(mints) == ["m1", "m2"]     # the second m1 call waited for and reused m1's token


async def test_the_http_client_ignores_environment_proxies_and_netrc(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    async with _client(_Rail())._client() as http:
        assert http.trust_env is False and not http.follow_redirects


@pytest.mark.parametrize("data", [
    {"paymentCryptogram": "AOHHzRvcAAAAAAAAAAAAAAAAAAAA"},   # unpadded
    {"cavv": "AAABBBCCCDDDEEEFFF00112233"},
    {"authenticationValue": "x"},
    {"auth": {"eci": "05", "ucaf": "x"}},
    {"tokenNumber": "4111 0000 0000 0001"},                 # not Luhn, still a token number
    {"token": {"number": "4111000000000001", "expiry": "1129"}},
    {"token": [{"value": "opaque"}]},
])
def test_named_authentication_values_and_token_numbers_are_removed(data):
    cleaned, removed = a2a._strip_credentials(data)
    text = json.dumps(cleaned)
    assert removed >= 1
    for secret in ("AOHHzRvc", "AAABBBCCC", '"x"', "4111", "opaque", '"05"'):
        assert secret not in text


def test_token_ids_and_metadata_are_not_mistaken_for_token_numbers():
    data = {"paymentTokenId": "ptk_8eb3e0486e5f4c689296be5d8bcff7c2", "token": {"id": "ptk_1", "status": "ACTIVE"},
            "recipient": "shop", "decimalPlaces": 2}
    cleaned, removed = a2a._strip_credentials(data)
    assert cleaned == data and removed == 0


async def test_a_money_result_card_in_the_artifacts_is_caught():
    body = _task(_card("payment.method-list", {"methods": []}))
    body["result"]["artifacts"] = [{"artifactId": "a1", "parts": [
        {"kind": "data", "data": {"component": "payment.pay-result", "data": {"chargeNo": "c1"}}}]}]
    result = await _send(_client(_Rail(body)), "payment.await", "poll")
    assert result.kind == "money_result" and not result.ok


async def test_a_drip_fed_answer_is_cut_by_the_total_deadline():
    import asyncio

    async def drip(request):
        if request.url.path == a2a.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

        async def body():
            for _ in range(100):
                await asyncio.sleep(0.02)
                yield b" "

        return httpx.Response(200, content=body())

    client = a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(drip), total_timeout_s=0.1)
    started = asyncio.get_running_loop().time()
    write = await _send(client, "payment.token-setup", "submit")
    read = await _send(client)
    assert write.kind == "outcome_unknown" and read.kind == "timeout"
    assert asyncio.get_running_loop().time() - started < 1.5


async def test_a_token_exchange_that_hangs_is_cut():
    import asyncio

    async def hang(request):
        await asyncio.sleep(30)

    client = a2a.A2ARailClient(BASE, API_KEY, transport=httpx.MockTransport(hang), total_timeout_s=0.05)
    started = asyncio.get_running_loop().time()
    assert (await _send(client)).kind == "auth_failed"
    assert asyncio.get_running_loop().time() - started < 5


async def test_an_echoed_context_carrying_a_card_number_is_dropped():
    body = _task(_card("payment.method-list", {"methods": []}))
    body["result"]["contextId"] = f"ctx-{DPAN}"
    assert (await _send(_client(_Rail(body)))).context_id is None
