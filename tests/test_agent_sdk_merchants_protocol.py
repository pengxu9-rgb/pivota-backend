"""GET /agent/v1/merchants, default scope `available`: the protocol merchant directory.

A partner buys from merchants reached through a checkout protocol, none of which is integrated
through the merchant portal. The directory (services/agent_merchant_directory.py) lists each
(merchant domain, market) with at least one route a purchase can take, using the purchase
gates' own functions for every fact. These cases seed the three tables those gates read and
drive the real route on a minimal app (never `main`, never TestClient).

Only rows whose domain carries this run's suffix are asserted on: the Postgres dialect gate
shares one database across files, so other rows may be present.
"""

from __future__ import annotations

import os
import sys
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database  # noqa: E402

RUN = uuid.uuid4().hex[:8]
AGENT = f"agent_mp_{RUN}"


def _d(label: str) -> str:
    return f"mp-{label}-{RUN}.example.com"


D_CARD = _d("card")            # fresh ELIGIBLE + card: storefront, card verified
D_NOCARD = _d("nocard")        # fresh NO_CARD_PAYMENT: storefront only, card not verified
D_STALE = _d("stale")          # ELIGIBLE + card, last checked long ago: nothing fresh
D_LOGIN = _d("login")          # fresh LOGIN_REQUIRED: no route
D_CART = _d("cart")            # cart-link verdict only
D_VARIANT = _d("variant")      # enabled on the variant lane + fresh ELIGIBLE + card
D_DISABLED = _d("disabled")    # an operator's disabled row; otherwise listable
D_ENVX = _d("envx")            # excluded by the operator's env list
D_DEV = f"mp-dev-{RUN}.myshopify.com"   # a development store
D_WHOLESALE = f"wholesale.mp-{RUN}.example.com"
D_PAGE = [_d(f"page{i}") for i in range(3)]  # three NZ storefront rows, for paging

OURS = {D_CARD, D_NOCARD, D_STALE, D_LOGIN, D_CART, D_VARIANT, D_DISABLED, D_ENVX, D_DEV, D_WHOLESALE, *D_PAGE}


async def _table_exists(name: str) -> bool:
    from db.database import IS_SQLITE

    if IS_SQLITE:
        row = await database.fetch_one(
            "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = :n", {"n": name}
        )
    else:
        row = await database.fetch_one(
            "SELECT 1 AS x FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :n",
            {"n": name},
        )
    return row is not None


def _ts(offset: str) -> str:
    """A server-side timestamp expression, `offset` like '+1 day' or '-30 days'."""
    from db.database import IS_SQLITE

    if IS_SQLITE:
        return f"datetime('now', '{offset}')"
    sign, rest = offset[0], offset[1:]
    return f"(CURRENT_TIMESTAMP {sign} INTERVAL '{rest}')"


async def _fact(domain, market, verdict, *, card, checked="+0 seconds", positive_until=None):
    until = _ts(positive_until) if positive_until else "NULL"
    await database.execute(
        "INSERT INTO merchant_purchasability (merchant_domain, market_country, vantage, checked_at, "
        f"verdict, card_available, consecutive_failures, positive_until) VALUES (:d, :m, 'worker', "
        f"{_ts(checked)}, :v, :c, 0, {until})",
        {"d": domain, "m": market, "v": verdict, "c": card},
    )


async def _reap_row(domain, market, enabled):
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, "
        "market_country, enabled) VALUES (:d, '', '', :m, :e)",
        {"d": domain, "m": market, "e": enabled},
    )


@pytest.fixture
async def _rows(monkeypatch):
    from services import agent_merchant_directory as directory

    for name in ("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "MERCHANT_PURCHASABILITY_TTL_HOURS",
                 "MERCHANT_PURCHASABILITY_ENFORCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(directory.EXCLUDED_DOMAINS_ENV, D_ENVX)
    directory.reset_cache()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [
        t for t in ("merchant_purchasability", "reap_agentic_eligibility", "tierb_cart_link_eligibility")
        if not await _table_exists(t)
    ]
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_purchasability ("
        "merchant_domain VARCHAR(255) NOT NULL, market_country VARCHAR(2) NOT NULL, "
        "vantage VARCHAR(32) NOT NULL, checked_at TIMESTAMPTZ, verdict VARCHAR(32), "
        "card_available BOOLEAN, payment_methods TEXT, landed_price_minor BIGINT, "
        "landed_currency VARCHAR(8), expected_price_minor BIGINT, price_drift_minor BIGINT, "
        "variant_id VARCHAR(32), evidence TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, "
        "positive_until TIMESTAMPTZ, PRIMARY KEY (merchant_domain, market_country, vantage))"
    )
    await database.execute(
        "CREATE TABLE IF NOT EXISTS reap_agentic_eligibility ("
        "merchant_domain VARCHAR(255) NOT NULL, product_key TEXT NOT NULL DEFAULT '', "
        "variant_key TEXT NOT NULL DEFAULT '', market_country VARCHAR(2) NOT NULL, "
        "enabled BOOLEAN NOT NULL DEFAULT FALSE, accept_variant_labels TEXT, "
        "also_accept_domains TEXT, "
        "PRIMARY KEY (merchant_domain, market_country, product_key, variant_key))"
    )
    from db.tierb_cart_link_eligibility_schema import ensure_schema

    await ensure_schema()

    positive = dict(card=True, positive_until="+1 day")
    await _fact(D_CARD, "US", "ELIGIBLE", **positive)
    await _fact(D_CARD, "JP", "ELIGIBLE", **positive)
    await _fact(D_NOCARD, "US", "NO_CARD_PAYMENT", card=False)
    await _fact(D_STALE, "US", "ELIGIBLE", card=True, checked="-30 days", positive_until="-27 days")
    await _fact(D_LOGIN, "US", "LOGIN_REQUIRED", card=None)
    await _fact(D_VARIANT, "US", "ELIGIBLE", **positive)
    await _fact(D_DISABLED, "US", "ELIGIBLE", **positive)
    for d in (D_ENVX, D_DEV, D_WHOLESALE):
        await _fact(d, "US", "ELIGIBLE", **positive)
    for d in D_PAGE:
        await _fact(d, "NZ", "NO_CARD_PAYMENT", card=False)
    await _reap_row(D_VARIANT, "US", True)
    await _reap_row(D_DISABLED, "US", False)
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, variant_id, checked_at) "
        "VALUES (:d, 'US', 'ELIGIBLE', '1', CURRENT_TIMESTAMP)",
        {"d": D_CART},
    )

    yield

    params = {f"d{i}": d for i, d in enumerate(sorted(OURS))}
    in_list = ", ".join(f":d{i}" for i in range(len(OURS)))
    await database.execute(f"DELETE FROM merchant_purchasability WHERE merchant_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM reap_agentic_eligibility WHERE merchant_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM tierb_cart_link_eligibility WHERE shop_domain IN ({in_list})", params)
    for table in reversed(created):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    directory.reset_cache()
    if not was_connected and database.is_connected:
        await database.disconnect()


@pytest.fixture
def _rail_on(monkeypatch):
    """The purchase route live and creating, unrestricted pilot, cart-link lane armed."""
    import routes.agent_commerce_reap as reap_routes
    import services.reap_agentic_purchase as reap_svc

    monkeypatch.setattr(reap_routes, "_rail_is_live", lambda: True)
    monkeypatch.setattr(reap_svc, "is_create_enabled", lambda: True)
    monkeypatch.setattr(reap_svc, "is_cart_link_enabled", lambda: True)
    monkeypatch.setattr(reap_svc, "pilot_admission_scope", lambda: None)
    return reap_svc


def _app(allowed_merchants=None, agent_id=AGENT):
    from fastapi import FastAPI

    from routes.agent_auth import AgentContext, get_agent_context
    from routes.agent_sdk_fixed import router

    app = FastAPI()
    app.include_router(router)
    ctx = AgentContext(
        {"agent_id": agent_id, "agent_name": "t", "allowed_merchants": allowed_merchants},
        request=None,
    )
    app.dependency_overrides[get_agent_context] = lambda: ctx
    return app


async def _get(app, params=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get("/agent/v1/merchants", params={"limit": 100, **(params or {})})


def _ours(body):
    return {
        (m["merchant_domain"], m["market"]): m for m in body["merchants"] if m["merchant_domain"] in OURS
    }


async def _all_ours(app, params=None):
    """Every one of our rows across pages (other files' rows may push ours past page one)."""
    out, offset = {}, 0
    while True:
        body = (await _get(app, {**(params or {}), "offset": offset})).json()
        out.update(_ours(body))
        if not body["pagination"]["has_more"]:
            return body, out
        offset += 100


async def test_the_default_scope_is_the_protocol_directory(_rows):
    resp = await _get(_app())
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "available"
    _, ours = await _all_ours(_app())
    assert set(ours) == {(D_CARD, "US"), (D_CARD, "JP"), (D_NOCARD, "US"), (D_VARIANT, "US"),
                         *((d, "NZ") for d in D_PAGE)}
    card = ours[(D_CARD, "US")]
    assert card == {
        "merchant_domain": D_CARD,
        "market": "US",
        "store_url": f"https://{D_CARD}",
        "status": "active",
        "checkout_routes": ["storefront_checkout"],
        "card_checkout_verified": True,
    }
    assert ours[(D_NOCARD, "US")]["card_checkout_verified"] is False
    assert ours[(D_NOCARD, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_no_fresh_route_means_not_listed(_rows):
    _, ours = await _all_ours(_app())
    # Stale, a verdict no buyer can pass, and a cart-link verdict with the rail off.
    for domain in (D_STALE, D_LOGIN, D_CART):
        assert not [k for k in ours if k[0] == domain], domain


async def test_disabled_dev_wholesale_and_env_excluded_merchants_never_list(_rows, _rail_on):
    _, ours = await _all_ours(_app())
    for domain in (D_DISABLED, D_DEV, D_WHOLESALE, D_ENVX):
        assert not [k for k in ours if k[0] == domain], domain


async def test_agentic_checkout_follows_the_purchase_route(_rows, _rail_on):
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "US")]["checkout_routes"] == ["agentic_checkout", "storefront_checkout"]
    # Cart-link lane armed and the verdict fresh: reachable through the partner, though no
    # handoff fact exists.
    assert ours[(D_CART, "US")]["checkout_routes"] == ["agentic_checkout"]
    # A storefront-only merchant is on no purchase lane.
    assert ours[(D_CARD, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_the_cart_link_lane_disarmed_drops_its_merchants(_rows, _rail_on, monkeypatch):
    monkeypatch.setattr(_rail_on, "is_cart_link_enabled", lambda: False)
    _, ours = await _all_ours(_app())
    assert (D_CART, "US") not in ours
    assert ours[(D_VARIANT, "US")]["checkout_routes"][0] == "agentic_checkout"


async def test_enforcement_requires_the_card_fact_for_agentic(_rows, _rail_on, monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "true")
    _, ours = await _all_ours(_app())
    assert (D_CART, "US") not in ours  # no card fact
    assert ours[(D_VARIANT, "US")]["checkout_routes"][0] == "agentic_checkout"


@pytest.mark.parametrize("scope, expected", [
    ({}, False),  # an invalid pilot scope admits nothing
    ({"agent_ids": ["someone_else"], "merchant_domains": [D_VARIANT], "markets": ["US"]}, False),
    ({"agent_ids": [AGENT], "merchant_domains": ["other.example.com"], "markets": ["US"]}, False),
    ({"agent_ids": [AGENT], "merchant_domains": [D_VARIANT], "markets": ["JP"]}, False),
    ({"agent_ids": [AGENT], "merchant_domains": [D_VARIANT], "markets": ["US"]}, True),
])
async def test_the_pilot_scope_decides_agentic_per_agent(_rows, _rail_on, monkeypatch, scope, expected):
    monkeypatch.setattr(_rail_on, "pilot_admission_scope", lambda: scope)
    _, ours = await _all_ours(_app())
    assert ("agentic_checkout" in ours[(D_VARIANT, "US")]["checkout_routes"]) is expected


async def test_the_rail_off_means_no_agentic_route(_rows, _rail_on, monkeypatch):
    monkeypatch.setattr(_rail_on, "is_create_enabled", lambda: False)
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_the_market_filter(_rows):
    body, ours = await _all_ours(_app(), {"market": "jp"})
    assert set(ours) == {(D_CARD, "JP")}
    assert all(m["market"] == "JP" for m in body["merchants"])


@pytest.mark.parametrize("params", [{"market": "USA"}, {"market": "1"}, {"market": "US", "scope": "native"},
                                    {"status": "pending"}])
async def test_bad_market_and_status_are_rejected(_rows, params):
    assert (await _get(_app(), params)).status_code == 422


async def test_an_allowlist_matches_domains(_rows):
    _, ours = await _all_ours(_app(allowed_merchants=[f"www.{D_CARD}"]))
    assert set(ours) == {(D_CARD, "US"), (D_CARD, "JP")}
    body = (await _get(_app(allowed_merchants=["merch_portal_only"]))).json()
    assert body["merchants"] == [] and body["pagination"]["total"] == 0


async def test_paging_over_one_market(_rows):
    app = _app()
    first = (await _get(app, {"market": "NZ", "limit": 2})).json()
    rest = (await _get(app, {"market": "NZ", "limit": 2, "offset": 2})).json()
    seen = [m["merchant_domain"] for m in first["merchants"] + rest["merchants"]]
    assert len(seen) == len(set(seen)) == first["pagination"]["total"]
    assert set(D_PAGE) <= set(seen)
    assert first["pagination"]["has_more"] is True
    assert rest["pagination"]["has_more"] is False


async def test_a_failed_fact_read_answers_closed(_rows, _rail_on, monkeypatch):
    import db.tierb_cart_link_eligibility as tierb
    import routes.agent_commerce_reap as reap_routes

    async def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(tierb, "is_cart_link_eligible", _boom)
    _, ours = await _all_ours(_app())
    assert (D_CART, "US") not in ours

    from services import agent_merchant_directory as directory

    directory.reset_cache()
    monkeypatch.setattr(reap_routes, "_refuse_if_merchant_disabled", _boom)
    _, ours = await _all_ours(_app())
    assert ours == {}


async def test_facts_are_cached_between_calls(_rows, monkeypatch):
    await _get(_app())

    async def _boom(*a, **k):
        raise AssertionError("a cached directory must not query")

    monkeypatch.setattr(database, "fetch_all", _boom)
    monkeypatch.setattr(database, "fetch_one", _boom)
    _, ours = await _all_ours(_app())
    assert (D_CARD, "US") in ours


async def test_a_db_error_is_not_echoed_to_the_caller(_rows, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError('relation "merchant_purchasability_secret_detail" does not exist')

    monkeypatch.setattr(database, "fetch_all", _boom)
    resp = await _get(_app())
    assert resp.status_code == 500
    assert "secret_detail" not in resp.text
    assert resp.json()["detail"] == "Failed to list merchants"
