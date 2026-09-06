"""Pure mapper: a Squarespace Commerce order -> canonical ledger events.

Squarespace's Orders API shapes this module in four ways, each of which is
recorded as VERIFIED or ASSUMED in docs/SQUARESPACE_TELEMETRY.md:

1. **There is no payment status.** The order resource carries no
   ``paymentStatus`` / ``financialStatus`` field at all. An order exists in the
   Orders API because a checkout was paid, so ``order.paid`` is emitted from
   the order's own existence, anchored at ``createdOn`` — payment precedes the
   order rather than following it. The one exception is ``testmode``, which is
   dropped entirely rather than counted (see 4).

2. **Refunds are a CUMULATIVE total with no per-refund identity.**
   ``refundedTotal`` is one money object on the order; there is no ``refunds[]``
   array, no refund id, and no per-refund timestamp anywhere in the Orders API.
   So the only per-observation refund amount that exists is the delta against
   what Pivota has already recorded — exactly the Shoplazza shape
   (services/shopline_family_event_adapter.py::_shoplazza_refund_batch), and
   deliberately the same code shape: the CALLER supplies
   ``previously_recorded_refund_cents`` so this module stays pure and the read
   and the write it feeds are held under one lock.

3. **The webhook delivery is thin.** A notification names ``data.orderId`` and
   carries no order fields, so the receiver fetches the order first
   (services/squarespace_order_fetch.py) and this mapper only ever sees a real
   order resource — the same resource the reconciliation sweep lists. That is
   why every event id here is derived from the ORDER id and the event type and
   never from the notification id: a webhook observation and a later sweep
   observation of the same fact must land on one ledger row, not two.

4. ``testmode`` orders are real rows in the Orders API that were never paid
   for. They are ignored, never ingested: a test order in
   ``paid_amount_cents_by_currency`` is fabricated GMV.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


PLATFORM = "squarespace"

# The notification topics this bridge subscribes and maps. Both deliver the
# same thin envelope and both are answered by re-reading the order, so they map
# identically; the topic is kept only as metadata.
SQUARESPACE_ORDER_TOPICS = ("order.create", "order.update")
# Subscribed by `ensure` only so an uninstall is observable; it names no order
# and is ignored by the receiver rather than mapped.
SQUARESPACE_UNINSTALL_TOPIC = "extension.uninstall"
SUPPORTED_SQUARESPACE_TOPICS = SQUARESPACE_ORDER_TOPICS

_TOPIC_BY_NORMALIZED = {topic.lower(): topic for topic in SUPPORTED_SQUARESPACE_TOPICS}

# `fulfillmentStatus` is the ONLY lifecycle enum on a Squarespace order.
# CANCELED is the cancellation tell; PENDING and FULFILLED are not lifecycle
# facts this ledger models (fulfilment is not a canonical event type here).
CANCELLED_FULFILLMENT_STATUS = "CANCELED"

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

# Squarespace line items carry `productName`, which is merchant copy rather
# than a join key, and nothing else that the ledger's line-item allowlist
# accepts. Only join keys and the unit amount are kept.
_MAX_LINE_ITEMS = 50


class UnsupportedSquarespaceEvent(ValueError):
    """This observation carries nothing the ledger should record. 2xx, not 5xx."""


def normalize_squarespace_topic(value: Any) -> Optional[str]:
    """The canonical spelling of a supported notification topic, else None."""
    return _TOPIC_BY_NORMALIZED.get(str(value or "").strip().lower())


def is_supported_squarespace_topic(value: Any) -> bool:
    return normalize_squarespace_topic(value) is not None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value if value is not None else "").strip()
    return normalized or None


def _money(value: Any) -> Dict[str, Any]:
    """A Squarespace money object ``{"currency": "USD", "value": "10.00"}``."""
    return dict(value) if isinstance(value, dict) else {}


def _currency_of(*candidates: Any) -> Optional[str]:
    for candidate in candidates:
        currency = _text(_money(candidate).get("currency"))
        if currency:
            return currency.upper()
    return None


def _amount_cents(money: Any, currency: Optional[str]) -> Optional[int]:
    """Minor units from a money object. None when absent or not a real amount.

    A negative value is refused rather than clamped: it is a malformed money
    claim, and silently reading it as 0 would let it shadow a real amount.
    """
    raw = _money(money).get("value")
    if raw in (None, ""):
        return None
    try:
        amount = Decimal(str(raw))
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


def _occurred_at(*values: Any) -> datetime:
    """UTC instant from the first parseable ISO-8601 value.

    Squarespace timestamps are ISO 8601 with a ``Z`` suffix
    (``2026-09-05T10:00:00.000Z``). Falling through to "now" is the last
    resort; every caller passes at least ``createdOn``.
    """
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


def _entity_event_id(store_id: str, event_type: str, entity_id: str) -> str:
    """Deterministic id from (platform, store, event type, entity).

    NOT from the notification id. The reconciliation sweep sees the same order
    with no notification at all, and both observations must collapse onto one
    ledger row.
    """
    material = json.dumps(
        [PLATFORM, store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{PLATFORM}:{event_type}:{digest}"


def squarespace_order_id(order: Any) -> Optional[str]:
    """The native order id, or None. Never raises: callers use it to decide."""
    if not isinstance(order, dict):
        return None
    return _text(order.get("id"))


def squarespace_order_ref(order: Any) -> Optional[str]:
    order_id = squarespace_order_id(order)
    return build_order_ref(PLATFORM, order_id) if order_id else None


def squarespace_order_currency(order: Any) -> Optional[str]:
    """The order's currency, upper-cased, or None.

    The receiver and the sweep need this before mapping: the "already
    recorded" figure they subtract is only comparable to this observation's
    cumulative total if both are in the same unit.
    """
    if not isinstance(order, dict):
        return None
    return _currency_of(
        order.get("grandTotal"), order.get("refundedTotal"), order.get("subtotal")
    )


def squarespace_refunded_total_cents(order: Any) -> Optional[int]:
    """The order's CUMULATIVE refunded total in minor units, or None.

    Used by the callers to decide whether the money read-modify-write lock has
    to be taken at all: an order with nothing refunded has no delta to compute,
    whatever the baseline is.
    """
    if not isinstance(order, dict):
        return None
    return _amount_cents(order.get("refundedTotal"), squarespace_order_currency(order))


def is_squarespace_testmode(order: Any) -> bool:
    return bool(isinstance(order, dict) and order.get("testmode"))


def _line_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Join keys and the unit amount only.

    `productName` is merchant copy, `customizations` is buyer-entered free
    text, and neither is a join key. The ledger's line-item allowlist would
    reject both anyway; dropping them here is what makes that rejection
    impossible to trip rather than merely unlikely.
    """
    raw = order.get("lineItems")
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in raw[:_MAX_LINE_ITEMS]:
        if not isinstance(entry, dict):
            continue
        item: Dict[str, Any] = {}
        for source_key, target_key in (
            ("id", "id"),
            ("productId", "product_id"),
            ("variantId", "variant_id"),
            ("sku", "sku"),
        ):
            value = _text(entry.get(source_key))
            if value:
                item[target_key] = value
        quantity = entry.get("quantity")
        if isinstance(quantity, int) and not isinstance(quantity, bool):
            item["quantity"] = quantity
        price = _text(_money(entry.get("unitPricePaid")).get("value"))
        if price:
            item["price"] = price
        if item:
            items.append(item)
    return items


def map_squarespace_order(
    order: Dict[str, Any],
    *,
    store_id: str,
    source: str,
    topic: Optional[str] = None,
    trace_id: Optional[str] = None,
    previously_recorded_refund_cents: Optional[int] = None,
) -> MerchantEventBatch:
    """Canonical events for ONE fetched Squarespace order.

    ``source`` names the observing ingress (``squarespace_webhook`` or
    ``squarespace_reconciliation``); it is a diagnostic on the row and is
    deliberately NOT part of any event id, so the two ingresses dedupe against
    each other.

    Raises :class:`UnsupportedSquarespaceEvent` when the order carries nothing
    to record (a ``testmode`` order), and ``ValueError`` when it is malformed.
    """
    if not isinstance(order, dict):
        raise ValueError("Squarespace order must be an object")
    order_id = squarespace_order_id(order)
    if not order_id:
        raise ValueError("Squarespace order is missing an id")
    if is_squarespace_testmode(order):
        # A `testmode` order was never paid for. Recording it would fabricate
        # GMV, and its deterministic key would then be occupied for good.
        raise UnsupportedSquarespaceEvent(
            f"testmode: Squarespace order {order_id} is a test-mode order"
        )

    order_ref = build_order_ref(PLATFORM, order_id)
    if not order_ref:
        raise ValueError("Squarespace order has no usable order reference")
    currency = squarespace_order_currency(order)
    grand_total_cents = _amount_cents(order.get("grandTotal"), currency)
    fulfillment_status = (_text(order.get("fulfillmentStatus")) or "").upper()
    created_at = _occurred_at(order.get("createdOn"))
    modified_at = _occurred_at(order.get("modifiedOn"), order.get("createdOn"))
    # No buyer identity is carried: the Orders API's only buyer field is
    # `customerEmail`, which is PII the ledger must never hold, and there is no
    # customer id on the order. `externalOrderReference` is merchant/extension
    # free text and is deliberately NOT read as a Pivota order marker — a
    # forgeable string must not be able to merge this order into an interaction
    # it does not own (same reasoning as the BigCommerce adapter).
    metadata: Dict[str, Any] = {
        "native_topic": _text(topic),
        "native_fulfillment_status": _text(order.get("fulfillmentStatus")),
        "native_order_number": _text(order.get("orderNumber")),
        "native_line_items": _line_items(order) or None,
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}
    resolved_trace_id = _text(trace_id) or _entity_event_id(store_id, "observation", order_id)

    def _event(
        event_type: str,
        *,
        entity_id: str,
        occurred_at: datetime,
        amount_cents: Optional[int],
        refund_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> MerchantCommerceEvent:
        return MerchantCommerceEvent(
            event_id=_entity_event_id(store_id, event_type, entity_id),
            event_type=event_type,
            occurred_at=occurred_at,
            platform=PLATFORM,
            source=source,
            store_id=store_id,
            order_id=order_id,
            order_ref=order_ref,
            refund_id=refund_id,
            trace_id=resolved_trace_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata={**metadata, **(extra_metadata or {})},
        )

    events: List[MerchantCommerceEvent] = [
        _event(
            "order.created",
            entity_id=order_id,
            occurred_at=created_at,
            amount_cents=grand_total_cents,
        )
    ]

    # `order.paid` from the order's EXISTENCE. There is no payment status to
    # read, and a Squarespace order is created by a successful checkout.
    # Guarded on a positive amount and a currency: a money event with a zero or
    # absent amount would take this order's deterministic key and permanently
    # shadow the real figure on the next observation (first-write-wins).
    if currency and grand_total_cents:
        events.append(
            _event(
                "order.paid",
                entity_id=order_id,
                occurred_at=created_at,
                amount_cents=grand_total_cents,
                extra_metadata={"native_amount_semantics": "order_grand_total"},
            )
        )

    if fulfillment_status == CANCELLED_FULFILLMENT_STATUS:
        events.append(
            _event(
                "order.cancelled",
                entity_id=f"{order_id}:cancelled",
                occurred_at=modified_at,
                amount_cents=grand_total_cents,
            )
        )

    refund_event = _refund_event(
        order,
        order_id=order_id,
        order_ref=order_ref,
        currency=currency,
        previously_recorded_refund_cents=previously_recorded_refund_cents,
        build=_event,
    )
    if refund_event is not None:
        events.append(refund_event)
    return MerchantEventBatch(events=events)


def _refund_event(
    order: Dict[str, Any],
    *,
    order_id: str,
    order_ref: str,
    currency: Optional[str],
    previously_recorded_refund_cents: Optional[int],
    build,
) -> Optional[MerchantCommerceEvent]:
    """One ``refund.succeeded`` carrying the NEW money in a cumulative total.

    ``refundedTotal`` is cumulative across every refund of the order and there
    is no per-refund record anywhere in the Orders API, so the delta against
    what Pivota already recorded is the only per-observation refund amount that
    exists. The key is ``<order id>:<cumulative cents>``, which is
    deterministic: a re-observation of the SAME cumulative total lands on the
    same key and dedupes on the ledger's first-write-wins.

    That key protects against a repeat of the same total and nothing more. Two
    DIFFERENT totals observed concurrently produce two different keys, and the
    funnel SUMS them — which is why the callers hold
    ``order_money_read_modify_write_lock`` across the read and this write
    rather than treating it as an optimisation.

    Returns None when there is no new money (nothing refunded, a
    re-observation, or a downward correction): emitting a zero-amount row would
    occupy the key for good and shadow the real refund.
    """
    cumulative_cents = _amount_cents(order.get("refundedTotal"), currency)
    if cumulative_cents is None or cumulative_cents <= 0:
        return None
    if not currency:
        # An amount with no currency is not a refund the funnel can count.
        raise ValueError(
            f"Squarespace order {order_id} reports a refunded total with no currency"
        )
    previously = int(previously_recorded_refund_cents or 0)
    if previously < 0:
        raise ValueError("previously_recorded_refund_cents must not be negative")
    delta = cumulative_cents - previously
    if delta <= 0:
        return None
    refund_key = f"{order_id}:{cumulative_cents}"
    return build(
        "refund.succeeded",
        entity_id=refund_key,
        # The Orders API carries no per-refund timestamp; the order's own
        # modification time is the closest anchor there is.
        occurred_at=_occurred_at(order.get("modifiedOn"), order.get("createdOn")),
        amount_cents=delta,
        refund_id=refund_key,
        extra_metadata={
            "native_cumulative_refund_total": _text(
                _money(order.get("refundedTotal")).get("value")
            ),
            # amount_cents is the DELTA of that cumulative total, not the total.
            "native_amount_semantics": "cumulative_refund_total_delta",
        },
    )
