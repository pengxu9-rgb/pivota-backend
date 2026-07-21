"""Backfill script for Wix storefront URLs (scripts/backfill_wix_storefront_urls.py).

No live API, no DB: store lookup, the Wix fetch, and the cache upsert are all
monkeypatched. Products flow through the REAL WixProductAdapter._convert_product
so the upserted payload shape is exactly what organic sync writes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.product_adapters import WixProductAdapter  # noqa: E402
from scripts import backfill_wix_storefront_urls as bw  # noqa: E402


def _store_row(**over: Any) -> Dict[str, Any]:
    base = {
        "store_id": "store_wix_1",
        "merchant_id": "merch_wix",
        "name": "Wix Brand",
        "domain": "0e2cde5f-b353-468b-9f4e-36835fc60a0e",
        "api_key": "IST.test_key",
    }
    base.update(over)
    return base


def _wix_product(product_id: str) -> Any:
    return WixProductAdapter._convert_product(
        {
            "id": product_id,
            "name": f"Product {product_id}",
            "visible": True,
            "slug": f"product-{product_id}",
            "productPageUrl": {
                "base": "https://www.wixbrand.com/",
                "path": f"/product-page/product-{product_id}",
            },
            "priceData": {"price": 10.0, "currency": "USD"},
            "stock": {"quantity": 3, "inStock": True, "trackQuantity": True},
        },
        merchant_id="merch_wix",
    )


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch):
    """Wire fake store lookup + Wix fetch + upsert capture into the script."""
    state: Dict[str, Any] = {"upserts": [], "fetch_calls": []}

    async def fake_fetch_all(query: str, params: Optional[Dict[str, Any]] = None):
        state["stores_query_params"] = dict(params or {})
        return [_store_row()]

    monkeypatch.setattr(bw, "database", SimpleNamespace(fetch_all=fake_fetch_all))

    async def fake_fetch_products(*, site_id: str, api_key: str, merchant_id: str, limit: int, page_token):
        state["fetch_calls"].append({"site_id": site_id, "page_token": page_token})
        if page_token is None:
            return [_wix_product("p1"), _wix_product("p2")], "2", None
        return [_wix_product("p3")], None, None

    monkeypatch.setattr(bw.WixProductAdapter, "fetch_products", fake_fetch_products)

    async def fake_upsert(*, merchant_id: str, platform: str, platform_product_id: str, product_data: Dict[str, Any], ttl_seconds: int):
        state["upserts"].append(
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
                "product_data": product_data,
                "ttl_seconds": ttl_seconds,
            }
        )
        return 1

    monkeypatch.setattr(bw, "upsert_product_cache", fake_upsert)
    return state


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing(_wired):
    report = await bw.resync_wix_storefront_urls(apply=False)

    assert report["apply"] is False
    assert report["stores_found"] == 1
    store = report["stores"][0]
    assert store["products_fetched"] == 3
    assert store["products_with_storefront_url"] == 3
    assert store["rows_upserted"] == 0
    assert _wired["upserts"] == []
    # paginated through both pages
    assert [c["page_token"] for c in _wired["fetch_calls"]] == [None, "2"]


@pytest.mark.asyncio
async def test_apply_upserts_cache_rows_with_storefront_fields(_wired):
    report = await bw.resync_wix_storefront_urls(apply=True)

    store = report["stores"][0]
    assert store["rows_upserted"] == 3
    assert len(_wired["upserts"]) == 3
    row = _wired["upserts"][0]
    assert row["platform"] == "wix"
    assert row["merchant_id"] == "merch_wix"
    assert row["ttl_seconds"] == bw.CACHE_TTL_SECONDS
    # The payload the redirect lane reads: top-level online_store_url +
    # portal-readable platform_metadata permalink/slug.
    payload = row["product_data"]
    assert payload["online_store_url"] == "https://www.wixbrand.com/product-page/product-p1"
    assert payload["handle"] == "product-p1"
    assert payload["platform_metadata"]["permalink"] == payload["online_store_url"]
    assert payload["platform_metadata"]["slug"] == "product-p1"


@pytest.mark.asyncio
async def test_incomplete_credentials_reported_not_raised(_wired, monkeypatch):
    async def fake_fetch_all(query: str, params: Optional[Dict[str, Any]] = None):
        return [_store_row(api_key=None)]

    monkeypatch.setattr(bw, "database", SimpleNamespace(fetch_all=fake_fetch_all))

    report = await bw.resync_wix_storefront_urls(apply=True)
    store = report["stores"][0]
    assert store["error"] == "incomplete_credentials"
    assert store["rows_upserted"] == 0
    assert _wired["upserts"] == []


@pytest.mark.asyncio
async def test_mid_page_error_keeps_partial_upserts_and_surfaces_error(_wired, monkeypatch):
    async def fake_fetch_products(*, site_id, api_key, merchant_id, limit, page_token):
        if page_token is None:
            return [_wix_product("p1")], "1", None
        return [], None, "Wix API error: 500 - upstream error"

    monkeypatch.setattr(bw.WixProductAdapter, "fetch_products", fake_fetch_products)

    report = await bw.resync_wix_storefront_urls(apply=True)
    store = report["stores"][0]
    assert store["rows_upserted"] == 1
    assert "Wix API error: 500" in store["error"]
