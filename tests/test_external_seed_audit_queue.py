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
    assert item["seed"]["product_urls"][0].startswith("https://theordinary.com/")
    assert item["harvester"]["source_refs"][0]["source_ref"].startswith("https://theordinary.com/")
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
    assert len(item["seed"]["product_urls"]) >= 1
    assert len(item["harvester"]["source_refs"]) >= 1
    assert item["harvester"]["ready"] is False


def test_update_external_seed_supports_audit_review_edits(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    stored_row = _seed_row()

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        stored_row.update(values)
        if "seed_data" in values:
            stored_row["seed_data"] = values["seed_data"]

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)

    res = client.patch(
        f"/employee/products/external-seeds/{stored_row['id']}",
        headers={"Authorization": "Bearer test-token"},
        json={
            "title": "Retitled Serum",
            "description": "Updated reviewer description",
            "destination_url": "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html",
            "canonical_url": "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html",
            "image_url": "https://cdn.example.com/reviewed.jpg",
            "notes": "Reviewed by audit ops",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"

    assert stored_row["destination_url"] == "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html"
    assert stored_row["canonical_url"] == "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html"
    assert stored_row["domain"] == "theordinary.com"
    assert stored_row["notes"] == "Reviewed by audit ops"

    seed_data = stored_row["seed_data"]
    assert seed_data["title"] == "Retitled Serum"
    assert seed_data["description"] == "Updated reviewer description"
    assert seed_data["image_url"] == "https://cdn.example.com/reviewed.jpg"
    assert seed_data["snapshot"]["title"] == "Retitled Serum"
    assert seed_data["snapshot"]["description"] == "Updated reviewer description"
    assert seed_data["snapshot"]["canonical_url"] == "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html"
