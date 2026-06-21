"""Tests for the copy-back rung's `platform_admin_url` field on
GET /merchant/products/{platform}/{platform_product_id}.

Calls the route handler directly as a coroutine (the pattern used across the
merchant-products endpoint tests). The enriched copy is already returned under
`enrichment`; these pin the NEW store-admin deep-link the merchant uses to paste
it. Read-only — the endpoint performs no external write.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MERCHANT = {"role": "merchant", "merchant_id": "m1"}


def _stub_detail_deps(monkeypatch, *, store_domains):
    """Stub the heavy/DB deps of get_merchant_product_detail so the test isolates
    the platform_admin_url behavior. `_build_platform_admin_url` runs for real
    (it's a pure string builder); only the store-domain loader is stubbed."""
    import readiness.summary as summary
    import routes.merchant_products as module

    monkeypatch.setattr(module, "load_canonical_cache_row", AsyncMock(return_value={
        "product_data": {"title": "X"}, "merchant_id": "m1",
        "platform": "shopify", "platform_product_id": "p1",
    }))
    monkeypatch.setattr(module, "get_enrichment", AsyncMock(
        return_value={"description_markdown": "Generated copy"}))
    monkeypatch.setattr(module, "_build_quality_projection_bundle", AsyncMock(
        return_value={"projections_by_key": {}}))
    monkeypatch.setattr(module, "_build_standard_full", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "build_agent_push_projection_from_cache_row",
                        lambda *_a, **_k: {})
    monkeypatch.setattr(module, "_quality_response", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "_agent_push_response", lambda *_a, **_k: {})
    # the store-domain loader is lazy-imported from readiness.summary at call time
    monkeypatch.setattr(summary, "_load_store_domains_by_platform",
                        AsyncMock(return_value=store_domains))


@pytest.mark.asyncio
async def test_detail_returns_shopify_admin_deeplink(monkeypatch):
    """Happy path: copy (enrichment) + paste-target link returned together."""
    import routes.merchant_products as module
    _stub_detail_deps(monkeypatch, store_domains={"shopify": "myshop.myshopify.com"})
    resp = await module.get_merchant_product_detail(
        platform="shopify", platform_product_id="p1", current_user=MERCHANT)
    assert resp["enrichment"]["description_markdown"] == "Generated copy"
    url = resp["platform_admin_url"]
    assert url is not None
    assert "myshop.myshopify.com" in url
    assert "/admin/products/p1" in url


@pytest.mark.asyncio
async def test_detail_admin_url_none_when_no_store(monkeypatch):
    """No connected store -> link is None; the detail view still returns."""
    import routes.merchant_products as module
    _stub_detail_deps(monkeypatch, store_domains={})
    resp = await module.get_merchant_product_detail(
        platform="shopify", platform_product_id="p1", current_user=MERCHANT)
    assert resp["platform_admin_url"] is None
    assert resp["enrichment"]["description_markdown"] == "Generated copy"


@pytest.mark.asyncio
async def test_detail_admin_url_best_effort_on_error(monkeypatch):
    """If the link build raises, the detail view still succeeds with None — the
    link is a nicety and must never fail the view."""
    import readiness.summary as summary
    import routes.merchant_products as module
    _stub_detail_deps(monkeypatch, store_domains={"shopify": "x.myshopify.com"})

    def _raise(**_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(summary, "_build_platform_admin_url", _raise)
    resp = await module.get_merchant_product_detail(
        platform="shopify", platform_product_id="p1", current_user=MERCHANT)
    assert resp["platform_admin_url"] is None
    assert resp["enrichment"]["description_markdown"] == "Generated copy"


@pytest.mark.asyncio
async def test_detail_non_merchant_403():
    import routes.merchant_products as module
    with pytest.raises(HTTPException) as exc:
        await module.get_merchant_product_detail(
            platform="shopify", platform_product_id="p1",
            current_user={"role": "buyer", "merchant_id": "m1"})
    assert exc.value.status_code == 403
