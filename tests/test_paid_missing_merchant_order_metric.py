"""The paging metric must see orders whose sync task never ran.

`paid_merchant_order_failed_count` requires a `paid_merchant_order_failed`
marker, and that marker is only written when the merchant-order sync ATTEMPT ran
and failed. A `background_tasks.add_task` that died with the API process writes
no marker, so the marker-based count cannot see it — measured against prod on
2026-09-01, it read 4 while the real figure was 33.

`_count_paid_orders_missing_merchant_order_best_effort` is the complete count.
These tests pin the two properties that make it complete without making it
noisy: it must NOT filter on the marker, and it MUST still exclude orders that
reached a non-Shopify platform (which record `platform_order_id` in metadata and
leave `shopify_order_id` empty).
"""

import pytest

import routes.order_routes as order_routes


def _order(order_id, *, marker=None, platform_order_id=None):
    """A row shaped like `_fetch_paid_orders_missing_merchant_order` returns."""
    merchant_order = {}
    if marker:
        merchant_order["status"] = marker
    if platform_order_id:
        merchant_order["platform_order_id"] = platform_order_id
    return {
        "order_id": order_id,
        "merchant_id": "merch_test",
        "payment_status": "paid",
        "shopify_order_id": "",
        "metadata": {"merchant_order": merchant_order} if merchant_order else {},
    }


@pytest.mark.asyncio
async def test_counts_orders_with_no_marker_that_the_failed_count_misses(monkeypatch):
    """The whole point: an order whose task died leaves no marker, and must count."""
    rows = [
        _order("ORD_ATTEMPTED", marker="paid_merchant_order_failed"),
        _order("ORD_TASK_DIED"),  # dropped background task: no marker at all
    ]

    async def fake_fetch(*, merchant_id, limit):
        return rows

    monkeypatch.setattr(order_routes, "IS_POSTGRES", False)
    monkeypatch.setattr(
        order_routes, "_fetch_paid_orders_missing_merchant_order", fake_fetch
    )

    complete = await order_routes._count_paid_orders_missing_merchant_order_best_effort(
        merchant_id=None
    )
    marker_based = await order_routes._count_paid_merchant_order_failed_best_effort(
        merchant_id=None
    )

    assert complete["count"] == 2
    # Fails if the new counter is a copy of the marker-based one.
    assert marker_based["count"] == 1
    assert complete["count"] > marker_based["count"]


@pytest.mark.asyncio
async def test_excludes_orders_already_synced_to_a_non_shopify_platform(monkeypatch):
    """A Woo/Wix/BigCommerce order has an empty shopify_order_id but IS delivered."""
    rows = [
        _order("ORD_TASK_DIED"),
        _order("ORD_ON_WOO", platform_order_id="woo-8891"),
    ]

    async def fake_fetch(*, merchant_id, limit):
        return rows

    monkeypatch.setattr(order_routes, "IS_POSTGRES", False)
    monkeypatch.setattr(
        order_routes, "_fetch_paid_orders_missing_merchant_order", fake_fetch
    )

    result = await order_routes._count_paid_orders_missing_merchant_order_best_effort(
        merchant_id=None
    )

    # Counting on shopify_order_id alone would return 2 and page on a delivered order.
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_postgres_sql_drops_the_marker_and_keeps_the_platform_guard(monkeypatch):
    seen = {}

    async def fake_count_sql(sql, values):
        seen["sql"] = sql
        seen["values"] = values
        return {"count": 7, "available": True}

    monkeypatch.setattr(order_routes, "IS_POSTGRES", True)
    monkeypatch.setattr(order_routes, "_count_sql_best_effort", fake_count_sql)

    result = await order_routes._count_paid_orders_missing_merchant_order_best_effort(
        merchant_id=None
    )

    assert result == {"count": 7, "available": True}
    assert "paid_merchant_order_failed" not in seen["sql"]
    assert "platform_order_id" in seen["sql"]
    assert "payment_status = 'paid'" in seen["sql"]
    # Same bind-param discipline as the clause it builds.
    assert "merchant_id" not in seen["values"]
    assert ":merchant_id" not in seen["sql"]


@pytest.mark.asyncio
async def test_postgres_sql_binds_merchant_id_when_scoped(monkeypatch):
    seen = {}

    async def fake_count_sql(sql, values):
        seen["sql"] = sql
        seen["values"] = values
        return {"count": 2, "available": True}

    monkeypatch.setattr(order_routes, "IS_POSTGRES", True)
    monkeypatch.setattr(order_routes, "_count_sql_best_effort", fake_count_sql)

    await order_routes._count_paid_orders_missing_merchant_order_best_effort(
        merchant_id="merch_abc"
    )

    assert ":merchant_id" in seen["sql"]
    assert seen["values"]["merchant_id"] == "merch_abc"
