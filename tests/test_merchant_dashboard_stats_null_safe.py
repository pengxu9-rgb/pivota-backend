import pytest


@pytest.mark.asyncio
async def test_get_merchant_analytics_handles_null_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_dashboard_routes as module

    async def fake_fetch_one(query, values=None):
        q = str(query)
        if "total_orders_all_time" in q:
            return {
                "total_orders_all_time": 0,
                "gmv_all_time": 0,
                "confirmed_revenue_all_time": 0,
                "paid_orders_all_time": None,
                "avg_order_value_all_time": 0,
                "total_customers": 0,
                "orders_last_30_days": 0,
                "gmv_last_30_days": 0,
                "paid_orders_last_30_days": None,
                "confirmed_revenue_last_30_days": 0,
            }
        if "orders_prev_30" in q:
            return {
                "orders_prev_30": 0,
                "gmv_prev_30": 0,
                "paid_orders_prev_30": None,
                "confirmed_revenue_prev_30": 0,
            }
        if "FROM products_cache" in q:
            return {"count": 0}
        return None

    async def fake_fetch_all(query, values=None):
        return []

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    response = await module.get_merchant_analytics(
        merchant_id="merch_test_nulls",
        current_user={"role": "merchant"},
    )

    assert response["status"] == "success"
    assert response["data"]["total_orders"] == 0
    assert response["data"]["total_payments_succeeded"] == 0
    assert response["data"]["payment_success_rate"] == 0.0
    assert response["data"]["average_order_value"] == 0.0
    assert "error" not in response["data"]
