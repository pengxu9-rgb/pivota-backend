import pytest


@pytest.mark.asyncio
async def test_list_merchant_products_reports_true_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_products as module

    async def fake_fetch_one(query, values=None):
        return {"total": 740}

    async def fake_fetch_all(query, values=None):
        return [
            {
                "platform": "shopify",
                "platform_product_id": "9859804856648",
                "product_data": {
                    "title": "Warm Fall/Winter Padded Winter Vest for Dogs & Cats",
                    "price": 19.99,
                    "currency": "USD",
                    "image_url": "https://example.com/product.jpg",
                },
                "cached_at": "2026-03-19T00:00:00Z",
            }
        ]

    async def fake_get_enrichment(**kwargs):
        return {}

    async def fake_fetch_latest_quality_row(**kwargs):
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module, "get_enrichment", fake_get_enrichment)
    monkeypatch.setattr(
        module,
        "_fetch_latest_quality_row",
        fake_fetch_latest_quality_row,
    )

    response = await module.list_merchant_products(
        page=1,
        page_size=100,
        current_user={"role": "merchant", "merchant_id": "merch_test"},
    )

    assert response["page"] == 1
    assert response["page_size"] == 100
    assert response["total"] == 740
    assert len(response["items"]) == 1
    assert response["items"][0]["platform_product_id"] == "9859804856648"
