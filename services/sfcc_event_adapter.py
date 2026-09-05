from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


SUPPORTED_SFCC_EVENT_TYPES = frozenset(
    {
        "basket.created",
        "basket.item_added",
        "basket.updated",
        "checkout.started",
        "checkout.submitted",
        "order.created",
        "order.paid",
        "order.cancelled",
        "payment.authorized",
        "payment.declined",
        "payment.succeeded",
        "payment.failed",
        "refund.succeeded",
    }
)

_CANONICAL_TYPES = {
    "basket.created": "cart.created",
    "basket.item_added": "cart.item_added",
    "basket.updated": "cart.updated",
    "checkout.started": "checkout.started",
    "checkout.submitted": "checkout.submitted",
    "order.created": "order.created",
    "order.paid": "order.paid",
    "order.cancelled": "order.cancelled",
    "payment.authorized": "payment.authorized",
    "payment.declined": "payment.declined",
    "payment.succeeded": "payment.succeeded",
    "payment.failed": "payment.failed",
    "refund.succeeded": "refund.succeeded",
}


class UnsupportedSFCCEvent(ValueError):
    pass


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value or "").strip()
    return normalized or None


def _occurred_at(value: Any) -> datetime:
    raw = _text(value)
    if not raw:
        raise ValueError("SFCC event is missing occurred_at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("SFCC event occurred_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _amount_cents(value: Any, currency: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("SFCC event amount is invalid")
    if not amount.is_finite() or amount < 0:
        raise ValueError("SFCC event amount is invalid")
    multiplier = Decimal("1") if str(currency or "").upper() in {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    } else Decimal("100")
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _line_items(value: Any) -> List[Dict[str, Any]]:
    items = value if isinstance(value, list) else []
    safe: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        safe.append(
            {
                key: item.get(key)
                for key in ("id", "product_id", "variant_id", "sku", "quantity", "price", "total")
                if item.get(key) is not None
            }
        )
    return safe


def _canonical_event_id(store_id: str, event_type: str, native_event_id: str) -> str:
    material = json.dumps(
        ["salesforce_commerce_cloud", store_id, event_type, native_event_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"sfcc:{event_type}:{digest}"


def map_sfcc_integration_event(
    payload: Dict[str, Any],
    *,
    store_id: str,
    delivery_id: Optional[str] = None,
) -> MerchantCommerceEvent:
    """Map one allowlisted SFCC cartridge event into the canonical ledger."""
    if not isinstance(payload, dict):
        raise ValueError("SFCC event must be an object")
    native_type = str(payload.get("type") or "").strip().lower()
    if native_type not in SUPPORTED_SFCC_EVENT_TYPES:
        raise UnsupportedSFCCEvent(
            f"unsupported SFCC event type: {native_type or 'missing'}"
        )
    native_event_id = _text(payload.get("event_id"))
    if not native_event_id:
        raise ValueError("SFCC event is missing event_id")

    cart_id = _text(payload.get("basket_id") or payload.get("cart_id"))
    checkout_id = _text(payload.get("checkout_id"))
    order_id = _text(payload.get("order_id"))
    payment_id = _text(payload.get("payment_id"))
    refund_id = _text(payload.get("refund_id"))
    if native_type.startswith("basket.") and not cart_id:
        raise ValueError("SFCC basket event is missing basket_id")
    if native_type.startswith("checkout.") and not (checkout_id or cart_id):
        raise ValueError("SFCC checkout event is missing checkout_id or basket_id")
    if native_type.startswith("order.") and not order_id:
        raise ValueError("SFCC order event is missing order_id")
    if native_type.startswith("payment.") and not (payment_id or order_id):
        raise ValueError("SFCC payment event is missing payment_id or order_id")
    if native_type.startswith("refund.") and not (refund_id and order_id):
        raise ValueError("SFCC refund event is missing refund_id or order_id")

    currency = str(payload.get("currency") or "").strip().upper() or None
    metadata = {
        "native_event_name": native_type,
        "native_status": _text(payload.get("status")),
        "native_site_id": _text(payload.get("site_id")),
        "native_line_items": _line_items(payload.get("items")),
        "webhook_delivery_id": _text(delivery_id),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, [], {})}
    canonical_type = _CANONICAL_TYPES[native_type]
    return MerchantCommerceEvent(
        event_id=_canonical_event_id(store_id, canonical_type, native_event_id),
        event_type=canonical_type,
        occurred_at=_occurred_at(payload.get("occurred_at")),
        platform="salesforce_commerce_cloud",
        source="sfcc_cartridge_outbox",
        store_id=store_id,
        session_id=_text(payload.get("session_id")),
        buyer_id=_text(payload.get("customer_id")),
        click_id=_text(payload.get("click_id")),
        cart_id=cart_id,
        checkout_id=checkout_id,
        payment_id=payment_id,
        order_id=order_id,
        order_ref=build_order_ref("salesforce_commerce_cloud", order_id),
        refund_id=refund_id,
        trace_id=_text(payload.get("trace_id") or delivery_id or native_event_id),
        amount_cents=_amount_cents(payload.get("amount"), currency),
        currency=currency,
        metadata=metadata,
    )


def map_sfcc_integration_batch(
    events: List[Dict[str, Any]],
    *,
    store_id: str,
    delivery_id: Optional[str] = None,
) -> MerchantEventBatch:
    return MerchantEventBatch(
        events=[
            map_sfcc_integration_event(
                event,
                store_id=store_id,
                delivery_id=delivery_id,
            )
            for event in events
        ]
    )
