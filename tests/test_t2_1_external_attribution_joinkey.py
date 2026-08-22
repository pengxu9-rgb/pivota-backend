"""T2-1 — stable click_id + Shopify cart-permalink join key on external redirects.

Proves that an external-offer redirect now carries a stable click_id into BOTH the
signed token ctx (so surface_click_events becomes joinable instead of minting a fresh
throwaway id) AND onto the destination (Shopify cart attribute = order-side join;
non-Shopify = referral-only query param). See tier2_v1_build_plan §T2-1.
"""

from urllib.parse import parse_qs, urlparse

import pytest

import routes.agent_shop_gateway as gateway
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    materialize_attribution_context,
)
from services.outbound_links_service import (
    build_shopify_cart_permalink,
    extract_shopify_numeric_variant_id,
    parse_and_verify_redirect_token,
    shopify_cart_base_url,
)


def _decode_redirect(redirect_url: str) -> dict:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


# --- pure helpers ------------------------------------------------------------


def test_extract_shopify_numeric_variant_id():
    assert extract_shopify_numeric_variant_id("46123456789") == "46123456789"
    assert extract_shopify_numeric_variant_id("gid://shopify/ProductVariant/999") == "999"
    # Never fabricate a numeric id from a non-numeric SKU string.
    assert extract_shopify_numeric_variant_id("SKU-ABC") is None
    assert extract_shopify_numeric_variant_id("") is None
    assert extract_shopify_numeric_variant_id(None) is None


def test_build_shopify_cart_permalink_carries_click_attribute():
    permalink = build_shopify_cart_permalink(
        shop_domain="teststore.com",
        variant_id="46123456789",
        click_id="clk_abc123",
        quantity=2,
    )
    assert permalink == "https://teststore.com/cart/46123456789:2?attributes[pivota_click_id]=clk_abc123"


def test_build_shopify_cart_permalink_none_without_variant():
    assert build_shopify_cart_permalink(
        shop_domain="teststore.com", variant_id="SKU-ABC", click_id="clk_x"
    ) is None
    assert shopify_cart_base_url(shop_domain="", variant_id="123") is None


# --- _make_external_redirect_url: Shopify seed -> cart permalink --------------


@pytest.mark.asyncio
async def test_shopify_seed_builds_cart_permalink_with_matching_ctx():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_1", "variantId": "v1"},
        allowed_domains=["teststore.com"],
        merchant_id="merch_test",
        product_id="merch_test|shopify|999",
        variant_id="46123456789",
        shop_domain="teststore.com",
        platform="shopify",
        quantity=1,
        cart_variant_id="46123456789",
    )
    assert redirect_url is not None
    payload = _decode_redirect(redirect_url)
    dest = payload["dest"]
    ctx = payload["ctx"]

    # (a) destination is a Shopify cart permalink carrying the click id as an order-surviving attr
    assert "teststore.com/cart/46123456789:1" in dest
    assert "attributes[pivota_click_id]=" in dest
    assert ctx["join_mode"] == "cart_permalink"

    # ctx carries the SAME stable click id + merchant + canonical product/variant
    click_id = ctx[PVT_CLICK_ID]
    assert click_id and click_id.startswith("clk_")
    assert f"attributes[pivota_click_id]={click_id}" in dest
    assert ctx["merchant_id"] == "merch_test"
    assert ctx[PVT_PRODUCT_ID] == "merch_test|shopify|999"
    assert ctx[PVT_VARIANT_ID] == "46123456789"
    # UTM params preserved on the cart permalink
    assert "utm_source=pivota" in dest


# --- _make_external_redirect_url: non-Shopify / no-variant -> referral only ---


@pytest.mark.asyncio
async def test_non_shopify_seed_falls_back_to_referral_only():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_2"},
        allowed_domains=["teststore.com"],
        merchant_id="merch_test",
        product_id="merch_test|woocommerce|5",
        variant_id="46123456789",  # numeric, but platform is NOT shopify -> no cart permalink
        shop_domain="teststore.com",
        platform="woocommerce",
        cart_variant_id="46123456789",
    )
    assert redirect_url is not None
    payload = _decode_redirect(redirect_url)
    dest = payload["dest"]
    ctx = payload["ctx"]

    assert "/cart/" not in dest
    assert ctx["join_mode"] == "referral_only"
    # click id appended click-side only, product URL preserved
    click_id = ctx[PVT_CLICK_ID]
    assert click_id.startswith("clk_")
    assert f"pvt_click_id={click_id}" in dest
    assert "teststore.com/products/widget" in dest
    assert ctx["merchant_id"] == "merch_test"


@pytest.mark.asyncio
async def test_shopify_platform_but_non_numeric_variant_does_not_fabricate():
    # platform=shopify but the only variant id is a non-numeric SKU -> honest referral fallback,
    # never a fabricated cart permalink.
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_3"},
        allowed_domains=["teststore.com"],
        variant_id="SKU-ABC",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="SKU-ABC",
    )
    payload = _decode_redirect(redirect_url)
    assert payload["ctx"]["join_mode"] == "referral_only"
    assert "/cart/" not in payload["dest"]


# --- domain allowlist still enforced -----------------------------------------


@pytest.mark.asyncio
async def test_disallowed_domain_returns_none_even_for_shopify():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_4"},
        allowed_domains=["blocked.example.com"],
        variant_id="46123456789",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="46123456789",
    )
    assert redirect_url is None


# --- the ctx is what materialize_attribution_context uses (no fresh-id fallback) --


@pytest.mark.asyncio
async def test_ctx_click_id_is_used_by_materialize_not_a_fresh_id():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://teststore.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_5"},
        allowed_domains=["teststore.com"],
        merchant_id="merch_test",
        product_id="merch_test|shopify|999",
        variant_id="46123456789",
        shop_domain="teststore.com",
        platform="shopify",
        cart_variant_id="46123456789",
    )
    ctx = _decode_redirect(redirect_url)["ctx"]

    # record_surface_event materializes exactly this ctx. The stable id must survive
    # (fresh-id fallback NOT taken) and merchant + product must land on the row.
    attribution = materialize_attribution_context(
        ctx,
        default_surface=str(ctx.get("tool") or ""),
        merchant_id=ctx.get("merchant_id"),
    )
    assert attribution[PVT_CLICK_ID] == ctx[PVT_CLICK_ID]
    assert attribution["merchant_id"] == "merch_test"
    assert attribution[PVT_PRODUCT_ID] == "merch_test|shopify|999"
    assert attribution[PVT_SURFACE] == "offers.resolve"
    # Deterministic: re-materializing the same ctx yields the same id (proves no minting).
    again = materialize_attribution_context(ctx, merchant_id=ctx.get("merchant_id"))
    assert again[PVT_CLICK_ID] == attribution[PVT_CLICK_ID]


def test_materialize_without_pvt_click_id_mints_fresh_id():
    # Sanity: the OLD thin ctx (pre-T2-1) does mint a throwaway id with NULL merchant/product.
    thin = materialize_attribution_context({"seedId": "seed_x"})
    assert thin[PVT_CLICK_ID].startswith("clk_")
    assert thin["merchant_id"] is None
    assert thin[PVT_PRODUCT_ID] is None
