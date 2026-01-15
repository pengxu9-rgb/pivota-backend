"""
Refund Processing API
Handles full and partial refunds for orders
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Dict, Any
from decimal import Decimal
from datetime import datetime

from db.orders import get_order, update_order_status
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from utils.auth import require_admin
from adapters.psp_adapter import get_psp_adapter
from config.settings import settings
from utils.logger import logger
from services.shopify_transactions_service import (
    extract_shopify_access_token,
    ensure_external_refund_transaction_best_effort,
)


router = APIRouter(prefix="/orders", tags=["refunds"])


class RefundRequest(BaseModel):
    """Refund request model"""
    order_id: str
    amount: Optional[float] = None  # None = full refund
    reason: Optional[str] = None
    restore_inventory: bool = True  # Whether to restore Shopify inventory
    idempotency_key: Optional[str] = None  # Best-effort duplicate protection


@router.post("/{order_id}/refund")
async def process_refund(
    order_id: str,
    refund_request: RefundRequest,
    background_tasks: BackgroundTasks,
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

        # Get PSP adapter (refund must match the PSP that actually processed this order)
        order_psp_type = str(order.get("psp_used") or "").strip().lower() or None
        if not order_psp_type:
            psp_id = str(order.get("psp_id") or "").strip().lower()
            if psp_id.startswith("psp_stripe"):
                order_psp_type = "stripe"
            elif psp_id.startswith("psp_adyen"):
                order_psp_type = "adyen"

        if not order_psp_type:
            payment_intent_id = str(order.get("payment_intent_id") or "")
            if payment_intent_id.startswith("pi_"):
                order_psp_type = "stripe"

        merchant_psp_type = str(merchant.get("psp_type") or "").strip().lower() or None
        psp_type = order_psp_type or merchant_psp_type or "stripe"

        # Only use merchant-stored PSP keys if they correspond to the same PSP type.
        psp_key = None
        if merchant_psp_type and merchant_psp_type == psp_type:
            psp_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")

        if not psp_key:
            if psp_type == "stripe":
                psp_key = settings.stripe_secret_key
            else:
                psp_key = settings.adyen_api_key
        
        if not psp_key:
            raise ValueError(f"No PSP key found for merchant {merchant['merchant_id']}")
        
        psp_adapter = get_psp_adapter(psp_type, psp_key)
        
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
        
        # Update order status + totals (cumulative)
        next_total_refunded = total_refunded + refund_amount
        new_status = "refunded" if next_total_refunded >= order_total else "partially_refunded"
        is_partial = new_status == "partially_refunded"
        
        try:
            await update_order_status(
                order_id=order_id,
                status=new_status,
                payment_status=new_status,
                total_refunded=next_total_refunded,
                metadata={
                    **(order.get("metadata") or {}),
                    "refund_id": refund_id,
                    "refund_amount": str(refund_amount),
                    "refund_reason": refund_request.reason,
                    "refunded_by": current_user.get("user_id", "admin"),
                    "refunded_at": datetime.now().isoformat(),
                    "total_refunded": str(next_total_refunded),
                },
            )
        except Exception:
            # Best-effort schema self-heal for legacy DBs missing total_refunded
            try:
                from sqlalchemy import text
                from db.database import database as _db

                await _db.execute(
                    text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;")
                )
                await update_order_status(
                    order_id=order_id,
                    status=new_status,
                    payment_status=new_status,
                    total_refunded=next_total_refunded,
                    metadata={
                        **(order.get("metadata") or {}),
                        "refund_id": refund_id,
                        "refund_amount": str(refund_amount),
                        "refund_reason": refund_request.reason,
                        "refunded_by": current_user.get("user_id", "admin"),
                        "refunded_at": datetime.now().isoformat(),
                        "total_refunded": str(next_total_refunded),
                    },
                )
            except Exception:
                # Do not fail the refund response if persistence fails.
                pass

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
        
        # Log refund event
        await log_order_event(
            event_type="refund_processed",
            order_id=order_id,
            merchant_id=order["merchant_id"],
            metadata={
                "refund_id": refund_id,
                "refund_amount": str(refund_amount),
                "is_partial": is_partial,
                "reason": refund_request.reason
            }
        )

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
        
        # Background task: Cancel/update Shopify order
        async def update_shopify_order_task():
            """Update or cancel Shopify order after refund"""
            try:
                if not (order.get("shopify_order_id") and store_info and store_info.get("platform") == "shopify"):
                    return

                shop_domain = store_info.get("domain")
                access_token = extract_shopify_access_token(store_info.get("api_key"))
                if not (shop_domain and access_token):
                    return

                await ensure_external_refund_transaction_best_effort(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    shopify_order_id=str(order["shopify_order_id"]),
                    psp_used=order.get("psp_used") or psp_type,
                    external_refund_ref=refund_id,
                    amount=float(refund_amount),
                    currency=str(order.get("currency") or "USD"),
                    pivota_order_id=order_id,
                )

                # Full refund: optionally cancel the Shopify order for merchant ops visibility,
                # but do NOT ask Shopify to process refunds (external PSP already handled it).
                if not is_partial:
                    import httpx

                    url = f"https://{shop_domain}/admin/api/2024-01/orders/{order['shopify_order_id']}/cancel.json"
                    headers_shopify = {
                        "X-Shopify-Access-Token": access_token,
                        "Content-Type": "application/json",
                    }
                    cancel_data = {
                        "amount": str(refund_amount),
                        "currency": str(order.get("currency") or "USD"),
                        "reason": refund_request.reason or "customer_request",
                        "email": True,
                        "refund": False,
                    }

                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            url,
                            json=cancel_data,
                            headers=headers_shopify,
                            timeout=10.0,
                        )
                        if response.status_code == 200:
                            logger.info(f"Shopify order {order['shopify_order_id']} cancelled")
                        else:
                            logger.warning(
                                f"Failed to cancel Shopify order: {response.status_code}"
                            )
                                
            except Exception as e:
                logger.error(f"Error updating Shopify order after refund: {e}")
        
        background_tasks.add_task(update_shopify_order_task)

        response = {
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

        # Best-effort idempotency record
        if refund_request.idempotency_key:
            try:
                from mvp.idempotency import PostgresIdempotencyStore

                idem = PostgresIdempotencyStore()
                await idem.put(scope="refund", key=refund_request.idempotency_key, value=response)
            except Exception:
                pass

        return response
        
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
