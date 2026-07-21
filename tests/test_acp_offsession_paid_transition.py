"""P-T2.3.3b — off-session capture transitions the order to PAID.

Exercises finalize_payment_success exactly as create_payment's off-session lane
calls it: the order must flip to paid (atomic), payment_status updated, the
conversion event logged, and it must be idempotent on an already-paid order.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_offsession_success_finalizes_order_to_paid(monkeypatch):
    import services.psp_payment_finalizer as fin

    # GMV stamping hits the DB; stub it (its own path is covered elsewhere).
    async def fake_stamp(order_id, **kw):
        fake_stamp.called = order_id
    fake_stamp.called = None
    monkeypatch.setattr(fin, "stamp_gross_attributed_gmv", fake_stamp)

    paid, pinfo, events = [], [], []

    async def fake_mark_order_paid(order_id):
        paid.append(order_id)
        return True

    async def fake_update_payment_info(*, order_id, payment_intent_id, client_secret, payment_status, psp_used):
        pinfo.append({"order_id": order_id, "payment_status": payment_status, "pi": payment_intent_id, "psp": psp_used})
        return True

    async def fake_log_order_event(**kw):
        events.append(kw)
        return ["fe_1"]

    order = {
        "order_id": "ORD_ACP_1", "merchant_id": "merch_efbc46b4619cfbdf",
        "status": "created", "payment_status": "pending",
        "subtotal": "1.69", "discount_total": "0.00", "currency": "USD", "metadata": {},
    }

    res = await fin.finalize_payment_success(
        order, psp="stripe", payment_reference="pi_test", transaction_id="pi_test",
        amount_minor=None, currency="USD", source_event="acp_offsession_capture",
        metadata_extra={"acp_test_capture": True, "protocol_name": "acp"},
        update_payment_info_fn=fake_update_payment_info,
        mark_order_paid_fn=fake_mark_order_paid,
        log_order_event_fn=fake_log_order_event,
    )

    assert res["applied"] is True and res["transitioned"] is True
    assert paid == ["ORD_ACP_1"]                              # atomic paid transition fired
    assert any(p["payment_status"] == "paid" for p in pinfo)  # payment marked paid
    assert fake_stamp.called == "ORD_ACP_1"                   # GMV stamped on the edge
    assert events and events[0]["event_type"] == "acp_offsession_capture"


@pytest.mark.asyncio
async def test_finalize_idempotent_on_already_paid_order():
    from services.psp_payment_finalizer import finalize_payment_success

    async def must_not_call(*a, **k):
        raise AssertionError("side effects must not run on an already-paid order")

    order = {
        "order_id": "ORD_PAID", "merchant_id": "m", "status": "paid",
        "payment_status": "paid", "currency": "USD", "metadata": {},
    }
    res = await finalize_payment_success(
        order, psp="stripe", payment_reference="pi",
        mark_order_paid_fn=must_not_call, log_order_event_fn=must_not_call,
    )
    assert res["applied"] is False
    assert res["reason"] == "already_settled"


@pytest.mark.asyncio
async def test_finalize_suppresses_side_effects_when_transition_lost(monkeypatch):
    # Concurrent finalize: mark_order_paid returns False → treat as already settled,
    # do NOT double-log the conversion.
    import services.psp_payment_finalizer as fin

    events = []

    async def lost_transition(order_id):
        return False

    async def fake_log(**kw):
        events.append(kw)
        return []

    order = {"order_id": "ORD_RACE", "merchant_id": "m", "status": "created",
             "payment_status": "processing", "currency": "USD", "metadata": {}}
    res = await fin.finalize_payment_success(
        order, psp="stripe", payment_reference="pi",
        mark_order_paid_fn=lost_transition, log_order_event_fn=fake_log,
    )
    assert res["applied"] is False and res["transitioned"] is False
    assert events == []  # conversion event not double-logged
