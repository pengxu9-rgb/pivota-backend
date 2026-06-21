"""v2 product serializer exposes a cross-platform handle / online_store_url so
the portal can match audited product URLs back to synced SKUs."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.standard_product import StandardProduct
import routes.product_routes_v2 as v2


def _sp(**overrides) -> StandardProduct:
    base = dict(id="1", platform="shopify", merchant_id="m1", title="X", price=1.0)
    base.update(overrides)
    return StandardProduct(**base)


# --- _slug_from_url -----------------------------------------------------------

def test_slug_from_url_extracts_last_segment() -> None:
    assert v2._slug_from_url("https://brand.com/products/best-seller") == "best-seller"


def test_slug_from_url_strips_trailing_slash_query_fragment() -> None:
    assert v2._slug_from_url("https://brand.com/products/Best-Seller/?v=1#x") == "best-seller"


def test_slug_from_url_handles_empty_and_none() -> None:
    assert v2._slug_from_url(None) is None
    assert v2._slug_from_url("") is None


# --- _apply_storefront_fields -------------------------------------------------

def test_apply_shopify_handle_from_platform_metadata() -> None:
    sp = _sp(platform="shopify", platform_metadata={"handle": "best-seller"})
    v2._apply_storefront_fields(sp)
    assert sp.handle == "best-seller"
    assert sp.online_store_url is None  # Shopify has no full URL without domain


def test_apply_woocommerce_permalink_and_slug() -> None:
    sp = _sp(
        platform="woocommerce",
        platform_metadata={
            "permalink": "https://brand.com/product/vitamin-c/",
            "slug": "vitamin-c",
        },
    )
    v2._apply_storefront_fields(sp)
    assert sp.handle == "vitamin-c"
    assert sp.online_store_url == "https://brand.com/product/vitamin-c/"


def test_apply_woocommerce_handle_derived_from_permalink_when_no_slug() -> None:
    sp = _sp(
        platform="woocommerce",
        platform_metadata={"permalink": "https://brand.com/product/vitamin-c/"},
    )
    v2._apply_storefront_fields(sp)
    assert sp.handle == "vitamin-c"


def test_apply_no_metadata_leaves_fields_none() -> None:
    sp = _sp(platform="wix", platform_metadata=None)
    v2._apply_storefront_fields(sp)
    assert sp.handle is None
    assert sp.online_store_url is None


def test_apply_preserves_already_set_fields() -> None:
    sp = _sp(handle="explicit", online_store_url="https://x/y",
             platform_metadata={"handle": "other", "permalink": "https://z"})
    v2._apply_storefront_fields(sp)
    assert sp.handle == "explicit"
    assert sp.online_store_url == "https://x/y"


# --- _map_cache_row_to_standard_product --------------------------------------

def test_mapper_fast_path_populates_handle() -> None:
    # product_data already in StandardProduct shape (has id/merchant_id/platform/price).
    product_data = {
        "id": "100", "merchant_id": "m1", "platform": "shopify",
        "title": "Serum", "price": 9.0,
        "platform_metadata": {"handle": "vitamin-c-serum"},
    }
    sp = v2._map_cache_row_to_standard_product("m1", "shopify", product_data)
    assert sp.handle == "vitamin-c-serum"


def test_mapper_fallback_path_populates_handle_from_raw() -> None:
    # Minimal Shopify DTO payload (no top-level price) -> fallback mapping path,
    # which sets platform_metadata.handle from raw.handle; _apply derives handle.
    product_data = {
        "shopify_id": "200",
        "raw": {"id": 200, "title": "Cleanser", "handle": "gentle-cleanser",
                "variants": [{"id": 1, "price": "12.00"}]},
    }
    sp = v2._map_cache_row_to_standard_product("m1", "shopify", product_data)
    assert sp.handle == "gentle-cleanser"
