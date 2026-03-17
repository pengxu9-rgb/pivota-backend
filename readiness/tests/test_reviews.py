from __future__ import annotations

from datetime import datetime, timezone

import pytest

from readiness import reviews
from readiness.tests.conftest import build_live_shopify_products
from services.reviews_service import GLOBAL_IMPORT_MERCHANT_ID, build_product_key


@pytest.mark.asyncio
async def test_load_product_review_summaries_falls_back_when_group_membership_lookup_fails(monkeypatch):
    product = build_live_shopify_products()[0]
    merchant_product_key = build_product_key(
        merchant_id="merch_efbc46b4619cfbdf",
        platform="shopify",
        platform_product_id=str(product.id),
    )

    calls = {"count": 0}

    async def fake_fetch_all(_query):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("group membership query failed")
        if calls["count"] == 2:
            return [
                {
                    "bucket": merchant_product_key,
                    "review_count": 5,
                    "rated_total": 5,
                    "rating_sum": 23,
                    "verified_review_count": 3,
                    "latest_review_at": datetime(2026, 3, 17, 10, 0, tzinfo=timezone.utc),
                }
            ]
        raise AssertionError(f"unexpected fetch_all call #{calls['count']}")

    monkeypatch.setattr(reviews.database, "fetch_all", fake_fetch_all)

    summaries, diagnostics, warnings, audit_notes = await reviews.load_product_review_summaries(
        merchant_id="merch_efbc46b4619cfbdf",
        platform="shopify",
        products=[product],
    )

    assert diagnostics["integration_status"] == "ready"
    assert diagnostics["products_with_reviews"] == 1
    assert "reviews_group_membership_lookup_failed" in warnings
    assert summaries[str(product.id)]["source"] == "reviews_center.product_reviews.v1"
    assert summaries[str(product.id)]["review_count"] == 5
    assert any("fell back to direct product-review summaries" in note for note in audit_notes)


@pytest.mark.asyncio
async def test_load_product_review_summaries_keeps_group_summary_when_featured_lookup_fails(monkeypatch):
    product = build_live_shopify_products()[0]
    merchant_product_key = build_product_key(
        merchant_id="merch_efbc46b4619cfbdf",
        platform="shopify",
        platform_product_id=str(product.id),
    )
    global_product_key = build_product_key(
        merchant_id=GLOBAL_IMPORT_MERCHANT_ID,
        platform="shopify",
        platform_product_id=str(product.id),
    )

    calls = {"count": 0}

    async def fake_fetch_all(_query):
        calls["count"] += 1
        if calls["count"] == 1:
            return [
                {
                    "product_key": merchant_product_key,
                    "group_id": 2101,
                    "membership_confidence": 0.91,
                    "group_key": "gtin:8801234567890",
                    "group_confidence": 0.97,
                }
            ]
        if calls["count"] == 2:
            return [
                {
                    "bucket": 2101,
                    "review_count": 27,
                    "rated_total": 25,
                    "rating_sum": 118,
                    "verified_review_count": 21,
                    "latest_review_at": datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc),
                }
            ]
        if calls["count"] == 3:
            raise RuntimeError("review_featured missing")
        if calls["count"] == 4:
            return [
                {
                    "bucket": merchant_product_key,
                    "review_count": 2,
                    "rated_total": 2,
                    "rating_sum": 10,
                    "verified_review_count": 1,
                    "latest_review_at": datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
                },
                {
                    "bucket": global_product_key,
                    "review_count": 1,
                    "rated_total": 1,
                    "rating_sum": 5,
                    "verified_review_count": 1,
                    "latest_review_at": datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc),
                },
            ]
        raise AssertionError(f"unexpected fetch_all call #{calls['count']}")

    monkeypatch.setattr(reviews.database, "fetch_all", fake_fetch_all)

    summaries, diagnostics, warnings, _audit_notes = await reviews.load_product_review_summaries(
        merchant_id="merch_efbc46b4619cfbdf",
        platform="shopify",
        products=[product],
    )

    assert diagnostics["integration_status"] == "ready"
    assert diagnostics["products_with_reviews"] == 1
    assert "reviews_featured_lookup_failed" in warnings
    assert summaries[str(product.id)]["source"] == "reviews_center.review_group.v1"
    assert summaries[str(product.id)]["featured_review_count"] == 0
    assert summaries[str(product.id)]["review_count"] == 27
