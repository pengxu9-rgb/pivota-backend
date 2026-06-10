"""Janitorial cleanup for orphaned hosted-checkout link orders.

WHY THIS EXISTS: the keyless `create_payment_link` flow creates an order ("order A") + a checkout
intent, then the hosted page creates its OWN order ("order B") and pays B — leaving A orphaned as an
unpaid draft. Until the proper one-order resume fix lands, these orphans accumulate. This sweep cancels
them.

SAFETY: this is OFF the charge path. It only ever touches orders that are provably NEVER-PAID and have
NO payment surface:
  - status = 'pending'
  - payment_status IN ('unpaid','awaiting_payment','awaiting_checkout')
  - payment_intent_id IS NULL   (never got a real PSP intent → cancelling cannot affect any charge)
  - created_at older than `older_than_hours` (default 2h, > the checkout-intent TTL)
A paid order, an order with any PaymentIntent, or a recent order is NEVER selected. Dry-run by default.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from db.database import database
from db.orders import update_order_status

router = APIRouter(prefix="/admin/orders", tags=["Admin Orphan Order Cleanup"])

# Never-paid, pre-payment statuses that are safe to cancel when there is also no PaymentIntent.
_SAFE_UNPAID_STATUSES = ("unpaid", "awaiting_payment", "awaiting_checkout")


def _require_internal_key(request: Request, provided_header: Optional[str]) -> None:
    secret = (os.getenv("READINESS_INTERNAL_API_KEY") or "").strip() or (
        os.getenv("UCP_INTERNAL_API_KEY") or ""
    ).strip()
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    provided = (provided_header or "").strip() or (
        request.headers.get("x-pivota-internal-key") or ""
    ).strip()
    if not provided or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def _find_orphaned_link_orders(older_than_hours: int, limit: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, older_than_hours))
    rows = await database.fetch_all(
        """
        SELECT order_id, merchant_id, status, payment_status, payment_intent_id, total, created_at
        FROM orders
        WHERE status = 'pending'
          AND payment_status = ANY(:unpaid_statuses)
          AND payment_intent_id IS NULL
          AND created_at < :cutoff
        ORDER BY created_at ASC
        LIMIT :limit
        """,
        {
            "unpaid_statuses": list(_SAFE_UNPAID_STATUSES),
            "cutoff": cutoff,
            "limit": max(1, min(limit, 1000)),
        },
    )
    return [dict(r) for r in rows or []]


@router.post("/cleanup-orphaned-link-orders")
async def cleanup_orphaned_link_orders(
    request: Request,
    dry_run: bool = Query(default=True, description="Default true: report only, do not cancel."),
    older_than_hours: int = Query(default=2, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=1000),
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
) -> Dict[str, Any]:
    """Cancel orphaned, never-paid, no-PaymentIntent hosted-checkout draft orders. Dry-run by default."""
    _require_internal_key(request, x_pivota_internal_key)

    candidates = await _find_orphaned_link_orders(older_than_hours, limit)
    sample = [
        {
            "order_id": c.get("order_id"),
            "merchant_id": c.get("merchant_id"),
            "payment_status": c.get("payment_status"),
            "created_at": str(c.get("created_at")),
        }
        for c in candidates[:25]
    ]

    if dry_run:
        return {
            "dry_run": True,
            "candidate_count": len(candidates),
            "older_than_hours": older_than_hours,
            "sample": sample,
            "note": "No orders cancelled. Re-run with dry_run=false to cancel these never-paid drafts.",
        }

    cancelled: List[str] = []
    failed: List[Dict[str, Any]] = []
    for c in candidates:
        order_id = str(c.get("order_id") or "")
        if not order_id:
            continue
        try:
            ok = await update_order_status(
                order_id=order_id,
                status="cancelled",
                cancelled_at=datetime.now(timezone.utc),
                payment_status="cancelled",
                metadata={"cancellation_reason": "orphaned_hosted_link_order_cleanup"},
            )
            (cancelled if ok else failed).append(order_id if ok else {"order_id": order_id, "reason": "update_failed"})
        except Exception as exc:  # never let one bad row abort the sweep
            failed.append({"order_id": order_id, "reason": str(exc)[:160]})

    return {
        "dry_run": False,
        "candidate_count": len(candidates),
        "cancelled_count": len(cancelled),
        "failed_count": len(failed),
        "cancelled": cancelled[:100],
        "failed": failed[:25],
    }
