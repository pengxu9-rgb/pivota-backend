"""
Refund Processing API
Handles full and partial refunds for orders
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response, status
from pydantic import BaseModel
from typing import Optional, Dict, Any
import uuid
from decimal import Decimal
from datetime import datetime

from db.orders import get_order, update_order, update_order_status
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from utils.auth import require_admin, require_admin_or_key
from adapters.psp_adapter import get_psp_adapter
from utils.logger import logger
from services.shopify_transactions_service import (
    ensure_external_refund_transaction_best_effort,
)
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.merchant_webhook_service import emit_merchant_webhook_event
from db.merchant_order_sync_jobs import (
    OP_REFUND_SYNC,
    enqueue_merchant_order_sync_job,
)
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    fetch_active_runtime_merchant_psp,
    infer_runtime_provider,
)
from services.psp_payment_finalizer import finalize_refund_success
from services.refund_observability import (
    collect_refund_ids,
    build_order_refund_tracking_payload,
    extract_stripe_refund_snapshot,
    merge_refund_metadata,
    stripe_refund_metadata_patch,
)


router = APIRouter(prefix="/orders", tags=["refunds"])


class RefundRequest(BaseModel):
    """Refund request model"""
    order_id: str
    amount: Optional[float] = None  # None = full refund
    reason: Optional[str] = None
    restore_inventory: bool = True  # Whether to restore Shopify inventory
    idempotency_key: Optional[str] = None  # Best-effort duplicate protection


_SHOPIFY_CANCEL_REASONS = {"customer", "inventory", "fraud", "declined", "other"}


def _shopify_external_refund_cancel_payload(
    *,
    reason: Optional[str],
    restore_inventory: bool,
) -> Dict[str, Any]:
    """
    Build a Shopify order-cancel payload after Pivota/PSP already processed funds.

    Do not include amount/currency here: those fields ask Shopify to reason about
    refund money, while this path only needs merchant-side cancel/restock state.
    """
    normalized_reason = str(reason or "").strip().lower()
    if normalized_reason not in _SHOPIFY_CANCEL_REASONS:
        normalized_reason = "other"
    return {
        "reason": normalized_reason,
        "email": False,
        "refund": False,
        "restock": bool(restore_inventory),
    }


async def _resolve_refund_adapter(order: Dict[str, Any]) -> tuple[str, str, Dict[str, Any]]:
    order_psp_type = infer_runtime_provider(
        psp_used=order.get("psp_used"),
        psp_id=order.get("psp_id"),
        payment_reference=order.get("payment_intent_id"),
    )
    merchant_id = str(order.get("merchant_id") or "")
    order_psp_id = str(order.get("psp_id") or "").strip()
    canonical_row = await fetch_active_runtime_merchant_psp(
        merchant_id=merchant_id,
        provider=order_psp_type,
        psp_id=order_psp_id,
    )

    if canonical_row is not None:
        canonical = dict(canonical_row)
        psp_type = str(canonical.get("provider") or order_psp_type or "").strip().lower()
        psp_key = str(canonical.get("runtime_secret_key") or "").strip()
        if psp_type and psp_key:
            return (
                psp_type,
                psp_key,
                build_runtime_adapter_kwargs(
                    psp_type,
                    api_key=psp_key,
                    account_id=canonical.get("account_id"),
                    provider_config=canonical.get("provider_config"),
                    environment=canonical.get("environment"),
                    secret_key=canonical.get("runtime_secret_key"),
                ),
            )

    if order_psp_type in {"stripe", "adyen", "checkout", "paypal"}:
        raise ValueError(
            f"Canonical merchant_psps configuration is missing for {order_psp_type} refunds"
        )
    raise ValueError("Canonical merchant_psps configuration is missing for this refund")


async def _refresh_stripe_refund_observability_for_order(
    order: Dict[str, Any],
) -> Dict[str, Any]:
    psp_type, psp_key, adapter_kwargs = await _resolve_refund_adapter(order)
    if psp_type != "stripe":
        raise ValueError(f"Refund telemetry refresh only supports stripe orders; found {psp_type}")

    psp_adapter = get_psp_adapter(psp_type, psp_key, **adapter_kwargs)
    if not hasattr(psp_adapter, "get_refund_details"):
        raise ValueError("Stripe refund detail retrieval is unavailable")

    metadata = order.get("metadata") or {}
    refund_ids = collect_refund_ids(metadata, provider="stripe")
    refreshed_snapshots = []
    errors = []

    for refund_id in refund_ids:
        try:
            ok, refund_details, error = await psp_adapter.get_refund_details(refund_id)
        except Exception as exc:
            ok, refund_details, error = False, None, str(exc)
        if not ok or not refund_details:
            errors.append({"refund_id": refund_id, "error": error or "refund_not_found"})
            continue

        snapshot = extract_stripe_refund_snapshot(
            refund_details,
            source_event="refund.refresh",
        )
        metadata = merge_refund_metadata(
            metadata,
            stripe_refund_metadata_patch(
                snapshot,
                existing_metadata=metadata,
            ),
        )
        refreshed_snapshots.append(snapshot)

    updated = False
    if refreshed_snapshots:
        await update_order(str(order.get("order_id")), {"metadata": metadata})
        refreshed_order = await get_order(str(order.get("order_id")))
        if refreshed_order:
            order = refreshed_order
        else:
            order = {**order, "metadata": metadata}
        updated = True

    return {
        "order": order,
        "refund_ids": refund_ids,
        "refreshed_count": len(refreshed_snapshots),
        "refreshed": refreshed_snapshots,
        "errors": errors,
        "updated": updated,
        "refund_tracking": build_order_refund_tracking_payload(
            order,
            psp_used=order.get("psp_used"),
        ),
    }


@router.post("/{order_id}/refund")
async def process_refund(
    order_id: str,
    refund_request: RefundRequest,
    background_tasks: BackgroundTasks,
    response: Response = None,
    current_user: dict = Depends(require_admin)
):
    """
    Process a refund for an order
    
    Supports:
    - Full refunds (amount = None)
    - Partial refunds (amount = specific value)
    - Inventory restoration
    - Shopify order cancellation
    """
    
    # Get order
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    store_info: Optional[Dict[str, Any]] = None
    try:
        store_info = await get_primary_store(order.get("merchant_id"))
    except Exception:
        store_info = None
    
    # Check if order is in a refundable financial state
    # Note: partial refunds transition payment_status to "partially_refunded".
    if str(order.get("payment_status") or "").lower() not in (
        "paid",
        "completed",
        "partially_refunded",
        "refunded",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot refund unpaid order. Current status: {order['payment_status']}"
        )
    
    order_total = Decimal(str(order.get("total") or "0"))
    total_refunded = Decimal(str(order.get("total_refunded") or "0"))
    remaining = order_total - total_refunded

    # Check if already fully refunded (cumulative)
    if remaining <= Decimal("0"):
        return {
            "status": "already_refunded",
            "message": "Order was already refunded",
            "order_id": order_id,
        }
    
    # Get merchant
    merchant = await get_merchant_onboarding(order["merchant_id"])
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Calculate refund amount: None means "refund remaining", not necessarily full order total.
    refund_amount = Decimal(str(refund_request.amount)) if refund_request.amount is not None else remaining
    if refund_amount <= Decimal("0"):
        raise HTTPException(status_code=400, detail="Refund amount must be > 0")
    
    # Validate refund amount against remaining refundable amount
    if refund_amount > remaining:
        raise HTTPException(
            status_code=400,
            detail=f"Refund amount ${refund_amount} exceeds remaining refundable amount ${remaining}"
        )
    
    try:
        # Idempotency (best-effort): if key is provided and we've already processed this request,
        # return the cached response and do not attempt side effects again.
        if refund_request.idempotency_key:
            try:
                from mvp.idempotency import PostgresIdempotencyStore

                idem = PostgresIdempotencyStore()
                existing = await idem.get(scope="refund", key=refund_request.idempotency_key)
                if existing:
                    return existing.value
            except Exception:
                pass

        # MVP measurement scaffolding: refund requested (metadata-only).
        try:
            from mvp.constants import EVENT_REFUND_REQUESTED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            emit_best_effort(
                event_type=EVENT_REFUND_REQUESTED,
                payload={
                    "order_id": order_id,
                    "merchant_id": order.get("merchant_id"),
                    "amount": str(refund_amount),
                    "currency": order.get("currency"),
                    "reason": refund_request.reason,
                    "idempotency_key": refund_request.idempotency_key,
                },
                merchant_id=order.get("merchant_id"),
                geo=None,
                surface=SURFACE_BACKEND,
                adapter="refund_api",
                risk_tier="unknown",
                idempotency_key=refund_request.idempotency_key,
            )
        except Exception:
            pass

        # MVP ledger event (best-effort): refund requested.
        try:
            from mvp.ledger_events import emit_ledger_event_best_effort

            emit_ledger_event_best_effort(
                merchant_id=str(order.get("merchant_id")),
                event_type="refund_requested",
                order_id=str(order_id),
                source={"type": "backend"},
                amount={"value": float(refund_amount), "currency": str(order.get("currency") or "USD")},
                refs={"payment_intent_id": order.get("payment_intent_id")},
                geo=None,
                surface="backend",
                adapter="refund_api",
                risk_tier="unknown",
                idempotency_key=refund_request.idempotency_key,
            )
        except Exception:
            pass

        # PCS v0.2-b (best-effort): internal refund fact for reducer replay (no PII).
        try:
            from services.pcs_fact_ingest import append_internal_fact_best_effort

            await append_internal_fact_best_effort(
                merchant_id=str(order.get("merchant_id")),
                order_id=str(order_id),
                fact_type="internal.refund_requested",
                payload={
                    "order_id": str(order_id),
                    "merchant_id": str(order.get("merchant_id")),
                    "amount": float(refund_amount),
                    "currency": str(order.get("currency") or "USD"),
                    "reason": refund_request.reason,
                    "idempotency_key": refund_request.idempotency_key,
                },
                idempotency_key=refund_request.idempotency_key or f"{order_id}:{str(refund_amount)}",
            )
        except Exception:
            pass

        psp_type, psp_key, adapter_kwargs = await _resolve_refund_adapter(order)
        
        if not psp_key:
            raise ValueError(f"No PSP key found for merchant {order['merchant_id']}")
        
        psp_adapter = get_psp_adapter(psp_type, psp_key, **adapter_kwargs)
        
        # Process refund through PSP
        # For Stripe: passing amount=None refunds the full PaymentIntent amount, which is only safe
        # when no prior refunds exist. Otherwise always pass an explicit amount.
        psp_refund_amount = None
        try:
            if total_refunded <= Decimal("0") and refund_amount >= order_total:
                psp_refund_amount = None
            else:
                psp_refund_amount = refund_amount
        except Exception:
            psp_refund_amount = refund_amount

        success, refund_id, error = await psp_adapter.refund_payment(
            payment_intent_id=order["payment_intent_id"],
            amount=psp_refund_amount,
            reason=refund_request.reason,
            idempotency_key=refund_request.idempotency_key,
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Refund failed: {error}")
        
        next_total_refunded = total_refunded + refund_amount
        new_status = "refunded" if next_total_refunded >= order_total else "partially_refunded"
        is_partial = new_status == "partially_refunded"
        stripe_refund_snapshot: Optional[Dict[str, Any]] = None
        if psp_type == "stripe" and refund_id and hasattr(psp_adapter, "get_refund_details"):
            try:
                details_ok, refund_details, _details_error = await psp_adapter.get_refund_details(str(refund_id))
                if details_ok and refund_details:
                    stripe_refund_snapshot = extract_stripe_refund_snapshot(
                        refund_details,
                        source_event="refund.api",
                    )
            except Exception:
                stripe_refund_snapshot = None
        
        finalize_failed = False
        finalize_error = None
        try:
            await finalize_refund_success(
                order,
                psp=psp_type,
                refund_reference=str(refund_id),
                refund_amount=str(refund_amount),
                currency=str(order.get("currency") or "USD"),
                source_event="refund_processed",
                metadata_extra={
                    "refund_id": str(refund_id),
                    "refund_reason": refund_request.reason,
                    "refunded_by": current_user.get("user_id", "admin"),
                    "source_event": "refund_processed",
                    **(stripe_refund_snapshot or {}),
                },
                metadata_patch=(
                    stripe_refund_metadata_patch(
                        stripe_refund_snapshot,
                        existing_metadata=order.get("metadata"),
                    )
                    if stripe_refund_snapshot
                    else None
                ),
                update_order_status_fn=update_order_status,
                log_order_event_fn=log_order_event,
            )
        except Exception as fin_exc:
            finalize_failed = True
            finalize_error = str(fin_exc)
            logger.error(
                {
                    "event": "refund_finalize_failed_after_psp_refund_succeeded",
                    "order_id": order_id,
                    "refund_id": refund_id,
                    "error": finalize_error,
                }
            )

        # PCS v0.2-b (best-effort): internal refund processed fact for reducer replay (no PII).
        try:
            from services.pcs_fact_ingest import append_internal_fact_best_effort

            await append_internal_fact_best_effort(
                merchant_id=str(order.get("merchant_id")),
                order_id=str(order_id),
                fact_type="internal.refund_processed",
                payload={
                    "order_id": str(order_id),
                    "merchant_id": str(order.get("merchant_id")),
                    "status": str(new_status),
                    "amount": float(refund_amount),
                    "currency": str(order.get("currency") or "USD"),
                    "refund_id": str(refund_id),
                },
                idempotency_key=refund_request.idempotency_key or str(refund_id),
            )
        except Exception:
            pass
        
        # MVP ledger event (best-effort): refund completed.
        try:
            from mvp.ledger_events import emit_ledger_event_best_effort

            emit_ledger_event_best_effort(
                merchant_id=str(order.get("merchant_id")),
                event_type="refund_completed",
                order_id=str(order_id),
                source={"type": "psp", "psp": psp_type, "external_event_id": refund_id},
                amount={"value": float(refund_amount), "currency": str(order.get("currency") or "USD")},
                refs={"payment_intent_id": order.get("payment_intent_id"), "psp_transaction_id": refund_id},
                geo=None,
                surface="backend",
                adapter="refund_api",
                risk_tier="unknown",
                idempotency_key=refund_request.idempotency_key,
            )
        except Exception:
            pass
        
        # Durable enqueue — replaces `background_tasks.add_task`.
        #
        # A refund whose Shopify cancel never fired leaves NO state a reconciler
        # could find: the order is refunded and still carries its
        # shopify_order_id, exactly like one whose cancel succeeded. Unlike the
        # paid-order-missing-a-merchant-order case, there is nothing to select
        # for after the fact, so the intent has to be recorded durably here, at
        # the moment it is formed. Drained by
        # services/merchant_order_sync_drain.py under a lease, with backoff.
        #
        # The guard mirrors the former task's early return, so a merchant with
        # no Shopify store still queues nothing.
        if (
            order.get("shopify_order_id")
            and store_info
            and store_info.get("platform") == "shopify"
        ):
            enqueued_job_id = await enqueue_merchant_order_sync_job(
                order_id=str(order_id),
                merchant_id=str(order.get("merchant_id") or ""),
                op=OP_REFUND_SYNC,
                # One job per PSP refund: a retried refund request that reuses
                # the same refund_id must not queue the work twice.
                # `refund_id` can legitimately be empty when an adapter reports
                # success without a reference. Coercing it with str() would key
                # every such refund on the literal "None", so a second one would
                # collide on the unique index, ON CONFLICT would return the FIRST
                # job's id, and its sync would be silently dropped.
                dedupe_key=(
                    str(refund_id).strip()
                    or str(refund_request.idempotency_key or "").strip()
                    or f"noref-{uuid.uuid4()}"
                ),
                payload={
                    "order_id": str(order_id),
                    "merchant_id": str(order.get("merchant_id") or ""),
                    "shopify_order_id": str(order.get("shopify_order_id") or ""),
                    # The store the order was BOUND to at checkout. The worker
                    # prefers it over the primary store, per get_store_by_id's
                    # own guidance, so a multi-store merchant is not cancelled
                    # against the wrong shop.
                    "store_id": str(order.get("store_id") or "").strip() or None,
                    "psp_used": order.get("psp_used") or psp_type,
                    # Raw, NOT str(): a null reference must stay null so the
                    # transaction writer short-circuits instead of recording an
                    # authorization of "None".
                    "refund_id": refund_id,
                    "amount": float(refund_amount),
                    "currency": str(order.get("currency") or "USD"),
                    "is_partial": bool(is_partial),
                    # Built here so the Shopify cancel contract stays owned by
                    # this module rather than being restated in the worker.
                    "cancel_payload": _shopify_external_refund_cancel_payload(
                        reason=refund_request.reason,
                        restore_inventory=refund_request.restore_inventory,
                    ),
                },
            )
            # enqueue returns None only on a persistence failure, which it logs
            # at ERROR. It does not raise: the PSP has already moved funds, so
            # failing the request here would turn lost follow-up work into a 5xx
            # on a refund that actually succeeded.
            if enqueued_job_id is None:
                try:
                    await log_order_event(
                        event_type="merchant_order_sync_enqueue_failed",
                        order_id=str(order_id),
                        merchant_id=str(order.get("merchant_id") or ""),
                        total_amount=float(refund_amount),
                        currency=str(order.get("currency") or "USD"),
                        metadata={"op": OP_REFUND_SYNC, "refund_id": str(refund_id)},
                    )
                except Exception:
                    pass

        try:
            await emit_merchant_webhook_event(
                str(order.get("merchant_id")),
                event_type="refund.processed",
                payload={
                    "order_id": str(order_id),
                    "merchant_id": str(order.get("merchant_id")),
                    "refund_id": str(refund_id),
                    "amount": float(refund_amount),
                    "currency": str(order.get("currency") or "USD"),
                    "is_partial": is_partial,
                    "status": new_status,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to emit merchant refund.processed webhook for %s: %s",
                order.get("merchant_id"),
                exc,
            )

        response_payload = {
            "status": "success",
            "message": f"{'Partial refund' if is_partial else 'Full refund'} processed successfully",
            "order_id": order_id,
            "refund_id": refund_id,
            "refund_amount": str(refund_amount),
            "original_amount": str(order["total"]),
            "is_partial": is_partial,
            "new_order_status": new_status,
            "total_refunded": str(next_total_refunded),
            "remaining_refundable": str(max(order_total - next_total_refunded, Decimal('0'))),
        }
        if stripe_refund_snapshot:
            response_payload["psp_refund"] = build_order_refund_tracking_payload(
                {"metadata": stripe_refund_metadata_patch(stripe_refund_snapshot), "psp_used": "stripe"},
                psp_used="stripe",
            )

        if finalize_failed:
            if response is not None:
                response.status_code = status.HTTP_207_MULTI_STATUS
            response_payload = {
                "status": "partial_failure",
                "refund_id": refund_id,
                "psp_refund_id": refund_id,
                "manual_reconciliation_required": True,
                "error": finalize_error,
            }

        # Best-effort idempotency record
        if refund_request.idempotency_key:
            try:
                from mvp.idempotency import PostgresIdempotencyStore

                idem = PostgresIdempotencyStore()
                await idem.put(scope="refund", key=refund_request.idempotency_key, value=response_payload)
            except Exception:
                pass

        return response_payload
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Refund processing error: {e}")
        raise HTTPException(status_code=500, detail=f"Refund failed: {str(e)}")


@router.get("/{order_id}/refund-status")
async def get_refund_status(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """Check if an order has been refunded and get refund details"""
    
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    is_refunded = order.get("status") in ["refunded", "partially_refunded"]
    
    refund_info = {
        "order_id": order_id,
        "is_refunded": is_refunded,
        "refund_status": order.get("status"),
        "original_amount": str(order["total"]),
        "total_refunded": str(order.get("total_refunded") or 0),
        "currency": order["currency"]
    }
    
    if is_refunded and order.get("metadata"):
        metadata = order["metadata"]
        refund_info.update({
            "refund_id": metadata.get("refund_id"),
            "refund_amount": metadata.get("refund_amount"),
            "refund_reason": metadata.get("refund_reason"),
            "refunded_at": metadata.get("refunded_at") or (order.get("refunded_at").isoformat() if order.get("refunded_at") else None),
            "refunded_by": metadata.get("refunded_by")
        })
    
    return {
        "status": "success",
        "refund": refund_info
    }


@router.post("/{order_id}/refund-observability/refresh")
async def refresh_refund_observability(
    order_id: str,
    current_user: dict = Depends(require_admin_or_key)
):
    """Backfill Stripe refund telemetry for historical refunded orders."""

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        result = await _refresh_stripe_refund_observability_for_order(order)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error(f"Refund observability refresh failed for {order_id}: {exc}")
        raise HTTPException(status_code=500, detail="Refund observability refresh failed")

    return {
        "status": "success",
        "order_id": order_id,
        "refreshed_count": result["refreshed_count"],
        "refund_ids": result["refund_ids"],
        "errors": result["errors"],
        "updated": result["updated"],
        "refund": result["refund_tracking"],
        "requested_by": current_user.get("user_id", "admin"),
    }
