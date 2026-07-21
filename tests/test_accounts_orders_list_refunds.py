from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from routes.accounts_orders_api import (
    AccountsPrincipal,
    get_accounts_principal,
    router as accounts_router,
)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(accounts_router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _principal() -> AccountsPrincipal:
    return AccountsPrincipal(
        user_id="user_orders_1",
        email="buyer@example.com",
        email_normalized="buyer@example.com",
        primary_role="customer",
    )


def test_orders_list_returns_refund_summary_and_partial_refund_status(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.accounts_orders_api as accounts_routes

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_fetch_all(_: Any) -> List[dict]:
        return [
            {
                "order_id": "ORD_PARTIAL_REFUND_1",
                "merchant_id": "merch_1",
                "currency": "USD",
                "total": 25.00,
                "total_refunded": 5.00,
                "status": "partially_refunded",
                "payment_status": "partially_refunded",
                "fulfillment_status": "fulfilled",
                "tracking_number": "trk_123",
                "created_at": datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc),
                "shipping_address": {
                    "city": "San Francisco",
                    "country": "US",
                },
                "items": [
                    {
                        "title": "Winona Soothing Repair Serum",
                        "quantity": 1,
                    }
                ],
                "metadata": {
                    "refund_status": "partially_refunded",
                },
            }
        ]

    app.dependency_overrides[get_accounts_principal] = override_principal
    monkeypatch.setattr(accounts_routes.database, "fetch_all", fake_fetch_all)

    try:
        response = client.get("/accounts/orders/list")
    finally:
        app.dependency_overrides.pop(get_accounts_principal, None)

    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert body["orders"] == [
        {
            "order_id": "ORD_PARTIAL_REFUND_1",
            "currency": "USD",
            "total_amount_minor": 2500,
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "refund_status": "partially_refunded",
            "total_refunded_minor": 500,
            "fulfillment_status": "fulfilled",
            "delivery_status": "delivered",
            "created_at": "2026-04-22T12:00:00+00:00",
            "creator_id": None,
            "creator_name": None,
            "creator_slug": None,
            "shipping_city": "San Francisco",
            "shipping_country": "US",
            "items_summary": "Winona Soothing Repair Serum x1",
            "permissions": {
                "can_pay": False,
                "can_cancel": False,
                "can_reorder": False,
            },
            "first_item_image_url": None,
        }
    ]
