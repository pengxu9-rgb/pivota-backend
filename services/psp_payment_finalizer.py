from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional


AwaitableBoolFn = Callable[..., Awaitable[bool]]
AwaitableAnyFn = Callable[..., Awaitable[Any]]


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


def _is_terminal_failure_state(order: Dict[str, Any]) -> bool:
    payment_status = _payment_status_lower(order)
    status = _order_status_lower(order)
    if payment_status in {"payment_failed", "partially_refunded", "refunded", "cancelled"}:
        return True
    return status in {"payment_failed", "partially_refunded", "refunded", "cancelled"}


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

    await mark_order_paid_fn(order_id)
    await log_order_event_fn(
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
        "order_id": order_id,
        "merchant_id": merchant_id,
        "payment_reference": resolved_payment_reference,
        "metadata": next_metadata,
    }


async def finalize_payment_failure(
    order: Dict[str, Any],
    *,
    psp: str,
    payment_reference: Optional[str],
    error_message: Optional[str] = None,
    source_event: str = "payment_failed_webhook",
    record_generic_failure_metadata: bool = True,
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
    applied = not _is_terminal_failure_state(order)
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
