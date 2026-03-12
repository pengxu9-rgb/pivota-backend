import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
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
        "id": "eps_flagged_1",
        "external_product_id": "ext_flagged_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://theordinary.com/de-de/uv-filters-spf-45-serum-100720.html",
        "canonical_url": "https://theordinary.com/de-de/contact-us.html",
        "domain": "theordinary.com",
        "title": "UV Filters SPF 45 Serum",
        "image_url": "https://cdn.example.com/img-1.jpg",
        "price_amount": 24.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {
            "title": "UV Filters SPF 45 Serum",
            "description": "Experience the ultimate luxury with UV Filters SPF 45 Serum. Ein vielseitiges Öl für Haut und Haare.",
            "image_url": "https://cdn.example.com/img-1.jpg",
            "image_urls": ["https://cdn.example.com/img-1.jpg", "https://cdn.example.com/img-2.jpg"],
            "snapshot": {
                "canonical_url": "https://theordinary.com/de-de/contact-us.html",
                "title": "UV Filters SPF 45 Serum",
                "description": "Experience the ultimate luxury with UV Filters SPF 45 Serum. Ein vielseitiges Öl für Haut und Haare.",
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "TO-001",
                    "title": "30ml",
                    "price_amount": 24.0,
                    "currency": "EUR",
                    "availability": "in_stock",
                    "image_url": "https://cdn.example.com/img-1.jpg",
                    "description": "Ingredients: Aqua, Glycerin",
                }
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


def test_external_seed_audit_queue_lists_flagged_rows(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    flagged = _seed_row()
    clean = _seed_row(
        id="eps_clean_1",
        external_product_id="ext_clean_1",
        destination_url="https://example.com/en-us/product/clean-serum.html",
        canonical_url="https://example.com/en-us/product/clean-serum.html",
        domain="example.com",
        title="Clean Serum",
        price_currency="USD",
        seed_data={
            "title": "Clean Serum",
            "description": "A lightweight daily serum that hydrates and protects skin.",
            "image_url": "https://cdn.example.com/clean-1.jpg",
            "image_urls": ["https://cdn.example.com/clean-1.jpg"],
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/clean-serum.html",
                "title": "Clean Serum",
                "description": "A lightweight daily serum that hydrates and protects skin.",
            },
            "variants": [
                {
                    "variant_id": "v-clean",
                    "sku": "CLN-001",
                    "title": "50ml",
                    "price_amount": 18.0,
                    "currency": "USD",
                    "availability": "in_stock",
                    "image_url": "https://cdn.example.com/clean-1.jpg",
                    "description": "Ingredients: Aqua, Niacinamide",
                }
            ],
        },
    )

    async def fake_fetch_all(_query: str, values=None):
        rows = [flagged, clean]
        if values and values.get("seed_id"):
            rows = [row for row in rows if row["id"] == values["seed_id"]]
        return rows

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)

    res = client.get(
        "/employee/products/external-seeds/audit-queue",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text

    payload = res.json()
    assert payload["status"] == "success"
    assert payload["summary"]["scanned"] == 1
    assert payload["summary"]["flagged_rows"] == 1
    assert payload["summary"]["by_severity"]["blocker"] >= 1

    items = payload["items"]
    assert len(items) == 1
    item = items[0]
    assert item["seed"]["id"] == "eps_flagged_1"
    assert item["audit"]["flagged"] is True
    assert "locale_market_mismatch" in item["audit"]["anomaly_types"]
    assert item["harvester"]["candidate_count"] == 1
    assert item["harvester"]["prefilled_ingredient_count"] == 1
    assert item["pipeline"]["seed_status"] == "blocked"


def test_external_seed_audit_detail_returns_item(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    row = _seed_row()

    async def fake_fetch_all(_query: str, values=None):
        if values and values.get("seed_id") == row["id"]:
            return [row]
        return []

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(employee_products_module.database, "fetch_one", AsyncMock(return_value=None))

    res = client.get(
        f"/employee/products/external-seeds/{row['id']}/audit",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text

    payload = res.json()
    assert payload["status"] == "success"
    item = payload["item"]
    assert item["seed"]["id"] == row["id"]
    assert item["audit"]["blocker_count"] >= 1
    assert any(f["anomaly_type"] == "locale_market_mismatch" for f in item["findings"])
    assert item["harvester"]["ready"] is False
