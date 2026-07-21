from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from utils.auth import get_current_employee


@pytest.fixture
def client():
    import routes.employee_products as employee_products_module

    async def override_employee():
        return {"employee_id": "emp_test", "email": "test@example.com"}

    app = FastAPI()
    app.include_router(employee_products_module.router)
    app.dependency_overrides[get_current_employee] = override_employee
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
                {
                    "variant_id": "v1",
                    "title": "50ml",
                    "price_amount": 12.34,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "image_url": "https://example.com/v1.jpg",
                    "label_image_url": "https://example.com/v1_swatch.png",
                },
                {
                    "variant_id": "v2",
                    "title": "100ml",
                    "price_amount": 19.99,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "image_url": "https://example.com/v2.jpg",
                    "label_image_url": "https://example.com/v2_swatch.png",
                },
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
    variants = product.get("variants") or []
    assert any(v.get("variant_id") == "v1" and v.get("image_url") == "https://example.com/v1.jpg" for v in variants)
    assert any(v.get("variant_id") == "v2" and v.get("image_url") == "https://example.com/v2.jpg" for v in variants)

    raw = payload.get("raw") or {}
    assert raw.get("source") == "external_seed"
    assert raw.get("external_product_id") == "ext_test_1"
    raw_variants = raw.get("variants") or []
    assert len(raw_variants) >= 1
    assert any(v.get("variant_id") == "v1" and v.get("label_image_url") == "https://example.com/v1_swatch.png" for v in raw_variants)
    assert any(v.get("variant_id") == "v2" and v.get("label_image_url") == "https://example.com/v2_swatch.png" for v in raw_variants)


def test_employee_product_detail_hides_stale_description_for_blocked_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    blocked_row = _seed_row(
        title="The Clear Set",
        canonical_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        destination_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        seed_data={
            "title": "The Clear Set",
            "description": "Ein dreistufiges Regimen mit Salicylic Acid 2% Solution für eine klarere Haut.",
            "snapshot": {
                "canonical_url": "https://theordinary.com/en-us/the-clear-set-100630.html",
                "description": "",
                "diagnostics": {"failure_category": "no_product_urls"},
            },
            "variants": [],
        },
    )

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [blocked_row]
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
    product = payload.get("product") or {}
    raw = payload.get("raw") or {}
    assert product.get("description") == ""
    assert raw.get("description") == ""


def test_employee_product_detail_prefers_manual_description_override_for_blocked_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    blocked_row = _seed_row(
        title="The Clear Set",
        canonical_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        destination_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        seed_data={
            "title": "The Clear Set",
            "description": "Ein dreistufiges Regimen mit Salicylic Acid 2% Solution für eine klarere Haut.",
            "manual_overrides": {
                "description": "A complete regimen that targets breakouts, blemishes, and the look of congestion.",
                "source": "employee_review",
            },
            "snapshot": {
                "canonical_url": "https://theordinary.com/en-us/the-clear-set-100630.html",
                "description": "",
                "diagnostics": {"failure_category": "no_product_urls"},
            },
            "variants": [],
        },
    )

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [blocked_row]
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
    product = payload.get("product") or {}
    raw = payload.get("raw") or {}
    expected = "A complete regimen that targets breakouts, blemishes, and the look of congestion."
    assert product.get("description") == expected
    assert raw.get("description") == expected


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
