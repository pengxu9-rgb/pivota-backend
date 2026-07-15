from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from readiness.flags import DEFAULT_ALPHA_MERCHANT_ID
from readiness.sources import shopify_live
from readiness.tests.conftest import build_live_shopify_products, build_review_summaries, load_real_merchant_fixture


@pytest.mark.asyncio
async def test_shopify_live_source_builds_real_merchant_dataset(monkeypatch):
    fixture = load_real_merchant_fixture()
    live_products = build_live_shopify_products()

    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": fixture["merchant_id"], "business_name": fixture["merchant_name"]}

    async def fake_get_primary_store(_merchant_id: str):
        return fixture["store"]

    async def fake_get_shopify_cfg(_merchant_id: str):
        return fixture["shopify_config"]

    async def fake_get_cached_products(*, merchant_id: str, platform: str, include_expired: bool = False):
        assert merchant_id == fixture["merchant_id"]
        assert platform == "shopify"
        assert include_expired is True
        rows = fixture["products_cache_rows"]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows[0]["cached_at"] = (now - timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
        rows[1]["cached_at"] = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        return rows

    async def fake_get_active_psp(_merchant_id: str):
        return fixture["merchant_psp"]

    async def fake_fetch_live_products(_merchant_id: str, _shop_domain: str, _access_token: str):
        return live_products, None

    async def fake_load_product_review_summaries(**_kwargs):
        return build_review_summaries()

    monkeypatch.setattr(shopify_live, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(shopify_live, "get_cached_products", fake_get_cached_products)
    monkeypatch.setattr(shopify_live, "_fetch_active_psp_config", fake_get_active_psp)
    monkeypatch.setattr(shopify_live, "_fetch_live_products", fake_fetch_live_products)
    monkeypatch.setattr(shopify_live, "load_product_review_summaries", fake_load_product_review_summaries)

    dataset = await shopify_live.load_shopify_live_merchant_dataset(DEFAULT_ALPHA_MERCHANT_ID)

    assert dataset.merchant_alpha_mode == "real_merchant_alpha"
    assert dataset.merchant_id == fixture["merchant_id"]
    assert dataset.payment_capabilities["merchant_native_checkout_supported"] is True
    assert dataset.capability_status["checkout"] == "ready"
    assert dataset.source_of_truth["catalog"] == "shopify_cache.standard_product.v1"
    assert dataset.source_of_truth["price"] == "shopify_admin.products.v2025-10"
    assert dataset.source_of_truth["inventory"] == "shopify_admin.inventory.v2025-10"
    assert dataset.source_of_truth["reviews_confidence"] == "reviews_center.review_group.v1"
    assert dataset.capability_status["reviews_confidence"] == "ready"
    assert len(dataset.products) == 2
    assert dataset.merchant_blockers == []
    assert dataset.variant_diagnostics["431000000003"]["field_sources"]["inventory"]["source"] == "shopify_admin.inventory.v2025-10"
    assert dataset.product_review_summaries["9886500749640"]["review_count"] == 27
