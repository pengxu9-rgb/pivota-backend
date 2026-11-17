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
    # `paid` holds its write-ahead across the relay; the confirm (a money write) has its own.
    assert paid["pending"] == setup["pending"] == "payment.visa-action-required#paid"
    assert confirm["pending"] == worker.CHECKOUT_CONFIRM_ACTION and confirm["payload"] == {}
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
    (_checkout_result(order_id=None, errors=["card declined"]), "failed", "checkout_payment_no_order"),
    (_checkout_result(total=None), "completed", "checkout_total_unparsed"),
    (_checkout_result(total="EUR 51,49"), "completed", "checkout_total_unparsed"),
    (_checkout_result("timeout", success=False, order_id=None), "paying", "checkout_unknown_timeout"),
    (_checkout_result("script-error", success=False, order_id=None), "paying", "checkout_unknown_script_error"),
    (_checkout_result(total="$51.50"), "paying", "checkout_total_over_approved"),
    (_checkout_result("payment-reached", success=False), "paying", "checkout_result_unrecognised"),
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
    (httpx.Response(502), "checkout_setup_unresolved"),
    (httpx.ConnectError("reset"), "checkout_setup_unresolved"),
    (httpx.Response(400), "checkout_setup_refused:http_error:400"),
    (_task(_card("error", {"code": "BAD_CARD", "recoverable": False})), "checkout_setup_refused:error_card:200"),
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


async def test_a_setup_marker_left_by_an_unseen_answer_fails_the_purchase_without_a_send():
    pid = await _approving()
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = :w, pending_write_at = CURRENT_TIMESTAMP, "
                           "last_error_code = 'paid_sent:1:await'", {"w": worker.CHECKOUT_SETUP_ACTION})
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["last_error_code"]) == ([], "failed", worker.CHECKOUT_SETUP_UNRESOLVED)


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
    (_text_only("I could not place that order."), "checkout_confirm_not_accepted"),
    (httpx.Response(400), "checkout_confirm_refused:http_error:400"),
])
async def test_a_confirm_that_is_not_accepted_fails_nothing_was_submitted(answer, code):
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=answer), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == ("failed", None, code)


async def test_a_throttled_confirm_is_confirmed_again_next_poll_and_then_not_after_the_window():
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=httpx.Response(429)), pid) == "rail_refused"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["checkout_task_id"]) == ("paying", None, None)
    rail = Rail().on("checkout.confirm#confirm", _checkout_await())
    assert await _step(rail, pid) == "placing"
    assert rail.actions() == ["checkout.confirm#confirm"]
    # Another purchase, set up but never confirmed within the window: cancelled at the provider, failed.
    await database.execute("DELETE FROM a2a_rail_purchases")
    pid = await _approving()
    assert await _step(_approved(Rail(), confirm_answer=httpx.Response(429)), pid) == "rail_refused"
    await _ago("state_entered_at", worker.CHECKOUT_CONFIRM_WINDOW_SECONDS + 5, pid)
    rail = Rail().on("checkout.confirm#cancel", _text_only("Cancelled."))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["checkout.confirm#cancel"]
    assert (await _row(pid))["last_error_code"] == worker.CHECKOUT_CONFIRM_WINDOW_PASSED


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
    for path in ("services/payment_orchestration/a2a_rail.py", "services/payment_orchestration/a2a_rail_worker.py",
                 "db/a2a_rail_ledger.py", "db/migrations/287_a2a_rail_checkout_task_id.sql", "tests/test_a2a_rail_checkout.py"):
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
