from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from services.merchant_catalog_listing_fallback_service import fetch_listing_rows_with_catalog_fallback


def _supported_indexed_statuses() -> tuple[str, ...]:
    return ("exported", "indexed", "tradeable")


async def _fetch_listing_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    return await fetch_listing_rows_with_catalog_fallback(merchant_id, surface)


async def _fetch_click_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(surface_click_events).where(surface_click_events.c.merchant_id == merchant_id)
    if surface:
        query = query.where(surface_click_events.c.surface == surface)
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows]


async def _fetch_edge_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(commerce_attribution_edges).where(commerce_attribution_edges.c.merchant_id == merchant_id)
    if surface:
        query = query.where(commerce_attribution_edges.c.surface == surface)
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows]


async def get_merchant_commerce_funnel(
    *,
    merchant_id: str,
    surface: Optional[str] = None,
    group_by: str = "product",
) -> Dict[str, Any]:
    listing_rows = await _fetch_listing_rows(merchant_id, surface)
    click_rows = await _fetch_click_rows(merchant_id, surface)
    edge_rows = await _fetch_edge_rows(merchant_id, surface)

    indexed_statuses = set(_supported_indexed_statuses())
    indexed_rows = [row for row in listing_rows if str(row.get("status") or "").strip().lower() in indexed_statuses]
    surfaced_exposure = len({row.get("click_id") for row in click_rows if row.get("click_id") and int(row.get("impression_count") or 0) > 0})
    clicked_exposure = len({row.get("click_id") for row in click_rows if row.get("click_id")})
    total_click_events = sum(int(row.get("click_count") or 0) for row in click_rows)
    ordered_conversion = len({row.get("order_id") for row in edge_rows if row.get("order_id")})
    refunded_orders = len({row.get("order_id") for row in edge_rows if row.get("latest_refund_id")})
    refunded_amount = str(sum(Decimal(str(row.get("refunded_amount") or "0")) for row in edge_rows))
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
        "refunded_orders": refunded_orders,
        "refunded_amount": refunded_amount,
        "clicked_rate": (clicked_exposure / clicked_rate_denominator) if clicked_rate_denominator else 0,
        "ordered_rate": (ordered_conversion / ordered_rate_denominator) if ordered_rate_denominator else 0,
        "listing_rows_total": len(listing_rows),
        "listing_status_breakdown": listing_status_breakdown,
        "listing_status_breakdown_rows": listing_status_breakdown,
        "listing_status_breakdown_by_surface": listing_status_breakdown_by_surface,
    }

    slices: Dict[str, Dict[str, Any]] = {}
    if group_by in {"product", "variant", "surface"}:
        key_field = {
            "product": "canonical_product_id",
            "variant": "canonical_variant_id",
            "surface": "surface",
        }[group_by]
        grouped: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "key": None,
                "indexed_exposure": 0,
                "surfaced_exposure": 0,
                "clicked_exposure": 0,
                "clicked_events_total": 0,
                "ordered_conversion": 0,
                "refunded_orders": 0,
                "refunded_amount": Decimal("0"),
                "clicked_rate": 0,
                "ordered_rate": 0,
                "listing_rows_total": 0,
                "listing_status_breakdown_rows": defaultdict(int),
                "listing_status_breakdown_by_surface": defaultdict(lambda: defaultdict(int)),
                "_indexed_variant_ids": set(),
                "_surfaced_click_ids": set(),
                "_click_ids": set(),
                "_order_ids": set(),
                "_refunded_order_ids": set(),
            }
        )

        for row in listing_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            bucket["listing_rows_total"] += 1
            status_key = str(row.get("status") or "unknown")
            surface_key = str(row.get("surface") or "unknown")
            bucket["listing_status_breakdown_rows"][status_key] += 1
            bucket["listing_status_breakdown_by_surface"][surface_key][status_key] += 1

        for row in indexed_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            variant_id = str(row.get("canonical_variant_id") or "").strip()
            if variant_id and variant_id not in bucket["_indexed_variant_ids"]:
                bucket["_indexed_variant_ids"].add(variant_id)
                bucket["indexed_exposure"] += 1

        for row in click_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            click_id = str(row.get("click_id") or "").strip()
            if click_id and int(row.get("impression_count") or 0) > 0 and click_id not in bucket["_surfaced_click_ids"]:
                bucket["_surfaced_click_ids"].add(click_id)
                bucket["surfaced_exposure"] += 1
            if click_id and click_id not in bucket["_click_ids"]:
                bucket["_click_ids"].add(click_id)
                bucket["clicked_exposure"] += 1
            bucket["clicked_events_total"] += int(row.get("click_count") or 0)

        for row in edge_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            order_id = str(row.get("order_id") or "").strip()
            if order_id and order_id not in bucket["_order_ids"]:
                bucket["_order_ids"].add(order_id)
                bucket["ordered_conversion"] += 1
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

    return {
        "merchant_id": merchant_id,
        "surface": surface,
        "summary": summary,
        "group_by": group_by,
        "slices": list(slices.values()),
    }
