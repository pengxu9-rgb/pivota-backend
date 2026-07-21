from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from routes.accounts_orders_api import AccountsPrincipal, get_accounts_or_guest_principal_ugc
from routes.buyer_reviews import router as buyer_reviews_router


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(buyer_reviews_router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _payload() -> Dict[str, Any]:
    return {
        "product_id": "prod_100",
        "subject": {
            "merchant_id": "merch_100",
            "platform": "shopify",
            "platform_product_id": "prod_100",
        },
        "rating": 5,
        "title": "Great",
        "body": "Works well.",
    }


def _principal() -> AccountsPrincipal:
    return AccountsPrincipal(
        user_id="user_123",
        email="buyer@example.com",
        email_normalized="buyer@example.com",
    )


def test_from_user_single_paid_order_allows_once_then_returns_409(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    slot_summaries = [
        {
            "paid_order_ids": ["ord_1"],
            "available_order_ids": ["ord_1"],
            "bindings": [],
            "total_paid_orders": 1,
            "used_slots": 0,
            "available_slots": 1,
            "legacy_binding_count": 0,
        },
        {
            "paid_order_ids": ["ord_1"],
            "available_order_ids": [],
            "bindings": [{"review_id": 7101, "order_id": "ord_1"}],
            "total_paid_orders": 1,
            "used_slots": 1,
            "available_slots": 0,
            "legacy_binding_count": 0,
        },
    ]
    bind_calls: List[Dict[str, Any]] = []
    created_review_ids = iter([7101])

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        if not slot_summaries:
            raise AssertionError("Unexpected extra get_review_slot_summary call")
        return slot_summaries.pop(0)

    async def fake_execute(query: Any) -> int:
        if "insert into product_reviews" not in str(query).lower():
            raise AssertionError(f"Unexpected execute query: {query}")
        return next(created_review_ids)

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        if "product_reviews" in str(query):
            return {"id": 7101, "verification": "verified_purchase", "rating": 5}
        return None

    async def fake_bind_user_review_subject(**kwargs: Any) -> None:
        bind_calls.append(kwargs)

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_enabled", lambda: True)
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_merchant_allowed", lambda _: True)
    monkeypatch.setattr(buyer_reviews_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(buyer_reviews_routes, "bind_user_review_subject", fake_bind_user_review_subject)
    monkeypatch.setattr(buyer_reviews_routes.database, "execute", fake_execute)
    monkeypatch.setattr(buyer_reviews_routes.database, "fetch_one", fake_fetch_one)

    try:
        first = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
        second = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert first.status_code == 200
    assert first.json().get("review_id") == 7101
    assert first.json().get("moderation_state") == "under_review"
    assert second.status_code == 409
    assert second.json().get("detail") == "ALREADY_REVIEWED"
    assert [c.get("order_id") for c in bind_calls] == ["ord_1"]


def test_from_user_two_paid_orders_allows_two_reviews_third_is_409(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    slot_summaries = [
        {
            "paid_order_ids": ["ord_2", "ord_1"],
            "available_order_ids": ["ord_2", "ord_1"],
            "bindings": [],
            "total_paid_orders": 2,
            "used_slots": 0,
            "available_slots": 2,
            "legacy_binding_count": 0,
        },
        {
            "paid_order_ids": ["ord_2", "ord_1"],
            "available_order_ids": ["ord_1"],
            "bindings": [{"review_id": 7201, "order_id": "ord_2"}],
            "total_paid_orders": 2,
            "used_slots": 1,
            "available_slots": 1,
            "legacy_binding_count": 0,
        },
        {
            "paid_order_ids": ["ord_2", "ord_1"],
            "available_order_ids": [],
            "bindings": [
                {"review_id": 7202, "order_id": "ord_1"},
                {"review_id": 7201, "order_id": "ord_2"},
            ],
            "total_paid_orders": 2,
            "used_slots": 2,
            "available_slots": 0,
            "legacy_binding_count": 0,
        },
    ]
    bind_calls: List[Dict[str, Any]] = []
    created_review_ids = iter([7201, 7202])

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        if not slot_summaries:
            raise AssertionError("Unexpected extra get_review_slot_summary call")
        return slot_summaries.pop(0)

    async def fake_execute(query: Any) -> int:
        if "insert into product_reviews" not in str(query).lower():
            raise AssertionError(f"Unexpected execute query: {query}")
        return next(created_review_ids)

    async def fake_fetch_one(query: Any) -> Dict[str, Any] | None:
        if "product_reviews" in str(query):
            return {"verification": "verified_purchase", "rating": 5}
        return None

    async def fake_bind_user_review_subject(**kwargs: Any) -> None:
        bind_calls.append(kwargs)

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_enabled", lambda: True)
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_merchant_allowed", lambda _: True)
    monkeypatch.setattr(buyer_reviews_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(buyer_reviews_routes, "bind_user_review_subject", fake_bind_user_review_subject)
    monkeypatch.setattr(buyer_reviews_routes.database, "execute", fake_execute)
    monkeypatch.setattr(buyer_reviews_routes.database, "fetch_one", fake_fetch_one)

    try:
        first = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
        second = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
        third = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert first.status_code == 200
    assert first.json().get("review_id") == 7201
    assert first.json().get("moderation_state") == "under_review"
    assert second.status_code == 200
    assert second.json().get("review_id") == 7202
    assert second.json().get("moderation_state") == "under_review"
    assert third.status_code == 409
    assert third.json().get("detail") == "ALREADY_REVIEWED"
    assert [c.get("order_id") for c in bind_calls] == ["ord_2", "ord_1"]


def test_from_user_non_purchaser_creates_unverified_under_review_review(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    captured_insert: Dict[str, Any] = {}
    bind_calls: List[Dict[str, Any]] = []

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        return {
            "paid_order_ids": [],
            "available_order_ids": [],
            "bindings": [],
            "total_paid_orders": 0,
            "used_slots": 0,
            "available_slots": 0,
            "legacy_binding_count": 0,
        }

    async def fake_execute(query: Any) -> int:
        if "insert into product_reviews" not in str(query).lower():
            raise AssertionError(f"Unexpected execute query: {query}")
        captured_insert.update(query.compile().params)
        return 7251

    async def fake_bind_user_review_subject(**kwargs: Any) -> None:
        bind_calls.append(kwargs)

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_enabled", lambda: True)
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_merchant_allowed", lambda _: True)
    monkeypatch.setattr(buyer_reviews_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(buyer_reviews_routes, "bind_user_review_subject", fake_bind_user_review_subject)
    monkeypatch.setattr(buyer_reviews_routes.database, "execute", fake_execute)

    try:
        response = client.post("/buyer/reviews/v1/reviews/from_user", json=_payload())
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert response.status_code == 200
    assert response.json().get("review_id") == 7251
    assert captured_insert["verification"] == "unverified"
    assert captured_insert["rating"] == 5
    assert bind_calls[0]["order_id"] is None


def test_from_user_high_risk_text_stays_under_review(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    slot_summaries = [
        {
            "paid_order_ids": ["ord_9"],
            "available_order_ids": ["ord_9"],
            "bindings": [],
            "total_paid_orders": 1,
            "used_slots": 0,
            "available_slots": 1,
            "legacy_binding_count": 0,
        },
    ]
    created_review_ids = iter([7301])

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        if not slot_summaries:
            raise AssertionError("Unexpected extra get_review_slot_summary call")
        return slot_summaries.pop(0)

    async def fake_execute(query: Any) -> int:
        if "insert into product_reviews" not in str(query).lower():
            raise AssertionError(f"Unexpected execute query: {query}")
        return next(created_review_ids)

    async def fake_bind_user_review_subject(**kwargs: Any) -> None:
        _ = kwargs

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_enabled", lambda: True)
    monkeypatch.setattr(buyer_reviews_routes, "buyer_submit_merchant_allowed", lambda _: True)
    monkeypatch.setattr(buyer_reviews_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(buyer_reviews_routes, "bind_user_review_subject", fake_bind_user_review_subject)
    monkeypatch.setattr(buyer_reviews_routes.database, "execute", fake_execute)

    try:
        response = client.post(
            "/buyer/reviews/v1/reviews/from_user",
            json={
                **_payload(),
                "title": "xxx",
                "body": "porn content",
            },
        )
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert response.status_code == 200
    assert response.json().get("review_id") == 7301
    assert response.json().get("moderation_state") == "under_review"
