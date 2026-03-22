import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_offers_resolve_prefers_external_outbound(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "eps_1",
                    "external_product_id": "ext_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://fentybeauty.com/products/gloss-bomb",
                    "canonical_url": "https://fentybeauty.com/products/gloss-bomb",
                    "domain": "fentybeauty.com",
                    "title": "Gloss Bomb",
                    "price_amount": 19.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "seed_data": {
                        "brand": "Fenty Beauty",
                        "variants": [
                            {
                                "variant_id": "SKU_FENTY_001",
                                "title": "Gloss Bomb 9ml",
                                "price_amount": 19.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                }
            ]
        if "FROM products_cache" in q:
            # Internal exists but should not be used when external has high confidence.
            return [
                {
                    "merchant_id": "merch_1",
                    "product_data": {
                        "id": "prod_internal_1",
                        "title": "Internal Product",
                        "currency": "USD",
                        "price": 20.0,
                        "inventory_quantity": 10,
                        "variants": [{"id": "SKU_FENTY_001", "price": 20.0, "inventory_quantity": 10}],
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=test"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU_FENTY_001"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    offers = body.get("offers") or []
    assert offers, "should return at least one offer"
    assert any(offer.get("purchase_route") == "affiliate_outbound" for offer in offers)
    external_offer = next(offer for offer in offers if offer.get("purchase_route") == "affiliate_outbound")
    assert external_offer["affiliate_url"].startswith("https://example.com/r?token=")
    assert external_offer["seller"].lower().find("fenty") >= 0


def test_offers_resolve_falls_back_to_internal_checkout(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_2",
                    "product_data": {
                        "id": "prod_internal_2",
                        "title": "Internal Product 2",
                        "currency": "USD",
                        "price": 55.0,
                        "inventory_quantity": 3,
                        "variants": [{"id": "SKU_INT_002", "price": 55.0, "inventory_quantity": 3}],
                        "merchant_name": "Internal Store",
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_internal_2"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    offers = body.get("offers") or []
    assert offers, "should return at least one offer"
    assert offers[0]["purchase_route"] == "internal_checkout"
    assert offers[0]["affiliate_url"] is None
    assert isinstance(offers[0]["internal_checkout_items"], list)


def test_offers_resolve_recovers_attached_seed_after_broad_seed_timeout(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q:
            return [
                {
                    "id": "eps_attached_1",
                    "external_product_id": "ext_attached_1",
                    "market": "EU-DE",
                    "tool": "*",
                    "destination_url": "https://merchant.example/products/prod-internal-1",
                    "canonical_url": "https://merchant.example/products/prod-internal-1",
                    "domain": "merchant.example",
                    "title": "Attached Offer",
                    "price_amount": 29.0,
                    "price_currency": "EUR",
                    "availability": "in_stock",
                    "utm_template": None,
                    "attached_product_key": "merch_2|shopify|prod_internal_1",
                    "attached_variant_id": "SKU_INT_002",
                    "seed_data": {
                        "brand": "Merchant Example",
                        "variants": [
                            {
                                "variant_id": "SKU_INT_002",
                                "title": "Attached Variant",
                                "price_amount": 29.0,
                                "price_currency": "EUR",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                }
            ]
        if "FROM external_product_seeds" in q:
            raise asyncio.TimeoutError()
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_2",
                    "platform": "shopify",
                    "platform_product_id": "prod_internal_1",
                    "product_data": {
                        "id": "prod_internal_1",
                        "title": "Internal Product 1",
                        "currency": "EUR",
                        "price": 29.0,
                        "inventory_quantity": 3,
                        "variants": [{"id": "SKU_INT_002", "price": 29.0, "inventory_quantity": 3}],
                        "merchant_name": "Internal Store",
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=attached"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_internal_1"}, "limit": 10, "market": "EU-DE", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert len(offers) >= 2
    assert offers[0]["purchase_route"] == "internal_checkout"
    assert any(offer.get("purchase_route") == "affiliate_outbound" for offer in offers)
    metadata = body.get("metadata") or {}
    assert metadata.get("has_external") is True
    assert any(
        str(source.get("source")) in {"external_product_seeds_attached_retry", "external_product_seeds"}
        and str(source.get("status")) == "ok"
        for source in (metadata.get("sources") or [])
    )


def test_offers_resolve_prefetches_attached_seed_before_broad_seed_query(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q:
            return [
                {
                    "id": "eps_prefetch_1",
                    "external_product_id": "ext_prefetch_1",
                    "market": "EU-DE",
                    "tool": "*",
                    "destination_url": "https://merchant.example/products/prod-internal-prefetch",
                    "canonical_url": "https://merchant.example/products/prod-internal-prefetch",
                    "domain": "merchant.example",
                    "title": "Prefetch Offer",
                    "price_amount": 31.0,
                    "price_currency": "EUR",
                    "availability": "in_stock",
                    "utm_template": None,
                    "attached_product_key": "merch_9|shopify|prod_internal_prefetch",
                    "attached_variant_id": "SKU_PREFETCH_1",
                    "seed_data": {
                        "brand": "Merchant Example",
                        "variants": [
                            {
                                "variant_id": "SKU_PREFETCH_1",
                                "title": "Attached Variant",
                                "price_amount": 31.0,
                                "price_currency": "EUR",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                }
            ]
        if "FROM external_product_seeds" in q:
            raise AssertionError("broad external seed query should be skipped when attached prefetch succeeds")
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_9",
                    "platform": "shopify",
                    "platform_product_id": "prod_internal_prefetch",
                    "product_data": {
                        "id": "prod_internal_prefetch",
                        "title": "Internal Product Prefetch",
                        "currency": "EUR",
                        "price": 31.0,
                        "inventory_quantity": 5,
                        "variants": [{"id": "SKU_PREFETCH_1", "price": 31.0, "inventory_quantity": 5}],
                        "merchant_name": "Internal Store",
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=prefetch"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_internal_prefetch"}, "limit": 10, "market": "EU-DE", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert len(offers) >= 2
    assert any(offer.get("purchase_route") == "affiliate_outbound" for offer in offers)
    metadata = body.get("metadata") or {}
    assert metadata.get("failure_breakdown") in ({}, None)
    assert any(
        str(source.get("source")) == "external_product_seeds"
        and str(source.get("status")) == "ok"
        and str(source.get("query")) == "external_seed_by_canonical_attached_prefetch"
        for source in (metadata.get("sources") or [])
    )
def test_offers_resolve_strict_surface_substitutes_same_product_variant(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            raise AssertionError("strict serving should not query external seeds")
        if "FROM product_group_members" in q:
            return []
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_strict_1",
                    "platform": "shopify",
                    "platform_product_id": "prod_strict_1",
                    "product_data": {
                        "id": "prod_strict_1",
                        "title": "Strict Product",
                        "currency": "USD",
                        "price": 29.0,
                        "inventory_quantity": 0,
                        "orderable": True,
                        "variants": [
                            {"id": "sku_blocked", "sku": "sku_blocked", "price": 29.0, "inventory_quantity": 0},
                            {"id": "sku_ok", "sku": "sku_ok", "price": 31.0, "inventory_quantity": 8},
                        ],
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"sku_id": "sku_blocked"},
                "limit": 10,
                "commerce_surface": "agent_api",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["resolution_mode"] == "same_product_substitution"
    assert body["requested_target"]["sku_id"] == "sku_blocked"
    assert body["resolved_target"]["variant_id"] == "sku_ok"
    assert "requested_variant_not_servable" in (body.get("substitution_reason_codes") or [])
    offers = body.get("offers") or []
    assert len(offers) == 1
    assert offers[0]["purchase_route"] == "internal_checkout"
    assert offers[0]["source"]["variant_id"] == "sku_ok"
    metadata = body.get("metadata") or {}
    assert metadata.get("has_external") is False
    assert metadata.get("commerce_surface") == "agent_api"


def test_offers_resolve_strict_surface_fails_closed_without_eligible_variant(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            raise AssertionError("strict serving should not query external seeds")
        if "FROM product_group_members" in q:
            return []
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_strict_2",
                    "platform": "shopify",
                    "platform_product_id": "prod_strict_2",
                    "product_data": {
                        "id": "prod_strict_2",
                        "title": "Strict Product 2",
                        "currency": "USD",
                        "price": 20.0,
                        "inventory_quantity": 0,
                        "orderable": True,
                        "variants": [
                            {"id": "sku_none", "sku": "sku_none", "price": 20.0, "inventory_quantity": 0},
                        ],
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"sku_id": "sku_none"},
                "limit": 10,
                "commerceSurface": "agent_api",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["resolution_mode"] == "not_servable"
    assert body["resolved_target"] is None
    assert body.get("offers") == []
    metadata = body.get("metadata") or {}
    assert metadata.get("reason_code") == "NOT_SERVABLE"
    assert metadata.get("reason") == "not_servable"
    assert "out_of_stock" in (metadata.get("servable_reason_codes") or [])
