from types import SimpleNamespace
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


def test_external_seed_audit_queue_supports_merchant_id_filter(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    attached = _seed_row(
        id="eps_attached",
        attached_product_key="merch_1|shopify|prod_1",
    )
    unattached = _seed_row(
        id="eps_unattached",
        domain="example.com",
        canonical_url="https://example.com/en-us/product/referral-1",
        destination_url="https://example.com/en-us/product/referral-1",
        seed_data={
            "title": "Referral Serum",
            "description": "Experience the ultimate luxury with Referral Serum.",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/referral-1",
                "title": "Referral Serum",
                "description": "Experience the ultimate luxury with Referral Serum.",
            },
            "variants": [],
        },
    )

    async def fake_inventory(*, merchant_id: str, status: str):
        assert merchant_id == "merch_1"
        assert status == "active"
        return {
            "merchant_id": merchant_id,
            "matched_domains": ["example.com"],
            "attached_rows": [attached],
            "domain_unattached_rows": [unattached],
            "rows": [attached, unattached],
            "matched_via_by_seed": {"eps_attached": "attached_product_key", "eps_unattached": "merchant_domain"},
        }

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "fetch_merchant_referral_inventory", fake_inventory)

    res = client.get(
        "/employee/products/external-seeds/audit-queue",
        params={"merchant_id": "merch_1", "flagged_only": "false"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["meta"]["filters"]["merchant_id"] == "merch_1"
    returned_ids = [item["seed"]["id"] for item in payload["items"]]
    assert "eps_attached" in returned_ids
    assert "eps_unattached" in returned_ids


def test_backfill_storefront_external_seeds_dry_run(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    async def fake_candidates(*, merchant_id: str, limit: int, market=None):
        assert merchant_id == "merch_1"
        assert limit == 25
        assert market is None
        return {
            "merchant_id": merchant_id,
            "market": "US",
            "primary_domain": "example.com",
            "matched_domains": ["example.com"],
            "candidates": [
                {
                    "attached_product_key": "merch_1|shopify|prod_1",
                    "storefront_url": "https://example.com/products/serum-1",
                    "title": "Serum 1",
                    "external_product_id": "ext_1",
                },
                {
                    "attached_product_key": "merch_1|shopify|prod_2",
                    "storefront_url": "https://example.com/products/serum-2",
                    "title": "Serum 2",
                    "external_product_id": "ext_2",
                },
            ],
        }

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "_fetch_storefront_referral_seed_candidates", fake_candidates)

    res = client.post(
        "/employee/products/external-seeds/backfill-storefront",
        headers={"Authorization": "Bearer test-token"},
        json={"merchant_id": "merch_1", "limit": 25, "dry_run": True},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["dry_run"] is True
    assert payload["candidate_count"] == 2
    assert payload["created"] == 0
    assert payload["items"][0]["action"] == "preview"
    assert payload["items"][0]["canonical_url"] == "https://example.com/products/serum-1"


def test_backfill_storefront_external_seeds_write_mode(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    candidates = [
        {
            "attached_product_key": "merch_1|shopify|prod_1",
            "storefront_url": "https://example.com/products/serum-1",
            "title": "Serum 1",
            "external_product_id": "ext_1",
        },
        {
            "attached_product_key": "merch_1|shopify|prod_2",
            "storefront_url": "https://example.com/products/serum-2",
            "title": "Serum 2",
            "external_product_id": "ext_2",
        },
        {
            "attached_product_key": "merch_1|shopify|prod_3",
            "storefront_url": "https://example.com/products/serum-3",
            "title": "Serum 3",
            "external_product_id": "ext_3",
        },
    ]

    async def fake_candidates(*, merchant_id: str, limit: int, market=None):
        return {
            "merchant_id": merchant_id,
            "market": "US",
            "primary_domain": "example.com",
            "matched_domains": ["example.com"],
            "candidates": candidates,
        }

    actions = [
        {"action": "created", "seed_id": "eps_1", "canonical_url": candidates[0]["storefront_url"]},
        {"action": "updated", "seed_id": "eps_2", "canonical_url": candidates[1]["storefront_url"]},
        {"action": "skipped", "seed_id": "eps_3", "canonical_url": candidates[2]["storefront_url"]},
    ]

    async def fake_upsert(candidate, *, employee_id=None):
        assert employee_id == "emp_test"
        return actions.pop(0)

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "_fetch_storefront_referral_seed_candidates", fake_candidates)
    monkeypatch.setattr(employee_products_module, "_upsert_storefront_referral_seed_candidate", fake_upsert)

    res = client.post(
        "/employee/products/external-seeds/backfill-storefront",
        headers={"Authorization": "Bearer test-token"},
        json={"merchant_id": "merch_1", "dry_run": False},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["dry_run"] is False
    assert payload["candidate_count"] == 3
    assert payload["created"] == 1
    assert payload["updated"] == 1
    assert payload["skipped"] == 1


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


def test_audit_prefers_snapshot_description_over_stale_variant_description() -> None:
    from services.external_seed_audit import get_primary_description

    row = _seed_row(
        seed_data={
            "description": "Legacy German description",
            "snapshot": {
                "description": "A lightweight SPF 45 sunscreen serum that protects and hydrates.",
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "description": "Ein leichtes Sonnenschutzserum mit Lichtschutzfaktor 45.",
                }
            ],
        }
    )

    description = get_primary_description(row)
    assert description == "A lightweight SPF 45 sunscreen serum that protects and hydrates."


def test_audit_prefers_manual_description_override_for_blocked_seed() -> None:
    from services.external_seed_audit import get_primary_description

    row = _seed_row(
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

    description = get_primary_description(row)
    assert description == "A complete regimen that targets breakouts, blemishes, and the look of congestion."


def test_audit_queue_hides_stale_description_for_blocked_seed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    blocked = _seed_row(
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

    async def fake_fetch_all(_query: str, values=None):
        rows = [blocked]
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
    assert payload["items"][0]["seed"]["description"] == ""


def test_refresh_external_seed_replaces_stale_localized_description_and_variants(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    stored_row = _seed_row(
        canonical_url="https://theordinary.com/de-de/uv-filters-spf-45-serum-100720.html",
        destination_url="https://theordinary.com/de-de/uv-filters-spf-45-serum-100720.html",
        seed_data={
            "title": "UV Filters SPF 45 Serum",
            "description": "Ein leichtes Sonnenschutzserum mit Lichtschutzfaktor 45.",
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "TO-001",
                    "title": "30ml",
                    "description": "Ein leichtes Sonnenschutzserum mit Lichtschutzfaktor 45.",
                }
            ],
            "snapshot": {
                "canonical_url": "https://theordinary.com/de-de/uv-filters-spf-45-serum-100720.html",
                "description": "Ein leichtes Sonnenschutzserum mit Lichtschutzfaktor 45.",
            },
        },
    )

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        stored_row.update(values)
        if "seed_data" in values:
            stored_row["seed_data"] = values["seed_data"]

    fake_snapshot = SimpleNamespace(
        canonical_url="https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html",
        domain="theordinary.com",
        title="UV Filters SPF 45 Serum",
        image_url="https://cdn.example.com/en.jpg",
        price_amount=24.0,
        price_currency="USD",
        availability="in_stock",
        fetched_at=None,
        evidence={
            "description": "A lightweight SPF 45 sunscreen serum that protects and hydrates.",
            "image_urls": ["https://cdn.example.com/en.jpg"],
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "TO-001",
                    "title": "30ml",
                    "description": "A lightweight SPF 45 sunscreen serum that protects and hydrates.",
                }
            ],
        },
    )

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(employee_products_module, "resolve_external_offer", AsyncMock(return_value=fake_snapshot))
    monkeypatch.setattr(
        employee_products_module,
        "_make_redirect_url",
        AsyncMock(return_value="https://employee.pivota.cc/redirect/test"),
    )

    res = client.post(
        f"/employee/products/external-seeds/{stored_row['id']}/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"

    seed_data = stored_row["seed_data"]
    assert stored_row["canonical_url"] == "https://theordinary.com/en-us/uv-filters-spf-45-serum-100720.html"
    assert seed_data["description"] == "A lightweight SPF 45 sunscreen serum that protects and hydrates."
    assert seed_data["variants"][0]["description"] == "A lightweight SPF 45 sunscreen serum that protects and hydrates."


def test_refresh_external_seed_clears_stale_description_when_seed_remains_blocked(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    stored_row = _seed_row(
        title="The Clear Set",
        canonical_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        destination_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        seed_data={
            "title": "The Clear Set",
            "description": "Ein dreistufiges Regimen mit Salicylic Acid 2% Solution für eine klarere Haut.",
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "TO-CLEAR-SET",
                    "title": "Default",
                    "description": "Ein dreistufiges Regimen mit Salicylic Acid 2% Solution für eine klarere Haut.",
                }
            ],
            "snapshot": {
                "canonical_url": "https://theordinary.com/en-us/the-clear-set-100630.html",
                "description": "",
                "diagnostics": {"failure_category": "no_product_urls"},
            },
        },
    )

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        stored_row.update(values)
        if "seed_data" in values:
            stored_row["seed_data"] = values["seed_data"]

    fake_snapshot = SimpleNamespace(
        canonical_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        domain="theordinary.com",
        title="The Clear Set",
        image_url=None,
        price_amount=None,
        price_currency="USD",
        availability="unknown",
        fetched_at=None,
        evidence={},
    )

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(employee_products_module, "resolve_external_offer", AsyncMock(return_value=fake_snapshot))
    monkeypatch.setattr(
        employee_products_module,
        "_make_redirect_url",
        AsyncMock(return_value="https://employee.pivota.cc/redirect/test"),
    )

    res = client.post(
        f"/employee/products/external-seeds/{stored_row['id']}/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"

    seed_data = stored_row["seed_data"]
    assert seed_data.get("description") is None
    assert seed_data["snapshot"].get("description") is None
    assert seed_data["variants"][0].get("description") is None


def test_refresh_external_seed_preserves_manual_description_override_when_seed_remains_blocked(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.employee_products as employee_products_module

    stored_row = _seed_row(
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
            "variants": [],
            "snapshot": {
                "canonical_url": "https://theordinary.com/en-us/the-clear-set-100630.html",
                "description": "",
                "diagnostics": {"failure_category": "no_product_urls"},
            },
        },
    )

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        stored_row.update(values)
        if "seed_data" in values:
            stored_row["seed_data"] = values["seed_data"]

    fake_snapshot = SimpleNamespace(
        canonical_url="https://theordinary.com/en-us/the-clear-set-100630.html",
        domain="theordinary.com",
        title="The Clear Set",
        image_url=None,
        price_amount=None,
        price_currency="USD",
        availability="unknown",
        fetched_at=None,
        evidence={},
    )

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(employee_products_module, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(employee_products_module, "resolve_external_offer", AsyncMock(return_value=fake_snapshot))
    monkeypatch.setattr(
        employee_products_module,
        "_make_redirect_url",
        AsyncMock(return_value="https://employee.pivota.cc/redirect/test"),
    )

    res = client.post(
        f"/employee/products/external-seeds/{stored_row['id']}/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text

    seed_data = stored_row["seed_data"]
    assert seed_data.get("description") is None
    assert seed_data.get("manual_overrides", {}).get("description") == (
        "A complete regimen that targets breakouts, blemishes, and the look of congestion."
    )
