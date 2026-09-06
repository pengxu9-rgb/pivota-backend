"""Pure mapper: a Webflow Ecommerce order -> canonical ledger events.

Four facts about the Webflow Ecommerce API shape this module. Each is recorded
as VERIFIED or ASSUMED, with its consequence, in docs/WEBFLOW_TELEMETRY.md.

1. **Money is already in MINOR UNITS.** A Webflow money object is
   ``{"unit": "USD", "value": 5898, "string": "$58.98"}`` — the currency lives
   under ``unit``, not ``currency``, and ``value`` is an integer number of cents.
   There is no decimal conversion anywhere in this file, and that absence is the
   single most consequential thing about it: multiplying by 100 "for consistency
   with every other adapter in this repo" would file $58.98 as $5,898.00. So a
   ``value`` this module cannot read as a whole number of minor units is a LOUD
   ``WebflowMoneyFormatError`` rather than a skipped event — a silent skip
   under-counts, but a misread over-counts by 100x, and only one of those is
   visible in a total.

2. **Refunds are FULL-ORDER only.** ``POST /v2/sites/{id}/orders/{id}/refund``
   refunds the whole order; the status becomes ``refunded`` and ``refundedOn`` is
   set. There are no partial refunds and no per-refund records, so a refund is
   exactly one event carrying the whole ``customerPaid`` amount, once per order.
   That is why this integration needs NONE of the cumulative-delta machinery
   Shoplazza and Squarespace carry: there is no arithmetic against a baseline,
   so there is no read-modify-write, so there is no lock. If Webflow ever ships
   partial refunds this file is where that changes.

3. **A lost dispute is money leaving the merchant, and it shares the refund's
   key.** ``dispute-lost`` is emitted as ``refund.succeeded`` for the same full
   amount and — deliberately — under the SAME entity key as an ordinary refund,
   ``<orderId>:refund``. The two statuses are mutually exclusive on one order at
   any instant, but an order can MOVE between them across observations, and two
   keys would then record the same money twice. One key makes at most one refund
   row per order structurally true. ``disputed`` (funds merely held) emits no
   money event at all.

4. **The webhook delivery cannot be trusted for money.** The receiver fetches
   the order and only ever hands this module a fetched order object, which is
   the same object the sweep lists. So every event id here derives from the
   ORDER id and the event type, never from a delivery id: a webhook observation
   and a later sweep observation of one order must collapse onto one ledger row.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.commerce_order_ref import build_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


PLATFORM = "webflow"

# The webhook trigger types this bridge subscribes and maps. Both are answered
# by re-reading the order, so they map identically; the trigger is metadata.
WEBFLOW_ORDER_TRIGGERS = ("ecomm_new_order", "ecomm_order_changed")
SUPPORTED_WEBFLOW_TRIGGERS = WEBFLOW_ORDER_TRIGGERS

_TRIGGER_BY_NORMALIZED = {t.lower(): t for t in SUPPORTED_WEBFLOW_TRIGGERS}

# `status` is the only lifecycle enum on a Webflow order.
STATUS_PENDING = "pending"
STATUS_UNFULFILLED = "unfulfilled"
STATUS_FULFILLED = "fulfilled"
STATUS_DISPUTED = "disputed"
STATUS_DISPUTE_LOST = "dispute-lost"
STATUS_REFUNDED = "refunded"
WEBFLOW_ORDER_STATUSES = (
    STATUS_PENDING,
    STATUS_UNFULFILLED,
    STATUS_FULFILLED,
    STATUS_DISPUTED,
    STATUS_DISPUTE_LOST,
    STATUS_REFUNDED,
)

# A `pending` order is one whose payment has not been accepted yet (the
# documented case is a PayPal payment awaiting capture). It is NOT paid, so it
# gets `order.created` and no money event; a later `ecomm_order_changed`, or the
# sweep, completes it once the status moves on. Every other status implies the
# payment was accepted.
_PAID_STATUSES = frozenset(
    {
        STATUS_UNFULFILLED,
        STATUS_FULFILLED,
        STATUS_DISPUTED,
        STATUS_DISPUTE_LOST,
        STATUS_REFUNDED,
    }
)
# The statuses in which money has left the merchant.
_REFUND_STATUSES = frozenset({STATUS_REFUNDED, STATUS_DISPUTE_LOST})

# Webflow is not known to mark test orders at all (there is no test mode in
# Webflow Ecommerce; a sandbox site is a separate site with its own site id).
# These are checked anyway, and cheaply: if such a flag does exist, or is added,
# an unpaid test order in `paid_amount_cents_by_currency` is fabricated GMV
# under a deterministic key that can then never be reused for the real one.
_TEST_ORDER_FLAGS = ("isTest", "isTestOrder", "testMode", "testmode")

_MAX_LINE_ITEMS = 50

# A whole number, optionally signed, and nothing else. `"58.98"` must NOT match:
# reading it as 58 cents (or as 5898) is the 100x error this module exists to
# refuse rather than guess at.
_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


class UnsupportedWebflowEvent(ValueError):
    """This observation carries nothing the ledger should record. 2xx, not 5xx."""


class WebflowMoneyFormatError(ValueError):
    """A Webflow money `value` was not a whole number of minor units.

    Its own type because it is the one malformed field that must never be
    tolerated. Webflow states amounts in cents; anything else means either the
    API shape changed under us or the object is not a Webflow money object, and
    either way a guess is a 100x error in the direction that inflates GMV.
    """


def normalize_webflow_trigger(value: Any) -> Optional[str]:
    """The canonical spelling of a supported trigger type, else None."""
    return _TRIGGER_BY_NORMALIZED.get(str(value or "").strip().lower())


def is_supported_webflow_trigger(value: Any) -> bool:
    return normalize_webflow_trigger(value) is not None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value if value is not None else "").strip()
    return normalized or None


def _money(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _currency_of(*candidates: Any) -> Optional[str]:
    """The ISO code from a Webflow money object's `unit`.

    `currency` is accepted as a second spelling purely as a hedge against a
    future/legacy envelope; `unit` is the documented field. Anything that is not
    a three-letter code is None rather than a guess — the ledger column is
    exactly three characters and a bad value would fail the whole batch.
    """
    for candidate in candidates:
        money = _money(candidate)
        for key in ("unit", "currency"):
            code = _text(money.get(key))
            if code and len(code) == 3 and code.isalpha():
                return code.upper()
    return None


def _amount_minor_units(money: Any, *, field: str) -> Optional[int]:
    """Minor units straight out of a Webflow money object. NO conversion.

    Returns None when the field is absent (nothing to record), and raises
    :class:`WebflowMoneyFormatError` when it is present but is not a whole
    number of minor units — including a decimal string like ``"58.98"``, which
    is precisely the shape that would silently become either 58 or 5898.
    """
    raw = _money(money).get("value")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise WebflowMoneyFormatError(f"Webflow {field}.value is a boolean")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        if not float(raw).is_integer():
            raise WebflowMoneyFormatError(
                f"Webflow {field}.value {raw!r} is not a whole number of minor units"
            )
        value = int(raw)
    elif isinstance(raw, str):
        candidate = raw.strip()
        if not _INTEGER_TEXT.match(candidate):
            raise WebflowMoneyFormatError(
                f"Webflow {field}.value {raw!r} is not a whole number of minor units"
            )
        value = int(candidate)
    else:
        raise WebflowMoneyFormatError(
            f"Webflow {field}.value has unreadable type {type(raw).__name__}"
        )
    if value < 0:
        # A negative paid amount is not a smaller amount, it is a malformed
        # claim. Clamping it to 0 would take the order's deterministic money key
        # for good and shadow the real figure.
        raise WebflowMoneyFormatError(
            f"Webflow {field}.value is negative ({value})"
        )
    return value


def _occurred_at(*values: Any) -> datetime:
    """UTC instant from the first parseable ISO-8601 value.

    Webflow timestamps are ISO 8601 with a `Z` suffix. Falling through to "now"
    is the last resort and only reachable for a `pending` order, which has no
    `acceptedOn` yet; the event id is still deterministic, so the first
    observation's timestamp is the one that persists.
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

    NOT from the webhook delivery. The sweep sees the same order with no
    delivery at all, and both observations must land on one ledger row.
    """
    material = json.dumps(
        [PLATFORM, store_id, event_type, entity_id],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{PLATFORM}:{event_type}:{digest}"


def webflow_order_id(order: Any) -> Optional[str]:
    """The native order id, or None. Never raises: callers use it to decide."""
    if not isinstance(order, dict):
        return None
    return _text(order.get("orderId")) or _text(order.get("id"))


def webflow_order_ref(order: Any) -> Optional[str]:
    order_id = webflow_order_id(order)
    return build_order_ref(PLATFORM, order_id) if order_id else None


def webflow_order_status(order: Any) -> str:
    if not isinstance(order, dict):
        return ""
    return (_text(order.get("status")) or "").lower()


def is_webflow_test_order(order: Any) -> bool:
    """Defensive: Webflow is not known to flag test orders (see module docstring)."""
    if not isinstance(order, dict):
        return False
    metadata = order.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for flag in _TEST_ORDER_FLAGS:
        if bool(order.get(flag)) or bool(metadata.get(flag)):
            return True
    return False


def _line_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Join keys and amounts only.

    `productName` is merchant copy rather than a join key, and the ledger's
    line-item allowlist would reject it anyway; dropping it here makes that
    rejection impossible to trip rather than merely unlikely.
    """
    raw = order.get("purchasedItems")
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in raw[:_MAX_LINE_ITEMS]:
        if not isinstance(entry, dict):
            continue
        item: Dict[str, Any] = {}
        for source_key, target_key in (
            ("productId", "product_id"),
            ("variantId", "variant_id"),
            ("variantSKU", "sku"),
        ):
            value = _text(entry.get(source_key))
            if value:
                item[target_key] = value
        count = entry.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            item["quantity"] = count
        # `rowTotal` is deliberately NOT carried. The ledger's line-item
        # vocabulary spells amounts as `price`/`subtotal`/`total`, and every
        # other adapter writes a DECIMAL string there. Webflow's are minor-unit
        # integers, so putting one under those names would plant exactly the
        # 100x ambiguity this module refuses everywhere else — for a field that
        # is a diagnostic, while the order's real money is `customerPaid`.
        if item:
            items.append(item)
    return items


def map_webflow_order(
    order: Dict[str, Any],
    *,
    store_id: str,
    source: str,
    trigger_type: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> MerchantEventBatch:
    """Canonical events for ONE fetched Webflow order.

    ``source`` names the observing ingress (``webflow_webhook`` or
    ``webflow_reconciliation``). It is a diagnostic on the row and is
    deliberately NOT part of any event id, so the two ingresses dedupe against
    each other.

    Raises :class:`UnsupportedWebflowEvent` when there is nothing to record, and
    ``ValueError`` (including :class:`WebflowMoneyFormatError`) when the order is
    malformed.
    """
    if not isinstance(order, dict):
        raise ValueError("Webflow order must be an object")
    order_id = webflow_order_id(order)
    if not order_id:
        raise ValueError("Webflow order is missing an id")
    if is_webflow_test_order(order):
        raise UnsupportedWebflowEvent(
            f"test_order: Webflow order {order_id} is flagged as a test order"
        )

    order_ref = build_order_ref(PLATFORM, order_id)
    if not order_ref:
        raise ValueError("Webflow order has no usable order reference")

    status = webflow_order_status(order)
    currency = _currency_of(order.get("customerPaid"), order.get("netAmount"))
    paid_minor = _amount_minor_units(order.get("customerPaid"), field="customerPaid")
    accepted_at = _occurred_at(order.get("acceptedOn"))
    stripe = order.get("stripeDetails")
    stripe = stripe if isinstance(stripe, dict) else {}

    # No buyer identity is carried. `customerInfo` holds `fullName` and `email`,
    # both PII the ledger must never hold, and there is no customer id on the
    # order other than `stripeDetails.customerId`, which is a PSP identity for a
    # natural person. `customData` is buyer-entered free text and is not read as
    # a Pivota order marker: a forgeable string must not be able to merge this
    # order into an interaction it does not own (the BigCommerce reasoning).
    metadata: Dict[str, Any] = {
        # `native_topic` is the ledger's vocabulary for "what the platform
        # called this notification"; for Webflow that is the trigger type.
        "native_topic": _text(trigger_type),
        "native_status": _text(order.get("status")),
        "native_payment_gateway": _text(order.get("paymentProcessor")),
        "native_line_items": _line_items(order) or None,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}
    resolved_trace_id = _text(trace_id) or _entity_event_id(
        store_id, "observation", order_id
    )

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
            occurred_at=accepted_at,
            amount_cents=paid_minor,
        )
    ]

    # `order.paid` from the STATUS. A Webflow order exists as soon as a checkout
    # is submitted, so its existence is not proof of payment the way a
    # Squarespace order's is; `pending` is the documented unpaid state. Guarded
    # additionally on a positive amount and a currency: a money event with a
    # zero or absent amount would take this order's deterministic key and
    # permanently shadow the real figure on the next observation.
    if status in _PAID_STATUSES and currency and paid_minor:
        events.append(
            _event(
                "order.paid",
                entity_id=order_id,
                occurred_at=accepted_at,
                amount_cents=paid_minor,
                extra_metadata={"native_amount_semantics": "customer_paid"},
            )
        )

    # Webflow has NO cancelled state. `order.cancelled` is therefore never
    # emitted — inventing one from `refunded` would file a refund as a
    # cancellation and count the same order twice in two different funnels.

    if status in _REFUND_STATUSES:
        events.append(
            _refund_event(
                order,
                order_id=order_id,
                status=status,
                currency=currency,
                paid_minor=paid_minor,
                stripe=stripe,
                build=_event,
            )
        )
    return MerchantEventBatch(events=events)


def _refund_event(
    order: Dict[str, Any],
    *,
    order_id: str,
    status: str,
    currency: Optional[str],
    paid_minor: Optional[int],
    stripe: Dict[str, Any],
    build,
) -> MerchantCommerceEvent:
    """The one ``refund.succeeded`` a Webflow order can ever have.

    Webflow refunds are full-order only, so the amount is the whole
    ``customerPaid`` and there is exactly one of them. ``dispute-lost`` is
    mapped here too, under the SAME key: the money left the merchant either way,
    the amount is identical, and an order that moves between the two statuses
    must not produce two rows.

    The PSP's own refund id is METADATA, never the key. It is present for a
    Stripe order and absent for a PayPal one, and an order first observed
    without it and later with it would land on two different keys — two refund
    rows for one refund, which the funnel sums.
    """
    if paid_minor is None or paid_minor <= 0 or not currency:
        # A refunded order whose money cannot be read is not a "no event"
        # outcome: it is a refund this bridge would silently omit. Loud.
        raise ValueError(
            f"Webflow order {order_id} is {status} but carries no readable "
            "customerPaid amount and currency"
        )
    dispute_lost = status == STATUS_DISPUTE_LOST
    return build(
        "refund.succeeded",
        entity_id=f"{order_id}:refund",
        occurred_at=_occurred_at(
            order.get("disputeUpdatedOn") if dispute_lost else None,
            order.get("disputedOn") if dispute_lost else None,
            order.get("refundedOn"),
            order.get("acceptedOn"),
        ),
        amount_cents=paid_minor,
        refund_id=f"{order_id}:refund",
        extra_metadata={
            k: v
            for k, v in {
                "native_amount_semantics": (
                    "dispute_lost_full_amount" if dispute_lost else "full_order_refund"
                ),
                "native_psp_refund_id": _text(stripe.get("refundId")),
                # `refundReason` is deliberately NOT carried: it is free text a
                # merchant types, and the ledger's metadata vocabulary is an
                # allowlist of native identifiers rather than a document store.
                "native_psp_dispute_id": _text(stripe.get("disputeId")),
            }.items()
            if v
        },
    )
