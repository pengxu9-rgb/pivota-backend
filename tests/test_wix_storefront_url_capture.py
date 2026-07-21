"""Wix storefront URL capture → attributed-redirect lane join.

Covers the Wix half of the P2b emission gap: WixProductAdapter._convert_product
must capture the public product-page URL (Wix ``productPageUrl`` base+path
parts, or a plain string) and ``slug`` as
  - TOP-LEVEL StandardProduct fields (handle / online_store_url) — the agent
    gateway serves StandardProduct(**products_cache.product_data), so only
    top-level fields survive to the card builder, and
  - platform_metadata permalink/slug — what the portal's products-v2 mapper
    (_apply_storefront_fields) reads.
Plus the end-to-end proof: a Wix card built from the converted product gets an
/r link whose decoded token dest is the Wix product page with
join_mode=referral_only and both referral carriers (pvt_click_id, utm_content).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.product_adapters import WixProductAdapter  # noqa: E402
from models.standard_product import StandardProduct  # noqa: E402


def _wp(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "prod_wix_1",
        "name": "Gentle Cleanser",
        "description": "desc",
        "visible": True,
        "slug": "gentle-cleanser",
        "productPageUrl": {
            "base": "https://www.wixbrand.com/",
            "path": "/product-page/gentle-cleanser",
        },
        "priceData": {"price": 18.0, "currency": "USD"},
        "stock": {"quantity": 5, "inStock": True, "trackQuantity": True},
    }
    base.update(over)
    return base


def test_convert_product_captures_storefront_url_and_slug():
    product = WixProductAdapter._convert_product(_wp(), merchant_id="merch_wix")
    assert product is not None
    # Top-level fields — what the gateway card builder reads.
    assert product.online_store_url == "https://www.wixbrand.com/product-page/gentle-cleanser"
    assert product.handle == "gentle-cleanser"
    # platform_metadata — what _apply_storefront_fields reads (portal path).
    assert product.platform_metadata["permalink"] == (
        "https://www.wixbrand.com/product-page/gentle-cleanser"
    )
    assert product.platform_metadata["slug"] == "gentle-cleanser"


def test_convert_product_accepts_string_product_page_url():
    wp = _wp(productPageUrl="https://www.wixbrand.com/product-page/gentle-cleanser")
    product = WixProductAdapter._convert_product(wp, merchant_id="merch_wix")
    assert product.online_store_url == "https://www.wixbrand.com/product-page/gentle-cleanser"


def test_convert_product_without_page_url_never_fabricates():
    wp = _wp()
    del wp["productPageUrl"]
    del wp["slug"]
    product = WixProductAdapter._convert_product(wp, merchant_id="merch_wix")
    assert product.online_store_url is None
    assert product.handle is None
    assert product.platform_metadata["permalink"] is None
    assert product.platform_metadata["slug"] is None


def test_convert_product_relative_or_partial_page_url_rejected():
    # base not absolute → reject; path missing → reject (base alone is the
    # site root, not a product page).
    for bad in ({"base": "wixbrand.com", "path": "/p/x"}, {"base": "https://w.com/"}, "not-a-url"):
        product = WixProductAdapter._convert_product(
            _wp(productPageUrl=bad, slug=""), merchant_id="merch_wix"
        )
        assert product.online_store_url is None


def test_slug_derived_from_page_url_when_slug_field_missing():
    wp = _wp(slug="")
    product = WixProductAdapter._convert_product(wp, merchant_id="merch_wix")
    assert product.handle == "gentle-cleanser"


def test_cache_roundtrip_preserves_storefront_fields():
    """Serving-path contract: universal sync caches json.loads(product.json())
    and the gateway rebuilds StandardProduct(**product_data). The storefront
    fields must survive that roundtrip or the redirect post-pass never sees
    them."""
    product = WixProductAdapter._convert_product(_wp(), merchant_id="merch_wix")
    cached = json.loads(product.json())
    assert cached["online_store_url"] == "https://www.wixbrand.com/product-page/gentle-cleanser"
    assert cached["handle"] == "gentle-cleanser"
    revived = StandardProduct(**cached)
    assert revived.online_store_url == product.online_store_url
    assert revived.handle == product.handle


def test_apply_storefront_fields_lifts_wix_metadata():
    """Portal path: a payload with ONLY platform_metadata permalink/slug (no
    top-level fields) still resolves via _apply_storefront_fields."""
    from routes.product_routes_v2 import _apply_storefront_fields

    sp = StandardProduct(
        id="prod_wix_1",
        merchant_id="merch_wix",
        platform="wix",
        title="Gentle Cleanser",
        price=18.0,
        platform_metadata={
            "permalink": "https://www.wixbrand.com/product-page/gentle-cleanser",
            "slug": "gentle-cleanser",
        },
    )
    out = _apply_storefront_fields(sp)
    assert out.online_store_url == "https://www.wixbrand.com/product-page/gentle-cleanser"
    assert out.handle == "gentle-cleanser"


@pytest.mark.asyncio
async def test_wix_card_joins_redirect_lane_referral_only(monkeypatch: pytest.MonkeyPatch):
    """End-to-end over the real card builder + post-pass + token mint: a
    connected-Wix product card carries external_redirect_url whose decoded
    dest is the Wix product page (referral_only join, pvt_click_id +
    utm_content carriers present)."""
    import routes.agent_shop_gateway as gw
    import services.merchant_store_service as mss
    from services.outbound_links_service import parse_and_verify_redirect_token

    async def fake_stores(merchant_id: str) -> List[Dict[str, Any]]:
        return [{"platform": "wix", "domain": "site_abc123", "status": "active"}]

    monkeypatch.setattr(mss, "get_merchant_active_stores", fake_stores)

    product = WixProductAdapter._convert_product(_wp(), merchant_id="merch_wix")
    card = gw._standard_to_shop_product(product)
    assert card["online_store_url"] == "https://www.wixbrand.com/product-page/gentle-cleanser"

    await gw._attach_connected_product_redirects([card], market="US", tool="find_products")

    url = card.get("external_redirect_url")
    assert url and "/r?token=" in url
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = parse_and_verify_redirect_token(token)
    dest = payload["dest"]
    assert dest.startswith("https://www.wixbrand.com/product-page/gentle-cleanser")
    assert "pvt_click_id=" in dest and "utm_content=" in dest
    ctx = payload.get("ctx") or {}
    assert ctx.get("join_mode") == "referral_only"
    assert ctx.get("merchant_id") == "merch_wix"
