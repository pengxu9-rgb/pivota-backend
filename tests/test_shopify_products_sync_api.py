from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from main import app


def test_shopify_sync_endpoint_passes_ttl_seconds(monkeypatch) -> None:
    import routes.shopify_products_sync_api as sync_api_module

    monkeypatch.setenv("ADMIN_API_KEY", "admin_test_key")
    monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)
    monkeypatch.setattr(
        sync_api_module,
        "sync_shopify_products_for_merchant",
        AsyncMock(return_value={"merchantId": "m_001", "ttlSeconds": 3600, "productsUpserted": 3}),
    )

    client = TestClient(app)
    resp = client.post(
        "/agent/internal/shopify/products/sync/m_001?limit=250&ttl_seconds=3600",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("summary", {}).get("ttlSeconds") == 3600
    sync_api_module.sync_shopify_products_for_merchant.assert_awaited_once_with(  # type: ignore[attr-defined]
        merchant_id="m_001",
        limit=250,
        ttl_seconds=3600,
    )
