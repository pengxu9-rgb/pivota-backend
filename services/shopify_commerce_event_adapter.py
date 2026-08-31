from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.commerce_attribution_service import (
    extract_click_id_from_note_attributes,
    shopify_order_total_to_cents,
)
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


SUPPORTED_SHOPIFY_TOPICS = frozenset(
    {"orders/create", "orders/paid", "orders/cancelled", "refunds/create"}
)


class UnsupportedShopifyCommerceEvent(ValueError):
    pass


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value or "").strip()
    return normalized or None


def _occurred_at(*values: Any) -> datetime:
    for value in values:
        raw = _text(value)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _payload_fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:32]


def _event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        ["shopify", store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"shopify:{event_type}:{digest}"


def _line_items(value: Any, *, refund: bool = False) -> List[Dict[str, Any]]:
    """Retain commerce join keys while excluding customer and arbitrary properties."""
    safe: List[Dict[str, Any]] = []
    for raw_item in value or []:
        if not isinstance(raw_item, dict):
            continue
        item = raw_item
        if refund and isinstance(raw_item.get("line_item"), dict):
            item = {**raw_item["line_item"], **raw_item}
        normalized = {
            "id": raw_item.get("line_item_id") or item.get("id"),
            "product_id": item.get("product_id"),
            "variant_id": item.get("variant_id"),
            "sku": item.get("sku"),
            "quantity": raw_item.get("quantity", item.get("quantity")),
            "price": item.get("price"),
            "subtotal": raw_item.get("subtotal"),
            "total": raw_item.get("total"),
        }
        safe.append({key: val for key, val in normalized.items() if val is not None})
    return safe


def _order_metadata(
    order: Dict[str, Any], topic: str, delivery_id: Optional[str]
) -> Dict[str, Any]:
    metadata = {
        "native_topic": topic,
        "native_status": _text(order.get("status")),
        "native_financial_status": _text(order.get("financial_status")),
        "native_fulfillment_status": _text(order.get("fulfillment_status")),
        "native_payment_method": _text(order.get("gateway")),
        "native_line_items": _line_items(order.get("line_items")),
        "native_discount_total": order.get("current_total_discounts")
        or order.get("total_discounts"),
        "native_shipping_total": order.get("current_total_shipping_price")
        or order.get("total_shipping_price_set"),
        "native_total_tax": order.get("current_total_tax") or order.get("total_tax"),
        "webhook_delivery_id": _text(delivery_id),
    }
    # Shopify's *_price_set values are nested money objects. The canonical
    # metadata contract is scalar-only, so do not leak or stringify them.
    return {
        key: value
        for key, value in metadata.items()
        if value not in (None, [], {}) and not isinstance(value, (dict, tuple, set))
    }


def _order_events(
    order: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
    fallback_occurred_at: Optional[datetime],
) -> MerchantEventBatch:
    order_id = _text(order.get("id") or order.get("order_number") or order.get("name"))
    if not order_id:
        raise ValueError("Shopify order webhook is missing order id")

    event_type = {
        "orders/create": "order.created",
        "orders/paid": "order.paid",
        "orders/cancelled": "order.cancelled",
    }[topic]
    customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
    trace_id = _text(delivery_id) or _payload_fingerprint(order)
    amount_cents, currency = shopify_order_total_to_cents(order)
    occurred = _occurred_at(
        order.get("cancelled_at") if event_type == "order.cancelled" else None,
        order.get("processed_at") if event_type == "order.paid" else None,
        order.get("created_at") if event_type == "order.created" else None,
        order.get("updated_at"),
        fallback_occurred_at,
    )
    return MerchantEventBatch(
        events=[
            MerchantCommerceEvent(
                event_id=_event_id(store_id, event_type, order_id),
                event_type=event_type,
                occurred_at=occurred,
                platform="shopify",
                source="shopify_webhook",
                store_id=store_id,
                buyer_id=_text(customer.get("id")),
                click_id=extract_click_id_from_note_attributes(order.get("note_attributes")),
                cart_id=_text(order.get("cart_token")),
                checkout_id=_text(order.get("checkout_id") or order.get("checkout_token")),
                order_id=order_id,
                trace_id=trace_id,
                amount_cents=amount_cents,
                currency=currency,
                metadata=_order_metadata(order, topic, delivery_id),
            )
        ]
    )


def _refund_events(
    refund: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
    fallback_occurred_at: Optional[datetime],
) -> MerchantEventBatch:
    refund_id = _text(refund.get("id"))
    order_id = _text(refund.get("order_id"))
    if not refund_id or not order_id:
        raise ValueError("Shopify refund webhook is missing refund or order id")
    trace_id = _text(delivery_id) or _payload_fingerprint(refund)
    created_at = _occurred_at(
        refund.get("processed_at"), refund.get("created_at"), fallback_occurred_at
    )
    base_metadata = {
        "native_topic": topic,
        "native_line_items": _line_items(refund.get("refund_line_items"), refund=True),
        "webhook_delivery_id": _text(delivery_id),
    }
    base_metadata = {
        key: value for key, value in base_metadata.items() if value not in (None, [], {})
    }
    events: List[MerchantCommerceEvent] = [
        MerchantCommerceEvent(
            event_id=_event_id(store_id, "refund.created", refund_id),
            event_type="refund.created",
            occurred_at=created_at,
            platform="shopify",
            source="shopify_webhook",
            store_id=store_id,
            order_id=order_id,
            refund_id=refund_id,
            trace_id=trace_id,
            metadata=base_metadata,
        )
    ]

    # refunds/create means a refund object exists, not necessarily that money
    # moved. Only successful refund transactions become refund.succeeded.
    seen_transactions = set()
    for index, transaction in enumerate(refund.get("transactions") or []):
        if not isinstance(transaction, dict):
            continue
        kind = str(transaction.get("kind") or "").strip().lower()
        status = str(transaction.get("status") or "").strip().lower()
        if kind != "refund" or status != "success":
            continue
        transaction_id = _text(transaction.get("id")) or f"{refund_id}:{index}"
        if transaction_id in seen_transactions:
            continue
        seen_transactions.add(transaction_id)
        amount_cents, currency = shopify_order_total_to_cents(
            {
                "total_price": transaction.get("amount"),
                "currency": transaction.get("currency"),
            }
        )
        events.append(
            MerchantCommerceEvent(
                event_id=_event_id(store_id, "refund.succeeded", transaction_id),
                event_type="refund.succeeded",
                occurred_at=_occurred_at(
                    transaction.get("processed_at"), transaction.get("created_at"), created_at
                ),
                platform="shopify",
                source="shopify_webhook",
                store_id=store_id,
                payment_id=transaction_id,
                order_id=order_id,
                refund_id=refund_id,
                trace_id=trace_id,
                amount_cents=amount_cents,
                currency=currency,
                metadata={
                    **base_metadata,
                    "native_transaction_kind": kind,
                    "native_transaction_status": status,
                },
            )
        )
    return MerchantEventBatch(events=events)


def map_shopify_webhook(
    payload: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
    occurred_at: Optional[datetime] = None,
) -> MerchantEventBatch:
    """Map a verified Shopify lifecycle webhook into the canonical ledger."""
    normalized_topic = str(topic or "").strip().lower()
    if normalized_topic not in SUPPORTED_SHOPIFY_TOPICS:
        raise UnsupportedShopifyCommerceEvent(
            f"unsupported Shopify webhook topic: {normalized_topic or 'missing'}"
        )
    if not isinstance(payload, dict):
        raise ValueError("Shopify webhook body must be an object")
    if not _text(store_id):
        raise ValueError("Shopify canonical mapping requires store_id")
    if normalized_topic == "refunds/create":
        return _refund_events(
            payload,
            topic=normalized_topic,
            delivery_id=delivery_id,
            store_id=store_id,
            fallback_occurred_at=occurred_at,
        )
    return _order_events(
        payload,
        topic=normalized_topic,
        delivery_id=delivery_id,
        store_id=store_id,
        fallback_occurred_at=occurred_at,
    )
