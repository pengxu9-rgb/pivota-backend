from __future__ import annotations

import pytest


import routes.employee_reviews as employee_reviews_routes


@pytest.mark.asyncio
async def test_moderation_list_supports_order_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return [
            {
                "id": 9315,
                "merchant_id": "merch_efbc46b4619cfbdf",
                "platform": "shopify",
                "platform_product_id": "9859804856648",
                "variant_id": None,
                "group_id": None,
                "source_type": "native",
                "source_system": "accounts",
                "external_review_id": None,
                "verification": "verified_purchase",
                "rating": 5,
                "title": "Great quality",
                "body_effective": "Looks good.",
                "media_count": 1,
                "pending_media_count": 1,
                "active_media_count": 0,
                "total_media_count": 1,
                "status": "under_review",
                "created_at": None,
                "updated_at": None,
                "order_id": "ORD_5FC726A48A2565BF",
            }
        ]

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        merchant_id="merch_efbc46b4619cfbdf",
        status="under_review",
        source_type="native",
        source_system="accounts",
        order_id=" ORD_5FC726A48A2565BF ",
        limit=20,
        actor={"employee_id": "emp_test"},
    )

    assert payload["limit"] == 20
    assert len(payload["items"]) == 1
    assert payload["items"][0]["order_id"] == "ORD_5FC726A48A2565BF"

    values = captured["values"]
    assert isinstance(values, dict)
    assert values.get("oid") == "ORD_5FC726A48A2565BF"

    query = str(captured["query"])
    assert "LEFT JOIN (" in query
    assert "FROM buyer_review_user_subject" in query
    assert "FROM media_assets" in query
    assert "pending_media_count" in query
    assert "active_media_count" in query
    assert "total_media_count" in query
    assert "risk_flags ->> 'order_id'" in query
    assert "AS order_id" in query
    assert "= :oid" in query


@pytest.mark.asyncio
async def test_moderation_list_ignores_blank_order_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return []

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        order_id="   ",
        actor={"employee_id": "emp_test"},
    )

    assert payload == {"items": [], "limit": 50}
    values = captured["values"]
    assert isinstance(values, dict)
    assert "oid" not in values


@pytest.mark.asyncio
async def test_moderation_list_supports_has_pending_media_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return []

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        has_pending_media=True,
        actor={"employee_id": "emp_test"},
    )

    assert payload == {"items": [], "limit": 50}
    query = str(captured["query"])
    assert "COALESCE(media_stats.pending_media_count, 0) > 0" in query


@pytest.mark.asyncio
async def test_moderation_list_supports_deepseek_review_queue_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return [
            {
                "id": 9401,
                "merchant_id": "merch_efbc46b4619cfbdf",
                "platform": "shopify",
                "platform_product_id": "9859803873608",
                "variant_id": None,
                "group_id": None,
                "source_type": "native",
                "source_system": "accounts",
                "external_review_id": None,
                "verification": "unverified",
                "rating": 4,
                "title": "Maybe unrelated",
                "body_effective": "The text needs a person to review.",
                "media_count": 0,
                "risk_flags": {
                    "moderation_decision": "needs_human_review",
                    "text_risk_level": "medium",
                    "employee_review_queue": True,
                },
                "pending_media_count": 0,
                "active_media_count": 0,
                "total_media_count": 0,
                "status": "under_review",
                "created_at": None,
                "updated_at": None,
                "order_id": None,
            }
        ]

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        moderation_decision="needs_human_review",
        risk_level="medium",
        employee_review_queue=True,
        actor={"employee_id": "emp_test"},
    )

    assert payload["items"][0]["risk_flags"]["moderation_decision"] == "needs_human_review"
    values = captured["values"]
    assert isinstance(values, dict)
    assert values.get("mdec") == "needs_human_review"
    assert values.get("risk_level") == "medium"
    assert values.get("employee_review_queue") == "true"
    query = str(captured["query"])
    assert "product_reviews.risk_flags" in query
    assert "risk_flags ->> 'moderation_decision'" in query
    assert "risk_flags ->> 'text_risk_level'" in query
    assert "risk_flags ->> 'employee_review_queue'" in query


@pytest.mark.asyncio
async def test_moderation_list_supports_review_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return [
            {
                "id": 9306,
                "merchant_id": "merch_efbc46b4619cfbdf",
                "platform": "shopify",
                "platform_product_id": "9859803873608",
                "variant_id": None,
                "group_id": None,
                "source_type": "native",
                "source_system": "accounts",
                "external_review_id": None,
                "verification": "verified_purchase",
                "rating": 5,
                "title": "Review title",
                "body_effective": "Review body",
                "media_count": 0,
                "pending_media_count": 1,
                "active_media_count": 0,
                "total_media_count": 1,
                "status": "active",
                "created_at": None,
                "updated_at": None,
                "order_id": "ORD_5FC726A48A2565BF",
            }
        ]

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        review_id=9306,
        actor={"employee_id": "emp_test"},
    )

    assert payload["limit"] == 50
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == 9306

    values = captured["values"]
    assert isinstance(values, dict)
    assert values.get("rid") == 9306

    query = str(captured["query"])
    assert "product_reviews.id = :rid" in query


@pytest.mark.asyncio
async def test_moderation_list_supports_review_id_with_pending_media_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_fetch_all(query: str, values=None):
        captured["query"] = str(query)
        captured["values"] = dict(values or {})
        return []

    monkeypatch.setattr(employee_reviews_routes.database, "fetch_all", fake_fetch_all)

    payload = await employee_reviews_routes.employee_list_reviews_for_moderation(
        review_id=9306,
        has_pending_media=True,
        actor={"employee_id": "emp_test"},
    )

    assert payload == {"items": [], "limit": 50}
    values = captured["values"]
    assert isinstance(values, dict)
    assert values.get("rid") == 9306

    query = str(captured["query"])
    assert "product_reviews.id = :rid" in query
    assert "COALESCE(media_stats.pending_media_count, 0) > 0" in query
