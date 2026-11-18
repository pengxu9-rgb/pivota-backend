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
    return {"brand": "Visa", "eci": "05", "cryptogram": CRYPTO, "expiryDate": "1129", "value": DPAN, **over}


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
        "number": DPAN, "expiry": "11/29", "cvv": CRYPTO, "name": "Pat Example", "brand": "visa"}


@pytest.mark.parametrize("token,brand,name", [
    (_network_token(expiryDate="1329"), "visa", "Pat"),        # month 13
    (_network_token(expiryDate="11/29"), "visa", "Pat"),       # not MMYY
    (_network_token(expiryDate=None), "visa", "Pat"),
    (_network_token(value="4895 3700 1200 3478"), "visa", "Pat"),
    (_network_token(value=None), "visa", "Pat"),
    (_network_token(cryptogram=""), "visa", "Pat"),
    (_network_token(cryptogram="has space ="), "visa", "Pat"),
    (_network_token(), "scheme_a", "Pat"),                     # a brand the checkout does not take
    (_network_token(), "visa", "   "),
    ("not a mapping", "visa", "Pat"),
])
def test_a_token_that_does_not_map_cleanly_is_none_never_a_partial_card(token, brand, name):
    assert a2a_rail.checkout_card_from_network_token(token, brand=brand, name=name) is None


def test_the_checkout_shipping_takes_only_its_own_keys_and_needs_a_us_state():
    good = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100",
            "country": "US", "address1": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "78701"}
    assert a2a_rail.checkout_shipping(good) == good
    assert a2a_rail.checkout_shipping({k: v for k, v in good.items() if k != "state"}) is None
    assert a2a_rail.checkout_shipping({**{k: v for k, v in good.items() if k != "state"}, "country": "GB"})
    assert a2a_rail.checkout_shipping({**good, "card": {"number": DPAN}}) is None      # no other keys
    assert a2a_rail.checkout_shipping({**good, "address2": f"apt {DPAN}"}) is None   # no card number anywhere
    assert a2a_rail.checkout_shipping({**good, "zip_code": ""}) is None
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
        cardholder_name="Pat Example", idempotency_key="pvt-ap_000000000000000000000001-checkout")
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
        "card": {"number": DPAN, "expiry": "11/29", "cvv": CRYPTO, "name": "Pat Example", "brand": "visa"},
        "idempotency_key": "pvt-ap_000000000000000000000001-checkout",
    }
    assert provider.bodies[0]["params"]["message"]["contextId"] != provider.bodies[1]["params"]["message"]["contextId"]
    assert relay.refused is None and relay.checkout.card("checkout.confirm") is not None
    assert relay.token.card("payment.token-result").data["paymentTokenId"] == "ptk_1"
    assert relay.token.credential_fields_stripped >= 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in NEEDLES:
        assert needle not in repr(relay) and needle not in logged, needle
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
    assert setup["payload"]["card"] == {"number": DPAN, "expiry": "11/29", "cvv": CRYPTO, "name": "Pat Example",
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


async def test_an_incomplete_contact_mints_no_token_at_all():
    pid = await _locked({k: v for k, v in ADDRESS.items() if k != "postalCode"})
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
        "routes/agent_internal_commerce_purchases.py", "tests/test_agent_internal_commerce_purchases.py",
        "tests/test_agent_internal_commerce_purchases_postgres.py",
        "tests/test_a2a_rail_checkout.py", "tests/test_a2a_rail_checkout_postgres.py", "tests/test_a2a_rail_client.py",
        "tests/test_a2a_rail_ledger_postgres.py", "main.py", "jobs/a2a_rail_operator.py",
        "services/payment_orchestration/checkout_amounts.py",
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
    "nested": [{"whatever": DPAN}, {"x": CRYPTO.rstrip("=")}],
}, ("confirm", "edit", "cancel"), kind="confirm"))


def _echo_result(order_id="1001"):
    return _task(_card("checkout.result", {
        "task_id": "ckout_abc123", "state": "completed", "success": True, "status": "payment-reached",
        "total": "$51.49", "errors": [], "order_id": order_id, "cardLast4": "3478", "duration_ms": 1,
        "card": {"number": DPAN, "expiry": EXPIRY_SHOWN, "cvv": CRYPTO, "brand": "visa"},
    }))


async def test_the_relay_scrubs_the_tokens_own_values_from_an_echoing_confirm_card():
    provider = _Provider(_token_result(), ECHO_CONFIRM)
    relay = await _relay(provider)
    confirm = relay.checkout.card("checkout.confirm")
    assert confirm is not None and "confirm" in confirm.action_ids()
    _no_credential_in(confirm.data)
    _no_credential_in(repr(relay))
    assert confirm.data["cardLast4"] == "3478" and confirm.data["merchant"] == "merchant.example"
    # The unpadded cryptogram is no base64 shape the stripper knows: only the by-value scrub catches it.
    assert CRYPTO.rstrip("=") not in json.dumps(confirm.data)


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


async def test_a_card_shaped_order_id_is_never_stored():
    pid = await _placing()
    assert await _step(Rail().on("checkout.await#poll", _echo_result(order_id=DPAN)), pid) == (
        worker.CHECKOUT_ORDER_ID_CARD_SHAPED)
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["last_error_code"]) == (
        "paying", None, worker.CHECKOUT_ORDER_ID_CARD_SHAPED)
    assert DPAN not in await _stored_text()


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


@pytest.mark.parametrize("spelling", TOTAL_SPELLINGS, ids=lambda d: json.dumps(d))
async def test_a_confirm_card_total_within_the_ceiling_is_confirmed(spelling):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(_fill(spelling, "$51.49")))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions() and "checkout.confirm#cancel" not in rail.actions()


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
    card = _confirm_with({"note": "free gift", "giftWrap": True, "items": 2, "zip": "78701", "sku": "SKU-40000000001",
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
    loose = CRYPTO.rstrip("=")  # alphanumeric: a valid identifier shape
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
    # The next ticks: the rail would answer everything, and nothing may go out.
    for _ in range(3):
        _everything(rail)
        await _ago("next_poll_at", 1, pid)
        await _ago("state_entered_at", 3600, pid)
        await worker.run_tick(_client(rail), W, limit=5)
    assert rail.actions()[before:] == []
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
#: Within the ceiling, or no amount at all: confirmed.
CONFIRMED_CARDS = {
    "exact": {"total": "$51.49"}, "zero": {"shipping_cost": "$0.00"},
    "nested_obj_ok": {"total": {"amount": "$51.49", "currency": "USD"}},
    "nested_amount_ok": {"order": {"lines": [{"price": "$20.00"}, {"price": "US$31.49"}]}},
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
    # Non-monetary numbers are not amounts.
    assert worker.confirm_card_refused({"zip": "78701", "phone": "+15125550100", "cardLast4": "3478", "items": 3,
                                        "orderRef": "40000000001", "sku": 40000000001}, TOTAL) is False


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
    # Inside `shipping` only address / contact keys are exempt: a number under any other key there (an
    # "eta") is read as money we cannot verify. Fail closed.
    "shipping_unknown_key_with_digits": {"shipping": {"method": "standard", "eta": "3-5 days"}},
}
#: Within the ceiling, or no amount: confirmed (the scan must not refuse the ordinary shapes).
READABLE_AND_WITHIN = {
    "documented_card_with_a_real_address": {
        "merchant": "merchant.example", "cardLast4": "3478", "cardBrand": "visa",
        "shipping": {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550100",
                     "country": "US", "address1": "1 Main St", "address2": "Unit 2", "city": "Austin",
                     "state": "TX", "zip_code": "78701"}},
    "parts_and_their_total": {"subtotal": "$45.00", "tax": "$5.00", "total": "$50.00"},
    "one_item_quantity_one": {"items": [{"qty": 1, "price": "$50.00"}]},
    "usd_lower_within": {"total": "51.49 usd"},
    "no_amount_words": {"total": None, "shipping": {"method": "standard"}, "note": "free"},
    # A boolean is never an amount (round 4: `residential: true`, `discountApplied: false` must not cancel).
    "boolean_total": {"total": True},
}


@pytest.mark.parametrize("name", sorted(UNREADABLE_OR_OVER))
def test_every_amount_the_scan_cannot_read_with_certainty_or_that_adds_up_past_the_ceiling_cancels(name):
    assert worker.confirm_card_refused(UNREADABLE_OR_OVER[name], TOTAL) is True


@pytest.mark.parametrize("name", sorted(READABLE_AND_WITHIN))
def test_readable_amounts_within_the_ceiling_and_a_real_address_are_confirmed(name):
    assert worker.confirm_card_refused(READABLE_AND_WITHIN[name], TOTAL) is False


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
    data = READABLE_AND_WITHIN["documented_card_with_a_real_address"]
    card = _task(_card("checkout.confirm", data, ("confirm", "edit", "cancel"), kind="confirm"))
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
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, "$1.00"), TOTAL) is False  # read
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH + 1, "$1.00"), TOTAL) is True
    assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, "$999.00"), TOTAL) is True
    # Every kind of leaf meets the same bound: a container and a number too, not only a string.
    for leaf in ({}, [], 7, None):
        assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH, leaf), TOTAL) is False, leaf
        assert worker.confirm_card_refused(_deep(worker._CARD_SCAN_MAX_DEPTH + 1, leaf), TOTAL) is True, leaf
    # The data itself, the list, then the items: exactly the cap is read, one more is refused.
    at_cap = {"items": list(range(worker._CARD_SCAN_MAX_NODES - 2))}
    over_cap = {"items": list(range(worker._CARD_SCAN_MAX_NODES - 1))}
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


async def _operator(*argv):
    import io
    import jobs.a2a_rail_operator as operator

    out = io.StringIO()
    code = await operator.run(list(argv), out=out)
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
#: The P2-3 killing cases: each is caught ONLY by the guard named (proved by mutation, see the report).
GUARD_KILLERS = {
    "nfkc_fullwidth_code": ({"total": "$40.00 ＥＵＲ"}, True),
    "nfkc_fullwidth_usd_reads_as_usd": ({"total": "＄51.49"}, False),
    "invisible_strip_note": ({"note": "$​999.00"}, True),
    "invisible_u2062": ({"note": "$⁢999.00"}, True),
    "invisible_u034f": ({"note": "$͏999.00"}, True),
    "line_items_under_a_grand_total": ({"grand_total": ["$30.00", "$30.00"]}, True),
    "line_items_totals_list": ({"totals": [{"price": "$40.00"}, {"price": "$40.00"}]}, True),
    "qty_times_price_under_a_total": ({"total": {"qty": 20, "price": "$50.00"}}, True),
    "money_key": ({"money": 999}, True),
    "postage_key": ({"postage": 999}, True),
    "loose_spacing_within_ceiling": ({"note": "$  5.00"}, True),
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
    "zip_int": _doc(zip_code=78701), "address2_apt": _doc(address2="Apt 4"),
    "merchant_euro_24": {**_doc(), "merchant": "Euro 24 Shop"},
    "item_gel_pen": {**_doc(), "items": [{"name": "Gel Pen 12 pack"}]},
    "item_pound_bag": {**_doc(), "items": [{"name": "Dog food 30 pound bag"}]},
    "discount_applied_false": {**_doc(), "discountApplied": False},
    "card_last4_int": {**_doc(), "cardLast4": 3478},
    "card_object": {**_doc(), "card": {"last4": "3478", "brand": "visa"}},
    "order_ref": {**_doc(), "orderRef": "40000000001"},
    "accented_key": {**_doc(), "código": "78701"},
    "items_with_their_totals": {"items": [{"price": "$51.49"}], "totals": {"total": "$51.49"}},
}
#: Unmarked or lookalike amounts the round-3 scan read wrong.
UNMARKED_GAPS = {
    "shipping_rate": {"shippingRate": 999}, "gst": {"gst": 999}, "tip": {"tip": 999}, "freight": {"freight": "999.00"},
    "surcharge": {"surcharge": 999}, "prose_total_word": {"summary": "Total 999.00"},
    "amount_in_words": {"total": "nine hundred ninety-nine dollars"},
    "python_literal_string": {"meta": "{'total': 999}"},
    "cyrillic_ca_dollar": {"total": "СА$40.00"}, "cyrillic_ky_dollar": {"total": "КY$50.00"},
    "cyrillic_us_dollar": {"total": "UЅ$40.00"}, "cyrillic_eur": {"total": "$40.00 ЕUR"},
    "lookalike_key": {"tоtal": 999},
}


@pytest.mark.parametrize("name", sorted(SUM_BYPASSES))
def test_every_sum_spelling_past_the_ceiling_cancels(name):
    assert worker.confirm_card_refused(SUM_BYPASSES[name], TOTAL) is True


@pytest.mark.parametrize("name", ["qty_string", "quantity_ordered", "unit_cost", "price_money_object",
                                  "total_named_parts", "json_string_part"])
async def test_the_sum_bypasses_end_to_end_cancel_instead_of_confirming(name):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(SUM_BYPASSES[name])).on("checkout.confirm#cancel", CANCELLED)
    assert await _step(rail, pid) == "failed"
    assert "checkout.confirm#confirm" not in rail.actions()


def test_a_fractional_quantity_cancels_and_a_within_ceiling_product_confirms():
    assert worker.confirm_card_refused({"items": [{"qty": "1.5", "price": "$10.00"}]}, TOTAL) is True
    assert worker.confirm_card_refused({"items": [{"qty": 2.5, "price": "$10.00"}]}, TOTAL) is True
    assert worker.confirm_card_refused({"items": [{"qty": "2", "unitPrice": {"amount": "$25.00"}}]}, TOTAL) is False


@pytest.mark.parametrize("name", sorted(GUARD_KILLERS))
def test_each_guard_has_a_case_only_it_catches(name):
    data, refused = GUARD_KILLERS[name]
    assert worker.confirm_card_refused(data, TOTAL) is refused


@pytest.mark.parametrize("name", sorted(NEVER_CANCEL))
def test_the_buyers_own_data_and_ordinary_names_never_cancel(name):
    assert worker.confirm_card_refused(NEVER_CANCEL[name], TOTAL) is False


@pytest.mark.parametrize("name", ["address_pen_rd", "address_euro_plaza", "email_aud1", "email_try2buy"])
async def test_the_documented_card_echoing_the_buyers_own_contact_confirms_end_to_end(name):
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(NEVER_CANCEL[name]))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()


@pytest.mark.parametrize("name", sorted(UNMARKED_GAPS))
def test_unmarked_lookalike_and_worded_amounts_cancel(name):
    assert worker.confirm_card_refused(UNMARKED_GAPS[name], TOTAL) is True


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
            "checkout_payment_no_order", "checkout_payment_reached_unconfirmed"} <= worker._CHECKOUT_PAYMENT_REACHED_CODES


@pytest.mark.parametrize("code", sorted([
    "checkout_total_over_approved", "checkout_total_unverified", "checkout_order_id_card_shaped",
    "checkout_payment_no_order", "checkout_payment_reached_unconfirmed"]))
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


# ── discounts: a discount only lowers the charge (never cancels for its sign, never summed) ─────

DISCOUNTED_CARDS = {
    "subtotal_discount_total": {"subtotal": "$45.00", "discount": "-$5.00", "total": "$40.00"},
    "unicode_minus": {"subtotal": "$45.00", "discount": "−$5.00", "total": "$40.00"},
    "discount_money_object": {"subtotal": "$45.00", "discount": {"amount": "-$5.00", "currency": "USD"},
                              "total": "$40.00"},
    "discount_amount_key": {"subtotal": "$45.00", "discountAmount": "-$5.00", "total": "$40.00"},
    "total_discounts": {"total_line_items_price": "$45.00", "total_discounts": "-$5.00", "total": "$40.00"},
    "discounts_list": {"subtotal": "$45.00", "discounts": [{"amount": "-$5.00"}, {"amount": "$1.00"}],
                       "total": "$39.00"},
    "positive_savings": {"subtotal": "$45.00", "savings": "$5.00", "total": "$40.00"},
    "promo_amount": {"subtotal": "$45.00", "promoAmount": "$5.00", "total": "$40.00"},
    "coupon_amount": {"subtotal": "$45.00", "coupon": {"amount": "-$5.00", "code": "SAVE10"}, "total": "$40.00"},
    "coupon_code_with_digits": {"subtotal": "$45.00", "couponCode": "SAVE10", "total": "$45.00"},
    "percent_off": {"subtotal": "$45.00", "discount": "10% off", "total": "$40.50"},
    # The sign rule: a negative amount under a pure discount key is taken off, whatever its size (it can
    # never be a hidden charge). Only an explicit slot (a discount's own amount, the sum of the discounts)
    # is capped at the ceiling, since a positive there is not summed.
    "negative_discount_of_any_size": {"subtotal": "$45.00", "discount": "-$999.00", "total": "$40.00"},
}
REFUSED_WITH_A_DISCOUNT = {
    "discount_in_eur_symbol": {"subtotal": "$45.00", "discount": "-€5.00", "total": "$40.00"},
    "discount_in_eur_code": {"subtotal": "$45.00", "discount": "-5.00 EUR", "total": "$40.00"},
    "discount_malformed": {"subtotal": "$45.00", "discount": "-$5.0", "total": "$40.00"},
    "discount_unmarked_string": {"subtotal": "$45.00", "discount": "-5.00", "total": "$40.00"},
    "negative_total": {"total": "-$5.00"},
    "negative_line_price": {"items": [{"price": "-$5.00"}, {"price": "$40.00"}]},
    "negative_subtotal": {"subtotal": "-$45.00", "total": "$40.00"},
    # A discount never lowers a sum: items past the ceiling still cancel whatever the discount says.
    "discount_does_not_rescue_an_over_sum": {"items": [{"price": "$30.00"}, {"price": "$30.00"}],
                                             "discount": "-$10.00"},
    # A discount slot larger than the ceiling is more likely a mislabelled total.
    "discount_slot_over_the_ceiling": {"subtotal": "$45.00", "discount": {"amount": "-$999.00"}, "total": "$40.00"},
    # A bare number under a discount key: its units are unknown, exactly as under a money key (v5: -500 used
    # to be read as cents and confirm).
    "bare_negative_number": {"subtotal": "$45.00", "discount": -500, "total": "$40.00"},
    # A POSITIVE amount under a pure discount key outside an explicit slot is money and is summed (v5: it
    # used to be ignored): $45 + $10 is past the ceiling.
    "positive_savings_summed": {"subtotal": "$45.00", "savings": "$10.00", "total": "$40.00"},
}


@pytest.mark.parametrize("name", sorted(DISCOUNTED_CARDS))
def test_a_discount_within_the_ceiling_never_cancels_for_its_sign(name):
    assert worker.confirm_card_refused(DISCOUNTED_CARDS[name], TOTAL) is False


@pytest.mark.parametrize("name", sorted(REFUSED_WITH_A_DISCOUNT))
def test_an_unreadable_discount_or_a_negative_elsewhere_still_cancels(name):
    assert worker.confirm_card_refused(REFUSED_WITH_A_DISCOUNT[name], TOTAL) is True


async def test_a_discounted_checkout_within_the_ceiling_confirms_end_to_end():
    pid = await _approving()
    rail = _approved(Rail(), setup_answer=_confirm_with(DISCOUNTED_CARDS["subtotal_discount_total"]))
    assert await _step(rail, pid) == "placing"
    assert "checkout.confirm#confirm" in rail.actions()



# ── discount keys that carry the charge, nested discounts, and a discount's size ───────────────

#: Keys with a discount word that name what is CHARGED: money, read strictly ($999.00 over the ceiling).
CHARGED_WITH_A_DISCOUNT_WORD = [
    "totalAfterDiscount", "discountedTotal", "discounted_price", "priceAfterDiscount", "totalWithCoupon",
    "couponFee", "voucherFee", "promoShipping", "netOfDiscount", "amountAfterSavings", "subtotal_after_discount",
]
#: Pure discounts (the amount taken off): -$5.00 within the ceiling confirms.
PURE_DISCOUNT_SPELLINGS = [
    "discount", "discounts", "discountAmount", "total_discounts", "totalDiscounts", "discount_total", "savings",
    "promoAmount", "couponDiscount",
]


@pytest.mark.parametrize("key", CHARGED_WITH_A_DISCOUNT_WORD)
def test_a_discount_word_beside_a_money_word_is_money_and_cancels_over_the_ceiling(key):
    assert worker.confirm_card_refused({key: "$999.00"}, TOTAL) is True
    assert worker.confirm_card_refused({key: 99900}, TOTAL) is True  # a bare number under money: units unknown


@pytest.mark.parametrize("key", PURE_DISCOUNT_SPELLINGS)
def test_each_pure_discount_spelling_within_the_ceiling_confirms(key):
    assert worker.confirm_card_refused({"subtotal": "$45.00", key: "-$5.00", "total": "$40.00"}, TOTAL) is False


def test_a_total_after_discount_is_a_component_not_the_grand_total():
    # Only the exact grand-total spellings are grand totals (v5): `totalAfterDiscount` is a component, so
    # beside a $45 subtotal it adds up past the ceiling. Alone, or beside a discount, it is read as is.
    card = {"subtotal": "$45.00", "discount": "-$5.00", "totalAfterDiscount": "$40.00"}
    assert worker.confirm_card_refused(card, TOTAL) is True
    assert worker.confirm_card_refused({"discount": "-$5.00", "totalAfterDiscount": "$40.00"}, TOTAL) is False
    assert worker.confirm_card_refused({"subtotal": "$45.00", "discount": "-$5.00", "total": "$40.00"}, TOTAL) is False
    assert worker.confirm_card_refused({"totalAfterDiscount": "$51.50"}, TOTAL) is True
    # A discounted unit price still counts for quantity x price.
    assert worker.confirm_card_refused({"items": [{"qty": 2, "discounted_price": "$30.00"}]}, TOTAL) is True


@pytest.mark.parametrize("card,refused", [
    ({"discount": {"amount": "-$5.00", "total": "$999.00"}}, True),
    ({"coupon": {"value": "-$5.00", "finalPrice": "$999.00"}}, True),
    ({"discount": {"amount": "-$5.00", "unknown": 99900}}, True),   # anything but its amount: money
    ({"promos": [{"coupon": {"amount": "-$5.00", "net": "$999.00"}}]}, True),  # at every depth
    ({"discount": {"amount": "-$5.00", "currency": "USD", "code": "SAVE5"}}, False),
    ({"coupon": {"value": "-$5.00"}}, False),
    ({"discounts": [{"amount": "-$5.00"}, {"savings": {"amount": "-$1.00"}}]}, False),
], ids=["discount.total", "coupon.finalPrice", "discount.unknown", "deep_net", "amount_currency_code",
        "coupon.value", "nested_discounts"])
def test_under_a_discount_only_its_amount_is_a_discount(card, refused):
    assert worker.confirm_card_refused(card, TOTAL) is refused


@pytest.mark.parametrize("card,refused", [
    ({"discount_total": "$999.00"}, True),
    ({"discount_total": "-$5.00"}, False),
    ({"total_discounts": "$999.00"}, True),  # the only money field: a mislabelled total cannot pass
    ({"totalDiscounts": "-$999.00"}, True),
    ({"discount": -99900}, True),            # a bare number under a discount key: units unknown
    ({"discount": -500}, True),              # v5: unreadable, like under a money key (was read as $5.00)
    ({"discount": -5.5}, True),              # v5: unreadable (was read as $5.50)
    ({"discount": -999.5}, True),
    ({"discount": {"amount": "-$51.50"}}, True),   # a slot one cent past the ceiling
    ({"discount": {"amount": "-$51.49"}}, False),  # a slot at the ceiling
], ids=["discount_total_999", "discount_total_minus_5", "total_discounts_only", "totalDiscounts_minus_999",
        "bare_cents_over", "bare_int_within", "bare_fraction_within", "fraction_over", "slot_over", "slot_at"])
def test_a_discount_larger_than_the_ceiling_cancels(card, refused):
    assert worker.confirm_card_refused(card, TOTAL) is refused


#: Within the ceiling, where reading a key as a discount and reading it as money DISAGREE (a negative, a
#: small bare number, a component): the magnitude cap cannot stand in for the classification here.
@pytest.mark.parametrize("card", [
    {"discountedTotal": "-$5.00"},                      # a negative TOTAL: money, cancels
    {"discountedTotal": 500},                           # a bare number under a total: units unknown
    {"totalAfterDiscount": "-$5.00"},
    {"subtotal": "$45.00", "discountedTotal": "$40.00", "couponFee": "$10.00"},  # a fee is a component
], ids=["discountedTotal_negative", "discountedTotal_bare", "totalAfterDiscount_negative", "couponFee_component"])
def test_a_total_named_discount_key_within_the_ceiling_is_still_read_as_money(card):
    assert worker.confirm_card_refused(card, TOTAL) is True


@pytest.mark.parametrize("card", [
    {"discount": {"amount": "-$5.00", "total": "-$5.00"}},       # a negative total under a discount
    {"discount": {"amount": "-$5.00", "total": 4000}},            # a bare number under a total
    {"subtotal": "$45.00", "coupon": {"value": "-$5.00", "fee": "$10.00"}},  # a fee under a coupon is summed
    {"promos": [{"coupon": {"amount": "-$5.00", "finalPrice": "-$1.00"}}]},
], ids=["nested_total_negative", "nested_total_bare", "nested_fee_component", "deep_final_price_negative"])
def test_a_money_child_of_a_discount_within_the_ceiling_is_still_read_as_money(card):
    assert worker.confirm_card_refused(card, TOTAL) is True


# ── controller review v5: whole-word keys, exact grand totals, the sign rule, money beats contact ──

def test_keys_are_split_into_whole_words_in_every_spelling():
    from services.payment_orchestration import checkout_amounts as amounts

    for key in ("totalAfterDiscount", "TOTAL_AFTER_DISCOUNT", "total-after-discount", "total.after.discount",
                "Total After Discount", "totalafterdiscount", "ＴｏｔａｌＡｆｔｅｒＤｉｓｃｏｕｎｔ", "tоtalAfterDiscount"):
        assert amounts.key_words(key) == ("total", "after", "discount"), key
    assert amounts.key_words("DiscountedTotal2") == ("discounted", "total", "2")
    assert amounts.key_words("address1") == ("address", "1")
    assert amounts.key_words("grandtotal") == ("grand", "total")       # a run of known words is split
    assert amounts.key_words("internetDiscount") == ("internet", "discount")  # "internet" holds no "net"
    assert amounts.key_words("payeeDiscount") == ("payee", "discount")


#: A word that merely CONTAINS a qualifier ("internet" / "cabinet" / "network" hold "net", "payee" /
#: "paypal" hold "pay", "withdrawal" holds "with", "residue" holds "due", "aftercare" holds "after") is not
#: that word: each is a pure discount, and -$5.00 beside a $40.00 total confirms.
EMBEDDED_WORD_DISCOUNTS = [
    "internetDiscount", "cabinetDiscount", "payeeDiscount", "paypalDiscount", "withdrawalRebate",
    "residueDiscount", "aftercareDiscount", "networkPromo", "discountNetwork",
]


def test_a_word_that_only_contains_a_money_word_is_not_money():
    # "multiple" holds "tip", "residue" holds "due", "costume" holds "cost": none is a money word, so a
    # bare number under them is the buyer's data, not an amount with unknown units.
    assert worker.confirm_card_refused({"multiplePacks": 3, "residue": 2, "costume": "Size 12"}, TOTAL) is False
    assert worker.confirm_card_refused({"tipAmount": 3}, TOTAL) is True  # the whole word: money


@pytest.mark.parametrize("key", EMBEDDED_WORD_DISCOUNTS)
def test_a_word_that_only_contains_a_qualifier_does_not_make_a_discount_money(key):
    assert worker.confirm_card_refused({key: "-$5.00", "total": "$40.00"}, TOTAL) is False


#: Total-ish keys that are NOT one of the exact grand-total spellings are components: they add up, and
#: these add up past the $51.49 ceiling (the P1-B table, and words that only contain a qualifier).
COMPONENT_TOTALS = {
    "items_and_shipping_after_discount": {"itemsTotalAfterDiscount": "$40.00", "shippingTotalAfterDiscount": "$15.00"},
    "merchandise_and_shipping_with_tax": {"merchandiseTotalWithTax": "$40.00", "shippingTotalWithTax": "$15.00"},
    "items_and_shipping_net": {"itemsNetTotal": "$40.00", "shippingNetTotal": "$15.00"},
    "final_item_and_shipping": {"finalItemTotal": "$40.00", "finalShippingTotal": "$15.00"},
    "subtotal_and_discounted_shipping": {"subtotal": "$40.00", "discountedShippingTotal": "$15.00"},
    "line_items_after_discount_and_shipping": {"line_items_total_after_discount": "$40.00", "shipping": "$15.00"},
    "merchandise_with_tax": {"merchandiseTotalWithTax": "$40.00", "shipping": "$15.00"},
    "aftercare_total": {"subtotal": "$40.00", "aftercareTotal": "$30.00"},
    "cabinet_total": {"subtotal": "$40.00", "cabinetTotal": "$30.00"},
    "internet_service_total": {"subtotal": "$40.00", "internetServiceTotal": "$30.00"},
    "withholding_total": {"subtotal": "$40.00", "withholdingTotal": "$30.00"},
    # A subtotal is never the grand total, whatever qualifies it.
    "subtotal_after_discount": {"subtotalAfterDiscount": "$40.00", "shipping": "$15.00"},
}
#: The exact grand-total spellings, in each case and separator: checked alone, never added to their parts.
GRAND_TOTAL_SPELLINGS = [
    "total", "grandTotal", "grand_total", "grand-total", "totalAmount", "total_amount", "amountDue", "amount_due",
    "orderTotal", "order_total", "totalPrice", "total_price", "totalDue", "total_due", "balanceDue", "balance_due",
    "TOTAL", "GrandTotal",
]


@pytest.mark.parametrize("name", sorted(COMPONENT_TOTALS))
def test_a_total_that_is_not_an_exact_grand_total_is_a_component_and_adds_up(name):
    assert worker.confirm_card_refused(COMPONENT_TOTALS[name], TOTAL) is True


@pytest.mark.parametrize("key", GRAND_TOTAL_SPELLINGS)
def test_each_exact_grand_total_beside_its_parts_is_checked_against_the_larger_of_the_two(key):
    # Parts and their total within the ceiling: confirmed (the total is not added to its parts).
    assert worker.confirm_card_refused(
        {"subtotal": "$40.00", "tax": "$5.00", "shipping": "$5.00", key: "$50.00"}, TOTAL) is False
    # A grand total below the sum of its parts does not hide the parts: max(total, parts) is checked.
    assert worker.confirm_card_refused({"subtotal": "$45.00", "shipping": "$10.00", key: "$40.00"}, TOTAL) is True
    # A summary beside the line items is not counted twice.
    assert worker.confirm_card_refused({"items": [{"price": "$30.00"}], "summary": {key: "$30.00"}}, TOTAL) is False
    # Each object in a list adds the larger of its grand total and its parts: $45 + $10 is past the ceiling.
    assert worker.confirm_card_refused(
        {"orders": [{"subtotal": "$45.00", key: "$5.00"}, {"subtotal": "$10.00", key: "$10.00"}]}, TOTAL) is True


def test_a_quote_is_checked_before_its_discount():
    # The ceiling is the approved quote before any discount (checkout_ceiling): a $55 subtotal at a $50
    # ceiling cancels even when a $5 discount brings the total to $50.
    assert worker.confirm_card_refused({"subtotal": "$55.00", "discount": "-$5.00", "total": "$50.00"}, 5000) is True
    assert worker.confirm_card_refused({"subtotal": "$50.00", "discount": "-$5.00", "total": "$45.00"}, 5000) is False


#: P2-A: a money word beats every contact word but an address / contact LAST word. Each of these is money,
#: and a bare or unmarked amount under it cancels.
MONEY_WITH_A_CONTACT_WORD = [
    "serviceFee", "serviceCharge", "merchantFee", "carrierFee", "labelPrice", "currencyTotal", "titleTotal",
    "descriptionAmount", "totalWithCode", "promoCodeTotal", "barcodePrice", "shippingMethodCost",
    "shippingServiceCost", "cardSurcharge",
]
#: Exempt: an address / contact last word, and the exact code keys.
CONTACT_DESPITE_A_MONEY_WORD = {
    "shipping_address": {"shippingAddress": "1 Main St, Austin TX 78701"},
    "payment_method": {"paymentMethod": "Visa ending 3478"},
    "shipping_method": {"shippingMethod": "2-Day"},
    "delivery_date": {"deliveryDate": "2026-11-20"},
    "tax_id": {"taxId": "12-3456789"},
    "shipping_zip_code": {"shippingZipCode": "78701"},
    "coupon_code": {"couponCode": "SAVE10"}, "promo_code": {"promoCode": "2FOR1"},
    "discount_code": {"discountCode": "SPRING25"},
    # An all-digit code: under a code key it is a code, never an unmarked amount.
    "coupon_code_digits": {"couponCode": "2026"}, "promo_code_digits": {"promoCode": "10"},
}


@pytest.mark.parametrize("key", MONEY_WITH_A_CONTACT_WORD)
def test_a_contact_word_never_beats_a_money_word(key):
    assert worker.confirm_card_refused({key: 99900}, TOTAL) is True
    assert worker.confirm_card_refused({key: "999.00"}, TOTAL) is True
    assert worker.confirm_card_refused({key: "$5.00"}, TOTAL) is False  # readable and within: confirmed


@pytest.mark.parametrize("name", sorted(CONTACT_DESPITE_A_MONEY_WORD))
def test_an_address_or_contact_last_word_and_the_code_keys_stay_contact(name):
    assert worker.confirm_card_refused({**CONTACT_DESPITE_A_MONEY_WORD[name], "total": "$40.00"}, TOTAL) is False


#: P2-B: ordinary discount shapes within the ceiling, none of which may cancel.
ORDINARY_DISCOUNTS = {
    "shipping_discount": {"shippingDiscount": "-$5.00"}, "shipping_discount_snake": {"shipping_discount": "-$5.00"},
    "tax_discount": {"taxDiscount": "-$5.00"}, "fee_discount": {"feeDiscount": "-$5.00"},
    "price_discount": {"priceDiscount": "-$5.00"},
    "total_savings": {"totalSavings": "-$5.00"}, "total_savings_positive": {"total_savings": "$5.00"},
    "total_discount_amount": {"totalDiscountAmount": "-$5.00"},
    "discount_amount_total": {"discount_amount_total": "-$5.00"},
    "total_coupon_amount": {"totalCouponAmount": "$5.00"},
    "discount_with_percent": {"discount": {"amount": "-$5.00", "percent": 10}},
    "discount_with_percentage": {"discount": {"amount": "-$5.00", "percentage": "10%"}},
    "discount_with_rate": {"discount": {"amount": "-$5.00", "rate": 10}},
    "discount_rate": {"discount_rate": 0.1},
    "coupon_code_string": {"coupon": "SAVE10"}, "promo_code_string": {"promo": "SPRING25"},
    "voucher_code_string": {"voucher": "GIFT2024"}, "promotion_prose": {"promotion": "Buy 2 get 1 free"},
    "discount_code_string": {"discount": "SAVE10"}, "spaced_percent": {"discount": "10 % off"},
    "discount_reason": {"discounts": [{"amount": "-$5.00", "target": "line 2", "reason": "Spend 50 save 5"}]},
}
#: P2-C: a positive amount under a discount word outside an explicit slot is MONEY and is summed:
#: $45 + $10 (or $30 + $30) is past the ceiling.
CHARGES_UNDER_A_DISCOUNT_WORD = {
    "coupon_penalty": {"subtotal": "$45.00", "couponPenalty": "$10.00"},
    "voucher_handling": {"subtotal": "$45.00", "voucherHandling": "$10.00"},
    "rebate_clawback": {"subtotal": "$45.00", "rebateClawback": "$10.00"},
    "coupon_redemption": {"subtotal": "$45.00", "couponRedemption": "$10.00"},
    "promo_bundle": {"subtotal": "$45.00", "promoBundle": "$10.00"},
    "promo_item": {"subtotal": "$45.00", "promoItem": "$10.00"},
    "promotional_items": {"promotionalItems": ["$30.00", "$30.00"]},
    "promo_items_prices": {"promoItems": [{"price": "$30.00"}, {"price": "$30.00"}]},
    "positive_discount_word": {"subtotal": "$45.00", "discount": "$10.00"},
    "promo_lines_amounts": {"promo": {"lines": [{"amount": "$30.00"}, {"amount": "$30.00"}]}},
}


@pytest.mark.parametrize("name", sorted(ORDINARY_DISCOUNTS))
def test_an_ordinary_discount_shape_within_the_ceiling_confirms(name):
    assert worker.confirm_card_refused({**ORDINARY_DISCOUNTS[name], "total": "$40.00"}, TOTAL) is False


@pytest.mark.parametrize("name", sorted(CHARGES_UNDER_A_DISCOUNT_WORD))
def test_a_positive_amount_under_a_discount_word_is_money_and_is_summed(name):
    assert worker.confirm_card_refused(CHARGES_UNDER_A_DISCOUNT_WORD[name], TOTAL) is True


#: The words that, beside a discount word, make a key what is CHARGED: each key is money, so a negative
#: cancels, and a positive is summed. (Before v5 each was tested only over the ceiling.)
CHARGED_BESIDE_A_DISCOUNT_WORD = ["discountNet", "discountWithTax", "promoPay", "couponDue", "promoShipping"]


@pytest.mark.parametrize("key", CHARGED_BESIDE_A_DISCOUNT_WORD)
def test_a_charge_word_beside_a_discount_word_is_money_in_a_sum_and_for_its_sign(key):
    assert worker.confirm_card_refused({key: "-$5.00", "total": "$40.00"}, TOTAL) is True
    assert worker.confirm_card_refused({"subtotal": "$40.00", key: "$30.00"}, TOTAL) is True
    assert worker.confirm_card_refused({key: "$5.00", "total": "$40.00"}, TOTAL) is False


@pytest.mark.parametrize("card", [
    {"discount": -500}, {"discountAmount": -500, "total": "$40.00"}, {"discount": 5}, {"total_discounts": 999},
    {"discount": float("nan")}, {"discount": {"amount": -500}}, {"coupon": {"value": 5.0}},
], ids=["discount_int", "discount_amount_int", "discount_positive_int", "total_discounts_int", "nan",
        "slot_int", "slot_float"])
def test_a_bare_number_under_a_discount_key_cancels(card):
    assert worker.confirm_card_refused(card, TOTAL) is True


#: Quantity shapes (and the guards no test could fail before v5).
QUANTITY_SHAPES = {
    "qty_times_amount": {"items": [{"quantity": 2, "amount": "$30.00"}]},
    "pcs_key": {"items": [{"pcs": 2, "price": "$30.00"}]},
    "qty_in_a_list": {"items": [{"qty": [2], "price": "$30.00"}]},
    "quantity_object": {"items": [{"quantity": {"value": 2}, "price": "$30.00"}]},
    "two_quantity_keys_the_larger": {"items": [{"qty": 1, "quantity": 2, "price": "$30.00"}]},
    # The negative-quantity guard is the only one that catches this ($30 x -1 would lower the line sum).
    "negative_quantity": {"items": [{"qty": -1, "price": "$30.00"}, "$30.00", "$30.00"]},
    "negative_quantity_in_a_list": {"items": [{"qty": [-1], "price": "$30.00"}, "$30.00", "$30.00"]},
    # A negative quantity is unreadable whatever it does to the sum (here it cannot lower it).
    "negative_quantity_alone": {"items": [{"qty": -2, "price": "$1.00"}]},
}


@pytest.mark.parametrize("name", sorted(QUANTITY_SHAPES))
def test_every_quantity_shape_times_its_unit_price_is_checked(name):
    assert worker.confirm_card_refused(QUANTITY_SHAPES[name], TOTAL) is True


def test_a_quantity_shape_within_the_ceiling_confirms():
    assert worker.confirm_card_refused({"items": [{"quantity": {"value": 1}, "price": "$51.49"}]}, TOTAL) is False
    assert worker.confirm_card_refused({"items": [{"qty": [2], "price": "$25.00"}]}, TOTAL) is False


def test_an_upper_case_iso_code_in_a_plain_field_makes_an_amount():
    # M27: only this condition reads "40.00 EUR" in a plain field as an amount (and refuses it).
    assert worker.confirm_card_refused({"summary": "40.00 EUR"}, TOTAL) is True
    assert worker.confirm_card_refused({"summary": "Euro 24 Shop"}, TOTAL) is False


@pytest.mark.parametrize("prose", [
    "Your order total is 999.00", "You will pay 999.00", "Charged 999", "Grand total - 999.00", "Total:\n999.00",
])
def test_unmarked_prose_near_a_total_word_cancels(prose):
    from services.payment_orchestration import checkout_amounts as amounts

    assert amounts.text_refused(prose, TOTAL) is True
    assert worker.confirm_card_refused({"summary": prose}, TOTAL) is True


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
    relay = await _relay(_Provider(_token_result(), answer), setup=_setup(ceiling_minor=TOTAL))
    assert relay.checkout.text_amounts_refused is False


@pytest.mark.parametrize("order_id", [
    CRYPTO, CRYPTO.rstrip("="), f"ORD-{CRYPTO.rstrip('=')}", f"ORD-{DPAN}", f"#{DPAN}",
], ids=["cryptogram", "cryptogram_unpadded", "contains_cryptogram", "contains_dpan", "hash_dpan"])
async def test_an_order_id_that_is_or_holds_the_cryptogram_or_the_token_is_held_never_stored(order_id):
    # The worker refuses the shape itself (it never sees the token's values: only the relay held them).
    assert worker._card_shaped(order_id)
    pid = await _placing()
    outcome = await _step(Rail().on("checkout.await#poll", _echo_result(order_id=order_id)), pid)
    # The transport already strips a padded cryptogram and a card number from any answer (the order then
    # names no order id: held as such); what it lets through, the worker holds as card-shaped.
    expected = (worker.CHECKOUT_ORDER_ID_CARD_SHAPED if "=" not in order_id and DPAN not in order_id
                else worker.CHECKOUT_PAYMENT_NO_ORDER)
    assert outcome == expected
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["last_error_code"]) == ("paying", None, expected)
    assert expected in worker._CHECKOUT_PAYMENT_REACHED_CODES
    stored = await _stored_text()
    assert CRYPTO.rstrip("=") not in stored and DPAN not in stored


def test_an_ordinary_order_id_is_not_card_or_cryptogram_shaped():
    import uuid

    for order_id in ("1001", "ORD-12345", "112-1234567-1234567", str(uuid.uuid4()), uuid.uuid4().hex,
                     "pvt-ap_01234567.89abcdef.01234567", "SHOP-2026-ABCDEF"):
        assert not worker._card_shaped(order_id), order_id


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
