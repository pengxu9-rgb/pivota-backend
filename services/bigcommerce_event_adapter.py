"""Pure mapper: a BigCommerce order (plus its refunds) -> canonical events.

BigCommerce differs from Shopify/WooCommerce/SHOPLINE in one way that shapes
this module: **the webhook delivery carries no order fields**. A delivery is

    {"scope": "store/order/created", "store_id": "1025646",
     "data": {"type": "order", "id": 250},
     "hash": "3f9ea4...", "producer": "stores/{store_hash}"}

so the receiver must fetch `GET /v2/orders/{id}` (and, for refunds,
`GET /v3/orders/{id}/payment_actions/refunds`) before anything can be mapped.
That fetch lives in services/bigcommerce_order_fetch.py; this module stays
pure so it can be exercised against documented fixtures with no network.

Verified against the BigCommerce docs on 2026-09-04:

* delivery envelope and the header-credential auth model —
  https://docs.bigcommerce.com/docs/integrations/webhooks
* order scopes incl. ``store/order/refund/created`` —
  https://docs.bigcommerce.com/docs/integrations/webhooks/events
* ``status_id`` -> status names (5=Cancelled, 6=Declined, 4=Refunded,
  14=Partially Refunded) —
  https://docs.bigcommerce.com/docs/rest-management/orders/order-status
* ``payment_status`` enum, RFC-2822 order dates, and the fact that v2's
  ``refunded_amount`` **always returns 0** —
  https://docs.bigcommerce.com/api-reference/store-management/orders/orders/getanorder
* refund object fields (``id``, ``order_id``, ``total_amount``, ``created``,
  ``items[]``) inside a ``{"data": [...], "meta": {...}}`` wrapper —
  https://docs.bigcommerce.com/api-reference/store-management/order-transactions/payment-actions/getorderrefunds
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


PLATFORM = "bigcommerce"

# The scopes this bridge subscribes and maps. `store/order/refund/created` is a
# real documented scope, so refunds do NOT have to be inferred from an
# `order/updated` — but they are still re-read on the order scopes whose
# `payment_status` says money went back, because a refund created before the
# subscription existed would otherwise never reach the ledger.
BIGCOMMERCE_REFUND_SCOPE = "store/order/refund/created"
SUPPORTED_BIGCOMMERCE_SCOPES = (
    "store/order/created",
    "store/order/updated",
    "store/order/statusUpdated",
    BIGCOMMERCE_REFUND_SCOPE,
)
_SCOPE_BY_NORMALIZED = {scope.lower(): scope for scope in SUPPORTED_BIGCOMMERCE_SCOPES}

# `payment_status` values that mean the money was actually taken. `authorized`
# and `capture pending` are deliberately NOT here: an authorization is a hold,
# not a payment, and emitting `order.paid` for one would overstate GMV.
# `partially refunded` / `refunded` stay in because a refund presupposes a
# capture, and the refund magnitude is carried by its own events.
PAID_PAYMENT_STATUSES = frozenset({"captured", "paid", "partially refunded", "refunded"})
# The order-level tell that a refund exists. v2's `refunded_amount` is
# documented as "Always returns 0", so it can never be that tell.
REFUNDED_PAYMENT_STATUSES = frozenset({"refunded", "partially refunded"})
DECLINED_PAYMENT_STATUSES = frozenset({"declined"})

CANCELLED_STATUS_ID = 5
DECLINED_STATUS_ID = 6

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


class UnsupportedBigCommerceEvent(ValueError):
    pass


def normalize_bigcommerce_scope(value: Any) -> Optional[str]:
    """The canonical spelling of a supported scope, else None."""
    return _SCOPE_BY_NORMALIZED.get(str(value or "").strip().lower())


def is_supported_bigcommerce_scope(value: Any) -> bool:
    return normalize_bigcommerce_scope(value) is not None


def refunds_are_relevant(scope: Any, order: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the refund list has to be fetched for this delivery.

    True on the refund scope itself, and on any order scope whose
    `payment_status` says money went back.
    """
    if normalize_bigcommerce_scope(scope) == BIGCOMMERCE_REFUND_SCOPE:
        return True
    payment_status = str((order or {}).get("payment_status") or "").strip().lower()
    return payment_status in REFUNDED_PAYMENT_STATUSES


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value if value is not None else "").strip()
    return normalized or None


def _native_id(value: Any) -> Optional[str]:
    """BigCommerce reports ids as integers and `customer_id` as a double.

    ``8.0`` and ``8`` are the same customer; normalizing here keeps the event
    id stable across the two spellings.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    raw = _text(value)
    if raw is None:
        return None
    try:
        as_decimal = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw
    if as_decimal == as_decimal.to_integral_value():
        return str(int(as_decimal))
    return raw


def _occurred_at(*values: Any) -> datetime:
    """UTC instant from the first parseable value.

    Order dates are RFC-2822 ("Tue, 05 Mar 2019 21:40:11 +0000"); refund
    `created` is ISO 8601. Both spellings are accepted for either field so a
    fixture from one API version cannot silently fall through to "now".
    """
    for value in values:
        raw = _text(value)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                continue
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _amount_cents(value: Any, currency: Optional[str]) -> Optional[int]:
    """Minor units. BigCommerce reports money as decimal strings ("49.99")."""
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


def _entity_event_id(store_id: str, event_type: str, entity_id: str) -> str:
    material = json.dumps(
        [PLATFORM, store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{PLATFORM}:{event_type}:{digest}"


def _status_id(order: Dict[str, Any]) -> Optional[int]:
    raw = order.get("status_id")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _usable_refunds(refunds: Any) -> List[Dict[str, Any]]:
    """Every refund entry carrying a native id, de-duplicated, order preserved.

    The id is the only thing that makes a refund idempotent across the repeated
    `order/updated` deliveries that re-read the whole list, so an entry without
    one is skipped. A malformed sibling never suppresses a valid entry.
    """
    usable: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in refunds or []:
        if not isinstance(entry, dict):
            continue
        refund_id = _native_id(entry.get("id"))
        if not refund_id or refund_id in seen:
            continue
        seen.add(refund_id)
        usable.append(entry)
    return usable


def map_bigcommerce_order(
    order: Dict[str, Any],
    refunds: Optional[List[Dict[str, Any]]] = None,
    *,
    scope: str,
    delivery_hash: Optional[str],
    store_id: str,
) -> MerchantEventBatch:
    """Canonical events for one fetched BigCommerce order.

    ``order`` is the `GET /v2/orders/{id}` body; ``refunds`` is the ``data``
    array of `GET /v3/orders/{id}/payment_actions/refunds` (empty when the
    delivery did not warrant that second call).
    """
    canonical_scope = normalize_bigcommerce_scope(scope)
    if canonical_scope is None:
        raise UnsupportedBigCommerceEvent(
            f"unsupported BigCommerce webhook scope: {str(scope or '').strip() or 'missing'}"
        )
    if not isinstance(order, dict):
        raise ValueError("BigCommerce order must be an object")
    order_id = _native_id(order.get("id"))
    if not order_id:
        raise ValueError("BigCommerce order is missing an id")

    currency = (_text(order.get("currency_code")) or "").upper() or None
    payment_status = str(order.get("payment_status") or "").strip().lower()
    status_id = _status_id(order)
    payment_id = _native_id(order.get("payment_provider_id"))
    buyer_id = _native_id(order.get("customer_id"))
    if buyer_id == "0":
        buyer_id = None
    cart_id = _text(order.get("cart_id"))
    # BigCommerce's order writeback (routes/order_routes.py::create_bigcommerce_order)
    # records the Pivota order id only in `customer_message` / `staff_notes`,
    # both free text — `customer_message` is filled in by the BUYER at
    # checkout. Reading either would let a shopper forge a `pivota:` claim and
    # merge their order into someone else's interaction, so every BigCommerce
    # order is treated as platform-originated. Recovering the Pivota identity
    # needs a structured marker (a v3 order metafield) stamped at writeback.
    order_ref = build_order_ref(PLATFORM, order_id)
    # No BigCommerce equivalent of `extract_click_id_from_wc_order` exists:
    # the v2 order carries no note-attribute / meta channel a Pivota click id
    # could ride in, and the writeback stamps none. Left None deliberately.
    click_id = None
    trace_id = _text(delivery_hash) or _entity_event_id(store_id, "delivery", order_id)

    metadata: Dict[str, Any] = {
        "native_topic": canonical_scope,
        "native_status": _text(order.get("status")),
        "native_payment_method": _text(order.get("payment_method")),
        "native_total_tax": order.get("total_tax"),
        "webhook_delivery_id": _text(delivery_hash),
    }
    # `native_line_items` is NOT populated: the v2 order body does not embed
    # its products, it exposes them as a `products` sub-resource URL, and a
    # third HTTP call per delivery is not worth a metadata field.
    metadata = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}

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
            source="bigcommerce_webhook",
            store_id=store_id,
            buyer_id=buyer_id,
            click_id=click_id,
            cart_id=cart_id,
            payment_id=payment_id,
            order_id=order_id,
            order_ref=order_ref,
            refund_id=refund_id,
            trace_id=trace_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata={**metadata, **(extra_metadata or {})},
        )

    created_at = _occurred_at(order.get("date_created"))
    modified_at = _occurred_at(order.get("date_modified"), order.get("date_created"))
    total_cents = _amount_cents(order.get("total_inc_tax"), currency)

    events: List[MerchantCommerceEvent] = [
        _event(
            "order.created",
            entity_id=order_id,
            occurred_at=created_at,
            amount_cents=total_cents,
        )
    ]
    if payment_status in PAID_PAYMENT_STATUSES:
        events.append(
            _event(
                "order.paid",
                entity_id=order_id,
                occurred_at=modified_at,
                amount_cents=total_cents,
            )
        )
    if status_id == CANCELLED_STATUS_ID:
        events.append(
            _event(
                "order.cancelled",
                entity_id=f"{order_id}:cancelled",
                occurred_at=modified_at,
                amount_cents=total_cents,
            )
        )
    if status_id == DECLINED_STATUS_ID or payment_status in DECLINED_PAYMENT_STATUSES:
        events.append(
            _event(
                "payment.failed",
                entity_id=f"{order_id}:declined",
                occurred_at=modified_at,
                amount_cents=total_cents,
            )
        )

    for refund in _usable_refunds(refunds):
        refund_id = _native_id(refund.get("id"))
        events.append(
            _event(
                "refund.succeeded",
                # Keyed on the NATIVE refund id, never the order id: two
                # partial refunds of one order are two distinct ledger facts.
                entity_id=str(refund_id),
                occurred_at=_occurred_at(refund.get("created"), order.get("date_modified")),
                amount_cents=_amount_cents(refund.get("total_amount"), currency),
                refund_id=str(refund_id),
                # `refund.reason` is merchant free text and may carry buyer
                # PII; it is deliberately not copied into canonical metadata.
                extra_metadata={"native_amount_semantics": "native_refund_total"},
            )
        )
    return MerchantEventBatch(events=events)
