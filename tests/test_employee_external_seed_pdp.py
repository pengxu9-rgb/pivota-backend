import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def _seed_row(**overrides):
    row = {
        "id": "eps_test_1",
        "external_product_id": "ext_test_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://example.com/product/1",
        "canonical_url": "https://example.com/product/1",
        "domain": "example.com",
        "title": "Example External Product",
        "image_url": "https://example.com/img_main.jpg",
        "price_amount": 12.34,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {
            "title": "Example External Product",
            "description": "Seed description",
            "image_urls": ["https://example.com/img_main.jpg", "https://example.com/img_2.jpg"],
            "variants": [
                {"variant_id": "v1", "title": "50ml", "price_amount": 12.34, "price_currency": "USD", "availability": "in_stock"},
                {"variant_id": "v2", "title": "100ml", "price_amount": 19.99, "price_currency": "USD", "availability": "in_stock"},
            ],
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_employee_product_detail_external_seed_synthesizes_view(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [_seed_row()]
        return []

    monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "get_enrichment", AsyncMock(return_value=None))

    res = client.get(
        "/employee/products/external_seed%7Cexternal%7Cext_test_1",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "success"
    assert payload.get("merchant_id") == "external_seed"
    assert payload.get("platform") == "external"
    assert payload.get("platform_product_id") == "ext_test_1"
    assert payload.get("product_key") == "external_seed|external|ext_test_1"

    product = payload.get("product") or {}
    assert product.get("product_id") == "ext_test_1"
    assert product.get("title") == "Example External Product"
    assert product.get("orderable") is False

    raw = payload.get("raw") or {}
    assert raw.get("source") == "external_seed"
    assert raw.get("external_product_id") == "ext_test_1"
    assert len(raw.get("variants") or []) >= 1


def test_employee_product_offers_external_seed_returns_external_offers(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [_seed_row()]
        return []

    monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "_make_redirect_url", AsyncMock(return_value="https://example.com/r?token=test"))
    monkeypatch.setattr(employee_products_module, "_get_primary_offer", AsyncMock(return_value=None))

    res = client.get(
        "/employee/products/external_seed%7Cexternal%7Cext_test_1/offers",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload.get("status") == "success"
    items = payload.get("items") or []
    assert items and items[0].get("source") == "external_seed"
    assert items[0].get("seed_id") == "eps_test_1"
    assert isinstance(items[0].get("action", {}).get("redirect_url"), str)

