import asyncio
import logging
import re
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


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


def test_offers_resolve_recovers_external_seed_by_internal_identity_after_store_rebind(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
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
            "payload": {"product": {"product_id": "prod_new_gbr"}, "limit": 10, "market": "US", "tool": "*"},
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
    snapshot = {"variants": [{"variant_id": "SKU-1", "title": "30ml",
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
