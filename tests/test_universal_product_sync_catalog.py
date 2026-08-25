from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.standard_product import ProductStatus, StandardProduct
from routes import universal_product_sync as module


def _standard_product(product_id: str = "prod_1") -> StandardProduct:
    return StandardProduct(
        id=product_id,
        product_id=product_id,
        merchant_id="merch_1",
        platform="wix",
        title="Rope Dog Leash",
        price=19.0,
        currency="USD",
        inventory_quantity=5,
        status=ProductStatus.ACTIVE,
        variants=[],
    )


def _client(monkeypatch, ingest_result):
    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return {"role": "admin"}

    async def fake_get_merchant_onboarding(_merchant_id):
        return {"merchant_id": "merch_1", "business_name": "Merchant"}

    async def fake_find_connected_store(*_args, **_kwargs):
        return {
            "store_id": "store_1",
            "platform": "wix",
            "domain": "https://example.wixsite.com/site",
            "api_key": "wix_api_key",
        }

    async def fake_fetch_merchant_products(**_kwargs):
        return [_standard_product()], None, None

    async def fake_upsert_product_cache(**_kwargs):
        return None

    async def fake_delete_missing_products_from_cache(**_kwargs):
        return 0

    async def fake_update_sync_status(*_args, **_kwargs):
        return None

    async def fake_ingest_standard_products(**_kwargs):
        if isinstance(ingest_result, Exception):
            raise ingest_result
        return ingest_result

    app.dependency_overrides[module.get_current_user] = fake_current_user
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "find_connected_store", fake_find_connected_store)
    monkeypatch.setattr(
        module,
        "prepare_platform_credentials",
        lambda *_args, **_kwargs: {"site_id": "site_1", "api_key": "api_key_1"},
    )
    monkeypatch.setattr(module, "fetch_merchant_products", fake_fetch_merchant_products)
    monkeypatch.setattr(module, "upsert_product_cache", fake_upsert_product_cache)
    monkeypatch.setattr(
        module,
        "delete_missing_products_from_cache",
        fake_delete_missing_products_from_cache,
    )
    monkeypatch.setattr(module, "update_sync_status", fake_update_sync_status)
    monkeypatch.setattr(module, "ingest_standard_products", fake_ingest_standard_products)

    return TestClient(app)


def test_universal_sync_returns_partial_failure_when_catalog_ingest_fails(monkeypatch):
    client = _client(monkeypatch, RuntimeError("catalog write failed"))

    response = client.post(
        "/products/sync-universal/",
        json={"merchant_id": "merch_1", "platform": "wix", "limit": 50},
    )

    assert response.status_code == 207
    payload = response.json()
    assert payload["status"] == "partial_failure"
    assert payload["products_synced"] == 1
    assert payload["catalog_synced"] == 0
    assert "catalog write failed" in payload["error"]


def test_universal_sync_returns_success_when_catalog_ingest_succeeds(monkeypatch):
    client = _client(monkeypatch, {"products_ingested": 1})

    response = client.post(
        "/products/sync-universal/",
        json={"merchant_id": "merch_1", "platform": "wix", "limit": 50},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["products_synced"] == 1
    assert payload["catalog_synced"] == 1
    assert payload["error"] is None


def test_universal_sync_rejects_payment_only_source_before_catalog_fetch(monkeypatch):
    client = _client(monkeypatch, {"products_ingested": 1})

    response = client.post(
        "/products/sync-universal/",
        json={"merchant_id": "merch_1", "platform": "antom", "limit": 50},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unsupported"
    assert payload["products_synced"] == 0
    assert "payment-orchestration" in payload["message"]
