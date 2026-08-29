from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


SUPPORTED_ADOBE_COMMERCE_EVENT_CODES = frozenset(
    {
        "observer.checkout_submit_all_after",
        "observer.sales_order_save_after",
        "observer.sales_order_invoice_save_after",
        "observer.sales_order_creditmemo_save_after",
    }
)


class UnsupportedAdobeCommerceEvent(ValueError):
    pass


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value or "").strip()
    return normalized or None


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _event_code(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    prefix = "com.adobe.commerce."
    return normalized[len(prefix):] if normalized.startswith(prefix) else ""


def _occurred_at(*values: Any) -> datetime:
    for value in values:
        raw = _text(value)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _amount_cents(value: Any, currency: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    # Adobe Commerce amounts are decimal major-unit values. The native adapter
    # intentionally supports its common 0-decimal currencies without retaining
    # any payment instrument data from the observer payload.
    multiplier = Decimal("1") if str(currency or "").upper() in {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    } else Decimal("100")
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _line_items(value: Any) -> List[Dict[str, Any]]:
    safe: List[Dict[str, Any]] = []
    items = value if isinstance(value, (list, tuple)) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        quantity = item.get("qty")
        if quantity is None:
            quantity = item.get("qty_ordered")
        safe.append(
            {
                key: child
                for key, child in {
                    "id": item.get("entity_id") or item.get("item_id"),
                    "product_id": item.get("product_id"),
                    "sku": item.get("sku"),
                    "quantity": quantity,
                    "price": item.get("price"),
                    "subtotal": item.get("row_total"),
                    "total": item.get("row_total_incl_tax"),
                }.items()
                if child is not None
            }
        )
    return safe


def _entity_event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        ["adobe_commerce", store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"adobe_commerce:{event_type}:{digest}"


def _is_paid_order(order: Dict[str, Any]) -> bool:
    try:
        total_paid = Decimal(str(order["total_paid"]))
        total_due = Decimal(str(order["total_due"])) if order.get("total_due") is not None else None
        grand_total = (
            Decimal(str(order["grand_total"]))
            if order.get("grand_total") is not None
            else None
        )
    except (InvalidOperation, KeyError):
        return False
    if not total_paid.is_finite() or total_paid <= 0:
        return False
    if total_due is not None and total_due.is_finite() and total_due == 0:
        return True
    return bool(
        grand_total is not None
        and grand_total.is_finite()
        and grand_total > 0
        and total_paid >= grand_total
    )


def map_adobe_commerce_io_event(
    envelope: Dict[str, Any],
    *,
    store_id: str,
    delivery_id: Optional[str] = None,
) -> MerchantEventBatch:
    """Reduce one signed Adobe I/O CloudEvent to safe canonical lifecycle facts."""
    if not isinstance(envelope, dict):
        raise ValueError("Adobe I/O event must be an object")
    code = _event_code(envelope.get("type") or envelope.get("event_code"))
    if code not in SUPPORTED_ADOBE_COMMERCE_EVENT_CODES:
        raise UnsupportedAdobeCommerceEvent(
            f"unsupported Adobe Commerce event code: {code or 'missing'}"
        )

    data = _dict(envelope.get("data"))
    value = _dict(data.get("value")) or data
    trace_id = _text(envelope.get("eventid") or envelope.get("id") or delivery_id)
    if not trace_id:
        trace_id = hashlib.sha256(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:32]
    occurred = _occurred_at(envelope.get("time"), value.get("updated_at"), value.get("created_at"))

    if code == "observer.checkout_submit_all_after":
        entity = _dict(value.get("order")) or value
        event_types = ["order.created"]
        entity_kind = "order"
    elif code == "observer.sales_order_save_after":
        entity = value
        native_state = str(entity.get("state") or entity.get("status") or "").strip().lower()
        event_types = ["order.created"]
        if native_state in {"canceled", "cancelled"}:
            event_types = ["order.cancelled"]
        elif _is_paid_order(entity):
            event_types.append("order.paid")
        entity_kind = "order"
    elif code == "observer.sales_order_invoice_save_after":
        entity = value
        native_state = str(entity.get("state") or "").strip().lower()
        if native_state not in {"2", "paid"}:
            raise UnsupportedAdobeCommerceEvent("Adobe Commerce invoice is not paid")
        event_types = ["payment.succeeded"]
        entity_kind = "invoice"
    else:
        entity = value
        native_state = str(entity.get("state") or "").strip().lower()
        if native_state not in {"2", "refunded"}:
            raise UnsupportedAdobeCommerceEvent("Adobe Commerce credit memo is not refunded")
        event_types = ["refund.succeeded"]
        entity_kind = "creditmemo"

    payment = _dict(entity.get("payment"))
    order_id = _text(
        entity.get("order_id")
        if entity_kind != "order"
        else entity.get("entity_id") or entity.get("increment_id") or entity.get("real_order_id")
    )
    entity_id = _text(entity.get("entity_id") or entity.get("increment_id"))
    if entity_kind == "order":
        entity_id = order_id
    if not order_id:
        raise ValueError("Adobe Commerce event is missing order id")
    if not entity_id:
        raise ValueError(f"Adobe Commerce {entity_kind} event is missing entity id")

    payment_id = _text(
        entity.get("transaction_id")
        or payment.get("last_trans_id")
        or payment.get("cc_trans_id")
    )
    refund_id = entity_id if entity_kind == "creditmemo" else None
    currency = _text(
        entity.get("order_currency_code")
        or entity.get("store_currency_code")
        or entity.get("base_currency_code")
    )
    currency = currency.upper() if currency else None
    amount = entity.get("grand_total")
    if entity_kind == "order" and amount is None:
        amount = payment.get("amount_ordered")
    metadata = {
        "native_event_code": code,
        "native_status": _text(entity.get("status") or entity.get("state")),
        "native_line_items": _line_items(entity.get("items")),
        "native_transaction_status": _text(entity.get("state")) if entity_kind != "order" else None,
        "webhook_delivery_id": _text(delivery_id or envelope.get("eventid") or envelope.get("id")),
    }
    metadata = {key: child for key, child in metadata.items() if child not in (None, [], {})}

    events: List[MerchantCommerceEvent] = []
    for event_type in event_types:
        stable_entity = entity_id
        if event_type in {"order.created", "order.paid", "order.cancelled"}:
            stable_entity = order_id
        events.append(
            MerchantCommerceEvent(
                event_id=_entity_event_id(store_id, event_type, stable_entity),
                event_type=event_type,
                occurred_at=occurred,
                platform="magento",
                source="adobe_io_events",
                store_id=store_id,
                buyer_id=_text(entity.get("customer_id")),
                payment_id=payment_id,
                order_id=order_id,
                refund_id=refund_id,
                trace_id=trace_id,
                amount_cents=_amount_cents(amount, currency),
                currency=currency,
                metadata=metadata,
            )
        )
    return MerchantEventBatch(events=events)
