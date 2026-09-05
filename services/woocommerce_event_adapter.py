from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from services.commerce_attribution_service import PIVOTA_ORDER_ID_NOTE_ATTR
from services.commerce_order_ref import build_order_ref, pivota_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch
from services.woocommerce_conversion_poller import extract_click_id_from_wc_order


SUPPORTED_WOOCOMMERCE_TOPICS = frozenset({"order.created", "order.updated"})
_PAID_STATUSES = frozenset({"processing", "completed"})


class UnsupportedWooCommerceEvent(ValueError):
    pass


def _wc_order_ref(order: Dict[str, Any], order_id: str) -> Optional[str]:
    """The canonical ref: Pivota's when the writeback marker is on the order.

    Pivota's WooCommerce order writeback stamps ``pivota_order_id`` into the
    order's ``meta_data``. Its presence means this purchase originated in
    Pivota and is already in the ledger under ``pivota:<order id>`` from the
    Stripe bridge; without it the order was placed on the storefront and
    WooCommerce is its system of record.

    Unlike Shopify there is no indexed column holding the WooCommerce order id
    on the Pivota row (the writeback records it inside `orders.metadata`), so
    orders written back before the marker existed have no fallback and keep
    their `woocommerce:` identity. Honest, and no worse than today.
    """
    meta = order.get("meta_data")
    if isinstance(meta, list):
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("key") or "").strip() == PIVOTA_ORDER_ID_NOTE_ATTR:
                pivota_id = str(entry.get("value") or "").strip()
                if pivota_id:
                    return pivota_order_ref(pivota_id)
    return build_order_ref("woocommerce", order_id)


def _text(value: Any) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


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


def _amount_cents(value: Any, decimals: Any = 2) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
        places = max(0, min(int(decimals), 6))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount < 0:
        return None
    multiplier = Decimal(10) ** places
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _refund_amount_cents(value: Any, decimals: Any = 2) -> Optional[int]:
    """Minor units for one entry of the wc/v3 order `refunds[]` array.

    WooCommerce reports a refund line `total` as a NEGATIVE decimal string in the
    order currency ("-10.50"). The canonical contract stores a non-negative
    magnitude, so the sign is dropped rather than the whole event: a refund whose
    total cannot be parsed still happened, and is emitted with a null amount.
    """
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
        places = max(0, min(int(decimals), 6))
    except (InvalidOperation, TypeError, ValueError):
        return None
    multiplier = Decimal(10) ** places
    return int((abs(amount) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _native_refunds(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every `refunds[]` entry that carries a usable native refund identity.

    An entry without an `id` is skipped: the id is the only thing that makes a
    refund idempotent across the `order.updated` deliveries that repeat the whole
    array on every later change. A malformed sibling never suppresses a valid one.
    """
    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in order.get("refunds") or []:
        if not isinstance(entry, dict):
            continue
        refund_id = _text(entry.get("id"))
        if not refund_id or refund_id in seen:
            continue
        seen.add(refund_id)
        entries.append(entry)
    return entries


def _line_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    allowlisted = []
    for item in order.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        allowlisted.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    "product_id",
                    "variation_id",
                    "sku",
                    "quantity",
                    "price",
                    "subtotal",
                    "total",
                )
                if item.get(key) is not None
            }
        )
    return allowlisted


def _entity_event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        ["woocommerce", store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"woocommerce:{event_type}:{digest}"


def map_woocommerce_webhook(
    payload: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
) -> MerchantEventBatch:
    normalized_topic = str(topic or "").strip().lower()
    if normalized_topic not in SUPPORTED_WOOCOMMERCE_TOPICS:
        raise UnsupportedWooCommerceEvent(
            f"unsupported WooCommerce webhook topic: {normalized_topic or 'missing'}"
        )
    if not isinstance(payload, dict):
        raise ValueError("WooCommerce webhook body must be an object")
    # Modern wc/v3 sends the order object directly. Older webhook versions wrap
    # it as {"order": {...}}; accepting both keeps the mapper version-tolerant.
    wrapped = payload.get("order")
    order = dict(wrapped) if isinstance(wrapped, dict) else dict(payload)
    order_id = _text(order.get("id") or order.get("order_number"))
    if not order_id:
        raise ValueError("WooCommerce order webhook is missing order id")

    status = str(order.get("status") or "").strip().lower()
    transaction_id = _text(order.get("transaction_id"))
    customer_id = _text(order.get("customer_id"))
    if customer_id == "0":
        customer_id = None
    click_id = extract_click_id_from_wc_order(order)
    order_ref = _wc_order_ref(order, order_id)
    currency = str(order.get("currency") or "").strip().upper() or None
    decimals = order.get("currency_minor_unit", 2)
    trace_id = _text(delivery_id) or hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:32]
    metadata = {
        "native_topic": normalized_topic,
        "native_status": status or None,
        "native_payment_method": _text(order.get("payment_method")),
        "native_payment_method_title": _text(order.get("payment_method_title")),
        "native_line_items": _line_items(order),
        "native_discount_total": order.get("discount_total"),
        "native_shipping_total": order.get("shipping_total"),
        "native_total_tax": order.get("total_tax"),
        "webhook_delivery_id": _text(delivery_id),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, [], {})}

    # WooCommerce has no refund webhook topic. A partial refund arrives as an
    # `order.updated` whose `refunds[]` array has grown by one entry while the
    # order status stays `processing`/`completed`, so refunds are read off the
    # order payload on every topic rather than inferred from the status alone.
    native_refunds = _native_refunds(order)
    refund_events = [
        MerchantCommerceEvent(
            event_id=_entity_event_id(store_id, "refund.succeeded", refund_id),
            event_type="refund.succeeded",
            # A `refunds[]` entry carries no timestamp of its own in the order
            # payload, so the order's modification time is the closest available
            # anchor. It is the delivery that first exposed the refund, not the
            # moment the refund was issued; a per-refund time would need a
            # separate wc/v3 /orders/<id>/refunds fetch.
            occurred_at=_occurred_at(
                order.get("date_modified_gmt"),
                order.get("date_modified"),
                order.get("date_created_gmt"),
                order.get("date_created"),
            ),
            platform="woocommerce",
            source="woocommerce_webhook",
            store_id=store_id,
            buyer_id=customer_id,
            click_id=click_id,
            payment_id=transaction_id,
            order_id=order_id,
            refund_id=refund_id,
            trace_id=trace_id,
            amount_cents=_refund_amount_cents(refund.get("total"), decimals),
            currency=currency,
            # `refunds[].reason` is merchant free text and may carry buyer PII;
            # it is deliberately not copied into canonical metadata.
            metadata={**metadata, "native_amount_semantics": "native_refund_total"},
        )
        for refund, refund_id in (
            (refund, _text(refund.get("id"))) for refund in native_refunds
        )
    ]

    event_types: List[str]
    if status == "cancelled":
        event_types = ["order.cancelled"]
    elif status == "failed":
        event_types = ["payment.failed"]
    elif status == "refunded":
        # Older wc/v3 payloads omit `refunds[]` entirely. Only then does the
        # cumulative `total_refunded` stand in for per-refund identity.
        event_types = [] if refund_events else ["refund.succeeded"]
    elif status in _PAID_STATUSES or order.get("date_paid_gmt") or order.get("date_paid"):
        event_types = ["order.created", "order.paid"]
    else:
        event_types = ["order.created"]

    events = []
    for event_type in event_types:
        if event_type == "order.created":
            event_occurred = _occurred_at(
                order.get("date_created_gmt"),
                order.get("date_created"),
            )
        elif event_type == "order.paid":
            event_occurred = _occurred_at(
                order.get("date_paid_gmt"),
                order.get("date_paid"),
                order.get("date_modified_gmt"),
                order.get("date_modified"),
            )
        else:
            event_occurred = _occurred_at(
                order.get("date_modified_gmt"),
                order.get("date_modified"),
                order.get("date_created_gmt"),
                order.get("date_created"),
            )
        amount_value = order.get("total_refunded") if event_type.startswith("refund.") else order.get("total")
        stable_entity = (
            transaction_id
            if event_type.startswith("payment.") and transaction_id
            else f"{order_id}:refund"
            if event_type.startswith("refund.")
            else order_id
        )
        event_metadata = metadata
        if event_type.startswith("refund."):
            event_metadata = {
                **metadata,
                "native_amount_semantics": "cumulative_refund_total",
                **(
                    {"native_cumulative_refund_total": amount_value}
                    if amount_value not in (None, "")
                    else {}
                ),
            }
        events.append(
            MerchantCommerceEvent(
                event_id=_entity_event_id(store_id, event_type, stable_entity),
                event_type=event_type,
                occurred_at=event_occurred,
                platform="woocommerce",
                source="woocommerce_webhook",
                store_id=store_id,
                buyer_id=customer_id,
                click_id=click_id,
                payment_id=transaction_id,
                order_id=order_id,
                order_ref=order_ref,
                trace_id=trace_id,
                amount_cents=_amount_cents(amount_value, decimals),
                currency=currency,
                metadata=event_metadata,
            )
        )
    return MerchantEventBatch(events=events + refund_events)
