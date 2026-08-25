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

from collections import Counter
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


@pytest.mark.asyncio
async def test_disconnected_legacy_mcp_store_does_not_keep_the_catalog_alive(monkeypatch):
    """The app-uninstall / auto-disconnect path.

    `get_primary_store`'s legacy leg appends a store dict whenever
    `merchant_onboarding.mcp_platform` is truthy, merely LABELLING it
    `status='disconnected'` when `mcp_connected` is false. The Shopify
    `app/uninstalled` webhook and the hourly auto-disconnect job both set
    `merchant_stores.status='disconnected'` without touching any `mcp_*`
    column, so `mcp_platform` stays set forever and the legacy leg keeps
    answering with a non-None store.

    A gate written as `store is not None` therefore passes for exactly the two
    detach paths a merchant is most likely to hit. Only the portal's own DELETE
    route clears `mcp_platform`, which is why the reported bug looked fixed.
    """
    fixture = load_real_merchant_fixture()
    disconnected_legacy_store = {
        "store_id": f"legacy_{ALPHA_MERCHANT_ID}",
        "merchant_id": ALPHA_MERCHANT_ID,
        "platform": "shopify",
        "domain": fixture["store"]["domain"],
        # What the legacy leg produces once mcp_connected has been set FALSE.
        "status": "disconnected",
        "source": "legacy_mcp",
    }

    dataset, cache_calls, live_calls = await _load(
        monkeypatch,
        store=disconnected_legacy_store,
        shopify_config=fixture["shopify_config"],
    )

    assert dataset.products == []
    assert cache_calls == []
    assert live_calls == []
    assert "store_connection_missing" in dataset.merchant_blockers


@pytest.mark.asyncio
async def test_store_with_an_unexpected_status_is_not_treated_as_attached(monkeypatch):
    """Allowlist, not denylist: only 'active'/'connected' count as attached."""
    fixture = load_real_merchant_fixture()
    for status in ("deleted", "archived", "inactive", "", None, "pending"):
        store = dict(fixture["store"])
        store["status"] = status
        dataset, cache_calls, _ = await _load(
            monkeypatch, store=store, shopify_config=fixture["shopify_config"]
        )
        assert dataset.products == [], f"status={status!r} must not keep the catalog alive"
        assert cache_calls == [], f"status={status!r} must not read the cache"


@pytest.mark.asyncio
async def test_connected_status_is_also_accepted(monkeypatch):
    """The mutant guard for the allowlist: 'connected' is a real live status in
    merchant_stores, and dropping it would blank live merchants."""
    fixture = load_real_merchant_fixture()
    for status in ("active", "connected", "ACTIVE", "  Connected  "):
        store = dict(fixture["store"])
        store["status"] = status
        dataset, cache_calls, _ = await _load(
            monkeypatch, store=store, shopify_config=fixture["shopify_config"]
        )
        assert len(dataset.products) > 0, f"status={status!r} is live and must be read"
        assert cache_calls == [ALPHA_MERCHANT_ID]


def test_a_store_less_merchant_is_never_told_they_are_ready():
    """With no storefront there are no products, so every product-shaped branch
    of `_recommended_actions` is silent. It used to fall through to
    "This merchant is ready for supervised LLM commerce." — rendered on the
    overview card beside a summary saying the merchant is blocked."""
    from readiness.summary import _recommended_actions

    actions = _recommended_actions(
        assessment_state="assessed",
        blocker_counts=Counter(),
        blockers=["store_connection_missing"],
        warnings=[],
        blocked_variant_count=0,
        capability_status={},
    )

    assert actions, "a blocked merchant must get at least one action"
    assert not any("ready for supervised LLM commerce" in a for a in actions), actions
    assert any("Reconnect your store" in a for a in actions), actions


@pytest.mark.asyncio
async def test_detached_merchant_card_never_reads_ready(monkeypatch):
    """Assert on the RENDERED summary, not on `merchant_blockers`.

    Every other test here checks the dataset. That is one layer below what the
    merchant actually looks at, and it let a regression through: with no
    products, every product-shaped branch of `_recommended_actions` was silent
    and the overview card showed "This merchant is ready for supervised LLM
    commerce." directly beside a summary saying the merchant was blocked.
    """
    from readiness.scoring import build_merchant_snapshot
    from readiness.summary import summarize_readiness_snapshot

    fixture = load_real_merchant_fixture()
    dataset, _, _ = await _load(
        monkeypatch, store=None, shopify_config=fixture["shopify_config"]
    )
    summary = summarize_readiness_snapshot(build_merchant_snapshot(dataset))

    blob = " ".join(
        [summary.action_text or "", summary.next_action or "", summary.summary_text or ""]
    )
    assert "ready for supervised LLM commerce" not in blob, blob
    assert "can be enabled for supervised LLM commerce" not in blob, blob
    assert "Reconnect" in blob, blob

    # A merchant with no storefront cannot execute checkout or write orders back.
    assert summary.capability_status.get("merchant_adapter") == "blocked"
    assert summary.capability_status.get("order_sync") == "blocked"


@pytest.mark.asyncio
async def test_connected_merchant_card_still_reports_capabilities_ready(monkeypatch):
    """Mutant guard for the capability gating: it must not blanket-block."""
    from readiness.scoring import build_merchant_snapshot
    from readiness.summary import summarize_readiness_snapshot

    fixture = load_real_merchant_fixture()
    dataset, _, _ = await _load(
        monkeypatch, store=fixture["store"], shopify_config=fixture["shopify_config"]
    )
    summary = summarize_readiness_snapshot(build_merchant_snapshot(dataset))

    assert summary.capability_status.get("merchant_adapter") == "ready"
    assert summary.capability_status.get("order_sync") == "ready"


def test_detach_route_invalidates_the_readiness_caches():
    """The invalidation hunk in routes/manage_integrations.py had no coverage.

    Asserts the route imports the real invalidator and that it clears BOTH the
    optimization cache and the snapshot cache underneath it — the specific claim
    the route's comment makes.
    """
    from readiness import service as readiness_service
    from readiness import summary as readiness_summary
    from routes import manage_integrations

    assert (
        manage_integrations.invalidate_readiness_optimization_cache
        is readiness_summary.invalidate_readiness_optimization_cache
    ), "the route must call the real invalidator, not a shadowed name"

    merchant = "merch_cache_probe"
    readiness_summary._OPTIMIZATION_CACHE[f"{merchant}|ucp"] = ("payload", 0.0)
    readiness_service._SNAPSHOT_CACHE[f"{merchant}|ucp"] = ("snapshot", 0.0)
    readiness_summary._OPTIMIZATION_CACHE["other|ucp"] = ("payload", 0.0)
    readiness_service._SNAPSHOT_CACHE["other|ucp"] = ("snapshot", 0.0)

    try:
        manage_integrations.invalidate_readiness_optimization_cache(merchant)

        assert f"{merchant}|ucp" not in readiness_summary._OPTIMIZATION_CACHE
        assert f"{merchant}|ucp" not in readiness_service._SNAPSHOT_CACHE, (
            "the route comment claims the snapshot cache is cleared too"
        )
        # Scoped to one merchant — it must not flush the whole fleet's caches.
        assert "other|ucp" in readiness_summary._OPTIMIZATION_CACHE
        assert "other|ucp" in readiness_service._SNAPSHOT_CACHE
    finally:
        readiness_summary._OPTIMIZATION_CACHE.pop("other|ucp", None)
        readiness_service._SNAPSHOT_CACHE.pop("other|ucp", None)


def test_shopify_configuration_missing_is_humanized():
    """Mutant guard: the humanize entry added alongside the bucket mapping."""
    assert _humanize_code("shopify_configuration_missing") == "Store credentials missing"


def test_detach_route_body_actually_calls_the_invalidator():
    """The test above proves the invalidator WORKS. This proves the route CALLS it.

    Needed because they fail independently: replacing the call in `delete_store`
    with `pass` left the whole readiness suite green, since an identity check on
    the imported name still passed. A source gate rather than a spy, because
    driving the route needs the merchant_stores fixture DB that lives in
    tests/test_store_lifecycle_reconciliation.py, and a call-count spy would not
    prove the cache actually cleared — which the companion test does.
    """
    import inspect

    from routes import manage_integrations

    body = inspect.getsource(manage_integrations.delete_store)
    assert "invalidate_readiness_optimization_cache(merchant_id)" in body, (
        "DELETE /merchant/integrations/store/{store_id} must invalidate the "
        "readiness caches, or the overview serves pre-detach counts for the "
        "full 300s TTL"
    )
