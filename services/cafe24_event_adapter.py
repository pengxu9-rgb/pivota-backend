from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from adapters.cafe24_adapter import normalize_cafe24_mall_id
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


DATA_BRIDGE_EVENT_MAP = {
    "view_content": "product.viewed",
    "initiate_orderform": "checkout.started",
    "create_order": "order.created",
}

STORE_EVENT_MAP = {
    90023: "order.created",
    90025: "payment.status_changed",
    90026: "order.cancelled",
    90027: "return.created",
    90028: "return.created",
    90029: "refund.status_changed",
    90072: "order.cancelled",
    90073: "refund.status_changed",
    90074: "return.created",
    90084: "cart.item_added",
}

BULK_ORDER_EVENT_NUMBERS = {90072, 90073, 90074}

ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"
}


class UnsupportedCafe24Event(ValueError):
    pass


def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _products(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []


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
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    exponent = Decimal("1") if str(currency or "").upper() in ZERO_DECIMAL_CURRENCIES else Decimal("100")
    return int((amount * exponent).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _native_products(products: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Retain commerce facts while dropping arbitrary/PII-rich native payloads."""
    sanitized = []
    for product in products:
        sanitized.append(
            {
                key: product.get(key)
                for key in (
                    "product_no",
                    "variant_code",
                    "product_code",
                    "product_name",
                    "cate_no",
                    "cate_name",
                    "quantity",
                    "product_price",
                    "option_extra_price",
                    "option_value",
                )
                if product.get(key) is not None
            }
        )
    return sanitized


def _source_from_url(value: Any) -> Optional[str]:
    raw = _text(value)
    if not raw:
        return None
    try:
        return (urlparse(raw).hostname or "").lower() or None
    except Exception:
        return None


def extract_cafe24_mall_id(payload: Dict[str, Any]) -> str:
    event_data = _dict(payload.get("event_data"))
    resource = _dict(payload.get("resource"))
    return normalize_cafe24_mall_id(event_data.get("mall_id") or resource.get("mall_id"))


def _event_id(trace_id: str, event_type: str, suffix: Any = None) -> str:
    parts = ["cafe24", trace_id, event_type]
    if suffix not in (None, ""):
        parts.append(str(suffix))
    return ":".join(parts)


def _entity_event_id(
    *,
    store_id: str,
    event_type: str,
    entity_kind: str,
    entity_id: Optional[str],
    fallback_trace_id: str,
) -> str:
    """Deduplicate the same lifecycle fact delivered by multiple Cafe24 feeds."""
    if not entity_id:
        return _event_id(fallback_trace_id, event_type)
    material = json.dumps(
        ["cafe24", store_id, event_type, entity_kind, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"cafe24:{event_type}:{digest}"


def _data_bridge_batch(
    payload: Dict[str, Any],
    *,
    trace_id: str,
    store_id: str,
) -> MerchantEventBatch:
    native_name = str(payload.get("event_name") or "").strip().lower()
    canonical_type = DATA_BRIDGE_EVENT_MAP.get(native_name)
    if not canonical_type:
        raise UnsupportedCafe24Event(f"unsupported Cafe24 Data Bridge event: {native_name}")

    data = _dict(payload.get("event_data"))
    analytics = _dict(payload.get("analytics_data"))
    mall_id = normalize_cafe24_mall_id(data.get("mall_id"))
    products = _products(data.get("product_list"))
    occurred = _occurred_at(payload.get("event_time"))
    session_id = _text(analytics.get("CVID"))
    visitor_id = _text(analytics.get("CVID_Y") or analytics.get("CVID"))
    order_id = _text(data.get("order_id"))
    source_url = _text(analytics.get("event_source_url"))
    base_metadata = {
        "native_event_name": native_name,
        "native_mall_id": mall_id,
        "native_shop_no": data.get("shop_no"),
        "native_products": _native_products(products),
        "webhook_trace_id": trace_id,
    }
    base_metadata = {key: value for key, value in base_metadata.items() if value not in (None, [], {})}

    if canonical_type == "product.viewed":
        # One canonical event per viewed product keeps product-level funnel
        # counts honest while sharing the same Cafe24 CVID interaction.
        events = []
        for index, product in enumerate(products or [{}]):
            native_product_no = _text(product.get("product_no"))
            events.append(
                MerchantCommerceEvent(
                    event_id=_event_id(trace_id, canonical_type, native_product_no or index),
                    event_type=canonical_type,
                    occurred_at=occurred,
                    platform="cafe24",
                    source="cafe24_data_bridge",
                    store_id=store_id,
                    session_id=session_id,
                    visitor_id=visitor_id,
                    trace_id=trace_id,
                    query_source=_source_from_url(source_url),
                    metadata={**base_metadata, "native_product_no": native_product_no},
                )
            )
        return MerchantEventBatch(events=events)

    currency = _text(data.get("currency")) or "KRW"
    amount = data.get("actual_payment_amount")
    if amount is None:
        amount = data.get("order_price_amount") or data.get("total_price")
    return MerchantEventBatch(
        events=[
            MerchantCommerceEvent(
                event_id=_entity_event_id(
                    store_id=store_id,
                    event_type=canonical_type,
                    entity_kind="order",
                    entity_id=order_id,
                    fallback_trace_id=trace_id,
                ),
                event_type=canonical_type,
                occurred_at=occurred,
                platform="cafe24",
                source="cafe24_data_bridge",
                store_id=store_id,
                session_id=session_id,
                visitor_id=visitor_id,
                buyer_id=_text(data.get("member_id")),
                order_id=order_id,
                trace_id=trace_id,
                query_source=_source_from_url(source_url),
                amount_cents=_amount_cents(amount, currency),
                currency=currency,
                metadata=base_metadata,
            )
        ]
    )


def _store_webhook_batch(
    payload: Dict[str, Any],
    *,
    trace_id: str,
    store_id: str,
) -> MerchantEventBatch:
    try:
        event_no = int(payload.get("event_no"))
    except (TypeError, ValueError) as exc:
        raise UnsupportedCafe24Event("Cafe24 webhook is missing event_no") from exc
    mapped = STORE_EVENT_MAP.get(event_no)
    if not mapped:
        raise UnsupportedCafe24Event(f"unsupported Cafe24 webhook event_no: {event_no}")

    resource = _dict(payload.get("resource"))
    if event_no in BULK_ORDER_EVENT_NUMBERS:
        order_ids = [
            item.strip()
            for item in str(resource.get("order_id") or "").split(",")
            if item.strip()
        ]
        if len(order_ids) > 1:
            events = []
            for child_order_id in order_ids:
                child_batch = _store_webhook_batch(
                    {
                        **payload,
                        "resource": {**resource, "order_id": child_order_id},
                    },
                    trace_id=trace_id,
                    store_id=store_id,
                )
                events.extend(child_batch.events)
            return MerchantEventBatch(events=events)
    mall_id = normalize_cafe24_mall_id(resource.get("mall_id"))
    order_id = _text(resource.get("order_id"))
    cart_id = _text(resource.get("cart_id") or resource.get("basket_id"))
    paid = str(resource.get("paid") or "").upper()
    currency = _text(resource.get("currency")) or "KRW"
    amount = resource.get("actual_payment_amount")
    if amount is None:
        amount = resource.get("order_price_amount")
    if event_no == 90029:
        amount = resource.get("refunded_amount") or resource.get("refund_amount") or amount
    amount_cents = _amount_cents(amount, currency)
    occurred = _occurred_at(
        resource.get("payment_date") if event_no == 90025 else None,
        resource.get("refunded_date"),
        resource.get("request_date"),
        resource.get("order_date"),
        resource.get("updated_date"),
    )
    products = _products(resource.get("items") or resource.get("product_list"))
    if event_no == 90084:
        products = [resource]
    payment_id = _text(
        resource.get("payment_id")
        or resource.get("transaction_id")
        or resource.get("payment_gateway_transaction_id")
    )
    refund_id = _text(resource.get("refund_id") or resource.get("refund_no"))
    return_id = _text(resource.get("return_id") or resource.get("return_no"))
    metadata = {
        "native_event_no": event_no,
        "native_event_code": _text(resource.get("event_code")),
        "native_mall_id": mall_id,
        "native_shop_no": resource.get("event_shop_no"),
        "native_paid_state": paid or None,
        "native_payment_method": _text(resource.get("payment_method")),
        "native_payment_gateway": _text(resource.get("payment_gateway_name")),
        "native_order_place_id": _text(resource.get("order_place_id")),
        "native_product_no": _text(resource.get("product_no")),
        "native_variant_code": _text(resource.get("variant_code")),
        "native_quantity": resource.get("quantity"),
        "native_shipping_type": _text(resource.get("shipping_type")),
        "native_product_bundle": _text(resource.get("product_bundle")),
        "native_products": _native_products(products),
        "webhook_trace_id": trace_id,
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, [], {})}

    canonical_types: List[str]
    if mapped == "payment.status_changed":
        canonical_types = [
            "order.paid" if paid == "T" else "payment.authorized" if paid == "M" else "payment.failed"
        ]
    elif mapped == "refund.status_changed":
        canonical_types = ["refund.succeeded" if resource.get("refunded_date") else "refund.created"]
    else:
        canonical_types = [mapped]
        if mapped == "order.created" and paid == "T":
            canonical_types.append("order.paid")

    events = []
    for canonical_type in canonical_types:
        if canonical_type in {"order.created", "order.paid", "order.cancelled"}:
            entity_kind, stable_entity_id = "order", order_id
        elif canonical_type.startswith("refund."):
            entity_kind = "refund" if refund_id else "order_refund"
            stable_entity_id = refund_id or (order_id if event_no == 90073 else None)
        elif canonical_type.startswith("return."):
            entity_kind = "return" if return_id else "order_return"
            stable_entity_id = return_id or (order_id if event_no == 90074 else None)
        elif canonical_type.startswith("payment."):
            entity_kind, stable_entity_id = "payment", payment_id
        else:
            entity_kind, stable_entity_id = "event", None
        event_occurred = _occurred_at(
            resource.get("payment_date") if canonical_type == "order.paid" else None,
            resource.get("refunded_date") if canonical_type == "refund.succeeded" else None,
            resource.get("request_date"),
            occurred,
        )
        events.append(
            MerchantCommerceEvent(
                event_id=_entity_event_id(
                    store_id=store_id,
                    event_type=canonical_type,
                    entity_kind=entity_kind,
                    entity_id=stable_entity_id,
                    fallback_trace_id=trace_id,
                ),
                event_type=canonical_type,
                occurred_at=event_occurred,
                platform="cafe24",
                source="cafe24_webhook",
                store_id=store_id,
                buyer_id=_text(resource.get("member_id")),
                cart_id=cart_id,
                order_id=order_id,
                payment_id=payment_id,
                refund_id=refund_id,
                return_id=return_id,
                trace_id=trace_id,
                amount_cents=amount_cents,
                currency=currency,
                metadata=metadata,
            )
        )
    return MerchantEventBatch(events=events)


def map_cafe24_webhook(
    payload: Dict[str, Any],
    *,
    trace_id: Optional[str],
    store_id: str,
) -> MerchantEventBatch:
    if not isinstance(payload, dict):
        raise ValueError("Cafe24 webhook body must be an object")
    stable_trace = _text(trace_id)
    if not stable_trace:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stable_trace = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    if payload.get("event_name") is not None:
        return _data_bridge_batch(payload, trace_id=stable_trace, store_id=store_id)
    return _store_webhook_batch(payload, trace_id=stable_trace, store_id=store_id)
