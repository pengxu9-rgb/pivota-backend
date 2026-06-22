from __future__ import annotations

from services.content_writeback_readiness import (
    is_store_content_writeback_allowed,
    normalize_store_content_writeback_readiness,
    store_content_writeback_context,
)


def _shop(**kw):
    base = {"store_id": "s1", "platform": "shopify", "status": "active"}
    base.update(kw)
    return base


def test_defaults_disabled():
    store = normalize_store_content_writeback_readiness(
        {"store_id": "s1", "platform": "shopify", "status": "active"}
    )
    assert store["content_writeback_status"] == "disabled"
    assert is_store_content_writeback_allowed(store, platform="shopify", product_id="p1") is False


def test_enabled_requires_active_and_matching_platform():
    assert is_store_content_writeback_allowed(
        _shop(content_writeback_status="enabled"), platform="shopify", product_id="p1"
    ) is True

    inactive = store_content_writeback_context(
        _shop(status="inactive", content_writeback_status="enabled"),
        platform="shopify", product_id="p1",
    )
    assert inactive["allowed"] is False
    assert inactive["blocker"] == "store_inactive"

    mismatch = store_content_writeback_context(
        _shop(content_writeback_status="enabled"), platform="wix", product_id="p1",
    )
    assert mismatch["allowed"] is False
    assert mismatch["blocker"] == "platform_mismatch"


def test_canary_matches_one_product_only():
    store = _shop(content_writeback_status="canary", content_writeback_canary_product_id="p_allowed")
    assert is_store_content_writeback_allowed(store, platform="shopify", product_id="p_allowed") is True

    miss = store_content_writeback_context(store, platform="shopify", product_id="p_other")
    assert miss["allowed"] is False
    assert miss["blocker"] == "canary_product_mismatch"

    no_canary = store_content_writeback_context(
        _shop(content_writeback_status="canary"), platform="shopify", product_id="p1",
    )
    assert no_canary["blocker"] == "canary_product_missing"


def test_global_kill_switch_blocks_even_enabled(monkeypatch):
    monkeypatch.setenv("DISABLE_CONTENT_WRITEBACK", "1")
    ctx = store_content_writeback_context(
        _shop(content_writeback_status="enabled"), platform="shopify", product_id="p1",
    )
    assert ctx["allowed"] is False
    assert ctx["blocker"] == "global_content_writeback_disabled"


def test_store_missing_and_unknown_status_fail_closed():
    assert store_content_writeback_context(None, platform="shopify", product_id="p1")["blocker"] == "store_missing"
    unknown = store_content_writeback_context(
        _shop(content_writeback_status="bogus"), platform="shopify", product_id="p1",
    )
    assert unknown["allowed"] is False
    assert unknown["content_writeback_status"] == "disabled"  # normalized
