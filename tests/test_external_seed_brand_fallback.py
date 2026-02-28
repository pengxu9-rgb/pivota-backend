from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_brand_query_strict_nonempty_but_irrelevant_triggers_broad_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.agent_api as agent_api_module

    strict_rows = [
        {
            "id": "seed_strict_irrelevant",
            "market": "US",
            "title": "Hydrating Lip Gloss",
            "destination_url": "https://example.com/strict",
            "seed_data": {"brand": "Other Brand"},
        }
    ]
    broad_rows = [
        {
            "id": "seed_broad_brand",
            "market": "UK",
            "title": "Fenty Beauty Gloss Bomb",
            "destination_url": "https://example.com/broad",
            "seed_data": {"brand": "Fenty Beauty"},
        }
    ]
    fetch_mock = AsyncMock(
        side_effect=[
            {
                "rows": strict_rows,
                "total_count": 1,
                "query_ms": 11,
                "query_timeout": False,
                "table_missing": False,
            },
            {
                "rows": broad_rows,
                "total_count": 1,
                "query_ms": 13,
                "query_timeout": False,
                "table_missing": False,
            },
        ]
    )
    monkeypatch.setattr(agent_api_module, "fetch_external_seed_rows", fetch_mock)
    monkeypatch.setattr(
        agent_api_module,
        "get_allowed_domains_for_market",
        AsyncMock(return_value=["example.com"]),
    )

    async def fake_build_external_seed_product(*, req, seed_row, allowed_domains=None, metrics_out=None):
        return {
            "product_id": str(seed_row.get("id") or ""),
            "id": str(seed_row.get("id") or ""),
            "title": str(seed_row.get("title") or ""),
            "source": "external_seed",
            "merchant_id": "external_seed",
            "destination_url": seed_row.get("destination_url"),
        }

    monkeypatch.setattr(agent_api_module, "_build_external_seed_product", fake_build_external_seed_product)

    metrics = {}
    products = await agent_api_module._load_external_seed_products_for_search(
        req=None,
        query="fenty beauty",
        limit=24,
        page_offset=0,
        build_budget_ms=500,
        build_concurrency=2,
        include_seed_data_text_match=True,
        brand_terms=["fenty beauty"],
        brand_query_detected=True,
        metrics_out=metrics,
    )

    assert fetch_mock.await_count == 2
    assert fetch_mock.await_args_list[0].kwargs.get("scope") == "brand_strict"
    assert fetch_mock.await_args_list[1].kwargs.get("scope") == "brand_broad"
    assert metrics.get("brand_strict_rows") == 1
    assert metrics.get("brand_relevant_rows") == 0
    assert metrics.get("broad_fallback_used") is True
    assert metrics.get("broad_scope_rows") == 1
    assert any(str(product.get("product_id")) == "seed_broad_brand" for product in products)
