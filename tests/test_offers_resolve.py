import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


from main import app

# A SEED ONLY SERVES IF ITS DESTINATION HAS ACTUALLY BEEN VERIFIED.
#
# `should_block_external_referral_runtime` used to infer freshness from `updated_at` — a column
# any writer bumps — and its staleness check was guarded on "if we have a timestamp at all", so
# a row nobody had ever fetched passed. `destination_checked_at` is written only by a fetch that
# reached the origin (services/external_seed_destination_liveness), and NULL now blocks.
#
# Spread into every seed fixture below so these tests exercise the OFFER lane rather than the
# readiness gate — which has its own suite in tests/test_external_referral_readiness.py.
_VERIFIED_DESTINATION = {
    "destination_checked_at": datetime.now(timezone.utc).isoformat(),
    "destination_http_status": 200,
    "destination_verdict": "live",
    "destination_failure_streak": 0,
}

# ...AND ITS CONTENT HAS ACTUALLY BEEN READ. A SECOND, INDEPENDENT FACT.
#
# The sweep proves the LINK resolves without ever reading a price; a content refresh reads the
# price without proving the link still resolves. `stale_snapshot` asks the first question and
# `destination_stale` the second, and a seed needs both before it may serve. Collapsing them
# onto one column is what would have let ~11.3k rows with a 99-day-old price start serving the
# day the first destination sweep completed.
_VERIFIED_CONTENT = {"extracted_at": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# Parametrized over the surface because the two cases used to behave differently: an explicit
# commerce_surface flipped strict mode, and strict mode silently disabled the external lane
# entirely. External offers are first-class on every surface now (founder directive 2026-08-26) —
# the agent/MCP door pins commerce_surface=agent_api on every get_offers call, so the explicit
# row is exactly the door that served zero offers in prod on a 100%-external corpus.
@pytest.mark.parametrize("commerce_surface", [None, "agent_api"])
def test_offers_resolve_prefers_external_outbound(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, commerce_surface
) -> None:
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
                        "snapshot": dict(_VERIFIED_CONTENT),
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
                    **_VERIFIED_DESTINATION,
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
            "payload": {
                "product": {"sku_id": "SKU_FENTY_001"},
                "limit": 10,
                "market": "US",
                "tool": "*",
                **({"commerce_surface": commerce_surface} if commerce_surface else {}),
            },
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
                        "snapshot": dict(_VERIFIED_CONTENT),
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
                    **_VERIFIED_DESTINATION,
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
                        "snapshot": dict(_VERIFIED_CONTENT),
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
                    **_VERIFIED_DESTINATION,
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


# The agent_api row pins the identity-retry lane specifically under an explicit surface: the
# retry used to be conjunct-gated on `allow_external_fallback`, so a strict caller lost it even
# after the primary external lane was made unconditional.
@pytest.mark.parametrize("commerce_surface", [None, "agent_api"])
def test_offers_resolve_recovers_external_seed_by_internal_identity_after_store_rebind(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, commerce_surface
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "LOWER(COALESCE(title,'')) LIKE" in q:
            assert values["identity_title_0"] == "%kravebeauty great barrier relief%"
            assert values["identity_title_1"] == "%great barrier relief%"
            return [
                {
                    "id": "eps_krave_gbr",
                    "external_product_id": "ext_krave_gbr",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://kravebeauty.com/products/great-barrier-relief",
                    "canonical_url": "https://kravebeauty.com/products/great-barrier-relief",
                    "domain": "kravebeauty.com",
                    "title": "Great Barrier Relief",
                    "price_amount": 32.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "attached_product_key": "old_merch|shopify|old_product",
                    "attached_variant_id": "old_variant",
                    "seed_data": {
                        "snapshot": dict(_VERIFIED_CONTENT),
                        "brand": "KraveBeauty",
                        "variants": [
                            {
                                "variant_id": "external_standard",
                                "title": "Standard - 45 mL",
                                "price_amount": 32.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        if "FROM external_product_seeds" in q:
            raise asyncio.TimeoutError()
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_new",
                    "platform": "shopify",
                    "platform_product_id": "prod_new_gbr",
                    "product_data": {
                        "id": "prod_new_gbr",
                        "title": "KraveBeauty Great Barrier Relief",
                        "brand": "KraveBeauty",
                        "currency": "USD",
                        "price": 28.0,
                        "inventory_quantity": 10,
                        "variants": [{"id": "variant_new_standard", "price": 28.0, "inventory_quantity": 10}],
                        "merchant_name": "KraveBeauty",
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=identity"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"product_id": "prod_new_gbr"},
                "limit": 10,
                "market": "US",
                "tool": "*",
                **({"commerce_surface": commerce_surface} if commerce_surface else {}),
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert len(offers) >= 2
    assert offers[0]["purchase_route"] == "internal_checkout"
    external = next(offer for offer in offers if offer.get("purchase_route") == "affiliate_outbound")
    assert external["source"]["external_product_id"] == "ext_krave_gbr"
    metadata = body.get("metadata") or {}
    assert metadata.get("has_external") is True
    assert any(
        str(source.get("source")) == "external_product_seeds_identity_retry"
        and str(source.get("status")) == "ok"
        and str(source.get("query")) == "external_seed_by_internal_identity"
        for source in (metadata.get("sources") or [])
    )
    # Mixed internal+external must never read "external_only": the internal offer
    # resolved (no variant was requested, so both surfaces report exact_match).
    assert body["resolution_mode"] == "exact_match"


@pytest.mark.parametrize("commerce_surface", [None, "agent_api"])
def test_offers_resolve_external_only_product_serves_on_explicit_surface(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, commerce_surface
) -> None:
    """The prod symptom this contract exists for (2026-08-26): a product with NO internal
    offer at all — the whole external_seed corpus — asked for on the agent/MCP door, which
    pins commerce_surface=agent_api. With no internal match there is no canonical product and
    no internal identity, so neither retry lane can recover: the PRIMARY external lane must
    itself run under an explicit surface, or the caller gets zero offers for every seed row.

    Follow-up (same probe): serving the offer is not enough — the metadata has to say so.
    Only the internal lane wrote resolution_mode, so this exact response used to carry
    resolution_mode="not_servable" (strict) / "exact_match" (relaxed) next to offers_count=1.
    External-only now stamps "external_only" on every surface; resolved_target stays None
    because nothing internal resolved."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "eps_only_1",
                    "external_product_id": "ext_only_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://seedbrand.example/products/serum",
                    "canonical_url": "https://seedbrand.example/products/serum",
                    "domain": "seedbrand.example",
                    "title": "Seed Brand Serum",
                    "price_amount": 18.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "seed_data": {
                        "snapshot": dict(_VERIFIED_CONTENT),
                        "brand": "Seed Brand",
                        "variants": [
                            {
                                "variant_id": "SKU_SEED_1",
                                "title": "Seed Brand Serum 30ml",
                                "price_amount": 18.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=seedonly"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"sku_id": "SKU_SEED_1"},
                "limit": 10,
                "market": "US",
                "tool": "*",
                **({"commerce_surface": commerce_surface} if commerce_surface else {}),
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    offers = body.get("offers") or []
    assert offers, "an external-only product must still yield its outbound offer on the agent surface"
    assert offers[0]["purchase_route"] == "affiliate_outbound"
    metadata = body.get("metadata") or {}
    if commerce_surface:
        assert metadata.get("commerce_surface") == commerce_surface
    assert metadata.get("has_external") is True
    assert metadata.get("has_internal") is False
    assert metadata.get("reason_code") == "OK"
    assert body["resolution_mode"] == "external_only"
    assert body["resolved_target"] is None
    assert (body.get("mapping") or {}).get("resolution_mode") == "external_only"
    assert metadata.get("resolution_mode") == "external_only"


def test_offers_resolve_attached_retry_serves_external_on_explicit_surface(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The attached-retry lane (canonical product from the internal offer → attached seeds)
    must run under an explicit commerce_surface. It carried its own `allow_external_fallback`
    conjunct, so reverting only that conjunct would pass the primary-lane tests while a strict
    caller still lost every external offer whose seed is findable only via the internal match."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q:
            return [
                {
                    "id": "eps_arm_1",
                    "external_product_id": "ext_arm_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://armbrand.example/products/prod-arm-1",
                    "canonical_url": "https://armbrand.example/products/prod-arm-1",
                    "domain": "armbrand.example",
                    "title": "Arm Brand Serum",
                    "price_amount": 24.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "attached_product_key": "merch_arm|shopify|prod_arm_1",
                    "attached_variant_id": "SKU_ARM_1",
                    "seed_data": {
                        "snapshot": dict(_VERIFIED_CONTENT),
                        "brand": "Arm Brand",
                        "variants": [
                            {
                                "variant_id": "SKU_ARM_1",
                                "title": "Arm Brand Serum 30ml",
                                "price_amount": 24.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        if "FROM external_product_seeds" in q:
            # The broad fuzzy lane misses: this seed is only reachable via the attached ref.
            return []
        if "FROM product_group_members" in q:
            return []
        if "FROM products_cache" in q and "LIMIT 20" in q:
            # The canonical-context prefetch misses too, so lane 1 has no attached ref to try —
            # only the post-internal attached retry can surface the seed.
            return []
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_arm",
                    "platform": "shopify",
                    "platform_product_id": "prod_arm_1",
                    "product_data": {
                        "id": "prod_arm_1",
                        "title": "Arm Brand Serum",
                        "brand": "Arm Brand",
                        "currency": "USD",
                        "price": 26.0,
                        "inventory_quantity": 5,
                        "variants": [{"id": "SKU_ARM_1", "price": 26.0, "inventory_quantity": 5}],
                        "merchant_name": "Arm Brand",
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=arm"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"product_id": "prod_arm_1"},
                "limit": 10,
                "market": "US",
                "tool": "*",
                "commerce_surface": "agent_api",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    offers = body.get("offers") or []
    assert any(offer.get("purchase_route") == "affiliate_outbound" for offer in offers)
    assert any(offer.get("purchase_route") == "internal_checkout" for offer in offers)
    metadata = body.get("metadata") or {}
    assert metadata.get("commerce_surface") == "agent_api"
    assert metadata.get("has_external") is True
    assert any(
        str(source.get("source")) == "external_product_seeds_attached_retry"
        and str(source.get("status")) == "ok"
        and str(source.get("query")) == "external_seed_by_canonical_attached_ref"
        for source in (metadata.get("sources") or [])
    )


def test_offers_resolve_strict_surface_substitutes_same_product_variant(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            # External offers are a first-class source on every surface (founder directive
            # 2026-08-26): strict mode tightens internal servability, never sourcing. No
            # seeds exist for this product, so the lane comes back empty — but it must ask.
            return []
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
            # Queried on every surface; empty here so the strict fail-closed path is what
            # this test still exercises.
            return []
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


# ---------------------------------------------------------------------------
# T2-4 — merit-first ranking neutrality (decision #3) + honest buy-here/referral labels
# ---------------------------------------------------------------------------


def _internal_offer(offer_id: str, confidence: float) -> dict:
    return {
        "offer_id": offer_id,
        "confidence": confidence,
        "purchase_route": "internal_checkout",
        "affiliate_url": None,
        "internal_checkout_items": [{"merchant_id": "m", "product_id": "p", "quantity": 1}],
        "source": {"type": "internal_product"},
    }


def _external_offer(offer_id: str, confidence: float) -> dict:
    return {
        "offer_id": offer_id,
        "confidence": confidence,
        "purchase_route": "affiliate_outbound",
        "affiliate_url": "https://example.com/r?token=x",
        "internal_checkout_items": None,
        "source": {"type": "external_seed"},
    }


def test_rank_offers_merit_first_higher_merit_external_beats_lower_merit_internal() -> None:
    """Neutrality invariant: a higher-merit external (referred) offer ranks ABOVE a
    lower-merit internal (buy-here) offer — no blanket down-rank by integration status."""
    import routes.agent_shop_gateway as gateway

    internal = _internal_offer("of:internal", 0.8)
    external = _external_offer("of:external", 1.0)

    ranked = gateway._rank_offers_merit_first([internal, external])

    assert [o["offer_id"] for o in ranked] == ["of:external", "of:internal"]
    assert ranked[0]["purchase_route"] == "affiliate_outbound"
    assert ranked[0]["confidence"] > ranked[1]["confidence"]


def test_rank_offers_merit_first_transactability_breaks_ties() -> None:
    """Tiebreaker ONLY: when fit is equal, the transactable (buy-here) offer wins."""
    import routes.agent_shop_gateway as gateway

    external = _external_offer("of:external", 0.9)
    internal = _internal_offer("of:internal", 0.9)

    # External is listed first in the input; the tiebreaker must still promote internal.
    ranked = gateway._rank_offers_merit_first([external, internal])

    assert [o["offer_id"] for o in ranked] == ["of:internal", "of:external"]
    assert ranked[0]["purchase_route"] == "internal_checkout"


def test_rank_offers_merit_first_exact_tiers_collapse_across_scales() -> None:
    """Scale-artifact guard: internal exact (0.95) and external exact (1.0) are the SAME fit
    tier, so the transactability tiebreaker fires and buy-here (internal) ranks first — the
    0.05 cross-scale gap must NOT invert the demotion."""
    import routes.agent_shop_gateway as gateway

    external_exact = _external_offer("of:external_exact", 1.0)
    internal_exact = _internal_offer("of:internal_exact", 0.95)

    ranked = gateway._rank_offers_merit_first([external_exact, internal_exact])

    assert [o["offer_id"] for o in ranked] == ["of:internal_exact", "of:external_exact"]
    assert ranked[0]["purchase_route"] == "internal_checkout"


def test_rank_offers_merit_first_exact_external_beats_lower_tier_internal() -> None:
    """Merit still wins ACROSS tiers: an exact external (1.0) outranks a product-tier (0.8)
    or loose (0.7) internal offer — genuinely higher fit, not a scale artifact."""
    import routes.agent_shop_gateway as gateway

    external_exact = _external_offer("of:external_exact", 1.0)
    internal_product = _internal_offer("of:internal_product", 0.8)
    internal_loose = _internal_offer("of:internal_loose", 0.7)

    ranked = gateway._rank_offers_merit_first([internal_product, internal_loose, external_exact])

    assert ranked[0]["offer_id"] == "of:external_exact"


def test_rank_offers_merit_first_pure_internal_order_unchanged() -> None:
    """Regression: a pure-internal set is ordered identically to the old
    ``internal_offers + external_offers`` construction (stable sort, no internal churn)."""
    import routes.agent_shop_gateway as gateway

    # Same-product internal offers share a confidence in real runs -> stable => input order.
    internal_offers = [
        _internal_offer("of:internal_a", 0.95),
        _internal_offer("of:internal_b", 0.95),
        _internal_offer("of:internal_c", 0.95),
    ]

    ranked = gateway._rank_offers_merit_first(list(internal_offers))

    assert [o["offer_id"] for o in ranked] == [o["offer_id"] for o in internal_offers]


def test_offers_resolve_ranks_higher_merit_external_above_lower_merit_internal(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """End-to-end: an exact-match external referral (confidence 1.0) outranks a loose-match
    internal offer (confidence 0.8) in the resolved output, and every offer carries an
    unambiguous buy-here vs referral label."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "eps_merit_1",
                    "external_product_id": "ext_merit_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://brand.example/products/serum",
                    "canonical_url": "https://brand.example/products/serum",
                    "domain": "brand.example",
                    "title": "Brand Serum",
                    "price_amount": 25.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "seed_data": {
                        "snapshot": dict(_VERIFIED_CONTENT),
                        "brand": "Brand Example",
                        "variants": [
                            {
                                "variant_id": "SKU_MERIT_WIN",
                                "title": "Brand Serum 30ml",
                                "price_amount": 25.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        if "FROM products_cache" in q:
            # Internal product exists but its variant does NOT match the queried sku -> a
            # lower-merit (0.8) loose match, versus the external exact match (1.0).
            return [
                {
                    "merchant_id": "merch_loose",
                    "product_data": {
                        "id": "prod_loose_1",
                        "title": "Loose Internal Product",
                        "currency": "USD",
                        "price": 30.0,
                        "inventory_quantity": 5,
                        "merchant_name": "Loose Internal Store",
                        "variants": [{"id": "SKU_INTERNAL_OTHER", "price": 30.0, "inventory_quantity": 5}],
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=merit")
    )

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU_MERIT_WIN"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    offers = body.get("offers") or []
    assert len(offers) >= 2, "both the external and the internal offer should be present"

    # Neutrality invariant: the higher-merit external referral ranks first.
    assert offers[0]["purchase_route"] == "affiliate_outbound"
    assert offers[0]["source"]["type"] == "external_seed"
    internal = next(o for o in offers if o["purchase_route"] == "internal_checkout")
    assert offers[0]["confidence"] > internal["confidence"]
    assert offers.index(offers[0]) < offers.index(internal)

    # Honest, unambiguous labels: referral vs buy-here on every offer.
    assert offers[0]["affiliate_url"].startswith("https://example.com/r?token=")
    assert offers[0]["internal_checkout_items"] is None
    assert internal["affiliate_url"] is None
    assert isinstance(internal["internal_checkout_items"], list)


def test_offers_resolve_exact_internal_beats_exact_external_end_to_end(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """End-to-end tiebreaker (now REACHABLE): when the SAME sku matches both an internal
    (buy-here, confidence 0.95) and an external (referral, confidence 1.0) offer, they are the
    same fit tier -> transactability breaks the tie -> the internal buy-here offer ranks first.
    The 0.95-vs-1.0 cross-scale gap must not invert the demotion."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return [
                {
                    "id": "eps_tie_1",
                    "external_product_id": "ext_tie_1",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://brand.example/products/serum",
                    "canonical_url": "https://brand.example/products/serum",
                    "domain": "brand.example",
                    "title": "Brand Serum (referral)",
                    "price_amount": 25.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "seed_data": {
                        "snapshot": dict(_VERIFIED_CONTENT),
                        "brand": "Brand Example",
                        "variants": [
                            {
                                "variant_id": "SKU_TIE_EXACT",
                                "title": "Brand Serum 30ml",
                                "price_amount": 25.0,
                                "price_currency": "USD",
                                "availability": "in_stock",
                            }
                        ],
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        if "FROM products_cache" in q:
            # Internal product's variant matches the SAME queried sku -> exact (0.95).
            return [
                {
                    "merchant_id": "merch_tie",
                    "product_data": {
                        "id": "prod_tie_1",
                        "title": "Internal Serum (buy-here)",
                        "currency": "USD",
                        "price": 24.0,
                        "inventory_quantity": 5,
                        "merchant_name": "Buy-Here Store",
                        "variants": [{"id": "SKU_TIE_EXACT", "price": 24.0, "inventory_quantity": 5}],
                    },
                }
            ]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=tie")
    )

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU_TIE_EXACT"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert len(offers) >= 2, "both the exact internal and exact external offer should be present"

    # Same fit tier (exact) -> transactability tiebreaker -> buy-here (internal) first.
    assert offers[0]["purchase_route"] == "internal_checkout"
    assert offers[0]["source"]["type"] == "internal_product"
    external = next(o for o in offers if o["purchase_route"] == "affiliate_outbound")
    assert offers.index(offers[0]) < offers.index(external)
    # Despite the external carrying a numerically higher raw confidence.
    assert external["confidence"] > offers[0]["confidence"]

    # Honest labels remain on both.
    assert offers[0]["affiliate_url"] is None
    assert isinstance(offers[0]["internal_checkout_items"], list)
    assert external["affiliate_url"].startswith("https://example.com/r?token=")
    assert external["internal_checkout_items"] is None


def test_offers_resolve_pure_internal_order_unchanged_end_to_end(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Regression: with no external seeds, the resolved output is internal-only and preserves
    the source row order (no internal-only churn from the merit-first sort)."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_a",
                    "product_data": {
                        "id": "prod_pure_a",
                        "title": "Pure Internal A",
                        "currency": "USD",
                        "price": 10.0,
                        "inventory_quantity": 4,
                        "variants": [{"id": "SKU_PURE_A", "price": 10.0, "inventory_quantity": 4}],
                    },
                },
                {
                    "merchant_id": "merch_b",
                    "product_data": {
                        "id": "prod_pure_b",
                        "title": "Pure Internal B",
                        "currency": "USD",
                        "price": 12.0,
                        "inventory_quantity": 4,
                        "variants": [{"id": "SKU_PURE_B", "price": 12.0, "inventory_quantity": 4}],
                    },
                },
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_pure_a"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert offers, "should return at least one internal offer"
    assert all(o["purchase_route"] == "internal_checkout" for o in offers)
    # Source row order (merch_a before merch_b) is preserved.
    sellers_or_ids = [str((o.get("source") or {}).get("product_id") or "") for o in offers]
    assert sellers_or_ids[0] == "prod_pure_a"


# ---------------------------------------------------------------------------
# T1 — attached-seed mainline matches the STORAGE format (prod::…), never pipe.
# Regression for the confirmed dead path in _fetch_attached_seed_rows
# (docs/IDENTITY_REFERENCE.md "Trap T1"; ADR-009 §Prerequisite fix). The existing
# offers.resolve tests mock fetch_all by query STRING and return canned rows
# regardless of the WHERE params, so they never exercised the match-key format —
# which is exactly how the pipe-vs-prod:: bug survived. These tests SIMULATE the SQL
# match so the key construction is actually load-bearing.
# ---------------------------------------------------------------------------


def _like_to_regex(pattern: str) -> "re.Pattern[str]":
    """Translate a SQL ``LIKE ... ESCAPE '\\'`` pattern to an anchored regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _attached_query_matches(stored: dict, params: dict) -> bool:
    """Replay the _fetch_attached_seed_rows WHERE clause against one stored seed:
    ``attached_product_key LIKE :attached_prefix AND (pid clauses OR variant clauses)``.

    pid params (``attached_pid_*``) match ``attached_product_key`` as LIKE/equality;
    variant params (``attached_sku_*``) match ``attached_variant_id`` by equality.
    """
    apk = str(stored.get("attached_product_key") or "")
    avid = str(stored.get("attached_variant_id") or "")
    prefix = params.get("attached_prefix")
    if not prefix or not _like_to_regex(prefix).match(apk):
        return False
    for key, value in params.items():
        if key.startswith("attached_pid_") and _like_to_regex(str(value)).match(apk):
            return True
        if key.startswith("attached_sku_") and str(value) == avid:
            return True
    return False


_STORAGE_SEED = {
    "id": "eps_storage_1",
    "external_product_id": "ext_storage_1",
    "market": "US",
    "tool": "*",
    "destination_url": "https://brand.example/products/thing",
    "canonical_url": "https://brand.example/products/thing",
    "domain": "brand.example",
    "title": "Storage Format Offer",
    "price_amount": 42.0,
    "price_currency": "USD",
    "availability": "in_stock",
    "utm_template": None,
    # REAL prod:: storage-format key (make_catalog_product_key(merch_x, shopify, 123)).
    "attached_product_key": "prod::merch_x::shopify::123",
    "attached_variant_id": "var_9",
    "seed_data": {
        "snapshot": dict(_VERIFIED_CONTENT),
        "brand": "Brand Example",
        "variants": [
            {
                "variant_id": "var_9",
                "title": "Variant Nine",
                "price_amount": 42.0,
                "price_currency": "USD",
                "availability": "in_stock",
            }
        ],
    },
    "status": "active",
                    **_VERIFIED_DESTINATION,
}


def test_fetch_attached_seed_matches_storage_format_key(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """POSITIVE: a seed whose attached_product_key is the REAL prod:: storage form is
    matched by _fetch_attached_seed_rows when the caller passes the raw platform ids
    (product '123', variant 'var_9'). If the handler built pipe-format keys (the bug),
    the simulated SQL match would find nothing and no external offer would surface."""
    import routes.agent_shop_gateway as gateway

    captured: dict = {}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q and "attached_prefix" in q:
            captured["attached_params"] = dict(values or {})
            return [dict(_STORAGE_SEED)] if _attached_query_matches(_STORAGE_SEED, values or {}) else []
        if "FROM external_product_seeds" in q:
            return []  # fuzzy must not be needed
        if "FROM products_cache" in q:
            return []  # external-only; no internal offer
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=storage"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"product_id": "123", "sku_id": "var_9", "merchant_id": "merch_x"},
                "limit": 10,
                "market": "US",
                "tool": "*",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    external = [o for o in offers if o.get("purchase_route") == "affiliate_outbound"]
    assert external, "the storage-format attached seed must surface via the attached-ref mainline"
    assert external[0]["source"]["external_product_id"] == "ext_storage_1"

    metadata = body.get("metadata") or {}
    assert any(
        str(s.get("source")) == "external_product_seeds"
        and str(s.get("query")) == "external_seed_by_attached_ref"
        for s in (metadata.get("sources") or [])
    ), "the match must be labeled as the attached-ref mainline, not fuzzy"

    # Format pin: the match keys the handler built are prod:: storage form, never pipe.
    params = captured.get("attached_params") or {}
    assert params, "the attached-ref query must have run"
    assert str(params["attached_prefix"]).startswith("prod::merch")
    for key, value in params.items():
        if key.startswith("attached_pid_"):
            assert str(value).startswith("prod::"), f"{key}={value!r} must be prod:: storage form"
        if key != "limit":
            assert "|" not in str(value), f"no pipe-format key may be built: {key}={value!r}"


def test_fetch_attached_seed_pipe_format_key_finds_nothing(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """NEGATIVE (pins the storage format): a seed stored in the legacy PIPE form is NOT
    matched by the (now storage-format) match keys — reproducing the exact prod fact
    (8,004 prod:: rows match, 0 pipe rows). It must not surface via the attached-ref path."""
    import routes.agent_shop_gateway as gateway

    pipe_seed = dict(_STORAGE_SEED, id="eps_pipe_1", attached_product_key="merch_x|shopify|123")
    saw_fuzzy = {"ran": False}

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q and "attached_prefix" in q:
            # Simulate matching the storage-format params against a PIPE-stored seed.
            return [dict(pipe_seed)] if _attached_query_matches(pipe_seed, values or {}) else []
        if "FROM external_product_seeds" in q:
            saw_fuzzy["ran"] = True
            return []  # fuzzy also finds nothing in this test
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=pipe"))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {
                "product": {"product_id": "123", "sku_id": "var_9", "merchant_id": "merch_x"},
                "limit": 10,
                "market": "US",
                "tool": "*",
            },
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    offers = body.get("offers") or []
    assert not [o for o in offers if o.get("purchase_route") == "affiliate_outbound"], (
        "a pipe-format attached_product_key must NOT match the storage-format mainline"
    )
    assert saw_fuzzy["ran"], "control: the fuzzy path still ran (the pipe seed simply matched nothing)"


def test_fetch_attached_seed_fuzzy_surfacing_is_observable_mainline_miss(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """OBSERVABILITY: when the fuzzy path surfaces a seed that HAS a storage-format
    attached_product_key for a (merchant, pid) the attached-ref mainline actually
    searched, that is a mainline MISS — it must be logged (attached_seed_mainline_miss)
    and stamped on the source metadata, while the offer is still delivered honestly."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q and "attached_product_key IS NOT NULL" in q and "attached_prefix" in q:
            return []  # simulate a residual attached-ref gap
        if "FROM external_product_seeds" in q and "external_product_id =" in q:
            return [dict(_STORAGE_SEED, id="eps_miss_1")]  # fuzzy surfaces the attached seed
        if "FROM external_product_seeds" in q:
            return []
        if "FROM products_cache" in q:
            return []
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?token=miss"))

    with caplog.at_level(logging.WARNING):
        res = client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "offers.resolve",
                "payload": {
                    "product": {"product_id": "123", "sku_id": "var_9", "merchant_id": "merch_x"},
                    "limit": 10,
                    "market": "US",
                    "tool": "*",
                },
                "metadata": {"source": "creator-agent-ui"},
            },
        )
    assert res.status_code == 200
    body = res.json()

    # Honest delivery: the offer still surfaces.
    offers = body.get("offers") or []
    assert [o for o in offers if o.get("purchase_route") == "affiliate_outbound"], "the offer must still be delivered"

    # Observable: a distinct WARNING fired naming the missed seed.
    miss_records = [r for r in caplog.records if r.message == "attached_seed_mainline_miss"]
    assert miss_records, "the fuzzy surfacing of an attached seed must emit a mainline-miss warning"
    assert "eps_miss_1" in (getattr(miss_records[0], "seed_ids", []) or [])

    # Truthful label + stamped metadata so telemetry can alarm on the fuzzy:attached ratio.
    metadata = body.get("metadata") or {}
    seed_source = next(
        (s for s in (metadata.get("sources") or []) if str(s.get("source")) == "external_product_seeds"),
        None,
    )
    assert seed_source is not None
    assert str(seed_source.get("query")) == "external_seed_by_fuzzy_ref"
    assert "eps_miss_1" in (seed_source.get("mainline_miss_seed_ids") or [])


# ---------------------------------------------------------------------------
# ADR-009 ratified decision 1 (no-fallback) — offers key on product_group_id
# UNCONDITIONALLY. canonical_ref is `pg:…` or ABSENT (honest
# `no_canonical_identity`), never a merchant-scoped `pc:{merchant}:{platform}:
# {pid}` substitute.
# ---------------------------------------------------------------------------


def _iter_strings(obj):
    """Yield every string anywhere inside a JSON-shaped payload."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def test_offers_resolve_grouped_product_keys_on_pg_and_reports_resolved(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A product WITH a product_group_members row resolves with canonical_ref ==
    `pg:<gid>` and mapping.canonical_identity_status == 'resolved'."""
    import routes.agent_shop_gateway as gateway

    group_id = "pg_32de31827aded89c8d0339895b6a2786"

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return []
        if "FROM product_group_members" in q:
            return [
                {
                    "product_group_id": group_id,
                    "merchant_id": "merch_grp",
                    "platform": "shopify",
                    "platform_product_id": "prod_grp_1",
                    "is_primary": True,
                }
            ]
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_grp",
                    "platform": "shopify",
                    "platform_product_id": "prod_grp_1",
                    "product_data": {
                        "id": "prod_grp_1",
                        "title": "Grouped Product",
                        "currency": "USD",
                        "price": 18.0,
                        "inventory_quantity": 7,
                        "variants": [{"id": "SKU_GRP_1", "price": 18.0, "inventory_quantity": 7}],
                        "merchant_name": "Grouped Store",
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_grp_1"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body.get("offers"), "should return the internal offer"

    mapping = body.get("mapping") or {}
    assert mapping.get("canonical_ref") == f"pg:{group_id}"
    assert mapping.get("canonical_product_group_id") == group_id
    assert mapping.get("canonical_identity_status") == "resolved"
    assert body.get("canonical_product_ref") == f"pg:{group_id}"
    canonical_product = mapping.get("canonical_product") or {}
    assert canonical_product.get("product_group_id") == group_id


def test_offers_resolve_ungrouped_product_is_honest_absent_never_pc(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A product with NO group membership yields canonical_ref None and
    canonical_identity_status == 'no_canonical_identity' — and NOTHING in the
    mapping payload carries a merchant-scoped `pc:` ref (the removed fallback
    must not resurface anywhere, including nested candidates/sources)."""
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return []
        if "FROM product_group_members" in q:
            return []  # pg-NULL product: no membership row anywhere
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_nogroup",
                    "platform": "shopify",
                    "platform_product_id": "prod_nogroup_1",
                    "product_data": {
                        "id": "prod_nogroup_1",
                        "title": "Ungrouped Product",
                        "currency": "USD",
                        "price": 22.0,
                        "inventory_quantity": 4,
                        "variants": [{"id": "SKU_NOGRP_1", "price": 22.0, "inventory_quantity": 4}],
                        "merchant_name": "Ungrouped Store",
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"product_id": "prod_nogroup_1"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body.get("offers"), "the offer itself is still served — only the canonical ref is absent"

    mapping = body.get("mapping") or {}
    assert mapping.get("canonical_ref") is None
    assert mapping.get("canonical_identity_status") == "no_canonical_identity"
    assert "canonical_product_ref" not in body

    # The banned fallback must not resurface ANYWHERE in the mapping payload —
    # not in candidates, canonical_product, targets, or any nested string.
    pc_strings = [s for s in _iter_strings(mapping) if s.startswith("pc:")]
    assert pc_strings == [], f"merchant-scoped pc: refs must never be minted: {pc_strings}"
    # Belt-and-braces: nor anywhere else in the response body.
    pc_strings_body = [s for s in _iter_strings(body) if s.startswith("pc:")]
    assert pc_strings_body == [], f"pc: ref leaked outside mapping: {pc_strings_body}"


def test_the_cart_id_reaching_the_builder_is_the_evidenced_one_not_the_sku(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """THE WIRE BETWEEN IDENTITY AND BUILDER, which nothing covered.

    `_external_seed_redirect_identity` is tested, and `_make_external_redirect_url` is
    tested, but every seed call site reaches the builder through a monkeypatched AsyncMock
    that discards its kwargs — so the argument connecting them was unverified at three of
    four production call sites. Round-6 review demonstrated the consequence: reinstating the
    round-5 P0 verbatim (`cart_variant_id=redirect_identity.get("variant_id")`) left the
    whole suite GREEN.

    This records the real call. The seed below is the dangerous shape: storefront evidence
    present, and an all-digit SKU on the seed variant — the combination that used to build a
    cart for a product Shopify never had under that id.
    """
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                {
                    "id": "eps_wire",
                    "external_product_id": "ext_wire",
                    "market": "US",
                    "tool": "*",
                    "destination_url": "https://brand.com/products/serum",
                    "canonical_url": "https://brand.com/products/serum",
                    "domain": "brand.com",
                    "title": "Serum",
                    "price_amount": 19.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                    "utm_template": None,
                    "seed_data": {
                        "brand": "Brand",
                        "snapshot": {
                            **_VERIFIED_CONTENT,
                            "storefront_platform": "shopify",
                            "storefront_platform_source": "products_js_v1",
                            "variants": [
                                {
                                    # all-digit SKU: passes extract_shopify_numeric_variant_id
                                    # by shape, but is NOT a Shopify-issued variant id
                                    "variant_id": "80072940",
                                    "shopify_variant_id": "41234567890123",
                                    "title": "30ml",
                                    "price_amount": 19.0,
                                    "price_currency": "USD",
                                    "availability": "in_stock",
                                }
                            ],
                        },
                    },
                    "status": "active",
                    **_VERIFIED_DESTINATION,
                }
            ]
        return []

    seen: dict = {}

    async def recording_builder(**kwargs):
        seen.update(kwargs)
        return "https://example.com/r?token=test"

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", recording_builder)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "80072940"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text

    assert seen, "the redirect builder was never reached — this test would prove nothing"
    assert "cart_variant_id" in seen, "the required argument must actually be threaded"
    assert seen["cart_variant_id"] == "41234567890123", (
        "the cart must use the STAMPED id; passing variant_id here is the round-5 P0"
    )
    assert seen.get("variant_id") != seen["cart_variant_id"], (
        "attribution and the cart are different channels — if they are equal here the "
        "fixture no longer exercises the divergence this test exists for"
    )


def _seed_row_for_exec_spec(*, evidence: bool):
    snapshot = {**_VERIFIED_CONTENT,
                "variants": [{"variant_id": "SKU-1", "title": "30ml",
                              "price_amount": 19.0, "price_currency": "USD",
                              "availability": "in_stock"}]}
    if evidence:
        snapshot["storefront_platform"] = "shopify"
        snapshot["storefront_platform_source"] = "products_js_v1"
        snapshot["variants"][0]["shopify_variant_id"] = "41234567890123"
    return {
        "id": "eps_spec", "external_product_id": "ext_spec", "market": "US", "tool": "*",
        "destination_url": "https://brand.com/products/serum",
        "canonical_url": "https://brand.com/products/serum",
        "domain": "brand.com", "title": "Serum", "price_amount": 19.0,
        "price_currency": "USD", "availability": "in_stock", "utm_template": None,
        "seed_data": {"brand": "Brand", "snapshot": snapshot}, "status": "active",
                    **_VERIFIED_DESTINATION,
    }


@pytest.mark.parametrize("evidence,expected", [(True, True), (False, False)])
def test_cart_prefilled_tells_the_agent_what_the_link_actually_does(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, evidence: bool, expected: bool
) -> None:
    """EXECUTION SPEC v0. `affiliate_url` resolves either to a pre-filled cart or a bare PDP,
    and the agent could not tell which — the decision lived inside the redirect builder,
    was stamped into the signed token as `join_mode`, and was never returned.

    The flag must track what the LINK does, so both are asserted from the same request: a
    claim that can drift from the URL it describes is worse than no claim.
    """
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [_seed_row_for_exec_spec(evidence=evidence)]
        return []

    real_builder = gateway._make_external_redirect_url
    built: dict = {}

    async def recording_builder(**kwargs):
        url = await real_builder(**kwargs)
        # decode the signed token to see the destination the buyer would actually reach
        import base64
        import json as _json
        from urllib.parse import parse_qs, urlparse

        tok = parse_qs(urlparse(url).query)["token"][0].split(".")[0]
        payload = _json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))
        built["dest"] = payload["dest"]
        return url

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", recording_builder)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or [])
              if o.get("purchase_route") == "affiliate_outbound"]
    assert offers, "the external offer must be returned for this to prove anything"

    assert offers[0]["cart_prefilled"] is expected
    # and the flag agrees with the link it describes
    assert built, "the real builder was never reached"
    assert ("/cart/" in built["dest"]) is expected, built["dest"]


# ---------------------------------------------------------------------------
# cart_prefilled vs the warm-handoff click lane
#
# `cart_prefilled: false` is a POSITIVE claim relayed to a buyer ("this link lands on a
# product page, you pick the variant yourself"). The warm-handoff lane on `GET /r` can land
# that same buyer in a prefilled cart, and its eligibility fires on exactly the cold
# population — so an unguarded `false` is a statement we already sent and cannot correct.
# The field is a tri-state (PIVOTA-Agent #2082); these prove the `false` leg is only emitted
# where it is PROVABLE, and that `true` is never collateral damage.
# See docs/runbooks/outbound_warm_handoff_rollout.md.
# ---------------------------------------------------------------------------

_WARM_HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@pytest.fixture
def warm_lane(monkeypatch: pytest.MonkeyPatch):
    """The warm canary as deployed (flag on, key set, brand allowlist) — prod posture.

    Also swaps in a throwaway anonymous-invoke rate-limit store. That counter is
    module-level and keyed on the shared "testclient" IP with a 60/min ceiling, so a
    test that spends from it silently starves whatever runs later in the same minute —
    adding these tests tripped a 429 in test_pdp_resolution_stability's 30-call loop.
    Replacing the dict (rather than clearing it) isolates in both directions.
    """
    import routes.agent_shop_gateway as gateway
    from config.settings import settings as app_settings

    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_brands_raw", "brand.com")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_rollout_pct", 0)
    return app_settings


def _resolve_exec_spec_offer(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    *,
    evidence: bool,
    row_overrides: dict = None,
    allowed_domains: list = None,
) -> dict:
    """One external offer for the exec-spec fixture, through the real route."""
    import routes.agent_shop_gateway as gateway

    row = _seed_row_for_exec_spec(evidence=evidence)
    row.update(row_overrides or {})

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [row]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=allowed_domains or ["brand.com"]))
    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or [])
              if o.get("purchase_route") == "affiliate_outbound"]
    assert offers, "the external offer must be returned for this to prove anything"
    return offers[0]


def _token_of(offer: dict) -> str:
    from urllib.parse import parse_qs, urlparse

    token = parse_qs(urlparse(offer["affiliate_url"]).query).get("token", [""])[0]
    assert token, f"no signed token on the affiliate_url: {offer.get('affiliate_url')!r}"
    return token


def test_cart_prefilled_is_null_not_false_when_the_warm_lane_could_contradict_it(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The defect: a cold PDP offer on a warm-eligible brand must NOT claim `false`.

    Without the guard this is `False` — and the buyer lands in a prefilled cart anyway.
    """
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert "cart_prefilled" in offer, "the field must still be present — null is a STATE, not an omission"
    assert offer["cart_prefilled"] is None, (
        "a `false` here is a claim the warm-handoff lane can contradict after the answer "
        "was sent; the honest tri-state answer is null/unknown"
    )


def test_cart_prefilled_true_is_never_downgraded_by_warm_eligibility(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """One-sided exposure: the warm lane only ever BUILDS a cart, so `true` stays `true`.

    Guards the over-correction — blanket-nulling on an eligible host would destroy the
    field's whole purpose for the offers that CAN answer it.
    """
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=True)
    assert offer["cart_prefilled"] is True


def test_cart_prefilled_false_survives_when_the_host_is_not_warm_eligible(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """`false` is still emitted where it is PROVABLE — here, a brand outside the allowlist."""
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "someone-else.com")
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["cart_prefilled"] is False, (
        "an offer the warm lane can never touch must keep its claim — otherwise the guard "
        "is a blanket null and the field says nothing"
    )


def test_cart_prefilled_false_survives_when_the_lane_is_off(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_enabled", False)
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["cart_prefilled"] is False


def test_cart_prefilled_false_survives_when_the_internal_key_is_unset(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The lane fail-closes without a key even with the flag on — so `false` holds."""
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_internal_key", None)
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["cart_prefilled"] is False


@pytest.mark.parametrize("pct,expected", [(0, False), (100, None)])
def test_cart_prefilled_tracks_the_percentage_rollout(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane, pct: int, expected
) -> None:
    """With an empty allowlist the rollout pct decides — and the claim must follow it."""
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_rollout_pct", pct)
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["cart_prefilled"] is expected


def test_cart_prefilled_agrees_with_the_click_lane_on_the_SAME_token(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The INTERPRETATION must hold: `cart_prefilled is None` exactly when the click lane
    would find the link eligible, evaluated on the token the offer actually carries.

    Deliberately NOT the token-identity guarantee — this test cannot give it. A substituted
    token only changes the answer when the two happen to land in different buckets, so
    measured against a fabricated-token mutant this assertion kills it 3-4 times in 12. Token
    identity is pinned deterministically by
    test_the_token_we_evaluate_is_the_token_we_hand_out; this test covers the mapping from
    eligibility to tri-state, which that one does not.
    """
    from services.outbound_warm_handoff import evaluate_warm_eligibility

    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_rollout_pct", 50)
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)

    click_eligible, reason = evaluate_warm_eligibility(
        dest=_dest_of(offer),
        user_agent=_WARM_HUMAN_UA,
        token=_token_of(offer),
        ctx=_ctx_of(offer),
        settings=warm_lane,
    )
    assert reason in {"rollout", "control"}, (
        f"the fixture no longer exercises the token-keyed bucket (reason={reason}) — "
        "this test would prove nothing about token parity"
    )
    assert (offer["cart_prefilled"] is None) is click_eligible, (
        "resolve time and click time disagreed about the SAME token: the claim we send is "
        f"cart_prefilled={offer['cart_prefilled']!r} but the click lane says "
        f"eligible={click_eligible} ({reason})"
    )


def _dest_of(offer: dict) -> str:
    """The destination the buyer actually reaches, decoded from the signed token."""
    return str(_payload_of(offer)["dest"])


def _payload_of(offer: dict) -> dict:
    import base64
    import json as _json

    tok = _token_of(offer).split(".")[0]
    return _json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))


def _ctx_of(offer: dict) -> dict:
    """The signed ctx the CLICK path would read off this exact token.

    Passing the real ctx (not `{}`) is what keeps the parity test below honest: eligibility
    now reads `join_mode` from it, so an empty dict would compare the click lane against
    inputs it never sees.
    """
    ctx = _payload_of(offer).get("ctx")
    return ctx if isinstance(ctx, dict) else {}


def test_a_null_claim_is_warranted_because_the_click_really_does_land_in_a_cart(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """End-to-end, both layers in one test: resolve says "unknown", and the very link it
    handed out then 302s the buyer into a prefilled cart.

    This is the test that would have caught the defect. Asserting the field in isolation
    cannot: `false` looks perfectly reasonable until you follow the link it describes.
    """
    import routes.outbound_links as outbound_routes
    import services.outbound_warm_handoff as warm

    warm.memo_clear()
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["cart_prefilled"] is None

    cart_url = "https://brand.myshopify.com/cart/c/abc123?key=k"

    async def _fake_resolve(**kwargs):
        return {"continue_url": cart_url, "cart_id": "c_1"}

    async def _fake_log(**kwargs):
        return None

    monkeypatch.setattr(outbound_routes, "resolve_warm_handoff", _fake_resolve)
    monkeypatch.setattr(outbound_routes, "log_outbound_click", _fake_log)
    try:
        res = client.get(
            "/r",
            params={"token": _token_of(offer)},
            headers={"user-agent": _WARM_HUMAN_UA},
            follow_redirects=False,
        )
        assert res.status_code == 302, res.text
        assert res.headers["location"] == cart_url, (
            "the buyer landed in a prefilled cart — had the offer claimed "
            "cart_prefilled=false, that answer was already sent and now wrong"
        )
    finally:
        warm.memo_clear()


def test_cart_url_is_load_bearing_for_the_cart_verdict(
    monkeypatch: pytest.MonkeyPatch, warm_lane
) -> None:
    """`cart_url` — the decision compose_attributed_destinations already made — is what
    drives the `True` leg, and it must be READ rather than re-derived.

    Same warm-eligible host in both calls, so only `cart_url` differs: a `True` here proves
    the cart leg short-circuits before the warm guard, and the `None` proves the guard runs
    when there is no cart.
    """
    import routes.agent_shop_gateway as gateway

    kwargs = dict(
        destination_url="https://brand.com/products/serum",
        redirect_url="https://api.example.com/r?token=t",
    )
    assert gateway._cart_prefilled_claim(cart_url="https://brand.com/cart/41234567890123:1",
                                         **kwargs) is True
    assert gateway._cart_prefilled_claim(cart_url=None, **kwargs) is None
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_enabled", False)
    assert gateway._cart_prefilled_claim(cart_url=None, **kwargs) is False


def test_cart_prefilled_says_unknown_when_the_token_cannot_be_recovered(
    monkeypatch: pytest.MonkeyPatch, warm_lane
) -> None:
    """Defensive leg of _cart_prefilled_claim, asserted directly.

    The rollout bucket is keyed on the token, so a link we cannot read a token back out of
    makes `false` unprovable. Unknown beats a guess.

    Pinned on the ROLLOUT branch (empty allowlist), because that is the only configuration
    where the guard changes the answer: `rollout_bucket` refuses every token at pct=0, so
    without the guard a token-less link confidently answers `false`. Asserting this against
    an allowlisted brand instead would pass with the guard deleted — eligibility answers
    `None` there on the host alone, and the test would prove nothing.
    """
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_rollout_pct", 0)
    kwargs = dict(
        cart_url=None,
        destination_url="https://brand.com/products/serum",
        redirect_url="https://api.example.com/r",  # no token param at all
    )
    assert gateway._cart_prefilled_claim(**kwargs) is None, (
        "without a token the pct branch cannot be evaluated, so `false` is not provable"
    )
    # A real token in the same configuration IS provable, and answers `false`.
    from services.outbound_links_service import make_redirect_token

    token = make_redirect_token({"market": "US", "tool": "*",
                                 "dest": "https://brand.com/products/serum", "ctx": {}})
    assert gateway._cart_prefilled_claim(
        **{**kwargs, "redirect_url": f"https://api.example.com/r?token={token}"}
    ) is False, "the guard must be about the MISSING token, not a blanket null"
    # And with the lane off there is nothing to be unknown about.
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_enabled", False)
    assert gateway._cart_prefilled_claim(**kwargs) is False


def test_the_token_we_evaluate_is_the_token_we_hand_out(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The rollout bucket is a stable hash of the token, so the claim is only sound if the
    token weighed at resolve time is the one the click will actually carry.

    Asserted by identity, not by outcome: re-minting or fabricating a token still yields a
    plausible boolean that agrees with the click roughly half the time, so an outcome-only
    test detects it as a COIN FLIP. (Measured: a `token="fabricated"` mutant passed the
    outcome test on the first run.)
    """
    import routes.agent_shop_gateway as gateway

    real = gateway.could_upgrade_at_click_time
    seen: dict = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(gateway, "could_upgrade_at_click_time", spy)
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "")
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_rollout_pct", 50)

    # A host/path no other fixture uses. Against the shared default a hardcoded
    # `dest="https://brand.com/products/serum"` at the call site passes, because every
    # fixture in this file happens to equal that literal.
    distinctive = "https://brand.com/products/uniquely-named-fixture-sku"
    offer = _resolve_exec_spec_offer(
        monkeypatch, client, evidence=False,
        row_overrides={"canonical_url": distinctive, "destination_url": distinctive},
    )
    assert seen, "the eligibility predicate was never consulted — this test proves nothing"
    assert seen["token"] == _token_of(offer), (
        "resolve time weighed a DIFFERENT token than the buyer's link carries; the rollout "
        "bucket, and therefore the claim, is decided on the wrong input"
    )
    assert seen["dest"] == distinctive, (
        "the predicate was handed a different URL than the link was built from"
    )


def test_redirect_token_is_recovered_byte_identically(client: TestClient) -> None:
    """No percent-decoding gap between the link we write and the token `GET /r` receives."""
    import routes.agent_shop_gateway as gateway
    from services.outbound_links_service import make_redirect_token

    token = make_redirect_token({"market": "US", "tool": "*", "dest": "https://brand.com/x", "ctx": {}})
    assert "." in token and "=" not in token, "fixture no longer resembles a real token"
    assert gateway._redirect_token_from_url(f"https://api.example.com/r?token={token}") == token
    # It must key on `token=` specifically, not "the first param" or "anything with an =".
    # Without these two rows a selector of `item.startswith("t")`, `"=" in item`, or
    # "return the first value" passes — every other fixture URL carries a single param.
    assert gateway._redirect_token_from_url(
        f"https://api.example.com/r?utm_source=x&token={token}") == token
    assert gateway._redirect_token_from_url(
        f"https://api.example.com/r?tokenish=nope&token={token}") == token
    # and the absent/odd cases degrade to "" rather than raising
    assert gateway._redirect_token_from_url("https://api.example.com/r") == ""
    assert gateway._redirect_token_from_url("https://api.example.com/r?utm_source=x") == ""
    assert gateway._redirect_token_from_url("") == ""


def test_the_claim_follows_the_url_the_link_was_actually_built_from(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The redirect is built from `canonical_url or destination_url`. When those two point at
    different HOSTS, warm eligibility must be judged on the one the buyer will actually reach.

    Judging the wrong column flips the answer silently: here `other.com` is off the allowlist,
    so reading it would restore a `false` the warm lane can contradict.
    """
    offer = _resolve_exec_spec_offer(
        monkeypatch, client, evidence=False,
        row_overrides={
            "canonical_url": "https://brand.com/products/serum",
            "destination_url": "https://other.com/products/serum",
        },
        allowed_domains=["brand.com", "other.com"],
    )
    assert _dest_of(offer).startswith("https://brand.com/"), (
        "fixture drift: the link no longer resolves to the canonical host, so this test "
        "cannot distinguish the two columns"
    )
    assert offer["cart_prefilled"] is None


def _resolve_offer_with_spec(monkeypatch, client, *, evidence: bool):
    """Drive the REAL route and return (offer, decoded_token_dest).

    The real `_make_external_redirect_url` runs — only the DB read is faked. The token is
    decoded so every assertion can compare the published spec against the destination a buyer
    would actually reach, rather than against another copy of our own intent.
    """
    import base64
    import json as _json
    from urllib.parse import parse_qs, urlparse

    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [_seed_row_for_exec_spec(evidence=evidence)]
        return []

    # These tests are about the execution spec, not about rate limiting, and the anon limiter is
    # MODULE-LEVEL shared state (`_INVOKE_ANON_IP_LIMIT_STORE`, 60 rpm per IP per minute). Every
    # request here would spend budget that a later file needs — adding these tests tipped
    # test_pdp_resolution_stability's 30-call loop into a 429. `rpm == 0` short-circuits BEFORE
    # the store is incremented, so this spends nothing rather than merely resetting a counter.
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or [])
              if o.get("purchase_route") == "affiliate_outbound"]
    assert offers, "the external offer must be returned for this to prove anything"
    offer = offers[0]

    tok = parse_qs(urlparse(offer["affiliate_url"]).query)["token"][0].split(".")[0]
    payload = _json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))
    return offer, payload


def test_cart_url_is_byte_identical_to_what_the_redirect_signs(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """THE anti-drift assertion for execution spec v0.

    `cart_url` is published to the agent; `affiliate_url` resolves to the token's `dest`. Two
    code paths compose them. If they can disagree, the spec is worse than absent — the agent
    would plan against a URL the buyer never reaches. They are equal by construction only
    because both go through `compose_attributed_destinations` with the same click id, and this
    is what proves it stays that way.
    """
    offer, payload = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    spec = offer["execution_spec"]

    assert spec["cart_url"], "evidence-stamped seed must produce a cart"
    assert spec["cart_url"] == payload["dest"], (
        "the published cart_url and the destination the token resolves to must be the SAME "
        "string — a spec that describes a different URL from the one the buyer reaches is a "
        "fabrication, not an approximation"
    )
    assert "/cart/41234567890123:1" in spec["cart_url"]


def test_one_click_id_spans_the_agents_lane_and_the_signed_token(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """T2-12. The join key must be ONE id across every surface, or attribution splits.

    Before this, the id lived only inside the signed `/r` token: an agent that used the
    destination URL directly produced revenue with no way to join it back. Minting per-surface
    would look correct in every isolated test and still split the join in production, so the
    identity is asserted across all three at once.
    """
    import routes.agent_shop_gateway as gateway

    offer, payload = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    spec = offer["execution_spec"]
    click_id = spec["tracking"]["click_id"]

    assert click_id, "a spec with no join key attributes nothing"
    assert payload["ctx"]["pvt_click_id"] == click_id, (
        "the token ctx feeds surface_click_events; a different id here means the click row and "
        "the merchant order can never be joined"
    )
    # The cart carries it as a cart ATTRIBUTE (Shopify persists that into note_attributes);
    # the PDP carries it as a plain query param. Same id, two carriers.
    assert f"attributes[pivota_click_id]={click_id}" in spec["cart_url"]
    assert f"{gateway.REFERRAL_CLICK_PARAM}={click_id}" in spec["pdp_url"]


def test_a_seed_with_no_variant_evidence_gets_a_pdp_but_never_a_cart(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Honest degradation. No justifiable numeric variant id means no cart — and the spec must
    say so consistently across FOUR fields, not just the one an agent happens to read."""
    offer, payload = _resolve_offer_with_spec(monkeypatch, client, evidence=False)
    spec = offer["execution_spec"]

    assert spec["cart_url"] is None
    assert spec["variant_id"] is None
    assert spec["rail"] == "referral"
    assert offer["cart_prefilled"] is False
    assert spec["tracking"]["join_mode"] == "referral_only"
    # ...and the PDP is still attributed, which is the whole point of degrading rather than
    # dropping the offer.
    assert spec["pdp_url"].startswith("https://brand.com/products/serum")
    assert spec["tracking"]["click_id"] in spec["pdp_url"]
    assert "/cart/" not in payload["dest"]


def test_rail_and_variant_id_track_the_cart_rather_than_being_asserted(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`rail` names what we will actually execute. It is derived from the composed cart, never
    from the platform label — a seed can be Shopify and still have no cart-able variant, and
    claiming `shopify_cart` there would send an agent down a path we cannot deliver."""
    offer, _payload = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    spec = offer["execution_spec"]

    assert spec["rail"] == "shopify_cart"
    assert spec["variant_id"] == "41234567890123", "the NUMERIC storefront id, not the SKU"
    assert spec["merchant_domain"] == "brand.com"
    # `rail` is never a rail we do not execute on this route.
    assert spec["rail"] in {"shopify_cart", "referral"}


def test_expires_at_is_read_off_the_token_not_recomputed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """An agent caching a spec needs to know when `affiliate_url` stops resolving. Recomputing
    the TTL beside the signer would be free to drift from it, so the value is read back off the
    token that was actually signed — and this asserts they are the same instant."""
    from datetime import datetime

    offer, payload = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    spec = offer["execution_spec"]

    assert spec["expires_at"], "a spec with no expiry invites an agent to cache it forever"
    parsed = datetime.fromisoformat(spec["expires_at"].replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == int(payload["exp"]), (
        "expires_at must be the token's own exp; any other number is a second TTL that can "
        "disagree with the one that signed the link"
    )


def test_the_allowlist_still_sees_the_destination_without_the_join_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the refactor that introduced `compose_attributed_destinations`.

    The domain allowlist historically ran on the UTM'd destination BEFORE the join key was
    appended. Passing the keyed URL instead would hand the allowlist reader literal `[` and `]`
    from the cart-attribute form. Nothing else in the suite pins which of the two it receives.
    """
    import asyncio

    import routes.agent_shop_gateway as gateway

    seen: dict = {}

    def recording_allowed(*, destination_url: str, allowed_domains):
        seen["destination_url"] = destination_url
        return True

    monkeypatch.setattr(gateway, "is_destination_domain_allowed", recording_allowed)

    url = asyncio.run(
        gateway._make_external_redirect_url(
            market="US", tool="*",
            destination_url="https://brand.com/products/serum",
            utm_template=None, ctx={},
            merchant_id=None, product_id=None, variant_id="SKU-1",
            cart_variant_id="41234567890123",
            shop_domain="brand.com", platform="shopify",
            seller_ref=None, seed_kind=None,
            allowed_domains=["brand.com"],
        )
    )
    assert url, "the redirect must still be built"
    checked = seen["destination_url"]
    assert "/cart/41234567890123:1" in checked, "it must still be the CART destination"
    assert "attributes[" not in checked, (
        "the allowlist must not receive the cart-attribute form — literal brackets are not part "
        "of a domain decision"
    )
    assert "pivota_click_id" not in checked and "pvt_click_id" not in checked


def _seed_row_split_hosts():
    """A seed whose canonical_url is on a DIFFERENT host from its shop domain.

    Nothing corrupt: the crawl can legitimately record a mirror/CDN canonical while the
    storefront evidence names the real shop. It is exactly the case where the allowlist gate
    and the published PDP part company.
    """
    row = _seed_row_for_exec_spec(evidence=True)
    row["canonical_url"] = "https://cdn-mirror.example/products/serum"
    row["destination_url"] = "https://cdn-mirror.example/products/serum"
    row["domain"] = "brand.com"
    row["seed_data"]["snapshot"]["shop_domain"] = "brand.com"
    return row


def test_pdp_url_is_never_published_for_a_host_the_allowlist_did_not_approve(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The allowlist gate runs on ONE url. When a cart exists that url is the CART, built on the
    shop domain — so a `pdp_url` composed from `canonical_url` can be a host nothing vetted.

    Publishing it would be net-new egress to an unapproved destination carrying our click id,
    and would contradict the `merchant_domain` printed beside it. Withhold it instead: a spec
    that omits a field is honest, one that points at an unvetted host is not.
    """
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [_seed_row_split_hosts()]
        return []

    # These tests are about the execution spec, not about rate limiting, and the anon limiter is
    # MODULE-LEVEL shared state (`_INVOKE_ANON_IP_LIMIT_STORE`, 60 rpm per IP per minute). Every
    # request here would spend budget that a later file needs — adding these tests tipped
    # test_pdp_resolution_stability's 30-call loop into a 429. `rpm == 0` short-circuits BEFORE
    # the store is incremented, so this spends nothing rather than merely resetting a counter.
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or [])
              if o.get("purchase_route") == "affiliate_outbound"]
    if not offers:
        pytest.skip("this seed shape produced no external offer; the gate is unreachable here")

    spec = offers[0]["execution_spec"]
    assert spec["cart_url"], "the cart is on the approved host and must still be served"
    assert spec["pdp_url"] is None, (
        f"pdp_url on an unvetted host must be withheld, got {spec['pdp_url']!r}"
    )
    # And the spec stays internally consistent: no field names a host another field contradicts.
    assert "cdn-mirror.example" not in json.dumps(spec)


def test_tracking_param_names_the_carrier_that_is_actually_in_the_url(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`tracking.param` tells an agent where to FIND the join key. A cart carries it as a cart
    attribute, a referral as a plain query param. A constant here points an agent following
    `cart_url` at a string that appears nowhere in it — the key looks missing when it is present.
    """
    import routes.agent_shop_gateway as gateway

    cart_offer, _ = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    cart_spec = cart_offer["execution_spec"]
    assert cart_spec["cart_url"]
    assert cart_spec["tracking"]["param"] == gateway.SHOPIFY_CART_CLICK_ATTRIBUTE
    assert f'{cart_spec["tracking"]["param"]}={cart_spec["tracking"]["click_id"]}' in (
        cart_spec["cart_url"]
    ), "the named carrier must literally appear in the url it describes"


def test_tracking_param_names_the_referral_carrier_when_there_is_no_cart(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import routes.agent_shop_gateway as gateway

    offer, _ = _resolve_offer_with_spec(monkeypatch, client, evidence=False)
    spec = offer["execution_spec"]
    assert spec["cart_url"] is None
    assert spec["tracking"]["param"] == gateway.REFERRAL_CLICK_PARAM
    assert f'{spec["tracking"]["param"]}={spec["tracking"]["click_id"]}' in spec["pdp_url"]


def test_pdp_url_stays_the_product_page_even_when_a_cart_exists(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Review F3. The only test pinning `pdp_url` as a product page ran in the NO-CART branch,
    where composing it from `cart_base or destination_url` is inert. An agent may legitimately
    want to show the PDP while still handing off to the cart, so the distinction has to hold
    exactly where it is destroyable.
    """
    offer, _payload = _resolve_offer_with_spec(monkeypatch, client, evidence=True)
    spec = offer["execution_spec"]

    assert spec["cart_url"] and "/cart/" in spec["cart_url"]
    assert spec["pdp_url"], "the PDP must still be published on the approved host"
    assert "/cart/" not in spec["pdp_url"], "pdp_url must be the PRODUCT page, not the cart"
    assert "/products/serum" in spec["pdp_url"]
    assert spec["pdp_url"] != spec["cart_url"]


def test_merchant_domain_is_normalized_not_echoed(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Review F3. `merchant_domain` is a HOST, and the fixture's bare `domain` column made the
    normalization unassertable. Drive a shop_domain carrying scheme, userinfo, port and path —
    every part that must not survive into a field an agent may compare or key on.
    """
    import routes.agent_shop_gateway as gateway

    # The messy value must go on the `domain` COLUMN: _external_seed_redirect_identity reads
    # `row["domain"] or seed_data["domain"]` RAW and only falls back to a URL-derived host,
    # which is already normalized. Putting it anywhere else makes this test pass without ever
    # exercising the normalization — which is exactly how it failed to kill its mutant first time.
    row = _seed_row_for_exec_spec(evidence=True)
    row["domain"] = "https://User:Pass@Brand.COM:443/shop/"

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [row]
        return []

    # These tests are about the execution spec, not about rate limiting, and the anon limiter is
    # MODULE-LEVEL shared state (`_INVOKE_ANON_IP_LIMIT_STORE`, 60 rpm per IP per minute). Every
    # request here would spend budget that a later file needs — adding these tests tipped
    # test_pdp_resolution_stability's 30-call loop into a 429. `rpm == 0` short-circuits BEFORE
    # the store is incremented, so this spends nothing rather than merely resetting a counter.
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10, "market": "US", "tool": "*"},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200, res.text
    offers = [o for o in (res.json().get("offers") or [])
              if o.get("purchase_route") == "affiliate_outbound"]
    if not offers:
        pytest.skip("no external offer for this shape")

    domain = offers[0]["execution_spec"]["merchant_domain"]
    assert domain == "brand.com", f"expected a bare lowercase host, got {domain!r}"
    for leaked in ("https://", "User", "Pass", ":443", "/shop"):
        assert leaked not in (domain or ""), f"{leaked!r} must not survive into merchant_domain"


# ---------------------------------------------------------------------------
# execution_spec.rail vs the warm-handoff click lane (#1846 shipped `rail` after the
# cart_prefilled guard landed, carrying the same exposure into a second field).
# ---------------------------------------------------------------------------


def test_rail_is_null_rather_than_referral_when_the_warm_lane_could_upgrade(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """`"referral"` is a positive claim about where the buyer ends up, on exactly the cold
    population the warm lane targets — the same defect `cart_prefilled: false` had.

    An agent that hands the buyer `affiliate_url` (the attributed link we WANT it to use) is
    told "referral" and the buyer can land in a prefilled cart.
    """
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    spec = offer["execution_spec"]
    assert "rail" in spec, "the field must stay present — null is a STATE, not an omission"
    assert spec["rail"] is None


def test_rail_and_cart_prefilled_can_never_contradict_each_other(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """THE invariant. Both answer "what does following our link land the buyer in", so a single
    offer must not carry two different answers in one payload.

    Asserted across every configuration that moves the verdict, because computing them from two
    separate expressions is exactly how they would drift.

    LIMIT, stated so nobody over-trusts this: the single shared call is a STRUCTURAL property
    and no behavioural test can enforce it. Re-splitting it into two byte-identical calls is
    invisible here and always will be — only a recomputation that actually DIVERGES (a wrong
    `destination_url`, say) gets caught. This test guards the agreement, not the sharing.
    """
    cases = [
        ("allowlisted brand, cold", "brand.com", 0, False),
        ("allowlisted brand, warm", "brand.com", 0, True),
        ("host off the allowlist", "someone-else.com", 0, False),
        ("no allowlist, full rollout", "", 100, False),
        ("no allowlist, no rollout", "", 0, False),
    ]
    for label, brands, pct, evidence in cases:
        monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", brands)
        monkeypatch.setattr(warm_lane, "outbound_warm_handoff_rollout_pct", pct)
        offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=evidence)
        rail = offer["execution_spec"]["rail"]
        prefilled = offer["cart_prefilled"]
        assert (rail is None) is (prefilled is None), (
            f"{label}: rail={rail!r} but cart_prefilled={prefilled!r} — one hedges and the "
            "other commits, about the same link in the same payload"
        )
        if prefilled is True:
            assert rail == "shopify_cart", f"{label}: {rail!r}"
        elif prefilled is False:
            assert rail == "referral", f"{label}: {rail!r}"


def test_rail_still_says_referral_where_that_claim_is_provable(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """The guard must not blanket the field with nulls — a host the lane can never touch keeps
    its rail, or the field stops carrying information at all."""
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "someone-else.com")
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["execution_spec"]["rail"] == "referral"

    # Put the host BACK on the allowlist before flipping the flag, or this leg proves nothing
    # about the flag: eligibility returns `not_allowlisted` before it is ever consulted, and the
    # leg silently becomes a weaker duplicate of the one above. Measured — without this line,
    # deleting the resolve-time flag check entirely left all four of these tests green.
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_brands_raw", "brand.com")
    monkeypatch.setattr(warm_lane, "outbound_warm_handoff_enabled", False)
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=False)
    assert offer["execution_spec"]["rail"] == "referral"


def test_a_shopify_cart_rail_is_never_nulled_by_the_guard(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, warm_lane
) -> None:
    """One-sided, like cart_prefilled: the lane only ever BUILDS carts (and since #1848 refuses
    a dest that is already one), so a cart rail cannot be falsified and must survive."""
    offer = _resolve_exec_spec_offer(monkeypatch, client, evidence=True)
    assert offer["execution_spec"]["rail"] == "shopify_cart"
    assert offer["cart_prefilled"] is True


# ---------------------------------------------------------------------------
# resolution_mode HONESTY (follow-up to #1907)
#
# #1907 fixed the external-only half of a two-part mislabel and its own body called the
# remaining half "equally untrue". This is that half, pinned.
#
# The handler used to initialize `resolution_mode` from the surface --
# `"not_servable" if strict_serving_mode else "exact_match"` -- and the internal lane's
# three assignments all sat inside `if strict_serving_mode:`. On the RELAXED surface
# nothing ever wrote the field, so it emitted the initializer unconditionally: a request
# that matched nothing at all still answered "we matched your product exactly" next to
# offers_count=0.
# ---------------------------------------------------------------------------


def test_relaxed_surface_zero_offers_must_not_claim_exact_match(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """THE NEGATIVE. A relaxed-surface request that resolves nothing must not say exact_match.

    Probe-verified on the #1907 branch and on main, this exact request answered:
        offers_count=0  resolution_mode=exact_match  reason_code=NO_CANDIDATES

    Every source returns empty, so there is no product, no variant and no offer anywhere in
    this response for "exact_match" to refer to. Asserted on all three placements because the
    field ships three times (top level, `mapping`, `metadata`) and an agent may read any one.
    """
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            # NO commerce_surface -> commerce_surface_explicit False -> the relaxed lane.
            "payload": {"product": {"sku_id": "sku_nonexistent_xyz"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["offers_count"] == 0
    assert body.get("offers") == []

    metadata = body.get("metadata") or {}
    mapping = body.get("mapping") or {}

    # The lie, named directly: zero offers can never be an exact match.
    assert body["resolution_mode"] != "exact_match"
    assert mapping.get("resolution_mode") != "exact_match"
    assert metadata.get("resolution_mode") != "exact_match"

    # ...and the truthful value, so this cannot be satisfied by emitting junk.
    assert body["resolution_mode"] == "not_servable"
    assert mapping.get("resolution_mode") == "not_servable"
    assert metadata.get("resolution_mode") == "not_servable"

    assert body["resolution_mode"] in gateway.RESOLUTION_MODES
    assert metadata.get("reason_code") == "NO_CANDIDATES"
    assert metadata.get("has_external") is False
    assert metadata.get("has_internal") is False


def test_relaxed_surface_reports_substitution_when_variant_was_substituted(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The relaxed surface must ANSWER the variant question, not skip it.

    The caller asks for `sku_blocked`; it is out of stock, so the offer ships `sku_ok`
    instead. That is a substitution on any surface -- the predicate compares requested
    against shipped and has nothing to do with commerce_surface. The strict surface already
    said so (test_offers_resolve_substitutes_variant_...); the relaxed surface said
    "exact_match" because the assignment block was gated on strict_serving_mode.
    """
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_relaxed_sub",
                    "platform": "shopify",
                    "platform_product_id": "prod_relaxed_sub",
                    "product_data": {
                        "id": "prod_relaxed_sub",
                        "title": "Relaxed Substitution Product",
                        "currency": "USD",
                        "price": 30.0,
                        "orderable": True,
                        "variants": [
                            {"id": "sku_blocked", "sku": "sku_blocked", "price": 30.0, "inventory_quantity": 0},
                            {"id": "sku_ok", "sku": "sku_ok", "price": 30.0, "inventory_quantity": 7},
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
            # Relaxed: no commerce_surface.
            "payload": {"product": {"sku_id": "sku_blocked"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"

    # An offer DID ship -- this is not the zero-offer case.
    assert body["offers_count"] >= 1
    assert body["resolved_target"]["variant_id"] == "sku_ok"
    assert body["requested_target"]["sku_id"] == "sku_blocked"

    # ...for a variant the caller did not ask for.
    assert body["resolution_mode"] == "same_product_substitution"
    assert (body.get("mapping") or {}).get("resolution_mode") == "same_product_substitution"
    assert (body.get("metadata") or {}).get("resolution_mode") == "same_product_substitution"
    assert "requested_variant_not_servable" in (body.get("substitution_reason_codes") or [])


# A seed row shaped like the ones that actually SERVE (see
# test_offers_resolve_prefers_external_outbound). An earlier cut of the two tests below used a
# differently-shaped row, `should_block_external_referral_runtime` filtered it, and the
# assertions passed against zero offers WITHOUT live verification ever running -- green for the
# wrong reason. Both tests now assert the verifier was actually handed offers.
def _servable_seed_row(*, seed_id: str, external_product_id: str, variant_id: str) -> dict:
    return {
        "id": seed_id,
        "external_product_id": external_product_id,
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
            "snapshot": dict(_VERIFIED_CONTENT),
            "brand": "Fenty Beauty",
            "variants": [
                {
                    "variant_id": variant_id,
                    "title": "Gloss Bomb 9ml",
                    "price_amount": 19.0,
                    "price_currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
        "status": "active",
        **_VERIFIED_DESTINATION,
    }


def test_live_verification_dropping_every_offer_downgrades_resolution_mode(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`external_only` over offers_count=0, and `has_external` true over an empty list.

    `live_offer_verification.apply_verdicts` DROPS offers it proved GONE and can drop all of
    them. #1907 stamped "external_only" ABOVE that block, and metadata computed `has_external`
    from the pre-verification candidate list, so a response whose only offer was verified away
    announced "external_only" + has_external=true next to an empty `offers` list.

    Latent on main only because LIVE_OFFER_VERIFICATION_ENABLED defaults OFF. This test arms
    the flag, which is the state arming it in prod would produce.
    """
    import routes.agent_shop_gateway as gateway
    from services import live_offer_verification as lov

    async def fake_fetch_all(query: str, values=None):
        if "FROM external_product_seeds" in str(query):
            return [
                _servable_seed_row(
                    seed_id="eps_gone",
                    external_product_id="ext_gone_1",
                    variant_id="SKU_GONE_001",
                )
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?t=x")
    )

    verified: dict = {}

    async def fake_verify_offers(offers):
        # Records that verification actually ran, and on how much. Without this the test can
        # go green on a response that had zero offers before the verifier was ever consulted.
        verified["count"] = len(offers)
        return {index: lov.Verdict(status=lov.GONE, reason="probe_dead_link") for index in range(len(offers))}

    # apply_verdicts is left REAL so the genuine drop path runs, not a simulation of it.
    monkeypatch.setattr(lov, "is_enabled", lambda: True)
    monkeypatch.setattr(lov, "verify_offers", fake_verify_offers)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU_GONE_001"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"

    # The offer EXISTED and was handed to the verifier -- this is a drop, not a no-match.
    assert verified.get("count"), "live verification never ran; the seed never became an offer"

    # ...and then every one of them was dropped.
    assert body["offers_count"] == 0
    assert body.get("offers") == []

    # So nothing is external-only, and nothing is external at all.
    metadata = body.get("metadata") or {}
    assert body["resolution_mode"] != "external_only"
    assert body["resolution_mode"] == "not_servable"
    assert (body.get("mapping") or {}).get("resolution_mode") == "not_servable"
    assert metadata.get("resolution_mode") == "not_servable"
    assert metadata.get("has_external") is False
    assert metadata.get("has_internal") is False


def test_live_verification_dropping_the_internal_offer_reports_external_only(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Internal resolved, then died. What ships is external, so the answer is external_only.

    The old stamp keyed on `external_offers and not internal_offers` -- pre-verification
    candidate lists -- so an internal offer that verification proved GONE still suppressed the
    external_only stamp, and the response kept the internal lane's "exact_match" while
    shipping nothing but a referral. Deriving from the shipped list is what sees this.
    """
    import routes.agent_shop_gateway as gateway
    from services import live_offer_verification as lov

    async def fake_fetch_all(query: str, values=None):
        q = str(query)
        if "FROM external_product_seeds" in q:
            return [
                _servable_seed_row(
                    seed_id="eps_survivor",
                    external_product_id="ext_survivor",
                    variant_id="SKU_MIX_001",
                )
            ]
        if "FROM products_cache" in q:
            return [
                {
                    "merchant_id": "merch_mix",
                    "platform": "shopify",
                    "platform_product_id": "prod_mix",
                    "product_data": {
                        "id": "prod_mix",
                        "title": "Internal Product",
                        "currency": "USD",
                        "price": 20.0,
                        "orderable": True,
                        "variants": [{"id": "SKU_MIX_001", "price": 20.0, "inventory_quantity": 5}],
                    },
                }
            ]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        gateway, "_make_external_redirect_url", AsyncMock(return_value="https://example.com/r?t=x")
    )

    routes_seen: dict = {}

    async def fake_verify_offers(offers):
        # Kill ONLY the internal offer; the referral survives.
        routes_seen["routes"] = [str(o.get("purchase_route") or "") for o in offers]
        return {
            index: lov.Verdict(status=lov.GONE, reason="internal_dead")
            for index, offer in enumerate(offers)
            if str(offer.get("purchase_route") or "") == "internal_checkout"
        }

    monkeypatch.setattr(lov, "is_enabled", lambda: True)
    monkeypatch.setattr(lov, "verify_offers", fake_verify_offers)

    res = client.post(
        "/agent/shop/v1/invoke",
        json={
            "operation": "offers.resolve",
            "payload": {"product": {"sku_id": "SKU_MIX_001"}, "limit": 10},
            "metadata": {"source": "creator-agent-ui"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"

    # Both lanes really did produce an offer, and the internal one really was verified.
    assert "internal_checkout" in (routes_seen.get("routes") or []), routes_seen
    assert "affiliate_outbound" in (routes_seen.get("routes") or []), routes_seen

    offers = body.get("offers") or []
    assert offers, "the external offer should have survived verification"
    assert all(o.get("purchase_route") == "affiliate_outbound" for o in offers), offers

    metadata = body.get("metadata") or {}
    assert body["resolution_mode"] == "external_only"
    assert (body.get("mapping") or {}).get("resolution_mode") == "external_only"
    assert metadata.get("resolution_mode") == "external_only"
    assert metadata.get("has_internal") is False
    assert metadata.get("has_external") is True
