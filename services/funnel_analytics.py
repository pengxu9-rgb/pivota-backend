"""PR-5: funnel analytics — stage-level conversion rates per channel.

Reads from `funnel_events`. The headline question this answers:
"For channel X over the last N days, what's the impression →
click → conversion drop-off?"

Pure functions where possible (the rollup math is
`compute_stage_conversion_rates(stage_counts)` — no DB), with thin
DB-touching helpers that fetch counts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Canonical stage order for funnel rendering (top of funnel → bottom).
# Channels skip stages they don't surface (e.g. ai_agent goes
# impression → click → conversion; no profile_visit). The renderer
# preserves this order and just zeros stages with no events.
STAGE_ORDER = [
    "impression",
    "profile_visit",
    "click",
    "pdp_view",
    "add_to_cart",
    "conversion",
]


def compute_stage_conversion_rates(
    stage_counts: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Pure function: given {stage: count} mapping, produce the funnel
    rendering with stage-to-stage conversion percentages.

    Returns list of dicts in canonical stage order:
      [
        {"stage": "impression", "count": 1000, "conversion_to_next": 0.30, "drop_off_pct": 0.70},
        {"stage": "click",      "count": 300,  "conversion_to_next": 0.50, "drop_off_pct": 0.50},
        {"stage": "conversion", "count": 150,  "conversion_to_next": null, "drop_off_pct": null},
      ]

    `conversion_to_next` is null for the last stage in the canonical
    order. Stages with zero count still appear (count=0) so the
    rendered funnel keeps a consistent shape.

    No DB I/O — caller fetches the counts and passes them in.
    """
    out: List[Dict[str, Any]] = []
    for i, stage in enumerate(STAGE_ORDER):
        count = int(stage_counts.get(stage) or 0)
        next_stage = STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None
        next_count = int(stage_counts.get(next_stage) or 0) if next_stage else None
        if next_stage is None:
            conversion_to_next: Optional[float] = None
            drop_off_pct: Optional[float] = None
        elif count == 0:
            # Avoid division by zero. When the upstream stage had
            # zero events, downstream conversion is undefined.
            conversion_to_next = None
            drop_off_pct = None
        else:
            conversion_to_next = round(next_count / count, 4)
            drop_off_pct = round(1.0 - conversion_to_next, 4)
        out.append({
            "stage": stage,
            "count": count,
            "conversion_to_next": conversion_to_next,
            "drop_off_pct": drop_off_pct,
        })
    return out


async def fetch_stage_counts(
    *,
    merchant_id: str,
    source_channel: Optional[str] = None,
    window_days: int = 30,
) -> Dict[str, int]:
    """Aggregate funnel_events by stage for one merchant in the
    trailing window. When `source_channel` is provided, filter to
    that channel only; when None, count across all channels.

    Returns `{stage: count}` (missing stages omitted — caller fills
    via compute_stage_conversion_rates).
    """
    from db.database import database
    from db.funnel_events import ensure_funnel_events_table
    await ensure_funnel_events_table()

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    sql_parts = [
        "SELECT stage, COUNT(*) AS n FROM funnel_events",
        "WHERE merchant_id = :merchant_id",
        "  AND occurred_at >= :cutoff",
    ]
    params: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "cutoff": cutoff,
    }
    if source_channel:
        sql_parts.append("  AND source_channel = :source_channel")
        params["source_channel"] = source_channel
    sql_parts.append("GROUP BY stage")
    sql = "\n".join(sql_parts)

    try:
        rows = await database.fetch_all(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fetch_stage_counts failed for merchant=%s channel=%s: %s",
            merchant_id, source_channel, str(exc)[:200],
        )
        return {}
    return {r["stage"]: int(r["n"]) for r in (rows or []) if r.get("stage")}


async def compute_funnel(
    *,
    merchant_id: str,
    source_channel: Optional[str] = None,
    window_days: int = 30,
) -> Dict[str, Any]:
    """End-to-end: fetch stage counts + compute the rollup.

    Returns:
      {
        "merchant_id": ...,
        "source_channel": "ai_agent" | None (None = all channels),
        "window_days": 30,
        "total_events": int,
        "stages": [...],  # see compute_stage_conversion_rates
      }
    """
    counts = await fetch_stage_counts(
        merchant_id=merchant_id,
        source_channel=source_channel,
        window_days=window_days,
    )
    stages = compute_stage_conversion_rates(counts)
    return {
        "merchant_id": merchant_id,
        "source_channel": source_channel,
        "window_days": int(window_days),
        "total_events": sum(counts.values()),
        "stages": stages,
    }


async def channel_breakdown(
    *,
    merchant_id: str,
    window_days: int = 30,
) -> List[Dict[str, Any]]:
    """For the merchant dashboard: total event count per
    source_channel over the window. Lets the operator pick which
    channel to drill into.

    Returns list ordered by count descending:
      [{"source_channel": "ai_agent", "total_events": 1234, ...}]
    """
    from db.database import database
    from db.funnel_events import ensure_funnel_events_table
    await ensure_funnel_events_table()

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    try:
        rows = await database.fetch_all(
            """
            SELECT source_channel, COUNT(*) AS n
            FROM funnel_events
            WHERE merchant_id = :merchant_id
              AND occurred_at >= :cutoff
            GROUP BY source_channel
            ORDER BY n DESC
            """,
            {"merchant_id": merchant_id, "cutoff": cutoff},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "channel_breakdown failed for merchant=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    return [
        {"source_channel": r["source_channel"], "total_events": int(r["n"])}
        for r in (rows or [])
        if r.get("source_channel")
    ]
