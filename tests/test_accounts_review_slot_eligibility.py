from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from routes.accounts_orders_api import (
    AccountsPrincipal,
    get_accounts_principal_ugc,
    router as accounts_router,
)


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(accounts_router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _principal() -> AccountsPrincipal:
    return AccountsPrincipal(
        user_id="user_123",
        email="buyer@example.com",
        email_normalized="buyer@example.com",
    )


def test_review_eligibility_returns_not_purchaser_when_no_paid_orders(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as accounts_routes

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        return {
            "total_paid_orders": 0,
            "used_slots": 0,
            "available_slots": 0,
            "legacy_binding_count": 0,
        }

    async def fake_get_user_review_for_subject(**_: Any) -> None:
        return None

    app.dependency_overrides[get_accounts_principal_ugc] = override_principal
    monkeypatch.setattr(accounts_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(accounts_routes, "get_user_review_for_subject", fake_get_user_review_for_subject)

    try:
        response = client.get("/accounts/reviews/eligibility?productId=prod_1")
    finally:
        app.dependency_overrides.pop(get_accounts_principal_ugc, None)

    assert response.status_code == 200
    assert response.json() == {
        "eligible": False,
        "reason": "NOT_PURCHASER",
        "canRate": False,
        "reviewSlots": {
            "totalPaidOrders": 0,
            "usedOrders": 0,
            "availableOrders": 0,
            "legacyBindings": 0,
        },
    }


def test_review_eligibility_returns_already_reviewed_when_slots_exhausted(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as accounts_routes

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        return {
            "total_paid_orders": 2,
            "used_slots": 2,
            "available_slots": 0,
            "legacy_binding_count": 0,
        }

    async def fake_get_user_review_for_subject(**_: Any) -> Dict[str, Any]:
        return {
            "review_id": 9001,
            "verification": "verified_purchase",
            "has_rating": True,
            "order_id": "ord_2",
        }

    app.dependency_overrides[get_accounts_principal_ugc] = override_principal
    monkeypatch.setattr(accounts_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(accounts_routes, "get_user_review_for_subject", fake_get_user_review_for_subject)

    try:
        response = client.get("/accounts/reviews/eligibility?productId=prod_1")
    finally:
        app.dependency_overrides.pop(get_accounts_principal_ugc, None)

    assert response.status_code == 200
    assert response.json() == {
        "eligible": False,
        "reason": "ALREADY_REVIEWED",
        "canRate": False,
        "reviewSlots": {
            "totalPaidOrders": 2,
            "usedOrders": 2,
            "availableOrders": 0,
            "legacyBindings": 0,
        },
    }


def test_personalization_allows_write_when_unreviewed_order_slot_exists(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as accounts_routes

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_get_review_slot_summary(**_: Any) -> Dict[str, Any]:
        return {
            "total_paid_orders": 2,
            "used_slots": 1,
            "available_slots": 1,
            "legacy_binding_count": 0,
        }

    async def fake_get_user_review_for_subject(**_: Any) -> None:
        return None

    async def fake_is_question_rate_limited(**_: Any) -> bool:
        return False

    app.dependency_overrides[get_accounts_principal_ugc] = override_principal
    monkeypatch.setattr(accounts_routes, "get_review_slot_summary", fake_get_review_slot_summary)
    monkeypatch.setattr(accounts_routes, "get_user_review_for_subject", fake_get_user_review_for_subject)
    monkeypatch.setattr(accounts_routes, "is_question_rate_limited", fake_is_question_rate_limited)

    try:
        response = client.get("/accounts/pdp/v2/personalization?productId=prod_1")
    finally:
        app.dependency_overrides.pop(get_accounts_principal_ugc, None)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ugcCapabilities": {
            "canUploadMedia": True,
            "canWriteReview": True,
            "canRateReview": True,
            "canAskQuestion": True,
            "reasons": {},
            "review": None,
            "reviewSlots": {
                "totalPaidOrders": 2,
                "usedOrders": 1,
                "availableOrders": 1,
                "legacyBindings": 0,
            },
        }
    }
