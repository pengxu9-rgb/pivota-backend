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
    """Raised when the job should be re-queued rather than treated as done."""


class _TerminalSyncError(Exception):
    """Raised when no number of retries can change the outcome.

    Burning the whole attempt budget on these costs 2h of backoff and then
    fires the `GAVE UP` money-path incident, which should mean "we tried and
    Shopify would not take it", not "this job was never going to work".
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
    from services.merchant_store_service import get_primary_store, get_store_by_id

    merchant_id = str(payload.get("merchant_id") or "")
    bound_store_id = str(payload.get("store_id") or "").strip() or None

    if bound_store_id:
        store = await get_store_by_id(bound_store_id, merchant_id=merchant_id)
        if not store:
            raise _TerminalSyncError(
                f"bound store {bound_store_id} is missing or inactive; refusing "
                "to fall back to the primary store"
            )
        return store

    store = await get_primary_store(merchant_id)
    if not store:
        raise _RetryableSyncError("no primary store is readable for this merchant")
    return store


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
    if str(store_info.get("platform") or "").lower() != "shopify":
        raise _RetryableSyncError(
            f"bound store is platform={store_info.get('platform')!r}, not shopify"
        )

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
            pivota_order_id=order_id,
        )
        result["transaction_sync"] = sync
        if isinstance(sync, dict) and not sync.get("ok"):
            reason = str(sync.get("reason") or "").strip()
            if reason not in _TERMINAL_SYNC_REASONS:
                # Covers the writer's explicit `retryable` refusal (the
                # transaction list was unreadable) AND every bare failure it
                # returns without one, e.g. a 5xx from the create call.
                raise _RetryableSyncError(
                    "refund transaction sync did not succeed: "
                    f"reason={reason or None} error={sync.get('error')}"
                )
            # Nothing to write and no retry can change that; record it as its
            # own outcome rather than letting it read as a clean success.
            result["transaction_sync_skipped"] = reason
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


_HANDLERS = {OP_REFUND_SYNC: _run_refund_sync_job}


async def run_merchant_order_sync_worker_tick() -> Dict[str, Any]:
    """Claim and process up to MAX_JOBS_PER_TICK jobs.

    Swallows every `Exception`. Does NOT swallow `BaseException` — in 3.11 a
    `CancelledError` from scheduler_job_runner's run deadline is a
    BaseException, so a cut tick escapes here and leaves its job leased, to be
    re-claimed once the lease expires. That is the behaviour we want; noting it
    because "never raises" would be wrong.
    """
    summary = {"claimed": 0, "done": 0, "requeued": 0, "failed": 0, "lease_lost": 0}
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
            if wrote:
                summary["done"] += 1
                logger.info(
                    "merchant_order_sync: %s done job=%s order=%s result=%s",
                    op,
                    job_id,
                    job.get("order_id"),
                    result,
                )
            else:
                # Our lease expired and a sibling owns the job now. The work
                # itself was idempotent, so the sibling redoing it is safe.
                summary["lease_lost"] += 1
        except Exception as exc:  # noqa: BLE001
            # A terminal error spends the whole budget at once: retrying cannot
            # change it, and the operator should see it now rather than in 2h.
            spent = max_attempts if isinstance(exc, _TerminalSyncError) else attempts
            status = await fail_merchant_order_sync_job(
                job_id=job_id,
                worker_id=WORKER_ID,
                attempts=spent,
                max_attempts=max_attempts,
                error=f"{type(exc).__name__}: {exc}",
            )
            if status == "lease_lost":
                summary["lease_lost"] += 1
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
