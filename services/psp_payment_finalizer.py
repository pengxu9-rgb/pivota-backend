from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional


AwaitableBoolFn = Callable[..., Awaitable[bool]]
AwaitableAnyFn = Callable[..., Awaitable[Any]]

logger = logging.getLogger(__name__)
database: Any = None

_STAMP_GROSS_ATTRIBUTED_GMV_QUERY = """
UPDATE commerce_attribution_edges
SET gross_attributed_gmv_cents = :gross
WHERE order_id = :order_id
  AND gross_attributed_gmv_cents IS NULL
"""


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _decimal_money(value: Any) -> Decimal:
    try:
        if value is None:
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _money_from_minor_units(value: Any, currency: Optional[str]) -> Decimal:
    minor = _decimal_money(value)
    code = (currency or "").strip().lower()
    zero_decimal = {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
        "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
    three_decimal = {"bhd", "jod", "kwd", "omr", "tnd"}
    if code in zero_decimal:
        return minor
    if code in three_decimal:
        return minor / Decimal("1000")
    return minor / Decimal("100")


def _get_database() -> Any:
    global database
    if database is None:
        from db.database import database as configured_database

        database = configured_database
    return database


def _money_to_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).quantize(Decimal("1")))


def _gross_attributed_gmv_cents(subtotal: Any, discount_total: Any) -> int:
    gross = _decimal_money(subtotal) - _decimal_money(discount_total)
    if gross < Decimal("0"):
        gross = Decimal("0")
    return _money_to_cents(gross)


def _coerce_update_count(result: Any) -> Optional[int]:
    if isinstance(result, bool):
        return None
    if isinstance(result, int):
        return result
    if isinstance(result, str):
        parts = result.strip().split()
        if len(parts) >= 2 and parts[0].upper() == "UPDATE":
            try:
                return int(parts[-1])
            except Exception:
                return None
    return None


async def stamp_gross_attributed_gmv(
    order_id: str,
    *,
    subtotal: Any,
    discount_total: Any = None,
) -> Optional[int]:
    """Stamp initial attributed GMV in cents for a paid commerce order.

    v1.3 GMV is strictly subtotal minus discount_total. Tax and shipping are
    excluded from this attribution basis, and refunds are handled elsewhere.
    """
    # Tax and shipping are intentionally excluded from v1.3 attributed GMV.
    gross = _gross_attributed_gmv_cents(subtotal, discount_total)
    result = await _get_database().execute(
        _STAMP_GROSS_ATTRIBUTED_GMV_QUERY,
        {"order_id": order_id, "gross": gross},
    )
    updated_count = _coerce_update_count(result)
    if updated_count == 0:
        logger.info(
            "No unstamped commerce attribution edges found for paid order %s",
            order_id,
        )
    return updated_count


def _order_status_lower(order: Dict[str, Any]) -> str:
    return str((order or {}).get("status") or "").strip().lower()


def _payment_status_lower(order: Dict[str, Any]) -> str:
    return str((order or {}).get("payment_status") or "").strip().lower()


def _is_terminal_paid_state(order: Dict[str, Any]) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if payment_status in {"paid", "completed", "succeeded", "success", "settled", "partially_refunded", "refunded"}:
        return True
    return status in {"paid", "completed", "fulfilled", "partially_refunded", "refunded"}


# Post-settlement states that block a payment-FAILURE transition under ALL
# circumstances: the order has already reached a terminal money state, so a
# (possibly stale or out-of-order) failure event must never move it.
_FAILURE_TERMINAL_PAYMENT_STATES = frozenset(
    {"payment_failed", "partially_refunded", "refunded", "cancelled"}
)
_FAILURE_TERMINAL_ORDER_STATES = frozenset(
    {"payment_failed", "partially_refunded", "refunded", "cancelled"}
)
# Paid-family states. A stale / mis-correlated failure event (e.g. a late Stripe
# payment_intent.payment_failed) must NOT demote these back to payment_failed —
# the inverse of the charge-stuck incident. The EXCEPTION is a genuine capture
# failure on a two-phase (authorise-then-capture) PSP: AUTHORISATION marks the
# order paid, but a subsequent CAPTURE_FAILED means the money was never actually
# captured, so the order MUST be demoted. Callers opt into that demotion via
# allow_paid_demotion=True (Adyen CAPTURE_FAILED only); the default keeps the
# paid order protected.
_FAILURE_TERMINAL_PAID_PAYMENT_STATES = frozenset(
    {"paid", "completed", "succeeded", "success", "settled"}
)
_FAILURE_TERMINAL_PAID_ORDER_STATES = frozenset({"paid", "completed", "fulfilled"})


def _is_terminal_failure_state(
    order: Dict[str, Any], *, allow_paid_demotion: bool = False
) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    payment_terminal = set(_FAILURE_TERMINAL_PAYMENT_STATES)
    order_terminal = set(_FAILURE_TERMINAL_ORDER_STATES)
    if not allow_paid_demotion:
        payment_terminal |= _FAILURE_TERMINAL_PAID_PAYMENT_STATES
        order_terminal |= _FAILURE_TERMINAL_PAID_ORDER_STATES
    if payment_status in payment_terminal:
        return True
    return status in order_terminal


def _blocks_payment_success_recovery(order: Dict[str, Any]) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if payment_status in {"partially_refunded", "refunded", "cancelled"}:
        return True
    return status in {"partially_refunded", "refunded", "cancelled"}


async def _safe_update_order_status(
    update_order_status_fn: AwaitableBoolFn,
    order_id: str,
    status: str,
    **kwargs: Any,
) -> Any:
    try:
        return await update_order_status_fn(order_id, status, **kwargs)
    except TypeError:
        return await update_order_status_fn(order_id, status)


def _reconcile_refund_status(order_total: Decimal, total_refunded: Decimal) -> str:
    if total_refunded <= Decimal("0"):
        return "paid"
    if order_total > Decimal("0") and total_refunded >= order_total:
        return "refunded"
    return "partially_refunded"


def _build_refund_key(psp: str, refund_reference: Optional[str]) -> str:
    ref = str(refund_reference or "").strip()
    return f"{psp}:{ref}" if ref else psp


def _full_refund_fulfillment_status(order: Dict[str, Any]) -> Optional[str]:
    fulfillment_status = str((order or {}).get("fulfillment_status") or "").strip().lower()
    if fulfillment_status in {"", "pending", "processing", "not_fulfilled", "unfulfilled", "open"}:
        return "cancelled"
    return None


async def finalize_payment_success(
    order: Dict[str, Any],
    *,
    psp: str,
    payment_reference: Optional[str],
    transaction_id: Optional[str] = None,
    amount_minor: Any = None,
    currency: Optional[str] = None,
    source_event: str = "payment_confirmed_webhook",
    metadata_extra: Optional[Dict[str, Any]] = None,
    update_payment_info_fn: Optional[AwaitableBoolFn] = None,
    mark_order_paid_fn: AwaitableBoolFn,
    log_order_event_fn: AwaitableAnyFn,
) -> Dict[str, Any]:
    if not order:
        return {"applied": False, "reason": "order_missing"}
    if _is_terminal_paid_state(order) or _blocks_payment_success_recovery(order):
        return {"applied": False, "reason": "already_settled", "order_id": order.get("order_id")}

    order_id = str(order.get("order_id") or "")
    merchant_id = str(order.get("merchant_id") or "")
    resolved_payment_reference = (
        str(transaction_id or "").strip()
        or str(payment_reference or "").strip()
        or str(order.get("payment_intent_id") or "").strip()
    )
    existing_metadata = _as_dict(order.get("metadata"))
    next_metadata = {
        **existing_metadata,
        "last_payment_confirmation": {
            "psp": psp,
            "payment_reference": resolved_payment_reference,
            "amount_minor": str(amount_minor) if amount_minor is not None else None,
            "currency": currency or str(order.get("currency") or ""),
            "received_at": datetime.now().isoformat(),
            **(metadata_extra or {}),
        },
    }

    if update_payment_info_fn and resolved_payment_reference:
        await update_payment_info_fn(
            order_id=order_id,
            payment_intent_id=resolved_payment_reference,
            client_secret=str(order.get("client_secret") or ""),
            payment_status="paid",
            psp_used=psp,
        )

    # `mark_order_paid` now performs an ATOMIC conditional transition and returns
    # True only if THIS call flipped a non-terminal order to paid. When two
    # finalizers race (webhook + sync confirm, or webhook + reconcile sweep),
    # exactly one observes transitioned=True. Callers gate one-time side effects
    # (Shopify order creation, merchant payment.completed webhook) on this so a
    # concurrent finalize cannot double-fulfill. Recovery of a paid-but-
    # unfulfilled order is handled by the reconcile sweep, not by re-finalizing.
    transitioned = bool(await mark_order_paid_fn(order_id))
    if not transitioned:
        # Someone else won the paid transition between our in-memory guard above
        # and this atomic update. Treat as already-settled, suppress duplicate
        # side effects.
        return {
            "applied": False,
            "transitioned": False,
            "reason": "already_settled_concurrent",
            "order_id": order_id,
        }
    try:
        await stamp_gross_attributed_gmv(
            order_id,
            subtotal=order.get("subtotal"),
            discount_total=order.get("discount_total"),
        )
    except Exception as exc:
        logger.exception(
            "Failed to stamp gross attributed GMV for paid order %s: %s",
            order_id,
            exc,
        )
    funnel_event_ids = await log_order_event_fn(
        event_type=source_event,
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "psp": psp,
            "payment_intent_id": resolved_payment_reference,
            "amount": amount_minor,
            "currency": currency or str(order.get("currency") or ""),
            **(metadata_extra or {}),
        },
    )

    return {
        "applied": True,
        "transitioned": True,
        "order_id": order_id,
        "merchant_id": merchant_id,
        "payment_reference": resolved_payment_reference,
        "metadata": next_metadata,
        # funnel_event_ids produced by the conversion log_order_event hook, surfaced
        # so the caller can join the settled sale back to its originating decision
        # (agent_decision_funnel_links). May be [] if the funnel hook found no
        # merchant_id / swallowed an error — callers treat it as best-effort.
        "funnel_event_ids": funnel_event_ids or [],
    }


async def finalize_payment_failure(
    order: Dict[str, Any],
    *,
    psp: str,
    payment_reference: Optional[str],
    error_message: Optional[str] = None,
    source_event: str = "payment_failed_webhook",
    record_generic_failure_metadata: bool = True,
    allow_paid_demotion: bool = False,
    metadata_extra: Optional[Dict[str, Any]] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    update_order_status_fn: AwaitableBoolFn,
    log_order_event_fn: AwaitableAnyFn,
) -> Dict[str, Any]:
    if not order:
        return {"applied": False, "reason": "order_missing"}

    order_id = str(order.get("order_id") or "")
    merchant_id = str(order.get("merchant_id") or "")
    existing_metadata = _as_dict(order.get("metadata"))
    applied = not _is_terminal_failure_state(
        order, allow_paid_demotion=allow_paid_demotion
    )
    if applied:
        await _safe_update_order_status(
            update_order_status_fn,
            order_id,
            "payment_failed",
            payment_status="payment_failed",
            metadata={
                **existing_metadata,
                **(
                    {
                        "last_payment_failure": {
                            "psp": psp,
                            "payment_reference": str(payment_reference or "").strip(),
                            "error": error_message,
                            "received_at": datetime.now().isoformat(),
                            **(metadata_extra or {}),
                        }
                    }
                    if record_generic_failure_metadata
                    else {}
                ),
                **(metadata_patch or {}),
            },
        )
    await log_order_event_fn(
        event_type=source_event,
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "psp": psp,
            "payment_intent_id": str(payment_reference or "").strip(),
            "error": error_message,
            **(metadata_extra or {}),
        },
    )
    return {"applied": applied, "order_id": order_id, "merchant_id": merchant_id}


async def finalize_refund_success(
    order: Dict[str, Any],
    *,
    psp: str,
    refund_reference: Optional[str],
    refund_amount_minor: Any = None,
    refund_amount: Any = None,
    refund_total: Any = None,
    currency: Optional[str] = None,
    source_event: str = "refund_processed_webhook",
    metadata_extra: Optional[Dict[str, Any]] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    update_order_status_fn: AwaitableBoolFn,
    log_order_event_fn: AwaitableAnyFn,
) -> Dict[str, Any]:
    if not order:
        return {"applied": False, "reason": "order_missing"}

    order_id = str(order.get("order_id") or "")
    merchant_id = str(order.get("merchant_id") or "")
    order_total = _decimal_money(order.get("total"))
    current_total_refunded = _decimal_money(order.get("total_refunded"))
    resolved_currency = currency or str(order.get("currency") or "")
    refund_key = _build_refund_key(psp, refund_reference)
    existing_metadata = _as_dict(order.get("metadata"))
    seen_refs = list(existing_metadata.get("psp_refund_refs") or [])
    refund_records = _as_dict(existing_metadata.get("psp_refund_records"))

    if refund_total is not None:
        next_total_refunded = max(current_total_refunded, _decimal_money(refund_total))
        resolved_refund_amount = _decimal_money(refund_amount)
        if resolved_refund_amount <= Decimal("0"):
            resolved_refund_amount = max(Decimal("0"), next_total_refunded - current_total_refunded)
    else:
        resolved_refund_amount = _decimal_money(refund_amount)
        if resolved_refund_amount <= Decimal("0"):
            resolved_refund_amount = _money_from_minor_units(refund_amount_minor, resolved_currency)
        if refund_key in seen_refs:
            return {"applied": False, "reason": "duplicate_refund", "order_id": order_id}
        next_total_refunded = current_total_refunded + resolved_refund_amount

    next_status = _reconcile_refund_status(order_total, next_total_refunded)
    next_refs = seen_refs if refund_key in seen_refs else [*seen_refs, refund_key]
    refund_records[refund_key] = {
        "psp": psp,
        "refund_reference": str(refund_reference or "").strip(),
        "amount_minor": str(refund_amount_minor) if refund_amount_minor is not None else None,
        "amount": str(resolved_refund_amount),
        "currency": resolved_currency,
        "received_at": datetime.now().isoformat(),
        **(metadata_extra or {}),
    }

    update_fields = {
        "payment_status": next_status,
        "total_refunded": next_total_refunded,
        "refunded_at": datetime.now() if next_status == "refunded" else None,
        "metadata": {
            **existing_metadata,
            "psp_refund_refs": next_refs,
            "psp_refund_records": refund_records,
            "last_refund": refund_records[refund_key],
            **(metadata_patch or {}),
        },
    }
    terminal_fulfillment_status = (
        _full_refund_fulfillment_status(order) if next_status == "refunded" else None
    )
    if terminal_fulfillment_status:
        update_fields["fulfillment_status"] = terminal_fulfillment_status

    await _safe_update_order_status(
        update_order_status_fn,
        order_id,
        next_status,
        **update_fields,
    )
    await log_order_event_fn(
        event_type=source_event,
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "psp": psp,
            "payment_intent_id": str(refund_reference or "").strip(),
            "refund_amount_minor": str(refund_amount_minor) if refund_amount_minor is not None else None,
            "refund_amount": str(resolved_refund_amount),
            "currency": resolved_currency,
            **(metadata_extra or {}),
        },
    )
    return {
        "applied": True,
        "order_id": order_id,
        "merchant_id": merchant_id,
        "refund_amount": resolved_refund_amount,
        "total_refunded": next_total_refunded,
        "next_status": next_status,
    }


async def finalize_refund_failure(
    order: Dict[str, Any],
    *,
    psp: str,
    refund_reference: Optional[str],
    failure_reason: Optional[str],
    rollback_reference: Optional[str] = None,
    rollback_amount: Any = None,
    source_event: str = "refund_failed_webhook",
    metadata_extra: Optional[Dict[str, Any]] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    update_order_status_fn: AwaitableBoolFn,
    log_order_event_fn: AwaitableAnyFn,
) -> Dict[str, Any]:
    if not order:
        return {"applied": False, "reason": "order_missing"}

    order_id = str(order.get("order_id") or "")
    merchant_id = str(order.get("merchant_id") or "")
    order_total = _decimal_money(order.get("total"))
    current_total_refunded = _decimal_money(order.get("total_refunded"))
    existing_metadata = _as_dict(order.get("metadata"))
    seen_refs = list(existing_metadata.get("psp_refund_refs") or [])
    refund_records = _as_dict(existing_metadata.get("psp_refund_records"))
    rolled_back = False
    next_total_refunded = current_total_refunded
    rollback_key = _build_refund_key(psp, rollback_reference) if rollback_reference else None

    rollback_value = _decimal_money(rollback_amount)
    if rollback_key and rollback_key in refund_records:
        rollback_value = _decimal_money(rollback_amount)
        if rollback_value <= Decimal("0"):
            rollback_value = _decimal_money(refund_records[rollback_key].get("amount"))
        next_total_refunded = current_total_refunded - rollback_value
        if next_total_refunded < Decimal("0"):
            next_total_refunded = Decimal("0")
        seen_refs = [ref for ref in seen_refs if ref != rollback_key]
        refund_records = {key: value for key, value in refund_records.items() if key != rollback_key}
        rolled_back = True
    elif rollback_reference and rollback_value > Decimal("0"):
        next_total_refunded = current_total_refunded - rollback_value
        if next_total_refunded < Decimal("0"):
            next_total_refunded = Decimal("0")
        rolled_back = True

    next_status = _reconcile_refund_status(order_total, next_total_refunded)
    if rolled_back or metadata_patch:
        await _safe_update_order_status(
            update_order_status_fn,
            order_id,
            next_status,
            payment_status=next_status,
            total_refunded=next_total_refunded,
            metadata={
                **existing_metadata,
                "psp_refund_refs": seen_refs,
                "psp_refund_records": refund_records,
                "last_refund_failure": {
                    "psp": psp,
                    "refund_reference": str(refund_reference or "").strip(),
                    "rollback_reference": str(rollback_reference or "").strip() or None,
                    "rollback_amount": str(rollback_amount) if rollback_amount is not None else None,
                    "failure_reason": failure_reason,
                    "received_at": datetime.now().isoformat(),
                    **(metadata_extra or {}),
                },
                **(metadata_patch or {}),
            },
        )
    await log_order_event_fn(
        event_type=source_event,
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "psp": psp,
            "payment_intent_id": str(refund_reference or "").strip(),
            "failure_reason": failure_reason,
            "rollback_applied": rolled_back,
            "rollback_reference": str(rollback_reference or "").strip() or None,
            "next_total_refunded": str(next_total_refunded),
            **(metadata_extra or {}),
        },
    )
    return {
        "applied": True,
        "rolled_back": rolled_back,
        "order_id": order_id,
        "merchant_id": merchant_id,
        "total_refunded": next_total_refunded,
        "next_status": next_status,
    }


async def finalize_cancellation(
    order: Dict[str, Any],
    *,
    psp: str,
    cancel_reference: Optional[str],
    reason: Optional[str] = None,
    source_event: str = "order_cancelled_webhook",
    metadata_extra: Optional[Dict[str, Any]] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    update_order_status_fn: AwaitableBoolFn,
    log_order_event_fn: AwaitableAnyFn,
) -> Dict[str, Any]:
    if not order:
        return {"applied": False, "reason": "order_missing"}
    if _order_status_lower(order) == "cancelled":
        return {"applied": False, "reason": "already_cancelled", "order_id": order.get("order_id")}

    order_id = str(order.get("order_id") or "")
    merchant_id = str(order.get("merchant_id") or "")
    existing_metadata = _as_dict(order.get("metadata"))
    await _safe_update_order_status(
        update_order_status_fn,
        order_id,
        "cancelled",
        payment_status="cancelled",
        cancelled_at=datetime.now(),
        metadata={
            **existing_metadata,
            "last_cancellation": {
                "psp": psp,
                "cancel_reference": str(cancel_reference or "").strip(),
                "reason": reason,
                "received_at": datetime.now().isoformat(),
                **(metadata_extra or {}),
            },
            **(metadata_patch or {}),
        },
    )
    await log_order_event_fn(
        event_type=source_event,
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "psp": psp,
            "payment_intent_id": str(cancel_reference or "").strip(),
            "reason": reason,
            **(metadata_extra or {}),
        },
    )
    return {"applied": True, "order_id": order_id, "merchant_id": merchant_id}
