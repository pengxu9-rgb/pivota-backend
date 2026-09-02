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
    def __init__(self, refund_ref="re_queue_test"):
        self._ref = refund_ref

    async def refund_payment(self, **kwargs):
        # Real adapters can report success with NO reference —
        # adapters/psp_adapter.py returns `data.get("pspReference")`, which is
        # None whenever the PSP omits it.
        return True, self._ref, None


def _wire_refund_route(monkeypatch, module, *, store, bound_store=None,
                       refund_ref="re_queue_test", store_id=None):
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
            "store_id": store_id,
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
    monkeypatch.setattr(
        module, "get_psp_adapter", lambda *a, **k: _FakeRefundAdapter(refund_ref)
    )
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
        return "written"

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
        return "lease_lost"

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
                  bound_store=None, sync_result=None, stores=None):
    """Patch the modules the handler imports at call time.

    `stores` gives the raw list when a test needs several; otherwise it is built
    from `bound_store` (the order's bound store) and `store` (the merchant's
    other/primary store), which is how most of these read.
    """
    import httpx

    import services.merchant_store_service as store_svc
    import services.shopify_access_token_service as token_svc
    import services.shopify_transactions_service as txn_svc

    calls = {"cancel": [], "sync": [], "progress": []}

    async def fake_active_stores(merchant_id):
        if stores is not None:
            return list(stores)
        out = []
        if bound_store:
            out.append(bound_store)
        if store:
            out.append(store)
        return out

    async def fake_token(**kwargs):
        return "shpat_token", None

    async def fake_sync(**kwargs):
        calls["sync"].append(kwargs)
        return sync_result if sync_result is not None else {"ok": True, "created": True}

    monkeypatch.setattr(store_svc, "get_merchant_active_stores", fake_active_stores)
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
        "parent_transaction_id": None,
        "cancel_payload": {"reason": "customer", "email": False,
                           "refund": False, "restock": True},
    }
    base.update(over)
    return base


_SHOPIFY_STORE = {"platform": "shopify", "domain": "x.myshopify.com", "store_id": "st_1"}
_OTHER_SHOPIFY_STORE = {
    "platform": "shopify", "domain": "other.myshopify.com", "store_id": "st_other",
}


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
        # The exact shape a loose reader gets wrong: contains "cancel" and
        # "has been", but the order was NOT cancelled. Accepting this marks the
        # step done and completes the job on an uncancelled merchant order.
        cancel_response=_FakeResponse(
            422,
            '{"errors":"Order cannot be cancelled because it has been fulfilled"}',
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
async def test_handler_refuses_to_guess_between_several_shopify_stores(monkeypatch):
    """Cancelling on the wrong shop is worse than not cancelling. With more than
    one Shopify store and a bound id matching none of them, guessing is exactly
    how a refund lands on the wrong merchant shop."""
    _wire_handler(
        monkeypatch, stores=[_SHOPIFY_STORE, _OTHER_SHOPIFY_STORE]
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(store_id="st_missing"), {}, None)


@pytest.mark.asyncio
async def test_a_legacy_bound_store_id_still_resolves(monkeypatch):
    """`get_merchant_active_stores` SYNTHESISES `legacy_<merchant_id>` for a
    merchant with no store row but an mcp_platform, and order creation binds
    that id verbatim. Resolving it through `get_store_by_id` — which queries
    merchant_stores alone — returned None forever, so every refund for that
    merchant burned the budget and paged, on a store whose token works."""
    legacy = {
        "platform": "shopify",
        "domain": "legacy.myshopify.com",
        "store_id": "legacy_merch_1",
        "source": "legacy_mcp",
    }
    calls = _wire_handler(monkeypatch, stores=[legacy])

    await drain._run_refund_sync_job(_payload(store_id="legacy_merch_1"), {}, None)

    assert "legacy.myshopify.com" in calls["cancel"][0]["url"]


@pytest.mark.asyncio
async def test_a_non_shopify_bound_store_falls_back_to_the_only_shopify_one(monkeypatch):
    """Order creation binds the platform-agnostic PRIMARY store while the
    Shopify order write falls back to any active Shopify store — so an order can
    carry a shopify_order_id and a store_id pointing at the Woo row."""
    woo = {"platform": "woocommerce", "domain": "shop.example", "store_id": "st_woo"}
    calls = _wire_handler(monkeypatch, stores=[woo, _SHOPIFY_STORE])

    await drain._run_refund_sync_job(_payload(store_id="st_woo"), {}, None)

    assert "x.myshopify.com" in calls["cancel"][0]["url"]


@pytest.mark.asyncio
async def test_an_unrecorded_completion_is_not_reported_as_a_lost_lease(monkeypatch):
    """The work DID reach Shopify; only the row does not say so. Reporting a
    lost lease says another worker owns it, which is false."""

    async def handler(payload, progress=None, on_progress=None):
        return {"cancelled": True}

    _wire_drain(monkeypatch, _job(), handler=handler)

    async def complete_write_fails(*, job_id, worker_id):
        return "write_failed"

    monkeypatch.setattr(drain, "complete_merchant_order_sync_job", complete_write_fails)

    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["write_failed"] == 1
    assert summary["lease_lost"] == 0
    assert summary["done"] == 0


@pytest.mark.asyncio
async def test_the_tick_hands_stored_progress_to_the_handler(monkeypatch):
    """The Postgres gate proves progress round-trips through the row and the
    handler tests take a dict directly; nothing joined the two."""
    got = {}

    async def handler(payload, progress=None, on_progress=None):
        got["progress"] = progress
        return {}

    job = _job()
    job["progress"] = {drain._STEP_TRANSACTION: True}
    _wire_drain(monkeypatch, job, handler=handler)
    await drain.run_merchant_order_sync_worker_tick()

    assert got["progress"] == {drain._STEP_TRANSACTION: True}


@pytest.mark.asyncio
async def test_handler_does_not_reissue_a_cancel_already_recorded(monkeypatch):
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(
        _payload(),
        {drain._STEP_TRANSACTION: True, drain._STEP_CANCEL: True},
        None,
    )

    assert calls["cancel"] == [], "a recorded cancel must not be re-issued"
    assert calls["sync"] == []


@pytest.mark.asyncio
async def test_handler_does_not_cancel_on_a_partial_refund(monkeypatch):
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(_payload(is_partial=True), {}, None)

    assert len(calls["sync"]) == 1
    assert calls["cancel"] == [], "a partial refund must not cancel the order"


@pytest.mark.asyncio
async def test_handler_retries_a_bare_transaction_write_failure(monkeypatch):
    """A Shopify 5xx on POST /transactions.json comes back as a bare
    {"ok": False, "error": ...} with NO `retryable` flag. Completing on that
    would mark the job SUCCESS on a merchant order that was never updated."""
    _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={"ok": False, "created": False,
                     "error": "Failed to create transaction (status=500)"},
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_a_missing_refund_reference_completes_only_once_annotated(monkeypatch):
    """No retry can produce a reference, so the transaction can never be
    written — but the order must still carry the annotation, or nothing at all
    reached the merchant."""
    calls = _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={"ok": False, "skipped": True,
                     "reason": "missing_gateway_or_refund_ref",
                     "annotation": {"ok": True, "status": 200}},
    )

    result = await drain._run_refund_sync_job(_payload(refund_id=None), {}, None)

    assert result["transaction_sync_soft_skipped"] == "missing_gateway_or_refund_ref"
    # The cancel still runs: a full refund must still cancel the merchant order.
    assert len(calls["cancel"]) == 1


@pytest.mark.asyncio
async def test_a_partial_refund_with_no_reference_cannot_complete_silently(monkeypatch):
    """THE BLOCKER round four found, and the fourth instance of one class.

    `missing_gateway_or_refund_ref` returned before any HTTP, so unlike every
    sibling non-write path it annotated nothing. On a PARTIAL refund the cancel
    step is skipped too, so the job completed having made ZERO Shopify calls:
    buyer refunded, merchant's order carrying no transaction, no note attribute
    and no tag, and the queue recording `done`.

    Reachable today: adapters/checkout_adapter returns `action_id or id`, which
    is None when the 202 body carries neither.
    """
    calls = _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={"ok": False, "skipped": True,
                     "reason": "missing_gateway_or_refund_ref",
                     "annotation": {"ok": False, "status": 500}},
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(
            _payload(refund_id=None, is_partial=True), {}, None
        )

    assert calls["cancel"] == []


@pytest.mark.asyncio
async def test_a_transaction_only_job_cannot_complete_without_reaching_shopify(monkeypatch):
    """The readiness surface sets skip_cancel, so the transaction IS the whole
    job — there is no second step to make the outcome visible."""
    _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={"ok": True, "created": False, "soft_skipped": True,
                     "reason": "missing_parent_transaction",
                     "annotation": {"ok": False, "status": 502}},
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(skip_cancel=True), {}, None)


@pytest.mark.asyncio
async def test_handler_honours_an_explicit_skip_cancel(monkeypatch):
    """Transaction-only callers mirror the refund without cancelling the order."""
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(_payload(skip_cancel=True), {}, None)

    assert len(calls["sync"]) == 1
    assert calls["cancel"] == []


@pytest.mark.asyncio
async def test_dedupe_key_does_not_collapse_refunds_with_no_psp_reference(monkeypatch):
    """`str(None)` is the truthy string "None". Keying on it puts every
    reference-less refund on one unique-index slot: the second one's ON CONFLICT
    returns the FIRST job's id, so the enqueue looks successful and that
    refund's sync is silently never queued."""
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    _wire_refund_route(
        monkeypatch, module,
        store={"platform": "shopify", "domain": "x.myshopify.com"},
        refund_ref=None,
    )
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_NO_REF")

    assert len(calls) == 1
    assert calls[0]["dedupe_key"] != "None"
    # And the payload keeps the raw null so the writer short-circuits rather
    # than recording an authorization of "None".
    assert calls[0]["payload"]["refund_id"] is None


@pytest.mark.asyncio
async def test_enqueue_carries_the_bound_store_id(monkeypatch):
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    _wire_refund_route(
        monkeypatch, module,
        store={"platform": "shopify", "domain": "primary.myshopify.com"},
        bound_store={"platform": "shopify", "domain": "bound.myshopify.com"},
        store_id="st_bound",
    )
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_BOUND_STORE")

    assert calls[0]["payload"]["store_id"] == "st_bound"


@pytest.mark.asyncio
async def test_enqueue_gate_follows_the_bound_store_not_the_primary(monkeypatch):
    """The worker resolves the bound store, so gating on the primary skips work
    the worker could have done."""
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    _wire_refund_route(
        monkeypatch, module,
        store={"platform": "woocommerce", "domain": "primary.example"},
        bound_store={"platform": "shopify", "domain": "bound.myshopify.com"},
        store_id="st_bound",
    )
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_GATE_BOUND")

    assert len(calls) == 1, "primary is non-Shopify but the bound store is not"


@pytest.mark.asyncio
async def test_a_soft_skip_whose_annotation_also_failed_is_not_success(monkeypatch):
    """`ok: True` does not mean the refund was mirrored.

    `soft_skipped` means NO refund transaction was written and the writer
    annotated the order instead — and that annotation returns ok:False on any
    non-2xx. If neither landed, nothing at all reached Shopify, and completing
    records success on an order the merchant can never reconcile.
    """
    _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={
            "ok": True,
            "created": False,
            "soft_skipped": True,
            "reason": "missing_parent_transaction",
            "annotation": {"ok": False, "status": 500, "error": "Shopify 500"},
        },
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_a_soft_skip_whose_annotation_landed_completes(monkeypatch):
    """Positive counterpart: a missing parent transaction is structural, so once
    the order carries the reconciliation tag there is nothing left to retry."""
    _wire_handler(
        monkeypatch,
        store=_SHOPIFY_STORE,
        sync_result={
            "ok": True,
            "created": False,
            "soft_skipped": True,
            "reason": "missing_parent_transaction",
            "annotation": {"ok": True, "status": 200},
        },
    )

    result = await drain._run_refund_sync_job(_payload(), {}, None)

    assert result["transaction_sync_soft_skipped"] == "missing_parent_transaction"


@pytest.mark.asyncio
async def test_the_handler_forwards_a_known_parent_transaction_id(monkeypatch):
    calls = _wire_handler(monkeypatch, store=_SHOPIFY_STORE)

    await drain._run_refund_sync_job(_payload(parent_transaction_id=1444), {}, None)

    assert calls["sync"][0]["parent_transaction_id"] == 1444


@pytest.mark.asyncio
async def test_an_unrecorded_failure_is_not_reported_as_a_requeue(monkeypatch):
    """`write_failed` means the outcome was never written: the row keeps its live
    lease and `last_error` stays NULL, so recovery is the lease expiry rather
    than the 30s backoff. Counting it as `requeued` describes work that did not
    happen."""

    async def handler(payload, progress=None, on_progress=None):
        raise RuntimeError("shopify 503")

    seen = _wire_drain(monkeypatch, _job(attempts=1, max_attempts=10), handler=handler)

    async def fail_write_fails(*, job_id, worker_id, attempts, max_attempts, error):
        seen["failed"].append({"job_id": job_id, "status": "write_failed"})
        return "write_failed"

    monkeypatch.setattr(drain, "fail_merchant_order_sync_job", fail_write_fails)

    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["write_failed"] == 1
    assert summary["requeued"] == 0
    assert summary["failed"] == 0


@pytest.mark.asyncio
async def test_an_unknown_op_that_loses_its_lease_is_not_counted_as_requeued(monkeypatch):
    seen = _wire_drain(
        monkeypatch, _job(op="op_from_the_future", attempts=1, max_attempts=10)
    )

    async def fail_lease_lost(*, job_id, worker_id, attempts, max_attempts, error):
        seen["failed"].append({"job_id": job_id, "status": "lease_lost"})
        return "lease_lost"

    monkeypatch.setattr(drain, "fail_merchant_order_sync_job", fail_lease_lost)

    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["lease_lost"] == 1
    assert summary["requeued"] == 0


@pytest.mark.asyncio
async def test_no_shopify_store_is_retryable_not_terminal(monkeypatch):
    """`get_merchant_active_stores` filters status IN ('active','connected'), so
    "no store" and "store mid-reconnect" are indistinguishable here — failing
    fast fires the money-path incident for something that self-heals."""
    _wire_handler(monkeypatch, store=None)

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_refund_sync_job(_payload(), {}, None)


@pytest.mark.asyncio
async def test_enqueue_is_not_skipped_when_the_bound_store_cannot_be_resolved(monkeypatch):
    """The one outcome this path must never produce. A stale bound store_id is a
    documented production class; dropping the enqueue leaves a refunded order
    with no job, no event and no log — nothing anyone can reconcile."""
    import routes.refund_api as module

    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return "job-1"

    # Primary is NOT Shopify and the bound store is unresolvable — the worker
    # decides, and a job that fails there is at least visible.
    _wire_refund_route(
        monkeypatch, module,
        store={"platform": "woocommerce", "domain": "primary.example"},
        store_id="st_stale",
    )
    monkeypatch.setattr(module, "enqueue_merchant_order_sync_job", fake_enqueue)

    await _run_refund(module, "ORD_STALE_BOUND")

    assert len(calls) == 1, "a refund with a bound store_id was silently dropped"
    assert calls[0]["payload"]["store_id"] == "st_stale"


# ---------------------------------------------------------------------------
# The merchant-order CREATE op, which replaced `background_tasks.add_task` at
# five call sites.
# ---------------------------------------------------------------------------


def _wire_create(monkeypatch, *, sync_returns=True, order=None, primary=None,
                 linked=True):
    """`linked` controls whether the order carries a linked platform order after
    a successful sync — the handler now verifies that, because every platform
    creator returns True when another holder has the create lock."""
    import db.orders as orders_module
    import routes.order_routes as order_routes
    import services.merchant_store_service as store_svc

    calls = {"sync": [], "primary": []}

    async def fake_sync(order_id):
        calls["sync"].append(order_id)
        return sync_returns

    async def fake_get_order(order_id):
        if order is not None:
            return order
        return {"metadata": {"merchant_order": {"platform_order_id": "shop-1"}}} if linked else {}

    async def fake_primary(merchant_id):
        calls["primary"].append(merchant_id)
        return primary

    monkeypatch.setattr(order_routes, "sync_order_to_connected_store", fake_sync)
    monkeypatch.setattr(orders_module, "get_order", fake_get_order)
    monkeypatch.setattr(store_svc, "get_primary_store", fake_primary)
    return calls


def _marker(*, retryable, reason, platform="shopify", fresh=True):
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - (
        timedelta(seconds=5) if fresh else timedelta(hours=3)
    )
    return {"metadata": {"merchant_order": {
        "retryable": retryable,
        "last_failure_reason": reason,
        "platform": platform,
        "last_attempt_at": when.isoformat().replace("+00:00", "Z"),
    }}}


def _create_payload(**over):
    base = {"order_id": "ORD_1", "merchant_id": "merch_1",
            "require_shopify_primary": False}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_create_job_syncs_the_order(monkeypatch):
    calls = _wire_create(monkeypatch, sync_returns=True)

    result = await drain._run_merchant_order_create_job(_create_payload(), {}, None)

    assert result == {"created": True}
    assert calls["sync"] == ["ORD_1"]


@pytest.mark.asyncio
async def test_create_job_retries_a_recoverable_failure(monkeypatch):
    """`sync_order_to_connected_store` returns False and writes a marker saying
    whether the condition is worth retrying."""
    _wire_create(
        monkeypatch, sync_returns=False,
        order=_marker(retryable=True, reason="merchant_order_create_returned_false"),
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_merchant_order_create_job(_create_payload(), {}, None)


@pytest.mark.asyncio
async def test_create_job_does_not_burn_the_budget_on_an_unretryable_failure(monkeypatch):
    """`wix_order_writeback_not_ready` and a missing bound store are both marked
    retryable=False. Unlike the refund path this leaves a queryable trace — the
    order stays paid with no merchant order — so completing is safe and the
    standing `paid_missing_merchant_order_count` signal keeps counting it."""
    _wire_create(
        monkeypatch, sync_returns=False,
        order=_marker(retryable=False, reason="bound_store_missing_or_inactive"),
    )

    result = await drain._run_merchant_order_create_job(_create_payload(), {}, None)

    assert result["created"] is False
    assert result["skipped"] == "bound_store_missing_or_inactive"


@pytest.mark.asyncio
async def test_create_job_honours_the_webhooks_shopify_primary_guard(monkeypatch):
    """The Checkout.com webhook only ever created merchant orders for a Shopify
    PRIMARY store, even though the sync also dispatches Woo/BigCommerce/Wix.
    Widening that here would start creating orders on platforms it never served."""
    calls = _wire_create(
        monkeypatch, sync_returns=True,
        primary={"platform": "woocommerce", "domain": "shop.example"},
    )

    result = await drain._run_merchant_order_create_job(
        _create_payload(require_shopify_primary=True), {}, None
    )

    assert result == {"skipped": "primary_store_not_shopify"}
    assert calls["sync"] == [], "the sync must not run behind this guard"


@pytest.mark.asyncio
async def test_create_job_proceeds_when_the_primary_is_shopify(monkeypatch):
    calls = _wire_create(
        monkeypatch, sync_returns=True,
        primary={"platform": "shopify", "domain": "x.myshopify.com"},
    )

    result = await drain._run_merchant_order_create_job(
        _create_payload(require_shopify_primary=True), {}, None
    )

    assert result == {"created": True}
    assert calls["sync"] == ["ORD_1"]


@pytest.mark.asyncio
async def test_create_job_never_retries_a_platform_without_remote_idempotency(monkeypatch):
    """THE BLOCKER round five found, and the same class as the refund path's H1:
    a queue adding retries to a write that was only safe because it ran once.

    `sync_order_to_connected_store` collapses the adapter's own `retryable` flag
    and hardcodes True, so `wix_physical_order_auto_fulfilled` — raised AFTER a
    real Wix order and payment record were written, with order_id=None so
    nothing links — read as retryable. Only Shopify looks for an existing order
    before creating; Woo/Wix/BigCommerce would POST a brand-new one. Ten
    attempts, ten merchant orders, ten shipments, one buyer.
    """
    _wire_create(
        monkeypatch, sync_returns=False,
        order=_marker(retryable=True, reason="wix_physical_order_auto_fulfilled",
                      platform="wix"),
    )

    result = await drain._run_merchant_order_create_job(_create_payload(), {}, None)

    assert result["created"] is False
    assert result["platform"] == "wix"


@pytest.mark.asyncio
async def test_create_job_still_retries_shopify(monkeypatch):
    """Positive counterpart: Shopify's create IS remotely idempotent
    (`_find_existing_order_id_best_effort` plus tag reuse), so it keeps the
    retry the queue exists to provide."""
    _wire_create(
        monkeypatch, sync_returns=False,
        order=_marker(retryable=True, reason="merchant_order_create_returned_false",
                      platform="shopify"),
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_merchant_order_create_job(_create_payload(), {}, None)


@pytest.mark.asyncio
async def test_create_job_does_not_complete_on_lock_contention(monkeypatch):
    """Every platform creator returns True when another holder has the per-order
    advisory lock — "someone else is doing it", not "it is done". Completing
    there records success for a merchant order that may never arrive, and the
    constant dedupe key means nothing would re-queue it."""
    _wire_create(monkeypatch, sync_returns=True, linked=False)

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_merchant_order_create_job(_create_payload(), {}, None)


@pytest.mark.asyncio
async def test_create_job_does_not_trust_a_stale_unretryable_marker(monkeypatch):
    """The marker lives in ORDER metadata, not job state, and
    `_mark_merchant_order_sync_failed_best_effort` swallows its own write
    failure — so a `retryable: False` from an earlier attempt can still be
    sitting there when a later attempt fails transiently."""
    _wire_create(
        monkeypatch, sync_returns=False,
        order=_marker(retryable=False, reason="bound_store_missing_or_inactive",
                      fresh=False),
    )

    with pytest.raises(drain._RetryableSyncError):
        await drain._run_merchant_order_create_job(_create_payload(), {}, None)


@pytest.mark.asyncio
async def test_the_tick_routes_a_create_job_to_the_create_handler(monkeypatch):
    """Pins the `_HANDLERS` registration itself.

    Every other create test calls `_run_merchant_order_create_job` directly, so
    deleting the `OP_MERCHANT_ORDER_CREATE` entry from `_HANDLERS` left the whole
    suite green — while in production every create job would take the
    `no handler for op` branch, burn its attempts and land terminal `failed`,
    stopping merchant-order creation platform-wide.
    """
    from db.merchant_order_sync_jobs import OP_MERCHANT_ORDER_CREATE

    calls = _wire_create(monkeypatch, sync_returns=True, linked=True)
    job = _job(op=OP_MERCHANT_ORDER_CREATE)
    job["payload"] = _create_payload()
    seen = _wire_drain(monkeypatch, job)  # NO handler override: real dispatch

    summary = await drain.run_merchant_order_sync_worker_tick()

    assert summary["done"] == 1, "the tick did not route the job to a handler"
    assert calls["sync"] == ["ORD_1"], "the create handler never ran"
    assert seen["failed"] == []
