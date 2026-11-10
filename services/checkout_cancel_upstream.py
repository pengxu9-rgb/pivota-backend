"""Cancel an agent's checkout for real: close the hosted payment page, then cancel the order only while unpaid.

FIX_PLAN C-11 (2026-11-08), behind CHECKOUT_CANCEL_UPSTREAM_ENABLED (default OFF).

WHAT WAS WRONG. The gateway's `cancel_checkout_session` flipped only its own kernel row and answered "canceled".
Nothing here was told: the order stayed payable, the PSP payment the hosted page had already prepared (a Stripe
PaymentIntent, or a Stripe Checkout Session) stayed confirmable, and a buyer holding the link could pay an order
the agent had just told them was canceled.

WHAT THIS DOES (POST /agent/v2/orders/{order_id}/cancel, routes/agent_v2.py; with the flag on, also
POST /agent/v1/orders/{order_id}/cancel, routes/agent_api.py, with the same answers):

1. An order already paid is not canceled. The answer says so and names the refund-REQUEST path: refunds are the
   merchant's to make (D-03, services/agent_refund_policy.py), and nothing here executes one.
2. Otherwise the order's PSP payment surface is closed FIRST, at the PSP: a Checkout Session is expired, a
   PaymentIntent is canceled. If the PSP refuses because the buyer has already paid (or a payment is still
   settling), the order is left alone and the answer says paid (or processing). If it fails for any other reason
   the order is left alone and the answer is "not confirmed" (retriable): never "canceled" on a guess.
3. Only then is the order cancelled, with one conditional UPDATE that refuses a paid row, and a row whose payment
   surface changed since it was read (a concurrent create_payment: `_payment_surface_unchanged`). `mark_order_paid`
   is the same kind of conditional UPDATE, so a payment and a cancel racing for one order serialise: exactly one wins.

AFTERWARDS. A cancelled order can no longer get a new PSP surface (routes/agent_payment_sdk.py create_payment), and
a checkout token bound to it can no longer create an order or a payment (routes/agent_auth.py). A payment can still
land on a PSP object nobody holds a handle to any more (an older PaymentIntent a stale tab still confirms, a PSP
with no void). Such a LATE PAYMENT is recorded on the order, raised as a refund request to the merchant (an
after-sales case) and logged on the alert line below; it is never silently kept and never auto-refunded
(`record_late_payment_after_cancel`, called from services/psp_payment_finalizer.py).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from utils.logger import logger

CHECKOUT_CANCEL_UPSTREAM_FLAG = "CHECKOUT_CANCEL_UPSTREAM_ENABLED"

OUTCOME_CANCELED = "canceled"
OUTCOME_ALREADY_CANCELED = "already_canceled"
OUTCOME_PAID = "not_canceled_paid"
OUTCOME_PROCESSING = "not_canceled_processing"
OUTCOME_NOT_CONFIRMED = "not_confirmed"

# The payment_status a cancelled order carries once money arrived on it anyway. Not a paid state anywhere else
# (GMV, fulfilment, the reconcile sweep all key on their own lists), which is the point: it is money to give back.
LATE_PAYMENT_STATUS = "paid_after_cancel"
LATE_PAYMENT_REASON_CODE = "payment_after_cancel"
LATE_PAYMENT_ALERT = "CHECKOUT_LATE_PAYMENT_AFTER_CANCEL_ALERT"

# The row states that already settled money. Mirrors psp_payment_finalizer._is_terminal_paid_state and the
# exclusions of db.orders.mark_order_paid, so a cancel can never undo a payment either of them recorded.
_PAID_PAYMENT_STATES = frozenset(
    {"paid", "completed", "succeeded", "success", "settled", "partially_refunded", "refunded", LATE_PAYMENT_STATUS}
)
_PAID_ORDER_STATES = frozenset({"paid", "completed", "fulfilled", "partially_refunded", "refunded"})

# What the PSP says about a payment whose void it refused.
_PSP_PAID_STATES = frozenset({"succeeded", "paid", "completed", "captured", "settled"})
_PSP_IN_FLIGHT_STATES = frozenset({"processing", "pending"})
_PSP_CLOSED_STATES = frozenset({"canceled", "cancelled", "expired", "voided"})

REFUND_REQUEST_PATH = "/agent/v1/after-sales/cases"


def checkout_cancel_upstream_enabled() -> bool:
    return (os.getenv(CHECKOUT_CANCEL_UPSTREAM_FLAG) or "").strip().lower() in {"1", "true", "yes", "on"}


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _metadata(order: Dict[str, Any]) -> Dict[str, Any]:
    raw = (order or {}).get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def is_cancelled_order(order: Optional[Dict[str, Any]]) -> bool:
    return _lower((order or {}).get("status")) == "cancelled"


def is_paid_order(order: Optional[Dict[str, Any]]) -> bool:
    row = order or {}
    return _lower(row.get("payment_status")) in _PAID_PAYMENT_STATES or _lower(row.get("status")) in _PAID_ORDER_STATES


def refund_request_answer() -> Dict[str, Any]:
    """Where a refund for a paid order goes: a request to the merchant. Never an executed refund (D-03)."""
    from services.agent_refund_policy import REFUSAL_DETAIL

    return {
        "executed": False,
        "path": REFUND_REQUEST_PATH,
        "code": REFUSAL_DETAIL["code"],
        "message": REFUSAL_DETAIL["message"],
    }


async def _resolve_psp_adapter(order: Dict[str, Any]) -> Tuple[str, Any]:
    from routes.order_routes import _resolve_order_psp_adapter

    return await _resolve_order_psp_adapter(order)


async def _psp_payment_state(adapter: Any, reference: str) -> str:
    """The PSP's own status for a payment reference, normalised the way order_routes reads it. "unknown" on failure."""
    from routes.order_routes import _get_order_payment_status_details, _normalize_psp_status_result

    try:
        details = await _get_order_payment_status_details(adapter, reference)
        if details is not None:
            ok, info, _error = details
            return _lower((info or {}).get("status")) if ok else "unknown"
        ok, status, _error = _normalize_psp_status_result(await adapter.get_payment_status(reference))
        return _lower(status) if ok else "unknown"
    except Exception:
        return "unknown"


async def close_payment_surface(order: Dict[str, Any]) -> Dict[str, Any]:
    """Close the order's PSP payment surface so the hosted page cannot be paid.

    Returns {"action": ...}:
      none            the order has no PSP payment yet (the buyer never reached the pay step)
      expired         a Stripe Checkout Session was expired
      canceled        a PaymentIntent (or the PSP's equivalent) was voided
      already_closed  the PSP says it was already canceled / expired
      paid            the PSP refused because the buyer already paid
      processing      the PSP refused because a payment is still settling
      failed          anything else: the surface may still be payable
    """
    reference = str(order.get("payment_intent_id") or "").strip()
    if not reference:
        return {"action": "none", "reference_type": None}
    is_session = reference.startswith("cs_")
    reference_type = "checkout_session" if is_session else "payment_intent"
    try:
        _provider, adapter = await _resolve_psp_adapter(order)
    except Exception as exc:
        logger.warning("checkout cancel: PSP adapter unresolved for order %s: %s", order.get("order_id"), type(exc).__name__)
        return {"action": "failed", "reference_type": reference_type}

    if is_session:
        expire = getattr(adapter, "expire_checkout_session", None)
        if expire is None:
            return {"action": "failed", "reference_type": reference_type}
        ok, state, _error = await expire(reference)
        if ok:
            return {"action": "expired" if state == "expired" else "already_closed", "reference_type": reference_type}
        if state == "complete_paid":
            return {"action": "paid", "reference_type": reference_type}
        if state == "complete_unpaid":
            return {"action": "processing", "reference_type": reference_type}
        return {"action": "failed", "reference_type": reference_type}

    ok, _ref, _error = await adapter.cancel_payment_authorization(
        reference,
        reason="abandoned",
        idempotency_key=f"checkout_cancel:{order.get('order_id')}:{reference}",
    )
    if ok:
        return {"action": "canceled", "reference_type": reference_type}
    psp_state = await _psp_payment_state(adapter, reference)
    if psp_state in _PSP_PAID_STATES:
        return {"action": "paid", "reference_type": reference_type}
    if psp_state in _PSP_IN_FLIGHT_STATES:
        return {"action": "processing", "reference_type": reference_type}
    if psp_state in _PSP_CLOSED_STATES:
        return {"action": "already_closed", "reference_type": reference_type}
    return {"action": "failed", "reference_type": reference_type}


def _payment_surface_unchanged(order: Dict[str, Any], surface: Dict[str, Any]) -> Any:
    """The WHERE clause that the row still carries the payment surface this cancel closed (or still carries none).

    A concurrent create_payment (routes/agent_payment_sdk.py) writes a new PSP reference (a `cs_` Checkout Session or
    a PaymentIntent) and payment_status "processing" in one UPDATE (db.orders.update_payment_info), between this
    cancel's read and its UPDATE. Without this clause the cancel would still win the row and answer "canceled" while
    that new surface stays payable. With it the cancel loses and answers not-confirmed (retriable); the retry reads
    the new reference and closes it at the PSP first.

      action "none"  the row must still have NO reference, and the payment_status this cancel read: a writer that
                     moved either one has started a payment.
      otherwise      the row must still carry the reference that was just expired / voided, not a newer one.
    """
    from sqlalchemy import and_, func, or_

    from db.orders import orders

    if surface.get("action") == "none":
        return and_(
            or_(orders.c.payment_intent_id.is_(None), func.trim(orders.c.payment_intent_id) == ""),
            func.lower(func.trim(func.coalesce(orders.c.payment_status, ""))) == _lower(order.get("payment_status")),
        )
    return func.trim(func.coalesce(orders.c.payment_intent_id, "")) == str(order.get("payment_intent_id") or "").strip()


SURFACE_NOT_CLOSED_EVENT = "cancelled_order_surface_not_closed"
_CLOSED_ACTIONS = frozenset({"expired", "canceled", "already_closed"})


def _emit_money_alert(event: str, **fields: Any) -> None:
    """One `money_alert: {...}` line (compact sorted JSON, ids and codes only) through the "pivota" logger, the shape
    of utils/money_alerts.py (FIX_PLAN R-02). Never raises."""
    try:
        body = {"event": event, **{k: (None if v is None else str(v)[:128]) for k, v in fields.items()}}
        logger.error("money_alert: %s", json.dumps(body, sort_keys=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - an alert must never break the money path it reports on
        pass


async def close_surface_created_for_cancelled_order(order: Dict[str, Any], *, reference: str, psp: Optional[str]) -> Dict[str, Any]:
    """create_payment made a PSP object (a `cs_` Checkout Session or a PaymentIntent) for an order that a cancel
    cancelled while the object was being created, and its order write refused the cancelled row. Close that object
    at the PSP so the buyer cannot pay it. Best effort and never raises: anything short of a confirmed close raises
    the money_alert line, since a live surface for a cancelled order is money that may arrive and must be refunded.
    """
    order_id = str((order or {}).get("order_id") or "")
    surface: Dict[str, Any] = {"action": "failed", "reference_type": None}
    error_type: Optional[str] = None
    try:
        surface = await close_payment_surface({**(order or {}), "payment_intent_id": reference})
    except Exception as exc:  # noqa: BLE001
        error_type = type(exc).__name__
    if surface.get("action") not in _CLOSED_ACTIONS:
        _emit_money_alert(
            SURFACE_NOT_CLOSED_EVENT,
            order_id=order_id,
            psp=psp,
            payment_reference=reference or None,
            action=surface.get("action"),
            reference_type=surface.get("reference_type"),
            error_type=error_type,
        )
    else:
        logger.info("checkout cancel: order %s was cancelled during create_payment; its new surface was closed", order_id)
    return surface


async def _cancel_row_if_unpaid(order: Dict[str, Any], *, agent_id: str, surface: Dict[str, Any]) -> bool:
    """ONE conditional UPDATE: cancel the row only while it is not paid, not already cancelled, and still carries
    the payment surface this cancel closed. True when THIS call cancelled it. The same shape as
    db.orders.mark_order_paid, which refuses a cancelled row."""
    from sqlalchemy import and_, func

    from db.database import database
    from db.orders import orders

    now = datetime.now(timezone.utc)
    metadata = _metadata(order)
    metadata["checkout_cancel"] = {
        "at": now.isoformat(),
        "agent_id": agent_id,
        "payment_surface": surface.get("action"),
        "reference_type": surface.get("reference_type"),
    }
    query = (
        orders.update()
        .where(
            and_(
                orders.c.order_id == str(order.get("order_id") or ""),
                func.lower(func.coalesce(orders.c.status, "")).notin_(sorted(_PAID_ORDER_STATES | {"cancelled"})),
                func.lower(func.coalesce(orders.c.payment_status, "")).notin_(sorted(_PAID_PAYMENT_STATES | {"cancelled"})),
                _payment_surface_unchanged(order, surface),
            )
        )
        .values(status="cancelled", payment_status="cancelled", cancelled_at=now, updated_at=now, metadata=metadata)
        .returning(orders.c.order_id)
    )
    row = await database.fetch_one(query)
    return row is not None


async def cancel_order_upstream(order: Dict[str, Any], *, agent_id: str) -> Dict[str, Any]:
    """Cancel an order the caller owns (ownership is the route's job). Returns {"outcome": ..., ...}."""
    from db.orders import get_order

    order_id = str(order.get("order_id") or "")
    if is_cancelled_order(order):
        return {"outcome": OUTCOME_ALREADY_CANCELED, "order": order, "payment_surface": {"action": "none"}}
    if is_paid_order(order):
        return {"outcome": OUTCOME_PAID, "order": order, "payment_surface": {"action": "none"}}

    surface = await close_payment_surface(order)
    action = surface.get("action")
    if action == "paid":
        return {"outcome": OUTCOME_PAID, "order": order, "payment_surface": surface}
    if action == "processing":
        return {"outcome": OUTCOME_PROCESSING, "order": order, "payment_surface": surface}
    if action not in {"none", "expired", "canceled", "already_closed"}:
        return {"outcome": OUTCOME_NOT_CONFIRMED, "order": order, "payment_surface": surface}

    if await _cancel_row_if_unpaid(order, agent_id=agent_id, surface=surface):
        after = await get_order(order_id) or {**order, "status": "cancelled", "payment_status": "cancelled"}
        logger.info("checkout cancel: order %s cancelled (payment surface: %s)", order_id, action)
        return {"outcome": OUTCOME_CANCELED, "order": after, "payment_surface": surface}

    # The conditional UPDATE refused: something changed the row since it was read (a payment, another cancel, or a
    # new payment surface). Say what it is now; a row that is neither cancelled nor paid is "not confirmed".
    after = await get_order(order_id) or order
    if is_cancelled_order(after):
        return {"outcome": OUTCOME_ALREADY_CANCELED, "order": after, "payment_surface": surface}
    if is_paid_order(after):
        return {"outcome": OUTCOME_PAID, "order": after, "payment_surface": surface}
    return {"outcome": OUTCOME_NOT_CONFIRMED, "order": after, "payment_surface": surface}


# ---- late payment after a cancel ---------------------------------------------------------------------------


async def _ensure_refund_request_case(
    *,
    order: Dict[str, Any],
    idempotency_key: str,
    amount_major: Optional[Decimal],
    currency: Optional[str],
    psp: str,
    payment_reference: str,
) -> Tuple[Optional[str], bool]:
    """A refund REQUEST to the merchant as an after-sales case (D-03): never a refund. (case_id, created_now)."""
    from db.database import database
    from services.agent_refund_policy import CASE_STATUS_REQUESTED_FROM_MERCHANT

    agent_id = str(order.get("agent_id") or "").strip() or "pivota_system"
    existing = await database.fetch_one(
        "SELECT case_id FROM after_sales_cases WHERE agent_id = :agent_id AND idempotency_key = :k",
        {"agent_id": agent_id, "k": idempotency_key},
    )
    if existing:
        return str(dict(existing).get("case_id") or "") or None, False

    case_id = f"ASC_{uuid.uuid4().hex[:20].upper()}"
    audit = [
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "refund_requested_from_merchant",
            "payload": {
                "reason": LATE_PAYMENT_REASON_CODE,
                "psp": psp,
                "payment_reference": payment_reference,
                "amount": str(amount_major) if amount_major is not None else None,
            },
        }
    ]
    await database.execute(
        """
        INSERT INTO after_sales_cases (
          case_id, order_id, merchant_id, agent_id, agent_user_ref, buyer_ref,
          case_type, resolution, reason_code, reason_text,
          requested_refund_amount, currency_order, currency_charge,
          line_items_json, amount_breakdown_json,
          status, label_url, audit_log, idempotency_key, created_at, updated_at
        )
        VALUES (
          :case_id, :order_id, :merchant_id, :agent_id, NULL, NULL,
          'refund', 'refund_without_return', :reason_code, :reason_text,
          :amount, :currency, NULL,
          CAST('[]' AS JSONB), CAST('{}' AS JSONB),
          :status, NULL, CAST(:audit_log AS JSONB), :idempotency_key, NOW(), NOW()
        )
        """,
        {
            "case_id": case_id,
            "order_id": str(order.get("order_id") or ""),
            "merchant_id": str(order.get("merchant_id") or ""),
            "agent_id": agent_id,
            "reason_code": LATE_PAYMENT_REASON_CODE,
            "reason_text": "A payment arrived after the order was canceled. Refund it to the buyer.",
            "amount": float(amount_major) if amount_major is not None else None,
            "currency": (currency or str(order.get("currency") or "")).upper() or None,
            "status": CASE_STATUS_REQUESTED_FROM_MERCHANT,
            "audit_log": json.dumps(audit),
            "idempotency_key": idempotency_key,
        },
    )
    return case_id, True


async def record_late_payment_after_cancel(
    order: Dict[str, Any],
    *,
    psp: str,
    payment_reference: str,
    amount_major: Optional[Decimal],
    amount_minor: Any,
    currency: Optional[str],
    source_event: str,
) -> Dict[str, Any]:
    """Money arrived on a cancelled order. Record it, ask the merchant to refund it, raise the alert line.

    Never raises (a PSP webhook must still be answered) and never refunds. Idempotent per payment reference: a
    second delivery of the same payment finds its case and does not alert again.
    """
    from sqlalchemy import and_, func

    from db.database import database
    from db.orders import orders

    order_id = str(order.get("order_id") or "")
    reference = str(payment_reference or "").strip()
    idempotency_key = f"late_payment:{psp}:{reference or order_id}"[:255]
    alert = {
        "order_id": order_id,
        "merchant_id": str(order.get("merchant_id") or ""),
        "psp": psp,
        "payment_reference": reference or None,
        "amount_minor": str(amount_minor) if amount_minor is not None else None,
        "currency": (currency or str(order.get("currency") or "")).upper() or None,
        "source_event": source_event,
    }

    recorded = False
    try:
        metadata = _metadata(order)
        late = metadata.get("late_payments") if isinstance(metadata.get("late_payments"), list) else []
        if not any(isinstance(item, dict) and item.get("payment_reference") == (reference or None) for item in late):
            late = [*late, {**alert, "received_at": datetime.now(timezone.utc).isoformat()}]
        metadata["late_payments"] = late
        row = await database.fetch_one(
            orders.update()
            .where(and_(orders.c.order_id == order_id, func.lower(func.coalesce(orders.c.status, "")) == "cancelled"))
            .values(payment_status=LATE_PAYMENT_STATUS, metadata=metadata, updated_at=datetime.now(timezone.utc))
            .returning(orders.c.order_id)
        )
        recorded = row is not None
    except Exception as exc:
        logger.error("checkout late payment: order %s not recorded: %s", order_id, type(exc).__name__)

    case_id: Optional[str] = None
    created = True
    try:
        case_id, created = await _ensure_refund_request_case(
            order=order,
            idempotency_key=idempotency_key,
            amount_major=amount_major,
            currency=currency,
            psp=psp,
            payment_reference=reference,
        )
    except Exception as exc:
        try:
            from routes.after_sales_cases import _ensure_after_sales_cases_table

            await _ensure_after_sales_cases_table()
            case_id, created = await _ensure_refund_request_case(
                order=order,
                idempotency_key=idempotency_key,
                amount_major=amount_major,
                currency=currency,
                psp=psp,
                payment_reference=reference,
            )
        except Exception as exc2:
            logger.error(
                "checkout late payment: refund request for order %s not recorded: %s / %s",
                order_id,
                type(exc).__name__,
                type(exc2).__name__,
            )

    if created:
        # THE ALERT LINE. One per late payment (a redelivery finds its case and stays quiet). It still fires when
        # recording failed: money on a cancelled order is never silent.
        logger.error(
            "%s %s",
            LATE_PAYMENT_ALERT,
            json.dumps({**alert, "case_id": case_id, "recorded": recorded}, sort_keys=True),
        )
    else:
        logger.info("checkout late payment: order %s reference already recorded (case %s)", order_id, case_id)

    return {
        "applied": False,
        "transitioned": False,
        "reason": "late_payment_after_cancel",
        "order_id": order_id,
        "refund_request_case_id": case_id,
    }
