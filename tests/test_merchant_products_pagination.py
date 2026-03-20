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

    async def fake_build_quality_projection_bundle(_merchant_id, _cache_rows):
        return {
            "enrichments_by_key": {},
            "snapshot_rows_by_key": {},
            "projections_by_key": {},
            "coverage": {
                "total_products": 1,
                "snapshot_scored_products": 0,
                "effective_scored_products": 0,
                "preview_only_products": 0,
                "unscored_products": 1,
                "coverage_state": "empty",
                "latest_snapshot_at": None,
                "backfill_recommended": False,
                "active_backfill_job": None,
            },
        }

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        module,
        "_build_quality_projection_bundle",
        fake_build_quality_projection_bundle,
    )
    monkeypatch.setattr(
        module,
        "build_agent_push_projection_from_cache_row",
        lambda _cache_row: {
            "agent_push_status": "eligible_for_agent_push",
            "agent_push_reason_codes": [],
            "eligible_variant_count": 1,
            "excluded_variant_count": 0,
            "store_data_last_checked_at": "2026-03-19T00:00:00Z",
        },
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
    assert response["items"][0]["agent_push"]["agent_push_status"] == "eligible_for_agent_push"


@pytest.mark.asyncio
async def test_product_quality_summary_includes_effective_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.merchant_products as module

    async def fake_fetch_all(query, values=None):
        return [
            {
                "platform": "shopify",
                "platform_product_id": "prod_1",
                "product_data": {"title": "One", "price": 10, "currency": "USD"},
                "cached_at": "2026-03-19T00:00:00Z",
                "expires_at": "2026-03-20T00:00:00Z",
            },
            {
                "platform": "shopify",
                "platform_product_id": "prod_2",
                "product_data": {"title": "Two", "price": 20, "currency": "USD"},
                "cached_at": "2026-03-19T00:00:00Z",
                "expires_at": "2026-03-20T00:00:00Z",
            },
        ]

    async def fake_build_quality_projection_bundle(_merchant_id, _rows):
        return {
            "enrichments_by_key": {},
            "snapshot_rows_by_key": {
                ("shopify", "prod_1"): {
                    "content_quality_score": 88.0,
                    "model_readiness_score": 77.0,
                    "snapshot_date": "2026-03-19T00:00:00Z",
                }
            },
            "projections_by_key": {
                ("shopify", "prod_1"): {
                    "content_quality_score": 88.0,
                    "model_readiness_score": 77.0,
                    "conversion_potential_score": None,
                    "last_evaluated_at": "2026-03-19T00:00:00Z",
                    "quality_source": "snapshot",
                },
                ("shopify", "prod_2"): {
                    "content_quality_score": 63.0,
                    "model_readiness_score": 59.0,
                    "conversion_potential_score": None,
                    "last_evaluated_at": None,
                    "quality_source": "preview",
                },
            },
            "coverage": {
                "total_products": 2,
                "snapshot_scored_products": 1,
                "effective_scored_products": 2,
                "preview_only_products": 1,
                "unscored_products": 0,
                "coverage_state": "full",
                "latest_snapshot_at": "2026-03-19T00:00:00Z",
                "backfill_recommended": True,
                "active_backfill_job": None,
            },
        }

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(module, "_build_quality_projection_bundle", fake_build_quality_projection_bundle)

    response = await module.get_product_quality_summary(
        current_user={"role": "merchant", "merchant_id": "merch_test"},
    )

    assert response["status"] == "success"
    assert response["data"]["total_products"] == 2
    assert response["data"]["scored_products"] == 2
    assert response["data"]["snapshot_scored_products"] == 1
    assert response["data"]["preview_only_products"] == 1
    assert response["data"]["coverage_state"] == "full"
