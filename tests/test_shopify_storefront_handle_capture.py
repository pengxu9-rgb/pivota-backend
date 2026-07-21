"""Shopify storefront handle capture → attributed-redirect lane join.

Covers the Shopify half of the P2b emission gap (the Wix half shipped in
#1230): ShopifyProductAdapter.convert_to_standard set ``handle`` ONLY inside
platform_metadata, but the agent gateway serves
StandardProduct(**products_cache.product_data) directly — it never runs the
portal's _apply_storefront_fields lift — so cached connected-Shopify rows had
top-level handle=null and the redirect post-pass
(_attach_connected_product_redirects) skipped every connected-Shopify card:
no /r link, unattributed click-outs. Verified in prod 2026-07-08: 777/777
cached rows across all connected-active Shopify merchants had top-level
handle=null while platform_metadata.handle was present on all of them.

The fix mirrors the Wix capture: set TOP-LEVEL StandardProduct.handle at
convert time (platform_metadata.handle stays for the portal path). The
gateway then derives the destination from connected shop domain + handle, and
a numeric variant id upgrades the join to cart_permalink (order-side join).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.product_adapters import ShopifyProductAdapter  # noqa: E402
from models.standard_product import StandardProduct  # noqa: E402


def _sp(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": 15087315419502,
        "title": "Gentle Cleanser",
        "body_html": "<p>desc</p>",
        "handle": "gentle-cleanser",
        "status": "active",
        "vendor": "Brand",
        "product_type": "Skincare",
        "variants": [
            {
                "id": 55555555,
                "title": "Default",
                "price": "18.00",
                "inventory_quantity": 5,
                "inventory_management": None,
            }
        ],
    }
    base.update(over)
    return base


def test_convert_to_standard_captures_top_level_handle():
    product = ShopifyProductAdapter.convert_to_standard(_sp(), merchant_id="merch_shop")
    # Top-level field — what the gateway card builder + redirect post-pass read.
    assert product.handle == "gentle-cleanser"
    # platform_metadata — what the portal's _apply_storefront_fields reads.
    assert product.platform_metadata["handle"] == "gentle-cleanser"


def test_convert_to_standard_without_handle_never_fabricates():
    for absent in ({"handle": None}, {"handle": ""}, {"handle": "   "}):
        product = ShopifyProductAdapter.convert_to_standard(
            _sp(**absent), merchant_id="merch_shop"
        )
        assert product.handle is None


def test_cache_roundtrip_preserves_handle():
    """Serving-path contract: the Shopify sync caches json.loads(product.json())
    (services/shopify_products_sync) and the gateway rebuilds
    StandardProduct(**product_data). The handle must survive that roundtrip or
    the redirect post-pass never sees it."""
    product = ShopifyProductAdapter.convert_to_standard(_sp(), merchant_id="merch_shop")
    cached = json.loads(product.json())
    assert cached["handle"] == "gentle-cleanser"
    revived = StandardProduct(**cached)
    assert revived.handle == "gentle-cleanser"


def test_apply_storefront_fields_still_lifts_metadata_handle():
    """Portal path regression guard: a legacy payload with ONLY
    platform_metadata.handle (pre-fix cached rows) still resolves via
    _apply_storefront_fields."""
    from routes.product_routes_v2 import _apply_storefront_fields

    sp = StandardProduct(
        id="15087315419502",
        merchant_id="merch_shop",
        platform="shopify",
        title="Gentle Cleanser",
        price=18.0,
        platform_metadata={"handle": "gentle-cleanser"},
    )
    out = _apply_storefront_fields(sp)
    assert out.handle == "gentle-cleanser"


@pytest.mark.asyncio
async def test_shopify_card_joins_redirect_lane_cart_permalink(monkeypatch: pytest.MonkeyPatch):
    """End-to-end over the real card builder + post-pass + token mint: a
    connected-Shopify product card carries external_redirect_url whose decoded
    dest is the shop's cart permalink (numeric variant → join_mode=
    cart_permalink, order-side join) carrying the click id as an
    order-surviving cart attribute."""
    import routes.agent_shop_gateway as gw
    import services.merchant_store_service as mss
    from services.outbound_links_service import parse_and_verify_redirect_token

    async def fake_stores(merchant_id: str) -> List[Dict[str, Any]]:
        return [{"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)

    product = ShopifyProductAdapter.convert_to_standard(_sp(), merchant_id="merch_shop")
    card = gw._standard_to_shop_product(product)
    assert card["handle"] == "gentle-cleanser"

    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = parse_and_verify_redirect_token(token)
    dest = payload["dest"]
    assert dest.startswith("https://brand.myshopify.com/cart/55555555:1")
    assert "attributes%5Bpivota_click_id%5D=" in dest or "attributes[pivota_click_id]=" in dest
    ctx = payload.get("ctx") or {}
    assert ctx.get("join_mode") == "cart_permalink"
    assert ctx.get("merchant_id") == "merch_shop"


@pytest.mark.asyncio
async def test_shopify_card_without_numeric_variant_degrades_to_referral(
    monkeypatch: pytest.MonkeyPatch,
):
    """Non-numeric variant id → no cart permalink is fabricated; the card still
    joins the lane via shop domain + handle as a referral_only PDP link."""
    import routes.agent_shop_gateway as gw
    import services.merchant_store_service as mss
    from services.outbound_links_service import parse_and_verify_redirect_token

    async def fake_stores(merchant_id: str) -> List[Dict[str, Any]]:
        return [{"platform": "shopify", "domain": "brand.myshopify.com", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)

    product = ShopifyProductAdapter.convert_to_standard(_sp(), merchant_id="merch_shop")
    card = gw._standard_to_shop_product(product)
    for v in card["variants"]:
        v["variant_id"] = "SKU-ABC"
        v["id"] = "SKU-ABC"

    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = parse_and_verify_redirect_token(token)
    dest = payload["dest"]
    assert dest.startswith("https://brand.myshopify.com/products/gentle-cleanser")
    assert "pvt_click_id=" in dest and "utm_content=" in dest
    assert (payload.get("ctx") or {}).get("join_mode") == "referral_only"
