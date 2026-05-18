"""Tests for ShopifyProductAdapter metafield ingest (Phase O-5b #4)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.product_adapters import (  # noqa: E402
    ShopifyProductAdapter,
    _shopify_metafield_ingest_enabled,
)


# ---------- env flag gate ----------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SHOPIFY_METAFIELD_INGEST_ENABLED", raising=False)
    assert _shopify_metafield_ingest_enabled() is False


def test_flag_recognizes_true_variants(monkeypatch):
    for v in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("SHOPIFY_METAFIELD_INGEST_ENABLED", v)
        assert _shopify_metafield_ingest_enabled() is True


def test_flag_ignores_false_variants(monkeypatch):
    for v in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("SHOPIFY_METAFIELD_INGEST_ENABLED", v)
        assert _shopify_metafield_ingest_enabled() is False


# ---------- convert_to_standard attaches metafields ----------

def test_convert_to_standard_includes_metafields():
    shopify_product = {
        "id": 12345,
        "title": "Linen Dress",
        "handle": "linen-dress",
        "product_type": "Dress",
        "vendor": "Atlas",
        "tags": "",
        "body_html": "A breezy linen dress.",
        "images": [{"src": "https://cdn.example.com/dress.jpg"}],
        "variants": [{"id": "v1", "title": "M", "price": "89.00"}],
        "status": "active",
    }
    metafields = [
        {"namespace": "shopify", "key": "material", "value": "100% linen",
         "type": "single_line_text_field"},
        {"namespace": "custom", "key": "care_instructions",
         "value": "Hand wash cold."},
    ]
    product = ShopifyProductAdapter.convert_to_standard(
        shopify_product, merchant_id="merch_1", currency="USD", metafields=metafields,
    )
    assert product.platform_metadata is not None
    mfs = product.platform_metadata.get("metafields")
    assert isinstance(mfs, list)
    assert len(mfs) == 2
    assert mfs[0]["namespace"] == "shopify"
    assert mfs[0]["key"] == "material"
    assert mfs[1]["key"] == "care_instructions"


def test_convert_to_standard_defaults_metafields_to_empty_list():
    shopify_product = {
        "id": 12345, "title": "x", "handle": "x",
        "variants": [{"id": "v", "title": "v", "price": "1.0"}], "status": "active",
    }
    product = ShopifyProductAdapter.convert_to_standard(
        shopify_product, merchant_id="m", currency="USD",  # metafields omitted
    )
    assert product.platform_metadata.get("metafields") == []


def test_convert_to_standard_accepts_none_metafields():
    shopify_product = {
        "id": 12345, "title": "x", "handle": "x",
        "variants": [{"id": "v", "title": "v", "price": "1.0"}], "status": "active",
    }
    product = ShopifyProductAdapter.convert_to_standard(
        shopify_product, merchant_id="m", currency="USD", metafields=None,
    )
    assert product.platform_metadata.get("metafields") == []


# ---------- _fetch_metafields_for_products (parallel HTTP) ----------

class _MockResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = "mock"
    def json(self) -> Dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_fetch_metafields_returns_per_product_lists():
    products = [{"id": 100}, {"id": 200}]
    responses_by_url = {
        "https://shop.example/admin/api/2024-07/products/100/metafields.json": _MockResponse(
            200, {"metafields": [{"namespace": "custom", "key": "material", "value": "cotton"}]}
        ),
        "https://shop.example/admin/api/2024-07/products/200/metafields.json": _MockResponse(
            200, {"metafields": [{"namespace": "custom", "key": "care", "value": "machine wash"}]}
        ),
    }
    mock_client = MagicMock()
    async def _get(url, headers=None):
        return responses_by_url[url]
    mock_client.get = _get

    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example",
        headers={"X-Shopify-Access-Token": "tok"}, products=products,
    )
    assert set(result.keys()) == {"100", "200"}
    assert result["100"][0]["value"] == "cotton"
    assert result["200"][0]["value"] == "machine wash"


@pytest.mark.asyncio
async def test_fetch_metafields_per_product_failure_isolated():
    # Product 100 returns 500; product 200 returns 200 — one bad product
    # must not poison the other.
    products = [{"id": 100}, {"id": 200}]
    responses_by_url = {
        "https://shop.example/admin/api/2024-07/products/100/metafields.json": _MockResponse(
            500, {}),
        "https://shop.example/admin/api/2024-07/products/200/metafields.json": _MockResponse(
            200, {"metafields": [{"namespace": "custom", "key": "material", "value": "wool"}]}
        ),
    }
    mock_client = MagicMock()
    async def _get(url, headers=None):
        return responses_by_url[url]
    mock_client.get = _get
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example",
        headers={"X-Shopify-Access-Token": "tok"}, products=products,
    )
    assert result["100"] == []  # failure isolated → empty
    assert result["200"][0]["value"] == "wool"


@pytest.mark.asyncio
async def test_fetch_metafields_handles_empty_product_list():
    mock_client = MagicMock()
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=[],
    )
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_metafields_skips_products_with_no_id():
    products = [{"title": "no id"}, {"id": 500}]
    responses_by_url = {
        "https://shop.example/admin/api/2024-07/products/500/metafields.json": _MockResponse(
            200, {"metafields": []}),
    }
    mock_client = MagicMock()
    async def _get(url, headers=None):
        return responses_by_url[url]
    mock_client.get = _get
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    assert set(result.keys()) == {"500"}
