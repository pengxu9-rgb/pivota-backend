"""Drain tick for the durable merchant-order sync queue.

Replaces `background_tasks.add_task` on the post-payment refund path. The task
version ran in the API process with no retry and died with a Cloud Run revision
swap; this one claims work under a lease, retries with backoff, and leaves a
terminally-failed row behind when it gives up.

Shape follows `services/audit_run_worker.py`: a scheduler-registered tick that
claims bounded work per run via SKIP LOCKED, processes serially, and is paired
with a lease reaper on a faster cadence as a backstop.
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Dict, Optional

from db.merchant_order_sync_jobs import (
    OP_MERCHANT_ORDER_CREATE,
    OP_REFUND_SYNC,
    claim_next_merchant_order_sync_job,
    complete_merchant_order_sync_job,
    fail_merchant_order_sync_job,
    record_merchant_order_sync_progress,
    release_stale_merchant_order_sync_leases,
)
from utils.logger import logger

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Bounded per tick so one backlog cannot monopolise the scheduler thread.
MAX_JOBS_PER_TICK = 20

_SHOPIFY_API_VERSION = "2025-10"


class _RetryableSyncError(Exception):
    """Raised when the job should be re-queued rather than treated as done.

    There is deliberately no terminal counterpart. The conditions that looked
    terminal — no bound store, no primary store — are both resolved through
    `status IN ('active','connected')`, so a merchant re-authing their Shopify
    app flips them within minutes. Failing fast on those fires the money-path
    incident for something the attempt budget absorbs, and a job that genuinely
    cannot succeed still ends `failed` after the budget, which is visible.
    """


# Progress keys recorded on the job row, so a retry resumes rather than
# re-running a side-effecting call that already landed.
_STEP_TRANSACTION = "refund_transaction_synced"
_STEP_CANCEL = "order_cancelled"

# `ok: False` reasons that retrying genuinely cannot change. Everything else
# that is not ok RETRIES — a Shopify 500 on POST /transactions.json comes back
# as a bare {"ok": False, "error": ...} with no `retryable` flag, and treating
# that as done would mark the job SUCCESS on a merchant order that was never
# updated. That is the exact state this queue exists to eliminate, and it is
# the same mistake the cancel-404 handling below was hardened against.
_TERMINAL_SYNC_REASONS = {"missing_gateway_or_refund_ref"}


def _is_already_cancelled(body: str) -> bool:
    """Shopify reports an already-cancelled order as a 422 with a message, not a
    distinct status. Mirrors the house pattern in
    `shopify_transactions_service._is_non_fatal_invalid_sale_error`: read the
    body before deciding a 422 is benign, because 422 on this endpoint also
    covers conditions a retry genuinely should not paper over.
    """
    # Match the one phrase Shopify actually uses, the way
    # `_is_non_fatal_invalid_sale_error` does. A looser reader turns
    # "cannot be cancelled because it has been fulfilled" into a completed
    # step — a refunded-but-uncancelled order with a `done` row, which is the
    # exact class the cancel-404 handling below exists to prevent.
    return "already been cancel" in (body or "").lower()


async def _resolve_store_for_refund(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the store this order is actually bound to.

    `get_store_by_id`'s own docstring: "Orders are bound to a store_id at
    checkout time. Downstream jobs (Shopify sync, invites, etc.) should prefer
    that store_id over 'primary store' to avoid cross-store token/domain
    mismatches when a merchant connects multiple stores." The former background
    task used `get_primary_store` unconditionally, so on a multi-store merchant
    it cancelled against the wrong shop — which returns 404 and, before this,
    was recorded as a successful job.

    Never falls back to the primary store when a bound store_id exists and
    cannot be resolved: cancelling on the wrong shop is worse than not
    cancelling, and `routes/order_routes.sync_order_to_connected_store` takes
    the same stance.
    """
    from services.merchant_store_service import get_merchant_active_stores

    merchant_id = str(payload.get("merchant_id") or "")
    bound_store_id = str(payload.get("store_id") or "").strip() or None

    # Resolve through `get_merchant_active_stores`, NOT `get_store_by_id`.
    #
    # `get_store_by_id` queries `merchant_stores` alone, but the id bound onto
    # an order can come from elsewhere: `get_merchant_active_stores` SYNTHESISES
    # `legacy_<merchant_id>` for a merchant with no store row but an
    # `merchant_onboarding.mcp_platform`, and order creation binds that id
    # verbatim. Resolving such an order through `get_store_by_id` returns None
    # forever — so every refund for that merchant burned the whole attempt
    # budget and fired the money-path incident, on a merchant whose store and
    # token both work.
    stores = await get_merchant_active_stores(merchant_id) or []
    shopify_stores = [
        s for s in stores
        if str((s or {}).get("platform") or "").strip().lower() == "shopify"
    ]

    if bound_store_id:
        for store in shopify_stores:
            if str(store.get("store_id") or "") == bound_store_id:
                return store

    if len(shopify_stores) == 1:
        # The bound id names no Shopify store this merchant has — it is a legacy
        # id, a detached row, or (routinely) a non-Shopify primary, because
        # order creation binds the platform-agnostic primary while the Shopify
        # order write falls back to any active Shopify store. With exactly one
        # Shopify store there is nothing to guess: that is where the order is.
        return shopify_stores[0]

    if not shopify_stores:
        raise _RetryableSyncError(
            "no active Shopify store for this merchant; nothing to sync to"
        )

    # Several Shopify stores and a bound id matching none of them. Guessing here
    # is how a refund gets cancelled on the wrong shop, which is worse than not
    # cancelling — refuse and let the attempt budget expose it.
    raise _RetryableSyncError(
        f"bound store {bound_store_id} matches none of this merchant's "
        f"{len(shopify_stores)} Shopify stores; refusing to guess"
    )


async def _run_refund_sync_job(
    payload: Dict[str, Any],
    progress: Optional[Dict[str, Any]] = None,
    on_progress=None,
) -> Dict[str, Any]:
    """Sync a completed refund to the merchant's store.

    Replaces the former `update_shopify_order_task` closure in refund_api. Two
    side-effecting steps, each recorded on the job so a retry of one does not
    re-run the other.
    """
    import httpx

    from services.shopify_access_token_service import resolve_shopify_admin_access_token
    from services.shopify_transactions_service import (
        ensure_external_refund_transaction_best_effort,
    )

    progress = dict(progress or {})
    order_id = str(payload.get("order_id") or "")
    shopify_order_id = str(payload.get("shopify_order_id") or "").strip()
    merchant_id = str(payload.get("merchant_id") or "")

    if not (shopify_order_id and merchant_id):
        # The enqueue guard prevents this; retrying cannot change it.
        return {"skipped": "no_shopify_order_or_merchant"}

    store_info = await _resolve_store_for_refund(payload)

    shop_domain = store_info.get("domain")
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
        store_id=str(store_info.get("store_id") or "").strip() or None,
    )
    if not (shop_domain and access_token):
        raise _RetryableSyncError("could not resolve shopify admin credentials")

    result: Dict[str, Any] = {}

    if progress.get(_STEP_TRANSACTION):
        result["transaction_sync"] = {"skipped": "already_recorded_on_a_prior_attempt"}
    else:
        sync = await ensure_external_refund_transaction_best_effort(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            psp_used=payload.get("psp_used"),
            # NOT coerced with str(): a null PSP reference must stay None so the
            # writer short-circuits to "missing_gateway_or_refund_ref" instead of
            # recording a transaction whose authorization is the string "None".
            external_refund_ref=payload.get("refund_id"),
            amount=float(payload.get("amount") or 0),
            currency=str(payload.get("currency") or "USD"),
            # Carried by producers that already know it. Without it the writer
            # falls back to `_find_successful_parent_transaction`, which only
            # accepts kind in (sale, capture) — so an `authorization`-kind
            # parent recorded in order metadata is invisible to it, the writer
            # soft-skips with ok:True, and the job completes having written
            # nothing.
            parent_transaction_id=payload.get("parent_transaction_id"),
            pivota_order_id=order_id,
        )
        result["transaction_sync"] = sync
        if isinstance(sync, dict):
            reason = str(sync.get("reason") or "").strip()

            if not sync.get("ok") and reason not in _TERMINAL_SYNC_REASONS:
                # The writer's explicit `retryable` refusal (the transaction
                # list was unreadable) AND every bare failure it returns without
                # one, e.g. a 5xx from the create call.
                raise _RetryableSyncError(
                    "refund transaction sync did not succeed: "
                    f"reason={reason or None} error={sync.get('error')}"
                )

            # THE RULE, stated once instead of per-path: a job may only complete
            # when something actually reached the merchant's order. `created`
            # means the refund transaction was written; a dedupe hit means it was
            # already there. Every other outcome wrote nothing, and is allowed to
            # complete ONLY if the fallback annotation landed — which is itself
            # best-effort and returns ok:False on any non-2xx.
            #
            # Three review rounds each found a different path that completed
            # having written nothing and annotated nothing: a bare `ok: False`,
            # a `soft_skipped` whose annotation failed, and a
            # `missing_gateway_or_refund_ref` that returned before any HTTP at
            # all. This predicate covers them together.
            wrote_something = bool(sync.get("ok")) and not sync.get("soft_skipped")
            if not wrote_something:
                annotation = sync.get("annotation")
                if not (isinstance(annotation, dict) and annotation.get("ok")):
                    raise _RetryableSyncError(
                        "nothing reached the merchant's order: the refund "
                        f"transaction was not written (reason={reason or None}) "
                        "and the fallback annotation did not land either"
                    )
                result["transaction_sync_soft_skipped"] = reason
        progress[_STEP_TRANSACTION] = True
        if on_progress is not None:
            await on_progress(progress)

    # Full refund only: cancel the Shopify order for merchant ops visibility. Do
    # NOT ask Shopify to process refunds — the external PSP already moved funds.
    if (
        bool(payload.get("is_partial"))
        or bool(payload.get("skip_cancel"))
        or progress.get(_STEP_CANCEL)
    ):
        return result

    cancel_data = payload.get("cancel_payload")
    if not isinstance(cancel_data, dict) or not cancel_data:
        # Built by refund_api so the Shopify cancel contract has one owner. A job
        # without it can never succeed; retrying only delays the terminal state.
        return {**result, "skipped": "missing_cancel_payload"}

    url = (
        f"https://{shop_domain}/admin/api/{_SHOPIFY_API_VERSION}"
        f"/orders/{shopify_order_id}/cancel.json"
    )
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json=cancel_data,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    body = (response.text or "")[:500]
    if response.status_code == 200:
        result["cancelled"] = True
        progress[_STEP_CANCEL] = True
        if on_progress is not None:
            await on_progress(progress)
        return result

    # The former closure logged a WARNING with the body for EVERY non-200.
    # Keep that: this is the failure class the queue exists to surface.
    logger.warning(
        "merchant_order_sync: shopify cancel returned %s for order=%s shop=%s body=%s",
        response.status_code,
        order_id,
        shop_domain,
        body,
    )

    if response.status_code == 422 and _is_already_cancelled(body):
        # Genuinely nothing left to do; the merchant already has the state.
        result["cancelled"] = False
        result["cancel_terminal"] = "already_cancelled"
        progress[_STEP_CANCEL] = True
        if on_progress is not None:
            await on_progress(progress)
        return result

    # Everything else retries, INCLUDING 404. A 404 here means this order is not
    # on the shop we just authenticated against — a credential or store-binding
    # problem, not proof the work is done. Treating it as success is how a
    # refunded-but-untouched merchant order acquires a positive success record.
    raise _RetryableSyncError(
        f"shopify cancel returned {response.status_code}: {body}"
    )


async def _run_merchant_order_create_job(
    payload: Dict[str, Any],
    progress: Optional[Dict[str, Any]] = None,
    on_progress=None,
) -> Dict[str, Any]:
    """Create the merchant-side order for an order the buyer has already paid.

    Replaces the `background_tasks.add_task(create_shopify_order, ...)` at five
    call sites. Safe to retry: `sync_order_to_connected_store` returns early
    when the order already carries a linked platform order, and takes an
    advisory lock around the create itself.
    """
    from db.orders import get_order
    from routes.order_routes import sync_order_to_connected_store

    order_id = str(payload.get("order_id") or "")
    merchant_id = str(payload.get("merchant_id") or "")
    if not order_id:
        return {"skipped": "no_order_id"}

    if payload.get("require_shopify_primary"):
        # Preserves the Checkout.com webhook's own narrower guard. That path
        # only ever created merchant orders for a Shopify PRIMARY store, even
        # though `sync_order_to_connected_store` also dispatches WooCommerce,
        # BigCommerce and Wix. Widening it here would silently start creating
        # orders on platforms that webhook has never served.
        from services.merchant_store_service import get_primary_store

        store = await get_primary_store(merchant_id)
        if not (store and str(store.get("platform") or "").lower() == "shopify"):
            return {"skipped": "primary_store_not_shopify"}

    if await sync_order_to_connected_store(order_id):
        return {"created": True}

    # It returned False, and on the way it wrote a `merchant_order` marker
    # saying whether the condition is worth retrying. Read that rather than
    # guessing: `wix_order_writeback_not_ready` and a missing bound store are
    # both marked retryable=False, and burning ten attempts on either only
    # delays a page for something no retry fixes.
    order = await get_order(order_id) or {}
    metadata = order.get("metadata") if isinstance(order.get("metadata"), dict) else {}
    merchant_order = metadata.get("merchant_order") or {}
    reason = str(merchant_order.get("last_failure_reason") or "unknown")

    if merchant_order.get("retryable") is False:
        # Completing here does NOT mean the merchant received the order — it
        # means no retry can change that. Unlike the refund path, this failure
        # leaves a queryable trace: the order stays paid with no merchant order,
        # so `paid_missing_merchant_order_count` keeps counting it and the ops
        # retry endpoint can still act on it. That standing signal is what makes
        # completing safe here.
        logger.error(
            "merchant_order_sync: %s cannot be created for order %s (reason=%s, "
            "not retryable); it remains counted by "
            "paid_missing_merchant_order_count",
            OP_MERCHANT_ORDER_CREATE,
            order_id,
            reason,
        )
        return {"created": False, "skipped": reason}

    raise _RetryableSyncError(
        f"merchant order creation returned false for {order_id} (reason={reason})"
    )


_HANDLERS = {
    OP_REFUND_SYNC: _run_refund_sync_job,
    OP_MERCHANT_ORDER_CREATE: _run_merchant_order_create_job,
}


async def run_merchant_order_sync_worker_tick() -> Dict[str, Any]:
    """Claim and process up to MAX_JOBS_PER_TICK jobs.

    Swallows every `Exception`. Does NOT swallow `BaseException` — in 3.11 a
    `CancelledError` from scheduler_job_runner's run deadline is a
    BaseException, so a cut tick escapes here and leaves its job leased, to be
    re-claimed once the lease expires. That is the behaviour we want; noting it
    because "never raises" would be wrong.
    """
    summary = {
        "claimed": 0,
        "done": 0,
        "requeued": 0,
        "failed": 0,
        "lease_lost": 0,
        "write_failed": 0,
    }
    for _ in range(MAX_JOBS_PER_TICK):
        try:
            job = await claim_next_merchant_order_sync_job(worker_id=WORKER_ID)
        except Exception as exc:  # noqa: BLE001
            logger.warning("merchant_order_sync: tick claim failed: %s", str(exc)[:200])
            break
        if job is None:
            break

        summary["claimed"] += 1
        job_id = str(job.get("job_id"))
        op = str(job.get("op") or "")
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 0)
        handler = _HANDLERS.get(op)

        if handler is None:
            # An op this build does not know about. REQUEUE rather than give up:
            # during a rolling deploy an old revision can claim a job enqueued by
            # a new one, and failing it terminally would destroy work the next
            # revision could have done. The attempt budget still bounds it.
            logger.warning(
                "merchant_order_sync: no handler for op=%s job=%s on this build; "
                "requeuing for a revision that has one",
                op,
                job_id,
            )
            status = await fail_merchant_order_sync_job(
                job_id=job_id,
                worker_id=WORKER_ID,
                attempts=attempts,
                max_attempts=max_attempts,
                error=f"no handler for op {op} on this build",
            )
            if status == "lease_lost":
                summary["lease_lost"] += 1
            elif status == "write_failed":
                summary["write_failed"] += 1
            else:
                summary["failed" if status == "failed" else "requeued"] += 1
            continue

        async def _on_progress(progress, _job_id=job_id):
            await record_merchant_order_sync_progress(
                job_id=_job_id, worker_id=WORKER_ID, progress=progress
            )

        try:
            result = await handler(
                job.get("payload") or {},
                job.get("progress") or {},
                _on_progress,
            )
            wrote = await complete_merchant_order_sync_job(
                job_id=job_id, worker_id=WORKER_ID
            )
            if wrote == "written":
                summary["done"] += 1
                logger.info(
                    "merchant_order_sync: %s done job=%s order=%s result=%s",
                    op,
                    job_id,
                    job.get("order_id"),
                    result,
                )
            elif wrote == "write_failed":
                # The work reached Shopify; only the row does not say so. Do NOT
                # report this as a lost lease — nobody else owns the job, it is
                # simply unrecorded until the reaper hands it back.
                summary["write_failed"] += 1
                logger.error(
                    "merchant_order_sync: job=%s order=%s SUCCEEDED but the "
                    "completion could not be recorded; it stays leased until "
                    "expiry and will be redone (idempotently)",
                    job_id,
                    job.get("order_id"),
                )
            else:
                # Our lease expired and a sibling owns the job now. The work
                # itself was idempotent, so the sibling redoing it is safe.
                summary["lease_lost"] += 1
        except Exception as exc:  # noqa: BLE001
            status = await fail_merchant_order_sync_job(
                job_id=job_id,
                worker_id=WORKER_ID,
                attempts=attempts,
                max_attempts=max_attempts,
                error=f"{type(exc).__name__}: {exc}",
            )
            if status == "lease_lost":
                summary["lease_lost"] += 1
            elif status == "write_failed":
                # We could not record the outcome. The row keeps its live lease
                # and `last_error` stays NULL, so recovery is the lease expiry
                # rather than the 30s backoff — say so instead of reporting a
                # requeue that did not happen.
                summary["write_failed"] += 1
                logger.error(
                    "merchant_order_sync: could not record failure for job=%s "
                    "order=%s; row stays leased until it expires: %s",
                    job_id,
                    job.get("order_id"),
                    str(exc)[:300],
                )
            elif status == "failed":
                summary["failed"] += 1
                # Terminal on this queue means the buyer was refunded and the
                # merchant's store was never told. That is an incident.
                logger.error(
                    "merchant_order_sync: GAVE UP on %s job=%s order=%s after %s "
                    "attempts — merchant store not updated: %s",
                    op,
                    job_id,
                    job.get("order_id"),
                    attempts,
                    str(exc)[:300],
                )
            else:
                summary["requeued"] += 1
                logger.warning(
                    "merchant_order_sync: %s job=%s attempt %s failed, requeued: %s",
                    op,
                    job_id,
                    attempts,
                    str(exc)[:300],
                )

    if summary["claimed"]:
        logger.info("merchant_order_sync tick summary: %s", summary)
    return summary


async def run_merchant_order_sync_lease_reaper_tick() -> int:
    """Release leases held by workers that died. Never raises."""
    try:
        released = await release_stale_merchant_order_sync_leases()
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_sync: reaper tick failed: %s", str(exc)[:200])
        return 0
    if released:
        logger.warning(
            "merchant_order_sync: released %s stale lease(s)", released
        )
    return released
