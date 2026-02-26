from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

import services.reviews_service as reviews_service


def _install_summary_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    preview_rows: List[Dict[str, Any]],
    preview_media_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    fetch_one_rows = iter(
        [
            {"total": 3, "media_count": 2, "avg_rating": 4.5},  # merchant row
            {"total": 0, "media_count": 0},  # global row
            {"total": 3, "rated_total": 3, "avg_rating": 4.5},  # scope row
        ]
    )
    captured: Dict[str, Any] = {"preview_query": None}

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        try:
            return next(fetch_one_rows)
        except StopIteration:
            return None

    async def fake_fetch_all(query: Any, values: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        q = str(query)
        if "GROUP BY r.rating" in q:
            return [{"rating": 5, "c": 2}, {"rating": 4, "c": 1}]
        if "COALESCE(NULLIF(r.body_redacted, ''), r.body)" in q:
            captured["preview_query"] = q
            return preview_rows
        if "media_assets" in q:
            return preview_media_rows
        return []

    async def fake_group_membership(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(reviews_service, "get_active_group_membership_for_product_key", fake_group_membership)
    monkeypatch.setattr(reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(reviews_service.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        reviews_service,
        "_signed_media_url",
        lambda *, public_id, media_id: f"/signed/{public_id or media_id}",
    )
    return captured


@pytest.mark.asyncio
async def test_get_review_summary_preview_items_include_media_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    preview_rows = [
        {
            "id": 9311,
            "merchant_id": "m_demo",
            "rating": 5,
            "title": "Amazing set",
            "body_effective": "Looks great.",
            "created_at": now,
            "media_count": 2,
        },
        {
            "id": 9310,
            "merchant_id": "m_demo",
            "rating": 4,
            "title": "Solid quality",
            "body_effective": "Nice quality.",
            "created_at": now,
            "media_count": 0,
        },
    ]
    preview_media_rows = [
        {
            "id": 1001,
            "review_id": 9311,
            "type": "image",
            "public_id": "pub_9311",
            "url": "s3://reviews/pub_9311",
            "status": "active",
        },
        # Same review has another media row; summary should keep first only.
        {
            "id": 1002,
            "review_id": 9311,
            "type": "image",
            "public_id": "pub_9311_second",
            "url": "s3://reviews/pub_9311_second",
            "status": "active",
        },
    ]

    captured = _install_summary_stubs(
        monkeypatch,
        preview_rows=preview_rows,
        preview_media_rows=preview_media_rows,
    )

    summary = await reviews_service.get_review_summary_for_sku(
        merchant_id="m_demo",
        platform="shopify",
        platform_product_id="p_demo",
        variant_id=None,
    )

    preview_items = summary["preview_items"]
    assert len(preview_items) == 2
    assert "LIMIT 6" in str(captured["preview_query"] or "")

    first = preview_items[0]
    assert first["review_id"] == 9311
    assert first["title"] == "Amazing set"
    assert first["has_media"] is True
    assert first["media_count"] == 2
    assert first["media"] == [{"type": "image", "url": "/signed/pub_9311"}]

    second = preview_items[1]
    assert second["review_id"] == 9310
    assert second["title"] == "Solid quality"
    assert second["has_media"] is False
    assert second["media_count"] == 0
    assert "media" not in second


@pytest.mark.asyncio
async def test_get_review_summary_preview_items_keep_backward_compatible_shape_without_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    preview_rows = [
        {
            "id": 9400,
            "merchant_id": "m_demo",
            "rating": 5,
            "title": "No photo review title",
            "body_effective": "No photo review.",
            "created_at": now,
            "media_count": 0,
        }
    ]

    _install_summary_stubs(
        monkeypatch,
        preview_rows=preview_rows,
        preview_media_rows=[],
    )

    summary = await reviews_service.get_review_summary_for_sku(
        merchant_id="m_demo",
        platform="shopify",
        platform_product_id="p_demo",
        variant_id=None,
    )

    item = summary["preview_items"][0]
    assert item["review_id"] == 9400
    assert item["rating"] == 5
    assert item["title"] == "No photo review title"
    assert "text_snippet" in item
    assert item["has_media"] is False
    assert item["media_count"] == 0
    assert "media" not in item


@pytest.mark.asyncio
async def test_get_review_summary_preview_items_text_snippet_falls_back_to_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    preview_rows = [
        {
            "id": 9500,
            "merchant_id": "m_demo",
            "rating": 5,
            "title": "Title fallback only",
            "body_effective": None,
            "created_at": now,
            "media_count": 0,
        }
    ]

    _install_summary_stubs(
        monkeypatch,
        preview_rows=preview_rows,
        preview_media_rows=[],
    )

    summary = await reviews_service.get_review_summary_for_sku(
        merchant_id="m_demo",
        platform="shopify",
        platform_product_id="p_demo",
        variant_id=None,
    )

    item = summary["preview_items"][0]
    assert item["title"] == "Title fallback only"
    assert item["text_snippet"] == "Title fallback only"
