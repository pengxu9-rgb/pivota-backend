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
test_import_refuses_to_run_on_the_platform_store red; making the refusal a
terminal ShopifyConfigError again turns the transient-retry test red.
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

    async def _record_and_explode(*args, **kwargs):
        fetched.append(kwargs or args)
        raise AssertionError("the import reached Shopify with no merchant credentials")

    # BOTH outbound entry points are doubled, and `fetched` is what the test
    # asserts on — not the exception.
    #
    # fetch_shop_currency alone is NOT a trip-wire: the worker wraps it in a bare
    # `except Exception: shop_currency = None`, which swallows an AssertionError
    # raised from a double. An earlier version of this test patched only that,
    # so under a mutant the flow sailed past it and made a REAL HTTPS request to
    # the env-configured domain; the test still went red, but via the retry
    # trip-wire below rather than the assertion that claims to prove it, and it
    # was not network-isolated. Recording into a list survives the swallow.
    monkeypatch.setattr(worker.ShopifyProductAdapter, "fetch_shop_currency", _record_and_explode)
    monkeypatch.setattr(worker, "_fetch_shopify_products_page", _record_and_explode)
    monkeypatch.setattr(worker, "_fetch_shopify_products", _record_and_explode)

    recorded = {}

    async def _fake_retry(task_id, error, counts=None, next_run_at=None):
        recorded["error"] = error
        recorded["counts"] = counts
        return True

    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _fake_retry)

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

    assert fetched == [], (
        "no Shopify call may be made without merchant credentials; "
        f"reached: {fetched}"
    )
    assert result["status"] == "retry_scheduled"
    assert "reconnect it in Integrations" in recorded["error"]
    assert recorded["counts"]["error_category"] == "credentials_unavailable"


async def test_a_transient_resolution_failure_is_retried_not_failed_permanently(monkeypatch):
    """The regression the first draft of this change introduced.

    `get_merchant_active_stores` catches its own DB errors and returns `[]`, so
    a statement-timeout blip is indistinguishable from "this merchant has no
    store". Making the refusal terminal would permanently fail a FULLY CONNECTED
    merchant's import on attempt 1 over a momentary wobble — and nothing
    re-enqueues a `failed` row. Retrying costs zero Shopify calls, because the
    resolver returns before any fetch.
    """
    import services.merchant_store_service as store_service

    async def _db_blip(merchant_id):
        return []  # what the service returns when it swallows a statement timeout

    monkeypatch.setattr(store_service, "get_merchant_active_stores", _db_blip)

    outcome = {}

    async def _fake_retry(task_id, error, counts=None, next_run_at=None):
        outcome["retried"] = True
        outcome["counts"] = counts
        return True

    async def _fail_is_wrong(*args, **kwargs):
        raise AssertionError(
            "a transient resolution failure must be retried, not failed terminally"
        )

    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _fake_retry)
    monkeypatch.setattr(worker, "mark_import_task_failed", _fail_is_wrong)

    result = await worker._process_import_task_record(
        {
            "id": 2,
            "merchant_id": "merch_connected_but_db_blipped",
            "source_type": "connector",
            "connector": "shopify",
            "attempt": 1,
            "counts": {},
        }
    )

    assert outcome.get("retried") is True
    assert result["status"] == "retry_scheduled"


async def test_a_genuinely_storeless_merchant_still_terminates(monkeypatch):
    """Positive counterpart: retrying must not mean retrying forever. At the
    attempt ceiling the task fails, so a merchant who will never have a store
    does not occupy the drain's FIFO head indefinitely."""
    outcome = {}

    async def _fake_failed(task_id, error, counts=None):
        outcome["failed"] = True
        return True

    async def _retry_is_wrong(*args, **kwargs):
        raise AssertionError("past the attempt ceiling this must be terminal")

    monkeypatch.setattr(worker, "mark_import_task_failed", _fake_failed)
    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _retry_is_wrong)

    result = await worker._process_import_task_record(
        {
            "id": 3,
            "merchant_id": "merch_no_store",
            "source_type": "connector",
            "connector": "shopify",
            "attempt": worker.SHOPIFY_MAX_RETRY_ATTEMPTS,
            "counts": {},
        }
    )

    assert outcome.get("failed") is True
    assert result["status"] == "failed"
