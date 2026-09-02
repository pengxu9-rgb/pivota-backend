"""A merchant-scoped catalog import must never run on the PLATFORM's Shopify store.

`_get_shopify_config_for_merchant` resolves three tiers: this merchant's
connector_credentials, then this merchant's connected store, then the GLOBAL
settings/env credentials. The third answers a different question than the one
asked — it returns the platform's own store for any merchant — and those env
vars are set in production (readiness/tests/test_store_detach_catalog_gate.py
says so in as many words, and exists because of it).

On a read path that is merely wrong. On the import path it is destructive: the
Shopify branch upserts what it fetches into the merchant's products_cache and
then expires every older row of theirs in the full-sync sweep. A merchant who
detached their store, or whose credentials were revoked, would have their
catalog replaced by the platform's own.

This was survivable while the only runner was the endpoint's BackgroundTask,
which gates on a connected store before enqueuing. `catalog_import_drain_tick`
has no such precondition — it claims whatever is oldest — so arming the drain is
what makes this reachable at scale.

Mutation-checked: reverting the call site to `_get_shopify_config_for_merchant(
merchant_id)` (i.e. letting the fallback back in) turns
test_import_refuses_to_run_on_the_platform_store red.
"""

from __future__ import annotations

import pytest

import jobs.catalog_import_worker as worker


@pytest.fixture(autouse=True)
def _global_env_store(monkeypatch):
    """Prod has these set; that is the whole premise."""
    monkeypatch.setenv("SHOPIFY_STORE_URL", "platform-own-store.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "shpat_platform_token")
    monkeypatch.setattr(worker.settings, "shopify_store_url", None, raising=False)
    monkeypatch.setattr(worker.settings, "shopify_access_token", None, raising=False)


@pytest.fixture(autouse=True)
def _no_merchant_credentials(monkeypatch):
    """The merchant has neither connector credentials nor a connected store."""
    async def _no_credential(merchant_id, connector):
        return None

    monkeypatch.setattr(worker, "get_latest_connector_credential_for_merchant", _no_credential)

    import services.merchant_store_service as store_service

    async def _no_stores(merchant_id):
        return []

    monkeypatch.setattr(store_service, "get_merchant_active_stores", _no_stores)


async def test_the_resolver_still_falls_back_for_read_callers():
    """Positive counterpart. The fallback is not deleted — readiness read paths
    (readiness/sources/shopify_live.py, readiness/service.py) still get it, so
    this change cannot alter their behaviour. Without this, a test that only
    asserted the refusal would also pass if the fallback were removed outright.
    """
    cfg = await worker._get_shopify_config_for_merchant("merch_no_store")

    assert cfg["shop_domain"] == "platform-own-store.myshopify.com"
    assert cfg["access_token"] == "shpat_platform_token"


async def test_the_resolver_refuses_the_fallback_when_asked_to():
    cfg = await worker._get_shopify_config_for_merchant(
        "merch_no_store", allow_global_fallback=False
    )

    assert cfg == {"shop_domain": "", "access_token": ""}


async def test_import_refuses_to_run_on_the_platform_store(monkeypatch):
    """The delivering assertion: the IMPORT fails rather than fetching.

    Asserting only on the resolver would leave open that the worker calls it
    with the default. This drives _process_import_task_record and proves no
    Shopify fetch is ever attempted.
    """
    fetched = []

    async def _explode(*args, **kwargs):
        fetched.append(args)
        raise AssertionError("the import fetched from Shopify with no merchant credentials")

    monkeypatch.setattr(worker.ShopifyProductAdapter, "fetch_shop_currency", _explode)

    recorded = {}

    async def _fake_failed(task_id, error, counts=None):
        recorded["error"] = error
        recorded["counts"] = counts
        return True

    async def _unexpected_retry(*args, **kwargs):
        raise AssertionError("a missing-credentials import must be terminal, not retried")

    monkeypatch.setattr(worker, "mark_import_task_failed", _fake_failed)
    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _unexpected_retry)

    result = await worker._process_import_task_record(
        {
            "id": 1,
            "merchant_id": "merch_no_store",
            "source_type": "connector",
            "connector": "shopify",
            "attempt": 1,
            "counts": {},
        }
    )

    assert fetched == [], "no Shopify call may be made without merchant credentials"
    assert result["status"] == "failed"
    assert "No Shopify credentials for this merchant" in recorded["error"]
    # terminal, not retried: a merchant with no store will never acquire one by
    # waiting, so five backoff rounds against the platform store is pure risk
    assert recorded["counts"]["error_category"] == "config"
