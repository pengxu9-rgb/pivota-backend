"""The A2A rail's checkout relay: the approved network token goes into the provider's checkout skill.

Two halves, both offline (httpx MockTransport stands in for the provider; nothing real is called):

  the transport (services/payment_orchestration/a2a_rail.py): the token is used only inside
      `relay_token_to_checkout`, in the one request that carries it back to the provider; every answer
      comes back stripped; `checkout.setup#submit` cannot be sent any other way; the checkout writes
      and the production token step follow the checkout dial (rules 1, 6, 7).
  the worker (services/payment_orchestration/a2a_rail_worker.py), on SQLite through the REAL client:
      approval -> setup -> paying -> confirm (a money write, recorded first, never resent) -> poll /
      lookup -> completed / failed / held; and with the dial off, exactly the pre-checkout behaviour.

Every needle loop below looks for the DPAN and the cryptogram in what Pivota keeps or shows: the
ledger rows, the logs, the outcomes, the agent view, exception text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
from services.payment_orchestration import a2a_purchase_view as view  # noqa: E402
from services.payment_orchestration import a2a_rail, a2a_rail_worker as worker  # noqa: E402
from services.payment_orchestration import router as rail_router  # noqa: E402

# The SQLite arm; tests/test_a2a_rail_checkout_postgres.py runs these same tests on Postgres.
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

HOST = "rail-dev.test"
BASE = f"https://{HOST}"
KEY = "sk_test_checkout_key_0123456789"
W = "w1"
EMAIL = "buyer@example.test"
#: A Luhn-valid DPAN and a cryptogram-shaped blob (fake values).
DPAN = "4895370012003478"
CRYPTO = "AgAAAAAABk4DWZ4C28yUQAAAAAA="
#: The 3-digit dynamic CVV the token is minted with on the checkout path (`dynamic_data_type: "DAVV"`), in its
#: own field (`a2a_rail.DAVV_FIELD`). The cryptogram (`CRYPTO`, the TAVV) is never sent and never used as cvv.
DAVV = "715"


def _no_davv_in(*texts):
    """The DAVV as a VALUE (quoted, as JSON or a repr would carry it) is in none of `texts`. (A bare "715" also
    occurs inside hex ids and timings, so the quoted value is what a leak looks like.)"""
    for text in texts:
        for form in (f'"{DAVV}"', f"'{DAVV}'", f": {DAVV},", f"={DAVV} "):
            assert form not in text, form
NEEDLES = (DPAN, CRYPTO)
APPROVAL = f"{BASE}/approve?t=xyz"
ORDER = "ord_123"
TOTAL = 5149
ADDRESS = {
    "firstName": "Pat", "lastName": "Example", "phone": "+15125550100", "addressLine1": "1 Main St",
    "addressLine2": "Unit 2", "city": "Austin", "region": "TX", "postalCode": "78701", "country": "US",
}
VISA = {"id": "pm_visa", "status": "ACTIVE", "payment_brand": "visa", "last4": "3478",
        "created_at": "2026-10-10T00:00:00Z"}
PLATFORM_ENV_KEYS = (
    "PIVOTA_ENV", "PIVOTA_PLATFORM", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
    "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION",
)


@pytest.fixture(autouse=True)
def _dials(monkeypatch):
    """Both dials on, outside production; a test that needs one off turns it off."""
    for key in PLATFORM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, "1")
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "1")
    # Shipping capture is the read dial AND both steps (a2a_shipping_address.capture_enabled): on with them here.
    monkeypatch.setenv("AGENPAS_INTERNAL_READ_ENABLED", "1")
    # The rail's own per-currency cap (the create route needs it set; the ceiling must fit it too).
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000")
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(HOST.encode()).hexdigest()}))


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    # On Postgres agent_purchases is shared with other files: only this rail's tables are dropped.
    tables = ("a2a_rail_purchases", "a2a_rail_buyer_refs", "a2a_rail_worker_heartbeat") if IS_POSTGRES else (
        "agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs", "a2a_rail_worker_heartbeat")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    if not IS_POSTGRES:
        yield
        return
    # asyncpg's pool belongs to this test's event loop: give it back at the end of every test.
    try:
        yield
    finally:
        await database.disconnect()


# ── fake provider ────────────────────────────────────────────────────────────────────────────


def _card(component, data, actions=(), kind="detail"):
    return {"kind": "data", "data": {"component": component, "kind": kind, "data": data,
                                     "actions": [{"id": a, "dispatch": "agent"} for a in actions]}}


def _task(*parts):
    return {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", "id": "t", "contextId": "c",
            "status": {"state": "completed"}, "history": [{"role": "agent", "parts": list(parts)}]}}


def _text_only(text):
    return _task({"kind": "text", "text": text})


def _methods(*methods):
    return _task(_card("payment.method-list", {"methods": list(methods)}, kind="list"))


def _visa_page(token="ptk_1"):
    return _task(_card("payment.visa-action-required", {
        "paymentTokenId": token, "paymentUrl": APPROVAL, "paymentUrlExpiresIn": 900, "status": "PENDING"}))


def _network_token(**over):
    """The provider's documented token-result `networkToken` (11-19): the DAVV in `cvv`, marked by
    dynamicDataType "DAVV", an empty cryptogram and no ECI. Placeholder DPAN and DAVV."""
    return {"brand": "visa", "value": DPAN, "expiryDate": "1129", "cvv": DAVV, "cryptogram": "", "eci": None,
            "dynamicDataType": "DAVV", **over}


#: The older shape, with a cryptogram (the TAVV) and an ECI: neither is ever sent, the cryptogram never as cvv.
def _network_token_with_cryptogram(**over):
    return _network_token(cryptogram=CRYPTO, eci="05", **over)


def _token_result(token="ptk_1", status="ACTIVE", network_token=None, brand="visa"):
    data = {"paymentTokenId": token, "status": status, "paymentBrand": brand,
            "networkToken": _network_token() if network_token is None else network_token}
    if network_token is False:
        data.pop("networkToken")
    # The provider also says the token in prose: never returned, never relayed from there.
    return _task({"kind": "text", "text": f"Network Token: {DPAN}"}, _card("payment.token-result", data))


def _await(status="PENDING", token="ptk_1"):
    return _task(_card("payment.await", {"paymentTokenId": token, "status": status}))


#: A definite cancel: a card answer with no sign of a submission (the cancel's real shape is not measured).
CANCELLED = _task(_card("checkout.cancelled", {"state": "cancelled"}))
CONFIRM_CARD = _task(_card("checkout.confirm", {
    "merchant": "merchant.example", "shipping": {"email": EMAIL, "city": "Austin"}, "cardLast4": "3478",
    "cardBrand": "visa"}, ("confirm", "edit", "cancel"), kind="confirm"))
SETUP_FORM = _task(_card("checkout.setup", {"missing_required": ["zip_code", "not a field!"]}, kind="form"))


def _checkout_await(task="ckout_abc123", state="accepted"):
    return _task(_card("checkout.await", {"task_id": task, "state": state}))


def _checkout_result(status="payment-reached", *, task="ckout_abc123", success=True, total="$51.49",
                     order_id="1001", errors=()):
    return _task(_card("checkout.result", {
        "task_id": task, "state": "completed" if success else "failed", "success": success, "status": status,
        "total": total, "errors": list(errors), "order_id": order_id, "cardLast4": "3478", "duration_ms": 41000,
    }))


class Rail:
    """Answers per `component#action` from a queue; records each action, its payload, the conversation
    and the row's pending write AT THE MOMENT it arrived."""

    def __init__(self):
        self.answers = {}
        self.sent = []

    def on(self, key, *answers):
        self.answers.setdefault(key, []).extend(answers)
        return self

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == a2a_rail.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        body = json.loads(request.content)
        message = body["params"]["message"]
        data = message["parts"][0]["data"]
        key = f"{data['component']}#{data['action']}"
        row = await database.fetch_one("SELECT pending_write FROM a2a_rail_purchases")
        self.sent.append({"key": key, "payload": data["payload"], "context": message["contextId"],
                          "pending": row["pending_write"] if row else None})
        queue = self.answers.get(key) or []
        answer = queue.pop(0) if queue else httpx.Response(500)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def actions(self):
        return [s["key"] for s in self.sent]

    def payload(self, key):
        return next(s["payload"] for s in self.sent if s["key"] == key)


def _client(rail):
    return a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(rail.handler))


async def _ago(column, seconds, pid):
    """Set `column` to `seconds` ago, on either dialect."""
    if IS_POSTGRES:
        sql = f"UPDATE a2a_rail_purchases SET {column} = now() - (CAST(:age AS INTEGER) * INTERVAL '1 second') WHERE id = :i"
    else:
        sql = f"UPDATE a2a_rail_purchases SET {column} = datetime('now', '-' || :age || ' seconds') WHERE id = :i"
    await database.execute(sql, {"age": int(seconds), "i": pid})


async def _step(rail, pid):
    await _ago("next_poll_at", 1, pid)
    [row] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    return await worker.step(_client(rail), row, W)


async def _row(pid):
    return await ledger.get_purchase_internal(pid)


async def _open(address=ADDRESS):
    row = await ledger.create_purchase(
        agent_id="agent_w", agent_user_ref_hash="h" * 64, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address=address)
    return row["id"]


async def _locked(address=ADDRESS):
    pid = await _open(address)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA)), pid) == "method_ready"
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": ORDER, "t": TOTAL, "i": pid})
    return pid


async def _approving(address=ADDRESS):
    pid = await _locked(address)
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
    return pid


def _approved(rail, setup_answer=CONFIRM_CARD, confirm_answer=None):
    rail.on("payment.visa-action-required#paid", _token_result()).on("checkout.setup#submit", setup_answer)
    rail.on("checkout.confirm#confirm", confirm_answer if confirm_answer is not None else _checkout_await())
    return rail


async def _placing():
    """A purchase whose checkout was confirmed and is being placed (`paying`, a task recorded)."""
    pid = await _approving()
    assert await _step(_approved(Rail()), pid) == "placing"
    return pid


async def _stored_text():
    rows = await database.fetch_all("SELECT * FROM a2a_rail_purchases")
    return json.dumps([dict(r) for r in rows], default=str)


# ── the card mapping (the one function the provider's open question changes) ─────────────────


def test_the_token_maps_onto_the_checkout_card_by_the_default_rule():
    assert a2a_rail.checkout_card_from_network_token(_network_token(), brand="Visa", name="  Pat   Example ") == {
        "number": DPAN, "expiry": "11/29", "cvv": DAVV, "name": "Pat Example", "brand": "visa"}


@pytest.mark.parametrize("token,brand,name", [
    (_network_token(expiryDate="1329"), "visa", "Pat"),        # month 13
    (_network_token(expiryDate="11/29"), "visa", "Pat"),       # not MMYY
    (_network_token(expiryDate=None), "visa", "Pat"),
    (_network_token(value="4895 3700 1200 3478"), "visa", "Pat"),
    (_network_token(value=None), "visa", "Pat"),
    (_network_token(**{a2a_rail.DAVV_FIELD: ""}), "visa", "Pat"),
    (_network_token(**{a2a_rail.DAVV_FIELD: "has space ="}), "visa", "Pat"),
    (_network_token(), "scheme_a", "Pat"),                     # a brand the checkout does not take
    (_network_token(), "visa", "   "),
    ("not a mapping", "visa", "Pat"),
])
def test_a_token_that_does_not_map_cleanly_is_none_never_a_partial_card(token, brand, name):
    assert a2a_rail.checkout_card_from_network_token(token, brand=brand, name=name) is None


def test_the_checkout_shipping_takes_only_its_own_keys_and_needs_no_state_or_zip():
    good = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100",
            "country": "US", "address1": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "78701"}
    assert a2a_rail.checkout_shipping(good) == good
    no_state = {k: v for k, v in good.items() if k != "state"}
    assert a2a_rail.checkout_shipping(no_state) == no_state  # never required: the store platform's to ask for
    assert a2a_rail.checkout_shipping({**{k: v for k, v in good.items() if k != "state"}, "country": "GB"})
    assert a2a_rail.checkout_shipping({**good, "card": {"number": DPAN}}) is None      # no other keys
    assert a2a_rail.checkout_shipping({**good, "address2": f"apt {DPAN}"}) is None   # no card number anywhere
    assert a2a_rail.checkout_shipping({**good, "zip_code": ""}) is None  # a zip_code given must be a value
    no_zip = {k: v for k, v in good.items() if k != "zip_code"}
    assert a2a_rail.checkout_shipping(no_zip) == no_zip  # never required: the store platform's to ask for
    assert a2a_rail.checkout_shipping({**good, "country": "usa"}) is None


@pytest.mark.parametrize("text,currency,minor", [
    ("$23.00", "USD", 2300), ("US$1,234.50", "USD", 123450), ("USD 23", "USD", 2300), ("23.00 USD", "USD", 2300),
    ("$0.99", "USD", 99), ("23.00", "USD", None), ("€23,00", "USD", None), ("$23.0", "USD", None),
    ("$-5.00", "USD", None), (None, "USD", None), ("$23.00", "EUR", None), (2300, "USD", None),
])
def test_the_display_total_is_read_only_when_it_is_unambiguously_the_purchase_currency(text, currency, minor):
    assert worker.checkout_total_minor(text, currency) == minor


# ── the transport ────────────────────────────────────────────────────────────────────────────


class _Provider:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.bodies = []

    def handler(self, request):
        if request.url.path == a2a_rail.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        self.bodies.append(json.loads(request.content))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def sent(self):
        return [b["params"]["message"]["parts"][0]["data"] for b in self.bodies]


MEMBER = "pvm_" + "a" * 24


def _setup(**over):
    values = dict(checkout_url="https://merchant.example/cart/40000000001:1", shipping={
        "email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100", "country": "US",
        "address1": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "78701"},
        cardholder_name="Pat Example", idempotency_key="pvt-ap_000000000000000000000001-checkout", currency="USD")
    values.update(over)
    return a2a_rail.CheckoutSetup(**values)


async def _relay(provider, *, pair=("payment.visa-action-required", "paid"), token="ptk_1", setup=None):
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    return await client.relay_token_to_checkout(
        member_id=MEMBER, context_id=client.context_for(MEMBER, "ap_x_p"), component=pair[0], action=pair[1],
        expected_token_id=token, checkout_context_id=client.context_for(MEMBER, "ap_x_c"), setup=setup or _setup())


async def test_the_setup_can_be_sent_only_by_the_relay():
    provider = _Provider(CONFIRM_CARD)
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    with pytest.raises(a2a_rail.ActionNotAllowed):
        await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                 component="checkout.setup", action="submit", payload={"card": {"number": DPAN}})
    assert provider.bodies == []
    assert ("checkout.result", "retry") not in a2a_rail.ALLOWED_ACTIONS  # it takes a card again
    assert a2a_rail.CHECKOUT_WRITE_ACTIONS.isdisjoint(a2a_rail.READ_ONLY_ACTIONS)


@pytest.mark.parametrize("action", sorted(a2a_rail.CHECKOUT_WRITE_ACTIONS - a2a_rail.RELAY_ONLY_ACTIONS))
async def test_with_the_checkout_dial_off_no_checkout_write_is_sent_anywhere(monkeypatch, action):
    monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)
    provider = _Provider(_checkout_await())
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    with pytest.raises(a2a_rail.CheckoutStepRefused):
        await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                 component=action[0], action=action[1])
    with pytest.raises(a2a_rail.CheckoutStepRefused):
        await _relay(provider)
    assert provider.bodies == []
    # The reads are not the dial's: the worker decides whether to read.
    provider = _Provider(_checkout_await())
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    result = await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                      component="checkout.await", action="poll", payload={"task_id": "ckout_1"})
    assert result.ok and not result.may_write


async def test_the_relay_sends_the_token_back_to_the_provider_in_the_setup_and_returns_nothing_of_it(caplog):
    provider = _Provider(_token_result(), CONFIRM_CARD)
    with caplog.at_level(logging.DEBUG):
        relay = await _relay(provider)
    paid, setup = provider.sent()
    assert (paid["component"], paid["action"], paid["payload"]) == ("payment.visa-action-required", "paid", {})
    assert (setup["component"], setup["action"]) == ("checkout.setup", "submit")
    assert setup["payload"] == {
        "checkout_url": "https://merchant.example/cart/40000000001:1",
        "shipping": _setup().shipping,
        "card": {"number": DPAN, "expiry": "11/29", "cvv": DAVV, "name": "Pat Example", "brand": "visa"},
        "idempotency_key": "pvt-ap_000000000000000000000001-checkout",
    }
    assert provider.bodies[0]["params"]["message"]["contextId"] != provider.bodies[1]["params"]["message"]["contextId"]
    assert relay.refused is None and relay.checkout.card("checkout.confirm") is not None
    assert relay.token.card("payment.token-result").data["paymentTokenId"] == "ptk_1"
    assert relay.token.credential_fields_stripped >= 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in NEEDLES:
        assert needle not in repr(relay) and needle not in logged, needle
    _no_davv_in(repr(relay), logged)
    assert EMAIL not in logged and "Example" not in logged


@pytest.mark.parametrize("answer", [
    _token_result(token="ptk_other"),                    # another token
    _token_result(status="PENDING"),
    _visa_page(),                                        # not approved yet
    _await("ACTIVE"),                                    # names the status only: no token to relay
    _task(_card("payment.pay-result", {"status": "success", "chargeNo": "c1"})),  # a money result
    httpx.Response(502),
])
async def test_anything_but_this_purchases_active_token_sends_no_setup(answer):
    provider = _Provider(answer)
    relay = await _relay(provider)
    assert len(provider.bodies) == 1 and relay.checkout is None and relay.refused is None


@pytest.mark.parametrize("answer,reason", [
    (_token_result(network_token=False), "network_token_missing"),
    (_token_result(network_token=_network_token(expiryDate="2911x")), "card_unmappable"),
    (_token_result(brand="scheme_a", network_token=_network_token(brand="scheme_a")), "card_unmappable"),
])
async def test_an_active_token_that_cannot_make_a_card_is_refused_and_not_sent(answer, reason):
    provider = _Provider(answer)
    relay = await _relay(provider)
    assert len(provider.bodies) == 1 and relay.checkout is None and relay.refused == reason


async def test_the_relay_checks_everything_before_the_first_byte():
    provider = _Provider(_token_result(), CONFIRM_CARD)
    for bad in (_setup(checkout_url="http://merchant.example/cart/1:1"), _setup(idempotency_key="short"),
                _setup(shipping={"email": EMAIL})):
        with pytest.raises(ValueError) as raised:
            await _relay(provider, setup=bad)
        assert EMAIL not in str(raised.value) and "Pat" not in str(raised.value)
    with pytest.raises(a2a_rail.ActionNotAllowed):
        await _relay(provider, pair=("payment.token-setup", "submit"))
    assert provider.bodies == []
    assert "Pat" not in repr(_setup()) and EMAIL not in repr(_setup())


async def test_a_setup_that_fails_mid_flight_is_classified_never_raised(monkeypatch, caplog):
    provider = _Provider(_token_result(), httpx.ConnectError("reset"))
    relay = await _relay(provider)
    assert relay.checkout.kind == "outcome_unknown" and not relay.checkout.resend_safe
    # Anything else that goes wrong after the token is in hand: classified, and its text (which could
    # quote a value) never logged.
    real = a2a_rail.A2ARailClient._send
    calls = []

    async def _boom(self, **kw):
        calls.append(kw["component"])
        if kw["component"] == "checkout.setup":
            raise RuntimeError(f"cannot send {DPAN} {CRYPTO}")
        return await real(self, **kw)

    monkeypatch.setattr(a2a_rail.A2ARailClient, "_send", _boom)
    with caplog.at_level(logging.DEBUG):
        relay = await _relay(_Provider(_token_result()))
    assert calls == ["payment.visa-action-required", "checkout.setup"]
    assert relay.checkout.kind == "outcome_unknown"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in NEEDLES:
        assert needle not in logged and needle not in repr(relay)
    _no_davv_in(logged, repr(relay))


async def test_in_production_the_token_step_runs_only_with_the_checkout_relay_on(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)
    provider = _Provider(_token_result())
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    with pytest.raises(a2a_rail.TokenStepRefused):
        await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                 component="payment.visa-action-required", action="paid")
    assert provider.bodies == [] and a2a_rail.token_step_refused_in_production() is True
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "1")
    assert a2a_rail.token_step_refused_in_production() is False
    relay = await _relay(_Provider(_token_result(), CONFIRM_CARD))
    assert relay.checkout.card("checkout.confirm") is not None


# ── the worker: the happy path ───────────────────────────────────────────────────────────────


async def test_an_approved_token_is_relayed_confirmed_and_the_merchant_order_completes_the_purchase(caplog):
    pid = await _approving()
    rail = _approved(Rail())
    with caplog.at_level(logging.DEBUG):
        assert await _step(rail, pid) == "placing"
    assert rail.actions() == ["payment.visa-action-required#paid", "checkout.setup#submit", "checkout.confirm#confirm"]
    paid, setup, confirm = rail.sent
    # `paid` holds the relay's own write-ahead across it; the confirm (a money write) has its own.
    assert paid["pending"] == setup["pending"] == worker.CHECKOUT_RELAY_PAID_ACTION
    assert confirm["pending"] == worker.CHECKOUT_CONFIRM_ACTION
    assert confirm["payload"] == {}  # the documented body: nothing of ours rides on the money write
    assert setup["context"] == confirm["context"] != paid["context"]
    assert setup["payload"]["card"] == {"number": DPAN, "expiry": "11/29", "cvv": DAVV, "name": "Pat Example",
                                        "brand": "visa"}
    assert setup["payload"]["shipping"] == {
        "email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100", "country": "US",
        "address1": "1 Main St", "address2": "Unit 2", "city": "Austin", "state": "TX", "zip_code": "78701"}
    assert setup["payload"]["checkout_url"] == "https://merchant.example/cart/40000000001:1"
    assert setup["payload"]["idempotency_key"] == f"pvt-{pid}-checkout"
    row = await _row(pid)
    assert (row["state"], row["checkout_task_id"], row["pending_write"], row["last_error_code"], row["claimed_by"]) == (
        "paying", "ckout_abc123", None, worker.CHECKOUT_PLACING, None)
    # The agent sees the purchase being placed.
    assert view.build_view(row)["state"] == "paying" and view.build_view(row)["poll_after_seconds"] == 5

    rail = Rail().on("checkout.await#poll", _checkout_await(state="processing"), _checkout_result())
    with caplog.at_level(logging.DEBUG):
        assert await _step(rail, pid) == "placing"
        assert await _step(rail, pid) == "completed"
    assert [s["payload"] for s in rail.sent] == [{"task_id": "ckout_abc123"}] * 2
    assert all(s["pending"] is None and s["context"] == confirm["context"] for s in rail.sent)  # reads
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["final_total_minor"], row["last_error_code"]) == (
        "completed", "1001", 5149, None)
    assert row["buyer_email"] is None and row["shipping_address"] is None  # terminal writes drop the contact
    shown = view.build_view(row)
    assert shown["order_reference"] == "1001" and shown["totals"]["final_total_minor"] == 5149
    assert "checkout_task_id" not in json.dumps(shown, default=str) and "ckout_" not in json.dumps(shown, default=str)
    # Not an orphan: a completed purchase's token paid.
    assert await ledger.count_orphan_tokens() == 0
    stored = await _stored_text()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in NEEDLES:
        assert needle not in stored, needle
        assert needle not in logged, needle
        assert needle not in json.dumps(shown, default=str), needle
    _no_davv_in(stored, logged, json.dumps(shown, default=str))
    assert EMAIL not in logged and "Example" not in logged and "1 Main St" not in logged


async def test_an_approval_seen_by_the_await_poll_is_relayed_under_its_own_write_ahead():
    pid = await _approving()
    rail = Rail().on("payment.visa-action-required#paid", _await("PENDING"))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert (await _row(pid))["last_error_code"] == "paid_sent:1:await"
    # Not approved yet: the marker is resolved by the (read-only) answer, nothing went to the checkout.
    rail = Rail().on("payment.await#poll", _await("PENDING"))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.sent[0]["pending"] == worker.CHECKOUT_SETUP_ACTION
    row = await _row(pid)
    assert (row["pending_write"], row["last_error_code"]) == (None, "paid_sent:1:await")
    rail = Rail().on("payment.await#poll", _token_result()).on("checkout.setup#submit", CONFIRM_CARD).on(
        "checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "placing"
    assert rail.actions() == ["payment.await#poll", "checkout.setup#submit", "checkout.confirm#confirm"]
    assert [s["pending"] for s in rail.sent] == [worker.CHECKOUT_SETUP_ACTION, worker.CHECKOUT_SETUP_ACTION,
                                                 worker.CHECKOUT_CONFIRM_ACTION]
    assert (await _row(pid))["checkout_task_id"] == "ckout_abc123"


async def test_a_confirm_answered_with_the_result_at_once_completes_in_the_same_step():
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=_checkout_result(total="$50.00")), pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["final_total_minor"], row["checkout_task_id"]) == (
        "completed", "1001", 5000, "ckout_abc123")


async def test_a_task_past_the_poll_cap_is_looked_up_in_a_conversation_of_its_own():
    pid = await _placing()
    await _ago("state_entered_at", worker.CHECKOUT_POLL_CAP_SECONDS + 5, pid)
    rail = Rail().on("checkout.lookup#submit", _checkout_await(state="processing"), _checkout_result(order_id="#1002"))
    assert await _step(rail, pid) == "placing"
    assert await _step(rail, pid) == "completed"
    assert rail.actions() == ["checkout.lookup#submit"] * 2
    client = _client(Rail())
    member = (await _row(pid))["member_id"]
    assert rail.sent[0]["context"] == client.context_for(member, f"{pid}_l")
    assert (await _row(pid))["merchant_order_id"] == "1002"


# ── the worker: the result codes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("answer,state,code", [
    (_checkout_result("captcha-failed", success=False, order_id=None, total=None), "failed", "checkout_captcha_failed"),
    (_checkout_result("form-error", success=False, order_id=None, total=None), "failed", "checkout_form_error"),
    (_checkout_result("pay-disabled", success=False, order_id=None, total=None), "failed", "checkout_pay_disabled"),
    (_checkout_result(order_id=None, errors=["card declined"]), "paying", "checkout_payment_no_order"),
    (_checkout_result(total=None), "paying", "checkout_total_unverified"),
    (_checkout_result(total="EUR 51,49"), "paying", "checkout_total_unverified"),
    (_checkout_result("timeout", success=False, order_id=None), "paying", "checkout_unknown_timeout"),
    (_checkout_result("script-error", success=False, order_id=None), "paying", "checkout_unknown_script_error"),
    (_checkout_result(total="$51.50"), "paying", "checkout_total_over_approved"),
    (_checkout_result("payment-reached", success=False), "paying", "checkout_payment_reached_unconfirmed"),
    (_checkout_result("brand-new-status"), "paying", "checkout_result_unrecognised"),
    (_checkout_result(task="ckout_other"), "paying", "checkout_task_mismatch"),
])
async def test_each_checkout_result_lands_on_its_own_state_and_code(answer, state, code, caplog):
    pid = await _placing()
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        await _step(Rail().on("checkout.await#poll", answer), pid)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == (state, code)
    if state == "paying":
        # Held for a human: on later ticks nothing is read or sent, and the code stays.
        rail = Rail()
        assert await _step(rail, pid) == "checkout_held"
        assert rail.sent == [] and (await _row(pid))["last_error_code"] == code
    if code in ("checkout_payment_no_order", "checkout_total_over_approved", "checkout_unknown_timeout"):
        assert any(r.levelno == logging.ERROR for r in caplog.records)
    if state == "completed":
        assert (row["merchant_order_id"], row["final_total_minor"]) == ("1001", None)


# ── the worker: the setup's failures (nothing was submitted) ──────────────────────────────────


@pytest.mark.parametrize("answer,code", [
    (SETUP_FORM, "checkout_setup_incomplete"),
    (httpx.Response(400), "checkout_setup_refused:http_error:400"),
    (_task(_card("checkout.something-else", {})), "checkout_setup_unrecognised"),
    (_task(_card("checkout.confirm", {"merchant": "merchant.example"}, ("edit",))), "checkout_setup_unrecognised"),
])
async def test_a_setup_that_does_not_reach_the_confirm_card_fails_and_nothing_is_confirmed(answer, code):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=answer)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"], row["payment_token_id"]) == (
        "failed", None, code, "ptk_1")  # the token stays for the orphan count / void


async def test_a_money_result_on_the_setup_parks_for_good():
    pid = await _approving()
    money = _task(_card("payment.pay-result", {"status": "success", "chargeNo": "c1"}))
    assert await _step(_approved(Rail(), setup_answer=money), pid) == "money_result_seen"
    row = await _row(pid)
    assert row["last_error_code"].endswith("money_result_seen") and row["pending_write"]


@pytest.mark.parametrize("answer,code", [
    (_token_result(network_token=False), "checkout_card_unavailable:network_token_missing"),
    (_token_result(network_token=_network_token(expiryDate="xx")), "checkout_card_unavailable:card_unmappable"),
])
async def test_an_approved_token_that_cannot_make_a_card_fails_without_a_setup(answer, code):
    pid = await _approving()
    rail = Rail().on("payment.visa-action-required#paid", answer)
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.visa-action-required#paid"]
    assert (await _row(pid))["last_error_code"] == code


async def _set_address(pid, address):
    cast = "CAST(:a AS JSONB)" if IS_POSTGRES else ":a"
    await database.execute(f"UPDATE a2a_rail_purchases SET shipping_address = {cast} WHERE id = :i",
                           {"a": json.dumps(address, sort_keys=True) if address is not None else None, "i": pid})


@pytest.mark.usefixtures("consent_gate_on")
async def test_an_incomplete_contact_mints_no_token_at_all():
    # Incomplete from the start: parked in `opened`, NOTHING sent (AgenPas asks the buyer for the address).
    pid = await _open({k: v for k, v in ADDRESS.items() if k != "city"})
    rail = Rail()
    assert await _step(rail, pid) == worker.SHIPPING_ADDRESS_REQUIRED
    assert rail.sent == [] and (await _row(pid))["state"] == "opened"
    # Incomplete only after the card step (an edited row): still never minted, and failed as before.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _locked()
    await _set_address(pid, {k: v for k, v in ADDRESS.items() if k != "city"})
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "failed"
    assert rail.sent == []
    assert (await _row(pid))["last_error_code"] == worker.CHECKOUT_CONTACT_INCOMPLETE


async def test_a_setup_marker_left_by_an_unseen_answer_is_held_without_a_send():
    pid = await _approving()
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = :w, pending_write_at = CURRENT_TIMESTAMP, "
                           "last_error_code = 'paid_sent:1:await'", {"w": worker.CHECKOUT_SETUP_ACTION})
    rail = Rail()
    assert await _step(rail, pid) == "checkout_setup_unresolved"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"]) == ([], "paying", "checkout_setup_unresolved")
    rail = Rail()
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []


# ── the worker: the confirm is a money write ─────────────────────────────────────────────────


@pytest.mark.parametrize("answer", [httpx.Response(502), httpx.ReadTimeout("slow"), httpx.Response(200, content=b"{")])
async def test_an_unseen_confirm_is_held_and_never_sent_again(answer):
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=answer), pid) == worker.CHECKOUT_CONFIRM_UNRESOLVED
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"], row["checkout_task_id"]) == (
        "paying", worker.CHECKOUT_CONFIRM_ACTION, worker.CHECKOUT_CONFIRM_UNRESOLVED, None)
    for _ in range(2):
        rail = Rail().on("checkout.confirm#confirm", _checkout_await())
        assert await _step(rail, pid) == "checkout_held"
        assert rail.sent == []
    assert await ledger.expire_overdue_purchases(max_age_seconds=1, max_lifetime_seconds=1) == 0  # never swept


async def test_a_confirm_answered_with_no_task_is_held_with_the_write_kept():
    pid = await _approving()
    weird = _task(_card("checkout.await", {"state": "accepted"}))
    assert await _step(_approved(Rail(), confirm_answer=weird), pid) == worker.CHECKOUT_CONFIRM_UNRECOGNISED
    row = await _row(pid)
    assert (row["pending_write"], row["last_error_code"]) == (worker.CHECKOUT_CONFIRM_ACTION,
                                                              worker.CHECKOUT_CONFIRM_UNRECOGNISED)


@pytest.mark.parametrize("answer,code", [
    (httpx.Response(400), "checkout_confirm_refused:http_error:400"),
])
async def test_a_confirm_that_is_not_accepted_fails_nothing_was_submitted(answer, code):
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=answer), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == ("failed", None, code)


async def _unconfirmed_paying(code="worker_error"):
    """A `paying` checkout with no task, no pending write and no hold (a crashed step's code, say)."""
    pid = await _placing()
    await database.execute("UPDATE a2a_rail_purchases SET checkout_task_id = NULL, pending_write = NULL, "
                           "last_error_code = :c WHERE id = :i", {"c": code, "i": pid})
    return pid


def _answers_everything():
    return Rail().on("checkout.confirm#cancel", CANCELLED, CANCELLED, CANCELLED).on(
        "checkout.confirm#confirm", _checkout_await(), _checkout_await(), _checkout_await()).on(
        "checkout.await#poll", _checkout_result(), _checkout_result(), _checkout_result())


async def test_a_later_tick_finding_paying_without_a_task_holds_and_sends_nothing(caplog):
    pid = await _unconfirmed_paying()
    rail = _answers_everything()
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert await _step(rail, pid) == "checkout_paying_without_task"
    assert any(r.levelno == logging.ERROR and "no task" in r.getMessage() for r in caplog.records)
    for _ in range(3):
        assert await _step(rail, pid) == "checkout_held"
    assert rail.sent == []  # no confirm, no cancel, no read
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"], row["terminal_at"]) == (
        "paying", None, "checkout_paying_without_task", None)
    assert worker._sticky(row)


async def test_a_lease_taken_over_from_a_dead_worker_on_paying_without_a_task_holds_and_sends_nothing():
    pid = await _unconfirmed_paying()
    # A worker died holding the row: its lease outlives the run deadline, then the requeue frees it.
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w-dead', next_poll_at = CURRENT_TIMESTAMP "
                           "WHERE id = :i", {"i": pid})
    await _ago("claimed_at", worker.STALE_LEASE_SECONDS + 60, pid)
    rail = _answers_everything()
    counts = await worker.run_tick(_client(rail), W, limit=5)
    assert counts.get("requeued") == 1 and counts.get("checkout_paying_without_task") == 1
    for _ in range(3):
        await _ago("next_poll_at", 1, pid)
        assert (await worker.run_tick(_client(rail), W, limit=5)).get("checkout_held") == 1
    assert rail.sent == []
    assert (await _row(pid))["last_error_code"] == "checkout_paying_without_task"


async def test_a_submitted_checkout_with_no_result_for_a_day_is_held():
    pid = await _placing()
    await _ago("state_entered_at", worker.CHECKOUT_CEILING_SECONDS + 5, pid)
    rail = Rail()
    assert await _step(rail, pid) == worker.CHECKOUT_RESULT_OVERDUE
    assert rail.sent == []


async def test_a_crash_never_overwrites_a_checkout_hold(monkeypatch):
    pid = await _placing()
    await _step(Rail().on("checkout.await#poll", _checkout_result("timeout", success=False, order_id=None)), pid)
    assert worker._sticky(await _row(pid))

    async def _crash(*a, **k):
        raise RuntimeError("step blew up")

    monkeypatch.setattr(worker, "step", _crash)
    await _ago("next_poll_at", 1, pid)
    counts = await worker.run_tick(_client(Rail()), W)
    assert counts.get("error") == 1
    assert (await _row(pid))["last_error_code"] == worker.CHECKOUT_UNKNOWN_TIMEOUT


# ── with the checkout dial off: exactly the pre-checkout behaviour ────────────────────────────


async def test_with_the_checkout_dial_off_an_approved_token_ends_as_before_and_the_relay_is_never_used(monkeypatch):
    monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)

    async def _never(*a, **k):
        raise AssertionError("the relay must not be used with the checkout dial off")

    monkeypatch.setattr(a2a_rail.A2ARailClient, "relay_token_to_checkout", _never)
    # An incomplete contact does not matter either: nothing checks it with the dial off.
    pid = await _approving({k: v for k, v in ADDRESS.items() if k != "postalCode"})
    rail = Rail().on("payment.visa-action-required#paid", _token_result()).on("checkout.setup#submit", CONFIRM_CARD)
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.visa-action-required#paid"]
    assert rail.sent[0]["pending"] == "payment.visa-action-required#paid"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"], row["payment_token_id"]) == (
        "failed", None, worker.PAY_STEP_UNAVAILABLE, "ptk_1")
    # The await poll is a plain read again (no write-ahead).
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _approving()
    await _step(Rail().on("payment.visa-action-required#paid", _await("PENDING")), pid)
    rail = Rail().on("payment.await#poll", _token_result())
    assert await _step(rail, pid) == "failed"
    assert [(s["key"], s["pending"]) for s in rail.sent] == [("payment.await#poll", None)]
    assert (await _row(pid))["last_error_code"] == worker.PAY_STEP_UNAVAILABLE


async def test_with_the_checkout_dial_off_a_checkout_being_placed_parks_unread(monkeypatch):
    pid = await _placing()
    monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)
    rail = Rail().on("checkout.await#poll", _checkout_result())
    assert await _step(rail, pid) == "parked"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"]) == ([], "paying", worker.CHECKOUT_DISABLED)


async def test_in_production_with_the_checkout_dial_off_nothing_changes(monkeypatch):
    """Production, checkout dial off, token dial ON: the token step is refused exactly as before."""
    pid = await _locked()
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)
    monkeypatch.setattr(worker, "_token_step_refusal_logged_at", None)
    assert worker.token_step_enabled() is False
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "parked"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"]) == ([], "quoting", worker.TOKEN_STEP_REFUSED_PRODUCTION)


async def test_in_production_with_both_dials_on_the_purchase_is_placed(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "production")
    assert worker.token_step_enabled() is True
    pid = await _approving()
    assert await _step(_approved(Rail()), pid) == "placing"
    assert (await _row(pid))["state"] == "paying"


async def test_a_paying_row_that_is_not_a_checkout_still_parks_as_before():
    pid = await _open()
    await database.execute("UPDATE a2a_rail_purchases SET state = 'paying' WHERE id = :i", {"i": pid})
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "shop_protocol_pending"


# ── the ledger ───────────────────────────────────────────────────────────────────────────────


async def test_the_task_is_recorded_once_only_with_the_confirm_pending_and_never_a_card_number():
    pid = await _placing()
    row = await _row(pid)
    assert row["checkout_task_id"] == "ckout_abc123"
    await _ago("next_poll_at", 1, pid)
    await ledger.claim_due_purchases(W, limit=50)
    await ledger.mark_pending_write(pid, W, worker.CHECKOUT_CONFIRM_ACTION)
    # A task is already recorded: a second one never replaces it.
    assert await ledger.record_checkout_task_as_holder(
        pid, W, checkout_task_id="ckout_new", resolves_write=worker.CHECKOUT_CONFIRM_ACTION) is None
    with pytest.raises(ValueError):
        await ledger.record_checkout_task_as_holder(
            pid, W, checkout_task_id=f"ck_{DPAN}", resolves_write=worker.CHECKOUT_CONFIRM_ACTION)
    assert (await _row(pid))["checkout_task_id"] == "ckout_abc123"


def test_migration_287_holds_the_columns_ddl():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    up = (repo / "db/migrations/287_a2a_rail_checkout_task_id.sql").read_text("utf-8")
    squash = lambda text: " ".join(text.split())  # noqa: E731
    assert squash(ledger._ADD_CHECKOUT_TASK_ID_PG) in squash(up)
    assert "schema-guard-exempt:" in up
    down = (repo / "db/migrations/down/287_a2a_rail_checkout_task_id_down.sql").read_text("utf-8")
    assert "DROP COLUMN IF EXISTS checkout_task_id" in down


def test_no_provider_or_store_name_is_in_the_new_code():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    names = ("ev" + "onet", "ever" + "onet", "agen" + "zo", "cardinfo" + "link")
    for path in (
        "services/payment_orchestration/a2a_rail.py", "services/payment_orchestration/a2a_rail_worker.py",
        "services/payment_orchestration/a2a_purchase_view.py", "db/a2a_rail_ledger.py", "db/agent_purchase_ledger.py",
        "db/migrations/287_a2a_rail_checkout_task_id.sql", "db/migrations/down/287_a2a_rail_checkout_task_id_down.sql",
        "db/migrations/288_a2a_rail_ceiling_source.sql", "db/migrations/down/288_a2a_rail_ceiling_source_down.sql",
        "db/migrations/290_a2a_rail_checkout_lookups.sql", "db/migrations/down/290_a2a_rail_checkout_lookups_down.sql",
        "routes/agent_internal_commerce_purchases.py", "tests/test_agent_internal_commerce_purchases.py",
        "tests/test_agent_internal_commerce_purchases_postgres.py",
        "tests/test_a2a_rail_checkout.py", "tests/test_a2a_rail_checkout_postgres.py", "tests/test_a2a_rail_client.py",
        "tests/test_a2a_rail_ledger_postgres.py", "main.py", "jobs/a2a_rail_operator.py",
        "services/payment_orchestration/checkout_amounts.py", "tests/a2a_rail_confirm_card_corpus.py",
        "docs/adr/ADR-028-agent-completed-checkout.md", "docs/agent_purchases_routes.md",
        "docs/adr/ADR-030-pivota-as-protocol-platform-card-networks-as-credential-layer.md",
    ):
        text = (repo / path).read_text("utf-8").lower()
        assert not any(name in text for name in names), path


# ── the checkout path's lock: Pivota's order reference and approval ceiling ──────────────────


async def _unlocked(**kw):
    """A purchase in `quoting` with an ACTIVE card and no lock yet (what the door + first step leave)."""
    args = dict(agent_id="agent_w", agent_user_ref_hash="h" * 64, merchant_domain="merchant.example",
                checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
                buyer_email=EMAIL, shipping_address=ADDRESS)
    args.update(kw)
    pid = (await ledger.create_purchase(**args))["id"]
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA)), pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["order_id"], row["quoted_total_minor"]) == ("quoting", None, None)
    return pid


def _lock_rail():
    return Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())


@pytest.mark.parametrize("price,quantity,expected", [
    (4650, 1, 4650 + 698 + 1000),     # 15% of 46.50 = 6.975 -> 6.98 (up to the cent)
    (4000, 2, 8000 + 1200 + 1000),
    (1, 1, 1 + 1 + 1000),
])
def test_the_ceiling_is_items_plus_tax_and_shipping_allowances_rounded_up(price, quantity, expected):
    assert worker.checkout_ceiling({"currency": "USD", "our_price_minor": price, "quantity": quantity}) == (
        expected, worker.CEILING_SOURCE_ALLOWANCE_V1)


def test_the_ceiling_allowances_and_cap_are_config_and_a_bad_value_falls_back(monkeypatch):
    row = {"currency": "USD", "our_price_minor": 10000, "quantity": 1}
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, "1000")
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, "0")
    assert worker.checkout_ceiling(row)[0] == 11000
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV, "10999")
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling(row)
    assert raised.value.code == worker.CHECKOUT_CEILING_OVER_CAP and raised.value.refused
    for bad in ("x", "-1", "99999"):
        monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, bad)
        monkeypatch.delenv(worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV, raising=False)
        assert worker.checkout_ceiling(row)[0] == 10000 + 1500 + 0  # the default 15%


@pytest.mark.parametrize("row,code,refused", [
    ({"currency": "EUR", "our_price_minor": 4650, "quantity": 1}, "checkout_currency_unsupported", True),
    ({"currency": "USD", "our_price_minor": None, "quantity": 1}, "checkout_price_missing", False),
    ({"currency": "USD", "our_price_minor": 0, "quantity": 1}, "checkout_price_missing", False),
    ({"currency": "USD", "our_price_minor": True, "quantity": 1}, "checkout_price_missing", False),
    ({"currency": "USD", "our_price_minor": 4650, "quantity": None}, "checkout_price_missing", False),
    ({"currency": "USD", "our_price_minor": 18000, "quantity": 1}, "checkout_ceiling_over_cap", True),
])
def test_no_ceiling_is_guessed(row, code, refused):
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling(row)
    assert (raised.value.code, raised.value.refused) == (code, refused)


def test_a_ceiling_above_the_rails_own_cap_is_refused(monkeypatch):
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:6000")
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling({"currency": "USD", "our_price_minor": 4650, "quantity": 1})
    assert (raised.value.code, raised.value.refused) == ("checkout_ceiling_over_rail_limit", True)


def test_the_order_reference_is_deterministic_and_never_card_shaped():
    import uuid

    from services.payment_orchestration.a2a_rail import _has_card_number

    assert worker.checkout_order_id("ap_0123456789abcdef01234567") == "pvt-ap_01234567.89abcdef.01234567"
    assert worker.checkout_order_id("ap_0123456789abcdef01234567") == worker.checkout_order_id("ap_0123456789abcdef01234567")
    assert not _has_card_number(worker.checkout_order_id("ap_" + "4" * 24))
    for _ in range(2000):
        order_id = worker.checkout_order_id(f"ap_{uuid.uuid4().hex[:24]}")
        assert not _has_card_number(order_id) and ledger._rail_ref(order_id, "order_id") == order_id


async def test_the_lock_writes_the_order_and_ceiling_and_the_token_is_asked_for_exactly_that():
    pid = await _unlocked()
    rail = _lock_rail()
    assert await _step(rail, pid) == "approval_page_issued"
    row = await _row(pid)
    expected = 4650 + 698 + 1000
    assert (row["order_id"], row["quoted_total_minor"], row["ceiling_source"]) == (
        worker.checkout_order_id(pid), expected, "allowance_v1")
    setup = rail.payload("payment.token-setup#submit")
    assert (setup["amount_cents"], setup["external_transaction_id"]) == (expected, worker.checkout_order_id(pid))
    assert (row["state"], row["payment_token_id"]) == ("awaiting_authorization", "ptk_1")
    totals = view.build_view(row)["totals"]
    assert (totals["quoted_total_minor"], totals["ceiling_source"]) == (expected, "allowance_v1")


async def test_a_second_tick_never_relocks_or_moves_the_ceiling(monkeypatch):
    pid = await _unlocked()
    # Locked through the ledger (as a first tick does), then the next tick must use that lock as it is.
    await _ago("next_poll_at", 1, pid)
    [held] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    ceiling, source = worker.checkout_ceiling(held)
    first = await ledger.lock_checkout_order_as_holder(
        pid, W, order_id=worker.checkout_order_id(pid), quoted_total_minor=ceiling, ceiling_source=source)
    assert first is not None
    assert await ledger.lock_checkout_order_as_holder(
        pid, W, order_id="pvt-other", quoted_total_minor=1, ceiling_source="other") is None  # never twice
    await ledger.release_claim(pid, W)
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, "0")  # a changed config later

    async def _no_relock(*a, **k):
        raise AssertionError("a locked purchase was locked again")

    monkeypatch.setattr(ledger, "lock_checkout_order_as_holder", _no_relock)
    rail = _lock_rail()
    assert await _step(rail, pid) == "approval_page_issued"
    row = await _row(pid)
    assert (row["order_id"], row["quoted_total_minor"]) == (worker.checkout_order_id(pid), ceiling)
    assert rail.payload("payment.token-setup#submit")["amount_cents"] == ceiling


async def test_over_the_cap_the_purchase_is_refused_and_nothing_is_sent():
    pid = await _unlocked(our_price_minor=18000)
    rail = _lock_rail()
    assert await _step(rail, pid) == "refused"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["refusal_reason"], row["order_id"]) == (
        [], "refused", "checkout_ceiling_over_cap", None)


async def test_a_non_usd_purchase_is_refused_and_nothing_is_sent():
    pid = await _unlocked(currency="EUR")
    rail = _lock_rail()
    assert await _step(rail, pid) == "refused"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["refusal_reason"]) == ([], "refused", "checkout_currency_unsupported")


async def test_a_missing_price_fails_and_nothing_is_sent():
    pid = await _unlocked()
    await _ago("next_poll_at", 1, pid)
    [held] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    rail = _lock_rail()
    # The column is NOT NULL > 0 in the ledger; the step must still never guess one.
    assert await worker.step(_client(rail), {**held, "our_price_minor": None}, W) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"], row["order_id"]) == (
        [], "failed", "checkout_price_missing", None)


@pytest.mark.parametrize("dials", ["checkout_off", "token_off", "both_off"])
async def test_with_either_dial_off_an_unlocked_purchase_parks_exactly_as_before(monkeypatch, dials):
    pid = await _unlocked()
    if dials in ("checkout_off", "both_off"):
        monkeypatch.delenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV)
    if dials in ("token_off", "both_off"):
        monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV)

    async def _no_lock(*a, **k):
        raise AssertionError("locked with a dial off")

    monkeypatch.setattr(ledger, "lock_checkout_order_as_holder", _no_lock)
    rail = _lock_rail()
    assert await _step(rail, pid) == "parked"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["order_id"], row["quoted_total_minor"], row["ceiling_source"],
            row["last_error_code"]) == ([], "quoting", None, None, None, "shop_protocol_pending")
    assert "ceiling_source" not in view.build_view(row)["totals"]


def test_migration_290_holds_the_columns_ddl():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    up = (repo / "db/migrations/290_a2a_rail_checkout_lookups.sql").read_text("utf-8")
    squash = lambda text: " ".join(text.split())  # noqa: E731
    assert squash(ledger._ADD_CHECKOUT_LOOKUPS_PG) in squash(up)
    assert "ADD COLUMN IF NOT EXISTS checkout_lookups INTEGER" in ledger._HEAL_CHECKOUT_COLUMNS_PG
    assert "schema-guard-exempt:" in up
    down = (repo / "db/migrations/down/290_a2a_rail_checkout_lookups_down.sql").read_text("utf-8")
    assert "DROP COLUMN IF EXISTS checkout_lookups" in down


def test_migration_288_holds_the_columns_ddl():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    up = (repo / "db/migrations/288_a2a_rail_ceiling_source.sql").read_text("utf-8")
    squash = lambda text: " ".join(text.split())  # noqa: E731
    assert squash(ledger._ADD_CEILING_SOURCE_PG) in squash(up)
    assert "schema-guard-exempt:" in up
    down = (repo / "db/migrations/down/288_a2a_rail_ceiling_source_down.sql").read_text("utf-8")
    assert "DROP COLUMN IF EXISTS ceiling_source" in down


# ── review follow-ups (commit 4) ─────────────────────────────────────────────────────────────

EXPIRY_RAW, EXPIRY_SHOWN = "1129", "11/29"


def _string_values(obj):
    """Every string leaf (and key) of a JSON-like value, to look for an exact credential value."""
    if isinstance(obj, dict):
        return set(map(str, obj)) | set().union(*(_string_values(v) for v in obj.values())) if obj else set()
    if isinstance(obj, (list, tuple)):
        return set().union(*(_string_values(v) for v in obj)) if obj else set()
    return {str(obj)} if obj is not None else set()


def _no_credential_in(text_or_obj):
    text = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj, default=str)
    for needle in (*NEEDLES, EXPIRY_SHOWN):
        assert needle not in text, needle
    if not isinstance(text_or_obj, str):
        assert EXPIRY_RAW not in _string_values(text_or_obj)


@pytest.mark.parametrize("answer,outcome,state,code,pending", [
    (_task(_card("error", {"code": "TRY_LATER", "recoverable": True})),
     "checkout_confirm_error_card", "paying", "checkout_confirm_error_card", True),
    (_task(_card("error", {"code": "NOPE", "recoverable": False})),
     "checkout_confirm_error_card", "paying", "checkout_confirm_error_card", True),
    (_task(_card("error", {"code": "NO_FLAG"})),
     "checkout_confirm_error_card", "paying", "checkout_confirm_error_card", True),
    (httpx.Response(401), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(403), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(429), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(408), "checkout_confirm_unresolved", "paying", "checkout_confirm_unresolved", True),
    # A conflict may mean "already confirmed or submitted": held, never failed (a failure invites a second
    # real purchase). So is every 4xx that does not clearly mean "never accepted".
    (httpx.Response(409), "checkout_confirm_conflict", "paying", "checkout_confirm_conflict", True),
    (httpx.Response(404), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(410), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(423), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    (httpx.Response(425), "checkout_confirm_ambiguous_4xx", "paying", "checkout_confirm_ambiguous_4xx", True),
    # Validation refusals: never accepted, nothing submitted.
    (httpx.Response(400), "failed", "failed", "checkout_confirm_refused:http_error:400", False),
    (httpx.Response(422), "failed", "failed", "checkout_confirm_refused:http_error:422", False),
])
async def test_the_confirm_counts_only_a_validation_4xx_as_not_submitted(answer, outcome, state, code, pending):
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=answer), pid) == outcome
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == (state, code)
    assert (row["pending_write"] == worker.CHECKOUT_CONFIRM_ACTION) is pending
    if pending:
        rail = Rail().on("checkout.confirm#confirm", _checkout_await())
        assert await _step(rail, pid) == "checkout_held" and rail.sent == []


def _failing_confirm_tokens(monkeypatch, failures):
    """Our own token exchange fails for the next `failures` exchanges after `paid` and the setup."""
    real = a2a_rail.A2ARailClient._access_token
    calls = []

    async def _token(self, member_id):
        calls.append(1)
        return None if 3 <= len(calls) < 3 + failures else await real(self, member_id)

    monkeypatch.setattr(a2a_rail.A2ARailClient, "_access_token", _token)


async def test_a_confirm_refused_for_our_own_token_is_sent_again(monkeypatch):
    pid = await _approving()
    rail = _approved(Rail())
    _failing_confirm_tokens(monkeypatch, 1)  # nothing sent the first time: once more, in the same step
    assert await _step(rail, pid) == "placing"
    assert rail.actions().count("checkout.confirm#confirm") == 1  # the failed one never reached the provider
    assert (await _row(pid))["checkout_task_id"] == "ckout_abc123"


async def test_a_confirm_our_token_exchange_never_lets_out_is_held_and_its_write_resolved(monkeypatch):
    pid = await _approving()
    rail = _approved(Rail())
    _failing_confirm_tokens(monkeypatch, 2)
    assert await _step(rail, pid) == "checkout_confirm_not_sent"
    assert "checkout.confirm#confirm" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == ("paying", None, "checkout_confirm_not_sent")
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []


async def test_payment_reached_without_an_order_is_held_not_failed(caplog):
    pid = await _placing()
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        outcome = await _step(Rail().on("checkout.await#poll", _checkout_result(order_id=None)), pid)
    assert outcome == worker.CHECKOUT_PAYMENT_NO_ORDER
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["terminal_at"]) == ("paying", "checkout_payment_no_order", None)
    assert worker._sticky(row) and view.build_view(row)["last_error_code"] == "checkout_payment_no_order"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("missing", ["checkout_task_id", "ceiling_source"])
async def test_a_half_healed_schema_never_starts_the_checkout_path(missing):
    pid = await _unlocked()
    await _ago("next_poll_at", 1, pid)
    [held] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    rail = _lock_rail()
    row = {k: v for k, v in held.items() if k != missing}  # what SELECT * returns before the heal
    assert await worker.step(_client(rail), row, W) == "parked"
    after = await _row(pid)
    assert (rail.sent, after["state"], after["order_id"], after["last_error_code"]) == (
        [], "quoting", None, worker.CHECKOUT_SCHEMA_INCOMPLETE)


async def test_a_half_healed_schema_never_confirms_a_checkout_being_placed():
    pid = await _unconfirmed_paying()
    await _ago("next_poll_at", 1, pid)
    [held] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    row = {k: v for k, v in held.items() if k != "checkout_task_id"}
    assert await worker.step(_client(rail), row, W) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == worker.CHECKOUT_SCHEMA_INCOMPLETE


def test_both_checkout_columns_heal_in_one_guarded_statement_on_postgres():
    from db.schema_guard import guarded_add_columns

    heal = ledger._HEAL_CHECKOUT_COLUMNS_PG
    assert "checkout_task_id VARCHAR(128)" in heal and "ceiling_source VARCHAR(32)" in heal
    assert len(guarded_add_columns(heal + ";")) == 1


@pytest.mark.parametrize("answer", [
    _checkout_await(),                                                        # it was submitted after all
    _checkout_result(),
    _task(_card("error", {"code": "CANNOT_CANCEL", "recoverable": False})),
    _text_only("Cancelled."),                                                 # prose is not a definite cancel
    httpx.Response(502),
    httpx.ConnectError("reset"),
])
async def test_a_cancel_that_does_not_say_cancelled_is_held(answer):
    # The one cancel there is: in the step that read a bad confirm-card amount (the confirm never sent).
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with({"total": "$999.00"})).on("checkout.confirm#cancel", answer)
    assert await _step(rail, pid) == worker.CHECKOUT_CANCEL_UNRESOLVED
    assert "checkout.confirm#confirm" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == (
        "paying", worker.CHECKOUT_CANCEL_UNRESOLVED, worker.CHECKOUT_CANCEL_ACTION)  # the write kept
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []


@pytest.mark.parametrize("total", ["$12,34", "€230.00", "CA$250.00", "$250.00 CAD", "250.00", None, 2300])
async def test_an_unverified_final_total_is_held_never_completed(total, caplog):
    pid = await _placing()
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        outcome = await _step(Rail().on("checkout.await#poll", _checkout_result(total=total)), pid)
    assert outcome == "checkout_total_unverified"
    row = await _row(pid)
    assert (row["state"], row["final_total_minor"], row["merchant_order_id"], row["last_error_code"]) == (
        "paying", None, None, "checkout_total_unverified")
    assert view.build_view(row)["last_error_code"] == "checkout_total_unverified" and worker._sticky(row)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_zero_and_comma_decimal_totals():
    assert worker.checkout_total_minor("$0.00", "USD") == 0
    assert worker.checkout_total_minor("$12,34", "USD") is None


ECHO_CONFIRM = _task(_card("checkout.confirm", {
    "merchant": "merchant.example", "cardLast4": "3478", "cardBrand": "visa",
    "shown": EXPIRY_SHOWN, "ref": EXPIRY_RAW, "note": f"card {DPAN} ok", "auth": CRYPTO,
    "nested": [{"whatever": DPAN}, {"card": {"code": DAVV}}],
}, ("confirm", "edit", "cancel"), kind="confirm"))


def _echo_result(order_id="1001"):
    return _task(_card("checkout.result", {
        "task_id": "ckout_abc123", "state": "completed", "success": True, "status": "payment-reached",
        "total": "$51.49", "errors": [], "order_id": order_id, "cardLast4": "3478", "duration_ms": 1,
        "card": {"number": DPAN, "expiry": EXPIRY_SHOWN, "cvv": DAVV, "brand": "visa"},
    }))


async def test_the_relay_scrubs_the_tokens_own_values_from_an_echoing_confirm_card():
    provider = _Provider(_token_result(network_token=_network_token_with_cryptogram()), ECHO_CONFIRM)
    relay = await _relay(provider)
    confirm = relay.checkout.card("checkout.confirm")
    assert confirm is not None and "confirm" in confirm.action_ids()
    _no_credential_in(confirm.data)
    _no_credential_in(repr(relay))
    assert confirm.data["cardLast4"] == "3478" and confirm.data["merchant"] == "merchant.example"
    # The DAVV under a card-shaped key (`card.code`) is no key the stripper knows: only the by-value scrub
    # catches it, and only there.
    assert confirm.data["nested"][1] == {"card": {"code": None}}


async def test_an_echoed_card_never_reaches_the_ledger_logs_or_view(caplog):
    # An echo in the SETUP's answer: the relay scrubs it, and a scrubbed card is never confirmed (cancelled).
    pid = await _approving()
    with caplog.at_level(logging.DEBUG):
        rail = _approved(Rail(), setup_answer=ECHO_CONFIRM).on("checkout.confirm#cancel", CANCELLED)
        assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_total_refused"
    # An echo in the final RESULT (after a clean confirm card): stripped, and the order completes.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _approving()
    with caplog.at_level(logging.DEBUG):
        assert await _step(_approved(Rail()), pid) == "placing"
        assert await _step(Rail().on("checkout.await#poll", _echo_result()), pid) == "completed"
    row = await _row(pid)
    assert row["merchant_order_id"] == "1001"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    _no_credential_in(logged)
    stored = [dict(r) for r in await database.fetch_all("SELECT * FROM a2a_rail_purchases")]
    _no_credential_in(json.loads(json.dumps(stored, default=str)))
    _no_credential_in(json.loads(json.dumps(view.build_view(row), default=str)))


async def test_a_card_shaped_order_id_is_never_stored_and_the_order_still_completes(caplog):
    # v6 (P2-H): the merchant placed the order, so it completes; only its reference is withheld, with a
    # code the operator sees. Never a hold for this.
    pid = await _placing()
    with caplog.at_level(logging.DEBUG):
        assert await _step(Rail().on("checkout.await#poll", _echo_result(order_id=DPAN)), pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["last_error_code"]) == (
        "completed", None, worker.CHECKOUT_ORDER_REFERENCE_WITHHELD)
    assert DPAN not in await _stored_text()
    [line] = [r for r in caplog.records if "order reference withheld" in r.getMessage()]
    assert line.levelno == logging.ERROR and "rule=luhn_run" in line.getMessage() and f"length={len(DPAN)}" in line.getMessage()


def test_a_long_order_number_that_fails_luhn_is_not_card_shaped():
    assert worker._card_shaped(DPAN) and not worker._card_shaped("4895370012003479")
    assert not worker._card_shaped("1001") and not worker._card_shaped("48953700120034781234")


async def test_in_production_with_the_relay_on_paid_and_the_await_poll_go_only_through_the_relay(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "production")
    provider = _Provider(_token_result(), _token_result())
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    for component, action in sorted(a2a_rail.TOKEN_RELAY_SOURCES):
        with pytest.raises(a2a_rail.TokenStepRefused):
            await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                     component=component, action=action)
    assert provider.bodies == []
    # The mint step is still a plain send (its answer is an approval page).
    provider = _Provider(_visa_page())
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    result = await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                      component="payment.token-setup", action="submit")
    assert result.ok and len(provider.bodies) == 1
    # Outside production nothing changes.
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    provider = _Provider(_await("PENDING"))
    client = a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(provider.handler))
    assert (await client.send_action(member_id=MEMBER, context_id=client.context_for(MEMBER, "r"),
                                     component="payment.await", action="poll")).ok


async def test_with_the_checkout_on_no_paid_goes_out_without_the_relay():
    pid = await _approving()
    # The contact went incomplete after the token was asked for (an edited row, say).
    await database.execute("UPDATE a2a_rail_purchases SET buyer_email = NULL WHERE id = :i", {"i": pid})
    rail = Rail().on("payment.visa-action-required#paid", _token_result())
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["last_error_code"]) == ([], worker.CHECKOUT_CONTACT_INCOMPLETE)


def test_only_validation_4xx_answers_fail_a_confirm():
    assert worker._CONFIRM_NOT_ACCEPTED_4XX == {400, 422}
    # Nothing a provider answers resends a confirm: only our own token exchange failing (nothing sent).
    assert not hasattr(worker, "_CONFIRM_RESEND_4XX")
    assert {"checkout_confirm_conflict", "checkout_confirm_ambiguous_4xx"} <= worker._CHECKOUT_HOLD_CODES


# ── controller review, commit 7 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prose", ["Your order #1001 has been placed.", "I could not place that order.", "OK"])
async def test_a_text_only_confirm_answer_is_held_with_the_write_kept(prose):
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=_text_only(prose)), pid) == "checkout_confirm_text_only"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"], row["terminal_at"]) == (
        "paying", worker.CHECKOUT_CONFIRM_ACTION, "checkout_confirm_text_only", None)
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []  # never resent


def _confirm_with(data):
    return _task(_card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478", **data},
                       ("confirm", "edit", "cancel"), kind="confirm"))


#: Every total-like spelling matched: top level, one and two levels down, any case / separator.
TOTAL_SPELLINGS = [
    {"total": "{t}"}, {"Total": "{t}"}, {"amount": "{t}"}, {"grandTotal": "{t}"}, {"grand_total": "{t}"},
    {"total_price": "{t}"}, {"totalPrice": "{t}"}, {"TOTAL-PRICE": "{t}"},
    {"totals": {"total": "{t}"}}, {"order": {"amount": "{t}"}}, {"summary": {"order": {"grandTotal": "{t}"}}},
]


def _fill(spelling, total):
    return json.loads(json.dumps(spelling).replace("{t}", total))


@pytest.mark.parametrize("spelling", TOTAL_SPELLINGS, ids=lambda d: json.dumps(d))
async def test_a_confirm_card_total_above_the_ceiling_is_cancelled_instead_of_confirmed(spelling):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(_fill(spelling, "$999.00"))).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.visa-action-required#paid", "checkout.setup#submit", "checkout.confirm#cancel"]
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", "checkout_confirm_total_refused", None)


@pytest.mark.parametrize("total", ["€40.00", "CA$40.00", "$40.00 CAD", "40.00", 4000, {"value": 4000}, "-$5.00"])
async def test_an_unreadable_or_foreign_confirm_card_total_is_cancelled(total):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with({"total": total})).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_total_refused"


@pytest.mark.parametrize("answer", [_text_only("Cancelled."), _checkout_await(), httpx.Response(502)])
async def test_a_refused_confirm_card_total_whose_cancel_is_not_definite_is_held(answer):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with({"total": "$999.00"})).on("checkout.confirm#cancel", answer)
    assert await _step(rail, pid) == "checkout_cancel_unresolved"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["state"] == "paying"


async def test_a_confirm_card_without_a_monetary_amount_proceeds_only_under_the_tokens_amount_cap():
    """No amount on the card (non-monetary numbers and words only): the confirm goes out, and what bounds the
    charge is the token's amount, minted for exactly the ceiling: asserted on THIS purchase's own mint."""
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert rail.payload("payment.token-setup#submit")["amount_cents"] == TOTAL
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
    card = _confirm_with({"note": "free gift", "giftWrap": True, "items": "2", "zip": "78701", "sku": "SKU-40000000001",
                          "shipping": {"zip_code": "78701", "phone": "+15125550100", "city": "Austin"}})
    rail = _approved(Rail(), setup_answer=card)
    assert await _step(rail, pid) == "placing"
    assert rail.actions()[-1] == "checkout.confirm#confirm"


async def test_the_mint_carries_exactly_the_locked_ceiling_and_the_order_reference():
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    mint = rail.payload("payment.token-setup#submit")
    row = await _row(pid)
    assert mint["amount_cents"] == row["quoted_total_minor"] == TOTAL
    assert mint["external_transaction_id"] == row["order_id"] == ORDER
    assert "currency" not in mint  # USD only: the token step refuses any other currency before minting


@pytest.mark.parametrize("order_id,ceiling", [(ORDER, None), (ORDER, 0), (ORDER, -5), (None, TOTAL), ("  ", TOTAL)])
async def test_a_malformed_lock_never_mints(order_id, ceiling):
    pid = await _unlocked()
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": order_id, "t": ceiling, "i": pid})
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["last_error_code"], row["payment_token_id"]) == ([], "checkout_lock_invalid", None)


async def test_a_non_usd_locked_row_never_mints():
    pid = await _unlocked(currency="EUR")
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": ORDER, "t": TOTAL, "i": pid})
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    assert "payment.token-setup#submit" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "token_currency_unsupported"


@pytest.mark.parametrize("answer,code", [
    (_checkout_await(), "checkout_setup_submitted"),
    (_checkout_result(), "checkout_setup_submitted"),
    (httpx.Response(502), "checkout_setup_unresolved"),
    (httpx.ReadTimeout("slow"), "checkout_setup_unresolved"),
    (httpx.ConnectError("reset"), "checkout_setup_unresolved"),
    (httpx.Response(200, content=b"{not json"), "checkout_setup_unresolved"),
    (_text_only("Done, your order is in."), "checkout_setup_unresolved"),
    (httpx.Response(409), "checkout_setup_unresolved"),
])
async def test_a_setup_showing_a_submission_or_never_seen_is_held_not_failed(answer, code):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=answer)
    assert await _step(rail, pid) == code
    assert "checkout.confirm#confirm" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["terminal_at"]) == ("paying", code, None)
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []


@pytest.mark.parametrize("data", [
    {"success": True, "order_id": None},
    {"success": None, "order_id": None},
])
@pytest.mark.parametrize("status", ["captcha-failed", "form-error", "pay-disabled"])
async def test_a_contradictory_result_is_held(status, data):
    pid = await _placing()
    answer = _checkout_result(status, total=None, **data)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "checkout_result_unrecognised"
    assert (await _row(pid))["state"] == "paying"


async def test_a_relay_that_cannot_be_sent_ends_cleanly_with_nothing_sent():
    pid = await _approving()
    await database.execute("UPDATE a2a_rail_purchases SET payment_token_id = NULL WHERE id = :i", {"i": pid})
    rail = Rail().on("payment.visa-action-required#paid", _token_result())
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["pending_write"], row["last_error_code"]) == ([], None, "checkout_relay_refused")


async def test_a_relay_refusing_after_the_write_ahead_resolves_it_and_ends_cleanly(monkeypatch):
    pid = await _approving()

    async def _refuses(*a, **k):
        raise ValueError("refused before the first byte")

    monkeypatch.setattr(a2a_rail.A2ARailClient, "relay_token_to_checkout", _refuses)
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["pending_write"], row["last_error_code"]) == ([], None, "checkout_relay_refused")


async def test_the_scrub_covers_action_dispatch_role_and_the_context_id_and_runs_before_the_log_line(caplog):
    loose = DPAN  # the token's value: a valid identifier shape
    answer = _task(_card(f"x.{loose}", {"ok": 1}), {"kind": "data", "data": {
        "component": "checkout.confirm", "kind": "confirm", "data": {"merchant": "merchant.example"},
        "actions": [{"id": "confirm", "dispatch": loose, "role": loose}, {"id": "cancel"}]}})
    answer["result"]["contextId"] = loose
    provider = _Provider(_token_result(), answer)
    with caplog.at_level(logging.DEBUG, logger=a2a_rail.__name__):
        relay = await _relay(provider)
    confirm = relay.checkout.card("checkout.confirm")
    assert [(a.id, a.dispatch, a.role) for a in confirm.actions] == [("confirm", None, None), ("cancel", None, None)]
    assert relay.checkout.context_id is None and loose not in repr(relay)
    assert all(loose not in r.getMessage() for r in caplog.records)


# ── controller review round 2 (commit 8) ─────────────────────────────────────────────────────


class _Cut(Rail):
    """A fake rail that raises CancelledError (the scheduler's deadline cutting the step) when `cut_on`
    arrives, once. Every later answer is scripted, so a later tick COULD resend if it were allowed to."""

    def __init__(self, cut_on):
        super().__init__()
        self.cut_on = cut_on

    async def handler(self, request):
        if request.url.path != a2a_rail.TOKEN_PATH:
            data = json.loads(request.content)["params"]["message"]["parts"][0]["data"]
            key = f"{data['component']}#{data['action']}"
            if key == self.cut_on:
                self.cut_on = None
                self.sent.append({"key": f"{key}(cut)", "payload": None, "context": None, "pending": None})
                raise asyncio.CancelledError()
        return await super().handler(request)


async def _tick_cut(rail, pid):
    """One scheduler tick through run_tick, cut by CancelledError; the claim is released as run_tick does."""
    await _ago("next_poll_at", 1, pid)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_tick(_client(rail), W, limit=5)


def _everything(rail):
    return _approved(rail).on("payment.visa-action-required#paid", _token_result()).on(
        "checkout.setup#submit", CONFIRM_CARD).on("checkout.confirm#confirm", _checkout_await())


@pytest.mark.parametrize("cut,held_code", [
    ("before_paid", "checkout_setup_unresolved"),
    ("after_paid_before_setup", "checkout_setup_unresolved"),
    ("during_setup", "checkout_setup_unresolved"),
    ("after_setup_before_task", "checkout_confirm_pending_check"),
    ("during_confirm", "checkout_confirm_unresolved"),
])
async def test_a_step_cut_anywhere_in_the_relay_or_confirm_is_held_and_nothing_is_resent(monkeypatch, cut, held_code):
    pid = await _approving()
    rail = _Cut({"before_paid": "payment.visa-action-required#paid", "during_setup": "checkout.setup#submit",
                 "during_confirm": "checkout.confirm#confirm"}.get(cut))
    _approved(rail)
    if cut == "after_paid_before_setup":
        def _cut_card(*a, **k):
            raise asyncio.CancelledError()
        monkeypatch.setattr(a2a_rail, "checkout_card_from_network_token", _cut_card)
    if cut == "after_setup_before_task":
        real_mark = ledger.mark_pending_write

        async def _cut_at_the_confirms_mark(purchase_id, worker_id, action):
            if action == worker.CHECKOUT_CONFIRM_ACTION:
                raise asyncio.CancelledError()
            return await real_mark(purchase_id, worker_id, action)
        monkeypatch.setattr(ledger, "mark_pending_write", _cut_at_the_confirms_mark)
    await _tick_cut(rail, pid)
    monkeypatch.undo()
    _dial_env(monkeypatch)
    before = len(rail.sent)
    held_pending = (await _row(pid))["pending_write"]  # the unseen write the hold keeps (never resent)
    # The next ticks: the rail would answer every WRITE, and none may go out. Only the read-only lookup by our
    # idempotency key does (it finds nothing here: still held).
    for _ in range(3):
        _everything(rail)
        await _ago("next_poll_at", 1, pid)
        await _ago("state_entered_at", 3600, pid)
        await worker.run_tick(_client(rail), W, limit=5)
    later = rail.sent[before:]
    # A lookup marks no write: the row holds at most the unseen write it already had (the first later tick may
    # turn a relay marker into the `paying` hold, resolving it).
    assert later and all(x["key"] == "checkout.lookup#submit" and x["pending"] in (held_pending, None) for x in later)
    assert all(x["payload"] == {"task_id": f"pvt-{pid}-checkout"} for x in later)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", held_code)
    assert row["checkout_task_id"] is None and row["terminal_at"] is None


def _dial_env(monkeypatch):
    """`monkeypatch.undo()` drops the autouse dials too: put them back."""
    for key in PLATFORM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, "1")
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000")
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(HOST.encode()).hexdigest()}))


async def test_a_cut_cancel_is_held_and_never_followed_by_a_confirm(monkeypatch):
    pid = await _approving()
    rail = _Cut("checkout.confirm#cancel")
    _approved(rail, setup_answer=_confirm_with({"total": "$999.00"}))
    await _tick_cut(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("paying", worker.CHECKOUT_CANCEL_ACTION)
    for _ in range(2):
        rail.on("checkout.confirm#confirm", _checkout_await()).on("checkout.confirm#cancel", CANCELLED)
        await _ago("next_poll_at", 1, pid)
        await worker.run_tick(_client(rail), W, limit=5)
    assert "checkout.confirm#confirm" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", "checkout_cancel_unresolved")


async def test_an_unseen_plain_paid_on_a_checkout_row_is_held_never_resent():
    pid = await _approving()
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = 'payment.visa-action-required#paid', "
                           "pending_write_at = CURRENT_TIMESTAMP, last_error_code = 'paid_sent:1:visa' WHERE id = :i",
                           {"i": pid})
    rail = _everything(Rail())
    assert await _step(rail, pid) == "checkout_setup_unresolved"
    assert rail.sent == []
    assert (await _row(pid))["state"] == "paying"


async def test_a_move_to_paying_whose_lease_is_lost_is_left_held_not_confirmable(monkeypatch):
    pid = await _approving()
    real = ledger.transition_as_holder

    async def _lease_gone_after(*a, **k):
        moved = await real(*a, **k)
        if moved is not None and k.get("to_state") == "paying":
            await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w2' WHERE id = :i", {"i": pid})
            moved = {**moved, "claimed_by": "w2"}
        return moved

    monkeypatch.setattr(ledger, "transition_as_holder", _lease_gone_after)
    rail = _approved(Rail())
    await _step(rail, pid)
    assert "checkout.confirm#confirm" not in rail.actions()
    monkeypatch.setattr(ledger, "transition_as_holder", real)
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = NULL WHERE id = :i", {"i": pid})
    rail = Rail().on("checkout.confirm#confirm", _checkout_await()).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_pending_check"


#: The 17 shapes the round-2 review used to beat the key matcher, plus its other refused shapes: each one
#: must cancel, never confirm ($999.00 against a $51.49 ceiling unless noted).
REFUSED_CARDS = {
    "totalAmount": {"totalAmount": "$999.00"}, "order_total": {"order_total": "$999.00"},
    "amount_due": {"amount_due": "$999.00"}, "amountDue": {"amountDue": "$999.00"},
    "grand_total_amount": {"grand_total_amount": "$999.00"}, "price": {"price": "$999.00"},
    "sum": {"sum": "$999.00"}, "cost": {"cost": "$999.00"}, "charge": {"charge": "$999.00"},
    "payment_due": {"payment": {"due": "$999.00"}}, "total_space": {"Total Price": "$999.00"},
    "total_trailing_space": {"total ": "$999.00"}, "totals_list": {"totals": [{"total": "$999.00"}]},
    "line_items": {"lineItems": [{"total": "$999.00"}]}, "depth3": {"a": {"b": {"c": {"total": "$999.00"}}}},
    "lookalike_key": {"t\u043etal": "$999.00"}, "in_prose": {"summary": "Total: $999.00"},  # the key: a Cyrillic "о"
    "usd_prefix": {"total": "USD 999.00"}, "cents_int": {"total": 99900}, "dollars_float": {"total": 999.0},
    "negative": {"total": "-$999.00"}, "one_cent_over": {"total": "$51.50"},
    "nested_obj_over": {"total": {"amount": "$999.00", "currency": "USD"}},
    "two_totals_one_bad": {"total": "$51.49", "totals": {"grand_total": "$999.00"}},
    "dotted_key": {"total.price": "$999.00"}, "bare_number_string": {"total": "999.00"},
    "subtotal_alone": {"subtotal": "$999.00"}, "foreign_symbol": {"note": "approx €40"},
    "deep_in_list": {"x": [[[{"y": [{"z": "$999.00"}]}]]]},
}
#: Exactly a named shape within the ceiling (checkout_amounts rule 2), or nothing money-like: confirmed.
CONFIRMED_CARDS = {
    "s1_exact": {"total": "51.49", "currency": "USD"}, "s1_zero": {"total": 0, "currency": "USD"},
    "s2_receipt": {"line_items": [{"quantity": 2, "price": "20.00"}], "subtotal": "40.00", "shipping": "5.00",
                   "tax": "3.30", "total": "48.30", "currency": "USD"},
    "s3_order": {"line_items": [{"quantity": 1, "price": 45, "compare_at_price": "50.00"}], "subtotal_price": "45.00",
                 "shipping_lines": [{"price": "6.49"}], "total_price": "51.49", "currency": "USD"},
    "documented_card": {"shipping": {"email": EMAIL, "zip_code": "78701", "phone": "+15125550100"},
                        "cardBrand": "visa"},
}


@pytest.mark.parametrize("name", sorted(REFUSED_CARDS))
async def test_any_monetary_amount_on_the_confirm_card_that_is_not_within_the_ceiling_cancels(name):
    data = REFUSED_CARDS[name]
    assert worker.confirm_card_refused({"merchant": "merchant.example", **data}, TOTAL) is True
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(data)).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions() and "checkout.confirm#cancel" in rail.actions()
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_total_refused"


@pytest.mark.parametrize("name", sorted(CONFIRMED_CARDS))
async def test_a_confirm_card_whose_amounts_are_all_within_the_ceiling_is_confirmed(name):
    assert worker.confirm_card_refused(CONFIRMED_CARDS[name], TOTAL) is False
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(CONFIRMED_CARDS[name]))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#cancel" not in rail.actions()


def test_the_card_scan_refuses_what_it_cannot_read_whole():
    wide = {"items": [{"n": i} for i in range(worker._CARD_SCAN_MAX_NODES + 1)]}
    deep = current = {}
    for _ in range(worker._CARD_SCAN_MAX_DEPTH + 2):
        current["k"] = {}
        current = current["k"]
    assert worker.confirm_card_refused(wide, TOTAL) is True
    assert worker.confirm_card_refused(deep, TOTAL) is True
    assert worker.confirm_card_refused({"total": "$1.00"}, None) is True  # no ceiling: nothing confirms
    # Digit strings under keys with no money word are not money-like (checkout_amounts rule 1).
    assert worker.confirm_card_refused({"zip": "78701", "phone": "+15125550100", "cardLast4": "3478", "items": "3",
                                        "orderRef": "40000000001", "sku": "40000000001"}, TOTAL) is False


@pytest.mark.parametrize("answer", [httpx.Response(403), _task(_card("error", {"code": "NOPE", "recoverable": False}))])
async def test_a_403_or_an_error_card_on_the_setup_is_held(answer):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=answer)
    assert await _step(rail, pid) == "checkout_setup_unresolved"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["state"] == "paying"


async def test_a_zero_final_total_is_held_not_completed():
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(total="$0.00")), pid) == (
        "checkout_total_unverified")
    row = await _row(pid)
    assert (row["state"], row["final_total_minor"]) == ("paying", None)


# ── controller review round 3: the amount scan (fix/a2a-checkout-amount-scan) ────────────────

#: Every amount the v3 review showed the scan confirming, and the other shapes it probed: each one is
#: CANCELLED against the $51.49 ceiling (amounts we cannot read with certainty, or adding up past it).
UNREADABLE_OR_OVER = {
    "usd_lower_suffix": {"total": "999.00 usd"}, "usd_lower_prefix": {"total": "usd 999.00"},
    "dollars_word": {"total": "999.00 dollars"}, "incl_tax_suffix": {"total": "999.00 incl. tax"},
    "exponent_string": {"total": "9.99e2"}, "exponent_number": {"total": 9.99e2},
    "eur_lower": {"total": "999 eur"}, "eur_upper": {"total": "999 EUR"}, "rupees": {"total": "Rs. 999"},
    "three_spaces": {"total": "$   999.00"}, "zero_width": {"total": "$​999.00"},
    "no_break_space": {"total": "$ 999.00"},
    "fullwidth_digits": {"total": "$９９９.００"}, "fullwidth_dollar": {"total": "＄999.00"},
    "arabic_indic": {"total": "$٩٩٩"}, "comma_euro": {"total": "999,00 €"},
    "accounting_negative": {"total": "($5.00)"}, "one_decimal": {"total": "$51.5"},
    "value_currency": {"value": 99900, "currency": "USD"},
    "money_value_string": {"money": {"value": "999.00", "currency": "USD"}},
    "vat": {"vat": 999}, "duty": {"duty": 999}, "delivery": {"delivery": 999},
    "shipping_bare": {"shipping": 999}, "shipping_string": {"shipping": "999.00"}, "payment_bare": {"payment": 999},
    "payment_due_bare": {"payment": {"due": 999}}, "lookalike_key_bare": {"tоtal": 999},
    "amount_cents": {"amount_cents": 99900},
    "json_in_string_bare": {"meta": json.dumps({"total": 999})},
    "json_in_string_marked": {"meta": json.dumps({"total": "$999.00"})},
    "amount_in_a_key": {"$999.00": "x"},
    "shipping_cost": {"shipping": {"cost": "$999.00"}},
    "shipping_amount_beside_an_address": {"shipping": {"email": EMAIL, "zip_code": "78701", "amount": 999}},
    # P2-2: the parts add up past the ceiling, though each is under it
    "components_sum": {"subtotal": "$45.00", "shipping_cost": "$10.00", "tax": "$5.00"},
    "line_items_sum": {"items": [{"price": "$40.00"}, {"price": "$40.00"}]},
    "quantity_times_price": {"items": [{"qty": 20, "price": "$50.00"}]},
    "line_strings_sum": {"lines": ["$30.00", "$30.00"]},
}


@pytest.mark.parametrize("name", ["usd_lower_suffix", "fullwidth_digits", "value_currency", "shipping_bare",
                                  "json_in_string_bare", "components_sum", "quantity_times_price",
                                  "shipping_cost"])
async def test_the_scan_gaps_end_to_end_cancel_instead_of_confirming(name):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(UNREADABLE_OR_OVER[name])).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_total_refused"


async def test_the_documented_confirm_card_with_a_real_address_still_confirms():
    pid = await _approving()
    card = _task(_card("checkout.confirm", MEASURED_CARD, ("confirm", "edit", "cancel"), kind="confirm"))
    rail = _approved(Rail(), setup_answer=card)
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()


async def test_an_amount_on_a_sibling_card_of_the_setup_answer_cancels():
    pid = await _approving()
    answer = _task(
        _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
              ("confirm", "edit", "cancel"), kind="confirm"),
        _card("checkout.summary", {"total": "$999.00"}))
    rail = _approved(Rail(), setup_answer=answer).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


async def test_a_value_the_relay_scrubbed_is_unknown_and_cancels():
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with({"total": f"${DPAN}"})).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert DPAN not in await _stored_text()


def _deep(levels, leaf):
    data = leaf
    for _ in range(levels):
        data = {"k": data}
    return data


def test_the_scan_bounds_are_exact():
    # A leaf with nothing money-like in it (checkout_amounts rule 1): read at the bound, refused past it.
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, "n1"), TOTAL) is False  # read
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH + 1, "n1"), TOTAL) is True
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, "$999.00"), TOTAL) is True
    # Every kind of leaf meets the same bound: a container too, not only a string.
    for leaf in ({}, [], "7", None):
        assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, leaf), TOTAL) is False, leaf
        assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH + 1, leaf), TOTAL) is True, leaf
    # The data itself, the list, then the items: exactly the cap is read, one more is refused.
    at_cap = {"items": [str(i) for i in range(worker._CARD_SCAN_MAX_NODES - 2)]}
    over_cap = {"items": [str(i) for i in range(worker._CARD_SCAN_MAX_NODES - 1)]}
    assert worker.confirm_card_refused(at_cap, TOTAL) is False
    assert worker.confirm_card_refused(over_cap, TOTAL) is True


async def test_a_crash_after_the_move_to_paying_keeps_the_pending_check_hold(monkeypatch):
    pid = await _approving()
    real_mark = ledger.mark_pending_write

    async def _crash_at_the_confirms_mark(purchase_id, worker_id, action):
        if action == worker.CHECKOUT_CONFIRM_ACTION:
            raise RuntimeError("crash between the move to paying and the confirm's mark")
        return await real_mark(purchase_id, worker_id, action)

    monkeypatch.setattr(ledger, "mark_pending_write", _crash_at_the_confirms_mark)
    rail = _approved(Rail())
    await _ago("next_poll_at", 1, pid)
    assert (await worker.run_tick(_client(rail), W, limit=5)).get("error") == 1
    row = await _row(pid)
    # The hold the step wrote survives the crash release (not `paid_sent:...:worker_error`).
    assert (row["state"], row["last_error_code"], row["pending_write"]) == (
        "paying", "checkout_confirm_pending_check", None)
    monkeypatch.setattr(ledger, "mark_pending_write", real_mark)
    rail = _answers_everything()
    for _ in range(3):
        await _ago("next_poll_at", 1, pid)
        assert (await worker.run_tick(_client(rail), W, limit=5)).get("checkout_held") == 1
    assert rail.sent == []


async def test_a_hold_code_is_written_bare_never_behind_the_approval_schedule():
    pid = await _unconfirmed_paying(code="paid_sent:1:visa")
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = :w, pending_write_at = CURRENT_TIMESTAMP "
                           "WHERE id = :i", {"w": worker.CHECKOUT_CANCEL_ACTION, "i": pid})
    assert await _step(Rail(), pid) == "checkout_cancel_unresolved"
    assert (await _row(pid))["last_error_code"] == "checkout_cancel_unresolved"


# ── the operator's path for a held checkout (never automatic) ────────────────────────────────


async def _held_text_only():
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=_text_only("Order placed.")), pid) == "checkout_confirm_text_only"
    return pid


async def _operator(*argv, client=None):
    """resolve-held / list-held with NO rail client configured (None) unless a fake one is given."""
    import io
    import jobs.a2a_rail_operator as operator

    out = io.StringIO()
    code = await operator.run(list(argv), out=out, client=client)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


async def test_the_operator_lists_held_checkouts_without_pii():
    pid = await _held_text_only()
    code, rows = await _operator("list-held")
    assert code == 0 and [r["id"] for r in rows] == [pid]
    assert rows[0]["last_error_code"] == "checkout_confirm_text_only" and rows[0]["pending_write"]
    text = json.dumps(rows)
    for needle in (EMAIL, "1 Main St", "Example", "ptk_1", "pvm_", "https://"):
        assert needle not in text


async def test_the_operator_resolves_a_held_checkout_only_with_every_guard():
    pid = await _held_text_only()
    expected = ("resolve-held", "--purchase-id", pid, "--expected-code", "checkout_confirm_text_only")
    guards = ("--apply", "--operator", "ops-1", "--expect-env", "staging", "--provider-checked")
    # Held just now: too recent (the provider's staged checkout may still land an order).
    code, [preview] = await _operator(*expected)
    assert (code, preview["eligible"], preview["reason"]) == (3, False, "held_too_recently")
    code, [result] = await _operator(*expected, *guards)
    assert (code, result["outcome"]) == (3, "refused") and (await _row(pid))["state"] == "paying"
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 60, pid)
    code, [preview] = await _operator(*expected)
    assert (code, preview["outcome"], preview["eligible"]) == (0, "dry_run", True)
    assert (await _row(pid))["state"] == "paying"  # a dry run writes nothing
    for missing in (("--operator", "ops-1", "--expect-env", "staging"),
                    ("--operator", "ops-1", "--provider-checked"),
                    ("--expect-env", "staging", "--provider-checked"),
                    ("--operator", "ops-1", "--expect-env", "production", "--provider-checked")):
        code, _ = await _operator(*expected, "--apply", *missing)
        assert code == 2
        assert (await _row(pid))["state"] == "paying"
    code, [result] = await _operator(*expected, "--apply", "--operator", "ops-1", "--expect-env", "staging",
                                     "--provider-checked")
    assert (code, result["outcome"]) == (0, "resolved")
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["buyer_email"]) == (
        "failed", "operator_confirmed_not_charged", None, None)


async def test_the_operator_refuses_a_moved_claimed_or_unheld_row():
    pid = await _held_text_only()
    guards = ("--apply", "--operator", "ops-1", "--expect-env", "staging", "--provider-checked")
    code, [result] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                     "checkout_confirm_unresolved", *guards)  # not the code it holds now
    assert (code, result["outcome"]) == (3, "refused")
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 60, pid)  # old enough: the claim refuses
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w9' WHERE id = :i", {"i": pid})
    code, _ = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                              "checkout_confirm_text_only", *guards)
    assert code == 3 and (await _row(pid))["state"] == "paying"  # a worker holds it right now
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = NULL, last_error_code = 'checkout_placing' "
                           "WHERE id = :i", {"i": pid})
    result = await worker.operator_fail_held_checkout(pid, expected_code="checkout_placing", operator="ops-1")
    assert result == {"outcome": "refused", "reason": "not_a_checkout_hold_code"}
    assert (await _row(pid))["state"] == "paying"


def test_nothing_in_the_worker_or_its_jobs_resolves_a_hold_by_itself():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    worker_source = (repo / "services/payment_orchestration/a2a_rail_worker.py").read_text("utf-8")
    assert worker_source.count("operator_fail_held_checkout(") == 2  # its definition and the ledger call in it
    for path in ("jobs/a2a_rail_purchase_poll.py", "services/audit_scheduler.py"):
        assert "operator_fail_held_checkout" not in (repo / path).read_text("utf-8")


# ── controller review round 4 (scan fix): sums, guards that must fail, false cancels, operator ───

#: Shapes whose parts add up past the $51.49 ceiling in spellings the round-3 sum check missed.
SUM_BYPASSES = {
    "qty_string": {"items": [{"qty": "2", "price": "$30.00"}]},
    "qty_float": {"items": [{"qty": 2.0, "price": "$30.00"}]},
    "quantity_ordered": {"items": [{"quantityOrdered": 2, "price": "$30.00"}]},
    "quantity_ordered_snake": {"items": [{"quantity_ordered": 2, "price": "$30.00"}]},
    "count_key": {"items": [{"count": 2, "price": "$30.00"}]},
    "unit_cost": {"items": [{"qty": 2, "unitCost": "$30.00"}]},
    "unit_cost_snake": {"items": [{"qty": 2, "unit_cost": "$30.00"}]},
    "unit_price": {"items": [{"qty": 2, "unitPrice": "$30.00"}]},
    "each": {"items": [{"qty": 2, "each": "$30.00"}]},
    "price_money_object": {"items": [{"qty": 2, "price": {"amount": "$30.00", "currency": "USD"}}]},
    "price_value_object": {"items": [{"qty": 2, "price": {"value": "$30.00", "currency": "USD"}}]},
    "total_named_parts": {"total_line_items_price": "$45.00", "total_shipping": "$10.00", "total_tax": "$5.00"},
    "json_string_part": {"items": [{"price": "$30.00"}], "extra": json.dumps({"fee": "$30.00"})},
    "two_money_objects": {"a": {"amount": "$30.00"}, "b": {"amount": "$30.00"}},
}
#: The buyer's own data, merchant and item names, booleans and non-ASCII keys: none may cancel.
DOC_SHIPPING = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100", "country": "US",
                "address1": "1 Main St", "address2": "Unit 2", "city": "Austin", "state": "TX", "zip_code": "78701"}


def _doc(**shipping):
    return {"merchant": "merchant.example", "cardLast4": "3478", "cardBrand": "visa",
            "shipping": {**DOC_SHIPPING, **shipping}}


NEVER_CANCEL = {
    "address_pen_rd": _doc(address1="12 Pen Rd"), "address_euro_plaza": _doc(address1="100 Euro Plaza"),
    "email_aud1": _doc(email="aud1@example.test"), "email_try2buy": _doc(email="try2buy@example.test"),
    "postcode_plus4": _doc(zip_code="78701-1234"), "phone_formatted": _doc(phone="(512) 555-0100"),
    "house_number": _doc(house_number="12"), "unit": _doc(unit="4B"), "apartment": _doc(apartment="4"),
    "company": _doc(company="3M"), "instructions": _doc(instructions="Gate code 1234"),
    "speed": _doc(speed="2-Day"), "method": _doc(method="2-Day"), "residential": _doc(residential=True),
    "address2_apt": _doc(address2="Apt 4"),
    "merchant_euro_24": {**_doc(), "merchant": "Euro 24 Shop"},
    "item_gel_pen": {**_doc(), "items": [{"name": "Gel Pen 12 pack"}]},
    "item_pound_bag": {**_doc(), "items": [{"name": "Dog food 30 pound bag"}]},
    "discount_applied_false": {**_doc(), "discountApplied": False},
    "card_object": {**_doc(), "card": {"last4": "3478", "brand": "visa"}},
    "order_ref": {**_doc(), "orderRef": "40000000001"},
    "accented_key": {**_doc(), "código": "78701"},
}


@pytest.mark.parametrize("name", ["qty_string", "quantity_ordered", "unit_cost", "price_money_object",
                                  "total_named_parts", "json_string_part"])
async def test_the_sum_bypasses_end_to_end_cancel_instead_of_confirming(name):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(SUM_BYPASSES[name])).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


@pytest.mark.parametrize("name", sorted(NEVER_CANCEL))
def test_the_buyers_own_data_and_ordinary_names_never_cancel(name):
    assert worker.confirm_card_refused(NEVER_CANCEL[name], TOTAL) is False


@pytest.mark.parametrize("name", ["address_pen_rd", "address_euro_plaza", "email_aud1", "email_try2buy"])
async def test_the_documented_card_echoing_the_buyers_own_contact_confirms_end_to_end(name):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(NEVER_CANCEL[name]))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()


async def test_an_amount_in_the_setup_answers_prose_cancels():
    pid = await _approving()
    answer = _task({"kind": "text", "text": "Order total: $999.00"},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    rail = _approved(Rail(), setup_answer=answer).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


async def test_ordinary_prose_in_the_setup_answer_does_not_cancel():
    pid = await _approving()
    answer = _task({"kind": "text", "text": "Please confirm your order. Card ending 3478, ship to 1 Main St."},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    rail = _approved(Rail(), setup_answer=answer)
    assert await _step(rail, pid) == "placing"


async def test_the_prose_check_is_read_in_the_transport_and_never_returns_the_text(caplog):
    answer = _task({"kind": "text", "text": f"Total: $999.00 on {DPAN}"},
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    with caplog.at_level(logging.DEBUG):
        relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is True
    assert "999.00" not in repr(relay) and DPAN not in repr(relay)
    assert all(DPAN not in r.getMessage() for r in caplog.records)
    relay = await _relay(_Provider(_token_result(), answer))  # no ceiling given: any prose refuses
    assert relay.checkout.text_amounts_refused is True


# ── the operator path: never "not charged" over a recorded payment; a minimum hold age ──────


def test_every_hold_code_is_classified_and_the_payment_reached_branch_writes_only_payment_codes():
    import inspect
    import re as _re

    assert worker._CHECKOUT_PAYMENT_REACHED_CODES | worker._OPERATOR_RESOLVABLE_HOLD_CODES == worker._CHECKOUT_HOLD_CODES
    assert not worker._CHECKOUT_PAYMENT_REACHED_CODES & worker._OPERATOR_RESOLVABLE_HOLD_CODES
    source = inspect.getsource(worker._checkout_result)
    branch = source[source.index('if status == "payment-reached":'):source.index("if status in _CHECKOUT_NOT_SUBMITTED")]
    held = set(_re.findall(r"_checkout_hold\(s, (CHECKOUT_[A-Z_]+)\)", branch))
    assert held, "the payment-reached branch writes holds"
    assert {getattr(worker, name) for name in held} <= worker._CHECKOUT_PAYMENT_REACHED_CODES
    # The three codes the review named are among them.
    assert {"checkout_total_over_approved", "checkout_total_unverified", "checkout_order_id_card_shaped",
            "checkout_payment_no_order", "checkout_payment_reached_unconfirmed",
            "checkout_answer_scrubbed"} <= worker._CHECKOUT_PAYMENT_REACHED_CODES
    # `checkout_answer_scrubbed` is written ONLY by the readers of answers that come after the confirm, and it is
    # on the refused side of the partition.
    full = inspect.getsource(worker)
    writers = {name for name, fn in inspect.getmembers(worker, inspect.iscoroutinefunction)
               if "_checkout_hold(s, CHECKOUT_ANSWER_SCRUBBED)" in inspect.getsource(fn)}
    assert writers == {"_confirm_answer", "_checkout_progress", "_checkout_lookup"}
    assert full.count("_checkout_hold(s, CHECKOUT_ANSWER_SCRUBBED)") == 3
    # No other use writes it: its name appears only where it is defined, in the two code sets, and in those holds.
    uses = [line.strip() for line in full.splitlines() if "CHECKOUT_ANSWER_SCRUBBED" in line and not line.strip().startswith("#")]
    assert len(uses) == 1 + 2 + 3, uses
    assert worker.CHECKOUT_ANSWER_SCRUBBED not in worker._OPERATOR_RESOLVABLE_HOLD_CODES


@pytest.mark.parametrize("code", sorted([
    "checkout_total_over_approved", "checkout_total_unverified", "checkout_order_id_card_shaped",
    "checkout_payment_no_order", "checkout_payment_reached_unconfirmed", "checkout_payment_reached_declined",
    "checkout_answer_scrubbed"]))
async def test_the_operator_never_marks_a_recorded_payment_not_charged(code):
    pid = await _placing()
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = :c, claimed_by = NULL WHERE id = :i",
                           {"c": code, "i": pid})
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    expected = ("resolve-held", "--purchase-id", pid, "--expected-code", code)
    code_, [preview] = await _operator(*expected)
    assert (code_, preview["eligible"], preview["reason"]) == (3, False, "payment_reached_recorded")
    assert "merchant refunds" in preview["message"]
    code_, [result] = await _operator(*expected, "--apply", "--operator", "ops-1", "--expect-env", "staging",
                                      "--provider-checked")
    assert (code_, result["outcome"], result["reason"]) == (3, "refused", "payment_reached_recorded")
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", code)


async def test_the_minimum_hold_age_is_in_the_write_itself():
    pid = await _held_text_only()
    assert worker.OPERATOR_MIN_HOLD_AGE_SECONDS >= 3600
    # The ledger refuses a row held too recently even when called directly (the SQL of the compare-and-swap).
    assert await ledger.operator_fail_held_checkout(
        pid, expected_code="checkout_confirm_text_only", code="operator_confirmed_not_charged",
        min_age_seconds=worker.OPERATOR_MIN_HOLD_AGE_SECONDS) is None
    with pytest.raises(ValueError):
        await ledger.operator_fail_held_checkout(
            pid, expected_code="checkout_confirm_text_only", code="operator_confirmed_not_charged", min_age_seconds=60)
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 60, pid)
    assert await ledger.operator_fail_held_checkout(
        pid, expected_code="checkout_confirm_text_only", code="operator_confirmed_not_charged",
        min_age_seconds=worker.OPERATOR_MIN_HOLD_AGE_SECONDS) is not None


async def test_the_preview_reads_that_row_by_id_and_says_what_a_resolution_causes(monkeypatch):
    pid = await _held_text_only()
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 120, pid)

    async def _empty_list(**_):
        return []  # the row is not among the listed ones: the preview must still find it

    monkeypatch.setattr(ledger, "list_paying_checkouts", _empty_list)
    code, [preview] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                      "checkout_confirm_text_only")
    assert (code, preview["eligible"], preview["code"]) == (0, True, "checkout_confirm_text_only")
    assert preview["held_for_seconds"] >= worker.OPERATOR_MIN_HOLD_AGE_SECONDS
    assert "voids its token" in preview["on_resolve"]
    text = json.dumps(preview)
    for needle in (EMAIL, "ptk_1", "pvm_", "https://"):
        assert needle not in text


# ── a discount (S3, total_discounts) ────────────────────────────────────────────────────────


async def test_a_discounted_checkout_within_the_ceiling_confirms_end_to_end():
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(S3_DISCOUNTED))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()


# ── the setup answer's prose (unchanged by the allowlist) ──────────────────────────────────────


@pytest.mark.parametrize("prose", [
    "Your order total is 999.00", "You will pay 999.00", "Charged 999", "Grand total - 999.00", "Total:\n999.00",
])
def test_unmarked_prose_near_a_total_word_cancels(prose):
    from services.payment_orchestration import checkout_amounts as amounts

    assert amounts.text_refused(prose, TOTAL) is True


async def test_the_prose_scrub_never_removes_an_expiry_from_inside_an_amount():
    # The token's expiry is "1129": "$1129.00" is an amount, not the expiry, and is read (and refused).
    answer = _task({"kind": "text", "text": "Order total: $1129.00"},
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is True
    answer = _task({"kind": "text", "text": "Order total: $1129"},  # no cents: nothing else to read
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is True
    # In card context the expiry is still scrubbed, and so are the token's value and cryptogram anywhere.
    answer = _task({"kind": "text", "text": f"Card {DPAN} exp {EXPIRY_RAW} ({CRYPTO}). Total: $51.49"},
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(network_token=_network_token_with_cryptogram()), answer),
                         setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is False


#: A 13-digit Luhn-valid run (the transport's own card-number strip starts at 14 digits, so this one reaches
#: the worker), and the cryptogram without its padding (the padded form never gets past the transport).
LUHN_13 = "4222222222222"


#: An unpadded cryptogram that LOOKS like base64 (a "+" or "/" in it; a KSUID-style id never has one).
TAVV_UNPADDED = "AgAAAAAABk4DWZ4C28yUQ+/AAAA"


@pytest.mark.parametrize("order_id,rule", [
    (TAVV_UNPADDED, "cryptogram_shape"), (LUHN_13, "luhn_run"), (f"ORD-{LUHN_13}", "luhn_run"), (DPAN, "luhn_run"),
], ids=["cryptogram_unpadded", "luhn_13", "contains_luhn_13", "dpan"])
async def test_an_order_id_that_may_echo_the_card_or_token_completes_without_its_reference(order_id, rule, caplog):
    assert worker._credential_rule(order_id) == rule
    pid = await _placing()
    with caplog.at_level(logging.DEBUG):
        assert await _step(Rail().on("checkout.await#poll", _echo_result(order_id=order_id)), pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["last_error_code"]) == (
        "completed", None, worker.CHECKOUT_ORDER_REFERENCE_WITHHELD)
    assert worker.CHECKOUT_ORDER_REFERENCE_WITHHELD not in worker._CHECKOUT_HOLD_CODES  # not a hold
    # Log hygiene: the line names the rule and the length; the value, the order id and the token are nowhere
    # in the log or the stored row.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    stored = await _stored_text()
    assert f"rule={rule}" in logged and f"length={len(order_id)}" in logged
    for needle in {order_id, LUHN_13, DPAN, CRYPTO, CRYPTO.rstrip("=")}:
        assert needle not in logged, needle
        assert needle not in stored, needle


async def test_a_padded_cryptogram_or_a_card_number_inside_an_order_id_never_reaches_the_row():
    # The transport strips them from the answer: the order then names no id, which is held as such.
    for order_id in (CRYPTO, f"ORD-{DPAN}", f"#{DPAN}"):
        await database.execute("DELETE FROM a2a_rail_purchases")
        pid = await _placing()
        assert await _step(Rail().on("checkout.await#poll", _echo_result(order_id=order_id)), pid) == (
            worker.CHECKOUT_PAYMENT_NO_ORDER)
        stored = await _stored_text()
        assert CRYPTO.rstrip("=") not in stored and DPAN not in stored


def test_an_ordinary_order_id_is_not_card_or_cryptogram_shaped():
    import uuid

    import random
    import string

    rng = random.Random(7)
    base62 = string.ascii_letters + string.digits
    for order_id in ("1001", "ORD-12345", "112-1234567-1234567", str(uuid.uuid4()), uuid.uuid4().hex,
                     "pvt-ap_01234567.89abcdef.01234567", "SHOP-2026-ABCDEF", "48953700120034781234"):
        assert not worker._card_shaped(order_id), order_id
    # v6 (P2-H): modern alphanumeric ids are never taken for a cryptogram (the old rule held ~97% of them).
    for _ in range(500):
        for order_id in ("".join(rng.choice(base62) for _ in range(20)),                    # base62, 20
                         "ord_" + "".join(rng.choice(base62) for _ in range(24)),           # prefixed, 28
                         "".join(rng.choice(base62 + "_-") for _ in range(21)),             # nanoid, 21
                         "-".join("".join(rng.choice(string.digits) for _ in range(n)) for n in (3, 7, 7))):
            assert not worker._card_shaped(order_id), order_id


@pytest.mark.parametrize("status", ["unpaid", "payment_failed", "payment-declined", "incomplete", "not_paid",
                                    "unpaid_balance", "not_completed", "payment-pending", "awaiting-payment",
                                    "repayment", "payment_cancelled"])
async def test_a_negated_payment_status_is_unrecognised_never_payment_reached(status):
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(status)), pid) == (
        worker.CHECKOUT_RESULT_UNRECOGNISED)


@pytest.mark.parametrize("status", ["paid", "payment_reached", "completed", "payment-completed"])
async def test_an_unknown_status_that_speaks_of_payment_is_held_as_payment_reached(status):
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(status)), pid) == (
        worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    assert worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED in worker._CHECKOUT_PAYMENT_REACHED_CODES


async def test_an_unknown_status_that_does_not_speak_of_payment_stays_unrecognised():
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result("queued-elsewhere")), pid) == (
        worker.CHECKOUT_RESULT_UNRECOGNISED)


# ── status words (v6 P2-I, v8 P2-L) ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["paid_pending_shipment", "paid_awaiting_fulfillment", "completed_with_errors",
                                    "completed_notification_sent", "payment_completed_non_refundable",
                                    "paid_cancel_requested", "succeeded", "charged", "settled", "captured",
                                    "payment reached", "Payment-Completed"])
async def test_a_status_that_says_payment_happened_is_payment_reached_and_never_resolvable(status):
    assert worker._status_says_paid(status)
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(status)), pid) == (
        worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    assert worker.operator_eligibility(await _row(pid), worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED) == (
        False, "payment_reached_recorded")


@pytest.mark.parametrize("status", ["unpaid", "payment_failed", "payment-declined", "incomplete", "not_paid",
                                    "not_completed", "non_completed", "never_charged", "payment-pending",
                                    "awaiting-payment", "repayment", "payment_cancelled", "uncaptured"])
def test_a_status_that_does_not_say_payment_happened_stays_unrecognised(status):
    assert not worker._status_says_paid(status)


async def test_an_expiry_that_is_a_whole_number_in_prose_but_not_in_card_context_is_read():
    # H16: "1129" is our expiry, but here it is an amount due, not the card's expiry: it is read (and refused).
    answer = _task({"kind": "text", "text": "Amount due 1129 today"},
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is True


def test_a_ksuid_or_base64url_order_id_keeps_its_reference():
    import random
    import string

    rng = random.Random(11)
    base62 = string.ascii_letters + string.digits
    for _ in range(500):
        ksuid = "".join(rng.choice(base62) for _ in range(27))
        b64url = "".join(rng.choice(base62 + "-_") for _ in range(27))
        assert worker._credential_rule(ksuid) is None and worker._credential_rule(b64url) is None
    assert worker._credential_rule(TAVV_UNPADDED) == "cryptogram_shape"
    assert worker._credential_rule("AgAAAAAABk4DWZ4C28yUQAAAAAA=") == "cryptogram_shape"


@pytest.mark.parametrize("echo,refused", [
    ({"foo": "715"}, True), ({"securityCode": "715"}, True),
    ({"ref": "$715.00", "suite": "Suite 715", "n": "715 units"}, False),
], ids=["whole_value_unknown_key", "card_key", "inside_other_values"])
async def test_a_davv_under_its_own_field_is_scrubbed_from_the_setup_answer_as_a_whole_value(echo, refused):
    token = _network_token(**{a2a_rail.DAVV_FIELD: "715"})
    answer = _task(_card("checkout.confirm", {"merchant": "merchant.example", **echo}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(network_token=token), answer), setup=_setup(ceiling_minor=100000))
    assert (relay.checkout.credential_fields_stripped >= 1) is refused
    data = relay.checkout.card("checkout.confirm").data
    assert "715" not in [v for v in data.values() if isinstance(v, str)] or not refused


# ── scan v8: the confirm card is a strict allowlist that fails closed (checkout_amounts rules 1, 2) ──

from services.payment_orchestration import checkout_amounts as amounts  # noqa: E402
from tests.a2a_rail_confirm_card_corpus import CORPUS  # noqa: E402

#: The MEASURED sandbox confirm card: exactly cardBrand, cardLast4, merchant and shipping, the buyer address
#: our setup sends. Placeholder values only.
MEASURED_SHIPPING = {
    "email": "buyer@example.com", "first_name": "Test", "last_name": "Buyer", "phone": "+15550100000",
    "country": "US", "address1": "100 Example Way", "address2": "Unit 2", "city": "Sampleville", "state": "CA",
    "zip_code": "00000",
}
MEASURED_CARD = {"cardBrand": "visa", "cardLast4": "4399", "merchant": "merchant.example.com",
                 "shipping": MEASURED_SHIPPING}
_KNOWN = {"cardBrand": "visa", "cardLast4": "4399", "merchant": "merchant.example.com"}


def _s1(**kw):
    return {**MEASURED_CARD, "total": "51.49", "currency": "USD", **kw}


def _s2(**kw):
    """$40.00 + $1.49 of lines, $5.00 shipping, $5.00 tax: $51.49."""
    return {**_KNOWN, "line_items": [{"quantity": 2, "price": "20.00"}, {"quantity": 1, "price": "1.49"}],
            "subtotal": "41.49", "shipping": "5.00", "tax": "5.00", "total": "51.49", "currency": "USD", **kw}


def _s3(**kw):
    """$45.00 of lines less a $5.00 discount, $4.49 shipping, $2.00 tax: $46.49; before the discount $51.49, the
    ceiling (P1-L). Shopify's REST Order fields."""
    return {**MEASURED_CARD,
            "line_items": [{"quantity": 2, "price": "20.00", "compare_at_price": "25.00"},
                           {"quantity": 1, "price": "5.00", "compare_at_price": None}],
            "subtotal_price": "40.00", "shipping_lines": [{"price": "4.49"}], "total_tax": "2.00",
            "total_discounts": "5.00", "total_price": "46.49", "currency": "USD", **kw}


def _without(card, *keys):
    return {k: v for k, v in card.items() if k not in keys}


S3_DISCOUNTED = _s3()


def test_the_measured_confirm_card_has_nothing_money_like_and_proceeds():
    assert amounts.money_like(MEASURED_CARD) is False
    assert worker.confirm_card_refused(MEASURED_CARD, TOTAL) is False
    short = {**MEASURED_CARD, "shipping": _without(MEASURED_SHIPPING, "address2", "state")}
    assert worker.confirm_card_refused(short, TOTAL) is False
    # The address keys are the ones our setup sends, and no leaf's key holds a money word (`shipping` does,
    # but it holds an object, which is never money-like itself).
    assert set(MEASURED_SHIPPING) == amounts.ADDRESS_KEYS == set(
        a2a_rail.CHECKOUT_SHIPPING_REQUIRED + a2a_rail.CHECKOUT_SHIPPING_OPTIONAL)
    assert [k for k in [*MEASURED_CARD, *MEASURED_SHIPPING] if amounts._money_key(k)] == ["shipping"]


@pytest.mark.parametrize("extra", [{"x": "1500"}, {"x": "4999"}, {"orderRef": "40000000001"},
                                   {"shipping": {**MEASURED_SHIPPING, "window": "3-5"}}],
                         ids=["x_1500", "x_4999", "order_ref", "shipping_window"])
def test_a_bare_digit_string_under_a_non_money_key_proceeds_the_accepted_residual(extra):
    # checkout_amounts rule 1, RESIDUAL: not money-like (cents as text cannot be told from a zip code), so the
    # token cap, minted for exactly the ceiling, decides.
    card = {**MEASURED_CARD, **extra}
    assert amounts.money_like(card) is False
    assert worker.confirm_card_refused(card, TOTAL) is False


#: Beside the measured card, nothing money-like (rule 1): proceeds.
NOT_MONEY_LIKE = {
    "booleans": {"discountApplied": False, "giftWrap": True}, "none_under_total": {"total": None},
    "empty_under_price": {"price": ""}, "blank_under_fee": {"fee": "  "},
    "zip_plus4": {"shipping": {**MEASURED_SHIPPING, "zip_code": "00000-1234"}},
    "phone_formatted": {"shipping": {**MEASURED_SHIPPING, "phone": "(555) 010-0000"}},
    "version_string": {"note": "Version 7.1.5"}, "one_decimal": {"note": "1.5 lbs"},
    "suite": {"note": "Suite 715"},
    "title_case_code_word": {"shipping": {**MEASURED_SHIPPING, "address1": "12 Top St"}},
    "lower_case_code_glued_in_an_email": {"shipping": {**MEASURED_SHIPPING, "email": "eur2buy@example.com"}},
    "object_under_a_money_word": {"delivery": {"city": "Sampleville", "window": "3-5"}},
    "embedded_without_money": {"meta": '{"sku": "A-1"}'},
}
#: Beside the measured card, money-like (rule 1), in no named shape: cancels.
MONEY_LIKE = {
    # (a) a JSON number leaf anywhere
    "int": {"x": 15}, "float": {"x": 15.5}, "zero": {"x": 0}, "negative": {"x": -1}, "number_in_a_list": {"x": ["a", 1]},
    "number_nested": {"x": {"y": {"z": 2}}}, "card_last4_int": {"cardLast4": 4399},
    "zip_int": {"shipping": {**MEASURED_SHIPPING, "zip_code": 12345}},
    # (b) a currency or amount pattern in a string leaf or a key
    "dollar": {"ref": "$15.00"}, "dollar_no_cents": {"ref": "$15"}, "euro": {"note": "€15"}, "fullwidth": {"note": "＄15"},
    "symbol_after": {"note": "15 €"}, "code_before": {"note": "USD 15"}, "code_after": {"note": "15 EUR"},
    "code_glued": {"note": "15EUR"}, "usd_lower": {"note": "usd15"}, "usd_title": {"note": "Usd 15"},
    "two_decimals": {"x": "15.00"}, "grouped": {"x": "1,234.56"}, "in_prose": {"note": "Total: 40.00 today"},
    "invisible_inside": {"x": "$​15"}, "arabic_indic": {"x": "٤٠.٠٠"},
    "three_decimals": {"weightKg": "1.234"}, "lower_case_eur": {"note": "eur 40"},  # P1-K
    "key_holds_an_amount": {"$15.00": "x"}, "key_holds_a_code_amount": {"USD15": "x"},
    "embedded_json": {"meta": '{"total": 15}'}, "embedded_literal": {"meta": "{'x': 15}"},
    "address_with_an_amount": {"shipping": {**MEASURED_SHIPPING, "address2": "Fee $5.00"}},
    "merchant_with_an_amount": {"merchant": "Deals 40.00"},
    # (c) a string leaf whose own key holds a money word (any case or separator; a lookalike letter)
    "shipping_method": {"shipping_method": "standard"}, "upper_total": {"TOTAL": "n/a"},
    "mixed_case_cost": {"Shipping_Cost": "free"}, "lookalike_total": {"tоtal": "n/a"},
    "list_item_takes_its_lists_key": {"fees": ["waived"]},
}


@pytest.mark.parametrize("name", sorted(NOT_MONEY_LIKE))
def test_what_is_not_money_like_proceeds(name):
    card = {**MEASURED_CARD, **NOT_MONEY_LIKE[name]}
    assert amounts.money_like(card) is False
    assert worker.confirm_card_refused(card, TOTAL) is False


@pytest.mark.parametrize("name", sorted(MONEY_LIKE))
def test_money_under_an_innocuous_key_is_money_like_and_cancels(name):
    card = {**MEASURED_CARD, **MONEY_LIKE[name]}
    assert amounts.money_like(card) is True
    assert amounts.matched_shape(card, TOTAL) is None
    assert worker.confirm_card_refused(card, TOTAL) is True


@pytest.mark.parametrize("word", sorted(amounts._MONEY_KEY_WORDS))
def test_every_money_word_makes_a_string_under_its_key_money_like(word):
    assert amounts.money_like({word: "n/a"}) is True
    assert amounts.money_like({f"order_{word.upper()}_x": "n/a"}) is True
    assert amounts.money_like({word: True}) is False  # a boolean is never money-like


@pytest.mark.parametrize("code", sorted(amounts._ISO_4217))
def test_every_iso_code_next_to_a_digit_is_money_like(code):
    for text in (f"{code} 15", f"15{code}", f"15 {code}"):
        assert amounts.money_like({"note": text}) is True, text
    assert amounts.money_like({"note": f"{code}X 15"}) is False  # not a whole word
    assert amounts.money_like({"note": f"{code.lower()} 15"}) is (code in ("USD", "EUR", "GBP"))  # P1-K: eur, gbp


#: Within the ceiling, exactly a named shape: proceeds (shape name expected).
SHAPES_WITHIN = {
    "s1": ("S1", _s1()), "s1_int": ("S1", _s1(total=51)), "s1_float": ("S1", _s1(total=51.49)),
    "s1_zero": ("S1", _s1(total="0.00")), "s1_lower_usd": ("S1", _s1(currency="usd")),
    "s1_alone": ("S1", {"total": "40.00", "currency": "USD"}),
    "s1_known_keys_only": ("S1", _without(_s1(), "shipping")),
    "s2": ("S2", _s2()), "s2_minimal": ("S2", _without(_s2(total="41.49"), "shipping", "tax")),
    "s2_numbers": ("S2", _s2(line_items=[{"quantity": 1, "price": 41.49}], subtotal=41.49, shipping=5, tax=5.0,
                             total=51.49)),
    "s2_total_above_its_parts": ("S2", _s2(tax="1.00")),
    "s3": ("S3", _s3()), "s3_no_compare_at": ("S3", _s3(line_items=[{"quantity": 9, "price": "5.00"}])),
    "s3_minimal": ("S3", _without(_s3(subtotal_price="45.00", total_price="45.00"), "shipping_lines", "total_tax",
                                  "total_discounts")),
    "s3_compare_at_above_the_price": ("S3", _s3(line_items=[{"quantity": 2, "price": "20.00", "compare_at_price": "51.49"},
                                                            {"quantity": 1, "price": "5.00"}])),
}


@pytest.mark.parametrize("name", sorted(SHAPES_WITHIN))
def test_each_named_shape_within_the_ceiling_proceeds(name):
    shape, card = SHAPES_WITHIN[name]
    assert amounts.money_like(card) is True
    assert amounts.matched_shape(card, TOTAL) == shape
    assert worker.confirm_card_refused(card, TOTAL) is False


def _line(**kw):
    return [{"quantity": 2, "price": "20.00", **kw}, {"quantity": 1, "price": "1.49"}]


#: Every way a card near a named shape is not exactly it: cancels.
SHAPES_REFUSED = {
    # over the ceiling (an amount, the total, a line, a display-only price)
    "s1_over": _s1(total="51.50"), "s1_int_over": _s1(total=52),
    "s2_total_over": _s2(total="51.50"), "s2_line_over": _s2(line_items=[{"quantity": 1, "price": "51.50"}], subtotal="51.50"),
    # 2 x $30.00 is $60.00: read without its quantity, the line would match the $30.00 subtotal
    "s2_quantity_times_price_over": _s2(line_items=[{"quantity": 2, "price": "30.00"}], subtotal="30.00"),
    "s3_total_over": _s3(total_price="51.50"), "s3_shipping_line_over": _s3(shipping_lines=[{"price": "51.50"}]),
    # short: the total does not cover its parts; the subtotal is not its lines
    "s2_total_short": _s2(total="51.48"), "s2_subtotal_above_lines": _s2(subtotal="41.50"),
    "s2_subtotal_below_lines": _s2(subtotal="41.48"),
    "s3_total_short": _s3(total_price="46.48"), "s3_subtotal_before_discounts": _s3(subtotal_price="45.00"),
    "s3_discount_above_lines": _s3(total_discounts="45.01", subtotal_price="0.00"),
    # P1-L: the parts BEFORE the discount above the ceiling (the earlier standard fixture: $56.49 before $5.00 off)
    "s3_old_fixture_56_49_before_the_discount": _s3(
        line_items=[{"quantity": 2, "price": "20.00"}, {"quantity": 1, "price": "10.00"}], subtotal_price="45.00",
        total_price="51.49"),
    "s3_reviewers_102_98_before_the_discount": _s3(
        line_items=[{"quantity": 1, "price": "51.49"}], total_discounts="51.49", subtotal_price="0.00",
        shipping_lines=[{"price": "51.49"}], total_tax="0.00", total_price="51.49"),
    "s3_2x30_less_10": _s3(line_items=[{"quantity": 2, "price": "30.00"}], total_discounts="10.00",
                           subtotal_price="50.00", shipping_lines=[{"price": "0.00"}], total_tax="0.00",
                           total_price="50.00"),
    "s3_2x50_less_50": _s3(line_items=[{"quantity": 2, "price": "50.00"}], total_discounts="50.00",
                           subtotal_price="50.00", shipping_lines=[{"price": "0.00"}], total_tax="0.00",
                           total_price="50.00"),
    "s3_one_cent_over_before_the_discount": _s3(
        line_items=[{"quantity": 2, "price": "20.00"}, {"quantity": 1, "price": "5.01"}], subtotal_price="40.01",
        total_price="46.50"),
    # an extra key: at the top, in a line, in a shipping line, in the address
    "s1_extra_charge": _s1(shipping_cost="5.00"), "s1_extra_plain_key": _s1(note="thanks"),
    "s2_extra_discount": _s2(discount="5.00"), "s2_line_title": _s2(line_items=_line(title="Widget"), subtotal="41.49"),
    "s3_line_sku": _s3(line_items=[{"quantity": 9, "price": "5.00", "sku": "A-1"}]),
    "s3_shipping_line_title": _s3(shipping_lines=[{"price": "4.49", "title": "Ground"}]),
    "s1_address_extra_key": _s1(shipping={**MEASURED_SHIPPING, "company": "Example Co"}),
    "s3_address_extra_key": _s3(shipping={**MEASURED_SHIPPING, "method": "ground"}),
    # the wrong type (a money key: a number or a two-decimal string only; a bare digit string is a mismatch)
    "s1_dollar_string": _s1(total="$51.49"), "s1_bare_digits": _s1(total="1500"), "s1_bare_40": _s1(total="40"),
    "s1_one_decimal": _s1(total="51.5"), "s1_three_decimals": _s1(total=51.491), "s1_trailing_space": _s1(total="51.49 "),
    "s1_negative": _s1(total="-5.00"), "s1_negative_number": _s1(total=-5),
    "s1_bool_beside_money": _s1(total=True, note="$5.00"), "s1_object": _s1(total={"amount": "51.49"}), "s1_list": _s1(total=["51.49"]),
    "s1_leading_zero": _s1(total="051.49"), "s1_grouped": _s1(total="1,051.49"), "s1_nan": _s1(total=float("nan")),
    "s2_bare_price": _s2(line_items=[{"quantity": 1, "price": "40"}], subtotal="40.00", total="51.49"),
    "s2_bare_total": _s2(total="5149"), "s2_bare_subtotal": _s2(subtotal="4149"),
    "s2_dollar_shipping": _s2(shipping="$5.00"),
    "s3_bare_price": _s3(line_items=[{"quantity": 1, "price": "40"}], subtotal_price="40.00", total_discounts="0.00"),
    "s3_bare_total": _s3(total_price="45"), "s3_negative_discount": _s3(total_discounts="-5.00"),
    "s3_compare_at_dollar": _s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "$12.00"}]),
    "quantity_string": _s2(line_items=_line(quantity="2"), subtotal="41.49"),
    "quantity_float": _s2(line_items=_line(quantity=2.0), subtotal="41.49"),
    "quantity_zero": _s2(line_items=_line(quantity=0), subtotal="1.49"),
    "quantity_bool": _s2(line_items=[{"quantity": True, "price": "41.49"}], subtotal="41.49"),
    # the wrong currency
    "s1_eur": _s1(currency="EUR"), "s1_no_currency": _without(_s1(), "currency"), "s1_title_usd": _s1(currency="Usd"),
    "s1_padded_usd": _s1(currency=" USD"), "s1_numeric_currency": _s1(currency=840), "s2_eur": _s2(currency="EUR"),
    "s3_cad": _s3(currency="CAD"),
    # a duplicate, an alias, a case or separator variant (two totals)
    "s1_total_and_Total": _s1(Total="51.49"), "s1_upper_total": _without(_s1(TOTAL="51.49"), "total"),
    "s1_grand_total": _s1(grand_total="51.49"), "s2_sub_total": _s2(sub_total="41.49"),
    "s2_total_price_beside_total": _s2(total_price="51.49"), "s3_totalPrice": _s3(totalPrice="51.49"),
    "s3_with_s2_subtotal": _s3(subtotal="45.00"), "s3_shipping_amount_beside_lines": _s3(total_shipping="4.49"),
    # a quantity outside its line
    "s1_outer_quantity": _s1(quantity=2), "s2_outer_quantity": _s2(quantity=1), "s3_outer_qty": _s3(qty=2),
    "s2_quantity_in_a_child": _s2(line_items=[{"quantity": 1, "price": "20.00", "details": {"quantity": 2}},
                                              {"quantity": 1, "price": "21.49"}]),
    # unexpected nesting
    "s2_lines_as_an_object": _s2(line_items={"a": {"quantity": 1, "price": "41.49"}}),
    "s2_no_lines": _s2(line_items=[], subtotal="0.00"), "s3_no_shipping_lines": _s3(shipping_lines=[]),
    "s2_line_a_string": _s2(line_items=["41.49"]), "s1_nested_in_summary": {"summary": {"total": "51.49", "currency": "USD"}},
    # a known key that is not the measured one: money in it, a number, the address where an amount belongs
    "s1_merchant_with_money": _s1(merchant="Store $60.00"), "s1_last4_int": _s1(cardLast4=4399),
    "s1_merchant_embedded": _s1(merchant='{"total": 60}'), "s2_address_as_shipping": _s2(shipping=MEASURED_SHIPPING),
    "s1_shipping_amount": _s1(shipping="5.00"), "s3_shipping_amount": _s3(shipping="4.49"),
    "s1_address_with_money": _s1(shipping={**MEASURED_SHIPPING, "address2": "$15.00"}),
    "s1_address_number": _s1(shipping={**MEASURED_SHIPPING, "zip_code": 12345}),
    "s1_address_list_value": _s1(shipping={**MEASURED_SHIPPING, "state": ["CA"]}),
    "s1_merchant_object": _s1(merchant={"name": "Example"}),
    "s2_bool_price": _s2(line_items=[{"quantity": 1, "price": True}], subtotal="1.00", total="11.00"),
}


@pytest.mark.parametrize("name", sorted(SHAPES_REFUSED))
def test_a_card_that_is_not_exactly_a_named_shape_within_the_ceiling_cancels(name):
    card = SHAPES_REFUSED[name]
    assert amounts.matched_shape(card, TOTAL) is None
    assert worker.confirm_card_refused(card, TOTAL) is True


#: compare_at_price (S3) is display only: exempt from the ceiling, still type-checked, never summed, never the total
#: and never a price. Each card is S3-shaped; `None` = it proceeds, else the reason it cancels.
COMPARE_AT_CASES = {
    # exempt from the ceiling: a list price above what the buyer approved proceeds when the rest is S3 in range
    "over_the_ceiling_string": (_s3(line_items=[{"quantity": 2, "price": "20.00", "compare_at_price": "99999.00"},
                                                {"quantity": 1, "price": "5.00"}]), None),
    "over_the_ceiling_number": (_s3(line_items=[{"quantity": 2, "price": "20.00", "compare_at_price": 1000000},
                                                {"quantity": 1, "price": "5.00", "compare_at_price": 75.5}]), None),
    "one_cent_over_the_ceiling": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "51.50"}]), None),
    # still type-checked
    "dollar_string": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "$99.00"}]), "type"),
    "negative": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "-1.00"}]), "type"),
    "negative_number": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": -1}]), "type"),
    "bare_digits": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "9999"}]), "type"),
    "boolean": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": True}]), "type"),
    "words": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": "was 12"}]), "type"),
    "object": (_s3(line_items=[{"quantity": 9, "price": "5.00", "compare_at_price": {"amount": "12.00"}}]), "type"),
    # the price, the total and the sums still cap, whatever the compare_at_price
    # (a price over the ceiling under a discount that brings the charged sums within it: its own cap and the cap
    # before the discount both refuse it)
    "price_over_the_ceiling": (_s3(line_items=[{"quantity": 1, "price": "51.50", "compare_at_price": "60.00"}],
                                   subtotal_price="41.50", total_discounts="10.00", shipping_lines=[{"price": "0.00"}],
                                   total_tax="0.00", total_price="41.50"), "price"),
    "total_over_the_ceiling": (_s3(total_price="51.50"), "total"),
    "subtotal_over_the_ceiling": (
        _s3(line_items=[{"quantity": 2, "price": "30.00", "compare_at_price": "40.00"}], subtotal_price="55.00",
            total_discounts="5.00", total_price="51.49", total_tax="0.00", shipping_lines=[{"price": "0.00"}]),
        "sums"),
    "parts_add_up_past_the_ceiling": (
        _s3(line_items=[{"quantity": 1, "price": "45.00", "compare_at_price": "90.00"}], subtotal_price="45.00",
            total_discounts="0.00", shipping_lines=[{"price": "5.00"}], total_tax="2.00", total_price="51.49"),
        "sums"),
    # never a price, never summed: a compare_at_price that would make the sums reconcile does not
    # (read as the price, this line would make every sum reconcile: $45.00 + $4.49 + $2.00 = $51.49)
    "the_only_amount_of_a_line": (_s3(line_items=[{"quantity": 1, "compare_at_price": "45.00"}],
                                      total_discounts="0.00", subtotal_price="45.00", total_price="51.49"), "shape"),
    "would_reconcile_a_short_subtotal": (
        _s3(line_items=[{"quantity": 1, "price": "45.00", "compare_at_price": "30.00"}], subtotal_price="30.00",
            total_discounts="0.00", total_price="30.00", total_tax="0.00", shipping_lines=[{"price": "0.00"}]), "short"),
    "would_reconcile_if_added": (
        _s3(line_items=[{"quantity": 1, "price": "10.00", "compare_at_price": "35.00"}], subtotal_price="45.00",
            total_discounts="0.00", total_price="51.49"), "short"),
    "short_total_beside_a_large_compare_at": (
        _s3(line_items=[{"quantity": 2, "price": "20.00", "compare_at_price": "500.00"},
                        {"quantity": 1, "price": "5.00"}], total_price="46.48"), "short"),
}


@pytest.mark.parametrize("name", sorted(COMPARE_AT_CASES))
def test_compare_at_price_is_display_only_exempt_from_the_ceiling_and_never_counted(name):
    card, refused_for = COMPARE_AT_CASES[name]
    assert amounts.matched_shape(card, TOTAL) == (None if refused_for else "S3")
    assert worker.confirm_card_refused(card, TOTAL) is (refused_for is not None)


async def test_an_over_ceiling_compare_at_price_confirms_end_to_end():
    pid = await _approving()
    card = _task(_card("checkout.confirm", COMPARE_AT_CASES["over_the_ceiling_string"][0],
                       ("confirm", "edit", "cancel"), kind="confirm"))
    rail = _approved(Rail(), setup_answer=card)
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()


def test_the_shape_bounds_are_exact_to_the_cent():
    # The total at the ceiling proceeds, a cent over cancels; a total equal to its parts proceeds, a cent short cancels.
    assert worker.confirm_card_refused(_s1(total="51.49"), TOTAL) is False
    assert worker.confirm_card_refused(_s1(total="51.49"), TOTAL - 1) is True
    assert worker.confirm_card_refused(_s2(), TOTAL) is False and worker.confirm_card_refused(_s2(), TOTAL - 1) is True
    assert worker.confirm_card_refused(_s3(), TOTAL) is False and worker.confirm_card_refused(_s3(), TOTAL - 1) is True
    assert worker.confirm_card_refused(_s3(total_discounts="45.00", subtotal_price="0.00", total_price="6.49"), TOTAL) is False
    # compare_at_price is display only: never summed (the lines would not reach the subtotal), never the total.
    shown = _s3(line_items=[{"quantity": 2, "price": "20.00", "compare_at_price": "30.00"},
                            {"quantity": 1, "price": "5.00", "compare_at_price": "11.00"}])
    assert amounts.matched_shape(shown, TOTAL) == "S3"
    assert worker.confirm_card_refused(_s3(total_price="51.49", total_tax="30.00"), TOTAL) is True  # parts 79.49


@pytest.mark.parametrize("shape", ["s1", "s2", "s3"])
async def test_each_named_shape_within_the_ceiling_confirms_end_to_end(shape):
    pid = await _approving()
    card = {"s1": _s1, "s2": _s2, "s3": _s3}[shape]()
    rail = _approved(Rail(), setup_answer=_task(_card("checkout.confirm", card, ("confirm", "edit", "cancel"),
                                                      kind="confirm")))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions() and "checkout.confirm#cancel" not in rail.actions()


@pytest.mark.parametrize("name", ["s1_bare_digits", "s2_total_short", "s3_subtotal_before_discounts",
                                  "s1_shipping_amount", "s1_total_and_Total"])
async def test_a_near_shape_cancels_end_to_end(name):
    pid = await _approving()
    card = _task(_card("checkout.confirm", SHAPES_REFUSED[name], ("confirm", "edit", "cancel"), kind="confirm"))
    rail = _approved(Rail(), setup_answer=card).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "checkout_confirm_total_refused"


# ── the regression corpus: every attack shape of the v4-v7 reviews and the earlier tests ──────

_DOLLARS_RE = re.compile(r"^\$([0-9]+\.[0-9]{2})\Z")


def _undollared(value):
    """Every "$D.DD" string written "D.DD", and currency USD beside the top level: so the shape checks, not
    the "$", are what refuses the card."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v]
        match = _DOLLARS_RE.match(v) if isinstance(v, str) else None
        return match.group(1) if match else v
    out = strip(value)
    return {**out, "currency": "USD"} if isinstance(out, dict) and "currency" not in out else out


#: The corpus cards that PROCEED, and why (neither is an over-ceiling charge):
CORPUS_PROCEEDS = {
    # rule 1's accepted residual: no number, no currency or amount pattern, no money word ("3-5" is digits)
    ("492_test_every_amount_the_scan_cannot_read_with_cert", "as_reported"): "rule1",
    ("492_test_every_amount_the_scan_cannot_read_with_cert", "undollared"): "rule1",
    # {"total": "40.00"} with currency USD added: literally S1, $40.00 under the $51.49 cap
    ("082_test_an_unreadable_or_foreign_confirm_card_total", "undollared"): "S1",
}
_CORPUS_CASES = [(rid, ceiling, card, "as_reported") for rid, ceiling, card in CORPUS] + [
    (rid, ceiling, _undollared(card), "undollared") for rid, ceiling, card in CORPUS if _undollared(card) != card]


@pytest.mark.parametrize("rid,ceiling,card,variant", _CORPUS_CASES, ids=[f"{c[0]}-{c[3]}" for c in _CORPUS_CASES])
def test_every_attack_shape_from_the_reviews_cancels_unless_it_is_literally_a_named_shape_under_the_cap(
        rid, ceiling, card, variant):
    expected = CORPUS_PROCEEDS.get((rid, variant))
    assert worker.confirm_card_refused(card, ceiling) is (expected is None)
    if expected == "rule1":
        assert amounts.money_like(card) is False
    elif expected is not None:
        assert amounts.matched_shape(card, ceiling) == expected


def test_the_corpus_is_whole_and_its_exceptions_are_its_own():
    assert len(CORPUS) == 615 and len({rid for rid, _, _ in CORPUS}) == 615
    ids = {(c[0], c[3]) for c in _CORPUS_CASES}
    assert set(CORPUS_PROCEEDS) <= ids


# ── v8 P2-L: a status is split on every character that is not a letter or a digit ─────────────

@pytest.mark.parametrize("status", ["Paid.", "paid!", "order.paid", "payment:captured", "status=paid", "paid,shipped",
                                    "payment.reached", "PAID/SHIPPED", "chargedback", "chargeback", "Charged-Back",
                                    "not.refunded.paid"])
async def test_a_status_split_on_any_punctuation_or_a_chargeback_is_payment_reached_and_never_resolvable(status):
    assert worker._status_says_paid(status)
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(status)), pid) == (
        worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    assert worker.operator_eligibility(await _row(pid), worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED) == (
        False, "payment_reached_recorded")


@pytest.mark.parametrize("status", ["not.paid", "not:paid", "never=charged", "non/captured", "Not paid.", "notpaid",
                                    "not.completed", "no,charge", "uncaptured", "payment.failed"])
def test_a_negated_status_split_on_any_punctuation_stays_unrecognised(status):
    assert not worker._status_says_paid(status)


# ── scan v8 review: P1-K (rule 1 reads what the prose rule reads as an amount), P3-v8-1 (base64 JSON) ──

#: Amount strings the v8 rule 1 let through under a plain key: each is money-like now, and the card cancels.
RULE_1_ESCAPES = ["Total charged: 999 dollars", "Your total is 999", "You will pay 999 at delivery", "999 dollars",
                  "999 bucks", "999 euros", "999 eur", "999,00", "1.234,56", "999.000", "٩٩٩"]
#: THE PINNED RESIDUAL (the owner's decision: a known gap; the token cap is the backstop): still not money-like
#: under a plain key, so the card PROCEEDS. Any change to one of these shows here.
RULE_1_RESIDUAL = ["999.5", "60.0", "1e3", "Rs 999", "nine hundred ninety-nine dollars", "1 000"]
#: Ordinary strings that must stay not money-like (the measured card's own fields among them).
RULE_1_BENIGN = ["visa", "4399", "merchant.example.com", "buyer@example.com", "Test", "Buyer", "+15550100000", "US",
                 "100 Example Way", "Unit 2", "Sampleville", "CA", "00000", "00000-1234", "(555) 010-0000",
                 "100 Euro Plaza", "Euro 24 Shop", "30 pound bag", "3-5 days", "Size 10", "5 stars", "v1.2.3",
                 "2.5 lb", "Rating 4.5", "2026-11-19", "11/19/2026", "Nov 19, 2026", "12 Top St", "Apt 4",
                 "Gel Pen 12 pack", "Ships in 7 days", "SKU-40000000001", "Order #1001", "Suite 715", "Version 7.1.5",
                 "Dog food 30 pound bag", "Gate code 1234", "2-Day", "3M", "Size 10.5", "Pack of 12", "7 items"]


@pytest.mark.parametrize("text", RULE_1_ESCAPES)
def test_an_amount_the_prose_rule_would_read_is_money_like_on_a_card_leaf_and_cancels(text):
    card = {**MEASURED_CARD, "note": text}
    assert amounts.money_like(card) is True
    assert worker.confirm_card_refused(card, TOTAL) is True


@pytest.mark.parametrize("text", RULE_1_RESIDUAL)
def test_the_pinned_rule_1_residual_still_proceeds(text):
    card = {**MEASURED_CARD, "note": text}
    assert amounts.money_like(card) is False
    assert worker.confirm_card_refused(card, TOTAL) is False


@pytest.mark.parametrize("text", RULE_1_BENIGN)
def test_ordinary_strings_stay_not_money_like(text):
    assert amounts.money_like({**MEASURED_CARD, "note": text}) is False


def test_the_escape_and_benign_measurement_is_11_of_17_and_0_of_42():
    assert len(RULE_1_ESCAPES) + len(RULE_1_RESIDUAL) == 17 and len(RULE_1_BENIGN) == 42
    assert sum(amounts.money_like({"note": t}) for t in RULE_1_ESCAPES + RULE_1_RESIDUAL) == 11
    assert sum(amounts.money_like({"note": t}) for t in RULE_1_BENIGN) == 0


@pytest.mark.parametrize("data", ["Total charged: 999 dollars", "hello", ["x"], [], 5, None],
                         ids=["string_with_an_amount", "plain_string", "list", "empty_list", "number", "none"])
def test_card_data_that_is_not_an_object_cancels(data):
    assert worker.confirm_card_refused(data, TOTAL) is True


def test_a_base64_json_blob_is_judged_like_embedded_data():
    import base64

    blob = base64.b64encode(json.dumps({"total": 999}).encode()).decode()
    urlsafe = base64.urlsafe_b64encode(json.dumps({"note": "Your total is 999"}).encode()).decode().rstrip("=")
    plain = base64.b64encode(json.dumps({"sku": "A-1"}).encode()).decode()
    assert amounts.money_like({**MEASURED_CARD, "blob": blob}) is True
    assert amounts.money_like({**MEASURED_CARD, "blob": urlsafe}) is True
    assert amounts.money_like({**MEASURED_CARD, "blob": plain}) is False
    # An id that merely looks like base64 is not data: never money-like for it.
    assert amounts.money_like({**MEASURED_CARD, "ref": "AgAAAAAABk4DWZ4C28yUQAAAAAA"}) is False


def test_the_prose_rule_and_rule_1_share_one_amount_shape_test():
    # Reused, not copied: the prose rule refuses what rule 1 now reads as money (and the prose rule is unchanged).
    for text in ("Your total is 999", "You will pay 999 at delivery", "Total charged: 999 dollars"):
        assert amounts.text_refused(text, TOTAL) is True and amounts._prose_money(text) is True


# ── the provider's answer (11-18): DAVV, not TAVV; resolving an unseen answer by lookup ─────────

async def test_on_the_checkout_path_the_token_is_minted_with_a_davv(monkeypatch):
    pid = await _unlocked()
    rail = _lock_rail()
    assert await _step(rail, pid) == "approval_page_issued"
    assert rail.payload("payment.token-setup#submit")["dynamic_data_type"] == "DAVV"
    # The picker (naming the card still answers with it) mints too: it is asked the same way.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _unlocked()
    picker = _task(_card("payment.method-list", {"methods": [VISA]}, ("select-method",), kind="list"))
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", picker).on(
        "payment.method-list#select-method", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert rail.payload("payment.method-list#select-method")["dynamic_data_type"] == "DAVV"


async def test_off_the_checkout_path_the_mint_is_exactly_as_before(monkeypatch):
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "0")
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert "dynamic_data_type" not in rail.payload("payment.token-setup#submit")


@pytest.mark.parametrize("value", [None, "", "7150", "71", "７１５", " 715", "715 ", "7a5", 715, CRYPTO],
                         ids=["absent", "empty", "four_digits", "two_digits", "fullwidth", "lead_space", "trail_space",
                              "letter", "int", "tavv_in_the_davv_field"])
async def test_without_a_three_ascii_digit_davv_the_checkout_fails_closed_and_nothing_is_sent(value):
    token = _network_token(**{a2a_rail.DAVV_FIELD: value})
    if value is None:
        token.pop(a2a_rail.DAVV_FIELD)
    assert a2a_rail.davv_from_network_token(token) is None
    assert a2a_rail.checkout_card_from_network_token(token, brand="visa", name="Pat Example") is None
    provider = _Provider(_token_result(network_token=token))
    relay = await _relay(provider)
    assert relay.refused == "davv_missing" and relay.checkout is None
    assert [d["component"] for d in provider.sent()] == ["payment.visa-action-required"]  # no setup went out


def test_the_cryptogram_is_never_the_cvv_even_when_it_is_three_digits():
    token = _network_token(cryptogram="715")
    token.pop(a2a_rail.DAVV_FIELD)
    assert a2a_rail.davv_from_network_token(token) is None
    assert a2a_rail.checkout_card_from_network_token(token, brand="visa", name="Pat Example") is None


async def test_the_worker_fails_closed_as_davv_missing_before_anything_reaches_the_checkout():
    pid = await _approving()
    token = _network_token()
    token.pop(a2a_rail.DAVV_FIELD)
    rail = Rail().on("payment.visa-action-required#paid", _token_result(network_token=token))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.visa-action-required#paid"]
    assert (await _row(pid))["last_error_code"] == "checkout_card_unavailable:davv_missing"


async def test_the_card_is_exactly_the_five_normal_card_keys_and_never_the_cryptogram_or_eci():
    # THE ONE CASE with a non-empty cryptogram and an ECI ("05"): neither is sent, the cryptogram never as cvv.
    token = _network_token_with_cryptogram()
    assert (token["cryptogram"], token["eci"]) == (CRYPTO, "05")
    card = a2a_rail.checkout_card_from_network_token(token, brand="visa", name="Pat Example")
    assert card == {"number": DPAN, "expiry": "11/29", "cvv": DAVV, "name": "Pat Example", "brand": "visa"}
    assert a2a_rail.davv_from_network_token({**token, "cvv": None}) is None  # no fallback to the cryptogram
    provider = _Provider(_token_result(network_token=token), CONFIRM_CARD)
    await _relay(provider)
    setup = provider.sent()[1]["payload"]
    assert set(setup["card"]) == {"number", "expiry", "cvv", "name", "brand"}
    text = json.dumps(setup)
    assert CRYPTO not in text and CRYPTO.rstrip("=") not in text and "cryptogram" not in text
    assert "eci" not in json.dumps(list(setup)) + json.dumps(list(setup["card"]))


@pytest.mark.parametrize("name", ["", "   ", None])
async def test_the_card_name_is_never_empty_when_it_is_sent(name):
    assert a2a_rail.checkout_card_from_network_token(_network_token(), brand="visa", name=name) is None
    provider = _Provider(_token_result())
    relay = await _relay(provider, setup=_setup(cardholder_name=name) if name is not None else _setup(cardholder_name=""))
    assert relay.refused is not None and relay.checkout is None
    assert len(provider.sent()) == 1


def test_the_card_name_is_the_buyers_first_and_last_name_from_the_shipping_contact():
    row = {"id": "ap_" + "0" * 24, "buyer_email": EMAIL, "shipping_address": ADDRESS,
           "checkout_url": "https://merchant.example/cart/40000000001:1", "quoted_total_minor": TOTAL,
           "currency": "USD"}  # every stored row has its currency; the relay pairs it with the address's market
    assert worker._checkout_setup(row).cardholder_name == "Pat Example"


def _plain_confirm(**data):
    return _task(_card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478", **data},
                       ("confirm", "edit", "cancel"), kind="confirm"))


async def test_a_number_equal_to_the_davv_in_an_address_an_amount_or_prose_is_never_scrubbed():
    answer = _plain_confirm(shipping={"address2": "Suite 715", "city": "Austin"}, total="$715.00", ref="715 units")
    answer["result"]["history"][0]["parts"].insert(0, {"kind": "text", "text": "Ship to Suite 715. Order total: $715.00"})
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=100000))
    confirm = relay.checkout.card("checkout.confirm")
    assert relay.checkout.credential_fields_stripped == 0  # nothing was "had to scrub"
    assert (confirm.data["shipping"]["address2"], confirm.data["total"], confirm.data["ref"]) == (
        "Suite 715", "$715.00", "715 units")
    assert relay.checkout.text_amounts_refused is False


@pytest.mark.parametrize("value", ["715", 715], ids=["string", "int"])
async def test_the_davv_as_a_whole_value_under_any_key_is_scrubbed_and_refused(value):
    # The scan fix (v6, P3-v6-1): the DAVV is a whole-value needle like the expiry, whatever key carries it.
    relay = await _relay(_Provider(_token_result(), _plain_confirm(ref=value)), setup=_setup(ceiling_minor=100000))
    assert relay.checkout.credential_fields_stripped >= 1
    assert relay.checkout.card("checkout.confirm").data["ref"] is None


@pytest.mark.parametrize("echo", [{"cvv": DAVV}, {"card": {"code": DAVV}}, {"card": {"securityCode": 715}},
                                  {"cardCvv": DAVV}, {"payment": {"card": {"cvc": DAVV}}},
                                  {"verificationCode": DAVV}, {"cid": DAVV}, {"dynamicDataValue": DAVV},
                                  {"payment": {"code": DAVV}}, {"securityValue": DAVV}, {"pin": 715},
                                  {"cards": [DAVV]}, {"cardExtra": DAVV}, {"cvn": DAVV}],
                         ids=["cvv", "card_code", "card_security_code_int", "card_cvv_key", "nested_card",
                              "verification_code", "cid", "dynamic_data_value", "payment_code", "security_value",
                              "pin_int", "list_under_a_card_key", "card_prefix", "cvn"])
async def test_the_davv_echoed_under_a_card_key_is_scrubbed_and_the_answer_refused(echo):
    relay = await _relay(_Provider(_token_result(), _plain_confirm(**echo)))
    assert relay.checkout.credential_fields_stripped >= 1
    assert DAVV not in json.dumps(relay.checkout.card("checkout.confirm").data)
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_plain_confirm(**echo)).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


# ── resolving an unseen answer by lookup ─────────────────────────────────────────────────────

async def _held(code, *, pending=None, task=None, age=120):
    """A placed checkout turned into a HELD one: `code`, the write `pending`, the task id `task` (or none)."""
    pid = await _placing()
    await database.execute(
        "UPDATE a2a_rail_purchases SET last_error_code = :c, pending_write = :p, checkout_task_id = :t, "
        "claimed_by = NULL WHERE id = :i", {"c": code, "p": pending, "t": task, "i": pid})
    await _ago("state_entered_at", age, pid)
    return pid


#: Each lookup-resolvable hold, with the write it keeps (if any).
LOOKUP_HOLDS = [
    ("checkout_confirm_unresolved", worker.CHECKOUT_CONFIRM_ACTION),
    ("checkout_setup_unresolved", None),
    ("checkout_confirm_pending_check", None),
    ("checkout_paying_without_task", None),
    ("checkout_cancel_unresolved", worker.CHECKOUT_CANCEL_ACTION),
    ("checkout_confirm_text_only", worker.CHECKOUT_CONFIRM_ACTION),
    ("checkout_confirm_unrecognised", worker.CHECKOUT_CONFIRM_ACTION),
    ("checkout_confirm_error_card", worker.CHECKOUT_CONFIRM_ACTION),
]


def _lookup_only(rail, pid):
    """Every send was a read-only lookup by our idempotency key, in the lookup conversation."""
    assert rail.sent and all(x["key"] == "checkout.lookup#submit" for x in rail.sent), rail.actions()
    assert all(x["payload"] == {"task_id": f"pvt-{pid}-checkout"} for x in rail.sent)
    assert all("_l" in x["context"] for x in rail.sent)


@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS, ids=[c for c, _ in LOOKUP_HOLDS])
async def test_a_lookup_that_finds_our_own_tasks_result_completes_on_a_verified_total(code, pending):
    # Condition 6: a BOUND task (recorded from our own confirm answer) keeps the current rules.
    pid = await _held(code, pending=pending, task="ckout_abc123")
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await _step(rail, pid) == "completed"
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["checkout_task_id"], row["pending_write"]) == (
        "completed", "1001", "ckout_abc123", None)


@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS, ids=[c for c, _ in LOOKUP_HOLDS])
async def test_an_unbound_completed_lookup_answer_never_completes_and_is_held_as_payment_reached(code, pending):
    # N1, conditions 1 and 2: a completed, successful answer with a verified total on a task this row never
    # recorded: no completion, no order id, no total, no task; the hold moves to the payment-reached set.
    pid = await _held(code, pending=pending)
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await _step(rail, pid) == worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["merchant_order_id"], row["final_total_minor"],
            row["checkout_task_id"], row["pending_write"], row["terminal_at"]) == (
        "paying", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED, None, None, None, pending, None)


@pytest.mark.parametrize("answer", [
    _checkout_result("form-error", success=False, order_id=None, total=None),
    _checkout_result("captcha-failed", success=False, order_id=None, total=None),
    _checkout_result("pay-disabled", success=False, order_id=None, total=None),
], ids=["form_error", "captcha_failed", "pay_disabled"])
@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS, ids=[c for c, _ in LOOKUP_HOLDS])
async def test_a_not_submitted_lookup_answer_on_a_task_we_never_recorded_keeps_the_hold(code, pending, answer):
    # P2-1: nothing ties the answer to OUR checkout (another purchase's result could come back): never failed.
    pid = await _held(code, pending=pending)
    rail = Rail().on("checkout.lookup#submit", answer)
    assert await _step(rail, pid) == "checkout_held"
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["checkout_task_id"]) == (
        "paying", code, pending, None)


async def test_purchase_a_held_never_fails_on_purchase_bs_not_submitted_result():
    # One buyer: A held confirm_unresolved (it may be charged); B ended form-error. A's lookup gets B's result.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    b_result = _checkout_result("form-error", task="ckout_B", success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.lookup#submit", b_result), pid) == "checkout_held"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["checkout_task_id"], row["pending_write"]) == (
        "paying", "checkout_confirm_unresolved", None, worker.CHECKOUT_CONFIRM_ACTION)


async def test_a_not_submitted_lookup_answer_on_our_own_recorded_task_fails_the_purchase():
    pid = await _held("checkout_confirm_text_only", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    rail = Rail().on("checkout.lookup#submit", _checkout_result("form-error", success=False, order_id=None, total=None))
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["last_error_code"], row["pending_write"]) == ("checkout_form_error", None)


async def test_a_lookup_on_our_recorded_task_with_a_pending_write_completes_and_resolves_it():
    # M19: the write nobody saw is resolved by the lookup's answer (never resent).
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    assert await _step(Rail().on("checkout.lookup#submit", _checkout_result()), pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["merchant_order_id"]) == ("completed", None, "1001")


@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS, ids=[c for c, _ in LOOKUP_HOLDS])
async def test_a_lookup_that_finds_an_unbound_task_in_progress_keeps_the_hold_and_adopts_nothing(code, pending):
    # P2-1a: an await on a task the row never recorded is not adopted (nor noted): still held, the write kept.
    pid = await _held(code, pending=pending)
    rail = Rail().on("checkout.lookup#submit", _checkout_await())
    assert await _step(rail, pid) == "checkout_held"
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["checkout_task_id"], row["last_error_code"], row["pending_write"]) == (
        "paying", None, code, pending)


async def test_a_lookup_that_finds_our_own_task_in_progress_records_it_and_the_poll_goes_on():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    rail = Rail().on("checkout.lookup#submit", _checkout_await())
    assert await _step(rail, pid) == "placing"
    row = await _row(pid)
    assert (row["checkout_task_id"], row["last_error_code"], row["pending_write"]) == (
        "ckout_abc123", worker.CHECKOUT_PLACING, None)
    poll = Rail().on("checkout.await#poll", _checkout_result())
    assert await _step(poll, pid) == "completed"
    assert poll.actions() == ["checkout.await#poll"]


async def test_purchase_a_never_fails_on_purchase_bs_task_found_in_progress_then_not_submitted():
    # The reviewer's P2-1a probe: A held with no task; its lookup answers await on B's task; B later ends
    # form-error. A is still held, nothing was resolved, the write is kept.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    assert await _step(Rail().on("checkout.lookup#submit", _checkout_await(task="ckout_B")), pid) == "checkout_held"
    b_failed = _checkout_result("form-error", task="ckout_B", success=False, order_id=None, total=None)
    rail = Rail().on("checkout.lookup#submit", b_failed).on("checkout.await#poll", b_failed)
    assert await _step(rail, pid) == "checkout_held"
    assert "checkout.await#poll" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["checkout_task_id"], row["pending_write"], row["terminal_at"]) == (
        "paying", "checkout_confirm_unresolved", None, worker.CHECKOUT_CONFIRM_ACTION, None)


@pytest.mark.parametrize("status", ["payment-reached", "paid"])
async def test_an_unbound_payment_reached_answer_with_success_false_is_held_as_payment_reached(status):
    # P2-4: the provider said payment reached; that is never an operator-resolvable hold.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    answer = _checkout_result(status, task="ckout_B", success=False)
    assert await _step(Rail().on("checkout.lookup#submit", answer), pid) == worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    expected = ("resolve-held", "--purchase-id", pid, "--expected-code", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    code_, [preview] = await _operator(*expected)
    assert (code_, preview["eligible"], preview["reason"]) == (3, False, "payment_reached_recorded")
    code_, [result] = await _operator(*expected, "--apply", "--operator", "ops-1", "--expect-env", "staging",
                                      "--provider-checked")
    assert (code_, result["outcome"], result["reason"]) == (3, "refused", "payment_reached_recorded")
    assert (await _row(pid))["state"] == "paying"


async def test_the_lookup_count_sql_itself_refuses_at_the_cap():
    # N2 (W09): a second line behind the worker's own check.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    await _ago("next_poll_at", 1, pid)
    await ledger.claim_due_purchases(W, limit=5)
    await database.execute("UPDATE a2a_rail_purchases SET checkout_lookups = :n WHERE id = :i",
                           {"n": worker.CHECKOUT_LOOKUP_MAX_TRIES, "i": pid})
    assert await ledger.count_checkout_lookup_as_holder(pid, W, max_tries=worker.CHECKOUT_LOOKUP_MAX_TRIES) is None
    await database.execute("UPDATE a2a_rail_purchases SET checkout_lookups = :n WHERE id = :i",
                           {"n": worker.CHECKOUT_LOOKUP_MAX_TRIES - 1, "i": pid})
    counted = await ledger.count_checkout_lookup_as_holder(pid, W, max_tries=worker.CHECKOUT_LOOKUP_MAX_TRIES)
    assert counted is not None and counted["checkout_lookups"] == worker.CHECKOUT_LOOKUP_MAX_TRIES


@pytest.mark.parametrize("answer", [httpx.Response(404), httpx.Response(500), _text_only("No such checkout."),
                                    _task(_card("error", {"code": "NOT_FOUND", "recoverable": False}))],
                         ids=["404", "500", "text", "error_card"])
@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS[:2], ids=[c for c, _ in LOOKUP_HOLDS[:2]])
async def test_a_lookup_that_finds_nothing_leaves_it_held_unchanged(code, pending, answer):
    pid = await _held(code, pending=pending)
    rail = Rail().on("checkout.lookup#submit", answer)
    assert await _step(rail, pid) == "checkout_held"
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["checkout_task_id"]) == (
        "paying", code, pending, None)
    # Backed off: due again only after about as long again as it has been in `paying`.
    wait = (_aware(row["next_poll_at"]) - worker._now()).total_seconds()
    assert 100 <= wait <= 130


def _aware(value):
    from datetime import timezone

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def test_a_lookup_result_with_another_task_or_an_unverified_total_stays_held():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_other")
    assert await _step(Rail().on("checkout.lookup#submit", _checkout_result()), pid) == worker.CHECKOUT_TASK_MISMATCH
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    rail = Rail().on("checkout.lookup#submit", _checkout_result(total="€51.49"))
    assert await _step(rail, pid) == worker.CHECKOUT_TOTAL_UNVERIFIED  # a held code stays held
    assert (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("code", sorted(worker._CHECKOUT_PAYMENT_REACHED_CODES | {
    "checkout_confirm_conflict", "checkout_task_mismatch", "checkout_confirm_not_sent", "checkout_result_overdue"}))
async def test_a_hold_that_says_money_moved_or_is_not_an_unseen_answer_is_never_looked_up(code):
    pid = await _held(code)
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await _step(rail, pid) == "checkout_held"
    assert rail.sent == []
    assert (await _row(pid))["last_error_code"] == code


async def test_lookups_wait_for_the_first_delay_back_off_and_stop_after_the_window():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, age=10)
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []  # too soon
    await _ago("state_entered_at", worker.CHECKOUT_LOOKUP_WINDOW_SECONDS + 60, pid)
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []  # past the window: a human's
    # The schedule: each lookup when the time in `paying` has doubled, at most MAX_TRIES in the window.
    t, tries = float(worker.CHECKOUT_LOOKUP_FIRST_SECONDS), 0
    while t <= worker.CHECKOUT_LOOKUP_WINDOW_SECONDS:
        tries += 1
        t += worker._lookup_wait({"state": "paying", "last_error_code": "checkout_confirm_unresolved",
                                  "state_entered_at": worker._now() - __import__("datetime").timedelta(seconds=t),
                                  "checkout_task_id": None, "ceiling_source": "allowance_v1", "checkout_lookups": 0})
    assert 1 <= tries <= worker.CHECKOUT_LOOKUP_MAX_TRIES


async def test_at_most_max_tries_lookups_ever_go_out_and_a_crash_mid_lookup_still_counts(monkeypatch):
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    sent = 0
    for _ in range(worker.CHECKOUT_LOOKUP_MAX_TRIES + 3):
        rail = Rail().on("checkout.lookup#submit", httpx.Response(500))
        assert await _step(rail, pid) == "checkout_held"
        sent += len(rail.sent)
    assert sent == worker.CHECKOUT_LOOKUP_MAX_TRIES
    assert (await _row(pid))["checkout_lookups"] == worker.CHECKOUT_LOOKUP_MAX_TRIES
    # A crash in the middle of a lookup (the read raised): it was counted before it went out.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)

    async def _crash(self, *a, **k):
        raise RuntimeError("the step was cut")

    monkeypatch.setattr(worker._Step, "send", _crash)
    with pytest.raises(RuntimeError):
        await _step(Rail(), pid)
    assert (await _row(pid))["checkout_lookups"] == 1


async def test_a_row_read_without_the_lookup_counter_is_never_looked_up():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    [row] = [r for r in await ledger.claim_due_purchases(W, limit=5) if r["id"] == pid] or [None]
    if row is None:
        await _ago("next_poll_at", 1, pid)
        [row] = [r for r in await ledger.claim_due_purchases(W, limit=5) if r["id"] == pid]
    row = {k: v for k, v in row.items() if k != "checkout_lookups"}  # a half-healed schema (migration 290)
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await worker.step(_client(rail), row, W) == "checkout_held" and rail.sent == []


async def test_with_the_checkout_dial_off_a_held_row_is_never_looked_up(monkeypatch):
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "0")
    rail = Rail().on("checkout.lookup#submit", _checkout_result())
    assert await _step(rail, pid) == "checkout_held" and rail.sent == []


def test_the_lookup_is_a_read_the_transport_never_counts_as_a_write():
    assert ("checkout.lookup", "submit") == worker.CHECKOUT_LOOKUP
    assert worker._action(worker.CHECKOUT_LOOKUP) not in worker._TOKEN_WRITES
    assert ("checkout.lookup", "submit") not in a2a_rail.CHECKOUT_WRITE_ACTIONS


# ── a card security code echoed in prose or a card string (the DAVV is token data) ─────────────

CARD_CODE_PHRASES = ["cvv 715", "CVV: 715", "security code is 715", "715 (cvv)", "davv=715", "Dynamic CVV 715",
                     "card verification value 715", "verification code: 715", "CVV2 715", "ＣＶＶ 715", "cv​v 715",
                     "Card code - 715", "CSC 715", "cid: 715"]
NOT_CARD_CODES = ["order verification 123", "card ending 123", "Suite 715", "verification sent to 555", "card 1 of 3",
                  "$715.00", "qty 715", "decided 715", "ascvv 715", "cid 4821"]


@pytest.mark.parametrize("text", CARD_CODE_PHRASES)
def test_each_card_code_phrasing_is_found_case_folded_after_nfkc(text):
    assert a2a_rail.has_card_code(text)
    result = a2a_rail.parse_response(_task({"kind": "text", "text": f"Thanks. {text}."},
                                           _card("checkout.confirm", {"merchant": "merchant.example", "note": text})))
    assert result.card_codes_scrubbed == 2 and result.credential_fields_stripped >= 2
    assert result.card("checkout.confirm").data["note"] is None  # the string removed whole
    assert text not in repr(result)


@pytest.mark.parametrize("text", NOT_CARD_CODES)
def test_an_ordinary_number_away_from_a_card_code_word_is_neither_mangled_nor_counted(text):
    assert not a2a_rail.has_card_code(text)
    result = a2a_rail.parse_response(_task({"kind": "text", "text": text},
                                           _card("checkout.confirm", {"merchant": "merchant.example", "note": text})))
    assert (result.card_codes_scrubbed, result.card("checkout.confirm").data["note"]) == (0, text)


def test_the_window_is_three_words_and_either_order():
    assert a2a_rail.has_card_code("cvv for your order 715")           # three words between
    assert not a2a_rail.has_card_code("cvv for your new order 715")   # four
    assert a2a_rail.has_card_code("715 is the cvv")
    assert not a2a_rail.has_card_code("715 is not at all the cvv")


async def test_before_the_confirm_a_setup_answer_with_a_card_code_is_cancelled_not_confirmed():
    answer = _task({"kind": "text", "text": "Your CVV: 715 was used."},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=answer).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"  # a definite cancel: nothing was submitted
    assert "checkout.confirm#confirm" not in rail.actions() and "checkout.confirm#cancel" in rail.actions()
    # A cancel answer that itself echoes a code is not definite: held, never failed.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _approving()
    echo_cancel = _task({"kind": "text", "text": "cvv 715"}, _card("checkout.cancelled", {"state": "cancelled"}))
    rail = _approved(Rail(), setup_answer=answer).on("checkout.confirm#cancel", echo_cancel)
    assert await _step(rail, pid) == "checkout_cancel_unresolved"
    assert (await _row(pid))["state"] == "paying"


async def _assert_held_scrubbed(pid, pending):
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["terminal_at"]) == (
        "paying", worker.CHECKOUT_ANSWER_SCRUBBED, pending, None)
    assert worker.CHECKOUT_ANSWER_SCRUBBED in worker._CHECKOUT_HOLD_CODES  # held for the worker and the gateway
    assert worker.CHECKOUT_ANSWER_SCRUBBED in worker._CHECKOUT_PAYMENT_REACHED_CODES  # never "not charged"


async def test_after_the_confirm_a_confirm_answer_with_a_card_code_is_held_with_the_write_kept():
    pid = await _approving()
    confirm = _task({"kind": "text", "text": "security code is 715"}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(_approved(Rail(), confirm_answer=confirm), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
    await _assert_held_scrubbed(pid, worker.CHECKOUT_CONFIRM_ACTION)


async def test_after_the_confirm_a_poll_with_a_card_code_is_held():
    pid = await _placing()
    poll = _task({"kind": "text", "text": "715 (cvv)"}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(Rail().on("checkout.await#poll", poll), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
    await _assert_held_scrubbed(pid, None)


async def test_after_the_confirm_a_result_with_a_card_code_is_held_never_completed_or_failed():
    for status, success in (("payment-reached", True), ("form-error", False)):
        await database.execute("DELETE FROM a2a_rail_purchases")
        pid = await _placing()
        result = _task(_card("checkout.result", {
            "task_id": "ckout_abc123", "state": "completed", "success": success, "status": status,
            "total": "$51.49", "order_id": "1001", "note": "davv=715"}))
        assert await _step(Rail().on("checkout.await#poll", result), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
        await _assert_held_scrubbed(pid, None)


async def test_a_lookup_with_a_card_code_is_held_never_completed_or_failed():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    answer = _task({"kind": "text", "text": "CVV2 715"}, _card("checkout.result", {
        "task_id": "ckout_abc123", "state": "failed", "success": False, "status": "form-error"}))
    assert await _step(Rail().on("checkout.lookup#submit", answer), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
    await _assert_held_scrubbed(pid, worker.CHECKOUT_CONFIRM_ACTION)


async def test_an_echoed_card_code_never_reaches_a_log_a_row_an_exception_or_the_view(caplog, monkeypatch):
    phrase = "Your CVV: 715 is on file"
    with caplog.at_level(logging.DEBUG):
        pid = await _placing()
        poll = _task({"kind": "text", "text": phrase}, _card("checkout.await", {"task_id": "ckout_abc123", "m": phrase}))
        assert await _step(Rail().on("checkout.await#poll", poll), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
        # The prose check raising (with the text in its message) is swallowed: refused, nothing of it logged.
        def _boom(text, ceiling):
            raise ValueError(text)
        from services.payment_orchestration import checkout_amounts

        monkeypatch.setattr(checkout_amounts, "text_refused", _boom)
        answer = _task({"kind": "text", "text": phrase}, _card("checkout.confirm", {"merchant": "merchant.example"},
                                                                ("confirm",), kind="confirm"))
        relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
        assert relay.checkout.text_amounts_refused is True and phrase not in repr(relay)
    logged = "\n".join(r.getMessage() + " " + str(r.exc_info or "") for r in caplog.records
                       if not r.name.startswith(("databases", "aiosqlite", "asyncpg")))
    stored = await _stored_text()
    shown = json.dumps(view.build_view(await _row(pid)), default=str)
    for text in (logged, stored, shown):
        assert phrase not in text and "CVV: 715" not in text
    assert DAVV not in _string_values(await _row(pid)) and DAVV not in _string_values(view.build_view(await _row(pid)))


# ── controller review of the DAVV / lookup round (P2-2, P3-2..P3-7) ───────────────────────────

async def test_a_money_result_answer_to_a_lookup_parks_for_good_and_the_operator_refuses_it():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    money = _task(_card("payment.pay-result", {"status": "success"}))
    assert await _step(Rail().on("checkout.lookup#submit", money), pid) == "money_result_seen"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == (
        "paying", "money_result_seen", worker.CHECKOUT_CONFIRM_ACTION)
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    code_, [preview] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                       "checkout_confirm_unresolved")
    assert preview["eligible"] is False and code_ != 0
    code_, [result] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                      "checkout_confirm_unresolved", "--apply", "--operator", "ops-1",
                                      "--expect-env", "staging", "--provider-checked")
    assert result["outcome"] == "refused" and (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("code", ["checkout_setup_unresolved", "checkout_confirm_pending_check",
                                  "checkout_paying_without_task", "checkout_confirm_unresolved"])
async def test_an_echo_on_any_lookup_is_held_and_the_operator_still_refuses_it(code):
    pid = await _held(code)
    echo = _task({"kind": "text", "text": "security code is 123"}, _checkout_await())
    assert await _step(Rail().on("checkout.lookup#submit", echo), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    code_, [preview] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                       worker.CHECKOUT_ANSWER_SCRUBBED)
    assert (code_, preview["eligible"], preview["reason"]) == (3, False, "payment_reached_recorded")


async def test_the_scrubbed_confirm_answer_hold_notes_its_task_id_and_keeps_the_write():
    pid = await _approving()
    confirm = _task({"kind": "text", "text": "cvv 715"}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(_approved(Rail(), confirm_answer=confirm), pid) == worker.CHECKOUT_ANSWER_SCRUBBED
    row = await _row(pid)
    assert (row["checkout_task_id"], row["pending_write"], row["last_error_code"]) == (
        "ckout_abc123", worker.CHECKOUT_CONFIRM_ACTION, worker.CHECKOUT_ANSWER_SCRUBBED)


@pytest.mark.parametrize("token", [{"value": "４" * 16}, {"expiryDate": "11٢٩"}, {"value": "٤" * 16}],
                         ids=["fullwidth_dpan", "arabic_expiry", "arabic_dpan"])
def test_the_dpan_and_expiry_take_ascii_digits_only(token):
    assert a2a_rail.checkout_card_from_network_token(_network_token(**token), brand="visa", name="Pat Example") is None


@pytest.mark.parametrize("prose", [
    "Order 123 - CVV verified", "CVV check passed. Order #123 placed.", "cvv verified for order 123",
    "Suite 716 · security code verified", "Apt 204, CVV not needed", "We never store your CVV. Ref 445",
    "verification code sent to (512) 555-0100", "Item 716 (cid)",
])
async def test_before_the_confirm_prose_near_a_card_word_that_is_not_our_davv_does_not_cancel(prose):
    answer = _task({"kind": "text", "text": prose},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    pid = await _approving()
    assert await _step(_approved(Rail(), setup_answer=answer), pid) == "placing"


@pytest.mark.parametrize("prose", ["CVV: 7 1 5", "security code 7-1-5", "cvv ٧١٥", "dcvv 715", "cv2 715", "CVN 715",
                                   "715 (cvv)",
                                   # N4: whitespace runs, HTML entities, %-escapes and a dotted word are folded
                                   "cvv 7  1  5", "&#55;&#49;&#53; (cvv)", "cvv%20715", "c.v.v 715",
                                   # our digits beside a card word by coincidence still cancel: fail closed
                                   "Suite 715 · security code verified"])
async def test_before_the_confirm_our_davv_echoed_in_any_spelling_cancels(prose):
    answer = _task({"kind": "text", "text": prose},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=answer).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


async def test_after_the_confirm_any_three_digit_code_near_a_card_word_still_holds():
    pid = await _placing()
    poll = _task({"kind": "text", "text": "cvv 123"}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(Rail().on("checkout.await#poll", poll), pid) == worker.CHECKOUT_ANSWER_SCRUBBED


@pytest.mark.parametrize("text", ["Version 7.1.5 supports CVV", "CVV checks run on v.2.1.5 and 7.1.5"])
async def test_after_the_confirm_a_dotted_version_number_is_not_a_card_code_and_is_not_held(text):
    # N6: only dotted LETTERS are joined ("c.v.v" -> "cvv"); "7.1.5" is never read as the code 715.
    assert not a2a_rail.has_card_code(text)
    assert a2a_rail.has_card_code("c.v.v 715") and a2a_rail._fold("c.v.v.2 715") == "cvv2 715"
    pid = await _placing()
    poll = _task({"kind": "text", "text": text}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(Rail().on("checkout.await#poll", poll), pid) != worker.CHECKOUT_ANSWER_SCRUBBED
    assert (await _row(pid))["last_error_code"] != worker.CHECKOUT_ANSWER_SCRUBBED


async def test_after_the_confirm_a_code_in_other_digits_is_folded_and_held():
    # No amount scan runs after the confirm, so only the digit folding catches "٧١٥".
    assert a2a_rail.has_card_code("cvv ٧١٥") and a2a_rail.has_card_code("security code ٤٤٥")
    pid = await _placing()
    poll = _task({"kind": "text", "text": "cvv ٤٤٥"}, _card("checkout.await", {"task_id": "ckout_abc123"}))
    assert await _step(Rail().on("checkout.await#poll", poll), pid) == worker.CHECKOUT_ANSWER_SCRUBBED


async def test_a_bound_not_submitted_status_without_success_false_goes_through_the_result_rules():
    # A contradictory answer ("form-error", success true) on OUR task: held as unrecognised, never failed.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_B")
    answer = _checkout_result("form-error", task="ckout_B", success=True, order_id=None, total=None)
    assert await _step(Rail().on("checkout.lookup#submit", answer), pid) == worker.CHECKOUT_RESULT_UNRECOGNISED
    assert (await _row(pid))["state"] == "paying"


# ── N1 closed by "no unbound completion" (the provider's lookup answer, 11-19) ─────────────────

def _documented_result(*, task="ckout_abc123", state="completed", success=True, result_status="payment-reached",
                       total="$51.49", currency="USD", order_id="1001", **extra):
    """A lookup answer with the provider's documented field set (11-19), placeholder values: in any state
    task_id, state, card_last4; on completed / failed also success, result_status, total, currency, subtotal,
    shipping_fee, tax, errors, order_id, duration_ms. It echoes nothing we sent."""
    data = {"task_id": task, "state": state, "card_last4": "4399", "success": success, "result_status": result_status,
            "total": total, "currency": currency, "subtotal": "$45.00", "shipping_fee": "$4.49", "tax": "$2.00",
            "errors": [], "order_id": order_id, "duration_ms": 41000, **extra}
    return _task(_card("checkout.result", {k: v for k, v in data.items() if v is not None}))


#: Unbound answers that say the checkout completed or succeeded (condition 2): payment-reached hold.
UNBOUND_DONE = {
    "documented_completed_success": _documented_result(),
    "success_true_only": _task(_card("checkout.result", {"task_id": "ckout_B", "success": True})),
    "result_status_payment_reached": _task(_card("checkout.result", {"task_id": "ckout_B", "result_status": "payment-reached"})),
    "result_status_completed": _task(_card("checkout.result", {"task_id": "ckout_B", "result_status": "completed"})),
    "state_paid": _task(_card("checkout.result", {"task_id": "ckout_B", "state": "paid"})),
    "status_captured": _task(_card("checkout.result", {"task_id": "ckout_B", "status": "captured"})),
    "status_paid_punctuated": _task(_card("checkout.result", {"task_id": "ckout_B", "status": "Paid."})),
    "await_state_captured": _task(_card("checkout.await", {"task_id": "ckout_B", "state": "captured"})),
    "over_the_ceiling_success": _documented_result(task="ckout_B", total="$999.00"),
    "other_currency_success": _documented_result(task="ckout_B", currency="EUR"),
}
#: Unbound answers that do not (the hold stays exactly as it was).
UNBOUND_NOT_DONE = {
    "documented_failed_form_error": _documented_result(state="failed", success=False, result_status="form-error",
                                                       order_id=None, total=None),
    "result_status_timeout": _task(_card("checkout.result", {"task_id": "ckout_B", "result_status": "timeout"})),
    "result_status_unpaid": _task(_card("checkout.result", {"task_id": "ckout_B", "result_status": "unpaid",
                                                            "success": False})),
    "await_running": _task(_card("checkout.await", {"task_id": "ckout_B", "state": "running"})),
    "state_failed": _task(_card("checkout.result", {"task_id": "ckout_B", "state": "failed", "success": False})),
    # `state` is the task's lifecycle: "completed" alone is not done (a finished task that placed nothing).
    "state_completed_alone": _task(_card("checkout.result", {"task_id": "ckout_B", "state": "completed"})),
    "await_state_completed_alone": _task(_card("checkout.await", {"task_id": "ckout_B", "state": "completed"})),
    "documented_completed_but_not_submitted": _documented_result(task="ckout_B", state="completed", success=False,
                                                                  result_status="form-error", order_id=None,
                                                                  total=None),
}


@pytest.mark.parametrize("name", sorted(UNBOUND_DONE))
async def test_an_unbound_answer_that_says_completed_or_success_is_held_as_payment_reached_and_never_resolvable(name):
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    assert await _step(Rail().on("checkout.lookup#submit", UNBOUND_DONE[name]), pid) == (
        worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["merchant_order_id"], row["final_total_minor"],
            row["checkout_task_id"], row["pending_write"]) == (
        "paying", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED, None, None, None, worker.CHECKOUT_CONFIRM_ACTION)
    assert worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED in worker._CHECKOUT_PAYMENT_REACHED_CODES
    assert worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED in worker._CHECKOUT_HOLD_CODES
    assert worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED not in worker._OPERATOR_RESOLVABLE_HOLD_CODES
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    expected = ("resolve-held", "--purchase-id", pid, "--expected-code", worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED)
    code_, [result] = await _operator(*expected, "--apply", "--operator", "ops-1", "--expect-env", "staging",
                                      "--provider-checked")
    assert (code_, result["outcome"], result["reason"]) == (3, "refused", "payment_reached_recorded")
    assert (await _row(pid))["state"] == "paying"


@pytest.mark.parametrize("name", sorted(UNBOUND_NOT_DONE))
@pytest.mark.parametrize("code,pending", LOOKUP_HOLDS[:2], ids=[c for c, _ in LOOKUP_HOLDS[:2]])
async def test_any_other_unbound_answer_keeps_the_hold_exactly_as_it_was(name, code, pending):
    pid = await _held(code, pending=pending)
    assert await _step(Rail().on("checkout.lookup#submit", UNBOUND_NOT_DONE[name]), pid) == "checkout_held"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["merchant_order_id"], row["final_total_minor"],
            row["checkout_task_id"], row["pending_write"], row["terminal_at"]) == (
        "paying", code, None, None, None, pending, None)


def test_every_payment_reached_code_is_refused_by_the_operator_test():
    # Condition 3: the operator refusal test (preview and --apply --provider-checked) covers EVERY payment-reached
    # code, so a new one cannot be added without it.
    cases = next(m for m in test_the_operator_never_marks_a_recorded_payment_not_charged.pytestmark
                 if m.name == "parametrize").args[1]
    assert set(cases) == worker._CHECKOUT_PAYMENT_REACHED_CODES


#: Condition 5: the documented full field set on OUR task (unknown fields ignored, never stored).
BOUND_DOCUMENTED = {
    "completes": (_documented_result(), "completed"),
    "status_key_alone": (_task(_card("checkout.result", {"task_id": "ckout_abc123", "status": "payment-reached",
                                                         "success": True, "total": "$51.49", "order_id": "1001"})),
                         "completed"),
    "over_the_ceiling": (_documented_result(total="$51.50"), worker.CHECKOUT_TOTAL_OVER_APPROVED),
    "other_currency": (_documented_result(currency="EUR"), worker.CHECKOUT_TOTAL_UNVERIFIED),
    "other_currency_lower_case": (_documented_result(currency="cad"), worker.CHECKOUT_TOTAL_UNVERIFIED),
    "usd_lower_case": (_documented_result(currency="usd"), "completed"),
    # (either one alone would decide differently: payment-reached completes, the other is unrecognised)
    "status_and_result_status_disagree": (_documented_result(status="payment-reached", result_status="queued-elsewhere"),
                                          worker.CHECKOUT_RESULT_UNRECOGNISED),
    "failed_form_error": (_documented_result(state="failed", success=False, result_status="form-error",
                                             order_id=None, total=None), "failed"),
}


@pytest.mark.parametrize("name", sorted(BOUND_DOCUMENTED))
async def test_a_bound_answer_with_the_documented_field_set_follows_the_result_rules(name):
    answer, outcome = BOUND_DOCUMENTED[name]
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    assert await _step(Rail().on("checkout.lookup#submit", answer), pid) == outcome
    row = await _row(pid)
    if outcome == "completed":
        assert (row["merchant_order_id"], row["final_total_minor"]) == ("1001", TOTAL)
    else:
        assert row["merchant_order_id"] is None and row["final_total_minor"] is None
    # card_last4, subtotal, shipping_fee, duration_ms: never a stored value (compared whole, or JSON-quoted
    # inside a JSON column: a random id may contain "4399" by chance).
    stored = await _stored_text()
    values = [str(v) for r in await database.fetch_all("SELECT * FROM a2a_rail_purchases")
              for v in dict(r).values() if v is not None]
    for needle in ("4399", "41000"):
        assert needle not in values and f'"{needle}"' not in stored.replace('\\"', '"'), needle
    for needle in ("$45.00", "$4.49"):
        assert needle not in stored, needle


def _held_row_for_preview(pid):
    return database.fetch_one("SELECT * FROM a2a_rail_purchases WHERE id = :i", {"i": pid})


async def _preview_lookup(pid, answer, code="checkout_confirm_unresolved"):
    import io
    import jobs.a2a_rail_operator as operator

    rail = Rail().on("checkout.lookup#submit", answer)
    out = io.StringIO()
    exit_code = await operator.run(["resolve-held", "--purchase-id", pid, "--expected-code", code, "--lookup"],
                                   out=out, client=_client(rail))
    return exit_code, json.loads(out.getvalue()), rail


async def test_resolve_held_shows_an_unbound_answers_safe_fields_labelled_and_stores_nothing():
    # Condition 4: read at preview time, never persisted; never card_last4 or the order id.
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    before = dict(await _held_row_for_preview(pid))
    code_, preview, rail = await _preview_lookup(pid, _documented_result(task="ckout_B"))
    assert code_ == 3 and preview["outcome"] == "dry_run"
    assert preview["lookup"] == {
        "ran": True, "found": True, "bound": False, "says_done": True, "confirmed_decline": False,
        "label": "UNBOUND: this answer may belong to another purchase; not verified as ours",
        "fields": {"state": "completed", "result_status": "payment-reached", "total": "$51.49", "currency": "USD",
                   "task_id": "ckout_B"}}
    assert worker.CHECKOUT_LOOKUP_UNBOUND_LABEL == preview["lookup"]["label"]
    _lookup_only(rail, pid)
    text = json.dumps(preview, default=str)
    assert "4399" not in text and '"1001"' not in text  # never card_last4, never the order id
    assert dict(await _held_row_for_preview(pid)) == before  # nothing written, not even the lookup count


async def test_resolve_held_labels_an_answer_for_the_recorded_task_bound():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task="ckout_abc123")
    _, preview, _ = await _preview_lookup(pid, _documented_result())
    assert (preview["lookup"]["bound"], preview["lookup"]["label"]) == (True, worker.CHECKOUT_LOOKUP_BOUND_LABEL)
    _, preview, _ = await _preview_lookup(pid, _documented_result(task="ckout_B"))
    assert (preview["lookup"]["bound"], preview["lookup"]["label"]) == (False, worker.CHECKOUT_LOOKUP_UNBOUND_LABEL)


async def test_resolve_held_lookup_runs_only_when_asked_and_never_with_the_dial_off(monkeypatch):
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    code_, [preview] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code",
                                       "checkout_confirm_unresolved")
    assert "lookup" not in preview  # no --lookup: nothing sent
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "0")
    _, preview, rail = await _preview_lookup(pid, _documented_result())
    assert preview["lookup"] == {"ran": False, "reason": "the checkout dial is off"} and rail.sent == []


async def test_resolve_held_lookup_withholds_a_card_shaped_task_id_and_shows_nothing_of_an_echo():
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION)
    _, preview, _ = await _preview_lookup(pid, _documented_result(task="4222222222222"))
    assert "task_id" not in preview["lookup"]["fields"]
    _, preview, _ = await _preview_lookup(pid, _task({"kind": "text", "text": "cvv 715"},
                                                      _card("checkout.result", {"task_id": "ckout_B"})))
    assert preview["lookup"] == {"ran": True, "found": False, "answer_scrubbed": True}


# ── the buyer's shipping address, asked for when the agent did not send it (AgenPas) ───────────

from services.payment_orchestration import a2a_shipping_address as shipping_capture  # noqa: E402
from services.payment_orchestration import purchase_view as unified_view  # noqa: E402

OWNER = "h" * 64


@pytest.fixture
def consent_gate_on(monkeypatch):
    """Shipping capture needs its own dial, the AgenPas consent gate on and a consent that can be given
    (a2a_shipping_address.capture_enabled). Test values."""
    for name, value in {
        "A2A_SHIPPING_CAPTURE_ENABLED": "1", "AGENPAS_CONSENT_GATE": "1", "AGENPAS_CONSENT_WRITE_ENABLED": "1",
        "AGENPAS_TERMS_URL": "https://legal.example/terms", "AGENPAS_TERMS_VERSION": "terms-test",
        "AGENPAS_PRIVACY_URL": "https://legal.example/privacy", "AGENPAS_PRIVACY_VERSION": "privacy-test",
        "AGENPAS_CONSENT_OFFER_SECRET": "offer_secret_for_capture_tests_0123456789",
        "AGENPAS_INTERNAL_CONSENT_KEY": "agenpas_consent_key_for_capture_tests_0123",
    }.items():
        monkeypatch.setenv(name, value)


async def _open_without(*, email=None, address=None):
    row = await ledger.create_purchase(
        agent_id="agent_w", agent_user_ref_hash=OWNER, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email=email, shipping_address=address)
    return row["id"]


@pytest.mark.usefixtures("consent_gate_on")
async def test_a_checkout_purchase_without_its_contact_parks_sending_nothing():
    pid = await _open_without()
    rail = Rail()
    assert await _step(rail, pid) == worker.SHIPPING_ADDRESS_REQUIRED
    row = await _row(pid)
    assert rail.sent == []  # not even the card read
    assert (row["state"], row["last_error_code"], row["claimed_by"]) == ("opened", worker.SHIPPING_ADDRESS_REQUIRED, None)
    assert (row["buyer_action_kind"], row["buyer_action_url"]) == (None, None)
    created, deadline = row["created_at"], row["buyer_action_expires_at"]
    assert abs((deadline - created).total_seconds() - worker.SHIPPING_ADDRESS_SECONDS) < 5
    # A second park never moves the deadline.
    assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    assert (await _row(pid))["buyer_action_expires_at"] == deadline


#: Every dial capture needs (a2a_shipping_address.capture_enabled), as (env, how it is turned off).
CAPTURE_DIALS_OFF = {
    "gate": ("AGENPAS_CONSENT_GATE", None),
    "capture_dial": ("A2A_SHIPPING_CAPTURE_ENABLED", "0"),
    "checkout_step": (a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "0"),
    "token_step": (worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, "0"),
    "consent_write": ("AGENPAS_CONSENT_WRITE_ENABLED", "0"),
    "consent_key": ("AGENPAS_INTERNAL_CONSENT_KEY", None),
    "terms_config": ("AGENPAS_TERMS_URL", None),
    "read_dial": ("AGENPAS_INTERNAL_READ_ENABLED", "0"),
}


@pytest.mark.parametrize("parked", [True, False], ids=["parked", "not_yet_parked"])
@pytest.mark.parametrize("dial", sorted(CAPTURE_DIALS_OFF))
@pytest.mark.usefixtures("consent_gate_on")
async def test_a_dial_turned_off_while_a_purchase_waits_ends_it_before_any_rail_call(monkeypatch, dial, parked):
    pid = await _open_without()
    if parked:
        assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    name, value = CAPTURE_DIALS_OFF[dial]
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    assert shipping_capture.capture_enabled() is False
    rail = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail, pid) == "expired"
    assert rail.sent == []  # no card read, no binding: the contact can never arrive now
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["payment_method_id"], row["buyer_action_kind"]) == (
        "expired", worker.SHIPPING_ADDRESS_NOT_COLLECTED, None, None)
    assert (row["buyer_email"], row["shipping_address"]) == (None, None)  # a terminal write drops the contact
    assert worker.SHIPPING_ADDRESS_NOT_COLLECTED == shipping_capture.SHIPPING_ADDRESS_NOT_COLLECTED == (
        "shipping_address_not_collected")  # the shipping write's 409 code


@pytest.mark.usefixtures("consent_gate_on")
async def test_with_capture_off_only_an_absent_part_ends_the_purchase(monkeypatch):
    monkeypatch.setenv("A2A_SHIPPING_CAPTURE_ENABLED", "0")
    # The door with capture off takes a purchase whole (both parts present): an address without a region,
    # say, goes on exactly as before, to the card read.
    whole = await _open_without(email=EMAIL, address={k: v for k, v in ADDRESS.items() if k != "region"})
    rail = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail, whole) != "expired"
    assert rail.actions()[0] == "payment.method-view#submit"
    assert (await _row(whole))["state"] != "expired"
    # An absent email (only the capture door opens one) ends it.
    emailless = await _open_without(address=ADDRESS)
    rail = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail, emailless) == "expired" and rail.sent == []


@pytest.mark.usefixtures("consent_gate_on")
async def test_an_incomplete_address_parks_like_a_missing_one_and_never_loops():
    # The park's "the contact landed meanwhile" check counts an address that is still exactly the incomplete
    # one the worker read as absent (THE waiting rule): the row parks with its deadline, it is not made due at
    # once, and a second park keeps the same deadline.
    partial = {k: v for k, v in ADDRESS.items() if k != "city"}
    pid = await _open_without(email=EMAIL, address=partial)
    assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("opened", worker.SHIPPING_ADDRESS_REQUIRED)
    deadline = row["buyer_action_expires_at"]
    assert deadline is not None
    assert pid not in [r["id"] for r in await ledger.claim_due_purchases(W, limit=5)]  # not due at once
    assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    assert (await _row(pid))["buyer_action_expires_at"] == deadline


@pytest.mark.usefixtures("consent_gate_on")
async def test_the_email_is_asked_for_first_and_each_missing_part_is_named():
    assert shipping_capture.fields_missing(None, None) == [
        "email", "firstName", "lastName", "phone", "addressLine1", "city", "country"]  # never a region or postcode
    assert shipping_capture.fields_missing(EMAIL, {k: v for k, v in ADDRESS.items() if k != "postalCode"}) == []
    assert shipping_capture.fields_missing(None, ADDRESS) == ["email"]
    assert shipping_capture.fields_missing(EMAIL, {k: v for k, v in ADDRESS.items() if k != "city"}) == ["city"]
    assert shipping_capture.fields_missing(EMAIL, {k: v for k, v in ADDRESS.items() if k != "region"}) == []
    assert shipping_capture.fields_missing(EMAIL, ADDRESS) == []
    # An address alone still waits for the email: the card binding needs it before anything is sent.
    pid = await _open_without(address=ADDRESS)
    rail = Rail()
    assert await _step(rail, pid) == worker.SHIPPING_ADDRESS_REQUIRED and rail.sent == []


@pytest.mark.usefixtures("consent_gate_on")
async def test_past_its_deadline_a_waiting_purchase_expires_and_its_contact_is_gone():
    pid = await _open_without(email=EMAIL)
    await _ago("created_at", worker.SHIPPING_ADDRESS_SECONDS + 5, pid)
    assert await _step(Rail(), pid) == "expired"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["buyer_email"]) == ("expired", worker.SHIPPING_ADDRESS_EXPIRED, None)


@pytest.mark.usefixtures("consent_gate_on")
async def test_the_expiry_sweep_names_a_passed_shipping_address_deadline():
    pid = await _open_without()
    assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    await _ago("buyer_action_expires_at", 5, pid)
    assert await ledger.expire_overdue_purchases() >= 1
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("expired", ledger.SHIPPING_ADDRESS_EXPIRED_CODE)
    # Any other passed deadline keeps its own code.
    other = await _open()
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = 'bound_sent:1' WHERE id = :i", {"i": other})
    await _ago("buyer_action_expires_at", 5, other)
    await ledger.expire_overdue_purchases()
    assert (await _row(other))["last_error_code"] == "buyer_action_expired"


@pytest.mark.usefixtures("consent_gate_on")
async def test_the_submitted_contact_fills_only_an_empty_part_and_makes_the_row_due():
    pid = await _open_without()
    assert await _step(Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED
    moved = await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS)
    assert moved is not None
    assert (moved["buyer_email"], moved["shipping_address"]) == (EMAIL, ADDRESS)
    assert (moved["last_error_code"], moved["buyer_action_expires_at"]) == (None, None)
    # Due at once: the next claim takes it, and the purchase goes on to the card step.
    assert [r["id"] for r in await ledger.claim_due_purchases(W, limit=5)] == [pid]
    await ledger.release_claim(pid, W)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA)), pid) == "method_ready"
    # Nothing waits any more: the same contact again changes nothing (the route answers it 200), another one
    # is never written over it.
    assert await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS) is None
    other = {**ADDRESS, "addressLine1": "2 Other St"}
    assert await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=other) is None
    assert (await _row(pid))["shipping_address"] == ADDRESS


@pytest.mark.usefixtures("consent_gate_on")
async def test_a_stored_part_is_never_overwritten_and_another_owner_never_writes():
    pid = await _open_without(email=EMAIL)
    assert await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email="other@example.test",
                                                shipping_address=ADDRESS) is None  # a different email
    assert await ledger.submit_shipping_address(pid, "agent_other", OWNER, buyer_email=None,
                                                shipping_address=ADDRESS) is None  # another agent
    assert await ledger.submit_shipping_address(pid, "agent_w", "g" * 64, buyer_email=None,
                                                shipping_address=ADDRESS) is None  # another buyer
    # The stored email may be left out, or sent again in another case.
    moved = await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL.upper(), shipping_address=ADDRESS)
    assert moved is not None and moved["buyer_email"] == EMAIL


@pytest.mark.usefixtures("consent_gate_on")
async def test_two_submits_at_once_write_exactly_one_contact():
    pid = await _open_without()
    first, second = {**ADDRESS, "addressLine1": "1 First St"}, {**ADDRESS, "addressLine1": "2 Second St"}
    results = await asyncio.gather(
        ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=first),
        ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=second))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r is not None)
    assert (await _row(pid))["shipping_address"] == winner["shipping_address"]


@pytest.mark.usefixtures("consent_gate_on")
async def test_a_row_that_does_not_wait_takes_no_contact():
    pid = await _open_without()
    await _ago("buyer_action_expires_at", 5, pid)  # the deadline passed (the sweep not yet run)
    assert await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS) is None
    purged = await _open_without(email=EMAIL)
    await database.execute("UPDATE a2a_rail_purchases SET contact_purged_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": purged})
    assert await ledger.submit_shipping_address(purged, "agent_w", OWNER, buyer_email=None, shipping_address=ADDRESS) is None
    # Complete already (still `opened`, the worker about to read the cards): nothing waits, nothing is touched.
    complete = await _open_without(email=EMAIL, address=ADDRESS)
    before = await _row(complete)
    assert await ledger.submit_shipping_address(complete, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS) is None
    assert (await _row(complete))["next_poll_at"] == before["next_poll_at"]
    moved_on = await _open_without()
    await database.execute("UPDATE a2a_rail_purchases SET state = 'quoting' WHERE id = :i", {"i": moved_on})
    assert await ledger.submit_shipping_address(moved_on, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS) is None


@pytest.mark.usefixtures("consent_gate_on")
async def test_the_park_written_after_the_contact_landed_makes_the_row_due_at_once():
    pid = await _open_without()
    [row] = [r for r in await ledger.claim_due_purchases(W, limit=5) if r["id"] == pid]
    # The gateway's write lands while the worker holds the row (between its read and its park).
    assert await ledger.submit_shipping_address(pid, "agent_w", OWNER, buyer_email=EMAIL, shipping_address=ADDRESS)
    parked = await ledger.park_for_shipping_address_as_holder(
        pid, W, expires_at=worker._later(1800), next_poll_at=worker._later(600))
    assert parked is not None and parked["claimed_by"] is None
    assert [r["id"] for r in await ledger.claim_due_purchases(W, limit=5)] == [pid]


@pytest.mark.usefixtures("consent_gate_on")
async def test_with_the_checkout_path_off_an_emailless_purchase_ends_before_any_rail_call(monkeypatch):
    # Only the door with capture on opens an email-less row; with the checkout path off capture is off, the
    # gateway's write refuses, and the email can never arrive: ended, nothing sent (not even the card read).
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "0")
    pid = await _open_without(address=ADDRESS)
    rail = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail, pid) == "expired"
    assert rail.sent == []
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["refusal_reason"]) == (
        "expired", worker.SHIPPING_ADDRESS_NOT_COLLECTED, None)


@pytest.mark.usefixtures("consent_gate_on")
def test_the_view_names_the_missing_fields_and_never_a_stored_value():
    from datetime import datetime, timedelta, timezone

    deadline = datetime.now(timezone.utc) + timedelta(minutes=20)
    partial = {**ADDRESS}
    partial.pop("city")
    row = {"id": "ap_" + "0" * 24, "state": "opened", "buyer_email": EMAIL, "shipping_address": partial,
           "buyer_action_expires_at": deadline, "last_error_code": worker.SHIPPING_ADDRESS_REQUIRED,
           "currency": "USD", "our_price_minor": 4650, "merchant_domain": "merchant.example"}
    detail = view.build_view(row)
    assert detail["shipping_address_required"] == {"fields_missing": ["city"], "country": "US", "expires_at": deadline}
    parent = {"id": "pp_" + "0" * 24, "rail": "a2a", "executor": "rail_managed", "rail_purchase_id": row["id"]}
    body = unified_view.unified_body(parent, detail)
    assert body["state"] == "routing"
    assert body["next_action"] == {"kind": "shipping_address", "fields_missing": ["city"], "country": "US",
                                   "expires_at": deadline}
    # The country is the purchase's own market (G2: the form offers exactly it); none when no one market has the currency.
    for currency, country in (("HKD", "HK"), ("KRW", "KR"), ("TWD", "TW"), ("JPY", "JP")):
        assert view.build_view({**row, "currency": currency})["shipping_address_required"]["country"] == country
    assert "country" not in view.build_view({**row, "currency": "EUR"})["shipping_address_required"]
    assert "url" not in body["next_action"] and "prefill" not in body["next_action"]
    text = json.dumps(body, default=str)
    for value in (EMAIL, *partial.values()):
        if len(value) > 3:  # not "US" / "TX", which "USD" and other words contain
            assert value not in text, value
    # No deadline yet (the worker has not looked): no `expires_at`. Complete, or past `opened`: no action.
    assert "expires_at" not in view.build_view({**row, "buyer_action_expires_at": None})["shipping_address_required"]
    assert "shipping_address_required" not in view.build_view({**row, "shipping_address": ADDRESS})
    assert "shipping_address_required" not in view.build_view({**row, "state": "quoting"})


# ── review of dc95b4357: --apply runs its own lookup; one "done"; --lookup with --apply; held rows only ──

async def _aged_hold(task=None):
    pid = await _held("checkout_confirm_unresolved", pending=worker.CHECKOUT_CONFIRM_ACTION, task=task)
    await _ago("state_entered_at", worker.OPERATOR_MIN_HOLD_AGE_SECONDS + 600, pid)
    return pid


_APPLY = ("--apply", "--operator", "ops-1", "--expect-env", "staging", "--provider-checked")


def _resolve(pid, *extra):
    return ("resolve-held", "--purchase-id", pid, "--expected-code", "checkout_confirm_unresolved", *extra)


@pytest.mark.parametrize("answer,task", [
    (_documented_result(task="ckout_B"), None),                                        # unbound payment-reached
    (_task(_card("checkout.result", {"task_id": "ckout_B", "success": True})), None),  # unbound success
    (_documented_result(), "ckout_abc123"),                                            # bound payment-reached
    (_task(_card("checkout.await", {"task_id": "ckout_B", "state": "captured"})), None),
], ids=["unbound_payment_reached", "unbound_success", "bound_payment_reached", "unbound_await_captured"])
async def test_apply_runs_its_own_lookup_and_never_resolves_a_reported_payment(answer, task):
    # P2-1: the preview's answer is not stored, so --apply reads again and refuses on its own.
    pid = await _aged_hold(task=task)
    rail = Rail().on("checkout.lookup#submit", answer)
    code_, [result] = await _operator(*_resolve(pid, *_APPLY), client=_client(rail))
    assert (code_, result["outcome"], result["reason"]) == (3, "refused", worker.OPERATOR_PAYMENT_REPORTED_BY_LOOKUP)
    assert result["lookup"]["says_done"] is True
    _lookup_only(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", "checkout_confirm_unresolved")


async def test_the_preview_says_not_eligible_and_escalates_when_its_lookup_reports_payment():
    pid = await _aged_hold()
    code_, [plain] = await _operator(*_resolve(pid))
    assert (code_, plain["eligible"]) == (0, True)
    rail = Rail().on("checkout.lookup#submit", _documented_result(task="ckout_B"))
    code_, [preview] = await _operator(*_resolve(pid, "--lookup"), client=_client(rail))
    assert (code_, preview["eligible"], preview["reason"]) == (3, False, worker.OPERATOR_PAYMENT_REPORTED_BY_LOOKUP)
    assert "escalate" in preview["message"]


@pytest.mark.parametrize("answer", [
    _documented_result(task="ckout_B", state="failed", success=False, result_status="form-error", order_id=None,
                       total=None),
    _task(_card("checkout.result", {"task_id": "ckout_B", "state": "completed"})),
    httpx.Response(404),
], ids=["not_submitted", "state_completed_alone", "not_found"])
async def test_apply_resolves_when_its_lookup_reports_no_payment(answer):
    pid = await _aged_hold()
    rail = Rail().on("checkout.lookup#submit", answer)
    code_, [result] = await _operator(*_resolve(pid, *_APPLY), client=_client(rail))
    assert (code_, result["outcome"]) == (0, "resolved")
    _lookup_only(rail, pid)


@pytest.mark.parametrize("client", ["none", "raising"])
async def test_without_a_client_or_with_a_failing_lookup_apply_rests_on_provider_checked(client):
    # The documented semantics: no lookup to read, the apply stands on the operator's --provider-checked statement.
    pid = await _aged_hold()
    fake = None if client == "none" else _client(Rail().on("checkout.lookup#submit", httpx.ConnectError("down")))
    code_, [result] = await _operator(*_resolve(pid, *_APPLY), client=fake)
    assert (code_, result["outcome"]) == (0, "resolved")


async def test_lookup_together_with_apply_is_a_usage_error_and_sends_nothing():
    pid = await _aged_hold()
    rail = Rail().on("checkout.lookup#submit", _documented_result(task="ckout_B"))
    code_, [result] = await _operator(*_resolve(pid, "--lookup", *_APPLY), client=_client(rail))
    assert (code_, result["outcome"]) == (2, "refused") and "--lookup" in result["reason"]
    assert rail.sent == [] and (await _row(pid))["state"] == "paying"


async def test_the_preview_looks_up_only_a_held_row():
    pid = await _placing()  # paying, placing: not held
    rail = Rail().on("checkout.lookup#submit", _documented_result())
    _, [preview] = await _operator("resolve-held", "--purchase-id", pid, "--expected-code", "checkout_placing",
                                   "--lookup", client=_client(rail))
    assert preview["lookup"] == {"ran": False, "reason": "no such held checkout"} and rail.sent == []


# ── the provider's documented token-result (11-19): the DAVV is networkToken.cvv, marked dynamicDataType "DAVV" ──

PROVIDER_DAVV = "077"  # a leading-zero placeholder DAVV


def _provider_token_result(**over):
    """The documented token-result, placeholder values: {component: payment.token-result, paymentTokenId,
    status, paymentBrand, providerId, networkToken: {brand, value, expiryDate, cvv, cryptogram "", eci null,
    dynamicDataType "DAVV"}}."""
    network_token = _network_token(cvv=PROVIDER_DAVV, **over)
    data = {"paymentTokenId": "ptk_1", "status": "ACTIVE", "paymentBrand": "visa", "providerId": "prov_test_1",
            "networkToken": network_token}
    return _task(_card("payment.token-result", data))


async def test_the_documented_token_result_relays_its_leading_zero_davv_as_the_three_char_cvv():
    assert a2a_rail.DAVV_FIELD == "cvv"
    provider = _Provider(_provider_token_result(), CONFIRM_CARD)
    relay = await _relay(provider)
    assert relay.refused is None
    setup = provider.sent()[1]["payload"]
    assert setup["card"]["cvv"] == "077" and isinstance(setup["card"]["cvv"], str)
    assert set(setup["card"]) == {"number", "expiry", "cvv", "name", "brand"}


@pytest.mark.parametrize("marker", [None, "absent", "CVV", "davv", "Davv", " DAVV", "DAVV ", "TAVV", 1],
                         ids=["null", "absent", "CVV", "lower_davv", "title_davv", "padded_left", "padded_right",
                              "TAVV", "int"])
async def test_a_cvv_on_a_token_not_marked_davv_is_never_read_or_relayed(marker):
    # Condition 1: a cvv leaf without the exact DAVV marker could be a real CVV: davv_missing, nothing sent.
    token = _network_token(cvv=PROVIDER_DAVV)
    if marker == "absent":
        token.pop("dynamicDataType")
    else:
        token["dynamicDataType"] = marker
    assert a2a_rail.davv_from_network_token(token) is None
    assert a2a_rail.checkout_card_from_network_token(token, brand="visa", name="Pat Example") is None
    provider = _Provider(_token_result(network_token=token), CONFIRM_CARD)
    relay = await _relay(provider)
    assert relay.refused == "davv_missing"
    assert [x for x in provider.sent() if x["payload"].get("card")] == []  # nothing went to the checkout


@pytest.mark.parametrize("cvv", [77, 715, 77.0, "77", "0770", None], ids=["int_77", "int_715", "float", "two_chars",
                                                                         "four_chars", "null"])
async def test_a_davv_that_is_not_a_three_char_string_is_missing(cvv):
    # Condition 2: an int (its leading zero lost) or any other form is davv_missing.
    token = _network_token(cvv=cvv)
    assert a2a_rail.davv_from_network_token(token) is None
    relay = await _relay(_Provider(_token_result(network_token=token), CONFIRM_CARD))
    assert relay.refused == "davv_missing"


@pytest.mark.parametrize("echo", [{"code": 77}, {"code": "77"}, {"code": "077"}, {"cvv": 77}],
                         ids=["int_77", "string_77", "string_077", "cvv_int_77"])
async def test_a_leading_zero_davv_echoed_as_77_or_077_is_scrubbed_and_the_answer_refused(echo):
    answer = _task(_card("checkout.confirm", {"merchant": "merchant.example", **echo}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_provider_token_result(), answer), setup=_setup(ceiling_minor=100000))
    assert relay.checkout.credential_fields_stripped >= 1


async def test_a_leading_zero_davv_inside_other_values_is_kept():
    # The leading-zero equivalents of the "$715.00" / "Suite 715" guards: never a whole value, never scrubbed.
    echo = {"ref": "$77.00", "suite": "Apt 77", "n": "77 units", "total": "$0.77"}
    answer = _task(_card("checkout.confirm", {"merchant": "merchant.example", **echo}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_provider_token_result(), answer), setup=_setup(ceiling_minor=100000))
    assert relay.checkout.credential_fields_stripped == 0
    assert {k: relay.checkout.card("checkout.confirm").data[k] for k in echo} == echo


def test_the_echo_matcher_on_a_leading_zero_davv_catches_it_without_its_zero():
    matches = a2a_rail.davv_echo_matcher("077")
    for text in ("cvv 077", "cvv 77", "CVV: 0 7 7", "security code 7-7", "77 (cvv)", "dcvv 77"):
        assert matches(text), text
    for text in ("Apt 77", "Total $77.00", "77 items in stock", "cvv 177", "cvv 770", "cvv verified, order 7"):
        assert not matches(text), text
    plain = a2a_rail.davv_echo_matcher("715")  # a DAVV with no leading zero is unchanged
    assert plain("cvv 715") and not plain("cvv 15")


async def test_before_the_confirm_a_leading_zero_davv_echoed_in_prose_without_its_zero_cancels():
    answer = _task({"kind": "text", "text": "Card verified, cvv 77"},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    relay = await _relay(_Provider(_provider_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.card_codes_scrubbed >= 1


# ── review of 995c64e8c (P3-1): no one-digit short form for a "00x" DAVV ──────────────────────────

@pytest.mark.parametrize("davv,echo", [
    ("007", {"quantity": 7}), ("007", {"errorCount": "7"}), ("000", {"errorCount": "0"}), ("000", {"quantity": 0}),
    ("001", {"quantity": 1}),
], ids=["007_quantity_7", "007_count_7", "000_count_0", "000_quantity_0", "001_quantity_1"])
async def test_a_00x_davv_never_becomes_a_one_digit_needle(davv, echo):
    answer = _task(_card("checkout.confirm", {"merchant": "merchant.example", **echo}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(network_token=_network_token(cvv=davv)), answer),
                         setup=_setup(ceiling_minor=100000))
    assert relay.refused is None and relay.checkout.credential_fields_stripped == 0
    assert relay.checkout.card("checkout.confirm").data == {"merchant": "merchant.example", **echo}


async def test_a_00x_davv_in_prose_ships_in_7_days_does_not_cancel_and_is_caught_verbatim():
    matches = a2a_rail.davv_echo_matcher("007")
    assert not matches("Ships in 7 days · CVV verified") and not matches("cvv 7")
    assert matches("cvv 007") and matches("CVV: 0 0 7")
    answer = _task({"kind": "text", "text": "Ships in 7 days · CVV verified"},
                   _card("checkout.confirm", {"merchant": "merchant.example", "cardLast4": "3478"},
                         ("confirm", "edit", "cancel"), kind="confirm"))
    relay = await _relay(_Provider(_token_result(network_token=_network_token(cvv="007")), answer),
                         setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.card_codes_scrubbed == 0
    exact = a2a_rail._scrub_needles(_network_token(cvv="007"))[1]
    assert "007" in exact and "7" not in exact


def test_the_short_form_needs_two_digits_left_and_077_still_catches_77():
    assert a2a_rail._davv_short_form("077") == "77" and a2a_rail._davv_short_form("715") is None
    assert a2a_rail._davv_short_form("007") is None and a2a_rail._davv_short_form("000") is None
    exact = a2a_rail._scrub_needles(_network_token(cvv="077"))[1]
    assert {"077", "77"} <= exact
    assert a2a_rail.davv_echo_matcher("077")("cvv 77")


# ── ADR-030 D11's run condition in the worker (2b-D; dark while the setting does not name `a2a`) ──────────────

from db import merchant_agent_checkout_facts as d11_facts  # noqa: E402
from db import partner_agent_merchant_blocks as d11_blocks  # noqa: E402
from tests.test_merchant_agent_checkout_facts import POSITIVE_UCP as D11_POSITIVE  # noqa: E402
from tests.test_merchant_agent_checkout_facts import _assert_throwaway_database as _d11_throwaway  # noqa: E402
from tests.test_merchant_agent_checkout_facts import _create_table as _d11_create_facts  # noqa: E402
from tests.test_partner_agent_merchant_blocks import _create_table as _d11_create_blocks  # noqa: E402

D11_MERCHANT = "merchant.example"  # `_open`'s merchant_domain
D11_RAILS_ENV = rail_router.PARTNER_AGENT_RUN_CONDITION_RAILS_ENV


@pytest.fixture
async def d11_records(monkeypatch):
    """Migrations 294 and 295's tables, empty (on Postgres the migrations themselves), and the setting naming
    the token rail. A test that wants the condition met records the fact itself."""
    _d11_throwaway()
    monkeypatch.delenv(d11_facts.MAX_AGE_ENV, raising=False)
    monkeypatch.setenv(D11_RAILS_ENV, "a2a")
    for create, table in ((_d11_create_facts, d11_facts.TABLE), (_d11_create_blocks, d11_blocks.TABLE)):
        await create()
        await database.execute(f"DELETE FROM {table}")
    yield
    for create, table in ((_d11_create_facts, d11_facts.TABLE), (_d11_create_blocks, d11_blocks.TABLE)):
        await create()
        await database.execute(f"DELETE FROM {table}")


async def _d11_met():
    assert await d11_facts.record_check(D11_MERCHANT, D11_POSITIVE)


async def _d11_block(rail="a2a", purchase="ap_" + "f" * 24):
    """Another purchase met a challenge on this merchant (an event the worker or an operator recorded)."""
    assert await d11_blocks.record_block(
        rail, D11_MERCHANT, "captcha", source=d11_blocks.SOURCE_WORKER, purchase_id=purchase) == "recorded"


async def _d11_events():
    return await d11_blocks.list_blocks(rail="a2a", domain=D11_MERCHANT) or []


def _d11_reads_forbidden(monkeypatch):
    """Every read or write of either record is noted (and answers 'not met' / 'not recorded'); the returned list
    must stay empty. Noted rather than raised: the worker's backstops would swallow a raise."""
    touched = []

    def _noting(name, answer):
        async def _note(*a, **k):
            touched.append(name)
            return answer
        return _note

    for module, name, answer in ((d11_facts, "meets_run_condition", False), (d11_facts, "run_condition_answer", False),
                                 (d11_blocks, "is_clear", False), (d11_blocks, "clear_answer", False),
                                 (d11_blocks, "record_block", None)):
        monkeypatch.setattr(module, name, _noting(name, answer))
    return touched


@pytest.mark.parametrize("setting", [None, "reap", "a2a_rail"])
async def test_off_the_worker_reads_and_writes_no_record_and_a_captcha_fails_as_before(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv(D11_RAILS_ENV, raising=False)
    else:
        monkeypatch.setenv(D11_RAILS_ENV, setting)
    touched = _d11_reads_forbidden(monkeypatch)
    pid = await _placing()  # the token request and the `paid` relay: nothing read
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("failed", "checkout_captcha_failed")
    assert touched == []


async def test_on_and_met_the_checkout_runs_as_before_and_a_captcha_is_recorded_before_the_failure(
        d11_records, caplog):
    await _d11_met()
    await _d11_block(rail="reap", purchase="rp_" + "e" * 24)  # Reap's event says nothing about this rail
    pid = await _placing()
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("failed", "checkout_captcha_failed")
    [event] = await _d11_events()
    assert (event["rail"], event["merchant_domain"], event["kind"], event["source"], event["purchase_id"],
            event["cleared_at"]) == ("a2a", D11_MERCHANT, "captcha", "worker", pid, None)
    assert any(f"merchant challenge recorded purchase={pid}" in r.getMessage() for r in caplog.records)
    # The merchant is now closed to this rail's agent: a new purchase there is refused before its token.
    second = await _locked()
    rail = Rail()
    assert await _step(rail, second) == "refused"
    assert (rail.sent, (await _row(second))["refusal_reason"]) == ([], "merchant_not_eligible")


async def test_a_crash_after_the_record_then_the_same_answer_again_is_one_event(d11_records, monkeypatch):
    await _d11_met()
    pid = await _placing()
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    real_failed = worker._checkout_failed

    async def _crash(*a, **k):
        raise RuntimeError("instance killed after the record")

    monkeypatch.setattr(worker, "_checkout_failed", _crash)
    with pytest.raises(RuntimeError):
        await _step(Rail().on("checkout.await#poll", answer), pid)
    monkeypatch.setattr(worker, "_checkout_failed", real_failed)
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = NULL WHERE id = :i", {"i": pid})
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    assert [e["purchase_id"] for e in await _d11_events()] == [pid]


@pytest.mark.parametrize("status", ["form-error", "pay-disabled"])
async def test_a_failure_that_is_not_a_challenge_records_nothing(d11_records, status):
    await _d11_met()
    pid = await _placing()
    answer = _checkout_result(status, success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    assert await _d11_events() == []


@pytest.mark.parametrize("success", [True, None])
async def test_a_contradictory_captcha_is_held_and_records_nothing(d11_records, success):
    await _d11_met()
    pid = await _placing()
    answer = _checkout_result("captcha-failed", success=success, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "checkout_result_unrecognised"
    assert await _d11_events() == []


async def test_a_record_that_does_not_land_is_logged_for_an_operator_and_the_purchase_still_fails(
        d11_records, monkeypatch, caplog):
    await _d11_met()
    pid = await _placing()

    async def _fails(*a, **k):
        return None

    monkeypatch.setattr(d11_blocks, "record_block", _fails)
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    [record] = [r for r in caplog.records if "challenge NOT recorded" in r.getMessage()]
    assert record.levelname == "ERROR" and f"purchase={pid}" in record.getMessage()
    assert (await _row(pid))["last_error_code"] == "checkout_captcha_failed"


@pytest.mark.parametrize("closed", ["no_fact", "open_block", "facts_table_missing", "blocks_table_missing"])
async def test_on_and_not_met_no_token_is_asked_for_and_the_purchase_is_refused(d11_records, closed, caplog):
    if closed != "no_fact":
        await _d11_met()
    if closed == "open_block":
        await _d11_block()
    pid = await _locked()
    if closed == "facts_table_missing":
        await database.execute(f"DROP TABLE {d11_facts.TABLE}")
    if closed == "blocks_table_missing":
        await database.execute(f"DROP TABLE {d11_blocks.TABLE}")
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    with caplog.at_level(logging.WARNING):
        assert await _step(rail, pid) == "refused"
    row = await _row(pid)
    assert rail.sent == [], "not even the card read: nothing goes to the rail"
    assert (row["state"], row["refusal_reason"], row["payment_token_id"], row["pending_write"], row["claimed_by"]) == (
        "refused", "merchant_not_eligible", None, None, None)
    assert any("agent_run_condition: not met rail=a2a merchant=merchant.example" in r.getMessage()
               for r in caplog.records), "the cause goes to the log"


async def test_a_challenge_recorded_while_the_buyer_approves_stops_the_paid_relay(d11_records):
    await _d11_met()
    pid = await _approving()  # the token was minted while the merchant was open
    await _d11_block()
    rail = _approved(Rail())
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert rail.sent == [], "no `paid`, so no checkout setup"
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", "merchant_not_eligible", None)
    assert row["payment_token_id"] == "ptk_1", "the minted token stays on the row: counted as an orphan"


async def test_a_challenge_recorded_before_the_await_poll_stops_that_relay_too(d11_records):
    await _d11_met()
    pid = await _approving()
    assert await _step(Rail().on("payment.visa-action-required#paid", _await("PENDING")), pid) == "waiting_on_buyer"
    await _d11_block()
    rail = Rail().on("payment.await#poll", _token_result()).on("checkout.setup#submit", CONFIRM_CARD)
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"], row["pending_write"]) == ([], "failed", "merchant_not_eligible", None)


async def test_a_buyer_still_inside_the_schedule_gap_is_not_read_for(d11_records, monkeypatch):
    """The re-check sits after the schedule: a tick that sends nothing reads nothing."""
    await _d11_met()
    pid = await _approving()
    await _ago("state_entered_at", 0, pid)
    touched = _d11_reads_forbidden(monkeypatch)
    assert await _step(Rail(), pid) == "waiting_on_buyer"
    assert touched == []


async def test_a_checkout_being_placed_is_never_re_checked(d11_records, monkeypatch):
    """A send already out is not reached: a placing purchase is polled whatever the records say."""
    await _d11_met()
    pid = await _placing()
    await _d11_block()
    touched = _d11_reads_forbidden(monkeypatch)
    rail = Rail().on("checkout.await#poll", _checkout_await(state="processing"))
    assert await _step(rail, pid) == "placing"
    assert (rail.actions(), touched) == (["checkout.await#poll"], [])


async def test_the_challenge_is_recorded_while_the_purchase_is_still_paying(d11_records, monkeypatch):
    """Recorded BEFORE the failure: a step cut between the two leaves the row live, so a retry records it."""
    await _d11_met()
    pid = await _placing()
    real_record, seen = d11_blocks.record_block, []

    async def _watch(*a, **k):
        seen.append((await _row(pid))["state"])
        return await real_record(*a, **k)

    monkeypatch.setattr(d11_blocks, "record_block", _watch)
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    assert seen == ["paying"]


async def test_a_record_that_raises_is_logged_and_the_purchase_still_fails(d11_records, monkeypatch, caplog):
    await _d11_met()
    pid = await _placing()

    async def _raises(*a, **k):
        raise RuntimeError("the record's own guard was bypassed")

    monkeypatch.setattr(d11_blocks, "record_block", _raises)
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    messages = [r.getMessage() for r in caplog.records]
    assert any("challenge record raised" in m and "error_type=RuntimeError" in m for m in messages)
    assert any("challenge NOT recorded" in m for m in messages)
    assert (await _row(pid))["last_error_code"] == "checkout_captcha_failed"


# ── Follow-ups: the relay waits out a record it cannot read; the CAPTCHAs met while off are backfilled ──────

from scripts import partner_agent_merchant_blocks as d11_tool  # noqa: E402

D11_STAFF = "Ops Person One"
D11_HELD = worker.RUN_CONDITION_UNREADABLE


async def _d11_unreadable(which):
    """Make one record unreadable the way production can: its table missing, or its read failing."""
    if which == "facts_table_missing":
        await database.execute(f"DROP TABLE {d11_facts.TABLE}")
    elif which == "blocks_table_missing":
        await database.execute(f"DROP TABLE {d11_blocks.TABLE}")
    else:
        raise AssertionError(which)


async def _d11_readable_again(which):
    if which == "facts_table_missing":
        await _d11_create_facts()
        await _d11_met()
    else:
        await _d11_create_blocks()


@pytest.mark.parametrize("which", ["facts_table_missing", "blocks_table_missing"])
async def test_an_unreadable_record_at_the_paid_relay_is_a_hold_and_the_next_tick_sends(d11_records, which):
    await _d11_met()
    pid = await _approving()
    before = await _row(pid)
    await _d11_unreadable(which)
    rail = _approved(Rail())
    assert await _step(rail, pid) == D11_HELD
    row = await _row(pid)
    assert rail.sent == [], "no `paid`, so no checkout setup"
    assert (row["state"], row["pending_write"], row["claimed_by"], row["payment_token_id"]) == (
        "awaiting_authorization", None, None, "ptk_1")
    assert row["last_error_code"] == f"{before['last_error_code']}:{D11_HELD}"
    assert worker._paid_state(row) == worker._paid_state(before), "the schedule's slot is not spent"
    await _d11_readable_again(which)
    rail = _approved(Rail())
    assert await _step(rail, pid) == "placing"
    assert rail.actions()[0] == "payment.visa-action-required#paid"


async def test_an_unreadable_record_at_the_await_poll_relay_is_a_hold_too(d11_records):
    await _d11_met()
    pid = await _approving()
    assert await _step(Rail().on("payment.visa-action-required#paid", _await("PENDING")), pid) == "waiting_on_buyer"
    await _d11_unreadable("blocks_table_missing")
    rail = Rail().on("payment.await#poll", _token_result()).on("checkout.setup#submit", CONFIRM_CARD)
    assert await _step(rail, pid) == D11_HELD
    row = await _row(pid)
    assert (rail.sent, row["state"], row["pending_write"]) == ([], "awaiting_authorization", None)
    assert worker._paid_state(row)[1] == "await" and row["last_error_code"].endswith(f":{D11_HELD}")


async def test_a_hold_ends_at_the_approval_page_deadline_like_a_buyer_who_never_approves(d11_records):
    await _d11_met()
    pid = await _approving()
    await _d11_unreadable("facts_table_missing")
    assert await _step(_approved(Rail()), pid) == D11_HELD
    await _ago("buyer_action_expires_at", 5, pid)
    assert await ledger.expire_overdue_purchases() >= 1
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("expired", "buyer_action_expired")


async def test_a_definite_block_beside_an_unreadable_fact_still_fails_at_the_relay(d11_records):
    await _d11_met()
    pid = await _approving()
    await _d11_block()
    await _d11_unreadable("facts_table_missing")
    rail = _approved(Rail())
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"]) == ([], "failed", "merchant_not_eligible")


async def test_a_read_that_cannot_tell_is_a_hold_and_a_definite_no_is_a_failure(d11_records, monkeypatch):
    """The block reader's own answers, as the relay reads them: None holds, False fails."""
    await _d11_met()
    held, failed = await _approving(), await _approving()
    answers = {held: None, failed: False}
    current = []

    async def _answer(*_a, **_k):
        return answers[current[-1]]

    monkeypatch.setattr(d11_blocks, "clear_answer", _answer)
    current.append(held)
    assert await _step(_approved(Rail()), held) == D11_HELD
    current.append(failed)
    assert await _step(_approved(Rail()), failed) == "failed"
    assert (await _row(failed))["last_error_code"] == "merchant_not_eligible"


async def test_off_an_unreadable_record_changes_nothing_at_the_relay(monkeypatch):
    monkeypatch.delenv(D11_RAILS_ENV, raising=False)
    touched = _d11_reads_forbidden(monkeypatch)
    pid = await _approving()
    assert await _step(_approved(Rail()), pid) == "placing"
    assert touched == []


async def _d11_run_tool(*argv):
    lines = []
    code = await d11_tool.amain(d11_tool.build_parser().parse_args(list(argv)), out=lines.append)
    return code, lines


def _d11_payloads(lines, prefix):
    return [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]


async def _d11_captcha_while_off(monkeypatch):
    """A purchase whose checkout met a CAPTCHA while the setting did not name `a2a`: the worker's own code, no event."""
    monkeypatch.delenv(D11_RAILS_ENV, raising=False)
    pid = await _placing()
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    monkeypatch.setenv(D11_RAILS_ENV, "a2a")
    assert (await _row(pid))["last_error_code"] == d11_tool.CAPTCHA_FAILURE_CODE, "the code the worker writes"
    assert await _d11_events() == []
    return pid


async def test_the_backfill_records_a_captcha_the_worker_met_while_off_and_closes_the_merchant(d11_records, monkeypatch):
    await _d11_met()
    pid = await _d11_captcha_while_off(monkeypatch)
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    assert code == 0
    [candidate] = [c for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX) if c["purchase_id"] == pid]
    assert (candidate["domain"], candidate["recorded"], candidate["usable"]) == (D11_MERCHANT, False, True)
    [dry] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert (dry["action"], dry["would_record"], dry["purchase_ids"], dry["more"]) == ("backfill-captcha", 1, pid, False)
    assert await _d11_events() == [], "a dry run writes nothing"

    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 0
    assert [r["result"] for r in _d11_payloads(lines, d11_tool.RECORDED_PREFIX)] == ["recorded"]
    assert _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX) == [{"recorded": 1, "duplicate": 0, "failed": 0}]
    [event] = await _d11_events()
    assert (event["rail"], event["kind"], event["source"], event["purchase_id"], event["recorded_by"]) == (
        "a2a", "captcha", "operator", pid, D11_STAFF)

    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 0 and _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX) == [{"recorded": 0, "duplicate": 1, "failed": 0}]
    assert len(await _d11_events()) == 1, "one purchase, one event"
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [dry] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert (dry["would_record"], dry["purchase_ids"]) == (0, "")

    second = await _locked()
    rail = Rail()
    assert await _step(rail, second) == "refused"
    assert (rail.sent, (await _row(second))["refusal_reason"]) == ([], "merchant_not_eligible")


async def test_a_captcha_the_worker_recorded_is_shown_recorded_and_never_a_second_event(d11_records):
    await _d11_met()
    pid = await _placing()
    answer = _checkout_result("captcha-failed", success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [candidate] = [c for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX) if c["purchase_id"] == pid]
    assert candidate["recorded"] is True and _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)[0]["would_record"] == 0
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 0 and _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX)[0]["duplicate"] == 1
    [event] = await _d11_events()
    assert event["source"] == "worker", "the worker's own event stands"


@pytest.mark.parametrize("status", ["form-error", "pay-disabled"])
async def test_a_failure_that_is_not_a_captcha_is_no_candidate(d11_records, monkeypatch, status):
    monkeypatch.delenv(D11_RAILS_ENV, raising=False)
    pid = await _placing()
    answer = _checkout_result(status, success=False, order_id=None, total=None)
    assert await _step(Rail().on("checkout.await#poll", answer), pid) == "failed"
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    assert code == 0 and pid not in {c["purchase_id"] for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX)}
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 2 and any(line.startswith(f"{d11_tool.REFUSED_PREFIX}ids_not_candidates") for line in lines)


@pytest.mark.parametrize("argv,reason", [
    (("--by", D11_STAFF, "--apply"), "purchase_ids_required"),
    (("--by", D11_STAFF, "--purchase-ids", "ap_not_a_captcha_purchase", "--apply"), "ids_not_candidates"),
    (("--by", D11_STAFF, "--purchase-ids", "{pid},{pid}", "--apply"), "purchase_ids_invalid"),
    (("--by", "Ops‮Person", "--purchase-ids", "{pid}", "--apply"), "backfill_refused part=by_hidden_character"),
    (("--by", "  ", "--purchase-ids", "{pid}", "--apply"), "backfill_refused part=by_missing"),
    (("--by", D11_STAFF, "--purchase-ids", "ap_not_a_captcha_purchase"), "ids_not_candidates"),
])
async def test_a_backfill_that_does_not_name_reviewed_candidates_writes_nothing(d11_records, monkeypatch, argv, reason):
    pid = await _d11_captcha_while_off(monkeypatch)
    argv = tuple(a.replace("{pid}", pid) for a in argv)
    code, lines = await _d11_run_tool("backfill-captcha", *argv)
    assert code == 2 and lines[-1].startswith(f"{d11_tool.REFUSED_PREFIX}{reason}"), lines
    assert "Ops‮Person" not in "".join(lines), "a refusal never repeats what was typed"
    assert await _d11_events() == []


async def test_a_mixed_apply_with_an_unrecordable_id_writes_nothing(d11_records, monkeypatch):
    """All or nothing on the review: one listed id that is not a candidate, and the good one is not recorded either."""
    pid = await _d11_captcha_while_off(monkeypatch)
    other = await _locked()
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", f"{pid},{other}", "--apply")
    assert code == 2 and await _d11_events() == []


async def test_a_backfill_write_that_does_not_land_is_named_and_exits_2(d11_records, monkeypatch):
    pid = await _d11_captcha_while_off(monkeypatch)

    async def _fails(*_a, **_k):
        return None

    monkeypatch.setattr(d11_blocks, "record_block", _fails)
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 2
    assert any(line == f"{d11_tool.REFUSED_PREFIX}record_failed purchase_id={pid} nothing was written for it" for line in lines)
    assert _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX) == [{"recorded": 0, "duplicate": 0, "failed": 1}]


async def test_the_backfill_reaches_past_the_cap_because_recorded_purchases_sort_last(d11_records, monkeypatch):
    first, second = await _d11_captcha_while_off(monkeypatch), await _d11_captcha_while_off(monkeypatch)
    monkeypatch.setattr(d11_tool, "BACKFILL_MAX", 1)
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [dry] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert (dry["candidates"], dry["more"]) == (1, True)
    code, _ = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", dry["purchase_ids"], "--apply")
    assert code == 0
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [dry_next] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert dry_next["would_record"] == 1 and dry_next["purchase_ids"] != dry["purchase_ids"]
    assert {dry["purchase_ids"], dry_next["purchase_ids"]} == {first, second}


async def test_a_dry_run_given_ids_shows_only_those(d11_records, monkeypatch):
    first, second = await _d11_captcha_while_off(monkeypatch), await _d11_captcha_while_off(monkeypatch)
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", second)
    assert code == 0
    assert [c["purchase_id"] for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX)] == [second]
    [dry] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert (dry["candidates"], dry["would_record"], dry["purchase_ids"]) == (1, 1, second)
    assert first != second and await _d11_events() == []


async def test_only_a_failed_purchase_is_a_candidate_whatever_its_code_says(d11_records, monkeypatch):
    """A live or ended row carrying the code another way (a hand edit, a later state) is not a CAPTCHA the worker met."""
    pid = await _d11_captcha_while_off(monkeypatch)
    live = await _locked()
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = :c WHERE id = :i",
                           {"c": d11_tool.CAPTCHA_FAILURE_CODE, "i": live})
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    assert code == 0 and [c["purchase_id"] for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX)] == [pid]


async def test_a_slow_fact_read_at_the_relay_is_a_hold_with_nothing_sent(d11_records, monkeypatch, caplog):
    """The likeliest "cannot tell" in production: the fact's read runs past its bound. Held, never failed."""
    await _d11_met()
    pid = await _approving()
    real = database.fetch_one

    async def _slow_fact(query, *args, **kwargs):
        if d11_facts.TABLE in str(query):
            await asyncio.sleep(0.2)
        return await real(query, *args, **kwargs)

    monkeypatch.setattr(d11_facts, "READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database, "fetch_one", _slow_fact)
    rail = _approved(Rail())
    with caplog.at_level(logging.WARNING):
        assert await _step(rail, pid) == D11_HELD
    row = await _row(pid)
    assert (rail.sent, row["state"], row["pending_write"]) == ([], "awaiting_authorization", None)
    assert row["last_error_code"].endswith(f":{D11_HELD}")
    messages = [r.getMessage() for r in caplog.records]
    assert any("cause=record_unreadable" in m for m in messages)
    assert any(f"run condition unreadable at the relay purchase={pid}; held" in m for m in messages)


async def _d11_clear_merchant(age_seconds=0):
    """An operator clears the token rail on the merchant on D11's evidence (an event recorded for it first)."""
    assert await d11_blocks.record_block("a2a", D11_MERCHANT, "block", source=d11_blocks.SOURCE_OPERATOR,
                                         recorded_by=D11_STAFF) == "recorded"
    [event] = await d11_blocks.list_blocks(rail="a2a", domain=D11_MERCHANT, open_only=True)
    assert await d11_blocks.clear_blocks("a2a", D11_MERCHANT, ids=[event["id"]], basis="written_permission",
                                         cleared_by=D11_STAFF, evidence="ticket OPS-0002") == 1
    return event["id"]


async def test_a_captcha_older_than_a_clearance_is_held_back_and_never_reopens_the_merchant(d11_records, monkeypatch):
    await _d11_met()
    pid = await _d11_captcha_while_off(monkeypatch)
    await _ago("terminal_at", 3600, pid)  # met an hour ago
    await _d11_clear_merchant()  # cleared now, on newer evidence
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    assert code == 0
    [candidate] = [c for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX) if c["purchase_id"] == pid]
    assert (candidate["usable"], candidate["held_back"]) == (False, "cleared_since") and candidate["cleared_at"]
    [dry] = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)
    assert (dry["would_record"], dry["purchase_ids"]) == (0, "")
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 2 and lines[-1].startswith(f"{d11_tool.REFUSED_PREFIX}ids_not_candidates")
    assert await d11_blocks.list_blocks(rail="a2a", domain=D11_MERCHANT, open_only=True) == []
    assert await d11_blocks.is_clear("a2a", D11_MERCHANT) is True, "the clearance stands"


async def test_a_captcha_met_after_the_latest_clearance_is_recorded(d11_records, monkeypatch):
    await _d11_met()
    pid = await _d11_captcha_while_off(monkeypatch)
    await _d11_clear_merchant()
    backdate = ("CURRENT_TIMESTAMP - INTERVAL '30 days'" if IS_POSTGRES else "datetime('now', '-30 days')")
    await database.execute(f"UPDATE {d11_blocks.TABLE} SET cleared_at = {backdate}")  # cleared well before the CAPTCHA
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [candidate] = [c for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX) if c["purchase_id"] == pid]
    assert (candidate["usable"], candidate["held_back"]) == (True, None) and candidate["cleared_at"]
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", pid, "--apply")
    assert code == 0 and _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX)[0]["recorded"] == 1


async def test_a_clearance_on_another_rail_or_merchant_holds_nothing_back(d11_records, monkeypatch):
    await _d11_met()
    pid = await _d11_captcha_while_off(monkeypatch)
    await _ago("terminal_at", 3600, pid)
    assert await d11_blocks.record_block("reap", D11_MERCHANT, "block", source=d11_blocks.SOURCE_OPERATOR,
                                         recorded_by=D11_STAFF) == "recorded"
    [event] = await d11_blocks.list_blocks(rail="reap", domain=D11_MERCHANT, open_only=True)
    assert await d11_blocks.clear_blocks("reap", D11_MERCHANT, ids=[event["id"]], basis="written_permission",
                                         cleared_by=D11_STAFF, evidence="ticket OPS-0003") == 1
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    [candidate] = [c for c in _d11_payloads(lines, d11_tool.CANDIDATE_PREFIX) if c["purchase_id"] == pid]
    assert (candidate["usable"], candidate["held_back"], candidate["cleared_at"]) == (True, None, None)


async def test_a_rerun_with_the_same_ids_after_an_apply_past_the_cap_is_a_duplicate_not_a_refusal(
        d11_records, monkeypatch):
    first, second = await _d11_captcha_while_off(monkeypatch), await _d11_captcha_while_off(monkeypatch)
    monkeypatch.setattr(d11_tool, "BACKFILL_MAX", 1)
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF)
    listed = _d11_payloads(lines, d11_tool.DRY_RUN_PREFIX)[0]["purchase_ids"]
    assert listed in (first, second)
    assert (await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", listed, "--apply"))[0] == 0
    # The recorded one now sorts after the other, past the cap of one: the rerun still reads it by its id.
    code, lines = await _d11_run_tool("backfill-captcha", "--by", D11_STAFF, "--purchase-ids", listed, "--apply")
    assert code == 0 and _d11_payloads(lines, d11_tool.BACKFILLED_PREFIX) == [{"recorded": 0, "duplicate": 1, "failed": 0}]


# ── markets other than the US (services/payment_orchestration/a2a_markets.py) ─────────────────────────────


JP_ADDRESS = {
    "firstName": "Hana", "lastName": "Example", "phone": "+81312345678", "addressLine1": "1-1 Example-cho",
    "city": "Chiyoda", "region": "13", "postalCode": "100-0001", "country": "JP",
}
JPY_ENV = {
    "A2A_RAIL_MARKETS": "US,JP",
    worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV: "1500,JPY:1000",
    worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV: "1000,JPY:1500",
    worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV: "20000,JPY:30000",
    rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV: "USD:50000,JPY:30000",
}


@pytest.fixture
def jp_on(monkeypatch):
    for name, value in JPY_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("text,currency,minor", [
    ("CA$25.00", "CAD", 2500), ("C$ 25.00", "CAD", 2500), ("CAD 25.00", "CAD", 2500), ("25.00 CAD", "CAD", 2500),
    ("$25.00", "CAD", None), ("A$12.30", "AUD", 1230), ("AU$1,234.56", "AUD", 123456), ("A$12.30", "CAD", None),
    ("NZ$9.90", "NZD", 990), ("S$9.90", "SGD", 990), ("SG$9.90", "SGD", 990), ("NT$330", "TWD", 33000),
    ("NT$330.50", "TWD", 33050), ("$330", "TWD", None), ("NT$330", "USD", None),
    ("JPY 1,500", "JPY", 1500), ("1,500円", "JPY", 1500), ("JP¥1500", "JPY", 1500), ("¥1,500", "JPY", None),
    ("JPY 1,500.00", "JPY", None), ("₩15,000", "KRW", 15000), ("15,000원", "KRW", 15000), ("KRW 15000", "KRW", 15000),
    ("₩15,000.00", "KRW", None), ("HK$10.00", "HKD", 1000), ("HKD 1,234.50", "HKD", 123450), ("$10.00", "HKD", None),
    ("HK$10.00", "USD", None), ("HK$10.00", "TWD", None), ("25.00", "CAD", None), ("CA$25.00", "USD", None),
    ("€23,00", "EUR", None),
])
def test_a_total_outside_usd_is_read_only_by_a_mark_that_names_its_currency(text, currency, minor):
    assert worker.checkout_total_minor(text, currency) == minor


@pytest.mark.parametrize("country", ["US", "CA", "AU", "JP", "NZ", "SG", "KR", "TW", "HK", "GB"])
def test_the_checkout_shipping_needs_no_state_in_any_market(country):
    shipping = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100",
                "country": country, "address1": "1 Main St", "city": "Somewhere"}
    assert a2a_rail.checkout_shipping(shipping) == shipping  # the store platform's to ask for
    assert a2a_rail.checkout_shipping({**shipping, "state": "XX"})["state"] == "XX"


def test_a_ceiling_outside_usd_needs_its_own_three_entries_and_is_in_its_minor_units(monkeypatch, jp_on):
    row = {"currency": "JPY", "our_price_minor": 4650, "quantity": 2}
    # yen are whole units: 9300 + 10% (930) + 1500 shipping
    assert worker.checkout_ceiling(row) == (9300 + 930 + 1500, worker.CEILING_SOURCE_ALLOWANCE_V1)
    assert worker.checkout_ceiling({"currency": "JPY", "our_price_minor": 1, "quantity": 1})[0] == 1 + 1 + 1500
    for name in (worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV,
                 worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV):
        for value in ("1500", "", "JPY:x", "JPY:-1", "USD:1000"):  # a bare value is USD's, never JPY's
            monkeypatch.setenv(name, value)
            with pytest.raises(worker.CeilingRefused) as raised:
                worker.checkout_ceiling(row)
            assert (raised.value.code, raised.value.refused) == (worker.CHECKOUT_CEILING_UNCONFIGURED, True), (name, value)
        monkeypatch.setenv(name, JPY_ENV[name])
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV, "20000,JPY:11729")
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling(row)
    assert raised.value.code == worker.CHECKOUT_CEILING_OVER_CAP


def test_the_usd_ceiling_reads_as_before_and_a_usd_entry_wins_over_a_bare_value(monkeypatch, jp_on):
    row = {"currency": "USD", "our_price_minor": 10000, "quantity": 1}
    assert worker.checkout_ceiling(row)[0] == 10000 + 1500 + 1000  # the bare values, unchanged
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, "1500,USD:1000,JPY:1000")
    assert worker.checkout_ceiling(row)[0] == 10000 + 1000 + 1000
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, "JPY:1000")
    assert worker.checkout_ceiling(row)[0] == 10000 + 1500 + 1000  # no USD entry: the USD default


@pytest.mark.parametrize("markets", [None, "", "US", "CA"])
def test_a_ceiling_in_a_currency_whose_market_is_not_on_is_refused(monkeypatch, jp_on, markets):
    if markets is None:
        monkeypatch.delenv("A2A_RAIL_MARKETS", raising=False)
    else:
        monkeypatch.setenv("A2A_RAIL_MARKETS", markets)
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling({"currency": "JPY", "our_price_minor": 4650, "quantity": 1})
    assert (raised.value.code, raised.value.refused) == (worker.CHECKOUT_CURRENCY_UNSUPPORTED, True)


async def _jp_unlocked():
    pid = (await ledger.create_purchase(
        agent_id="agent_w", agent_user_ref_hash="h" * 64, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="JPY", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address=JP_ADDRESS))["id"]
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA)), pid) == "method_ready"
    return pid


@pytest.mark.usefixtures("jp_on")
async def test_a_yen_purchase_locks_and_mints_in_whole_yen_and_completes_on_a_yen_total():
    pid = await _jp_unlocked()
    rail = _lock_rail()
    assert await _step(rail, pid) == "approval_page_issued"
    expected = 4650 + 465 + 1500
    row = await _row(pid)
    assert (row["quoted_total_minor"], rail.payload("payment.token-setup#submit")["amount_cents"]) == (expected, expected)
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
    rail = _approved(Rail())
    assert await _step(rail, pid) == "placing"
    setup = rail.payload("checkout.setup#submit")
    assert (setup["shipping"]["country"], setup["shipping"]["state"], setup["shipping"]["zip_code"]) == (
        "JP", "13", "100-0001")
    rail = Rail().on("checkout.await#poll", _checkout_result(total="JPY 6,615"))
    assert await _step(rail, pid) == "completed"
    assert (await _row(pid))["final_total_minor"] == 6615


@pytest.mark.parametrize("total,code", [("¥6,615", "checkout_total_unverified"), ("$66.15", "checkout_total_unverified"),
                                        ("JPY 6,616", "checkout_total_over_approved")])
@pytest.mark.usefixtures("jp_on")
async def test_a_yen_total_that_does_not_read_as_yen_or_is_over_the_ceiling_is_held(total, code):
    pid = await _jp_unlocked()
    assert await _step(_lock_rail(), pid) == "approval_page_issued"
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
    assert await _step(_approved(Rail()), pid) == "placing"
    assert await _step(Rail().on("checkout.await#poll", _checkout_result(total=total)), pid) == code
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying", code)
    assert await _step(Rail(), pid) == "checkout_held"  # held for a human: nothing more is read or sent


async def test_a_yen_purchase_never_mints_while_its_market_is_off(monkeypatch, jp_on):
    pid = await _jp_unlocked()
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": ORDER, "t": TOTAL, "i": pid})
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US")
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    assert "payment.token-setup#submit" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == "token_currency_unsupported"


# ── review follow-ups: the address's market, and amounts outside USD ─────────────────────────────────────


CA_ADDRESS = {
    "firstName": "Pat", "lastName": "Example", "phone": "+16135550100", "addressLine1": "1 Example St",
    "city": "Ottawa", "region": "ON", "postalCode": "K1A 0B1", "country": "CA",
}
CAD_ENV = {
    "A2A_RAIL_MARKETS": "US,CA",
    worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV: "1500,CAD:1500",
    worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV: "1000,CAD:1500",
    worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV: "20000,CAD:30000",
    rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV: "USD:50000,CAD:30000",
}


@pytest.fixture
def ca_on(monkeypatch):
    for name, value in CAD_ENV.items():
        monkeypatch.setenv(name, value)


async def _cad_unlocked(address):
    pid = (await ledger.create_purchase(
        agent_id="agent_w", agent_user_ref_hash="h" * 64, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="CAD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address=address))["id"]
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA)), pid) == "method_ready"
    return pid


@pytest.mark.usefixtures("ca_on")
async def test_a_cad_purchase_with_a_us_address_never_locks_or_mints():
    pid = await _cad_unlocked(ADDRESS)  # a US (TX) address on a purchase priced in CAD
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    assert "payment.token-setup#submit" not in rail.actions()
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("failed", worker.SHIPPING_MARKET_MISMATCH)


@pytest.mark.usefixtures("ca_on")
async def test_a_locked_cad_purchase_with_a_us_address_never_mints():
    pid = await _cad_unlocked(ADDRESS)
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": ORDER, "t": TOTAL, "i": pid})
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    assert "payment.token-setup#submit" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == worker.SHIPPING_MARKET_MISMATCH


@pytest.mark.usefixtures("ca_on")
async def test_the_token_step_refuses_another_markets_address_even_past_the_lock(monkeypatch):
    """`_start_token`'s own check, should a row reach it without the lock branch's."""
    pid = await _cad_unlocked(CA_ADDRESS)
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
                           {"o": ORDER, "t": TOTAL, "i": pid})
    await database.execute("UPDATE a2a_rail_purchases SET shipping_address = :a WHERE id = :i",
                           {"a": json.dumps(ADDRESS), "i": pid})
    monkeypatch.setattr(worker, "_checkout_setup", lambda row: object())  # past the lock branch's checks
    real, calls = worker.a2a_markets.address_matches_currency, []

    def _lock_branch_passes(address, currency):
        calls.append(currency)
        return True if len(calls) == 1 else real(address, currency)  # the lock branch's call passes; the next is real

    monkeypatch.setattr(worker.a2a_markets, "address_matches_currency", _lock_branch_passes)
    rail = _lock_rail()
    assert await _step(rail, pid) == "failed"
    assert "payment.token-setup#submit" not in rail.actions()
    assert (await _row(pid))["last_error_code"] == worker.SHIPPING_MARKET_MISMATCH


def test_the_relay_is_never_built_with_another_markets_address(ca_on):
    row = {"id": "ap_0123456789abcdef01234567", "buyer_email": EMAIL, "checkout_url": "https://merchant.example/c",
           "currency": "CAD", "quoted_total_minor": 5000}
    assert worker._checkout_setup({**row, "shipping_address": ADDRESS}) is None
    setup = worker._checkout_setup({**row, "shipping_address": CA_ADDRESS})
    assert setup is not None and setup.currency == "CAD" and setup.shipping["country"] == "CA"
    assert worker._checkout_setup({**row, "currency": "USD", "shipping_address": ADDRESS}).currency == "USD"


def test_a_relay_is_built_without_a_zip_code_when_the_buyer_gave_no_postcode(monkeypatch):
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,HK")
    hk = {"firstName": "Pat", "lastName": "Example", "phone": "+85221234567", "addressLine1": "1 Example Road",
          "city": "Kowloon", "country": "HK"}
    row = {"id": "ap_0123456789abcdef01234567", "buyer_email": EMAIL, "checkout_url": "https://merchant.example/c",
           "currency": "HKD", "quoted_total_minor": 10000, "shipping_address": hk}
    setup = worker._checkout_setup(row)
    assert setup is not None and setup.currency == "HKD"
    assert setup.shipping["country"] == "HK" and "zip_code" not in setup.shipping and "state" not in setup.shipping
    assert worker._checkout_setup({**row, "currency": "USD"}) is None  # an HK address in USD: another market's
    us_without_zip = {**ADDRESS}
    us_without_zip.pop("postalCode")
    us_setup = worker._checkout_setup({**row, "currency": "USD", "shipping_address": us_without_zip})
    assert us_setup is not None and "zip_code" not in us_setup.shipping and us_setup.shipping["state"] == "TX"
    assert worker._checkout_setup({**row, "currency": "USD", "shipping_address": ADDRESS}).shipping["zip_code"] == (
        "78701")  # one given is sent


def _setup_with(*datas):
    from types import SimpleNamespace

    return SimpleNamespace(credential_fields_stripped=0, text_amounts_refused=False,
                           cards=[SimpleNamespace(data=d) for d in datas])


MEASURED_CARD = {"merchant": "merchant.example", "cardLast4": "3478", "cardBrand": "visa",
                 "shipping": {"email": EMAIL, "city": "Chiyoda", "zip_code": "100-0001", "state": "13"}}


@pytest.mark.parametrize("data,currency,ceiling,refused", [
    ({"total": "15.00", "currency": "USD"}, "JPY", 1500, True),   # a USD total against a yen ceiling
    ({"total": "24.00", "currency": "USD"}, "CAD", 2500, True),   # a USD total against a CAD ceiling
    ({"total": "24.00", "currency": "CAD"}, "CAD", 2500, True),   # no measured CAD shape: refused too
    ({"total": "1,500円"}, "JPY", 1500, True),
    ({"note": "合計 1,500円"}, "JPY", 1500, True),
    ({"note": "15,000원"}, "KRW", 15000, True),
    ({"note": "1500円"}, "JPY", 1500, True),          # no grouping comma: only the letter-currency rule sees it
    ({"note": "15000원"}, "KRW", 15000, True),
    ({"items": ["1万円"]}, "JPY", 10000, True),
    ({"note": "HK$100"}, "HKD", 10000, True),
    ({"note": "100港幣"}, "HKD", 10000, True),        # a number then 港幣 / 港元: the Han word after the 港
    ({"note": "100 港元"}, "HKD", 10000, True),
    ({"note": "330新台幣"}, "TWD", 33000, True),
    ({**MEASURED_CARD, "shipping": {"email": EMAIL, "city": "Kowloon", "country": "HK"}}, "HKD", 10000, False),
    (MEASURED_CARD, "JPY", 1500, False),                         # nothing money-like: the token cap decides
    ({**MEASURED_CARD, "shipping": {**MEASURED_CARD["shipping"], "address1": "1-2 円山町"}}, "JPY", 1500, False),
    ({"total": "15.00", "currency": "USD"}, "USD", 1500, False),  # USD unchanged: S1 within the ceiling
    ({"total": "15.01", "currency": "USD"}, "USD", 1500, True),
])
def test_outside_usd_a_card_with_any_amount_is_refused(data, currency, ceiling, refused):
    assert worker.setup_answer_refused(_setup_with(data), ceiling, currency) is refused


@pytest.mark.parametrize("text,currency,ceiling,refused", [
    ("Total: $15.00", "JPY", 1500, True),
    ("1,500円", "JPY", 1500, True),
    ("お支払い 1500円です", "JPY", 1500, True),
    ("Total JPY 1500", "JPY", 1500, True),
    ("15,000원 결제", "KRW", 15000, True),
    ("Total: HK$100.00", "HKD", 10000, True),
    ("共 100 港幣", "HKD", 10000, True),
    ("Deliver to 1 Example Road, Kowloon", "HKD", 10000, False),
    ("Ship to 3 台中市", "TWD", 33000, False),  # 台 is not a currency word alone
    ("Total: CA$24.00", "CAD", 2500, True),
    ("Your order is ready to confirm.", "JPY", 1500, False),
    ("Ship to 1-2 円山町, 150-0044", "JPY", 1500, False),
    ("Total: $15.00", "USD", 1500, False),  # USD unchanged
    ("Total: $15.01", "USD", 1500, True),
])
def test_outside_usd_prose_with_any_amount_is_refused(text, currency, ceiling, refused):
    assert a2a_rail._text_parts_refused(_text_only(text), None, ceiling, currency) is refused


@pytest.mark.parametrize("text,shaped", [
    # F1: a prefixed Han currency word is money whatever follows it; a bare 元 may begin a place name.
    ("100 港元整", True), ("100 港幣整", True), ("100 台幣元", True), ("100港幣", True), ("2 元朗", False),
    ("1-2 元町", False), ("1500 円", True),
    # F2: a currency word before the number, NTD, a market's code in any case, 蚊, and more total words.
    ("港幣 100", True), ("新台幣 330", True), ("新台幣330元", True), ("NTD 330", True), ("330 NTD", True),
    ("100 hkd", True), ("twd330", True), ("100蚊", True), ("總數 100", True), ("收費 50", True), ("運費 30", True),
    ("add 2 all", False), ("3 top picks", False), ("Ship to 3 台中市", False), ("香港 2 號", False),
    # A spaced 蚊 is money only when no Han character follows it (蚊帳 a mosquito net, 蚊子街 a street).
    ("1 蚊帳", False), ("No. 5 蚊子街", False), ("100 蚊", True), ("100 蚊 total", True), ("5蚊帳", True),
])
def test_outside_usd_the_hong_kong_and_taiwan_amount_shapes(text, shaped):
    assert amounts.text_amount_shaped(text) is shaped


def test_the_relay_carries_a_region_and_postcode_as_the_buyer_gave_them():
    # No address rules of ours: the store's checkout and Visa judge the address.
    row = {"id": "ap_0123456789abcdef01234567", "buyer_email": EMAIL, "checkout_url": "https://merchant.example/c",
           "currency": "USD", "quoted_total_minor": 5000, "merchant_domain": "merchant.example"}
    for over in ({"region": "ON"}, {"postalCode": "K1A 0B1"}, {"region": "Texas", "postalCode": "787011234"}):
        setup = worker._checkout_setup({**row, "shipping_address": {**ADDRESS, **over}})
        assert setup is not None, over
        sent = {"region": setup.shipping.get("state"), "postalCode": setup.shipping.get("zip_code")}
        assert {k: sent[k] for k in over} == over


@pytest.mark.parametrize("text,currency", [("CA$12.00 USD", "CAD"), ("CA$12.00 USD", "USD"), ("$12.00 CAD", "USD"),
                                           ("JPY 1,500 USD", "JPY")])
def test_a_total_with_two_currencies_reads_as_neither(text, currency):
    assert worker.checkout_total_minor(text, currency) is None


@pytest.mark.parametrize("name,value", [
    (worker.A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS_ENV, "1500,JPY:20000"),       # out of range: never a USD default
    (worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, "1000,JPY:999999999"),
    (worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV, "20000,JPY:0"),
    (worker.A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR_ENV, "JPY:30000,20000"),     # a bare value only comes first
    (worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, "1000,JPY:1,500"),  # a thousands separator
])
def test_an_unusable_non_usd_entry_refuses_and_never_takes_the_usd_default(monkeypatch, jp_on, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(worker.CeilingRefused) as raised:
        worker.checkout_ceiling({"currency": "JPY", "our_price_minor": 4650, "quantity": 1})
    assert (raised.value.code, raised.value.refused) == (worker.CHECKOUT_CEILING_UNCONFIGURED, True)


@pytest.mark.parametrize("value,shipping", [("2,000", 1000), ("USD:1,000", 1000), ("1,000", 1000), ("2000", 2000),
                                            ("USD:2000", 2000), ("2000,JPY:1500", 2000)])
def test_a_usd_value_with_a_thousands_separator_falls_back_to_the_default_as_before(monkeypatch, caplog, value,
                                                                                   shipping):
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, value)
    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        ceiling = worker.checkout_ceiling({"currency": "USD", "our_price_minor": 10000, "quantity": 1})[0]
    assert ceiling == 10000 + 1500 + shipping
    assert ("," in value and ":" not in value.split(",")[1]) == any(
        "not CUR:value entries" in r.getMessage() for r in caplog.records)


# ── delta review of fbcecf3ed: every amount shape outside USD, and the rest ─────────────────────────────


#: Every shape the delta review named: each must refuse on a non-USD purchase.
NON_USD_PROSE_AMOUNTS = [
    "1500 yen", "15000 won", "1500 円", "合計 1500", "総額 1500", "お会計は1500です", "15 Canadian dollars",
    "Order value 1500", "合计1500元", "총액 15000", "15000 원", "1500円送料", "Amount: 1500", "20 NZ dollars",
]
#: What a confirm card or prose holds that is not an amount: never refused for it.
NON_USD_NOT_AMOUNTS = [
    "3丁目1-2", "〒150-0043", "Qty 2", "2026-11-27", "円山町", "1-2 円山町", "1-2 元町", "1 원효로",
    "Ship to 1-2 円山町, 150-0044", "Your order is ready to confirm.", "15 entries",
]


@pytest.mark.parametrize("text", NON_USD_PROSE_AMOUNTS)
def test_outside_usd_every_amount_shape_in_prose_refuses(text):
    assert a2a_rail._text_parts_refused(_text_only(text), None, 150000, "JPY") is True


@pytest.mark.parametrize("text", NON_USD_NOT_AMOUNTS)
def test_outside_usd_addresses_postcodes_quantities_and_dates_do_not_refuse(text):
    assert a2a_rail._text_parts_refused(_text_only(text), None, 150000, "JPY") is False
    assert worker.setup_answer_refused(_setup_with({**MEASURED_CARD, "note": text}), 150000, "JPY") is False


@pytest.mark.parametrize("data", [
    {"note": "1500 円"}, {"note": "1500 yen"}, {"合計": "1500"}, {"总计": "1500"}, {"label": "15000 won"},
    {"合計": "15000"}, {"summary": {"総額": "1500"}}, {"lines": [{"金額": "1500"}]},
    {"合計 1500": None}, {"1500円": True},  # an amount written as the key alone
])
def test_outside_usd_every_amount_shape_on_a_card_refuses(data):
    assert worker.setup_answer_refused(_setup_with(data), 1500, "JPY") is True


@pytest.mark.parametrize("ceiling", [None, 0, -1, True])
def test_outside_usd_no_positive_ceiling_refuses_even_a_card_without_an_amount(ceiling):
    assert worker.setup_answer_refused(_setup_with(MEASURED_CARD), ceiling, "JPY") is True
    assert worker.setup_answer_refused(_setup_with(MEASURED_CARD), 1500, "JPY") is False


async def test_a_relay_setup_without_a_currency_reads_its_prose_strictly():
    """`CheckoutSetup.currency` None is not USD: an amount within the ceiling is still refused."""
    answer = _task({"kind": "text", "text": "Order total: $51.49"},
                   _card("checkout.confirm", {"merchant": "merchant.example"}, ("confirm",), kind="confirm"))
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL, currency=None))
    assert relay.checkout.text_amounts_refused is True
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL, currency="USD"))
    assert relay.checkout.text_amounts_refused is False


@pytest.mark.usefixtures("ca_on")
async def test_a_legacy_row_on_the_approval_page_with_another_markets_address_fails_by_name():
    pid = await _cad_unlocked(CA_ADDRESS)
    rail = _lock_rail()
    assert await _step(rail, pid) == "approval_page_issued"
    await database.execute("UPDATE a2a_rail_purchases SET shipping_address = :a WHERE id = :i",
                           {"a": json.dumps(ADDRESS), "i": pid})
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    assert rail.sent == []
    assert (await _row(pid))["last_error_code"] == worker.SHIPPING_MARKET_MISMATCH


def test_a_currency_named_twice_reads_the_first_and_says_so(monkeypatch, caplog, jp_on):
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, "1000,JPY:1500,JPY:9000")
    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        assert worker.checkout_ceiling({"currency": "JPY", "our_price_minor": 4650, "quantity": 1})[0] == (
            4650 + 465 + 1500)
    assert any("names JPY twice" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", [",2000", "USD:2000,1500"])
def test_a_bare_value_after_another_entry_is_unusable_and_logged(monkeypatch, caplog, value):
    monkeypatch.setenv(worker.A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR_ENV, value)
    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        assert worker.checkout_ceiling({"currency": "USD", "our_price_minor": 10000, "quantity": 1})[0] == (
            10000 + 1500 + 1000)
    assert any("not CUR:value entries" in r.getMessage() for r in caplog.records)
