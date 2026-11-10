"""Agent-initiated refunds are a REQUEST to the merchant. Pivota never executes them.

Decision 2026-11-08 (D-03): the merchant is merchant of record on every lane and Pivota is never the
payee, so a refund is the merchant's to make. An agent (any partner, on any door) may ask for one;
nothing an agent calls runs a PSP refund, restocks inventory or cancels a store order.

Before this, four agent routes ran `routes.refund_api.process_refund` directly: an immediate refund of
the whole remainder on the merchant's own PSP key, with no fulfilment, return-window or merchant
approval check:
  * POST /agent/v1/orders/{order_id}/refund            (routes/agent_api.py)
  * POST /agent/v2/orders/{order_id}/refunds           (routes/agent_v2.py)
  * POST /agent/v2/commerce/checkouts/{id}/refunds     (routes/agent_commerce.py)
  * POST /agent/v1/after-sales/cases/{case_id}/refund  (routes/after_sales_cases.py)
The first three now refuse with REFUND_HANDLED_BY_MERCHANT after their ownership checks. The case route
keeps the request: it records it on the case and answers that the merchant handles it.

Staff and merchant refund routes (admin `/orders/{id}/refund`, the merchant portal) are unchanged.
"""
from __future__ import annotations

from fastapi import HTTPException

REFUND_HANDLED_BY_MERCHANT = "REFUND_HANDLED_BY_MERCHANT"
CASE_STATUS_REQUESTED_FROM_MERCHANT = "refund_requested_from_merchant"

REFUSAL_DETAIL = {
    "code": REFUND_HANDLED_BY_MERCHANT,
    "message": (
        "Refunds are made by the merchant, not by Pivota. Ask the buyer to contact the merchant, "
        "or open an after-sales case so the request is recorded."
    ),
    "retryable": False,
}


def refuse_agent_refund_execution() -> None:
    """Raise the one refusal every agent refund route answers with. 409: the request is understood and
    authorised, but this party cannot perform it; retrying never helps."""
    raise HTTPException(status_code=409, detail=dict(REFUSAL_DETAIL))
