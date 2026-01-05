"""
Agent cart validation contract: ensure cached products are loaded correctly and variant_id is returned.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_agent_cart_validate_uses_cached_products_and_returns_variant_id(client):
    merchant_id = "merch_test_1"
    product_id = "prod_1"
    variant_id = "var_1"

    product_data = {
        "id": product_id,
        "product_id": product_id,
        "platform": "shopify",
        "merchant_id": merchant_id,
        "title": "Test Product",
        "price": 12.5,
        "currency": "USD",
        "in_stock": True,
        "variants": [
            {"id": variant_id, "variant_id": variant_id, "title": "Default", "price": 12.5, "inventory_quantity": 10},
        ],
    }

    with patch("routes.agent_api.get_merchant_active_stores", new=AsyncMock(return_value=[{"platform": "shopify"}])), patch(
        "routes.agent_api.get_cached_products",
        new=AsyncMock(return_value=[{"product_data": product_data}]),
    ), patch(
        "routes.agent_api.verify_merchant_active",
        new=AsyncMock(return_value={"id": merchant_id, "status": "active"}),
    ), patch(
        "routes.agent_api.log_agent_request",
        new=AsyncMock(return_value=None),
    ):
        res = client.post(
            f"/agent/v1/cart/validate?merchant_id={merchant_id}&shipping_country=US",
            headers={"X-API-Key": "test-api-key"},
            json=[{"product_id": product_id, "quantity": 1}],
        )

    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "success"
    assert isinstance(payload.get("items"), list) and payload["items"]
    assert payload["items"][0]["product_id"] == product_id
    assert payload["items"][0]["variant_id"] == variant_id

