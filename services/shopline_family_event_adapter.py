from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


SUPPORTED_SHOPLINE_TOPICS = frozenset(
    {"orders/create", "orders/paid", "orders/cancelled", "refunds/create"}
)
SUPPORTED_SHOPLAZZA_TOPICS = frozenset(
    {
        "orders/create",
        "orders/paid",
        "orders/cancelled",
        "orders/partially_refunded",
        "orders/refunded",
    }
)
# MerchantCommerceEvent.click_id is capped at 64 characters including `clk_`.
_CLICK_ID_RE = re.compile(r"^clk_[A-Za-z0-9_-]{6,60}$")
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)


class UnsupportedShoplineFamilyEvent(ValueError):
    pass


def _text(value: Any) -> Optional[str]:
    value = str(value or "").strip()
    return value or None


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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
    if amount < 0:
        return None
    multiplier = (
        Decimal("1")
        if str(currency or "").upper() in ZERO_DECIMAL_CURRENCIES
        else Decimal("100")
    )
    return int((amount * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _event_id(
    platform: str,
    store_id: str,
    event_type: str,
    entity_id: str,
) -> str:
    material = json.dumps(
        [platform, store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{platform}:{event_type}:{digest}"


def _payload_fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:32]


def _click_id(*urls: Any) -> Optional[str]:
    for value in urls:
        raw = _text(value)
        if not raw:
            continue
        try:
            query = parse_qs(urlparse(raw).query)
        except Exception:
            continue
        for key in ("pvt_click_id", "pivota_click_id", "click_id", "utm_content"):
            for candidate in query.get(key) or []:
                if _CLICK_ID_RE.fullmatch(str(candidate)):
                    return str(candidate)
    return None


def _line_items(items: Any) -> List[Dict[str, Any]]:
    """Keep commerce join keys and amounts, never arbitrary properties or names."""
    safe: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        safe.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    "product_id",
                    "variant_id",
                    "sku",
                    "spu",
                    "quantity",
                    "price",
                    "total",
                    "subtotal",
                )
                if item.get(key) is not None
            }
        )
    return safe


def _first_payment_id(order: Dict[str, Any]) -> Optional[str]:
    payment_line = _dict(order.get("payment_line"))
    candidates: Iterable[Any] = (
        payment_line.get("transaction_no"),
        payment_line.get("id"),
        order.get("transaction_id"),
    )
    for candidate in candidates:
        if _text(candidate):
            return _text(candidate)
    for key in ("payment_lines", "transactions", "transactions_v2"):
        for transaction in order.get(key) or []:
            if not isinstance(transaction, dict):
                continue
            candidate = _text(
                transaction.get("transaction_no")
                or transaction.get("pay_channel_deal_id")
                or transaction.get("id")
            )
            if candidate:
                return candidate
    return None


def _order_metadata(order: Dict[str, Any], topic: str, delivery_id: Optional[str]) -> Dict[str, Any]:
    metadata = {
        "native_topic": topic,
        "native_status": _text(order.get("status")),
        "native_financial_status": _text(order.get("financial_status")),
        "native_fulfillment_status": _text(order.get("fulfillment_status")),
        "native_payment_method": _text(order.get("payment_method")),
        "native_line_items": _line_items(order.get("line_items")),
        "native_discount_total": order.get("current_total_discounts")
        or order.get("total_discount"),
        "native_shipping_total": order.get("current_total_shipping_price")
        or order.get("total_shipping"),
        "native_total_tax": order.get("current_total_tax") or order.get("total_tax"),
        "webhook_delivery_id": _text(delivery_id),
    }
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def _unwrap(payload: Dict[str, Any], resource_name: str) -> Dict[str, Any]:
    wrapped = payload.get(resource_name)
    return dict(wrapped) if isinstance(wrapped, dict) else dict(payload)


def map_shopline_webhook(
    payload: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
) -> MerchantEventBatch:
    normalized_topic = str(topic or "").strip().lower()
    if normalized_topic not in SUPPORTED_SHOPLINE_TOPICS:
        raise UnsupportedShoplineFamilyEvent(
            f"unsupported SHOPLINE webhook topic: {normalized_topic or 'missing'}"
        )
    if not isinstance(payload, dict):
        raise ValueError("SHOPLINE webhook body must be an object")

    trace_id = _text(delivery_id) or _payload_fingerprint(payload)
    if normalized_topic == "refunds/create":
        refund = _unwrap(payload, "refund")
        refund_id = _text(refund.get("id"))
        successful = []
        for key in ("transactions", "transactions_v2"):
            for transaction in refund.get(key) or []:
                if not isinstance(transaction, dict):
                    continue
                if str(transaction.get("kind") or "").lower() != "refund":
                    continue
                if str(transaction.get("status") or "").lower() != "success":
                    continue
                successful.append(dict(transaction))
        if not successful:
            # SHOPLINE explicitly says refunds/create is not proof of funds flow.
            raise UnsupportedShoplineFamilyEvent(
                "SHOPLINE refunds/create has no successful refund transaction"
            )
        events: List[MerchantCommerceEvent] = []
        seen = set()
        for index, transaction in enumerate(successful):
            transaction_id = _text(transaction.get("id")) or f"{trace_id}:{index}"
            if transaction_id in seen:
                continue
            seen.add(transaction_id)
            order_id = _text(transaction.get("order_id") or refund.get("order_id"))
            if not order_id:
                raise ValueError("SHOPLINE refund webhook is missing order id")
            currency = _text(transaction.get("currency"))
            events.append(
                MerchantCommerceEvent(
                    event_id=_event_id("shopline", store_id, "refund.succeeded", transaction_id),
                    event_type="refund.succeeded",
                    occurred_at=_occurred_at(
                        transaction.get("processed_at"),
                        transaction.get("created_at"),
                        transaction.get("create_at"),
                        refund.get("processed_at"),
                        refund.get("created_at"),
                    ),
                    platform="shopline",
                    source="shopline_webhook",
                    store_id=store_id,
                    payment_id=transaction_id,
                    order_id=order_id,
                    order_ref=build_order_ref("shopline", order_id),
                    refund_id=refund_id,
                    trace_id=trace_id,
                    amount_cents=_amount_cents(transaction.get("amount"), currency),
                    currency=currency.upper() if currency else None,
                    metadata={
                        "native_topic": normalized_topic,
                        "native_transaction_kind": "refund",
                        "native_transaction_status": "success",
                        "webhook_delivery_id": _text(delivery_id),
                    },
                )
            )
        return MerchantEventBatch(events=events)

    order = _unwrap(payload, "order")
    order_id = _text(order.get("id") or order.get("order_id"))
    if not order_id:
        raise ValueError("SHOPLINE order webhook is missing order id")
    event_type = {
        "orders/create": "order.created",
        "orders/paid": "order.paid",
        "orders/cancelled": "order.cancelled",
    }[normalized_topic]
    customer = _dict(order.get("customer"))
    amount = order.get("current_total_price")
    if amount is None:
        amount = order.get("total_price")
    currency = _text(order.get("currency") or order.get("presentment_currency"))
    return MerchantEventBatch(
        events=[
            MerchantCommerceEvent(
                event_id=_event_id("shopline", store_id, event_type, order_id),
                event_type=event_type,
                occurred_at=_occurred_at(
                    order.get("cancelled_at") if event_type == "order.cancelled" else None,
                    order.get("processed_at") if event_type == "order.paid" else None,
                    order.get("updated_at"),
                    order.get("created_at"),
                ),
                platform="shopline",
                source="shopline_webhook",
                store_id=store_id,
                buyer_id=_text(customer.get("id")),
                click_id=_click_id(order.get("landing_site"), order.get("referring_site")),
                cart_id=_text(order.get("cart_token")),
                checkout_id=_text(order.get("checkout_id") or order.get("checkout_token")),
                payment_id=_first_payment_id(order),
                order_id=order_id,
                order_ref=build_order_ref("shopline", order_id),
                trace_id=trace_id,
                amount_cents=_amount_cents(amount, currency),
                currency=currency.upper() if currency else None,
                metadata=_order_metadata(order, normalized_topic, delivery_id),
            )
        ]
    )


def map_shoplazza_webhook(
    payload: Dict[str, Any],
    *,
    topic: str,
    delivery_id: Optional[str],
    store_id: str,
) -> MerchantEventBatch:
    normalized_topic = str(topic or "").strip().lower()
    if normalized_topic not in SUPPORTED_SHOPLAZZA_TOPICS:
        raise UnsupportedShoplineFamilyEvent(
            f"unsupported Shoplazza webhook topic: {normalized_topic or 'missing'}"
        )
    if not isinstance(payload, dict):
        raise ValueError("Shoplazza webhook body must be an object")
    order = _unwrap(payload, "order")
    order_id = _text(order.get("id") or order.get("order_id"))
    if not order_id:
        raise ValueError("Shoplazza order webhook is missing order id")

    event_type = {
        "orders/create": "order.created",
        "orders/paid": "order.paid",
        "orders/cancelled": "order.cancelled",
        "orders/partially_refunded": "refund.succeeded",
        "orders/refunded": "refund.succeeded",
    }[normalized_topic]
    trace_id = _text(delivery_id) or _payload_fingerprint(payload)
    customer = _dict(order.get("customer"))
    refund_event = event_type == "refund.succeeded"
    amount = None if refund_event else (
        order.get("real_total_paid")
        or order.get("total_paid")
        or order.get("total_price")
    )
    currency = _text(order.get("currency"))
    metadata = _order_metadata(order, normalized_topic, delivery_id)
    if refund_event and order.get("total_refund_price") not in (None, ""):
        # This field is cumulative. Preserve it as a labelled fact rather than
        # putting it in amount_cents, where multiple partial-refund events could
        # be incorrectly summed as incremental refunds.
        metadata["native_cumulative_refund_total"] = order.get("total_refund_price")
        metadata["native_amount_semantics"] = "cumulative_refund_total"
    entity_id = trace_id if refund_event else order_id
    return MerchantEventBatch(
        events=[
            MerchantCommerceEvent(
                event_id=_event_id("shoplazza", store_id, event_type, entity_id),
                event_type=event_type,
                occurred_at=_occurred_at(
                    order.get("canceled_at") if event_type == "order.cancelled" else None,
                    order.get("placed_at") if event_type == "order.paid" else None,
                    order.get("updated_at"),
                    order.get("created_at"),
                ),
                platform="shoplazza",
                source="shoplazza_webhook",
                store_id=store_id,
                buyer_id=_text(customer.get("id")),
                click_id=_click_id(
                    order.get("landing_site"),
                    order.get("last_landing_url"),
                    order.get("checkout_url"),
                ),
                payment_id=_first_payment_id(order),
                order_id=order_id,
                order_ref=build_order_ref("shoplazza", order_id),
                trace_id=trace_id,
                amount_cents=_amount_cents(amount, currency),
                currency=currency.upper() if currency else None,
                metadata=metadata,
            )
        ]
    )
