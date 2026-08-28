from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from db.orders import orders
from services.merchant_catalog_listing_fallback_service import fetch_listing_rows_with_catalog_fallback
from services.merchant_commerce_event_funnel_service import get_merchant_commerce_event_funnel
from services.traffic_taxonomy_service import taxonomy_from_row


SUPPORTED_COMMERCE_FUNNEL_GROUP_BYS = {
    "product",
    "variant",
    "surface",
    "commerce_surface",
    "source_channel",
    "source_family",
    "protocol_name",
    "agent_id",
    "query_source",
    "llm_provider",
    "llm_model",
    "platform",
    "store",
}


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return dict(mapping)
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return {}


def _supported_indexed_statuses() -> tuple[str, ...]:
    return ("exported", "indexed", "tradeable")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


_PAID_PAYMENT_STATUSES = {
    "paid",
    "completed",
    "succeeded",
    "success",
    "settled",
    "partially_refunded",
    "refunded",
}

_PAID_ORDER_STATUSES = {
    "paid",
    "completed",
    "fulfilled",
}


def _is_paid_order(row: Dict[str, Any]) -> bool:
    payment_status = _normalize_text(row.get("payment_status")).lower()
    status = _normalize_text(row.get("status")).lower()
    return payment_status in _PAID_PAYMENT_STATUSES or status in _PAID_ORDER_STATUSES


def _listing_key_aliases(row: Dict[str, Any], *, key_field: str) -> set[str]:
    aliases: set[str] = set()
    key = _normalize_text(row.get(key_field))
    if key:
        aliases.add(key)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if key_field == "canonical_product_id":
        for alias_key in ("catalog_product_key", "product_key"):
            alias = _normalize_text(metadata.get(alias_key))
            if alias:
                aliases.add(alias)
    elif key_field == "canonical_variant_id":
        for alias_key in ("catalog_sku_key", "sku_key"):
            alias = _normalize_text(metadata.get(alias_key))
            if alias:
                aliases.add(alias)
    return aliases


def _resolve_bucket_key(row: Dict[str, Any], *, key_field: str, alias_to_bucket: Dict[str, str]) -> str:
    raw = _normalize_text(row.get(key_field))
    if not raw:
        return ""
    return alias_to_bucket.get(raw, raw)


def _observed_fields(event_summary: Dict[str, Any]) -> Dict[str, Any]:
    stages = event_summary.get("stages") if isinstance(event_summary.get("stages"), dict) else {}
    return {
        "observed_product_views": int(stages.get("product_viewed") or 0),
        "observed_cart_interactions": int(stages.get("cart_active") or 0),
        "observed_checkouts": int(stages.get("checkout_started") or 0),
        "observed_payment_attempts": int(stages.get("payment_attempted") or 0),
        "observed_orders": int(stages.get("order_created") or 0),
        "observed_paid_interactions": int(stages.get("paid") or 0),
        "observed_refunds": int(stages.get("refunded") or 0),
    }


def _empty_legacy_slice(key: str) -> Dict[str, Any]:
    return {
        "key": key,
        "indexed_exposure": 0,
        "surfaced_exposure": 0,
        "clicked_exposure": 0,
        "clicked_events_total": 0,
        "ordered_conversion": 0,
        "attributed_orders": 0,
        "paid_conversion": 0,
        "refunded_orders": 0,
        "refunded_amount": "0",
        "clicked_rate": 0,
        "ordered_rate": 0,
        "paid_order_rate": 0,
        "listing_rows_total": 0,
        "listing_status_breakdown_rows": {},
        "listing_status_breakdown_by_surface": {},
    }


async def _fetch_listing_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    return await fetch_listing_rows_with_catalog_fallback(merchant_id, surface)


async def _fetch_click_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(surface_click_events).where(surface_click_events.c.merchant_id == merchant_id)
    if surface:
        query = query.where(surface_click_events.c.surface == surface)
    rows = await database.fetch_all(query)
    return [_row_to_dict(row) for row in rows or []]


async def _fetch_edge_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(commerce_attribution_edges).where(commerce_attribution_edges.c.merchant_id == merchant_id)
    if surface:
        query = query.where(commerce_attribution_edges.c.surface == surface)
    rows = await database.fetch_all(query)
    return [_row_to_dict(row) for row in rows or []]


async def _fetch_order_rows(merchant_id: str, order_ids: List[str]) -> List[Dict[str, Any]]:
    normalized_order_ids = [order_id for order_id in {_normalize_text(value) for value in order_ids} if order_id]
    if not normalized_order_ids:
        return []
    query = (
        select(orders.c.order_id, orders.c.status, orders.c.payment_status)
        .where(orders.c.merchant_id == merchant_id)
        .where(orders.c.order_id.in_(normalized_order_ids))
        .where(or_(orders.c.is_deleted.is_(None), orders.c.is_deleted.is_(False)))
    )
    rows = await database.fetch_all(query)
    return [_row_to_dict(row) for row in rows or []]


async def get_merchant_commerce_funnel(
    *,
    merchant_id: str,
    surface: Optional[str] = None,
    group_by: str = "product",
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
) -> Dict[str, Any]:
    platform = _normalize_text(platform).lower() or None
    store_id = _normalize_text(store_id) or None
    resolved_surface = surface or commerce_surface
    unsupported_legacy_filters = [
        field
        for field, value in (("platform", platform), ("store_id", store_id))
        if value is not None
    ]
    if unsupported_legacy_filters:
        # The legacy tables cannot safely distinguish stores or platforms.
        # Fail closed rather than mixing unscoped listings/attribution into a
        # precisely scoped canonical event response.
        listing_rows: List[Dict[str, Any]] = []
        click_rows: List[Dict[str, Any]] = []
        edge_rows: List[Dict[str, Any]] = []
    else:
        listing_rows = await _fetch_listing_rows(merchant_id, resolved_surface)
        click_rows = await _fetch_click_rows(merchant_id, resolved_surface)
        edge_rows = await _fetch_edge_rows(merchant_id, resolved_surface)

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
    event_funnel = await get_merchant_commerce_event_funnel(
        merchant_id=merchant_id,
        group_by=group_by,
        surface=surface,
        source_channel=source_channel,
        source_family=source_family,
        protocol_name=protocol_name,
        agent_id=agent_id,
        query_source=query_source,
        llm_provider=llm_provider,
        llm_model=llm_model,
        commerce_surface=commerce_surface,
        platform=platform,
        store_id=store_id,
    )

    def _row_dimension(row: Dict[str, Any], field: str) -> str:
        if field == "commerce_surface":
            return _normalize_text(row.get("commerce_surface") or row.get("surface")) or "unknown"
        if field == "store_id":
            return _normalize_text(row.get("store_id")) or "unknown"
        if field in {
            "source_channel",
            "source_family",
            "protocol_name",
            "agent_id",
            "query_source",
            "llm_provider",
            "llm_model",
        }:
            return _normalize_text(taxonomy_from_row(row).get(field)) or "unknown"
        return _normalize_text(row.get(field)) or "unknown"

    def _matches_filters(row: Dict[str, Any]) -> bool:
        for field, expected in filters.items():
            if not expected:
                continue
            if _row_dimension(row, field) != _normalize_text(expected):
                return False
        return True

    filtered_click_rows = [row for row in click_rows if _matches_filters(row)]
    filtered_edge_rows = [row for row in edge_rows if _matches_filters(row)]
    order_rows = await _fetch_order_rows(
        merchant_id,
        [_normalize_text(row.get("order_id")) for row in filtered_edge_rows],
    )
    paid_order_ids = {
        _normalize_text(row.get("order_id"))
        for row in order_rows
        if _normalize_text(row.get("order_id")) and _is_paid_order(row)
    }

    indexed_statuses = set(_supported_indexed_statuses())
    indexed_rows = [row for row in listing_rows if str(row.get("status") or "").strip().lower() in indexed_statuses]
    surfaced_exposure = len({row.get("click_id") for row in filtered_click_rows if row.get("click_id") and int(row.get("impression_count") or 0) > 0})
    clicked_exposure = len(
        {
            row.get("click_id")
            for row in filtered_click_rows
            if row.get("click_id") and int(row.get("click_count") or 0) > 0
        }
    )
    total_click_events = sum(int(row.get("click_count") or 0) for row in filtered_click_rows)
    ordered_conversion = len({row.get("order_id") for row in filtered_edge_rows if row.get("order_id")})
    paid_conversion = len(paid_order_ids)
    refunded_orders = len({row.get("order_id") for row in filtered_edge_rows if row.get("latest_refund_id")})
    refunded_amount = str(sum(Decimal(str(row.get("refunded_amount") or "0")) for row in filtered_edge_rows))
    listing_status_breakdown = dict(Counter(str(row.get("status") or "unknown") for row in listing_rows))
    listing_status_breakdown_by_surface: Dict[str, Dict[str, int]] = {}
    for row in listing_rows:
        surface_key = str(row.get("surface") or "unknown")
        surface_bucket = listing_status_breakdown_by_surface.setdefault(surface_key, {})
        status_key = str(row.get("status") or "unknown")
        surface_bucket[status_key] = int(surface_bucket.get(status_key, 0)) + 1

    clicked_rate_denominator = surfaced_exposure or len({row.get("canonical_variant_id") for row in indexed_rows if row.get("canonical_variant_id")})
    ordered_rate_denominator = clicked_exposure or 0

    summary = {
        "indexed_exposure": len({row.get("canonical_variant_id") for row in indexed_rows if row.get("canonical_variant_id")}),
        "surfaced_exposure": surfaced_exposure,
        "surfaced_exposure_supported": True,
        "clicked_exposure": clicked_exposure,
        "clicked_events_total": total_click_events,
        "ordered_conversion": ordered_conversion,
        "attributed_orders": ordered_conversion,
        "paid_conversion": paid_conversion,
        "refunded_orders": refunded_orders,
        "refunded_amount": refunded_amount,
        "clicked_rate": (clicked_exposure / clicked_rate_denominator) if clicked_rate_denominator else 0,
        "ordered_rate": (ordered_conversion / ordered_rate_denominator) if ordered_rate_denominator else 0,
        "paid_order_rate": (paid_conversion / ordered_rate_denominator) if ordered_rate_denominator else 0,
        "listing_rows_total": len(listing_rows),
        "listing_status_breakdown": listing_status_breakdown,
        "listing_status_breakdown_rows": listing_status_breakdown,
        "listing_status_breakdown_by_surface": listing_status_breakdown_by_surface,
    }
    legacy_order_ids = {
        _normalize_text(row.get("order_id"))
        for row in filtered_edge_rows
        if _normalize_text(row.get("order_id"))
    }
    legacy_refunded_order_ids = {
        _normalize_text(row.get("order_id"))
        for row in filtered_edge_rows
        if _normalize_text(row.get("order_id")) and row.get("latest_refund_id")
    }
    ledger_summary = event_funnel.payload.get("summary") or {}
    summary.update(
        {
            **_observed_fields(ledger_summary),
            "ledger_events_total": int(ledger_summary.get("events_total") or 0),
            "ledger_interactions_total": int(ledger_summary.get("interactions_total") or 0),
            "observed_order_conversion": (
                len(event_funnel.order_keys)
                + len(legacy_order_ids - event_funnel.order_ids)
            ),
            "observed_paid_conversion": (
                len(event_funnel.paid_keys)
                + len(paid_order_ids - event_funnel.paid_order_ids)
            ),
            "observed_refunded_orders": (
                len(event_funnel.refund_keys)
                + len(legacy_refunded_order_ids - event_funnel.refund_order_ids)
            ),
        }
    )

    slices: Dict[str, Dict[str, Any]] = {}
    if group_by in {"product", "variant", "surface"}:
        key_field = {
            "product": "canonical_product_id",
            "variant": "canonical_variant_id",
            "surface": "surface",
        }[group_by]
        alias_to_bucket: Dict[str, str] = {}
        grouped: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "key": None,
                "indexed_exposure": 0,
                "surfaced_exposure": 0,
                "clicked_exposure": 0,
                "clicked_events_total": 0,
                "ordered_conversion": 0,
                "attributed_orders": 0,
                "paid_conversion": 0,
                "refunded_orders": 0,
                "refunded_amount": Decimal("0"),
                "clicked_rate": 0,
                "ordered_rate": 0,
                "paid_order_rate": 0,
                "listing_rows_total": 0,
                "listing_status_breakdown_rows": defaultdict(int),
                "listing_status_breakdown_by_surface": defaultdict(lambda: defaultdict(int)),
                "_indexed_variant_ids": set(),
                "_surfaced_click_ids": set(),
                "_click_ids": set(),
                "_order_ids": set(),
                "_paid_order_ids": set(),
                "_refunded_order_ids": set(),
            }
        )

        for row in listing_rows:
            key = _normalize_text(row.get(key_field))
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            for alias in _listing_key_aliases(row, key_field=key_field):
                alias_to_bucket[alias] = key
            bucket["listing_rows_total"] += 1
            status_key = _normalize_text(row.get("status")) or "unknown"
            surface_key = _normalize_text(row.get("surface")) or "unknown"
            bucket["listing_status_breakdown_rows"][status_key] += 1
            bucket["listing_status_breakdown_by_surface"][surface_key][status_key] += 1

        for row in indexed_rows:
            key = _resolve_bucket_key(row, key_field=key_field, alias_to_bucket=alias_to_bucket)
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            variant_id = _normalize_text(row.get("canonical_variant_id"))
            if variant_id and variant_id not in bucket["_indexed_variant_ids"]:
                bucket["_indexed_variant_ids"].add(variant_id)
                bucket["indexed_exposure"] += 1

        for row in filtered_click_rows:
            key = _resolve_bucket_key(row, key_field=key_field, alias_to_bucket=alias_to_bucket)
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            click_id = _normalize_text(row.get("click_id"))
            if click_id and int(row.get("impression_count") or 0) > 0 and click_id not in bucket["_surfaced_click_ids"]:
                bucket["_surfaced_click_ids"].add(click_id)
                bucket["surfaced_exposure"] += 1
            if click_id and int(row.get("click_count") or 0) > 0 and click_id not in bucket["_click_ids"]:
                bucket["_click_ids"].add(click_id)
                bucket["clicked_exposure"] += 1
            bucket["clicked_events_total"] += int(row.get("click_count") or 0)

        for row in filtered_edge_rows:
            key = _resolve_bucket_key(row, key_field=key_field, alias_to_bucket=alias_to_bucket)
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            order_id = _normalize_text(row.get("order_id"))
            if order_id and order_id not in bucket["_order_ids"]:
                bucket["_order_ids"].add(order_id)
                bucket["ordered_conversion"] += 1
                bucket["attributed_orders"] += 1
            if order_id and order_id in paid_order_ids and order_id not in bucket["_paid_order_ids"]:
                bucket["_paid_order_ids"].add(order_id)
                bucket["paid_conversion"] += 1
            if row.get("latest_refund_id") and order_id and order_id not in bucket["_refunded_order_ids"]:
                bucket["_refunded_order_ids"].add(order_id)
                bucket["refunded_orders"] += 1
            bucket["refunded_amount"] += Decimal(str(row.get("refunded_amount") or "0"))

        slices = {
            key: {
                **value,
                "clicked_rate": (
                    value["clicked_exposure"] / (value["surfaced_exposure"] or value["indexed_exposure"])
                    if (value["surfaced_exposure"] or value["indexed_exposure"])
                    else 0
                ),
                "ordered_rate": (
                    value["ordered_conversion"] / value["clicked_exposure"]
                    if value["clicked_exposure"]
                    else 0
                ),
                "paid_order_rate": (
                    value["paid_conversion"] / value["clicked_exposure"]
                    if value["clicked_exposure"]
                    else 0
                ),
                "refunded_amount": str(value["refunded_amount"]),
                "listing_rows_total": value["listing_rows_total"],
                "listing_status_breakdown_rows": dict(value["listing_status_breakdown_rows"]),
                "listing_status_breakdown_by_surface": {
                    surface_key: dict(status_counts)
                    for surface_key, status_counts in value["listing_status_breakdown_by_surface"].items()
                },
            }
            for key, value in grouped.items()
        }
    elif group_by in SUPPORTED_COMMERCE_FUNNEL_GROUP_BYS - {"platform", "store"}:
        grouped: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "key": None,
                "indexed_exposure": 0,
                "surfaced_exposure": 0,
                "clicked_exposure": 0,
                "clicked_events_total": 0,
                "ordered_conversion": 0,
                "attributed_orders": 0,
                "paid_conversion": 0,
                "refunded_orders": 0,
                "refunded_amount": Decimal("0"),
                "clicked_rate": 0,
                "ordered_rate": 0,
                "paid_order_rate": 0,
                "listing_rows_total": 0,
                "listing_status_breakdown_rows": {},
                "listing_status_breakdown_by_surface": {},
                "_surfaced_click_ids": set(),
                "_click_ids": set(),
                "_order_ids": set(),
                "_paid_order_ids": set(),
                "_refunded_order_ids": set(),
            }
        )

        for row in filtered_click_rows:
            key = _row_dimension(row, group_by) or "unknown"
            bucket = grouped[key]
            bucket["key"] = key
            click_id = _normalize_text(row.get("click_id"))
            if click_id and int(row.get("impression_count") or 0) > 0 and click_id not in bucket["_surfaced_click_ids"]:
                bucket["_surfaced_click_ids"].add(click_id)
                bucket["surfaced_exposure"] += 1
            if click_id and int(row.get("click_count") or 0) > 0 and click_id not in bucket["_click_ids"]:
                bucket["_click_ids"].add(click_id)
                bucket["clicked_exposure"] += 1
            bucket["clicked_events_total"] += int(row.get("click_count") or 0)

        for row in filtered_edge_rows:
            key = _row_dimension(row, group_by) or "unknown"
            bucket = grouped[key]
            bucket["key"] = key
            order_id = _normalize_text(row.get("order_id"))
            if order_id and order_id not in bucket["_order_ids"]:
                bucket["_order_ids"].add(order_id)
                bucket["ordered_conversion"] += 1
                bucket["attributed_orders"] += 1
            if order_id and order_id in paid_order_ids and order_id not in bucket["_paid_order_ids"]:
                bucket["_paid_order_ids"].add(order_id)
                bucket["paid_conversion"] += 1
            if row.get("latest_refund_id") and order_id and order_id not in bucket["_refunded_order_ids"]:
                bucket["_refunded_order_ids"].add(order_id)
                bucket["refunded_orders"] += 1
            bucket["refunded_amount"] += Decimal(str(row.get("refunded_amount") or "0"))

        slices = {
            key: {
                **value,
                "clicked_rate": (
                    value["clicked_exposure"] / value["surfaced_exposure"]
                    if value["surfaced_exposure"]
                    else 0
                ),
                "ordered_rate": (
                    value["ordered_conversion"] / value["clicked_exposure"]
                    if value["clicked_exposure"]
                    else 0
                ),
                "paid_order_rate": (
                    value["paid_conversion"] / value["clicked_exposure"]
                    if value["clicked_exposure"]
                    else 0
                ),
                "refunded_amount": str(value["refunded_amount"]),
            }
            for key, value in grouped.items()
        }

    for event_slice in event_funnel.payload.get("slices") or []:
        key = _normalize_text(event_slice.get("key")) or "unknown"
        slice_payload = slices.get(key) or _empty_legacy_slice(key)
        slice_payload.update(_observed_fields(event_slice))
        slice_payload["event_funnel"] = {
            field: value for field, value in event_slice.items() if field != "key"
        }
        slices[key] = slice_payload

    return {
        "merchant_id": merchant_id,
        "surface": surface,
        "summary": summary,
        "group_by": group_by,
        "applied_filters": {key: value for key, value in filters.items() if value is not None},
        "metric_scopes": {
            "legacy_attribution": {
                "included": not unsupported_legacy_filters,
                "slices_grouped": (
                    not unsupported_legacy_filters and group_by not in {"platform", "store"}
                ),
                "unsupported_filters": unsupported_legacy_filters,
                "reason": (
                    "Legacy click and attribution rows do not carry a reliable platform/store identity; "
                    "legacy metrics are excluded instead of being assigned to the wrong store."
                    if unsupported_legacy_filters
                    else None
                ),
            },
            "canonical_events": {
                "included": bool(event_funnel.payload.get("available", True)),
                "scoped_filters": [
                    field
                    for field in ("platform", "store_id")
                    if filters.get(field) is not None
                ],
            },
        },
        "slices": list(slices.values()),
        "event_funnel": event_funnel.payload,
    }
