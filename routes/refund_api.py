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
    
    # Check if order is paid
    if order["payment_status"] != "paid":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot refund unpaid order. Current status: {order['payment_status']}"
        )
    
    # Check if already refunded
    if order.get("status") == "refunded":
        return {
            "status": "already_refunded",
            "message": "Order was already refunded",
            "order_id": order_id
        }
    
    # Get merchant
    merchant = await get_merchant_onboarding(order["merchant_id"])
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Calculate refund amount
    refund_amount = Decimal(str(refund_request.amount)) if refund_request.amount else Decimal(str(order["total"]))
    
    # Validate refund amount
    if refund_amount > Decimal(str(order["total"])):
        raise HTTPException(
            status_code=400,
            detail=f"Refund amount ${refund_amount} exceeds order total ${order['total']}"
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

        # Get PSP adapter
        psp_type = merchant.get("psp_type", "stripe")
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
        success, refund_id, error = await psp_adapter.refund_payment(
            payment_intent_id=order["payment_intent_id"],
            amount=refund_amount if refund_amount < Decimal(str(order["total"])) else None,
            reason=refund_request.reason
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Refund failed: {error}")
        
        # Update order status
        is_partial = refund_amount < Decimal(str(order["total"]))
        new_status = "partially_refunded" if is_partial else "refunded"
        
        await update_order_status(
            order_id=order_id,
            status=new_status,
            refunded_at=datetime.now(),
            metadata={
                **(order.get("metadata") or {}),
                "refund_id": refund_id,
                "refund_amount": str(refund_amount),
                "refund_reason": refund_request.reason,
                "refunded_by": current_user.get("user_id", "admin")
            }
        )
        
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
                if True and order.get("shopify_order_id"):
                    shop_domain = store_info.get("domain")
                    access_token = store_info.get("api_key")
                    
                    if shop_domain and access_token:
                        import httpx
                        
                        # Cancel the Shopify order
                        url = f"https://{shop_domain}/admin/api/2024-01/orders/{order['shopify_order_id']}/cancel.json"
                        headers_shopify = {
                            "X-Shopify-Access-Token": access_token,
                            "Content-Type": "application/json"
                        }
                        
                        cancel_data = {
                            "amount": str(refund_amount),
                            "currency": order["currency"],
                            "reason": refund_request.reason or "customer_request",
                            "email": True,  # Send cancellation email
                            "refund": True
                        }
                        
                        async with httpx.AsyncClient() as client:
                            response = await client.post(
                                url,
                                json=cancel_data,
                                headers=headers_shopify,
                                timeout=10.0
                            )
                            
                            if response.status_code == 200:
                                logger.info(f"Shopify order {order['shopify_order_id']} cancelled")
                            else:
                                logger.warning(f"Failed to cancel Shopify order: {response.status_code}")
                                
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
            "new_order_status": new_status
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
        "currency": order["currency"]
    }
    
    if is_refunded and order.get("metadata"):
        metadata = order["metadata"]
        refund_info.update({
            "refund_id": metadata.get("refund_id"),
            "refund_amount": metadata.get("refund_amount"),
            "refund_reason": metadata.get("refund_reason"),
            "refunded_at": order.get("refunded_at").isoformat() if order.get("refunded_at") else None,
            "refunded_by": metadata.get("refunded_by")
        })
    
    return {
        "status": "success",
        "refund": refund_info
    }
