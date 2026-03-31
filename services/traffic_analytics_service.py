from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from db.database import database
from services.traffic_taxonomy_service import taxonomy_from_row


TRAFFIC_BREAKDOWN_FIELDS = {
    "source_channel",
    "source_family",
    "protocol_name",
    "agent_id",
    "query_source",
    "llm_provider",
    "llm_model",
    "commerce_surface",
}


def _normalize_row(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return dict(row)


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_window(window: str) -> timedelta:
    token = str(window or "30d").strip().lower()
    mapping = {
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }
    return mapping.get(token, timedelta(days=30))


def _dimension_value(row: Dict[str, Any], field: str) -> str:
    return str(taxonomy_from_row(row).get(field) or "unknown")


def _matches_filters(row: Dict[str, Any], filters: Dict[str, Optional[str]]) -> bool:
    for field, expected in filters.items():
        if not expected:
            continue
        if _dimension_value(row, field) != str(expected):
            return False
    return True


async def _fetch_request_rows(
    *,
    start_at: datetime,
    end_at: datetime,
    merchant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = """
        SELECT *
        FROM agent_usage_logs
        WHERE timestamp >= :start_at
          AND timestamp < :end_at
    """
    params: Dict[str, Any] = {"start_at": start_at, "end_at": end_at}
    if merchant_id:
        query += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    rows = await database.fetch_all(query, params)
    return [_normalize_row(row) for row in rows or []]


async def _fetch_click_rows(
    *,
    start_at: datetime,
    end_at: datetime,
    merchant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = """
        SELECT *
        FROM surface_click_events
        WHERE COALESCE(first_click_at, first_impression_at, created_at) >= :start_at
          AND COALESCE(first_click_at, first_impression_at, created_at) < :end_at
    """
    params: Dict[str, Any] = {"start_at": start_at, "end_at": end_at}
    if merchant_id:
        query += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    rows = await database.fetch_all(query, params)
    return [_normalize_row(row) for row in rows or []]


async def _fetch_edge_rows(
    *,
    start_at: datetime,
    end_at: datetime,
    merchant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = """
        SELECT
            cae.*,
            o.total AS order_total,
            o.currency AS order_currency,
            o.created_at AS order_created_at
        FROM commerce_attribution_edges cae
        LEFT JOIN orders o
          ON o.order_id = cae.order_id
        WHERE COALESCE(o.created_at, cae.created_at) >= :start_at
          AND COALESCE(o.created_at, cae.created_at) < :end_at
          AND (o.is_deleted IS NULL OR o.is_deleted = FALSE)
    """
    params: Dict[str, Any] = {"start_at": start_at, "end_at": end_at}
    if merchant_id:
        query += " AND cae.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    rows = await database.fetch_all(query, params)
    return [_normalize_row(row) for row in rows or []]


def _unknown_rate(rows: List[Dict[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    missing = 0
    for row in rows:
        if _dimension_value(row, field) == "unknown":
            missing += 1
    return round(missing / len(rows), 4)


async def build_employee_traffic_overview(
    *,
    window: str = "30d",
    merchant_id: Optional[str] = None,
    filters: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    end_at = _now()
    start_at = end_at - _parse_window(window)
    request_rows, click_rows, edge_rows = await _fetch_request_rows(
        start_at=start_at,
        end_at=end_at,
        merchant_id=merchant_id,
    ), await _fetch_click_rows(
        start_at=start_at,
        end_at=end_at,
        merchant_id=merchant_id,
    ), await _fetch_edge_rows(
        start_at=start_at,
        end_at=end_at,
        merchant_id=merchant_id,
    )

    filter_values = dict(filters or {})
    request_rows = [row for row in request_rows if _matches_filters(row, filter_values)]
    click_rows = [row for row in click_rows if _matches_filters(row, filter_values)]
    edge_rows = [row for row in edge_rows if _matches_filters(row, filter_values)]

    return {
        "window": window,
        "generated_at": end_at.isoformat(),
        "merchant_id": merchant_id,
        "requests_total": len(request_rows),
        "clicked_exposure": len({row.get("click_id") for row in click_rows if row.get("click_id")}),
        "ordered_conversion": len({row.get("order_id") for row in edge_rows if row.get("order_id")}),
        "refunded_orders": len({row.get("order_id") for row in edge_rows if row.get("latest_refund_id")}),
        "gmv_total": str(sum((_to_decimal(row.get("order_total")) for row in edge_rows), Decimal("0"))),
        "refunded_amount_total": str(sum((_to_decimal(row.get("refunded_amount")) for row in edge_rows), Decimal("0"))),
        "unknown_rates": {
            "request_unknown_share": round(
                sum(1 for row in request_rows if _dimension_value(row, "source_channel") == "unknown") / len(request_rows),
                4,
            ) if request_rows else 0.0,
            "click_unknown_share": round(
                sum(1 for row in click_rows if _dimension_value(row, "source_channel") == "unknown") / len(click_rows),
                4,
            ) if click_rows else 0.0,
            "order_unknown_share": round(
                sum(1 for row in edge_rows if _dimension_value(row, "source_channel") == "unknown") / len(edge_rows),
                4,
            ) if edge_rows else 0.0,
            "source_channel_missing_ratio": _unknown_rate(edge_rows or click_rows or request_rows, "source_channel"),
            "protocol_name_missing_ratio": _unknown_rate(edge_rows or click_rows or request_rows, "protocol_name"),
            "query_source_missing_ratio": _unknown_rate(edge_rows or click_rows or request_rows, "query_source"),
            "agent_id_missing_ratio": _unknown_rate(edge_rows or click_rows or request_rows, "agent_id"),
        },
    }


async def build_employee_traffic_breakdown(
    *,
    group_by: str,
    window: str = "30d",
    merchant_id: Optional[str] = None,
    filters: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    if group_by not in TRAFFIC_BREAKDOWN_FIELDS:
        raise ValueError(f"Unsupported group_by: {group_by}")
    end_at = _now()
    start_at = end_at - _parse_window(window)
    filter_values = dict(filters or {})
    request_rows = [row for row in await _fetch_request_rows(start_at=start_at, end_at=end_at, merchant_id=merchant_id) if _matches_filters(row, filter_values)]
    click_rows = [row for row in await _fetch_click_rows(start_at=start_at, end_at=end_at, merchant_id=merchant_id) if _matches_filters(row, filter_values)]
    edge_rows = [row for row in await _fetch_edge_rows(start_at=start_at, end_at=end_at, merchant_id=merchant_id) if _matches_filters(row, filter_values)]

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "key": None,
            "requests_total": 0,
            "clicked_exposure": 0,
            "ordered_conversion": 0,
            "refunded_orders": 0,
            "gmv_total": Decimal("0"),
            "refunded_amount_total": Decimal("0"),
            "_click_ids": set(),
            "_order_ids": set(),
            "_refunded_order_ids": set(),
        }
    )

    for row in request_rows:
        key = _dimension_value(row, group_by)
        bucket = buckets[key]
        bucket["key"] = key
        bucket["requests_total"] += 1

    for row in click_rows:
        key = _dimension_value(row, group_by)
        bucket = buckets[key]
        bucket["key"] = key
        click_id = str(row.get("click_id") or "").strip()
        if click_id and click_id not in bucket["_click_ids"]:
            bucket["_click_ids"].add(click_id)
            bucket["clicked_exposure"] += 1

    for row in edge_rows:
        key = _dimension_value(row, group_by)
        bucket = buckets[key]
        bucket["key"] = key
        order_id = str(row.get("order_id") or "").strip()
        if order_id and order_id not in bucket["_order_ids"]:
            bucket["_order_ids"].add(order_id)
            bucket["ordered_conversion"] += 1
            bucket["gmv_total"] += _to_decimal(row.get("order_total"))
        if row.get("latest_refund_id") and order_id and order_id not in bucket["_refunded_order_ids"]:
            bucket["_refunded_order_ids"].add(order_id)
            bucket["refunded_orders"] += 1
        bucket["refunded_amount_total"] += _to_decimal(row.get("refunded_amount"))

    slices = [
        {
            **value,
            "gmv_total": str(value["gmv_total"]),
            "refunded_amount_total": str(value["refunded_amount_total"]),
        }
        for value in buckets.values()
    ]
    slices.sort(key=lambda item: (-int(item.get("ordered_conversion") or 0), str(item.get("key") or "")))
    return {
        "window": window,
        "generated_at": end_at.isoformat(),
        "merchant_id": merchant_id,
        "group_by": group_by,
        "applied_filters": {key: value for key, value in filter_values.items() if value is not None},
        "slices": slices,
    }


async def build_employee_merchant_traffic(
    *,
    merchant_id: str,
    window: str = "30d",
    filters: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    overview = await build_employee_traffic_overview(
        window=window,
        merchant_id=merchant_id,
        filters=filters,
    )
    breakdown = await build_employee_traffic_breakdown(
        group_by="source_channel",
        window=window,
        merchant_id=merchant_id,
        filters=filters,
    )
    return {
        "merchant_id": merchant_id,
        "overview": overview,
        "breakdown": breakdown,
    }
