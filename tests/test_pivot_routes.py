from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.pivot_routes as module
from models.catalog import (
    MerchantNode,
    OfferNode,
    PivotOffersResolveResponse,
    PivotPricing,
    PivotQueryResponse,
    PivotResultItem,
    ProductNode,
    SkuNode,
)
from utils.auth import get_current_user


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "user_123"}
    return app


def _sample_result() -> PivotResultItem:
    return PivotResultItem(
        merchant=MerchantNode(merchant_id="merch_1", merchant_name="Demo Merchant", primary_platform="shopify"),
        product=ProductNode(product_key="prod::1", source_product_id="111", title="Vitamin C Serum", brand="Demo", product_type="serum"),
        sku=SkuNode(sku_key="sku::1", source_variant_id="var_1", sku="SKU-1", title="Vitamin C Serum 30ml"),
        offers=[
            OfferNode(
                offer_id="offer::1",
                catalog_track="internal_merchant",
                truth_tier="primary",
                readiness_tier="knowledge_ready",
                offer_mode="merchant_checkout",
                source_system="shopify_products_sync",
                availability="in_stock",
                inventory_quantity=10,
                pricing=PivotPricing(
                    currency="USD",
                    list_price=Decimal("32.00"),
                    merchant_effective_price=Decimal("28.00"),
                    estimated_best_price=Decimal("26.60"),
                    price_confidence=Decimal("1.0"),
                ),
                incentives=[],
            )
        ],
        catalog_track="internal_merchant",
        truth_tier="primary",
        readiness_tier="knowledge_ready",
        freshness={"updated_at": "2026-03-28T00:00:00Z"},
        source_system="shopify_products_sync",
        match_explanation={"lane": "catalog_discovery", "exact_match": False},
        verticals={"beauty": {"ingredients": ["vitamin_c"]}},
    )


def test_pivot_query_route(monkeypatch) -> None:
    app = _build_app()

    async def fake_search(_req):
        return PivotQueryResponse(query="vitamin c", total=1, items=[_sample_result()])

    monkeypatch.setattr(module, "search_pivot_catalog", fake_search)

    client = TestClient(app)
    response = client.post(
        "/v1/pivot/query",
        json={"query": "vitamin c", "merchant_id": "merch_1", "limit": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["product"]["title"] == "Vitamin C Serum"
    assert payload["items"][0]["offers"][0]["pricing"]["estimated_best_price"] == "26.60"


def test_pivot_resolve_offers_route(monkeypatch) -> None:
    app = _build_app()

    async def fake_resolve(_req):
        sample = _sample_result()
        return PivotOffersResolveResponse(
            merchant_id="merch_1",
            product_key=sample.product.product_key,
            sku_key=sample.sku.sku_key,
            offers=sample.offers,
            offers_count=len(sample.offers),
        )

    monkeypatch.setattr(module, "resolve_pivot_offers", fake_resolve)

    client = TestClient(app)
    response = client.post(
        "/v1/pivot/offers/resolve",
        json={"merchant_id": "merch_1", "query": "vitamin c"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["product_key"] == "prod::1"
    assert payload["sku_key"] == "sku::1"
    assert payload["offers_count"] == 1
    assert payload["offers"][0]["offer_id"] == "offer::1"
