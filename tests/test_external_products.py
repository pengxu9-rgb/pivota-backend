import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_agent_cart_validate_rejects_external_seed_merchant(client: TestClient) -> None:
    res = client.post(
        "/agent/v1/cart/validate?merchant_id=external_seed&shipping_country=US",
        headers={"X-API-Key": "test-api-key"},
        json=[{"product_id": "ext_1", "quantity": 1}],
    )
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "EXTERNAL_PRODUCT_CHECKOUT_DISABLED"


def test_agent_orders_create_rejects_external_seed_merchant(client: TestClient) -> None:
    payload = {
        "merchant_id": "external_seed",
        "customer_email": "buyer@example.com",
        "items": [
            {
                "product_id": "ext_1",
                "product_title": "External",
                "variant_id": "ext_1",
                "quantity": 1,
                "unit_price": 10.0,
                "subtotal": 10.0,
            }
        ],
        "shipping_address": {
            "name": "Buyer",
            "address_line1": "1 Main St",
            "city": "SF",
            "postal_code": "94105",
            "country": "US",
        },
        "currency": "USD",
    }

    res = client.post(
        "/agent/v1/orders/create",
        headers={"X-API-Key": "test-api-key"},
        json=payload,
    )
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "EXTERNAL_PRODUCT_CHECKOUT_DISABLED"


def test_agent_products_search_surfaces_external_seeds(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_test_1",
                    "external_product_id": "ext_test_1",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Example External Product",
                    "image_url": None,
                    "price_amount": 12.34,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {},
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module, "_is_domain_allowed", AsyncMock(return_value=True))

    res = client.get(
        "/agent/v1/products/search?merchant_id=external_seed&query=&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)
    external = next(p for p in products if p.get("merchant_id") == "external_seed")
    assert isinstance(external.get("external_redirect_url"), str)
    assert "/r?token=" in external.get("external_redirect_url")


def test_agent_products_search_cross_merchant_injects_external_seeds_by_domain(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_sdk_fixed as agent_sdk_fixed_module

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_test_1",
                    "external_product_id": "ext_test_1",
                    "market": "US",
                    "tool": "*",
                    "utm_template": None,
                    "partner_type": None,
                    "disclosure_text": None,
                    "destination_url": "https://example.com/product/1",
                    "canonical_url": None,
                    "domain": "example.com",
                    "title": "Example External Product",
                    "image_url": None,
                    "price_amount": 12.34,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "seed_data": {},
                    "status": "active",
                    "notes": None,
                    "created_by_employee_id": None,
                    "attached_product_key": None,
                    "attached_variant_id": None,
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        return []

    async def fake_fetch_one(query: str, values=None):
        if "COUNT" in str(query):
            return {"total": 0}
        return None

    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(agent_sdk_fixed_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_sdk_fixed_module, "_is_domain_allowed", AsyncMock(return_value=True))

    res = client.get(
        "/agent/v1/products/search?query=example.com&limit=20&offset=0&in_stock_only=false",
        headers={"X-API-Key": "test-api-key"},
    )
    assert res.status_code == 200
    payload = res.json()
    products = payload.get("products") or []
    assert any(p.get("merchant_id") == "external_seed" for p in products)
