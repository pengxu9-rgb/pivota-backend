"""routes/agent_commerce_reap.py — the agent-facing door onto the agentic purchase rail, SQLite.

WHAT IS REAL HERE AND WHAT IS NOT, same split as tests/test_reap_agentic_purchase.py:

  REAL   the routes, the app, the ledger, the schema (built by the SELF-HEAL, because production
         skips db/migrations and the self-heal is therefore the schema that ships), the catalog
         tables (built from the repo's own `metadata`, so a column this route reads is the column
         the catalog actually has), `hash_agent_user_ref`, and every allowlist in
         `services.reap_agentic_client`.

  FAKE   the two auth dependencies, overridden per test so one file can be two agents and three
         end users; and nothing else. There is no fake for the Reap transport here because
         **these routes make no partner call at all** — `start_purchase` is documented as making
         none, and the autouse `_no_network` fixture is the control that proves it: an
         `httpx.AsyncClient` constructed anywhere under a request raises.

NEVER `TestClient`. It hangs against the asyncpg pool; `httpx.ASGITransport` is the house rule.

The Postgres arm is tests/test_agent_commerce_reap_routes_postgres.py, which is not a copy: it
re-runs the parts where the ENGINE is what is under test (migration-226 parity against the
self-heal, the numeric→text price cast, and the ownership conjunct over real SQL).

EVERY GUARD BELOW WAS MUTATED ONE AT A TIME AND CONFIRMED TO KILL AT LEAST ONE TEST; the table is
in the PR body.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.database import IS_POSTGRES, database, metadata  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402
import routes.agent_commerce_reap as routes_reap  # noqa: E402
import services.reap_agentic_purchase as svc  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402

#: IMPORTED AT MODULE LEVEL, NOT INSIDE A FIXTURE. `main` pulls in the billing routes, which
#: construct a Stripe client at import time, which constructs an `httpx.AsyncClient` — so an
#: import under the `_no_network` fixture trips a ban aimed at the routes rather than at the
#: import graph. Importing here also means the mounting test exercises the same app object every
#: request test does.
from main import app  # noqa: E402

#: Captured before `_no_network` can replace the attribute. The HARNESS uses this; application
#: code reaching for `httpx.AsyncClient` during a test still gets the forbidden one.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the agentic purchase routes; the Postgres arm is "
        "tests/test_agent_commerce_reap_routes_postgres.py"
    ),
)

BASE = "/agent/v2/commerce/reap"

AGENT = "agent_reap_routes"
OTHER_AGENT = "agent_reap_routes_other"
USER_REF = "user-ada"
OTHER_USER_REF = "user-grace"

BUYER_ID = "buy_ada"
OTHER_BUYER_ID = "buy_grace"

DOMAIN = "brand.example"
PRODUCT_KEY = "prod::m_brand::shopify::1001"
SKU_KEY = "sku::prod::m_brand::shopify::1001::v1"
SECOND_SKU_KEY = "sku::prod::m_brand::shopify::1001::v2"

EMAIL = "ada@example.test"
ADDRESS = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "phone": "+15550100",
    "addressLine1": "900 Brannan St",
    "city": "San Francisco",
    "country": "US",
    "postalCode": "94103",
}

#: Written out rather than derived from ADDRESS: a test that built its expectation from the same
#: dict it fed in cannot notice a field being dropped from both.
PII_STRINGS = ("ada@example.test", "900 Brannan St", "Lovelace", "+15550100")

CATALOG_TABLES = ("catalog_products", "catalog_skus", "catalog_offers", "catalog_merchants")
RAIL_TABLES = (
    "reap_agentic_purchase_keys",
    "reap_agentic_buyer_refs",
    "reap_agentic_eligibility",
    "reap_agentic_purchases",
    "reap_agentic_enrollments",
)


def _error(resp) -> Optional[str]:
    """The refusal reason, out of the app-wide error envelope.

    `middleware/error_handler.ErrorHandlerMiddleware` re-wraps EVERY 4xx JSON response in the
    house shape (`status` / `error` / `metadata`) and preserves the route's own body verbatim
    under `detail`. So the route writes `{"error": "<reason>"}` and the caller reads it at
    `detail.error` — which is the field docs/reap_agentic_routes.md tells the gateway to read,
    and the one this helper asserts on. Reading `error.message` instead would also work today and
    would stop working the day the middleware gains a code hint for one of these statuses.
    """
    payload = resp.json()
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return detail.get("error")
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return error["details"].get("error")
    return None


def _body(**over) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "merchant_domain": DOMAIN,
        "product_key": PRODUCT_KEY,
        "variant_key": SKU_KEY,
        "quantity": 1,
        "buyer": {"email": EMAIL, "shipping_address": dict(ADDRESS)},
    }
    payload.update(over)
    return payload


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _db():
    """The rail's tables the way PRODUCTION builds them — the self-heal, not a test-only CREATE
    TABLE — and the catalog's tables from the repo's own `metadata`, so the columns this route
    reads are the columns the catalog really has. A hand-written catalog fixture would let a
    renamed column pass here and fail in production."""
    if not database.is_connected:
        await database.connect()
    for table in RAIL_TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()

    import sqlalchemy
    from db.buyer_vault import buyer_identity_links
    from db.catalog import catalog_merchants, catalog_offers, catalog_products, catalog_skus

    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://")
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(
        engine,
        tables=[
            catalog_products,
            catalog_skus,
            catalog_offers,
            catalog_merchants,
            buyer_identity_links,
        ],
        checkfirst=True,
    )
    engine.dispose()

    for table in CATALOG_TABLES + ("buyer_identity_links",):
        await database.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    monkeypatch.delenv("REAP_AGENTIC_RETURN_URL", raising=False)
    monkeypatch.delenv("BUYER_IDENTITY_LINK_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """These routes make NO partner call. This is the control that proves it rather than the
    docstring asserting it: a handler that reached the wire fails instead of hanging."""

    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a route reached the network; this rail's routes make no call")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class _Caller:
    """Who is calling. Mutated per test rather than re-overridden, so the override installed on
    the app is one object and a test that forgets to restore cannot leak an identity."""

    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF

    agent_name = "Reap Routes Test"
    allowed_merchants = None
    session_id = "session_reap_routes"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


CALLER = _Caller()


@pytest.fixture
def client(monkeypatch):
    """A real httpx client over the REAL app, with only the two auth dependencies overridden.

    `httpx.AsyncClient` is forbidden by `_no_network`, so the transport is constructed from the
    UNPATCHED class captured before that fixture replaced it — the ban is on a route reaching the
    wire, not on the test harness existing.
    """
    CALLER.agent_id = AGENT
    CALLER.agent_user_ref = USER_REF

    async def _agent():
        return CALLER

    def _user():
        ref = CALLER.agent_user_ref
        return AgentUserContext(agent_user_ref=ref) if ref else None

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = _user

    class _Harness:
        def __init__(self):
            self._app = app

        async def request(self, method, url, **kwargs):
            transport = httpx.ASGITransport(app=self._app)
            async with _REAL_ASYNC_CLIENT(
                transport=transport, base_url="http://test"
            ) as http:
                return await http.request(method, url, **kwargs)

        async def post(self, url, **kwargs):
            return await self.request("POST", url, **kwargs)

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

    try:
        yield _Harness()
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)



# ── seeds ────────────────────────────────────────────────────────────────────────────────────


async def _seed_catalog(
    *,
    platform: str = "shopify",
    domain: str = DOMAIN,
    price: str = "42.50",
    currency: str = "USD",
    second_variant: bool = False,
    variant_title: Optional[str] = "Standard",
    priced: bool = True,
):
    await database.execute(
        """
        INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,
                                      title, brand, category, product_type, source_domain)
        VALUES (:pk, 'm_brand', :platform, '1001', 'Standard Eau de Parfum', 'Brand',
                'fragrance', 'Fragrance', :domain)
        """,
        {"pk": PRODUCT_KEY, "platform": platform, "domain": domain},
    )
    skus = [(SKU_KEY, variant_title)] + ([(SECOND_SKU_KEY, "Large")] if second_variant else [])
    for sku_key, title in skus:
        await database.execute(
            """
            INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
                                      source_product_id, source_variant_id, title, currency)
            VALUES (:sk, :pk, 'm_brand', :platform, '1001', :vid, :title, :currency)
            """,
            {
                "sk": sku_key,
                "pk": PRODUCT_KEY,
                "platform": platform,
                "vid": sku_key[-2:],
                "title": title,
                "currency": currency,
            },
        )
        if priced:
            await database.execute(
                """
                INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
                                            currency, merchant_effective_price)
                VALUES (:oid, :sk, :pk, 'm_brand', :currency, :price)
                """,
                {
                    "oid": f"off_{sku_key}",
                    "sk": sku_key,
                    "pk": PRODUCT_KEY,
                    "currency": currency,
                    "price": price,
                },
            )


async def _seed_eligibility(
    *,
    domain: str = DOMAIN,
    market: str = "US",
    enabled: bool = True,
    product_key: str = "",
    variant_key: str = "",
    labels=None,
    domains=None,
):
    await database.execute(
        """
        INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key,
                                              market_country, enabled,
                                              accept_variant_labels, also_accept_domains)
        VALUES (:d, :pk, :vk, :m, :enabled, :labels, :domains)
        """,
        {
            "d": domain,
            "pk": product_key,
            "vk": variant_key,
            "m": market,
            "enabled": enabled,
            "labels": json.dumps(labels) if labels else None,
            "domains": json.dumps(domains) if domains else None,
        },
    )


async def _seed_link(*, agent_id: str = AGENT, ref: str = USER_REF, buyer_id: str = BUYER_ID):
    await database.execute(
        """
        INSERT INTO buyer_identity_links (agent_id, agent_user_ref_hash, buyer_id)
        VALUES (:a, :h, :b)
        """,
        {"a": agent_id, "h": hash_agent_user_ref(ref), "b": buyer_id},
    )


async def _seed_all():
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()


async def _purchase_row(purchase_id: str) -> Dict[str, Any]:
    row = await ledger.get_purchase_internal(purchase_id)
    assert row is not None
    return row


# ── the router is mounted ────────────────────────────────────────────────────────────────────


def test_the_router_is_mounted_on_the_real_app():
    """A merged route is not a running route. This asserts the three paths exist on the app
    `main` builds — not that the module imports, and not that a router object has routes on it.
    Registration is a line in main.py that nothing else would notice the absence of."""
    paths = {getattr(route, "path", None) for route in app.routes}
    assert f"{BASE}/purchases" in paths
    assert f"{BASE}/purchases/{{purchase_id}}" in paths


def test_the_mounted_routes_carry_the_methods_the_contract_promises():
    """The path existing is not the verb existing: a POST-only mount would pass the test above
    and 405 every read."""
    methods = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path and path.startswith(BASE):
            methods.setdefault(path, set()).update(getattr(route, "methods", set()) or set())
    assert "POST" in methods[f"{BASE}/purchases"]
    assert "GET" in methods[f"{BASE}/purchases"]
    assert "GET" in methods[f"{BASE}/purchases/{{purchase_id}}"]


# ── the dial: every route 404s while the rail is dark ────────────────────────────────────────


async def test_post_answers_404_while_the_dial_is_off(client, monkeypatch):
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 404
    assert _error(resp) == "not_available_on_this_rail"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_get_answers_404_while_the_dial_is_off(client, monkeypatch):
    await _seed_all()
    created = await client.post(f"{BASE}/purchases", json=_body())
    purchase_id = created.json()["purchase_id"]

    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 404
    assert _error(resp) == "not_available_on_this_rail"


async def test_list_answers_404_while_the_dial_is_off(client, monkeypatch):
    await _seed_all()
    await client.post(f"{BASE}/purchases", json=_body())

    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    resp = await client.get(f"{BASE}/purchases")
    assert resp.status_code == 404
    assert _error(resp) == "not_available_on_this_rail"


async def test_an_unconfigured_client_is_as_absent_as_a_dial_that_is_off(client, monkeypatch):
    """`is_configured()` is half the gate. One unset credential must not read as an outage."""
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    await _seed_all()
    for coro in (
        client.post(f"{BASE}/purchases", json=_body()),
        client.get(f"{BASE}/purchases"),
        client.get(f"{BASE}/purchases/rp_nope"),
    ):
        resp = await coro
        assert resp.status_code == 404
        assert _error(resp) == "not_available_on_this_rail"


async def test_a_dark_rail_answers_404_even_for_a_body_that_would_not_validate(client, monkeypatch):
    """The gate runs BEFORE the body is validated. A 422 here would tell a caller the route is
    there, which is the one thing 404 is for."""
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)
    resp = await client.post(f"{BASE}/purchases", json={"nonsense": True})
    assert resp.status_code == 404


# ── the buyer is required ────────────────────────────────────────────────────────────────────


async def test_every_route_refuses_401_without_an_agent_user(client):
    await _seed_all()
    CALLER.agent_user_ref = None
    for resp in (
        await client.post(f"{BASE}/purchases", json=_body()),
        await client.get(f"{BASE}/purchases"),
        await client.get(f"{BASE}/purchases/rp_nope"),
    ):
        assert resp.status_code == 401
        assert _error(resp) == "agent_user_required"


async def test_a_missing_buyer_link_refuses_rather_than_minting_a_buyer(client):
    await _seed_catalog()
    await _seed_eligibility()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "buyer_unlinked"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 0


# ── the happy path, and where the price comes from ───────────────────────────────────────────


async def test_a_purchase_opens_and_the_response_says_so(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["purchase_id"].startswith("rp_")
    assert payload["status"] == "resolving"
    assert payload["poll_after_seconds"] == svc.POLL_INTERVALS["resolving"]

    row = await _purchase_row(payload["purchase_id"])
    assert row["state"] == "resolving"
    assert row["merchant_domain"] == DOMAIN
    assert row["product_key"] == PRODUCT_KEY
    assert row["variant_key"] == SKU_KEY
    assert row["variant_title"] == "Standard"
    assert row["brand"] == "Brand"
    assert row["market_country"] == "US"
    assert row["agent_id"] == AGENT
    assert row["agent_user_ref_hash"] == hash_agent_user_ref(USER_REF)


async def test_the_price_is_our_catalogs_and_the_request_cannot_move_it(client):
    """THE CENTRAL GUARD. `verify_quote` compares `quantity * our_price_minor` against Reap's
    subtotal EXACTLY. If the caller could set that number, the comparison would be the caller's
    claim against itself and a substituted, repriced variant would sail through."""
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(
            unit_price=1.00,
            price=1.00,
            our_price_minor=100,
            currency="EUR",
            amount_minor=100,
        ),
    )
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250
    assert row["currency"] == "USD"


async def test_the_price_survives_the_numeric_to_text_round_trip(client):
    """A price the binary-float path would mangle. 42.50 as a float is 42.4999999999999964 and
    `major_to_minor` refuses a float outright — so this is also the test that fails if the CAST
    ever comes out of the offer query."""
    await _seed_catalog(price="19.99")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 1999


async def test_a_click_id_is_minted_and_recorded(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    row = await _purchase_row(resp.json()["purchase_id"])
    assert str(row["click_id"] or "").startswith("clk_")
    assert row["click_id"] not in (None, "")


async def test_two_purchases_get_different_click_ids(client):
    """A click id shared between purchases is not an attribution join, it is a collision — and
    this rail has no other join between a Reap checkout and the session that started it."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body())
    second = await client.post(f"{BASE}/purchases", json=_body())
    a = (await _purchase_row(first.json()["purchase_id"]))["click_id"]
    b = (await _purchase_row(second.json()["purchase_id"]))["click_id"]
    assert a != b


# ── eligibility ──────────────────────────────────────────────────────────────────────────────


async def test_a_merchant_with_no_eligibility_row_is_refused(client):
    await _seed_catalog()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_a_disabled_eligibility_row_is_refused(client):
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility(enabled=False)
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_a_buyer_in_another_market_is_refused(client):
    """DOMESTIC ONLY. The merchant is enabled — for SG. The buyer ships to US."""
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility(market="SG")
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_the_market_comes_from_the_shipping_country_not_from_the_caller(client):
    """Two eligibility rows, two markets. The one that applies is the one the parcel goes to.

    The catalogue is seeded in CAD because the parcel goes to Canada: the market a purchase binds
    and the currency its price is in are the same decision, and a CAD buyer against a USD offer is
    `row_currency_mismatch` by design.
    """
    await _seed_catalog(currency="CAD")
    await _seed_link()
    await _seed_eligibility(market="US")
    await _seed_eligibility(market="CA")
    address = dict(ADDRESS, country="CA")
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(buyer={"email": EMAIL, "shipping_address": address}, market_country="US"),
    )
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["market_country"] == "CA"


async def test_an_eligibility_row_for_another_domain_does_not_enable_this_one(client):
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility(domain="someone-else.example")
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


async def test_per_row_aliases_reach_the_purchase_row(client):
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility()
    await _seed_eligibility(
        product_key=PRODUCT_KEY,
        labels=["Standard 50ml"],
        domains=["shop.brand.example"],
    )
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert ledger._decode_json(row["accept_variant_labels"]) == ["Standard 50ml"]
    assert ledger._decode_json(row["also_accept_domains"]) == ["shop.brand.example"]


async def test_an_override_pinned_to_another_variant_does_not_leak_onto_this_one(client):
    """An accepted label names ONE object. A variant-pinned override that applied to its siblings
    would vouch for a label nobody vouched for."""
    await _seed_catalog(second_variant=True)
    await _seed_link()
    await _seed_eligibility()
    await _seed_eligibility(
        product_key=PRODUCT_KEY, variant_key=SECOND_SKU_KEY, labels=["Large 100ml"]
    )
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["accept_variant_labels"] in (None, "null")


async def test_a_product_override_alone_cannot_make_a_merchant_eligible(client):
    """`enabled` is read from the MERCHANT row only. A per-product exception that granted
    eligibility would be a way to turn a merchant on without turning it on."""
    await _seed_catalog()
    await _seed_link()
    await _seed_eligibility(product_key=PRODUCT_KEY, labels=["Standard 50ml"])
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "merchant_not_eligible"


# ── our catalog row ──────────────────────────────────────────────────────────────────────────


async def test_a_product_that_does_not_exist_is_refused(client):
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_a_product_belonging_to_another_domain_answers_row_not_found(client):
    """Not `merchant_not_eligible`, and certainly not a purchase: the domain is a CONJUNCT on the
    product read, so naming an enabled merchant and somebody else's product finds nothing — the
    same answer as a key that does not exist, so this cannot enumerate another catalogue."""
    await _seed_catalog(domain="someone-else.example")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_a_non_shopify_row_is_refused(client):
    await _seed_catalog(platform="external_seed")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_not_shopify"


async def test_an_unpriced_row_is_refused(client):
    await _seed_catalog(priced=False)
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_unpriced"


async def test_a_variant_key_belonging_to_another_product_is_refused(client):
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body(variant_key="sku::other::v1"))
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_no_variant_key_resolves_a_sole_variant(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(variant_key=None))
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["variant_key"] == SKU_KEY


async def test_no_variant_key_on_a_multi_variant_product_is_refused(client):
    await _seed_catalog(second_variant=True)
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body(variant_key=None))
    assert resp.status_code == 409
    assert _error(resp) == "row_not_found"


async def test_a_titleless_sole_variant_is_declared_rather_than_named(client):
    """`declared_single_variant` is carried as the ABSENCE of a variant_title — there is no column
    for the flag, and `start_purchase` refuses the inconsistent pair.

    The seed is an EMPTY title, not a NULL one: `catalog_skus.title` is `NOT NULL`, so a
    titleless variant in this catalogue is spelled `''`. That is the shape the route has to
    recognise, and a fixture that used NULL would be testing a row the catalog cannot hold.
    """
    await _seed_catalog(variant_title="")
    await _seed_eligibility()
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["variant_title"] is None


# ── the buyer ref ────────────────────────────────────────────────────────────────────────────


async def test_the_buyer_ref_is_minted_once_and_reused(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body())
    second = await client.post(f"{BASE}/purchases", json=_body())
    a = (await _purchase_row(first.json()["purchase_id"]))["buyer_ref"]
    b = (await _purchase_row(second.json()["purchase_id"]))["buyer_ref"]
    assert a == b
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_buyer_refs") == 1


async def test_the_buyer_ref_is_not_the_buyer_id_and_not_the_agent_ref(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    buyer_ref = (await _purchase_row(resp.json()["purchase_id"]))["buyer_ref"]
    assert buyer_ref not in (BUYER_ID, USER_REF, hash_agent_user_ref(USER_REF), AGENT)
    assert BUYER_ID not in buyer_ref
    assert len(buyer_ref) >= 20


async def test_two_buyers_get_two_refs(client):
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(ref=OTHER_USER_REF, buyer_id=OTHER_BUYER_ID)

    first = await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_user_ref = OTHER_USER_REF
    second = await client.post(f"{BASE}/purchases", json=_body())

    a = (await _purchase_row(first.json()["purchase_id"]))["buyer_ref"]
    b = (await _purchase_row(second.json()["purchase_id"]))["buyer_ref"]
    assert a != b


async def test_one_buyer_reached_through_two_agents_keeps_one_ref(client):
    """An enrollment is a CARD. Scoping the ref to an agent would give one human two cards."""
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(agent_id=OTHER_AGENT, ref=OTHER_USER_REF, buyer_id=BUYER_ID)

    first = await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_id = OTHER_AGENT
    CALLER.agent_user_ref = OTHER_USER_REF
    second = await client.post(f"{BASE}/purchases", json=_body())

    a = (await _purchase_row(first.json()["purchase_id"]))["buyer_ref"]
    b = (await _purchase_row(second.json()["purchase_id"]))["buyer_ref"]
    assert a == b


# ── the return url ───────────────────────────────────────────────────────────────────────────


async def test_a_return_url_on_a_host_that_is_not_ours_is_refused(client):
    """Reap sends a buyer's BROWSER here after they have entered a card. A caller-supplied host
    would turn this into an open redirector with a payment page in front of it."""
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(return_url="https://evil.example/collect")
    )
    assert resp.status_code == 400
    assert _error(resp) == "invalid_return_url"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_a_return_url_with_userinfo_is_refused(client):
    """`https://agent.pivota.cc@evil.example/` has a HOSTNAME of evil.example."""
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases", json=_body(return_url="https://agent.pivota.cc@evil.example/")
    )
    assert resp.status_code == 400
    assert _error(resp) == "invalid_return_url"


async def test_the_default_return_url_is_used_and_is_allowlisted(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(return_url=None))
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["return_url"].startswith("https://agent.pivota.cc/")


async def test_an_operator_configured_return_url_is_validated_too(client, monkeypatch):
    """The env var is an override, not an exemption."""
    monkeypatch.setenv("REAP_AGENTIC_RETURN_URL", "https://evil.example/collect")
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(return_url=None))
    assert resp.status_code == 400
    assert _error(resp) == "invalid_return_url"


# ── idempotency ──────────────────────────────────────────────────────────────────────────────


async def test_the_same_idempotency_key_returns_the_same_purchase(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    assert first.status_code == second.status_code == 202
    assert first.json()["purchase_id"] == second.json()["purchase_id"]
    assert await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_purchases WHERE state = 'resolving'"
    ) == 1


async def test_a_different_idempotency_key_starts_a_new_purchase(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-2"))
    assert first.json()["purchase_id"] != second.json()["purchase_id"]


async def test_another_buyers_idempotency_key_is_not_this_buyers(client):
    """The key is scoped to the OWNERSHIP PAIR. Two end users of one agent will pick the same
    key, and one replaying into the other's purchase is a cross-buyer read."""
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(ref=OTHER_USER_REF, buyer_id=OTHER_BUYER_ID)

    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    CALLER.agent_user_ref = OTHER_USER_REF
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    assert first.json()["purchase_id"] != second.json()["purchase_id"]


async def test_a_replay_reports_the_purchases_real_state(client):
    """Not a hardcoded 'resolving': by the time a caller retries the row may have moved, and the
    status field is the one the caller polls on."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    purchase_id = first.json()["purchase_id"]
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")

    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    assert second.json()["purchase_id"] == purchase_id
    assert second.json()["status"] == "quoting"


async def test_an_expired_idempotency_key_starts_a_new_purchase(client):
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    await database.execute(
        "UPDATE reap_agentic_purchase_keys SET created_at = :old",
        {"old": datetime.now(timezone.utc) - timedelta(hours=48)},
    )
    second = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    assert second.json()["purchase_id"] != first.json()["purchase_id"]


# ── ownership ────────────────────────────────────────────────────────────────────────────────


async def test_a_buyer_reads_their_own_purchase(client):
    await _seed_all()
    created = await client.post(f"{BASE}/purchases", json=_body())
    purchase_id = created.json()["purchase_id"]

    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == purchase_id
    assert resp.json()["state"] == "resolving"


async def test_another_agent_cannot_read_this_purchase(client):
    """THE AGENT CONJUNCT. 404, not 403: a 403 confirms the id exists."""
    await _seed_all()
    created = await client.post(f"{BASE}/purchases", json=_body())
    purchase_id = created.json()["purchase_id"]

    CALLER.agent_id = OTHER_AGENT
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 404
    assert _error(resp) == "purchase_not_found"


async def test_another_end_user_of_the_same_agent_cannot_read_this_purchase(client):
    """THE USER-REF CONJUNCT — the one an agent-only check would miss entirely. Same agent, same
    API key, different human."""
    await _seed_all()
    created = await client.post(f"{BASE}/purchases", json=_body())
    purchase_id = created.json()["purchase_id"]

    CALLER.agent_user_ref = OTHER_USER_REF
    resp = await client.get(f"{BASE}/purchases/{purchase_id}")
    assert resp.status_code == 404
    assert _error(resp) == "purchase_not_found"


async def test_a_purchase_that_does_not_exist_answers_the_same_as_one_that_is_not_yours(client):
    await _seed_all()
    resp = await client.get(f"{BASE}/purchases/rp_definitely_not_a_real_id")
    assert resp.status_code == 404
    assert _error(resp) == "purchase_not_found"


async def test_the_list_shows_only_this_buyers_purchases(client):
    """A get that leaks needs an id guessed first. A list that leaks hands the set over."""
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(ref=OTHER_USER_REF, buyer_id=OTHER_BUYER_ID)

    mine = await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_user_ref = OTHER_USER_REF
    theirs = await client.post(f"{BASE}/purchases", json=_body())

    CALLER.agent_user_ref = USER_REF
    resp = await client.get(f"{BASE}/purchases")
    ids = [item["id"] for item in resp.json()["purchases"]]
    assert ids == [mine.json()["purchase_id"]]
    assert theirs.json()["purchase_id"] not in ids


async def test_the_list_shows_only_this_agents_purchases(client):
    await _seed_catalog()
    await _seed_eligibility()
    await _seed_link()
    await _seed_link(agent_id=OTHER_AGENT, ref=USER_REF, buyer_id=OTHER_BUYER_ID)

    mine = await client.post(f"{BASE}/purchases", json=_body())
    CALLER.agent_id = OTHER_AGENT
    theirs = await client.post(f"{BASE}/purchases", json=_body())

    CALLER.agent_id = AGENT
    resp = await client.get(f"{BASE}/purchases")
    ids = [item["id"] for item in resp.json()["purchases"]]
    assert ids == [mine.json()["purchase_id"]]
    assert theirs.json()["purchase_id"] not in ids


async def test_the_list_is_capped(client):
    await _seed_all()
    for _ in range(3):
        await client.post(f"{BASE}/purchases", json=_body())
    resp = await client.get(f"{BASE}/purchases?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["purchases"]) == 2
    assert resp.json()["limit"] == 2

    # FastAPI's own `le=` refusal, which the error envelope rewrites from 422 to 400 — the same
    # rewrite that is the reason this module's own refusals answer 400 in the first place.
    over = await client.get(f"{BASE}/purchases?limit=99999")
    assert over.status_code == 400


# ── what the read exposes, and what it must not ──────────────────────────────────────────────


async def _set(purchase_id: str, **values):
    sets = ", ".join(f"{key} = :{key}" for key in values)
    await database.execute(
        f"UPDATE reap_agentic_purchases SET {sets} WHERE id = :id",
        dict(values, id=purchase_id),
    )


async def _open(client) -> str:
    await _seed_all()
    created = await client.post(f"{BASE}/purchases", json=_body())
    assert created.status_code == 202
    return created.json()["purchase_id"]


async def test_the_read_carries_no_pii_and_no_identity(client):
    purchase_id = await _open(client)
    for resp in (
        await client.get(f"{BASE}/purchases/{purchase_id}"),
        await client.get(f"{BASE}/purchases"),
    ):
        text = resp.text
        for secret in PII_STRINGS:
            assert secret not in text
        for identity in (BUYER_ID, USER_REF, hash_agent_user_ref(USER_REF), AGENT):
            assert identity not in text


async def test_the_read_carries_no_reap_evidence_ids(client):
    """`reap_product_id`, `reap_variant_id`, `reap_quote_id` and `reap_checkout_id` are EVIDENCE.
    Handing them out would read as identity downstream, and Reap's ids change under a
    substitution."""
    purchase_id = await _open(client)
    await _set(
        purchase_id,
        reap_product_id="prd_x",
        reap_variant_id="var_x",
        reap_quote_id="q_x",
        reap_checkout_id="chk_x",
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "reap_product_id" not in body
    assert "reap_variant_id" not in body
    assert "reap_quote_id" not in body
    assert "reap_checkout_id" not in body
    assert "buyer_ref" not in body
    assert "enrollment_id" not in body
    assert "click_id" not in body
    assert "return_url" not in body


async def test_the_totals_are_one_object_with_one_spelling_each(client):
    purchase_id = await _open(client)
    await _set(purchase_id, quoted_total_minor=4500, shipping_minor=100, tax_minor=150)
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["totals"] == {
        "currency": "USD",
        "our_price_minor": 4250,
        "quoted_total_minor": 4500,
        "final_total_minor": None,
        "shipping_minor": 100,
        "tax_minor": 150,
    }
    for key in ("currency", "our_price_minor", "quoted_total_minor", "tax_minor"):
        assert key not in body


async def test_a_hosted_url_is_shown_while_the_buyer_needs_it(client):
    purchase_id = await _open(client)
    await ledger.transition(
        purchase_id,
        from_states=("resolving",),
        to_state="needs_enrollment",
        hosted_url="https://pay.prava.space/enroll/abc",
        hosted_url_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["hosted_url"] == "https://pay.prava.space/enroll/abc"
    assert body["hosted_url_expires_at"]
    assert body["poll_after_seconds"] == svc.POLL_INTERVALS["needs_enrollment"]


async def test_a_hosted_url_on_a_host_we_do_not_vouch_for_is_never_forwarded(client):
    """DEFENCE IN DEPTH. The client refuses this URL on the way IN; this asserts the read refuses
    it too. `evilprava.space` passes a naive `endswith`, and this is a page where somebody types a
    card number."""
    purchase_id = await _open(client)
    await ledger.transition(
        purchase_id, from_states=("resolving",), to_state="needs_enrollment"
    )
    await _set(purchase_id, hosted_url="https://evilprava.space/enroll/steal")
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body
    assert "evilprava" not in (await client.get(f"{BASE}/purchases/{purchase_id}")).text


async def test_an_http_hosted_url_is_never_forwarded(client):
    purchase_id = await _open(client)
    await ledger.transition(
        purchase_id, from_states=("resolving",), to_state="needs_enrollment"
    )
    await _set(purchase_id, hosted_url="http://pay.prava.space/enroll/abc")
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body


async def test_an_expired_hosted_url_is_not_shown(client):
    purchase_id = await _open(client)
    await ledger.transition(
        purchase_id,
        from_states=("resolving",),
        to_state="needs_enrollment",
        hosted_url="https://pay.prava.space/enroll/abc",
        hosted_url_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body
    assert "hosted_url_expires_at" not in body


async def test_a_hosted_url_is_not_shown_on_a_state_that_has_no_page(client):
    """A completed purchase whose approval link still worked would be a second charge waiting to
    happen."""
    purchase_id = await _open(client)
    await ledger.transition(
        purchase_id,
        from_states=("resolving",),
        to_state="needs_enrollment",
        hosted_url="https://pay.prava.space/enroll/abc",
    )
    await ledger.transition(purchase_id, from_states=("needs_enrollment",), to_state="quoting")
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert "hosted_url" not in body


async def test_an_order_reference_appears_only_when_the_purchase_is_complete(client):
    purchase_id = await _open(client)
    await ledger.transition(purchase_id, from_states=("resolving",), to_state="quoting")
    await ledger.transition(
        purchase_id, from_states=("quoting",), to_state="awaiting_approval", reap_order_id="ord_9"
    )
    assert "order_reference" not in (
        await client.get(f"{BASE}/purchases/{purchase_id}")
    ).json()

    await ledger.transition(
        purchase_id,
        from_states=("awaiting_approval",),
        to_state="completed",
        reap_order_id="ord_9",
        final_total_minor=4500,
    )
    body = (await client.get(f"{BASE}/purchases/{purchase_id}")).json()
    assert body["order_reference"] == "ord_9"
    assert body["totals"]["final_total_minor"] == 4500
    assert body["poll_after_seconds"] is None


# ── validation and PII in logs ───────────────────────────────────────────────────────────────


async def test_a_quantity_over_the_cap_is_refused(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(quantity=svc.MAX_QUANTITY + 1))
    assert resp.status_code == 400
    assert _error(resp) == "invalid_request"


async def test_an_incomplete_address_is_refused_without_naming_a_value(client):
    await _seed_all()
    address = {key: value for key, value in ADDRESS.items() if key != "phone"}
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer={"email": EMAIL, "shipping_address": address})
    )
    assert resp.status_code == 400
    assert _error(resp) == "invalid_address"
    for secret in PII_STRINGS:
        assert secret not in resp.text


async def test_the_buyer_name_and_phone_are_fallbacks_not_overrides(client):
    await _seed_all()
    address = {
        key: value for key, value in ADDRESS.items() if key not in ("firstName", "lastName", "phone")
    }
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(
            buyer={
                "email": EMAIL,
                "name": "Ada Lovelace",
                "phone": "+15550100",
                "shipping_address": address,
            }
        ),
    )
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    stored = ledger._decode_json(row["shipping_address"])
    assert stored["firstName"] == "Ada"
    assert stored["lastName"] == "Lovelace"
    assert stored["phone"] == "+15550100"


async def test_an_address_that_names_a_recipient_wins_over_the_buyer_name(client):
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(buyer={"email": EMAIL, "name": "Somebody Else", "shipping_address": dict(ADDRESS)}),
    )
    assert resp.status_code == 202
    stored = ledger._decode_json((await _purchase_row(resp.json()["purchase_id"]))["shipping_address"])
    assert stored["firstName"] == "Ada"
    assert stored["lastName"] == "Lovelace"


async def test_a_bad_email_is_refused(client):
    await _seed_all()
    resp = await client.post(
        f"{BASE}/purchases",
        json=_body(buyer={"email": "not-an-email", "shipping_address": dict(ADDRESS)}),
    )
    assert resp.status_code == 400
    assert _error(resp) == "invalid_request"


#: Loggers this repo does not own. `aiosqlite` and `databases` log every statement WITH ITS BOUND
#: PARAMETERS at DEBUG, and on this table those parameters are the buyer's address and email — so
#: an unfiltered scan of `caplog` is a scan of the SQLite driver's debug output, not of our code.
#: That is a real property of the test harness and not of production (asyncpg does not do it, and
#: nothing runs these loggers at DEBUG in prod), and folding it into the assertion would make this
#: test permanently red for a reason nobody can fix here. The claim being made is "OUR code does
#: not log the buyer", so the detector looks at our code's records.
_DRIVER_LOGGERS = ("aiosqlite", "databases", "sqlalchemy", "httpx", "httpcore", "asyncio")


def _ours(record: logging.LogRecord) -> bool:
    return not record.name.startswith(_DRIVER_LOGGERS)


async def test_nothing_on_this_path_logs_the_buyer(client, caplog):
    await _seed_all()
    with caplog.at_level(logging.DEBUG):
        created = await client.post(f"{BASE}/purchases", json=_body())
        await client.get(f"{BASE}/purchases/{created.json()['purchase_id']}")
        await client.get(f"{BASE}/purchases")

    ours = [record for record in caplog.records if _ours(record)]
    # THE CONTROL. An absence assertion passes when the mechanism is absent too: if the filter
    # above ever matched everything, the loop below would be checking an empty string and would
    # go on passing after somebody started logging the buyer's email.
    assert ours, "precondition: this path logged something of ours to look at"
    assert any(
        "reap_agentic" in record.getMessage() for record in ours
    ), "precondition: the rail's own log line is among the records being scanned"

    logged = "\n".join(record.getMessage() for record in ours)
    for secret in PII_STRINGS:
        assert secret not in logged


async def test_an_extra_address_key_cannot_widen_what_is_stored(client):
    """The model drops it and `build_shipping_address` drops it again. A caller cannot add a field
    to what reaches a third party."""
    await _seed_all()
    address = dict(ADDRESS, ssn="000-00-0000", note="deliver to the neighbour")
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer={"email": EMAIL, "shipping_address": address})
    )
    assert resp.status_code == 202
    stored = ledger._decode_json((await _purchase_row(resp.json()["purchase_id"]))["shipping_address"])
    assert "ssn" not in stored
    assert "note" not in stored


# ── the module's own shape ───────────────────────────────────────────────────────────────────


def test_the_route_never_reads_the_unscoped_ledger_read():
    """`get_purchase_internal` is the UNSCOPED, UNREDACTED read. It is named `_internal` precisely
    because it is the one a route author reaches for, and the difference between it and
    `get_purchase_for_owner` is the whole access-control story on this rail. A grep, because the
    failure is a call that compiles."""
    source = Path(routes_reap.__file__).read_text(encoding="utf-8")
    assert "get_purchase_internal" not in source


def test_every_refusal_reason_the_module_raises_has_a_status():
    """A reason with no entry falls to the 409 default, which is a defensible answer but not a
    chosen one. This pins that every reason this module writes was actually decided on."""
    source = Path(routes_reap.__file__).read_text(encoding="utf-8")
    import re

    raised = set(re.findall(r'PurchaseRefused\(\s*"([a-z_]+)"', source))
    assert raised, "precondition: the module raises some refusals"
    assert raised <= set(routes_reap._REFUSAL_STATUS), (
        f"no status decided for: {sorted(raised - set(routes_reap._REFUSAL_STATUS))}"
    )


# ── the offer seller ─────────────────────────────────────────────────────────────────────────


async def _seed_competitor_offer(*, price: str = "1.00", merchant_id: str = "m_competitor"):
    """A SECOND SELLER's offer on the SAME sku, cheaper. `catalog_offers.merchant_id` is the offer
    seller and is a different column from `catalog_products.merchant_id`."""
    await database.execute(
        """
        INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
                                    currency, merchant_effective_price)
        VALUES (:oid, :sk, :pk, :mid, 'USD', :price)
        """,
        {
            "oid": f"off_{merchant_id}",
            "sk": SKU_KEY,
            "pk": PRODUCT_KEY,
            "mid": merchant_id,
            "price": price,
        },
    )


async def test_a_competing_sellers_cheaper_offer_is_not_our_price(client):
    """THE P0. Without `o.merchant_id = p.merchant_id` this query was `ORDER BY price ASC LIMIT 1`
    across every seller on the sku, so a competitor's 1.00 became `our_price_minor`. The buyer
    would then be sent through card enrolment and refused `price_changed` at the quote — for a
    price change that never happened."""
    await _seed_all()
    await _seed_competitor_offer(price="1.00")

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250, (
        "the price came from a seller who is not the merchant this purchase is with"
    )


async def test_a_stale_mirror_copy_of_the_same_sku_is_not_our_price(client):
    """The other shape of the same defect: one sku carrying two offer rows because the product was
    both mirrored into the crawl lane and synced by the merchant. The cheaper copy is often the
    stale one, and `ORDER BY price ASC` picks it every time."""
    await _seed_all()
    await _seed_competitor_offer(price="28.20", merchant_id="m_brand_external_seed")

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202
    row = await _purchase_row(resp.json()["purchase_id"])
    assert row["our_price_minor"] == 4250


async def test_a_merchant_with_no_offer_of_its_own_is_unpriced(client):
    """Not "somebody else will sell it to you". The eligible merchant has no offer on this sku, so
    there is no price we are entitled to commit to."""
    await _seed_catalog(priced=False)
    await _seed_eligibility()
    await _seed_link()
    await _seed_competitor_offer(price="9.99")

    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_unpriced"


# ── the market's currency ────────────────────────────────────────────────────────────────────


async def test_an_offer_priced_in_another_currency_is_refused(client):
    """A US buyer against a EUR-priced offer. Every other check here passes; the divergence would
    surface at the quote as `price_changed`, which names the wrong cause and arrives after the
    buyer has entered a card."""
    await _seed_catalog(currency="EUR")
    await _seed_eligibility(market="US")
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 409
    assert _error(resp) == "row_currency_mismatch"


async def test_a_market_the_currency_map_does_not_know_fails_closed(client):
    """Adding a market is a one-line change in `_MARKET_CURRENCY`. Until somebody makes it, the
    rail refuses rather than skipping the check — the same direction every other decision here
    takes."""
    assert "ZZ" not in routes_reap._MARKET_CURRENCY, "precondition: ZZ is not a known market"
    await _seed_catalog(currency="USD")
    await _seed_eligibility(market="ZZ")
    await _seed_link()
    address = dict(ADDRESS, country="ZZ")
    resp = await client.post(
        f"{BASE}/purchases", json=_body(buyer={"email": EMAIL, "shipping_address": address})
    )
    assert resp.status_code == 409
    assert _error(resp) == "row_currency_mismatch"


async def test_a_matching_market_currency_is_accepted(client):
    """The control: an absence assertion passes when the mechanism is absent too."""
    await _seed_catalog(currency="USD")
    await _seed_eligibility(market="US")
    await _seed_link()
    resp = await client.post(f"{BASE}/purchases", json=_body())
    assert resp.status_code == 202


# ── unprintable characters in the route-owned identifiers ────────────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("merchant_domain", "brand.example\x00"),
        ("product_key", PRODUCT_KEY + "\x00"),
        ("variant_key", SKU_KEY + "\x00"),
        ("idempotency_key", "k-1\x00"),
    ],
)
async def test_a_nul_byte_in_an_identifier_is_refused(client, field, value):
    """REFUSED, not stripped. These four are matched EXACTLY against storage, so removing a
    character would not sanitise the value — it would turn it into a different identifier. On
    Postgres an unstripped NUL reached an asyncpg bind and raised
    `CharacterNotInRepertoireError`, which is not a `PurchaseRefused` and became a 500; see the
    Postgres arm, which is the only one that can observe that."""
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(**{field: value}))
    assert resp.status_code == 400
    assert _error(resp) == "invalid_request"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_an_overlong_identifier_is_refused_rather_than_truncated(client):
    await _seed_all()
    resp = await client.post(f"{BASE}/purchases", json=_body(merchant_domain="a" * 300))
    assert resp.status_code == 400
    assert _error(resp) == "invalid_request"


# ── idempotency is a key PLUS the request it was used for ────────────────────────────────────


async def test_the_same_key_on_a_different_body_is_a_conflict(client):
    """A key alone answers "have I seen this key?", and every caller reads the 202 as "your
    request was carried out". A client that derives keys from a session or a cart id WILL reuse
    one across two bodies, and the old code answered 202 naming a purchase of something else."""
    await _seed_catalog(second_variant=True)
    await _seed_eligibility()
    await _seed_link()

    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    assert first.status_code == 202

    second = await client.post(
        f"{BASE}/purchases", json=_body(idempotency_key="k-1", quantity=3)
    )
    assert second.status_code == 409
    assert _error(second) == "idempotency_conflict"

    third = await client.post(
        f"{BASE}/purchases", json=_body(idempotency_key="k-1", variant_key=SECOND_SKU_KEY)
    )
    assert _error(third) == "idempotency_conflict"

    address = dict(ADDRESS, addressLine1="1 Other St")
    fourth = await client.post(
        f"{BASE}/purchases",
        json=_body(idempotency_key="k-1", buyer={"email": EMAIL, "shipping_address": address}),
    )
    assert _error(fourth) == "idempotency_conflict"

    # ...and none of them opened anything.
    assert await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_purchases WHERE state = 'resolving'"
    ) == 1


async def test_a_cosmetically_different_but_identical_request_still_replays(client):
    """The hash is built from the NORMALISED values, so a retry that differs only in the casing of
    a domain — or supplies the recipient through `buyer.name` instead of the address, resolving to
    the same recipient — is the same request and must replay rather than conflict."""
    await _seed_all()
    first = await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))

    address = {
        key: value for key, value in ADDRESS.items() if key not in ("firstName", "lastName")
    }
    second = await client.post(
        f"{BASE}/purchases",
        json=_body(
            idempotency_key="k-1",
            merchant_domain=DOMAIN.upper(),
            buyer={"email": EMAIL, "name": "Ada Lovelace", "shipping_address": address},
        ),
    )
    assert second.status_code == 202
    assert second.json()["purchase_id"] == first.json()["purchase_id"]


async def test_the_recorded_hash_is_what_the_conflict_compares(client):
    await _seed_all()
    await client.post(f"{BASE}/purchases", json=_body(idempotency_key="k-1"))
    stored = await database.fetch_val(
        "SELECT request_hash FROM reap_agentic_purchase_keys WHERE idempotency_key = 'k-1'"
    )
    assert isinstance(stored, str) and len(stored) == 64


# ── the dark rail is not probeable through a validator ───────────────────────────────────────


async def test_a_dark_rail_answers_404_for_every_shape_of_bad_input(client, monkeypatch):
    """THE PROBE. A parameter in the handler signature is validated by FastAPI BEFORE the handler
    runs, so a malformed body used to answer 400 with a field message while every valid request
    answered 404 — a working existence oracle for a feature that is supposed to be absent. Every
    answer below must be byte-for-byte the one a well-formed request gets."""
    await _seed_all()
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)

    baseline = await client.post(f"{BASE}/purchases", json=_body())
    assert baseline.status_code == 404
    expected = _error(baseline)

    probes = [
        await client.post(f"{BASE}/purchases", json=[1, 2]),
        await client.post(f"{BASE}/purchases", json={"nonsense": True}),
        await client.post(f"{BASE}/purchases", content=b"not json at all"),
        await client.post(
            f"{BASE}/purchases",
            content=b"merchant_domain=brand.example",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ),
        await client.get(f"{BASE}/purchases?limit=500"),
        await client.get(f"{BASE}/purchases?limit=not-a-number"),
    ]
    for probe in probes:
        assert probe.status_code == 404, probe.text
        assert _error(probe) == expected == "not_available_on_this_rail"


async def test_an_over_large_limit_is_refused_when_the_rail_is_live(client):
    """The control for the probe test: with the dial ON, the bound is still enforced — and as a
    refusal rather than a silent clamp, because a caller that asked for 500 and got 20 has been
    handed a page it does not know is partial."""
    await _seed_all()
    for resp in (
        await client.get(f"{BASE}/purchases?limit=500"),
        await client.get(f"{BASE}/purchases?limit=0"),
        await client.get(f"{BASE}/purchases?limit=not-a-number"),
    ):
        assert resp.status_code == 400
        assert _error(resp) == "invalid_request"


async def test_a_malformed_body_is_refused_when_the_rail_is_live(client):
    await _seed_all()
    for resp in (
        await client.post(f"{BASE}/purchases", json=[1, 2]),
        await client.post(f"{BASE}/purchases", content=b"not json at all"),
    ):
        assert resp.status_code == 400
        assert _error(resp) == "invalid_request"


# ── the self-heal survives its own worst statement ───────────────────────────────────────────


async def test_a_failing_unique_index_does_not_starve_the_keys_table():
    """ONE try PER STATEMENT. The block's own comment names `uq_reap_agentic_buyer_refs_ref` as
    the statement that fails in the field — on a database already holding two buyers with one ref
    — and `reap_agentic_purchase_keys` used to sit after it in the SAME try. Production has no
    other route to that table, so the raise would have starved it forever on exactly the databases
    that were already unwell."""
    for table in RAIL_TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    # A buyer-refs table WITHOUT its unique index, holding the rows that make the index
    # impossible. This is the shape the self-heal has to survive.
    await database.execute(
        """
        CREATE TABLE reap_agentic_buyer_refs (
            buyer_id VARCHAR(50) PRIMARY KEY,
            reap_buyer_ref VARCHAR(128) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for buyer in ("b1", "b2"):
        await database.execute(
            "INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref) "
            "VALUES (:b, 'collides')",
            {"b": buyer},
        )

    await ensure_required_schema_light()

    # The index could not be created — that is the precondition, not the failure.
    assert await database.fetch_val(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'index' AND name = 'uq_reap_agentic_buyer_refs_ref'"
    ) == 0, "precondition: the unique index really is impossible on this data"

    # ...and every other statement in the block still landed.
    for table in ("reap_agentic_eligibility", "reap_agentic_purchase_keys"):
        assert await database.fetch_val(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = :t",
            {"t": table},
        ) == 1, f"{table} was starved by the failing index"
