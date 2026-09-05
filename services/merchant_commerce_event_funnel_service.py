from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import or_, select

from db.commerce_interactions import commerce_interaction_events
from db.database import database
from services.traffic_taxonomy_service import taxonomy_from_row
from services.commerce_order_ref import PIVOTA_ORDER_REF_NAMESPACE


logger = logging.getLogger("merchant_commerce_event_funnel_service")
OPS_CANARY_SURFACE = "ops_canary"


def _event_limit() -> int:
    raw = str(os.getenv("COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT") or "50000").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 50000
    return max(100, min(value, 200000))


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    try:
        return dict(row)
    except Exception:
        return {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _analytics_row(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    # Canonical columns are authoritative when present. Payload retains the
    # wider checkout/order/refund references that predate dedicated columns.
    return {
        **payload,
        **{key: value for key, value in row.items() if value is not None},
    }


def _dimension(row: Dict[str, Any], group_by: str) -> str:
    if group_by == "product":
        return _text(row.get("canonical_product_id")) or "unknown"
    if group_by == "variant":
        return _text(row.get("canonical_variant_id")) or "unknown"
    if group_by == "store":
        return _text(row.get("store_id")) or "unknown"
    if group_by == "commerce_surface":
        return _text(row.get("commerce_surface") or row.get("surface")) or "unknown"
    if group_by in {
        "source_channel",
        "source_family",
        "protocol_name",
        "agent_id",
        "query_source",
        "llm_provider",
        "llm_model",
    }:
        return _text(taxonomy_from_row(row).get(group_by)) or "unknown"
    return _text(row.get(group_by)) or "unknown"


def _matches_filters(row: Dict[str, Any], filters: Dict[str, Optional[str]]) -> bool:
    for field, expected in filters.items():
        if not expected:
            continue
        dimension = "store" if field == "store_id" else field
        if _dimension(row, dimension).lower() != _text(expected).lower():
            return False
    return True


# The scope a canonical order_ref lives in. A ref already carries its own
# namespace (`pivota:`, `shopify:`, ...) and is the SAME string from every
# authority that recognised the purchase, so putting it under (platform,
# store) would re-introduce exactly the fragmentation it exists to remove:
# the Stripe bridge and the Shopify webhook report the same purchase under
# platform="shopify" but the attribution edge reports it with no store at all.
_CANONICAL_ORDER_SCOPE = "order_ref"


def _event_scope(row: Dict[str, Any]) -> tuple[str, str]:
    """The namespace this row's native ids live in: (platform, store)."""
    return (
        _text(row.get("platform")).lower() or "unknown",
        _text(row.get("store_id")) or "unknown",
    )


def _order_scope(row: Dict[str, Any]) -> tuple[str, str]:
    """The scope this row's resolved order reference is counted in.

    A row whose reference is a canonical ``order_ref`` — its own, or one
    inherited through the payment/refund/interaction maps — shares one global
    scope with every other authority reporting that purchase. Everything else
    keeps the (platform, store) scope that made native ids comparable.
    """
    if row.get("_resolved_order_is_ref"):
        return (_CANONICAL_ORDER_SCOPE, _CANONICAL_ORDER_SCOPE)
    return _event_scope(row)


def _refund_authority(row: Dict[str, Any]) -> str:
    """Separate PSP and store observations of the same underlying refund.

    The ingress-stamped ``authority`` column is the answer whenever it exists.
    The string inference below survives only for rows written before
    migration 213; ``source`` and ``surface`` are caller-supplied and a
    merchant collector can set them to anything.
    """
    stamped = _text(row.get("authority")).lower()
    if stamped:
        return stamped
    surface = _text(row.get("surface")).lower()
    source = _text(row.get("source")).lower()
    if surface == "psp" or source in {
        "stripe_webhook",
        "adyen_webhook",
        "checkout_webhook",
        "paypal_webhook",
    }:
        return "psp"
    if source.endswith("_webhook") or source.endswith("_adapter"):
        return "store"
    return source or surface or "unknown"


def _attach_resolved_order_ids(rows: List[Dict[str, Any]]) -> None:
    """Give every row the order identity it should be counted under.

    A row's own identity is its canonical ``order_ref`` when it has one, else
    its native ``order_id``. Rows that carry neither — a refund that names only
    a payment — inherit one through the payment/refund/interaction maps.

    The map KEYS are native identifiers, which mean something only inside one
    (platform, store). The map VALUES carry the identity AND whether it is
    canonical, so canonical-ness travels with the row that actually declared an
    ``order_ref``; it is never re-derived from the shape of a string, and a
    native order id that happens to read like a ref cannot borrow that scope.
    """
    Reference = tuple[str, bool]
    interaction_orders: Dict[tuple[str, str, str], Set[Reference]] = defaultdict(set)
    payment_orders: Dict[tuple[str, str, str], Set[Reference]] = defaultdict(set)
    refund_orders: Dict[tuple[str, str, str], Set[Reference]] = defaultdict(set)

    def _own_reference(row: Dict[str, Any]) -> Optional[Reference]:
        order_ref = _text(row.get("order_ref"))
        if order_ref:
            return (order_ref, True)
        order_id = _text(row.get("order_id"))
        if order_id:
            return (order_id, False)
        return None

    for row in rows:
        reference = _own_reference(row)
        if reference is None:
            continue
        platform, store_id = _event_scope(row)
        interaction_id = _text(row.get("interaction_id"))
        payment_id = _text(row.get("payment_id"))
        refund_id = _text(row.get("refund_id"))
        if interaction_id:
            interaction_orders[(platform, store_id, interaction_id)].add(reference)
        if payment_id:
            payment_orders[(platform, store_id, payment_id)].add(reference)
        if refund_id:
            refund_orders[(platform, store_id, refund_id)].add(reference)

    for row in rows:
        platform, store_id = _event_scope(row)
        own = _own_reference(row)
        interaction_id = _text(row.get("interaction_id"))
        candidates: Set[Reference] = set()
        if own is not None:
            candidates.add(own)
        for native_id, mapping in (
            (_text(row.get("payment_id")), payment_orders),
            (_text(row.get("refund_id")), refund_orders),
            (interaction_id, interaction_orders),
        ):
            scoped_reference = (platform, store_id, native_id)
            if native_id and len(mapping.get(scoped_reference, set())) == 1:
                candidates.update(mapping[scoped_reference])
        resolved = own or (next(iter(candidates)) if len(candidates) == 1 else None)
        row["_resolved_order_id"] = resolved[0] if resolved else ""
        row["_resolved_order_is_ref"] = bool(resolved) and resolved[1]


_CART_EVENTS = {
    "cart.created",
    "cart.item_added",
    "cart.item_removed",
    "cart.updated",
}
_CHECKOUT_EVENTS = {"checkout.started", "checkout.submitted"}
_PAYMENT_ATTEMPT_EVENTS = {
    "payment.attempted",
    "payment.authorized",
    "payment.declined",
    "payment.failed",
    "payment.succeeded",
}
_PAID_EVENTS = {"payment.succeeded", "order.paid"}
_ORDER_EVENTS = {"order.created", "order.paid"}
_REFUND_EVENTS = {"refund.created", "refund.succeeded"}
_RETURN_EVENTS = {"return.created", "return.completed"}


@dataclass
class _Accumulator:
    event_types: Counter[str] = field(default_factory=Counter)
    interaction_ids: Set[str] = field(default_factory=set)
    event_ids: Set[str] = field(default_factory=set)
    stage_interactions: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    platforms: Counter[str] = field(default_factory=Counter)
    stores: Counter[str] = field(default_factory=Counter)
    paid_amounts: Dict[str, Dict[tuple[str, str, str], int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    refund_amounts: Dict[str, Dict[tuple[str, str, str, str, str], int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    order_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    paid_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    refund_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    order_ids: Set[str] = field(default_factory=set)
    paid_order_ids: Set[str] = field(default_factory=set)
    refund_order_ids: Set[str] = field(default_factory=set)

    def add(self, row: Dict[str, Any]) -> None:
        event_type = _text(row.get("event_type")).lower()
        event_id = _text(row.get("event_id"))
        interaction_id = _text(row.get("interaction_id")) or event_id
        if not event_type or not interaction_id:
            return

        if event_id:
            self.event_ids.add(event_id)
        self.interaction_ids.add(interaction_id)
        self.event_types[event_type] += 1
        self.platforms[_text(row.get("platform")) or "unknown"] += 1
        self.stores[_text(row.get("store_id")) or "unknown"] += 1

        if event_type == "agent.requested":
            self.stage_interactions["agent_requested"].add(interaction_id)
        if event_type == "search.performed":
            self.stage_interactions["search_performed"].add(interaction_id)
        if event_type == "product.viewed":
            self.stage_interactions["product_viewed"].add(interaction_id)
        if event_type in _CART_EVENTS:
            self.stage_interactions["cart_active"].add(interaction_id)
        if event_type in _CHECKOUT_EVENTS:
            self.stage_interactions["checkout_started"].add(interaction_id)
        if event_type in _PAYMENT_ATTEMPT_EVENTS:
            self.stage_interactions["payment_attempted"].add(interaction_id)
        if event_type == "payment.authorized":
            self.stage_interactions["payment_authorized"].add(interaction_id)
        if event_type in {"payment.declined", "payment.failed"}:
            self.stage_interactions["payment_failed"].add(interaction_id)
        if event_type in _ORDER_EVENTS:
            self.stage_interactions["order_created"].add(interaction_id)
        if event_type in _PAID_EVENTS:
            self.stage_interactions["paid"].add(interaction_id)
        if event_type == "order.cancelled":
            self.stage_interactions["order_cancelled"].add(interaction_id)
        if event_type in _REFUND_EVENTS:
            self.stage_interactions["refund_active"].add(interaction_id)
        if event_type == "refund.succeeded":
            self.stage_interactions["refunded"].add(interaction_id)
        if event_type in _RETURN_EVENTS:
            self.stage_interactions["return_active"].add(interaction_id)
        if event_type == "return.completed":
            self.stage_interactions["return_completed"].add(interaction_id)

        platform, store_id = _order_scope(row)
        resolved_order_id = _text(row.get("_resolved_order_id"))
        order_key = (platform, store_id, resolved_order_id or f"interaction:{interaction_id}")
        # The *_order_ids sets exist for one reader: merchant_commerce_funnel_service
        # subtracts them from the legacy attribution/orders rows, which are keyed
        # on the BARE Pivota order id. A canonical `pivota:<id>` ref must therefore
        # also expose `<id>`, or a Pivota-originated purchase stops cancelling its
        # legacy twin and observed_* counts it twice.
        overlap_ids = {resolved_order_id} if resolved_order_id else set()
        if row.get("_resolved_order_is_ref"):
            namespace, _, native = resolved_order_id.partition(":")
            if namespace == PIVOTA_ORDER_REF_NAMESPACE and native:
                overlap_ids.add(native)
        if event_type in _ORDER_EVENTS:
            self.order_keys.add(order_key)
            self.order_ids.update(overlap_ids)
        if event_type in _PAID_EVENTS:
            self.paid_keys.add(order_key)
            self.paid_order_ids.update(overlap_ids)
        if event_type == "refund.succeeded":
            self.refund_keys.add(order_key)
            self.refund_order_ids.update(overlap_ids)

        currency = _text(row.get("currency")).upper()
        amount_cents = _int(row.get("amount_cents"))
        if not currency or amount_cents is None or amount_cents < 0:
            return
        if event_type in _PAID_EVENTS:
            # A purchase can legitimately receive both payment.succeeded and
            # order.paid. Keep the largest reported total per resolved order
            # instead of double-counting that purchase.
            current = self.paid_amounts[currency].get(order_key, 0)
            self.paid_amounts[currency][order_key] = max(current, amount_cents)
        if event_type == "refund.succeeded":
            order_reference = resolved_order_id or f"interaction:{interaction_id}"
            refund_key = (
                platform,
                store_id,
                order_reference,
                _refund_authority(row),
                _text(row.get("refund_id")) or event_id or interaction_id,
            )
            current = self.refund_amounts[currency].get(refund_key, 0)
            self.refund_amounts[currency][refund_key] = max(current, amount_cents)

    def _refunded_amounts_by_currency(self) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for currency, amounts in self.refund_amounts.items():
            by_order_authority: Dict[tuple[str, str, str], Dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )
            for (platform, store_id, order_reference, authority, _refund_id), amount in amounts.items():
                by_order_authority[(platform, store_id, order_reference)][authority] += amount
            # A PSP and a store platform can both report the same refund with
            # unrelated native IDs. Sum partial refunds inside each authority,
            # then take the largest authority total per order to avoid counting
            # the same money twice.
            totals[currency] = sum(
                max(authority_totals.values(), default=0)
                for authority_totals in by_order_authority.values()
            )
        return dict(sorted(totals.items()))

    def public_summary(self) -> Dict[str, Any]:
        return {
            "events_total": len(self.event_ids),
            "interactions_total": len(self.interaction_ids),
            "stages": {
                stage: len(interactions)
                for stage, interactions in sorted(self.stage_interactions.items())
            },
            "event_type_breakdown": dict(sorted(self.event_types.items())),
            "platform_breakdown": dict(sorted(self.platforms.items())),
            "store_breakdown": dict(sorted(self.stores.items())),
            "paid_amount_cents_by_currency": {
                currency: sum(amounts.values())
                for currency, amounts in sorted(self.paid_amounts.items())
            },
            "refunded_amount_cents_by_currency": self._refunded_amounts_by_currency(),
        }


@dataclass
class CommerceEventFunnelResult:
    payload: Dict[str, Any]
    order_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    paid_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    refund_keys: Set[tuple[str, str, str]] = field(default_factory=set)
    order_ids: Set[str] = field(default_factory=set)
    paid_order_ids: Set[str] = field(default_factory=set)
    refund_order_ids: Set[str] = field(default_factory=set)


def empty_event_funnel_result(
    *,
    limit: Optional[int] = None,
    available: bool = True,
) -> CommerceEventFunnelResult:
    return CommerceEventFunnelResult(
        payload={
            "summary": {
                "events_total": 0,
                "interactions_total": 0,
                "stages": {},
                "event_type_breakdown": {},
                "platform_breakdown": {},
                "store_breakdown": {},
                "paid_amount_cents_by_currency": {},
                "refunded_amount_cents_by_currency": {},
            },
            "slices": [],
            "truncated": False,
            "event_limit": limit or _event_limit(),
            "available": available,
            "unavailable_reason": None if available else "canonical_event_store_unavailable",
        }
    )


async def _fetch_event_rows(
    *,
    merchant_id: str,
    surface: Optional[str],
    platform: Optional[str],
    store_id: Optional[str],
    limit: int,
) -> tuple[List[Dict[str, Any]], bool]:
    if not getattr(database, "is_connected", True):
        raise RuntimeError("database is not connected")
    query = select(commerce_interaction_events).where(
        commerce_interaction_events.c.merchant_id == merchant_id
    )
    if surface:
        query = query.where(commerce_interaction_events.c.surface == surface)
    else:
        # Synthetic production probes remain queryable through an explicit
        # surface=ops_canary request, but must never contribute to a merchant's
        # default funnel stages, order counts, paid GMV, or refund totals.
        # The ingress-stamped `synthetic` column is authoritative; the surface
        # match covers rows written before that column existed. A NULL
        # `synthetic` (pre-migration row) is not evidence of a probe.
        query = query.where(
            or_(
                commerce_interaction_events.c.synthetic.is_(None),
                commerce_interaction_events.c.synthetic == False,  # noqa: E712
            ),
            or_(
                commerce_interaction_events.c.surface.is_(None),
                commerce_interaction_events.c.surface != OPS_CANARY_SURFACE,
            ),
        )
    if platform:
        query = query.where(commerce_interaction_events.c.platform == platform)
    if store_id:
        query = query.where(commerce_interaction_events.c.store_id == store_id)
    query = query.order_by(commerce_interaction_events.c.occurred_at.desc()).limit(limit + 1)
    rows = [_row_to_dict(row) for row in await database.fetch_all(query)]
    return rows[:limit], len(rows) > limit


async def get_merchant_commerce_event_funnel(
    *,
    merchant_id: str,
    group_by: str,
    surface: Optional[str] = None,
    source_channel: Optional[str] = None,
    source_family: Optional[str] = None,
    protocol_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    query_source: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    commerce_surface: Optional[str] = None,
    platform: Optional[str] = None,
    store_id: Optional[str] = None,
) -> CommerceEventFunnelResult:
    limit = _event_limit()
    try:
        raw_rows, truncated = await _fetch_event_rows(
            merchant_id=merchant_id,
            surface=surface,
            platform=platform,
            store_id=store_id,
            limit=limit,
        )
    except Exception as exc:
        # The legacy funnel remains available during a partial schema rollout.
        logger.warning(
            "Canonical commerce event funnel unavailable merchant_id=%s: %s",
            merchant_id,
            exc,
        )
        return empty_event_funnel_result(limit=limit, available=False)

    filters = {
        "source_channel": source_channel,
        "source_family": source_family,
        "protocol_name": protocol_name,
        "agent_id": agent_id,
        "query_source": query_source,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "commerce_surface": commerce_surface,
        "platform": platform,
        "store_id": store_id,
    }
    rows = [
        analytics_row
        for analytics_row in (_analytics_row(row) for row in raw_rows)
        if _matches_filters(analytics_row, filters)
    ]
    _attach_resolved_order_ids(rows)

    total = _Accumulator()
    grouped: Dict[str, _Accumulator] = defaultdict(_Accumulator)
    for row in rows:
        total.add(row)
        grouped[_dimension(row, group_by)].add(row)

    payload = {
        "summary": total.public_summary(),
        "slices": [
            {"key": key, **accumulator.public_summary()}
            for key, accumulator in sorted(grouped.items())
        ],
        "truncated": truncated,
        "event_limit": limit,
        "available": True,
        "unavailable_reason": None,
    }
    return CommerceEventFunnelResult(
        payload=payload,
        order_keys=set(total.order_keys),
        paid_keys=set(total.paid_keys),
        refund_keys=set(total.refund_keys),
        order_ids=set(total.order_ids),
        paid_order_ids=set(total.paid_order_ids),
        refund_order_ids=set(total.refund_order_ids),
    )
