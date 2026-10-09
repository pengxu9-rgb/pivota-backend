from __future__ import annotations

import asyncio
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
    captured: Dict[str, Any] = {"preview_query": None}

    # Key the stub on query content (like real Postgres), not call order — the head
    # reads (merchant/global aggregates) now run concurrently via asyncio.gather, so a
    # call-order iterator would be brittle. scope row has `rated_total`; the merchant
    # aggregate has `AVG(rating)`; the global aggregate has neither.
    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
        q = str(query)
        if "rated_total" in q:
            return {"total": 3, "rated_total": 3, "avg_rating": 4.5}  # scope row
        if "AVG(rating)" in q:
            return {"total": 3, "media_count": 2, "avg_rating": 4.5}  # merchant aggregate
        return {"total": 0, "media_count": 0}  # global aggregate

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
    assert first["media"][0]["type"] == "image"
    assert first["media"][0]["url"] == "/signed/pub_9311"
    assert first["media"][0]["role"] == "customer_review"
    assert first["media"][0]["provenance"] == {
        "source_type": "customer_review", "review_id": "9311", "source_record_id": "pub_9311",
        "verification_status": "review_linked", "moderation_status": "active",
        "merchant_id": "m_demo", "scope": "unknown",
    }
    assert summary["availability_state"] == "ready"
    assert summary["review_scope"] == "linked_review_store"
    assert "r.product_key" in captured["preview_query"]

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


@pytest.mark.asyncio
async def test_get_review_summary_runs_independent_reads_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the head reads (membership/merchant/global) and the scope
    reads (scope/distribution/preview) must be dispatched concurrently via
    asyncio.gather. Tracks max in-flight DB calls; a sequential implementation would
    never exceed 1. Deterministic (uses asyncio.sleep(0) yields), not timing-based."""
    active = 0
    max_active = 0

    async def _yield_and_track():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)  # let any other in-flight gather() coroutines start
        active -= 1

    async def fake_fetch_one(query: Any, values: Dict[str, Any] | None = None):
        await _yield_and_track()
        q = str(query)
        if "rated_total" in q:
            return {"total": 0, "rated_total": 0, "avg_rating": 0.0}
        if "AVG(rating)" in q:
            return {"total": 0, "media_count": 0, "avg_rating": 0.0}
        return {"total": 0, "media_count": 0}

    async def fake_fetch_all(query: Any, values: Dict[str, Any] | None = None):
        await _yield_and_track()
        return []

    async def fake_group_membership(*_args: Any, **_kwargs: Any):
        await _yield_and_track()
        return None

    monkeypatch.setattr(
        reviews_service, "get_active_group_membership_for_product_key", fake_group_membership
    )
    monkeypatch.setattr(reviews_service.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(reviews_service.database, "fetch_all", fake_fetch_all)

    await reviews_service.get_review_summary_for_sku(
        merchant_id="m_demo",
        platform="shopify",
        platform_product_id="p_demo",
        variant_id=None,
    )

    # Head wave gathers 3 coroutines; scope wave gathers 3. Sequential code => max 1.
    assert max_active >= 2, f"expected concurrent DB reads, max in-flight was {max_active}"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_listing,group_id,expected_scope", [(True, None, "exact_item"), (False, 42, "review_group"), (False, None, "unknown")])
async def test_review_media_preserves_exact_identity_without_group_family_aliasing(monkeypatch, same_listing, group_id, expected_scope):
    product_key = reviews_service.build_product_key(merchant_id="m_demo", platform="shopify", platform_product_id="p_demo")
    rows = [{"id": 88, "merchant_id": "m_demo", "product_key": product_key if same_listing else "different_product",
        "group_id": group_id, "rating": 5, "created_at": datetime.now(timezone.utc), "media_count": 1}]
    _install_summary_stubs(monkeypatch, preview_rows=rows, preview_media_rows=[{
        "id": 89, "review_id": 88, "type": "image", "public_id": "asset_89", "status": "active"}])
    summary = await reviews_service.get_review_summary_for_sku(merchant_id="m_demo", platform="shopify", platform_product_id="p_demo", variant_id=None)
    provenance = summary["preview_items"][0]["media"][0]["provenance"]
    assert provenance["scope"] == expected_scope
    assert "review_family_id" not in provenance
    assert "source_observed_at" not in provenance  # review creation is not source capture
    if same_listing:
        assert provenance["product_id"] == "p_demo"
    elif group_id:
        assert provenance["review_group_id"] == "42"


@pytest.mark.asyncio
async def test_missing_review_store_read_is_not_reported_as_empty(monkeypatch):
    _install_summary_stubs(monkeypatch, preview_rows=[], preview_media_rows=[])
    async def broken(*args, **kwargs):
        raise RuntimeError("review store unavailable")
    monkeypatch.setattr(reviews_service.database, "fetch_one", broken)
    with pytest.raises(RuntimeError, match="review store unavailable"):
        await reviews_service.get_review_summary_for_sku(merchant_id="m_demo", platform="shopify", platform_product_id="p_demo", variant_id=None)
