"""P2b — attributed /r links on CONNECTED-merchant product cards.

Covers `_attach_connected_product_redirects` (routes/agent_shop_gateway.py):
  - Shopify card w/ handle + connected domain → /r link; token dest = derived
    storefront PDP; numeric variant → cart_permalink join (order-side).
  - online_store_url on a custom domain still mints (dest host allowlisted).
  - external_seed / already-stamped / no-destination (Wix today) cards untouched.
  - one merchant-store lookup per merchant; mint deduped per (dest, variant).
No live API, no DB: merchant-store lookup is monkeypatched; the REAL
`_make_external_redirect_url` + token mint run (mint is pure CPU/HMAC).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_shop_gateway as gw  # noqa: E402
from services.outbound_links_service import parse_and_verify_redirect_token  # noqa: E402
import services.merchant_store_service as mss  # noqa: E402


def _token_payload(redirect_url: str) -> Dict[str, Any]:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


def _card(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "9990001",
        "product_id": "9990001",
        "merchant_id": "merch_conn",
        "title": "Connected Serum",
        "platform": "shopify",
        "handle": "connected-serum",
        "variants": [{"variant_id": "46000000001", "id": "46000000001"}],
    }
    base.update(over)
    return base


@pytest.fixture()
def _stores(monkeypatch: pytest.MonkeyPatch):
    calls: List[str] = []

    async def fake_stores(merchant_id: str):
        calls.append(merchant_id)
        return [{"platform": "shopify", "domain": "conn-store.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)
    return calls


@pytest.mark.asyncio
async def test_shopify_card_gets_cart_permalink_join(_stores) -> None:
    card = _card()
    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    payload = _token_payload(url)
    dest = payload["dest"]
    # numeric variant + shop domain → cart permalink with the order-surviving attribute
    assert "conn-store.myshopify.com/cart/46000000001:1" in dest
    assert "attributes%5Bpivota_click_id%5D=" in dest or "attributes[pivota_click_id]=" in dest
    ctx = payload.get("ctx") or {}
    assert ctx.get("merchant_id") == "merch_conn"
    assert ctx.get("join_mode") == "cart_permalink"


@pytest.mark.asyncio
async def test_custom_domain_online_store_url_mints(_stores) -> None:
    card = _card(online_store_url="https://brand.example/products/connected-serum", variants=[])
    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    payload = _token_payload(url)
    assert payload["dest"].startswith("https://brand.example/products/connected-serum")
    assert (payload.get("ctx") or {}).get("join_mode") == "referral_only"
    # referral carrier params present for click-side + Woo-style order-side joins
    assert "pvt_click_id=" in payload["dest"] and "utm_content=" in payload["dest"]


@pytest.mark.asyncio
async def test_skips_external_seed_stamped_and_unresolvable(_stores) -> None:
    seed = _card(source="external_seed", external_redirect_url=None)
    stamped = _card(external_redirect_url="https://api.pivota.cc/r?token=x.y")
    wix = _card(platform="wix", handle="", online_store_url="", variants=[])
    await gw._attach_connected_product_redirects([seed, stamped, wix], market="US")

    assert not seed.get("external_redirect_url")
    assert stamped["external_redirect_url"] == "https://api.pivota.cc/r?token=x.y"
    assert not wix.get("external_redirect_url")


@pytest.mark.asyncio
async def test_one_store_lookup_per_merchant_and_mint_dedup(_stores) -> None:
    cards = [_card(id=str(i), product_id=str(i)) for i in range(4)]  # same merchant
    await gw._attach_connected_product_redirects(cards, market="US")

    assert _stores.count("merch_conn") == 1
    assert all(c.get("external_redirect_url") for c in cards)


@pytest.mark.asyncio
async def test_fail_soft_on_store_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(merchant_id: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(mss, "get_merchant_active_stores", boom)
    card = _card()
    await gw._attach_connected_product_redirects([card], market="US")
    # no domain → shopify handle path unavailable → card unchanged, no raise
    assert not card.get("external_redirect_url")


# ---- A-F1.3: connected-WooCommerce fallback destination ----

@pytest.fixture()
def _woo_store(monkeypatch: pytest.MonkeyPatch):
    async def fake_stores(merchant_id: str):
        return [{"platform": "woocommerce", "domain": "woostore.example", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)


@pytest.mark.asyncio
async def test_woo_card_default_permalink_from_handle(_woo_store) -> None:
    """No online_store_url → derive the WooCommerce default /product/<slug>
    base from the connected Woo store domain (referral_only join)."""
    card = _card(platform="woocommerce", handle="blue-widget", online_store_url="", variants=[])
    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    payload = _token_payload(url)
    assert payload["dest"].startswith("https://woostore.example/product/blue-widget")
    assert (payload.get("ctx") or {}).get("join_mode") == "referral_only"
    assert "utm_content=" in payload["dest"]


@pytest.mark.asyncio
async def test_woo_real_permalink_wins_over_default_base(_woo_store) -> None:
    """A real permalink captured at sync (online_store_url, possibly a custom
    permalink structure) takes precedence over the /product/<slug> default."""
    card = _card(
        platform="woocommerce",
        handle="blue-widget",
        online_store_url="https://woostore.example/shop/cat/blue-widget-123",
        variants=[],
    )
    await gw._attach_connected_product_redirects([card], market="US")
    payload = _token_payload(card["external_redirect_url"])
    assert payload["dest"].startswith("https://woostore.example/shop/cat/blue-widget-123")


@pytest.mark.asyncio
async def test_woo_card_without_handle_is_skipped(_woo_store) -> None:
    """No permalink and no slug → never fabricate a destination."""
    card = _card(platform="woocommerce", handle="", online_store_url="", variants=[])
    await gw._attach_connected_product_redirects([card], market="US")
    assert not card.get("external_redirect_url")
