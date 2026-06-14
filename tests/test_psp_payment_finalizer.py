from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_finalize_refund_success_cancels_unfulfilled_fulfillment_status() -> None:
    from services.psp_payment_finalizer import finalize_refund_success

    status_updates = []

    async def fake_update_order_status(order_id: str, status: str, **kwargs):
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs):
        return None

    result = await finalize_refund_success(
        {
            "order_id": "ORD_FINALIZER_REFUND",
            "merchant_id": "merch_1",
            "status": "paid",
            "payment_status": "paid",
            "fulfillment_status": "processing",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        },
        psp="stripe",
        refund_reference="re_full",
        refund_amount="20.00",
        currency="USD",
        update_order_status_fn=fake_update_order_status,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["next_status"] == "refunded"
    assert status_updates[0]["status"] == "refunded"
    assert status_updates[0]["payment_status"] == "refunded"
    assert status_updates[0]["fulfillment_status"] == "cancelled"


@pytest.mark.asyncio
async def test_finalize_refund_success_does_not_overwrite_fulfilled_status() -> None:
    from services.psp_payment_finalizer import finalize_refund_success

    status_updates = []

    async def fake_update_order_status(order_id: str, status: str, **kwargs):
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs):
        return None

    await finalize_refund_success(
        {
            "order_id": "ORD_FINALIZER_SHIPPED_REFUND",
            "merchant_id": "merch_1",
            "status": "paid",
            "payment_status": "paid",
            "fulfillment_status": "shipped",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "metadata": {},
        },
        psp="stripe",
        refund_reference="re_full_shipped",
        refund_amount="20.00",
        currency="USD",
        update_order_status_fn=fake_update_order_status,
        log_order_event_fn=fake_log_order_event,
    )

    assert "fulfillment_status" not in status_updates[0]


@pytest.mark.asyncio
async def test_finalize_refund_failure_logs_without_mutating_when_no_rollback() -> None:
    from services.psp_payment_finalizer import finalize_refund_failure

    status_updates = []
    order_events = []

    async def fake_update_order_status(order_id: str, status: str, **kwargs):
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs):
        order_events.append(kwargs)

    result = await finalize_refund_failure(
        {
            "order_id": "ORD_FINALIZER_1",
            "merchant_id": "merch_1",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total": "20.00",
            "total_refunded": "12.00",
            "metadata": {},
        },
        psp="stripe",
        refund_reference="re_missing",
        failure_reason="failed",
        rollback_reference=None,
        rollback_amount=None,
        update_order_status_fn=fake_update_order_status,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["applied"] is True
    assert result["rolled_back"] is False
    assert status_updates == []
    assert len(order_events) == 1
    assert order_events[0]["event_type"] == "refund_failed_webhook"
    assert order_events[0]["metadata"]["rollback_applied"] is False


@pytest.mark.asyncio
async def test_finalize_refund_failure_rolls_back_from_explicit_amount_without_record() -> None:
    from services.psp_payment_finalizer import finalize_refund_failure

    status_updates = []

    async def fake_update_order_status(order_id: str, status: str, **kwargs):
        status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_log_order_event(**kwargs):
        return None

    result = await finalize_refund_failure(
        {
            "order_id": "ORD_FINALIZER_2",
            "merchant_id": "merch_1",
            "status": "partially_refunded",
            "payment_status": "partially_refunded",
            "total": "20.00",
            "total_refunded": "12.00",
            "metadata": {"refund_id": "re_123"},
        },
        psp="stripe",
        refund_reference="re_123",
        failure_reason="failed",
        rollback_reference="re_123",
        rollback_amount="12.00",
        update_order_status_fn=fake_update_order_status,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["rolled_back"] is True
    assert result["next_status"] == "paid"
    assert result["total_refunded"] == Decimal("0")
    assert status_updates[0]["status"] == "paid"
    assert str(status_updates[0]["total_refunded"]) == "0.00"


@pytest.mark.asyncio
async def test_finalize_payment_success_surfaces_funnel_event_ids() -> None:
    """The paid transition surfaces the conversion log_order_event's funnel_event_ids
    so the webhook can join the settled sale back to its decision."""
    from services.psp_payment_finalizer import finalize_payment_success

    async def fake_mark_order_paid(order_id: str):
        return True

    async def fake_log_order_event(**kwargs):
        # mirrors db.products.log_order_event → record_from_order_event return shape
        return ["funnel-evt-1"]

    result = await finalize_payment_success(
        {
            "order_id": "ORD_FINALIZER_PAID",
            "merchant_id": "merch_1",
            "status": "awaiting_payment",
            "payment_status": "awaiting_payment",
            "currency": "USD",
            "subtotal": "10.00",
            "metadata": {"decision_layer": {"decision_id": "dec-1"}},
        },
        psp="stripe",
        payment_reference="pi_123",
        transaction_id="pi_123",
        amount_minor=1000,
        currency="USD",
        mark_order_paid_fn=fake_mark_order_paid,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["applied"] is True
    assert result["funnel_event_ids"] == ["funnel-evt-1"]


@pytest.mark.asyncio
async def test_finalize_payment_success_funnel_event_ids_defaults_empty() -> None:
    """A None-returning log hook (e.g. no merchant_id) yields [] not None — callers
    iterate it safely."""
    from services.psp_payment_finalizer import finalize_payment_success

    async def fake_mark_order_paid(order_id: str):
        return True

    async def fake_log_order_event(**kwargs):
        return None

    result = await finalize_payment_success(
        {
            "order_id": "ORD_FINALIZER_PAID_2",
            "merchant_id": "merch_1",
            "status": "awaiting_payment",
            "payment_status": "awaiting_payment",
            "currency": "USD",
            "metadata": {},
        },
        psp="stripe",
        payment_reference="pi_456",
        mark_order_paid_fn=fake_mark_order_paid,
        log_order_event_fn=fake_log_order_event,
    )

    assert result["applied"] is True
    assert result["funnel_event_ids"] == []
