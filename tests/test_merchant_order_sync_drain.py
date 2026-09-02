"""The refund path enqueues durably, and the drain retries instead of losing work.

`routes/refund_api.py` used to dispatch the post-refund Shopify cancel through
`background_tasks.add_task`, which runs in the API process with no retry — a
Cloud Run revision swap dropped it and left NO recoverable state, because a
refunded order whose cancel never fired is indistinguishable from one whose
cancel succeeded.

The Postgres-dialect gate for the queue's SQL lives in
tests/test_merchant_order_sync_jobs_postgres.py. These tests cover the two
halves SQLite can vouch for: that the route reaches the enqueue at all, and that
the drain classifies outcomes correctly.
"""

import pytest

import services.merchant_order_sync_drain as drain


class _FakeRefundAdapter:
    async def refund_payment(self, **kwargs):
        return True, "re_queue_test", None


def _wire_refund_route(monkeypatch, module, *, store):
    """Minimal harness around `process_refund`, mirroring the house pattern in
    tests/test_refund_api_canonical_psp.py."""

    async def fake_get_order(order_id: str):
        return {
            "order_id": order_id,
            "merchant_id": "merch_1",
            "payment_status": "paid",
            "total": "20.00",
            "total_refunded": "0.00",
            "currency": "USD",
            "payment_intent_id": "pi_queue_test",
            "psp_used": "stripe",
            "psp_id": "psp_stripe_1",
            "shopify_order_id": "6001",
            "metadata": {},
        }

    async def fake_get_primary_store(merchant_id: str):
        return store

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id}

    async def fake_resolve_refund_adapter(order):
        return "stripe", "sk_test", {}

    async def ok(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "_resolve_refund_adapter", fake_resolve_refund_adapter)
    monkeypatch.setattr(module, "get_psp_adapter", lambda *a, **k: _FakeRefundAdapter())
    monkeypatch.setattr(module, "finalize_refund_success", ok)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", ok)


async def _run_refund(module, order_id):
    from fastapi import BackgroundTasks, Response

    return await module.process_refund(
        order_id,
        module.RefundRequest(order_id=order_id, amount=20.0, reason="customer"),
        BackgroundTasks(),
        response=Response(),
        current_user={"user_id": "admin"},
    )


@pytest.mark.asyncio
async def test_refund_route_enqueues_a_durable_job(monkeypatch):
    """Delivery-path cover: the line that replaced `add_task` actually runs."""
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    _wire_refund_route(
        monkeypatch, module, store={"platform": "shopify", "domain": "x.myshopify.com"}
    )
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_QUEUE_ENQUEUED")

    assert len(calls) == 1
    call = calls[0]
    assert call["op"] == module.OP_REFUND_SYNC
    assert call["order_id"] == "ORD_QUEUE_ENQUEUED"
    # Dedupe on the PSP refund, so a retried request cannot double-queue.
    assert call["dedupe_key"] == "re_queue_test"
    payload = call["payload"]
    assert payload["shopify_order_id"] == "6001"
    assert payload["is_partial"] is False
    # The cancel contract is built by refund_api, not restated in the worker.
    assert payload["cancel_payload"] == {
        "reason": "customer",
        "email": False,
        "refund": False,
        # RefundRequest.restore_inventory defaults to True.
        "restock": True,
    }


@pytest.mark.asyncio
async def test_refund_route_enqueues_nothing_without_a_shopify_store(monkeypatch):
    """Negative counterpart: the guard must not queue jobs that can only no-op."""
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    _wire_refund_route(monkeypatch, module, store=None)
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_QUEUE_NO_STORE")

    assert calls == []


def _job(op=None, attempts=1, max_attempts=3):
    from db.merchant_order_sync_jobs import OP_REFUND_SYNC

    return {
        "job_id": "job-1",
        "order_id": "ORD_1",
        "merchant_id": "merch_1",
        "op": OP_REFUND_SYNC if op is None else op,
        "dedupe_key": "re_1",
        "payload": {"order_id": "ORD_1"},
        "attempts": attempts,
        "max_attempts": max_attempts,
    }


def _wire_drain(monkeypatch, job, handler=None):
    """One job, then an empty queue. Records what the tick wrote back."""
    seen = {"completed": [], "failed": []}
    remaining = [job]

    async def fake_claim(*, worker_id, **kwargs):
        return remaining.pop(0) if remaining else None

    async def fake_complete(*, job_id):
        seen["completed"].append(job_id)

    async def fake_fail(*, job_id, attempts, max_attempts, error):
        status = "failed" if int(attempts) >= int(max_attempts) else "pending"
        seen["failed"].append({"job_id": job_id, "status": status, "error": error})
        return status

    monkeypatch.setattr(drain, "claim_next_merchant_order_sync_job", fake_claim)
    monkeypatch.setattr(drain, "complete_merchant_order_sync_job", fake_complete)
    monkeypatch.setattr(drain, "fail_merchant_order_sync_job", fake_fail)
    if handler is not None:
        from db.merchant_order_sync_jobs import OP_REFUND_SYNC

        monkeypatch.setitem(drain._HANDLERS, OP_REFUND_SYNC, handler)
    return seen


@pytest.mark.asyncio
async def test_successful_job_is_completed(monkeypatch):
    async def handler(payload):
        return {"cancelled": True}

    seen = _wire_drain(monkeypatch, _job(), handler=handler)
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["done"] == 1
    assert seen["completed"] == ["job-1"]
    assert seen["failed"] == []


@pytest.mark.asyncio
async def test_transient_failure_is_requeued_not_dropped(monkeypatch):
    """The whole point: a failing attempt must survive as work still to do."""

    async def handler(payload):
        raise RuntimeError("shopify 503")

    seen = _wire_drain(monkeypatch, _job(attempts=1, max_attempts=3), handler=handler)
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["requeued"] == 1
    assert summary["failed"] == 0
    assert seen["completed"] == []
    assert seen["failed"][0]["status"] == "pending"
    assert "shopify 503" in seen["failed"][0]["error"]


@pytest.mark.asyncio
async def test_exhausted_job_is_terminal_and_never_marked_done(monkeypatch):
    async def handler(payload):
        raise RuntimeError("shopify 503")

    seen = _wire_drain(monkeypatch, _job(attempts=3, max_attempts=3), handler=handler)
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["failed"] == 1
    assert summary["done"] == 0
    # A job that never reached the merchant must not be recorded as completed.
    assert seen["completed"] == []
    assert seen["failed"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_unknown_op_fails_terminally_without_burning_retries(monkeypatch):
    """An op this build cannot run will never succeed by being retried."""
    seen = _wire_drain(monkeypatch, _job(op="op_from_the_future", max_attempts=3))
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["failed"] == 1
    assert seen["failed"][0]["status"] == "failed"
