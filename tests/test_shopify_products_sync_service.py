import json
from typing import Optional

import pytest

import services.shopify_products_sync as sync_service
from adapters.product_adapters import SHOPIFY_NEXT_PAGE_TOKEN_UNPARSEABLE


class _DummyStandardProduct:
    def __init__(self, product_id: str, description: Optional[str] = None) -> None:
        self.product_id = product_id
        self.description = description

    def json(self) -> str:
        return json.dumps(
            {
                "product_id": self.product_id,
                "title": f"Product {self.product_id}",
                "description": self.description,
                "raw": {"body_html": self.description},
            }
        )


@pytest.mark.asyncio
async def test_sync_marks_truncated_when_limit_reached_with_next_page(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [_DummyStandardProduct("p1"), _DummyStandardProduct("p2")], "next_1", None

    async def fake_upsert(**_kwargs):
        return None

    async def fake_delete_missing(**_kwargs):
        raise AssertionError("cache prune should not run for truncated sync")

    monkeypatch.setattr(sync_service, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(sync_service, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(sync_service, "upsert_product_cache", fake_upsert)
    monkeypatch.setattr(sync_service, "delete_missing_products_from_cache", fake_delete_missing)

    summary = await sync_service.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=2,
        ttl_seconds=3600,
        per_page=2,
        max_pages=20,
        ingest_catalog=False,
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

    async def fake_delete_missing(**_kwargs):
        raise AssertionError("cache prune should not run for truncated sync")

    monkeypatch.setattr(sync_service, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(sync_service, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(sync_service, "upsert_product_cache", fake_upsert)
    monkeypatch.setattr(sync_service, "delete_missing_products_from_cache", fake_delete_missing)

    summary = await sync_service.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=10,
        ttl_seconds=3600,
        per_page=5,
        max_pages=20,
        ingest_catalog=False,
    )

    assert summary["productsFetched"] == 1
    assert summary["productsUpserted"] == 1
    assert summary["pagesFetched"] == 1
    assert summary["nextPageToken"] is None
    assert summary["nextPageTokenPresent"] is False
    assert summary["truncated"] is True
    assert summary["truncatedReason"] == "next_page_token_unparseable"


@pytest.mark.asyncio
async def test_sync_injects_description_text_for_html_descriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.shopify_products_sync as module

    captured: list[dict] = []

    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [
            _DummyStandardProduct(
                "p_html",
                "<p>Clean lines</p><ul><li>Soft mesh</li><li>Light feel</li></ul>",
            )
        ], None, None

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        return None

    async def fake_hydrate(**kwargs):
        payloads = kwargs["product_payloads"]
        payloads[0]["platform_metadata"] = {"reviewed_ingredient_ids": ["Niacinamide"]}
        return payloads

    async def fake_delete_missing(**_kwargs):
        return 0

    monkeypatch.setattr(module, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(module, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(module, "upsert_product_cache", fake_upsert)
    monkeypatch.setattr(module, "hydrate_product_payloads_from_attached_seed_runtime_evidence", fake_hydrate)
    monkeypatch.setattr(module, "delete_missing_products_from_cache", fake_delete_missing)

    await module.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=10,
        ttl_seconds=3600,
        per_page=5,
        max_pages=20,
        ingest_catalog=False,
    )

    assert len(captured) == 1
    product_data = captured[0]["product_data"]
    assert product_data["description_text"] == "Clean lines\n- Soft mesh\n- Light feel"
    assert product_data["raw"]["description_text"] == "Clean lines\n- Soft mesh\n- Light feel"
    assert product_data["platform_metadata"]["reviewed_ingredient_ids"] == ["Niacinamide"]


@pytest.mark.asyncio
async def test_complete_shopify_sync_prunes_missing_catalog_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.catalog_sync_service as catalog_module
    import services.shopify_products_sync as module

    observed: dict = {}

    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [_DummyStandardProduct("p1"), _DummyStandardProduct("p2")], None, None

    async def fake_upsert(**_kwargs):
        return None

    async def fake_hydrate(**kwargs):
        return kwargs["product_payloads"]

    async def fake_delete_missing(**_kwargs):
        return 0

    async def fake_ingest(**kwargs):
        observed["ingest"] = kwargs
        return {"products_ingested": len(kwargs["product_payloads"])}

    async def fake_prune(**kwargs):
        observed["prune"] = kwargs
        return {"catalog_products": 3}

    monkeypatch.setattr(module, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(module, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(module, "upsert_product_cache", fake_upsert)
    monkeypatch.setattr(module, "hydrate_product_payloads_from_attached_seed_runtime_evidence", fake_hydrate)
    monkeypatch.setattr(module, "delete_missing_products_from_cache", fake_delete_missing)
    monkeypatch.setattr(catalog_module, "ingest_standard_products", fake_ingest)
    monkeypatch.setattr(catalog_module, "prune_missing_catalog_products_for_source", fake_prune)

    summary = await module.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=10,
        ttl_seconds=3600,
        per_page=5,
        max_pages=20,
        ingest_catalog=True,
    )

    assert summary["catalogDeletedStale"] == {"catalog_products": 3}
    assert summary["catalogPruneSkippedReason"] is None
    assert observed["prune"]["merchant_id"] == "merch_1"
    assert observed["prune"]["platform"] == "shopify"
    assert observed["prune"]["source_system"] == "shopify_products_sync"
    assert set(observed["prune"]["valid_source_product_ids"]) == {"p1", "p2"}


@pytest.mark.asyncio
async def test_truncated_shopify_sync_skips_catalog_prune(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.catalog_sync_service as catalog_module
    import services.shopify_products_sync as module

    async def fake_creds(_merchant_id: str):
        return {"shop_domain": "test-shop.myshopify.com", "access_token": "token"}

    async def fake_fetch(**_kwargs):
        return [_DummyStandardProduct("p1"), _DummyStandardProduct("p2")], "next_1", None

    async def fake_upsert(**_kwargs):
        return None

    async def fake_hydrate(**kwargs):
        return kwargs["product_payloads"]

    async def fake_delete_missing(**_kwargs):
        raise AssertionError("cache prune should not run for truncated sync")

    async def fake_ingest(**kwargs):
        return {"products_ingested": len(kwargs["product_payloads"])}

    async def fail_prune(**_kwargs):
        raise AssertionError("catalog prune should not run for truncated sync")

    monkeypatch.setattr(module, "_get_shopify_store_credentials", fake_creds)
    monkeypatch.setattr(module, "fetch_merchant_products", fake_fetch)
    monkeypatch.setattr(module, "upsert_product_cache", fake_upsert)
    monkeypatch.setattr(module, "hydrate_product_payloads_from_attached_seed_runtime_evidence", fake_hydrate)
    monkeypatch.setattr(module, "delete_missing_products_from_cache", fake_delete_missing)
    monkeypatch.setattr(catalog_module, "ingest_standard_products", fake_ingest)
    monkeypatch.setattr(catalog_module, "prune_missing_catalog_products_for_source", fail_prune)

    summary = await module.sync_shopify_products_for_merchant(
        merchant_id="merch_1",
        limit=2,
        ttl_seconds=3600,
        per_page=2,
        max_pages=20,
        ingest_catalog=True,
    )

    assert summary["truncated"] is True
    assert summary["deletedStale"] == 0
    assert summary["cachePruneSkippedReason"] == "limit_reached_with_next_page"
    assert summary["catalogDeletedStale"] is None
    assert summary["catalogPruneSkippedReason"] == "limit_reached_with_next_page"
