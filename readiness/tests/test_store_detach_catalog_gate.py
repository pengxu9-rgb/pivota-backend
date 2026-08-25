"""Detaching a store must retire its catalog from the readiness report.

The product cache is keyed on merchant_id + platform ALONE — no store_id — and
readiness reads it with include_expired=True. `DELETE /merchant/integrations/
store/{store_id}` drops the merchant_stores row and nothing retires those cached
rows, so before this gate every product a detached store ever synced kept
generating per-variant blockers (out_of_stock, inventory_stale, ...) forever.
The merchant portal rendered that as "historical issues" on the overview card
and in the product-optimization workspace, for a store that no longer existed.

The load-bearing detail these tests pin down is WHICH signal the gate reads.
`shopify_connected` is the wrong one: `_get_shopify_config_for_merchant` falls
back to the GLOBAL settings/env Shopify credentials, which are set in prod, so
it reads "connected" for a merchant with zero stores. Only `get_primary_store`
answers the structural question.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from readiness.sources import shopify_live
from readiness.summary import _CODE_TO_BUCKET, _humanize_code
from readiness.tests.conftest import (
    ALPHA_MERCHANT_ID,
    build_review_summaries,
    load_real_merchant_fixture,
)


def _install_common_stubs(monkeypatch, fixture, *, cache_calls, live_calls):
    async def fake_get_merchant_onboarding(_merchant_id: str):
        return {"merchant_id": fixture["merchant_id"], "business_name": fixture["merchant_name"]}

    async def fake_get_active_psp(_merchant_id: str):
        return fixture["merchant_psp"]

    async def fake_load_runtime_cache_rows(merchant_id: str):
        cache_calls.append(merchant_id)
        rows = fixture["products_cache_rows"]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        # Deliberately FRESH, so a cached-row path needs no live overlay and any
        # products that appear came from the cache and nowhere else.
        for row in rows:
            row["cached_at"] = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        return rows

    async def fake_fetch_live_products(merchant_id: str, shop_domain: str, _access_token: str):
        live_calls.append((merchant_id, shop_domain))
        return [], None

    async def fake_load_product_review_summaries(**_kwargs):
        return build_review_summaries()

    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", ALPHA_MERCHANT_ID)
    monkeypatch.setattr(shopify_live, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(shopify_live, "_fetch_active_psp_config", fake_get_active_psp)
    monkeypatch.setattr(shopify_live, "_load_runtime_cache_rows", fake_load_runtime_cache_rows)
    monkeypatch.setattr(shopify_live, "_fetch_live_products", fake_fetch_live_products)
    monkeypatch.setattr(
        shopify_live, "load_product_review_summaries", fake_load_product_review_summaries
    )


async def _load(monkeypatch, *, store, shopify_config):
    fixture = load_real_merchant_fixture()
    cache_calls: list[str] = []
    live_calls: list[tuple[str, str]] = []
    _install_common_stubs(monkeypatch, fixture, cache_calls=cache_calls, live_calls=live_calls)

    async def fake_get_primary_store(_merchant_id: str):
        return store

    async def fake_get_shopify_cfg(_merchant_id: str):
        return shopify_config

    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)

    dataset = await shopify_live.load_shopify_live_merchant_dataset(ALPHA_MERCHANT_ID)
    return dataset, cache_calls, live_calls


@pytest.mark.asyncio
async def test_detached_store_retires_cached_catalog(monkeypatch):
    """No store row -> the cache is never read and no product issues are minted."""
    fixture = load_real_merchant_fixture()
    dataset, cache_calls, live_calls = await _load(
        monkeypatch, store=None, shopify_config=fixture["shopify_config"]
    )

    assert dataset.products == []
    assert cache_calls == [], "detached store must not read the orphaned product cache"
    assert live_calls == [], "detached store must not hit the live Shopify Admin API"
    assert "store_connection_missing" in dataset.merchant_blockers


@pytest.mark.asyncio
async def test_connected_store_still_reads_the_cached_catalog(monkeypatch):
    """The mutant guard: the gate must not be an unconditional blank-out.

    Without this, `cached_rows = []` unconditionally would pass every other
    assertion in this file.
    """
    fixture = load_real_merchant_fixture()
    dataset, cache_calls, _ = await _load(
        monkeypatch, store=fixture["store"], shopify_config=fixture["shopify_config"]
    )

    assert len(dataset.products) == len(fixture["products_cache_rows"]) > 0
    assert cache_calls == [ALPHA_MERCHANT_ID]
    assert "store_connection_missing" not in dataset.merchant_blockers


@pytest.mark.asyncio
async def test_global_env_credentials_do_not_resurrect_a_detached_catalog(monkeypatch):
    """The reason the gate reads `get_primary_store` and not `shopify_connected`.

    `_get_shopify_config_for_merchant` falls back to the global settings/env
    Shopify credentials, so a merchant with ZERO stores still gets a usable
    shop_domain + access_token in prod. Gating on `shopify_connected` would
    leave the orphaned catalog fully readable — this is the exact case that
    kept the portal showing historical issues after a detach.
    """
    fixture = load_real_merchant_fixture()
    dataset, cache_calls, live_calls = await _load(
        monkeypatch,
        store=None,
        # A non-empty config, exactly as the global env fallback returns it.
        shopify_config=fixture["shopify_config"],
    )

    # Preconditions: the config really does look "connected".
    assert fixture["shopify_config"]["shop_domain"]
    assert fixture["shopify_config"]["access_token"]
    assert "shopify_configuration_missing" not in dataset.merchant_blockers

    # ...and the catalog is retired anyway.
    assert dataset.products == []
    assert cache_calls == []
    assert live_calls == []


@pytest.mark.asyncio
async def test_detached_store_does_not_also_claim_catalog_missing(monkeypatch):
    """`catalog_missing` means "you have a store but we cannot see its products".

    Emitting it for a merchant with no store points them at a sync they have no
    store to run.
    """
    fixture = load_real_merchant_fixture()
    dataset, _, _ = await _load(
        monkeypatch, store=None, shopify_config=fixture["shopify_config"]
    )
    assert "catalog_missing" not in dataset.merchant_blockers


@pytest.mark.asyncio
async def test_connected_store_with_empty_cache_still_reports_catalog_missing(monkeypatch):
    """The mutant guard for the line above: `catalog_missing` must stay reachable."""
    fixture = load_real_merchant_fixture()
    cache_calls: list[str] = []
    live_calls: list[tuple[str, str]] = []
    _install_common_stubs(monkeypatch, fixture, cache_calls=cache_calls, live_calls=live_calls)

    async def fake_get_primary_store(_merchant_id: str):
        return fixture["store"]

    async def fake_get_shopify_cfg(_merchant_id: str):
        return fixture["shopify_config"]

    async def empty_cache(merchant_id: str):
        cache_calls.append(merchant_id)
        return []

    monkeypatch.setattr(shopify_live, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(shopify_live, "_get_shopify_config_for_merchant", fake_get_shopify_cfg)
    monkeypatch.setattr(shopify_live, "_load_runtime_cache_rows", empty_cache)

    dataset = await shopify_live.load_shopify_live_merchant_dataset(ALPHA_MERCHANT_ID)

    assert dataset.products == []
    assert "catalog_missing" in dataset.merchant_blockers
    assert "store_connection_missing" not in dataset.merchant_blockers


def test_store_connection_missing_routes_the_merchant_to_integrations():
    """A "reconnect your store" blocker must not deep-link into the product
    workspace, which is exactly where the merchant cannot fix it."""
    assert _CODE_TO_BUCKET["store_connection_missing"] == "checkout_payment_setup"
    assert _CODE_TO_BUCKET["shopify_configuration_missing"] == "checkout_payment_setup"
    assert _humanize_code("store_connection_missing") == "Store not connected"
