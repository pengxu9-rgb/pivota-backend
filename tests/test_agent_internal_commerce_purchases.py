"""routes/agent_internal_commerce_purchases.py: the AgenPas gateway's server-to-server purchase read.

On SQLite, over the REAL app. The public route's two auth dependencies are overridden only to build the
body the internal read must equal. No route here may reach the network.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import services.payment_orchestration.purchase_view as purchase_view  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402

from main import app  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# The SQLite arm; tests/test_agent_internal_commerce_purchases_postgres.py runs these same tests on Postgres.
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")
#: Whether main.app already carries the read (built with it registered), and whether the dial was on when
#: it was built (read at build: CI builds it off).
_MOUNTED_AT_IMPORT = any(getattr(r, "path", "").startswith("/agent/internal/commerce/purchases") for r in app.routes)
_ARMED_AT_IMPORT = internal_routes.is_enabled()

INTERNAL = "/agent/internal/commerce/purchases"
PUBLIC = "/agent/v2/commerce/purchases"
KEY = "agenpas_read_test_key_0123456789abcdef"  # >= 32 characters (a shorter one counts as missing)
INTROSPECT_KEY = "introspect_test_key_0123456789abcdef"
AGENT = "agent_agenpas_internal"
OTHER_AGENT = "agent_agenpas_other"
USER_REF = "user-pat"
EMAIL = "buyer@example.test"
STREET = "1 Main St"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    # On Postgres agent_purchases is shared with other files: only its rail rows are cleared.
    tables = ("reap_agentic_purchases", "reap_agentic_enrollments", "a2a_rail_purchases", "a2a_rail_buyer_refs")
    if not IS_POSTGRES:
        tables = ("agent_purchases",) + tables
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    if not IS_POSTGRES:
        yield
        return
    await database.execute("DELETE FROM agent_purchases WHERE rail IN ('reap', 'a2a')")
    # asyncpg's pool belongs to this test's event loop: give it back at the end of every test.
    try:
        yield
    finally:
        await database.disconnect()


@pytest.fixture(autouse=True)
def _mounted(request):
    """The read as an app built with the dial ON registers it (`mount`), at the front of the route table.
    Tests named `..._dial_off_at_build_...` see main.app exactly as it was built (dial off in CI)."""
    if "_dial_off_at_build_" in request.node.name or _MOUNTED_AT_IMPORT:
        yield
        return
    before = list(app.router.routes)
    app.include_router(internal_routes.router)
    added = [r for r in app.router.routes if r not in before]
    app.router.routes[:] = added + before
    app.openapi_schema = None
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, KEY)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", INTROSPECT_KEY)
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a read route reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "AgenPas internal read test"
    allowed_merchants = None
    session_id = "session_agenpas_internal"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _get(url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
        return await http.get(url, **kwargs)


async def _public(url):
    """The public route's answer for AGENT / USER_REF (its auth dependencies overridden)."""
    caller = _Caller()

    async def _agent():
        return caller

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = lambda: AgentUserContext(agent_user_ref=USER_REF)
    try:
        return await _get(url)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


def _error(resp) -> Optional[str]:
    """The route's reason, out of the app-wide error envelope (`detail.error`), as the public route's
    tests read it."""
    detail = resp.json().get("detail")
    return detail.get("error") if isinstance(detail, dict) else detail


def _envelope(resp):
    """The error body without its per-request metadata (timestamp, request id)."""
    return {k: v for k, v in resp.json().items() if k != "metadata"}


def _headers(key: Optional[str] = KEY, agent: Optional[str] = AGENT, **extra: str):
    headers = dict(extra)
    if key is not None:
        headers["X-Internal-Key"] = key
    if agent is not None:
        headers["X-Agenpas-Agent-Id"] = agent
    return headers


async def _reap(*, agent_id: str = AGENT, user_ref: str = USER_REF, state: str = "resolving",
                **columns: Any) -> str:
    purchase = await reap_ledger.create_purchase(
        buyer_ref="bref_pat", agent_id=agent_id, agent_user_ref_hash=hash_agent_user_ref(user_ref),
        merchant_domain="merchant.example", product_key="pk_1", product_name="Placeholder Serum", quantity=1,
        currency="USD", our_price_minor=4250, buyer_email=EMAIL,
        shipping_address={"addressLine1": STREET, "city": "Austin"},
    )
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE reap_agentic_purchases SET {assignments} WHERE id = :id",
                           {**updates, "id": purchase["id"]})
    await purchases.ensure_reap_parent(purchase["id"], basis="backfill")
    return purchase["id"]


def test_the_router_is_mounted_on_the_real_app():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert f"{INTERNAL}/{{purchase_id}}" in paths
    assert f"{INTERNAL}/{{purchase_id}}/shipping-address" in paths  # the gateway's address write, same router


async def test_the_happy_path_body_equals_the_public_routes_body_for_the_same_row():
    rail_id = await _reap(state="completed", reap_order_id="ord_merchant_1", final_total_minor=4650)
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert public.status_code == 200
    parent_id = public.json()["purchase_id"]
    for purchase_id in (rail_id, parent_id):
        internal = await _get(f"{INTERNAL}/{purchase_id}", headers=_headers())
        assert internal.status_code == 200, internal.text
        assert internal.json() == public.json()
    text = json.dumps(internal.json())
    for leaked in (EMAIL, STREET, hash_agent_user_ref(USER_REF), "agent_user_ref_hash"):
        assert leaked not in text


async def test_the_gateway_user_ref_is_ignored_for_scoping():
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(**{"X-Agenpas-Agent-User-Ref": "usr_someone_else"}))
    assert answer.status_code == 200 and answer.json()["rail_purchase_id"] == rail_id


@pytest.mark.parametrize("key", [None, "", "wrong-key", KEY + "x", "k\u00e9y".encode("latin-1")])
async def test_a_wrong_or_missing_key_is_401(key):
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=key))
    assert answer.status_code == 401 and _error(answer) == "unauthorized"
    assert rail_id not in answer.text


async def test_a_server_without_the_key_refuses_everyone(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(internal_routes.INTERNAL_KEY_ENV)
    for key in (KEY, "", None):
        answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=key))
        assert answer.status_code == 503


async def test_with_the_dial_off_it_is_the_same_404_as_an_unknown_route(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    unknown = await _get("/agent/internal/commerce/no-such-route/x", headers=_headers())
    for headers in (_headers(), _headers(key=None), _headers(key="wrong")):
        answer = await _get(f"{INTERNAL}/{rail_id}", headers=headers)
        assert (answer.status_code, _envelope(answer)) == (unknown.status_code, _envelope(unknown))
        assert answer.status_code == 404 and _error(answer) == "Not Found"


async def test_another_agents_purchase_is_not_found_like_no_purchase():
    mine = await _reap()
    theirs = await _reap(agent_id=OTHER_AGENT)
    missing = "rp_" + "0" * 24
    answers = [await _get(f"{INTERNAL}/{pid}", headers=_headers()) for pid in (theirs, missing)]
    assert [(a.status_code, _error(a)) for a in answers] == [(404, "purchase_not_found")] * 2
    assert _envelope(answers[0]) == _envelope(answers[1])
    # and the other way round
    other = await _get(f"{INTERNAL}/{mine}", headers=_headers(agent=OTHER_AGENT))
    assert (other.status_code, _error(other)) == (404, "purchase_not_found")


@pytest.mark.parametrize("purchase_id", ["rp_short", "pp_" + "g" * 24, "xx_" + "0" * 24, "rp_" + "0" * 23 + "%00"])
async def test_malformed_ids_are_not_found(purchase_id):
    answer = await _get(f"{INTERNAL}/{purchase_id}", headers=_headers())
    assert answer.status_code == 404


@pytest.mark.parametrize("agent", [None, "", "agent with spaces", "a" * 129])
async def test_the_agent_id_is_required_and_checked(agent):
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(agent=agent))
    assert answer.status_code == 400 and _error(answer) == "agent_id_required"


async def test_a_dark_ledger_is_not_found(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (404, "purchase_not_found")


async def test_the_read_writes_nothing():
    rail_id = await _reap()
    await database.execute("DELETE FROM agent_purchases")  # a purchase whose parent is missing
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 404
    assert (await database.fetch_one("SELECT COUNT(*) AS n FROM agent_purchases"))["n"] == 0


async def _a2a(*, agent_id: str = AGENT, state: str = "paying") -> str:
    row = await a2a_ledger.create_purchase(
        agent_id=agent_id, agent_user_ref_hash=hash_agent_user_ref(USER_REF), merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address={"addressLine1": STREET, "city": "Austin"})
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = :s, quoted_total_minor = 6348, ceiling_source = 'allowance_v1', "
        "order_id = 'pvt-ap_x', checkout_task_id = 'ckout_internal1' WHERE id = :i", {"s": state, "i": row["id"]})
    await purchases.ensure_a2a_parent(row["id"], basis="backfill")
    return row["id"]


async def test_an_a2a_rail_purchase_reads_the_same_as_on_the_public_route():
    rail_id = await _a2a()
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert public.status_code == 200, public.text
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and internal.json() == public.json()
    body = internal.json()
    assert (body["rail"], body["state"], body["totals"]["ceiling_source"]) == ("a2a", "placing", "allowance_v1")
    text = json.dumps(body)
    for leaked in (EMAIL, STREET, "ckout_internal1", "pvt-ap_x", hash_agent_user_ref(USER_REF)):
        assert leaked not in text
    theirs = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(agent=OTHER_AGENT))
    assert (theirs.status_code, _error(theirs)) == (404, "purchase_not_found")


async def test_a_view_or_ledger_fault_is_a_503_never_a_500(monkeypatch):
    rail_id = await _a2a()
    real_view = purchase_view.unified_purchase

    async def _broken_view(*a, **k):
        raise RuntimeError("view blew up")

    monkeypatch.setattr(purchase_view, "unified_purchase", _broken_view)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (503, "temporarily_unavailable")
    monkeypatch.setattr(purchase_view, "unified_purchase", real_view)

    async def _broken_ledger(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(purchases, "get_by_rail_id_for_agent", _broken_ledger)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (503, "temporarily_unavailable")


def test_the_read_has_its_own_key_env():
    assert internal_routes.INTERNAL_KEY_ENV == "AGENPAS_INTERNAL_READ_KEY"


async def test_the_introspect_key_never_opens_this_read(monkeypatch):
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=INTROSPECT_KEY))
    assert (answer.status_code, _error(answer)) == (401, "unauthorized")
    # Not even when it is the only internal key the server has.
    monkeypatch.delenv(internal_routes.INTERNAL_KEY_ENV)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=INTROSPECT_KEY))
    assert answer.status_code == 503


@pytest.mark.parametrize("configured", ["short", "x" * 31, "  " + "y" * 31 + "  "])
async def test_a_configured_key_shorter_than_32_characters_counts_as_missing(monkeypatch, configured):
    rail_id = await _reap()
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, configured)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=configured.strip()))
    assert (answer.status_code, _error(answer)) == (503, "internal_key_not_configured")


async def test_a_32_character_key_is_enough(monkeypatch):
    rail_id = await _reap()
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, "z" * 32)
    assert (await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key="z" * 32))).status_code == 200


METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


@pytest.mark.skipif(_ARMED_AT_IMPORT, reason="main.app was built with AGENPAS_INTERNAL_READ_ENABLED on")
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("key", [None, KEY])
@pytest.mark.parametrize("suffix", ["", "/shipping-address"])
async def test_with_the_dial_off_at_build_every_method_answers_like_a_path_that_does_not_exist(method, key, suffix):
    rail_id = await _reap()
    transport = httpx.ASGITransport(app=app)
    async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
        ours = await http.request(method, f"{INTERNAL}/{rail_id}{suffix}", headers=_headers(key=key))
        unknown = await http.request(method, f"/agent/internal/commerce/no-such-path/{rail_id}{suffix}",
                                     headers=_headers(key=key))
    # Byte for byte what a path that does not exist answers (404; OPTIONS is the CORS layer's for both).
    assert ours.status_code == unknown.status_code == (404 if method != "OPTIONS" else unknown.status_code)
    assert ours.headers.get("allow") == unknown.headers.get("allow")
    if method not in ("HEAD", "OPTIONS"):
        assert _envelope(ours) == _envelope(unknown)
    elif method == "OPTIONS":
        assert ours.content == unknown.content


@pytest.mark.skipif(_ARMED_AT_IMPORT, reason="main.app was built with AGENPAS_INTERNAL_READ_ENABLED on")
def test_with_the_dial_off_at_build_the_route_is_not_registered_or_in_the_openapi_schema():
    assert not any(getattr(r, "path", "").startswith(INTERNAL) for r in app.routes)
    app.openapi_schema = None
    schema = app.openapi()
    assert not any(path.startswith(INTERNAL) for path in schema["paths"])
    # The door's body is read by the handler (no model in the schema), so capture adds nothing to it either.
    assert "CreatePurchaseBuyer" not in json.dumps(schema)


def test_with_the_dial_on_the_address_write_is_in_the_openapi_schema():
    app.openapi_schema = None
    try:
        assert f"{INTERNAL}/{{purchase_id}}/shipping-address" in app.openapi()["paths"]
    finally:
        app.openapi_schema = None


def test_mount_registers_the_route_only_with_the_dial_on(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    off = FastAPI()
    assert internal_routes.mount(off) is False and not any(INTERNAL in getattr(r, "path", "") for r in off.routes)
    monkeypatch.setenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV, "1")
    on = FastAPI()
    assert internal_routes.mount(on) is True and any(INTERNAL in getattr(r, "path", "") for r in on.routes)


def test_main_registers_the_read_through_mount_only():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text("utf-8")
    assert "agent_internal_commerce_purchases.mount(app)" in source
    assert "agent_internal_commerce_purchases_router" not in source


# ── the card on the page (AGENPAS_PAYMENT_CARD_ENABLED) ──────────────────────────────────────────


async def _card(*, buyer_ref: str = "bref_pat", network: Optional[str] = "VISA", last4: Optional[str] = "4242",
                reap_id: str = "enr_card_1") -> str:
    pending = await reap_ledger.upsert_pending_enrollment(buyer_ref=buyer_ref, agent_id=AGENT)
    active = await reap_ledger.mark_enrollment_active(
        pending["id"], reap_enrollment_id=reap_id, card_network=network, card_last4=last4)
    assert active is not None
    return active["id"]


#: The columns a checkout dispatched under the journal leaves on the row (db/reap_continuation.begin_dispatch).
def _bound(enrollment_id: str, **extra: Any) -> dict:
    return {"enrollment_id": enrollment_id, "reap_checkout_id": "chk_card_1", "checkout_dispatch_key": "k" * 64,
            "dispatch_tracking_version": 1, **extra}


async def test_with_the_card_dial_off_the_body_has_no_card_and_equals_the_public_body(monkeypatch):
    monkeypatch.delenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, raising=False)
    rail_id = await _reap(state="awaiting_approval", **_bound(await _card()))
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert internal.status_code == 200 and "payment_card" not in internal.json()
    assert internal.json() == public.json()


@pytest.mark.parametrize("state", ["awaiting_approval", "processing", "completed"])
async def test_with_the_card_dial_on_a_bound_checkout_shows_its_enrollments_card(monkeypatch, state):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state=state, **_bound(await _card()))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200, answer.text
    assert answer.json()["payment_card"] == {"network": "VISA", "last4": "4242"}
    # Only the page's read carries it: never the public route (nor, by the same builder, the webhooks).
    assert "payment_card" not in (await _public(f"{PUBLIC}/{rail_id}")).json()


@pytest.mark.parametrize("state", ["resolving", "needs_enrollment", "quoting", "failed", "refused", "expired"])
async def test_no_card_before_the_checkout_is_bound_or_on_a_row_that_ended_without_one(monkeypatch, state):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state=state, **_bound(await _card()))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and "payment_card" not in answer.json()


@pytest.mark.parametrize("missing", ["reap_checkout_id", "checkout_dispatch_key", "dispatch_tracking_version"])
async def test_no_card_without_a_checkout_dispatched_under_the_journal(monkeypatch, missing):
    """A legacy row (no journal) cannot show which enrollment its create used; a row with no checkout has none."""
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state="awaiting_approval", **_bound(await _card(), **{missing: None}))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and "payment_card" not in answer.json()


async def test_a_newer_card_of_the_same_buyer_is_not_the_card_the_checkout_was_bound_to(monkeypatch):
    """The buyer adds another card after the checkout was created: the page still names the bound one."""
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    bound = await _card(network="VISA", last4="4242", reap_id="enr_card_old")
    rail_id = await _reap(state="awaiting_approval", **_bound(bound))
    await _card(network="MASTERCARD", last4="5454", reap_id="enr_card_new")
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.json()["payment_card"] == {"network": "VISA", "last4": "4242"}


async def test_another_buyers_enrollment_is_never_shown(monkeypatch):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state="awaiting_approval", **_bound(await _card(buyer_ref="bref_someone_else")))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and "payment_card" not in answer.json()


@pytest.mark.parametrize("network, last4, expected", [
    (None, "4242", {"network": None, "last4": "4242"}),
    ("VISA<script>", "4242", {"network": None, "last4": "4242"}),
    ("American Express", "0005", {"network": "American Express", "last4": "0005"}),
    ("VISA", None, None),
])
async def test_the_card_is_only_a_plain_network_name_and_exactly_four_digits(monkeypatch, network, last4, expected):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state="awaiting_approval", **_bound(await _card(network=network, last4=last4)))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and answer.json().get("payment_card") == expected


async def test_a_card_read_fault_drops_the_card_never_the_body(monkeypatch):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _reap(state="awaiting_approval", **_bound(await _card()))

    async def _broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(reap_ledger, "get_payment_card_for_owner", _broken)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and answer.json()["state"] == "awaiting_buyer_authorization"
    assert "payment_card" not in answer.json()


async def test_an_a2a_purchase_never_carries_a_card(monkeypatch):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _a2a()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and "payment_card" not in answer.json()


@pytest.mark.parametrize("stored", ["12a4", "    ", "4 42"])
async def test_a_stored_last4_that_is_not_four_digits_shows_no_card(monkeypatch, stored):
    """The writer refuses such a value; a row written another way (by hand, an older writer) still shows nothing."""
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    enrollment_id = await _card()
    await database.execute("UPDATE reap_agentic_enrollments SET card_last4 = :v WHERE id = :i",
                           {"v": stored, "i": enrollment_id})
    rail_id = await _reap(state="awaiting_approval", **_bound(enrollment_id))
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 200 and "payment_card" not in answer.json()


# ── the agent's name: set by hand (agents.buyer_display_name, mig 297), else the portal name ─────


async def _agent_row(agent_id: str = AGENT, *, agent_name: str = "Registered Assistant",
                     reviewed: Optional[str] = None) -> None:
    """One agents row for this file's agent id, built from the model when the table is missing, with the
    column the boot heal adds (re-run here: the fixture's heal ran before this table may have existed).
    `agent_name` is the name the agent registered with in the developer portal."""
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    import db.agents as agents_db

    dialect = postgresql.dialect() if IS_POSTGRES else sqlite.dialect()
    await database.execute(str(CreateTable(agents_db.agents, if_not_exists=True).compile(dialect=dialect)))
    await ensure_required_schema_light()
    await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": agent_id})
    await database.execute(
        "INSERT INTO agents (agent_id, agent_name, agent_type, api_key, api_key_hash, is_active, buyer_display_name) "
        "VALUES (:a, :n, 'custom', :k, :h, TRUE, :r)",
        {"a": agent_id, "n": agent_name, "k": f"redacted:{agent_id}", "h": "0" * 64, "r": reviewed})


@pytest.fixture
async def _agent_rows():
    yield
    await database.execute("DELETE FROM agents WHERE agent_id IN (:a, :b)", {"a": AGENT, "b": OTHER_AGENT})


async def _shown_name(rail_id: Optional[str] = None) -> Optional[str]:
    rail_id = rail_id or await _reap()
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200, internal.text
    body = internal.json()
    if "agent_display_name" not in body:
        # No name: the body is exactly the public one.
        assert body == (await _public(f"{PUBLIC}/{rail_id}")).json()
    return body.get("agent_display_name")


@pytest.mark.parametrize("registered, reviewed, shown", [
    ("Example Assistant", None, "Example Assistant"),        # the portal name
    ("Example Assistant", "Hand Set Name", "Hand Set Name"),  # a name set by hand wins
])
async def test_the_name_is_on_the_body_for_either_rail_and_never_on_the_public_route(
        _agent_rows, registered, reviewed, shown):
    await _agent_row(agent_name=registered, reviewed=reviewed)
    for rail_id in (await _reap(), await _a2a()):
        internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
        assert internal.status_code == 200, internal.text
        assert internal.json()["agent_display_name"] == shown
        public = await _public(f"{PUBLIC}/{rail_id}")
        assert public.status_code == 200 and "agent_display_name" not in public.json()
        assert shown not in public.text
        # Nothing else changed: the body is the public one plus the name.
        assert {k: v for k, v in internal.json().items() if k != "agent_display_name"} == public.json()


@pytest.mark.parametrize("reviewed", ["", "   ", "!!!", "Two  Spaces"])
async def test_a_hand_set_value_that_is_not_a_name_hides_the_portal_name_and_never_falls_through(
        _agent_rows, reviewed):
    await _agent_row(agent_name="Example Assistant", reviewed=reviewed)
    assert await _shown_name() is None


@pytest.mark.parametrize("registered", [
    "Your Bank", "VISA Helper", "Master-Card Agent", "American Express Concierge", "A.M.E.X", "Discover Deals",
    "Apple Pay Assistant", "google pay", "Pay Pal", "Stripe Bot", "Pivota", "Pivota Official", "AgenPaz",
    "AgenPas Checkout", "Secure Checkout", "Ｖｉｓａ", "Vísa Shopper", "Agent", "Unknown", "unknown agent", "UNKNOWN",
])
async def test_a_blocked_portal_name_shows_nothing(_agent_rows, registered):
    await _agent_row(agent_name=registered, reviewed=None)
    assert await _shown_name() is None


async def test_a_hand_set_name_corrects_a_blocked_portal_name(_agent_rows):
    await _agent_row(agent_name="Secure Bank Agent", reviewed="Example Assistant")
    assert await _shown_name() == "Example Assistant"


@pytest.mark.parametrize("stored", ["   ", "\u202eknaB ruoY", "Example\u200bAssistant", "Line\nBreak", "Two  Spaces",
                                    "!!!", "Tab\tName", "x" * 65])
@pytest.mark.parametrize("source", ["hand_set", "portal"])
async def test_a_stored_name_without_a_names_shape_is_not_shown(_agent_rows, stored, source):
    if source == "hand_set":
        await _agent_row(agent_name="Example Assistant", reviewed=stored)
    else:
        await _agent_row(agent_name=stored, reviewed=None)
    assert await _shown_name() is None


@pytest.mark.parametrize("source", ["hand_set", "portal"])
async def test_a_stored_name_is_trimmed(_agent_rows, source):
    if source == "hand_set":
        await _agent_row(agent_name="Other Name", reviewed="  Example Assistant ")
    else:
        await _agent_row(agent_name="  Example Assistant ", reviewed=None)
    assert await _shown_name() == "Example Assistant"


async def test_no_agent_row_means_no_name():
    assert await _shown_name() is None


async def test_another_agents_name_is_never_used(_agent_rows):
    # Only the purchase's own agent row counts: another agent's portal or hand-set name never stands in.
    await _agent_row(OTHER_AGENT, agent_name="Other Assistant", reviewed="Other Hand Set")
    assert await _shown_name() is None


async def test_no_registered_name_means_no_fallback(_agent_rows):
    # A portal name without a name's shape shows nothing: never a placeholder, never another field.
    await _agent_row(agent_name="!!!", reviewed=None)
    rail_id = await _reap()
    assert await _shown_name(rail_id) is None
    text = (await _get(f"{INTERNAL}/{rail_id}", headers=_headers())).text
    assert '"Agent"' not in text and '"Unknown"' not in text


async def test_a_name_read_fault_drops_the_name_never_the_body(monkeypatch, _agent_rows):
    import db.agents as agents_db

    await _agent_row(reviewed="Example Assistant")

    async def boom(agent_id):
        raise RuntimeError("agents read down")

    monkeypatch.setattr(agents_db, "get_buyer_display_name", boom)
    assert await _shown_name() is None


@pytest.mark.parametrize("name", [
    "Visa", "visa", "V-I-S-A", "Ｖｉｓａ", "Vísa", "Revisa Assistant", "MasterCard", "Master Card", "AMEX",
    "American  Express", "Discover", "Apple Pay", "ApplePay", "Google Pay", "PayPal", "Stripe", "Pivota",
    "pivota.ai", "AgenPaz", "Agen Paz", "AgenPas", "First Bank", "Banking Bot", "Secure", "SecureCheckout",
    "Agent", "Unknown", "Unknown Agent", "agent!",
    # Lookalike letters (Cyrillic or Greek for Latin), alone or mixed in: folded before matching.
    "V\u0456sa", "\u0420aypal", "M\u0430stercard", "Str\u0456pe", "\u0405ecure Pay", "\u0420\u0430\u0443\u0440\u0430\u04cf",
    "\u0392\u0391\u039d\u039a",
    # Digit and symbol stand-ins: 0 o, 1 i and l, 3 e, 4 a, 5 s, 7 t, $ s, @ a.
    "V1sa Checkout", "Paypa1", "P1vota", "B4nk", "$tripe", "@mex", "5ecure", "Di5c0ver", "S3cure", "S7ripe", "Ag3nt",
    # Latin letters that are not ASCII, and letters of scripts outside the allowlist.
    "\u1d20\u026a\ua731\u1d00", "\u0299\u1d00\u0274\u1d0b", "\u13d9isa", "\u13daripe", "\u054ftripe", "payp\u0561l",
    "\ua4d1aypal", "\u0251mex", "\u028bisa", "S\u0167ripe",
    # Wholly Greek: the capitals that complete a term are folded too.
    "\u03a1\u0391\u03a5\u03a1\u0391\u039b",
    # Shapes that read as one another: capital I or 1 for l (and l for i), rn for m, cl for d, vv for w.
    "PayPaI", "AppIePay", "GoogIePay", "VIsa", "Vlsa", "Arnex", "rnastercard", "Cliscover", "VVisa", "StRiPe",
    "A g e n P a z", "Unknovvn",
])
def test_portal_name_is_blocked(name):
    import db.agents as agents_db

    assert agents_db.portal_name_is_blocked(name) is True


@pytest.mark.parametrize("name", [
    "Example Assistant", "Shopping Agent", "示例助手", "Agent Ñandú", "Assistant 2", "Payment Helper", "Aurora",
    # Latin mixed with a script that is not a lookalike one, and names wholly in Cyrillic or Greek.
    "Meitu 美图", "示例 Shop", "Пример", "Παράδειγμα", "Пример Παράδειγμα", "Ａｕｒｏｒａ", "Agent 47",
    # Accented Latin (the accent decomposes off an ASCII letter) and every allowed script.
    "Shopping Helper", "Café Helper", "佐々木 Shop", "カード・ラボ", "ﾊﾟﾝ屋", "한국 쇼핑", "متجر", "חנות", "ร้านค้า", "दुकान",
])
def test_a_plain_portal_name_is_not_blocked(name):
    import db.agents as agents_db

    assert agents_db.portal_name_is_blocked(name) is False


@pytest.mark.parametrize("name", [
    "Ex\u0430mple Assistant",            # Latin with one Cyrillic a
    "\u0391urora",                        # Greek capital alpha, then Latin
    "\uff25\uff58\u0430\uff4d\uff50\uff4c\uff45",  # fullwidth Latin (read as Latin) with a Cyrillic a
    "Shop \u041c\u0430\u0433\u0430\u0437\u0438\u043d",  # a Latin word next to a Cyrillic one
])
def test_a_portal_name_mixing_latin_with_a_lookalike_script_is_refused(name):
    import db.agents as agents_db

    assert agents_db.portal_name_mixes_lookalike_scripts(name) is True
    assert agents_db.portal_name_is_blocked(name) is True


@pytest.mark.parametrize("name", [
    # Latin letters that are not ASCII a-z / A-Z after NFKD: small capitals, IPA, stroke letters, ø, ß, ł.
    "\u1d00urora", "\u0251urora", "Au\u0280ora", "S\u00f8ren Helper", "Stra\u00dfe Shop", "\u0141ukas Helper",
    "Ca\u0167 Shop",
    # Scripts outside the allowlist, alone or with Latin: Cherokee, Armenian, Lisu, Canadian syllabics, Bopomofo.
    "\u13aa\u13b3\u13a9", "\u053d\u0561\u0576\u0578\u0582\u0569", "\ua4d0\ua4f3\ua4e0", "\u1455\u144e",
    "\u3105\u3106", "\u13aaurora",
    # A modifier letter is not a letter of an allowed script.
    "Shop\u02bcs",
])
def test_a_letter_outside_the_allowlist_refuses_a_portal_name(name):
    import db.agents as agents_db

    assert agents_db._portal_name_letters_allowed(name) is False
    assert agents_db.portal_name_is_blocked(name) is True


async def test_a_portal_name_outside_the_allowlist_shows_nothing_but_a_hand_set_one_shows(_agent_rows):
    await _agent_row(agent_name="S\u00f8ren Helper", reviewed=None)
    assert await _shown_name() is None
    await _agent_row(agent_name="Example Assistant", reviewed="S\u00f8ren Helper")
    assert await _shown_name() == "S\u00f8ren Helper"


@pytest.mark.parametrize("name", ["Meitu 美图", "Пример", "Παράδειγμα", "Пример Παράδειγμα", "Example", "Ａｕｒｏｒａ"])
def test_one_script_or_latin_with_another_script_is_not_a_mix(name):
    import db.agents as agents_db

    assert agents_db.portal_name_mixes_lookalike_scripts(name) is False


async def test_a_mixed_script_portal_name_shows_nothing_but_a_hand_set_one_shows(_agent_rows):
    await _agent_row(agent_name="Ex\u0430mple Assistant", reviewed=None)
    assert await _shown_name() is None
    # The block list and the mix rule are for portal names: a name set by hand is the reviewed one.
    await _agent_row(agent_name="Example Assistant", reviewed="Ex\u0430mple Assistant")
    assert await _shown_name() == "Ex\u0430mple Assistant"


def test_the_block_list_is_kept_in_one_place():
    import db.agents as agents_db

    assert set(agents_db.PORTAL_NAME_BLOCKED_TERMS) == {
        "visa", "mastercard", "amex", "americanexpress", "discover", "applepay", "googlepay", "paypal", "stripe",
        "pivota", "agenpaz", "agenpas", "bank", "secure",
    }
    # Each term is matched by its skeleton, reduced exactly as a name is; the plain term itself is blocked.
    assert agents_db._BLOCKED_TERM_SKELETONS == tuple(
        agents_db._name_skeleton(term) for term in agents_db.PORTAL_NAME_BLOCKED_TERMS)
    assert all(agents_db.portal_name_is_blocked(term) for term in agents_db.PORTAL_NAME_BLOCKED_TERMS)


@pytest.mark.parametrize("a, b", [
    ("PayPaI", "paypal"), ("V1sa", "visa"), ("Vlsa", "visa"), ("Arnex", "amex"), ("Cliscover", "discover"),
    ("VVebshop", "webshop"), ("Ｐаy-PaI", "paypal"),
])
def test_shapes_that_read_alike_share_a_skeleton(a, b):
    import db.agents as agents_db

    assert agents_db._name_skeleton(a) == agents_db._name_skeleton(b)


# ── a rename hides the portal name until checked again (PUT /agents/{agent_id}, the one writer) ─


async def _put_agent(body: dict, *, role: str = "agent"):
    from utils.auth import get_current_user

    user = {"role": role, "email": "owner@example.test", "user_id": "u-1"}
    if role == "agent":
        user["agent_id"] = AGENT
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
            return await http.put(f"/agents/{AGENT}", json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _stored_names() -> dict:
    row = await database.fetch_one(
        "SELECT agent_name, buyer_display_name FROM agents WHERE agent_id = :a", {"a": AGENT})
    return dict(row)


@pytest.mark.parametrize("role", ["agent", "admin"])
async def test_a_rename_hides_the_portal_name_until_checked_again(_agent_rows, role):
    await _agent_row(agent_name="Example Assistant", reviewed=None)
    assert await _shown_name() == "Example Assistant"
    answer = await _put_agent({"agent_name": "Renamed Assistant"}, role=role)
    assert answer.status_code == 200, answer.text
    assert await _stored_names() == {"agent_name": "Renamed Assistant", "buyer_display_name": ""}
    assert await _shown_name() is None
    # The gated one-off's check: back to NULL, and the new portal name shows.
    await database.execute("UPDATE agents SET buyer_display_name = NULL WHERE agent_id = :a", {"a": AGENT})
    assert await _shown_name() == "Renamed Assistant"


async def test_a_put_with_the_same_name_does_not_hide_it(_agent_rows):
    await _agent_row(agent_name="Example Assistant", reviewed=None)
    answer = await _put_agent({"agent_name": "Example Assistant", "description": "new words"})
    assert answer.status_code == 200, answer.text
    assert (await _stored_names())["buyer_display_name"] is None
    assert await _shown_name() == "Example Assistant"


async def test_a_put_without_a_name_does_not_hide_it(_agent_rows):
    await _agent_row(agent_name="Example Assistant", reviewed=None)
    answer = await _put_agent({"description": "new words"})
    assert answer.status_code == 200, answer.text
    assert await _shown_name() == "Example Assistant"


@pytest.mark.parametrize("reviewed, shown", [("Hand Set Name", "Hand Set Name"), ("", None)])
async def test_a_rename_leaves_a_hand_set_value_as_it_is(_agent_rows, reviewed, shown):
    await _agent_row(agent_name="Example Assistant", reviewed=reviewed)
    answer = await _put_agent({"agent_name": "Renamed Assistant"})
    assert answer.status_code == 200, answer.text
    assert (await _stored_names())["buyer_display_name"] == reviewed
    assert await _shown_name() == shown


async def test_a_failed_hide_fails_the_rename(monkeypatch, _agent_rows):
    import routes.agent_management as agent_management

    await _agent_row(agent_name="Example Assistant", reviewed=None)
    monkeypatch.setattr(agent_management, "portal_name_rename_hide",
                        lambda agent_id, new_name: ("UPDATE no_such_table SET x = 1", {}))
    answer = await _put_agent({"agent_name": "Renamed Assistant"})
    assert answer.status_code == 500
    assert await _stored_names() == {"agent_name": "Example Assistant", "buyer_display_name": None}


def test_the_reviewed_name_column_is_not_on_the_agents_model():
    # Every model SELECT names every model column: one boot whose heal lost the lock race would then break
    # every agent read. Only db/agents.get_buyer_display_name names it.
    import db.agents as agents_db

    assert "buyer_display_name" not in agents_db.agents.c


@pytest.mark.parametrize("raw, expected", [
    ("Example Assistant", "Example Assistant"),
    ("  Example Assistant\t", "Example Assistant"),
    ("Agent Ñandú", "Agent Ñandú"),
    ("示例助手", "示例助手"),
    ("Assistant 2", "Assistant 2"),
    ("A", "A"),
    ("x" * 64, "x" * 64),
    ("x" * 65, None),
    ("", None),
    ("   ", None),
    (None, None),
    (42, None),
    ("Agent Ñandu", None),  # not NFC: refused, never normalised
    ("Two  Spaces", None),
    ("Line\nBreak", None),
    ("No Break", None),
    ("‮knaB", None),
    ("Zero​Width", None),
    ("Bom﻿", None),
    ("Para Sep", None),
    ("Private", None),
    ("!!! ---", None),
])
def test_clean_buyer_display_name(raw, expected):
    import db.agents as agents_db

    assert agents_db.clean_buyer_display_name(raw) == expected
