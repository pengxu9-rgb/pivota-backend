"""
Minimal payment routes for pivota_infra (legacy copy in this workspace).

This module exists primarily to support isolated unit tests that exercise the
Checkout.com webhook handler without requiring a live database connection.

Note: The production backend lives under `pivota-backend/` (repo root). This
legacy `pivota_infra/` tree contains historical copies and debug utilities.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("payment_routes")

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.post("/webhooks/checkout")
async def checkout_webhook(request: Request) -> dict[str, Any]:
    """
    Checkout.com webhook to finalize successful payments.

    Expected payload (minimum):
    {
      "type": "payment_captured",
      "data": { "id": "evt_...", "reference": "ORD_..." }
    }

    This handler intentionally avoids importing DB modules at import-time.
    In tests, `db.orders` and `routes.order_routes` are monkeypatched to fakes.
    """
    payload = await request.json()
    event_type = payload.get("type") or payload.get("event_type") or ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    order_id = (
        data.get("reference")
        or payload.get("reference")
        or (data.get("metadata") or {}).get("order_id")
        or (payload.get("metadata") or {}).get("order_id")
    )
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order reference")

    try:
        from db.orders import mark_order_paid, update_payment_info
        from routes.order_routes import log_order_event

        await update_payment_info(order_id=order_id, psp="checkout", raw_payload=payload)
        await log_order_event(order_id=order_id, event_type=event_type, payload=payload)
        await mark_order_paid(order_id=order_id)
    except Exception as exc:
        logger.exception("Checkout webhook processing failed")
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {exc}") from exc

    return {"status": "success", "order_id": order_id}

