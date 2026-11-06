"""GET /agent/v1/merchants?scope=protocol: the protocol merchant directory.

A partner buys from merchants reached through a checkout protocol, none of which is integrated
through the merchant portal. The directory (services/agent_merchant_directory.py) lists each
(merchant domain, market) with at least one route a purchase can take, using the purchase
gates' own functions for every fact. The default scope (`available`, the portal list) is
unchanged; tests/test_agent_sdk_merchants_list.py owns it.

These cases seed the tables those gates read and drive the real route on a minimal app (never
`main`, never TestClient). Only rows whose domain carries this run's suffix are asserted on:
the Postgres dialect gate shares one database across files, so other rows may be present.
"""

from __future__ import annotations

import asyncio
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


D_CARD = _d("card")            # fresh ELIGIBLE + card: storefront, card accepted
D_NOCARD = _d("nocard")        # fresh NO_CARD_PAYMENT: storefront only, no card
D_STALE = _d("stale")          # ELIGIBLE + card, last checked long ago: nothing fresh
D_LOGIN = _d("login")          # fresh LOGIN_REQUIRED: no route
D_CART = _d("cart")            # cart-link verdict only
D_VARIANT = _d("variant")      # enabled on the variant lane (US and KR) + fresh ELIGIBLE + card
D_DISABLED = _d("disabled")    # an operator's disabled row; otherwise listable
D_ENVX = _d("envx")            # excluded by the operator's env list
D_RIG_KNOWN = _d("rigknown")   # store domain of a baked-in test merchant id
D_RIG_ENV = _d("rigenv")       # onboarding store_url of an env-listed test merchant id
D_REJECTED = _d("rejected")    # store_url of a rejected portal merchant
D_APPROVED = _d("approved")    # store_url of an approved portal merchant: still listable
D_QUAR = _d("quarantined")     # an active domain quarantine
D_REVOKED = _d("revoked")      # a revoked domain quarantine: still listable
D_DEV = f"mp-dev-{RUN}.myshopify.com"   # a development store
D_DEMO = f"pivota-review-demo-mp-{RUN}.example.com"
D_WHOLESALE = f"wholesale.mp-{RUN}.example.com"
D_PAGE = [_d(f"page{i}") for i in range(3)]  # three NZ storefront rows, for paging

OURS = {
    D_CARD, D_NOCARD, D_STALE, D_LOGIN, D_CART, D_VARIANT, D_DISABLED, D_ENVX, D_RIG_KNOWN, D_RIG_ENV,
    D_REJECTED, D_APPROVED, D_QUAR, D_REVOKED, D_DEV, D_DEMO, D_WHOLESALE, *D_PAGE,
}
ENV_RIG_ID = f"merch_mp_envrig_{RUN}"
REJECTED_ID = f"merch_mp_rejected_{RUN}"
APPROVED_ID = f"merch_mp_approved_{RUN}"
STORE_ID = f"st_mp_{RUN}"


def _known_rig_id() -> str:
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    return sorted(KNOWN_TEST_MERCHANT_IDS)[0]


def _sync_url() -> str:
    return (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


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


POSITIVE = dict(card=True, positive_until="+1 day")


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


async def _onboarding(mid, status, store_url):
    from db.merchant_onboarding import merchant_onboarding

    await database.execute(
        merchant_onboarding.insert().values(
            merchant_id=mid, business_name=f"Biz {mid}", store_url=store_url,
            contact_email=f"owner@{mid}.example", region="US", status=status,
            psp_connected=False, operating_mode="storefront", apm_enabled=False,
        )
    )


@pytest.fixture
async def _rows(monkeypatch):
    import sqlalchemy

    from db.database import IS_SQLITE, metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.orders import orders
    from services import agent_merchant_directory as directory
    from services import source_quarantine, test_merchant_policy

    for name in ("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "MERCHANT_PURCHASABILITY_TTL_HOURS",
                 "MERCHANT_PURCHASABILITY_ENFORCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(directory.EXCLUDED_DOMAINS_ENV, D_ENVX)
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", ENV_RIG_ID)
    directory.reset_cache()
    test_merchant_policy.reset_cache()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [
        t for t in (
            "merchant_purchasability", "reap_agentic_eligibility", "tierb_cart_link_eligibility",
            "merchant_stores", "merchant_onboarding", "orders", "merchant_psps", "catalog_source_quarantine",
        )
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
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, "
        "platform VARCHAR(50) NOT NULL, name VARCHAR(255) NOT NULL, domain VARCHAR(255), "
        "api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
        "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE "
        "DEFAULT CURRENT_TIMESTAMP)"
    )
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_psps ("
        "psp_id VARCHAR(100) PRIMARY KEY, merchant_id VARCHAR(50), provider VARCHAR(50), "
        "status VARCHAR(50), connected_at TIMESTAMP)"
    )
    await database.execute(
        "CREATE TABLE IF NOT EXISTS catalog_source_quarantine ("
        + ("quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, " if IS_SQLITE else "quarantine_id BIGSERIAL PRIMARY KEY, ")
        + "match_type TEXT NOT NULL, match_value TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', "
        "reason TEXT, expires_at TIMESTAMPTZ, created_by TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMPTZ, "
        "revoked_by TEXT, metadata TEXT)"
    )
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, orders], checkfirst=True)
    engine.dispose()
    from db.tierb_cart_link_eligibility_schema import ensure_schema

    await ensure_schema()
    if IS_SQLITE:
        # The production reader filters on Postgres `now()`, which SQLite lacks; the Postgres
        # twin of this file runs the real one.
        async def _sqlite_quarantines(*, db):
            rows = await db.fetch_all(
                f"SELECT {source_quarantine.QUARANTINE_COLUMNS} FROM catalog_source_quarantine "
                "WHERE state = 'active'"
            )
            return [source_quarantine._quarantine_from_row(dict(r)) for r in rows]

        monkeypatch.setattr(source_quarantine, "load_active_quarantines", _sqlite_quarantines)

    await _fact(D_CARD, "US", "ELIGIBLE", **POSITIVE)
    await _fact(D_CARD, "JP", "ELIGIBLE", **POSITIVE)
    await _fact(D_NOCARD, "US", "NO_CARD_PAYMENT", card=False)
    await _fact(D_STALE, "US", "ELIGIBLE", card=True, checked="-30 days", positive_until="-27 days")
    await _fact(D_LOGIN, "US", "LOGIN_REQUIRED", card=None)
    await _fact(D_VARIANT, "US", "ELIGIBLE", **POSITIVE)
    await _fact(D_VARIANT, "KR", "ELIGIBLE", **POSITIVE)
    for d in (D_DISABLED, D_ENVX, D_DEV, D_DEMO, D_WHOLESALE, D_RIG_KNOWN, D_RIG_ENV, D_REJECTED,
              D_APPROVED, D_QUAR, D_REVOKED):
        await _fact(d, "US", "ELIGIBLE", **POSITIVE)
    for d in D_PAGE:
        await _fact(d, "NZ", "NO_CARD_PAYMENT", card=False)
    await _reap_row(D_VARIANT, "US", True)
    await _reap_row(D_VARIANT, "KR", True)
    await _reap_row(D_DISABLED, "US", False)
    await database.execute(
        "INSERT INTO tierb_cart_link_eligibility (shop_domain, market, verdict, variant_id, checked_at) "
        "VALUES (:d, 'US', 'ELIGIBLE', '1', CURRENT_TIMESTAMP)",
        {"d": D_CART},
    )
    await database.execute(
        "INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain) "
        "VALUES (:s, :m, 'shopify', 'rig', :d)",
        {"s": STORE_ID, "m": _known_rig_id(), "d": f"www.{D_RIG_KNOWN}"},
    )
    await _onboarding(ENV_RIG_ID, "approved", f"https://{D_RIG_ENV}/collections/all")
    await _onboarding(REJECTED_ID, "rejected", f"https://www.{D_REJECTED}")
    await _onboarding(APPROVED_ID, "approved", f"https://{D_APPROVED}")
    for value, state in ((D_QUAR, "active"), (D_REVOKED, "revoked")):
        await database.execute(
            "INSERT INTO catalog_source_quarantine (match_type, match_value, state, created_by) "
            "VALUES ('domain', :v, :s, 'test')",
            {"v": f"www.{value}", "s": state},
        )

    yield

    params = {f"d{i}": d for i, d in enumerate(sorted(OURS))}
    in_list = ", ".join(f":d{i}" for i in range(len(OURS)))
    await database.execute(f"DELETE FROM merchant_purchasability WHERE merchant_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM reap_agentic_eligibility WHERE merchant_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM tierb_cart_link_eligibility WHERE shop_domain IN ({in_list})", params)
    await database.execute("DELETE FROM merchant_stores WHERE store_id = :s", {"s": STORE_ID})
    for mid in (ENV_RIG_ID, REJECTED_ID, APPROVED_ID):
        await database.execute("DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": mid})
    await database.execute(
        "DELETE FROM catalog_source_quarantine WHERE created_by = 'test' AND match_value IN (:a, :b)",
        {"a": f"www.{D_QUAR}", "b": f"www.{D_REVOKED}"},
    )
    for table in reversed(created):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    directory.reset_cache()
    test_merchant_policy.reset_cache()
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
        return await client.get(
            "/agent/v1/merchants", params={"limit": 100, "scope": "protocol", **(params or {})}
        )


def _ours(body):
    return {
        (m["merchant_domain"], m["market"]): m for m in body["merchants"] if m["merchant_domain"] in OURS
    }


async def _all_ours(app, params=None):
    """Every one of our rows across pages (other files' rows may push ours past page one)."""
    out, offset = {}, 0
    while True:
        resp = await _get(app, {**(params or {}), "offset": offset})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        out.update(_ours(body))
        if not body["pagination"]["has_more"]:
            return body, out
        offset += 100


LISTED = {(D_CARD, "US"), (D_CARD, "JP"), (D_NOCARD, "US"), (D_VARIANT, "US"), (D_VARIANT, "KR"),
          (D_APPROVED, "US"), (D_REVOKED, "US"), *((d, "NZ") for d in D_PAGE)}


async def test_the_default_scope_is_still_the_portal_list(_rows):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/agent/v1/merchants", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "available"
    assert all("checkout_routes" not in m and "merchant_id" in m for m in body["merchants"])


async def test_scope_protocol_is_the_directory(_rows):
    resp = await _get(_app())
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "protocol"
    _, ours = await _all_ours(_app())
    assert set(ours) == LISTED
    assert ours[(D_CARD, "US")] == {
        "merchant_domain": D_CARD,
        "market": "US",
        "store_url": f"https://{D_CARD}",
        "status": "active",
        "checkout_routes": ["storefront_checkout"],
        "merchant_checkout_accepts_cards": True,
    }
    assert ours[(D_NOCARD, "US")]["merchant_checkout_accepts_cards"] is False
    assert ours[(D_NOCARD, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_no_fresh_route_means_not_listed(_rows):
    _, ours = await _all_ours(_app())
    # Stale, a verdict no buyer can pass, and a cart-link verdict with the rail off.
    for domain in (D_STALE, D_LOGIN, D_CART):
        assert not [k for k in ours if k[0] == domain], domain


# Fixed ids: the domains carry a per-process random suffix, and the sharded sweep requires every
# shard to collect the same test ids.
@pytest.mark.parametrize("domain", [D_DISABLED, D_DEV, D_DEMO, D_WHOLESALE, D_ENVX],
                         ids=["disabled", "dev_store", "demo_prefix", "wholesale", "env_excluded"])
async def test_structural_and_operator_exclusions_never_list(_rows, _rail_on, domain):
    _, ours = await _all_ours(_app())
    assert not [k for k in ours if k[0] == domain]


async def test_test_rigs_never_list_by_their_store_domains(_rows, _rail_on):
    """A baked-in rig id (merchant_stores domain) and an env-listed id (onboarding store_url),
    both on custom domains with a fresh ELIGIBLE fact, to a brand-new agent."""
    _, ours = await _all_ours(_app(agent_id=f"agent_mp_new_{RUN}"))
    assert not [k for k in ours if k[0] in (D_RIG_KNOWN, D_RIG_ENV)]
    assert (D_APPROVED, "US") in ours  # an approved portal merchant's domain is not a rig


async def test_a_rejected_portal_merchant_never_lists(_rows):
    _, ours = await _all_ours(_app())
    assert not [k for k in ours if k[0] == D_REJECTED]


async def test_an_active_domain_quarantine_never_lists(_rows):
    _, ours = await _all_ours(_app())
    assert not [k for k in ours if k[0] == D_QUAR]
    assert (D_REVOKED, "US") in ours


async def test_agentic_checkout_follows_the_purchase_route(_rows, _rail_on):
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "US")]["checkout_routes"] == ["agentic_checkout", "storefront_checkout"]
    # Cart-link lane armed and the verdict fresh: reachable through the partner, though no
    # handoff fact exists.
    assert ours[(D_CART, "US")]["checkout_routes"] == ["agentic_checkout"]
    # A storefront-only merchant is on no purchase lane.
    assert ours[(D_CARD, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_a_market_the_route_cannot_price_has_no_agentic_route(_rows, _rail_on):
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "KR")]["checkout_routes"] == ["storefront_checkout"]


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


async def test_the_rail_not_creating_means_no_agentic_route(_rows, _rail_on, monkeypatch):
    monkeypatch.setattr(_rail_on, "is_create_enabled", lambda: False)
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_the_rail_not_live_means_no_agentic_route(_rows, _rail_on, monkeypatch):
    import routes.agent_commerce_reap as reap_routes

    monkeypatch.setattr(reap_routes, "_rail_is_live", lambda: False)
    _, ours = await _all_ours(_app())
    assert ours[(D_VARIANT, "US")]["checkout_routes"] == ["storefront_checkout"]


async def test_the_market_filter(_rows):
    body, ours = await _all_ours(_app(), {"market": "jp"})
    assert set(ours) == {(D_CARD, "JP")}
    assert all(m["market"] == "JP" for m in body["merchants"])


@pytest.mark.parametrize("params", [{"market": "USA"}, {"market": "1"}, {"status": "pending"}])
async def test_bad_market_and_status_are_rejected(_rows, params):
    assert (await _get(_app(), params)).status_code == 422


@pytest.mark.parametrize("scope", ["available", "mine"])
async def test_the_other_scopes_ignore_market_as_before(_rows, scope):
    """The #2708 contract: `market` was no parameter there, so it is accepted and ignored."""
    plain = await _get(_app(), {"scope": scope})
    filtered = await _get(_app(), {"scope": scope, "market": "US"})
    assert plain.status_code == filtered.status_code == 200
    assert filtered.json() == plain.json()


async def test_an_allowlist_matches_domains(_rows):
    _, ours = await _all_ours(_app(allowed_merchants=[f"www.{D_CARD}"]))
    assert set(ours) == {(D_CARD, "US"), (D_CARD, "JP")}
    body = (await _get(_app(allowed_merchants=["merch_portal_only"]))).json()
    assert body["merchants"] == [] and body["pagination"]["total"] == 0


async def test_one_cached_directory_serves_agents_with_different_allowlists(_rows):
    """No reset between agents: the cache must hold only agent-independent facts."""
    a = _app(allowed_merchants=[D_CARD], agent_id=f"agent_mp_a_{RUN}")
    b = _app(agent_id=f"agent_mp_b_{RUN}")
    c = _app(allowed_merchants=[D_NOCARD], agent_id=f"agent_mp_c_{RUN}")
    _, seen_a = await _all_ours(a)
    _, seen_b = await _all_ours(b)
    _, seen_c = await _all_ours(c)
    _, seen_a_again = await _all_ours(a)
    assert set(seen_a) == set(seen_a_again) == {(D_CARD, "US"), (D_CARD, "JP")}
    assert set(seen_b) == LISTED
    assert set(seen_c) == {(D_NOCARD, "US")}


async def test_paging_over_one_market(_rows):
    app = _app()
    first = (await _get(app, {"market": "NZ", "limit": 2})).json()
    rest = (await _get(app, {"market": "NZ", "limit": 2, "offset": 2})).json()
    seen = [m["merchant_domain"] for m in first["merchants"] + rest["merchants"]]
    assert len(seen) == len(set(seen)) == first["pagination"]["total"]
    assert set(D_PAGE) <= set(seen)
    assert first["pagination"]["has_more"] is True
    assert rest["pagination"]["has_more"] is False


async def test_has_more_is_false_on_an_exactly_full_last_page(_rows):
    total = (await _get(_app(), {"market": "NZ"})).json()["pagination"]["total"]
    exact = (await _get(_app(), {"market": "NZ", "limit": total})).json()
    assert len(exact["merchants"]) == total
    assert exact["pagination"]["has_more"] is False


async def test_a_failed_fact_read_answers_closed_and_is_not_cached(_rows, _rail_on, monkeypatch):
    import db.tierb_cart_link_eligibility as tierb

    real = tierb.is_cart_link_eligible

    async def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(tierb, "is_cart_link_eligible", _boom)
    _, ours = await _all_ours(_app())
    assert (D_CART, "US") not in ours
    monkeypatch.setattr(tierb, "is_cart_link_eligible", real)
    _, ours = await _all_ours(_app())
    assert (D_CART, "US") in ours  # the degraded build was not cached


async def test_a_swallowed_purchasability_error_is_not_cached(_rows, monkeypatch):
    import db.merchant_purchasability as purchasability

    class _Down:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("down")

        async def fetch_all(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(purchasability, "database", _Down())
    _, ours = await _all_ours(_app())
    assert (D_CARD, "US") not in ours  # both readers answered False
    from services import agent_merchant_directory as directory

    assert directory._cache is None  # counted from their ERROR records, so not cached


async def test_a_failed_disabled_read_lists_nothing(_rows, monkeypatch):
    import routes.agent_commerce_reap as reap_routes

    async def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(reap_routes, "_refuse_if_merchant_disabled", _boom)
    _, ours = await _all_ours(_app())
    assert ours == {}


@pytest.mark.parametrize("needle", ["merchant_stores", "merchant_onboarding"])
async def test_an_unreadable_exclusion_source_is_a_500_not_a_list(_rows, monkeypatch, needle):
    real = database.fetch_all

    async def _fetch_all(query, *a, **k):
        if f"FROM {needle}" in str(query):
            raise RuntimeError("secret detail")
        return await real(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", _fetch_all)
    resp = await _get(_app())
    assert resp.status_code == 500
    assert "secret" not in resp.text


async def test_facts_are_cached_between_calls(_rows, monkeypatch):
    await _get(_app())

    async def _boom(*a, **k):
        raise AssertionError("a cached directory must not query")

    monkeypatch.setattr(database, "fetch_all", _boom)
    monkeypatch.setattr(database, "fetch_one", _boom)
    _, ours = await _all_ours(_app())
    assert (D_CARD, "US") in ours


async def test_the_cache_expires(_rows):
    from services import agent_merchant_directory as directory

    first = {f.merchant_domain for f in await directory.merchant_facts(now=1000.0)}
    assert D_CARD in first
    late = _d("late")
    OURS.add(late)
    await _fact(late, "US", "ELIGIBLE", **POSITIVE)
    within = {f.merchant_domain for f in await directory.merchant_facts(now=1000.0 + directory.CACHE_TTL_SECONDS - 1)}
    assert late not in within
    after = {f.merchant_domain for f in await directory.merchant_facts(now=1000.0 + directory.CACHE_TTL_SECONDS + 1)}
    assert late in after


async def test_concurrent_callers_share_one_build(_rows, monkeypatch):
    from services import agent_merchant_directory as directory

    real = directory._build
    calls = []

    async def _counted():
        calls.append(1)
        await asyncio.sleep(0.05)
        return await real()

    monkeypatch.setattr(directory, "_build", _counted)
    results = await asyncio.gather(directory.merchant_facts(), directory.merchant_facts())
    assert len(calls) == 1
    assert results[0] == results[1]


async def test_a_db_error_is_not_echoed_to_the_caller(_rows, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError('relation "merchant_purchasability_secret_detail" does not exist')

    monkeypatch.setattr(database, "fetch_all", _boom)
    resp = await _get(_app())
    assert resp.status_code == 500
    assert "secret_detail" not in resp.text
    assert resp.json()["detail"] == "Failed to list merchants"


async def test_a_cancelled_caller_does_not_cancel_the_shared_build(_rows, monkeypatch):
    from services import agent_merchant_directory as directory

    real = directory._build
    started = asyncio.Event()

    async def _slow():
        started.set()
        await asyncio.sleep(0.1)
        return await real()

    monkeypatch.setattr(directory, "_build", _slow)
    callers = [asyncio.ensure_future(directory.merchant_facts()) for _ in range(3)]
    await started.wait()
    callers[0].cancel()
    done = await asyncio.gather(*callers, return_exceptions=True)
    assert isinstance(done[0], asyncio.CancelledError)
    for result in done[1:]:
        assert not isinstance(result, BaseException), result
        assert D_CARD in {f.merchant_domain for f in result}
    assert directory._cache is not None  # the build finished and cached for nobody's request


async def test_a_build_that_never_finishes_is_a_bounded_503(_rows, monkeypatch):
    from services import agent_merchant_directory as directory

    gate = asyncio.Event()  # never set

    async def _stuck():
        await gate.wait()

    monkeypatch.setattr(directory, "BUILD_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(directory, "_build", _stuck)
    try:
        resp = await asyncio.wait_for(_get(_app()), 5)
        assert resp.status_code == 503
        assert "secret" not in resp.text
    finally:
        directory.reset_cache()  # cancels the stuck build


async def test_a_build_that_never_finishes_serves_the_last_clean_one(_rows, monkeypatch):
    from services import agent_merchant_directory as directory

    clean = await directory.merchant_facts(now=1000.0)
    gate = asyncio.Event()  # never set

    async def _stuck():
        await gate.wait()

    monkeypatch.setattr(directory, "BUILD_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(directory, "_build", _stuck)
    try:
        late = 1000.0 + directory.CACHE_TTL_SECONDS + 1
        assert await asyncio.wait_for(directory.merchant_facts(now=late), 5) == clean
    finally:
        directory.reset_cache()


async def test_a_failed_build_serves_a_recent_clean_one_then_fails_closed(_rows, monkeypatch):
    from services import agent_merchant_directory as directory

    clean = await directory.merchant_facts(now=1000.0)

    async def _boom():
        raise RuntimeError("merchant_stores unreadable")

    monkeypatch.setattr(directory, "_build", _boom)
    recent = 1000.0 + directory.CACHE_TTL_SECONDS + 1
    assert await directory.merchant_facts(now=recent) == clean
    with pytest.raises(RuntimeError):
        await directory.merchant_facts(now=1000.0 + directory.STALE_SERVE_SECONDS + 1)
