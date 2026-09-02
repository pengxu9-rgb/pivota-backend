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
from typing import Any, Dict

from db.merchant_order_sync_jobs import (
    OP_REFUND_SYNC,
    claim_next_merchant_order_sync_job,
    complete_merchant_order_sync_job,
    fail_merchant_order_sync_job,
    release_stale_merchant_order_sync_leases,
)
from utils.logger import logger

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Bounded per tick so one backlog cannot monopolise the scheduler thread.
MAX_JOBS_PER_TICK = 20

_SHOPIFY_API_VERSION = "2025-10"


class _RetryableSyncError(Exception):
    """Raised when the job should be re-queued rather than treated as done."""


async def _run_refund_sync_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror of the former `update_shopify_order_task` closure in refund_api.

    Behaviour is deliberately identical to the background-task version, including
    resolving the store with `get_primary_store`. NOTE: `get_store_by_id`'s own
    docstring says downstream jobs should prefer the order's bound `store_id` to
    avoid cross-store token/domain mismatches on multi-store merchants; the
    refund path has never done that. Left as-is here so this change delivers
    exactly one behavioural difference — durability — and the store-resolution
    question can be decided on its own.
    """
    import httpx

    from services.merchant_store_service import get_primary_store
    from services.shopify_access_token_service import resolve_shopify_admin_access_token
    from services.shopify_transactions_service import (
        ensure_external_refund_transaction_best_effort,
    )

    order_id = str(payload.get("order_id") or "")
    shopify_order_id = str(payload.get("shopify_order_id") or "").strip()
    merchant_id = str(payload.get("merchant_id") or "")

    if not (shopify_order_id and merchant_id):
        # The enqueue guard should prevent this; nothing to do and retrying
        # cannot change it.
        return {"skipped": "no_shopify_order_or_merchant"}

    store_info = await get_primary_store(merchant_id)
    if not (store_info and store_info.get("platform") == "shopify"):
        # A store that is momentarily unreadable and a merchant who genuinely has
        # no Shopify store are indistinguishable here. Retry: the backoff is
        # bounded and a terminal failure is visible, which is the safer error.
        raise _RetryableSyncError("primary store is not a readable shopify store")

    shop_domain = store_info.get("domain")
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
        store_id=str(store_info.get("store_id") or "").strip() or None,
    )
    if not (shop_domain and access_token):
        raise _RetryableSyncError("could not resolve shopify admin credentials")

    result: Dict[str, Any] = {}
    result["transaction_sync"] = await ensure_external_refund_transaction_best_effort(
        shop_domain=shop_domain,
        access_token=access_token,
        shopify_order_id=shopify_order_id,
        psp_used=payload.get("psp_used"),
        external_refund_ref=payload.get("refund_id"),
        amount=float(payload.get("amount") or 0),
        currency=str(payload.get("currency") or "USD"),
        pivota_order_id=order_id,
    )

    # Full refund only: cancel the Shopify order for merchant ops visibility. Do
    # NOT ask Shopify to process refunds — the external PSP already moved funds.
    if not bool(payload.get("is_partial")):
        url = (
            f"https://{shop_domain}/admin/api/{_SHOPIFY_API_VERSION}"
            f"/orders/{shopify_order_id}/cancel.json"
        )
        # Built by routes/refund_api.py via
        # `_shopify_external_refund_cancel_payload`, so the Shopify cancel
        # contract has exactly one owner and cannot drift between the enqueue
        # site and this worker.
        cancel_data = payload.get("cancel_payload")
        if not isinstance(cancel_data, dict) or not cancel_data:
            # A job with no cancel payload can never succeed; retrying it eight
            # times would only delay the terminal failure.
            return {"skipped": "missing_cancel_payload"}
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
        if response.status_code == 200:
            result["cancelled"] = True
        elif response.status_code in (422, 404):
            # Already cancelled, or the order is not cancellable. Retrying cannot
            # change either, and both mean there is no outstanding merchant work.
            result["cancelled"] = False
            result["cancel_terminal"] = response.status_code
        else:
            raise _RetryableSyncError(
                f"shopify cancel returned {response.status_code}: "
                f"{(response.text or '')[:200]}"
            )

    return result


_HANDLERS = {OP_REFUND_SYNC: _run_refund_sync_job}


async def run_merchant_order_sync_worker_tick() -> Dict[str, Any]:
    """Claim and process up to MAX_JOBS_PER_TICK jobs. Never raises."""
    summary = {"claimed": 0, "done": 0, "requeued": 0, "failed": 0}
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
        handler = _HANDLERS.get(op)

        if handler is None:
            # An op this build does not know about. Do not burn attempts on it.
            logger.error("merchant_order_sync: no handler for op=%s job=%s", op, job_id)
            await fail_merchant_order_sync_job(
                job_id=job_id,
                attempts=int(job.get("max_attempts") or 0),
                max_attempts=int(job.get("max_attempts") or 0),
                error=f"no handler for op {op}",
            )
            summary["failed"] += 1
            continue

        try:
            result = await handler(job.get("payload") or {})
            await complete_merchant_order_sync_job(job_id=job_id)
            summary["done"] += 1
            logger.info(
                "merchant_order_sync: %s done job=%s order=%s result=%s",
                op,
                job_id,
                job.get("order_id"),
                result,
            )
        except Exception as exc:  # noqa: BLE001
            status = await fail_merchant_order_sync_job(
                job_id=job_id,
                attempts=int(job.get("attempts") or 0),
                max_attempts=int(job.get("max_attempts") or 0),
                error=f"{type(exc).__name__}: {exc}",
            )
            if status == "failed":
                summary["failed"] += 1
                # Terminal on this queue means the buyer was refunded and the
                # merchant's store was never told. That is an incident.
                logger.error(
                    "merchant_order_sync: GAVE UP on %s job=%s order=%s after %s "
                    "attempts — merchant store not updated: %s",
                    op,
                    job_id,
                    job.get("order_id"),
                    job.get("attempts"),
                    str(exc)[:300],
                )
            else:
                summary["requeued"] += 1
                logger.warning(
                    "merchant_order_sync: %s job=%s attempt %s failed, requeued: %s",
                    op,
                    job_id,
                    job.get("attempts"),
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
