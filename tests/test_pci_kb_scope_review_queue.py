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


def _seed_row(seed_id: str, title: str, canonical_url: str, *, brand: str = "Ole Henriksen", domain: str = "olehenriksen.com", category: str = "Skincare", variants=None):
    return {
        "id": seed_id,
        "external_product_id": f"ext_{seed_id}",
        "market": "US",
        "tool": "*",
        "destination_url": canonical_url,
        "canonical_url": canonical_url,
        "domain": domain,
        "title": title,
        "image_url": None,
        "price_amount": None,
        "price_currency": None,
        "availability": None,
        "seed_data": {
            "brand": brand,
            "snapshot": {
                "canonical_url": canonical_url,
                "title": title,
                "category": category,
                "variants": variants or [],
            },
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": None,
        "updated_at": None,
    }


def test_pci_kb_scope_review_queue_lists_unresolved_rows(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    kb_rows = [
        {
            "sku_key": "extseed:eps_serum:41609",
            "brand": "Ole Henriksen",
            "product_name": "Banana Bright Vitamin C Serum - 30ml",
            "source_ref": "https://olehenriksen.com/products/banana-bright-vitamin-c-serum?variant=41609",
        },
        {
            "sku_key": "extseed:eps_blush:PIXI-CASSIS",
            "brand": "Pixi Beauty",
            "product_name": "On-the-Glow Blush - Cassis",
            "source_ref": "https://pixibeauty.com/products/on-the-glow-blush?variant=PIXI-CASSIS",
        },
    ]
    seed_rows = [
        _seed_row(
            "eps_serum",
            "Banana Bright Vitamin C Serum",
            "https://olehenriksen.com/products/banana-bright-vitamin-c-serum",
            variants=[
                {
                    "sku": "41609",
                    "variant_id": "41609",
                    "option_value": "30ml",
                    "url": "https://olehenriksen.com/products/banana-bright-vitamin-c-serum",
                }
            ],
        ),
        _seed_row(
            "eps_blush",
            "On-the-Glow Blush",
            "https://pixibeauty.com/products/on-the-glow-blush",
            brand="Pixi Beauty",
            domain="pixibeauty.com",
            category="Makeup",
            variants=[
                {
                    "sku": "PIXI-CASSIS",
                    "variant_id": "PIXI-CASSIS",
                    "option_value": "Cassis",
                    "url": "https://pixibeauty.com/products/on-the-glow-blush",
                }
            ],
        ),
    ]
    review_rows = [
        {
            "sku_key": "extseed:eps_serum:41609",
            "decision": "keep_in_kb",
            "notes": "Reviewed",
            "reviewed_by_employee_id": "emp_test",
            "reviewed_at": None,
            "updated_at": None,
        }
    ]

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "_ensure_employee_pci_kb_scope_reviews_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "fetch_pci_kb_rows_sync", lambda: kb_rows)
    monkeypatch.setattr(employee_products_module, "_fetch_external_seed_rows_by_ids", AsyncMock(return_value=seed_rows))
    monkeypatch.setattr(employee_products_module, "_fetch_employee_pci_kb_scope_review_rows", AsyncMock(return_value=review_rows))

    res = client.get(
        "/employee/products/pci-kb-scope-reviews",
        headers={"Authorization": "Bearer test-token"},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["status"] == "success"
    assert payload["summary"]["scanned"] == 2
    assert payload["summary"]["returned"] == 1
    assert payload["items"][0]["sku_key"] == "extseed:eps_blush:PIXI-CASSIS"
    assert payload["items"][0]["scope_decision"] == "block"
    assert payload["items"][0]["recommended_action"] == "remove_from_kb"


def test_pci_kb_scope_review_update_supports_keep_remove_and_reopen(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.employee_products as employee_products_module

    kb_rows = [
        {
            "sku_key": "extseed:eps_blush:PIXI-CASSIS",
            "brand": "Pixi Beauty",
            "product_name": "On-the-Glow Blush - Cassis",
            "source_ref": "https://pixibeauty.com/products/on-the-glow-blush?variant=PIXI-CASSIS",
        }
    ]
    seed_rows = [
        _seed_row(
            "eps_blush",
            "On-the-Glow Blush",
            "https://pixibeauty.com/products/on-the-glow-blush",
            brand="Pixi Beauty",
            domain="pixibeauty.com",
            category="Makeup",
            variants=[
                {
                    "sku": "PIXI-CASSIS",
                    "variant_id": "PIXI-CASSIS",
                    "option_value": "Cassis",
                    "url": "https://pixibeauty.com/products/on-the-glow-blush",
                }
            ],
        ),
    ]
    review_rows = []
    upserts = []
    deletions = []
    review_deletes = []

    async def fake_upsert(**kwargs):
        upserts.append(kwargs)

    async def fake_delete_review(sku_key: str):
        review_deletes.append(sku_key)

    def fake_delete_kb(sku_keys):
        deletions.append(list(sku_keys))
        return {"deleted_count": len(sku_keys), "deleted_keys": list(sku_keys)}

    monkeypatch.setattr(employee_products_module, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "_ensure_employee_pci_kb_scope_reviews_table", AsyncMock(return_value=None))
    monkeypatch.setattr(employee_products_module, "fetch_pci_kb_rows_sync", lambda: kb_rows)
    monkeypatch.setattr(employee_products_module, "_fetch_external_seed_rows_by_ids", AsyncMock(return_value=seed_rows))
    monkeypatch.setattr(employee_products_module, "_fetch_employee_pci_kb_scope_review_rows", AsyncMock(return_value=review_rows))
    monkeypatch.setattr(employee_products_module, "_upsert_employee_pci_kb_scope_review_row", fake_upsert)
    monkeypatch.setattr(employee_products_module, "_delete_employee_pci_kb_scope_review_row", fake_delete_review)
    monkeypatch.setattr(employee_products_module, "delete_pci_kb_rows_sync", fake_delete_kb)

    keep_res = client.patch(
        "/employee/products/pci-kb-scope-reviews/extseed:eps_blush:PIXI-CASSIS",
        headers={"Authorization": "Bearer test-token"},
        json={"decision": "keep_in_kb", "notes": "Allowed by policy"},
    )
    assert keep_res.status_code == 200, keep_res.text
    assert keep_res.json()["resolved"] is True
    assert upserts[-1]["decision"] == "keep_in_kb"

    remove_res = client.patch(
        "/employee/products/pci-kb-scope-reviews/extseed:eps_blush:PIXI-CASSIS",
        headers={"Authorization": "Bearer test-token"},
        json={"decision": "remove_from_kb", "notes": "Makeup out of scope"},
    )
    assert remove_res.status_code == 200, remove_res.text
    assert deletions[-1] == ["extseed:eps_blush:PIXI-CASSIS"]
    assert upserts[-1]["decision"] == "remove_from_kb"

    reopen_res = client.patch(
        "/employee/products/pci-kb-scope-reviews/extseed:eps_blush:PIXI-CASSIS",
        headers={"Authorization": "Bearer test-token"},
        json={"decision": "reopen"},
    )
    assert reopen_res.status_code == 200, reopen_res.text
    assert review_deletes == ["extseed:eps_blush:PIXI-CASSIS"]
