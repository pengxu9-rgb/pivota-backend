from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from db.orders import orders
from db.surface_listing_registry import surface_listing_states


def _supported_indexed_statuses() -> tuple[str, ...]:
    return ("exported", "indexed", "tradeable")


async def _fetch_listing_rows(merchant_id: str, surface: Optional[str]) -> List[Dict[str, Any]]:
    query = select(surface_listing_states).where(surface_listing_states.c.merchant_id == merchant_id)
    if surface:
        query = query.where(surface_listing_states.c.surface == surface)
    rows = await database.fetch_all(query)
    return [dict(row) for row in rows]


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
    clicked_exposure = len({row.get("click_id") for row in click_rows if row.get("click_id")})
    total_click_events = sum(int(row.get("click_count") or 0) for row in click_rows)
    ordered_conversion = len({row.get("order_id") for row in edge_rows if row.get("order_id")})
    refunded_orders = len({row.get("order_id") for row in edge_rows if row.get("latest_refund_id")})
    refunded_amount = str(sum(Decimal(str(row.get("refunded_amount") or "0")) for row in edge_rows))

    summary = {
        "indexed_exposure": len({row.get("canonical_variant_id") for row in indexed_rows if row.get("canonical_variant_id")}),
        "surfaced_exposure": None,
        "surfaced_exposure_supported": False,
        "clicked_exposure": clicked_exposure,
        "clicked_events_total": total_click_events,
        "ordered_conversion": ordered_conversion,
        "refunded_orders": refunded_orders,
        "refunded_amount": refunded_amount,
        "listing_status_breakdown": dict(
            defaultdict(
                int,
                {
                    str(row.get("status") or "unknown"): sum(
                        1
                        for candidate in listing_rows
                        if str(candidate.get("status") or "unknown") == str(row.get("status") or "unknown")
                    )
                    for row in listing_rows
                },
            )
        ),
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
                "clicked_exposure": 0,
                "clicked_events_total": 0,
                "ordered_conversion": 0,
                "refunded_orders": 0,
                "refunded_amount": Decimal("0"),
            }
        )

        for row in indexed_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            bucket["indexed_exposure"] += 1

        for row in click_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            bucket["clicked_exposure"] += 1
            bucket["clicked_events_total"] += int(row.get("click_count") or 0)

        for row in edge_rows:
            key = str(row.get(key_field) or "").strip()
            if not key:
                continue
            bucket = grouped[key]
            bucket["key"] = key
            bucket["ordered_conversion"] += 1
            if row.get("latest_refund_id"):
                bucket["refunded_orders"] += 1
            bucket["refunded_amount"] += Decimal(str(row.get("refunded_amount") or "0"))

        slices = {
            key: {
                **value,
                "refunded_amount": str(value["refunded_amount"]),
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
