"""MCP lane — the merchant URL a product card publishes carries the click id the /r link logs.

Before this, every external-seed card carried a signed ``external_redirect_url`` (which stamps
our click id at click time) beside a RAW merchant URL. An agent that drives checkout from the
raw URL — the MCP lane hands it out — generated revenue we could not join. offers.resolve fixed
this for offers (T2-12, ``execution_spec``); these tests pin the same property on product cards:

  the click id on ``destination_url`` == the click id inside the signed token == ``tracking``.

The id is READ BACK OUT OF THE TOKEN, never minted a second time, and the published URL must be
exactly what the redirect resolves to — a token we did not sign, or inputs that disagree with the
mint, yield NO attribution rather than a wrong one.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

import routes.agent_api as agent_api
import routes.agent_shop_gateway as gw
from services.commerce_attribution_service import PVT_CLICK_ID
from services.outbound_links_service import (
    REFERRAL_CLICK_PARAM,
    SHOPIFY_CART_CLICK_ATTRIBUTE,
    parse_and_verify_redirect_token,
)


def _token_payload(redirect_url: str) -> Dict[str, Any]:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


def _query(url: str) -> Dict[str, list]:
    return parse_qs(urlparse(url).query)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch: pytest.MonkeyPatch):
    async def allowed(*, market: str):
        return ["example.com", "teststore.com"]

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", allowed)


def _candidate(**over: Any) -> Dict[str, Any]:
    base = {
        "external_seed_id": "seed_1",
        "external_product_id": "ext_1",
        "destination_url": "https://example.com/products/serum",
        "canonical_url": "https://example.com/products/serum",
        "market": "US",
        "tool": "*",
        "title": "Serum",
        "price": 12.0,
        "currency": "USD",
        "in_stock": True,
        "seed_data": {"title": "Serum"},
    }
    base.update(over)
    return base


# --- the product card: prefetched-seed lane -------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetched_wrapper_destination_carries_the_token_click_id():
    wrappers = await gw._build_prefetched_external_seed_wrappers(
        {"external_seed_candidates": [_candidate()]}
    )
    assert len(wrappers) == 1
    product = wrappers[0]["product"]

    payload = _token_payload(product["external_redirect_url"])
    click_id = payload["ctx"][PVT_CLICK_ID]
    assert click_id.startswith("clk_")

    q = _query(product["destination_url"])
    assert q[REFERRAL_CLICK_PARAM] == [click_id]
    assert q["utm_content"] == [click_id]
    assert q["utm_source"] == ["pivota"]
    parsed = urlparse(product["destination_url"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "example.com", "/products/serum")

    # Exactly what the redirect resolves to — not a second composition.
    assert product["destination_url"] == payload["dest"]
    assert product["tracking"] == {
        "click_id": click_id,
        "param": REFERRAL_CLICK_PARAM,
        "join_mode": "referral_only",
    }
    # The raw URL keeps its own key; offer_currency_policy reads the host off it.
    assert product["external_destination_url"] == "https://example.com/products/serum"
    assert "cart_url" not in product


@pytest.mark.asyncio
async def test_prefetched_wrapper_with_a_caller_supplied_redirect_reads_that_token():
    # The caller already minted the link (offers.resolve does); the card must carry THAT id,
    # not a fresh one — the whole point is one id across the redirect and the published URL.
    redirect_url = await gw._make_external_redirect_url(
        market="US",
        tool="*",
        destination_url="https://example.com/products/serum",
        utm_template=None,
        ctx={"seedId": "seed_1"},
        allowed_domains=["example.com"],
        cart_variant_id=None,
    )
    wrappers = await gw._build_prefetched_external_seed_wrappers(
        {"external_seed_candidates": [_candidate(external_redirect_url=redirect_url)]}
    )
    product = wrappers[0]["product"]
    assert product["external_redirect_url"] == redirect_url
    click_id = _token_payload(redirect_url)["ctx"][PVT_CLICK_ID]
    assert _query(product["destination_url"])[REFERRAL_CLICK_PARAM] == [click_id]
    assert product["tracking"]["click_id"] == click_id


@pytest.mark.asyncio
async def test_a_redirect_we_did_not_sign_yields_no_attribution_not_an_error():
    wrappers = await gw._build_prefetched_external_seed_wrappers(
        {"external_seed_candidates": [_candidate(external_redirect_url="https://example.com/r?token=not-ours-at-all")]}
    )
    product = wrappers[0]["product"]
    assert product["external_redirect_url"] == "https://example.com/r?token=not-ours-at-all"
    assert "destination_url" not in product
    assert "tracking" not in product


# --- the helper itself: cart permalinks, mismatches, the F1 host guard ----------------------------


async def _mint_cart_redirect(*, destination_url: str = "https://teststore.com/products/widget") -> str:
    return await gw._make_external_redirect_url(
        market="US",
        tool="*",
        destination_url=destination_url,
        utm_template=None,
        ctx={"seedId": "seed_cart"},
        allowed_domains=["teststore.com"],
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="46123456789",
        quantity=1,
    )


@pytest.mark.asyncio
async def test_shopify_cart_join_publishes_both_urls_under_one_click_id():
    redirect_url = await _mint_cart_redirect()
    attribution = gw._seed_attribution_from_redirect(
        redirect_url,
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        market="US",
        tool="*",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="46123456789",
    )
    assert attribution is not None
    payload = _token_payload(redirect_url)
    click_id = payload["ctx"][PVT_CLICK_ID]

    # The cart carries the id as a cart ATTRIBUTE (order-side join); the PDP as the plain param.
    assert attribution["cart_url"] == payload["dest"]
    assert attribution["cart_url"].startswith("https://teststore.com/cart/46123456789:1?")
    assert _query(attribution["cart_url"])[SHOPIFY_CART_CLICK_ATTRIBUTE] == [click_id]
    assert urlparse(attribution["destination_url"]).path == "/products/widget"
    assert _query(attribution["destination_url"])[REFERRAL_CLICK_PARAM] == [click_id]
    assert attribution["tracking"] == {
        "click_id": click_id,
        "param": SHOPIFY_CART_CLICK_ATTRIBUTE,
        "join_mode": "cart_permalink",
    }


@pytest.mark.asyncio
async def test_inputs_that_disagree_with_the_mint_are_refused():
    # Same token, composed with a different quantity: the URL we would publish is not the one the
    # redirect resolves to, so the helper must refuse rather than publish it with our click id.
    redirect_url = await _mint_cart_redirect()
    assert (
        gw._seed_attribution_from_redirect(
            redirect_url,
            destination_url="https://teststore.com/products/widget",
            utm_template=None,
            market="US",
            tool="*",
            shop_domain="teststore.com",
            platform="shopify",
            cart_variant_id="46123456789",
            quantity=2,
        )
        is None
    )


@pytest.mark.asyncio
async def test_pdp_on_a_host_the_allowlist_never_saw_is_withheld():
    # The allowlist vetted the CART host (teststore.com). The PDP comes from destination_url on a
    # different host; publishing it — with our click id on it — would be net-new egress nothing
    # approved. Cart + tracking still publish; destination_url does not.
    redirect_url = await _mint_cart_redirect(destination_url="https://cdn-mirror.example/products/widget")
    attribution = gw._seed_attribution_from_redirect(
        redirect_url,
        destination_url="https://cdn-mirror.example/products/widget",
        utm_template=None,
        market="US",
        tool="*",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="46123456789",
    )
    assert attribution is not None
    assert attribution["destination_url"] is None
    assert attribution["cart_url"].startswith("https://teststore.com/cart/")
    assert attribution["tracking"]["join_mode"] == "cart_permalink"

    product: Dict[str, Any] = {"id": "x"}
    gw._apply_seed_attribution(product, attribution)
    assert "destination_url" not in product
    assert product["cart_url"] == attribution["cart_url"]
    assert product["tracking"]["click_id"] == attribution["tracking"]["click_id"]


def test_no_redirect_means_no_attribution():
    assert (
        gw._seed_attribution_from_redirect(
            None,
            destination_url="https://example.com/p",
            utm_template=None,
            market="US",
            tool="*",
            shop_domain=None,
            platform=None,
            cart_variant_id=None,
        )
        is None
    )
    product: Dict[str, Any] = {"id": "x"}
    gw._apply_seed_attribution(product, None)
    assert product == {"id": "x"}


# --- the product card: find_products_multi seed lane (end to end through the handler) ------------


@pytest.mark.asyncio
async def test_find_products_multi_seed_card_destination_carries_the_token_click_id(
    monkeypatch: pytest.MonkeyPatch,
):
    seed_row = {
        "id": "eps_test_1",
        "external_product_id": "ext_test_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://example.com/products/gloss-bomb",
        "canonical_url": "https://example.com/products/gloss-bomb",
        "domain": "example.com",
        "title": "Gloss Bomb",
        "image_url": None,
        "price_amount": 19.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"brand": "Fenty Beauty"},
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": None,
    }

    async def fake_fetch_all(query: str, values=None):
        return []

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)

    async def fake_fetch_external_seed_rows(**kwargs):
        return {"rows": [seed_row], "query_timeout": False, "query_ms": 1, "total_count": 1}

    monkeypatch.setattr(gw, "fetch_external_seed_rows", fake_fetch_external_seed_rows)

    payload = gw.FindProductsMultiPayload(
        search=gw.MultiSearchFilters(query="fenty beauty gloss", page=1, limit=10, in_stock_only=False)
    )
    result = await gw._handle_find_products_multi(payload, {"source": "creator-agent-ui"}, gw.BackgroundTasks())
    card = next(p for p in result.get("products") or [] if p.get("source") == "external_seed")

    click_id = _token_payload(card["external_redirect_url"])["ctx"][PVT_CLICK_ID]
    assert _query(card["destination_url"])[REFERRAL_CLICK_PARAM] == [click_id]
    assert urlparse(card["destination_url"]).netloc == "example.com"
    assert card["tracking"]["click_id"] == click_id
    assert card["external_destination_url"] == "https://example.com/products/gloss-bomb"


# --- connected-merchant cards (P2b lane) --------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_card_gets_an_attributed_destination_under_its_token_click_id(
    monkeypatch: pytest.MonkeyPatch,
):
    import services.merchant_store_service as mss

    async def fake_stores(merchant_id: str):
        return [{"platform": "shopify", "domain": "conn-store.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)

    card: Dict[str, Any] = {
        "id": "9990001",
        "product_id": "9990001",
        "merchant_id": "merch_conn",
        "title": "Connected Serum",
        "platform": "shopify",
        "handle": "connected-serum",
        "variants": [{"variant_id": "46000000001", "id": "46000000001"}],
    }
    await gw._attach_connected_product_redirects([card], market="US", tool="find_products_multi")

    payload = _token_payload(card["external_redirect_url"])
    click_id = payload["ctx"][PVT_CLICK_ID]
    # Numeric Shopify variant → the signed destination is the cart; the card publishes both.
    assert card["cart_url"] == payload["dest"]
    assert _query(card["cart_url"])[SHOPIFY_CART_CLICK_ATTRIBUTE] == [click_id]
    assert urlparse(card["destination_url"]).path == "/products/connected-serum"
    assert _query(card["destination_url"])[REFERRAL_CLICK_PARAM] == [click_id]
    assert card["tracking"]["click_id"] == click_id
    assert card["tracking"]["join_mode"] == "cart_permalink"


@pytest.mark.asyncio
async def test_connected_card_never_overwrites_a_destination_it_already_had(
    monkeypatch: pytest.MonkeyPatch,
):
    import services.merchant_store_service as mss

    async def fake_stores(merchant_id: str):
        return [{"platform": "shopify", "domain": "conn-store.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)
    card: Dict[str, Any] = {
        "id": "9990002",
        "product_id": "9990002",
        "merchant_id": "merch_conn",
        "title": "Connected Toner",
        "platform": "shopify",
        "handle": "connected-toner",
        "destination_url": "https://already.example/set-by-someone-else",
        "variants": [],
    }
    await gw._attach_connected_product_redirects([card], market="US", tool="find_products_multi")
    assert "/r?token=" in card["external_redirect_url"]
    assert card["destination_url"] == "https://already.example/set-by-someone-else"
    assert card["tracking"]["click_id"] == _token_payload(card["external_redirect_url"])["ctx"][PVT_CLICK_ID]


# --- the agent_api lane (already minted the id; now publishes it) ---------------------------------


@pytest.mark.asyncio
async def test_agent_api_seed_product_publishes_the_click_id_it_signs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        agent_api, "should_block_external_referral_runtime", AsyncMock(return_value=(False, None))
    )
    metrics: Dict[str, Any] = {}
    product = await agent_api._build_external_seed_product(
        req=type("Req", (), {"base_url": "https://agent.pivota.cc/"})(),
        seed_row={
            "id": "seed_9",
            "external_product_id": "ext_9",
            "market": "US",
            "tool": "*",
            "destination_url": "https://example.com/p/9",
            "canonical_url": "https://example.com/p/9",
            "seed_data": {"title": "Nine"},
        },
        allowed_domains=[],
        metrics_out=metrics,
    )
    assert product is not None
    payload = _token_payload(product["external_redirect_url"])
    click_id = payload["ctx"][PVT_CLICK_ID]

    assert product["destination_url"] == payload["dest"]
    assert product["external_url"] == product["destination_url"]
    q = _query(product["destination_url"])
    assert q[REFERRAL_CLICK_PARAM] == [click_id]
    assert q["utm_content"] == [click_id]
    assert product["canonical_url"] == "https://example.com/p/9"
    assert product["tracking"] == {
        "click_id": click_id,
        "param": REFERRAL_CLICK_PARAM,
        "join_mode": "referral_only",
    }
