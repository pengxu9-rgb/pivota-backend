"""AgenPas asks the buyer for the shipping address the agent did not send (Peng's decision).

The door (POST /agent/v2/commerce/purchases, routes/agent_commerce_purchases), the gateway's write
(POST /agent/internal/commerce/purchases/{id}/shipping-address, routes/agent_internal_commerce_purchases) and,
end to end, the worker on a fake provider. On a MINIMAL app (never `main`: the Postgres gate shares one process,
and main's registrations poison the gate files after it); tests/test_agenpas_shipping_address_postgres.py runs the
same tests on Postgres. Stubbed as in tests/test_agent_commerce_purchase_create.py: the two auth dependencies, our
catalog read, the Tier B verdict, the click write and the Reap route. No route may reach the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import db.tierb_cart_link_eligibility as tierb_eligibility  # noqa: E402
import routes.agent_commerce_purchases as unified_routes  # noqa: E402
import routes.agent_commerce_reap as reap_routes  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402
from middleware.error_handler import ErrorHandlerMiddleware  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402
from services.payment_orchestration import a2a_rail, a2a_rail_worker as worker  # noqa: E402
from services.payment_orchestration import a2a_shipping_address as shipping_capture  # noqa: E402
from services.payment_orchestration import router as rail_router  # noqa: E402
import tests.test_a2a_rail_checkout as ck  # noqa: E402 - the fake provider and its cards (a module, not names)

app = FastAPI()
app.add_middleware(ErrorHandlerMiddleware)
app.include_router(unified_routes.router)
app.include_router(reap_routes.router)
app.include_router(internal_routes.router)

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# The SQLite arm; tests/test_agenpas_shipping_address_postgres.py runs these same tests on Postgres.
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

BASE = "/agent/v2/commerce/purchases"
INTERNAL = "/agent/internal/commerce/purchases"
KEY = "agenpas_read_test_key_0123456789abcdef"
AGENT = "agent_shipping_capture"
OTHER_AGENT = "agent_other"
USER_REF = "user-pat"
SHOP = "example.myshopify.com"
VARIANT = "49922977038613"
EMAIL = "pat@example.test"
#: Distinctive placeholder values, so the needle loops below can look for each one.
ADDRESS = {"firstName": "Patneedle", "lastName": "Examplenee", "phone": "+15125550199",
           "addressLine1": "77 Needle Lane", "addressLine2": "Unit 9Q", "city": "Needleton", "region": "TX",
           "postalCode": "78799", "country": "US"}
FACTS = {"shop_domain": SHOP, "our_price_minor": 4650, "currency": "USD", "market_country": "US",
         "product_name": "Placeholder Serum", "product_key": "pk_1", "variant_title": "30 ml"}
#: The values long enough to be told apart from other text (not "US" / "TX", which "USD" and others contain).
NEEDLES = tuple(v for v in (EMAIL, *ADDRESS.values()) if len(v) > 3)
_TABLES = ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs", "reap_agentic_purchase_keys",
           "reap_agentic_purchases", "reap_agentic_eligibility", "reap_agentic_buyer_refs",
           "a2a_rail_worker_heartbeat")


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    if not IS_POSTGRES:
        for table in _TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        await ensure_required_schema_light()
        await a2a_ledger.record_worker_heartbeat()
        yield
        return
    await ensure_required_schema_light()
    for table in _TABLES:
        await database.execute(f"DELETE FROM {table}")
    await a2a_ledger.record_worker_heartbeat()
    try:
        yield
    finally:
        await database.disconnect()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in ck.PLATFORM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv(rail_router.PAYMENT_ORCHESTRATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    monkeypatch.setenv("A2A_RAIL_ENABLED", "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_CREATE_ENABLED_ENV, "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_PILOT_AGENT_IDS_ENV, f"{AGENT},{OTHER_AGENT}")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:US")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000")
    monkeypatch.setenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, KEY)
    monkeypatch.setenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, "1")
    monkeypatch.setenv(a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV, "1")
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(ck.HOST.encode()).hexdigest()}))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _MockTransportsOnly(_REAL_ASYNC_CLIENT):
        """The fake provider's MockTransport and the app's ASGITransport only: anything else is the network."""

        def __init__(self, *a, transport=None, **k):
            if not isinstance(transport, (httpx.MockTransport, httpx.ASGITransport)):
                raise AssertionError("a route reached the network")
            super().__init__(*a, transport=transport, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _MockTransportsOnly, raising=True)


class Stubs:
    def __init__(self):
        self.reap = []
        self.clicks = []


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    s = Stubs()

    async def _catalog(*, merchant_domain, product_key, variant_key, market_country):
        return dict(FACTS), "merch_obs_shop_a", VARIANT, "brand_official"

    async def _click(click):
        s.clicks.append(click)

    async def _eligible(domain, market):
        return True

    async def _reap(request, context, agent_user):
        s.reap.append(json.loads(await request.body()))
        return JSONResponse(status_code=202, content={"handled_by": "reap"})

    monkeypatch.setattr(reap_routes, "_load_cart_link_item", _catalog)
    monkeypatch.setattr(unified_routes, "issue_click", _click)
    monkeypatch.setattr(tierb_eligibility, "is_cart_link_eligible", _eligible)
    monkeypatch.setattr(reap_routes, "start_reap_purchase", _reap)
    return s


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "Shipping capture test"
    allowed_merchants = None
    session_id = "session_shipping_capture"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


CALLER = _Caller()


@pytest.fixture(autouse=True)
def _auth():
    CALLER.agent_id, CALLER.agent_user_ref = AGENT, USER_REF

    async def _agent():
        return CALLER

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = lambda: AgentUserContext(agent_user_ref=CALLER.agent_user_ref)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


async def _http(method, url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
        return await http.request(method, url, **kwargs)


def _body(*, email: Any = EMAIL, address: Any = None, **over) -> Dict[str, Any]:
    buyer: Dict[str, Any] = {"consent_version": "terms-2026-10"}
    if email is not None:
        buyer["email"] = email
    if address is not None:
        buyer["shipping_address"] = address
    b = {"item_source": "cart_link", "merchant_domain": SHOP, "product_key": "pk_1", "quantity": 1, "buyer": buyer,
         "idempotency_key": "key-1", "expected_unit_price_minor": 4650, "expected_currency": "USD"}
    b.update(over)
    return b


def _headers(agent: Optional[str] = AGENT, key: Optional[str] = KEY, **extra: str) -> Dict[str, str]:
    headers = dict(extra)
    if key is not None:
        headers["X-Internal-Key"] = key
    if agent is not None:
        headers["X-Agenpas-Agent-Id"] = agent
    return headers


def _form(**over) -> Dict[str, Any]:
    return {"email": EMAIL, **ADDRESS, **over}


def _error(resp) -> Optional[str]:
    body = resp.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    return detail.get("error")


async def _create(**body_over) -> str:
    resp = await _http("POST", BASE, json=_body(**body_over))
    assert resp.status_code == 202, resp.text
    return resp.json()["purchase_id"]


async def _submit(pid, form=None, **kwargs):
    return await _http("POST", f"{INTERNAL}/{pid}/shipping-address", json=_form() if form is None else form,
                       headers=kwargs.pop("headers", _headers()), **kwargs)


# ── the door ─────────────────────────────────────────────────────────────────────────────────


async def test_with_capture_on_the_a2a_door_opens_a_purchase_without_email_or_address():
    pid = await _create(email=None)
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["state"], row["buyer_email"], row["shipping_address"]) == ("opened", None, None)
    got = await _http("GET", f"{BASE}/{pid}")
    assert got.status_code == 200, got.text
    assert got.json()["next_action"] == {"kind": "shipping_address",
                                         "fields_missing": shipping_capture.fields_missing(None, None)}
    assert got.json()["next_action"]["fields_missing"][0] == "email"


async def test_an_email_alone_is_kept_and_only_the_address_is_asked_for():
    pid = await _create()
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["buyer_email"], row["shipping_address"]) == (EMAIL, None)
    got = (await _http("GET", f"{BASE}/{pid}")).json()
    assert got["next_action"]["fields_missing"] == list(shipping_capture.REQUIRED_ADDRESS_FIELDS)


async def test_a_full_contact_at_create_is_stored_as_before_and_asks_for_nothing():
    pid = await _create(address=dict(ADDRESS))
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["buyer_email"], row["shipping_address"]) == (EMAIL, ADDRESS)
    assert "next_action" not in (await _http("GET", f"{BASE}/{pid}")).json()


@pytest.mark.parametrize("address", [
    {k: v for k, v in ADDRESS.items() if k != "region"},
    {k: v for k, v in ADDRESS.items() if k != "postalCode"},
    {**ADDRESS, "region": "PR"},           # a territory
    {**ADDRESS, "postalCode": "7879"},
], ids=["no_region", "no_postal_code", "territory", "bad_zip"])
async def test_a_given_us_address_needs_a_real_state_and_a_zip(address, monkeypatch):
    resp = await _http("POST", BASE, json=_body(address=address))
    assert resp.status_code == 400 and "invalid_address" in resp.text
    for value in ADDRESS.values():
        assert value not in resp.text
    assert (await database.fetch_one("SELECT COUNT(*) AS n FROM a2a_rail_purchases"))["n"] == 0


@pytest.mark.parametrize("missing", ["email", "address", "both"])
async def test_with_capture_off_the_door_refuses_a_missing_contact_exactly_as_before(monkeypatch, missing):
    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    body = _body(email=None if missing in ("email", "both") else EMAIL,
                 address=None if missing in ("address", "both") else dict(ADDRESS))
    resp = await _http("POST", BASE, json=body)
    malformed = await _http("POST", BASE, json={**body, "quantity": "not a number"})
    assert resp.status_code == malformed.status_code == 400
    strip = lambda r: {k: v for k, v in r.json().items() if k != "metadata"}  # noqa: E731
    assert strip(resp) == strip(malformed)  # the model's own refusal, byte for byte
    # A region-less US address still opens, as before capture existed.
    no_region = {k: v for k, v in ADDRESS.items() if k != "region"}
    assert (await _http("POST", BASE, json=_body(address=no_region, idempotency_key="k-2"))).status_code == 202


async def test_reap_is_unchanged_a_request_it_takes_is_handed_over_whole_and_its_model_still_needs_the_contact(stubs):
    body = _body(email=None, payment_preference={"rail": "reap"})
    resp = await _http("POST", BASE, json=body)
    assert resp.json() == {"handled_by": "reap"}
    assert stubs.reap == [body]  # byte for byte what the caller sent
    with pytest.raises(Exception):
        reap_routes.StartPurchaseRequest.model_validate(body)  # the Reap route's own model refuses it
    assert (await database.fetch_one("SELECT COUNT(*) AS n FROM a2a_rail_purchases"))["n"] == 0


# ── the internal write ───────────────────────────────────────────────────────────────────────


async def test_the_submitted_contact_is_stored_and_the_answer_is_the_unified_view():
    pid = await _create(email=None)
    resp = await _submit(pid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["rail"], body["rail_purchase_id"], body["state"]) == ("a2a", pid, "routing")
    assert "next_action" not in body
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["buyer_email"], row["shipping_address"]) == (EMAIL, ADDRESS)
    assert row["last_error_code"] is None
    text = json.dumps(body)
    for needle in NEEDLES:
        assert needle not in text, needle
    # The same contact again: 200. A different one: 409, and the stored one stays.
    again = await _submit(pid)
    assert again.status_code == 200 and again.json()["rail_purchase_id"] == pid
    other = await _submit(pid, _form(addressLine1="2 Other St"))
    assert (other.status_code, _error(other)) == (409, "shipping_address_conflict")
    other_email = await _submit(pid, _form(email="someone@example.test"))
    assert other_email.status_code == 409
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] == ADDRESS


async def test_the_email_may_be_left_out_when_the_row_has_it_and_is_required_when_it_does_not():
    with_email = await _create()
    form = {k: v for k, v in _form().items() if k != "email"}
    assert (await _submit(with_email, form)).status_code == 200
    without = await _create(email=None, idempotency_key="key-2")
    resp = await _submit(without, form)
    assert resp.status_code == 422 and resp.json()["field_errors"] == {"email": "required"}


async def test_two_submits_at_once_one_wins_and_the_other_is_a_conflict():
    pid = await _create(email=None)
    a, b = await asyncio.gather(_submit(pid, _form(addressLine1="1 First St")),
                                _submit(pid, _form(addressLine1="2 Second St")))
    assert sorted([a.status_code, b.status_code]) == [200, 409]


@pytest.mark.parametrize("form,errors", [
    ({"email": EMAIL}, {f: "required" for f in shipping_capture.REQUIRED_ADDRESS_FIELDS}),
    (_form(email="not-an-email"), {"email": "invalid"}),
    (_form(country="CA"), {"country": "unsupported"}),
    (_form(region="PR"), {"region": "invalid"}),
    (_form(region="ZZ"), {"region": "invalid"}),
    (_form(postalCode="787"), {"postalCode": "invalid"}),
    (_form(phone="call me"), {"phone": "invalid"}),
    (_form(addressLine1="x" * 201), {"addressLine1": "too_long"}),
    (_form(city=12), {"city": "invalid"}),
    (_form(firstName="Pat\u2028Example"), {"firstName": "invalid"}),  # unprintable after the door's clean
    (_form(lastName="   "), {"lastName": "required"}),
], ids=["address_missing", "email", "country", "territory", "region", "zip", "phone", "too_long", "not_a_string",
        "control_char", "blank"])
async def test_an_invalid_submission_is_a_422_naming_fields_and_codes_never_values(form, errors):
    pid = await _create(email=None)
    resp = await _submit(pid, form)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"status": "error", "error": "invalid_shipping_address", "field_errors": errors}
    for value in form.values():
        if isinstance(value, str) and len(value) > 2:
            assert value not in resp.text
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] is None


async def test_the_normalised_form_reads_upper_case_region_and_country():
    pid = await _create(email=None)
    assert (await _submit(pid, _form(region="tx", country="us", postalCode="78799-1234"))).status_code == 200
    stored = (await a2a_ledger.get_purchase_internal(pid))["shipping_address"]
    assert (stored["region"], stored["country"], stored["postalCode"]) == ("TX", "US", "78799-1234")


async def test_another_agents_purchase_and_no_purchase_are_the_same_404():
    pid = await _create(email=None)
    theirs = await _submit(pid, headers=_headers(agent=OTHER_AGENT))
    missing = await _submit("ap_" + "0" * 24)
    assert [(r.status_code, _error(r)) for r in (theirs, missing)] == [(404, "purchase_not_found")] * 2
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] is None


async def test_the_gateway_user_ref_is_read_and_ignored():
    pid = await _create(email=None)
    resp = await _submit(pid, headers=_headers(**{"X-Agenpas-Agent-User-Ref": "usr_someone_else"}))
    assert resp.status_code == 200


@pytest.mark.parametrize("key,status", [(None, 401), ("wrong", 401)])
async def test_the_write_needs_the_dedicated_key(key, status):
    pid = await _create(email=None)
    resp = await _submit(pid, headers=_headers(key=key))
    assert resp.status_code == status
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] is None


async def test_a_server_without_the_key_and_a_bad_agent_id_are_refused(monkeypatch):
    pid = await _create(email=None)
    assert (await _submit(pid, headers=_headers(agent="has spaces"))).status_code == 400
    monkeypatch.delenv(internal_routes.INTERNAL_KEY_ENV)
    assert (await _submit(pid)).status_code == 503


async def test_with_the_dial_off_the_write_is_the_unknown_route_404(monkeypatch):
    pid = await _create(email=None)
    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    resp = await _submit(pid)
    unknown = await _http("POST", f"{INTERNAL}/{pid}/no-such-path", json=_form(), headers=_headers())
    assert resp.status_code == unknown.status_code == 404
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] is None


async def test_a_reap_purchase_does_not_wait_for_an_address():
    purchase = await reap_ledger.create_purchase(
        buyer_ref="bref_pat", agent_id=AGENT, agent_user_ref_hash=hash_agent_user_ref(USER_REF),
        merchant_domain="merchant.example", product_key="pk_1", product_name="Placeholder", quantity=1,
        currency="USD", our_price_minor=4650, buyer_email=EMAIL, shipping_address={"addressLine1": "1 Main St"})
    await purchases.ensure_reap_parent(purchase["id"], basis="backfill")
    resp = await _submit(purchase["id"])
    assert (resp.status_code, _error(resp)) == (409, "shipping_address_conflict")


async def test_a_ledger_fault_is_a_503_never_a_500(monkeypatch):
    pid = await _create(email=None)

    async def _broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(a2a_ledger, "submit_shipping_address", _broken)
    resp = await _submit(pid)
    assert (resp.status_code, _error(resp)) == (503, "temporarily_unavailable")


#: The database drivers' own DEBUG query log carries every bound value (an address included); production runs
#: the root logger at WARNING, so it is never emitted there. Every other logger is checked.
_DRIVER_LOGGERS = ("databases", "aiosqlite", "asyncpg", "sqlalchemy")


def _app_log(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(_DRIVER_LOGGERS))


async def test_no_address_field_reaches_a_log_or_an_answer(caplog):
    with caplog.at_level(logging.DEBUG):
        pid = await _create(email=None)
        await _submit(pid, _form(region="PR"))         # a 422
        await _submit(pid)                             # a 200
        await _submit(pid, _form(city="Elsewhere"))    # a 409
        await _http("GET", f"{BASE}/{pid}")
    logged = _app_log(caplog)
    for needle in NEEDLES:
        assert needle not in logged, needle


# ── end to end: create without an address, the buyer submits it, card, approval, checkout ──────


async def _step(rail, pid):
    await ck._ago("next_poll_at", 1, pid)
    rows = [r for r in await a2a_ledger.claim_due_purchases(ck.W, limit=50) if r["id"] == pid]
    assert rows, "the purchase is not due"
    return await worker.step(ck._client(rail), rows[0], ck.W)


async def test_the_whole_path_create_without_an_address_then_submit_then_card_approval_and_checkout(caplog):
    with caplog.at_level(logging.DEBUG):
        pid = await _create(email=None)
        # Waiting: nothing is sent.
        rail = ck.Rail()
        assert await _step(rail, pid) == worker.SHIPPING_ADDRESS_REQUIRED and rail.sent == []
        view = (await _http("GET", f"{BASE}/{pid}")).json()
        assert view["next_action"]["kind"] == "shipping_address" and "expires_at" in view["next_action"]
        # The buyer submits the address on the gateway's form.
        assert (await _submit(pid)).status_code == 200
        # The card (already on file), the lock, the token, the approval page.
        rail = ck.Rail().on("payment.method-view#submit", ck._methods(ck.VISA))
        assert await _step(rail, pid) == "method_ready"
        rail = ck.Rail().on("payment.method-view#submit", ck._methods(ck.VISA)).on(
            "payment.token-setup#submit", ck._visa_page())
        outcome = await _step(rail, pid)
        if outcome != "approval_page_issued":  # the lock is its own step
            outcome = await _step(rail, pid)
        assert outcome == "approval_page_issued"
        await ck._ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, pid)
        # Approved: the token is relayed into the checkout with the SUBMITTED address, and confirmed.
        rail = ck._approved(ck.Rail())
        assert await _step(rail, pid) == "placing"
        shipping = rail.payload("checkout.setup#submit")["shipping"]
        assert (shipping["email"], shipping["address1"], shipping["state"], shipping["zip_code"]) == (
            EMAIL, ADDRESS["addressLine1"], "TX", ADDRESS["postalCode"])
        assert await _step(ck.Rail().on("checkout.await#poll", ck._checkout_result()), pid) == "completed"
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["state"], row["buyer_email"], row["shipping_address"]) == ("completed", None, None)
    logged = _app_log(caplog)
    for needle in NEEDLES:
        assert needle not in logged, needle
