from __future__ import annotations

from services.platform_order_writeback_readiness import (
    is_store_order_writeback_allowed,
    normalize_store_order_writeback_readiness,
    store_order_writeback_context,
)


def test_store_order_writeback_defaults_disabled() -> None:
    store = normalize_store_order_writeback_readiness(
        {"store_id": "store_1", "platform": "wix", "status": "active"}
    )

    assert store["order_writeback_status"] == "disabled"
    assert is_store_order_writeback_allowed(store, platform="wix", order_id="ord_1") is False


def test_store_order_writeback_enabled_requires_active_expected_platform() -> None:
    assert is_store_order_writeback_allowed(
        {
            "store_id": "store_1",
            "platform": "wix",
            "status": "active",
            "order_writeback_status": "enabled",
        },
        platform="wix",
        order_id="ord_1",
    ) is True

    inactive = store_order_writeback_context(
        {
            "store_id": "store_1",
            "platform": "wix",
            "status": "inactive",
            "order_writeback_status": "enabled",
        },
        platform="wix",
        order_id="ord_1",
    )
    assert inactive["allowed"] is False
    assert inactive["blocker"] == "store_inactive"


def test_store_order_writeback_canary_matches_one_order() -> None:
    store = {
        "store_id": "store_1",
        "platform": "wix",
        "status": "active",
        "order_writeback_status": "canary",
        "order_writeback_canary_order_id": "ord_allowed",
    }

    assert is_store_order_writeback_allowed(store, platform="wix", order_id="ord_allowed") is True
    mismatch = store_order_writeback_context(store, platform="wix", order_id="ord_other")
    assert mismatch["allowed"] is False
    assert mismatch["blocker"] == "canary_order_mismatch"
