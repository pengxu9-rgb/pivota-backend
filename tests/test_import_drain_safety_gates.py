"""The two safety gates the drain needed before it could be armed by default.

#1997 turns `catalog_import_drain_tick` ON by default. Its premise was that the
backlog is "benign by construction" after #1989: a disconnected merchant has no
credentials, so their row fails at zero Shopify calls. Review broke that premise
in two places, and these tests pin the fixes.

DETACHED IS NOT CREDENTIAL-LESS. `connector_credentials` survives every detach
path — nothing in this repo ever sets `is_valid=False` for shopify — and
`get_merchant_active_stores` falls through to a legacy merchant_onboarding leg
that appends a store merely LABELLED 'disconnected'. Tier 2 of the resolver
filtered on platform only and imported anyway. So a merchant who clicked Sync,
whose row stranded on a revision swap, and who then detached their store a week
later, would have their catalog re-imported by the drain months on. The fix is
two gates: tier 2 honours the status label, and the import branch re-asserts the
Sync endpoint's own enqueue precondition (an active/connected store) at run time.

A CONTINUATION EXPIRED ITS OWN EARLIER RUN. `full_sync_started_at` was this
invocation's start. The completion sweep expires `cached_at < that`, so run 2 of
a >5,000-product catalog expired everything run 1 had just imported, leaving the
merchant serving the tail of their catalog. The fix carries the FIRST run's
start through `counts` and reuses it when resuming from a cursor.

Mutation-checked (each turns exactly one test red):
  * drop the `status in (active, connected)` skip in tier 2
      -> test_tier_two_skips_a_store_labelled_disconnected
  * drop the enqueue-precondition gate in the import branch
      -> test_a_detached_merchant_with_surviving_credentials_is_not_imported
  * `full_sync_started_at = started_at` unconditionally (stop carrying it)
      -> test_a_continuation_sweeps_against_the_first_runs_start
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import jobs.catalog_import_worker as worker


@pytest.fixture(autouse=True)
def _no_global_env_store(monkeypatch):
    """Keep tier 3 out of the picture entirely; these tests are about tiers 1/2."""
    monkeypatch.delenv("SHOPIFY_STORE_URL", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(worker.settings, "shopify_store_url", None, raising=False)
    monkeypatch.setattr(worker.settings, "shopify_access_token", None, raising=False)


def _legacy_disconnected_store(merchant_id: str) -> dict:
    """Exactly the shape services/merchant_store_service.py's legacy leg builds
    when merchant_onboarding.mcp_platform is set but mcp_connected is false."""
    return {
        "store_id": f"legacy_{merchant_id}",
        "merchant_id": merchant_id,
        "platform": "shopify",
        "domain": "detached-store.myshopify.com",
        "api_key_raw": "shpat_still_live_after_detach",
        "api_key": "shpat_still_live_after_detach",
        "status": "disconnected",
        "source": "legacy_mcp",
    }


async def test_tier_two_skips_a_store_labelled_disconnected(monkeypatch):
    """The resolver must honour the label the store service put on it.

    The token double RETURNS a live token rather than raising. A raising
    double is vacuous here: tier 2 wraps its loop in `except Exception`, so an
    AssertionError from inside it is swallowed and the resolver falls through
    to the same empty result the assertion expects — the test stays green with
    the status skip deleted. (Found by exactly that mutant.) Returning a token
    means an ignored label produces a visible wrong answer instead.
    """
    import services.merchant_store_service as store_service

    async def _no_credential(merchant_id, connector):
        return None

    async def _legacy_only(merchant_id):
        return [_legacy_disconnected_store(merchant_id)]

    async def _token_resolves(**kwargs):
        return "shpat_still_live_after_detach", None

    monkeypatch.setattr(worker, "get_latest_connector_credential_for_merchant", _no_credential)
    monkeypatch.setattr(store_service, "get_merchant_active_stores", _legacy_only)
    monkeypatch.setattr(worker, "resolve_shopify_admin_access_token", _token_resolves)

    cfg = await worker._get_shopify_config_for_merchant(
        "merch_detached", allow_global_fallback=False
    )

    assert cfg == {"shop_domain": "", "access_token": ""}, (
        f"tier 2 resolved credentials for a store labelled 'disconnected': {cfg}"
    )


async def test_a_detached_merchant_with_surviving_credentials_is_not_imported(monkeypatch):
    """The delivering gate. Tier 1 WOULD succeed here — the connector_credentials
    row is intact and decrypts — and that is precisely the case the enqueue
    precondition must refuse: a merchant with credentials but no connected store
    is a merchant who detached. Proves no Shopify fetch is attempted and the
    outcome is a retryable refusal, not an import."""
    import services.merchant_store_service as store_service

    async def _surviving_credential(merchant_id, connector):
        return {"id": 1, "credentials_encrypted": "irrelevant-because-gated-first"}

    def _decrypts(_blob):  # pragma: no cover - must not be reached
        raise AssertionError("credentials were resolved for a detached merchant")

    async def _legacy_only(merchant_id):
        return [_legacy_disconnected_store(merchant_id)]

    fetched = []

    async def _record_and_explode(*args, **kwargs):
        fetched.append(kwargs or args)
        raise AssertionError("the import reached Shopify for a detached merchant")

    monkeypatch.setattr(worker, "get_latest_connector_credential_for_merchant", _surviving_credential)
    monkeypatch.setattr(worker.crypto_service, "decrypt_json_secret", _decrypts)
    monkeypatch.setattr(store_service, "get_merchant_active_stores", _legacy_only)
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
            "id": 11,
            "merchant_id": "merch_detached",
            "source_type": "connector",
            "connector": "shopify",
            "attempt": 1,
            "counts": {},
        }
    )

    assert fetched == [], f"a detached merchant's import reached Shopify: {fetched}"
    assert result["status"] == "retry_scheduled"
    assert "not connected" in recorded["error"]
    assert recorded["counts"]["error_category"] == "credentials_unavailable"


async def test_a_connected_merchant_still_passes_the_gate(monkeypatch):
    """Positive counterpart, so the gate is not simply refusing everyone. A store
    with status 'active' must reach credential resolution — proven by the
    resolver being invoked, which the detached test proves it is NOT."""
    import services.merchant_store_service as store_service

    async def _active(merchant_id):
        return [{**_legacy_disconnected_store(merchant_id), "status": "active"}]

    reached = []

    async def _resolver_reached(merchant_id, *, allow_global_fallback=True):
        reached.append(merchant_id)
        return {"shop_domain": "", "access_token": ""}  # stop here; gate is the subject

    async def _fake_retry(task_id, error, counts=None, next_run_at=None):
        return True

    monkeypatch.setattr(store_service, "get_merchant_active_stores", _active)
    monkeypatch.setattr(worker, "_get_shopify_config_for_merchant", _resolver_reached)
    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _fake_retry)

    await worker._process_import_task_record(
        {
            "id": 12,
            "merchant_id": "merch_connected",
            "source_type": "connector",
            "connector": "shopify",
            "attempt": 1,
            "counts": {},
        }
    )

    assert reached == ["merch_connected"], "an active store must reach the resolver"


async def test_a_continuation_sweeps_against_the_first_runs_start(monkeypatch):
    """Drive a real two-run pagination through _process_import_task_record and
    spy on the completion sweep's `started_at` bind.

    Run 1 fetches one page with a cursor and stops on SHOPIFY_MAX_PAGES_PER_RUN=1
    -> retry_scheduled, carrying the cursor AND its own start in `counts`.
    Run 2 is fed run 1's counts, fetches an empty page (catalog exhausted),
    completes, and sweeps. The sweep must bind RUN 1's start — not its own —
    or it expires every row run 1 imported.

    No DB row is needed: the payload builder is made to raise, which the loop
    swallows into `failed` while still counting the page and carrying the
    cursor. That keeps the test on the delivering line (the bind value) without
    faking the whole product pipeline. The spy short-circuits the sweep's
    execute, because the real SQL uses NOW(), which sqlite lacks.
    """
    import services.merchant_store_service as store_service

    monkeypatch.setenv("SHOPIFY_MAX_PAGES_PER_RUN", "1")

    async def _active(merchant_id):
        return [{**_legacy_disconnected_store(merchant_id), "status": "active"}]

    async def _cfg(merchant_id, *, allow_global_fallback=True):
        return {"shop_domain": "m.myshopify.com", "access_token": "shpat_x"}

    async def _no_currency(**kwargs):
        return None

    async def _touch(**kwargs):
        return 0

    async def _noop(*args, **kwargs):
        return True

    def _build_raises(**kwargs):
        raise RuntimeError("payload build deliberately fails; page still counts")

    pages = []

    async def _fetch(*, shop_domain, access_token, limit, page_info=None):
        pages.append(page_info)
        if page_info is None:
            return [{"id": 1}], "CURSOR_PAGE_2"
        return [], None

    sweep_binds = []
    real_execute = worker.database.execute

    async def _spy_execute(query, values=None, *args, **kwargs):
        if isinstance(query, str) and "UPDATE products_cache" in query:
            sweep_binds.append(dict(values or {}))
            return 0
        return await real_execute(query, values, *args, **kwargs)

    monkeypatch.setattr(store_service, "get_merchant_active_stores", _active)
    monkeypatch.setattr(worker, "_get_shopify_config_for_merchant", _cfg)
    monkeypatch.setattr(worker.ShopifyProductAdapter, "fetch_shop_currency", _no_currency)
    monkeypatch.setattr(worker, "touch_products_cache_ttl", _touch)
    monkeypatch.setattr(worker, "update_import_task_status", _noop)
    monkeypatch.setattr(worker, "_best_effort_update_store_product_count", _noop)
    monkeypatch.setattr(worker, "mark_import_task_retry_scheduled", _noop)
    monkeypatch.setattr(worker, "mark_import_task_succeeded", _noop)
    monkeypatch.setattr(worker, "_build_shopify_cache_payload", _build_raises)
    monkeypatch.setattr(worker, "_fetch_shopify_products_page", _fetch)
    monkeypatch.setattr(worker.database, "execute", _spy_execute)

    base = {
        "id": 21,
        "merchant_id": "merch_big_catalog",
        "source_type": "connector",
        "connector": "shopify",
        "attempt": 1,
    }

    run1 = await worker._process_import_task_record({**base, "counts": {}})

    assert run1["status"] == "retry_scheduled", run1
    assert run1["counts"]["shopify_next_page_info"] == "CURSOR_PAGE_2"
    first_start = run1["counts"]["full_sync_started_at"]
    assert first_start, "run 1 must record the logical sync start"
    assert sweep_binds == [], "run 1 did not complete; it must not sweep"

    run2 = await worker._process_import_task_record({**base, "counts": dict(run1["counts"])})

    assert pages == [None, "CURSOR_PAGE_2"], "run 2 must resume from run 1's cursor"
    assert run2["status"] == "succeeded", run2
    assert len(sweep_binds) == 1, "exactly one completion sweep"
    assert sweep_binds[0]["started_at"] == datetime.fromisoformat(first_start), (
        "the sweep must expire rows older than RUN 1's start; binding run 2's "
        "start expires everything run 1 just imported"
    )
    assert "full_sync_started_at" not in run2["counts"], (
        "the logical sync is over; its start must not leak into a future run"
    )
