"""MERCHANT PURCHASABILITY on the product-CARD lanes — the mints offers.resolve's gate did not reach.

offers.resolve asks `_CartPurchasabilityGate` before it mints a `cart_permalink` /r link
(tests/test_offers_resolve.py). The card lanes minted carts for the same seeds without asking:
`mint_external_seed_links` (POST /attribution/external-seed-links), the find_products_multi seed
lane, `_build_prefetched_external_seed_wrappers`, and `_attach_connected_product_redirects`. Every
test below drives the lane's REAL entry point with the REAL `_make_external_redirect_url`, and
reads the answer off the card the lane emitted and the token it signed. Only the fact read
(`db.merchant_purchasability.human_handoff_allowed`) and the stores the lanes read from are faked.

What a decline must look like, on every lane:
  * the card's `cart_url` is gone and its `tracking.join_mode` is `referral_only` — which also
    proves the recompose (`_seed_attribution_from_redirect`, which REFUSES unless its primary
    equals the signed dest) was handed the same nulled cart id the mint was;
  * the signed dest is the PDP and the signed ctx carries `purchasability_tier: browse_only`, so
    the warm lane knocks the click out as `purchase_declined` instead of rebuilding the cart;
  * the fact is keyed on the cart's host x the REQUEST's market — never a row's market.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import routes.agent_shop_gateway as gw
import services.merchant_store_service as mss
from main import app
from routes.agent_auth import AgentContext, get_agent_context
from services.outbound_links_service import parse_and_verify_redirect_token
from services.outbound_warm_handoff import evaluate_warm_eligibility

HOST = "teststore.com"
PDP = f"https://{HOST}/products/widget"
SHOPIFY_VARIANT = "46123456789"
_HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Storefront evidence the identity builder accepts for a Shopify cart permalink (the same shape
#: tests/test_external_seed_links_mint_endpoint.py uses for its cart case).
_SHOPIFY_SNAPSHOT = {
    "snapshot": {
        "storefront_platform": "shopify",
        "variants": [{"shopify_variant_id": SHOPIFY_VARIANT, "title": "Default"}],
    }
}


# ── shared fakes and readers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch):
    async def allowed(*, market: str):
        return [HOST, "conn-store.myshopify.com"]

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", allowed)


def _enforce(monkeypatch: pytest.MonkeyPatch, *, on: bool = True) -> None:
    if on:
        monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    else:
        monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)


def _fact(monkeypatch: pytest.MonkeyPatch, *, purchasable: bool) -> List[tuple]:
    """Replace ONLY the fact read with a recording answer; the gate and the lanes are real."""
    import db.merchant_purchasability as mp

    calls: List[tuple] = []

    async def fake_human_handoff_allowed(domain, market):
        calls.append((domain, market))
        return purchasable

    monkeypatch.setattr(mp, "human_handoff_allowed", fake_human_handoff_allowed)
    return calls


def _payload(redirect_url: str) -> Dict[str, Any]:
    return parse_and_verify_redirect_token(parse_qs(urlparse(redirect_url).query)["token"][0])


@pytest.fixture
def warm_settings(monkeypatch: pytest.MonkeyPatch):
    """The warm canary as deployed, allowlisting this very host — prod posture for the knockout."""
    from config.settings import settings as app_settings

    monkeypatch.setattr(app_settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_brands_raw", f"{HOST},conn-store.myshopify.com")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_rollout_pct", 0)
    return app_settings


def _warm_verdict(redirect_url: str, settings) -> tuple:
    payload = _payload(redirect_url)
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return evaluate_warm_eligibility(
        dest=payload["dest"], user_agent=_HUMAN_UA, token=token, ctx=payload["ctx"], settings=settings,
    )


def _assert_declined_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """A card the fact declined: attributed PDP, no cart, the decline signed into the token."""
    assert not card.get("cart_url"), card
    assert card["tracking"]["join_mode"] == "referral_only", (
        "no tracking means the recompose disagreed with the token: it was handed a different "
        f"cart id than the mint. card={card}"
    )
    assert card["destination_url"] and "/cart/" not in card["destination_url"]
    payload = _payload(card["external_redirect_url"])
    assert "/cart/" not in payload["dest"], "the signed /r destination must be the PDP"
    assert payload["ctx"]["join_mode"] == "referral_only"
    assert payload["ctx"]["purchasability_tier"] == "browse_only"
    assert payload["ctx"]["pvt_click_id"] == card["tracking"]["click_id"]
    return payload


def _assert_cart_card(card: Dict[str, Any]) -> None:
    assert card["cart_url"] and "/cart/" in card["cart_url"], card
    assert card["tracking"]["join_mode"] == "cart_permalink"
    payload = _payload(card["external_redirect_url"])
    assert "/cart/" in payload["dest"]
    assert payload["ctx"]["join_mode"] == "cart_permalink"
    assert "purchasability_tier" not in payload["ctx"], (
        "an undeclined token must be byte-identical to a pre-gate one")


# ── 1. POST /attribution/external-seed-links (the gateway's JS-built cards) ────────────────────

LINKS_PATH = "/agent/shop/v1/attribution/external-seed-links"


@pytest.fixture
def client():
    async def fake_context() -> AgentContext:
        req = Request({
            "type": "http", "method": "POST", "path": LINKS_PATH, "headers": [],
            "query_string": b"", "client": ("127.0.0.1", 0), "scheme": "https",
            "server": ("api.pivota.cc", 443),
        })
        return AgentContext({"agent_id": "agent_gateway", "agent_name": "Gateway", "allowed_merchants": None}, req)

    app.dependency_overrides[get_agent_context] = fake_context
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def _link_candidate(**over: Any) -> Dict[str, Any]:
    base = {
        "external_seed_id": "seed_cart", "external_product_id": "ext_cart",
        "destination_url": PDP, "canonical_url": PDP, "domain": HOST,
        # The SEED ROW's market (the gateway's contract): where the card is LISTED.
        "market": "US", "tool": "*", "seed_data": _SHOPIFY_SNAPSHOT,
    }
    base.update(over)
    return base


def _mint_links(client: TestClient, *, market: Any, candidates: Optional[list] = None) -> list:
    body: Dict[str, Any] = {"tool": "search_catalog", "candidates": candidates or [_link_candidate()]}
    if market is not None:
        body["market"] = market
    res = client.post(LINKS_PATH, json=body)
    assert res.status_code == 200, res.text
    return res.json()["links"]


def test_links_a_browse_only_merchant_is_minted_a_referral_keyed_on_the_request_market(
    monkeypatch, client, warm_settings
):
    """The candidate is LISTED in US; the buyer's request named SG. The fact is asked for SG."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    (link,) = _mint_links(client, market="SG")
    assert calls == [(HOST, "SG")]
    _assert_declined_card(link)
    assert _warm_verdict(link["external_redirect_url"], warm_settings) == (False, "purchase_declined")


def test_links_a_purchasable_merchant_keeps_its_cart(monkeypatch, client):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (link,) = _mint_links(client, market="us")
    assert calls == [(HOST, "US")]
    _assert_cart_card(link)
    assert link["cart_url"].startswith(f"https://{HOST}/cart/{SHOPIFY_VARIANT}:1?")


@pytest.mark.parametrize("market", [None, "", "   "])
def test_links_no_request_market_declines_without_asking_even_when_the_row_names_one(
    monkeypatch, client, market
):
    """`candidate.market` "US" and the `or "US"` default the SERVED market falls back to are
    both non-observations of the buyer; the ops route's `market_unknown` is a decline."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (link,) = _mint_links(client, market=market)
    assert calls == [], "no market is answered without a database read"
    payload = _assert_declined_card(link)
    assert payload["market"] == "US", "the SERVED market is untouched"


def test_links_the_dial_off_mints_exactly_what_it_minted_before(monkeypatch, client):
    _enforce(monkeypatch, on=False)
    calls = _fact(monkeypatch, purchasable=False)
    (link,) = _mint_links(client, market="US")
    assert calls == []
    _assert_cart_card(link)


def test_links_a_referral_only_candidate_never_asks(monkeypatch, client):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    (link,) = _mint_links(client, market="US", candidates=[_link_candidate(seed_data=None)])
    assert calls == []
    assert link["cart_url"] is None
    assert "purchasability_tier" not in _payload(link["external_redirect_url"])["ctx"], (
        "only a DECLINE is signed")


def test_links_one_body_asks_once_per_host(monkeypatch, client):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    links = _mint_links(client, market="US", candidates=[
        _link_candidate(),
        _link_candidate(external_seed_id="seed_cart_2", external_product_id="ext_cart_2",
                        destination_url=f"https://{HOST}/products/other",
                        canonical_url=f"https://{HOST}/products/other"),
    ])
    assert len(links) == 2
    assert calls == [(HOST, "US")]
    for link in links:
        _assert_declined_card(link)


# ── 2. find_products_multi seed lane (through `_handle_find_products_multi`) ────────────────────

_MULTI_ROW: Dict[str, Any] = {
    "id": "eps_cart_1", "external_product_id": "ext_cart_1", "market": "US", "tool": "*",
    "utm_template": None, "partner_type": None, "disclosure_text": None,
    "destination_url": PDP, "canonical_url": PDP, "domain": HOST,
    "title": "Fenty Gloss Widget", "image_url": None, "price_amount": 19.0, "price_currency": "USD",
    "availability": "in_stock", "seed_data": {"brand": "Fenty Beauty", **_SHOPIFY_SNAPSHOT},
    "status": "active", "notes": None, "created_by_employee_id": None,
    "attached_product_key": None, "attached_variant_id": None, "created_at": None, "updated_at": None,
}


async def _multi_seed_cards(monkeypatch, rows, *, search_market=None, meta_market=None) -> list:
    async def fake_fetch_all(query: str, values=None):
        return []

    async def fake_rows(**kwargs):
        return {"rows": [dict(r) for r in rows], "query_timeout": False, "query_ms": 1,
                "total_count": len(rows)}

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "fetch_external_seed_rows", fake_rows)
    raw: Dict[str, Any] = {"search": {"query": "fenty beauty gloss", "page": 1, "limit": 10,
                                      "in_stock_only": False}}
    if search_market is not None:
        raw["search"]["market"] = search_market
    meta: Dict[str, Any] = {"source": "creator-agent-ui"}
    if meta_market is not None:
        meta["market"] = meta_market
    payload = gw.FindProductsMultiPayload(**gw._normalize_find_products_multi_payload(raw))
    result = await gw._handle_find_products_multi(payload, meta, gw.BackgroundTasks())
    cards = [p for p in result.get("products") or [] if p.get("source") == "external_seed"]
    assert len(cards) == len(rows), result.get("metadata")
    return cards


async def test_multi_a_browse_only_merchant_card_is_a_referral_keyed_on_the_request_market(
    monkeypatch, warm_settings
):
    """Row LISTED in US (and SERVED as "US"); the buyer named SG in search.market."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    (card,) = await _multi_seed_cards(monkeypatch, [_MULTI_ROW], search_market="SG")
    assert calls == [(HOST, "SG")]
    payload = _assert_declined_card(card)
    assert payload["market"] == "US", "the served market is the row's, untouched"
    assert _warm_verdict(card["external_redirect_url"], warm_settings) == (False, "purchase_declined")


async def test_multi_the_metadata_market_is_the_request_market_too(monkeypatch):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (card,) = await _multi_seed_cards(monkeypatch, [_MULTI_ROW], meta_market="us")
    assert calls == [(HOST, "US")]
    _assert_cart_card(card)


async def test_multi_a_market_less_request_declines_without_asking(monkeypatch):
    """96% of find_products_multi requests that reached this door in the 14 days to 2026-09-27
    named no market (389 of 404 caller requests, `multi.invoke.market`). Under enforcement they
    get no cart — the row's "US" is not the buyer's market."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (card,) = await _multi_seed_cards(monkeypatch, [_MULTI_ROW])
    assert calls == []
    _assert_declined_card(card)


async def test_multi_the_dial_off_mints_exactly_what_it_minted_before(monkeypatch):
    _enforce(monkeypatch, on=False)
    calls = _fact(monkeypatch, purchasable=False)
    (card,) = await _multi_seed_cards(monkeypatch, [_MULTI_ROW], search_market="US")
    assert calls == []
    _assert_cart_card(card)


async def test_multi_one_request_asks_once_per_host(monkeypatch):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    second = dict(_MULTI_ROW, id="eps_cart_2", external_product_id="ext_cart_2",
                  destination_url=f"https://{HOST}/products/other",
                  canonical_url=f"https://{HOST}/products/other", title="Fenty Gloss Other")
    cards = await _multi_seed_cards(monkeypatch, [_MULTI_ROW, second], search_market="US")
    assert calls == [(HOST, "US")]
    for card in cards:
        _assert_declined_card(card)


# ── 3. prefetched external-seed wrappers ─────────────────────────────────────────────────────


def _prefetched_candidate(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "ext_prefetch_1", "product_id": "ext_prefetch_1", "external_seed_id": "seed_prefetch_1",
        "title": "Niacinamide Widget Serum", "price": 22.0, "currency": "USD",
        "product_type": "Serum", "category": "Serum", "source": "external_seed",
        # LISTED in US.
        "market": "US", "tool": "*", "in_stock": True, "availability": "in_stock",
        "ingredient_ids": ["niacinamide"], "canonical_url": PDP, "destination_url": PDP,
        "domain": HOST, "seed_data": _SHOPIFY_SNAPSHOT,
    }
    base.update(over)
    return base


async def test_prefetched_a_browse_only_merchant_is_a_referral_on_the_request_market(
    monkeypatch, warm_settings
):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    (wrapper,) = await gw._build_prefetched_external_seed_wrappers(
        {"market": "SG", "external_seed_candidates": [_prefetched_candidate()]}
    )
    assert calls == [(HOST, "SG")], "the request's metadata.market, never the candidate's US"
    _assert_declined_card(wrapper["product"])
    assert _warm_verdict(wrapper["product"]["external_redirect_url"], warm_settings) == (
        False, "purchase_declined")


async def test_prefetched_a_market_less_request_declines_without_asking(monkeypatch):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (wrapper,) = await gw._build_prefetched_external_seed_wrappers(
        {"external_seed_candidates": [_prefetched_candidate()]}
    )
    assert calls == []
    _assert_declined_card(wrapper["product"])


async def test_prefetched_a_purchasable_merchant_keeps_its_cart(monkeypatch):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    (wrapper,) = await gw._build_prefetched_external_seed_wrappers(
        {"external_seed_candidates": [_prefetched_candidate()]}, request_market="US"
    )
    assert calls == [(HOST, "US")]
    _assert_cart_card(wrapper["product"])


async def test_prefetched_through_find_products_multi_is_gated_on_the_request_market(monkeypatch):
    """The handler hands the prefetched builder its own gate, built on `_request_market_for_multi`
    (search.market first). Driven through `_handle_find_products_multi` with the prefetched
    candidates the gateway sends in metadata."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)

    async def fake_fetch_all(query: str, values=None):
        return []

    async def fake_get_products_hybrid(merchant_id, limit, agent_id, background_tasks=None,
                                       force_cache_only=False):
        return [], "cache_all_platforms", None

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "get_products_hybrid", fake_get_products_hybrid)
    monkeypatch.setattr(gw, "MULTI_SEARCH_DELEGATE_SHOPPING_TO_UPSTREAM", False)
    monkeypatch.setattr(gw, "MULTI_SEARCH_SKIP_HISTORY_SHOPPING", True)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT_SHOPPING", False)
    payload = gw.FindProductsMultiPayload(
        search=gw.MultiSearchFilters(query="niacinamide serum", page=1, limit=5, in_stock_only=True,
                                     commerce_surface="agent_api", market="JP"),
        metadata=gw.RequestMetadata(source="shopping_agent"),
    )
    result = await gw._handle_find_products_multi(
        payload,
        {"source": "shopping_agent", "market": "SG",
         "external_seed_candidates": [_prefetched_candidate()]},
        gw.BackgroundTasks(),
    )
    cards = [p for p in result.get("products") or [] if p.get("source") == "external_seed"]
    assert len(cards) == 1, result.get("metadata")
    assert calls == [(HOST, "JP")], "search.market wins over metadata.market; the row's US never"
    _assert_declined_card(cards[0])


async def test_one_find_products_multi_request_asks_once_per_host_across_both_seed_lanes(monkeypatch):
    """The ranked seed lane and the prefetched wrappers share ONE gate per request, so a host
    both lanes serve costs one fact read, not one per lane."""
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)

    async def fake_fetch_all(query: str, values=None):
        return []

    async def fake_rows(**kwargs):
        return {"rows": [dict(_MULTI_ROW)], "query_timeout": False, "query_ms": 1, "total_count": 1}

    # The REAL builder, recorded: ranking may drop its card from the served slate, but what it
    # built (and what it asked) is the point here.
    real_builder = gw._build_prefetched_external_seed_wrappers
    built: List[dict] = []

    async def recording_builder(*args, **kwargs):
        out = await real_builder(*args, **kwargs)
        built.extend(out)
        return out

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "fetch_external_seed_rows", fake_rows)
    monkeypatch.setattr(gw, "_build_prefetched_external_seed_wrappers", recording_builder)
    raw = {"search": {"query": "fenty beauty gloss", "page": 1, "limit": 10, "in_stock_only": False,
                      "market": "US"}}
    payload = gw.FindProductsMultiPayload(**gw._normalize_find_products_multi_payload(raw))
    result = await gw._handle_find_products_multi(
        payload,
        {"source": "creator-agent-ui", "external_seed_candidates": [
            _prefetched_candidate(title="Fenty Gloss Prefetched Widget")]},
        gw.BackgroundTasks(),
    )
    ranked = [p for p in result.get("products") or [] if p.get("external_seed_id") == "eps_cart_1"]
    prefetched = [w["product"] for w in built]
    assert len(ranked) == 1 and len(prefetched) == 1, (
        "both lanes must mint on the host for this to prove anything")
    assert calls == [(HOST, "US")]
    for card in ranked + prefetched:
        _assert_declined_card(card)


# ── 4. connected-merchant cards (`_attach_connected_product_redirects`) ───────────────────────


def _connected_card(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "9990001", "product_id": "9990001", "merchant_id": "merch_conn",
        "title": "Connected Serum", "platform": "shopify", "handle": "connected-serum",
        # Priced, so find_products_multi's price contract serves it.
        "price": 24.0, "currency": "USD",
        "variants": [{"variant_id": "46000000001", "id": "46000000001"}],
    }
    base.update(over)
    return base


@pytest.fixture
def _stores(monkeypatch: pytest.MonkeyPatch):
    async def fake_stores(merchant_id: str):
        return [{"platform": "shopify", "domain": "conn-store.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)


async def _multi_connected_card(monkeypatch, *, search_market=None, meta_market=None) -> dict:
    """Through the find_products_multi WRAPPER, which is where connected cards are stamped on
    every return branch — so the request market it hands the stamping pass is what is tested."""
    card = _connected_card()

    async def fake_inner(payload, request_metadata, background_tasks):
        return {"products": [card], "total": 1, "page": 1, "page_size": 1, "metadata": {}}

    monkeypatch.setattr(gw, "_handle_find_products_multi_inner", fake_inner)
    raw: Dict[str, Any] = {"search": {"query": "serum", "page": 1, "limit": 10}}
    if search_market is not None:
        raw["search"]["market"] = search_market
    meta: Dict[str, Any] = {"source": "creator-agent-ui"}
    if meta_market is not None:
        meta["market"] = meta_market
    payload = gw.FindProductsMultiPayload(**gw._normalize_find_products_multi_payload(raw))
    result = await gw._handle_find_products_multi(
        payload, meta, gw.BackgroundTasks(), emit_decision_event=False)
    (out,) = result["products"]
    return out


async def test_connected_a_browse_only_store_card_is_a_referral_keyed_on_the_request_market(
    monkeypatch, _stores, warm_settings
):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=False)
    card = await _multi_connected_card(monkeypatch, search_market="SG")
    assert calls == [("conn-store.myshopify.com", "SG")]
    payload = _assert_declined_card(card)
    assert payload["dest"].startswith("https://conn-store.myshopify.com/products/connected-serum")
    assert payload["ctx"]["pvt_variant_id"] == "46000000001", (
        "the variant stays the attribution value it always was; only the CART is withdrawn")
    assert payload["market"] == "US", "the connected lane's served market is untouched"
    assert "market_observed" not in payload, "and so is its provenance"
    assert _warm_verdict(card["external_redirect_url"], warm_settings) == (False, "purchase_declined")


async def test_connected_a_purchasable_store_keeps_its_cart(monkeypatch, _stores):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    card = await _multi_connected_card(monkeypatch, meta_market="US")
    assert calls == [("conn-store.myshopify.com", "US")]
    _assert_cart_card(card)


async def test_connected_a_market_less_request_declines_without_asking(monkeypatch, _stores):
    _enforce(monkeypatch)
    calls = _fact(monkeypatch, purchasable=True)
    card = await _multi_connected_card(monkeypatch)
    assert calls == []
    _assert_declined_card(card)


async def test_connected_the_dial_off_mints_exactly_what_it_minted_before(monkeypatch, _stores):
    _enforce(monkeypatch, on=False)
    calls = _fact(monkeypatch, purchasable=False)
    card = await _multi_connected_card(monkeypatch, search_market="US")
    assert calls == []
    _assert_cart_card(card)


def test_connected_get_product_detail_hands_the_metadata_market_to_the_gate(monkeypatch):
    """The invoke door's find_products / get_product_detail payloads carry no market; the gate
    reads the envelope's `metadata.market`. Pinned at the door, with the handler recorded."""
    seen: Dict[str, Any] = {}

    async def fake_detail(ref, background_tasks, *, request_market=None):
        seen["request_market"] = request_market
        return {"product": {}}

    async def fake_find(filters, background_tasks, *, request_market=None):
        seen["find_request_market"] = request_market
        return {"products": [], "total": 0, "page": 1, "page_size": 0}

    monkeypatch.setattr(gw, "_handle_get_product_detail", fake_detail)
    monkeypatch.setattr(gw, "_handle_find_products", fake_find)
    monkeypatch.setattr(gw, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    client = TestClient(app)
    res = client.post("/agent/shop/v1/invoke", json={
        "operation": "get_product_detail",
        "payload": {"product": {"merchant_id": "merch_conn", "product_id": "9990001"}},
        "metadata": {"source": "creator-agent-ui", "market": "SG"},
    })
    assert res.status_code == 200, res.text
    res = client.post("/agent/shop/v1/invoke", json={
        "operation": "find_products",
        "payload": {"search": {"merchant_id": "merch_conn", "query": "serum"}},
        "metadata": {"source": "creator-agent-ui", "market": "JP"},
    })
    assert res.status_code == 200, res.text
    assert seen == {"request_market": "SG", "find_request_market": "JP"}
