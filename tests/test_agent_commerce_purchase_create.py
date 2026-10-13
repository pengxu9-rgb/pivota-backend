"""POST /agent/v2/commerce/purchases (routes/agent_commerce_purchases.create_purchase) and the rail router
(services/payment_orchestration/router.py), on SQLite, over the REAL router (in a minimal app) and the REAL ledgers.

Stubbed: the two auth dependencies, OUR catalog read for a cart link (`_load_cart_link_item`: the Reap
cart-link lane's tests own it), the Tier B verdict, the click write, and the Reap route (a request handed
to Reap is recorded, not run: the Reap route's tests own what it does). No route may reach the network.

Pinned: the dial; the key is looked up BEFORE the router (a retry replays the original on its rail); an
explicit rail the router cannot honour is a 404 and is never sent to the other rail; the A2A create makes
every check before any write and writes the purchase and its key in one unit; nothing is the caller's
(price, link, click); the parent records why the rail was chosen.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

import httpx
import pytest
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
from services.payment_orchestration import router as rail_router  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402

# A MINIMAL app (the router under test and the app's error middleware), never `main`: the Postgres gate
# shares one process and one database, and main's model registrations poison every gate file after it
# (see tests/test_agent_commerce_reap_routes_postgres.py).
from fastapi import FastAPI  # noqa: E402
from middleware.error_handler import ErrorHandlerMiddleware  # noqa: E402

app = FastAPI()
app.add_middleware(ErrorHandlerMiddleware)
app.include_router(unified_routes.router)
app.include_router(reap_routes.router)  # the REAL Reap door, for the shared-key cases

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# The SQLite arm; tests/test_agent_commerce_purchase_create_postgres.py runs these same tests on Postgres.
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

BASE = "/agent/v2/commerce/purchases"
AGENT = "agent_create_route"
USER_REF = "user-ada"
SHOP = "judydoll.com"
VARIANT = "49922977038613"
EMAIL = "ada@example.test"
ADDRESS = {"firstName": "Ada", "lastName": "Lovelace", "phone": "+15550100", "addressLine1": "900 Brannan St",
           "city": "San Francisco", "country": "US", "postalCode": "94103"}
FACTS = {"shop_domain": SHOP, "our_price_minor": 2820, "currency": "USD", "market_country": "US",
         "product_name": "Lip Gloss", "product_key": "pk_1", "variant_title": "01 Rose"}


_TABLES = ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs", "reap_agentic_purchase_keys",
           "reap_agentic_purchases", "reap_agentic_eligibility", "reap_agentic_buyer_refs")


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    if not IS_POSTGRES:
        for table in _TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        await ensure_required_schema_light()
        yield
        return
    # On Postgres these tables are shared with other files (one gate database): emptied, never dropped.
    await ensure_required_schema_light()
    for table in _TABLES:
        await database.execute(f"DELETE FROM {table}")
    # asyncpg's pool belongs to this test's event loop: give it back at the end of every test.
    try:
        yield
    finally:
        await database.disconnect()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(rail_router.PAYMENT_ORCHESTRATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    monkeypatch.setenv("A2A_RAIL_ENABLED", "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_CREATE_ENABLED_ENV, "1")
    monkeypatch.setenv(rail_router.A2A_RAIL_PILOT_AGENT_IDS_ENV, f"{AGENT},agent_other")
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:US, other.example:SG")
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "USD:10000, SGD:13500")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("the create route reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class Stubs:
    def __init__(self):
        self.catalog = []
        self.clicks = []
        self.reap = []
        self.tierb = True
        self.facts = dict(FACTS)


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    s = Stubs()

    async def _catalog(*, merchant_domain, product_key, variant_key, market_country):
        s.catalog.append((merchant_domain, product_key, variant_key, market_country))
        return dict(s.facts), "merch_obs_judydoll", VARIANT, "brand_official"

    async def _click(click):
        s.clicks.append(click)

    async def _eligible(domain, market):
        return s.tierb

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
    agent_name = "Create Route Test"
    allowed_merchants = None
    session_id = "session_create_route"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


CALLER = _Caller()


@pytest.fixture
def client():
    CALLER.agent_id, CALLER.agent_user_ref = AGENT, USER_REF

    async def _agent():
        return CALLER

    def _user():
        ref = CALLER.agent_user_ref
        return AgentUserContext(agent_user_ref=ref) if ref else None

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = _user

    class _Harness:
        async def post(self, url, body):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
                return await http.post(url, json=body)

        async def get(self, url):
            transport = httpx.ASGITransport(app=app)
            async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
                return await http.get(url)

    try:
        yield _Harness()
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


def body(**over) -> Dict[str, Any]:
    b = {
        "item_source": "cart_link", "merchant_domain": SHOP, "product_key": "pk_1", "quantity": 1,
        "buyer": {"email": EMAIL, "shipping_address": dict(ADDRESS), "consent_version": "terms-2026-10"},
        "idempotency_key": "key-1", "expected_unit_price_minor": 2820, "expected_currency": "USD",
    }
    b.update(over)
    return b


async def _count(table: str) -> int:
    row = await database.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"])


async def _nothing_written():
    assert await _count("a2a_rail_purchases") == 0
    assert await _count("a2a_rail_buyer_refs") == 0
    assert await _count("reap_agentic_purchase_keys") == 0
    assert await _count("agent_purchases") == 0


# ── the door ─────────────────────────────────────────────────────────────────────────────────


async def test_dark_until_the_orchestrator_dial_is_on(client, stubs, monkeypatch):
    monkeypatch.delenv(rail_router.PAYMENT_ORCHESTRATOR_ENABLED_ENV)
    resp = await client.post(BASE, body())
    assert resp.status_code == 404 and "not_available" in resp.text
    assert stubs.reap == [] and stubs.catalog == []
    await _nothing_written()


async def test_a_buyer_is_required(client, stubs):
    CALLER.agent_user_ref = None
    resp = await client.post(BASE, body())
    assert resp.status_code == 401
    await _nothing_written()


async def test_consent_is_required_before_any_read_or_write(client, stubs):
    b = body()
    b["buyer"].pop("consent_version")
    resp = await client.post(BASE, b)
    assert resp.status_code == 400 and "consent_required" in resp.text
    assert stubs.catalog == [] and stubs.reap == []
    await _nothing_written()


# ── the A2A create ───────────────────────────────────────────────────────────────────────────


async def test_an_eligible_request_opens_an_a2a_purchase_with_our_price_link_and_click(client, stubs):
    resp = await client.post(BASE, body())
    assert resp.status_code == 202, resp.text
    out = resp.json()
    assert (out["rail"], out["state"], out["rail_state"], out["variant_title"]) == (
        "a2a", "routing", "opened", "01 Rose")
    assert out["purchase_id"].startswith("ap_") and stubs.reap == []
    row = await a2a_ledger.get_purchase_internal(out["purchase_id"])
    assert (row["agent_id"], row["agent_user_ref_hash"]) == (AGENT, hash_agent_user_ref(USER_REF))
    assert (row["merchant_domain"], row["our_price_minor"], row["currency"], row["quantity"]) == (SHOP, 2820, "USD", 1)
    assert (row["buyer_email"], row["shipping_address"]["addressLine1"]) == (EMAIL, "900 Brannan St")
    assert row["consent_version"] == "terms-2026-10" and row["consented_at"] is not None
    # The link is OURS: the verified variant, the click we minted, the buyer's market.
    [click] = stubs.clicks
    assert click.surface == "a2a_cart_link" and click.agent_id == AGENT and click.destination_url == row["checkout_url"]
    assert row["checkout_url"].startswith(f"https://{SHOP}/cart/{VARIANT}:1")
    assert click.click_id in row["checkout_url"] and "country=US" in row["checkout_url"]
    # The key is in the one table both rails share; the parent says why this rail.
    key = await database.fetch_one("SELECT purchase_id FROM reap_agentic_purchase_keys WHERE idempotency_key = 'key-1'")
    assert key["purchase_id"] == out["purchase_id"]
    parent = await database.fetch_one("SELECT rail, routing_plan FROM agent_purchases")
    plan = json.loads(parent["routing_plan"]) if isinstance(parent["routing_plan"], str) else parent["routing_plan"]
    assert parent["rail"] == "a2a"
    assert plan == {"rail": "a2a", "executor": "rail_managed", "basis": "orchestrator_selected_rail",
                    "dropped": {"reap": "a2a_preferred"}}


async def test_the_created_purchase_reads_back_through_the_unified_get(client):
    pid = (await client.post(BASE, body())).json()["purchase_id"]
    got = await client.get(f"{BASE}/{pid}")
    assert got.status_code == 200, got.text
    assert (got.json()["rail"], got.json()["state"], got.json()["rail_purchase_id"]) == ("a2a", "routing", pid)


async def test_a_retry_with_the_same_key_replays_the_same_purchase_and_writes_nothing(client, stubs):
    first = (await client.post(BASE, body())).json()
    again = await client.post(BASE, body())
    assert again.status_code == 202 and again.json()["purchase_id"] == first["purchase_id"]
    assert await _count("a2a_rail_purchases") == 1 and len(stubs.clicks) == 1 and len(stubs.catalog) == 1


async def test_a_retry_replays_on_its_original_rail_even_when_the_router_would_now_say_otherwise(client, stubs, monkeypatch):
    first = (await client.post(BASE, body())).json()
    monkeypatch.delenv(rail_router.A2A_RAIL_MERCHANTS_ENV)  # the merchant is no longer routable to a2a
    again = await client.post(BASE, body())
    assert again.json()["purchase_id"] == first["purchase_id"] and stubs.reap == []


async def test_the_same_key_for_a_different_body_is_a_conflict_that_names_the_keys_purchase(client):
    pid = (await client.post(BASE, body())).json()["purchase_id"]
    resp = await client.post(BASE, body(quantity=2))
    assert resp.status_code == 409 and "idempotency_conflict" in resp.text
    detail = resp.json().get("detail") or resp.json()
    assert (detail["purchase_id"], detail["rail"]) == (pid, "a2a")
    assert await _count("a2a_rail_purchases") == 1


async def test_a_conflict_never_names_another_owners_purchase_or_a_marker(client, stubs):
    # Another end user's key with the same name: not this caller's row, so no conflict and no id at all.
    await client.post(BASE, body())
    CALLER.agent_user_ref = "user-grace"
    other = await client.post(BASE, body(quantity=2))
    assert other.status_code == 202 and other.json()["purchase_id"] not in ("", None)
    # A refusal tombstone under this key: a conflict, with no id.
    CALLER.agent_user_ref = USER_REF
    await database.execute(
        "UPDATE reap_agentic_purchase_keys SET purchase_id = 'refused:merchant_not_eligible' "
        "WHERE agent_user_ref_hash = :h", {"h": hash_agent_user_ref(USER_REF)})
    resp = await client.post(BASE, body(quantity=3))
    assert resp.status_code == 409 and "idempotency_conflict" in resp.text
    assert "purchase_id" not in resp.text


async def test_a_key_that_opened_a_reap_purchase_is_replayed_by_the_reap_route(client, stubs):
    req = unified_routes.CreatePurchaseRequest.model_validate(body())
    request_hash = reap_routes._request_hash(
        merchant_domain=SHOP, product_key="pk_1", variant_key=None, quantity=1, email=EMAIL,
        shipping_address=reap_routes._buyer_address_for_client(req.buyer), return_url=reap_routes._default_return_url(),
        item_source="cart_link", expected_unit_price_minor=2820, expected_currency="USD")
    await database.execute(
        "INSERT INTO reap_agentic_purchase_keys (agent_id, agent_user_ref_hash, idempotency_key, purchase_id, request_hash) "
        "VALUES (:a, :h, 'key-1', 'rp_0123456789abcdef01234567', :r)",
        {"a": AGENT, "h": hash_agent_user_ref(USER_REF), "r": request_hash})
    resp = await client.post(BASE, body())
    assert resp.json() == {"handled_by": "reap"} and len(stubs.reap) == 1
    assert stubs.catalog == [] and await _count("a2a_rail_purchases") == 0


@pytest.mark.parametrize("setup,status,reason", [
    ("price", 409, "price_changed"),
    ("tierb", 409, "merchant_not_eligible"),
    ("over_cap", 409, "amount_over_limit"),
    ("disabled", 409, "merchant_disabled"),
])
async def test_every_refusal_happens_before_any_write(client, stubs, monkeypatch, setup, status, reason):
    b = body()
    if setup == "price":
        stubs.facts["our_price_minor"] = 2990
    elif setup == "tierb":
        stubs.tierb = False
    elif setup == "over_cap":
        b = body(quantity=4)  # 4 x 2820 > 10000
    elif setup == "disabled":
        await database.execute(
            "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, market_country, enabled) "
            "VALUES (:d, '', '', 'US', FALSE)", {"d": SHOP})
    resp = await client.post(BASE, b)
    assert resp.status_code == status and reason in resp.text, resp.text
    assert stubs.clicks == [] and stubs.reap == []
    await _nothing_written()


async def test_a_new_attempt_without_the_shown_money_is_refused(client, stubs):
    b = body()
    b.pop("expected_unit_price_minor"), b.pop("expected_currency")
    resp = await client.post(BASE, b)
    assert resp.status_code == 400 and "invalid_request" in resp.text
    await _nothing_written()


async def test_a_lost_key_race_rolls_ours_back_and_answers_the_winner(client, stubs, monkeypatch):
    winner = (await client.post(BASE, body(idempotency_key="key-0"))).json()["purchase_id"]

    async def _other_won(**kwargs):
        return winner

    monkeypatch.setattr(reap_routes, "_claim_idempotency_key", _other_won)
    resp = await client.post(BASE, body())
    assert resp.json()["purchase_id"] == winner
    assert await _count("a2a_rail_purchases") == 1  # ours was rolled back with its key write


async def test_a_lost_key_race_to_a_reap_purchase_is_handed_to_the_reap_route(client, stubs, monkeypatch):
    async def _reap_won(**kwargs):
        return "rp_0123456789abcdef01234567"

    monkeypatch.setattr(reap_routes, "_claim_idempotency_key", _reap_won)
    resp = await client.post(BASE, body())
    assert resp.json() == {"handled_by": "reap"} and await _count("a2a_rail_purchases") == 0


# ── routing ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("change", ["rail_off", "create_off", "not_pilot", "not_proven", "no_cap", "variant_lane", "offer_code"])
async def test_without_a_choice_an_ineligible_request_goes_to_reap_unchanged(client, stubs, monkeypatch, change):
    b = body()
    if change == "rail_off":
        monkeypatch.delenv("A2A_RAIL_ENABLED")
    elif change == "create_off":
        monkeypatch.delenv(rail_router.A2A_RAIL_CREATE_ENABLED_ENV)
    elif change == "not_pilot":
        CALLER.agent_id = "agent_not_in_pilot"
    elif change == "not_proven":
        monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, f"{SHOP}:SG")
    elif change == "no_cap":
        monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, "lots")
    elif change == "variant_lane":
        b = body(item_source="reap_variant")
    elif change == "offer_code":
        b = body(offer_code="WELCOME10")
    resp = await client.post(BASE, b)
    assert resp.json() == {"handled_by": "reap"}
    assert stubs.reap == [b]  # the body as sent
    await _nothing_written()


async def test_an_explicit_reap_choice_goes_to_reap_even_when_a2a_could_take_it(client, stubs):
    resp = await client.post(BASE, body(payment_preference={"rail": "reap"}))
    assert resp.json() == {"handled_by": "reap"} and len(stubs.reap) == 1
    await _nothing_written()


@pytest.mark.parametrize("change", ["not_pilot", "not_proven", "offer_code"])
async def test_an_explicit_a2a_choice_it_cannot_take_is_a_404_never_reap(client, stubs, monkeypatch, change):
    b = body(payment_preference={"rail": "a2a"})
    if change == "not_pilot":
        CALLER.agent_id = "agent_not_in_pilot"
    elif change == "not_proven":
        monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, "")
    else:
        b["offer_code"] = "WELCOME10"
    resp = await client.post(BASE, b)
    assert resp.status_code == 404 and "not_available_on_this_rail" in resp.text
    # The reason is not in the answer: an agent cannot learn the pilot list or the merchant list from it.
    for word in ("pilot", "proven", "offer"):
        assert word not in resp.text
    assert stubs.reap == [] and stubs.catalog == []
    await _nothing_written()


async def test_an_explicit_a2a_choice_records_the_buyer_as_the_decider(client):
    pid = (await client.post(BASE, body(payment_preference={"rail": "a2a"}))).json()["purchase_id"]
    parent = await database.fetch_one("SELECT routing_plan FROM agent_purchases WHERE rail_purchase_id = :p", {"p": pid})
    plan = json.loads(parent["routing_plan"]) if isinstance(parent["routing_plan"], str) else parent["routing_plan"]
    assert plan["basis"] == "buyer_selected_rail" and plan["dropped"] == {"reap": "not_selected"}


# ── the router, pure ─────────────────────────────────────────────────────────────────────────


def test_merchant_entries_are_parsed_strictly_and_folded(monkeypatch):
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV,
                       " Judydoll.com:us ,bad, x.example:USA, :US, y.example: , www.brand.example:SG, bad host:US")
    assert rail_router.proven_merchants() == frozenset({("judydoll.com", "US"), ("brand.example", "SG")})


@pytest.mark.parametrize("raw,total,currency,ok", [
    ("USD:10000", 10000, "USD", True), ("USD:10000", 10001, "USD", False),
    ("USD:10000", 5000, "SGD", False),            # a currency with no cap is refused
    ("usd:10000, SGD:13500", 13000, "SGD", True), ("", 1, "USD", False), ("10000", 1, "USD", False),
    ("USD:0", 1, "USD", False), ("USD:-5", 1, "USD", False), ("USD:abc", 1, "USD", False),
    ("USD:10000", True, "USD", False),
])
def test_the_amount_cap_is_per_currency_and_fails_closed(monkeypatch, raw, total, currency, ok):
    monkeypatch.setenv(rail_router.A2A_RAIL_MAX_TOTAL_MINOR_ENV, raw)
    assert rail_router.amount_ok(total, currency) is ok


def test_the_router_names_every_rail_it_drops():
    choice = rail_router.choose_rail(agent_id="agent_x", merchant_domain=SHOP, market_country="US", item_source="cart_link")
    assert (choice.rail, choice.basis, choice.dropped) == ("reap", "orchestrator_selected_rail", {"a2a": "agent_not_in_pilot"})
    refused = rail_router.choose_rail(agent_id="agent_x", merchant_domain=SHOP, market_country="US",
                                      item_source="cart_link", preferred="a2a")
    assert (refused.rail, refused.refusal) == (None, "agent_not_in_pilot")


# ── review round 1 ───────────────────────────────────────────────────────────────────────────


async def test_dark_while_the_ledger_is_dark_too(client, stubs, monkeypatch):
    monkeypatch.delenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV)
    resp = await client.post(BASE, body())
    assert resp.status_code == 404 and "not_available" in resp.text
    assert stubs.catalog == [] and stubs.reap == []
    await _nothing_written()


@pytest.mark.parametrize("merchants", [f"{SHOP}:US", ""])
async def test_a_money_less_attempt_is_refused_the_same_whatever_the_merchant_lists_say(client, stubs, monkeypatch, merchants):
    monkeypatch.setenv(rail_router.A2A_RAIL_MERCHANTS_ENV, merchants)
    b = body(payment_preference={"rail": "a2a"})
    b.pop("expected_unit_price_minor"), b.pop("expected_currency")
    resp = await client.post(BASE, b)
    assert resp.status_code == 400 and "invalid_request" in resp.text
    await _nothing_written()


@pytest.mark.parametrize("buyer,reason", [
    ({"email": "not an email"}, "invalid_request"),
    ({"shipping_address": {k: v for k, v in ADDRESS.items() if k != "phone"}}, "invalid_address"),
])
async def test_the_buyer_is_admitted_by_the_rails_own_rules_before_any_read(client, stubs, buyer, reason):
    b = body()
    b["buyer"].update(buyer)
    resp = await client.post(BASE, b)
    assert resp.status_code == 400 and reason in resp.text, resp.text
    assert stubs.catalog == [] and stubs.clicks == []
    await _nothing_written()


async def test_a_currency_with_no_cap_is_refused(client, stubs):
    stubs.facts["currency"] = "EUR"
    resp = await client.post(BASE, body(expected_currency="EUR"))
    assert resp.status_code == 409 and "amount_over_limit" in resp.text
    await _nothing_written()


async def test_the_purchasability_gate_applies_when_enforced(client, stubs, monkeypatch):
    import db.merchant_purchasability as purchasability

    async def _not_purchasable(domain, market):
        return False

    monkeypatch.setattr(purchasability, "is_enforcement_enabled", lambda: True)
    monkeypatch.setattr(purchasability, "is_purchasable", _not_purchasable)
    resp = await client.post(BASE, body())
    assert resp.status_code == 409 and "merchant_not_purchasable" in resp.text
    assert stubs.catalog == []
    await _nothing_written()


async def test_a_database_failure_in_the_unit_is_outcome_unknown_and_writes_nothing(client, stubs, monkeypatch):
    async def _fails(**kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(a2a_ledger, "create_purchase", _fails)
    resp = await client.post(BASE, body())
    assert resp.status_code == 503 and "checkout_outcome_unknown" in resp.text
    await _nothing_written()


async def test_a_value_the_ledger_refuses_is_a_refusal_not_a_500(client, stubs, monkeypatch):
    async def _refuses(**kwargs):
        raise ValueError("checkout_url must be on merchant_domain")

    monkeypatch.setattr(a2a_ledger, "create_purchase", _refuses)
    resp = await client.post(BASE, body())
    assert resp.status_code == 409 and "row_not_found" in resp.text
    await _nothing_written()


async def test_a_key_pointing_at_a_missing_a2a_purchase_is_not_found(client, stubs):
    req = unified_routes.CreatePurchaseRequest.model_validate(body())
    request_hash = reap_routes._request_hash(
        merchant_domain=SHOP, product_key="pk_1", variant_key=None, quantity=1, email=EMAIL,
        shipping_address=reap_routes._buyer_address_for_client(req.buyer), return_url=reap_routes._default_return_url(),
        item_source="cart_link", expected_unit_price_minor=2820, expected_currency="USD")
    await database.execute(
        "INSERT INTO reap_agentic_purchase_keys (agent_id, agent_user_ref_hash, idempotency_key, purchase_id, request_hash) "
        "VALUES (:a, :h, 'key-1', 'ap_0123456789abcdef01234567', :r)",
        {"a": AGENT, "h": hash_agent_user_ref(USER_REF), "r": request_hash})
    resp = await client.post(BASE, body())
    assert resp.status_code == 404 and stubs.catalog == [] and await _count("a2a_rail_purchases") == 0


@pytest.fixture
def reap_door_open(monkeypatch):
    """The REAL Reap route's gates open (it is the code under test here, not the stub)."""
    for name, value in (("REAP_AGENTIC_ENABLED", "1"), ("REAP_AGENTIC_CREATE_ENABLED", "1"),
                        ("REAP_AGENTIC_CART_LINK_ENABLED", "1"), ("REAP_AGENTIC_PILOT_SCOPE", "unrestricted"),
                        ("REAP_API_BASE_URL", "https://reap.test"), ("REAP_API_KEY", "reap_test_key")):
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("path", ["/agent/v2/commerce/reap/purchases", "/agent/v2/commerce/reap/purchases/recover"])
async def test_the_reap_door_names_a_purchase_its_key_opened_on_the_other_rail(client, stubs, reap_door_open, path):
    pid = (await client.post(BASE, body())).json()["purchase_id"]
    resp = await client.post(path, body())  # the same key and body, on the old door
    assert resp.status_code == 409 and "opened_on_another_rail" in resp.text, resp.text
    assert pid in resp.text
    assert await _count("a2a_rail_purchases") == 1 and await _count("reap_agentic_purchases") == 0


async def test_the_stored_buyer_is_the_cleaned_one(client):
    b = body()
    b["buyer"]["shipping_address"] = {**ADDRESS, "city": "San\u200bFrancisco"}
    b["buyer"]["email"] = " ada@example.test "
    pid = (await client.post(BASE, b)).json()["purchase_id"]
    row = await a2a_ledger.get_purchase_internal(pid)
    assert row["shipping_address"]["city"] == "SanFrancisco" and row["buyer_email"] == EMAIL



async def test_the_reap_door_losing_a_key_race_to_another_rail_names_the_winner_and_scrubs_its_own_row(
        client, stubs, reap_door_open, monkeypatch):
    """The REAL Reap create, up to its key claim; the key was won by an A2A purchase. Its own row is refused as a
    duplicate with the PII gone, and the answer names the winner (never a 404 or 503 the client could read as
    "not created")."""
    import db.reap_agentic_ledger as reap_ledger

    clicks = []

    async def _click(click):
        clicks.append(click)

    async def _a2a_won(**kwargs):
        return "ap_0123456789abcdef01234567"

    # The buyer identity is the Reap route's own business (its tests own the minting): a minted buyer stands in.
    await database.execute(
        "INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref, consent_version, consented_at) "
        "VALUES ('buyer_race', 'bref_race', 'terms-2026-10', CURRENT_TIMESTAMP)")

    async def _buyer(**kwargs):
        return "buyer_race"

    async def _bref(buyer_id, **kwargs):
        return "bref_race"

    monkeypatch.setattr(reap_routes, "_buyer_id_for", _buyer)
    monkeypatch.setattr(reap_routes, "_reap_buyer_ref", _bref)
    monkeypatch.setattr(reap_routes, "issue_click", _click)
    monkeypatch.setattr(reap_routes, "_claim_idempotency_key", _a2a_won)
    resp = await client.post("/agent/v2/commerce/reap/purchases", body())
    assert resp.status_code == 409 and "opened_on_another_rail" in resp.text, resp.text
    assert "ap_0123456789abcdef01234567" in resp.text
    [row] = await database.fetch_all("SELECT id FROM reap_agentic_purchases")
    reap_row = await reap_ledger.get_purchase_internal(row["id"])
    assert (reap_row["state"], reap_row["refusal_reason"]) == ("refused", "duplicate_idempotency_key")
    assert reap_row["buyer_email"] is None and reap_row["shipping_address"] is None
