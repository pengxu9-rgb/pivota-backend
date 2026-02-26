import json

import pytest

import services.shopify_products_sync as sync_service
from adapters.product_adapters import SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE


class _DummyStandardProduct:
    def __init__(self, product_id: str) -> None:
        self.product_id = product_id

    def json(self) -> str:
        return json.dumps({"product_id": self.product_id, "title": f"Product {self.product_id}"})


@pytest.mark.asyncio
async def test_sync_marks_truncated_when_limit_reached_with_next_page(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [_DummyStandardProduct("p1"), _DummyStandardProduct("p2")], "next_1", None

    async def fake_upsert(**_kwargs):
        return None

    monkeypatch.setattr(sync_service, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(sync_service, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(sync_service, "upsert_product_cache", fake_upsert)

    summary = await sync_service.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=2,
        ttl_seconds=3600,
        per_page=2,
        max_pages=20,
    )

    assert summary["productsFetched"] == 2
    assert summary["productsUpserted"] == 2
    assert summary["pagesFetched"] == 1
    assert summary["nextPageToken"] == "next_1"
    assert summary["nextPageTokenPresent"] is True
    assert summary["truncated"] is True
    assert summary["truncatedReason"] == "limit_reached_with_next_page"


@pytest.mark.asyncio
async def test_sync_marks_truncated_when_next_token_is_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [_DummyStandardProduct("p1")], SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE, None

    async def fake_upsert(**_kwargs):
        return None

    monkeypatch.setattr(sync_service, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(sync_service, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(sync_service, "upsert_product_cache", fake_upsert)

    summary = await sync_service.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=10,
        ttl_seconds=3600,
        per_page=5,
        max_pages=20,
    )

    assert summary["productsFetched"] == 1
    assert summary["productsUpserted"] == 1
    assert summary["pagesFetched"] == 1
    assert summary["nextPageToken"] is None
    assert summary["nextPageTokenPresent"] is False
    assert summary["truncated"] is True
    assert summary["truncatedReason"] == "next_page_token_unparseable"
