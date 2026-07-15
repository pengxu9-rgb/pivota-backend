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


def _graphql_metafields_response(by_pid: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Build a Shopify GraphQL nodes(ids: [...]) response that mirrors the
    real wire format the adapter parses."""
    nodes = []
    for pid, mfs in by_pid.items():
        nodes.append({
            "id": f"gid://shopify/Product/{pid}",
            "metafields": {
                "edges": [{"node": m} for m in mfs],
            },
        })
    return {"data": {"nodes": nodes}}


def _install_graphql_mock(payload: Dict[str, Any], status: int = 200):
    captured: Dict[str, Any] = {}
    mock_client = MagicMock()
    async def _post(url, headers=None, json=None):
        captured["url"] = url
        captured["json"] = json
        return _MockResponse(status, payload)
    mock_client.post = _post
    return mock_client, captured


@pytest.mark.asyncio
async def test_fetch_metafields_returns_per_product_lists():
    products = [{"id": 100}, {"id": 200}]
    mock_client, captured = _install_graphql_mock(_graphql_metafields_response({
        "100": [{"namespace": "custom", "key": "material", "value": "cotton", "type": "single_line_text_field"}],
        "200": [{"namespace": "custom", "key": "care", "value": "machine wash", "type": "single_line_text_field"}],
    }))

    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example",
        headers={"X-Shopify-Access-Token": "tok"}, products=products,
    )
    # Single GraphQL POST regardless of N products.
    assert captured["url"] == "https://shop.example/admin/api/2025-10/graphql.json"
    assert set(captured["json"]["variables"]["ids"]) == {
        "gid://shopify/Product/100", "gid://shopify/Product/200",
    }
    assert set(result.keys()) == {"100", "200"}
    assert result["100"][0]["value"] == "cotton"
    assert result["200"][0]["value"] == "machine wash"


@pytest.mark.asyncio
async def test_fetch_metafields_normalizes_to_rest_shape():
    # The downstream consumer in services/fashion_field_payload_extractor.py
    # expects REST shape {namespace, key, value, type}. Verify GraphQL
    # nodes get flattened.
    products = [{"id": 300}]
    mock_client, _ = _install_graphql_mock(_graphql_metafields_response({
        "300": [{"namespace": "shopify", "key": "material", "value": "linen", "type": "single_line_text_field"}],
    }))
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    mf = result["300"][0]
    assert mf["namespace"] == "shopify"
    assert mf["key"] == "material"
    assert mf["value"] == "linen"
    assert mf["type"] == "single_line_text_field"


@pytest.mark.asyncio
async def test_fetch_metafields_handles_graphql_errors():
    # When the GraphQL endpoint returns 200 but with an "errors" key
    # (Shopify schema-level errors), the adapter logs + returns empty
    # lists for every product so one bad sync page doesn't break the
    # whole catalog sync.
    products = [{"id": 100}, {"id": 200}]
    mock_client, _ = _install_graphql_mock({"errors": [{"message": "Field not found"}]})
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    assert result == {"100": [], "200": []}


@pytest.mark.asyncio
async def test_fetch_metafields_handles_http_error():
    products = [{"id": 100}, {"id": 200}]
    mock_client, _ = _install_graphql_mock({}, status=500)
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    assert result == {"100": [], "200": []}


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
    mock_client, captured = _install_graphql_mock(_graphql_metafields_response({"500": []}))
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    # Only the product with an id is in the GraphQL request.
    assert captured["json"]["variables"]["ids"] == ["gid://shopify/Product/500"]
    assert set(result.keys()) == {"500"}


@pytest.mark.asyncio
async def test_fetch_metafields_returns_empty_list_for_unknown_products():
    # Products requested but not present in the GraphQL response (e.g.
    # deleted between fetch_products and the metafield call) get empty
    # lists rather than missing keys.
    products = [{"id": 100}, {"id": 200}]
    mock_client, _ = _install_graphql_mock(_graphql_metafields_response({"100": []}))
    result = await ShopifyProductAdapter._fetch_metafields_for_products(
        client=mock_client, shop_domain="shop.example", headers={}, products=products,
    )
    assert result == {"100": [], "200": []}
