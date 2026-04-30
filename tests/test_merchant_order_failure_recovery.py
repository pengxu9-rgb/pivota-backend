from __future__ import annotations

from typing import Any, Dict

import pytest


@pytest.mark.asyncio
async def test_paid_order_missing_merchant_order_failure_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    order: Dict[str, Any] = {
        "order_id": "ord_paid_missing_merchant",
        "merchant_id": "merch_1",
        "payment_status": "paid",
        "shopify_order_id": None,
        "metadata": {},
        "total": 12.34,
        "currency": "USD",
    }
    updates: list[Dict[str, Any]] = []
    events: list[Dict[str, Any]] = []

    async def fake_get_order(order_id: str):
        assert order_id == "ord_paid_missing_merchant"
        return dict(order)

    async def fake_get_primary_store(_merchant_id: str):
        return None

    async def fake_get_store_by_id(*args: Any, **kwargs: Any):
        return None

    async def fake_update_order_row(order_id: str, fields: Dict[str, Any]):
        updates.append({"order_id": order_id, "fields": fields})
        return None

    async def fake_log_order_event(**kwargs: Any):
        events.append(kwargs)
        return None

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "get_store_by_id", fake_get_store_by_id)
    monkeypatch.setattr(module, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    ok = await module.sync_order_to_connected_store("ord_paid_missing_merchant")

    assert ok is False
    assert updates
    merchant_order = updates[-1]["fields"]["metadata"]["merchant_order"]
    assert merchant_order["status"] == "paid_merchant_order_failed"
    assert merchant_order["requires_action"] == "requires_refund_or_retry"
    assert merchant_order["retryable"] is True
    payment_recovery = updates[-1]["fields"]["metadata"]["payment_recovery"]
    assert payment_recovery["refund_required"] is True
    assert payment_recovery["operator_action"] == "retry_merchant_order_or_issue_refund"
    assert events[-1]["event_type"] == "merchant_order_sync_failed"


@pytest.mark.asyncio
async def test_paid_merchant_order_failures_are_queryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    rows = [
        {
            "order_id": "ord_failed",
            "merchant_id": "merch_1",
            "status": "pending",
            "payment_status": "paid",
            "fulfillment_status": None,
            "shopify_order_id": None,
            "store_id": "store_1",
            "total": "25.00",
            "currency": "USD",
            "payment_intent_id": "pi_failed",
            "psp_used": "stripe",
            "created_at": None,
            "paid_at": None,
            "metadata": {
                "merchant_order": {
                    "status": "paid_merchant_order_failed",
                    "retryable": True,
                    "retry_count": 2,
                    "last_error": "Shopify 500",
                },
                "payment_recovery": {
                    "refund_required": True,
                    "operator_action": "retry_merchant_order_or_issue_refund",
                },
            },
        },
        {
            "order_id": "ord_pending_missing",
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": None,
            "metadata": {},
        },
    ]

    async def fake_fetch_all(*_args: Any, **_kwargs: Any):
        return rows

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    result = await module.list_paid_merchant_order_failures(
        merchant_id="merch_1",
        limit=10,
        include_all_paid_missing=False,
    )

    assert result["count"] == 1
    assert result["orders"][0]["order_id"] == "ord_failed"
    assert result["orders"][0]["merchant_order"]["status"] == "paid_merchant_order_failed"
    assert result["orders"][0]["payment_recovery"]["refund_required"] is True


@pytest.mark.asyncio
async def test_retry_merchant_order_skips_already_linked_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    calls = {"sync": 0}

    async def fake_get_order(order_id: str):
        assert order_id == "ord_linked"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": "shop_123",
            "metadata": {},
        }

    async def fake_sync(_order_id: str):
        calls["sync"] += 1
        return True

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "sync_order_to_connected_store", fake_sync)

    result = await module.retry_paid_merchant_order_failure("ord_linked")

    assert result["status"] == "already_linked"
    assert result["linked_merchant_order"]["platform_order_id"] == "shop_123"
    assert calls["sync"] == 0


@pytest.mark.asyncio
async def test_retry_merchant_order_success_is_idempotent_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    calls = {"sync": 0}
    events: list[Dict[str, Any]] = []

    async def fake_get_order(order_id: str):
        assert order_id == "ord_retry_success"
        if calls["sync"] == 0:
            return {
                "order_id": order_id,
                "merchant_id": "merch_1",
                "payment_status": "paid",
                "shopify_order_id": None,
                "total": 10,
                "currency": "USD",
                "metadata": {
                    "merchant_order": {
                        "status": "paid_merchant_order_failed",
                        "retryable": True,
                    }
                },
            }
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": "shop_456",
            "total": 10,
            "currency": "USD",
            "metadata": {
                "merchant_order": {
                    "status": "merchant_order_created",
                    "platform": "shopify",
                    "platform_order_id": "shop_456",
                }
            },
        }

    async def fake_sync(order_id: str):
        assert order_id == "ord_retry_success"
        calls["sync"] += 1
        return True

    async def fake_log_order_event(**kwargs: Any):
        events.append(kwargs)

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "sync_order_to_connected_store", fake_sync)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    result = await module.retry_paid_merchant_order_failure("ord_retry_success")

    assert result["status"] == "success"
    assert result["linked_merchant_order"]["platform_order_id"] == "shop_456"
    assert calls["sync"] == 1
    assert events[-1]["event_type"] == "merchant_order_retry_success"


@pytest.mark.asyncio
async def test_retry_merchant_order_failure_remains_visible_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    events: list[Dict[str, Any]] = []

    async def fake_get_order(order_id: str):
        assert order_id == "ord_retry_failed"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": None,
            "total": 10,
            "currency": "USD",
            "metadata": {
                "merchant_order": {
                    "status": "paid_merchant_order_failed",
                    "retryable": True,
                    "retry_count": 3,
                    "last_error": "still unavailable",
                },
                "payment_recovery": {
                    "refund_required": True,
                    "operator_action": "retry_merchant_order_or_issue_refund",
                },
            },
        }

    async def fake_sync(order_id: str):
        assert order_id == "ord_retry_failed"
        return False

    async def fake_log_order_event(**kwargs: Any):
        events.append(kwargs)

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "sync_order_to_connected_store", fake_sync)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    result = await module.retry_paid_merchant_order_failure("ord_retry_failed")

    assert result["status"] == "failed"
    assert result["order"]["merchant_order"]["status"] == "paid_merchant_order_failed"
    assert result["order"]["payment_recovery"]["refund_required"] is True
    assert events[-1]["event_type"] == "merchant_order_retry_failed"


@pytest.mark.asyncio
async def test_transaction_safety_metrics_expose_required_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    async def fake_fetch_paid_orders_missing_merchant_order(*_args: Any, **_kwargs: Any):
        return [
            {
                "order_id": "ord_failed",
                "merchant_id": "merch_1",
                "payment_status": "paid",
                "shopify_order_id": None,
                "metadata": {
                    "merchant_order": {"status": "paid_merchant_order_failed"},
                },
            }
        ]

    async def fake_fetch_one(query: Any, values: Dict[str, Any]):
        assert isinstance(query, str)
        sql = str(query)
        if "webhook_events" in sql and values.get("status") == "duplicate":
            return {"count": 7}
        if "webhook_events" in sql and values.get("status") == "failed":
            return {"count": 2}
        event_counts = {
            "merchant_order_retry_success": 3,
            "merchant_order_retry_failed": 1,
            "quote_revalidation_failed": 4,
            "reconciliation_drift_detected": 5,
            "payment_authorized": 6,
            "payment_captured_after_merchant_order": 8,
            "payment_capture_failed": 2,
            "payment_authorization_void_failed": 1,
            "fallback_pollution_attempt": 9,
        }
        return {"count": event_counts.get(values.get("event_type"), 0)}

    monkeypatch.setattr(module, "IS_POSTGRES", False)
    monkeypatch.setattr(
        module,
        "_fetch_paid_orders_missing_merchant_order",
        fake_fetch_paid_orders_missing_merchant_order,
    )
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    result = await module.get_transaction_safety_metrics(merchant_id="merch_1")

    metrics = result["metrics"]
    assert metrics["paid_merchant_order_failed_count"]["count"] == 1
    assert metrics["merchant_order_retry_success_count"]["count"] == 3
    assert metrics["merchant_order_retry_failed_count"]["count"] == 1
    assert metrics["quote_revalidation_failure_count"]["count"] == 4
    assert metrics["reconciliation_drift_count"]["count"] == 5
    assert metrics["payment_authorized_count"]["count"] == 6
    assert metrics["payment_captured_after_merchant_order_count"]["count"] == 8
    assert metrics["payment_capture_failed_count"]["count"] == 2
    assert metrics["payment_authorization_void_failed_count"]["count"] == 1
    assert metrics["webhook_duplicate_count"]["count"] == 7
    assert metrics["webhook_failed_count"]["count"] == 2
    assert metrics["fallback_pollution_attempt_count"]["count"] == 9


@pytest.mark.asyncio
async def test_refund_paid_merchant_order_failure_is_idempotent_and_updates_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module
    import services.refund_service as refund_module

    updates: list[Dict[str, Any]] = []
    events: list[Dict[str, Any]] = []
    refund_calls: list[Dict[str, Any]] = []

    order: Dict[str, Any] = {
        "order_id": "ord_refund_failed_writeback",
        "merchant_id": "merch_1",
        "payment_status": "paid",
        "shopify_order_id": None,
        "total": "25.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {
            "merchant_order": {
                "status": "paid_merchant_order_failed",
                "retryable": True,
            },
            "payment_recovery": {
                "status": "requires_operator_action",
                "refund_required": True,
            },
        },
    }

    async def fake_get_order(order_id: str):
        assert order_id == "ord_refund_failed_writeback"
        return dict(order)

    async def fake_update_order_row(order_id: str, fields: Dict[str, Any]):
        assert order_id == "ord_refund_failed_writeback"
        updates.append(fields)
        order["metadata"] = fields["metadata"]
        return True

    async def fake_create_refund(**kwargs: Any):
        refund_calls.append(kwargs)
        return {
            "status": "success",
            "refund_id": "REF_1",
            "psp_refund_id": "re_1",
        }

    async def fake_log_order_event(**kwargs: Any):
        events.append(kwargs)

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "update_order_row", fake_update_order_row)
    monkeypatch.setattr(refund_module.refund_service, "create_refund", fake_create_refund)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)

    result = await module.refund_paid_merchant_order_failure("ord_refund_failed_writeback")

    assert result["status"] == "success"
    assert refund_calls[0]["amount"] == 25.0
    assert refund_calls[0]["idempotency_key"] == "merchant_order_failure_refund:ord_refund_failed_writeback"
    recovery = updates[-1]["metadata"]["payment_recovery"]
    assert recovery["status"] == "refund_completed"
    assert recovery["refund_required"] is False
    assert recovery["psp_refund_id"] == "re_1"
    assert events[-1]["event_type"] == "merchant_order_failure_refund_succeeded"


@pytest.mark.asyncio
async def test_refund_paid_merchant_order_failure_skips_completed_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module
    import services.refund_service as refund_module

    calls = {"refund": 0}

    async def fake_get_order(order_id: str):
        assert order_id == "ord_already_refunded"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": None,
            "total": "25.00",
            "total_refunded": "25.00",
            "currency": "USD",
            "metadata": {
                "merchant_order": {
                    "status": "paid_merchant_order_failed",
                },
                "payment_recovery": {
                    "status": "refund_completed",
                    "refund_required": False,
                    "refund_id": "REF_EXISTING",
                },
            },
        }

    async def fake_create_refund(**_kwargs: Any):
        calls["refund"] += 1
        return {"status": "success"}

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(refund_module.refund_service, "create_refund", fake_create_refund)

    result = await module.refund_paid_merchant_order_failure("ord_already_refunded")

    assert result["status"] == "already_refunded"
    assert result["payment_recovery"]["refund_id"] == "REF_EXISTING"
    assert calls["refund"] == 0


@pytest.mark.asyncio
async def test_refund_paid_merchant_order_failure_blocks_linked_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    async def fake_get_order(order_id: str):
        assert order_id == "ord_linked"
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "shopify_order_id": "shop_123",
            "metadata": {
                "merchant_order": {
                    "status": "merchant_order_created",
                    "platform_order_id": "shop_123",
                }
            },
        }

    monkeypatch.setattr(module, "get_order", fake_get_order)

    with pytest.raises(Exception) as exc_info:
        await module.refund_paid_merchant_order_failure("ord_linked")

    assert getattr(exc_info.value, "status_code", None) == 409
