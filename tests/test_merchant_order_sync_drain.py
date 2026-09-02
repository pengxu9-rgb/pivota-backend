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

    async def fake_complete(*, job_id, worker_id):
        seen["completed"].append(job_id)
        seen["complete_worker"] = worker_id
        return True

    async def fake_fail(*, job_id, worker_id, attempts, max_attempts, error):
        status = "failed" if int(attempts) >= int(max_attempts) else "pending"
        seen["failed"].append({"job_id": job_id, "status": status, "error": error})
        seen["fail_worker"] = worker_id
        return status

    async def fake_progress(*, job_id, worker_id, progress):
        return True

    monkeypatch.setattr(drain, "claim_next_merchant_order_sync_job", fake_claim)
    monkeypatch.setattr(drain, "complete_merchant_order_sync_job", fake_complete)
    monkeypatch.setattr(drain, "fail_merchant_order_sync_job", fake_fail)
    monkeypatch.setattr(drain, "record_merchant_order_sync_progress", fake_progress)
    if handler is not None:
        from db.merchant_order_sync_jobs import OP_REFUND_SYNC

        monkeypatch.setitem(drain._HANDLERS, OP_REFUND_SYNC, handler)
    return seen


@pytest.mark.asyncio
async def test_successful_job_is_completed(monkeypatch):
    async def handler(payload, progress=None, on_progress=None):
        return {"cancelled": True}

    seen = _wire_drain(monkeypatch, _job(), handler=handler)
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["done"] == 1
    assert seen["completed"] == ["job-1"]
    assert seen["failed"] == []


@pytest.mark.asyncio
async def test_transient_failure_is_requeued_not_dropped(monkeypatch):
    """The whole point: a failing attempt must survive as work still to do."""

    async def handler(payload, progress=None, on_progress=None):
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
    async def handler(payload, progress=None, on_progress=None):
        raise RuntimeError("shopify 503")

    seen = _wire_drain(monkeypatch, _job(attempts=3, max_attempts=3), handler=handler)
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["failed"] == 1
    assert summary["done"] == 0
    # A job that never reached the merchant must not be recorded as completed.
    assert seen["completed"] == []
    assert seen["failed"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_unknown_op_is_requeued_for_a_build_that_has_a_handler(monkeypatch):
    """During a rolling deploy an old revision can claim a job enqueued by a new
    one. Failing it terminally would destroy work the next revision could do."""
    seen = _wire_drain(
        monkeypatch, _job(op="op_from_the_future", attempts=1, max_attempts=3)
    )
    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["requeued"] == 1
    assert summary["failed"] == 0
    assert seen["failed"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_a_lost_lease_is_not_counted_as_done(monkeypatch):
    """If our lease expired and a sibling re-claimed the job, our completing
    write must not land — and must not be reported as a completion."""

    async def handler(payload, progress=None, on_progress=None):
        return {"cancelled": True}

    seen = _wire_drain(monkeypatch, _job(), handler=handler)

    async def lease_lost(*, job_id, worker_id):
        return False

    monkeypatch.setattr(drain, "complete_merchant_order_sync_job", lease_lost)

    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["lease_lost"] == 1
    assert summary["done"] == 0


# ---------------------------------------------------------------------------
# The handler itself. Every test above stubs `_HANDLERS`, so before these the
# code that actually talks to Shopify had no coverage at all — which is how a
# non-idempotent refund write survived review of the queue that retries it.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Records cancel POSTs and replays a scripted response."""

    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        self._calls.append({"url": url, "json": json, "headers": headers})
        return self._response


def _wire_handler(monkeypatch, *, cancel_response=None, store=None,
                  bound_store=None, sync_result=None):
    """Patch the modules the handler imports at call time."""
    import httpx

    import services.merchant_store_service as store_svc
    import services.shopify_access_token_service as token_svc
    import services.shopify_transactions_service as txn_svc

    calls = {"cancel": [], "sync": [], "progress": []}

    async def fake_primary(merchant_id):
        return store

    async def fake_by_id(store_id, *, merchant_id=None):
        return bound_store

    async def fake_token(**kwargs):
        return "shpat_token", None

    async def fake_sync(**kwargs):
        calls["sync"].append(kwargs)
        return sync_result if sync_result is not None else {"ok": True, "created": True}

    monkeypatch.setattr(store_svc, "get_primary_store", fake_primary)
    monkeypatch.setattr(store_svc, "get_store_by_id", fake_by_id)
    monkeypatch.setattr(token_svc, "resolve_shopify_admin_access_token", fake_token)
    monkeypatch.setattr(
        txn_svc, "ensure_external_refund_transaction_best_effort", fake_sync
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(
            cancel_response or _FakeResponse(200), calls["cancel"]
        ),
    )
    return calls


def _payload(**over):
    base = {
        "order_id": "ORD_1",
        "merchant_id": "merch_1",
        "shopify_order_id": "6001",
        "store_id": None,
        "psp_used": "stripe",
        "refund_id": "re_1",
        "amount": 20.0,
        "currency": "USD",
        "is_partial": False,
        "cancel_payload": {"reason": "customer", "email": False,
                           "refund": False, "restock": True},
    }
    base.update(over)
    return base


_SHOPIFY_STORE = {"platform": "shopify", "domain": "x.myshopify.com", "store_id": "st_1"}


@pytest.mark.asyncio
async def test_handler_syncs_the_transaction_then_cancels(monkeypatch):
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)
    recorded = []

    async def on_progress(p):
        recorded.append(dict(p))

    result = await drain._run_refund_sync_job(_payload(), {}, on_progress)

    assert len(calls["sync"]) == 1
    assert len(calls["cancel"]) == 1
    assert result["cancelled"] is True
    # Both steps recorded, so a retry resumes rather than repeating them.
    assert recorded[-1][drain._STEP_TRANSACTION] is True
    assert recorded[-1][drain._STEP_CANCEL] is True


@pytest.mark.asyncio
async def test_handler_does_not_rewrite_a_transaction_a_prior_attempt_landed(monkeypatch):
    """H1's second layer: a retry after a cancel failure must not re-post the
    refund transaction."""
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    result = await drain._run_refund_sync_job(
        _payload(), {drain._STEP_TRANSACTION: True}, None
    )

    assert calls["sync"] == [], "refund transaction was written a second time"
    assert len(calls["cancel"]) == 1
    assert result["cancelled"] is True


@pytest.mark.asyncio
async def test_handler_passes_a_null_refund_reference_through_unchanged(monkeypatch):
    """str(None) would be the truthy string "None", which the writer would record
    as a real authorization instead of short-circuiting."""
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(_payload(refund_id=None), {}, None)

    assert calls["sync"][0]["external_refund_ref"] is None


@pytest.mark.asyncio
async def test_handler_retries_when_the_transaction_writer_defers(monkeypatch):
    """The writer refuses when it cannot read the existing transaction list."""
    _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={"ok": False, "retryable": True,
                     "reason": "transaction_list_unavailable"},
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_handler_does_not_treat_a_cancel_404_as_success(monkeypatch):
    """A 404 means the order is not on the shop we authenticated against — a
    store-binding problem, not proof the work is done. Recording it as success is
    how a refunded-but-untouched merchant order gets a positive success record."""
    _wire_handler(
        monkeypatch, store=_SHOPIFY_STORE,
        cancel_response=_FakeResponse(404, '{"errors":"Not Found"}'),
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_handler_accepts_an_already_cancelled_422(monkeypatch):
    _wire_handler(
        monkeypatch, store=_SHOPIFY_STORE,
        cancel_response=_FakeResponse(
            422, '{"errors":"Order has already been canceled."}'
        ),
    )

    result = await drain._run_refund_sync_job(_payload(), {}, None)

    assert result["cancel_terminal"] == "already_cancelled"


@pytest.mark.asyncio
async def test_handler_retries_a_422_that_is_not_already_cancelled(monkeypatch):
    """422 on this endpoint is a bucket, not a fact. Reading the body is the
    house pattern (see _is_non_fatal_invalid_sale_error)."""
    _wire_handler(
        monkeypatch, store=_SHOPIFY_STORE,
        cancel_response=_FakeResponse(
            422, '{"errors":"Order cannot be cancelled: fulfilled"}'
        ),
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_handler_prefers_the_bound_store_over_the_primary(monkeypatch):
    """get_store_by_id's own docstring: downstream jobs must prefer the order's
    bound store_id or a multi-store merchant is cancelled on the wrong shop."""
    wrong = {"platform": "shopify", "domain": "WRONG.myshopify.com", "store_id": "st_9"}
    right = {"platform": "shopify", "domain": "right.myshopify.com", "store_id": "st_2"}
    calls = _wire_handler(monkeypatch, store=wrong, bound_store=right)

    await drain._run_refund_sync_job(_payload(store_id="st_2"), {}, None)

    assert "right.myshopify.com" in calls["cancel"][0]["url"]


@pytest.mark.asyncio
async def test_handler_refuses_to_fall_back_when_the_bound_store_is_gone(monkeypatch):
    """Cancelling on the wrong shop is worse than not cancelling."""
    _wire_handler(monkeypatch, store=_SHOPIFY_STORE, bound_store=None)

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(store_id="st_missing"), {}, None)


@pytest.mark.asyncio
async def test_handler_does_not_cancel_on_a_partial_refund(monkeypatch):
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(_payload(is_partial=True), {}, None)

    assert len(calls["sync"]) == 1
    assert calls["cancel"] == [], "a partial refund must not cancel the order"
