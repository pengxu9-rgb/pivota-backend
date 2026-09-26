"""Who a link is issued to, and what is recorded about it (ADR-025 D1). No database.

The row writes themselves are covered on real Postgres in tests/test_issue_click_postgres.py.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

import routes.agent_auth as agent_auth
import routes.agent_shop_gateway as gateway
from services.commerce_attribution_service import PVT_CLICK_ID
from services.outbound_links_service import parse_and_verify_redirect_token, url_domain

AGENT_KEY = "ak_live_" + "ab" * 32


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _Request:
    def __init__(self, **headers):
        self.headers = _Headers({k.lower(): v for k, v in headers.items()})


# --- the caller's key -----------------------------------------------------------------------------


def test_the_key_is_read_from_x_api_key_or_a_bearer_header():
    assert agent_auth.request_api_key(_Request(**{"X-API-Key": AGENT_KEY})) == AGENT_KEY
    assert agent_auth.request_api_key(_Request(Authorization=f"Bearer {AGENT_KEY}")) == AGENT_KEY
    assert agent_auth.request_api_key(_Request(Authorization="Basic abc")) is None
    assert agent_auth.request_api_key(_Request()) is None
    assert agent_auth.request_api_key(None) is None


# --- the agent a link is issued to ----------------------------------------------------------------


@pytest.fixture
def lookups(monkeypatch):
    calls = []
    result = {"agent": {"agent_id": "agent_minds", "is_active": True}}

    async def _get_agent_by_key(api_key, metrics_out=None):
        calls.append(api_key)
        if isinstance(result["agent"], Exception):
            raise result["agent"]
        return result["agent"]

    monkeypatch.setattr(agent_auth, "get_agent_by_key", _get_agent_by_key)
    return calls, result


async def test_a_real_agent_key_names_its_agent(lookups):
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) == "agent_minds"


async def test_an_internal_key_is_never_an_agent_and_is_not_looked_up(lookups, monkeypatch):
    """The gateway sends its internal key on purpose for cached search results, which are shared
    across callers: those links must stay agent-less, never agent_internal_trusted_<hash>."""
    calls, _ = lookups
    # Shaped like an agent key on purpose: PIVOTA_API_KEY and friends can be ak_live_ keys, so the
    # internal check must hold on its own, not ride on the format check.
    internal_key = "ak_live_" + "cd" * 32
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (internal_key,))

    assert await agent_auth.resolve_issuing_agent_id(internal_key) is None
    assert calls == []


@pytest.mark.parametrize("key", [None, "", "  ", "not-an-agent-key", "ak_" + "zz" * 32])
async def test_no_key_or_a_malformed_one_names_nobody(lookups, key):
    calls, _ = lookups
    assert await agent_auth.resolve_issuing_agent_id(key) is None
    assert calls == []


@pytest.mark.parametrize("agent", [
    None,
    {"agent_id": "agent_off", "is_active": False},
    {"agent_id": "agent_off", "status": "suspended"},
    {"agent_id": "", "is_active": True},
])
async def test_an_unknown_or_inactive_agent_names_nobody(lookups, agent):
    _, result = lookups
    result["agent"] = agent
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) is None


async def test_a_failed_lookup_names_nobody_and_does_not_raise(lookups):
    _, result = lookups
    result["agent"] = RuntimeError("db down")
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) is None


# --- the issued click is the click the link carries -----------------------------------------------


async def test_the_issued_click_is_built_from_exactly_what_the_token_signs():
    issued = []
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        market_observed=True,
        tool="offers.resolve",
        destination_url="https://brand.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_1", "source": "offers.resolve"},
        allowed_domains=["brand.com"],
        merchant_id="merch_anchor",
        product_id="merch_anchor|shopify|999",
        variant_id="4711",
        shop_domain="brand.com",
        platform=None,
        seller_ref="merch_seller",
        seed_kind="cross",
        cart_variant_id=None,
        issued_clicks=issued,
    )
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    payload = parse_and_verify_redirect_token(token)

    assert len(issued) == 1
    click = issued[0]
    assert click.click_id == payload["ctx"][PVT_CLICK_ID]
    assert click.context == payload["ctx"]
    assert (click.merchant_id, click.canonical_product_id, click.canonical_variant_id) == (
        "merch_anchor", "merch_anchor|shopify|999", "4711"
    )
    assert click.destination_url == payload["dest"]
    assert click.dest_domain == url_domain(payload["dest"])
    assert click.agent_id is None  # set once per request, from the caller's key


async def test_a_link_that_is_refused_issues_nothing():
    issued = []
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        market_observed=True,
        tool="offers.resolve",
        destination_url="https://not-allowed.example/p",
        utm_template=None,
        ctx={},
        allowed_domains=["brand.com"],
        merchant_id="merch_anchor",
        product_id=None,
        variant_id=None,
        shop_domain=None,
        platform=None,
        cart_variant_id=None,
        issued_clicks=issued,
    )
    assert redirect_url is None
    assert issued == []


# --- offers.resolve records what it hands out ------------------------------------------------------


def _seed_row():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": "eps_1", "external_product_id": "ext_1", "market": "US", "tool": "*",
        "destination_url": "https://fentybeauty.com/products/gloss-bomb",
        "canonical_url": "https://fentybeauty.com/products/gloss-bomb",
        "domain": "fentybeauty.com", "title": "Gloss Bomb", "price_amount": 19.0,
        "price_currency": "USD", "availability": "in_stock", "utm_template": None,
        "seed_data": {
            "snapshot": {"extracted_at": now}, "brand": "Fenty Beauty",
            "variants": [{"variant_id": "SKU_FENTY_001", "title": "Gloss Bomb 9ml", "price_amount": 19.0,
                          "price_currency": "USD", "availability": "in_stock"}],
        },
        "status": "active",
        "destination_checked_at": now, "destination_http_status": 200,
        "destination_verdict": "live", "destination_failure_streak": 0,
    }


@pytest.fixture
def resolve_offers(monkeypatch):
    """The real invoke route for offers.resolve over one external seed. The redirect builder is
    replaced by one that honours the `issued_clicks` sink the way the real one does."""
    from fastapi.testclient import TestClient

    from main import app
    from services.commerce_attribution_service import IssuedClick

    async def fake_fetch_all(query, values=None):
        return [_seed_row()] if "FROM external_product_seeds" in str(query) else []

    minted = []

    async def fake_redirect(**kwargs):
        minted.append(kwargs["click_id"])
        link = f"https://example.com/r?token={kwargs['click_id']}"
        sink = kwargs.get("issued_clicks")
        if sink is not None:
            sink.append(IssuedClick(click_id=kwargs["click_id"], surface="offers_resolve",
                                    merchant_id=kwargs.get("merchant_id"), link=link))
        return link

    keys_seen, batches, assertions_seen = [], [], []

    async def fake_resolve(api_key, assertion=None, *, op=None):
        from services.issuing_agent_assertion import IssuingContext

        keys_seen.append(api_key)
        assertions_seen.append((assertion, op))
        return IssuingContext(agent_id="agent_minds" if api_key == AGENT_KEY else None)

    async def fake_issue(clicks):
        batches.append(list(clicks))
        return len(clicks)

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", fake_redirect)
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request", fake_resolve)
    monkeypatch.setattr(gateway, "issue_clicks", fake_issue)
    client = TestClient(app)

    def call(headers=None):
        res = client.post(
            "/agent/shop/v1/invoke",
            headers=headers or {},
            json={"operation": "offers.resolve",
                  "payload": {"product": {"sku_id": "SKU_FENTY_001"}, "limit": 10, "market": "US", "tool": "*"},
                  "metadata": {"source": "creator-agent-ui", "agent_id": "agent_spoofed"}},
        )
        assert res.status_code == 200, res.text
        return res.json()

    call.assertions_seen = assertions_seen
    return call, minted, keys_seen, batches


def test_offers_resolve_records_each_link_it_hands_out_with_the_callers_agent(resolve_offers):
    call, minted, keys_seen, batches = resolve_offers

    body = call({"X-API-Key": AGENT_KEY})

    assert body.get("offers")
    assert keys_seen == [AGENT_KEY]
    assert len(batches) == 1
    assert [c.click_id for c in batches[0]] == minted
    # From the caller's key. The body's agent_id ("agent_spoofed") is never read.
    assert {c.agent_id for c in batches[0]} == {"agent_minds"}


def test_offers_resolve_hands_the_gateways_signed_assertion_to_the_resolver(resolve_offers):
    """The MCP door: the header travels from the invoke route to the resolver, bound to offers.resolve.
    Whether it is TRUSTED is resolve_issuing_context_for_request's job (tests/test_issuing_agent_for_request.py)."""
    call, _, _, _ = resolve_offers

    call({"X-API-Key": AGENT_KEY, "X-Pivota-Issuing-Agent": "v1.signed.token"})
    call({"X-API-Key": AGENT_KEY})

    assert call.assertions_seen == [("v1.signed.token", "offers.resolve"), (None, "offers.resolve")]


def test_offers_resolve_without_a_key_issues_agentless_links(resolve_offers):
    call, _, keys_seen, batches = resolve_offers

    call()

    assert keys_seen == [None]
    assert {c.agent_id for c in batches[0]} == {None}


def test_a_failed_issue_write_still_serves_the_offers(resolve_offers, monkeypatch):
    call, minted, _, _ = resolve_offers

    async def broken_issue(clicks):
        raise RuntimeError("surface_click_events unavailable")

    monkeypatch.setattr(gateway, "issue_clicks", broken_issue)

    body = call({"X-API-Key": AGENT_KEY})

    assert body["status"] == "success"
    assert body.get("offers") and minted


# --- Pivota's own service agents are never an issuing agent ---------------------------------------

GATEWAY_AGENT = "agent_982b1ea2df866206"


async def test_the_gateways_own_agent_issues_agentless_links(lookups):
    """The gateway sends its own PIVOTA_API_KEY (agent_982b1ea2df866206, confirmed 2026-09-24) for
    the MCP get_offers door and cached search results: those links must not credit the gateway."""
    _, result = lookups
    result["agent"] = {"agent_id": GATEWAY_AGENT, "is_active": True}
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) is None


async def test_the_env_can_only_add_excluded_agents_never_remove_the_default(lookups, monkeypatch):
    _, result = lookups
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", "agent_other_service")
    assert agent_auth.issuing_excluded_agent_ids() >= {GATEWAY_AGENT, "agent_other_service"}

    result["agent"] = {"agent_id": "agent_other_service", "is_active": True}
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) is None
    # An env list that omits the code default still excludes it.
    result["agent"] = {"agent_id": GATEWAY_AGENT, "is_active": True}
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) is None
    # A real agent is unaffected.
    result["agent"] = {"agent_id": "agent_minds", "is_active": True}
    assert await agent_auth.resolve_issuing_agent_id(AGENT_KEY) == "agent_minds"


# --- only what ships is issued ----------------------------------------------------------------------


async def test_a_link_minted_but_not_served_is_never_issued(monkeypatch):
    """Ranking truncates to `limit` and live verification drops dead offers AFTER links are
    minted. The flush matches what actually ships: by the served link, or by the click id the
    seed lane publishes under execution_spec.tracking."""
    from services.commerce_attribution_service import IssuedClick

    batches = []

    async def fake_issue(clicks):
        batches.append(clicks)
        return len(clicks)

    async def fake_resolve(api_key, assertion=None, *, op=None):
        from services.issuing_agent_assertion import IssuingContext

        return IssuingContext(agent_id="agent_minds")

    monkeypatch.setattr(gateway, "issue_clicks", fake_issue)
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request", fake_resolve)
    minted = [IssuedClick(click_id=f"clk_{i}", surface="offers_resolve", link=f"https://x/r?token=t{i}")
              for i in range(4)]
    offers = [
        {"affiliate_url": "https://x/r?token=t0"},
        {"execution_spec": {"tracking": {"click_id": "clk_2"}}},
        {"tracking": {"click_id": "clk_3"}},
    ]

    await gateway._issue_served_clicks(minted, offers, AGENT_KEY)

    assert len(batches) == 1
    assert [c.click_id for c in batches[0]] == ["clk_0", "clk_2", "clk_3"]
    assert {c.agent_id for c in batches[0]} == {"agent_minds"}

    batches.clear()
    await gateway._issue_served_clicks(minted, [{"affiliate_url": "https://x/r?token=other"}], AGENT_KEY)
    assert batches == []


async def test_merchant_diagnostics_do_not_flag_links_nobody_followed(monkeypatch):
    """The catalog arm issues links with no variant id. Until a buyer follows one it is an offer,
    so it must not raise "Click rows are missing canonical variant ids" or count as a click row."""
    import services.merchant_commerce_diagnostics_service as diag

    clicks = [
        {"click_id": "clk_issued", "click_count": 0, "impression_count": 0, "canonical_variant_id": None},
        {"click_id": "clk_followed", "click_count": 1, "impression_count": 0, "canonical_variant_id": None},
        {"click_id": "clk_seen", "click_count": 0, "impression_count": 2, "canonical_variant_id": "v1"},
    ]

    async def fake_fetch_all(query, values=None):
        return clicks if "surface_click_events" in str(query) else []

    async def no_listings(*a, **k):
        return []

    monkeypatch.setattr(diag.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(diag, "fetch_listing_rows_with_catalog_fallback", no_listings)

    out = await diag.build_merchant_commerce_funnel_issues(merchant_id="m_anchor")

    missing = [i for i in out["issues"] if i["code"] == "MISSING_INFO"]
    assert len(missing) == 1 and missing[0]["count"] == 1
    assert [s["click_id"] for s in missing[0]["samples"]] == ["clk_followed"]
    assert out["summary"]["click_rows_total"] == 2


# --- the MCP door, end to end: route -> real resolver -> real verifier -> issued click -----------------


GATEWAY_KEY = "ak_live_" + "00" * 32


def test_an_mcp_link_is_issued_to_the_agent_the_gateway_signed_for(resolve_offers, monkeypatch):
    """The gateway's key plus a REALLY signed header, through the real invoke route, the real
    resolve_issuing_context_for_request and the real verifier. Only the key and agent lookups are stubbed."""
    import time

    from services.issuing_agent_assertion import sign_issuing_agent_assertion

    call, minted, _, batches = resolve_offers
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request", agent_auth.resolve_issuing_context_for_request)

    async def get_agent_by_key(api_key, metrics_out=None):
        return {GATEWAY_KEY: {"agent_id": GATEWAY_AGENT, "is_active": True},
                AGENT_KEY: {"agent_id": "agent_other", "is_active": True}}.get(api_key)

    async def get_agent(agent_id):
        return {"agent_id": agent_id, "is_active": True} if agent_id == "agent_minds" else None

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr("db.agents.get_agent", get_agent)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", "route_secret")

    def signed(sub, secret="route_secret"):
        return sign_issuing_agent_assertion(
            {"v": 1, "kind": "agent", "sub": sub, "op": "offers.resolve", "ts": int(time.time())}, secret)

    call({"X-API-Key": GATEWAY_KEY, "X-Pivota-Issuing-Agent": signed("agent_minds")})
    call({"X-API-Key": GATEWAY_KEY, "X-Pivota-Issuing-Agent": signed("agent_minds", secret="forged")})
    call({"X-API-Key": AGENT_KEY, "X-Pivota-Issuing-Agent": signed("agent_minds")})
    call({"X-Pivota-Issuing-Agent": signed("agent_minds")})

    assert [{c.agent_id for c in batch} for batch in batches] == [
        {"agent_minds"},   # the gateway vouched, signed with the shared secret
        {None},            # forged signature
        {"agent_other"},   # an agent's own key wins; it cannot vouch for another agent
        {None},            # no service identity, no vouching
    ]


def test_an_mcp_oauth_link_carries_its_platform_label_and_no_agent(resolve_offers, monkeypatch):
    """A public OAuth connector over MCP: the gateway's key plus a really signed OAuth assertion,
    through the real route, resolver and verifier. The issued clicks carry the UNVERIFIED platform
    label in their context and credit nobody. Only the DB lookups are stubbed."""
    import time

    import services.issuing_agent_assertion as ia
    from services.issuing_agent_assertion import sign_issuing_agent_assertion

    call, minted, _, batches = resolve_offers
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request",
                        agent_auth.resolve_issuing_context_for_request)

    async def get_agent_by_key(api_key, metrics_out=None):
        return {"agent_id": GATEWAY_AGENT, "is_active": True} if api_key == GATEWAY_KEY else None

    async def no_agent_for_client(issuer, client_id):
        return None

    async def platform(issuer, client_id):
        return "claude.ai" if client_id == "mcpc_public" else None

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(ia, "agent_for_oauth_client", no_agent_for_client)
    monkeypatch.setattr(ia, "oauth_platform_for_client", platform)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", "route_secret")
    token = sign_issuing_agent_assertion(
        {"v": 1, "kind": "oauth", "iss": "https://as.test", "cid": "mcpc_public", "op": "offers.resolve",
         "ts": int(time.time())}, "route_secret")

    call({"X-API-Key": GATEWAY_KEY, "X-Pivota-Issuing-Agent": token})

    assert len(batches) == 1 and [c.click_id for c in batches[0]] == minted
    assert {c.agent_id for c in batches[0]} == {None}
    for c in batches[0]:
        assert c.context["oauth_platform"] == "claude.ai"
        assert c.context["oauth_platform_verified"] is False
