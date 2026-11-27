"""AgenPas asks the buyer for the shipping address the agent did not send (the owner's decision).

The door (POST /agent/v2/commerce/purchases, routes/agent_commerce_purchases), the gateway's write
(POST /agent/internal/commerce/purchases/{id}/shipping-address, routes/agent_internal_commerce_purchases) and,
end to end, the worker on a fake provider. On a MINIMAL app (never `main`: the Postgres gate shares one process,
and main's registrations poison the gate files after it); tests/test_agenpas_shipping_address_postgres.py runs the
same tests on Postgres. Stubbed as in tests/test_agent_commerce_purchase_create.py: the two auth dependencies, our
catalog read, the Tier B verdict, the click write and the Reap route. No route may reach the network.
"""

from __future__ import annotations

import asyncio
import contextvars
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
import db.agent_purchase_consents as consents_db  # noqa: E402
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
from services.payment_orchestration import purchase_consent  # noqa: E402
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
#: Everything capture needs besides the read dial and the two steps (a2a_shipping_address.capture_enabled): its own
#: dial, the consent gate, and a consent that can be given (configuration, the write's dial and key). Test values.
CAPTURE_ENV = {
    "A2A_SHIPPING_CAPTURE_ENABLED": "1", "AGENPAS_CONSENT_GATE": "1", "AGENPAS_CONSENT_WRITE_ENABLED": "1",
    "AGENPAS_TERMS_URL": "https://legal.example/terms", "AGENPAS_TERMS_VERSION": "terms-test",
    "AGENPAS_PRIVACY_URL": "https://legal.example/privacy", "AGENPAS_PRIVACY_VERSION": "privacy-test",
    "AGENPAS_CONSENT_OFFER_SECRET": "offer_secret_for_capture_tests_0123456789",
    "AGENPAS_INTERNAL_CONSENT_KEY": "agenpas_consent_key_for_capture_tests_0123",
}
_TABLES = ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs", "reap_agentic_purchase_keys",
           "reap_agentic_purchases", "reap_agentic_eligibility", "reap_agentic_buyer_refs",
           "a2a_rail_worker_heartbeat", "a2a_rail_contact_purge_heartbeat")


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    if not IS_POSTGRES:
        for table in _TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        await ensure_required_schema_light()
        await a2a_ledger.record_worker_heartbeat()
        await a2a_ledger.record_contact_purge_heartbeat()
        yield
        return
    await ensure_required_schema_light()
    for table in _TABLES:
        await database.execute(f"DELETE FROM {table}")
    await a2a_ledger.record_worker_heartbeat()
    await a2a_ledger.record_contact_purge_heartbeat()
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
    # Capture needs its own dial, the AgenPas consent gate and a consent that can be given (CAPTURE_ENV): on here;
    # the tests below record the buyer's shipping-address consent where they show or submit the form (`_create`).
    for name, value in CAPTURE_ENV.items():
        monkeypatch.setenv(name, value)
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
        self.catalog_currency = []  # the `market_currency` the A2A route priced the cart link in


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    s = Stubs()

    async def _catalog(*, merchant_domain, product_key, variant_key, market_country, market_currency=None):
        s.catalog_currency.append(market_currency)
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


async def _consent(pid: str, step: str = "shipping_address", epoch: int = 0, agent: str = AGENT) -> None:
    """Record the buyer's consent for `step`, as POST .../consents does once the buyer accepted (placeholders)."""
    from datetime import datetime, timezone

    await consents_db.record(rail="a2a", rail_purchase_id=pid, agent_id=agent, step=step, approval_epoch=epoch,
                             terms_version="terms-test", privacy_version="privacy-test",
                             terms_url="https://legal.example/terms", privacy_url="https://legal.example/privacy",
                             offer_issued_at=datetime.now(timezone.utc))


async def _create(consented: bool = True, **body_over) -> str:
    """Open a purchase through the door; by default with its shipping-address consent already recorded."""
    resp = await _http("POST", BASE, json=_body(**body_over))
    assert resp.status_code == 202, resp.text
    pid = resp.json()["purchase_id"]
    if consented:
        await _consent(pid)
    return pid


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
    {**ADDRESS, "region": "PR"},           # a territory: a state given is checked
    {**ADDRESS, "postalCode": "7879"},     # a zip given is checked
], ids=["territory", "bad_zip"])
async def test_a_given_us_state_and_zip_are_checked_when_given(address, monkeypatch):
    resp = await _http("POST", BASE, json=_body(address=address))
    assert resp.status_code == 400 and "invalid_address" in resp.text
    for value in ADDRESS.values():
        assert value not in resp.text
    assert (await database.fetch_one("SELECT COUNT(*) AS n FROM a2a_rail_purchases"))["n"] == 0


async def test_a_us_address_with_no_state_or_zip_is_taken_the_store_platform_asks_if_it_needs_them():
    no_zip = {k: v for k, v in ADDRESS.items() if k not in ("postalCode", "region")}
    resp = await _http("POST", BASE, json=_body(address=no_zip))
    assert resp.status_code == 202, resp.text
    row = await a2a_ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert row["shipping_address"] == no_zip


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


PARTIAL = {"addressLine1": "1 Partial Way", "city": "Placeholder City"}


async def _store_partial(pid: str, partial: Dict[str, Any] = PARTIAL) -> None:
    # An address that lacks required fields, encoded as the door stores one (sorted keys).
    await database.execute("UPDATE a2a_rail_purchases SET shipping_address = :a WHERE id = :i",
                           {"a": json.dumps(partial, sort_keys=True), "i": pid})


async def test_an_incomplete_stored_address_counts_as_absent_and_the_whole_address_replaces_it():
    # THE waiting rule (a2a_shipping_address.awaits_contact) calls it missing, so the write does too: the form
    # asks for the whole address, and the buyer's replaces the incomplete one (no more 409 for a step the read
    # offered and the consent POST took).
    pid = await _create()
    await _store_partial(pid)
    form = {k: v for k, v in _form().items() if k != "email"}
    short = await _submit(pid, {k: form[k] for k in PARTIAL})
    assert short.status_code == 422
    assert set(short.json()["field_errors"]) == set(shipping_capture.REQUIRED_ADDRESS_FIELDS) - set(PARTIAL)
    # Nothing sent for the address: required whole, never "already there" (an empty form is not a 200).
    empty = await _submit(pid, {})
    assert empty.status_code == 422
    assert set(empty.json()["field_errors"]) == set(shipping_capture.REQUIRED_ADDRESS_FIELDS)
    resp = await _submit(pid, form)
    assert resp.status_code == 200, resp.text
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["buyer_email"], row["shipping_address"], row["last_error_code"]) == (EMAIL, ADDRESS, None)
    # Now complete: a different address is a conflict again, and the stored one stays.
    other = await _submit(pid, {**form, "addressLine1": "2 Other St"})
    assert (other.status_code, _error(other)) == (409, "shipping_address_conflict")
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] == ADDRESS


async def test_the_ledger_replaces_only_exactly_the_incomplete_address_it_was_given():
    pid = await _create()
    await _store_partial(pid)
    owner = (await a2a_ledger.get_purchase_internal(pid))["agent_user_ref_hash"]
    # Another incomplete value than the one stored (the row moved on since it was read): nothing written.
    stale = {**PARTIAL, "city": "Elsewhere"}
    assert await a2a_ledger.submit_shipping_address(
        pid, AGENT, owner, buyer_email=None, shipping_address=ADDRESS, replaces_address=stale) is None
    # An incomplete address replaced by nothing: the email alone does not end the wait.
    assert await a2a_ledger.submit_shipping_address(
        pid, AGENT, owner, buyer_email=EMAIL, shipping_address=None, replaces_address=PARTIAL) is None
    # Without the fence the stored (non-null) address is never overwritten, as before.
    assert await a2a_ledger.submit_shipping_address(
        pid, AGENT, owner, buyer_email=None, shipping_address=ADDRESS) is None
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] == PARTIAL
    written = await a2a_ledger.submit_shipping_address(
        pid, AGENT, owner, buyer_email=None, shipping_address=ADDRESS, replaces_address=PARTIAL)
    assert written is not None and written["shipping_address"] == ADDRESS
    # The route passes only what a2a_shipping_address.incomplete_address returns: never a complete address.
    assert shipping_capture.incomplete_address(ADDRESS) is None
    assert shipping_capture.incomplete_address(json.dumps(PARTIAL)) == PARTIAL
    assert shipping_capture.incomplete_address(None) is None and shipping_capture.incomplete_address({}) is None


async def test_the_email_may_be_left_out_when_the_row_has_it_and_is_required_when_it_does_not():
    with_email = await _create()
    form = {k: v for k, v in _form().items() if k != "email"}
    assert (await _submit(with_email, form)).status_code == 200
    without = await _create(email=None, idempotency_key="key-2")
    resp = await _submit(without, form)
    assert resp.status_code == 422 and resp.json()["field_errors"] == {"email": "required"}


async def test_two_submits_at_once_one_wins_and_the_other_is_a_conflict():
    pid = await _create(email=None)
    # As a server runs two requests: each in its own task with a FRESH context, so each gets its own database
    # connection (`databases` keeps the connection in a ContextVar; a plain gather would hand both tasks this
    # test's connection, and their transactions would interleave on it, which no server request ever does).
    a, b = await asyncio.gather(
        asyncio.create_task(_submit(pid, _form(addressLine1="1 First St")), context=contextvars.Context()),
        asyncio.create_task(_submit(pid, _form(addressLine1="2 Second St")), context=contextvars.Context()))
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



# ── review of 192b9e079: one capture predicate; the market; the park race; the second-line guards ──

_DIALS = {
    "read": internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV,
    "checkout": a2a_rail.A2A_RAIL_CHECKOUT_STEP_ENABLED_ENV,
    "token": worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV,
}
_COMBINATIONS = [(r, c, t) for r in (0, 1) for c in (0, 1) for t in (0, 1)]


def _set_dials(monkeypatch, read, checkout, token):
    for name, on in zip(_DIALS.values(), (read, checkout, token)):
        if on:
            monkeypatch.setenv(name, "1")
        else:
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("read,checkout,token", _COMBINATIONS, ids=[f"read{r}_checkout{c}_token{t}" for r, c, t in _COMBINATIONS])
async def test_the_door_the_view_and_the_worker_read_one_capture_predicate(monkeypatch, read, checkout, token):
    # P2-1: a purchase may open without its contact ONLY when the worker will wait for it, and only then is the
    # buyer asked for it.
    _set_dials(monkeypatch, read, checkout, token)
    capture = bool(read and checkout and token)
    assert shipping_capture.capture_enabled() is capture
    resp = await _http("POST", BASE, json=_body(email=None))
    if not capture:
        assert (resp.status_code, _error(resp)) == (400, "invalid_request")
        return
    assert resp.status_code == 202, resp.text
    pid = resp.json()["purchase_id"]
    assert (await _http("GET", f"{BASE}/{pid}")).json()["next_action"]["kind"] == "shipping_address"
    rail = ck.Rail()
    assert await _step(rail, pid) == worker.SHIPPING_ADDRESS_REQUIRED and rail.sent == []


async def test_the_reviewers_case_read_dial_on_checkout_off_shows_no_form_and_the_door_needs_the_contact(monkeypatch):
    _set_dials(monkeypatch, read=1, checkout=0, token=1)
    resp = await _http("POST", BASE, json=_body(email=None))
    assert (resp.status_code, _error(resp)) == (400, "invalid_request")  # never a purchase the worker will not wait for
    # A row left without its contact (opened while capture was on) is not asked for and does not wait.
    _set_dials(monkeypatch, read=1, checkout=1, token=1)
    pid = await _create(email=None, idempotency_key="k-left")
    for dials in ({"read": 1, "checkout": 0, "token": 1}, {"read": 0, "checkout": 1, "token": 1}):
        _set_dials(monkeypatch, **dials)
        assert (await _http("GET", f"{BASE}/{pid}")).json().get("next_action") is None, dials
        assert worker._awaits_contact(await a2a_ledger.get_purchase_internal(pid)) is False, dials


@pytest.mark.parametrize("missing", ["email", "address", "both"])
async def test_with_only_the_read_dial_on_the_door_answers_exactly_as_with_every_dial_off(monkeypatch, missing):
    # Merging changes nothing while only the read dial is on (the gateway's read may be on before the checkout).
    body = _body(email=None if missing in ("email", "both") else EMAIL,
                 address=None if missing in ("address", "both") else dict(ADDRESS))
    strip = lambda r: (r.status_code, {k: v for k, v in r.json().items() if k != "metadata"})  # noqa: E731
    _set_dials(monkeypatch, read=1, checkout=0, token=0)
    read_only = await _http("POST", BASE, json=body)
    _set_dials(monkeypatch, read=0, checkout=0, token=0)
    all_off = await _http("POST", BASE, json=body)
    assert strip(read_only) == strip(all_off) and read_only.status_code == 400


@pytest.mark.parametrize("country", ["CA", "GB"])
async def test_the_a2a_door_refuses_a_non_us_market_by_name(monkeypatch, country):
    # P3-1: in code, not only by the router's lists: here the router itself picks the A2A rail.
    monkeypatch.setattr(rail_router, "choose_rail",
                        lambda **kw: rail_router.RailChoice(rail="a2a", basis="buyer_selected_rail"))
    currency = reap_routes._MARKET_CURRENCY[country]
    address = {**ADDRESS, "country": country, "region": "ON" if country == "CA" else "LND", "postalCode": "M5V 2T6"}
    by_address = await _http("POST", BASE, json=_body(address=address, expected_currency=currency,
                                                      payment_preference={"rail": "a2a"}))
    by_currency = await _http("POST", BASE, json=_body(email=None, expected_currency=currency, idempotency_key="k-c",
                                                       payment_preference={"rail": "a2a"}))
    for resp in (by_address, by_currency):
        assert (resp.status_code, _error(resp)) == (400, shipping_capture.SHIPPING_COUNTRY_UNSUPPORTED), resp.text


@pytest.mark.parametrize("currency", ["omitted", "XYZ"])
async def test_with_no_address_and_no_currency_naming_one_market_the_door_refuses_by_name(currency):
    # P3-1: never a default market.
    body = _body(email=None, expected_currency=currency)
    if currency == "omitted":
        body.pop("expected_currency"), body.pop("expected_unit_price_minor")
    resp = await _http("POST", BASE, json=body)
    assert (resp.status_code, _error(resp)) == (400, shipping_capture.SHIPPING_MARKET_UNKNOWN)
    assert shipping_capture.market_for_currency("USD", reap_routes._MARKET_CURRENCY) == "US"
    assert shipping_capture.market_for_currency("USD", {"US": "USD", "EC": "USD"}) is None  # two markets: none


async def test_a_park_racing_the_buyers_submit_clears_the_wait_and_the_sweep_never_ends_the_purchase():
    # P3-2: the worker holds a stale row (no contact) while the buyer's submit lands; its park must not re-arm.
    pid = await _create(email=None)
    assert await _step(ck.Rail(), pid) == worker.SHIPPING_ADDRESS_REQUIRED  # the first park sets the deadline
    await ck._ago("next_poll_at", 1, pid)
    stale = [r for r in await a2a_ledger.claim_due_purchases(ck.W, limit=50) if r["id"] == pid][0]
    assert (await _submit(pid)).status_code == 200  # lands while the worker holds the row
    await worker.step(ck._client(ck.Rail()), stale, ck.W)  # parks from its stale read
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["state"], row["last_error_code"], row["buyer_action_expires_at"]) == ("opened", None, None)
    assert row["buyer_email"] == EMAIL and row["shipping_address"] is not None
    await a2a_ledger.expire_overdue_purchases()
    assert (await a2a_ledger.get_purchase_internal(pid))["state"] == "opened"


async def test_the_park_write_itself_needs_opened_and_no_pending_write():
    # P3-5 (S1, S12): second-line guards behind `_opened`, pinned on the ledger write.
    pid = await _create(email=None)
    await ck._ago("next_poll_at", 1, pid)
    assert [r for r in await a2a_ledger.claim_due_purchases(ck.W, limit=50) if r["id"] == pid]
    from datetime import datetime, timezone

    later = datetime(2099, 1, 1, tzinfo=timezone.utc)
    when = {"expires_at": later, "next_poll_at": later}
    await database.execute("UPDATE a2a_rail_purchases SET state = 'quoting' WHERE id = :i", {"i": pid})
    assert await a2a_ledger.park_for_shipping_address_as_holder(pid, ck.W, **when) is None
    await database.execute("UPDATE a2a_rail_purchases SET state = 'opened', pending_write = 'payment.method-add#submit' "
                           "WHERE id = :i", {"i": pid})
    assert await a2a_ledger.park_for_shipping_address_as_holder(pid, ck.W, **when) is None
    row = await a2a_ledger.get_purchase_internal(pid)
    assert (row["last_error_code"], row["buyer_action_expires_at"]) == (None, None)
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = NULL WHERE id = :i", {"i": pid})
    assert await a2a_ledger.park_for_shipping_address_as_holder(pid, ck.W, **when) is not None


async def test_the_gateway_sees_these_exact_409_and_422_bodies():
    # P3-3, for the gateway: the 422 is flat; the 409 is re-wrapped by ErrorHandlerMiddleware (read detail.error).
    pid = await _create(email=None)
    assert (await _submit(pid, _form(addressLine1="1 First St"))).status_code == 200
    conflict = await _submit(pid, _form(addressLine1="2 Second St"))
    body = {k: v for k, v in conflict.json().items() if k != "metadata"}
    assert conflict.status_code == 409
    assert body == {"status": "error", "detail": {"error": "shipping_address_conflict"},
                    "error": {"code": "CONFLICT", "message": "shipping_address_conflict",
                              "details": {"error": "shipping_address_conflict"},
                              "documentation_url": "https://api.pivota.cc/agent/docs/overview"}}
    pid2 = await _create(email=None, idempotency_key="k-422")
    invalid = await _submit(pid2, _form(postalCode="1"))
    assert (invalid.status_code, invalid.json()) == (
        422, {"status": "error", "error": "invalid_shipping_address", "field_errors": {"postalCode": "invalid"}})


async def test_an_address_line_2_equal_to_the_davv_cancels_before_the_confirm_failing_closed():
    # P3-6, pinned: a bare 3-digit addressLine2 that is exactly our DAVV is scrubbed as the DAVV echoed back (the
    # whole-value rule), so the setup answer is not confirmed. About 1 in 1000 bare 3-digit line 2s; it fails
    # closed (cancelled before anything is submitted). No behaviour change.
    shipping = {"email": EMAIL, "first_name": "Pat", "last_name": "Example", "phone": "+15125550199", "country": "US",
                "address1": "77 Needle Lane", "address2": ck.DAVV, "city": "Needleton", "state": "TX",
                "zip_code": "78799"}
    answer = ck._task(ck._card("checkout.confirm", {"merchant": "merchant.example", "shipping": shipping},
                               ("confirm",), kind="confirm"))
    relay = await ck._relay(ck._Provider(ck._token_result(), answer), setup=ck._setup(ceiling_minor=100000))
    assert relay.checkout.credential_fields_stripped >= 1


# ── markets other than the US (services/payment_orchestration/a2a_markets.py) ─────────────────────────────


JP_ADDRESS = {"firstName": "Hananeedle", "lastName": "Examplenee", "phone": "+81312345678",
              "addressLine1": "1-1 Needle-cho", "city": "Needleda", "region": "13", "postalCode": "100-0001",
              "country": "JP"}


async def test_a_japanese_address_shown_in_dollars_never_takes_the_rail(monkeypatch):
    # The merchant is listed for JP and USD is on, but USD is not Japan's currency: the router refuses the pairing
    # (`currency_market_mismatch`), so an explicit A2A choice is the usual 404 and nothing is opened.
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:JP")
    resp = await _http("POST", BASE, json=_body(address=JP_ADDRESS, payment_preference={"rail": "a2a"}))
    assert resp.status_code == 404 and _error(resp) == "not_available_on_this_rail", resp.text
    assert await database.fetch_all("SELECT id FROM a2a_rail_purchases") == []  # nothing was opened
    for value in JP_ADDRESS.values():
        if len(value) > 3:
            assert value not in resp.text


async def test_the_door_refuses_a_market_that_is_not_on_if_the_router_ever_lets_it_through(monkeypatch):
    """Belt and braces: the door's own market check (`a2a_markets.market_enabled`) stands even if the router
    were to take a market that is not on."""
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:JP")
    monkeypatch.setattr(rail_router, "a2a_unavailable_reason", lambda **kwargs: None)
    resp = await _http("POST", BASE, json=_body(address=JP_ADDRESS))
    assert resp.status_code == 400 and _error(resp) == "shipping_country_unsupported", resp.text
    assert await database.fetch_all("SELECT id FROM a2a_rail_purchases") == []


async def test_with_its_market_on_the_door_opens_a_yen_purchase_with_a_japanese_address(monkeypatch, stubs):
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,JP")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:JP")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,JPY:30000")
    monkeypatch.setitem(FACTS, "currency", "JPY")
    monkeypatch.setitem(FACTS, "market_country", "JP")
    resp = await _http("POST", BASE, json=_body(address=JP_ADDRESS, expected_currency="JPY"))
    assert resp.status_code == 202, resp.text
    row = await a2a_ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert (row["currency"], row["our_price_minor"]) == ("JPY", 4650)
    assert row["shipping_address"]["region"] == "13" and row["shipping_address"]["country"] == "JP"
    assert stubs.catalog_currency == ["JPY"]  # the cart link is priced in the market's own currency
    bad = await _http("POST", BASE, json=_body(address={**JP_ADDRESS, "region": "48"}, expected_currency="JPY",
                                               idempotency_key="key-2"))
    assert bad.status_code == 400 and _error(bad) == "invalid_address", bad.text


CA_FORM_ADDRESS = {"firstName": "Patneedle", "lastName": "Examplenee", "phone": "+16135550199",
                   "addressLine1": "77 Needle Lane", "city": "Needleton", "region": "ON", "postalCode": "K1A 0B1",
                   "country": "CA"}


def _ca_on(monkeypatch):
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,CA")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:CA")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,CAD:50000")
    monkeypatch.setitem(FACTS, "currency", "CAD")
    monkeypatch.setitem(FACTS, "market_country", "CA")


async def test_the_address_write_refuses_another_markets_address_for_a_cad_purchase(monkeypatch):
    _ca_on(monkeypatch)
    pid = await _create(email=None, expected_currency="CAD")  # no address: the market is CAD's, Canada
    resp = await _submit(pid, _form())  # a US (TX) address
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"status": "error", "error": "invalid_shipping_address",
                           "field_errors": {"country": "unsupported"}}
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"] is None
    ok = await _submit(pid, {"email": EMAIL, **CA_FORM_ADDRESS})
    assert ok.status_code in (200, 204), ok.text
    assert (await a2a_ledger.get_purchase_internal(pid))["shipping_address"]["country"] == "CA"


async def test_with_capture_off_a_non_us_address_still_meets_its_markets_rules(monkeypatch):
    monkeypatch.delenv("A2A_SHIPPING_CAPTURE_ENABLED", raising=False)
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,JP")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:JP")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,JPY:30000")
    monkeypatch.setitem(FACTS, "currency", "JPY")
    monkeypatch.setitem(FACTS, "market_country", "JP")
    bad = await _http("POST", BASE, json=_body(address={**JP_ADDRESS, "region": "48"}, expected_currency="JPY"))
    assert bad.status_code == 400 and _error(bad) == "invalid_address", bad.text
    good = await _http("POST", BASE, json=_body(address=JP_ADDRESS, expected_currency="JPY", idempotency_key="key-2"))
    assert good.status_code == 202, good.text


async def test_with_canada_on_a_canadian_address_shown_in_dollars_is_refused_by_the_router(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,CA")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:CA")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,CAD:50000")
    with caplog.at_level(logging.WARNING):
        resp = await _http("POST", BASE, json=_body(address=CA_FORM_ADDRESS, payment_preference={"rail": "a2a"}))
    assert resp.status_code == 404 and _error(resp) == "not_available_on_this_rail", resp.text
    assert any("currency_market_mismatch" in r.getMessage() for r in caplog.records)


KR_ADDRESS = {"firstName": "Minneedle", "lastName": "Examplenee", "phone": "+82212345678",
              "addressLine1": "1 Needle-ro", "city": "Jongno-gu", "region": "11", "postalCode": "03154",
              "country": "KR"}


def _kr_on(monkeypatch):
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,KR")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:KR")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,KRW:300000")
    monkeypatch.setitem(FACTS, "currency", "KRW")
    monkeypatch.setitem(FACTS, "market_country", "KR")


async def test_a_won_purchase_is_priced_in_won_though_the_reap_lane_does_not_sell_in_korea(monkeypatch, stubs):
    _kr_on(monkeypatch)
    assert "KR" not in reap_routes._MARKET_CURRENCY  # the Reap lane's markets, and the directory's, are unchanged
    resp = await _http("POST", BASE, json=_body(address=KR_ADDRESS, expected_currency="KRW"))
    assert resp.status_code == 202, resp.text
    row = await a2a_ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert (row["currency"], row["shipping_address"]["country"]) == ("KRW", "KR")
    assert stubs.catalog_currency == ["KRW"]


async def test_with_no_address_a_won_purchase_takes_korea_as_its_market(monkeypatch, stubs):
    _kr_on(monkeypatch)
    pid = await _create(email=None, expected_currency="KRW")
    assert (await a2a_ledger.get_purchase_internal(pid))["currency"] == "KRW"
    assert stubs.catalog_currency == ["KRW"]


async def test_while_korea_is_off_a_won_purchase_is_not_taken_by_the_a2a_rail(monkeypatch, stubs):
    _kr_on(monkeypatch)
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US")
    resp = await _http("POST", BASE, json=_body(address=KR_ADDRESS, expected_currency="KRW"))
    # The router drops the rail (currency_unsupported) and hands the request to Reap, as for any market it is off for.
    assert resp.json().get("handled_by") == "reap", resp.text
    assert stubs.catalog_currency == []
    assert await database.fetch_all("SELECT id FROM a2a_rail_purchases") == []


HK_ADDRESS = {"firstName": "Pat", "lastName": "Example", "phone": "+85221234567", "addressLine1": "1 Example Road",
              "city": "Kowloon", "country": "HK"}


async def test_a_hong_kong_purchase_is_taken_with_no_postcode_and_none_is_stored(monkeypatch, stubs):
    monkeypatch.setenv("A2A_RAIL_MARKETS", "US,HK")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:HK")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:50000,HKD:50000")
    monkeypatch.setitem(FACTS, "currency", "HKD")
    monkeypatch.setitem(FACTS, "market_country", "HK")
    resp = await _http("POST", BASE, json=_body(address=HK_ADDRESS, expected_currency="HKD"))
    assert resp.status_code == 202, resp.text
    row = await a2a_ledger.get_purchase_internal(resp.json()["purchase_id"])
    assert (row["currency"], row["shipping_address"]["country"]) == ("HKD", "HK")
    assert "postalCode" not in row["shipping_address"] and "region" not in row["shipping_address"]
    # A region given in Hong Kong (no ISO 3166-2 subdivision) is the buyer's: stored as written, unchecked.
    resp = await _http("POST", BASE, json=_body(address={**HK_ADDRESS, "region": "KLN"}, expected_currency="HKD",
                                                 idempotency_key="key-2"))
    assert resp.status_code == 202, resp.text
    assert (await a2a_ledger.get_purchase_internal(resp.json()["purchase_id"]))["shipping_address"]["region"] == "KLN"
