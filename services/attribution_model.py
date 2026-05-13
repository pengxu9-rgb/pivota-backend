"""PR-12 Phase 1: cross-channel attribution model service.

This module intentionally exposes library-shaped compute only. It reads
`funnel_events`, reconstructs conversion paths, and returns dashboard data;
it does not write attribution results or register API routes.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional

logger = logging.getLogger(__name__)

AttributionModel = Literal["last_click", "multi_touch_linear", "time_decay"]

ATTRIBUTION_MODELS = frozenset({
    "last_click",
    "multi_touch_linear",
    "time_decay",
})

# `conversion` is the canonical stage from db.funnel_events.STAGES. The
# aliases keep this reader tolerant of any pre-normalized checkout events.
CONVERSION_STAGES = frozenset({
    "conversion",
    "purchase",
    "order_placed",
    "checkout_completed",
})

_IDENTITY_KEYS = (
    "funnel_path_id",
    "path_id",
    "session_id",
    "sessionId",
    "visitor_id",
    "visitorId",
    "anonymous_id",
    "anonymousId",
    "customer_id",
    "customerId",
    "user_id",
    "userId",
    "cart_id",
    "cartId",
    "checkout_id",
    "checkoutId",
    "interaction_id",
    "interactionId",
    "click_id",
    "clickId",
    "pvt_click_id",
    "PVT_CLICK_ID",
)


@dataclass(frozen=True)
class _FunnelEvent:
    event_id: str
    merchant_id: str
    product_key: Optional[str]
    source_channel: Optional[str]
    stage: str
    occurred_at: datetime
    attribution: Dict[str, Any]
    path_identity: Optional[str]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _empty_result(
    *,
    merchant_id: str,
    product_key: Optional[str],
    model: str,
    lookback_days: int,
    computed_at: datetime,
) -> Dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "product_key": product_key,
        "model": model,
        "lookback_days": lookback_days,
        "attributed_conversions_total": 0,
        "attribution_by_channel": [],
        "sample_size_funnel_paths": 0,
        "computed_at": computed_at.isoformat(),
    }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    get = getattr(row, "get", None)
    if callable(get):
        return get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _coerce_attribution(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_identity(attribution: Dict[str, Any]) -> Optional[str]:
    for key in _IDENTITY_KEYS:
        value = _clean_text(attribution.get(key))
        if value:
            return f"{key}:{value}"
    return None


def _is_conversion(stage: str) -> bool:
    return str(stage or "").strip().lower() in CONVERSION_STAGES


def _normalize_events(
    rows: Iterable[Any],
    *,
    merchant_id: str,
    product_key: Optional[str],
    cutoff: datetime,
) -> List[_FunnelEvent]:
    events: List[_FunnelEvent] = []
    for idx, row in enumerate(rows or []):
        row_merchant_id = _clean_text(_row_get(row, "merchant_id"))
        if row_merchant_id != merchant_id:
            continue
        row_product_key = _clean_text(_row_get(row, "product_key"))
        if product_key is not None and row_product_key != product_key:
            continue
        occurred_at = _coerce_datetime(_row_get(row, "occurred_at"))
        if occurred_at is None or occurred_at < cutoff:
            continue
        stage = _clean_text(_row_get(row, "stage"))
        if not stage:
            continue
        attribution = _coerce_attribution(_row_get(row, "attribution_jsonb"))
        events.append(
            _FunnelEvent(
                event_id=_clean_text(_row_get(row, "event_id")) or f"row-{idx}",
                merchant_id=row_merchant_id,
                product_key=row_product_key,
                source_channel=_clean_text(_row_get(row, "source_channel")),
                stage=stage.lower(),
                occurred_at=occurred_at,
                attribution=attribution,
                path_identity=_extract_identity(attribution),
            )
        )

    return sorted(events, key=lambda event: (event.occurred_at, event.event_id))


def _group_funnel_paths(
    events: List[_FunnelEvent],
    *,
    lookback_days: int,
) -> List[List[_FunnelEvent]]:
    identity_groups: Dict[str, List[_FunnelEvent]] = defaultdict(list)
    fallback_groups: Dict[tuple, List[_FunnelEvent]] = defaultdict(list)

    for event in events:
        if event.path_identity:
            identity_groups[event.path_identity].append(event)
        else:
            fallback_groups[(event.merchant_id, event.product_key)].append(event)

    paths: List[List[_FunnelEvent]] = []
    for grouped_events in identity_groups.values():
        paths.append(sorted(grouped_events, key=lambda event: (event.occurred_at, event.event_id)))

    fallback_gap = timedelta(days=max(1, int(lookback_days)))
    for grouped_events in fallback_groups.values():
        current: List[_FunnelEvent] = []
        previous: Optional[_FunnelEvent] = None
        for event in sorted(grouped_events, key=lambda item: (item.occurred_at, item.event_id)):
            should_split = False
            if previous is not None:
                should_split = (
                    _is_conversion(previous.stage)
                    or event.occurred_at - previous.occurred_at > fallback_gap
                )
            if should_split and current:
                paths.append(current)
                current = []
            current.append(event)
            previous = event
        if current:
            paths.append(current)

    return sorted(paths, key=lambda path: (path[0].occurred_at, path[0].event_id) if path else (datetime.max, ""))


def _touches_for_conversion(
    path: List[_FunnelEvent],
    *,
    conversion: _FunnelEvent,
) -> List[_FunnelEvent]:
    touches = [
        event
        for event in path
        if (
            event.occurred_at <= conversion.occurred_at
            and not _is_conversion(event.stage)
            and event.source_channel
        )
    ]
    if touches:
        return touches
    if conversion.source_channel:
        return [conversion]
    return []


def _time_decay_shares(
    touches: List[_FunnelEvent],
    *,
    conversion: _FunnelEvent,
    half_life_days: float,
) -> List[float]:
    weights: List[float] = []
    for touch in touches:
        age_seconds = max(0.0, (conversion.occurred_at - touch.occurred_at).total_seconds())
        age_days = age_seconds / 86400.0
        weights.append(math.pow(0.5, age_days / half_life_days))
    total_weight = sum(weights)
    if total_weight <= 0.0:
        return []
    return [weight / total_weight for weight in weights]


def _apply_attribution_model(
    path: List[_FunnelEvent],
    *,
    model: str,
    half_life_days: float,
) -> tuple[Dict[str, float], int]:
    credit_by_channel: Dict[str, float] = defaultdict(float)
    attributed_conversions = 0
    conversions = [event for event in path if _is_conversion(event.stage)]

    for conversion in conversions:
        touches = _touches_for_conversion(path, conversion=conversion)
        if not touches:
            continue
        attributed_conversions += 1
        if model == "last_click":
            last_touch = max(touches, key=lambda event: (event.occurred_at, event.event_id))
            credit_by_channel[last_touch.source_channel or "unknown"] += 1.0
            continue
        if model == "multi_touch_linear":
            share = 1.0 / len(touches)
            for touch in touches:
                credit_by_channel[touch.source_channel or "unknown"] += share
            continue
        if model == "time_decay":
            shares = _time_decay_shares(
                touches,
                conversion=conversion,
                half_life_days=half_life_days,
            )
            for touch, share in zip(touches, shares):
                credit_by_channel[touch.source_channel or "unknown"] += share
            continue
        raise ValueError(f"Unsupported attribution model: {model}")

    return dict(credit_by_channel), attributed_conversions


def _format_channel_rows(
    credit_by_channel: Dict[str, float],
    *,
    attributed_conversions_total: int,
) -> List[Dict[str, Any]]:
    if attributed_conversions_total <= 0:
        return []

    rows = []
    for source_channel, credit in sorted(
        credit_by_channel.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        rows.append({
            "source_channel": source_channel,
            "attributed_conversions": round(credit, 6),
            "percentage": round((credit / attributed_conversions_total) * 100.0, 4),
        })
    return rows


async def _fetch_funnel_events(
    *,
    merchant_id: str,
    product_key: Optional[str],
    cutoff: datetime,
) -> List[Any]:
    from db.database import database

    sql_parts = [
        "SELECT event_id, merchant_id, product_key, source_channel, stage, occurred_at, attribution_jsonb",
        "FROM funnel_events",
        "WHERE merchant_id = :merchant_id",
        "  AND occurred_at >= :cutoff",
    ]
    params: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "cutoff": cutoff,
    }
    if product_key is not None:
        sql_parts.append("  AND product_key = :product_key")
        params["product_key"] = product_key
    sql_parts.append("ORDER BY occurred_at ASC, event_id ASC")

    try:
        return list(await database.fetch_all("\n".join(sql_parts), params) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compute_attribution fetch failed for merchant=%s product=%s: %s",
            merchant_id,
            product_key,
            str(exc)[:200],
        )
        return []


async def compute_attribution(
    *,
    merchant_id: str,
    product_key: Optional[str] = None,
    model: AttributionModel,
    lookback_days: int = 30,
    time_decay_half_life_days: float = 7.0,
) -> Dict[str, Any]:
    """Compute cross-channel conversion attribution from funnel_events."""
    model_name = str(model)
    if model_name not in ATTRIBUTION_MODELS:
        raise ValueError(f"Unsupported attribution model: {model_name}")
    half_life_days = float(time_decay_half_life_days)
    if model_name == "time_decay" and half_life_days <= 0.0:
        raise ValueError("time_decay_half_life_days must be positive")

    normalized_lookback_days = max(1, int(lookback_days))
    computed_at = _now_utc()
    cutoff = computed_at - timedelta(days=normalized_lookback_days)
    rows = await _fetch_funnel_events(
        merchant_id=merchant_id,
        product_key=product_key,
        cutoff=cutoff,
    )
    events = _normalize_events(
        rows,
        merchant_id=merchant_id,
        product_key=product_key,
        cutoff=cutoff,
    )
    if not events:
        return _empty_result(
            merchant_id=merchant_id,
            product_key=product_key,
            model=model_name,
            lookback_days=normalized_lookback_days,
            computed_at=computed_at,
        )

    paths = _group_funnel_paths(events, lookback_days=normalized_lookback_days)
    credit_by_channel: Dict[str, float] = defaultdict(float)
    attributed_conversions_total = 0
    sample_size_funnel_paths = 0

    for path in paths:
        if not any(_is_conversion(event.stage) for event in path):
            continue
        sample_size_funnel_paths += 1
        path_credit, path_conversions = _apply_attribution_model(
            path,
            model=model_name,
            half_life_days=half_life_days,
        )
        attributed_conversions_total += path_conversions
        for source_channel, credit in path_credit.items():
            credit_by_channel[source_channel] += credit

    return {
        "merchant_id": merchant_id,
        "product_key": product_key,
        "model": model_name,
        "lookback_days": normalized_lookback_days,
        "attributed_conversions_total": attributed_conversions_total,
        "attribution_by_channel": _format_channel_rows(
            dict(credit_by_channel),
            attributed_conversions_total=attributed_conversions_total,
        ),
        "sample_size_funnel_paths": sample_size_funnel_paths,
        "computed_at": computed_at.isoformat(),
    }
