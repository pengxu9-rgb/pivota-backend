"""Pure mapper: a verified Wix eCom webhook event -> canonical events.

Input is the object ``services/wix_webhook_auth.verify_wix_webhook_jwt``
returns — the decoded ``data`` claim — whose own ``data`` is a JSON string
holding the Wix *domain event*::

    {"id": ..., "entityFqdn": "wix.ecom.v1.order", "slug": "created",
     "entityId": "<order guid>", "createdEvent": {"entity": {...order...}},
     "eventTime": "2023-12-05T10:48:58.278491Z"}

Everything below was verified against the Wix docs on 2026-09-04.

Nesting differs per event, and there is no shortcut — each was read:

* ``order`` / ``created``   -> ``createdEvent.entity``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/order-created.md
* ``order`` / ``updated``   -> ``updatedEvent.currentEntity``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/order-updated.md
* ``order`` / ``approved``  -> ``actionEvent.body.order``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/order-approved.md
* ``order`` / ``canceled``  -> ``actionEvent.body.order``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/order-canceled.md
* ``order`` / ``payment_status_updated`` -> ``actionEvent.body.order``
  (plus ``actionEvent.body.previousPaymentStatus``)
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/payment-status-updated.md
* ``order_transactions`` / ``refund_completed`` ->
  ``actionEvent.body`` = ``{orderId, refund, sideEffects, orderTransactions}``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/order-transactions/order-transactions-refund-completed.md
* ``order_transactions`` / ``details_updated`` ->
  ``actionEvent.body`` = ``{orderTransactions, paymentIds, refundIds}``
  https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/order-transactions/order-transactions-details-updated.md

Field facts: ``id`` is the order GUID and ``number`` is the human order number
(``id`` is the native id here); ``priceSummary.total.amount`` is a decimal
string and ``currency`` is ISO-4217; ``paymentStatus`` and ``status`` are the
enums pinned below; ``createdDate``/``updatedDate``/``eventTime`` are ISO 8601 —
https://dev.wix.com/docs/api-reference/business-solutions/e-commerce/orders/orders/order-object.md

UNVERIFIED, and handled defensively for that reason: the docs quote the
``eventType`` STRING only for order events (e.g. ``wix.ecom.v1.order_created``),
never for the two Order Transactions events, whose reference pages give a
description but no ``entityFqdn``/``slug`` literal. Dispatch is therefore keyed
on the domain event's own ``entityFqdn`` + ``slug`` — both documented, both
inside the signed body — with ``eventType`` only as a fallback, and the
transactions FQDN is matched on its trailing segment so a
``wix.ecom.v1.order_transactions`` vs ``wix.ecom.v2...`` spelling cannot
silently drop refunds.

**The order entity is NOT in a transactions delivery.** Its body carries
``orderId`` and amounts but no ``currency`` (Wix ``Price`` is
``{amount, formattedAmount}``), and the funnel drops any money row without a
currency. Those two events therefore need the order read back;
``services/wix_order_fetch.py`` does that and the result arrives here as
``order=``. Order-domain events need no fetch at all.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from services.commerce_order_ref import build_order_ref, pivota_order_ref
from services.merchant_event_ingest_service import MerchantCommerceEvent, MerchantEventBatch


PLATFORM = "wix"

ORDER_FQDN = "wix.ecom.v1.order"
ORDER_TRANSACTIONS_FQDN = "wix.ecom.v1.order_transactions"

# (entity, slug) pairs this bridge maps. `entity` is the trailing segment of
# the domain event's entityFqdn.
ORDER_SLUGS = frozenset({"created", "updated", "approved", "canceled", "payment_status_updated"})
TRANSACTION_SLUGS = frozenset({"refund_completed", "details_updated"})

# `paymentStatus` values that mean money was actually taken.
# PARTIALLY_REFUNDED / FULLY_REFUNDED are in because a refund presupposes a
# capture — you cannot refund what was never paid — and the refund magnitude
# rides on its own `refund.succeeded` events, so counting the order as paid
# does not double-count anything.
# PARTIALLY_PAID is deliberately OUT: part of the balance is still owed, the
# order total would overstate what was captured, and Wix reports the captured
# portion only through Order Transactions.
# PENDING / PENDING_MERCHANT / NOT_PAID / CANCELED / DECLINED / UNSPECIFIED are
# out for the obvious reason.
PAID_PAYMENT_STATUSES = frozenset({"PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"})
DECLINED_PAYMENT_STATUSES = frozenset({"DECLINED"})
CANCELED_ORDER_STATUS = "CANCELED"

# OrderTransactions payment statuses. DECLINED is the only one that is a
# failed charge; CANCELED is a charge that never started and VOIDED released a
# hold, so neither is `payment.failed`.
DECLINED_TRANSACTION_STATUSES = frozenset({"DECLINED"})
SUCCEEDED_REFUND_STATUS = "SUCCEEDED"

# The sales-channel type Pivota's own order writeback stamps
# (adapters/wix_adapter.py::build_wix_order_payload) alongside
# `channelInfo.externalOrderId = <pivota order id>`.
PIVOTA_CHANNEL_TYPE = "OTHER_PLATFORM"

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


class UnsupportedWixEvent(ValueError):
    pass


class NoWixCanonicalEvents(UnsupportedWixEvent):
    """A supported event that says nothing canonical — ignore it, don't fail.

    ``details_updated`` fires for every ordinary payment update, and a refund
    that is still ``PENDING`` has moved no money. ``MerchantEventBatch``
    requires at least one event, so "nothing to record" has to be raised rather
    than returned; it subclasses ``UnsupportedWixEvent`` so the receiver
    already answers it with the same 200 ``ignored``.
    """


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    normalized = str(value if value is not None else "").strip()
    return normalized or None


def _obj(value: Any) -> Dict[str, Any]:
    """A dict from a value that may be a dict or a JSON string (Wix sends both)."""
    if isinstance(value, dict):
        return value
    raw = _text(value)
    if not raw or not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _entity_of(fqdn: Any) -> str:
    """``wix.ecom.v1.order_transactions`` -> ``order_transactions``."""
    raw = str(fqdn or "").strip().lower()
    return raw.rsplit(".", 1)[-1] if raw else ""


def domain_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """The inner domain event; Wix ships it as a JSON string inside ``data``."""
    if not isinstance(event, dict):
        raise UnsupportedWixEvent("Wix event must be an object")
    return _obj(event.get("data"))


def _dispatch_key(event: Dict[str, Any], inner: Dict[str, Any]) -> Tuple[str, str]:
    """``(entity, slug)`` from the signed body's own fqdn/slug, else eventType.

    ``entityFqdn`` and ``slug`` are documented verbatim for every event this
    bridge maps; ``eventType`` is only quoted for the order ones. Preferring
    the pair is what lets the transactions events be matched at all.
    """
    entity = _entity_of(inner.get("entityFqdn"))
    slug = str(inner.get("slug") or "").strip().lower()
    if entity and slug:
        return entity, slug
    # Fallback: `wix.ecom.v1.order_created` -> the documented convention is
    # `<entityFqdn>_<slug>`, so split the known prefixes off the tail.
    event_type = str(event.get("eventType") or "").strip().lower()
    for prefix, known in ((ORDER_TRANSACTIONS_FQDN, TRANSACTION_SLUGS), (ORDER_FQDN, ORDER_SLUGS)):
        if not event_type.startswith(f"{prefix}_"):
            continue
        tail = event_type[len(prefix) + 1 :]
        if tail in known:
            return _entity_of(prefix), tail
    return entity, slug


def is_supported_wix_event(event: Dict[str, Any]) -> bool:
    try:
        inner = domain_event(event)
    except UnsupportedWixEvent:
        return False
    entity, slug = _dispatch_key(event, inner)
    if entity == "order":
        return slug in ORDER_SLUGS
    if entity == "order_transactions":
        return slug in TRANSACTION_SLUGS
    return False


def needs_wix_order_fetch(event: Dict[str, Any]) -> bool:
    """True for the transactions events, whose body carries no order entity.

    They name an ``orderId`` and amounts but no ``currency``, and the funnel
    drops a money row with no currency, so the order has to be read back.
    """
    try:
        inner = domain_event(event)
    except UnsupportedWixEvent:
        return False
    entity, slug = _dispatch_key(event, inner)
    return entity == "order_transactions" and slug in TRANSACTION_SLUGS


def wix_event_order_id(event: Dict[str, Any]) -> Optional[str]:
    """The native order GUID a delivery is about, before any mapping."""
    inner = domain_event(event)
    entity, slug = _dispatch_key(event, inner)
    if entity == "order_transactions":
        body = _obj(_obj(inner.get("actionEvent")).get("body"))
        return _text(body.get("orderId")) or _text(
            _obj(body.get("orderTransactions")).get("orderId")
        ) or _text(inner.get("entityId"))
    order = _order_entity(inner, slug)
    return _text(order.get("id")) or _text(inner.get("entityId"))


def _order_entity(inner: Dict[str, Any], slug: str) -> Dict[str, Any]:
    """The Order object, from whichever envelope this slug uses."""
    if slug == "created":
        return _obj(_obj(inner.get("createdEvent")).get("entity"))
    if slug == "updated":
        return _obj(_obj(inner.get("updatedEvent")).get("currentEntity"))
    # approved / canceled / payment_status_updated all use actionEvent.body.order
    return _obj(_obj(_obj(inner.get("actionEvent")).get("body")).get("order"))


def _occurred_at(*values: Any) -> datetime:
    """UTC instant from the first parseable ISO-8601 value."""
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


def _amount_cents(value: Any, currency: Optional[str]) -> Optional[int]:
    """Minor units. Wix reports money as decimal strings ("40.56", "5.0", "2")."""
    if isinstance(value, dict):
        value = value.get("amount")
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


def _order_ref(order: Dict[str, Any], order_id: str) -> Optional[str]:
    """``pivota:<id>`` for an order Pivota wrote back, else ``wix:<guid>``.

    Pivota's Wix writeback stamps the Pivota order id in TWO places, and only
    one of them is safe to read back:

    * ``buyerNote`` — documented as the "Buyer note left by the customer".
      It is buyer free text, so reading it would let a shopper type
      ``Pivota Order ID: <someone else's>`` at checkout and merge their order
      into another interaction. Never read. (Same hazard as BigCommerce's
      ``customer_message``.)
    * ``channelInfo.externalOrderId`` — "Reference to an order ID from an
      external system", set by whoever RECORDS the order through the API, next
      to ``channelInfo.type = OTHER_PLATFORM``. A buyer checking out on the
      storefront gets ``channelInfo.type = WEB`` and no ``externalOrderId``;
      the field is not on any buyer-facing form. That is the structured,
      non-buyer-writable marker, and it is what is read here — the same
      contract as Shopify's ``pivota_order_id`` note attribute and
      WooCommerce's order meta.

    Residual, deliberately accepted and noted in docs/WIX_TELEMETRY.md: another
    app on the SAME site could also record an external order with
    ``OTHER_PLATFORM`` and an id from its own system. That is a merchant-scoped
    confusion within one store, not a cross-merchant one, and it is the same
    exposure the Shopify and WooCommerce bridges already carry.
    """
    channel = _obj(order.get("channelInfo"))
    external_id = _text(channel.get("externalOrderId"))
    channel_type = str(channel.get("type") or "").strip().upper()
    if external_id and channel_type == PIVOTA_CHANNEL_TYPE:
        ref = pivota_order_ref(external_id)
        if ref:
            return ref
    return build_order_ref(PLATFORM, order_id)


def _usable_refunds(transactions: Dict[str, Any], body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every refund carrying an id, de-duplicated, order preserved.

    ``orderTransactions.refunds`` is the full list after the change, so a
    repeated delivery re-offers every refund and each dedupes on its own id.
    ``actionEvent.body.refund`` (refund_completed only) is unioned in first so
    the completing refund is present even if the list lags.
    """
    usable: List[Dict[str, Any]] = []
    seen: set[str] = set()
    completed = _obj(body.get("refund"))
    for entry in [completed, *(transactions.get("refunds") or [])]:
        if not isinstance(entry, dict):
            continue
        refund_id = _text(entry.get("id"))
        if not refund_id or refund_id in seen:
            continue
        seen.add(refund_id)
        usable.append(entry)
    return usable


def _refunded_amount(refund: Dict[str, Any], currency: Optional[str]) -> Optional[int]:
    """What this refund actually moved back, or None when nothing succeeded.

    ``summary.refunded`` is documented as "the portion of `requestedRefund`
    that refunded successfully" and is preferred. Without it, only the
    ``SUCCEEDED`` refund transactions are summed — ``requestedRefund`` and
    ``PENDING``/``FAILED`` transactions are requests, not money movement.

    A ``summary.refunded`` of **zero** (or one that will not parse) is *not* an
    answer, it is the absence of one, and settling for it was worse than
    useless: the refund event id is keyed on the refund id alone, so a
    ``refund.succeeded`` emitted at 0 while the refund was still ``PENDING``
    would be the row the real ``refund_completed`` delivery deduped against —
    the refund lost for good, under its own id, at amount 0. Zero therefore
    falls through to the transactions sum, and a zero sum emits nothing (the
    caller's "nothing settled" path), so the later delivery is the first to
    land.
    """
    summary = _obj(refund.get("summary"))
    if "refunded" in summary:
        settled = _amount_cents(_obj(summary.get("refunded")).get("amount"), currency)
        if settled:
            return settled
    transactions = refund.get("transactions")
    if not isinstance(transactions, list):
        return None
    total = Decimal("0")
    found = False
    for entry in transactions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("refundStatus") or "").strip().upper() != SUCCEEDED_REFUND_STATUS:
            continue
        raw = _text(_obj(entry.get("amount")).get("amount"))
        if raw is None:
            continue
        try:
            total += Decimal(raw)
        except (InvalidOperation, ValueError):
            continue
        found = True
    if not found:
        return None
    # A sum of zero is nothing settled, exactly like a zero `summary.refunded`.
    return _amount_cents(total, currency) or None


def map_wix_event(
    event: Dict[str, Any],
    *,
    store_id: str,
    order: Optional[Dict[str, Any]] = None,
) -> MerchantEventBatch:
    """Canonical events for one verified Wix webhook delivery.

    ``event`` is the decoded ``data`` claim. ``order`` is the order read back
    for a transactions delivery (see ``needs_wix_order_fetch``); it is ignored
    for order-domain events, which carry their own entity.
    """
    inner = domain_event(event)
    entity, slug = _dispatch_key(event, inner)
    if entity == "order" and slug in ORDER_SLUGS:
        return _map_order_event(event, inner, slug=slug, store_id=store_id)
    if entity == "order_transactions" and slug in TRANSACTION_SLUGS:
        return _map_transactions_event(
            event, inner, slug=slug, store_id=store_id, order=order or {}
        )
    named = _text(event.get("eventType")) or "{}/{}".format(entity or "missing", slug or "missing")
    raise UnsupportedWixEvent(f"unsupported Wix webhook event: {named}")


def _common(
    order: Dict[str, Any],
    *,
    order_id: str,
    store_id: str,
    entity: str,
    slug: str,
    event_id: Optional[str],
):
    currency = (_text(order.get("currency")) or "").upper() or None
    buyer = _obj(order.get("buyerInfo"))
    # `contactId` is always present (Wix auto-creates a contact); `memberId`
    # only when the buyer was logged in. Neither is an email or a name.
    buyer_id = _text(buyer.get("contactId")) or _text(buyer.get("memberId"))
    order_ref = _order_ref(order, order_id)
    # No Wix field carries a Pivota click id: the writeback stamps none, and
    # nothing on the storefront order (`channelInfo`, `customFields`,
    # `extendedFields` — the last needs a Dev Center schema we do not have)
    # holds one today. Left None deliberately rather than guessed at.
    click_id = None
    trace_id = _text(event_id) or _entity_event_id(store_id, "delivery", order_id)

    metadata: Dict[str, Any] = {
        # The documented eventType spelling, `<entityFqdn>_<slug>`, rebuilt
        # from the pair actually dispatched on.
        "native_topic": "wix.ecom.v1.{}_{}".format(entity, slug),
        "native_status": _text(order.get("status")),
        "native_financial_status": _text(order.get("paymentStatus")),
        "native_fulfillment_status": _text(order.get("fulfillmentStatus")),
        "native_checkout_id": _text(order.get("checkoutId")),
        "webhook_delivery_id": _text(event_id),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}

    def _event(
        event_type: str,
        *,
        entity_id: str,
        occurred_at: datetime,
        amount_cents: Optional[int],
        refund_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> MerchantCommerceEvent:
        return MerchantCommerceEvent(
            event_id=_entity_event_id(store_id, event_type, entity_id),
            event_type=event_type,
            occurred_at=occurred_at,
            platform=PLATFORM,
            source="wix_webhook",
            store_id=store_id,
            buyer_id=buyer_id,
            click_id=click_id,
            payment_id=payment_id,
            order_id=order_id,
            order_ref=order_ref,
            refund_id=refund_id,
            trace_id=trace_id,
            amount_cents=amount_cents,
            currency=currency,
            metadata={**metadata, **(extra_metadata or {})},
        )

    return currency, _event


def _map_order_event(
    event: Dict[str, Any],
    inner: Dict[str, Any],
    *,
    slug: str,
    store_id: str,
) -> MerchantEventBatch:
    order = _order_entity(inner, slug)
    if not order:
        raise ValueError(f"Wix {slug} event carries no order entity")
    order_id = _text(order.get("id")) or _text(inner.get("entityId"))
    if not order_id:
        raise ValueError("Wix order is missing an id")

    currency, _event = _common(
        order,
        order_id=order_id,
        store_id=store_id,
        entity="order",
        slug=slug,
        event_id=_text(inner.get("id")),
    )
    payment_status = str(order.get("paymentStatus") or "").strip().upper()
    status = str(order.get("status") or "").strip().upper()
    total_cents = _amount_cents(_obj(_obj(order.get("priceSummary")).get("total")).get("amount"), currency)

    created_at = _occurred_at(order.get("createdDate"))
    updated_at = _occurred_at(order.get("updatedDate"), inner.get("eventTime"), order.get("createdDate"))

    events: List[MerchantCommerceEvent] = [
        # Emitted on EVERY order delivery, keyed on the order id, so an order
        # whose `created` webhook was missed still enters the ledger on its
        # next update and a repeat is a duplicate rather than a second order.
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
                occurred_at=updated_at,
                amount_cents=total_cents,
            )
        )
    if status == CANCELED_ORDER_STATUS:
        events.append(
            _event(
                "order.cancelled",
                entity_id=f"{order_id}:cancelled",
                occurred_at=updated_at,
                amount_cents=total_cents,
            )
        )
    if payment_status in DECLINED_PAYMENT_STATUSES:
        events.append(
            _event(
                "payment.failed",
                entity_id=f"{order_id}:declined",
                occurred_at=updated_at,
                amount_cents=total_cents,
            )
        )
    return MerchantEventBatch(events=events)


def _map_transactions_event(
    event: Dict[str, Any],
    inner: Dict[str, Any],
    *,
    slug: str,
    store_id: str,
    order: Dict[str, Any],
) -> MerchantEventBatch:
    body = _obj(_obj(inner.get("actionEvent")).get("body"))
    transactions = _obj(body.get("orderTransactions"))
    order_id = (
        _text(body.get("orderId"))
        or _text(transactions.get("orderId"))
        or _text(inner.get("entityId"))
        or _text(order.get("id"))
    )
    if not order_id:
        raise ValueError("Wix order transactions event is missing an order id")

    currency, _event = _common(
        order,
        order_id=order_id,
        store_id=store_id,
        entity="order_transactions",
        slug=slug,
        event_id=_text(inner.get("id")),
    )

    events: List[MerchantCommerceEvent] = []
    for payment in transactions.get("payments") or []:
        if not isinstance(payment, dict):
            continue
        if str(payment.get("status") or "").strip().upper() not in DECLINED_TRANSACTION_STATUSES:
            continue
        payment_id = _text(payment.get("id"))
        if not payment_id:
            continue
        events.append(
            _event(
                "payment.failed",
                entity_id=f"{payment_id}:declined",
                occurred_at=_occurred_at(
                    payment.get("updatedDate"), payment.get("createdDate"), inner.get("eventTime")
                ),
                amount_cents=_amount_cents(_obj(payment.get("amount")).get("amount"), currency),
                payment_id=payment_id,
            )
        )

    for refund in _usable_refunds(transactions, body):
        refund_id = _text(refund.get("id"))
        amount_cents = _refunded_amount(refund, currency)
        if amount_cents is None:
            # Requested but nothing settled yet (all PENDING/FAILED). A later
            # refund_completed delivery carries the same id and will land it.
            continue
        events.append(
            _event(
                "refund.succeeded",
                # Keyed on the NATIVE refund id, never the order id: two
                # partial refunds of one order are two distinct ledger facts
                # that must dedupe independently across repeated deliveries.
                entity_id=str(refund_id),
                occurred_at=_occurred_at(refund.get("createdDate"), inner.get("eventTime")),
                amount_cents=amount_cents,
                refund_id=str(refund_id),
                # `details.reason` is customer-supplied free text and may carry
                # PII; deliberately not copied into canonical metadata.
                extra_metadata={"native_amount_semantics": "native_refund_succeeded_total"},
            )
        )

    if not events:
        raise NoWixCanonicalEvents(
            f"Wix {slug} event carried no settled payment or refund"
        )
    return MerchantEventBatch(events=events)
