from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


SUPPORTED_STRIPE_COMMERCE_EVENTS = frozenset(
    {
        "payment_intent.amount_capturable_updated",
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "refund.created",
        "refund.updated",
        "charge.refunded",
    }
)


class UnsupportedStripeCommerceEvent(ValueError):
    pass


def _text(value: Any, *, max_length: int = 128) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _occurred_at(*values: Any) -> datetime:
    for value in values:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            try:
                parsed = datetime.fromtimestamp(value, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                continue
        else:
            raw = _text(value, max_length=64)
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


def _minor_amount(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        ["stripe", store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"stripe:{event_type}:{digest}"


def _order_metadata(order: Dict[str, Any]) -> Dict[str, Any]:
    value = order.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _common_event(
    *,
    event_type: str,
    native_event_type: str,
    entity_id: str,
    payment_id: Optional[str],
    refund_id: Optional[str],
    amount_minor: Any,
    currency: Any,
    native_status: Any,
    occurred_at: datetime,
    stripe_event_id: str,
    order: Dict[str, Any],
    store_id: str,
    platform: str,
) -> MerchantCommerceEvent:
    order_id = _text(order.get("order_id"))
    if not order_id:
        raise ValueError("Stripe canonical event requires an order id")
    order_meta = _order_metadata(order)
    click_id = _text(
        order_meta.get("pivota_click_id") or order_meta.get("click_id"),
        max_length=64,
    )
    return MerchantCommerceEvent(
        event_id=_event_id(store_id, event_type, entity_id),
        event_type=event_type,
        occurred_at=occurred_at,
        platform=platform,
        source="stripe_webhook",
        store_id=store_id,
        surface="psp",
        session_id=_text(order.get("agent_session_id")),
        buyer_id=_text(order.get("buyer_id")),
        click_id=click_id,
        checkout_id=_text(order_meta.get("checkout_id")),
        payment_id=_text(payment_id),
        order_id=order_id,
        refund_id=_text(refund_id),
        trace_id=_text(stripe_event_id),
        brief_id=_text(order_meta.get("brief_id")),
        amount_cents=_minor_amount(amount_minor),
        currency=_text(currency, max_length=3),
        metadata={
            key: value
            for key, value in {
                "native_event_name": native_event_type,
                "native_status": _text(native_status),
                "native_payment_gateway": "stripe",
                "native_amount_semantics": "psp_minor_units",
                "webhook_delivery_id": _text(stripe_event_id),
            }.items()
            if value is not None
        },
    )


def map_stripe_webhook_event(
    data: Dict[str, Any],
    *,
    event_type: str,
    stripe_event_id: str,
    event_created: Any,
    order: Dict[str, Any],
    store_id: str,
    platform: str,
) -> MerchantEventBatch:
    """Map one already-verified and business-validated Stripe event."""
    native_type = str(event_type or "").strip().lower()
    if native_type not in SUPPORTED_STRIPE_COMMERCE_EVENTS:
        raise UnsupportedStripeCommerceEvent(
            f"unsupported Stripe commerce event: {native_type or 'missing'}"
        )
    if not isinstance(data, dict) or not isinstance(order, dict):
        raise ValueError("Stripe event data and resolved order must be objects")
    if not _text(store_id) or not _text(platform, max_length=32):
        raise ValueError("Stripe canonical event requires store_id and platform")
    trace_id = _text(stripe_event_id)
    if not trace_id:
        raise ValueError("Stripe canonical event requires event id")
    occurred = _occurred_at(data.get("created"), event_created)

    if native_type.startswith("payment_intent."):
        payment_id = _text(data.get("id"))
        if not payment_id:
            raise ValueError("Stripe payment event is missing payment intent id")
        canonical_type = {
            "payment_intent.amount_capturable_updated": "payment.authorized",
            "payment_intent.succeeded": "payment.succeeded",
            "payment_intent.payment_failed": "payment.failed",
        }[native_type]
        amount = (
            data.get("amount_received")
            if canonical_type == "payment.succeeded" and data.get("amount_received") is not None
            else data.get("amount_capturable")
            if canonical_type == "payment.authorized" and data.get("amount_capturable") is not None
            else data.get("amount")
        )
        return MerchantEventBatch(
            events=[
                _common_event(
                    event_type=canonical_type,
                    native_event_type=native_type,
                    entity_id=payment_id,
                    payment_id=payment_id,
                    refund_id=None,
                    amount_minor=amount,
                    currency=data.get("currency"),
                    native_status=data.get("status"),
                    occurred_at=occurred,
                    stripe_event_id=trace_id,
                    order=order,
                    store_id=store_id,
                    platform=platform,
                )
            ]
        )

    if native_type in {"refund.created", "refund.updated"}:
        refund_id = _text(data.get("id"))
        payment_id = _text(data.get("payment_intent"))
        if not refund_id:
            raise ValueError("Stripe refund event is missing refund id")
        status = str(data.get("status") or "").strip().lower()
        if native_type == "refund.updated" and status != "succeeded":
            raise UnsupportedStripeCommerceEvent(
                "Stripe refund.updated is not a successful refund"
            )
        canonical_type = "refund.created" if native_type == "refund.created" else "refund.succeeded"
        return MerchantEventBatch(
            events=[
                _common_event(
                    event_type=canonical_type,
                    native_event_type=native_type,
                    entity_id=refund_id,
                    payment_id=payment_id,
                    refund_id=refund_id,
                    amount_minor=data.get("amount") if canonical_type == "refund.succeeded" else None,
                    currency=data.get("currency"),
                    native_status=status,
                    occurred_at=occurred,
                    stripe_event_id=trace_id,
                    order=order,
                    store_id=store_id,
                    platform=platform,
                )
            ]
        )

    # charge.refunded is cumulative, so never emit the charge total as a second
    # refund. Emit only embedded successful refund objects, using their IDs; a
    # later refund.updated delivery then deduplicates against the same entity.
    refunds = data.get("refunds") if isinstance(data.get("refunds"), dict) else {}
    refund_rows = refunds.get("data") if isinstance(refunds.get("data"), list) else []
    events: List[MerchantCommerceEvent] = []
    seen = set()
    for refund in refund_rows:
        if not isinstance(refund, dict):
            continue
        status = str(refund.get("status") or "").strip().lower()
        refund_id = _text(refund.get("id"))
        if status != "succeeded" or not refund_id or refund_id in seen:
            continue
        seen.add(refund_id)
        events.append(
            _common_event(
                event_type="refund.succeeded",
                native_event_type=native_type,
                entity_id=refund_id,
                payment_id=_text(refund.get("payment_intent") or data.get("payment_intent")),
                refund_id=refund_id,
                amount_minor=refund.get("amount"),
                currency=refund.get("currency") or data.get("currency"),
                native_status=status,
                occurred_at=_occurred_at(refund.get("created"), occurred),
                stripe_event_id=trace_id,
                order=order,
                store_id=store_id,
                platform=platform,
            )
        )
    if not events:
        raise UnsupportedStripeCommerceEvent(
            "Stripe charge.refunded has no embedded successful refund objects"
        )
    return MerchantEventBatch(events=events)
